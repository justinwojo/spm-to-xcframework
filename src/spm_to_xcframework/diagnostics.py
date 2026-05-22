"""Pattern-based diagnoses for opaque downstream-tool failures.

When `xcodebuild archive` or `swift package` fails, the raw stderr /
xcresult tells the user WHAT broke but rarely WHAT TO DO. This module
maps stable substrings in those failure messages to a labelled
diagnosis plus a suggested fix.

Two entry points:

  * `scan(error_text)` — classify a formatted xcodebuild-archive failure
    (xcresult `errors[]` + log tail concatenated) against a small list of
    known patterns. Returns a `Diagnosis` on first match, else None. Used
    by `_format_execute_error` to prepend a "Diagnosis: ... / Try: ..."
    block ABOVE the raw error list — actionable signal before evidence.

  * `format_swift_package_failure(cmd, stderr)` — shape a `swift package
    dump-package` / `swift package describe` failure: surface the first
    `error:` line up top so the proximate cause is visible without
    scrolling, then append a tools-version hint when stderr matches the
    canonical SPM mismatch shape.

Design notes:

  * Pattern entries are flat tuples (substring, headline, suggestion).
    No class hierarchy or capture-group extraction today — the five
    initial patterns don't need it. Promote to a dataclass with custom
    extractors once a pattern actually requires it.

  * Matches are case-insensitive substring against short, stable tokens.
    Apple-side rewords of full diagnostic sentences won't silently break
    the match. Each entry carries the Xcode/Swift version it was
    verified against; update when new shapes appear.

  * Misses are silent — unmatched failures fall through to the caller's
    existing rendering. Never gate correctness on a match; this is UX
    surface only.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass(frozen=True)
class Diagnosis:
    """One labelled translation of an opaque tool failure.

    `headline` is the short "this is what broke" line. `suggestion` is
    the "this is what to try" line. `pattern` is the substring that
    fired the match — kept on the result for testing and so the caller
    can log which entry triggered without re-running the scan.
    """
    headline: str
    suggestion: str
    pattern: str


# (substring, headline, suggestion). Order matters — the first hit wins.
# Substrings are kept short and rooted in tokens Apple has held stable
# across the Xcode 15..26 range. Last verified: 2026-05-22 against
# Xcode 26.3 / Swift 6.2.4. When adding entries: include the failure
# shape you observed, the suggestion you'd give a user, and the
# toolchain the pattern was verified against.
_PATTERNS: List[Tuple[str, str, str]] = [
    (
        "Unable to find module dependency: 'SwiftSyntax'",
        "Swift macro plugin dependency missing at archive time",
        "spm-to-xcframework pre-builds `.macro(...)` plugins host-side before "
        "xcodebuild archive runs (see prepare._build_macro_plugin). If this "
        "fires, the pre-build silently failed — re-run with -v to inspect the "
        "`swift build --product <macro>` output.",
    ),
    (
        # The Swift library-evolution switch-exhaustiveness diagnostic.
        # Already drives `execute/run_unit.py`'s dedup-overlap fallback;
        # if the user sees this in surfaced output, the fallback didn't
        # fire and the user can disable dedup globally.
        "may have additional unknown values",
        "Library-evolution resilience boundary in dedup-overlap rewrite",
        "spm-to-xcframework normally auto-recovers from this via the "
        "dedup-overlap fallback in execute/run_unit.py. If it didn't, pass "
        "--no-dedup-overlap to skip dedup globally (trade-off: sibling targets "
        "will static-embed into every umbrella that imports them, so consumers "
        "linking both this xcframework and the sibling xcframework will see "
        "duplicate symbols).",
    ),
    (
        # Xcode 26.3 / swift-collections 1.5.x interaction. NOT a tool
        # bug — vanilla `xcodebuild archive` against the unmodified
        # checkout reproduces. Documented in the swift-async-algorithms
        # and swift-composable-architecture matrix entries.
        "'@_lifetime' attribute is only valid when experimental feature Lifetimes is enabled",
        "Xcode 26.3 + swift-collections 1.5.x library-evolution bug",
        "This reproduces with a vanilla `xcodebuild archive` against an "
        "unmodified checkout — an Apple/SPM bug, not a spm-to-xcframework bug. "
        "Pin the consuming package to swift-collections < 1.5.0 (e.g. 1.4.2), "
        "or wait for Xcode 26.4 / swift-collections 1.6.",
    ),
    (
        # Generic linker diagnostic — can be a missing sibling target
        # (the common SPM case), an SDK/toolchain mismatch, vendored
        # auto-link metadata, or a stale `-l` flag. Wording stays soft
        # so we don't push users down the --target path when the cause
        # is something else.
        "Could not find or use auto-linked library",
        "Linker can't resolve a referenced library",
        "If the missing library is another package target/product, add it "
        "via --target <name> or --include-deps (run with --inspect-only to "
        "list declared targets). If it's a system framework or vendored "
        "binary, the package's linker settings or the binary's auto-link "
        "metadata is the place to look.",
    ),
    (
        # Most often post-edit damage from Prepare/rewrite passes, but
        # could also be a pre-existing cycle in the upstream manifest
        # (rare but real). Wording stays neutral so a user with a real
        # upstream cycle isn't pointed at a bug in us. Pointing at the
        # staged manifest is the load-bearing piece — the diff against
        # .original-Package.swift makes the cause obvious either way.
        "Cycle in dependencies",
        "Dependency cycle in the staged Package.swift",
        "Diff the staged manifest against the pre-edit backup to see what "
        "changed: <work-dir>/staged/Package.swift vs "
        "<work-dir>/staged/.original-Package.swift (re-run with -v if the "
        "staged dir is gone). If the cycle is in the backup too, the "
        "upstream package ships with it; otherwise a Prepare-phase edit "
        "introduced it — file a bug with both manifests attached.",
    ),
    (
        "was built with a different version of Swift",
        "Vendored .binaryTarget built with an incompatible Swift toolchain",
        "A `.binaryTarget` xcframework in the package was produced by a "
        "different swiftc than your current toolchain. Update the vendored "
        "binary in the upstream package, or switch to a matching Xcode.",
    ),
    (
        # Generic Xcode wording — fires for any non-zero shell-script
        # build phase. In SPM-generated archives this is *usually* a
        # `.plugin(...)` build-tool, but it can also be a user-added
        # Run Script phase or a codesign/notarization helper. Suggestion
        # leads with the most common cause but keeps the door open.
        "Command PhaseScriptExecution failed",
        "A shell-script build phase failed during archive",
        "This is most often an SPM `.plugin(...)` build-tool returning "
        "non-zero — re-run with -v to see the script output. Some build-tool "
        "plugins also assume developer-mode SDKROOT or codesigning that "
        "`xcodebuild archive` doesn't grant. If the package has no plugin, "
        "look for a custom Run Script phase in the package or its deps.",
    ),
]


def scan(error_text: str) -> Optional[Diagnosis]:
    """Classify `error_text` against the known-pattern table.

    Returns the first matching `Diagnosis` (by table order), or None if
    nothing matched. Matching is case-insensitive substring. Pure
    function — no I/O, no module-level state, safe to call from any
    thread / under the parallel slice scheduler.

    The expectation is that callers concatenate every available error
    source (xcresult-parsed errors, log tail) and pass the joined text
    in — patterns can match against either source.
    """
    if not error_text:
        return None
    haystack = error_text.lower()
    for pattern, headline, suggestion in _PATTERNS:
        if pattern.lower() in haystack:
            return Diagnosis(
                headline=headline,
                suggestion=suggestion,
                pattern=pattern,
            )
    return None


def format_block(diagnosis: Diagnosis) -> str:
    """Render the diagnostic as the two-line block we prepend to error
    output. Stable shape so tests can pin it and downstream tooling can
    grep for the `Diagnosis:` prefix.
    """
    return f"Diagnosis: {diagnosis.headline}\nTry: {diagnosis.suggestion}"


# --------------------------------------------------------------------------
# swift package failure shaping

# Canonical SPM tools-version-mismatch text. Stable since SPM 5.x:
# "package at '<path>' is using Swift tools version 5.5.0 but the minimum
# required by the toolchain is 5.7.0"
_TOOLS_VERSION_MISMATCH = re.compile(
    r"package(?: at '[^']+')? is using Swift tools version "
    r"(\d+\.\d+(?:\.\d+)?) but the minimum required by the toolchain is "
    r"(\d+\.\d+(?:\.\d+)?)",
    re.IGNORECASE,
)

# Opposite direction — manifest pinned to a Swift tools version newer
# than the installed Xcode supports. Canonical wording: "package at
# '<path>' is using Swift tools version 6.0.0 but the installed version
# is 5.10.0" (or the older "supports up to" phrasing). The hint here
# tells the user to upgrade Xcode rather than bump the manifest down.
_TOOLS_VERSION_TOO_NEW = re.compile(
    r"is using Swift tools version (\d+\.\d+(?:\.\d+)?) but the "
    r"(?:installed version is|installed Swift toolchain supports up to) "
    r"(\d+\.\d+(?:\.\d+)?)",
    re.IGNORECASE,
)

# Older/alternate SPM wording: "package requires minimum Swift tools
# version X.Y but found Z.A". Kept as a fallback so we still pin a
# tools-version mismatch even if Apple flips the phrasing.
_TOOLS_VERSION_REQUIRES = re.compile(
    r"requires (?:minimum )?Swift tools version (\d+\.\d+(?:\.\d+)?)",
    re.IGNORECASE,
)

# Manifest declares an executable product whose backing target SPM can't
# treat as executable — typically a `.binaryTarget(url:, checksum:)`
# artifact-bundle exposed as `.executable(name: ...)`. swift-protobuf
# 1.32.0's `protoc` product is the canonical case. Captured via regex
# (not the substring-matched `_PATTERNS` table) so we can surface the
# offending product name in the hint.
_EXECUTABLE_PRODUCT_BAD_BACKING = re.compile(
    r"executable product '([^']+)' expects target '([^']+)' to be "
    r"executable; an executable target requires a 'main\.swift' file",
    re.IGNORECASE,
)


def _first_error_line(stderr: str) -> Optional[str]:
    """Return the first stderr line containing `error:` (case-insensitive),
    stripped of leading/trailing whitespace. None if no such line exists.

    `error:` is the SPM/swiftc/clang convention for the proximate
    diagnostic; surfacing it verbatim gives the user the same line the
    compiler considered most important without making them scroll past
    setup chatter.
    """
    for raw in stderr.splitlines():
        stripped = raw.strip()
        if "error:" in stripped.lower():
            return stripped
    return None


def format_swift_package_failure(cmd: str, stderr: str) -> str:
    """Shape a `swift package <cmd>` subprocess failure into an actionable
    InspectError body.

    Output shape (lines joined with \\n):

        <cmd> failed.
        First error: <first 'error:' line, verbatim>            (when present)
        Try: <hint>                                              (when matched)
        ---                                                      (separator)
        <last 15 lines of raw stderr>                            (always)

    The first-error line + the tools-version hint together cover the
    overwhelmingly common case (manifest written against a newer
    swift-tools-version than the user's Xcode supports). Unrecognised
    failures still surface the stderr tail so the user has the same
    information they had before, just better-organised.
    """
    stderr = (stderr or "").rstrip()
    lines: List[str] = [f"{cmd} failed."]

    first = _first_error_line(stderr)
    if first:
        lines.append(f"First error: {first}")

    hint: Optional[str] = None
    m = _TOOLS_VERSION_MISMATCH.search(stderr)
    if m:
        used, required = m.group(1), m.group(2)
        hint = (
            f"Try: this package's manifest declares "
            f"`// swift-tools-version:{used}`, but your toolchain requires "
            f"at least {required}. Bump the manifest's swift-tools-version "
            f"line to {required} (or newer), or switch to an older Xcode "
            f"that supports {used}."
        )
    else:
        m2 = _TOOLS_VERSION_TOO_NEW.search(stderr)
        if m2:
            used, installed = m2.group(1), m2.group(2)
            hint = (
                f"Try: this package's manifest declares "
                f"`// swift-tools-version:{used}`, but your installed Xcode "
                f"only supports up to {installed}. Upgrade Xcode to a version "
                f"that ships Swift {used} or newer, or pin the package to a "
                f"release whose manifest still targets <={installed}."
            )
        elif _TOOLS_VERSION_REQUIRES.search(stderr):
            hint = (
                "Try: this looks like a swift-tools-version mismatch — check "
                "the package's `// swift-tools-version:` line against your "
                "installed Xcode toolchain."
            )
        else:
            m3 = _EXECUTABLE_PRODUCT_BAD_BACKING.search(stderr)
            if m3:
                product = m3.group(1)
                hint = (
                    f"Try: the package declares an executable product "
                    f"'{product}' whose backing target SPM doesn't accept as "
                    f"executable (most often a `.binaryTarget(url:, checksum:)` "
                    f"artifact bundle exposed via `.executable(name: ...)`). "
                    f"spm-to-xcframework is library-focused — xcframeworks "
                    f"can't ship executable products. Check whether the "
                    f"package gates that product behind an env-var (e.g. "
                    f"swift-protobuf's `PROTOBUF_NO_PROTOC=true` disables "
                    f"its `protoc` executable product); otherwise use "
                    f"--product to select a library product, or skip this "
                    f"package."
                )
    if hint:
        lines.append(hint)

    if stderr:
        # 50 lines balances "show enough manifest-parse context" against
        # "don't drown the user". Labelled `stderr tail` so the truncation
        # is honest. Inspect-phase swift-package failures are usually
        # under 50 lines anyway — this cap only trims pathological output.
        tail = stderr.splitlines()[-50:]
        lines.append("--- stderr tail (last 50 lines) ---")
        lines.extend(tail)
    else:
        lines.append("(no stderr)")

    return "\n".join(lines)
