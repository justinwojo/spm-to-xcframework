"""Phase 4 — Execute · post-archive framework rename.

When the planner emits a `synth_dynamic_library` edit, the build unit's
`scheme` is the synthetic product name (e.g. `AlamofireDynamic`) while
`framework_name` is the original product name (e.g. `Alamofire`).
xcodebuild against the synthetic scheme produces
`{synthetic}.framework`; downstream consumers expect
`{framework_name}.framework`, so we rename the bundle in place once per
slice between archive and injection.

The rename is platform-aware: macOS frameworks ship in the versioned
bundle layout (`Versions/A/<exec>` with root symlinks), while every
other Apple SDK uses the flat layout. Both shapes have to be handled —
codesigning rejects a versioned framework whose root holds anything
other than `Versions/` plus symlinks, so a flat-style rename on a
macOS bundle would break consumers.

The install_name written by `install_name_tool -id` uses the FLAT form
(`@rpath/X.framework/X`) even on macOS. The top-level
`X -> Versions/Current/X` symlink at the framework root resolves this
on macOS, and Verify only checks the binary is dynamically linked
(`file ... dynamically linked`) and never inspects `LC_ID_DYLIB`, so
the flat form is safe for our consumer contract and avoids the
surprise of two different install-name shapes inside the same
xcframework.
"""
from __future__ import annotations

import plistlib
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from ..errors import ExecuteError
from ..log import dim, verbose_log


def _is_versioned_bundle(fw_path: Path) -> bool:
    """True iff `fw_path` is a macOS-style versioned bundle (real
    `Versions/A/` at the root, not a symlink)."""
    versions_a = fw_path / "Versions" / "A"
    return versions_a.is_dir() and not versions_a.is_symlink()


def _read_plist_string(plist_path: Path, key: str) -> Optional[str]:
    """Return the top-level string entry at `key`, or None if the file
    is missing, malformed, or the key isn't a string. Used to inspect
    the original CFBundleIdentifier before deciding whether to rewrite
    it — see the rename step in `rename_framework_bundle`.
    """
    if not plist_path.is_file():
        return None
    try:
        with plist_path.open("rb") as fh:
            data = plistlib.load(fh)
    except (plistlib.InvalidFileException, OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    v = data.get(key)
    return v if isinstance(v, str) else None


def _set_plist_string(plist_path: Path, key: str, value: str) -> None:
    """Set a top-level string entry in a (binary or XML) plist. Uses
    `plistlib` rather than PlistBuddy so we don't depend on the
    `/usr/libexec/PlistBuddy` binary being on PATH and so we can write
    binary plists back in the same format we read.
    """
    if not plist_path.is_file():
        raise ExecuteError(f"Info.plist missing at {plist_path}")
    with plist_path.open("rb") as fh:
        data = plistlib.load(fh)
    # Preserve the original format (binary vs XML). plistlib re-derives
    # this from the file header when reading; on write we need to
    # request it explicitly.
    fmt = plistlib.FMT_XML
    with plist_path.open("rb") as fh:
        head = fh.read(8)
        if head.startswith(b"bplist"):
            fmt = plistlib.FMT_BINARY
    if not isinstance(data, dict):
        raise ExecuteError(f"Info.plist at {plist_path} is not a dict")
    data[key] = value
    with plist_path.open("wb") as fh:
        plistlib.dump(data, fh, fmt=fmt)


def _run(cmd: list, *, verbose: bool, label: str) -> None:
    verbose_log(verbose, f"  $ {' '.join(cmd)}")
    cp = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    if cp.returncode != 0:
        tail = "\n".join((cp.stdout or "").rstrip().splitlines()[-10:])
        raise ExecuteError(
            f"{label} failed (exit {cp.returncode}):\n" + (tail or "  (no output)")
        )


def rename_framework_bundle(
    fw_path: Path,
    *,
    new_name: str,
    verbose: bool,
) -> Path:
    """Rename a framework bundle and its inner executable + metadata in
    place, returning the path to the renamed bundle.

    Steps:
      1. Rename the inner Mach-O (`Versions/A/<old>` on macOS, `<old>`
         at the root on iOS et al.) to the new name.
      2. Rewrite the binary's `LC_ID_DYLIB` to
         `@rpath/<new>.framework/<new>` via `install_name_tool -id`.
      3. Update `Info.plist` (`Versions/A/Resources/Info.plist` on
         macOS, `Info.plist` at the root otherwise): CFBundleExecutable
         and CFBundleName become `<new>`. CFBundleIdentifier is
         rewritten only when its existing value mentions the old
         scheme name (xcodebuild's default `org.swift.<scheme>` shape),
         to avoid clobbering a custom PRODUCT_BUNDLE_IDENTIFIER.
      4. Rename the framework bundle directory itself
         (`<old>.framework` → `<new>.framework`).
      5. On macOS only, replace the root symlink that pointed at the
         old executable name with one pointing at the new name; same
         for the root Resources symlink (no rename needed) and
         Versions/Current (already "A").
      6. Re-codesign ad-hoc (`codesign --force --sign -`). xcodebuild
         signs the framework during archive with whatever identity the
         destination requires (`-` for simulator/macOS); re-signing
         after any binary or bundle mutation is mandatory or downstream
         consumers (codesign --verify, dyld signature checks on real
         devices) reject the framework.

    Returns the new framework path (`<parent>/<new>.framework`).
    """
    if not fw_path.is_dir():
        raise ExecuteError(f"rename: framework path not a directory: {fw_path}")
    old_name = fw_path.stem
    if old_name == new_name:
        return fw_path  # nothing to do

    versioned = _is_versioned_bundle(fw_path)
    dim(f"  Renaming {old_name}.framework → {new_name}.framework")

    # Step 1: inner executable rename.
    if versioned:
        inner_dir = fw_path / "Versions" / "A"
    else:
        inner_dir = fw_path
    old_exec = inner_dir / old_name
    new_exec = inner_dir / new_name
    if not old_exec.is_file():
        raise ExecuteError(
            f"rename: inner executable {old_exec} not found"
        )
    if new_exec.exists():
        # Defensive: a prior failed rename may have left a stale
        # destination. Remove it so the rename below doesn't fail.
        new_exec.unlink()
    old_exec.rename(new_exec)

    # Step 2: rewrite LC_ID_DYLIB. Flat install_name form so both iOS
    # and macOS use the same shape; the macOS top-level symlink
    # resolves it.
    _run(
        [
            "install_name_tool",
            "-id",
            f"@rpath/{new_name}.framework/{new_name}",
            str(new_exec),
        ],
        verbose=verbose,
        label="install_name_tool -id",
    )

    # Step 3: Info.plist updates.
    if versioned:
        plist = inner_dir / "Resources" / "Info.plist"
    else:
        plist = fw_path / "Info.plist"
    _set_plist_string(plist, "CFBundleExecutable", new_name)
    _set_plist_string(plist, "CFBundleName", new_name)
    # Only rewrite CFBundleIdentifier if xcodebuild produced the
    # default SPM-shaped identifier (`org.swift.<scheme>`) — that's
    # the one case where the old scheme name is structural and the
    # rename target name must follow it. Packages that set their own
    # PRODUCT_BUNDLE_IDENTIFIER or INFO_PLIST entry end up with an
    # identifier that's user-chosen and unrelated to the scheme;
    # rewriting it would silently lose that state. We also exclude
    # custom identifiers that happen to mention the old scheme name
    # (e.g. `com.acme.FooDynamic.module`) — Codex round-2 Low.
    old_id = _read_plist_string(plist, "CFBundleIdentifier")
    if old_id is not None and old_id.startswith("org.swift.") and old_name in old_id:
        new_id = old_id.replace(old_name, new_name)
        _set_plist_string(plist, "CFBundleIdentifier", new_id)

    # Step 4: rename the bundle directory itself.
    new_fw_path = fw_path.parent / f"{new_name}.framework"
    if new_fw_path.exists():
        # Same defensive cleanup as the executable case.
        shutil.rmtree(new_fw_path)
    fw_path.rename(new_fw_path)

    # Step 5: on macOS, swap the root symlink that pointed at the old
    # executable name for one pointing at the new name. `Versions/Current`
    # (-> A) and the Resources root symlink don't need renaming — they're
    # already content-shaped.
    if versioned:
        old_root_link = new_fw_path / old_name
        if old_root_link.is_symlink():
            old_root_link.unlink()
        new_root_link = new_fw_path / new_name
        if new_root_link.exists() or new_root_link.is_symlink():
            new_root_link.unlink()
        new_root_link.symlink_to(Path("Versions") / "Current" / new_name)

    # Step 6: ad-hoc re-codesign. Required after any binary or bundle
    # mutation — without it `codesign --verify` and on-device dyld
    # signature checks fail.
    _run(
        ["codesign", "--force", "--sign", "-", str(new_fw_path)],
        verbose=verbose,
        label="codesign --force --sign -",
    )

    return new_fw_path
