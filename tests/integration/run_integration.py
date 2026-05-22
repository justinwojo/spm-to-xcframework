#!/usr/bin/env python3
"""Integration test runner for spm-to-xcframework.

Builds every package in `packages.toml` by subprocess-invoking the local
`spm-to-xcframework` binary, asserts the produced xcframeworks meet the
verify contract (dynamic Mach-O, .swiftmodule for Swift / Headers+modulemap
for ObjC, expected platforms), and writes a Markdown + JSON report to
`reports/<UTC timestamp>/`.

The tool already runs its own Verify phase at the end of every build, so a
zero exit code means the tool *thinks* the output is good. This harness
re-checks the verify contract independently — that's the whole point: catch
the silent-pass case where the tool ships a broken xcframework downstream.

Requires Python 3.11+ (uses `tomllib`).
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import plistlib
import re
import subprocess
import sys
import tempfile
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Per-package build cap. xcodebuild has no inherent overall timeout; without
# this, a wedged compile/codesign step hangs the suite (and CI) forever.
BUILD_TIMEOUT_SEC = 30 * 60

REPO_ROOT = Path(__file__).resolve().parents[2]
INTEGRATION_DIR = Path(__file__).resolve().parent
TOOL = REPO_ROOT / "spm-to-xcframework"
PACKAGES_TOML = INTEGRATION_DIR / "packages.toml"
REPORTS_DIR = INTEGRATION_DIR / "reports"


@dataclasses.dataclass
class PackageSpec:
    name: str
    url: str
    version: str
    revision: str | None
    args: list[str]
    min_versions: dict[str, str]
    expected_products: list[str]
    exercises: list[str]
    notes: str
    known_broken: bool
    broken_reason: str
    broken_signature: str
    required_log_signatures: list[str]
    allowed_unresolved: list[str]
    smoke: bool

    @property
    def platforms(self) -> list[str]:
        return list(self.min_versions.keys())


@dataclasses.dataclass
class PackageResult:
    name: str
    status: str  # pass | fail | known_broken
    duration_sec: float
    tool_exit_code: int
    products_found: list[str]
    products_missing: list[str]
    verify_failures: list[str]
    error: str | None
    log_path: str


# --------------------------------------------------------------------------
# Loading

def load_packages(only: str | None, smoke_only: bool) -> list[PackageSpec]:
    with open(PACKAGES_TOML, "rb") as f:
        data = tomllib.load(f)
    out: list[PackageSpec] = []
    for entry in data.get("package", []):
        min_versions = entry.get("min_versions") or {"ios": "15.0"}
        if not isinstance(min_versions, dict) or not min_versions:
            die(f"package {entry.get('name')!r}: min_versions must be a non-empty table")
        spec = PackageSpec(
            name=entry["name"],
            url=entry["url"],
            version=entry["version"],
            revision=entry.get("revision"),
            args=list(entry.get("args", [])),
            min_versions={str(k): str(v) for k, v in min_versions.items()},
            expected_products=list(entry["expected_products"]),
            exercises=list(entry.get("exercises", [])),
            notes=str(entry.get("notes", "")),
            known_broken=bool(entry.get("known_broken", False)),
            broken_reason=str(entry.get("broken_reason", "")),
            broken_signature=str(entry.get("broken_signature", "")),
            required_log_signatures=list(entry.get("required_log_signatures", [])),
            allowed_unresolved=list(entry.get("allowed_unresolved", [])),
            smoke=bool(entry.get("smoke", False)),
        )
        if only is not None and spec.name != only:
            continue
        if smoke_only and not spec.smoke:
            continue
        out.append(spec)
    if only and not out:
        die(f"No package named {only!r} in {PACKAGES_TOML}")
    if smoke_only and not out:
        die("No packages tagged smoke = true.")
    return out


# --------------------------------------------------------------------------
# Building one package

def build_one(spec: PackageSpec, report_dir: Path, keep_output: bool) -> PackageResult:
    pkg_report_dir = report_dir / spec.name
    pkg_report_dir.mkdir(parents=True, exist_ok=True)
    log_path = pkg_report_dir / "xcodebuild.log"

    if keep_output:
        work_dir = pkg_report_dir / "work"
        work_dir.mkdir(exist_ok=True)
        return _run_in(spec, pkg_report_dir, work_dir, log_path)

    with tempfile.TemporaryDirectory(prefix=f"spmxcf-int-{spec.name}-") as tmp:
        return _run_in(spec, pkg_report_dir, Path(tmp), log_path)


def _run_in(spec: PackageSpec, pkg_report_dir: Path, work_dir: Path, log_path: Path) -> PackageResult:
    out_dir = work_dir / "xcframeworks"
    platform_args: list[str] = []
    for platform, version in spec.min_versions.items():
        platform_args.extend([f"--min-{platform}", version])
    cmd = [
        str(TOOL),
        spec.url,
        "-v", spec.version,
        "-o", str(out_dir),
        *platform_args,
        *spec.args,
    ]
    if spec.revision:
        cmd.extend(["--revision", spec.revision])

    start = time.time()
    timed_out = False
    with open(log_path, "wb") as log_fh:
        log_fh.write(f"$ {' '.join(cmd)}\n\n".encode())
        log_fh.flush()
        try:
            proc = subprocess.run(
                cmd, stdout=log_fh, stderr=subprocess.STDOUT, cwd=work_dir,
                timeout=BUILD_TIMEOUT_SEC,
            )
            returncode = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            returncode = -1
            log_fh.write(f"\n[harness] killed after {BUILD_TIMEOUT_SEC}s timeout\n".encode())
    duration = time.time() - start

    if returncode != 0:
        # A known_broken entry only counts as "known" if its broken_signature
        # appears in the log — otherwise the failure has drifted and the entry
        # needs fresh triage. Entries without a signature fall back to the
        # blanket-mask behaviour (kept for entries where the signature is too
        # noisy to pin down, but flagged in the report).
        status = _classify_known_broken(spec, log_path, timed_out)
        error_msg = (
            f"tool timed out after {BUILD_TIMEOUT_SEC}s"
            if timed_out
            else f"tool exited {returncode} (see {log_path.name})"
        )
        return PackageResult(
            name=spec.name,
            status=status,
            duration_sec=duration,
            tool_exit_code=returncode,
            products_found=[],
            products_missing=list(spec.expected_products),
            verify_failures=[],
            error=error_msg,
            log_path=str(log_path),
        )

    found = sorted(p.name.removesuffix(".xcframework") for p in out_dir.glob("*.xcframework"))
    missing = [p for p in spec.expected_products if p not in found]

    sibling_products = set(found)
    verify_failures: list[str] = []
    for product in spec.expected_products:
        xcfw = out_dir / f"{product}.xcframework"
        if xcfw.exists():
            verify_failures.extend(_verify_xcframework(xcfw, spec, sibling_products))

    _write_tree_dump(out_dir, found, pkg_report_dir / "xcframework-tree.txt")

    if missing or verify_failures:
        status = _classify_known_broken(
            spec, log_path, timed_out=False, verify_failures=verify_failures
        )
    else:
        status = "pass"

    return PackageResult(
        name=spec.name,
        status=status,
        duration_sec=duration,
        tool_exit_code=0,
        products_found=found,
        products_missing=missing,
        verify_failures=verify_failures,
        error=None,
        log_path=str(log_path),
    )


# --------------------------------------------------------------------------
# Known-broken classification

def _classify_known_broken(
    spec: PackageSpec,
    log_path: Path,
    timed_out: bool,
    verify_failures: list[str] | None = None,
) -> str:
    """Return "known_broken" only if this is the failure we already triaged.

    Prevents `known_broken` from silently absorbing brand-new regressions in
    the same package. A timeout never counts as a known break — those need
    fresh triage even when the package was already broken for a different
    reason.

    The signature is matched against the union of the tool's stdout/stderr
    log AND any verify-phase failure messages this harness emitted, so a
    matrix entry that fails only at verify (where messages never reach the
    log file) can still be pinned by its harness-side failure text.

    `required_log_signatures` is a stricter gate: when set, *every* listed
    substring must also appear in the haystack for the entry to count as
    known_broken. This guards entries whose value is "thing X must have
    happened before the known failure" — e.g. the TCA entry needs the 14
    sibling xcframework-ready lines to appear before the swift-collections
    ceiling trips, otherwise a regression that fails early but still
    happens to log the same broken_signature would silently mask as
    known_broken instead of surfacing as a real fail.
    """
    if not spec.known_broken:
        return "fail"
    if timed_out:
        return "fail"
    if not spec.broken_signature:
        # No signature pinned → fall back to blanket-mask. Logged in the
        # report so we know which entries need tightening.
        return "known_broken"
    haystack_parts: list[str] = []
    try:
        haystack_parts.append(log_path.read_text(errors="replace"))
    except OSError:
        pass
    if verify_failures:
        haystack_parts.extend(verify_failures)
    haystack = "\n".join(haystack_parts)
    if spec.broken_signature not in haystack:
        return "fail"
    for required in spec.required_log_signatures:
        if required not in haystack:
            return "fail"
    return "known_broken"


# --------------------------------------------------------------------------
# Verify contract (independent of the tool's own Verify phase)

# Map our short `platforms` field to the SupportedPlatform / variant pairs
# we expect Apple's xcframework Info.plist to declare.
_EXPECTED_PLATFORM_VARIANTS: dict[str, set[tuple[str, str]]] = {
    "ios": {("ios", "device"), ("ios", "simulator")},
    "macos": {("macos", "device")},
    "maccatalyst": {("ios", "maccatalyst")},
    "tvos": {("tvos", "device"), ("tvos", "simulator")},
    "watchos": {("watchos", "device"), ("watchos", "simulator")},
    "visionos": {("visionos", "device"), ("visionos", "simulator")},
}


def _verify_xcframework(xcfw: Path, spec: PackageSpec, sibling_products: set[str]) -> list[str]:
    failures: list[str] = []
    info_plist = xcfw / "Info.plist"
    if not info_plist.exists():
        return [f"{xcfw.name}: missing Info.plist"]

    try:
        with open(info_plist, "rb") as f:
            plist = plistlib.load(f)
    except Exception as exc:  # noqa: BLE001 — plist corruption is itself a failure
        return [f"{xcfw.name}: Info.plist parse failed: {exc}"]

    libraries = plist.get("AvailableLibraries") or []

    found_pairs: set[tuple[str, str]] = set()
    for lib in libraries:
        identifier = lib.get("LibraryIdentifier", "?")
        platform = lib.get("SupportedPlatform", "?")
        variant = lib.get("SupportedPlatformVariant") or "device"
        found_pairs.add((platform, variant))

        slice_dir = xcfw / identifier
        fw_path = slice_dir / (lib.get("LibraryPath") or "")
        if not fw_path.exists():
            failures.append(f"{xcfw.name}/{identifier}: LibraryPath not found")
            continue

        binary = _locate_binary(fw_path)
        if binary is None:
            failures.append(f"{xcfw.name}/{identifier}: binary not found inside {fw_path.name}")
            continue
        # Dynamic Mach-O check, independent of the tool's own check.
        file_out = subprocess.run(
            ["file", str(binary)], capture_output=True, text=True, timeout=30,
        ).stdout
        if "dynamically linked" not in file_out:
            failures.append(
                f"{xcfw.name}/{identifier}: binary not dynamic Mach-O "
                f"({file_out.strip()})"
            )

        modules = _locate_modules(fw_path)
        if modules is None or not modules.exists():
            failures.append(f"{xcfw.name}/{identifier}: no Modules/ directory")
            continue

        swiftmod_dirs = list(modules.glob("*.swiftmodule"))
        modulemap = modules / "module.modulemap"
        has_swift = bool(swiftmod_dirs)
        has_modulemap = modulemap.exists()
        if not (has_swift or has_modulemap):
            failures.append(
                f"{xcfw.name}/{identifier}/Modules: neither .swiftmodule nor module.modulemap"
            )
            continue

        # ObjC / mixed surface: if the modulemap declares ObjC headers
        # (`umbrella header "X.h"` / `header "X.h"`), Headers/X.h must
        # actually exist. An empty Headers/ or a missing umbrella file
        # ships a slice that imports cleanly via SwiftPM but breaks any
        # clang consumer. Applies to both ObjC-only AND mixed
        # Swift+ObjC frameworks — only pure-Swift modulemaps (no
        # `header` directives) skip the check.
        if has_modulemap:
            declared = _modulemap_declared_headers(modulemap)
            if declared:
                # Framework modulemaps resolve `header "X.h"` against
                # Headers/ for public declarations and `private header` (or
                # headers inside a `module Foo.Private` submodule) against
                # PrivateHeaders/. We accept either location — the check is
                # "is this declared header present on disk somewhere clang
                # would find it", not "is it in the public surface".
                headers = _locate_headers(fw_path)
                private_headers = _locate_private_headers(fw_path)
                missing_headers = []
                for hdr in declared:
                    in_public = headers is not None and (headers / hdr).exists()
                    in_private = private_headers is not None and (private_headers / hdr).exists()
                    if not (in_public or in_private):
                        missing_headers.append(hdr)
                if missing_headers:
                    failures.append(
                        f"{xcfw.name}/{identifier}: module.modulemap declares "
                        f"{missing_headers} but they're missing under Headers/ "
                        f"or PrivateHeaders/"
                    )

        if has_swift:
            # Library-evolution clients need .swiftinterface; every emitted
            # .swiftmodule directory must contain one (per-arch interfaces
            # live under their architecture-named filename).
            seen_imports: set[str] = set()
            for swiftmod_dir in swiftmod_dirs:
                interfaces = list(swiftmod_dir.glob("*.swiftinterface"))
                if not interfaces:
                    failures.append(
                        f"{xcfw.name}/{identifier}/{swiftmod_dir.name}: missing .swiftinterface"
                    )
                    continue
                for iface in interfaces:
                    seen_imports.update(_parse_interface_imports(iface))

            # Every non-stdlib import the .swiftinterface declares must
            # either resolve to a sibling xcframework, a clang module
            # packaged somewhere inside this slice, or an OS-provided
            # framework. The WCDB-style regression — `import WCDB_Private`
            # with no shim packaged — would silently pass the older
            # structure-only check; this catches it. GRDB-style packaging
            # (CSQLite shipped as a sibling .framework alongside the main
            # one) resolves via the slice scan.
            local_clang_modules = _slice_resolvable_clang_modules(slice_dir)
            allowed = set(spec.allowed_unresolved)
            unresolved = [
                name for name in sorted(seen_imports)
                if not _is_system_module(name)
                and name not in sibling_products
                and name not in local_clang_modules
                and name not in allowed
            ]
            if unresolved:
                failures.append(
                    f"{xcfw.name}/{identifier}: .swiftinterface imports {unresolved} "
                    f"with no packaged shim or sibling xcframework"
                )

    expected_pairs: set[tuple[str, str]] = set()
    for p in spec.platforms:
        expected_pairs.update(_EXPECTED_PLATFORM_VARIANTS.get(p, set()))
    missing_pairs = expected_pairs - found_pairs
    if missing_pairs:
        failures.append(
            f"{xcfw.name}: missing slice(s) {sorted(missing_pairs)} "
            f"(found {sorted(found_pairs)})"
        )
    # Extra slices are only a contract violation in source mode (where
    # --min-<platform> determines the slice set). Binary mode just copies
    # whatever the vendor shipped, so e.g. Lottie's 8-slice prebuilt
    # xcframework legitimately exposes maccatalyst/macOS/visionOS variants
    # the harness didn't request.
    if "--binary" not in spec.args:
        extra_pairs = found_pairs - expected_pairs
        if extra_pairs:
            failures.append(
                f"{xcfw.name}: unexpected slice(s) {sorted(extra_pairs)} "
                f"not requested by min_versions={list(spec.min_versions)}"
            )

    return failures


# Swift/Apple modules a .swiftinterface can `import` without us needing to see
# them packaged or as siblings. Conservative — extend on a need-to-know basis.
_SYSTEM_MODULE_ALLOWLIST: set[str] = {
    "Swift", "_Concurrency", "_StringProcessing", "_Differentiation",
    "Foundation", "ObjectiveC", "Darwin", "Dispatch", "os",
    "Combine", "SwiftUI", "UIKit", "AppKit", "WatchKit", "CoreFoundation",
    "CoreGraphics", "CoreData", "CoreImage", "CoreText", "CoreLocation",
    "CoreMedia", "CoreVideo", "CoreAudio", "CoreBluetooth", "CoreML",
    "CoreMotion", "CoreServices", "CoreTelephony", "CoreSpotlight",
    "AVFoundation", "AVKit", "MediaPlayer", "PhotosUI", "Photos",
    "QuartzCore", "ImageIO", "Metal", "MetalKit", "MetalPerformanceShaders",
    "Network", "CFNetwork", "Security", "CommonCrypto", "CryptoKit",
    "LocalAuthentication", "MessageUI", "EventKit", "Contacts",
    "MapKit", "WebKit", "SafariServices", "StoreKit", "PassKit",
    "AuthenticationServices", "JavaScriptCore",
    "UserNotifications", "BackgroundTasks", "CloudKit",
    "DeveloperToolsSupport",  # SwiftUI Previews / #Preview macro support
    "GameKit", "GameController", "GameplayKit", "SceneKit", "SpriteKit",
    "Accelerate", "Compression", "SystemConfiguration", "SystemPackage",
    "OSLog", "Observation", "Synchronization",
    "MachO", "MetricKit", "PDFKit",
    "XCTest", "Testing",
    # iOS / macOS frameworks Kingfisher-class consumer libraries reach for.
    "CarPlay",  # Driving-mode UI; conditionally imported by media-display libs.
    "MobileCoreServices",  # Pre-iOS-14 UTI/MIME constants. Deprecated but still imported.
    "UniformTypeIdentifiers",  # iOS 14+ replacement for MobileCoreServices.
    "VisionKit", "Vision", "NaturalLanguage", "Speech", "SoundAnalysis",
    "Intents", "IntentsUI", "WidgetKit", "ActivityKit",
    "HomeKit", "HealthKit", "ARKit",
    "AdSupport", "AppTrackingTransparency",
    "CallKit", "PushKit", "FileProvider", "FileProviderUI",
    "WatchConnectivity", "ExternalAccessory", "MultipeerConnectivity",
    "NetworkExtension", "LinkPresentation", "QuickLook", "QuickLookThumbnailing",
    "VideoToolbox", "AudioToolbox", "AudioUnit",
    "ServiceManagement", "DeviceCheck",
    "ClockKit",  # WatchKit complications API
    "TVServices", "TVMLKit", "TVUIKit",  # tvOS
    "Translation", "Charts",  # Newer iOS/macOS additions
    "ScreenCaptureKit",
    # System C / Darwin modules Apple ships via SDK module maps.
    "SQLite3", "sqlite3", "zlib", "bzip2", "iconv", "libxml2", "libcurl",
    "pthread", "dispatch", "objc", "mach", "mach_o", "dyld", "dlfcn",
}


_IMPORT_LINE = re.compile(
    # Top-level `[attrs] [access] import [kind]? <Name>` — `kind` is one of
    # the Swift import-grammar specialiser keywords. Whitespace classes are
    # `[ \t]` (not `\s`) on purpose: `\s` would cross newlines and let the
    # optional kind-group swallow the next line's `import` token.
    r"^[ \t]*"
    r"(?:@[A-Za-z_]\w*(?:\([^)]*\))?[ \t]+)*"
    r"(?:public|internal|private|fileprivate)?[ \t]*"
    r"import[ \t]+"
    r"(?:typealias|struct|class|enum|protocol|let|var|func)?[ \t]*"
    r"([A-Za-z_]\w*)",
    re.MULTILINE,
)


def _parse_interface_imports(iface: Path) -> set[str]:
    try:
        text = iface.read_text(errors="replace")
    except OSError:
        return set()
    return {m.group(1) for m in _IMPORT_LINE.finditer(text)}


_MODULEMAP_MODULE = re.compile(
    r"^\s*(?:framework\s+|explicit\s+)*module\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)


def _modulemap_module_names(modulemap: Path) -> set[str]:
    try:
        text = modulemap.read_text(errors="replace")
    except OSError:
        return set()
    return {m.group(1) for m in _MODULEMAP_MODULE.finditer(text)}


_MODULEMAP_HEADER = re.compile(
    # Catches both `header "X.h"` and `umbrella header "X.h"`. Excludes
    # `umbrella "DirName"` (umbrella-dir form) — the header check is for
    # files, and umbrella-dir layout is rare in xcframework slices.
    r'(?:^|\s)(?:umbrella\s+)?header\s+"([^"]+)"',
)


def _modulemap_declared_headers(modulemap: Path) -> list[str]:
    """Header filenames the modulemap claims should be importable."""
    try:
        text = modulemap.read_text(errors="replace")
    except OSError:
        return []
    return _MODULEMAP_HEADER.findall(text)


def _slice_resolvable_clang_modules(slice_dir: Path) -> set[str]:
    """Clang module names a consumer can resolve from inside this slice.

    Two main sources, both common in real packaging:
      * Sibling `.framework` directories inside the slice. The framework
        bundle name itself is the importable module name (GRDB ships
        `CSQLite.framework` next to `GRDB.framework`; WCDBSwift ships
        `WCDB_Private.framework` next to `WCDBSwift.framework`).
      * Any `*.modulemap` shipped anywhere under those frameworks. We
        parse every modulemap and union the declared module names.

    Sibling-framework basenames are only credited if the framework
    actually ships an importable surface (a `module.modulemap` or a
    `.swiftmodule`). An empty `WCDB_Private.framework` shell — the exact
    failure shape of a partial bridge-clang-modules injection — would
    otherwise satisfy the import check and pass.
    """
    names: set[str] = set()
    for fw in slice_dir.glob("*.framework"):
        modulemaps = list(fw.rglob("*.modulemap"))
        modules_dir = _locate_modules(fw)
        has_swiftmod = modules_dir is not None and any(modules_dir.glob("*.swiftmodule"))
        if modulemaps or has_swiftmod:
            names.add(fw.name.removesuffix(".framework"))
        for mm in modulemaps:
            names.update(_modulemap_module_names(mm))
    return names


def _is_system_module(name: str) -> bool:
    """Heuristic: does this look like an Apple-provided import target?

    True for things the Apple SDKs ship via system module maps. The check
    isn't load-bearing for catching genuine packaging bugs — it just keeps
    the unresolved-imports check from spamming on stdlib/system names.
    """
    if name in _SYSTEM_MODULE_ALLOWLIST:
        return True
    if name.startswith("_"):
        # Swift stdlib private modules: _Concurrency, _StringProcessing,
        # _SwiftConcurrencyShims, _Builtin_*, _Differentiation, ...
        return True
    if name.endswith("_h"):
        # Darwin SDK convention for C-header system modules: string_h,
        # stdio_h, stdlib_h, time_h, unistd_h, fcntl_h, signal_h, ...
        return True
    return False


def _locate_binary(fw_path: Path) -> Path | None:
    name = fw_path.name.removesuffix(".framework")
    candidates = [
        fw_path / name,
        fw_path / "Versions" / "A" / name,
        fw_path / "Versions" / "Current" / name,
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def _locate_modules(fw_path: Path) -> Path | None:
    candidates = [
        fw_path / "Modules",
        fw_path / "Versions" / "A" / "Modules",
        fw_path / "Versions" / "Current" / "Modules",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def _locate_headers(fw_path: Path) -> Path | None:
    candidates = [
        fw_path / "Headers",
        fw_path / "Versions" / "A" / "Headers",
        fw_path / "Versions" / "Current" / "Headers",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def _locate_private_headers(fw_path: Path) -> Path | None:
    candidates = [
        fw_path / "PrivateHeaders",
        fw_path / "Versions" / "A" / "PrivateHeaders",
        fw_path / "Versions" / "Current" / "PrivateHeaders",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def _write_tree_dump(out_dir: Path, products: list[str], dest: Path) -> None:
    lines: list[str] = []
    for product in products:
        xcfw = out_dir / f"{product}.xcframework"
        if not xcfw.exists():
            continue
        for path in sorted(xcfw.rglob("*")):
            rel = path.relative_to(out_dir)
            kind = "l" if path.is_symlink() else ("d" if path.is_dir() else "f")
            lines.append(f"{kind} {rel}")
    dest.write_text("\n".join(lines) + "\n")


# --------------------------------------------------------------------------
# Reports

def write_reports(
    results: list[PackageResult],
    report_dir: Path,
    started: dt.datetime,
    toolchain: dict,
) -> Path:
    json_path = report_dir / "summary.json"
    json_path.write_text(json.dumps({
        "started": started.isoformat(),
        "toolchain": toolchain,
        "results": [dataclasses.asdict(r) for r in results],
    }, indent=2))

    passed = sum(1 for r in results if r.status == "pass")
    failed = sum(1 for r in results if r.status == "fail")
    known = sum(1 for r in results if r.status == "known_broken")
    total = len(results)

    lines: list[str] = [
        f"# Integration test run — {started.isoformat()}",
        "",
        f"Toolchain: Xcode {toolchain.get('xcode', '?')}, Swift {toolchain.get('swift', '?')}",
        f"spm-to-xcframework: {toolchain.get('tool_sha', '?')}",
        "",
        f"## Results: {passed}/{total} passed"
        + (f", {failed} failed" if failed else "")
        + (f", {known} known broken" if known else ""),
        "",
        "| Package | Result | Duration | Notes |",
        "|---|---|---|---|",
    ]
    for r in results:
        label = {"pass": "PASS", "fail": "FAIL", "known_broken": "KNOWN_BROKEN"}[r.status]
        parts: list[str] = []
        if r.products_missing:
            parts.append(f"missing: {', '.join(r.products_missing)}")
        if r.verify_failures:
            parts.append(f"verify: {len(r.verify_failures)} failure(s)")
        if r.error:
            parts.append(r.error)
        notes = "; ".join(parts) or ""
        lines.append(f"| {r.name} | {label} | {_fmt_duration(r.duration_sec)} | {notes} |")

    if any(r.verify_failures for r in results):
        lines += ["", "## Verify failures (details)", ""]
        for r in results:
            if not r.verify_failures:
                continue
            lines.append(f"### {r.name}")
            for failure in r.verify_failures:
                lines.append(f"- {failure}")
            lines.append("")

    md_path = report_dir / "summary.md"
    md_path.write_text("\n".join(lines))
    return md_path


# --------------------------------------------------------------------------
# Misc helpers

def detect_toolchain() -> dict:
    xcode = _run_capture(["xcodebuild", "-version"]).splitlines()[:1]
    xcode_line = xcode[0].replace("Xcode ", "") if xcode else "?"
    swift = _run_capture(["swift", "--version"]).splitlines()[:1]
    swift_line = swift[0] if swift else "?"
    try:
        tool_sha = _run_capture(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"]
        ).strip() or "unknown"
    except Exception:  # noqa: BLE001
        tool_sha = "unknown"
    return {"xcode": xcode_line, "swift": swift_line, "tool_sha": tool_sha}


def _run_capture(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=False).stdout
    except FileNotFoundError:
        return ""


def _fmt_duration(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    return f"{m}m {s:02d}s"


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(2)


# --------------------------------------------------------------------------
# Entry point

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Integration test runner for spm-to-xcframework"
    )
    parser.add_argument("--only", help="Run only the named package")
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Run only the entries tagged smoke = true (fast pre-commit subset)",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=1,
        help="Run N packages concurrently (xcodebuild already parallelizes inside each)",
    )
    parser.add_argument(
        "--keep-output",
        action="store_true",
        help="Keep produced xcframeworks under reports/<run>/<package>/work/",
    )
    args = parser.parse_args()

    if not TOOL.exists():
        die(f"spm-to-xcframework binary not found at {TOOL}")

    packages = load_packages(args.only, args.smoke)
    if not packages:
        die("No packages to run.")

    started = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    ts = started.strftime("%Y%m%dT%H%M%SZ")
    report_dir = REPORTS_DIR / ts
    suffix = 0
    while report_dir.exists():
        suffix += 1
        report_dir = REPORTS_DIR / f"{ts}_{suffix}"
    report_dir.mkdir(parents=True)

    toolchain = detect_toolchain()
    print(f"Running {len(packages)} package(s) → {report_dir.relative_to(REPO_ROOT)}")

    results: list[PackageResult] = []
    if args.parallel > 1 and len(packages) > 1:
        with ThreadPoolExecutor(max_workers=args.parallel) as pool:
            futures = {
                pool.submit(build_one, p, report_dir, args.keep_output): p
                for p in packages
            }
            for fut in as_completed(futures):
                p = futures[fut]
                try:
                    r = fut.result()
                except Exception as exc:  # noqa: BLE001 — never let a worker abort the run
                    r = PackageResult(
                        name=p.name,
                        status="fail",
                        duration_sec=0.0,
                        tool_exit_code=-1,
                        products_found=[],
                        products_missing=list(p.expected_products),
                        verify_failures=[],
                        error=f"runner exception: {exc!r}",
                        log_path="",
                    )
                results.append(r)
                _print_result(r)
        order = {p.name: i for i, p in enumerate(packages)}
        results.sort(key=lambda r: order[r.name])
    else:
        for p in packages:
            print(f"  running  {p.name} ({p.version})")
            r = build_one(p, report_dir, args.keep_output)
            results.append(r)
            _print_result(r)

    md_path = write_reports(results, report_dir, started, toolchain)
    print(f"\nReport: {md_path.relative_to(REPO_ROOT)}")

    real_failures = [r for r in results if r.status == "fail"]
    return 1 if real_failures else 0


def _print_result(r: PackageResult) -> None:
    label = {"pass": "PASS", "fail": "FAIL", "known_broken": "KNOWN"}[r.status]
    print(f"  {label:6s} {r.name} ({_fmt_duration(r.duration_sec)})")


if __name__ == "__main__":
    sys.exit(main())
