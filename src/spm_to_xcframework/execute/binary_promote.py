"""Phase 4 — Execute · binary-mode static→dynamic promotion + copy driver.

Vendor-shipped xcframeworks (e.g. KidozSDK 10.1.5) sometimes layer a
correct bundle layout over a `current ar archive` slice binary — Apple
allows it, but `dlopen` consumers (.NET P/Invoke in swift-bindings) can't
load a static archive. We re-link each slice binary in place as a real
dynamic Mach-O via `clang -dynamiclib + -all_load + -undefined
dynamic_lookup`, the same recipe the source pipeline used to apply
before the rewrite collapsed that path into the rename-after-archive
flow.

`execute_binary_plan` orchestrates the copy + promotion pass for every
`copy-artifact` build unit in the plan, and is the binary-mode mirror of
`run_unit.execute_source_plan`.
"""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional

from ..errors import ExecuteError
from ..log import bold, info, success, verbose_log, warn
from ..config import Config
from ..model import ExecutedUnit, Language, Plan
from ..platforms import (
    PlatformSlice,
    _PLATFORM_SLICES,
    _enabled_platforms,
    _expected_slice_classes,
    _platform_from_library_identifier,
    _variant_for_platform_slice,
    _variant_from_library_identifier,
)
from .create_xcframework import detect_framework_type


def _lipo_archs(static_lib: Path) -> List[str]:
    """Return the architectures present in a Mach-O archive, via
    `lipo -archs`. Empty list on failure (lipo missing, file is not a
    Mach-O object, etc.) — the caller treats that as "can't promote".
    """
    cp = subprocess.run(
        ["lipo", "-archs", str(static_lib)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if cp.returncode != 0:
        return []
    return [a for a in cp.stdout.strip().split() if a]


def _slice_minimum_deployment_target(
    available_entry: dict,
    platform: str,
) -> str:
    """Pull the slice's `MinimumOSVersion`/`MinimumDeploymentTarget` out
    of an xcframework `AvailableLibraries[]` entry. Falls back to a
    permissive low version per platform when the plist doesn't carry one
    — clang only uses it to set the load command, and the
    `-undefined dynamic_lookup` safety net means the link itself does not
    depend on the precise minimum.
    """
    for key in ("MinimumOSVersion", "MinimumDeploymentTarget"):
        v = available_entry.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return {
        "ios": "13.0",
        "macos": "11.0",
        "maccatalyst": "13.0",
        "tvos": "13.0",
        "watchos": "6.0",
        "visionos": "1.0",
    }.get(platform, "13.0")


def _platform_slice_for_library_identifier(lid: str) -> Optional[PlatformSlice]:
    """Map an xcframework `LibraryIdentifier` (e.g. `ios-arm64`,
    `ios-arm64_x86_64-simulator`) onto the matching `PlatformSlice` so we
    can pick the right SDK + clang min-version flag template. Returns
    None for identifiers we don't recognise; the caller treats that as
    "can't promote, surface the original verifier error".
    """
    platform = _platform_from_library_identifier(lid)
    if platform is None:
        return None
    variant = _variant_from_library_identifier(lid)
    for s in _PLATFORM_SLICES.get(platform, ()):
        if _variant_for_platform_slice(s) == variant:
            return s
    return None


def _filter_xcframework_slices_to_requested_platforms(
    xcframework_path: Path,
    requested_platforms: List[str],
    *,
    verbose: bool = False,
) -> List[str]:
    """Drop slices whose platform the user didn't request via `--min-*`.

    Vendor xcframeworks routinely ship more platforms than any one
    consumer asks for — Sentry 8.x ships ios+ios-simulator+ios-maccatalyst
    +macos+tvos+tvos-simulator+watchos+visionos. If the user only asked
    for `--min-ios`, the unrequested slices contribute nothing to their
    build, and worse: a static maccatalyst slice triggers a
    `-mtargetos=ios<ver>-macabi` clang invocation in
    `promote_binary_xcframework_static_to_dynamic` that not every
    installed clang accepts.

    Mutates the xcframework in place: deletes each unrequested slice
    directory and rewrites `Info.plist`'s `AvailableLibraries` to match.
    Returns the list of identifiers removed (empty list when the vendor
    xcframework already matches the requested set, including binary-only
    auto-detect where the requested set was derived from the slices
    themselves).

    Errors:
      - If requested_platforms is empty (defensive — caller should have
        validated this before binary execute starts), no filtering runs.
      - If the filter would leave zero slices, raise ExecuteError with
        the requested-vs-available breakdown so the user knows the vendor
        xcframework doesn't cover what they asked for.
    """
    if not requested_platforms:
        return []
    info_plist = xcframework_path / "Info.plist"
    if not info_plist.is_file():
        return []
    try:
        with info_plist.open("rb") as fh:
            data = plistlib.load(fh)
    except (plistlib.InvalidFileException, OSError, ValueError):
        return []
    available = data.get("AvailableLibraries")
    if not isinstance(available, list):
        return []

    requested = set(requested_platforms)
    kept: List[dict] = []
    removed: List[str] = []
    seen_platforms: set[str] = set()
    for entry in available:
        if not isinstance(entry, dict):
            kept.append(entry)
            continue
        identifier = entry.get("LibraryIdentifier")
        if not isinstance(identifier, str):
            kept.append(entry)
            continue
        platform = _platform_from_library_identifier(identifier)
        if platform is None:
            # Unrecognised — keep, let Verify surface the oddity rather
            # than silently dropping something we don't understand.
            kept.append(entry)
            continue
        seen_platforms.add(platform)
        if platform in requested:
            kept.append(entry)
        else:
            removed.append(identifier)
            slice_dir = xcframework_path / identifier
            if slice_dir.is_dir():
                shutil.rmtree(slice_dir, ignore_errors=True)

    if not kept:
        avail = ", ".join(sorted(seen_platforms)) or "(none recognisable)"
        req = ", ".join(sorted(requested))
        raise ExecuteError(
            f"Filtering {xcframework_path.name} to requested platforms "
            f"left zero slices. Requested: {req}. Vendor xcframework ships: "
            f"{avail}."
        )

    if removed:
        data["AvailableLibraries"] = kept
        with info_plist.open("wb") as fh:
            plistlib.dump(data, fh)
        # An upstream-signed xcframework (e.g. Mapbox's Turf) carries a
        # top-level `_CodeSignature/CodeResources` manifest that hashes
        # `Info.plist` plus every slice directory it shipped. Once we
        # rewrite `Info.plist` and delete unrequested slice dirs the
        # manifest no longer matches reality, and any downstream archive
        # that runs Xcode's `SignatureCollection` build phase on the
        # consumed sibling fails with an opaque `SWBUtil.CodeSignatureInfo.
        # Error error 0`. We can't re-sign with the upstream identity, so
        # drop the now-invalid manifest — the kept per-slice
        # `Framework/_CodeSignature/` entries still describe their own
        # binaries correctly, and the final app archive will re-sign on
        # embed.
        top_sig = xcframework_path / "_CodeSignature"
        if top_sig.is_dir():
            shutil.rmtree(top_sig, ignore_errors=True)
        verbose_log(
            verbose,
            f"  Dropped {len(removed)} unrequested slice(s) from "
            f"{xcframework_path.name}: {', '.join(removed)}",
        )
    return removed


def promote_binary_xcframework_static_to_dynamic(
    xcframework_path: Path,
    *,
    verbose: bool = False,
) -> List[str]:
    """Re-link every static-archive slice binary in a vendor xcframework as
    a dynamic Mach-O, in place. Returns the list of `LibraryIdentifier`s
    that were promoted (empty list when every slice is already dynamic).

    Why this exists: some upstream SDKs (Kidoz, others) ship xcframeworks
    whose `<slice>/<X>.framework/<X>` files are `current ar archive`
    static libraries rather than dynamically-linked Mach-O. Apple's
    xcframework spec tolerates this layout, but downstream consumers
    that need to `dlopen` the framework — notably .NET P/Invoke in the
    swift-bindings pipeline — cannot load a static archive. We rewrite
    each slice's binary into a real dynamic library using `clang
    -dynamiclib + -Xlinker -all_load + -Xlinker -undefined
    dynamic_lookup`, and drop the now-stale `_CodeSignature/` (Xcode
    resigns on embed; the user can also `codesign --force` post-hoc).

    System framework discovery is out of reach here — we don't have the
    Package.swift target to scan, only an opaque set of object files.
    The `-undefined dynamic_lookup` safety net handles that: any UIKit
    / Foundation / SDK symbol the static archive references is resolved
    at runtime by dyld instead of at link time.

    Side effects (per slice that needs promotion):
      - Replace `<slice>/<lib>/<X>` with a freshly-linked dynamic Mach-O.
      - Delete `<slice>/<lib>/_CodeSignature/` (no longer matches bytes).
      - macOS versioned bundles: same operation under `Versions/A/`.

    Raises `ExecuteError` if any slice promotion fails. Mid-loop
    failures leave already-promoted slices in their new state — Verify
    will surface the remaining static one and the run reports a clean
    failure.
    """
    # Late-bind `_check_binary_dynamic` through the package namespace so
    # tests that monkey-patch `spm_to_xcframework._check_binary_dynamic`
    # are honored at call time. A `from ..verify import _check_binary_dynamic`
    # at module top would freeze the original function reference into this
    # module's globals and tests' patches would be silently ignored.
    #
    # Side benefit: this is also what makes the single-file
    # concatenated form (`build_single_file.py`) work. The function
    # `_check_binary_dynamic` is defined in `verify.py`, which pastes
    # AFTER this module in `MODULE_ORDER`. A module-top `from ..verify
    # import` would translate to nothing in the single file (the
    # stripper drops it), and any top-level reference here would
    # `NameError` at import time. The function-body late-bind defers
    # the lookup until call time, by which point the entire script
    # has executed top-to-bottom and the global is bound. If you add a
    # new cross-phase private dep, keep the reference inside a function
    # body for the same reason.
    from .. import _check_binary_dynamic
    info_plist = xcframework_path / "Info.plist"
    if not info_plist.is_file():
        return []
    try:
        with info_plist.open("rb") as fh:
            data = plistlib.load(fh)
    except (plistlib.InvalidFileException, OSError, ValueError):
        return []
    available = data.get("AvailableLibraries")
    if not isinstance(available, list):
        return []

    promoted: List[str] = []
    for entry in available:
        if not isinstance(entry, dict):
            continue
        identifier = entry.get("LibraryIdentifier")
        library_path = entry.get("LibraryPath")
        binary_path_field = entry.get("BinaryPath")
        if not isinstance(identifier, str) or not isinstance(library_path, str):
            continue

        # Reconstruct the binary path the same way the verifier does — so
        # both agree on which file represents the slice executable.
        if isinstance(binary_path_field, str) and binary_path_field:
            binary_rel = binary_path_field
        else:
            lib_basename = os.path.basename(library_path.rstrip("/"))
            if not lib_basename.endswith(".framework"):
                # `.a`-as-library or other non-framework layout. Promotion
                # only knows about framework-wrapped statics; leave it to
                # the verifier to flag.
                continue
            stem = lib_basename[: -len(".framework")]
            binary_rel = f"{library_path.rstrip('/')}/{stem}"

        binary = xcframework_path / identifier / binary_rel
        if not binary.is_file():
            continue
        if _check_binary_dynamic(binary):
            continue

        platform = _platform_from_library_identifier(identifier)
        if platform is None:
            raise ExecuteError(
                f"static-archive slice {identifier!r} in "
                f"{xcframework_path.name}: unrecognised platform suffix; "
                "cannot map to an SDK to re-link as a dynamic library."
            )
        platform_slice = _platform_slice_for_library_identifier(identifier)
        if platform_slice is None:
            raise ExecuteError(
                f"static-archive slice {identifier!r} in "
                f"{xcframework_path.name}: no matching PlatformSlice for "
                f"platform {platform!r}; cannot promote to dynamic."
            )

        archs = _lipo_archs(binary)
        if not archs:
            raise ExecuteError(
                f"static-archive slice {identifier!r} in "
                f"{xcframework_path.name}: lipo could not enumerate "
                f"architectures of {binary}."
            )

        deployment_target = _slice_minimum_deployment_target(entry, platform)
        min_ver_flag = platform_slice.clang_min_flag_template.format(
            version=deployment_target
        )
        # `install_name` is derived from the binary basename (not from
        # LibraryPath's framework stem) because that's the name we will
        # actually write the dylib under. Apple framework bundle
        # conventions require the executable name inside a `.framework`
        # to match the bundle stem, so for any well-formed xcframework
        # the two are equal — and if they diverge, the upstream artefact
        # was already unloadable as a framework before promotion. This
        # mirrors how Verify reconstructs `BinaryPath` from `LibraryPath`.
        product = binary.name
        framework_dir = binary.parent  # `<...>.framework` (or `Versions/A`)
        tmp_out = binary.with_name(binary.name + ".dyn.tmp")

        info(
            f"  Promoting static archive → dynamic: "
            f"{xcframework_path.name}/{identifier} "
            f"({platform_slice.sdk_name}: {' '.join(archs)})"
        )

        cmd: List[str] = [
            "xcrun", "--sdk", platform_slice.sdk_name, "clang", "-dynamiclib",
        ]
        for a in archs:
            cmd.extend(["-arch", a])
        cmd.extend([
            min_ver_flag,
            "-install_name", f"@rpath/{product}.framework/{product}",
            "-Xlinker", "-all_load",
            str(binary),
            "-Xlinker", "-undefined", "-Xlinker", "dynamic_lookup",
            "-o", str(tmp_out),
        ])
        verbose_log(verbose, f"  $ {' '.join(cmd)}")

        cp = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        if cp.returncode != 0:
            if tmp_out.exists():
                try:
                    tmp_out.unlink()
                except OSError:
                    pass
            tail = "\n".join(
                ((cp.stderr or cp.stdout or "").rstrip().splitlines())[-10:]
            )
            raise ExecuteError(
                f"Failed to re-link static archive "
                f"{binary.relative_to(xcframework_path)} as a dynamic "
                f"library (sdk={platform_slice.sdk_name}, archs="
                f"{' '.join(archs)}):\n" + (tail or "  (no output)")
            )

        try:
            os.replace(str(tmp_out), str(binary))
        except OSError as exc:
            # Best-effort tmp cleanup so a failed replace doesn't leave
            # `<X>.dyn.tmp` next to the live binary forever — a second
            # run would skip promotion (the old static archive is still
            # in place and `_check_binary_dynamic` returns False), but
            # subsequent reruns shouldn't accumulate orphan tmpfiles.
            if tmp_out.exists():
                try:
                    tmp_out.unlink()
                except OSError:
                    pass
            raise ExecuteError(
                f"Failed to replace static archive at {binary}: {exc}"
            )

        # The prior `_CodeSignature/` no longer matches the new binary.
        # Drop it; Xcode re-signs on embed. Check both the framework root
        # (iOS-style flat layout) AND `Versions/A/` (macOS versioned
        # bundles) — `binary.parent` already points at whichever one
        # holds the executable, so removing `_CodeSignature` there is
        # correct for both layouts. Soft-fail with a warning here: at
        # this point the binary is already promoted and usable; a stale
        # signature is a downstream embed warning, not a build break.
        codesig = framework_dir / "_CodeSignature"
        if codesig.is_dir():
            try:
                shutil.rmtree(codesig)
            except OSError as exc:
                warn(
                    f"Could not remove stale code signature at {codesig} "
                    f"after promoting {identifier} (binary itself was "
                    f"successfully re-linked): {exc}"
                )

        promoted.append(identifier)

    return promoted


def execute_binary_plan(
    plan: Plan,
    config: Config,
) -> List[ExecutedUnit]:
    """Copy each `copy-artifact` build unit's xcframework into
    `config.output_dir`.

    The artifact paths come from the planner (which got them from
    `discover_binary_artifacts` during Fetch — same code path Execute
    would otherwise reach for, so the two phases never disagree about
    which xcframeworks exist). The Execute-side responsibilities are
    the copy itself, a paranoia guard against `__MACOSX` slipping in,
    and a static→dynamic promotion pass for any slice whose framework
    binary turns out to be a `current ar archive` rather than a
    dynamically-linked Mach-O (some vendors ship xcframeworks in that
    shape; downstream P/Invoke consumers cannot dlopen them).
    """
    config.output_dir.mkdir(parents=True, exist_ok=True)
    copy_units = [u for u in plan.build_units if u.archive_strategy == "copy-artifact"]
    if not copy_units:
        return []
    bold(f"\nCopying {len(copy_units)} binary artifact(s)...")
    results: List[ExecutedUnit] = []
    for unit in copy_units:
        if unit.artifact_path is None:
            raise ExecuteError(
                f"binary build unit {unit.name!r} has no artifact_path — "
                f"plan_binary_build did not record one. This is a bug."
            )
        src = unit.artifact_path
        if "__MACOSX" in src.parts:
            raise ExecuteError(
                f"refusing to copy ghost xcframework from {src} (path contains __MACOSX)"
            )
        if not src.is_dir():
            raise ExecuteError(f"binary artifact missing on disk: {src}")
        dest = config.output_dir / src.name
        if "__MACOSX" in dest.parts:
            raise ExecuteError(f"refusing to write to {dest} (path contains __MACOSX)")
        if dest.exists():
            shutil.rmtree(dest)
        info(f"  Copying {src.name}...")
        shutil.copytree(src, dest, symlinks=True)
        # Filter to user-requested platforms first. Two reasons: (a) the
        # source-mode contract is "you got what you asked for via
        # --min-*"; binary mode should behave the same, (b) keeping an
        # unrequested slice can crash the promotion pass below (Sentry's
        # maccatalyst slice → an unsupported `-mtargetos=ios<ver>-macabi`
        # clang invocation on some installed toolchains).
        requested_platforms = _enabled_platforms(config)
        dropped = _filter_xcframework_slices_to_requested_platforms(
            dest, requested_platforms, verbose=config.verbose,
        )
        if dropped:
            info(
                f"  Dropped {len(dropped)} unrequested slice(s) from "
                f"{dest.name}: {', '.join(dropped)}"
            )
        # Promote any static-archive slices to dynamic in place. No-op
        # for the common case of vendor xcframeworks that already ship
        # dynamic binaries — `_check_binary_dynamic` short-circuits the
        # per-slice work. Failures raise ExecuteError so the user gets
        # a clean phase error instead of a confusing Verify rejection
        # downstream.
        promoted = promote_binary_xcframework_static_to_dynamic(
            dest, verbose=config.verbose
        )
        if promoted:
            success(
                f"  Promoted {len(promoted)} static slice(s) to dynamic: "
                + ", ".join(promoted)
            )
        # `detect_framework_type` classifies by header / swiftinterface
        # presence, not by binary linkage, so promotion never changes its
        # answer. Call it after any on-disk mutation in this loop anyway
        # so the printed label is always derived from the final layout —
        # cheap, and survives any future mutation that *would* affect the
        # classification.
        fw_type = detect_framework_type(dest)
        success(f"  {dest.name} ready [{fw_type}]")
        results.append(
            ExecutedUnit(
                name=unit.name,
                xcframework_path=dest,
                framework_name=src.stem,
                framework_type=fw_type,
                # Binary mode never has an expected language — the plan
                # copied pre-built bytes without inspecting them. Leaving
                # this empty makes Verify fall back to post-hoc detection
                # for binary units, which is the only thing it can do.
                expected_language=Language.NA,
                expected_slice_classes=_expected_slice_classes(config),
                is_binary_copy=True,
            )
        )
    return results
