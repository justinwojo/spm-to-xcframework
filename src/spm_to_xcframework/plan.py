"""Phase 2: pure planner.

`plan_source_build` and `plan_binary_build` are pure functions over
(Config, Package | List[BinaryArtifact]) → Plan. No filesystem
mutations, no subprocesses, no input mutation. All downstream
decisions flow from here, so keeping this layer side-effect-free is
load-bearing: tests can exercise any planning rule with hand-rolled
fixtures and trust that production behaviour matches.

See REWRITE_DESIGN.md §5.2 for the rule set.
"""
from __future__ import annotations

import os
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .config import Config
from .errors import PlanError
from .inspect import _raw_internal_dep_names
from .log import bold
from .model import (
    BinaryArtifact,
    BuildUnit,
    Language,
    Linkage,
    Package,
    PackageSwiftEdit,
    Plan,
    Product,
    TargetKind,
)
from .platforms import _enabled_platforms, _selected_slices


def resolve_scheme(product_name: str, schemes: Sequence[str]) -> str:
    """Pick the best scheme for `product_name` out of the discovered list.

    Resolution order (design §5.2 rule 4, plus the session-1 inversion
    noted in the Session 2 brief: prefer the literal name over the
    `-Package` form when both exist):

      1. exact match
      2. case-insensitive exact match
      3. `<product>-Package`
      4. `<product> iOS`, `<product>-iOS`, `<product> (iOS)`
      5. fall back to `product_name` unchanged — xcodebuild will
         auto-generate the SPM scheme at build time, which works
         against our clean staged directory.
    """
    if not schemes:
        return product_name
    # 1. exact
    if product_name in schemes:
        return product_name
    # 2. case-insensitive exact
    lowered = {s.lower(): s for s in schemes}
    if product_name.lower() in lowered:
        return lowered[product_name.lower()]
    # 3. <product>-Package
    pkg_form = f"{product_name}-Package"
    if pkg_form in schemes:
        return pkg_form
    # 4. iOS-suffix variants
    for suffix in (" iOS", "-iOS", " (iOS)"):
        candidate = product_name + suffix
        if candidate in schemes:
            return candidate
    # 5. fall back to product name
    return product_name


def _derive_product_language(product: Product, package: Package) -> str:
    """Union of all backing target languages → one of Language._VALUES.

    Rules (design §5.2 item 6):
      Swift + Swift        → Swift
      ObjC  + ObjC         → ObjC
      Swift + ObjC         → Mixed
      anything + Mixed     → Mixed
      nothing classified   → N/A
    """
    has_swift = False
    has_objc = False
    has_mixed = False
    for tname in product.targets:
        t = package.target_by_name(tname)
        if t is None:
            continue
        if t.language == Language.SWIFT:
            has_swift = True
        elif t.language == Language.OBJC:
            has_objc = True
        elif t.language == Language.MIXED:
            has_mixed = True
    if has_mixed or (has_swift and has_objc):
        return Language.MIXED
    if has_swift:
        return Language.SWIFT
    if has_objc:
        return Language.OBJC
    return Language.NA


def _is_system_only_product(product: Product, package: Package) -> bool:
    """True iff every backing target of `product` is TargetKind.SYSTEM.

    This is the rule for dropping a product entirely (design §5.2 rule 2 +
    bug 5 in SPM_TO_XCFRAMEWORK_NOTES.md). An empty target list is NOT
    considered system-only — that's a malformed product, not a system
    wrapper, and we leave it to xcodebuild to complain.
    """
    if not product.targets:
        return False
    for tname in product.targets:
        t = package.target_by_name(tname)
        if t is None or t.kind != TargetKind.SYSTEM:
            return False
    return True


def _is_binary_only_product(product: Product, package: Package) -> bool:
    """True iff every backing target of `product` is TargetKind.BINARY.

    Such a product is backed entirely by `binaryTarget(...)` entries —
    SPM does not allow an explicit `type: .dynamic` on it ("invalid type
    for binary product; products referencing only binary targets must be
    executable or automatic library products"). The source-mode planner
    uses this to suppress the synthetic-dynamic-library injection for
    those products, and `_package_is_binary_only` uses it to recognise
    packages that
    should be handled by `--binary` mode instead of source mode.
    Empty target list returns False — same convention as
    `_is_system_only_product` (treat malformed products as not-binary
    and let xcodebuild surface the real problem).
    """
    if not product.targets:
        return False
    for tname in product.targets:
        t = package.target_by_name(tname)
        if t is None or t.kind != TargetKind.BINARY:
            return False
    return True


def _package_is_binary_only(package: Package) -> bool:
    """True iff every (non-system) product in `package` is backed only by
    binaryTarget targets.

    Source-mode planning for such a package would fail at SPM resolve
    time because the unconditional synthetic-dynamic-library injection
    produces `.library(..., type: .dynamic, targets: [<binary>])`, which
    SPM rejects. The caller switches transparently to `--binary` mode when
    this returns True. System-only products are skipped from the check
    (the planner drops them anyway), so a package with one binary
    library + one system-shim library still counts as binary-only.
    Packages with zero products return False — there's nothing for
    binary mode to discover either.
    """
    binary_products = 0
    for product in package.products:
        if _is_system_only_product(product, package):
            continue
        if not _is_binary_only_product(product, package):
            return False
        binary_products += 1
    return binary_products > 0


def compute_internal_target_deps(package: Package) -> Dict[str, Set[str]]:
    """For every target in `package`, return the set of TRANSITIVELY
    reachable internal target names — i.e. names that are also defined
    as targets in the same `Package.swift`.

    Walks the raw `dump-package` payload (`package.raw_dump`) rather
    than the typed `Target.dependencies` list, because the typed
    flattening (`_parse_dependencies`) collapses `byName`, `target`,
    AND `product` shapes into a single name list. That collapse loses
    the kind information we need here: a `.product("Foo", ...)`
    cross-package import has the same flat-name shape as a
    `.byName("Foo")` internal sibling, so a name collision between an
    external SPM product and a local target name would silently
    register a false internal edge. (Codex P2 regression.)

    Internal-only walk: only `byName` and `target` shapes from the raw
    dump count — those are the SPM dependency expressions that
    definitionally point inside the same package. `product` deps are
    skipped even if their name happens to match an internal target.
    External non-collision `product` deps are also filtered by virtue
    of the membership check against `internal_names`.

    Cycles are tolerated (the dump-package model shouldn't permit
    them, but the visited set guards against bad inputs anyway). The
    reflexive self-edge is excluded from the returned set so callers
    can iterate `deps_map[T]` without a separate "is this myself"
    check.

    Pure function over typed inputs — used by `topo_order_units` and
    the Execute-time dedup-overlap substitution loop.
    """
    internal_names = {t.name for t in package.targets}

    raw_targets_by_name: Dict[str, dict] = {}
    for raw_t in package.raw_dump.get("targets", []) or []:
        if not isinstance(raw_t, dict):
            continue
        nm = raw_t.get("name")
        if isinstance(nm, str):
            raw_targets_by_name[nm] = raw_t

    direct: Dict[str, List[str]] = {}
    for t in package.targets:
        raw_t = raw_targets_by_name.get(t.name)
        if raw_t is None:
            # Target was in the typed list but not in the raw dump — a
            # synthetic-library injection or test-fixture quirk. Treat
            # as having no internal deps; planner-driven sibling
            # discovery happens elsewhere.
            direct[t.name] = []
            continue
        deps: List[str] = []
        seen: Set[str] = set()
        for name in _raw_internal_dep_names(raw_t):
            if name == t.name:
                continue
            if name not in internal_names:
                continue
            if name in seen:
                continue
            seen.add(name)
            deps.append(name)
        direct[t.name] = deps

    cache: Dict[str, Set[str]] = {}

    def reachable(name: str, stack: Set[str]) -> Set[str]:
        if name in cache:
            return cache[name]
        if name in stack:
            return set()
        stack.add(name)
        out: Set[str] = set()
        for d in direct.get(name, []):
            out.add(d)
            out |= reachable(d, stack)
        stack.discard(name)
        out.discard(name)
        cache[name] = out
        return out

    return {t.name: reachable(t.name, set()) for t in package.targets}


def topo_order_units(
    units: Sequence[BuildUnit], package: Package
) -> List[BuildUnit]:
    """Order `units` so each one comes after every sibling unit it
    transitively depends on (via internal target deps). Stable on
    original index — units with no dependency relation between them keep
    their planner-assigned order.

    Unit-level dependency relation: unit U depends on unit V iff some
    target name in `V.source_targets` is in the transitive internal
    target dep closure of `U.source_targets`. Derived from
    `compute_internal_target_deps(package)`.

    This is the order Execute walks for dedup-overlap substitution: leaf
    products (StripeCore) build first, umbrella products (Stripe) build
    last with their already-built sibling targets swapped to
    `.binaryTarget`. See REWRITE_DESIGN.md §5.4 dedup-overlap.

    On a cycle in the unit graph (which would indicate a bad
    dump-package payload), remaining units are appended in original
    order so behavior stays deterministic instead of raising.
    """
    deps_map = compute_internal_target_deps(package)
    n = len(units)
    if n <= 1:
        return list(units)

    target_to_unit_idx: Dict[str, int] = {}
    for idx, u in enumerate(units):
        for t in u.source_targets:
            target_to_unit_idx[t] = idx

    # Edge i -> j  means unit i must come before unit j.
    children: List[List[int]] = [[] for _ in range(n)]
    in_degree: List[int] = [0] * n
    edges_seen: Set[Tuple[int, int]] = set()
    for j, u in enumerate(units):
        reach: Set[str] = set()
        for t in u.source_targets:
            reach |= deps_map.get(t, set())
        for t in reach:
            i = target_to_unit_idx.get(t)
            if i is None or i == j:
                continue
            if (i, j) in edges_seen:
                continue
            edges_seen.add((i, j))
            children[i].append(j)
            in_degree[j] += 1

    ready = sorted(i for i in range(n) if in_degree[i] == 0)
    out: List[BuildUnit] = []
    while ready:
        i = ready.pop(0)
        out.append(units[i])
        for j in children[i]:
            in_degree[j] -= 1
            if in_degree[j] == 0:
                # Insert j keeping `ready` sorted by original index.
                lo, hi = 0, len(ready)
                while lo < hi:
                    mid = (lo + hi) // 2
                    if ready[mid] < j:
                        lo = mid + 1
                    else:
                        hi = mid
                ready.insert(lo, j)

    if len(out) < n:
        seen = {id(u) for u in out}
        for u in units:
            if id(u) not in seen:
                out.append(u)
    return out


def _allocate_synthetic_product_name(
    base: str, taken: Set[str]
) -> str:
    """Compute a non-colliding synthetic product name for the dynamic
    re-export of `base`.

    Tries `f"{base}Dynamic"` first. If that collides with an existing or
    already-allocated product name (Alamofire ships both `Alamofire` and
    `AlamofireDynamic`, the latter would clash), falls back to
    `f"{base}__Dynamic"`, then `f"{base}__Dynamic2"`, `__Dynamic3`, … —
    the double-underscore disambiguator is unusual enough that it will
    almost never trip a real package's product list, and the numeric
    tail handles the pathological case where even that exists.

    Mutates `taken` in place — callers should pass the same set across
    all emissions in a single planning pass so two products that would
    each prefer `XDynamic` don't both allocate it.
    """
    primary = f"{base}Dynamic"
    if primary not in taken:
        taken.add(primary)
        return primary
    candidate = f"{base}__Dynamic"
    if candidate not in taken:
        taken.add(candidate)
        return candidate
    i = 2
    while True:
        candidate = f"{base}__Dynamic{i}"
        if candidate not in taken:
            taken.add(candidate)
            return candidate
        i += 1


def _validate_requested_platforms(config: Config, package: Package) -> None:
    """Fail fast if the user asked for a platform the package doesn't
    declare. SPM treats an empty `platforms:` array as "all platforms",
    so we only enforce when the package's list is non-empty.
    """
    if not package.platforms:
        return
    declared = {p.name.lower() for p in package.platforms}
    requested = _enabled_platforms(config)
    missing = [p for p in requested if p not in declared]
    if missing:
        decl = ", ".join(sorted(declared)) or "(none)"
        raise PlanError(
            f"Requested platform(s) not declared by the package: "
            f"{', '.join(missing)}. Package declares: {decl}."
        )


def plan_source_build(config: Config, package: Package) -> Plan:
    """Pure planner for source-mode builds.

    Takes the inspected Package and the user's Config and returns a Plan
    describing (a) the Package.swift edits Prepare should apply, (b) the
    list of build units Execute should run, and (c) the list of products
    the planner consciously skipped with a reason. See REWRITE_DESIGN.md
    §5.2 for the full rule set.
    """
    _validate_requested_platforms(config, package)
    plan = Plan()
    plan.include_deps = config.include_deps

    product_filter: Optional[Set[str]] = (
        set(config.product_filters) if config.product_filters else None
    )
    product_filter_matches: Set[str] = set()

    # Names that are off-limits for synthetic product allocation: every
    # existing product (the static + already-dynamic sibling case), plus
    # any synthetic name we hand out earlier in this pass.
    taken_product_names: Set[str] = {p.name for p in package.products}

    # First pass: walk the package products. Three possible outcomes per
    # product: (a) filtered out by --product, (b) dropped because it's a
    # system-library wrapper, (c) promoted to a build unit.
    for product in package.products:
        if product_filter is not None and product.name not in product_filter:
            continue
        if product_filter is not None:
            product_filter_matches.add(product.name)

        if _is_system_only_product(product, package):
            plan.skipped.append((product.name, "system-library product"))
            continue

        # Binary-only products in a *mixed* package are skipped entirely
        # from source-mode planning. The pure-binary whole-package case is
        # already auto-routed to --binary mode by `_run_source_mode` (we
        # never enter this loop for that input). What's left is the rarer
        # mixed shape — e.g. one .library backed by `binaryTarget` plus
        # other .libraries backed by Swift/ObjC source — where:
        #   - Adding a synthetic dynamic library against a binaryTarget
        #     would trigger SPM's "invalid type for binary product" error
        #     and abort the whole package.
        #   - Emitting an `archive` BuildUnit would push xcodebuild at a
        #     scheme that produces no source artefact; it can't build
        #     a binaryTarget-backed product as a fresh framework, only
        #     consume the prebuilt artefact as a dependency.
        # Source mode therefore can't service the binary product. We drop
        # it with a clear reason; users who want it should switch to
        # --binary or consume the upstream xcframework directly.
        if _is_binary_only_product(product, package):
            plan.skipped.append(
                (product.name, "binary-only product (use --binary mode)")
            )
            continue

        # Source-mode dynamic strategy — only when the product isn't
        # already dynamic. The Alamofire/GRDB-dynamic invariant from
        # Session 2: planning both GRDB and GRDB-dynamic is correct;
        # adding a synthetic dynamic library for the already-dynamic
        # sibling is not.
        language = _derive_product_language(product, package)
        if product.linkage != Linkage.DYNAMIC:
            # Add a synthetic dynamic .library that re-exports the same
            # targets, then build it and rename the output framework to
            # the original product name. Replaces the legacy
            # force_dynamic surgery; lets us drive everything through
            # `swift package add-product` instead of regex editing.
            synthetic_name = _allocate_synthetic_product_name(
                product.name, taken_product_names
            )
            plan.package_swift_edits.append(
                PackageSwiftEdit(
                    kind="synth_dynamic_library",
                    product_name=synthetic_name,
                    targets=list(product.targets),
                )
            )
            scheme = synthetic_name
        else:
            scheme = resolve_scheme(product.name, package.schemes)
        plan.build_units.append(
            BuildUnit(
                name=product.name,
                scheme=scheme,
                framework_name=product.name,
                language=language,
                archive_strategy="archive",
                source_targets=list(product.targets),
                synthetic=False,
            )
        )

    # Second pass: --target escape hatch. For each requested target we
    # synthesize a fresh .library() entry unless one already exists with
    # the same name (in which case we use the existing one and warn).
    existing_product_names = {p.name for p in package.products}
    existing_planned_names = {bu.name for bu in plan.build_units}
    for target_name in config.target_filters:
        tgt = package.target_by_name(target_name)
        if tgt is None:
            raise PlanError(
                f"--target {target_name!r}: no such target in package "
                f"(available: {sorted(t.name for t in package.targets)})"
            )
        if tgt.kind in (TargetKind.SYSTEM, TargetKind.BINARY,
                        TargetKind.PLUGIN, TargetKind.MACRO):
            raise PlanError(
                f"--target {target_name!r}: target kind is {tgt.kind!r}; "
                "only regular source targets can be synthesized into a "
                ".library() product."
            )
        if tgt.kind == TargetKind.TEST:
            raise PlanError(
                f"--target {target_name!r}: test targets cannot be built "
                "as xcframeworks."
            )
        if tgt.kind == TargetKind.EXECUTABLE:
            # Reject up front so this turns into a clean PlanError instead
            # of dying later inside Prepare when the synthesized
            # .library(…, targets: [executable]) fails the round-trip
            # dump-package validation. Matches the other excluded kinds
            # above and gives a clearer error message (Codex [P2]).
            raise PlanError(
                f"--target {target_name!r}: target kind is {tgt.kind!r}; "
                "executable targets cannot be built as xcframeworks. "
                "Pass a regular source target instead."
            )

        if target_name in existing_product_names:
            plan.warnings.append(
                f"--target {target_name}: already exposed as a .library() "
                "product; using the existing product instead of synthesizing "
                "a duplicate."
            )
            # If the existing product wasn't already planned (e.g. it was
            # filtered out by --product), surface it now — the user's
            # explicit --target is a stronger signal than --product.
            if target_name not in existing_planned_names:
                existing = next(p for p in package.products if p.name == target_name)
                if _is_system_only_product(existing, package):
                    raise PlanError(
                        f"--target {target_name!r}: existing product has "
                        "only system targets and cannot be built."
                    )
                if _is_binary_only_product(existing, package):
                    # Mirrors the first-pass skip above: an existing
                    # binary-only product surfaced via --target cannot be
                    # source-built. Record the skip and continue so the
                    # rest of the --target pass still processes other
                    # requested targets.
                    plan.skipped.append(
                        (existing.name, "binary-only product (use --binary mode)")
                    )
                    continue
                language = _derive_product_language(existing, package)
                if existing.linkage != Linkage.DYNAMIC:
                    synthetic_name = _allocate_synthetic_product_name(
                        existing.name, taken_product_names
                    )
                    plan.package_swift_edits.append(
                        PackageSwiftEdit(
                            kind="synth_dynamic_library",
                            product_name=synthetic_name,
                            targets=list(existing.targets),
                        )
                    )
                    scheme = synthetic_name
                else:
                    scheme = resolve_scheme(existing.name, package.schemes)
                plan.build_units.append(
                    BuildUnit(
                        name=existing.name,
                        scheme=scheme,
                        framework_name=existing.name,
                        language=language,
                        archive_strategy="archive",
                        source_targets=list(existing.targets),
                        synthetic=False,
                    )
                )
                existing_planned_names.add(existing.name)
                if product_filter is not None:
                    product_filter_matches.add(existing.name)
            continue

        # Repeated --target T on the command line must not synthesize
        # the same .library() twice — the second pass would produce a
        # duplicate PackageSwiftEdit and a duplicate BuildUnit that
        # later phases would either trip over or silently double-build.
        # Record a warning and move on. (Codex [P2] coverage gap.)
        if target_name in existing_planned_names:
            plan.warnings.append(
                f"--target {target_name}: specified more than once; "
                "ignoring the duplicate."
            )
            continue

        # Synthesize. Package.swift edit + build unit, both tagged with
        # synthetic=True for display purposes. The build unit's language
        # comes from the target itself (there's no "union" — it's one
        # target).
        # By construction `target_name` is NOT an existing product name
        # (the existing-product branch above already returned), so a
        # direct add-product call won't collide; record the allocation in
        # `taken_product_names` so a later first-pass synthetic doesn't
        # try to reuse it.
        taken_product_names.add(target_name)
        plan.package_swift_edits.append(
            PackageSwiftEdit(
                kind="synth_library",
                product_name=target_name,
                targets=[target_name],
            )
        )
        language = tgt.language if tgt.language in (
            Language.SWIFT, Language.OBJC, Language.MIXED
        ) else Language.NA
        plan.build_units.append(
            BuildUnit(
                name=target_name,
                scheme=target_name,
                framework_name=target_name,
                language=language,
                archive_strategy="archive",
                source_targets=[target_name],
                synthetic=True,
            )
        )
        existing_planned_names.add(target_name)

    # --product filter validation: if the user asked for something we
    # never saw, fail loudly. The synthetic pass may have broadened the
    # effective match set, so do this check last.
    if product_filter is not None:
        unmatched = sorted(product_filter - product_filter_matches)
        if unmatched:
            available = sorted(p.name for p in package.products)
            raise PlanError(
                f"--product filter matched no products: {unmatched}\n"
                f"Available products: {available}"
            )

    if not plan.build_units:
        raise PlanError(
            "Plan produced zero build units. Did --product filter out "
            "everything, or does this package declare only non-library "
            "products?"
        )

    return plan


def plan_binary_build(config: Config, artifacts: Sequence[BinaryArtifact]) -> Plan:
    """Pure planner for binary-mode builds.

    Input is the list of artifacts discovered by Fetch (see
    `discover_binary_artifacts`). This function just applies the
    --product filter and turns each surviving artifact into a build unit
    whose strategy is "copy-artifact" — Execute will literally `cp -R`
    it into the output directory.
    """
    if config.target_filters:
        raise PlanError(
            "--target is a source-build escape hatch and cannot be used "
            "with --binary."
        )

    plan = Plan()
    plan.binary_mode = True
    plan.include_deps = config.include_deps

    product_filter: Optional[Set[str]] = (
        set(config.product_filters) if config.product_filters else None
    )

    # Dedupe: a vendor artifactbundle can contain the same-named
    # xcframework in multiple locations (usually a bug — cf. the
    # `__MACOSX` ghost already pruned in Fetch), but even legit
    # multi-slice packages occasionally list duplicates. Keep the first
    # occurrence, record the rest on plan.skipped so --dry-run explains
    # why they disappeared from the plan.
    seen_names: Set[str] = set()
    for art in artifacts:
        if art.product_name in seen_names:
            plan.skipped.append(
                (art.product_name, f"duplicate artifact at {art.path}")
            )
            continue
        seen_names.add(art.product_name)
        if product_filter is not None and art.product_name not in product_filter:
            continue
        plan.build_units.append(
            BuildUnit(
                name=art.product_name,
                scheme="",  # n/a — nothing to archive
                framework_name=art.product_name,
                language=Language.NA,  # binary — we don't inspect the bytes
                archive_strategy="copy-artifact",
                source_targets=[],
                synthetic=False,
                artifact_path=art.path,
            )
        )

    if product_filter is not None:
        matched = {bu.name for bu in plan.build_units}
        unmatched = sorted(product_filter - matched)
        if unmatched:
            available = sorted(a.product_name for a in artifacts)
            raise PlanError(
                f"--product filter matched no binary artifacts: {unmatched}\n"
                f"Available artifacts: {available}"
            )

    if not plan.build_units:
        raise PlanError(
            "Binary plan produced zero build units. Did the vendor package "
            "actually ship xcframeworks under .build/artifacts/?"
        )

    return plan


def _derive_package_label(src: str) -> str:
    """Return a human label for a package source URL/path.

    Handles the three shapes we see in practice:
      - https/ssh URLs ending in `.git`  → repo name without .git
      - SCP-style `git@host:org/repo.git` → repo name without .git
      - local filesystem paths           → basename
    """
    src = src.rstrip("/")
    if ":" in src and "@" in src and not src.startswith(("http://", "https://", "ssh://")):
        # SCP-style: `git@github.com:org/repo.git`
        after_colon = src.rsplit(":", 1)[-1]
        base = os.path.basename(after_colon)
    else:
        base = os.path.basename(src)
    if base.endswith(".git"):
        base = base[:-4]
    return base or "(unknown)"


def print_plan(
    plan: Plan,
    *,
    package: Optional[Package],
    config: Config,
) -> None:
    """Render a Plan in the human-readable dry-run format.

    Matches the shape documented in the Session 2 brief. One horizontal
    rule per section; sections are elided when empty. Keeps alignment
    tidy by computing column widths up front so the output stays readable
    even for 12-target Stripe runs.
    """
    if package is not None:
        name = package.name
    else:
        # Binary mode has no Package model — derive the label from the
        # package source URL's basename. Handle SCP-style `git@host:org/repo.git`
        # specially because os.path.basename returns the whole string for it.
        name = _derive_package_label(config.package_source or "(unknown)")

    version = config.user_version or "(unversioned)"
    mode = "binary" if plan.binary_mode else "source"
    bold(f"\nPlan for {name} @ {version}  ({mode} mode)")

    if plan.package_swift_edits:
        print("  Package edits:")
        for edit in plan.package_swift_edits:
            if edit.kind == "synth_dynamic_library":
                tgts = ", ".join(edit.targets)
                print(
                    f"    - synth_dynamic_library: {edit.product_name} "
                    f"→ targets=[{tgts}]"
                )
            elif edit.kind == "synth_library":
                tgts = ", ".join(edit.targets)
                print(
                    f"    - synth_library: {edit.product_name} "
                    f"→ targets=[{tgts}]"
                )
            else:
                print(f"    - {edit.kind}: {edit.product_name}")
    elif not plan.binary_mode:
        print("  Package edits: (none)")

    if plan.build_units:
        print("  Build units:")
        name_w = max(len(bu.name) for bu in plan.build_units)
        scheme_w = max(len(bu.scheme or "-") for bu in plan.build_units)
        lang_w = max(len(bu.language or "-") for bu in plan.build_units)
        name_w = max(name_w, 10)
        scheme_w = max(scheme_w, 8)
        lang_w = max(lang_w, 5)
        for i, bu in enumerate(plan.build_units, start=1):
            markers = []
            if bu.synthetic:
                markers.append("[synthetic library]")
            if bu.archive_strategy == "copy-artifact":
                markers.append("[binary artifact]")
            marker = ("  " + " ".join(markers)) if markers else ""
            scheme_disp = bu.scheme or "-"
            print(
                f"    [{i}] {bu.name:<{name_w}}  "
                f"scheme={scheme_disp:<{scheme_w}}  "
                f"language={bu.language:<{lang_w}}  "
                f"→ {bu.framework_name}.xcframework{marker}"
            )
    else:
        print("  Build units: (none)")

    selected = _selected_slices(config)
    if selected:
        ids = ", ".join(s.slice_id for s, _ in selected)
        print(f"  Selected slices: {ids}")

    if plan.skipped:
        print("  Skipped:")
        for sname, reason in plan.skipped:
            print(f"    - {sname} ({reason})")

    if plan.include_deps:
        print("  Note: --include-deps is enabled; transitive frameworks will "
              "be discovered after Execute runs.")
