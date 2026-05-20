"""Phase 4 — Execute · Swift `.swiftmodule/.swiftinterface` injection.

Ports the legacy bash inject_swiftmodule pass plus the shared
`_framework_content_root` / `_ensure_root_symlink` helpers used by every
injection pass to keep macOS versioned bundles vs iOS flat bundles
agnostic at the call site.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional, Sequence, Tuple

from ..log import dim, verbose_log


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
    _ensure_root_symlink(fw_path, "Modules")
    return True
