"""Phase 4 — Execute · create-xcframework + dependency walker.

Drives `xcodebuild -create-xcframework` merges, and houses the
`--include-deps` walker that builds per-dependency xcframeworks from
device+sim archive output. Also owns the per-slice "primary framework"
discovery used by both Execute's surface checks and Verify's language
classification.
"""
from __future__ import annotations

import plistlib
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from ..errors import ExecuteError
from ..log import dim, success, verbose_log, warn
from ..model import (
    ArchiveSlice,
    BuildUnit,
    DependencyXcframework,
    Language,
    Package,
)

from .inject_clang_system import _SYSTEM_SHIM_SENTINEL
from .inject_objc import inject_objc_headers, inject_pure_swift_clang_modulemap
from .inject_resources import inject_resource_bundles
from .inject_swiftmodule import inject_swiftmodule


def create_xcframework(
    *,
    output_xcframework: Path,
    frameworks: Sequence[Path],
    verbose: bool,
) -> None:
    """Shell out to `xcodebuild -create-xcframework` with one or more
    `.framework` inputs. Removes any pre-existing output directory first
    (xcodebuild refuses to overwrite). Raises `ExecuteError` on failure
    or when `frameworks` is empty.

    Accepts 1..N inputs so single-slice platforms (macOS-only, Mac
    Catalyst-only) and the legacy iOS device+simulator pair share the
    same code path. xcodebuild handles heterogeneous-platform merges
    natively — iOS + macOS + tvOS frameworks in one invocation produce
    an xcframework with all the corresponding `*-arm64*` directories.
    """
    if not frameworks:
        raise ExecuteError(
            f"create_xcframework called with no frameworks for "
            f"{output_xcframework.name}"
        )
    if output_xcframework.exists():
        shutil.rmtree(output_xcframework)
    output_xcframework.parent.mkdir(parents=True, exist_ok=True)

    cmd: List[str] = ["xcodebuild", "-create-xcframework"]
    for fw in frameworks:
        cmd.extend(["-framework", str(fw)])
    cmd.extend(["-output", str(output_xcframework)])
    verbose_log(verbose, f"  $ {' '.join(cmd)}")
    cp = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if cp.returncode != 0:
        # Tail the output for diagnostics — same shape the legacy bash
        # uses (`tail -3` on the create-xcframework call).
        tail = "\n".join((cp.stdout or "").rstrip().splitlines()[-10:])
        raise ExecuteError(
            f"xcodebuild -create-xcframework failed for {output_xcframework.name}:\n"
            + (tail or "  (no output)")
        )


def _is_system_shim_framework(fw_path: Path) -> bool:
    """True iff `fw_path` is a system Clang module shim framework
    injected by `inject_system_clang_modules`. Detection is by sentinel
    file (`.spm-to-xcframework-system-shim`) written at injection time
    rather than structural inference, because a structural-only test
    can't reliably distinguish a header-only shim from an ObjC framework
    that hasn't been built yet (test fixtures, partial builds). The
    sentinel is invisible to xcframework consumers — Xcode and swiftc
    don't look for it — and writing it costs nothing.
    """
    if not fw_path.is_dir() or not fw_path.name.endswith(".framework"):
        return False
    return (fw_path / _SYSTEM_SHIM_SENTINEL).is_file()


def _read_xcframework_library_paths(xcfw_path: Path) -> Dict[str, str]:
    """Parse `Info.plist` and return `{LibraryIdentifier: LibraryPath}`.

    Returns an empty dict on any failure (missing plist, parse error,
    malformed AvailableLibraries) — the caller falls back to a direct-
    child `*.framework` scan in that case, which is fine for the legacy
    "framework lives directly under the slice" layout.

    The reason we read the plist at all is that `LibraryPath` may be a
    nested path like `Frameworks/Foo.framework`, in which case there is
    no top-level `*.framework` for the direct-child scan to find. Codex
    [P1]: without this lookup, `detect_framework_type` returns Unknown
    and `_verify_one_unit` silently passes those layouts through with
    no language surface checks at all.
    """
    info_plist = xcfw_path / "Info.plist"
    if not info_plist.is_file():
        return {}
    try:
        with info_plist.open("rb") as fh:
            data = plistlib.load(fh)
    except (plistlib.InvalidFileException, OSError, ValueError):
        return {}
    except Exception:  # noqa: BLE001 — defensive: any plist corruption → fall back
        return {}
    available = data.get("AvailableLibraries")
    if not isinstance(available, list):
        return {}
    result: Dict[str, str] = {}
    for entry in available:
        if not isinstance(entry, dict):
            continue
        identifier = entry.get("LibraryIdentifier")
        library_path = entry.get("LibraryPath")
        if isinstance(identifier, str) and isinstance(library_path, str) and library_path:
            result[identifier] = library_path
    return result


def _pick_primary_framework_in_slice(
    slice_dir: Path,
    library_path: Optional[str] = None,
) -> Optional[Path]:
    """Return the primary `.framework/` directory inside an xcframework
    slice.

    Resolution order:
      1. If the caller passed `library_path` (the slice's `LibraryPath`
         from `Info.plist`), resolve `slice_dir / library_path` and
         return it as long as it exists and ends in `.framework`. This
         is the only way to find frameworks that live in nested layouts
         like `Frameworks/Foo.framework` (Codex [P1]).
      2. Otherwise (or if the plist path doesn't resolve on disk), scan
         direct children for `*.framework` directories, skip sibling
         system Clang module shim frameworks injected by
         `inject_system_clang_modules`, and return the alphabetically
         first real framework.

    Falling back to "alphabetically first" matters because real-world
    xcframeworks always have a single primary framework per slice; the
    only reason this function exists at all is to filter out the
    binary-less system shims so they don't pollute language detection.
    """
    if not slice_dir.is_dir():
        return None
    if library_path:
        # Strip any leading "./" but otherwise preserve the relative
        # path verbatim — the plist is authoritative for nested layouts.
        primary = slice_dir / library_path.lstrip("./")
        if primary.is_dir() and primary.name.endswith(".framework"):
            return primary
        # Plist disagreement with disk; fall through to scan rather than
        # silently returning None.
    for entry in sorted(slice_dir.iterdir()):
        if not entry.is_dir() or not entry.name.endswith(".framework"):
            continue
        if _is_system_shim_framework(entry):
            continue
        return entry
    return None


def _iter_primary_framework_paths(xcfw_path: Path) -> Iterable[Path]:
    """Yield every file/dir path inside the primary framework of each
    xcframework slice. Skips sibling system shim frameworks injected by
    `inject_system_clang_modules` (they have no Mach-O binary).

    Reads the xcframework's `Info.plist` once up front to learn each
    slice's `LibraryPath`, so nested layouts (`Frameworks/Foo.framework`)
    are picked up by the same walk that handles the conventional
    `slice/Foo.framework` layout. If the plist is missing or malformed,
    falls back to per-slice direct-child scanning.

    Used by both `detect_framework_type` and the per-slice walk in
    `_verify_one_unit` so language classification and surface checks
    agree on what counts as "the framework" — and stay agnostic to
    augmentations like the system Clang module shim.
    """
    if not xcfw_path.is_dir():
        return
    library_paths = _read_xcframework_library_paths(xcfw_path)
    for slice_dir in sorted(xcfw_path.iterdir()):
        if not slice_dir.is_dir():
            continue
        primary_fw = _pick_primary_framework_in_slice(
            slice_dir,
            library_path=library_paths.get(slice_dir.name),
        )
        if primary_fw is None:
            continue
        yield from primary_fw.rglob("*")


def detect_framework_type(xcfw_path: Path) -> str:
    """Classify an xcframework as Swift / ObjC / Mixed / Unknown by walking
    its primary framework's contents. Used by Execute's per-unit summary
    line and (in Session 5) by the Verify phase. Same logic as the legacy
    bash detect_framework_type, refined to ignore sibling system shim
    frameworks injected for `.systemLibrary` Clang module deps.
    """
    has_swift = False
    has_objc = False
    for path in _iter_primary_framework_paths(xcfw_path):
        name = path.name
        if not has_swift and (name.endswith(".swiftinterface") or name.endswith(".swiftmodule")):
            has_swift = True
        if not has_objc and name.endswith(".h") and not name.endswith("-Swift.h"):
            # Only count headers under a Headers/ directory — that's
            # the SPM convention for public ObjC API. Bridge headers
            # outside Headers/ are framework-internal noise.
            if "Headers" in path.parts:
                has_objc = True
        if has_swift and has_objc:
            break
    if has_swift and has_objc:
        return "Mixed"
    if has_swift:
        return "Swift"
    if has_objc:
        return "ObjC"
    return "Unknown"


def _build_dependency_xcframeworks(
    *,
    unit: BuildUnit,
    package: Package,
    device_slice: ArchiveSlice,
    sim_slice: ArchiveSlice,
    primary_fw_name: str,
    output_dir: Path,
    verbose: bool,
) -> List[DependencyXcframework]:
    """Walk the device archive for `.framework` bundles that aren't the
    primary, inject swiftmodule + ObjC headers into each, and merge them
    into per-dependency xcframeworks under `output_dir`.

    Mirrors the legacy bash `build_dependency_xcframeworks` (lines
    1228-1270). Skips:
      - the primary framework itself (matched by both unit name and
        resolved fw_name in case those differ)
      - dependencies that already have a built `<X>.xcframework` in
        `output_dir` (avoids re-creating Stripe-style sub-frameworks
        that the user explicitly asked for via `--target`)
      - dependencies whose simulator counterpart isn't present (a
        device-only framework can't be merged)

    Each returned `DependencyXcframework` carries an `expected_language`
    derived from the matching target in the Package model, so Verify
    can enforce the same plan-time language contract on deps that it
    enforces on primary units (Codex follow-up).
    """
    products_dir = device_slice.archive_path / "Products"
    if not products_dir.is_dir():
        return []
    built: List[DependencyXcframework] = []
    for fw_path in sorted(products_dir.rglob("*.framework")):
        if not fw_path.is_dir():
            continue
        fw_name = fw_path.stem
        if fw_name in (unit.name, primary_fw_name):
            continue
        if (output_dir / f"{fw_name}.xcframework").is_dir():
            continue
        sim_products = sim_slice.archive_path / "Products"
        sim_fw: Optional[Path] = None
        if sim_products.is_dir():
            for candidate in sim_products.rglob(f"{fw_name}.framework"):
                if candidate.is_dir():
                    sim_fw = candidate
                    break
        if sim_fw is None:
            continue
        dim(f"  Dependency: {fw_name}")
        inject_swiftmodule(
            fw_path=fw_path,
            fw_name=fw_name,
            scheme=fw_name,
            dd_path=device_slice.dd_path,
            variant="device",
            verbose=verbose,
        )
        inject_swiftmodule(
            fw_path=sim_fw,
            fw_name=fw_name,
            scheme=fw_name,
            dd_path=sim_slice.dd_path,
            variant="simulator",
            verbose=verbose,
        )
        # Use the dependency name as both product_name and fw_name when
        # looking for ObjC headers — we don't have a richer mapping.
        inject_objc_headers(
            package=package,
            product_name=fw_name,
            fw_name=fw_name,
            fw_path=fw_path,
            verbose=verbose,
        )
        inject_objc_headers(
            package=package,
            product_name=fw_name,
            fw_name=fw_name,
            fw_path=sim_fw,
            verbose=verbose,
        )
        # Synthesize a Clang modulemap when the dep is pure-Swift with
        # @objc surface so the umbrella's ObjC code (or another mixed
        # dep) can `@import Dep` / `#import <Dep/Dep-Swift.h>`. No-op
        # when inject_objc_headers already wrote a modulemap, when no
        # `-Swift.h` exists in DerivedData, or when the framework has
        # no .swiftmodule (ObjC-only dep). Parallels the run_unit call
        # site so deps and the primary unit produce equivalent bundles.
        inject_pure_swift_clang_modulemap(
            fw_path=fw_path,
            fw_name=fw_name,
            dd_path=device_slice.dd_path,
            variant="device",
            verbose=verbose,
        )
        inject_pure_swift_clang_modulemap(
            fw_path=sim_fw,
            fw_name=fw_name,
            dd_path=sim_slice.dd_path,
            variant="simulator",
            verbose=verbose,
        )
        inject_resource_bundles(
            fw_path=fw_path,
            fw_name=fw_name,
            dd_path=device_slice.dd_path,
            variant="device",
            verbose=verbose,
        )
        inject_resource_bundles(
            fw_path=sim_fw,
            fw_name=fw_name,
            dd_path=sim_slice.dd_path,
            variant="simulator",
            verbose=verbose,
        )
        dep_xcframework = output_dir / f"{fw_name}.xcframework"
        try:
            create_xcframework(
                output_xcframework=dep_xcframework,
                frameworks=[fw_path, sim_fw],
                verbose=verbose,
            )
        except ExecuteError as exc:
            warn(f"  Failed to create {fw_name}.xcframework (dependency): {exc}")
            continue
        fw_type = detect_framework_type(dep_xcframework)
        success(f"  {fw_name}.xcframework ready (dependency) [{fw_type}]")
        # Resolve the dependency's expected language by matching
        # `fw_name` to a target in the Package model. xcodebuild
        # preserves the target name as the framework name, so this
        # lookup is reliable for internal targets. External
        # packages (transitive deps from a .package(…) clause) are
        # NOT in `package.targets`, so they stay as N/A and Verify
        # falls back to post-hoc detection for those — the plan
        # never had an opinion about their language to begin with.
        dep_target = package.target_by_name(fw_name)
        dep_language = Language.NA
        if dep_target is not None and dep_target.language in (
            Language.SWIFT, Language.OBJC, Language.MIXED
        ):
            dep_language = dep_target.language
        built.append(
            DependencyXcframework(path=dep_xcframework, expected_language=dep_language)
        )
    return built
