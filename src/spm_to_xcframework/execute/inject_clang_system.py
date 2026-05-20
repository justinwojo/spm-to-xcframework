"""Phase 4 — Execute · `.systemLibrary` Clang module shim injection.

When a regular Swift target depends on a `.systemLibrary` target (e.g.
GRDB → GRDBSQLite, which wraps `<sqlite3.h>`), swiftc emits `import
GRDBSQLite` into the framework's `.swiftinterface`. Any consumer whose
swiftc version differs from the build-time one has to rebuild the
interface, and the `import` then fails with `error: no such module
'GRDBSQLite'` unless the shim's module.modulemap is reachable.

This pass packages each transitive `.systemLibrary` modulemap + its
shim headers as a binary-less sibling `<Name>.framework` inside every
xcframework slice. swiftc discovers them via `-F` framework search at
consumer compile time without any additional flags.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from ..inspect import _raw_internal_dep_names
from ..log import dim, warn
from ..model import Package, Target, TargetKind


_SYSTEM_SHIM_SENTINEL = ".spm-to-xcframework-system-shim"


def _walk_system_library_target_deps(
    package: Package,
    source_targets: Sequence[str],
) -> List[Target]:
    """Return every transitive `.systemLibrary` target reachable from
    `source_targets` via internal `byName` / `target` dependency edges.

    Walks the raw dump rather than `Target.dependencies` because the typed
    list flattens out the `byName` vs `target` shapes — same convention as
    `_find_objc_headers_dir`. Result order is the discovery order (BFS)
    to keep diagnostics deterministic.
    """
    raw_targets_by_name: Dict[str, dict] = {}
    for raw_t in package.raw_dump.get("targets", []) or []:
        if isinstance(raw_t, dict) and isinstance(raw_t.get("name"), str):
            raw_targets_by_name[raw_t["name"]] = raw_t

    found: List[Target] = []
    seen: Set[str] = set()
    queue: List[str] = list(source_targets)
    while queue:
        name = queue.pop(0)
        if name in seen:
            continue
        seen.add(name)
        target = package.target_by_name(name)
        if target is None:
            continue
        if target.kind == TargetKind.SYSTEM:
            found.append(target)
            # System targets do not transitively depend on other targets
            # in any shape we care about — they are leaves by SPM design.
            continue
        raw = raw_targets_by_name.get(name)
        if raw is None:
            continue
        for dep_name in _raw_internal_dep_names(raw):
            if dep_name not in seen:
                queue.append(dep_name)
    return found


def _system_target_source_dir(target: Target, staged_dir: Path) -> Optional[Path]:
    """Locate the on-disk source directory for a `.systemLibrary` target.

    SPM convention: `Sources/<TargetName>/` unless `path:` is set
    explicitly. `_default_target_path` only returns paths for regular /
    executable / test kinds, so we duplicate the system-target convention
    here rather than widening that helper.
    """
    if target.path:
        full = staged_dir / target.path
    else:
        full = staged_dir / "Sources" / target.name
    return full if full.is_dir() else None


def _promote_modulemap_to_framework_form(modulemap_text: str) -> str:
    """Rewrite an SPM `.systemLibrary` modulemap so it can live inside a
    `.framework/Modules/module.modulemap` and be discovered via swiftc's
    `-F` framework search.

    Input  (SPM systemLibrary):  `module Foo [system] { header "shim.h" link "x" export * }`
    Output (framework module):   `framework module Foo [system] { header "shim.h" link "x" export * }`

    The only required change is the `framework ` qualifier on the
    top-level `module` declaration. Modern swiftc accepts plain `header`
    inside a framework module — `umbrella header` is not required when
    the framework has a single shim header (verified empirically against
    Swift 6.2.3, Xcode 26.2 simulator SDK).
    """
    return re.sub(
        r"(?m)^(\s*)module(\s+\w+)",
        r"\1framework module\2",
        modulemap_text,
        count=1,
    )


def inject_system_clang_modules(
    *,
    xcframework_path: Path,
    package: Package,
    source_targets: Sequence[str],
    verbose: bool,
) -> int:
    """Bundle every transitive `.systemLibrary` Clang module that the
    primary framework depends on as a sibling shim framework inside each
    xcframework slice. No-op if `source_targets` has no system deps.

    Background: when a regular Swift target depends on a `.systemLibrary`
    target (e.g. GRDB → GRDBSQLite, which wraps `<sqlite3.h>`), the
    Swift compiler always emits `import GRDBSQLite` into the framework's
    `.swiftinterface`. Any consumer that has to rebuild GRDB from its
    textual interface — every consumer whose Swift compiler version
    differs from the build-time one — needs `GRDBSQLite` to resolve as a
    Clang module, or the import fails with `error: no such module
    'GRDBSQLite'` and the framework is unconsumable.

    The system product itself is correctly dropped at planning time
    (`_is_system_only_product`) because there is no Mach-O to build, but
    its modulemap+headers must travel with the parent framework so the
    Clang module can be loaded at consumer compile time. We bundle them
    as a binary-less sibling `<Name>.framework` inside each slice
    directory of the xcframework. swiftc auto-discovers it via `-F`
    framework search — no extra `-I` / `-Xcc` flags required on the
    consumer side, including non-spm-to-xcframework consumers like
    plain Xcode app projects.

    Returns the number of system shims injected (counted once per
    distinct system target, not per slice). Idempotent: existing
    `<Name>.framework` siblings are skipped.
    """
    if not xcframework_path.is_dir():
        return 0

    system_targets = _walk_system_library_target_deps(package, source_targets)
    if not system_targets:
        return 0

    # Discover slice directories. The xcframework Info.plist lists them
    # under AvailableLibraries[].LibraryIdentifier, but we don't need to
    # parse it — every direct subdirectory of the xcframework other than
    # Info.plist is a slice. This avoids a plistlib import in the hot
    # path and matches what xcframework consumers do.
    slice_dirs = [
        p for p in sorted(xcframework_path.iterdir())
        if p.is_dir()
    ]
    if not slice_dirs:
        return 0

    injected = 0
    for sys_target in system_targets:
        src_dir = _system_target_source_dir(sys_target, package.staged_dir)
        if src_dir is None:
            warn(
                f"  System target {sys_target.name!r} has no resolvable "
                f"source dir under {package.staged_dir}; consumers will "
                f"fail to import {sys_target.name}"
            )
            continue
        modulemap = src_dir / "module.modulemap"
        if not modulemap.is_file():
            warn(
                f"  System target {sys_target.name!r} source dir {src_dir} "
                f"has no module.modulemap; skipping shim injection"
            )
            continue
        # Headers are every `.h` file under the system-target source
        # dir, walked recursively. SPM `.systemLibrary` targets usually
        # park a single `shim.h` next to the modulemap, but the modulemap
        # text (which we preserve verbatim aside from the framework
        # qualifier) is free to `header "Sub/foo.h"` into a nested path.
        # We must preserve those relative paths into the shim's Headers/
        # tree so the modulemap's references still resolve at consumer
        # compile time (Codex [P2] follow-up — flat copy silently
        # produced broken shims for nested layouts).
        headers: List[Tuple[Path, Path]] = []
        for p in sorted(src_dir.rglob("*.h")):
            if not p.is_file():
                continue
            rel = p.relative_to(src_dir)
            headers.append((p, rel))
        framework_modulemap = _promote_modulemap_to_framework_form(
            modulemap.read_text()
        )

        any_slice_injected = False
        for slice_dir in slice_dirs:
            shim_fw = slice_dir / f"{sys_target.name}.framework"
            if shim_fw.exists():
                continue  # idempotent — second run is a no-op
            shim_fw_modules = shim_fw / "Modules"
            shim_fw_headers = shim_fw / "Headers"
            shim_fw_modules.mkdir(parents=True, exist_ok=True)
            shim_fw_headers.mkdir(parents=True, exist_ok=True)
            (shim_fw_modules / "module.modulemap").write_text(framework_modulemap)
            for src_header, rel in headers:
                dest = shim_fw_headers / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_header, dest)
            # Sentinel file used by `_is_system_shim_framework` to skip
            # this directory during language classification and
            # surface-check walks. Invisible to Xcode/swiftc consumers.
            (shim_fw / _SYSTEM_SHIM_SENTINEL).write_text(
                "spm-to-xcframework injected this framework as a Clang "
                "module shim for a .systemLibrary SPM target. Do not "
                "delete; removing it makes language classification "
                "misclassify the parent xcframework.\n"
            )
            any_slice_injected = True

        if any_slice_injected:
            injected += 1
            dim(
                f"  Injected system Clang module shim: "
                f"{sys_target.name}.framework (× {len(slice_dirs)} slice(s))"
            )

    return injected
