"""Phase 5 — Verify.

Strict, per-unit verification of every produced xcframework. The
downstream .NET binding generator can't consume an xcframework that's
missing `.swiftinterface` (Swift path) or public headers + modulemap
(ObjC path), so the contract for this phase is "the build is only
successful if every planned unit can actually be bound." Verify makes
the failure visible instead of letting it surface days later in the
binding step.

Anything Verify finds is recorded on a `VerifyResult` rather than
raised: normal failures (broken Info.plist, missing slice, static
binary) flow through the same data path as the success printer, and
`main()` decides the exit code from the aggregate. `VerifyError` is
reserved for verify code crashing on its own — that's the path that
gets a Python traceback per REWRITE_DESIGN.md §7.
"""
from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path
from typing import List, Sequence, Set, Tuple

from .errors import VerifyUserError
from .log import _wrap, bold, dim, success, warn
from .model import ExecutedUnit, Language, VerifyResult
from .platforms import (
    _platform_from_library_identifier,
    _variant_from_library_identifier,
)
from .execute.create_xcframework import (
    _iter_primary_framework_paths,
    detect_framework_type,
)


def _check_binary_dynamic(binary: Path) -> bool:
    """Return True iff `file <binary>` reports a dynamically-linked Mach-O.

    The legacy bash uses the same string match — see
    `validate_xcframework` in the bash original. Static archives report
    `current ar archive` and fail the check; missing-binary / file-tool
    failures also fail the check (the caller surfaces "binary missing"
    separately, so a False here is unambiguous).

    Tests monkey-patch this attribute on the module to skip the real
    `file` invocation against synthetic test fixtures.
    """
    try:
        cp = subprocess.run(
            ["file", "-b", str(binary)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (FileNotFoundError, OSError):
        return False
    if cp.returncode != 0:
        return False
    return "dynamically linked" in cp.stdout


def _directory_size_bytes(path: Path) -> int:
    """Walk `path` and sum every regular file's size in bytes.

    Uses `os.lstat` so symlinks count as the size of the link itself, not
    the target — matches `du -sh` behaviour and avoids accidentally
    double-counting cycles.
    """
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            try:
                total += os.lstat(full).st_size
            except OSError:
                # File vanished mid-walk or unreadable — ignore.
                pass
    return total


def _format_size_iec(bytes_count: int) -> str:
    """1024-based human-readable size, matching `du -sh` formatting.

    Bytes        → "{n}B"
    < 1 MiB      → "{rounded n}K"     e.g. "880K", "310K"
    < 1 GiB      → "{n.n}M"           e.g. "4.2M", "1.1M"
    >= 1 GiB     → "{n.n}G"

    Negative input is clamped to 0 (defensive — verify always passes a
    non-negative directory size).
    """
    bytes_count = max(0, int(bytes_count))
    K = 1024
    M = K * 1024
    G = M * 1024
    if bytes_count < K:
        return f"{bytes_count}B"
    if bytes_count < M:
        return f"{round(bytes_count / K)}K"
    if bytes_count < G:
        return f"{bytes_count / M:.1f}M"
    return f"{bytes_count / G:.1f}G"


def _verify_one_unit(unit: ExecutedUnit, output_dir: Path) -> VerifyResult:
    """Run §5.5's strict checks against one already-built ExecutedUnit.

    The contract is "every fatal-per-unit failure ends up in
    `result.fatal_issues`; advisories end up in `result.warnings`;
    `result.passed` is true iff `fatal_issues` is empty after every check
    has run." Verify never short-circuits — collecting all the fatal
    findings in one pass makes the per-unit error message in the summary
    actually useful, instead of showing one issue at a time across
    multiple invocations.
    """
    # Late-bind `_check_binary_dynamic` via the package namespace so tests
    # that monkey-patch `spm_to_xcframework._check_binary_dynamic` are
    # honored. A direct module-level reference would freeze the original
    # function into our globals and silently ignore the patch.
    from . import _check_binary_dynamic  # noqa: F811 — late-bound shadow

    fatal: List[str] = []
    warnings: List[str] = []

    xcframework_path = (
        unit.xcframework_path
        if unit.xcframework_path is not None
        else output_dir / f"{unit.name}.xcframework"
    )
    framework_name = unit.framework_name or unit.name

    result = VerifyResult(
        unit_name=unit.name,
        framework_name=framework_name,
        xcframework_path=xcframework_path,
        framework_type=unit.framework_type or "Unknown",
        size_bytes=0,
        passed=False,
        fatal_issues=fatal,
        warnings=warnings,
    )

    # Fatal #1: xcframework directory exists.
    if not xcframework_path.exists():
        fatal.append(f"xcframework not found at {xcframework_path}")
        return result
    if not xcframework_path.is_dir():
        fatal.append(f"xcframework path is not a directory: {xcframework_path}")
        return result

    # Compute size up front so even partial failures get a size in the row.
    result.size_bytes = _directory_size_bytes(xcframework_path)

    # Fatal #2: Info.plist exists and parses via plistlib (catches
    # AppleDouble `__MACOSX` ghost plists, which are resource forks
    # that plistlib.InvalidFileException's on).
    info_plist = xcframework_path / "Info.plist"
    if not info_plist.is_file():
        fatal.append(
            "Info.plist missing — xcframework structure is corrupt "
            "(possible __MACOSX ghost?)"
        )
        return result
    try:
        with info_plist.open("rb") as fh:
            plist_data = plistlib.load(fh)
    except plistlib.InvalidFileException as exc:
        fatal.append(
            f"Info.plist parse failed (likely __MACOSX ghost / AppleDouble fork): {exc}"
        )
        return result
    except OSError as exc:
        fatal.append(f"Info.plist read failed: {exc}")
        return result
    except Exception as exc:  # noqa: BLE001 - defensive: any plist corruption is fatal-per-unit
        fatal.append(f"Info.plist load raised {type(exc).__name__}: {exc}")
        return result

    available = plist_data.get("AvailableLibraries")
    if not isinstance(available, list):
        fatal.append("Info.plist has no AvailableLibraries array")
        return result

    # Coverage check: when the unit carries `expected_slice_classes`,
    # every requested (platform, variant) pair must be represented by at
    # least one AvailableLibraries entry. The (platform, variant) shape
    # is tighter than family coverage — an iOS-device-only binary would
    # otherwise satisfy `--min-ios` despite missing the simulator slice
    # that the same flag promises to produce. Primary motivation is
    # binary mode, where we copy a vendor artifact unchanged and have no
    # other way to verify the requested coverage is actually present.
    if unit.expected_slice_classes:
        covered_pairs: Set[Tuple[str, str]] = set()
        for entry in available:
            if not isinstance(entry, dict):
                continue
            lid = str(entry.get("LibraryIdentifier") or "")
            plat = _platform_from_library_identifier(lid)
            if plat is None:
                continue
            covered_pairs.add((plat, _variant_from_library_identifier(lid)))
        missing_pairs = [
            (p, v) for (p, v) in unit.expected_slice_classes
            if (p, v) not in covered_pairs
        ]
        if missing_pairs:
            missing_repr = ", ".join(f"{p}-{v}" for p, v in missing_pairs)
            covered_repr = ", ".join(
                sorted(f"{p}-{v}" for p, v in covered_pairs)
            ) or "(none)"
            fatal.append(
                f"xcframework missing requested slice(s): {missing_repr} "
                f"(covered: {covered_repr})"
            )

    # Fatal: every slice's binary must be dynamically linked.
    for entry in available:
        if not isinstance(entry, dict):
            fatal.append(f"AvailableLibraries entry is not a dict: {entry!r}")
            continue
        identifier = str(entry.get("LibraryIdentifier") or "<unknown>")
        library_path = entry.get("LibraryPath") or ""
        binary_path = entry.get("BinaryPath") or ""
        if not library_path:
            fatal.append(f"slice {identifier}: missing LibraryPath in plist")
            continue
        if not binary_path:
            # Older xcframeworks omit BinaryPath; reconstruct as
            # `<LibraryPath>/<basename(LibraryPath, .framework)>`. The basename
            # call matters: if `LibraryPath` is `Frameworks/Foo.framework`
            # the binary lives at `Frameworks/Foo.framework/Foo`, not
            # `Frameworks/Foo.framework/Frameworks/Foo`. Only `.framework`
            # layouts are supported in this fallback — other library layouts
            # (e.g. `.a` static libs) ship a `BinaryPath` explicitly.
            lib_basename = os.path.basename(library_path.rstrip("/"))
            if not lib_basename.endswith(".framework"):
                fatal.append(
                    f"slice {identifier}: missing BinaryPath and "
                    f"LibraryPath {library_path!r} is not a .framework "
                    "(unsupported xcframework layout)"
                )
                continue
            stem = lib_basename[: -len(".framework")]
            binary_path = f"{library_path.rstrip('/')}/{stem}"
        binary = xcframework_path / identifier / binary_path
        if not binary.is_file():
            fatal.append(
                f"slice {identifier}: binary missing at "
                f"{binary.relative_to(xcframework_path)}"
            )
            continue
        if not _check_binary_dynamic(binary):
            fatal.append(
                f"slice {identifier}: binary is not dynamically linked "
                "(static archive masquerading as a framework?)"
            )

    # Post-hoc detection from the on-disk bytes. This is honest about
    # what shipped and drives the summary label, but it must NOT drive
    # the fatal checks on its own — a partially-built Mixed unit that
    # lost its ObjC surface would look like "Swift" to this function and
    # silently pass. Use the plan-time expected language below to decide
    # which surfaces are required.
    detected_type = detect_framework_type(xcframework_path)
    result.framework_type = detected_type

    # Walk the xcframework's primary framework once to gather the
    # language-specific facts. We deliberately scope this walk via
    # `_iter_primary_framework_paths` so sibling system Clang module
    # shims (injected by `inject_system_clang_modules`) don't masquerade
    # as ObjC surface on a Swift unit and silently fill in
    # has_public_header / has_modulemap.
    has_swiftinterface = False
    has_public_header = False
    has_modulemap = False
    has_abi_json = False
    for path in _iter_primary_framework_paths(xcframework_path):
        name = path.name
        if not has_swiftinterface and name.endswith(".swiftinterface"):
            has_swiftinterface = True
        if not has_abi_json and name.endswith(".abi.json"):
            has_abi_json = True
        if (
            not has_public_header
            and name.endswith(".h")
            and not name.endswith("-Swift.h")
            and "Headers" in path.parts
        ):
            has_public_header = True
        if not has_modulemap and name == "module.modulemap":
            has_modulemap = True
        if has_swiftinterface and has_public_header and has_modulemap and has_abi_json:
            break

    # Decide which language surfaces are required. Prefer the plan-time
    # expected language; fall back to post-hoc detection only when the
    # plan didn't carry one (legacy callers, binary mode, N/A targets).
    # This is the fix for the "mixed-language artifact silently passes
    # as Swift" hole — if Plan said Mixed, Verify MUST require both the
    # Swift ABI surface AND the ObjC header/modulemap surface, regardless
    # of what managed to land on disk.
    if unit.expected_language in ("Swift", "ObjC", "Mixed"):
        required_type = unit.expected_language
        required_source = "plan"
    else:
        required_type = detected_type
        required_source = "detected"

    # Fatal #5: Swift / Mixed must have at least one .swiftinterface.
    if required_type in ("Swift", "Mixed"):
        if not has_swiftinterface:
            if required_source == "plan":
                fatal.append(
                    f"plan expected {required_type} framework but zero "
                    ".swiftinterface files were produced (binding generation "
                    "cannot proceed without ABI surface)"
                )
            else:
                fatal.append(
                    "Swift/Mixed framework has zero .swiftinterface files "
                    "(binding generation cannot proceed without ABI surface)"
                )

    # Fatal #6: ObjC / Mixed must have public headers AND a modulemap.
    if required_type in ("ObjC", "Mixed"):
        if not has_public_header:
            if required_source == "plan":
                fatal.append(
                    f"plan expected {required_type} framework but no public "
                    ".h files were produced under Headers/ (ObjC header "
                    "injection likely failed)"
                )
            else:
                fatal.append(
                    "ObjC/Mixed framework has zero public .h files under Headers/"
                )
        if not has_modulemap:
            if required_source == "plan":
                fatal.append(
                    f"plan expected {required_type} framework but no "
                    "module.modulemap was produced"
                )
            else:
                fatal.append(
                    "ObjC/Mixed framework has no module.modulemap"
                )

    # Non-fatal warnings.
    if required_type == "Unknown":
        warnings.append(
            "framework type detected as Unknown — no .swiftinterface and "
            "no public headers; binding generation may not work"
        )
    if required_type in ("Swift", "Mixed") and not has_abi_json:
        warnings.append(
            "no .abi.json present (binding generator regenerates from "
            ".swiftinterface at bind time, so this is not a blocker)"
        )
    # Drift advisory: Plan said one thing, disk shows another. Not
    # fatal on its own because the fatal checks above already cover
    # the concrete missing-surface case, but surfacing the mismatch
    # makes failures less mysterious.
    if (
        required_source == "plan"
        and detected_type not in ("", "Unknown")
        and detected_type != required_type
    ):
        # The Swift→Mixed direction is benign for Stripe-shaped products
        # (Swift target with a `<TargetName>.h` umbrella stub at target
        # root): the umbrella injection is intentional so ObjC consumers
        # can `#import <Mod/Mod.h>`, and post-hoc detection sees the
        # Headers/ + modulemap and reports Mixed. Other directions (Mixed
        # plan but ObjC on disk, ObjC plan but Swift on disk, etc.) still
        # signal a partial injection worth investigating.
        if required_type == Language.SWIFT and detected_type == Language.MIXED:
            warnings.append(
                f"plan expected Swift framework but on-disk detection "
                "says Mixed (expected for products that ship a "
                "<TargetName>.h umbrella stub for ObjC consumers)"
            )
        else:
            warnings.append(
                f"plan expected {required_type} framework but on-disk "
                f"detection says {detected_type} (this usually means an "
                "injection step partially ran)"
            )
    if result.size_bytes > 500 * 1024 * 1024:
        warnings.append(
            f"size {_format_size_iec(result.size_bytes)} exceeds the 500 MB "
            "sanity threshold"
        )

    result.passed = not fatal
    return result


def verify_output(
    executed_units: Sequence[ExecutedUnit],
    output_dir: Path,
    min_ios: str = "",
) -> List[VerifyResult]:
    """Strict per-unit verification of every produced xcframework.

    Returns a list of `VerifyResult`s — one per `ExecutedUnit` — with the
    pass/fail flags and per-unit issues populated. The aggregate exit code
    is the caller's responsibility (see `print_verify_summary` and
    `_run_source_mode` / `_run_binary_mode` in main).

    `min_ios` is accepted for symmetry with the design signature; this
    pass doesn't currently cross-check against `MinimumOSVersion`, but the
    parameter is reserved so future strict checks (e.g. "rejected if the
    framework's minimum is below what the user asked for") can land
    without an API change.
    """
    del min_ios  # reserved
    if not output_dir.is_dir():
        raise VerifyUserError(
            f"output directory missing or not a directory: {output_dir}"
        )
    return [_verify_one_unit(unit, output_dir) for unit in executed_units]


def print_verify_summary(
    results: Sequence[VerifyResult],
    output_dir: Path,
) -> None:
    """Render the §5.5 final summary block.

    Format mirrors the legacy bash so consumers don't need to retrain
    their eyes:

        === Summary ===
          Built: 5    Verified: 5    Failed: 0

        Output: <dir>

        Xcframeworks:
          Foo.xcframework      (4.2M) [Swift]
          Bar.xcframework      (1.1M) [ObjC]

    Failed-unit detail is printed in red between the counts and the
    output line so it's the first thing the user sees when something
    broke. Per-unit warnings (size outliers, missing abi.json) are
    surfaced as `warn(...)` lines after the success table.
    """
    bold("\n=== Summary ===")
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    failed = total - passed
    counts_line = (
        f"  Built: {total}    Verified: {passed}    Failed: {failed}"
    )
    if failed:
        print(_wrap(counts_line, "red"))
    else:
        success(counts_line)

    if failed:
        print()
        print(_wrap("Failed units:", "red"))
        failed_results = [r for r in results if not r.passed]
        name_w = max(len(r.xcframework_path.name) for r in failed_results) + 2
        for r in failed_results:
            issues = "; ".join(r.fatal_issues) if r.fatal_issues else "(no detail)"
            print(_wrap(
                f"  {r.xcframework_path.name:<{name_w}}: {issues}",
                "red",
            ))

    print()
    bold(f"Output: {output_dir}")

    passed_results = [r for r in results if r.passed]
    if passed_results:
        print()
        dim("Xcframeworks:")
        name_w = max(len(r.xcframework_path.name) for r in passed_results) + 2
        for r in passed_results:
            size = _format_size_iec(r.size_bytes)
            label = f"[{r.framework_type}]"
            print(f"  {r.xcframework_path.name:<{name_w}}({size}) {label}")

    # Surface non-fatal warnings under each xcframework so the user sees
    # them. Failed units' warnings are deliberately swallowed — the
    # `Failed units:` block already names the issues that matter.
    for r in results:
        if r.passed and r.warnings:
            for w in r.warnings:
                warn(f"  {r.xcframework_path.name}: {w}")
