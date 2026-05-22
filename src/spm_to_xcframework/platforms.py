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


# Per-platform fallback deployment targets, applied by
# `_autodetect_min_versions` when (a) the user opted into a platform
# (via the bare `--<plat>` flag) but didn't pass `--min-<plat>`, and (b)
# the resolved `Package.platforms[]` doesn't declare a version for that
# platform either. Values chosen as Apple-SDK-modern floors — high
# enough to be buildable with current Xcode, low enough not to exclude
# the bulk of installed devices. The downstream binding-generation tool
# is expected to raise these minimums per its own consumer-language
# requirements (e.g. .NET) separately.
_PLATFORM_FALLBACK_VERSIONS: Dict[str, str] = {
    "ios": "15.0",
    "macos": "11.0",
    "maccatalyst": "15.0",
    "tvos": "15.0",
    "watchos": "8.0",
    "visionos": "1.0",
}


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


def _autodetect_min_versions(
    config: "Config", package: Optional["Package"] = None,
) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Resolve `config.min_<platform>` for every included platform.

    For each platform where `config.include_<plat>` is True:

      1. If the user already supplied `--min-<plat>`, keep it.
      2. Otherwise, if `package` is provided and `Package.platforms[]`
         declares a version for that platform, use it.
      3. Otherwise, fall back to `_PLATFORM_FALLBACK_VERSIONS[plat]`.

    Returns a `(derived, fallback)` tuple of `{platform: version}` maps
    covering only the auto-resolutions (step 2 vs step 3) — user-
    explicit values from step 1 are intentionally absent so the caller
    can log "what we picked for you" without echoing back things the
    user already typed. Mutates `config` in place by writing the
    resolved version into `config.min_<plat>`.

    `package=None` is the binary-mode path: with no Inspect there's no
    `Package.platforms[]` to read, so every auto-resolution falls
    straight through to the fallback table.

    Platforms with `include_<plat>` False are skipped entirely — their
    `min_<plat>` stays None and downstream slice-walkers (which key off
    truthiness) won't emit slices for them. `--no-ios` therefore flows
    through as `include_ios = False` at the CLI layer; this function
    doesn't special-case iOS.
    """
    declared: Dict[str, str] = {}
    if package is not None:
        for p in package.platforms:
            if p.name not in _PLATFORM_ORDER or not p.version:
                continue
            declared[p.name] = p.version

    derived: Dict[str, str] = {}
    fallback: Dict[str, str] = {}
    for plat in _PLATFORM_ORDER:
        if not getattr(config, f"include_{plat}"):
            continue
        if getattr(config, f"min_{plat}"):
            continue
        version = declared.get(plat)
        if version is not None:
            setattr(config, f"min_{plat}", version)
            derived[plat] = version
        else:
            version = _PLATFORM_FALLBACK_VERSIONS[plat]
            setattr(config, f"min_{plat}", version)
            fallback[plat] = version
    return derived, fallback


def _declared_unincluded_platforms(
    config: "Config", package: "Package",
) -> List[str]:
    """Return platforms `Package.platforms[]` declares but the user
    didn't opt into, in `_PLATFORM_ORDER`. Drives the CLI's "this
    package also supports X" hint after autodetect.

    iOS is excluded: there's no bare `--ios` flag and `--no-ios` is an
    intentional drop, so re-suggesting iOS would be noise.
    """
    declared = {p.name for p in package.platforms}
    return [
        plat for plat in _PLATFORM_ORDER
        if plat != "ios"
        and plat in declared
        and not getattr(config, f"include_{plat}")
    ]


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
