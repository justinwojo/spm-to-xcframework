"""Phase 4 — Execute · Swift `.swiftmodule/.swiftinterface` injection.

Ports the legacy bash inject_swiftmodule pass plus the shared
`_framework_content_root` / `_ensure_root_symlink` helpers used by every
injection pass to keep macOS versioned bundles vs iOS flat bundles
agnostic at the call site.
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from ..log import dim, verbose_log


# SPM passes `-package-name <pkg>` to every target in a Swift package so
# `package`-access symbols work between siblings. The flag gets recorded in
# the emitted `.swiftinterface` header — any consumer Swift module that
# imports this framework AND also gets `-package-name <pkg>` (i.e., happens
# to live in an SPM package with the same name) will require a matching
# `.package.swiftinterface` and refuse to load the public one. Since an
# `.xcframework` is an external artifact, no consumer should treat it as
# in-package; we strip the flag so the loaded interface is unambiguously
# non-package and Swift falls back to the normal public-import path.
#
# Triggers seen in the matrix: Nuke 12.x — Nuke and NukeUI both live in
# `staged`, so NukeUI fails to load Nuke.framework's interface with
# "Module 'Nuke' is in package 'staged' but was built from a non-package
# interface" unless we strip.
#
# The rewrite is scoped to the `// swift-module-flags[-ignorable]:` header
# comment lines only. A blind file-wide substitution would silently corrupt
# any public API whose body contains a string literal with the substring
# " -package-name X" — unlikely, but exactly the class of "silently
# producing a broken xcframework" we explicitly want to avoid.
_PACKAGE_NAME_FLAG_RE = re.compile(r"[ \t]+-package-name[ \t]+\S+")
_MODULE_FLAGS_PREFIX_RE = re.compile(
    r"^//\s*swift-module-flags(?:-ignorable)?\s*:"
)


def _strip_package_name_from_swiftinterfaces(swiftmod_dir: Path) -> None:
    for iface in swiftmod_dir.glob("*.swiftinterface"):
        try:
            text = iface.read_text(encoding="utf-8")
        except OSError:
            continue
        lines = text.splitlines(keepends=True)
        changed = False
        for idx, line in enumerate(lines):
            if not _MODULE_FLAGS_PREFIX_RE.match(line):
                continue
            stripped = _PACKAGE_NAME_FLAG_RE.sub("", line)
            if stripped != line:
                lines[idx] = stripped
                changed = True
        if changed:
            iface.write_text("".join(lines), encoding="utf-8")


def _strip_self_module_qualifier_from_swiftinterface(
    text: str, module_name: str
) -> str:
    """Remove leading `<module_name>.` qualifier from type references in
    the swiftinterface OF that module. Within module M's own emitted
    interface, every `M.X` qualifies type X in module M — the bare `X`
    resolves to the same thing in this lexical scope. Stripping the
    qualifier is the documented workaround for the Swift compiler bug
    where a type whose name shadows the module name causes the
    swiftinterface re-parser to bind `M.X` as "nested type X inside
    type M" (the shadowing class) instead of "top-level type X in
    module M", producing errors like
    "'EventBridge' is not a member type of class 'AnalyticsConnector.AnalyticsConnector'"
    (Swift issue #56573 / SR-14195; canonical packages: mixpanel-swift's
    `JSON.JSON`, analytics-connector-ios's `AnalyticsConnector.AnalyticsConnector`).

    Gated on shadowing detection: the strip is only safe (and only
    needed) when the module HAS a top-level type whose name equals
    the module name. Without that shadowing type, `M.X` is just a
    redundant qualifier — but if the interface contains a legitimate
    super-class reference like `class Observer : ReactiveSwift.Observer<Value, Error>`
    inside `extension Signal { ... }`, stripping `ReactiveSwift.`
    yields `class Observer : Observer<...>`, which resolves to
    `Signal.Observer` (the class being defined) and produces
    `'Observer' inherits from itself` at archive time
    (ReactiveSwift 6.x → Moya 15.x). The shadow-presence check below
    is a substring scan for `(class|struct|enum|protocol|actor) M`
    declarations; absence means no shadow, so skip.

    Scope:
    - Substitution runs over the code-only view of the swiftinterface so
      comments (`//` lines including the `swift-module-flags` header,
      `/* */` blocks) and string literals (deprecation messages in
      `@available(..., message: "...")`, etc.) are left untouched.
    - Lookbehind `(?<![\\w.])` ensures we only strip the FIRST `M.` of
      any qualified chain, so a legitimate nested-type reference
      `M.M.X` (where the second `M` is a real nested type inside the
      shadowing top-level type) survives as `M.X` rather than collapsing
      to `X`. The shadowing-class case `M.M` still collapses to `M`,
      which is what we want.
    - The lookahead `(?=[A-Z_])` requires the next character after the
      dot to start an identifier (uppercase or underscore by Swift
      convention for type names) — guards against substitutions inside
      attribute syntax like `@frozen` or numeric suffixes.
    """
    from ..prepare import _make_code_token_view
    code_view = _make_code_token_view(text)
    # Shadow-presence gate. Only fire the strip when the swiftinterface
    # declares a type whose name equals the module name — that's the
    # ONLY situation where the SR-14195 / Module.Self compiler bug
    # actually manifests on re-parse. Without the shadow, every `M.X`
    # is just a qualifier that the compiler resolves correctly, AND
    # stripping it can corrupt legitimate super-class references where
    # `M.X` is required to disambiguate from a same-named nested
    # type in surrounding scope (ReactiveSwift.Observer inside
    # `extension Signal { class Observer : ReactiveSwift.Observer<...> }`).
    shadow_pattern = re.compile(
        r"(?:class|struct|enum|protocol|actor)\s+"
        + re.escape(module_name)
        + r"\b"
    )
    if shadow_pattern.search(code_view) is None:
        return text
    pattern = re.compile(
        r"(?<![\w.])" + re.escape(module_name) + r"\.(?=[A-Z_])"
    )
    out: List[str] = []
    last = 0
    for m in pattern.finditer(code_view):
        out.append(text[last:m.start()])
        last = m.end()  # drop `<module_name>.`, keep the looked-ahead char
    if last == 0:
        return text
    out.append(text[last:])
    return "".join(out)


def _find_swiftmodule_in_dd(
    dd_path: Path,
    fw_name: str,
    scheme: str,
    extra_module_names: Sequence[str] = (),
) -> Optional[Tuple[Path, str]]:
    """Locate a `<name>.swiftmodule` directory under DerivedData.

    Search order:
      1. `*/<fw_name>.swiftmodule` anywhere under DerivedData.
      2. `ArchiveIntermediates/<scheme>/BuildProductsPath/*/<fw_name>.swiftmodule`
         (legacy bash 1296-1301 fallback for scheme/product name mismatch).
      3. For each name in extra_module_names (typically the underlying
         source targets), the same two passes. This handles cases like
         GRDB-dynamic, where the framework binary is named "GRDB-dynamic"
         but the Swift module the targets actually emit is "GRDB".

    Returns (path, module_name) so the caller knows which name to use
    for the destination directory inside the framework's Modules/.
    """

    def _find_for(name: str) -> Optional[Path]:
        target = f"{name}.swiftmodule"
        if dd_path.is_dir():
            for path in dd_path.rglob(target):
                if path.is_dir():
                    return path
        if scheme != name and dd_path.is_dir():
            intermediate = (
                dd_path / "Build" / "Intermediates.noindex"
                / "ArchiveIntermediates" / scheme / "BuildProductsPath"
            )
            if intermediate.is_dir():
                for path in intermediate.rglob(target):
                    if path.is_dir():
                        return path
        return None

    found = _find_for(fw_name)
    if found is not None:
        return found, fw_name
    for name in extra_module_names:
        if name == fw_name:
            continue
        found = _find_for(name)
        if found is not None:
            return found, name
    return None


# macOS frameworks ship in the "versioned bundle" layout: real content lives
# under `Versions/A/` and the framework root holds only `Versions/` plus
# convenience symlinks (`Foo -> Versions/Current/Foo`, etc.). Apple's
# codesigning rejects any framework that has real (non-symlink) directories
# at the root other than `Versions/` — "unsealed contents present in the
# root directory of an embedded framework."
#
# `xcodebuild archive` emits a macOS framework already in versioned layout
# (binary + Resources/Info.plist under Versions/A), but it does NOT pre-
# create Modules/ or Headers/ (xcodebuild's archive output doesn't include
# swiftinterface or public ObjC headers — those come from DerivedData and
# our injection passes). Our injectors therefore must write under Versions/A
# and create root symlinks themselves, otherwise they'd lay a real Modules/
# at the framework root and break codesigning downstream.
#
# iOS / tvOS / watchOS / visionOS / Mac Catalyst all use the flat layout
# (no Versions/ directory; everything at the framework root), and these
# helpers no-op on those slices — `_framework_content_root` returns
# fw_path itself, and `_ensure_root_symlink` returns without touching
# anything.


def _framework_content_root(fw_path: Path) -> Path:
    """Return the directory under a framework bundle where real injected
    content should live.

    Versioned (macOS-style) bundles: `<fw>/Versions/A`. Flat (iOS-style)
    bundles: `fw_path` itself. Detected by presence of a real (non-symlink)
    `Versions/A` directory at the framework root.
    """
    versions_a = fw_path / "Versions" / "A"
    if versions_a.is_dir() and not versions_a.is_symlink():
        return versions_a
    return fw_path


def _ensure_root_symlink(fw_path: Path, name: str) -> None:
    """For a versioned framework, ensure `<fw>/<name>` is a symlink to
    `Versions/Current/<name>`. No-op for flat frameworks.

    If a real (non-symlink) directory squats at the root path — typically
    the result of a prior injection that wrote in flat-layout style on a
    macOS framework — it is migrated to `Versions/A/<name>` and the
    symlink takes its place. Migration is the only reliable repair: a
    leftover real directory at the root will cause the codesigning
    rejection this whole machinery exists to prevent.
    """
    versions_a = fw_path / "Versions" / "A"
    if not (versions_a.is_dir() and not versions_a.is_symlink()):
        return  # flat (iOS-style) framework — nothing to symlink.
    link = fw_path / name
    target = Path("Versions") / "Current" / name
    if link.is_symlink():
        if os.readlink(link) == str(target):
            return
        link.unlink()
    elif link.exists():
        # Migrate a real root directory left over from a prior injection
        # into the versioned location, then drop the symlink in place.
        dest = versions_a / name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.move(str(link), str(dest))
    link.symlink_to(target)


def inject_swiftmodule(
    *,
    fw_path: Path,
    fw_name: str,
    scheme: str,
    dd_path: Path,
    variant: str,
    verbose: bool,
    extra_module_names: Sequence[str] = (),
) -> bool:
    """Copy `.swiftmodule/.swiftinterface` files from DerivedData into a
    framework's Modules directory if they're missing.

    `extra_module_names` lets the caller pass underlying source target
    names so that frameworks whose binary name doesn't match the actual
    Swift module name (e.g. GRDB-dynamic.framework whose module is
    `GRDB`) can still get their interfaces injected.

    Returns True iff anything was injected. Idempotent: if any
    `Modules/<X>.swiftmodule/` already contains `.swiftinterface` files
    this is a no-op.

    Versioned (macOS) frameworks: writes under `Versions/A/Modules/` and
    ensures `<fw>/Modules` is a symlink. See `_framework_content_root`
    for the rationale.
    """
    modules_dir = _framework_content_root(fw_path) / "Modules"
    if modules_dir.is_dir():
        for sm in modules_dir.glob("*.swiftmodule"):
            if sm.is_dir():
                for child in sm.iterdir():
                    if child.suffix == ".swiftinterface":
                        verbose_log(verbose, f"  Swift interfaces present in {variant} framework")
                        return False

    found = _find_swiftmodule_in_dd(dd_path, fw_name, scheme, extra_module_names)
    if found is None:
        verbose_log(
            verbose,
            f"  No Swift module found in DerivedData for {fw_name} ({variant})",
        )
        return False
    swiftmod, module_name = found

    dim(f"  Injecting Swift module interfaces ({variant})")
    modules_dir.mkdir(parents=True, exist_ok=True)
    dest = modules_dir / f"{module_name}.swiftmodule"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(swiftmod, dest)
    _disambiguate_self_module_qualifier_in_swiftmodule(dest, module_name)
    _strip_package_name_from_swiftinterfaces(dest)
    _ensure_root_symlink(fw_path, "Modules")
    return True


def _disambiguate_self_module_qualifier_in_swiftmodule(
    swiftmod_dir: Path, module_name: str
) -> None:
    for iface in swiftmod_dir.glob("*.swiftinterface"):
        try:
            text = iface.read_text(encoding="utf-8")
        except OSError:
            continue
        rewritten = _strip_self_module_qualifier_from_swiftinterface(text, module_name)
        if rewritten != text:
            iface.write_text(rewritten, encoding="utf-8")
