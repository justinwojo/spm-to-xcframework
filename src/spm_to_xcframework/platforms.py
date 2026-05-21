"""Platform slice table + xcodebuild/SPM rendering helpers.

`PlatformSlice` captures everything xcodebuild needs to know about one
output slice of an xcframework: the SDK, the `-destination` string, the
deployment-target build setting name, and the clang `-mtargetos`-style
flag template used by the binary-mode static→dynamic re-link in
`binary_promote.promote_binary_xcframework_static_to_dynamic`.
`_PLATFORM_SLICES` maps each internal platform id ("ios", "macos", ...)
to the ordered tuple of slices the build pipeline should emit.

Lives at the root of the package (not under execute/) because Fetch's
binary-resolve shim builds its `platforms:` array from `_spm_platform_entries`
before Execute is even on the call stack.

Per-flag knobs by platform (rendered into `clang_min_flag_template`):
  - iOS device:        -miphoneos-version-min=<ver>
  - iOS simulator:     -mios-simulator-version-min=<ver>
  - macOS:             -mmacosx-version-min=<ver>
  - Mac Catalyst:      -mtargetos=ios<ver>-macabi
  - tvOS device:       -mtvos-version-min=<ver>
  - tvOS simulator:    -mtvos-simulator-version-min=<ver>
  - watchOS device:    -mwatchos-version-min=<ver>
  - watchOS simulator: -mwatchos-simulator-version-min=<ver>
  - visionOS device:   -mtargetos=xros<ver>
  - visionOS sim:      -mtargetos=xros<ver>-simulator
                       (version goes BEFORE -simulator; the
                       -mtargetos=xros-simulator<ver> form is rejected)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .config import Config
from .model import Package


@dataclass(frozen=True)
class PlatformSlice:
    platform: str                   # "ios", "macos", "maccatalyst", "tvos", "watchos", "visionos"
    slice_id: str                   # unique key: "ios-arm64", "ios-simulator", "macos", ...
    sdk_name: str                   # `xcodebuild -showsdks` short name
    destination: str                # xcodebuild -destination value
    deployment_target_var: str      # build setting name passed as `<var>=<version>`
    clang_min_flag_template: str    # `{version}`-formatted single clang arg for binary-mode static→dynamic re-link


_PLATFORM_SLICES: Dict[str, Tuple[PlatformSlice, ...]] = {
    "ios": (
        PlatformSlice("ios", "ios-arm64", "iphoneos",
                      "generic/platform=iOS",
                      "IPHONEOS_DEPLOYMENT_TARGET",
                      "-miphoneos-version-min={version}"),
        PlatformSlice("ios", "ios-simulator", "iphonesimulator",
                      "generic/platform=iOS Simulator",
                      "IPHONEOS_DEPLOYMENT_TARGET",
                      "-mios-simulator-version-min={version}"),
    ),
    "macos": (
        PlatformSlice("macos", "macos", "macosx",
                      "generic/platform=macOS",
                      "MACOSX_DEPLOYMENT_TARGET",
                      "-mmacosx-version-min={version}"),
    ),
    "maccatalyst": (
        PlatformSlice("maccatalyst", "maccatalyst", "macosx",
                      "generic/platform=macOS,variant=Mac Catalyst",
                      "IPHONEOS_DEPLOYMENT_TARGET",
                      "-mtargetos=ios{version}-macabi"),
    ),
    "tvos": (
        PlatformSlice("tvos", "tvos-arm64", "appletvos",
                      "generic/platform=tvOS",
                      "TVOS_DEPLOYMENT_TARGET",
                      "-mtvos-version-min={version}"),
        PlatformSlice("tvos", "tvos-simulator", "appletvsimulator",
                      "generic/platform=tvOS Simulator",
                      "TVOS_DEPLOYMENT_TARGET",
                      "-mtvos-simulator-version-min={version}"),
    ),
    "watchos": (
        PlatformSlice("watchos", "watchos-arm64", "watchos",
                      "generic/platform=watchOS",
                      "WATCHOS_DEPLOYMENT_TARGET",
                      "-mwatchos-version-min={version}"),
        PlatformSlice("watchos", "watchos-simulator", "watchsimulator",
                      "generic/platform=watchOS Simulator",
                      "WATCHOS_DEPLOYMENT_TARGET",
                      "-mwatchos-simulator-version-min={version}"),
    ),
    "visionos": (
        PlatformSlice("visionos", "visionos-arm64", "xros",
                      "generic/platform=visionOS",
                      "XROS_DEPLOYMENT_TARGET",
                      "-mtargetos=xros{version}"),
        PlatformSlice("visionos", "visionos-simulator", "xrsimulator",
                      "generic/platform=visionOS Simulator",
                      "XROS_DEPLOYMENT_TARGET",
                      "-mtargetos=xros{version}-simulator"),
    ),
}


# Fixed iteration order so dry-run output, the slice merge command, and
# all parallel-build scheduling consume slices in a stable order across
# runs. iOS first (matches today's behaviour); subsequent platforms in
# the same order as the CLI flags appear in --help.
_PLATFORM_ORDER: Tuple[str, ...] = (
    "ios", "macos", "maccatalyst", "tvos", "watchos", "visionos",
)


def _selected_slices(config: "Config") -> List[Tuple[PlatformSlice, str]]:
    """Walk every `min_<platform>` field on `config` and return the
    ordered list of (slice, deployment_target_version) pairs the rest
    of the pipeline should drive xcodebuild against.

    Returns an empty list when no platform is enabled — the caller
    (`main()`) validates that ≥1 platform is enabled before reaching
    Execute, so an empty return from here is a programmer error.
    """
    out: List[Tuple[PlatformSlice, str]] = []
    versions: Dict[str, Optional[str]] = {
        "ios": config.min_ios,
        "macos": config.min_macos,
        "maccatalyst": config.min_maccatalyst,
        "tvos": config.min_tvos,
        "watchos": config.min_watchos,
        "visionos": config.min_visionos,
    }
    for plat in _PLATFORM_ORDER:
        version = versions.get(plat)
        if not version:
            continue
        for s in _PLATFORM_SLICES[plat]:
            out.append((s, version))
    return out


def _autodetect_min_versions(config: "Config", package: "Package") -> Dict[str, str]:
    """Fill in `config.min_<platform>` fields from `Package.platforms[]`
    when the user passed zero --min-* flags.

    Returns the dict of `{platform: version}` that was applied (empty
    when no auto-detect ran — i.e., the user provided explicit flags).
    Mutates `config` in place.

    Two policy choices, both deliberate:

    1. Explicit-vs-auto is all-or-nothing. If the user passed any
       --min-* flag, we honor exactly that set; no mixing with derived
       platforms. Keeps the mental model "what I typed is what I got."

    2. When the package declares NO platforms at all (`platforms:` array
       absent or empty), fall back to iOS 15.0 — today's pre-auto-detect
       default. Many small library packages omit `platforms:` entirely
       and rely on SPM's implicit minima; the tool's downstream (.NET
       binding generation) almost always wants iOS, so the fallback
       preserves the most common case.

    `--no-ios` is respected: if it was passed, auto-detect skips the iOS
    entry from the package even when the package declares iOS. The
    other declared platforms still get filled in. A `--no-ios`-only
    invocation against an iOS-only package therefore yields zero
    platforms; the caller's post-autodetect validator surfaces that as a
    clear error.
    """
    user_provided_any = any([
        config.min_ios, config.min_macos, config.min_maccatalyst,
        config.min_tvos, config.min_watchos, config.min_visionos,
    ])
    if user_provided_any:
        return {}

    derived: Dict[str, str] = {}
    for p in package.platforms:
        if p.name not in _PLATFORM_ORDER or not p.version:
            continue
        if p.name == "ios" and config.no_ios:
            continue
        derived[p.name] = p.version

    if not derived and not config.no_ios:
        config.min_ios = "15.0"
        return {"ios": "15.0"}

    for plat, ver in derived.items():
        setattr(config, f"min_{plat}", ver)
    return derived


def _enabled_platforms(config: "Config") -> List[str]:
    """Platforms the user requested via `--min-*` flags, in fixed order.
    Pure data — no side effects."""
    versions: Dict[str, Optional[str]] = {
        "ios": config.min_ios,
        "macos": config.min_macos,
        "maccatalyst": config.min_maccatalyst,
        "tvos": config.min_tvos,
        "watchos": config.min_watchos,
        "visionos": config.min_visionos,
    }
    return [p for p in _PLATFORM_ORDER if versions.get(p)]


# SPM `PackageDescription` platform case names, keyed by our internal
# platform identifier. Used by the binary-resolve shim to build the
# manifest's `platforms:` array. The string-version form
# (`.iOS("15.0")`) sidesteps the `.v<major>` enum table — which is
# wrong for macOS minima like 10.15 (`.v10` would be `.v10_0`).
_SPM_PLATFORM_CASE: Dict[str, str] = {
    "ios": "iOS",
    "macos": "macOS",
    "maccatalyst": "macCatalyst",
    "tvos": "tvOS",
    "watchos": "watchOS",
    "visionos": "visionOS",
}


def _platform_from_library_identifier(lid: str) -> Optional[str]:
    """Map an xcframework `AvailableLibraries[*].LibraryIdentifier` to one
    of our internal platform IDs. Apple's convention for these strings
    (per `man xcodebuild -create-xcframework` + observed real-world
    xcframeworks): `<platform>-<archs>[-<variant>]`, where `variant` is
    `simulator` or `maccatalyst`. visionOS uses the SDK prefix `xros`,
    not `visionos`. Returns None for shapes we don't recognise — callers
    treat unknown identifiers as not contributing to coverage.
    """
    if lid.endswith("-maccatalyst"):
        return "maccatalyst"
    if lid.startswith("ios-") or lid == "ios":
        return "ios"
    if lid.startswith("macos-") or lid == "macos":
        return "macos"
    if lid.startswith("tvos-") or lid == "tvos":
        return "tvos"
    if lid.startswith("watchos-") or lid == "watchos":
        return "watchos"
    if lid.startswith("xros-") or lid == "xros":
        return "visionos"
    return None


def _variant_from_library_identifier(lid: str) -> str:
    """Classify the device/simulator/maccatalyst flavour of a
    LibraryIdentifier. Pairs with `_platform_from_library_identifier`
    so Verify can assert (platform, variant) coverage — not just family
    coverage — against `unit.expected_slice_classes`."""
    if lid.endswith("-maccatalyst"):
        return "maccatalyst"
    if lid.endswith("-simulator"):
        return "simulator"
    return "device"


def _variant_for_platform_slice(s: "PlatformSlice") -> str:
    """Same taxonomy as `_variant_from_library_identifier`, but applied
    to our internal `PlatformSlice` (whose `slice_id` we control). Keeps
    the expected-set construction in lockstep with the covered-set
    construction inside `_verify_one_unit`."""
    if s.platform == "maccatalyst":
        return "maccatalyst"
    if s.slice_id.endswith("-simulator"):
        return "simulator"
    return "device"


def _expected_slice_classes(config: "Config") -> List[Tuple[str, str]]:
    """Per-(platform, variant) coverage requirement derived from the
    enabled `--min-*` flags. e.g. `--min-ios 15 --min-macos 11` yields
    `[("ios", "device"), ("ios", "simulator"), ("macos", "device")]`."""
    return [
        (s.platform, _variant_for_platform_slice(s))
        for s, _ in _selected_slices(config)
    ]


def _spm_platform_entries(config: "Config") -> List[str]:
    """Render one `.iOS("15.0")`-style entry per enabled platform, in
    fixed order. Used by the binary-resolve shim manifest emitter."""
    versions: Dict[str, Optional[str]] = {
        "ios": config.min_ios,
        "macos": config.min_macos,
        "maccatalyst": config.min_maccatalyst,
        "tvos": config.min_tvos,
        "watchos": config.min_watchos,
        "visionos": config.min_visionos,
    }
    entries: List[str] = []
    for plat in _PLATFORM_ORDER:
        v = versions.get(plat)
        if not v:
            continue
        entries.append(f'.{_SPM_PLATFORM_CASE[plat]}("{v}")')
    return entries
