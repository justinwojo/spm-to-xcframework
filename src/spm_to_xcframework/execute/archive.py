"""Phase 4 — Execute · archive driver.

Houses xcodebuild's `archive` action driver, parallel slice scheduler,
and xcresult-bundle diagnostic extractor. Pure with respect to module-
level state so the parallel slice builds (`ThreadPoolExecutor`) stay
thread-safe.
"""
from __future__ import annotations

import collections
import concurrent.futures
import json
import subprocess
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from ..diagnostics import format_block, scan
from ..errors import ExecuteError
from ..log import info, verbose_log
from ..model import ArchiveSlice, BuildUnit
from ..platforms import PlatformSlice


def _archive_framework_path(archive_path: Path, framework_name: str) -> Optional[Path]:
    """Return the path of `<framework_name>.framework` inside `archive_path`'s
    Products tree, or None if it isn't there.

    SPM-driven archives drop frameworks in different places depending on the
    package's setup:
      - Apps with embedded frameworks land in `Products/Library/Frameworks/`.
      - Standalone library archives (the common case here) land in
        `Products/usr/local/lib/<X>.framework` because xcodebuild uses
        `INSTALL_PATH=/usr/local/lib` by default for SPM library targets.
      - A few packages move things further (custom Xcode projects, etc.).

    Rather than maintain a hardcoded list, we walk the Products tree once
    and look for an exact `<framework_name>.framework` directory. The
    legacy bash spm-to-xcframework uses the same `find Products -name
    *.framework` strategy at lines 1094, 1168, 1248. Returns the first
    match (sorted) or None if no `.framework` bundle exists at all.
    """
    products = archive_path / "Products"
    if not products.is_dir():
        return None
    matches = sorted(products.rglob(f"{framework_name}.framework"))
    for m in matches:
        if m.is_dir():
            return m
    return None


def run_xcodebuild_archive(
    *,
    staged_dir: Path,
    scheme: str,
    slice: PlatformSlice,
    deployment_target: str,
    archive_path: Path,
    dd_path: Path,
    result_bundle_path: Path,
    log_path: Path,
    verbose: bool,
    extra_swift_flags: Optional[Sequence[str]] = None,
) -> int:
    """Run `xcodebuild archive` for one (build unit, slice) combination.

    Captures combined stdout+stderr into `log_path`. In verbose mode the
    output is also tee'd to the terminal as it streams; non-verbose mode
    only writes to the log file. Returns the xcodebuild exit code (does
    not raise).

    The flag set is exactly the one specified in REWRITE_DESIGN.md §5.4:

        BUILD_LIBRARY_FOR_DISTRIBUTION=YES
        SKIP_INSTALL=NO
        <slice.deployment_target_var>=<deployment_target>
        GCC_TREAT_WARNINGS_AS_ERRORS=NO
        SWIFT_TREAT_WARNINGS_AS_ERRORS=NO
        OTHER_SWIFT_FLAGS=-no-verify-emitted-module-interface [<extras>]
        -skipPackagePluginValidation
        -skipMacroValidation

    `extra_swift_flags` is appended to the OTHER_SWIFT_FLAGS value as
    additional space-separated tokens. The canonical use is the macro
    support pass: per-unit `-Xfrontend -load-plugin-executable
    -Xfrontend <path>#<macro_name>` triplets so swiftc can expand
    `#externalMacro` calls against a pre-built host plugin (see
    `MacroSupport` in model.py for the architectural rationale). Each
    element is concatenated verbatim — callers are responsible for any
    shell quoting needed. In practice the work-dir paths we generate
    don't contain spaces, so unquoted joining is safe.

    There is **no MACH_O_TYPE=mh_dylib**. Dynamic linkage is handled at
    the Package.swift layer in Prepare; never at the xcodebuild CLI layer.
    The whole point of the rewrite is that the synthetic
    `.library(type: .dynamic)` plumbing in Prepare replaces the global
    `mh_dylib` override that the bash tool used.
    """
    # Make sure parent dirs exist for the archive / dd / xcresult / log paths.
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    dd_path.mkdir(parents=True, exist_ok=True)
    result_bundle_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # `xcodebuild archive` refuses to overwrite an existing archive, and
    # similarly will refuse a pre-existing result bundle. Clear them so
    # retries work.
    if archive_path.exists():
        shutil.rmtree(archive_path, ignore_errors=True)
    if result_bundle_path.exists():
        shutil.rmtree(result_bundle_path, ignore_errors=True)

    swift_flag_tokens: List[str] = ["-no-verify-emitted-module-interface"]
    if extra_swift_flags:
        swift_flag_tokens.extend(extra_swift_flags)
    other_swift_flags = "OTHER_SWIFT_FLAGS=" + " ".join(swift_flag_tokens)

    cmd = [
        "xcodebuild",
        "archive",
        "-scheme", scheme,
        "-destination", slice.destination,
        "-archivePath", str(archive_path),
        "-derivedDataPath", str(dd_path),
        "-resultBundlePath", str(result_bundle_path),
        "BUILD_LIBRARY_FOR_DISTRIBUTION=YES",
        "SKIP_INSTALL=NO",
        f"{slice.deployment_target_var}={deployment_target}",
        "GCC_TREAT_WARNINGS_AS_ERRORS=NO",
        "SWIFT_TREAT_WARNINGS_AS_ERRORS=NO",
        other_swift_flags,
        "-skipPackagePluginValidation",
        "-skipMacroValidation",
    ]

    verbose_log(verbose, f"  $ (cd {staged_dir} && {' '.join(cmd)})")

    if verbose:
        # Stream output line-by-line to both the log file and stdout. Using
        # Popen with text=True keeps line buffering on the file objects.
        with open(log_path, "w") as logf:
            proc = subprocess.Popen(
                cmd,
                cwd=str(staged_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                logf.write(line)
            return proc.wait()
    else:
        with open(log_path, "w") as logf:
            cp = subprocess.run(
                cmd,
                cwd=str(staged_dir),
                stdout=logf,
                stderr=subprocess.STDOUT,
                text=True,
            )
            return cp.returncode


def _parse_xcresult_build_results(data: dict, limit: int = 5) -> List[dict]:
    """Pure parser for the JSON returned by `xcrun xcresulttool get
    build-results`. Extracted from `read_xcresult_errors` so the unit
    tests can exercise it without invoking xcrun.

    The Xcode 16+ schema (verified against `xcrun xcresulttool get
    build-results --schema` on Xcode 26.2) shapes errors as:

        {
          "errors": [
            {
              "issueType": "...",
              "message": "...",
              "targetName": "...",   // optional
              "sourceURL": "...",    // optional
              "className": "..."     // optional
            },
            ...
          ],
          ...
        }

    Returns a list of dicts shaped like
    `{"target": str, "message": str, "source": str, "issueType": str}`.
    Bad shapes are silently dropped — the diagnostic is best-effort and
    must never blow up Execute's error path.
    """
    if not isinstance(data, dict):
        return []
    errors = data.get("errors")
    if not isinstance(errors, list):
        return []
    out: List[dict] = []
    for err in errors:
        if len(out) >= limit:
            break
        if not isinstance(err, dict):
            continue
        out.append({
            "target": str(err.get("targetName") or ""),
            "message": str(err.get("message") or ""),
            "source": str(err.get("sourceURL") or ""),
            "issueType": str(err.get("issueType") or ""),
        })
    return out


def read_xcresult_errors(result_bundle_path: Path, limit: int = 5) -> List[dict]:
    """Read up to `limit` build errors from an xcresult bundle.

    Uses the Xcode 16+ `xcresulttool get build-results` API exclusively
    (no `--legacy`, no `get object`). Returns an empty list on any failure
    — the bundle is a diagnostic, not a contract, and Execute's error
    handling must still surface the underlying xcodebuild failure even if
    we can't parse the bundle.
    """
    if not result_bundle_path.exists():
        return []
    cp = subprocess.run(
        [
            "xcrun", "xcresulttool", "get", "build-results",
            "--path", str(result_bundle_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if cp.returncode != 0:
        return []
    try:
        data = json.loads(cp.stdout)
    except json.JSONDecodeError:
        return []
    return _parse_xcresult_build_results(data, limit=limit)


def _format_execute_error(unit_name: str, log_path: Path, errors: List[dict]) -> str:
    """Build a human-readable ExecuteError message body for one failed
    build unit. Pulled out so the parallelization work in Session 4 can
    reuse it without re-deriving the format.

    Output shape:
        xcodebuild archive failed for build unit '<unit>'.

        Diagnosis: ...                                (when matched)
        Try: ...

        Top N error(s) from xcresult:
          [i] [<target>] <message>
              at <sourceURL>
        Build log: <path>

    The diagnosis block lands ABOVE the raw xcresult errors — the
    actionable signal must precede the evidence for a first-time user.
    Misses are silent (no block emitted) so unmatched failures look
    identical to the pre-diagnostics output.
    """
    lines = [f"xcodebuild archive failed for build unit {unit_name!r}."]

    # Scan against the joined xcresult-error messages + log tail so a
    # pattern that never reached xcresulttool can still match. Log-tail
    # read is best-effort: any failure leaves the haystack empty rather
    # than masking the real xcodebuild failure underneath.
    haystack_parts: List[str] = [
        f"{err.get('target') or ''} {err.get('message') or ''}"
        for err in errors
    ]
    try:
        if log_path.is_file():
            with open(log_path, "r", errors="replace") as f:
                haystack_parts.append("".join(f.readlines()[-200:]))
    except OSError:
        pass
    diag = scan("\n".join(haystack_parts))
    if diag is not None:
        lines.append("")
        lines.append(format_block(diag))

    lines.append("")
    if errors:
        lines.append(f"Top {len(errors)} error(s) from xcresult:")
        for i, err in enumerate(errors, start=1):
            target = err.get("target") or "(unknown target)"
            msg = err.get("message") or "(no message)"
            src = err.get("source") or ""
            head = f"  [{i}] [{target}] {msg}"
            lines.append(head)
            if src:
                lines.append(f"      at {src}")
    else:
        lines.append("(xcresult bundle did not parse or contained no errors — "
                     "fall back to the build log)")
    lines.append(f"Build log: {log_path}")
    return "\n".join(lines)


def _slice_paths(work_dir: Path, unit_name: str, slice_id: str) -> Tuple[Path, Path, Path, Path]:
    """Pure path computation for one (build unit, slice). Centralised so
    the parallel scheduler and the post-build framework lookup agree on
    where each artifact lives.

    `slice_id` is the platform-prefixed identifier from
    `PlatformSlice.slice_id` (e.g. `ios-arm64`, `ios-simulator`, `macos`).
    Returns (archive_path, dd_path, result_bundle_path, log_path).
    """
    archive_path = work_dir / "archives" / f"{unit_name}-{slice_id}.xcarchive"
    dd_path = work_dir / "dd" / unit_name / slice_id
    result_bundle_path = work_dir / "results" / f"{unit_name}-{slice_id}.xcresult"
    log_path = work_dir / f".build-output-{unit_name}-{slice_id}"
    return archive_path, dd_path, result_bundle_path, log_path


def _archive_one_slice(
    unit: BuildUnit,
    *,
    platform_slice: PlatformSlice,
    deployment_target: str,
    staged_dir: Path,
    work_dir: Path,
    verbose: bool,
    extra_swift_flags: Optional[Sequence[str]] = None,
) -> Tuple[ArchiveSlice, int]:
    """Run xcodebuild archive once for one (build unit, slice) combination
    and return the slice metadata plus the xcodebuild return code.

    Does NOT raise on xcodebuild failure — the caller (`_archive_all_parallel`)
    needs all futures to settle so it can tail every log and surface a
    consolidated error. Locating the framework / static lib also happens
    here so the caller can decide whether to trigger static promotion
    without re-walking the archive.

    Pure with respect to module-level state: thread-safe under
    `ThreadPoolExecutor`. If xcodebuild exits 0 but no
    `{unit.scheme}.framework` lands under `<archive>/Products/`, the
    returned slice has `framework_path = None` and the caller raises an
    ExecuteError — the synth_dynamic_library product must produce a
    real framework bundle for the rename + injection passes to consume.
    """
    archive_path, dd_path, result_bundle_path, log_path = _slice_paths(
        work_dir, unit.name, platform_slice.slice_id
    )
    rc = run_xcodebuild_archive(
        staged_dir=staged_dir,
        scheme=unit.scheme,
        slice=platform_slice,
        deployment_target=deployment_target,
        archive_path=archive_path,
        dd_path=dd_path,
        result_bundle_path=result_bundle_path,
        log_path=log_path,
        verbose=verbose,
        extra_swift_flags=extra_swift_flags,
    )
    framework_path = None
    if rc == 0:
        # Look up the framework by `unit.scheme` — that is the name
        # xcodebuild actually emits. For non-synth units the scheme
        # equals the framework_name; for `synth_dynamic_library` units
        # the scheme is the synthetic product name (e.g.
        # `AlamofireDynamic`) and the run-unit driver renames the bundle
        # to `unit.framework_name` after this returns.
        framework_path = _archive_framework_path(archive_path, unit.scheme)

    slice_obj = ArchiveSlice(
        arch_suffix=platform_slice.slice_id,
        sdk_name=platform_slice.sdk_name,
        archive_path=archive_path,
        dd_path=dd_path,
        log_path=log_path,
        result_bundle_path=result_bundle_path,
        framework_path=framework_path,
    )
    return slice_obj, rc


def _tail_log(log_path: Path, n: int = 5) -> str:
    """Return the last `n` lines of a log file as a single string. Empty
    string if the file is missing or unreadable. Used for the post-build
    summary tails after both parallel slices settle (avoids interleaving
    output during the build itself)."""
    if not log_path.is_file():
        return ""
    try:
        with open(log_path, "r", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return ""
    return "".join(lines[-n:])


def _archive_all_parallel(
    unit: BuildUnit,
    *,
    selected: Sequence[Tuple[PlatformSlice, str]],
    staged_dir: Path,
    work_dir: Path,
    verbose: bool,
    extra_swift_flags: Optional[Sequence[str]] = None,
) -> "collections.OrderedDict[str, ArchiveSlice]":
    """Build every requested (platform_slice, deployment_target) archive
    for one build unit in parallel via `ThreadPoolExecutor`.

    All futures must settle before this function returns — the caller
    needs the consolidated state to decide whether the unit succeeded
    completely, partially, or failed. Slice logs are captured to separate
    files (`_slice_paths` enforces uniqueness via the platform-prefixed
    `slice_id`) and tailed only after every future settles, so output
    never interleaves on the user's terminal.

    Raises ExecuteError if any slice's xcodebuild call exited non-zero,
    with the parsed xcresult diagnostics for whichever slice(s) failed.
    On success, returns an `OrderedDict` keyed by `slice_id` in the same
    order as `selected` so downstream consumers (create-xcframework merge,
    dry-run summary) see a stable ordering.
    """
    slice_labels = ", ".join(ps.slice_id for ps, _v in selected)
    info(f"  Building {unit.name} — {slice_labels} (parallel)...")

    # Cap pool size at 4 so a high-platform invocation (iOS + macOS +
    # Catalyst + tvOS + visionOS) doesn't oversubscribe the host's xcodebuild
    # licenses or DerivedData I/O. Two-wide for the default --min-ios case
    # — identical to the legacy two-pool behaviour.
    max_workers = min(4, max(1, len(selected)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures: Dict[concurrent.futures.Future, PlatformSlice] = {}
        for platform_slice, deployment_target in selected:
            fut = pool.submit(
                _archive_one_slice,
                unit,
                platform_slice=platform_slice,
                deployment_target=deployment_target,
                staged_dir=staged_dir,
                work_dir=work_dir,
                verbose=verbose,
                extra_swift_flags=extra_swift_flags,
            )
            futures[fut] = platform_slice
        results: Dict[str, Tuple[ArchiveSlice, int]] = {}
        for fut in concurrent.futures.as_completed(futures):
            platform_slice = futures[fut]
            try:
                slice_obj, rc = fut.result()
            except Exception as exc:  # pragma: no cover — defensive
                # Any unexpected exception (a missing xcodebuild on PATH,
                # a permissions error, etc.) is reported as an
                # ExecuteError tagged to the slice that blew up.
                raise ExecuteError(
                    f"unexpected failure during {unit.name} "
                    f"{platform_slice.slice_id} archive: {exc}"
                ) from exc
            results[platform_slice.slice_id] = (slice_obj, rc)

    # Re-order results to match `selected` order (`as_completed` shuffles
    # them by finish time, which would make logs unreliable).
    ordered: "collections.OrderedDict[str, ArchiveSlice]" = collections.OrderedDict()
    for platform_slice, _v in selected:
        ordered[platform_slice.slice_id] = results[platform_slice.slice_id][0]

    # Tail every log after every slice settles. Interleaved live output is
    # unreadable, so non-verbose mode defers summaries until everything
    # exits. Verbose mode already streams everything live so we skip the
    # extra tail there to avoid double-printing.
    if not verbose:
        for slice_obj in ordered.values():
            tail = _tail_log(slice_obj.log_path)
            if tail:
                sys.stdout.write(tail)
                if not tail.endswith("\n"):
                    sys.stdout.write("\n")
        sys.stdout.flush()

    failed: List[ArchiveSlice] = []
    for platform_slice, _v in selected:
        _slice_obj, rc = results[platform_slice.slice_id]
        if rc != 0:
            failed.append(_slice_obj)
    if failed:
        sections: List[str] = []
        for slice_obj in failed:
            errors = read_xcresult_errors(slice_obj.result_bundle_path, limit=5)
            slice_label = f"{unit.name} ({slice_obj.arch_suffix})"
            sections.append(_format_execute_error(slice_label, slice_obj.log_path, errors))
        raise ExecuteError("\n\n".join(sections))

    return ordered
