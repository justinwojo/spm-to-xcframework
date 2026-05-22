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
from typing import Iterable, List, Optional, Tuple

from .config import Config
from .diagnostics import format_swift_package_failure
from .errors import InspectError
from .log import bold, info, verbose_log
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
    flat dict mapping identity → checkout path string for every node
    reachable from the root. The root itself is excluded — only its
    transitive deps appear.
    """
    flat: dict = {}

    def walk(node: dict) -> None:
        deps = node.get("dependencies") or []
        for dep in deps:
            if not isinstance(dep, dict):
                continue
            identity = dep.get("identity")
            path = dep.get("path")
            if isinstance(identity, str) and isinstance(path, str):
                if identity not in flat:
                    flat[identity] = path
            walk(dep)

    walk(tree)
    return flat


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
    if not referenced_by_identity:
        return []

    tree = _show_dependencies(staged_dir, verbose=verbose)
    if tree is None:
        return []
    flat = _flatten_dependency_tree(tree)
    if not flat:
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
        identity, checkout_str = match
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
            t_raw, t_products, _t_targets, _t_platforms, _t_name, t_tools = (
                dump_package(checkout_path)
            )
        except Exception as exc:  # noqa: BLE001 — Inspect must not crash
            verbose_log(
                verbose,
                f"  transitive: dump-package failed for {identity!r}: {exc}",
            )
            continue
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
    print(f"  tools-version: {pkg.tools_version}")
    if pkg.platforms:
        plats = ", ".join(f"{p.name} {p.version}" for p in pkg.platforms)
        print(f"  platforms:     {plats}")
    print(f"  staged dir:    {pkg.staged_dir}")
    print(f"  schemes:       {', '.join(pkg.schemes) if pkg.schemes else '(none discovered)'}")

    bold(f"\nProducts ({len(pkg.products)}):")
    if not pkg.products:
        print("  (none)")
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
        print(
            f"  - {p.name}  linkage={p.linkage}  targets={p.targets}{note_s}"
        )

    bold(f"\nTargets ({len(pkg.targets)}):")
    for t in pkg.targets:
        path_disp = t.path or "(default)"
        hdr = f" headers={t.public_headers_path}" if t.public_headers_path else ""
        print(
            f"  - {t.name}  kind={t.kind}  language={t.language}"
            f"  path={path_disp}{hdr}  files={t.source_file_count}"
        )
