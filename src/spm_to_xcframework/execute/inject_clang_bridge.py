"""Phase 4 — Execute · project-shipped Clang module shim injection.

Companion to `inject_clang_system` for the case where a package ships
its own `module.modulemap` declaring a Clang module distinct from any
`.systemLibrary` target (e.g. WCDB declares `WCDB_Private` inside
`src/bridge/module.modulemap`). SwiftPM picks these up at compile
time, the resulting swiftinterface emits `import WCDB_Private` plus
type references like `WCDB_Private.CPPColumn` in public signatures,
but xcodebuild's archive output does NOT propagate the bridge
target's module.modulemap into the framework — so any consumer that
has to rebuild the swiftinterface (different swiftc version) fails
with `error: no such module 'WCDB_Private'` and the framework is
unconsumable outside SwiftPM's own build context.

Distinguish from `inject_system_clang_modules`: that one handles
`.systemLibrary` SPM targets (always discovered via the parsed Package
model). This pass handles project-shipped modulemaps that aren't a
`.systemLibrary` declaration — they're regular C/ObjC/C++ targets
that happen to expose a custom Clang module name. The two complement
each other; both are "ship the missing modulemap as a sibling shim
framework discoverable via swiftc's `-F` framework search."
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import List, Optional, Set, Tuple

from ..fetch import _is_toxic_entry
from ..log import dim, warn
from ..model import Package

from .inject_clang_system import (
    _SYSTEM_SHIM_SENTINEL,
    _promote_modulemap_to_framework_form,
)
from .inject_swiftmodule import _framework_content_root


_PACKAGE_MODULEMAP_LINE_RE = re.compile(
    r"(?m)^\s*(?:framework\s+)?(?:explicit\s+)?module\s+([A-Za-z_][\w.]*)"
)
_SWIFTINTERFACE_IMPORT_RE = re.compile(r"^\s*import\s+([A-Za-z_][\w]*)(?:\.[A-Za-z_][\w]*)*\s*$")
_MODULEMAP_UMBRELLA_HEADER_RE = re.compile(r'umbrella\s+header\s+"([^"]+)"')
_MODULEMAP_UMBRELLA_DIR_RE = re.compile(r'umbrella\s+"([^"]+)"')
_MODULEMAP_HEADER_RE = re.compile(r'(?:^|\s)header\s+"([^"]+)"')


# Swift compiler built-in imports that always appear in swiftinterfaces
# but never need a shim — they're served by the consumer's toolchain.
_BUILTIN_SWIFT_IMPORTS: Set[str] = {
    "Swift",
    "SwiftOnoneSupport",
    "Foundation",
    "ObjectiveC",
    "Darwin",
    "Dispatch",
}


def _scan_swiftinterface_imports(fw_path: Path) -> Set[str]:
    """Collect every top-level `import X` from every swiftinterface inside
    the framework's `Modules/<Name>.swiftmodule/` directory. Both the
    public `<arch>.swiftinterface` and the `<arch>.private.swiftinterface`
    are scanned — both are emitted by `xcodebuild archive` for a Swift
    framework with library evolution enabled, and both must succeed when
    consumers rebuild the interface, so missing-module checks have to
    union both. Submodule imports (`import X.Sub`) are normalized to the
    top-level module name (`X`); a downstream pass that injects shims
    needs the top-level name to match what's declared in the project's
    own `module.modulemap`.
    """
    modules_dir = _framework_content_root(fw_path) / "Modules"
    if not modules_dir.is_dir():
        return set()
    imports: Set[str] = set()
    for iface in modules_dir.rglob("*.swiftinterface"):
        try:
            text = iface.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            m = _SWIFTINTERFACE_IMPORT_RE.match(line)
            if m:
                imports.add(m.group(1))
    return imports


def _modules_declared_in_modulemap(modulemap_text: str) -> Set[str]:
    """Top-level module names declared by a `module.modulemap`. Submodules
    (`module M.Sub { ... }`) are recorded under the dotted name as
    written; the caller compares against swiftinterface imports which we
    pre-normalize to top-level names, so submodule names won't false-
    positive a match."""
    return set(_PACKAGE_MODULEMAP_LINE_RE.findall(modulemap_text))


def _find_project_modulemap_for_module(
    package: Package, module_name: str
) -> Optional[Tuple[Path, List[Tuple[Path, Path]]]]:
    """Search the staged package source tree for a `module.modulemap`
    that declares a top-level Clang module named `module_name`. Returns
    the modulemap path plus a list of `(absolute_header_path,
    header_path_relative_to_modulemap_dir)` tuples for every `.h` file
    that belongs to the module (umbrella header sibling set, umbrella
    directory walk, or explicit `header` references), or None when no
    project-shipped modulemap matches.

    Relative paths preserve directory structure under the modulemap's
    parent so the bridge-shim copier can mirror them under
    `Headers/<rel>` and the modulemap's textual references (e.g.
    `umbrella header "include/U.h"`, `header "sub/A.h"`) still resolve
    at consumer compile time. Flattening to basenames here would silently
    produce broken shims for any modulemap with nested header layouts.

    Resolves `umbrella header "X"` by copying every `.h` in the same
    directory as `X` (clang's umbrella-header semantics: the named
    header is the apex, but every sibling header is implicitly part of
    the module). Resolves `umbrella "dir"` by walking that directory
    recursively. Resolves explicit `header "X"` references one file at a
    time. Skips `.modulemap` files inside `TOXIC_NAMES` directories
    (`.git`, `.build`, etc.) so we only consider source-tree modulemaps.
    """
    if not package.staged_dir.is_dir():
        return None
    for modulemap_path in sorted(package.staged_dir.rglob("module.modulemap")):
        # Skip any modulemap living inside a directory we'd otherwise
        # consider toxic for the staged tree (already pruned at fetch
        # time, but defensive — covers downstream regen of `.build/`).
        if any(_is_toxic_entry(part) for part in modulemap_path.relative_to(package.staged_dir).parts):
            continue
        try:
            text = modulemap_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if module_name not in _modules_declared_in_modulemap(text):
            continue

        modulemap_dir = modulemap_path.parent
        headers: List[Tuple[Path, Path]] = []
        seen_rel: Set[str] = set()

        # Umbrella header: copy the named header + all sibling .h in the
        # same directory (matches clang's umbrella-header expansion).
        # Use logical paths (no Path.resolve()) so umbrella headers that
        # are themselves symlinks pointing into a sibling directory don't
        # take us out of the umbrella's directory — WCDB ships
        # bridge/include/WCDBBridging.h as a symlink to ../WCDBBridging.h
        # while the 64 sibling .h are also symlinks into other subdirs;
        # the entire shim is meant to mirror bridge/include/, not bridge/.
        # Drop entries that don't resolve to a readable file: WCDB's
        # `bridge/include/` ships a few dangling symlinks pointing into
        # `bridge/tests/`, which SwiftPM-style staging excludes from the
        # source tree, and those test-only headers aren't part of any
        # public umbrella include chain.
        for um in _MODULEMAP_UMBRELLA_HEADER_RE.finditer(text):
            umbrella_rel = um.group(1)
            umbrella_path = modulemap_dir / umbrella_rel
            umbrella_dir = umbrella_path.parent
            if umbrella_dir.is_dir():
                for h in sorted(umbrella_dir.glob("*.h")):
                    if not h.is_file():
                        continue  # dangling symlink — nothing to copy
                    rel = h.relative_to(modulemap_dir)
                    rel_key = str(rel)
                    if rel_key not in seen_rel:
                        seen_rel.add(rel_key)
                        headers.append((h, rel))

        # Umbrella directory: walk recursively for .h files.
        for ud in _MODULEMAP_UMBRELLA_DIR_RE.finditer(text):
            # Skip if this match is actually `umbrella header "..."` —
            # the umbrella-header regex above already handled that.
            if 'umbrella header' in text[max(0, ud.start() - 20):ud.end() + 1]:
                continue
            dir_path = modulemap_dir / ud.group(1)
            if dir_path.is_dir():
                for h in sorted(dir_path.rglob("*.h")):
                    if not h.is_file():
                        continue  # dangling symlink — nothing to copy
                    rel = h.relative_to(modulemap_dir)
                    rel_key = str(rel)
                    if rel_key not in seen_rel:
                        seen_rel.add(rel_key)
                        headers.append((h, rel))

        # Explicit `header "X"` declarations.
        for hd in _MODULEMAP_HEADER_RE.finditer(text):
            header_rel = hd.group(1)
            header_path = modulemap_dir / header_rel
            if header_path.is_file():
                rel = Path(header_rel)
                rel_key = str(rel)
                if rel_key not in seen_rel:
                    seen_rel.add(rel_key)
                    headers.append((header_path, rel))

        if not headers:
            # Modulemap claims this module but we can't resolve any
            # headers from it (malformed, or all references point
            # outside the package). Better to surface than to silently
            # ship an empty shim.
            warn(
                f"  Found {modulemap_path.name} declaring {module_name!r} "
                f"but resolved no headers; cannot inject shim"
            )
            return None
        return (modulemap_path, headers)
    return None


def inject_bridge_clang_modules(
    *,
    xcframework_path: Path,
    package: Package,
    fw_name: str,
    verbose: bool,
) -> int:
    """Bundle every clang module the primary framework's swiftinterface
    imports but doesn't declare itself, sourced from a project-shipped
    `module.modulemap` in the package staged source tree.

    Mirrors `inject_system_clang_modules` for the project-shipped case.
    Without this, `import WCDB_Private` (and similar non-system modules
    a Swift target depends on via a custom modulemap) fails to resolve
    when the consumer's swiftc version differs from the build-time one
    and has to rebuild the swiftinterface.

    Returns the number of distinct shim frameworks injected (counted
    once per module name, not per slice). Idempotent: existing
    `<Name>.framework` siblings are skipped.
    """
    if not xcframework_path.is_dir():
        return 0

    slice_dirs = [p for p in sorted(xcframework_path.iterdir()) if p.is_dir()]
    if not slice_dirs:
        return 0

    # Pick the first slice's primary framework to scan swiftinterfaces
    # — every slice's swiftinterface has the same import set (Swift's
    # textual interface is target-triple-agnostic at the import level).
    primary_fw = slice_dirs[0] / f"{fw_name}.framework"
    if not primary_fw.is_dir():
        return 0

    imports = _scan_swiftinterface_imports(primary_fw)
    if not imports:
        return 0

    fw_modulemap = primary_fw / "Modules" / "module.modulemap"
    declared = (
        _modules_declared_in_modulemap(fw_modulemap.read_text(encoding="utf-8", errors="replace"))
        if fw_modulemap.is_file() else set()
    )
    # The framework itself is the most common "declared elsewhere"
    # case when its modulemap is absent (pure-Swift framework). Always
    # exclude the framework's own name + the swift built-ins.
    declared.add(fw_name)

    candidates = sorted(imports - declared - _BUILTIN_SWIFT_IMPORTS)
    if not candidates:
        return 0

    injected = 0
    for module_name in candidates:
        # Skip system modules: anything starting with `_` (Swift compiler
        # internals like `_Concurrency`, `_StringProcessing`,
        # `_SwiftConcurrencyShims`) is a stdlib-side import that doesn't
        # need a shim. System frameworks (Foundation, UIKit, …) won't
        # have a project-shipped modulemap and will fall through to the
        # `find` step returning None.
        if module_name.startswith("_"):
            continue
        match = _find_project_modulemap_for_module(package, module_name)
        if match is None:
            # Either a system framework (correctly skipped — consumers
            # link the SDK) or a genuinely missing dep we can't fix
            # from this layer. Stay quiet on the system case (most
            # candidates after dedup are systems) and only warn when
            # the modulemap WAS found but headers couldn't resolve
            # (handled inside the finder).
            continue
        modulemap_path, headers = match
        framework_modulemap = _promote_modulemap_to_framework_form(
            modulemap_path.read_text(encoding="utf-8", errors="replace")
        )

        any_slice_injected = False
        for slice_dir in slice_dirs:
            shim_fw = slice_dir / f"{module_name}.framework"
            if shim_fw.exists():
                continue  # idempotent
            shim_fw_modules = shim_fw / "Modules"
            shim_fw_headers = shim_fw / "Headers"
            shim_fw_modules.mkdir(parents=True, exist_ok=True)
            shim_fw_headers.mkdir(parents=True, exist_ok=True)
            (shim_fw_modules / "module.modulemap").write_text(framework_modulemap)
            # Preserve the modulemap-relative path under Headers/ so any
            # `umbrella header "include/U.h"` or `header "sub/A.h"` in the
            # original modulemap text still resolves against the shim's
            # header tree at consumer compile time. Mirrors the system-shim
            # path's relative-path preservation (see inject_system_clang_modules).
            for src_header, rel in headers:
                dest = shim_fw_headers / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_header, dest)
            (shim_fw / _SYSTEM_SHIM_SENTINEL).write_text(
                "spm-to-xcframework injected this framework as a Clang "
                "module shim for a project-shipped module.modulemap that "
                "the Swift target imports but xcodebuild didn't propagate "
                "into the primary framework. Do not delete; removing it "
                "makes language classification misclassify the parent "
                "xcframework.\n"
            )
            any_slice_injected = True

        if any_slice_injected:
            injected += 1
            dim(
                f"  Injected bridge Clang module shim: "
                f"{module_name}.framework (× {len(slice_dirs)} slice(s)) "
                f"from {modulemap_path.relative_to(package.staged_dir)}"
            )

    return injected
