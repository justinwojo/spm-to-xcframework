#!/usr/bin/env python3
"""Wild-sample stress-test campaign for spm-to-xcframework.

Runs `spm-to-xcframework --dry-run-json` against a curated list of SPM
packages NOT already in `packages.toml`, captures (exit code, stderr
diagnosis if any, plan JSON if successful) for each, and emits a
categorised markdown report.

This harness exists to give the project's "95%+ of arbitrary SPM
packages produce a valid xcframework on the first try" lower bound a
real data point. Plan-phase success is *necessary but not sufficient*
for archive success (xcodebuild can still fail later), so a successful
sample here is later promoted to a full archive run only when its
exercise paths aren't already covered by the matrix.

Usage:
    python3 tests/integration/wild_sample.py
    python3 tests/integration/wild_sample.py --jobs 8 --report /tmp/wild.md
    python3 tests/integration/wild_sample.py --only swift-log

Each candidate gets a 90-second timeout — Fetch+Inspect+Plan should be
well under a minute even for large packages; anything longer is a
regression worth investigating on its own.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
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
    exit_code: int
    duration_sec: float
    timed_out: bool
    plan_envelope: Optional[dict]  # parsed JSON from stdout when exit==0
    stderr_tail: str               # last ~30 lines of stderr (always recorded)
    diagnosis: Optional[str]       # extracted "Diagnosis: ... / Try: ..." block if present


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


def run_one(cand: Candidate, work_dir: Path) -> Result:
    """Run --dry-run-json against one candidate; never raises."""
    work_dir.mkdir(parents=True, exist_ok=True)
    cmd = [str(TOOL), cand.url, "-v", cand.version, "--dry-run-json", *cand.args]
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
            timeout=DRY_RUN_TIMEOUT_SEC,
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
    if exit_code == 0 and stdout.strip():
        try:
            envelope = json.loads(stdout)
        except json.JSONDecodeError:
            envelope = None

    diagnosis = _extract_diagnosis(stderr) if exit_code != 0 else None
    return Result(
        name=cand.name,
        url=cand.url,
        version=cand.version,
        exit_code=exit_code,
        duration_sec=duration,
        timed_out=timed_out,
        plan_envelope=envelope,
        stderr_tail=_stderr_tail(stderr),
        diagnosis=diagnosis,
    )


def _bucket(result: Result) -> str:
    """Classify the run into a coarse bucket for the summary table.

    - `plan_ok`              — exit 0 + JSON parsed; the plan was emitted
    - `plan_ok_no_json`      — exit 0 but stdout didn't parse (regression)
    - `timeout`              — the dry-run wedged past the timeout
    - `diagnosed_failure`    — non-zero exit AND the diagnostics layer
                                 emitted a Diagnosis/Try block — the tool
                                 already knows what's wrong
    - `undiagnosed_failure`  — non-zero exit with no Diagnosis line; the
                                 most interesting bucket for follow-on
                                 _PATTERNS or code fixes
    """
    if result.timed_out:
        return "timeout"
    if result.exit_code == 0:
        return "plan_ok" if result.plan_envelope is not None else "plan_ok_no_json"
    return "diagnosed_failure" if result.diagnosis is not None else "undiagnosed_failure"


def _markdown_report(results: List[Result]) -> str:
    buckets: Dict[str, List[Result]] = {}
    for r in results:
        buckets.setdefault(_bucket(r), []).append(r)

    total = len(results)
    counts = {k: len(v) for k, v in buckets.items()}
    plan_ok = counts.get("plan_ok", 0)

    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines: List[str] = []
    lines.append(f"# spm-to-xcframework wild-sample campaign")
    lines.append("")
    lines.append(f"Run at: `{now}`")
    lines.append(f"Candidates: **{total}**")
    lines.append(f"Tool: `{TOOL}`")
    lines.append("")
    lines.append("## Headline")
    lines.append("")
    lines.append(f"**Plan-phase success: {plan_ok}/{total} = "
                 f"{(plan_ok / total * 100):.1f}%** "
                 f"(`--dry-run-json` exit 0, parseable JSON envelope)")
    lines.append("")
    lines.append("## Bucket counts")
    lines.append("")
    lines.append("| Bucket | Count |")
    lines.append("|---|---|")
    for bucket in ("plan_ok", "plan_ok_no_json", "diagnosed_failure",
                   "undiagnosed_failure", "timeout"):
        lines.append(f"| {bucket} | {counts.get(bucket, 0)} |")
    lines.append("")

    def _section(title: str, bucket: str) -> None:
        rows = buckets.get(bucket, [])
        if not rows:
            return
        lines.append(f"## {title} ({len(rows)})")
        lines.append("")
        for r in rows:
            lines.append(f"### {r.name} @ {r.version}")
            lines.append("")
            lines.append(f"- URL: {r.url}")
            lines.append(f"- Exit: `{r.exit_code}`, "
                         f"duration: {r.duration_sec:.1f}s")
            if r.plan_envelope is not None:
                bu_count = len(r.plan_envelope.get("plan", {}).get("build_units", []))
                edit_count = len(r.plan_envelope.get("plan", {}).get("package_swift_edits", []))
                tp_count = len(r.plan_envelope.get("transitive_packages", []))
                lines.append(f"- Plan: {bu_count} build unit(s), "
                             f"{edit_count} package edit(s), "
                             f"{tp_count} transitive package(s)")
            if r.diagnosis:
                lines.append("")
                lines.append("```")
                lines.append(r.diagnosis)
                lines.append("```")
            if r.stderr_tail and bucket != "plan_ok":
                lines.append("")
                lines.append("<details><summary>stderr tail</summary>")
                lines.append("")
                lines.append("```")
                lines.append(r.stderr_tail)
                lines.append("```")
                lines.append("</details>")
            lines.append("")

    _section("plan_ok — emitted a parseable plan", "plan_ok")
    _section("plan_ok_no_json — emitted exit 0 but no JSON envelope", "plan_ok_no_json")
    _section("diagnosed_failure — failed with a known diagnostic", "diagnosed_failure")
    _section("undiagnosed_failure — failed without a diagnostic", "undiagnosed_failure")
    _section("timeout", "timeout")

    return "\n".join(lines) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Wild-sample stress test for spm-to-xcframework --dry-run-json"
    )
    parser.add_argument("--jobs", type=int, default=4,
                        help="Parallel --dry-run-json invocations (default: 4)")
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

    print(f"Running {len(candidates)} candidate(s) at jobs={args.jobs}, "
          f"timeout={DRY_RUN_TIMEOUT_SEC}s",
          file=sys.stderr)

    results: List[Result] = []
    with tempfile.TemporaryDirectory(prefix="spm2xc-wild-sample-") as tmp:
        tmp_root = Path(tmp)
        with ThreadPoolExecutor(max_workers=args.jobs) as ex:
            futures = {
                ex.submit(run_one, c, tmp_root / c.name): c
                for c in candidates
            }
            for fut in as_completed(futures):
                c = futures[fut]
                r = fut.result()
                results.append(r)
                bucket = _bucket(r)
                marker = {
                    "plan_ok": "OK ",
                    "plan_ok_no_json": "??",
                    "diagnosed_failure": "DX",
                    "undiagnosed_failure": "FAIL",
                    "timeout": "TO",
                }.get(bucket, "?")
                print(f"  {marker:>4} {c.name:<32} {r.duration_sec:5.1f}s  "
                      f"exit={r.exit_code}",
                      file=sys.stderr)

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
            "results": [
                {
                    "name": r.name,
                    "url": r.url,
                    "version": r.version,
                    "exit_code": r.exit_code,
                    "duration_sec": r.duration_sec,
                    "timed_out": r.timed_out,
                    "bucket": _bucket(r),
                    "diagnosis": r.diagnosis,
                    "plan_envelope": r.plan_envelope,
                    "stderr_tail": r.stderr_tail,
                }
                for r in ordered
            ],
        }
        args.json_report.write_text(json.dumps(payload, indent=2))
        print(f"JSON report written to {args.json_report}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
