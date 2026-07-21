"""Phase 5.5 — Import-closure completeness gate.

Verify is per-unit: it proves each produced xcframework is well-formed.
This gate runs once over the WHOLE produced set and asks a question no
per-unit check can: is the set CLOSED under public import? A source-mode
conversion can otherwise "succeed" while omitting a Swift module that a
produced `.swiftinterface` imports — the umbrella's nested SPM build
resolved that module from a source checkout we then never shipped as a
sibling xcframework, so the downstream binding consumer is left unable to
resolve `import <that module>`.

The gate derives the required-module set from the public-import closure of
every produced `.swiftinterface` (the same import surface a consumer's
swiftc must resolve), then flags any required module that is BOTH:

  * not resolvable from the produced artifacts on disk — no matching
    `<M>.framework`, `<M>.swiftmodule`, or `module M` in any shipped
    `module.modulemap`. This is module-identity based, never xcframework-
    basename based: it captures a `GRDB-dynamic.xcframework` that carries
    module `GRDB`, and an in-slice binary-less `GRDBSQLite.framework`
    system shim that ships `GRDBSQLite` without a top-level xcframework of
    its own; AND

  * owned by the dependency graph — declared as a target / product /
    product-backing-target of the umbrella or any transitive package. A
    platform SDK framework is never an SPM target, so it is never mistaken
    for a missing sibling; that keeps a legitimately complete conversion
    from being failed on `import Foundation`.

Every surviving module is a hard conversion failure carrying a precise root
reason. No source is rewritten and no resilience is stripped: the gate only
makes an already-broken artifact set fail loudly and nameably instead of
succeeding silently.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set

from .model import Linkage, Package, TargetKind

# Target kinds that can be produced as a consumer-resolvable xcframework
# module. A macro / plugin / executable / (empty) target is compiled through
# a different path (compiler-plugin support, an app binary) and is NEVER
# emitted as an xcframework, so it must not be treated as "owed" by the gate —
# flagging a public import of such a module would fail a conversion with no
# possible remediation, exactly like flagging a platform SDK import. SYSTEM
# targets become in-slice clang-module shims and always surface in the
# provided set via their `module.modulemap`, so excluding them from ownership
# is safe (they are never missing). BINARY targets DO become xcframeworks
# (vendor artifacts copied through), so they stay ownable.
_OWNABLE_TARGET_KINDS = frozenset({TargetKind.REGULAR, TargetKind.BINARY})

# --- public-import extraction ------------------------------------------------
#
# Parity with the swift-bindings ingestion preflight's import detector: a
# line-anchored matcher whose prefix is an unordered run of attributes
# (`@Name` optionally with `(...)`) and access modifiers, followed by
# `import`, an optional decl-kind keyword introducing a scoped import
# (`import struct Foo.Bar`), and the leading MODULE identifier.

_ACCESS_MODIFIERS = r"public|private|internal|fileprivate|open|package"
_DECL_KINDS = r"typealias|struct|class|enum|protocol|let|var|func"
_IMPORT_LINE_RE = re.compile(
    r"^[ \t]*"
    r"(?P<prefix>(?:@[A-Za-z_]\w*(?:\([^)]*\))?[ \t]+|(?:"
    + _ACCESS_MODIFIERS
    + r")[ \t]+)*)"
    r"import[ \t]+"
    r"(?:(?:" + _DECL_KINDS + r")[ \t]+)?"
    r"(?P<module>[A-Za-z_]\w*)",
    re.MULTILINE,
)

# An import line carrying any of these in its prefix is NOT part of the
# consumer-facing surface a client must resolve: `@_implementationOnly` hides
# the dependency from clients entirely, and a `package`/`internal`/
# `fileprivate`/`private` import never widens client visibility. (A `package
# import` the tool has already rewritten to `public import` in a shipped
# sibling reads as public here — which is correct: post-rewrite the consumer
# DOES have to resolve it.)
_NONPUBLIC_IMPORT_MODIFIERS = frozenset(
    {"package", "internal", "fileprivate", "private"}
)

# `module M { ... }`, `framework module M`, `explicit module M` — the module
# name may be dotted for a submodule (`M.Sub`); we normalise to the leading
# component so it matches an import (which is captured leading-component-only).
# Matched per-line (not global) so a brace-depth counter can restrict capture
# to TOP-LEVEL modules — a nested `module Private { ... }` submodule is what a
# consumer reaches via `import Foo.Private`, never a top-level `import Private`,
# so counting it as a provided top-level module could mask a genuinely missing
# owned module of the same name.
_MODULEMAP_MODULE_LINE_RE = re.compile(
    r"^\s*(?:framework\s+)?(?:explicit\s+)?module\s+([A-Za-z_][\w.]*)"
)


def _top_level_modulemap_modules(text: str) -> List[str]:
    """Every module declared at brace-depth 0 in a `module.modulemap`,
    normalised to its leading component. Nested submodules (depth ≥ 1) are
    skipped — they are not top-level importable modules."""
    out: List[str] = []
    depth = 0
    for line in text.splitlines():
        if depth == 0:
            m = _MODULEMAP_MODULE_LINE_RE.match(line)
            if m:
                out.append(m.group(1).split(".", 1)[0])
        depth += line.count("{") - line.count("}")
        if depth < 0:
            depth = 0
    return out


def _import_line_is_public(prefix: str) -> bool:
    """A prefix is non-public iff it carries `@_implementationOnly` or a
    non-public access modifier as a standalone token. Tokenising on
    whitespace keeps a modifier keyword that appears INSIDE an attribute's
    parens (e.g. `@_spi(private)`) from being read as an access modifier."""
    for tok in prefix.split():
        if tok in _NONPUBLIC_IMPORT_MODIFIERS:
            return False
        if tok.startswith("@_implementationOnly"):
            return False
    return True


def extract_public_imports(interface_text: str) -> Set[str]:
    """Every module named by a consumer-facing `import` in a swiftinterface.

    Recognises plain / attributed (`@_exported`, `@_spi(x)`,
    `@preconcurrency`) / access-modified (`public import`) / scoped
    (`import struct Foo.Bar` → `Foo`) forms; the captured name is the
    leading module component only. Non-public imports (`@_implementationOnly`
    or `package`/`internal`/... access) are excluded — a client never has to
    resolve them.
    """
    out: Set[str] = set()
    for m in _IMPORT_LINE_RE.finditer(interface_text):
        if _import_line_is_public(m.group("prefix")):
            out.add(m.group("module"))
    return out


# --- on-disk produced-module enumeration -------------------------------------


def _iter_consumer_swiftinterfaces(xcframework: Path):
    """Yield every consumer-relevant `.swiftinterface` under `xcframework`.

    Includes the public `<arch>.swiftinterface` AND the
    `<arch>.private.swiftinterface` — xcodebuild recompiles both when the
    interface is consumed, so a module imported by EITHER must ship (this is
    what catches an owned module imported only from the private interface,
    e.g. a `@usableFromInline`-driven `_NumericsShims`). The
    `.package.swiftinterface` variant is skipped: it is the same-package-only
    interface a foreign consumer never reads (and the tool deletes / rewrites
    it during packaging).
    """
    for iface in xcframework.rglob("*.swiftinterface"):
        if iface.name.endswith(".package.swiftinterface"):
            continue
        yield iface


def public_imports_in_xcframework(xcframework: Path) -> Set[str]:
    """Union of the public-import closure across every consumer-relevant
    `.swiftinterface` in the bundle."""
    out: Set[str] = set()
    for iface in _iter_consumer_swiftinterfaces(xcframework):
        try:
            # utf-8-sig strips a leading BOM if present so `^import Foo` on the
            # first line still anchor-matches.
            text = iface.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        out |= extract_public_imports(text)
    return out


def provided_modules_in_xcframework(xcframework: Path) -> Set[str]:
    """Every module a consumer can resolve from this bundle.

    Module-identity based, never xcframework-basename based: framework and
    swiftmodule DIRECTORY names (primary AND in-slice shim frameworks) plus
    every top-level module declared in any shipped `module.modulemap`. So a
    `GRDB-dynamic.xcframework` carrying `GRDB.framework` / `GRDB.swiftmodule`
    provides `GRDB`, and a binary-less in-slice `GRDBSQLite.framework` system
    shim (declared via its modulemap) provides `GRDBSQLite` even though no
    `GRDBSQLite.xcframework` exists.
    """
    provided: Set[str] = set()
    if not xcframework.is_dir():
        return provided
    for path in xcframework.rglob("*"):
        name = path.name
        if path.is_dir():
            if name.endswith(".framework"):
                provided.add(name[: -len(".framework")])
            elif name.endswith(".swiftmodule"):
                provided.add(name[: -len(".swiftmodule")])
        elif name == "module.modulemap":
            try:
                text = path.read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                continue
            for decl in _top_level_modulemap_modules(text):
                provided.add(decl)
    return provided


def find_produced_xcframeworks(output_dir: Path) -> List[Path]:
    """The xcframeworks produced by this run. During a build `output_dir` is
    the run-scoped staging directory, which holds ONLY this run's artifacts
    (umbrella + every sibling), so a plain glob is exact — no prior-run
    leftovers to filter out."""
    if output_dir is None or not Path(output_dir).is_dir():
        return []
    return sorted(Path(output_dir).glob("*.xcframework"))


# --- dependency-graph ownership ---------------------------------------------


@dataclass
class _Ownership:
    owned: Set[str]
    owner_of: Dict[str, str]  # module -> owning package identity / umbrella name
    static_only: Set[str]  # modules exposed ONLY via static-linkage product(s)


def _graph_ownership(package: Package) -> _Ownership:
    """Modules the dependency graph is responsible for producing: every
    target / product / product-backing-target name of the umbrella and of
    every transitive package. Records the owning package per module and which
    modules are exposed only through static-linkage products (for reason
    classification)."""
    owned: Set[str] = set()
    owner_of: Dict[str, str] = {}
    linkages: Dict[str, Set[str]] = {}

    def add(module: str, owner: str) -> None:
        owned.add(module)
        owner_of.setdefault(module, owner)

    for t in package.targets:
        # Only kinds that are actually produced as an xcframework are owed by
        # the gate (see `_OWNABLE_TARGET_KINDS`). A macro / plugin /
        # executable target is never emitted as a sibling, so a public import
        # of it is not a hole this tool can fill.
        if t.kind in _OWNABLE_TARGET_KINDS:
            add(t.name, package.name)
    for p in package.products:
        add(p.name, package.name)
        linkages.setdefault(p.name, set()).add(p.linkage)
        for tname in p.targets:
            add(tname, package.name)
            linkages.setdefault(tname, set()).add(p.linkage)

    for tp in package.transitive_packages:
        # `target_names` (retained by Inspect, already narrowed to ownable
        # kinds) covers internal helper modules that are not exposed as
        # products — e.g. a transitive's auto-synth'd
        # `InternalCollectionsUtilities` / `_NumericsShims`.
        for tname in getattr(tp, "target_names", None) or []:
            add(tname, tp.identity)
        for p in tp.products:
            add(p.name, tp.identity)
            linkages.setdefault(p.name, set()).add(p.linkage)
            for tname in p.targets:
                add(tname, tp.identity)
                linkages.setdefault(tname, set()).add(p.linkage)

    static_only = {
        m for m, ls in linkages.items() if ls and ls == {Linkage.STATIC}
    }
    return _Ownership(owned=owned, owner_of=owner_of, static_only=static_only)


# --- the gate ---------------------------------------------------------------


@dataclass
class MissingClosureModule:
    """One required-but-not-produced module, with its owning package (when
    known) and a human-readable root reason for the receipt."""

    module: str
    owner: Optional[str]
    reason: str


def _classify_reason(
    module: str,
    owner: Optional[str],
    static_only: Set[str],
    failures_by_unit: Dict[str, object],
) -> str:
    # 1. An already-recorded upstream failure for the module or its owning
    #    package is the most precise explanation — surface its diagnostic.
    for unit in (module, owner):
        if unit is not None and unit in failures_by_unit:
            f = failures_by_unit[unit]
            return (
                f"required module {module!r} was not produced: the build of "
                f"{unit!r} failed at the {f.phase} phase — {f.root_diagnostic}"
            )
    # 2. Exposed only through static-linkage product(s) — no dynamic
    #    xcframework could be produced.
    if module in static_only:
        return (
            f"required module {module!r} is exposed only through "
            f"static-linkage product(s); no dynamic xcframework was produced "
            f"for it"
        )
    # 3. Owned by a named dependency but simply absent from the output set.
    if owner is not None:
        return (
            f"required module {module!r} (declared by dependency {owner!r}) "
            f"is imported by the produced interface but no xcframework was "
            f"produced for it"
        )
    return (
        f"required module {module!r} is declared in the dependency graph but "
        f"no xcframework was produced for it"
    )


def compute_missing_closure_modules(
    package: Package,
    produced_xcframeworks: Sequence[Path],
    failures: Sequence[object] = (),
) -> List[MissingClosureModule]:
    """Return the graph-owned modules that the produced interfaces import but
    that the produced artifact set does not provide — ordered by module name.

    `failures` is the receipt's accumulated failure records (each with
    `.unit` / `.phase` / `.root_diagnostic`); used only to attribute a
    precise root reason. An empty list is returned when the produced set is
    closed under public import.
    """
    imported: Set[str] = set()
    provided: Set[str] = set()
    for xc in produced_xcframeworks:
        imported |= public_imports_in_xcframework(xc)
        provided |= provided_modules_in_xcframework(xc)

    own = _graph_ownership(package)
    failures_by_unit: Dict[str, object] = {}
    for f in failures:
        unit = getattr(f, "unit", None)
        if unit is not None:
            failures_by_unit.setdefault(unit, f)

    missing: List[MissingClosureModule] = []
    for module in sorted(imported):
        if module in provided:
            continue
        if module not in own.owned:
            # Not ours to produce: a platform SDK framework, an injected
            # system module, or a dependency outside the resolved graph. Never
            # fail on it — that is the boundary that keeps a complete
            # conversion from going red on `import Foundation`.
            continue
        owner = own.owner_of.get(module)
        missing.append(
            MissingClosureModule(
                module=module,
                owner=owner,
                reason=_classify_reason(
                    module, owner, own.static_only, failures_by_unit
                ),
            )
        )
    return missing
