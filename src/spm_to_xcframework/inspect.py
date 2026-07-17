"""Phase 1: turn `swift package dump-package` into a typed `Package`.

Inspect is read-only — no filesystem mutations on the staged tree. It
parses the dump-package JSON once, classifies each target's language
from `swift package describe --type json` (SPM's own per-target view,
authoritative for `module_type`), and lists xcodebuild schemes.
Everything downstream consumes the resulting `Package` model instead
of re-running swift.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Iterable, List, Optional, Set, Tuple

from .config import Config
from .diagnostics import format_swift_package_failure
from .errors import InspectError
from .log import bold, info, out, verbose_log
from .model import (
    Language,
    Linkage,
    Package,
    Platform,
    Product,
    Target,
    TargetKind,
    TransitivePackageInfo,
)


def _swift_dump_package(staged_dir: Path) -> dict:
    """Run `swift package dump-package` against the staged dir and return
    the parsed JSON. Raises InspectError on any failure.
    """
    cp = subprocess.run(
        ["swift", "package", "dump-package"],
        cwd=str(staged_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if cp.returncode != 0:
        raise InspectError(
            format_swift_package_failure("swift package dump-package", cp.stderr)
        )
    try:
        return json.loads(cp.stdout)
    except json.JSONDecodeError as exc:
        raise InspectError(f"Could not parse dump-package JSON: {exc}") from exc


def _parse_tools_version(raw: object) -> str:
    """`toolsVersion` is a struct: {'_version': '6.1.0'}. Be lenient about
    older shapes that might have been a flat string."""
    if isinstance(raw, dict):
        v = raw.get("_version")
        if isinstance(v, str):
            return v
    if isinstance(raw, str):
        return raw
    return "unknown"


def _parse_platforms(raw: object) -> List[Platform]:
    out: List[Platform] = []
    if not isinstance(raw, list):
        return out
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        out.append(
            Platform(
                name=str(entry.get("platformName", "")),
                version=str(entry.get("version", "")),
            )
        )
    return out


def _parse_linkage(type_field: object) -> Optional[str]:
    """SPM products have several shapes:
        {'library': ['automatic']}
        {'library': ['dynamic']}
        {'library': ['static']}
        {'executable': None}
        {'plugin': None}
        {'snippet': None}
    Returns the linkage as one of Linkage._VALUES, or None for non-library
    products (which we deliberately drop from the model since the tool only
    builds libraries).
    """
    if isinstance(type_field, dict):
        if "library" in type_field:
            inner = type_field["library"]
            if isinstance(inner, list) and inner:
                v = str(inner[0])
                if v in Linkage._VALUES:
                    return v
                return Linkage.UNKNOWN
            return Linkage.AUTOMATIC
        # Non-library product types — explicitly excluded from the model.
        if "executable" in type_field:
            return None
        if "plugin" in type_field:
            return None
        if "snippet" in type_field:
            return None
    # Older / unknown shape — log via UNKNOWN so the planner can decide.
    return Linkage.UNKNOWN


def _parse_target_kind(raw: object) -> str:
    if not isinstance(raw, str):
        return TargetKind.UNKNOWN
    return raw if raw in TargetKind._VALUES else TargetKind.UNKNOWN


def _parse_dependencies(raw: object) -> List[str]:
    """Targets list dependencies in several union shapes. We only need names
    here (the planner doesn't currently care about kind), so flatten and
    return string names. Unknown shapes are silently dropped."""
    out: List[str] = []
    if not isinstance(raw, list):
        return out
    for d in raw:
        if not isinstance(d, dict):
            continue
        # Common shapes:
        #   {"byName": ["X", null]}
        #   {"target": ["X", null]}
        #   {"product": ["X", "PkgName", null, null]}
        for key in ("byName", "target", "product"):
            v = d.get(key)
            if isinstance(v, list) and v and isinstance(v[0], str):
                out.append(v[0])
                break
    return out


def _raw_internal_dep_names(raw_target: dict) -> List[str]:
    """Return the first-level INTERNAL target-name deps of a raw dump-package
    target entry. "Internal" means same-package targets — i.e. the
    `byName` and `target` shapes, NOT the `product` shape (which refers
    to another package entirely and whose targets we can't reach).

    Used by dependency-walking code that needs to scan raw dump entries
    directly (e.g. `_find_objc_headers_dir`) rather than the typed
    `Target.dependencies` list. Those callers need the raw entries for
    other fields (settings, publicHeadersPath, …), so they can't just
    reuse `_parse_dependencies` on the typed model.

    Order-preserving; duplicates are kept in source order because both
    callers iterate and de-dupe with their own `scanned` sets.
    """
    out: List[str] = []
    for dep in raw_target.get("dependencies", []) or []:
        if not isinstance(dep, dict):
            continue
        for key in ("byName", "target"):
            v = dep.get(key)
            if isinstance(v, list) and v and isinstance(v[0], str):
                out.append(v[0])
                break
    return out


def dump_package(staged_dir: Path) -> Tuple[dict, List[Product], List[Target], List[Platform], str, str]:
    """Run dump-package and parse it into typed shards. Returns
    (raw_dump, products, targets, platforms, name, tools_version).

    Splitting this from build_package() lets the self-test fixtures feed
    in pre-canned dump payloads without invoking the swift toolchain.
    """
    raw = _swift_dump_package(staged_dir)
    return _parse_dump(raw)


def _parse_dump(raw: dict) -> Tuple[dict, List[Product], List[Target], List[Platform], str, str]:
    name = str(raw.get("name") or "")
    tools_version = _parse_tools_version(raw.get("toolsVersion"))
    platforms = _parse_platforms(raw.get("platforms"))

    products: List[Product] = []
    for prod in raw.get("products", []) or []:
        if not isinstance(prod, dict):
            continue
        linkage = _parse_linkage(prod.get("type"))
        if linkage is None:
            # Non-library product — skip entirely. Plan only ever builds libraries.
            continue
        targets = [str(t) for t in (prod.get("targets") or []) if isinstance(t, str)]
        products.append(Product(name=str(prod.get("name") or ""), linkage=linkage, targets=targets))

    targets: List[Target] = []
    for t in raw.get("targets", []) or []:
        if not isinstance(t, dict):
            continue
        targets.append(
            Target(
                name=str(t.get("name") or ""),
                kind=_parse_target_kind(t.get("type")),
                path=t.get("path") if isinstance(t.get("path"), str) else None,
                public_headers_path=(
                    t.get("publicHeadersPath")
                    if isinstance(t.get("publicHeadersPath"), str)
                    else None
                ),
                dependencies=_parse_dependencies(t.get("dependencies")),
                exclude=[str(e) for e in (t.get("exclude") or []) if isinstance(e, str)],
            )
        )

    return raw, products, targets, platforms, name, tools_version


# SPM `describe --type json` reports each target's classification as
# `module_type`. This is the same enum SwiftPM uses internally to drive
# the build, so it's strictly more authoritative than walking the
# source tree: a target declared with `.target()` is SwiftTarget even
# when the directory contains a stub umbrella `.h` Xcode auto-generated,
# which is exactly the umbrella-stub case the old FS scanner needed
# heuristics to avoid mis-classifying as Mixed.
#
# Module types not in this map (BinaryTarget, SystemLibraryTarget,
# PluginTarget, MacroTarget, …) collapse to Language.NA — none of them
# are candidate source-build units. MixedLanguageTarget is the shape
# SwiftPM accepts for the experimental mixed-language feature; today
# the SPM frontend rejects mixed-language source dirs outright, but the
# mapping is here so the moment SwiftPM enables it for real we already
# classify correctly.
_MODULE_TYPE_LANGUAGE = {
    "SwiftTarget": Language.SWIFT,
    "ClangTarget": Language.OBJC,
    "MixedLanguageTarget": Language.MIXED,
}

# Compiled-source extensions that make a ClangTarget NON-pure-C: ObjC (.m),
# ObjC++ (.mm), C++ (.cpp/.cc/.cxx/.c++/.cp), and assembly (.s/.asm). These
# are matched against a LOWERCASED filename, so uppercase spellings that are
# legal on case-sensitive filesystems (.CPP, .CC, .MM, .S, …) are caught too.
# The lone `.c`-vs-`.C` distinction is handled separately in the classifier
# because lowercasing would collide `.C` (the C++ convention) with `.c` (C).
# A ClangTarget whose compiled sources are all `.c` (headers ignored) is a
# pure-C shim — safe to build standalone as its own `.library(type:
# .dynamic)`, which `_auto_synth_sibling_units` relies on to promote helpers
# like swift-numerics' `_NumericsShims`.
_NON_PURE_C_SOURCE_EXTS = (
    ".m", ".mm",
    ".cpp", ".cc", ".cxx", ".c++", ".cp",
    ".s", ".asm",
)


def _clang_sources_are_pure_c(sources: object) -> bool:
    """True iff `sources` (describe's per-target source list) has at least
    one `.c` file and no ObjC/C++/assembly source. Header files (`.h`,
    `.hpp`, …) are neutral — they don't determine the compile language.
    Used to sub-classify a ClangTarget (which the language map otherwise
    collapses to Language.OBJC) as a standalone-safe pure-C shim."""
    if not isinstance(sources, list):
        return False
    saw_c = False
    for src in sources:
        if not isinstance(src, str):
            continue
        # `.c` (lowercase) is C; `.C` (uppercase) is the C++ convention on
        # case-sensitive filesystems. This single distinction is case-
        # SENSITIVE; every other non-C source is rejected case-insensitively
        # below, so mixed targets like `["shim.c", "backend.CPP"]` don't slip
        # through as pure C.
        if src.endswith(".c"):
            saw_c = True
            continue
        if src.endswith(".C"):
            return False
        low = src.lower()
        if any(low.endswith(ext) for ext in _NON_PURE_C_SOURCE_EXTS):
            return False
    return saw_c


def _swift_describe_package(staged_dir: Path) -> dict:
    """Run `swift package describe --type json` against the staged dir
    and return the parsed JSON. Raises InspectError on any failure.

    describe is SPM's own per-target view of the resolved package —
    `module_type` and `sources` come from the same code path that drives
    the build, so the classification matches what `xcodebuild` will see.
    """
    cp = subprocess.run(
        ["swift", "package", "describe", "--type", "json"],
        cwd=str(staged_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if cp.returncode != 0:
        raise InspectError(
            format_swift_package_failure("swift package describe", cp.stderr)
        )
    try:
        return json.loads(cp.stdout)
    except json.JSONDecodeError as exc:
        raise InspectError(f"Could not parse describe JSON: {exc}") from exc


def scan_target_languages(staged_dir: Path, targets: Iterable[Target]) -> None:
    """Label each target's `language` and `source_file_count` from the
    output of `swift package describe --type json`.

    Mutates the Target instances in place. Targets with kind SYSTEM /
    BINARY / PLUGIN / MACRO / TEST keep Language.NA — none of them are
    candidate source-build units. Targets describe doesn't enumerate
    (or whose `module_type` falls outside `_MODULE_TYPE_LANGUAGE`)
    also stay at NA.
    """
    described = _swift_describe_package(staged_dir)
    by_name: dict = {}
    for entry in described.get("targets", []) or []:
        if isinstance(entry, dict) and isinstance(entry.get("name"), str):
            by_name[entry["name"]] = entry

    for tgt in targets:
        if tgt.kind in (
            TargetKind.SYSTEM,
            TargetKind.BINARY,
            TargetKind.PLUGIN,
            TargetKind.MACRO,
            TargetKind.TEST,
        ):
            tgt.language = Language.NA
            continue
        entry = by_name.get(tgt.name)
        if not entry:
            tgt.language = Language.NA
            continue
        module_type = entry.get("module_type")
        tgt.language = _MODULE_TYPE_LANGUAGE.get(module_type, Language.NA)
        sources = entry.get("sources")
        tgt.source_file_count = len(sources) if isinstance(sources, list) else 0
        tgt.clang_is_pure_c = (
            module_type == "ClangTarget"
            and _clang_sources_are_pure_c(sources)
        )


def discover_schemes(staged_dir: Path, verbose: bool = False) -> List[str]:
    """Run `xcodebuild -list -json` against the staged dir.

    Because staging strips xcodeproj/xcworkspace, this returns the SPM
    auto-generated scheme list with no multi-project ambiguity (bug 4 in
    SPM_TO_XCFRAMEWORK_NOTES.md). On any failure, returns an empty list
    and lets the planner fall back to product-name schemes — Inspect
    never raises here, since some packages legitimately have nothing
    xcodebuild can list (very old swift-tools-version, etc.).

    `verbose=True` logs the xcodebuild stderr tail so users can tell the
    difference between "no schemes" and "scheme discovery failed".
    """
    cp = subprocess.run(
        ["xcodebuild", "-list", "-json"],
        cwd=str(staged_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if cp.returncode != 0:
        if verbose:
            tail = "\n".join((cp.stderr or "").rstrip().splitlines()[-5:])
            verbose_log(verbose, f"  xcodebuild -list failed: {tail}")
        return []

    try:
        data = json.loads(cp.stdout)
    except json.JSONDecodeError as exc:
        verbose_log(verbose, f"  xcodebuild -list returned non-JSON: {exc}")
        return []

    container = data.get("workspace") or data.get("project") or {}
    schemes = container.get("schemes") or []
    return [str(s) for s in schemes if isinstance(s, str)]


def _collect_referenced_package_products(
    raw_dump: dict, targets: List[Target]
) -> "dict[str, dict]":
    """Enumerate the `.product(name:, package:)` references that any
    REGULAR target in the root package makes against external packages,
    keyed by SPM identity.

    Filtering to REGULAR targets matches what the planner ultimately builds
    — test/macro/plugin targets are never built as xcframeworks, so we
    don't want their transitive product deps to drive patching of foreign
    checkouts. The dump-package shape we look for is:

        {"product": ["ProductName", "package-identity", null, null]}

    Returns an ordered dict keyed by identity. Each value is a dict with:
      - "products": ordered list of distinct product names the root
        referenced from this identity (used to scope the child build via
        `Config.product_filters` and to drive prune_child's needed-
        identity closure).
      - "root_targets": ordered list of distinct root REGULAR target
        names that reference this identity (used by the orchestrator to
        drop transitives reachable only from helper/example targets
        outside the user's selected-target closure).
      - "product_to_root_targets": dict from product name to ordered
        list of root REGULAR target names that reference that specific
        product. Preserves the fine-grained edge attribution the two
        aggregate lists above lose, so the orchestrator can trim
        `referenced_products` down to only the products imported by
        in-closure root targets.
    """
    target_kind_by_name = {t.name: t.kind for t in targets}
    by_identity: "dict[str, dict]" = {}
    for t in raw_dump.get("targets", []) or []:
        if not isinstance(t, dict):
            continue
        name = t.get("name")
        if not isinstance(name, str):
            continue
        if target_kind_by_name.get(name) != TargetKind.REGULAR:
            continue
        for dep in t.get("dependencies", []) or []:
            if not isinstance(dep, dict):
                continue
            prod = dep.get("product")
            if not isinstance(prod, list) or len(prod) < 2:
                continue
            product_name = prod[0]
            identity = prod[1]
            if not isinstance(identity, str) or not isinstance(product_name, str):
                continue
            entry = by_identity.setdefault(
                identity,
                {"products": [], "root_targets": [], "product_to_root_targets": {}},
            )
            if product_name not in entry["products"]:
                entry["products"].append(product_name)
            if name not in entry["root_targets"]:
                entry["root_targets"].append(name)
            per_product = entry["product_to_root_targets"].setdefault(
                product_name, []
            )
            if name not in per_product:
                per_product.append(name)
    return by_identity


def _show_dependencies(staged_dir: Path, verbose: bool) -> Optional[dict]:
    """Run `swift package show-dependencies --format json` to get the
    resolved dependency tree (each entry has identity, version, and the
    on-disk `path` to the checkout). Returns the parsed JSON or None on
    failure (treated as "no transitive packages" — Plan/Prepare gracefully
    no-op).
    """
    cp = subprocess.run(
        ["swift", "package", "show-dependencies", "--format", "json"],
        cwd=str(staged_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if cp.returncode != 0:
        verbose_log(
            verbose,
            f"  swift package show-dependencies failed: "
            f"{(cp.stderr or '').rstrip().splitlines()[-3:]}",
        )
        return None
    try:
        return json.loads(cp.stdout)
    except json.JSONDecodeError as exc:
        verbose_log(verbose, f"  show-dependencies JSON parse failed: {exc}")
        return None


def _flatten_dependency_tree(tree: dict) -> dict:
    """Walk the `show-dependencies` JSON (which is recursive) and return a
    flat dict mapping identity → (checkout_path, url, version) for every
    node reachable from the root. The root itself is excluded — only its
    transitive deps appear.

    `url` is the canonical upstream URL SPM resolved against (NOT the
    local `.build/checkouts/<id>/.git/config` origin, which points at
    SPM's intermediate `.build/repositories/<id>/` cache). `version` is
    the resolved release string ("3.67.0" — no `v` prefix). Both fields
    are required for the orchestrator's binary-only transitive auto-
    switch to route through `_run_binary_mode` against the upstream
    URL+tag. If show-dependencies omits a node's url or version (e.g.
    revision-pinned deps where `version == "unspecified"`), that
    entry's tuple still carries an entry but the binary-only auto-
    switch will fall back to the source-mode child instead.
    """
    flat: dict = {}

    def walk(node: dict) -> None:
        deps = node.get("dependencies") or []
        for dep in deps:
            if not isinstance(dep, dict):
                continue
            identity = dep.get("identity")
            path = dep.get("path")
            url = dep.get("url")
            version = dep.get("version")
            if isinstance(identity, str) and isinstance(path, str):
                if identity not in flat:
                    flat[identity] = (
                        path,
                        url if isinstance(url, str) else None,
                        version if isinstance(version, str) else None,
                    )
            walk(dep)

    walk(tree)
    return flat


def _looks_remote_url(url: Optional[str]) -> bool:
    """True for URLs we'd accept as `package_source` for binary mode.

    show-dependencies sometimes echoes a local path as `url` (the root
    package's own entry uses its on-disk path). We only want to treat a
    transitive dep as binary-mode-routable when its URL is genuinely
    remote — otherwise we'd loop back into the binary-only local-path
    refusal the auto-switch was supposed to fix.
    """
    if not url:
        return False
    return (
        url.startswith("http://")
        or url.startswith("https://")
        or url.startswith("git@")
        or url.startswith("ssh://")
    )


def _looks_resolved_version(version: Optional[str]) -> bool:
    """True for show-dependencies `version` values that look like a real
    resolved release (SemVer-ish). Excludes the `"unspecified"`/`None`
    cases (revision-pinned deps, root package, .package(name:, path:)
    overrides) where we don't have a tag to feed `--binary` mode.
    """
    if not version or version == "unspecified":
        return False
    return True


def _is_transitive_binary_only(
    products: List[Product], targets: List[Target]
) -> bool:
    """Return True iff every non-system product in the transitive is
    backed only by `.binaryTarget(...)` targets. Mirrors
    `plan._package_is_binary_only` but operates directly on the
    (products, targets) tuple Inspect already has for the transitive —
    avoiding a transient Package construction here just to call the
    Plan-side helper. Kept narrow on purpose; the only consumer is the
    Inspect-time precompute that drives the orchestrator's binary-only
    transitive auto-switch.
    """
    by_name = {t.name: t for t in targets}
    binary_products = 0
    for product in products:
        if not product.targets:
            # Malformed product — match the Plan-side helpers' policy
            # of "not binary, not system" so xcodebuild surfaces the
            # real complaint instead of us short-circuiting here.
            return False
        all_system = all(
            (t := by_name.get(tn)) is not None and t.kind == TargetKind.SYSTEM
            for tn in product.targets
        )
        if all_system:
            continue
        all_binary = all(
            (t := by_name.get(tn)) is not None and t.kind == TargetKind.BINARY
            for tn in product.targets
        )
        if not all_binary:
            return False
        binary_products += 1
    return binary_products > 0


def _collect_byname_dependency_names(
    raw_dump: dict, targets: List[Target]
) -> "dict[str, List[str]]":
    """Enumerate bare-name / `byName` dependencies that REGULAR targets in
    the root declare, EXCLUDING names that resolve to an internal target of
    the root package.

    SwiftPM lets a target depend on another package's product by bare name
    (`dependencies: ["SWXMLHash"]`) when the name is unambiguous — but such
    a dep carries NO package identity in `dump-package`. It appears as
    `{"byName": ["SWXMLHash", null]}`, byte-identical in shape to an
    internal sibling dep, so `_collect_referenced_package_products` (which
    only reads the `{"product": [...]}` shape) never sees it. Macaw →
    SWXMLHash and CocoaMQTT → MqttCocoaAsyncSocket are the canonical cases
    where this silent miss ships an umbrella xcframework whose dependency
    module was never built.

    Returns an ordered dict mapping bare name → ordered list of the root
    REGULAR target names that reference it. Names matching an internal
    target are dropped here (those are internal deps resolved by the
    auto-synth / internal-closure paths, not external products). The
    caller resolves each surviving name against the DIRECT dependency
    packages' product inventories; names that don't resolve to exactly one
    external product are dropped there.
    """
    internal_target_names = {t.name for t in targets}
    target_kind_by_name = {t.name: t.kind for t in targets}
    out: "dict[str, List[str]]" = {}
    for t in raw_dump.get("targets", []) or []:
        if not isinstance(t, dict):
            continue
        name = t.get("name")
        if not isinstance(name, str):
            continue
        if target_kind_by_name.get(name) != TargetKind.REGULAR:
            continue
        for dep in t.get("dependencies", []) or []:
            if not isinstance(dep, dict):
                continue
            by = dep.get("byName")
            if not isinstance(by, list) or not by:
                continue
            dep_name = by[0]
            if not isinstance(dep_name, str):
                continue
            # Internal sibling dep — resolved by the auto-synth / internal
            # closure paths, not an external product reference. SPM itself
            # resolves a bare name to an internal target before a dependency
            # product, so this exclusion also matches SPM's own precedence.
            if dep_name in internal_target_names:
                continue
            per_name = out.setdefault(dep_name, [])
            if name not in per_name:
                per_name.append(name)
    return out


def _resolve_byname_external_products(
    tree: dict,
    byname_candidates: "dict[str, List[str]]",
    referenced_by_identity: "dict[str, dict]",
    verbose: bool,
) -> None:
    """Resolve each bare-name dep in `byname_candidates` against the DIRECT
    dependency packages' product inventories and, on a UNIQUE match, fold
    it into `referenced_by_identity` (mutated in place) exactly as though
    the root had written `.product(name: <name>, package: <identity>)`.

    SwiftPM only lets a target byName-reference a product of a DIRECT
    dependency, so the search is bounded to the root's immediate deps
    (`tree["dependencies"]`) — a handful of `dump-package` calls, and only
    when an unresolved byName actually exists. A name that matches zero, or
    more than one, direct dep's products is left unresolved (verbose-
    logged): building/attributing the wrong sibling is worse than the
    pre-existing miss, and an ambiguous bare name is something SPM itself
    would reject.
    """
    direct_children: List[Tuple[str, str]] = []
    seen_identities: Set[str] = set()
    for child in tree.get("dependencies", []) or []:
        if not isinstance(child, dict):
            continue
        identity = child.get("identity")
        path = child.get("path")
        if not (isinstance(identity, str) and isinstance(path, str)):
            continue
        if identity in seen_identities:
            continue
        seen_identities.add(identity)
        direct_children.append((identity, path))
    if not direct_children:
        return

    # product name -> ordered list of direct-dep identities declaring a
    # product with that name. Built by dumping each direct dep checkout once.
    product_owners: "dict[str, List[str]]" = {}
    for identity, path in direct_children:
        checkout = Path(path)
        if not (checkout / "Package.swift").is_file() and not any(
            checkout.glob("Package@swift-*.swift")
        ):
            continue
        try:
            _r, d_products, _t, _pl, _n, _tv = dump_package(checkout)
        except Exception as exc:  # noqa: BLE001 — Inspect must not crash
            verbose_log(
                verbose,
                f"  byName resolve: dump-package failed for {identity!r}: {exc}",
            )
            continue
        for prod in d_products:
            owners = product_owners.setdefault(prod.name, [])
            if identity not in owners:
                owners.append(identity)

    for name, root_targets in byname_candidates.items():
        owners = product_owners.get(name)
        if not owners:
            verbose_log(
                verbose,
                f"  byName {name!r} matched no direct dependency product "
                f"(leaving unresolved)",
            )
            continue
        if len(owners) > 1:
            verbose_log(
                verbose,
                f"  byName {name!r} is ambiguous across {owners!r} "
                f"(leaving unresolved)",
            )
            continue
        identity = owners[0]
        entry = referenced_by_identity.setdefault(
            identity,
            {"products": [], "root_targets": [], "product_to_root_targets": {}},
        )
        if name not in entry["products"]:
            entry["products"].append(name)
        per_product = entry["product_to_root_targets"].setdefault(name, [])
        for rt in root_targets:
            if rt not in entry["root_targets"]:
                entry["root_targets"].append(rt)
            if rt not in per_product:
                per_product.append(rt)
        verbose_log(
            verbose,
            f"  byName {name!r} resolved to product of {identity!r}",
        )


def _discover_transitive_packages(
    staged_dir: Path,
    raw_dump: dict,
    targets: List[Target],
    verbose: bool,
) -> List[TransitivePackageInfo]:
    """Enumerate the direct external-package deps of the root's REGULAR
    targets, look up each one's resolved checkout, and dump-package the
    checkout to get its products list. Returns one
    `TransitivePackageInfo` per directly-referenced identity; packages
    pulled in only by test/macro/plugin paths are skipped at the source.

    Failure modes (each results in skipping the affected identity, never
    a hard error):
      - show-dependencies returns no entry for the identity (e.g. the
        package wasn't fully resolved)
      - the checkout's `dump-package` fails (rare — would mean the
        package author shipped a broken manifest)
    """
    referenced_by_identity = _collect_referenced_package_products(raw_dump, targets)

    # Bare-name (`byName`) external product deps carry no identity in
    # dump-package, so they're collected separately and resolved against the
    # direct deps' product inventories below. Any already covered by an
    # explicit `.product(...)` ref (same product referenced both ways) don't
    # need re-resolving — the explicit ref already carries the identity — but
    # their byName-referencing targets are still merged into that identity's
    # attribution (see the `if byname_candidates:` block below).
    byname_candidates = _collect_byname_dependency_names(raw_dump, targets)
    if byname_candidates:
        # A bare name may reference the SAME product another root target already
        # reaches via `.product(name:, package:)`. Those are already resolved to
        # an identity — nothing left to look up — but the byName-referencing
        # targets must still be merged into that entry's attribution, or
        # `--product` / `--target` closure-filtering (which keys transitives off
        # `root_targets` / `product_to_root_targets`) could drop a transitive
        # that a selected byName-only target actually needs. Only genuinely
        # unresolved names survive into `byname_candidates` for the lookup below.
        product_owner_identity = {
            p: identity
            for identity, entry in referenced_by_identity.items()
            for p in entry["products"]
        }
        remaining: "dict[str, List[str]]" = {}
        for name, rts in byname_candidates.items():
            identity = product_owner_identity.get(name)
            if identity is None:
                remaining[name] = rts
                continue
            entry = referenced_by_identity[identity]
            per_product = entry["product_to_root_targets"].setdefault(name, [])
            for rt in rts:
                if rt not in entry["root_targets"]:
                    entry["root_targets"].append(rt)
                if rt not in per_product:
                    per_product.append(rt)
        byname_candidates = remaining

    if not referenced_by_identity and not byname_candidates:
        return []

    tree = _show_dependencies(staged_dir, verbose=verbose)
    if tree is None:
        return []
    flat = _flatten_dependency_tree(tree)
    if not flat:
        return []

    # Resolve bare-name deps (Macaw → SWXMLHash, CocoaMQTT →
    # MqttCocoaAsyncSocket) to their owning direct dependency and fold them
    # into `referenced_by_identity` so they're built + consumed like any
    # `.product(name:, package:)` reference. A no-op when there are none.
    if byname_candidates:
        _resolve_byname_external_products(
            tree, byname_candidates, referenced_by_identity, verbose
        )
    if not referenced_by_identity:
        # Every byName candidate was unresolvable/ambiguous — nothing to build.
        return []

    # Case-insensitive identity lookup so root manifests that write
    # `package: "Swift-Clocks"` still match the SPM-normalised
    # "swift-clocks" identity.
    flat_ci = {k.lower(): (k, v) for k, v in flat.items()}

    out: List[TransitivePackageInfo] = []
    for ref, ref_entry in referenced_by_identity.items():
        ref_products = ref_entry["products"]
        ref_root_targets = ref_entry["root_targets"]
        ref_p2rt = ref_entry["product_to_root_targets"]
        match = flat_ci.get(ref.lower())
        if match is None:
            verbose_log(
                verbose,
                f"  transitive: identity {ref!r} referenced by root but "
                f"not present in show-dependencies output (skipping)",
            )
            continue
        identity, (checkout_str, upstream_url, resolved_version) = match
        checkout_path = Path(checkout_str)
        if not (checkout_path / "Package.swift").is_file() and not any(
            checkout_path.glob("Package@swift-*.swift")
        ):
            verbose_log(
                verbose,
                f"  transitive: {identity!r} at {checkout_path} has no "
                f"Package.swift (skipping)",
            )
            continue
        try:
            t_raw, t_products, t_targets, _t_platforms, _t_name, t_tools = (
                dump_package(checkout_path)
            )
        except Exception as exc:  # noqa: BLE001 — Inspect must not crash
            verbose_log(
                verbose,
                f"  transitive: dump-package failed for {identity!r}: {exc}",
            )
            continue
        # Precompute the binary-only flag so the orchestrator can route a
        # binary-only transitive through `_run_binary_mode`. The URL +
        # tag come from show-dependencies' parsed JSON (NOT the local
        # checkout's `.git/config`, which points at SPM's bare-repo
        # cache under `.build/repositories/<id>/` rather than the
        # canonical upstream). Both fields are best-effort — a
        # revision-pinned dep (`version == "unspecified"`) or a
        # `.package(name:, path:)` override returns None for the
        # affected field and the orchestrator falls back to the
        # pre-staged-dir source path.
        is_binary = _is_transitive_binary_only(t_products, t_targets)
        binary_target_names = [
            t.name for t in t_targets if t.kind == TargetKind.BINARY
        ]
        origin_url = upstream_url if _looks_remote_url(upstream_url) else None
        head_tag = resolved_version if _looks_resolved_version(resolved_version) else None
        out.append(
            TransitivePackageInfo(
                identity=identity,
                checkout_path=checkout_path,
                products=t_products,
                tools_version=t_tools,
                referenced_products=list(ref_products),
                referencing_root_targets=list(ref_root_targets),
                product_to_root_targets={
                    p: list(rts) for p, rts in ref_p2rt.items()
                },
                is_binary_only=is_binary,
                binary_target_names=binary_target_names,
                origin_url=origin_url,
                head_tag=head_tag,
            )
        )
    return out


def inspect_package(config: Config, staged_dir: Path) -> Package:
    """Top-level Inspect entry point. Reads only — no filesystem mutations
    on the staged tree. Wires together dump_package + scan + scheme list."""
    info("Inspecting package...")
    raw, products, targets, platforms, name, tools_version = dump_package(staged_dir)
    scan_target_languages(staged_dir, targets)
    schemes = discover_schemes(staged_dir, verbose=config.verbose)
    transitive = _discover_transitive_packages(
        staged_dir, raw, targets, verbose=config.verbose
    )
    return Package(
        name=name,
        tools_version=tools_version,
        platforms=platforms,
        products=products,
        targets=targets,
        schemes=schemes,
        raw_dump=raw,
        staged_dir=staged_dir,
        transitive_packages=transitive,
    )


def print_package(pkg: Package) -> None:
    """Human-readable Package summary, used by --inspect-only."""
    bold(f"\n=== {pkg.name} ===")
    out(f"  tools-version: {pkg.tools_version}")
    if pkg.platforms:
        plats = ", ".join(f"{p.name} {p.version}" for p in pkg.platforms)
        out(f"  platforms:     {plats}")
    out(f"  staged dir:    {pkg.staged_dir}")
    out(f"  schemes:       {', '.join(pkg.schemes) if pkg.schemes else '(none discovered)'}")

    bold(f"\nProducts ({len(pkg.products)}):")
    if not pkg.products:
        out("  (none)")
    for p in pkg.products:
        # Cross-reference each product's backing targets to flag system /
        # already-dynamic shapes the planner will care about.
        kinds = []
        for tname in p.targets:
            t = pkg.target_by_name(tname)
            kinds.append(t.kind if t else "?")
        notes = []
        if p.linkage == Linkage.DYNAMIC:
            notes.append("already dynamic")
        if all(k == TargetKind.SYSTEM for k in kinds) and kinds:
            notes.append("system-only — will be skipped")
        note_s = f"  [{', '.join(notes)}]" if notes else ""
        out(
            f"  - {p.name}  linkage={p.linkage}  targets={p.targets}{note_s}"
        )

    bold(f"\nTargets ({len(pkg.targets)}):")
    for t in pkg.targets:
        path_disp = t.path or "(default)"
        hdr = f" headers={t.public_headers_path}" if t.public_headers_path else ""
        out(
            f"  - {t.name}  kind={t.kind}  language={t.language}"
            f"  path={path_disp}{hdr}  files={t.source_file_count}"
        )
