#!/usr/bin/env python3
"""Wild-sample stress-test campaign for spm-to-xcframework.

Two modes, sharing a single curated candidate list:

* `plan` (default) — runs `spm-to-xcframework --dry-run-json` against each
  candidate. Fast: seconds per package, default 4-way parallel. Validates
  that Fetch + Inspect + Plan terminate cleanly and produce a parseable
  Plan envelope. Plan-phase success is *necessary but not sufficient* for
  archive success — xcodebuild can still fail downstream.

* `archive` (--archive) — runs the tool end-to-end (no --dry-run-json),
  writes each candidate's xcframeworks into a per-candidate tempdir, then
  verifies at least one non-empty `.xcframework` was produced. Slow:
  minutes per package, defaults to sequential (jobs=1) so xcodebuild
  doesn't thrash. Designed for overnight batch runs to put a real number
  on the project's "95%+ of arbitrary SPM packages produce a valid
  xcframework on the first try" lower bound.

The two modes share the same candidate list and the same diagnosis-
extraction logic, so a candidate that diagnoses cleanly at plan time
diagnoses the same way at archive time too.

Usage:
    python3 tests/integration/wild_sample.py
    python3 tests/integration/wild_sample.py --jobs 8 --report /tmp/wild-plan.md
    python3 tests/integration/wild_sample.py --only swift-log
    python3 tests/integration/wild_sample.py --archive --report /tmp/wild-archive.md
    python3 tests/integration/wild_sample.py --archive --only Moya --only swift-log

Plan-mode candidates get a 90-second timeout — Fetch+Inspect+Plan should
be well under a minute even for large packages. Archive-mode candidates
get 30 minutes (matching run_integration.py's BUILD_TIMEOUT_SEC) since
xcodebuild dominates the wall time.
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL = REPO_ROOT / "spm-to-xcframework"
DRY_RUN_TIMEOUT_SEC = 90
ARCHIVE_TIMEOUT_SEC = 30 * 60


@dataclasses.dataclass
class Candidate:
    name: str
    url: str
    version: str
    args: List[str] = dataclasses.field(default_factory=list)
    notes: str = ""


# Curated long-tail candidates. The goal is breadth, not stars-ranking.
# Already-in-matrix packages (snapkit, mbprogresshud, grdb, nuke,
# swift-collections, lottie-spm, sentry-cocoa, swift-dependencies, wcdb,
# stripe-ios, swift-syntax, swift-case-paths, swift-argument-parser,
# swift-async-algorithms, swift-composable-architecture, alamofire,
# kingfisher) are excluded by definition.
#
# Versions pinned to a recent tag at campaign authoring time so re-runs
# stay reproducible. If a candidate's tag has been retracted upstream,
# the Fetch step will fail with a clean error and that entry shows up
# in the report as a Fetch failure — the rest of the campaign continues.
CANDIDATES: List[Candidate] = [
    # Apple ecosystem — well-maintained SPM canon
    Candidate("swift-log", "https://github.com/apple/swift-log", "1.6.4",
              notes="simple pure-Swift logging API"),
    Candidate("swift-metrics", "https://github.com/apple/swift-metrics", "2.7.0",
              notes="simple pure-Swift metrics API"),
    Candidate("swift-system", "https://github.com/apple/swift-system", "1.5.0",
              notes="low-level system bindings"),
    Candidate("swift-protobuf", "https://github.com/apple/swift-protobuf", "1.32.0",
              notes="codegen + runtime"),
    Candidate("swift-crypto", "https://github.com/apple/swift-crypto", "4.5.0",
              notes="crypto primitives, large graph"),
    Candidate("swift-nio", "https://github.com/apple/swift-nio", "2.86.0",
              notes="networking, complex multi-product graph"),
    Candidate("swift-atomics", "https://github.com/apple/swift-atomics", "1.3.0",
              notes="atomic primitives"),
    Candidate("swift-numerics", "https://github.com/apple/swift-numerics", "1.0.3",
              notes="numerics primitives"),
    Candidate("swift-algorithms", "https://github.com/apple/swift-algorithms", "1.2.1",
              notes="algorithms library"),
    Candidate("swift-format", "https://github.com/apple/swift-format", "601.0.0",
              notes="formatter + lib, build-tool plugin candidate"),

    # PointFree — frequently uses macros, has deep transitive graphs
    Candidate("swift-snapshot-testing",
              "https://github.com/pointfreeco/swift-snapshot-testing", "1.18.7",
              notes="snapshot testing, simple pure-Swift"),
    Candidate("swift-perception",
              "https://github.com/pointfreeco/swift-perception", "2.0.6",
              notes="macros, observation backport"),
    Candidate("combine-schedulers",
              "https://github.com/pointfreeco/combine-schedulers", "1.0.3",
              notes="reactive helpers"),
    Candidate("swift-clocks",
              "https://github.com/pointfreeco/swift-clocks", "1.0.6",
              notes="testing-friendly clocks"),
    Candidate("swift-identified-collections",
              "https://github.com/pointfreeco/swift-identified-collections", "1.1.1",
              notes="ordered keyed collections"),
    Candidate("swift-tagged",
              "https://github.com/pointfreeco/swift-tagged", "0.10.0",
              notes="newtype-like tagged values"),

    # Networking / common ecosystem libs
    Candidate("alamofire-image",
              "https://github.com/Alamofire/AlamofireImage", "4.3.0",
              notes="Alamofire image variant, deps on Alamofire"),
    Candidate("Moya", "https://github.com/Moya/Moya", "15.0.3",
              notes="HTTP abstraction, depends on Alamofire"),
    Candidate("SDWebImage",
              "https://github.com/SDWebImage/SDWebImage", "5.21.4",
              notes="largely ObjC image library, exercises ObjC path"),
    Candidate("swift-collections-rx",
              "https://github.com/ReactiveX/RxSwift", "6.9.0",
              notes="reactive streams, large graph"),
    Candidate("PromiseKit", "https://github.com/mxcl/PromiseKit", "8.1.2",
              notes="promises, multi-product"),
    Candidate("Get", "https://github.com/kean/Get", "2.2.1",
              notes="HTTP client, pure Swift"),

    # Data / persistence
    Candidate("SQLite.swift",
              "https://github.com/stephencelis/SQLite.swift", "0.15.5",
              notes="SQLite wrapper, pure Swift"),

    # UI
    Candidate("SwiftMessages",
              "https://github.com/SwiftKickMobile/SwiftMessages", "10.0.2",
              notes="UI messages, ObjC compatibility"),
    Candidate("Charts", "https://github.com/danielgindi/Charts", "5.1.0",
              notes="charts library, complex product"),
    Candidate("Hero", "https://github.com/HeroTransitions/Hero", "1.6.4",
              notes="transition library"),
    Candidate("SwiftUI-Introspect",
              "https://github.com/siteline/swiftui-introspect", "1.3.0",
              notes="SwiftUI helper, multi-product"),
    Candidate("Then", "https://github.com/devxoul/Then", "3.0.0",
              notes="tiny utility lib"),

    # Logging
    Candidate("CocoaLumberjack",
              "https://github.com/CocoaLumberjack/CocoaLumberjack", "3.8.5",
              notes="mixed ObjC/Swift logger"),

    # Crypto
    Candidate("CryptoSwift",
              "https://github.com/krzyzanowskim/CryptoSwift", "1.8.4",
              notes="pure-Swift crypto, large module"),

    # Testing
    Candidate("Quick", "https://github.com/Quick/Quick", "7.6.2",
              notes="BDD testing framework"),
    Candidate("Nimble", "https://github.com/Quick/Nimble", "13.7.1",
              notes="matchers library"),

    # Web / Realtime
    Candidate("Starscream",
              "https://github.com/daltoniam/Starscream", "4.0.8",
              notes="websocket client"),
    Candidate("SwiftPhoenixClient",
              "https://github.com/davidstump/SwiftPhoenixClient", "5.3.5",
              notes="Phoenix websocket client"),

    # Utility
    Candidate("ZIPFoundation",
              "https://github.com/weichsel/ZIPFoundation", "0.9.19",
              notes="zip handling, pure Swift"),
    Candidate("SwiftyJSON",
              "https://github.com/SwiftyJSON/SwiftyJSON", "5.0.2",
              notes="JSON wrapper"),
    Candidate("Files", "https://github.com/JohnSundell/Files", "4.3.0",
              notes="file system wrapper"),
    Candidate("ShellOut", "https://github.com/JohnSundell/ShellOut", "2.3.0",
              notes="shell wrapper, tiny lib"),
    Candidate("Defaults", "https://github.com/sindresorhus/Defaults", "9.0.4",
              notes="UserDefaults wrapper"),
    Candidate("KeychainAccess",
              "https://github.com/kishikawakatsumi/KeychainAccess", "4.2.2",
              notes="keychain wrapper"),

    # Apollo / GraphQL
    Candidate("apollo-ios",
              "https://github.com/apollographql/apollo-ios", "1.21.0",
              notes="GraphQL client, multi-product, build-tool plugin",
              args=["--product", "Apollo"]),

    # Realm — known mixed Swift/ObjC, may surface known issues
    Candidate("realm-swift",
              "https://github.com/realm/realm-swift", "20.0.3",
              notes="mixed Swift/ObjC, may have binary dependencies",
              args=["--product", "RealmSwift"]),
]


@dataclasses.dataclass
class Result:
    name: str
    url: str
    version: str
    mode: str                              # "plan" or "archive"
    exit_code: int
    duration_sec: float
    timed_out: bool
    plan_envelope: Optional[dict]          # plan mode only: parsed --dry-run-json stdout
    stderr_tail: str
    diagnosis: Optional[str]
    # Archive-mode fields. Populated only after a successful tool exit;
    # `archive_failure` is set when the tool said exit 0 but the on-disk
    # xcframework didn't pass the basic exists+non-empty contract — the
    # most interesting bucket because the tool's own Verify phase didn't
    # catch the regression.
    xcframeworks: List[str] = dataclasses.field(default_factory=list)
    xcframework_bytes: int = 0
    archive_failure: Optional[str] = None


# Two diagnosis shapes to recognise:
#   1. xcodebuild-archive failures produce a labelled "Diagnosis: ... /
#      Try: ..." block via `_format_execute_error` (diagnostics.scan).
#   2. `swift package` failures produce a "First error: ... / Try: ..."
#      block via diagnostics.format_swift_package_failure.
# A `Try:` line is the universal anchor — it's the actionable hint the
# user is meant to act on, regardless of which producer emitted it.
_DIAGNOSIS_RE = re.compile(
    r"((?:Diagnosis|First error):[^\n]+(?:\n\s*Try:[^\n]+)?)",
    re.MULTILINE,
)


def _extract_diagnosis(stderr: str) -> Optional[str]:
    """Return the first labelled diagnosis block in `stderr`, or None.

    We only count a block as "diagnosed" if it carries a `Try:` line —
    a bare `First error:` without a hint is the un-shaped degraded
    case (no pattern matched in format_swift_package_failure), which
    should still bucket as undiagnosed_failure so the campaign report
    flags it for new-pattern follow-up.
    """
    match = _DIAGNOSIS_RE.search(stderr)
    if not match:
        return None
    block = match.group(1).strip()
    if "\nTry:" not in block and " Try:" not in block:
        return None
    return block


def _stderr_tail(text: str, lines: int = 30) -> str:
    return "\n".join(text.splitlines()[-lines:])


def _fmt_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def _execute(
    cmd: List[str],
    cand: Candidate,
    *,
    work_dir: Path,
    timeout_sec: int,
    mode: str,
) -> Result:
    """Shared subprocess wrapper used by both modes; never raises."""
    work_dir.mkdir(parents=True, exist_ok=True)
    start = time.time()
    timed_out = False
    stdout = ""
    stderr = ""
    exit_code = -1
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(work_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_sec,
        )
        exit_code = proc.returncode
        stdout = proc.stdout
        stderr = proc.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        # `subprocess.TimeoutExpired.stdout` / `.stderr` are documented
        # as "always bytes" — even when `text=True` was passed to
        # `subprocess.run()`. The text-mode decoding happens at the end
        # of `_communicate()`, after `_check_timeout()` has already
        # raised with the raw byte buffers. So on a real timeout we
        # must decode here; the downstream consumers (`_stderr_tail`,
        # `_extract_diagnosis`) all expect `str`.
        stdout_raw = exc.stdout or b""
        stderr_raw = exc.stderr or b""
        stdout = (
            stdout_raw.decode("utf-8", errors="replace")
            if isinstance(stdout_raw, (bytes, bytearray))
            else stdout_raw
        )
        stderr = (
            stderr_raw.decode("utf-8", errors="replace")
            if isinstance(stderr_raw, (bytes, bytearray))
            else stderr_raw
        )
    duration = time.time() - start

    envelope: Optional[dict] = None
    if mode == "plan" and exit_code == 0 and stdout.strip():
        try:
            envelope = json.loads(stdout)
        except json.JSONDecodeError:
            envelope = None

    diagnosis = _extract_diagnosis(stderr) if exit_code != 0 else None
    # Archive runs emit a much chattier stderr (full xcodebuild logs); the
    # tool's own Diagnosis block lives near the end so 100 lines of tail
    # is enough to capture it plus surrounding context for triage.
    tail_lines = 100 if mode == "archive" else 30
    return Result(
        name=cand.name,
        url=cand.url,
        version=cand.version,
        mode=mode,
        exit_code=exit_code,
        duration_sec=duration,
        timed_out=timed_out,
        plan_envelope=envelope,
        stderr_tail=_stderr_tail(stderr, lines=tail_lines),
        diagnosis=diagnosis,
    )


def run_plan(cand: Candidate, work_dir: Path, timeout_sec: int) -> Result:
    """Run --dry-run-json against one candidate."""
    cmd = [str(TOOL), cand.url, "-v", cand.version, "--dry-run-json", *cand.args]
    return _execute(cmd, cand, work_dir=work_dir, timeout_sec=timeout_sec, mode="plan")


def run_archive(cand: Candidate, timeout_sec: int) -> Result:
    """Full xcodebuild-archive run against one candidate.

    Each candidate gets its own auto-cleaned tempdir so peak disk usage
    stays bounded to the number of in-flight workers' active runs —
    important for overnight batches where ~40 archives back-to-back
    would otherwise accumulate gigabytes of intermediate output. We
    inspect the produced xcframeworks while the tempdir is still alive,
    then let the context manager remove everything before returning.
    """
    with tempfile.TemporaryDirectory(prefix=f"spm2xc-wild-archive-{cand.name}-") as tmp:
        tmp_path = Path(tmp)
        out_dir = tmp_path / "xcframeworks"
        cmd = [
            str(TOOL), cand.url, "-v", cand.version,
            "-o", str(out_dir),
            *cand.args,
        ]
        result = _execute(
            cmd, cand,
            work_dir=tmp_path,
            timeout_sec=timeout_sec,
            mode="archive",
        )
        if not result.timed_out and result.exit_code == 0:
            _evaluate_archive(result, out_dir)
        return result


def _evaluate_archive(result: Result, out_dir: Path) -> None:
    """Verify at least one .xcframework exists and is non-empty.

    "Non-empty" here is the minimum useful contract: Info.plist parses,
    declares at least one library, and every declared LibraryPath
    resolves to a binary with size > 0. We deliberately don't run the
    full verify contract (Mach-O dynamic-link check, .swiftinterface
    unresolved-imports scan, header-on-disk check, etc.) — that's the
    curated `run_integration.py` matrix's job. Wild-sample answers a
    coarser question: "did xcodebuild produce something on disk that
    isn't an empty shell?".
    """
    if not out_dir.exists():
        result.archive_failure = "tool exited 0 but no output directory was created"
        return
    xcfws = sorted(out_dir.glob("*.xcframework"))
    if not xcfws:
        result.archive_failure = "tool exited 0 but no .xcframework was produced"
        return
    for x in xcfws:
        size = sum(
            p.stat().st_size for p in x.rglob("*")
            if p.is_file() and not p.is_symlink()
        )
        result.xcframeworks.append(x.name)
        result.xcframework_bytes += size
        # Latch the first failure but keep enumerating so the report
        # still lists every produced bundle and the cumulative size.
        if result.archive_failure is not None:
            continue
        info_plist = x / "Info.plist"
        if not info_plist.exists() or info_plist.stat().st_size == 0:
            result.archive_failure = f"{x.name}: missing or empty Info.plist"
            continue
        try:
            with open(info_plist, "rb") as f:
                plist = plistlib.load(f)
        except Exception as exc:  # noqa: BLE001 — plist corruption is itself a failure
            result.archive_failure = f"{x.name}: Info.plist parse failed: {exc}"
            continue
        libs = plist.get("AvailableLibraries") or []
        if not libs:
            result.archive_failure = f"{x.name}: AvailableLibraries empty"
            continue
        for lib in libs:
            ident = lib.get("LibraryIdentifier", "?")
            lp = lib.get("LibraryPath") or ""
            slice_root = x / ident / lp
            if not slice_root.exists():
                result.archive_failure = (
                    f"{x.name}/{ident}: LibraryPath {lp!r} missing"
                )
                break
            binary = _find_slice_binary(slice_root)
            if binary is None:
                result.archive_failure = (
                    f"{x.name}/{ident}: no binary inside {lp!r}"
                )
                break
            if binary.stat().st_size == 0:
                result.archive_failure = (
                    f"{x.name}/{ident}: binary {binary.name!r} is empty"
                )
                break


def _find_slice_binary(slice_root: Path) -> Optional[Path]:
    """Locate the binary file inside a framework or static-lib slice."""
    if slice_root.is_file():
        # Static-library slice — LibraryPath points directly at .a / .dylib.
        return slice_root
    name = slice_root.name.removesuffix(".framework")
    for candidate in (
        slice_root / name,
        slice_root / "Versions" / "A" / name,
        slice_root / "Versions" / "Current" / name,
    ):
        if candidate.is_file():
            return candidate
    return None


def _bucket(result: Result) -> str:
    """Classify the run into a coarse bucket for the summary table.

    Plan-mode buckets:
    - `plan_ok`              — exit 0 + JSON parsed; the plan was emitted
    - `plan_ok_no_json`      — exit 0 but stdout didn't parse (regression)
    - `timeout`              — the dry-run wedged past the timeout
    - `diagnosed_failure`    — non-zero exit AND the diagnostics layer
                                 emitted a Diagnosis/Try block
    - `undiagnosed_failure`  — non-zero exit with no Diagnosis line

    Archive-mode buckets:
    - `archive_ok`                  — exit 0 + at least one non-empty xcframework
    - `archive_ok_broken`           — exit 0 but xcframework verification failed
                                       (the most interesting bucket — the tool's
                                       own Verify phase let a broken bundle through)
    - `archive_timeout`             — exceeded the per-candidate timeout
    - `archive_failed_diagnosed`    — non-zero exit, tool emitted a diagnosis
    - `archive_failed_undiagnosed`  — non-zero exit, no diagnosis
    """
    if result.timed_out:
        return "archive_timeout" if result.mode == "archive" else "timeout"
    if result.mode == "archive":
        if result.exit_code == 0:
            return "archive_ok_broken" if result.archive_failure else "archive_ok"
        return (
            "archive_failed_diagnosed" if result.diagnosis
            else "archive_failed_undiagnosed"
        )
    if result.exit_code == 0:
        return "plan_ok" if result.plan_envelope is not None else "plan_ok_no_json"
    return "diagnosed_failure" if result.diagnosis is not None else "undiagnosed_failure"


_PROGRESS_MARKER: Dict[str, str] = {
    "plan_ok": "OK ",
    "plan_ok_no_json": "??",
    "diagnosed_failure": "DX",
    "undiagnosed_failure": "FAIL",
    "timeout": "TO",
    "archive_ok": "OK ",
    "archive_ok_broken": "??",
    "archive_failed_diagnosed": "DX",
    "archive_failed_undiagnosed": "FAIL",
    "archive_timeout": "TO",
}


_PLAN_BUCKETS = (
    "plan_ok", "plan_ok_no_json",
    "diagnosed_failure", "undiagnosed_failure", "timeout",
)

_ARCHIVE_BUCKETS = (
    "archive_ok", "archive_ok_broken",
    "archive_failed_diagnosed", "archive_failed_undiagnosed", "archive_timeout",
)

_SECTION_TITLES: Dict[str, str] = {
    "plan_ok": "plan_ok — emitted a parseable plan",
    "plan_ok_no_json": "plan_ok_no_json — emitted exit 0 but no JSON envelope",
    "diagnosed_failure": "diagnosed_failure — failed with a known diagnostic",
    "undiagnosed_failure": "undiagnosed_failure — failed without a diagnostic",
    "timeout": "timeout",
    "archive_ok": "archive_ok — produced a non-empty xcframework",
    "archive_ok_broken": (
        "archive_ok_broken — tool exited 0 but xcframework verification failed"
    ),
    "archive_failed_diagnosed": (
        "archive_failed_diagnosed — failed with a known diagnostic"
    ),
    "archive_failed_undiagnosed": (
        "archive_failed_undiagnosed — failed without a diagnostic"
    ),
    "archive_timeout": "archive_timeout — exceeded the per-candidate timeout",
}


def _markdown_report(results: List[Result]) -> str:
    mode = results[0].mode if results else "plan"
    buckets: Dict[str, List[Result]] = {}
    for r in results:
        buckets.setdefault(_bucket(r), []).append(r)

    total = len(results)
    counts = {k: len(v) for k, v in buckets.items()}

    if mode == "archive":
        bucket_order = _ARCHIVE_BUCKETS
        ok_count = counts.get("archive_ok", 0)
        headline_line = (
            f"**Archive success: {ok_count}/{total} = "
            f"{(ok_count / total * 100):.1f}%** "
            f"(tool exit 0 AND a non-empty xcframework was produced)"
            if total else "**No candidates run.**"
        )
    else:
        bucket_order = _PLAN_BUCKETS
        ok_count = counts.get("plan_ok", 0)
        headline_line = (
            f"**Plan-phase success: {ok_count}/{total} = "
            f"{(ok_count / total * 100):.1f}%** "
            f"(`--dry-run-json` exit 0, parseable JSON envelope)"
            if total else "**No candidates run.**"
        )

    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines: List[str] = []
    lines.append(f"# spm-to-xcframework wild-sample campaign ({mode} mode)")
    lines.append("")
    lines.append(f"Run at: `{now}`")
    lines.append(f"Candidates: **{total}**")
    lines.append(f"Tool: `{TOOL}`")
    lines.append("")
    lines.append("## Headline")
    lines.append("")
    lines.append(headline_line)
    lines.append("")
    lines.append("## Bucket counts")
    lines.append("")
    lines.append("| Bucket | Count |")
    lines.append("|---|---|")
    for bucket in bucket_order:
        lines.append(f"| {bucket} | {counts.get(bucket, 0)} |")
    lines.append("")

    ok_buckets = {"plan_ok", "archive_ok"}

    def _section(bucket: str) -> None:
        rows = buckets.get(bucket, [])
        if not rows:
            return
        lines.append(f"## {_SECTION_TITLES[bucket]} ({len(rows)})")
        lines.append("")
        for r in rows:
            lines.append(f"### {r.name} @ {r.version}")
            lines.append("")
            lines.append(f"- URL: {r.url}")
            lines.append(
                f"- Exit: `{r.exit_code}`, duration: {r.duration_sec:.1f}s"
            )
            if r.plan_envelope is not None:
                bu_count = len(r.plan_envelope.get("plan", {}).get("build_units", []))
                edit_count = len(
                    r.plan_envelope.get("plan", {}).get("package_swift_edits", [])
                )
                tp_count = len(r.plan_envelope.get("transitive_packages", []))
                lines.append(
                    f"- Plan: {bu_count} build unit(s), "
                    f"{edit_count} package edit(s), "
                    f"{tp_count} transitive package(s)"
                )
            if r.xcframeworks:
                lines.append(
                    f"- Output: {len(r.xcframeworks)} xcframework(s), "
                    f"{_fmt_bytes(r.xcframework_bytes)} total — "
                    f"{', '.join(r.xcframeworks)}"
                )
            if r.archive_failure:
                lines.append(f"- Archive verify failure: {r.archive_failure}")
            if r.diagnosis:
                lines.append("")
                lines.append("```")
                lines.append(r.diagnosis)
                lines.append("```")
            if r.stderr_tail and bucket not in ok_buckets:
                lines.append("")
                lines.append("<details><summary>stderr tail</summary>")
                lines.append("")
                lines.append("```")
                lines.append(r.stderr_tail)
                lines.append("```")
                lines.append("</details>")
            lines.append("")

    for bucket in bucket_order:
        _section(bucket)

    return "\n".join(lines) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Wild-sample stress test for spm-to-xcframework "
                    "(plan or archive mode)"
    )
    parser.add_argument(
        "--archive", action="store_true",
        help="Run a full xcodebuild archive against each candidate instead "
             "of --dry-run-json. Slow (minutes per package) — designed for "
             "overnight runs. Default per-candidate timeout: "
             f"{ARCHIVE_TIMEOUT_SEC}s; default jobs: 1.",
    )
    parser.add_argument(
        "--jobs", type=int, default=None,
        help="Parallel invocations. Default: 4 in plan mode, 1 in archive "
             "mode (xcodebuild already parallelizes inside each build).",
    )
    parser.add_argument(
        "--timeout", type=int, default=None,
        help=f"Per-candidate timeout in seconds. Default: "
             f"{DRY_RUN_TIMEOUT_SEC} in plan mode, "
             f"{ARCHIVE_TIMEOUT_SEC} in archive mode.",
    )
    parser.add_argument("--only", action="append", default=None,
                        help="Run only the named candidate(s). Repeatable "
                             "(--only foo --only bar) for a small subset; "
                             "omit to run the full campaign.")
    parser.add_argument("--report", type=Path, default=None,
                        help="Write markdown report to this path "
                             "(default: print to stdout)")
    parser.add_argument("--json-report", type=Path, default=None,
                        help="Also write a machine-readable JSON copy of "
                             "the results to this path")
    args = parser.parse_args(argv)

    if not TOOL.is_file():
        print(f"ERROR: tool not found at {TOOL}; "
              f"run `python3 src/build_single_file.py` first.",
              file=sys.stderr)
        return 2

    mode = "archive" if args.archive else "plan"
    timeout_sec = args.timeout if args.timeout is not None else (
        ARCHIVE_TIMEOUT_SEC if mode == "archive" else DRY_RUN_TIMEOUT_SEC
    )
    jobs = args.jobs if args.jobs is not None else (1 if mode == "archive" else 4)
    if jobs < 1:
        print(f"ERROR: --jobs must be >= 1 (got {jobs})", file=sys.stderr)
        return 2

    candidates = CANDIDATES
    if args.only:
        wanted = set(args.only)
        candidates = [c for c in CANDIDATES if c.name in wanted]
        missing = wanted - {c.name for c in candidates}
        if missing:
            print(
                f"ERROR: no candidate(s) named {sorted(missing)!r}",
                file=sys.stderr,
            )
            return 2

    print(
        f"Running {len(candidates)} candidate(s) in {mode} mode "
        f"(jobs={jobs}, timeout={timeout_sec}s)",
        file=sys.stderr,
    )

    results: List[Result] = []

    def _record(c: Candidate, r: Result) -> None:
        results.append(r)
        marker = _PROGRESS_MARKER.get(_bucket(r), "?")
        extra = ""
        if r.mode == "archive" and r.xcframeworks:
            extra = (
                f"  [{len(r.xcframeworks)} xcfw, "
                f"{_fmt_bytes(r.xcframework_bytes)}]"
            )
        print(
            f"  {marker:>4} {c.name:<32} {r.duration_sec:6.1f}s  "
            f"exit={r.exit_code}{extra}",
            file=sys.stderr,
            flush=True,
        )

    if mode == "archive":
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            futures = {
                ex.submit(run_archive, c, timeout_sec): c
                for c in candidates
            }
            for fut in as_completed(futures):
                _record(futures[fut], fut.result())
    else:
        with tempfile.TemporaryDirectory(prefix="spm2xc-wild-sample-") as tmp:
            tmp_root = Path(tmp)
            with ThreadPoolExecutor(max_workers=jobs) as ex:
                futures = {
                    ex.submit(run_plan, c, tmp_root / c.name, timeout_sec): c
                    for c in candidates
                }
                for fut in as_completed(futures):
                    _record(futures[fut], fut.result())

    # Preserve candidate ordering in the report for stable diffs.
    by_name = {r.name: r for r in results}
    ordered = [by_name[c.name] for c in candidates if c.name in by_name]
    report = _markdown_report(ordered)

    if args.report:
        args.report.write_text(report)
        print(f"\nReport written to {args.report}", file=sys.stderr)
    else:
        print(report)

    if args.json_report:
        payload = {
            "tool": str(TOOL),
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
            "mode": mode,
            "timeout_sec": timeout_sec,
            "jobs": jobs,
            "results": [
                {
                    "name": r.name,
                    "url": r.url,
                    "version": r.version,
                    "mode": r.mode,
                    "exit_code": r.exit_code,
                    "duration_sec": r.duration_sec,
                    "timed_out": r.timed_out,
                    "bucket": _bucket(r),
                    "diagnosis": r.diagnosis,
                    "plan_envelope": r.plan_envelope,
                    "stderr_tail": r.stderr_tail,
                    "xcframeworks": r.xcframeworks,
                    "xcframework_bytes": r.xcframework_bytes,
                    "archive_failure": r.archive_failure,
                }
                for r in ordered
            ],
        }
        args.json_report.write_text(json.dumps(payload, indent=2))
        print(f"JSON report written to {args.json_report}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
