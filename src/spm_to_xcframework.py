#!/usr/bin/env python3
"""spm-to-xcframework — Build xcframeworks from Swift Package Manager packages.

The pipeline runs in six explicit phases (REWRITE_DESIGN.md §5):

    Fetch  →  Inspect  →  Plan  →  Prepare  →  Execute  →  Verify

Each phase has a single contract. Fetch produces a clean staged copy of the
source (`.git`, `.build`, `.xcodeproj`/`.xcworkspace` siblings, etc. pruned).
Inspect parses `swift package dump-package` once into a typed model. Plan
turns the model + user filters into a list of build units and a whitelist
of `Package.swift` edits. Prepare applies those edits and round-trips the
manifest through `dump-package` again to confirm every requested edit took
effect. Execute runs xcodebuild for each unit (device + simulator slices in
parallel) and merges the slices into xcframeworks. Verify is strict and
per-unit: every output must parse, ship ≥ 2 slices of dynamically-linked
Mach-O, and (for Swift/ObjC/Mixed) carry the relevant ABI surface.

Single-file, stdlib-only, Python 3.9+. No `match` statements, no `tomllib`,
no PEP 604 unions in runtime code (type hints use `from __future__ import
annotations` so they remain strings).
"""

# --- Imports (stdlib only) ---
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, NoReturn, Optional, Sequence, Set, Tuple

# ============================================================================
# --- Logging + color output ---
# ============================================================================

# ANSI color codes. Disabled when NO_COLOR is set or stdout is not a TTY.
_ANSI = {
    "red": "\033[0;31m",
    "green": "\033[0;32m",
    "yellow": "\033[0;33m",
    "cyan": "\033[0;36m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "reset": "\033[0m",
}


def _color_enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def _wrap(text: str, color: str) -> str:
    if not _color_enabled():
        return text
    return f"{_ANSI[color]}{text}{_ANSI['reset']}"


def info(msg: str) -> None:
    print(_wrap(msg, "cyan"))


def success(msg: str) -> None:
    print(_wrap(msg, "green"))


def warn(msg: str) -> None:
    print(_wrap(msg, "yellow"), file=sys.stderr)


def dim(msg: str) -> None:
    print(_wrap(msg, "dim"))


def bold(msg: str) -> None:
    print(_wrap(msg, "bold"))


def die(msg: str) -> NoReturn:
    """Print an error message and exit with status 1.

    Reserved for top-level usage / argument errors. Phase code should raise
    a typed error from the hierarchy below; main() catches and translates.
    """
    print(_wrap(f"Error: {msg}", "red"), file=sys.stderr)
    sys.exit(1)


# Verbose logger — gated on Config.verbose at call sites.
def verbose_log(verbose: bool, msg: str) -> None:
    if verbose:
        print(_wrap(msg, "dim"))


# ============================================================================
# --- Errors ---
# ============================================================================


class SpmToXcframeworkError(Exception):
    """Base class for all phase errors. main() catches these and exits with
    a phase-specific exit code; everything else is treated as a tool bug
    and traceback'd."""

    exit_code: int = 2


class FetchError(SpmToXcframeworkError):
    """Could not clone, copy, or stage the package source. User-facing:
    network failure, missing tag, missing local path, copy permission
    error."""

    exit_code = 3


class InspectError(SpmToXcframeworkError):
    """Could not parse Package.swift or interrogate the staged package.
    User-facing: unsupported swift-tools-version, malformed manifest,
    missing required toolchain output."""

    exit_code = 4


class PlanError(SpmToXcframeworkError):
    """Plan phase invariants violated. User-facing: filter matched
    nothing, --target on a binary target, --target in binary mode."""

    exit_code = 5


class PrepareError(SpmToXcframeworkError):
    """Base class for Prepare-phase failures. Split into two subclasses:
    `PrepareUserError` for clean-message user mistakes (unsupported
    manifest construct, product filter pointing at nothing, etc.) and
    `PrepareBug` for genuine invariant violations that should surface
    with a Python traceback. `PrepareError` itself is never raised —
    pick the right subclass."""

    exit_code = 6


class PrepareUserError(PrepareError):
    """The user's manifest or invocation put Prepare in a state it
    can't handle cleanly — unsupported Swift constructs, missing
    `products:` array, walker couldn't find balanced parens in a
    malformed manifest, etc. Surfaces as a one-line clean error."""


class PrepareBug(PrepareError):
    """A real invariant violation inside Prepare — round-trip validator
    caught edit drift, the planner asked to edit a product Prepare
    materialized into nothing, post-edit dump-package parse failure,
    etc. Surfaces with a traceback."""


class ExecuteError(SpmToXcframeworkError):
    """xcodebuild (or downstream tooling) failed. Surfaced with the
    parsed xcresult plus the top N errors per target."""

    exit_code = 7


class VerifyError(SpmToXcframeworkError):
    """Base class for Verify-phase exceptions. Split into two subclasses:
    `VerifyUserError` for clean-message user mistakes (output directory
    missing because `-o` was mistyped) and `VerifyBug` for invariant
    violations inside verify code itself. `VerifyError` itself is never
    raised — pick the right subclass. The base is kept so callers that
    need `VerifyError.exit_code` (the aggregate return code for
    `_finalize_with_verify`) keep working unchanged."""

    exit_code = 8


class VerifyUserError(VerifyError):
    """The user's invocation put Verify in a state it can't work in —
    output directory missing/not-a-directory is the canonical case.
    Surfaces as a one-line clean error."""


class VerifyBug(VerifyError):
    """A real invariant violation inside Verify (e.g. the strict check
    itself crashed on a shape it should have handled). Surfaces with a
    traceback."""


# ============================================================================
# --- Config (dataclass from argparse) ---
# ============================================================================


@dataclass
class Config:
    """Parsed CLI arguments. The single source of truth for user intent.

    Two version fields, by design (bug 1 in SPM_TO_XCFRAMEWORK_NOTES.md):
      - user_version: exactly what the user typed. Fed to SPM `exact:`.
      - resolved_version: tag rewritten via normalize_version_tag if the
        user said "1.2.3" but the repo only has "v1.2.3". Used for git
        operations.
    """

    package_source: str
    user_version: str = ""
    resolved_version: str = ""
    output_dir: Path = field(default_factory=lambda: Path("./xcframeworks"))
    product_filters: List[str] = field(default_factory=list)
    target_filters: List[str] = field(default_factory=list)
    revision: Optional[str] = None
    min_ios: str = "15.0"
    include_deps: bool = False
    binary_mode: bool = False
    verbose: bool = False
    dry_run: bool = False
    keep_work: bool = False
    inspect_only: bool = False
    # When False (default), Finalize cleans up stale xcframeworks from
    # prior runs recorded in `.spm-to-xcframework-manifest.json`. When
    # True, cleanup is skipped for this run AND the surviving old
    # entries are merged into the new manifest so they remain tracked
    # — a subsequent normal run will clean them. See REFACTOR_PLAN.md
    # Task 3 for the "delay cleanup by one run" semantics.
    no_cleanup_stale: bool = False
    work_dir: Optional[Path] = None  # set in main() before fetch/inspect run

    @property
    def is_remote(self) -> bool:
        s = self.package_source
        return (
            s.startswith("http://")
            or s.startswith("https://")
            or s.startswith("git@")
            or s.startswith("ssh://")
        )


# ============================================================================
# --- Model: Package, Product, Target, Platform, Linkage, TargetKind ---
# ============================================================================
#
# Closed enums implemented as string constants on a class — Python 3.9
# `enum.Enum` works, but classes-of-strings keep JSON dumping trivial and
# avoid Enum's `.value` ceremony at every call site. Membership is
# enforced via the `_VALUES` tuple at parse time.


class Linkage:
    AUTOMATIC = "automatic"
    DYNAMIC = "dynamic"
    STATIC = "static"
    EXECUTABLE = "executable"  # not a library product, but parsed for completeness
    PLUGIN = "plugin"
    SNIPPET = "snippet"
    UNKNOWN = "unknown"  # the dump-package payload was a shape we don't recognize

    _VALUES = (AUTOMATIC, DYNAMIC, STATIC, EXECUTABLE, PLUGIN, SNIPPET, UNKNOWN)


class TargetKind:
    REGULAR = "regular"
    TEST = "test"
    SYSTEM = "system"
    BINARY = "binary"
    PLUGIN = "plugin"
    MACRO = "macro"
    EXECUTABLE = "executable"
    UNKNOWN = "unknown"

    _VALUES = (REGULAR, TEST, SYSTEM, BINARY, PLUGIN, MACRO, EXECUTABLE, UNKNOWN)


class Language:
    """Per-target source-language classification, derived by walking the
    target's source tree under WORK_DIR/staged."""

    SWIFT = "Swift"
    OBJC = "ObjC"
    MIXED = "Mixed"
    NA = "N/A"  # system / binary / plugin / unknown — nothing to scan

    _VALUES = (SWIFT, OBJC, MIXED, NA)


@dataclass
class Platform:
    name: str            # "ios", "macos", "tvos", ...
    version: str         # "15.0"


@dataclass
class Product:
    name: str
    linkage: str         # one of Linkage._VALUES
    targets: List[str]   # backing target names


@dataclass
class Target:
    name: str
    kind: str            # one of TargetKind._VALUES
    path: Optional[str]  # relative to package root; None means SPM default
    public_headers_path: Optional[str]
    dependencies: List[str]
    exclude: List[str]
    language: str = Language.NA  # filled in by scan_target_languages()
    source_file_count: int = 0   # for diagnostics; counts files actually classified


@dataclass
class Package:
    """Typed snapshot of `swift package dump-package` for the staged
    package. Read-only after Inspect; the planner consumes it."""

    name: str
    tools_version: str
    platforms: List[Platform]
    products: List[Product]
    targets: List[Target]
    schemes: List[str]              # from xcodebuild -list -json against staged
    raw_dump: dict                  # untouched dump-package JSON, for debugging
    staged_dir: Path

    def target_by_name(self, name: str) -> Optional[Target]:
        for t in self.targets:
            if t.name == name:
                return t
        return None


# ============================================================================
# --- Model: Plan, BuildUnit, StageSpec, PackageSwiftEdit ---
# ============================================================================
#
# These are stubs for Session 2; defined here so Session 1's code already
# imports the right type names and the file structure stays stable.


@dataclass
class StageSpec:
    """Inclusion-by-default + exclusion-list staging rules. The actual
    list of toxic top-level entries lives in TOXIC_TOP_LEVEL below; this
    type exists so the planner can override per-package if it ever needs
    to. (Not used in session 1, but referenced by REWRITE_DESIGN.md §5.2.)"""

    exclude_globs: List[str] = field(default_factory=list)


@dataclass
class PackageSwiftEdit:
    """Whitelisted Package.swift edit, produced by Plan and consumed by
    Prepare. Two kinds:

      - "force_dynamic"           — rewrite an existing library product's
                                    linkage to `.dynamic`. `product_name`
                                    is the product to edit; `targets` is
                                    informational (the product's backing
                                    targets at planning time).
      - "add_synthetic_library"   — inject a brand-new `.library(name: T,
                                    type: .dynamic, targets: [T])` entry.
                                    `product_name == T`, `targets == [T]`.
                                    The --target escape hatch from §5.2.
    """

    kind: str  # "force_dynamic" | "add_synthetic_library"
    product_name: str
    targets: List[str] = field(default_factory=list)


@dataclass
class BuildUnit:
    """One unit of work the executor will run.

    `archive_strategy` discriminates the execute path:
      - "archive"         — run `xcodebuild archive` against the scheme.
      - "static-promote"  — xcodebuild produced a .a; clang -dynamiclib it.
                            Currently unused at plan time (Execute decides).
      - "copy-artifact"   — binary mode: the xcframework already exists on
                            disk at `artifact_path`; Execute just copies it.
    """

    name: str
    scheme: str
    framework_name: str
    language: str
    archive_strategy: str
    source_targets: List[str] = field(default_factory=list)
    # True iff this unit exists because of `--target T` — i.e. the plan
    # injects a synthetic `.library()` entry for it. Affects dry-run labels
    # and Verify's per-unit error messages.
    synthetic: bool = False
    # Populated only for archive_strategy == "copy-artifact". Absolute path
    # to the xcframework discovered under `.build/artifacts/` by Fetch.
    artifact_path: Optional[Path] = None


@dataclass
class BinaryArtifact:
    """A pre-built xcframework discovered via binary-mode SPM resolve.

    The planner takes a list of these (from `discover_binary_artifacts`)
    and filters by --product. Each surviving record becomes a build unit
    whose execute strategy is "copy-artifact".
    """

    product_name: str  # the xcframework name without the .xcframework suffix
    path: Path         # absolute path to the .xcframework directory


@dataclass
class Plan:
    """Output of the planner: a typed description of what the downstream
    phases will do. Pure data — no side effects, no filesystem handles
    beyond what inspect already gave us."""

    stage: StageSpec = field(default_factory=StageSpec)
    package_swift_edits: List[PackageSwiftEdit] = field(default_factory=list)
    build_units: List[BuildUnit] = field(default_factory=list)
    # Products the planner dropped, with a human-readable reason. Printed
    # by --dry-run and surfaced in the final run summary.
    skipped: List[Tuple[str, str]] = field(default_factory=list)
    # Planner diagnostics that aren't errors — surfaced to stderr by the
    # caller after planning completes. Kept as data so the planner stays
    # a pure function (§5.2 contract).
    warnings: List[str] = field(default_factory=list)
    # --include-deps flag forwarded to Execute. The planner can't enumerate
    # transitive deps ahead of time (they only exist after xcodebuild runs),
    # so this is just a gate.
    include_deps: bool = False
    # True for binary-mode plans. Execute uses this to skip xcodebuild
    # entirely. The planner is the only thing that sets it.
    binary_mode: bool = False


@dataclass
class PreparedPlan:
    """Output of Prepare. Wraps the original Plan together with the typed
    Package model that resulted from re-parsing the edited active manifest.

    Existence of a PreparedPlan is a contract: the active manifest under
    `package.staged_dir` (`Package.swift` or whichever
    `Package@swift-X.Y.swift` SPM picks for the toolchain) parses cleanly
    through `swift package dump-package` and every planner-requested edit
    appears in the dumped output the way the planner expected. Execute can
    rely on that invariant — it never re-validates the manifest itself.
    """

    plan: Plan
    package: Package


@dataclass
class ArchiveSlice:
    """One (build unit, slice) pair's outputs from xcodebuild archive.

    A "slice" is one of the two architectures we feed -create-xcframework
    (device arm64 and the iOS simulator fat archive). Both slices are
    archived in parallel by Session 4's `_archive_pair_parallel` and the
    located framework / static lib paths are recorded here so Execute's
    static-promote and injection passes don't need to re-walk the archive.
    """

    arch_suffix: str              # "arm64" (device) or "simulator"
    sdk_name: str                 # "iphoneos" or "iphonesimulator"
    archive_path: Path            # .xcarchive directory
    dd_path: Path                 # derived data path
    log_path: Path                # build log
    result_bundle_path: Path      # xcresult bundle
    framework_path: Optional[Path] = None
    static_lib_path: Optional[Path] = None


@dataclass
class DependencyXcframework:
    """One dependency xcframework built alongside a primary unit via
    `--include-deps`.

    Pairs the on-disk path with the expected language classification
    (derived from the matching target in the Package model, if one
    exists) so that `_finalize_with_verify` can thread the same
    plan-time expected-language signal into Verify for deps that the
    primary build units already get. Without this, dependency artifacts
    would fall back to post-hoc detection and the mixed-language
    silent-pass hole would still apply to them.
    """

    path: Path
    expected_language: str = ""  # Language._VALUES; "" / "N/A" → post-hoc fallback


@dataclass
class ExecutedUnit:
    """Output of Execute for a single build unit.

    Holds both archive slices (device + simulator) and the final
    xcframework path the merge step produced. The optional fields are
    populated only when the unit's archive_strategy is "archive". For
    binary-mode units (`copy-artifact`) the slices are empty and only
    `xcframework_path` is set, pointing at the copied artifact in
    `<output_dir>/`.
    """

    name: str                     # build unit name (matches plan.build_units[i].name)
    device: Optional[ArchiveSlice] = None
    simulator: Optional[ArchiveSlice] = None
    xcframework_path: Optional[Path] = None
    framework_name: Optional[str] = None  # the resolved <fw_name>, may differ from unit.name
    framework_type: str = ""              # "Swift" / "ObjC" / "Mixed" / "Unknown" — post-hoc detection from disk, for summary printing
    # Language the planner said this unit was supposed to produce. One of
    # Language._VALUES ("Swift" / "ObjC" / "Mixed" / "N/A"). Carried through
    # from `BuildUnit.language` so Verify can gate its language-specific
    # fatal checks on what the plan intended, not on what survived in the
    # partially-built artifact. Empty string for legacy callers; Verify
    # treats empty / "N/A" as "fall back to post-hoc detection".
    expected_language: str = ""
    is_binary_copy: bool = False
    dependency_xcframeworks: List[DependencyXcframework] = field(default_factory=list)


@dataclass
class VerifyResult:
    """One xcframework's strict-verify outcome (REWRITE_DESIGN.md §5.5).

    Verify is per-unit. A unit either `passed` (every fatal check cleared)
    or it didn't (one or more entries in `fatal_issues`). `warnings` are
    advisory and never cause `passed=False`. `framework_type` is reported
    for the summary printer's `[Swift|ObjC|Mixed|Unknown]` label.

    Verify never raises for "the user's xcframework is broken" — it
    records the failure here and lets `main()` translate the aggregate
    into an exit code. The only thing that surfaces as a `VerifyError`
    exception is verify code itself crashing (e.g. unreadable output
    directory) — see REWRITE_DESIGN.md §7.
    """

    unit_name: str
    framework_name: str
    xcframework_path: Path
    framework_type: str            # "Swift" | "ObjC" | "Mixed" | "Unknown"
    size_bytes: int
    passed: bool
    fatal_issues: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


# ============================================================================
# --- Phase 0: Fetch (clone/copy + stage) ---
# ============================================================================
#
# Fetch's contract is documented in REWRITE_DESIGN.md §5.0. The crucial
# rule is: every downstream phase reads from WORK_DIR/staged, never from
# WORK_DIR/source. Source is kept around solely for --keep-work debugging.

# Top-level (and any-depth) artifact directories that are unsafe to leave
# in the staged tree. Two reasons:
#   - .git, .build, DerivedData, node_modules: huge and not needed for SPM build
#   - *.xcodeproj, *.xcworkspace: cause `xcodebuild -list` and
#     `xcodebuild archive` to refuse to pick a project (bug 4 in
#     SPM_TO_XCFRAMEWORK_NOTES.md). The design always builds against
#     SPM-auto-generated schemes, so we never want these in staged.
TOXIC_NAMES = {
    ".git",
    ".build",
    "DerivedData",
    "node_modules",
}
TOXIC_SUFFIXES = (".xcodeproj", ".xcworkspace")


def _is_toxic_entry(name: str) -> bool:
    if name in TOXIC_NAMES:
        return True
    for sfx in TOXIC_SUFFIXES:
        if name.endswith(sfx):
            return True
    return False


def _git(args: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
    """Thin wrapper around git so the call sites stay readable."""
    return subprocess.run(
        ["git", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **kwargs,
    )


# Allowed remote URL prefixes. Local filesystem paths are accepted
# separately (they must resolve to an existing directory).
_REMOTE_URL_PREFIXES = ("http://", "https://", "git@", "ssh://")

# Permissive but bounded tag pattern: letters, digits, dots, hyphens,
# underscores, plus signs, and slashes (for `refs/heads/...`-style tags
# that sometimes leak through). Length-capped so a pathological input
# can't OOM the tool. Real-world tags in the wild almost always fit.
_TAG_PATTERN = re.compile(r"[A-Za-z0-9._+/\-]+")
_MAX_TAG_LENGTH = 200


def _validate_package_source(source: str) -> None:
    """Argument-injection hardening for `config.package_source`.

    Raises `FetchError` for shapes that could be misinterpreted as a
    command-line option (leading `-`) or inject through shell / git
    metacharacters (newline, carriage return, null byte).

    Accepts either:
      - a supported remote URL scheme (`http://`, `https://`, `git@`,
        `ssh://`), or
      - a local filesystem path that resolves to an existing directory.

    Called from `main()` so the error surfaces at argument-parse time,
    not after Execute has already done work. Remote / local detection
    here stays consistent with `Config.is_remote`.
    """
    if not source:
        raise FetchError("package source is empty")
    if any(ch in source for ch in ("\n", "\r", "\x00")):
        raise FetchError(
            "package source contains newline, carriage return, or null byte"
        )
    if source.startswith("-"):
        raise FetchError(
            f"package source {source!r} starts with `-` and would be "
            "misinterpreted as a command-line option"
        )
    if source.startswith(_REMOTE_URL_PREFIXES):
        return
    # Local path: must resolve to an existing directory.
    try:
        local = Path(source).expanduser()
    except (OSError, ValueError) as exc:
        raise FetchError(f"package source {source!r} is not a valid path: {exc}") from exc
    if not local.is_dir():
        raise FetchError(
            f"package source {source!r} is neither a supported remote URL "
            f"(http://, https://, git@, ssh://) nor an existing local directory"
        )


def _validate_git_ref(ref: str, *, field: str) -> None:
    """Reject git refs that would be misparsed as CLI options or that
    contain shell / git metacharacters. Used for `--version`,
    `--revision` resolution output, and any other user-supplied ref.

    `field` is a short label ("version", "tag", "revision") used in the
    error message so the user can tell which input tripped the check.
    """
    if not ref:
        return
    if any(ch in ref for ch in ("\n", "\r", "\x00")):
        raise FetchError(
            f"{field} contains newline, carriage return, or null byte"
        )
    if ref.startswith("-"):
        raise FetchError(
            f"{field} {ref!r} starts with `-` and would be "
            "misinterpreted as a command-line option"
        )
    if len(ref) > _MAX_TAG_LENGTH:
        raise FetchError(
            f"{field} {ref!r} is longer than {_MAX_TAG_LENGTH} characters"
        )
    if not _TAG_PATTERN.fullmatch(ref):
        raise FetchError(
            f"{field} {ref!r} contains characters outside the allowed "
            f"tag set (letters, digits, `._+/-`)"
        )


def normalize_version_tag(url: str, version: str) -> Tuple[str, bool]:
    """Resolve a user-supplied version string to an actual tag on the remote.

    Returns (resolved_tag, was_rewritten). The caller stores both the
    user-supplied value (for SPM `exact:`) and the resolved tag (for git
    operations). This is the bug-1 fix from SPM_TO_XCFRAMEWORK_NOTES.md.

    If neither `<version>` nor `v<version>` matches, returns the original
    version unchanged so downstream `git clone` produces a clear error.

    Note: we require an *exact* `refs/tags/<name>` match in the parsed
    `git ls-remote --tags` output. Earlier drafts trusted any non-empty
    output, but git's pattern matching can return sibling refs (e.g.
    asking for `1.2` matches `1.2.0`), which would silently leak the
    wrong version downstream.
    """
    if not version:
        return version, False

    if _exact_tag_exists(url, version):
        return version, False

    v_prefixed = f"v{version}"
    if _exact_tag_exists(url, v_prefixed):
        return v_prefixed, True

    return version, False


def _exact_tag_exists(url: str, tag: str) -> bool:
    """True iff `git ls-remote --tags <url>` reports an exact `refs/tags/<tag>`
    or `refs/tags/<tag>^{}` ref. Strict equality — no glob/prefix matching.
    """
    # `--` separates git's options from positional URL/ref arguments so
    # a future `url` or `tag` beginning with `-` can't be parsed as a flag.
    cp = _git(["ls-remote", "--tags", "--", url, f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"])
    if cp.returncode != 0:
        return False
    wanted = {f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"}
    for line in cp.stdout.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) == 2 and parts[1] in wanted:
            return True
    return False


def verify_revision(url: str, tag: str, expected_sha: str) -> None:
    """Verify a git tag resolves to the expected commit SHA *before* cloning.

    Annotated tags need ^{}-dereference to compare to a commit SHA, so we
    query both refs and prefer the dereferenced one.
    """
    if not re.fullmatch(r"[0-9a-f]{40}", expected_sha):
        raise FetchError(
            f"--revision requires a full 40-character SHA (got: {expected_sha})"
        )

    info(f"Verifying tag '{tag}' resolves to {expected_sha}...")
    # See `_exact_tag_exists` for the `--` rationale.
    cp = _git(["ls-remote", "--tags", "--", url, f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"])
    if cp.returncode != 0:
        raise FetchError(f"Failed to query remote tags from {url}: {cp.stderr.strip()}")

    lines = [ln for ln in cp.stdout.splitlines() if ln.strip()]
    if not lines:
        raise FetchError(f"Tag '{tag}' not found in {url}")

    deref_line = next((ln for ln in lines if ln.endswith("^{}")), None)
    sha_source = deref_line if deref_line else lines[0]
    actual_sha = sha_source.split()[0]

    if actual_sha != expected_sha:
        raise FetchError(
            f"Revision mismatch for tag '{tag}':\n"
            f"  Expected: {expected_sha}\n"
            f"  Actual:   {actual_sha}\n"
            f"The tag may have been force-pushed. Aborting for safety."
        )

    success("  Revision verified ✓")


def fetch_source(config: Config) -> Path:
    """Clone or copy the package into WORK_DIR/source.

    Returns the source directory. Does NOT stage — that's stage_source().
    """
    assert config.work_dir is not None
    source_dir = config.work_dir / "source"

    if config.is_remote:
        if not config.user_version:
            raise FetchError("Version (--version) is required for remote package URLs.")

        if config.revision:
            verify_revision(
                config.package_source, config.resolved_version, config.revision
            )

        info(f"Cloning {config.package_source} @ {config.resolved_version}")
        # `--` separates git-clone's options from the positional
        # <repo>/<dir> pair so a user-supplied URL or directory beginning
        # with `-` can't be parsed as a flag. The remaining defence is
        # the argparse-time validation in `_validate_package_source`
        # and `_validate_git_ref` — both hit before we ever reach the
        # clone call.
        cp = _git(
            [
                "clone",
                "--depth",
                "1",
                "--branch",
                config.resolved_version,
                "--",
                config.package_source,
                str(source_dir),
            ]
        )
        if cp.returncode != 0:
            raise FetchError(
                f"Failed to clone {config.package_source} at version "
                f"{config.resolved_version}\n{cp.stderr.strip()}"
            )
    else:
        # Local path. Resolve through Path so trailing slashes / `.` work.
        local_path = Path(config.package_source).expanduser().resolve()
        if not local_path.is_dir():
            raise FetchError(f"Local path not found: {config.package_source}")
        if not (local_path / "Package.swift").is_file():
            raise FetchError(f"No Package.swift found at {local_path}")

        info(f"Copying local package from {local_path}")
        # shutil.copytree refuses if dest exists; source_dir is freshly created
        # by main() so this is fine.
        shutil.copytree(local_path, source_dir, symlinks=True)

    if not (source_dir / "Package.swift").is_file():
        raise FetchError("No Package.swift found in package source")

    return source_dir


def _copy_excluding_toxic(src: Path, dst: Path) -> None:
    """Copy `src` → `dst`, dropping TOXIC_NAMES / TOXIC_SUFFIXES at every
    level. Implemented as a manual recursive walk so we can prune at the
    *directory* level (not just at the leaf), which matters for
    `.git/`-style trees with millions of objects.
    """
    dst.mkdir(parents=True, exist_ok=True)
    for entry in os.scandir(src):
        if _is_toxic_entry(entry.name):
            continue
        src_path = Path(entry.path)
        dst_path = dst / entry.name
        if entry.is_symlink():
            # Preserve symlinks verbatim. SPM packages occasionally use them
            # for vendored sources; following would copy the wrong tree.
            link_target = os.readlink(src_path)
            os.symlink(link_target, dst_path)
        elif entry.is_dir(follow_symlinks=False):
            _copy_excluding_toxic(src_path, dst_path)
        else:
            shutil.copy2(src_path, dst_path, follow_symlinks=False)


def stage_source(config: Config, source_dir: Path) -> Path:
    """Stage `source_dir` into WORK_DIR/staged.

    Two-pass per REWRITE_DESIGN.md §5.0:
      1. Copy everything except TOXIC_NAMES / TOXIC_SUFFIXES.
      2. Run `swift package dump-package` on the staged copy to learn the
         package's own `exclude:` paths, then delete those from staged.

    The second pass is best-effort: if dump-package can't parse yet (e.g.
    the package needs a newer swift toolchain), the inspect phase will
    surface that error directly. Staging only does the first pass and
    leaves the per-target excludes alone.
    """
    assert config.work_dir is not None
    staged_dir = config.work_dir / "staged"
    if staged_dir.exists():
        shutil.rmtree(staged_dir)

    info("Staging package into clean working tree...")
    _copy_excluding_toxic(source_dir, staged_dir)

    # Sanity: at minimum the staged dir must contain Package.swift, otherwise
    # the inclusion-by-default rule has already gone wrong.
    if not (staged_dir / "Package.swift").is_file():
        raise FetchError(
            "Stage produced no Package.swift — staging exclusion list dropped "
            "the manifest. This is a bug in spm-to-xcframework."
        )

    # Pass 2: drop the package's own `exclude:` paths. We do this best-effort
    # — failure to dump-package here is the inspect phase's job to surface,
    # not ours. The exclude pass is purely a hygiene measure (it stops
    # excluded files from leaking into the staged tree where they could
    # confuse later phases that walk the source tree).
    try:
        dump = _swift_dump_package(staged_dir)
    except InspectError:
        # Can't enumerate excludes — the next phase will fail loudly. Leave
        # the staged tree as-is.
        return staged_dir

    removed_count = 0
    for tgt in dump.get("targets", []):
        target_path_str = tgt.get("path") or _default_target_path(
            tgt.get("name", ""), tgt.get("type", "")
        )
        if not target_path_str:
            continue
        for ex in tgt.get("exclude", []) or []:
            ex_rel = Path(target_path_str) / ex
            # Lexical containment check: refuse anything that escapes
            # the staged tree at the path level (`..` traversal, absolute
            # path) BEFORE touching the filesystem. This is symlink-safe:
            # we never call `.resolve()` because the staged tree may
            # contain vendored symlinks pointing outside, and resolving
            # through them could let an exclude path escape the tree.
            try:
                # Path.is_absolute() catches `/etc/passwd`-style escapes;
                # the parts check catches `..` traversal.
                if ex_rel.is_absolute() or ".." in ex_rel.parts:
                    continue
            except (TypeError, ValueError):
                continue
            ex_path = staged_dir / ex_rel
            if ex_path.is_symlink():
                # Drop the link, not its target.
                ex_path.unlink()
                removed_count += 1
                continue
            if ex_path.is_dir():
                shutil.rmtree(ex_path)
                removed_count += 1
            elif ex_path.exists():
                ex_path.unlink()
                removed_count += 1

    # SPM resolve. Per design §5.0 step 5: pre-resolve dependencies on the
    # staged tree so downstream xcodebuild calls don't need network or
    # racy parallel resolution. Failure here is user-facing — the package
    # likely declares a dependency we can't fetch.
    info("  Resolving package dependencies...")
    cp = subprocess.run(
        ["swift", "package", "resolve"],
        cwd=str(staged_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if cp.returncode != 0:
        # Surface the last 5 lines of stderr — same shape as the bash tool.
        tail = "\n".join((cp.stderr or "").rstrip().splitlines()[-5:])
        raise FetchError(
            "swift package resolve failed on the staged package:\n" + (tail or "  (no stderr)")
        )

    if removed_count and config.verbose:
        verbose_log(config.verbose, f"  Removed {removed_count} excluded path(s) from staged tree")

    return staged_dir


def discover_binary_artifacts(config: Config) -> List[BinaryArtifact]:
    """Resolve a binary-mode SPM shim and walk `.build/artifacts/` for
    xcframeworks. Used by binary mode instead of fetch_source + stage.

    Side effects: writes a shim Package.swift under
    `WORK_DIR/binary-resolve/`, runs `swift package resolve`, walks the
    resulting `.build/artifacts/` tree. The planner that consumes the
    returned list is still a pure function.

    Two structural fixes live here (bugs 1 and 3 in
    SPM_TO_XCFRAMEWORK_NOTES.md):

      1. The shim's `exact:` string is `config.user_version` with a
         leading "v" stripped. That's the bare semver SPM's resolver
         requires, regardless of whether the user typed "7.6.2" or
         "v7.6.2".
      2. `os.walk` prunes any `__MACOSX` directories before descending,
         so AppleDouble ghost xcframeworks never make it into the
         returned list.

    Session 2 only needs this for `--dry-run`. Session 4 will call it
    from the real Execute path; the function is intentionally shaped
    so the return value feeds straight into `plan_binary_build`.
    """
    assert config.work_dir is not None
    if not config.is_remote:
        raise FetchError("--binary requires a remote package URL.")
    if not config.user_version:
        raise FetchError("--binary requires --version.")

    shim_dir = config.work_dir / "binary-resolve"
    shim_dir.mkdir(parents=True, exist_ok=True)

    # Bug 1 fix: feed the bare semver, not the git-tag form. Strip a
    # leading "v" so `--version v7.6.2` also resolves cleanly.
    exact_version = config.user_version[1:] if config.user_version.startswith("v") else config.user_version

    # Minimum iOS for the shim. SPM resolve doesn't actually care about
    # the version, but the manifest needs to be self-consistent. Use the
    # major version from config.min_ios.
    try:
        major = int(config.min_ios.split(".")[0])
    except (ValueError, IndexError):
        major = 15

    manifest = (
        "// swift-tools-version:5.7\n"
        "import PackageDescription\n"
        "\n"
        "let package = Package(\n"
        '    name: "binary-resolver",\n'
        f"    platforms: [.iOS(.v{major})],\n"
        "    dependencies: [\n"
        f'        .package(url: "{config.package_source}", exact: "{exact_version}"),\n'
        "    ],\n"
        "    targets: [\n"
        '        .target(name: "Dummy", path: "Sources"),\n'
        "    ]\n"
        ")\n"
    )
    (shim_dir / "Package.swift").write_text(manifest)
    sources_dir = shim_dir / "Sources"
    sources_dir.mkdir(parents=True, exist_ok=True)
    (sources_dir / "Dummy.swift").write_text("// placeholder\n")

    info(f"Resolving binary artifacts from {config.package_source} @ {exact_version}...")
    cp = subprocess.run(
        ["swift", "package", "resolve"],
        cwd=str(shim_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if cp.returncode != 0:
        tail = "\n".join((cp.stderr or "").rstrip().splitlines()[-10:])
        raise FetchError(
            "Failed to resolve binary package dependencies:\n"
            + (tail or "  (no stderr)")
        )

    artifacts_root = shim_dir / ".build" / "artifacts"
    if not artifacts_root.is_dir():
        raise FetchError(
            "No .build/artifacts directory after resolve — this package "
            "may not ship binary xcframeworks."
        )

    found: List[BinaryArtifact] = []
    for dirpath, dirnames, _filenames in os.walk(artifacts_root):
        # Bug 3 fix: prune __MACOSX ghosts before descending or matching.
        dirnames[:] = [d for d in dirnames if d != "__MACOSX"]
        # Collect .xcframework directories at the current level without
        # descending into them (they have their own internal structure
        # that we don't want to walk).
        remaining: List[str] = []
        for d in dirnames:
            if d.endswith(".xcframework"):
                name = d[: -len(".xcframework")]
                found.append(BinaryArtifact(product_name=name, path=Path(dirpath) / d))
            else:
                remaining.append(d)
        dirnames[:] = remaining

    if not found:
        raise FetchError(
            "No xcframeworks discovered under .build/artifacts/. "
            "Is this actually a binary-target SPM package?"
        )

    return found


def _default_target_path(target_name: str, target_kind: str) -> Optional[str]:
    """SPM's default path for a target when `path:` isn't set.

    Only returns a path for kinds that actually have a buildable source
    tree under `Sources/` or `Tests/`. System / binary / plugin / macro
    targets either point at host headers, prebuilt artifacts, or compiler
    plugins, so they have no default source path and we return None.

    For regular and executable targets it's `Sources/<name>`; for tests
    it's `Tests/<name>`. We don't try to resolve the case where SPM picks
    an alternate `Source/<name>` (Alamofire) — those targets always
    declare `path:` explicitly, so dump-package returns the value verbatim.
    """
    if not target_name:
        return None
    if target_kind == TargetKind.TEST:
        return f"Tests/{target_name}"
    if target_kind in (TargetKind.REGULAR, TargetKind.EXECUTABLE):
        return f"Sources/{target_name}"
    return None


# ============================================================================
# --- Phase 1: Inspect ---
# ============================================================================


def _swift_dump_package(staged_dir: Path) -> dict:
    """Run `swift package dump-package` against the staged dir and return
    the parsed JSON. Raises InspectError on any failure.
    """
    cp = subprocess.run(
        ["swift", "package", "dump-package"],
        cwd=str(staged_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if cp.returncode != 0:
        raise InspectError(
            "swift package dump-package failed:\n"
            + (cp.stderr or "  (no stderr)").rstrip()
            + "\nIs the swift-tools-version supported by your toolchain?"
        )
    try:
        return json.loads(cp.stdout)
    except json.JSONDecodeError as exc:
        raise InspectError(f"Could not parse dump-package JSON: {exc}") from exc


def _parse_tools_version(raw: object) -> str:
    """`toolsVersion` is a struct: {'_version': '6.1.0'}. Be lenient about
    older shapes that might have been a flat string."""
    if isinstance(raw, dict):
        v = raw.get("_version")
        if isinstance(v, str):
            return v
    if isinstance(raw, str):
        return raw
    return "unknown"


def _parse_platforms(raw: object) -> List[Platform]:
    out: List[Platform] = []
    if not isinstance(raw, list):
        return out
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        out.append(
            Platform(
                name=str(entry.get("platformName", "")),
                version=str(entry.get("version", "")),
            )
        )
    return out


def _parse_linkage(type_field: object) -> Optional[str]:
    """SPM products have several shapes:
        {'library': ['automatic']}
        {'library': ['dynamic']}
        {'library': ['static']}
        {'executable': None}
        {'plugin': None}
        {'snippet': None}
    Returns the linkage as one of Linkage._VALUES, or None for non-library
    products (which we deliberately drop from the model since the tool only
    builds libraries).
    """
    if isinstance(type_field, dict):
        if "library" in type_field:
            inner = type_field["library"]
            if isinstance(inner, list) and inner:
                v = str(inner[0])
                if v in Linkage._VALUES:
                    return v
                return Linkage.UNKNOWN
            return Linkage.AUTOMATIC
        # Non-library product types — explicitly excluded from the model.
        if "executable" in type_field:
            return None
        if "plugin" in type_field:
            return None
        if "snippet" in type_field:
            return None
    # Older / unknown shape — log via UNKNOWN so the planner can decide.
    return Linkage.UNKNOWN


def _parse_target_kind(raw: object) -> str:
    if not isinstance(raw, str):
        return TargetKind.UNKNOWN
    return raw if raw in TargetKind._VALUES else TargetKind.UNKNOWN


def _parse_dependencies(raw: object) -> List[str]:
    """Targets list dependencies in several union shapes. We only need names
    here (the planner doesn't currently care about kind), so flatten and
    return string names. Unknown shapes are silently dropped."""
    out: List[str] = []
    if not isinstance(raw, list):
        return out
    for d in raw:
        if not isinstance(d, dict):
            continue
        # Common shapes:
        #   {"byName": ["X", null]}
        #   {"target": ["X", null]}
        #   {"product": ["X", "PkgName", null, null]}
        for key in ("byName", "target", "product"):
            v = d.get(key)
            if isinstance(v, list) and v and isinstance(v[0], str):
                out.append(v[0])
                break
    return out


def _raw_internal_dep_names(raw_target: dict) -> List[str]:
    """Return the first-level INTERNAL target-name deps of a raw dump-package
    target entry. "Internal" means same-package targets — i.e. the
    `byName` and `target` shapes, NOT the `product` shape (which refers
    to another package entirely and whose targets we can't reach).

    Used by dependency-walking code that needs to scan raw dump entries
    directly (e.g. `detect_system_frameworks`, `_find_objc_headers_dir`)
    rather than the typed `Target.dependencies` list. Those callers need
    the raw entries for other fields (settings, publicHeadersPath, …),
    so they can't just reuse `_parse_dependencies` on the typed model.

    Order-preserving; duplicates are kept in source order because both
    callers iterate and de-dupe with their own `scanned` sets.
    """
    out: List[str] = []
    for dep in raw_target.get("dependencies", []) or []:
        if not isinstance(dep, dict):
            continue
        for key in ("byName", "target"):
            v = dep.get(key)
            if isinstance(v, list) and v and isinstance(v[0], str):
                out.append(v[0])
                break
    return out


def dump_package(staged_dir: Path) -> Tuple[dict, List[Product], List[Target], List[Platform], str, str]:
    """Run dump-package and parse it into typed shards. Returns
    (raw_dump, products, targets, platforms, name, tools_version).

    Splitting this from build_package() lets the self-test fixtures feed
    in pre-canned dump payloads without invoking the swift toolchain.
    """
    raw = _swift_dump_package(staged_dir)
    return _parse_dump(raw)


def _parse_dump(raw: dict) -> Tuple[dict, List[Product], List[Target], List[Platform], str, str]:
    name = str(raw.get("name") or "")
    tools_version = _parse_tools_version(raw.get("toolsVersion"))
    platforms = _parse_platforms(raw.get("platforms"))

    products: List[Product] = []
    for prod in raw.get("products", []) or []:
        if not isinstance(prod, dict):
            continue
        linkage = _parse_linkage(prod.get("type"))
        if linkage is None:
            # Non-library product — skip entirely. Plan only ever builds libraries.
            continue
        targets = [str(t) for t in (prod.get("targets") or []) if isinstance(t, str)]
        products.append(Product(name=str(prod.get("name") or ""), linkage=linkage, targets=targets))

    targets: List[Target] = []
    for t in raw.get("targets", []) or []:
        if not isinstance(t, dict):
            continue
        targets.append(
            Target(
                name=str(t.get("name") or ""),
                kind=_parse_target_kind(t.get("type")),
                path=t.get("path") if isinstance(t.get("path"), str) else None,
                public_headers_path=(
                    t.get("publicHeadersPath")
                    if isinstance(t.get("publicHeadersPath"), str)
                    else None
                ),
                dependencies=_parse_dependencies(t.get("dependencies")),
                exclude=[str(e) for e in (t.get("exclude") or []) if isinstance(e, str)],
            )
        )

    return raw, products, targets, platforms, name, tools_version


# Source-tree directories we never count when classifying a target's
# language — these are vendored examples or test scaffolding and would
# confuse the count.
_LANG_SCAN_SKIP_DIRS = {
    "Tests",
    "Test",
    "Demo",
    "Demos",
    "Example",
    "Examples",
    "Sample",
    "Samples",
    "Playground",
    "Playgrounds",
}


def scan_target_languages(staged_dir: Path, targets: Iterable[Target]) -> None:
    """Walk each target's source tree and label its language.

    Mutates the Target instances in place. Targets with kind SYSTEM /
    BINARY / PLUGIN / MACRO are left at Language.NA — there's nothing to
    scan and any of these would never be a build unit anyway.
    """
    for tgt in targets:
        if tgt.kind in (TargetKind.SYSTEM, TargetKind.BINARY, TargetKind.PLUGIN, TargetKind.MACRO):
            tgt.language = Language.NA
            continue
        if tgt.kind == TargetKind.TEST:
            tgt.language = Language.NA
            continue

        rel_path = tgt.path or _default_target_path(tgt.name, tgt.kind)
        if not rel_path:
            tgt.language = Language.NA
            continue

        target_root = staged_dir / rel_path
        if not target_root.is_dir():
            # Target path doesn't exist on disk. The package will fail later
            # in xcodebuild, so we don't raise here — just label N/A and
            # continue. Inspect is a read-only fact-finding pass.
            tgt.language = Language.NA
            continue

        swift_count, objc_count = _count_source_files(target_root)
        tgt.source_file_count = swift_count + objc_count
        if swift_count and objc_count:
            tgt.language = Language.MIXED
        elif swift_count:
            tgt.language = Language.SWIFT
        elif objc_count:
            tgt.language = Language.OBJC
        else:
            tgt.language = Language.NA


def _count_source_files(root: Path) -> Tuple[int, int]:
    """Walk `root` and return (swift_count, objc_count). Skips
    _LANG_SCAN_SKIP_DIRS at every directory level.

    ObjC counts .m / .mm implementation files AND bare .h headers — per
    REWRITE_DESIGN.md §5.1. Including .h is necessary for header-only
    ObjC targets (umbrella frameworks, system-wrapper modules) which
    otherwise classify as N/A and confuse the planner.

    Note: when `.swift` files are also present, the target is Mixed
    rather than ObjC, so the "(without matching .swift)" qualifier in
    the design is enforced by the caller, not here.
    """
    swift = 0
    objc = 0
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune skip dirs in place so os.walk doesn't descend into them.
        dirnames[:] = [d for d in dirnames if d not in _LANG_SCAN_SKIP_DIRS]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext == ".swift":
                swift += 1
            elif ext in (".m", ".mm", ".h"):
                objc += 1
    return swift, objc


def discover_schemes(staged_dir: Path, verbose: bool = False) -> List[str]:
    """Run `xcodebuild -list -json` against the staged dir.

    Because staging strips xcodeproj/xcworkspace, this returns the SPM
    auto-generated scheme list with no multi-project ambiguity (bug 4 in
    SPM_TO_XCFRAMEWORK_NOTES.md). On any failure, returns an empty list
    and lets the planner fall back to product-name schemes — Inspect
    never raises here, since some packages legitimately have nothing
    xcodebuild can list (very old swift-tools-version, etc.).

    `verbose=True` logs the xcodebuild stderr tail so users can tell the
    difference between "no schemes" and "scheme discovery failed".
    """
    cp = subprocess.run(
        ["xcodebuild", "-list", "-json"],
        cwd=str(staged_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if cp.returncode != 0:
        if verbose:
            tail = "\n".join((cp.stderr or "").rstrip().splitlines()[-5:])
            verbose_log(verbose, f"  xcodebuild -list failed: {tail}")
        return []

    try:
        data = json.loads(cp.stdout)
    except json.JSONDecodeError as exc:
        verbose_log(verbose, f"  xcodebuild -list returned non-JSON: {exc}")
        return []

    container = data.get("workspace") or data.get("project") or {}
    schemes = container.get("schemes") or []
    return [str(s) for s in schemes if isinstance(s, str)]


def inspect_package(config: Config, staged_dir: Path) -> Package:
    """Top-level Inspect entry point. Reads only — no filesystem mutations
    on the staged tree. Wires together dump_package + scan + scheme list."""
    info("Inspecting package...")
    raw, products, targets, platforms, name, tools_version = dump_package(staged_dir)
    scan_target_languages(staged_dir, targets)
    schemes = discover_schemes(staged_dir, verbose=config.verbose)
    return Package(
        name=name,
        tools_version=tools_version,
        platforms=platforms,
        products=products,
        targets=targets,
        schemes=schemes,
        raw_dump=raw,
        staged_dir=staged_dir,
    )


def print_package(pkg: Package) -> None:
    """Human-readable Package summary, used by --inspect-only."""
    bold(f"\n=== {pkg.name} ===")
    print(f"  tools-version: {pkg.tools_version}")
    if pkg.platforms:
        plats = ", ".join(f"{p.name} {p.version}" for p in pkg.platforms)
        print(f"  platforms:     {plats}")
    print(f"  staged dir:    {pkg.staged_dir}")
    print(f"  schemes:       {', '.join(pkg.schemes) if pkg.schemes else '(none discovered)'}")

    bold(f"\nProducts ({len(pkg.products)}):")
    if not pkg.products:
        print("  (none)")
    for p in pkg.products:
        # Cross-reference each product's backing targets to flag system /
        # already-dynamic shapes the planner will care about.
        kinds = []
        for tname in p.targets:
            t = pkg.target_by_name(tname)
            kinds.append(t.kind if t else "?")
        notes = []
        if p.linkage == Linkage.DYNAMIC:
            notes.append("already dynamic")
        if all(k == TargetKind.SYSTEM for k in kinds) and kinds:
            notes.append("system-only — will be skipped")
        note_s = f"  [{', '.join(notes)}]" if notes else ""
        print(
            f"  - {p.name}  linkage={p.linkage}  targets={p.targets}{note_s}"
        )

    bold(f"\nTargets ({len(pkg.targets)}):")
    for t in pkg.targets:
        path_disp = t.path or "(default)"
        hdr = f" headers={t.public_headers_path}" if t.public_headers_path else ""
        print(
            f"  - {t.name}  kind={t.kind}  language={t.language}"
            f"  path={path_disp}{hdr}  files={t.source_file_count}"
        )


# ============================================================================
# --- Phase 2: Plan ---
# ============================================================================
#
# The planner is a PURE FUNCTION over (Config, Package) -> Plan. It never
# touches the filesystem, never runs subprocesses, and never mutates its
# inputs. All the decisions the rest of the tool makes flow from here, so
# keeping this layer side-effect-free is load-bearing: any test that
# exercises a planning rule can do so with a hand-rolled Package fixture
# and trust that the production behavior matches.
#
# See REWRITE_DESIGN.md §5.2 for the rules this implements.


def resolve_scheme(product_name: str, schemes: Sequence[str]) -> str:
    """Pick the best scheme for `product_name` out of the discovered list.

    Resolution order (design §5.2 rule 4, plus the session-1 inversion
    noted in the Session 2 brief: prefer the literal name over the
    `-Package` form when both exist):

      1. exact match
      2. case-insensitive exact match
      3. `<product>-Package`
      4. `<product> iOS`, `<product>-iOS`, `<product> (iOS)`
      5. fall back to `product_name` unchanged — xcodebuild will
         auto-generate the SPM scheme at build time, which works
         against our clean staged directory.
    """
    if not schemes:
        return product_name
    # 1. exact
    if product_name in schemes:
        return product_name
    # 2. case-insensitive exact
    lowered = {s.lower(): s for s in schemes}
    if product_name.lower() in lowered:
        return lowered[product_name.lower()]
    # 3. <product>-Package
    pkg_form = f"{product_name}-Package"
    if pkg_form in schemes:
        return pkg_form
    # 4. iOS-suffix variants
    for suffix in (" iOS", "-iOS", " (iOS)"):
        candidate = product_name + suffix
        if candidate in schemes:
            return candidate
    # 5. fall back to product name
    return product_name


def _derive_product_language(product: Product, package: Package) -> str:
    """Union of all backing target languages → one of Language._VALUES.

    Rules (design §5.2 item 6):
      Swift + Swift        → Swift
      ObjC  + ObjC         → ObjC
      Swift + ObjC         → Mixed
      anything + Mixed     → Mixed
      nothing classified   → N/A
    """
    has_swift = False
    has_objc = False
    has_mixed = False
    for tname in product.targets:
        t = package.target_by_name(tname)
        if t is None:
            continue
        if t.language == Language.SWIFT:
            has_swift = True
        elif t.language == Language.OBJC:
            has_objc = True
        elif t.language == Language.MIXED:
            has_mixed = True
    if has_mixed or (has_swift and has_objc):
        return Language.MIXED
    if has_swift:
        return Language.SWIFT
    if has_objc:
        return Language.OBJC
    return Language.NA


def _is_system_only_product(product: Product, package: Package) -> bool:
    """True iff every backing target of `product` is TargetKind.SYSTEM.

    This is the rule for dropping a product entirely (design §5.2 rule 2 +
    bug 5 in SPM_TO_XCFRAMEWORK_NOTES.md). An empty target list is NOT
    considered system-only — that's a malformed product, not a system
    wrapper, and we leave it to xcodebuild to complain.
    """
    if not product.targets:
        return False
    for tname in product.targets:
        t = package.target_by_name(tname)
        if t is None or t.kind != TargetKind.SYSTEM:
            return False
    return True


def plan_source_build(config: Config, package: Package) -> Plan:
    """Pure planner for source-mode builds.

    Takes the inspected Package and the user's Config and returns a Plan
    describing (a) the Package.swift edits Prepare should apply, (b) the
    list of build units Execute should run, and (c) the list of products
    the planner consciously skipped with a reason. See REWRITE_DESIGN.md
    §5.2 for the full rule set.
    """
    plan = Plan()
    plan.include_deps = config.include_deps

    product_filter: Optional[Set[str]] = (
        set(config.product_filters) if config.product_filters else None
    )
    product_filter_matches: Set[str] = set()

    # First pass: walk the package products. Three possible outcomes per
    # product: (a) filtered out by --product, (b) dropped because it's a
    # system-library wrapper, (c) promoted to a build unit.
    for product in package.products:
        if product_filter is not None and product.name not in product_filter:
            continue
        if product_filter is not None:
            product_filter_matches.add(product.name)

        if _is_system_only_product(product, package):
            plan.skipped.append((product.name, "system-library product"))
            continue

        # force_dynamic edit — only if the product isn't already dynamic.
        # This is the Alamofire/GRDB-dynamic invariant from the Session 2
        # brief: planning both GRDB and GRDB-dynamic is correct; emitting
        # a force_dynamic edit for the already-dynamic one is not.
        if product.linkage != Linkage.DYNAMIC:
            plan.package_swift_edits.append(
                PackageSwiftEdit(
                    kind="force_dynamic",
                    product_name=product.name,
                    targets=list(product.targets),
                )
            )

        language = _derive_product_language(product, package)
        scheme = resolve_scheme(product.name, package.schemes)
        plan.build_units.append(
            BuildUnit(
                name=product.name,
                scheme=scheme,
                framework_name=product.name,
                language=language,
                archive_strategy="archive",
                source_targets=list(product.targets),
                synthetic=False,
            )
        )

    # Second pass: --target escape hatch. For each requested target we
    # synthesize a fresh .library() entry unless one already exists with
    # the same name (in which case we use the existing one and warn).
    existing_product_names = {p.name for p in package.products}
    existing_planned_names = {bu.name for bu in plan.build_units}
    for target_name in config.target_filters:
        tgt = package.target_by_name(target_name)
        if tgt is None:
            raise PlanError(
                f"--target {target_name!r}: no such target in package "
                f"(available: {sorted(t.name for t in package.targets)})"
            )
        if tgt.kind in (TargetKind.SYSTEM, TargetKind.BINARY,
                        TargetKind.PLUGIN, TargetKind.MACRO):
            raise PlanError(
                f"--target {target_name!r}: target kind is {tgt.kind!r}; "
                "only regular source targets can be synthesized into a "
                ".library() product."
            )
        if tgt.kind == TargetKind.TEST:
            raise PlanError(
                f"--target {target_name!r}: test targets cannot be built "
                "as xcframeworks."
            )
        if tgt.kind == TargetKind.EXECUTABLE:
            # Reject up front so this turns into a clean PlanError instead
            # of dying later inside Prepare when the synthesized
            # .library(…, targets: [executable]) fails the round-trip
            # dump-package validation. Matches the other excluded kinds
            # above and gives a clearer error message (Codex [P2]).
            raise PlanError(
                f"--target {target_name!r}: target kind is {tgt.kind!r}; "
                "executable targets cannot be built as xcframeworks. "
                "Pass a regular source target instead."
            )

        if target_name in existing_product_names:
            plan.warnings.append(
                f"--target {target_name}: already exposed as a .library() "
                "product; using the existing product instead of synthesizing "
                "a duplicate."
            )
            # If the existing product wasn't already planned (e.g. it was
            # filtered out by --product), surface it now — the user's
            # explicit --target is a stronger signal than --product.
            if target_name not in existing_planned_names:
                existing = next(p for p in package.products if p.name == target_name)
                if _is_system_only_product(existing, package):
                    raise PlanError(
                        f"--target {target_name!r}: existing product has "
                        "only system targets and cannot be built."
                    )
                if existing.linkage != Linkage.DYNAMIC:
                    plan.package_swift_edits.append(
                        PackageSwiftEdit(
                            kind="force_dynamic",
                            product_name=existing.name,
                            targets=list(existing.targets),
                        )
                    )
                language = _derive_product_language(existing, package)
                scheme = resolve_scheme(existing.name, package.schemes)
                plan.build_units.append(
                    BuildUnit(
                        name=existing.name,
                        scheme=scheme,
                        framework_name=existing.name,
                        language=language,
                        archive_strategy="archive",
                        source_targets=list(existing.targets),
                        synthetic=False,
                    )
                )
                existing_planned_names.add(existing.name)
                if product_filter is not None:
                    product_filter_matches.add(existing.name)
            continue

        # Repeated --target T on the command line must not synthesize
        # the same .library() twice — the second pass would produce a
        # duplicate PackageSwiftEdit and a duplicate BuildUnit that
        # later phases would either trip over or silently double-build.
        # Record a warning and move on. (Codex [P2] coverage gap.)
        if target_name in existing_planned_names:
            plan.warnings.append(
                f"--target {target_name}: specified more than once; "
                "ignoring the duplicate."
            )
            continue

        # Synthesize. Package.swift edit + build unit, both tagged with
        # synthetic=True for display purposes. The build unit's language
        # comes from the target itself (there's no "union" — it's one
        # target).
        plan.package_swift_edits.append(
            PackageSwiftEdit(
                kind="add_synthetic_library",
                product_name=target_name,
                targets=[target_name],
            )
        )
        language = tgt.language if tgt.language in (
            Language.SWIFT, Language.OBJC, Language.MIXED
        ) else Language.NA
        scheme = resolve_scheme(target_name, package.schemes)
        plan.build_units.append(
            BuildUnit(
                name=target_name,
                scheme=scheme,
                framework_name=target_name,
                language=language,
                archive_strategy="archive",
                source_targets=[target_name],
                synthetic=True,
            )
        )
        existing_planned_names.add(target_name)

    # --product filter validation: if the user asked for something we
    # never saw, fail loudly. The synthetic pass may have broadened the
    # effective match set, so do this check last.
    if product_filter is not None:
        unmatched = sorted(product_filter - product_filter_matches)
        if unmatched:
            available = sorted(p.name for p in package.products)
            raise PlanError(
                f"--product filter matched no products: {unmatched}\n"
                f"Available products: {available}"
            )

    if not plan.build_units:
        raise PlanError(
            "Plan produced zero build units. Did --product filter out "
            "everything, or does this package declare only non-library "
            "products?"
        )

    return plan


def plan_binary_build(config: Config, artifacts: Sequence[BinaryArtifact]) -> Plan:
    """Pure planner for binary-mode builds.

    Input is the list of artifacts discovered by Fetch (see
    `discover_binary_artifacts`). This function just applies the
    --product filter and turns each surviving artifact into a build unit
    whose strategy is "copy-artifact" — Execute will literally `cp -R`
    it into the output directory.
    """
    if config.target_filters:
        raise PlanError(
            "--target is a source-build escape hatch and cannot be used "
            "with --binary."
        )

    plan = Plan()
    plan.binary_mode = True
    plan.include_deps = config.include_deps

    product_filter: Optional[Set[str]] = (
        set(config.product_filters) if config.product_filters else None
    )

    # Dedupe: a vendor artifactbundle can contain the same-named
    # xcframework in multiple locations (usually a bug — cf. the
    # `__MACOSX` ghost already pruned in Fetch), but even legit
    # multi-slice packages occasionally list duplicates. Keep the first
    # occurrence, record the rest on plan.skipped so --dry-run explains
    # why they disappeared from the plan.
    seen_names: Set[str] = set()
    for art in artifacts:
        if art.product_name in seen_names:
            plan.skipped.append(
                (art.product_name, f"duplicate artifact at {art.path}")
            )
            continue
        seen_names.add(art.product_name)
        if product_filter is not None and art.product_name not in product_filter:
            continue
        plan.build_units.append(
            BuildUnit(
                name=art.product_name,
                scheme="",  # n/a — nothing to archive
                framework_name=art.product_name,
                language=Language.NA,  # binary — we don't inspect the bytes
                archive_strategy="copy-artifact",
                source_targets=[],
                synthetic=False,
                artifact_path=art.path,
            )
        )

    if product_filter is not None:
        matched = {bu.name for bu in plan.build_units}
        unmatched = sorted(product_filter - matched)
        if unmatched:
            available = sorted(a.product_name for a in artifacts)
            raise PlanError(
                f"--product filter matched no binary artifacts: {unmatched}\n"
                f"Available artifacts: {available}"
            )

    if not plan.build_units:
        raise PlanError(
            "Binary plan produced zero build units. Did the vendor package "
            "actually ship xcframeworks under .build/artifacts/?"
        )

    return plan


def _derive_package_label(src: str) -> str:
    """Return a human label for a package source URL/path.

    Handles the three shapes we see in practice:
      - https/ssh URLs ending in `.git`  → repo name without .git
      - SCP-style `git@host:org/repo.git` → repo name without .git
      - local filesystem paths           → basename
    """
    src = src.rstrip("/")
    if ":" in src and "@" in src and not src.startswith(("http://", "https://", "ssh://")):
        # SCP-style: `git@github.com:org/repo.git`
        after_colon = src.rsplit(":", 1)[-1]
        base = os.path.basename(after_colon)
    else:
        base = os.path.basename(src)
    if base.endswith(".git"):
        base = base[:-4]
    return base or "(unknown)"


def print_plan(
    plan: Plan,
    *,
    package: Optional[Package],
    config: Config,
) -> None:
    """Render a Plan in the human-readable dry-run format.

    Matches the shape documented in the Session 2 brief. One horizontal
    rule per section; sections are elided when empty. Keeps alignment
    tidy by computing column widths up front so the output stays readable
    even for 12-target Stripe runs.
    """
    if package is not None:
        name = package.name
    else:
        # Binary mode has no Package model — derive the label from the
        # package source URL's basename. Handle SCP-style `git@host:org/repo.git`
        # specially because os.path.basename returns the whole string for it.
        name = _derive_package_label(config.package_source or "(unknown)")

    version = config.user_version or "(unversioned)"
    mode = "binary" if plan.binary_mode else "source"
    bold(f"\nPlan for {name} @ {version}  ({mode} mode)")

    if plan.package_swift_edits:
        print("  Package edits:")
        for edit in plan.package_swift_edits:
            if edit.kind == "force_dynamic":
                print(f"    - force_dynamic: {edit.product_name}")
            elif edit.kind == "add_synthetic_library":
                tgts = ", ".join(edit.targets)
                print(f"    - add_synthetic_library: {edit.product_name} → targets=[{tgts}]")
            else:
                print(f"    - {edit.kind}: {edit.product_name}")
    elif not plan.binary_mode:
        print("  Package edits: (none)")

    if plan.build_units:
        print("  Build units:")
        name_w = max(len(bu.name) for bu in plan.build_units)
        scheme_w = max(len(bu.scheme or "-") for bu in plan.build_units)
        lang_w = max(len(bu.language or "-") for bu in plan.build_units)
        name_w = max(name_w, 10)
        scheme_w = max(scheme_w, 8)
        lang_w = max(lang_w, 5)
        for i, bu in enumerate(plan.build_units, start=1):
            markers = []
            if bu.synthetic:
                markers.append("[synthetic library]")
            if bu.archive_strategy == "copy-artifact":
                markers.append("[binary artifact]")
            marker = ("  " + " ".join(markers)) if markers else ""
            scheme_disp = bu.scheme or "-"
            print(
                f"    [{i}] {bu.name:<{name_w}}  "
                f"scheme={scheme_disp:<{scheme_w}}  "
                f"language={bu.language:<{lang_w}}  "
                f"→ {bu.framework_name}.xcframework{marker}"
            )
    else:
        print("  Build units: (none)")

    if plan.skipped:
        print("  Skipped:")
        for sname, reason in plan.skipped:
            print(f"    - {sname} ({reason})")

    if plan.include_deps:
        print("  Note: --include-deps is enabled; transitive frameworks will "
              "be discovered after Execute runs.")


# ============================================================================
# --- Phase 3: Prepare ---
# ============================================================================
#
# Prepare is the riskiest phase (§5.3). It mutates Package.swift via
# string surgery and then verifies the result by re-running real
# `swift package dump-package` and asserting against the planner's
# expectations. Three guardrails (per design):
#
#   1. Edits are *whitelisted*. Prepare never decides what to edit; it
#      consumes Plan.package_swift_edits verbatim.
#   2. Edits are *span-scoped*. We locate one .library(...) call by exact
#      `name: "X"` substring match, walk balanced parens to find its
#      extent, then edit only inside that span.
#   3. Mandatory round-trip validation. After every edit lands, we
#      re-dump the manifest and assert linkage / product membership match
#      the plan. A failure here raises PrepareError with a unified diff
#      and the failed assertions.
#
# Anything that gets past the round-trip validator is by construction
# safe for Execute to consume.


def _strip_swift_comments(text: str) -> str:
    """Return `text` with `//` line comments and `/* */` block comments
    replaced by equal-length spans of spaces (preserving newlines). The
    length/offset preservation keeps any downstream index math valid,
    and preserving newlines keeps line-counting error messages honest.

    This is NOT a full Swift tokenizer — it only tracks double-quoted
    string state so comment markers inside a regular string literal
    don't trigger. The file-wide `_assert_no_unsupported_swift_constructs`
    gate runs AFTER this stripper and guards against the advanced
    string shapes (raw strings, triple-quotes, interpolation) that
    could otherwise fool the state machine.

    Behavior on unterminated comments / strings: we stop stripping at
    the unterminated boundary and keep the rest of the text verbatim.
    This conservative fall-through means a weird-shaped file is still
    checked against the unsupported-construct triggers in both its
    stripped-comment view and whatever trailing span the stripper
    couldn't classify.
    """
    n = len(text)
    out: List[str] = []
    i = 0
    while i < n:
        c = text[i]
        # Double-quoted string: copy verbatim, honoring `\\` escapes.
        if c == '"':
            out.append(c)
            i += 1
            while i < n:
                cc = text[i]
                out.append(cc)
                if cc == "\\" and i + 1 < n:
                    out.append(text[i + 1])
                    i += 2
                    continue
                if cc == '"':
                    i += 1
                    break
                if cc == "\n":
                    # Unterminated string — bail out. Copy the rest
                    # verbatim so the trigger-token scanner still sees
                    # anything suspicious downstream.
                    i += 1
                    break
                i += 1
            continue
        # Line comment: replace span with spaces up to (but not including)
        # the newline.
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            end = text.find("\n", i + 2)
            if end == -1:
                end = n
            out.append(" " * (end - i))
            i = end
            continue
        # Block comment: replace span with spaces, but preserve newlines
        # so line numbers don't shift. Swift block comments NEST, so we
        # track depth rather than stopping at the first `*/`.
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            depth = 1
            j = i + 2
            while j < n and depth > 0:
                if j + 1 < n and text[j] == "/" and text[j + 1] == "*":
                    depth += 1
                    j += 2
                    continue
                if j + 1 < n and text[j] == "*" and text[j + 1] == "/":
                    depth -= 1
                    j += 2
                    continue
                j += 1
            if depth > 0:
                # Unterminated (possibly nested) block comment.
                # Conservative fallthrough: copy the rest as-is and
                # let the trigger-token scan re-check.
                out.append(text[i:])
                i = n
                break
            stop = j
            for ch in text[i:stop]:
                out.append("\n" if ch == "\n" else " ")
            i = stop
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _assert_no_unsupported_swift_constructs(text: str) -> None:
    """Fail loudly if `text` contains Swift constructs the balanced-paren
    walker can't reason about.

    The walker handles double-quoted strings (with backslash escapes),
    line comments (//) and block comments. It does NOT handle Swift raw
    strings (#"..."#), multi-line triple-quoted strings, or string
    interpolation: parens inside an interpolated expression would fool
    the depth counter, and unescaped quotes inside a raw string would
    confuse the string-skip state.

    To avoid flagging false positives on doc comments that legitimately
    mention these constructs (e.g. `/// Uses #"..."# internally`), we
    scan a comment-stripped view of the manifest. Real code uses of
    the constructs still fire; mentions inside `//`, `/* */`, or `///`
    doc comments pass through untouched.

    The check remains a heuristic gate, not a full parser. Known
    limitations:
      - Doc comments inside regular strings are stripped, since the
        stripper follows the string-state machine. This is the same
        behavior as the downstream `_balanced_close` walker.
      - A string literal like `let s = "#\\"hi\\"#"` looks the same
        to the scanner as a real raw-string use, so the check will
        reject it. Real manifests don't write strings like this.
    """
    scanned = _strip_swift_comments(text)
    if '#"' in scanned:
        raise PrepareUserError(
            "Package.swift uses Swift raw string literals (`#\"...\"#`), "
            "which the balanced-paren walker doesn't understand. "
            "File a bug if this needs to be supported."
        )
    if '"""' in scanned:
        raise PrepareUserError(
            "Package.swift uses Swift triple-quoted strings (`\"\"\"`), "
            "which the balanced-paren walker doesn't understand. "
            "File a bug if this needs to be supported."
        )
    if '\\(' in scanned:
        raise PrepareUserError(
            "Package.swift uses Swift string interpolation (`\\(...)`), "
            "which the balanced-paren walker doesn't understand. "
            "File a bug if this needs to be supported."
        )


def _balanced_close(text: str, open_idx: int) -> int:
    """Walk text from `open_idx` (which must point at one of `(`, `[`, `{`)
    to the matching closing bracket, returning its index. Skips over Swift
    string literals (`"..."` with `\\"` escapes), `// ...` line comments, and
    `/* ... */` block comments. Returns -1 if no matching close is found.

    Does NOT handle Swift multi-line triple-quoted strings, raw strings,
    or string interpolation. Callers should run
    `_assert_no_unsupported_swift_constructs` on the full manifest text
    before invoking this walker so unsupported syntax fails loudly with a
    targeted PrepareError instead of being silently mis-parsed.
    """
    if open_idx < 0 or open_idx >= len(text):
        return -1
    open_ch = text[open_idx]
    pair = {"(": ")", "[": "]", "{": "}"}
    if open_ch not in pair:
        return -1
    close_ch = pair[open_ch]

    depth = 0
    i = open_idx
    n = len(text)
    while i < n:
        c = text[i]
        # Line comment: skip to end-of-line.
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            nl = text.find("\n", i + 2)
            if nl == -1:
                return -1
            i = nl + 1
            continue
        # Block comment: skip past the matched close. Swift block
        # comments NEST, so we track depth rather than stopping at
        # the first `*/`.
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            block_depth = 1
            i += 2
            while i < n and block_depth > 0:
                if i + 1 < n and text[i] == "/" and text[i + 1] == "*":
                    block_depth += 1
                    i += 2
                    continue
                if i + 1 < n and text[i] == "*" and text[i + 1] == "/":
                    block_depth -= 1
                    i += 2
                    continue
                i += 1
            if block_depth > 0:
                return -1
            continue
        # String literal: skip to closing quote, honoring `\\"` escapes.
        if c == '"':
            i += 1
            while i < n:
                cc = text[i]
                if cc == "\\" and i + 1 < n:
                    i += 2
                    continue
                if cc == '"':
                    i += 1
                    break
                if cc == "\n":
                    # Unterminated string — give up rather than misparse.
                    return -1
                i += 1
            continue
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _find_library_call_for_product(text: str, product_name: str) -> Tuple[int, int]:
    """Locate the `.library(...)` call whose argument list contains
    `name: "<product_name>"` (exact substring match — no prefix, no glob).

    Returns `(open_paren_idx, close_paren_idx)` for the located call, or
    `(-1, -1)` if no `.library(` call's span contains a matching `name:`
    clause.

    Implementation note: GRDB's manifest declares THREE `.library(...)`
    products on the same target, and two of them have names starting with
    `GRDB`. Walking forward from the first `.library(` after a hard-coded
    name is wrong — we'd accidentally match the wrong product. Instead we
    enumerate every `.library(` span in the file and check whose interior
    matches the exact-name regex.
    """
    name_re = re.compile(r'name\s*:\s*"' + re.escape(product_name) + r'"')
    library_re = re.compile(r"\.library\s*\(")
    pos = 0
    while True:
        m = library_re.search(text, pos)
        if not m:
            return -1, -1
        open_idx = m.end() - 1
        close_idx = _balanced_close(text, open_idx)
        if close_idx == -1:
            raise PrepareUserError(
                f"Could not find balanced `)` for .library( at offset {open_idx}; "
                f"the manifest may be malformed."
            )
        span = text[open_idx + 1 : close_idx]
        if name_re.search(span):
            return open_idx, close_idx
        pos = close_idx + 1


def edit_force_dynamic(manifest_text: str, product_name: str) -> str:
    """Rewrite `manifest_text` so the `.library(name: "<product_name>", ...)`
    call has `type: .dynamic`.

    Three cases:
      - The library is already `type: .dynamic` → return text unchanged.
        (Defensive: the planner should never emit this edit, but Prepare
        treats it as a no-op rather than raising — the round-trip validator
        is what catches genuinely-broken edits.)
      - The library has an explicit `type: .X` → replace it with
        `type: .dynamic`.
      - The library has no `type:` clause → insert `, type: .dynamic`
        immediately after the matching `name: "<product_name>"` clause.

    All edits are scoped to the located `.library(...)` span; the rest of
    the file is left untouched. Raises PrepareError if no matching call is
    found (use `_find_library_call_for_product` to test before calling).
    """
    open_idx, close_idx = _find_library_call_for_product(manifest_text, product_name)
    if open_idx == -1:
        snippet = manifest_text[:200].replace("\n", "\\n")
        raise PrepareUserError(
            f"force_dynamic: could not locate `.library(name: {product_name!r}, ...)` "
            f"in manifest. The planner asked for an edit that doesn't match any "
            f"product. First 200 chars of manifest:\n  {snippet!r}"
        )

    span = manifest_text[open_idx + 1 : close_idx]

    type_dynamic_re = re.compile(r"type\s*:\s*\.dynamic\b")
    if type_dynamic_re.search(span):
        return manifest_text  # already dynamic; nothing to do

    type_other_re = re.compile(r"type\s*:\s*\.[A-Za-z_][A-Za-z0-9_]*")
    m = type_other_re.search(span)
    if m:
        new_span = span[: m.start()] + "type: .dynamic" + span[m.end() :]
        return manifest_text[: open_idx + 1] + new_span + manifest_text[close_idx:]

    name_re = re.compile(r'name\s*:\s*"' + re.escape(product_name) + r'"')
    nm = name_re.search(span)
    if not nm:
        # Should be unreachable: _find_library_call_for_product just confirmed it.
        raise PrepareBug(
            f"force_dynamic: located `.library(` for {product_name!r} but lost "
            f"track of the `name:` clause. This is a bug in spm-to-xcframework."
        )
    insert_at = nm.end()
    new_span = span[:insert_at] + ", type: .dynamic" + span[insert_at:]
    return manifest_text[: open_idx + 1] + new_span + manifest_text[close_idx:]


def _find_products_array(text: str) -> Tuple[int, int]:
    """Return `(open_bracket_idx, close_bracket_idx)` for the `products: [...]`
    array, or `(-1, -1)` if no such array is found.

    Matches `\\bproducts\\s*:\\s*\\[` so we don't accidentally trigger on a
    target field literally named `products` inside a settings dict (no real
    package does this, but defense-in-depth is cheap).
    """
    label_re = re.compile(r"\bproducts\s*:\s*\[")
    m = label_re.search(text)
    if not m:
        return -1, -1
    open_bracket = m.end() - 1
    close_bracket = _balanced_close(text, open_bracket)
    return open_bracket, close_bracket


def edit_add_synthetic_library(
    manifest_text: str, name: str, targets: Sequence[str]
) -> str:
    """Insert `.library(name: "<name>", type: .dynamic, targets: [...])`
    immediately before the closing `]` of the `products:` array.

    Handles both shapes commonly seen in real Package.swift files:
      - Trailing-comma form (GRDB):       `.library(...),\\n    ]`
      - No trailing comma (Stripe):       `.library(...)\\n    ]`

    The new entry's indent is sampled from the line containing the previous
    entry's last meaningful character. Empty arrays fall back to the close
    bracket's indent + 4 spaces.

    A leading comma is added iff the previous non-whitespace character
    inside the array is not `,` or `[`. This means trailing-comma manifests
    stay valid (no double comma) and no-trailing-comma manifests gain the
    necessary separator.
    """
    open_bracket, close_bracket = _find_products_array(manifest_text)
    if open_bracket == -1:
        raise PrepareUserError(
            "add_synthetic_library: could not locate `products:` array in "
            "manifest. The planner emitted an `add_synthetic_library` edit "
            "for a package without a products section."
        )
    if close_bracket == -1:
        raise PrepareUserError(
            "add_synthetic_library: products array `[` has no matching `]`. "
            "The manifest may be malformed."
        )

    # Walk back from the close bracket to find the last meaningful character
    # inside the array. Whitespace and the close bracket itself don't count.
    j = close_bracket - 1
    while j > open_bracket and manifest_text[j] in " \t\r\n":
        j -= 1
    has_entries = j > open_bracket
    prev_ch = manifest_text[j] if has_entries else "["

    # Compute the indent for the new entry. Sample from the line containing
    # the last meaningful character (which sits inside the previous entry's
    # span — typically the closing `)` or trailing `,`). For empty arrays we
    # fall back to one indent level deeper than the close bracket's line.
    sample_idx = j if has_entries else close_bracket
    line_start = manifest_text.rfind("\n", 0, sample_idx) + 1
    indent = ""
    for ch in manifest_text[line_start:sample_idx]:
        if ch in (" ", "\t"):
            indent += ch
        else:
            break
    if not has_entries:
        indent = indent + "    "
    entry_indent = indent

    targets_literal = ", ".join(f'"{t}"' for t in targets)
    new_entry = (
        f'.library(name: "{name}", type: .dynamic, targets: [{targets_literal}])'
    )

    if prev_ch in (",", "["):
        insertion = f"\n{entry_indent}{new_entry}"
    else:
        insertion = f",\n{entry_indent}{new_entry}"

    # Insert immediately after the last meaningful char (or at open_bracket+1
    # for empty arrays). The whitespace + indent + `]` that originally
    # followed stays in place, so the array's overall shape is preserved.
    insert_at = j + 1 if has_entries else open_bracket + 1
    return manifest_text[:insert_at] + insertion + manifest_text[insert_at:]


def _swift_toolchain_version() -> Optional[Tuple[int, int, int]]:
    """Return the Swift toolchain `(major, minor, patch)`, or None if it
    can't be parsed.

    Used by `_select_active_manifest` to mirror SPM's manifest-selection
    rule: SPM picks the highest `Package@swift-X.Y[.Z].swift` whose version
    is `<=` the active toolchain version, falling back to `Package.swift`.
    """
    try:
        cp = subprocess.run(
            ["swift", "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (FileNotFoundError, OSError):
        return None
    if cp.returncode != 0:
        return None
    m = re.search(r"Swift version (\d+)\.(\d+)(?:\.(\d+))?", cp.stdout or "")
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


_TOOLS_VERSION_RE = re.compile(
    r"//\s*swift-tools-version[: ]\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?"
)


def _read_declared_tools_version(path: Path) -> Optional[Tuple[int, int, int]]:
    """Parse the `// swift-tools-version: X.Y[.Z]` line from a manifest.

    SPM mandates this line as the very first line of every Package.swift
    (base or version-specific). Returns None on missing/unparseable lines.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for _ in range(8):
                line = fh.readline()
                if not line:
                    break
                m = _TOOLS_VERSION_RE.search(line)
                if m:
                    return (
                        int(m.group(1)),
                        int(m.group(2) or 0),
                        int(m.group(3) or 0),
                    )
    except OSError:
        return None
    return None


def _select_active_manifest(staged_dir: Path) -> Path:
    """Pick the Package.swift file SPM will actually read for this toolchain.

    Mirrors SPM's actual selection rule (SE-0152): for every candidate
    manifest in the package root (`Package.swift` and any sibling
    `Package@swift-X.Y[.Z].swift`), parse the `// swift-tools-version` line
    declared *inside* that file and pick the file whose declared
    tools-version is the highest one still `<=` the active toolchain.

    The filename's `@swift-X.Y` suffix is just a sort hint, not the
    selection key — Alamofire 5.10.2 ships `Package.swift` with
    `swift-tools-version: 6.0` plus `Package@swift-5.10.swift` (5.10) plus
    `Package@swift-5.9.swift` (5.9), and on a 6.x toolchain SPM picks
    `Package.swift` because 6.0 is the highest fitting tools-version.
    Selecting by filename alone — as the original implementation did —
    edits the wrong file and the round-trip validator catches it as a
    silent no-op when `dump-package` re-reads the real active manifest.

    Falls back to `Package.swift` if the toolchain version can't be parsed
    or if no manifest declares a tools-version we can read.
    """
    base = staged_dir / "Package.swift"
    tc = _swift_toolchain_version()
    if tc is None:
        return base

    candidates: List[Path] = []
    if base.is_file():
        candidates.append(base)
    candidates.extend(sorted(staged_dir.glob("Package@swift-*.swift")))

    best: Optional[Tuple[Tuple[int, int, int], Path]] = None
    for candidate in candidates:
        declared = _read_declared_tools_version(candidate)
        if declared is None:
            continue
        if declared > tc:
            continue
        # Tie-break by declared version, then prefer the un-suffixed
        # `Package.swift` over `Package@swift-X.Y.swift` on identical
        # tools-versions (matches SPM, which treats the base manifest as
        # canonical when both declare the same version).
        if best is None or declared > best[0] or (
            declared == best[0] and candidate.name == "Package.swift"
        ):
            best = (declared, candidate)
    return best[1] if best is not None else base


def apply_package_swift_edits(staged_dir: Path, plan: Plan) -> str:
    """Mutate the active Package.swift in `staged_dir` according to
    `plan.package_swift_edits`.

    "Active" means the manifest SPM actually reads for this toolchain —
    if the package ships a `Package@swift-X.Y.swift` whose version is
    `<=` the active Swift toolchain, that file is edited *instead of*
    `Package.swift`. This matches what `swift package dump-package` and
    `xcodebuild` see, so the planner's edit decisions (which were derived
    from the same dump) line up with the file we mutate.

    Side effects:
      - Writes `.original-<basename>` next to the active manifest as a
        debugging artifact (only if not already present from a prior run).
      - Overwrites the active manifest with the edited text.

    Order of operations: all `force_dynamic` edits first, then all
    `add_synthetic_library` edits. This matters because synthetic libraries
    are inserted at the END of the products array, so applying them last
    means the force_dynamic regex never trips over a freshly-added line.

    Returns the original (pre-edit) manifest text. The caller passes that
    to `validate_prepared_manifest` so a validation failure can render a
    diff against the same baseline this function used.
    """
    manifest_path = _select_active_manifest(staged_dir)
    if not manifest_path.is_file():
        raise PrepareBug(f"No Package.swift at {manifest_path}")

    original_text = manifest_path.read_text()
    # Fail loudly on Swift constructs the walker can't reason about
    # (raw strings, triple-quoted strings, string interpolation). Better
    # to abort here with a clear message than to silently mis-edit and
    # surface as an opaque round-trip mismatch later.
    _assert_no_unsupported_swift_constructs(original_text)
    edited = original_text

    # Snapshot the original for debugging. Don't clobber a prior snapshot:
    # if a previous run failed mid-Prepare, the .original from that run is
    # the *real* original and overwriting it would lose information.
    snapshot_path = manifest_path.parent / f".original-{manifest_path.name}"
    if not snapshot_path.exists():
        snapshot_path.write_text(original_text)

    # Phase 1: force_dynamic edits. Apply in plan order so the diff in any
    # validator-failure message stays deterministic across runs.
    for edit in plan.package_swift_edits:
        if edit.kind != "force_dynamic":
            continue
        edited = edit_force_dynamic(edited, edit.product_name)

    # Phase 2: synthetic library inserts. We append in plan order, so the
    # final products array preserves the planner's stated ordering.
    for edit in plan.package_swift_edits:
        if edit.kind != "add_synthetic_library":
            continue
        edited = edit_add_synthetic_library(edited, edit.product_name, edit.targets)

    if edited != original_text:
        manifest_path.write_text(edited)

    return original_text


def validate_prepared_manifest(
    staged_dir: Path,
    plan: Plan,
    original_text: str,
) -> Tuple[dict, List[Product], List[Target], List[Platform], str, str]:
    """Round-trip the edited Package.swift through `swift package dump-package`
    and assert every planner-requested edit landed correctly.

    On success, returns the parsed dump shards (the same shape `dump_package`
    returns) so the caller can build a Package model with whatever schemes
    list it wants attached. On failure, raises PrepareError.

    Asserts (per design §5.3):
      1. The dump still parses (handled by raising InspectError → caught
         and re-raised as PrepareError with diff context).
      2. Every `force_dynamic` edit produced a product with linkage DYNAMIC.
      3. Every `add_synthetic_library` edit produced a product with the
         requested name, linkage DYNAMIC, and targets matching exactly.
      4. Every build unit the planner intends to archive (i.e. not a
         copy-artifact) corresponds to a present product in the dumped
         manifest.

    On any failure, raises PrepareError with the failed assertion(s), a
    unified diff between the pre-edit and post-edit manifests, and a JSON
    snapshot of any directly-implicated post-edit products.
    """
    manifest_path = _select_active_manifest(staged_dir)
    edited_text = manifest_path.read_text() if manifest_path.is_file() else ""

    try:
        raw, products, targets, platforms, name, tools_version = dump_package(staged_dir)
    except InspectError as exc:
        diff = _unified_diff(original_text, edited_text, label=manifest_path.name)
        raise PrepareBug(
            f"Round-trip validation failed: edited {manifest_path.name} no longer "
            f"parses through `swift package dump-package`.\n\nUnderlying "
            f"error:\n  {exc}\n\nUnified diff (original → edited):\n{diff}"
        ) from exc

    products_by_name = {p.name: p for p in products}
    failures: List[str] = []
    implicated: List[Product] = []

    for edit in plan.package_swift_edits:
        if edit.kind == "force_dynamic":
            prod = products_by_name.get(edit.product_name)
            if prod is None:
                failures.append(
                    f"force_dynamic: product {edit.product_name!r} is missing "
                    f"from the post-edit dumped manifest"
                )
                continue
            if prod.linkage != Linkage.DYNAMIC:
                failures.append(
                    f"force_dynamic: product {edit.product_name!r} has linkage "
                    f"{prod.linkage!r} after Prepare; expected {Linkage.DYNAMIC!r}"
                )
                implicated.append(prod)
        elif edit.kind == "add_synthetic_library":
            prod = products_by_name.get(edit.product_name)
            if prod is None:
                failures.append(
                    f"add_synthetic_library: product {edit.product_name!r} is "
                    f"absent from the post-edit dumped manifest (the new "
                    f".library() entry didn't take effect)"
                )
                continue
            if prod.linkage != Linkage.DYNAMIC:
                failures.append(
                    f"add_synthetic_library: product {edit.product_name!r} "
                    f"has linkage {prod.linkage!r}; expected {Linkage.DYNAMIC!r}"
                )
                implicated.append(prod)
            expected_targets = list(edit.targets)
            if list(prod.targets) != expected_targets:
                failures.append(
                    f"add_synthetic_library: product {edit.product_name!r} "
                    f"targets are {prod.targets!r}; expected {expected_targets!r}"
                )
                implicated.append(prod)

    # Cross-check: every non-copy-artifact build unit must correspond to a
    # present product. This catches the class of bug where a planner change
    # silently emits a build unit for a product Prepare doesn't materialize.
    for unit in plan.build_units:
        if unit.archive_strategy == "copy-artifact":
            continue
        if unit.name not in products_by_name:
            failures.append(
                f"build_unit {unit.name!r}: not present in post-edit dumped "
                f"manifest (planner expected this product to exist)"
            )

    if failures:
        diff = _unified_diff(original_text, edited_text, label=manifest_path.name)
        impl_block = ""
        if implicated:
            seen = set()
            unique = []
            for p in implicated:
                if p.name in seen:
                    continue
                seen.add(p.name)
                unique.append(p)
            impl_lines = [
                f"  - {p.name}: linkage={p.linkage}, targets={p.targets}"
                for p in unique
            ]
            impl_block = "\n\nImplicated post-edit products:\n" + "\n".join(impl_lines)
        bullet = "\n".join(f"  - {f}" for f in failures)
        raise PrepareBug(
            "Round-trip validation failed:\n"
            + bullet
            + impl_block
            + "\n\nUnified diff (original → edited):\n"
            + diff
        )

    # Re-resolve dependencies if any synthetic library was added — new
    # products can pull new transitive deps that need .build/checkouts.
    if any(e.kind == "add_synthetic_library" for e in plan.package_swift_edits):
        cp = subprocess.run(
            ["swift", "package", "resolve"],
            cwd=str(staged_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if cp.returncode != 0:
            tail = "\n".join((cp.stderr or "").rstrip().splitlines()[-10:])
            raise PrepareBug(
                "swift package resolve failed after Prepare added synthetic "
                "libraries:\n" + (tail or "  (no stderr)")
            )

    return raw, products, targets, platforms, name, tools_version


def _unified_diff(original: str, edited: str, label: str = "Package.swift") -> str:
    """Render a unified diff between two manifest texts. Used in
    PrepareError messages so users (and the test harness) can see exactly
    what surgery Prepare attempted.

    Imported lazily because difflib is rarely needed at runtime — the
    happy path doesn't render diffs at all.
    """
    import difflib

    return "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            edited.splitlines(keepends=True),
            fromfile=f"{label} (pre-edit)",
            tofile=f"{label} (post-edit)",
            n=3,
        )
    )


def prepare(staged_dir: Path, plan: Plan, *, verbose: bool = False) -> PreparedPlan:
    """Top-level Prepare entry point.

    Applies the planner's whitelisted edits to `staged_dir/Package.swift`
    and runs the mandatory round-trip validator. Returns a PreparedPlan
    that wraps the original Plan and the post-edit Package model. Schemes
    from the inspect-time discovery are preserved on the returned Package
    (synthetic libraries don't need pre-discovered schemes — xcodebuild
    auto-generates them at archive time against our clean staged dir).
    """
    info("Preparing Package.swift edits...")
    active_manifest = _select_active_manifest(staged_dir)
    if active_manifest.name != "Package.swift":
        verbose_log(
            verbose,
            f"  Active manifest: {active_manifest.name} (selected by toolchain)",
        )
    no_op = not plan.package_swift_edits
    if no_op:
        verbose_log(verbose, "  No Package.swift edits requested by planner.")
        # Re-dump anyway so PreparedPlan.package reflects whatever the
        # current manifest says — Execute reads it for diagnostic context.
        raw, products, targets, platforms, name, tv = dump_package(staged_dir)
        return PreparedPlan(
            plan=plan,
            package=Package(
                name=name,
                tools_version=tv,
                platforms=platforms,
                products=products,
                targets=targets,
                schemes=[],
                raw_dump=raw,
                staged_dir=staged_dir,
            ),
        )

    for edit in plan.package_swift_edits:
        if edit.kind == "force_dynamic":
            verbose_log(verbose, f"  edit: force_dynamic {edit.product_name}")
        elif edit.kind == "add_synthetic_library":
            verbose_log(
                verbose,
                f"  edit: add_synthetic_library {edit.product_name} → "
                f"targets={edit.targets}",
            )

    original = apply_package_swift_edits(staged_dir, plan)
    raw, products, targets, platforms, name, tv = validate_prepared_manifest(
        staged_dir, plan, original
    )
    success(f"  Prepare validated {len(plan.package_swift_edits)} edit(s) ✓")
    return PreparedPlan(
        plan=plan,
        package=Package(
            name=name,
            tools_version=tv,
            platforms=platforms,
            products=products,
            targets=targets,
            # The schemes the planner saw at inspect time still describe the
            # pre-edit manifest, but synthetic-library schemes are
            # auto-generated by xcodebuild at archive time against our clean
            # staged dir, so the build unit's `scheme` field is what Execute
            # actually consumes. We leave schemes empty here rather than
            # carrying a stale list across the Prepare boundary.
            schemes=[],
            raw_dump=raw,
            staged_dir=staged_dir,
        ),
    )


# ============================================================================
# --- Phase 4: Execute ---
# ============================================================================
#
# Sessions 3 + 4 split this phase across two checkpoints:
#
# - Session 3 ran a single device-slice build per unit, recorded the
#   archive path + located framework, and stopped there. No xcframework,
#   no injection, no static promotion.
# - Session 4 turns that into the full pipeline: device + simulator
#   archives in parallel via ThreadPoolExecutor, static→dynamic promotion
#   when xcodebuild produced a `.a`, swiftmodule + ObjC header injection,
#   `xcodebuild -create-xcframework` merge, --include-deps walking, and
#   binary-mode artifact copies sharing the same `BinaryArtifact` model
#   that Fetch builds.
#
# Pure-function discipline matters because the parallel slice builds run
# under a thread pool: every helper below takes its inputs as parameters
# and returns its outputs as values. No module-level mutation, no shared
# mutable state. The only globally observable side effect is filesystem
# I/O scoped to `WORK_DIR` and `OUTPUT_DIR`, which are unique per
# invocation.


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


def _archive_static_lib_path(archive_path: Path) -> Optional[Path]:
    """Return the first `lib*.a` static library found anywhere under
    `<archive>/Products/`, or None.

    Session 3 records this for diagnostic purposes only — Session 4
    consumes it via `promote_static_to_framework`. Recording it here means
    the device-only slice's ExecutedUnit already carries enough information
    for Session 4 to take over without re-walking the archive.
    """
    products = archive_path / "Products"
    if not products.is_dir():
        return None
    for path in sorted(products.rglob("lib*.a")):
        if path.is_file():
            return path
    return None


def run_xcodebuild_archive(
    *,
    staged_dir: Path,
    scheme: str,
    destination: str,
    archive_path: Path,
    dd_path: Path,
    result_bundle_path: Path,
    log_path: Path,
    min_ios: str,
    verbose: bool,
) -> int:
    """Run `xcodebuild archive` for one (build unit, slice) combination.

    Captures combined stdout+stderr into `log_path`. In verbose mode the
    output is also tee'd to the terminal as it streams; non-verbose mode
    only writes to the log file. Returns the xcodebuild exit code (does
    not raise).

    The flag set is exactly the one specified in REWRITE_DESIGN.md §5.4:

        BUILD_LIBRARY_FOR_DISTRIBUTION=YES
        SKIP_INSTALL=NO
        IPHONEOS_DEPLOYMENT_TARGET=<min_ios>
        GCC_TREAT_WARNINGS_AS_ERRORS=NO
        SWIFT_TREAT_WARNINGS_AS_ERRORS=NO
        OTHER_SWIFT_FLAGS=-no-verify-emitted-module-interface
        -skipPackagePluginValidation
        -skipMacroValidation

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

    cmd = [
        "xcodebuild",
        "archive",
        "-scheme", scheme,
        "-destination", destination,
        "-archivePath", str(archive_path),
        "-derivedDataPath", str(dd_path),
        "-resultBundlePath", str(result_bundle_path),
        "BUILD_LIBRARY_FOR_DISTRIBUTION=YES",
        "SKIP_INSTALL=NO",
        f"IPHONEOS_DEPLOYMENT_TARGET={min_ios}",
        "GCC_TREAT_WARNINGS_AS_ERRORS=NO",
        "SWIFT_TREAT_WARNINGS_AS_ERRORS=NO",
        "OTHER_SWIFT_FLAGS=-no-verify-emitted-module-interface",
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
    reuse it without re-deriving the format."""
    lines = [f"xcodebuild archive failed for build unit {unit_name!r}."]
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


def _slice_paths(work_dir: Path, unit_name: str, arch_suffix: str) -> Tuple[Path, Path, Path, Path]:
    """Pure path computation for one (build unit, slice). Centralised so
    the parallel scheduler and the post-build framework lookup agree on
    where each artifact lives.

    Returns (archive_path, dd_path, result_bundle_path, log_path).
    """
    archive_path = work_dir / "archives" / f"{unit_name}-ios-{arch_suffix}.xcarchive"
    dd_path = work_dir / "dd" / unit_name / arch_suffix
    result_bundle_path = work_dir / "results" / f"{unit_name}-{arch_suffix}.xcresult"
    log_path = work_dir / f".build-output-{unit_name}-{arch_suffix}"
    return archive_path, dd_path, result_bundle_path, log_path


# Two slices per build unit. Order is fixed so the printed summary and
# the xcframework merge command consume them in a stable order
# (device first, then simulator).
_SLICES: Tuple[Tuple[str, str, str], ...] = (
    ("arm64", "iphoneos", "generic/platform=iOS"),
    ("simulator", "iphonesimulator", "generic/platform=iOS Simulator"),
)


def _archive_one_slice(
    unit: BuildUnit,
    *,
    arch_suffix: str,
    sdk_name: str,
    destination: str,
    staged_dir: Path,
    work_dir: Path,
    min_ios: str,
    verbose: bool,
) -> Tuple[ArchiveSlice, int]:
    """Run xcodebuild archive once for one (build unit, slice) combination
    and return the slice metadata plus the xcodebuild return code.

    Does NOT raise on xcodebuild failure — the caller (`_archive_pair_parallel`)
    needs both futures to settle so it can tail both logs and surface a
    consolidated error. Locating the framework / static lib also happens
    here so the caller can decide whether to trigger static promotion
    without re-walking the archive.

    Pure with respect to module-level state: thread-safe under
    `ThreadPoolExecutor`.
    """
    archive_path, dd_path, result_bundle_path, log_path = _slice_paths(
        work_dir, unit.name, arch_suffix
    )
    rc = run_xcodebuild_archive(
        staged_dir=staged_dir,
        scheme=unit.scheme,
        destination=destination,
        archive_path=archive_path,
        dd_path=dd_path,
        result_bundle_path=result_bundle_path,
        log_path=log_path,
        min_ios=min_ios,
        verbose=verbose,
    )
    framework_path = None
    static_lib_path = None
    if rc == 0:
        framework_path = _archive_framework_path(archive_path, unit.framework_name)
        if framework_path is None:
            static_lib_path = _archive_static_lib_path(archive_path)

    slice_obj = ArchiveSlice(
        arch_suffix=arch_suffix,
        sdk_name=sdk_name,
        archive_path=archive_path,
        dd_path=dd_path,
        log_path=log_path,
        result_bundle_path=result_bundle_path,
        framework_path=framework_path,
        static_lib_path=static_lib_path,
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


def _archive_pair_parallel(
    unit: BuildUnit,
    *,
    staged_dir: Path,
    work_dir: Path,
    min_ios: str,
    verbose: bool,
) -> Tuple[ArchiveSlice, ArchiveSlice]:
    """Build the device + simulator archives for one build unit in parallel
    via `ThreadPoolExecutor(max_workers=2)`.

    Both futures must settle before this function returns — the caller
    needs the consolidated state to decide whether the unit succeeded
    completely, partially, or failed. Slice logs are captured to separate
    files (`_slice_paths` enforces uniqueness) and tailed only after both
    settle, so device + sim output never interleave on the user's
    terminal.

    Raises ExecuteError if either slice's xcodebuild call exited non-zero,
    with the parsed xcresult diagnostics for whichever slice(s) failed.
    On success, returns (device_slice, simulator_slice).
    """
    info(f"  Building {unit.name} — device (arm64) + simulator (parallel)...")

    # Submit both archives. We use a fixed-size pool of 2 because that's
    # the only parallelism the iOS pipeline benefits from per build unit
    # — the device and simulator archives share scheme metadata but
    # otherwise touch disjoint paths under `work_dir`.
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures: Dict[concurrent.futures.Future, Tuple[str, str, str]] = {}
        for arch_suffix, sdk_name, destination in _SLICES:
            fut = pool.submit(
                _archive_one_slice,
                unit,
                arch_suffix=arch_suffix,
                sdk_name=sdk_name,
                destination=destination,
                staged_dir=staged_dir,
                work_dir=work_dir,
                min_ios=min_ios,
                verbose=verbose,
            )
            futures[fut] = (arch_suffix, sdk_name, destination)
        results: Dict[str, Tuple[ArchiveSlice, int]] = {}
        for fut in concurrent.futures.as_completed(futures):
            arch_suffix, _sdk, _dest = futures[fut]
            try:
                slice_obj, rc = fut.result()
            except Exception as exc:  # pragma: no cover — defensive
                # Any unexpected exception (a missing xcodebuild on PATH,
                # a permissions error, etc.) is reported as an
                # ExecuteError tagged to the slice that blew up.
                raise ExecuteError(
                    f"unexpected failure during {unit.name} {arch_suffix} archive: {exc}"
                ) from exc
            results[arch_suffix] = (slice_obj, rc)

    device_slice, device_rc = results["arm64"]
    sim_slice, sim_rc = results["simulator"]

    # Tail both logs after both settle. This is the legacy bash strategy
    # (build_archive at lines 814-820) — interleaved live output is
    # unreadable so the bash tool deferred summaries until both PIDs
    # exited. Verbose mode already streams everything live so we skip the
    # extra tail there to avoid double-printing.
    if not verbose:
        for slice_obj in (device_slice, sim_slice):
            tail = _tail_log(slice_obj.log_path)
            if tail:
                sys.stdout.write(tail)
                if not tail.endswith("\n"):
                    sys.stdout.write("\n")
        sys.stdout.flush()

    failed_slices: List[Tuple[ArchiveSlice, int]] = []
    if device_rc != 0:
        failed_slices.append((device_slice, device_rc))
    if sim_rc != 0:
        failed_slices.append((sim_slice, sim_rc))
    if failed_slices:
        sections: List[str] = []
        for slice_obj, _rc in failed_slices:
            errors = read_xcresult_errors(slice_obj.result_bundle_path, limit=5)
            slice_label = f"{unit.name} ({slice_obj.arch_suffix})"
            sections.append(_format_execute_error(slice_label, slice_obj.log_path, errors))
        raise ExecuteError("\n\n".join(sections))

    return device_slice, sim_slice


# ----- detect_system_frameworks --------------------------------------------
#
# Ported from the legacy bash spm-to-xcframework lines 896-976. Used by
# `promote_static_to_framework` when xcodebuild produced a static archive
# instead of a dynamic .framework. The legacy implementation embedded a
# python heredoc inside bash; we collapse it back to native Python here
# because the rewrite has the package dump as a typed model already.

# Directories we never recurse into when scanning a target's source tree
# for ObjC `#import <Framework/...>` lines. These are the SPM convention
# names for things that aren't part of the shipped library — running into
# them produces noise (a Demo app's `#import <UIKit/UIKit.h>` shouldn't
# count as a system-framework dependency of the library itself, even
# though UIKit happens to be a real system framework).
_TARGET_SOURCE_SCAN_EXCLUDES: Set[str] = {
    "Tests",
    "Demo",
    "Example",
    "Examples",
    "Samples",
    "Playground",
}

# Compiled regexes for ObjC system-framework imports. Both forms appear
# in the wild — `#import <UIKit/UIKit.h>` is the historical spelling and
# `@import UIKit;` is the module-aware form modern ObjC code prefers.
_RE_OBJC_HASH_IMPORT = re.compile(r"#import\s*<([A-Za-z_][A-Za-z0-9_]*)/")
_RE_OBJC_AT_IMPORT = re.compile(r"@import\s+([A-Za-z_][A-Za-z0-9_]*)")


def _scan_target_for_frameworks(
    target: Target,
    staged_dir: Path,
    frameworks: Set[str],
) -> None:
    """Scan one target's linker settings + source tree for system-framework
    references. Mutates `frameworks` in place.

    The two sources are unioned (legacy behaviour). Linker settings come
    from `swift package dump-package`'s `settings[].kind.linkedFramework`
    where `tool == "linker"`. Source-tree scanning walks the target's
    `path` (relative to staged_dir) and applies the same regex pair the
    legacy bash uses, with the same exclude list.
    """
    # NOTE: linker settings come from raw_dump in `dump_package`, but
    # `Target` doesn't currently store them — fall back to scanning the
    # raw dump via the package model. Caller passes the staged_dir for
    # filesystem access; raw_dump is read from `Package.raw_dump` in the
    # caller and threaded into `_resolve_target_linker_frameworks`.
    target_path_str = target.path or _default_target_path(target.name, target.kind)
    if not target_path_str:
        return
    target_path = staged_dir / target_path_str
    if not target_path.is_dir():
        return
    for root, dirs, files in os.walk(target_path):
        # Prune the walk in place. Pruning at the directory level (rather
        # than skipping in the file loop) saves a lot of work in repos
        # with large `Tests/` trees.
        dirs[:] = [d for d in dirs if d not in _TARGET_SOURCE_SCAN_EXCLUDES]
        for f in files:
            if not f.endswith((".h", ".m", ".mm")):
                continue
            try:
                with open(os.path.join(root, f), "r", errors="replace") as fp:
                    content = fp.read()
            except OSError:
                continue
            for m in _RE_OBJC_HASH_IMPORT.finditer(content):
                frameworks.add(m.group(1))
            for m in _RE_OBJC_AT_IMPORT.finditer(content):
                frameworks.add(m.group(1))


def _resolve_target_linker_frameworks(raw_target: dict, frameworks: Set[str]) -> None:
    """Pull `linkedFramework` entries out of a raw dump-package target's
    settings list. Mutates `frameworks` in place.

    The dump-package schema for linker settings is
    `{"tool": "linker", "kind": {"linkedFramework": "Foo"}}`. We accept
    any shape close to that and silently ignore everything else — this
    is best-effort detection, not a contract.
    """
    for s in raw_target.get("settings", []) or []:
        if not isinstance(s, dict):
            continue
        if s.get("tool") != "linker":
            continue
        kind = s.get("kind")
        if isinstance(kind, dict):
            fw = kind.get("linkedFramework")
            if isinstance(fw, str) and fw:
                frameworks.add(fw)


def detect_system_frameworks(package: Package, product_name: str) -> List[str]:
    """Return the sorted list of system frameworks the named product/target
    appears to need. Union of linker settings (from the package dump) and
    `#import <Framework/…>` / `@import Framework` references in target
    sources.

    `product_name` may name a `.library()` product OR a target (the
    `--target` escape hatch case). Direct dependencies of those targets
    are also scanned, matching the legacy bash behaviour at lines 954-966.
    Self-references (the product name itself, the scanned target names)
    are removed from the result so a library named `Stripe` doesn't
    spuriously appear to depend on a system framework called `Stripe`.
    """
    # Find the targets backing this product. First check products[], then
    # fall back to interpreting `product_name` as a bare target name (the
    # --target escape hatch case).
    raw_targets_by_name: Dict[str, dict] = {}
    for raw_t in package.raw_dump.get("targets", []) or []:
        if isinstance(raw_t, dict) and isinstance(raw_t.get("name"), str):
            raw_targets_by_name[raw_t["name"]] = raw_t

    product_targets: List[str] = []
    for raw_p in package.raw_dump.get("products", []) or []:
        if isinstance(raw_p, dict) and raw_p.get("name") == product_name:
            tlist = raw_p.get("targets")
            if isinstance(tlist, list):
                product_targets = [t for t in tlist if isinstance(t, str)]
            break
    if not product_targets and product_name in raw_targets_by_name:
        product_targets = [product_name]
    if not product_targets:
        return []

    frameworks: Set[str] = set()
    scanned: Set[str] = set()

    def scan(name: str) -> None:
        if name in scanned:
            return
        scanned.add(name)
        target = package.target_by_name(name)
        if target is not None:
            _scan_target_for_frameworks(target, package.staged_dir, frameworks)
        raw = raw_targets_by_name.get(name)
        if raw is not None:
            _resolve_target_linker_frameworks(raw, frameworks)

    # Direct product targets first.
    for tname in product_targets:
        scan(tname)
        # First-level internal dependencies. Mirrors the legacy bash
        # behaviour (only walk one level deep) but accepts BOTH the
        # `byName` and `target` dump-package shapes. GRDB's snapshot in
        # this file uses the `target` shape, and packages whose promoted
        # ObjC target pulls system frameworks through `target` edges
        # would otherwise silently miss those frameworks during
        # static→dynamic relinking (Codex [P1]).
        raw = raw_targets_by_name.get(tname)
        if raw is None:
            continue
        for dep_name in _raw_internal_dep_names(raw):
            if dep_name in raw_targets_by_name:
                scan(dep_name)

    # Strip self-references — the product name and any scanned target
    # name should never appear in the system-framework list.
    frameworks.discard(product_name)
    for tname in scanned:
        frameworks.discard(tname)
    return sorted(frameworks)


# ----- promote_static_to_framework -----------------------------------------
#
# Ported from the legacy bash spm-to-xcframework lines 985-1064. Triggered
# from `_run_one_unit` when neither slice produced a `.framework` and at
# least one slice produced a static archive instead.

_INFO_PLIST_FORMAT = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
    '<plist version="1.0">\n'
    '<dict>\n'
    '    <key>CFBundleExecutable</key>\n'
    '    <string>{product}</string>\n'
    '    <key>CFBundleIdentifier</key>\n'
    '    <string>com.spm-to-xcframework.{product}</string>\n'
    '    <key>CFBundleName</key>\n'
    '    <string>{product}</string>\n'
    '    <key>CFBundlePackageType</key>\n'
    '    <string>FMWK</string>\n'
    '    <key>MinimumOSVersion</key>\n'
    '    <string>{min_ios}</string>\n'
    '</dict>\n'
    '</plist>\n'
)


def _lipo_archs(static_lib: Path) -> List[str]:
    """Return the architectures present in a Mach-O static archive, via
    `lipo -archs`. Empty list on failure (lipo missing, file is not a
    Mach-O object, etc.) — the caller treats that as "can't promote"."""
    cp = subprocess.run(
        ["lipo", "-archs", str(static_lib)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if cp.returncode != 0:
        return []
    return [a for a in cp.stdout.strip().split() if a]


def promote_static_to_framework(
    *,
    static_lib: Path,
    product: str,
    sdk_name: str,
    min_ios: str,
    archive_path: Path,
    system_frameworks: Sequence[str],
    verbose: bool,
) -> Path:
    """Re-link a `.a` static archive into a dynamic `.framework` bundle.

    Same algorithm as the legacy bash promote_static_to_framework:
      1. `lipo -archs` to discover the slice's archs.
      2. `xcrun --sdk <sdk> clang -dynamiclib` with one `-arch` per arch,
         the appropriate `-m{iphoneos,ios-simulator}-version-min` flag,
         `-install_name @rpath/<X>.framework/<X>`, `-Xlinker -all_load`,
         every `-framework <Foo>` we detected for the product, and
         `-Xlinker -undefined dynamic_lookup` as a safety net for
         indirect dependencies the scanner missed.
      3. Wrap the resulting Mach-O in a minimal `.framework` bundle with
         a CFBundlePackageType=FMWK Info.plist.

    Returns the path to the newly-created `.framework` directory. Raises
    `ExecuteError` if any step fails.
    """
    fw_dir = archive_path / "Products" / "Library" / "Frameworks" / f"{product}.framework"
    if fw_dir.exists():
        shutil.rmtree(fw_dir)
    fw_dir.mkdir(parents=True, exist_ok=True)

    archs = _lipo_archs(static_lib)
    if not archs:
        raise ExecuteError(
            f"Failed to determine architectures from {static_lib} via lipo -archs."
        )

    if sdk_name == "iphonesimulator":
        min_ver_flag = f"-mios-simulator-version-min={min_ios}"
    else:
        min_ver_flag = f"-miphoneos-version-min={min_ios}"

    dim(f"  Re-linking static → dynamic ({sdk_name}: {' '.join(archs)})")

    cmd: List[str] = ["xcrun", "--sdk", sdk_name, "clang", "-dynamiclib"]
    for a in archs:
        cmd.extend(["-arch", a])
    cmd.extend([
        min_ver_flag,
        "-install_name", f"@rpath/{product}.framework/{product}",
        "-Xlinker", "-all_load",
        str(static_lib),
    ])
    for fw in system_frameworks:
        cmd.extend(["-framework", fw])
    # Safety net for any unresolved symbols (legacy line 1030). Indirect
    # transitive deps that the source-tree scan missed will resolve at
    # runtime via dyld instead of failing the link here.
    cmd.extend(["-Xlinker", "-undefined", "-Xlinker", "dynamic_lookup"])
    cmd.extend(["-o", str(fw_dir / product)])

    verbose_log(verbose, f"  $ {' '.join(cmd)}")

    log_path = archive_path / "relink.log"
    with open(log_path, "w") as logf:
        cp = subprocess.run(
            cmd,
            stdout=logf,
            stderr=subprocess.STDOUT,
            text=True,
        )
    if cp.returncode != 0:
        tail = _tail_log(log_path, n=10)
        raise ExecuteError(
            f"Failed to re-link static library {static_lib} as dynamic framework "
            f"{product}.framework (sdk={sdk_name}). Last lines of relink log "
            f"({log_path}):\n{tail}"
        )

    plist_text = _INFO_PLIST_FORMAT.format(product=product, min_ios=min_ios)
    (fw_dir / "Info.plist").write_text(plist_text)

    return fw_dir


# ----- inject_swiftmodule + inject_objc_headers ----------------------------
#
# Both ports of the legacy bash injection passes (lines 1273-1453). They
# operate on a single slice's framework path and DerivedData; called
# twice per build unit (once per slice).


def _find_swiftmodule_in_dd(
    dd_path: Path,
    fw_name: str,
    scheme: str,
    extra_module_names: Sequence[str] = (),
) -> Optional[Tuple[Path, str]]:
    """Locate a `<name>.swiftmodule` directory under DerivedData.

    Search order:
      1. `*/<fw_name>.swiftmodule` anywhere under DerivedData.
      2. `ArchiveIntermediates/<scheme>/BuildProductsPath/*/<fw_name>.swiftmodule`
         (legacy bash 1296-1301 fallback for scheme/product name mismatch).
      3. For each name in extra_module_names (typically the underlying
         source targets), the same two passes. This handles cases like
         GRDB-dynamic, where the framework binary is named "GRDB-dynamic"
         but the Swift module the targets actually emit is "GRDB".

    Returns (path, module_name) so the caller knows which name to use
    for the destination directory inside the framework's Modules/.
    """

    def _find_for(name: str) -> Optional[Path]:
        target = f"{name}.swiftmodule"
        if dd_path.is_dir():
            for path in dd_path.rglob(target):
                if path.is_dir():
                    return path
        if scheme != name and dd_path.is_dir():
            intermediate = (
                dd_path / "Build" / "Intermediates.noindex"
                / "ArchiveIntermediates" / scheme / "BuildProductsPath"
            )
            if intermediate.is_dir():
                for path in intermediate.rglob(target):
                    if path.is_dir():
                        return path
        return None

    found = _find_for(fw_name)
    if found is not None:
        return found, fw_name
    for name in extra_module_names:
        if name == fw_name:
            continue
        found = _find_for(name)
        if found is not None:
            return found, name
    return None


def inject_swiftmodule(
    *,
    fw_path: Path,
    fw_name: str,
    scheme: str,
    dd_path: Path,
    variant: str,
    verbose: bool,
    extra_module_names: Sequence[str] = (),
) -> bool:
    """Copy `.swiftmodule/.swiftinterface` files from DerivedData into a
    framework's Modules directory if they're missing.

    `extra_module_names` lets the caller pass underlying source target
    names so that frameworks whose binary name doesn't match the actual
    Swift module name (e.g. GRDB-dynamic.framework whose module is
    `GRDB`) can still get their interfaces injected.

    Returns True iff anything was injected. Idempotent: if any
    `Modules/<X>.swiftmodule/` already contains `.swiftinterface` files
    this is a no-op.
    """
    modules_dir = fw_path / "Modules"
    if modules_dir.is_dir():
        for sm in modules_dir.glob("*.swiftmodule"):
            if sm.is_dir():
                for child in sm.iterdir():
                    if child.suffix == ".swiftinterface":
                        verbose_log(verbose, f"  Swift interfaces present in {variant} framework")
                        return False

    found = _find_swiftmodule_in_dd(dd_path, fw_name, scheme, extra_module_names)
    if found is None:
        verbose_log(
            verbose,
            f"  No Swift module found in DerivedData for {fw_name} ({variant})",
        )
        return False
    swiftmod, module_name = found

    dim(f"  Injecting Swift module interfaces ({variant})")
    modules_dir.mkdir(parents=True, exist_ok=True)
    dest = modules_dir / f"{module_name}.swiftmodule"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(swiftmod, dest)
    return True


def _has_h_files_recursive(path: Path) -> bool:
    """True iff `path` contains at least one `.h` file at any depth.

    Used to filter out directories that exist but contain only Swift
    sources or subdirectories with no public ObjC surface — those
    aren't real header roots even when they happen to be named `include`
    or carry an explicit `publicHeadersPath`.
    """
    for _root, _dirs, files in os.walk(path):
        for name in files:
            if name.endswith(".h"):
                return True
    return False


def _find_objc_headers_dir(
    package: Package,
    product_name: str,
    fw_name: str,
) -> Optional[Path]:
    """Find a directory of public ObjC headers for the named product.

    Priority (matches legacy bash 1352-1397):
      1. A direct product target whose name == fw_name with a
         publicHeadersPath that exists and contains *.h files.
      2. A direct product target whose name == product_name.
      3. Any direct product target with headers.
      4. First-level dependencies of direct product targets, with the
         same fw_name > product_name > anything-with-headers priority.

    Returns the absolute path of the public-headers directory, or None.
    """
    raw_targets_by_name: Dict[str, dict] = {}
    for raw_t in package.raw_dump.get("targets", []) or []:
        if isinstance(raw_t, dict) and isinstance(raw_t.get("name"), str):
            raw_targets_by_name[raw_t["name"]] = raw_t

    product_targets: List[str] = []
    for raw_p in package.raw_dump.get("products", []) or []:
        if isinstance(raw_p, dict) and raw_p.get("name") == product_name:
            tlist = raw_p.get("targets")
            if isinstance(tlist, list):
                product_targets = [t for t in tlist if isinstance(t, str)]
            break
    if not product_targets and product_name in raw_targets_by_name:
        product_targets = [product_name]
    if not product_targets:
        return None

    def headers_dir_for(target_name: str, allow_implicit_layouts: bool) -> Optional[Path]:
        target = package.target_by_name(target_name)
        if target is None:
            return None
        target_path = target.path or _default_target_path(target.name, target.kind)
        if not target_path:
            return None
        target_dir = package.staged_dir / target_path
        # 1. Explicit `publicHeadersPath` always wins (the legacy contract).
        if target.public_headers_path:
            full_path = target_dir / target.public_headers_path
            if full_path.is_dir() and _has_h_files_recursive(full_path):
                return full_path
            return None
        # 2. Implicit layouts are only allowed for the direct product
        #    target — never when walking deps. A dep with `include/` or a
        #    top-level umbrella header (e.g. Stripe3DS2 underneath
        #    StripePayments) would otherwise silently leak its public
        #    surface into the parent framework, since dep targets are
        #    always built into their own xcframeworks separately.
        if not allow_implicit_layouts:
            return None
        # 2a. SPM default for ObjC targets: `<target_path>/include/`.
        include_dir = target_dir / "include"
        if include_dir.is_dir() and _has_h_files_recursive(include_dir):
            return include_dir
        # 2b. Umbrella-header-at-target-root pattern. Stripe ships every
        #    product this way: e.g. `StripePayments.h` sits at the top of
        #    `StripePayments/StripePayments/` with no `include/` subdir.
        #    Accept the two umbrella forms only — `<TargetName>.h` (the
        #    common Stripe target pattern) and `<TargetName>-umbrella.h`
        #    (used by the Stripe product target itself). Anything else
        #    risks over-injection: `inject_objc_headers` walks the
        #    returned dir recursively and copies every .h, so accepting
        #    a generic non-`-Swift.h` would leak private/internal
        #    top-level headers into the framework's public surface.
        for candidate in (f"{target.name}.h", f"{target.name}-umbrella.h"):
            if (target_dir / candidate).is_file():
                return target_dir
        return None

    product_match: Optional[Path] = None
    any_match: Optional[Path] = None
    for tname in product_targets:
        d = headers_dir_for(tname, allow_implicit_layouts=True)
        if d is None:
            continue
        if tname == fw_name:
            return d
        if tname == product_name:
            if product_match is None:
                product_match = d
        else:
            if any_match is None:
                any_match = d
    if product_match is not None:
        return product_match
    if any_match is not None:
        return any_match

    # First-level dependencies of direct product targets. Accepts both
    # `byName` and `target` dep shapes — the GRDB-style `.target()`
    # form would otherwise go unvisited and public headers from
    # depended-on ObjC targets would be missed (Codex [P1]). Strict on
    # the dep walk: only accept deps that explicitly declare
    # `publicHeadersPath`, never the implicit `include/` / umbrella
    # fallbacks. A dep is by definition part of a different framework,
    # so its public surface belongs to its own xcframework — bleeding
    # implicit-layout headers up to the parent breaks Stripe-style
    # multi-product packages.
    dep_fw_match: Optional[Path] = None
    dep_product_match: Optional[Path] = None
    dep_any_match: Optional[Path] = None
    seen_deps: Set[str] = set()
    for tname in product_targets:
        raw = raw_targets_by_name.get(tname)
        if raw is None:
            continue
        for dep_name in _raw_internal_dep_names(raw):
            if dep_name in seen_deps:
                continue
            seen_deps.add(dep_name)
            d = headers_dir_for(dep_name, allow_implicit_layouts=False)
            if d is None:
                continue
            if dep_name == fw_name and dep_fw_match is None:
                dep_fw_match = d
            elif dep_name == product_name and dep_product_match is None:
                dep_product_match = d
            elif dep_any_match is None:
                dep_any_match = d
    if dep_fw_match is not None:
        return dep_fw_match
    if dep_product_match is not None:
        return dep_product_match
    return dep_any_match


def _generate_modulemap(fw_path: Path, fw_name: str) -> None:
    """Write `Modules/module.modulemap` for a framework that has ObjC
    headers but no module map. Uses an umbrella header if `<fw_name>.h`
    exists, otherwise lists every header explicitly. Same shape as the
    legacy bash 1430-1450.
    """
    modules_dir = fw_path / "Modules"
    modules_dir.mkdir(parents=True, exist_ok=True)
    headers_dir = fw_path / "Headers"
    umbrella = headers_dir / f"{fw_name}.h"
    if umbrella.is_file():
        # The `module * { export * }` line tells Clang to walk the
        # Headers/ tree on its own, so nested layouts work without
        # having to enumerate every file here.
        text = (
            f"framework module {fw_name} {{\n"
            f"  umbrella header \"{fw_name}.h\"\n"
            f"  export *\n"
            f"  module * {{ export * }}\n"
            f"}}\n"
        )
    else:
        # Walk recursively so nested headers (e.g. Headers/Sub/Foo.h)
        # land in the modulemap too — otherwise Clang only sees the
        # top-level .h files and `#import <Module/Sub/Foo.h>` fails to
        # resolve at bind time. Relative paths are POSIX-style to
        # match Clang's own module-map syntax.
        header_rel_paths = sorted(
            p.relative_to(headers_dir).as_posix()
            for p in headers_dir.rglob("*.h")
            if p.is_file()
        )
        lines = [f"framework module {fw_name} {{"]
        for rel in header_rel_paths:
            lines.append(f"  header \"{rel}\"")
        lines.append("  export *")
        lines.append("}")
        text = "\n".join(lines) + "\n"
    (modules_dir / "module.modulemap").write_text(text)


def _walk_system_library_target_deps(
    package: Package,
    source_targets: Sequence[str],
) -> List[Target]:
    """Return every transitive `.systemLibrary` target reachable from
    `source_targets` via internal `byName` / `target` dependency edges.

    Walks the raw dump rather than `Target.dependencies` because the typed
    list flattens out the `byName` vs `target` shapes — same convention as
    `_find_objc_headers_dir` and `detect_system_frameworks`. Result order
    is the discovery order (BFS) to keep diagnostics deterministic.
    """
    raw_targets_by_name: Dict[str, dict] = {}
    for raw_t in package.raw_dump.get("targets", []) or []:
        if isinstance(raw_t, dict) and isinstance(raw_t.get("name"), str):
            raw_targets_by_name[raw_t["name"]] = raw_t

    found: List[Target] = []
    seen: Set[str] = set()
    queue: List[str] = list(source_targets)
    while queue:
        name = queue.pop(0)
        if name in seen:
            continue
        seen.add(name)
        target = package.target_by_name(name)
        if target is None:
            continue
        if target.kind == TargetKind.SYSTEM:
            found.append(target)
            # System targets do not transitively depend on other targets
            # in any shape we care about — they are leaves by SPM design.
            continue
        raw = raw_targets_by_name.get(name)
        if raw is None:
            continue
        for dep_name in _raw_internal_dep_names(raw):
            if dep_name not in seen:
                queue.append(dep_name)
    return found


def _system_target_source_dir(target: Target, staged_dir: Path) -> Optional[Path]:
    """Locate the on-disk source directory for a `.systemLibrary` target.

    SPM convention: `Sources/<TargetName>/` unless `path:` is set
    explicitly. `_default_target_path` only returns paths for regular /
    executable / test kinds, so we duplicate the system-target convention
    here rather than widening that helper.
    """
    if target.path:
        full = staged_dir / target.path
    else:
        full = staged_dir / "Sources" / target.name
    return full if full.is_dir() else None


def _promote_modulemap_to_framework_form(modulemap_text: str) -> str:
    """Rewrite an SPM `.systemLibrary` modulemap so it can live inside a
    `.framework/Modules/module.modulemap` and be discovered via swiftc's
    `-F` framework search.

    Input  (SPM systemLibrary):  `module Foo [system] { header "shim.h" link "x" export * }`
    Output (framework module):   `framework module Foo [system] { header "shim.h" link "x" export * }`

    The only required change is the `framework ` qualifier on the
    top-level `module` declaration. Modern swiftc accepts plain `header`
    inside a framework module — `umbrella header` is not required when
    the framework has a single shim header (verified empirically against
    Swift 6.2.3, Xcode 26.2 simulator SDK).
    """
    return re.sub(
        r"(?m)^(\s*)module(\s+\w+)",
        r"\1framework module\2",
        modulemap_text,
        count=1,
    )


def inject_system_clang_modules(
    *,
    xcframework_path: Path,
    package: Package,
    source_targets: Sequence[str],
    verbose: bool,
) -> int:
    """Bundle every transitive `.systemLibrary` Clang module that the
    primary framework depends on as a sibling shim framework inside each
    xcframework slice. No-op if `source_targets` has no system deps.

    Background: when a regular Swift target depends on a `.systemLibrary`
    target (e.g. GRDB → GRDBSQLite, which wraps `<sqlite3.h>`), the
    Swift compiler always emits `import GRDBSQLite` into the framework's
    `.swiftinterface`. Any consumer that has to rebuild GRDB from its
    textual interface — every consumer whose Swift compiler version
    differs from the build-time one — needs `GRDBSQLite` to resolve as a
    Clang module, or the import fails with `error: no such module
    'GRDBSQLite'` and the framework is unconsumable.

    The system product itself is correctly dropped at planning time
    (`_is_system_only_product`) because there is no Mach-O to build, but
    its modulemap+headers must travel with the parent framework so the
    Clang module can be loaded at consumer compile time. We bundle them
    as a binary-less sibling `<Name>.framework` inside each slice
    directory of the xcframework. swiftc auto-discovers it via `-F`
    framework search — no extra `-I` / `-Xcc` flags required on the
    consumer side, including non-spm-to-xcframework consumers like
    plain Xcode app projects.

    Returns the number of system shims injected (counted once per
    distinct system target, not per slice). Idempotent: existing
    `<Name>.framework` siblings are skipped.
    """
    if not xcframework_path.is_dir():
        return 0

    system_targets = _walk_system_library_target_deps(package, source_targets)
    if not system_targets:
        return 0

    # Discover slice directories. The xcframework Info.plist lists them
    # under AvailableLibraries[].LibraryIdentifier, but we don't need to
    # parse it — every direct subdirectory of the xcframework other than
    # Info.plist is a slice. This avoids a plistlib import in the hot
    # path and matches what xcframework consumers do.
    slice_dirs = [
        p for p in sorted(xcframework_path.iterdir())
        if p.is_dir()
    ]
    if not slice_dirs:
        return 0

    injected = 0
    for sys_target in system_targets:
        src_dir = _system_target_source_dir(sys_target, package.staged_dir)
        if src_dir is None:
            warn(
                f"  System target {sys_target.name!r} has no resolvable "
                f"source dir under {package.staged_dir}; consumers will "
                f"fail to import {sys_target.name}"
            )
            continue
        modulemap = src_dir / "module.modulemap"
        if not modulemap.is_file():
            warn(
                f"  System target {sys_target.name!r} source dir {src_dir} "
                f"has no module.modulemap; skipping shim injection"
            )
            continue
        # Headers are every `.h` file under the system-target source
        # dir, walked recursively. SPM `.systemLibrary` targets usually
        # park a single `shim.h` next to the modulemap, but the modulemap
        # text (which we preserve verbatim aside from the framework
        # qualifier) is free to `header "Sub/foo.h"` into a nested path.
        # We must preserve those relative paths into the shim's Headers/
        # tree so the modulemap's references still resolve at consumer
        # compile time (Codex [P2] follow-up — flat copy silently
        # produced broken shims for nested layouts).
        headers: List[Tuple[Path, Path]] = []
        for p in sorted(src_dir.rglob("*.h")):
            if not p.is_file():
                continue
            rel = p.relative_to(src_dir)
            headers.append((p, rel))
        framework_modulemap = _promote_modulemap_to_framework_form(
            modulemap.read_text()
        )

        any_slice_injected = False
        for slice_dir in slice_dirs:
            shim_fw = slice_dir / f"{sys_target.name}.framework"
            if shim_fw.exists():
                continue  # idempotent — second run is a no-op
            shim_fw_modules = shim_fw / "Modules"
            shim_fw_headers = shim_fw / "Headers"
            shim_fw_modules.mkdir(parents=True, exist_ok=True)
            shim_fw_headers.mkdir(parents=True, exist_ok=True)
            (shim_fw_modules / "module.modulemap").write_text(framework_modulemap)
            for src_header, rel in headers:
                dest = shim_fw_headers / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_header, dest)
            # Sentinel file used by `_is_system_shim_framework` to skip
            # this directory during language classification and
            # surface-check walks. Invisible to Xcode/swiftc consumers.
            (shim_fw / _SYSTEM_SHIM_SENTINEL).write_text(
                "spm-to-xcframework injected this framework as a Clang "
                "module shim for a .systemLibrary SPM target. Do not "
                "delete; removing it makes language classification "
                "misclassify the parent xcframework.\n"
            )
            any_slice_injected = True

        if any_slice_injected:
            injected += 1
            dim(
                f"  Injected system Clang module shim: "
                f"{sys_target.name}.framework (× {len(slice_dirs)} slice(s))"
            )

    return injected


def inject_objc_headers(
    *,
    package: Package,
    product_name: str,
    fw_name: str,
    fw_path: Path,
    verbose: bool,
) -> bool:
    """Copy public ObjC headers + a generated modulemap into a framework
    bundle. No-op if the framework already has `*.h` headers (excluding
    the auto-generated `*-Swift.h` bridge header).

    Returns True iff anything was injected.
    """
    headers_target = fw_path / "Headers"
    if headers_target.is_dir():
        for p in headers_target.glob("*.h"):
            if not p.name.endswith("-Swift.h"):
                verbose_log(verbose, "  Public headers already present in framework")
                return False

    headers_dir = _find_objc_headers_dir(package, product_name, fw_name)
    if headers_dir is None:
        verbose_log(verbose, f"  No ObjC public headers found in source tree for {fw_name}")
        return False

    dim(f"  Injecting ObjC headers ({fw_name})")
    headers_target.mkdir(parents=True, exist_ok=True)

    # SPM convention (legacy 1411-1420): headers may live in a
    # subdirectory named after the module (e.g., Public/FirebaseCore/*.h)
    # or directly in the public headers dir. Pick the scan base
    # accordingly, then walk it recursively and copy each header into
    # `Headers/<relative path>` — preserving subdirectories is required
    # because (a) `#import <Module/Sub/Header.h>` needs the physical
    # path to exist inside Headers/, and (b) a flat copy would silently
    # overwrite same-named headers that live in different subfolders
    # (Codex [P2]).
    module_subdir = headers_dir / fw_name
    scan_base = module_subdir if module_subdir.is_dir() else headers_dir
    copied = 0
    for h in sorted(scan_base.rglob("*.h")):
        if not h.is_file():
            continue
        rel = h.relative_to(scan_base)
        dest = headers_target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(h, dest)
        copied += 1

    if copied == 0:
        verbose_log(verbose, f"  No headers copied from {headers_dir}")
        return False

    _generate_modulemap(fw_path, fw_name)
    verbose_log(verbose, f"  Injected {copied} header(s) + modulemap")
    return True


# ----- create_xcframework + dependency walker ------------------------------


def create_xcframework(
    *,
    output_xcframework: Path,
    device_fw: Path,
    sim_fw: Path,
    verbose: bool,
) -> None:
    """Shell out to `xcodebuild -create-xcframework` for one (device, sim)
    framework pair. Removes any pre-existing output directory first
    (xcodebuild refuses to overwrite). Raises `ExecuteError` on failure.
    """
    if output_xcframework.exists():
        shutil.rmtree(output_xcframework)
    output_xcframework.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "xcodebuild", "-create-xcframework",
        "-framework", str(device_fw),
        "-framework", str(sim_fw),
        "-output", str(output_xcframework),
    ]
    verbose_log(verbose, f"  $ {' '.join(cmd)}")
    cp = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if cp.returncode != 0:
        # Tail the output for diagnostics — same shape the legacy bash
        # uses (`tail -3` on the create-xcframework call).
        tail = "\n".join((cp.stdout or "").rstrip().splitlines()[-10:])
        raise ExecuteError(
            f"xcodebuild -create-xcframework failed for {output_xcframework.name}:\n"
            + (tail or "  (no output)")
        )


_SYSTEM_SHIM_SENTINEL = ".spm-to-xcframework-system-shim"


def _is_system_shim_framework(fw_path: Path) -> bool:
    """True iff `fw_path` is a system Clang module shim framework
    injected by `inject_system_clang_modules`. Detection is by sentinel
    file (`.spm-to-xcframework-system-shim`) written at injection time
    rather than structural inference, because a structural-only test
    can't reliably distinguish a header-only shim from an ObjC framework
    that hasn't been built yet (test fixtures, partial builds). The
    sentinel is invisible to xcframework consumers — Xcode and swiftc
    don't look for it — and writing it costs nothing.
    """
    if not fw_path.is_dir() or not fw_path.name.endswith(".framework"):
        return False
    return (fw_path / _SYSTEM_SHIM_SENTINEL).is_file()


def _read_xcframework_library_paths(xcfw_path: Path) -> Dict[str, str]:
    """Parse `Info.plist` and return `{LibraryIdentifier: LibraryPath}`.

    Returns an empty dict on any failure (missing plist, parse error,
    malformed AvailableLibraries) — the caller falls back to a direct-
    child `*.framework` scan in that case, which is fine for the legacy
    "framework lives directly under the slice" layout.

    The reason we read the plist at all is that `LibraryPath` may be a
    nested path like `Frameworks/Foo.framework`, in which case there is
    no top-level `*.framework` for the direct-child scan to find. Codex
    [P1]: without this lookup, `detect_framework_type` returns Unknown
    and `_verify_one_unit` silently passes those layouts through with
    no language surface checks at all.
    """
    info_plist = xcfw_path / "Info.plist"
    if not info_plist.is_file():
        return {}
    try:
        with info_plist.open("rb") as fh:
            data = plistlib.load(fh)
    except (plistlib.InvalidFileException, OSError, ValueError):
        return {}
    except Exception:  # noqa: BLE001 — defensive: any plist corruption → fall back
        return {}
    available = data.get("AvailableLibraries")
    if not isinstance(available, list):
        return {}
    result: Dict[str, str] = {}
    for entry in available:
        if not isinstance(entry, dict):
            continue
        identifier = entry.get("LibraryIdentifier")
        library_path = entry.get("LibraryPath")
        if isinstance(identifier, str) and isinstance(library_path, str) and library_path:
            result[identifier] = library_path
    return result


def _pick_primary_framework_in_slice(
    slice_dir: Path,
    library_path: Optional[str] = None,
) -> Optional[Path]:
    """Return the primary `.framework/` directory inside an xcframework
    slice.

    Resolution order:
      1. If the caller passed `library_path` (the slice's `LibraryPath`
         from `Info.plist`), resolve `slice_dir / library_path` and
         return it as long as it exists and ends in `.framework`. This
         is the only way to find frameworks that live in nested layouts
         like `Frameworks/Foo.framework` (Codex [P1]).
      2. Otherwise (or if the plist path doesn't resolve on disk), scan
         direct children for `*.framework` directories, skip sibling
         system Clang module shim frameworks injected by
         `inject_system_clang_modules`, and return the alphabetically
         first real framework.

    Falling back to "alphabetically first" matters because real-world
    xcframeworks always have a single primary framework per slice; the
    only reason this function exists at all is to filter out the
    binary-less system shims so they don't pollute language detection.
    """
    if not slice_dir.is_dir():
        return None
    if library_path:
        # Strip any leading "./" but otherwise preserve the relative
        # path verbatim — the plist is authoritative for nested layouts.
        primary = slice_dir / library_path.lstrip("./")
        if primary.is_dir() and primary.name.endswith(".framework"):
            return primary
        # Plist disagreement with disk; fall through to scan rather than
        # silently returning None.
    for entry in sorted(slice_dir.iterdir()):
        if not entry.is_dir() or not entry.name.endswith(".framework"):
            continue
        if _is_system_shim_framework(entry):
            continue
        return entry
    return None


def _iter_primary_framework_paths(xcfw_path: Path) -> Iterable[Path]:
    """Yield every file/dir path inside the primary framework of each
    xcframework slice. Skips sibling system shim frameworks injected by
    `inject_system_clang_modules` (they have no Mach-O binary).

    Reads the xcframework's `Info.plist` once up front to learn each
    slice's `LibraryPath`, so nested layouts (`Frameworks/Foo.framework`)
    are picked up by the same walk that handles the conventional
    `slice/Foo.framework` layout. If the plist is missing or malformed,
    falls back to per-slice direct-child scanning.

    Used by both `detect_framework_type` and the per-slice walk in
    `_verify_one_unit` so language classification and surface checks
    agree on what counts as "the framework" — and stay agnostic to
    augmentations like the system Clang module shim.
    """
    if not xcfw_path.is_dir():
        return
    library_paths = _read_xcframework_library_paths(xcfw_path)
    for slice_dir in sorted(xcfw_path.iterdir()):
        if not slice_dir.is_dir():
            continue
        primary_fw = _pick_primary_framework_in_slice(
            slice_dir,
            library_path=library_paths.get(slice_dir.name),
        )
        if primary_fw is None:
            continue
        yield from primary_fw.rglob("*")


def detect_framework_type(xcfw_path: Path) -> str:
    """Classify an xcframework as Swift / ObjC / Mixed / Unknown by walking
    its primary framework's contents. Used by Execute's per-unit summary
    line and (in Session 5) by the Verify phase. Same logic as the legacy
    bash detect_framework_type, refined to ignore sibling system shim
    frameworks injected for `.systemLibrary` Clang module deps.
    """
    has_swift = False
    has_objc = False
    for path in _iter_primary_framework_paths(xcfw_path):
        name = path.name
        if not has_swift and (name.endswith(".swiftinterface") or name.endswith(".swiftmodule")):
            has_swift = True
        if not has_objc and name.endswith(".h") and not name.endswith("-Swift.h"):
            # Only count headers under a Headers/ directory — that's
            # the SPM convention for public ObjC API. Bridge headers
            # outside Headers/ are framework-internal noise.
            if "Headers" in path.parts:
                has_objc = True
        if has_swift and has_objc:
            break
    if has_swift and has_objc:
        return "Mixed"
    if has_swift:
        return "Swift"
    if has_objc:
        return "ObjC"
    return "Unknown"


def _build_dependency_xcframeworks(
    *,
    unit: BuildUnit,
    package: Package,
    device_slice: ArchiveSlice,
    sim_slice: ArchiveSlice,
    primary_fw_name: str,
    output_dir: Path,
    verbose: bool,
) -> List[DependencyXcframework]:
    """Walk the device archive for `.framework` bundles that aren't the
    primary, inject swiftmodule + ObjC headers into each, and merge them
    into per-dependency xcframeworks under `output_dir`.

    Mirrors the legacy bash `build_dependency_xcframeworks` (lines
    1228-1270). Skips:
      - the primary framework itself (matched by both unit name and
        resolved fw_name in case those differ)
      - dependencies that already have a built `<X>.xcframework` in
        `output_dir` (avoids re-creating Stripe-style sub-frameworks
        that the user explicitly asked for via `--target`)
      - dependencies whose simulator counterpart isn't present (a
        device-only framework can't be merged)

    Each returned `DependencyXcframework` carries an `expected_language`
    derived from the matching target in the Package model, so Verify
    can enforce the same plan-time language contract on deps that it
    enforces on primary units (Codex follow-up).
    """
    products_dir = device_slice.archive_path / "Products"
    if not products_dir.is_dir():
        return []
    built: List[DependencyXcframework] = []
    for fw_path in sorted(products_dir.rglob("*.framework")):
        if not fw_path.is_dir():
            continue
        fw_name = fw_path.stem
        if fw_name in (unit.name, primary_fw_name):
            continue
        if (output_dir / f"{fw_name}.xcframework").is_dir():
            continue
        sim_products = sim_slice.archive_path / "Products"
        sim_fw: Optional[Path] = None
        if sim_products.is_dir():
            for candidate in sim_products.rglob(f"{fw_name}.framework"):
                if candidate.is_dir():
                    sim_fw = candidate
                    break
        if sim_fw is None:
            continue
        dim(f"  Dependency: {fw_name}")
        inject_swiftmodule(
            fw_path=fw_path,
            fw_name=fw_name,
            scheme=fw_name,
            dd_path=device_slice.dd_path,
            variant="device",
            verbose=verbose,
        )
        inject_swiftmodule(
            fw_path=sim_fw,
            fw_name=fw_name,
            scheme=fw_name,
            dd_path=sim_slice.dd_path,
            variant="simulator",
            verbose=verbose,
        )
        # Use the dependency name as both product_name and fw_name when
        # looking for ObjC headers — we don't have a richer mapping.
        inject_objc_headers(
            package=package,
            product_name=fw_name,
            fw_name=fw_name,
            fw_path=fw_path,
            verbose=verbose,
        )
        inject_objc_headers(
            package=package,
            product_name=fw_name,
            fw_name=fw_name,
            fw_path=sim_fw,
            verbose=verbose,
        )
        dep_xcframework = output_dir / f"{fw_name}.xcframework"
        try:
            create_xcframework(
                output_xcframework=dep_xcframework,
                device_fw=fw_path,
                sim_fw=sim_fw,
                verbose=verbose,
            )
        except ExecuteError as exc:
            warn(f"  Failed to create {fw_name}.xcframework (dependency): {exc}")
            continue
        fw_type = detect_framework_type(dep_xcframework)
        success(f"  {fw_name}.xcframework ready (dependency) [{fw_type}]")
        # Resolve the dependency's expected language by matching
        # `fw_name` to a target in the Package model. xcodebuild
        # preserves the target name as the framework name, so this
        # lookup is reliable for internal targets. External
        # packages (transitive deps from a .package(…) clause) are
        # NOT in `package.targets`, so they stay as N/A and Verify
        # falls back to post-hoc detection for those — the plan
        # never had an opinion about their language to begin with.
        dep_target = package.target_by_name(fw_name)
        dep_language = Language.NA
        if dep_target is not None and dep_target.language in (
            Language.SWIFT, Language.OBJC, Language.MIXED
        ):
            dep_language = dep_target.language
        built.append(
            DependencyXcframework(path=dep_xcframework, expected_language=dep_language)
        )
    return built


def _run_one_unit(
    unit: BuildUnit,
    *,
    prepared: PreparedPlan,
    config: Config,
) -> ExecutedUnit:
    """End-to-end Execute pipeline for a single source-mode build unit.

    1. Parallel device + simulator archive.
    2. Locate framework, fall back to static-promote if both slices
       produced a `.a` instead.
    3. Inject swiftmodule + ObjC headers per slice.
    4. Merge slices via `xcodebuild -create-xcframework`.
    5. (Optional) walk dependency frameworks if `--include-deps` is set.
    """
    assert config.work_dir is not None
    work_dir = config.work_dir
    staged_dir = prepared.package.staged_dir

    device_slice, sim_slice = _archive_pair_parallel(
        unit,
        staged_dir=staged_dir,
        work_dir=work_dir,
        min_ios=config.min_ios,
        verbose=config.verbose,
    )

    # If a slice produced a `.a` instead of a `.framework`, run the
    # StaticPromote strategy on it and re-locate the framework. This is
    # the MBProgressHUD path. Per-slice (not "both must be static") so
    # the asymmetric case — one slice ends up with a framework, the
    # other only a static archive — is also handled instead of failing
    # with a misleading "framework missing" error.
    needs_promote = [
        s for s in (device_slice, sim_slice)
        if s.framework_path is None and s.static_lib_path is not None
    ]
    if needs_promote:
        warn(f"  {unit.name}: static archive(s) found — promoting to dynamic framework")
        system_frameworks = detect_system_frameworks(prepared.package, unit.name)
        if system_frameworks:
            verbose_log(
                config.verbose,
                f"  Linking system frameworks: {' '.join(system_frameworks)}",
            )
        for slice_obj in needs_promote:
            promote_static_to_framework(
                static_lib=slice_obj.static_lib_path,
                product=unit.framework_name,
                sdk_name=slice_obj.sdk_name,
                min_ios=config.min_ios,
                archive_path=slice_obj.archive_path,
                system_frameworks=system_frameworks,
                verbose=config.verbose,
            )
            slice_obj.framework_path = _archive_framework_path(
                slice_obj.archive_path, unit.framework_name
            )

    if device_slice.framework_path is None:
        # Last-resort: scan for any .framework in the archive and use it
        # as a best-guess (handles the rare case where the framework
        # binary name differs from both the product name and the target
        # name). Same fallback the legacy bash uses at lines 1097-1110.
        any_fw = None
        products = device_slice.archive_path / "Products"
        if products.is_dir():
            for candidate in sorted(products.rglob("*.framework")):
                if candidate.is_dir():
                    any_fw = candidate
                    break
        if any_fw is not None:
            actual_name = any_fw.stem
            warn(f"  Using {actual_name} instead of {unit.framework_name}")
            device_slice.framework_path = any_fw
            sim_products = sim_slice.archive_path / "Products"
            if sim_products.is_dir():
                for candidate in sim_products.rglob(f"{actual_name}.framework"):
                    if candidate.is_dir():
                        sim_slice.framework_path = candidate
                        break

    if device_slice.framework_path is None:
        raise ExecuteError(
            f"{unit.name}: archive completed but no .framework found anywhere "
            f"under {device_slice.archive_path}/Products/. Static promotion "
            f"either failed or no .a was produced either."
        )
    if sim_slice.framework_path is None:
        raise ExecuteError(
            f"{unit.name}: simulator framework missing under "
            f"{sim_slice.archive_path}/Products/."
        )

    fw_name = device_slice.framework_path.stem
    success(f"  {unit.name}: device {device_slice.framework_path.relative_to(device_slice.archive_path.parent)}")

    # Injection passes — both slices.
    inject_swiftmodule(
        fw_path=device_slice.framework_path,
        fw_name=fw_name,
        scheme=unit.scheme,
        dd_path=device_slice.dd_path,
        variant="device",
        verbose=config.verbose,
        extra_module_names=unit.source_targets,
    )
    inject_swiftmodule(
        fw_path=sim_slice.framework_path,
        fw_name=fw_name,
        scheme=unit.scheme,
        dd_path=sim_slice.dd_path,
        variant="simulator",
        verbose=config.verbose,
        extra_module_names=unit.source_targets,
    )
    inject_objc_headers(
        package=prepared.package,
        product_name=unit.name,
        fw_name=fw_name,
        fw_path=device_slice.framework_path,
        verbose=config.verbose,
    )
    inject_objc_headers(
        package=prepared.package,
        product_name=unit.name,
        fw_name=fw_name,
        fw_path=sim_slice.framework_path,
        verbose=config.verbose,
    )

    output_xcframework = config.output_dir / f"{unit.name}.xcframework"
    info(f"  Creating {unit.name}.xcframework...")
    create_xcframework(
        output_xcframework=output_xcframework,
        device_fw=device_slice.framework_path,
        sim_fw=sim_slice.framework_path,
        verbose=config.verbose,
    )
    # Bundle any `.systemLibrary` Clang modules the build unit depends on
    # as binary-less sibling shim frameworks inside each xcframework
    # slice. This is what makes GRDB-style packages (regular Swift target
    # → .systemLibrary wrapper around <sqlite3.h>) consumable: without
    # the shim, swiftc fails to rebuild the framework's swiftinterface
    # because `import GRDBSQLite` cannot resolve. See
    # `inject_system_clang_modules` for the full rationale.
    inject_system_clang_modules(
        xcframework_path=output_xcframework,
        package=prepared.package,
        source_targets=unit.source_targets,
        verbose=config.verbose,
    )
    fw_type = detect_framework_type(output_xcframework)
    success(f"  {unit.name}.xcframework ready [{fw_type}]")

    dep_xcframeworks: List[Path] = []
    if prepared.plan.include_deps:
        dep_xcframeworks = _build_dependency_xcframeworks(
            unit=unit,
            package=prepared.package,
            device_slice=device_slice,
            sim_slice=sim_slice,
            primary_fw_name=fw_name,
            output_dir=config.output_dir,
            verbose=config.verbose,
        )

    return ExecutedUnit(
        name=unit.name,
        device=device_slice,
        simulator=sim_slice,
        xcframework_path=output_xcframework,
        framework_name=fw_name,
        framework_type=fw_type,
        expected_language=unit.language,
        dependency_xcframeworks=dep_xcframeworks,
    )


def execute_source_plan(
    prepared: PreparedPlan,
    config: Config,
) -> List[ExecutedUnit]:
    """Run the full Execute pipeline for every source-mode unit in the
    plan. Sequential across units (each unit's slices already build in
    parallel internally).

    Skips any unit with `archive_strategy == "copy-artifact"` — binary
    mode is handled by `execute_binary_plan` below. Returns the list of
    ExecutedUnits in plan order. Raises `ExecuteError` on the first
    failure (later units are not attempted, matching Session 3's
    fail-fast contract).
    """
    if config.work_dir is None:
        raise ExecuteError("internal error: config.work_dir was not allocated before Execute")
    archive_units = [u for u in prepared.plan.build_units if u.archive_strategy != "copy-artifact"]
    if not archive_units:
        return []
    bold(f"\nExecuting {len(archive_units)} build unit(s)...")
    config.output_dir.mkdir(parents=True, exist_ok=True)
    results: List[ExecutedUnit] = []
    for unit in archive_units:
        executed = _run_one_unit(unit, prepared=prepared, config=config)
        results.append(executed)
    return results


def execute_binary_plan(
    plan: Plan,
    config: Config,
) -> List[ExecutedUnit]:
    """Copy each `copy-artifact` build unit's xcframework into
    `config.output_dir`.

    The artifact paths come from the planner (which got them from
    `discover_binary_artifacts` during Fetch — same code path Execute
    would otherwise reach for, so the two phases never disagree about
    which xcframeworks exist). The only Execute-side responsibility is
    the copy itself plus a paranoia guard against `__MACOSX` slipping
    in.
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
                is_binary_copy=True,
            )
        )
    return results


# ============================================================================
# --- Output directory manifest (cross-run hygiene) ---
# ============================================================================
#
# Every successful run writes a small JSON manifest into `<output_dir>/`
# recording exactly which xcframework directories the tool produced. The
# next run reads that manifest (before doing anything destructive) so it
# can clean up stale artifacts from prior runs that the new run no longer
# produces. Two safety properties (REFACTOR_PLAN.md Task 3):
#
#   1. Cleanup NEVER happens before Verify passes on every unit in the
#      current run. A failed run leaves prior state untouched so the
#      user's last known-good artifacts are preserved.
#   2. Cleanup only touches directories whose basenames were recorded in
#      the PRIOR manifest — i.e. things this tool produced. User-owned
#      files in `<output_dir>/` are ignored.
#
# The manifest filename starts with a `.` so casual `ls` output stays
# clean, and is namespaced with the tool name so it's unambiguous.

_MANIFEST_FILENAME = ".spm-to-xcframework-manifest.json"
_MANIFEST_VERSION = 1
_MANIFEST_KIND_PRIMARY = "primary"
_MANIFEST_KIND_DEPENDENCY = "dependency"
_MANIFEST_VALID_KINDS = frozenset({_MANIFEST_KIND_PRIMARY, _MANIFEST_KIND_DEPENDENCY})


@dataclass
class ManifestEntry:
    """One recorded xcframework, stored as a relative basename only.

    `name` is ALWAYS a bare basename (e.g. `Alamofire.xcframework`);
    absolute paths, `..`, path separators, and leading `.` are rejected
    at read time. `kind` is either `"primary"` or `"dependency"` so a
    subsequent run can tell top-level artifacts from `--include-deps`
    by-products.
    """

    name: str
    kind: str


@dataclass
class OutputManifest:
    """In-memory view of a parsed manifest. Missing / malformed /
    unknown-version manifests all flatten to an empty OutputManifest —
    callers never need to branch on those cases, they just fall through
    to "no cleanup" by default."""

    entries: List[ManifestEntry] = field(default_factory=list)


def _manifest_entry_basename_ok(name: str) -> bool:
    """True iff `name` is a safe basename-only entry to act on.

    Rejects:
      - empty strings,
      - anything containing a path separator (`/` or `\\`),
      - anything containing a `..` path component,
      - absolute paths,
      - dot-files (leading `.`).

    This is the load-bearing guard that keeps a tampered JSON manifest
    from coercing cleanup into touching paths outside `<output_dir>`.
    """
    if not name:
        return False
    if name.startswith("."):
        return False
    if "/" in name or "\\" in name:
        return False
    try:
        parts = Path(name).parts
    except (TypeError, ValueError):
        return False
    if ".." in parts:
        return False
    if Path(name).is_absolute():
        return False
    return True


def _read_output_manifest(output_dir: Path) -> OutputManifest:
    """Tolerant reader: returns an empty OutputManifest for every
    unhappy-path case (missing file, unparseable JSON, wrong schema
    version, wrong top-level shape). Individually corrupt entries
    inside a valid manifest are filtered out with a warning, but the
    other entries are kept.
    """
    path = output_dir / _MANIFEST_FILENAME
    if not path.is_file():
        return OutputManifest()
    try:
        raw_text = path.read_text()
        data = json.loads(raw_text)
    except (OSError, ValueError) as exc:
        warn(f"Ignoring malformed manifest {path}: {exc}")
        return OutputManifest()
    if not isinstance(data, dict):
        warn(f"Ignoring malformed manifest {path}: top-level is not a dict")
        return OutputManifest()
    version = data.get("version")
    if version != _MANIFEST_VERSION:
        warn(
            f"Ignoring manifest {path}: unknown schema version "
            f"{version!r} (expected {_MANIFEST_VERSION})"
        )
        return OutputManifest()
    raw_entries = data.get("entries")
    if not isinstance(raw_entries, list):
        warn(f"Ignoring manifest {path}: `entries` field is not a list")
        return OutputManifest()
    kept: List[ManifestEntry] = []
    for raw in raw_entries:
        if not isinstance(raw, dict):
            warn(f"Manifest {path}: dropping non-dict entry {raw!r}")
            continue
        name = raw.get("name")
        kind = raw.get("kind")
        if not isinstance(name, str) or not _manifest_entry_basename_ok(name):
            warn(f"Manifest {path}: dropping entry with suspect name {name!r}")
            continue
        if kind not in _MANIFEST_VALID_KINDS:
            warn(
                f"Manifest {path}: dropping entry {name!r} with "
                f"unknown kind {kind!r}"
            )
            continue
        kept.append(ManifestEntry(name=name, kind=kind))
    return OutputManifest(entries=kept)


def _write_output_manifest(
    output_dir: Path,
    entries: Sequence[ManifestEntry],
    *,
    package_source: str,
    package_version: str,
) -> None:
    """Atomic write of the manifest file via temp-file + `os.replace`
    in the same directory. A crash between the temp write and the rename
    leaves the prior manifest intact.
    """
    import datetime

    payload = {
        "version": _MANIFEST_VERSION,
        "tool": "spm-to-xcframework",
        "produced_at": datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec="seconds"
        ),
        "package_source": package_source,
        "package_version": package_version,
        "entries": [
            {"name": entry.name, "kind": entry.kind} for entry in entries
        ],
    }
    path = output_dir / _MANIFEST_FILENAME
    # Use a temp file in the same directory so os.replace is atomic on
    # the same filesystem. tempfile.NamedTemporaryFile with delete=False
    # is overkill here; a deterministic sibling is easier to clean up.
    tmp_path = output_dir / (_MANIFEST_FILENAME + ".tmp")
    try:
        tmp_path.write_text(json.dumps(payload, indent=2) + "\n")
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _cleanup_stale_manifest_entries(
    output_dir: Path,
    old_entries: Sequence[ManifestEntry],
    verified_produced: Set[str],
) -> List[str]:
    """Remove on-disk xcframework directories whose basenames appear in
    `old_entries` but NOT in `verified_produced`. Returns the list of
    basenames that were actually removed (so the caller can log them).

    Safe by construction: only walks `old_entries` (each of which passed
    the basename-only schema check in the reader), so the cleanup step
    can never touch a path outside `<output_dir>`. Entries whose target
    is already gone (user moved/deleted between runs) are silently
    skipped.
    """
    cleaned: List[str] = []
    for entry in old_entries:
        if entry.name in verified_produced:
            continue
        if not _manifest_entry_basename_ok(entry.name):
            # Defence in depth — the reader already filters these, but
            # anyone constructing a manifest in memory might skip that
            # step. Never act on a suspect name.
            continue
        target = output_dir / entry.name
        if not target.exists() and not target.is_symlink():
            continue
        try:
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        except OSError:
            continue
        cleaned.append(entry.name)
    return cleaned


# ============================================================================
# --- Phase 5: Verify ---
# ============================================================================
#
# Strict, per-unit verification of every produced xcframework. The downstream
# .NET binding generator can't consume an xcframework that's missing
# `.swiftinterface` (Swift path) or public headers + modulemap (ObjC path),
# so the contract for this phase is "the build is only successful if every
# planned unit can actually be bound." Verify makes the failure visible
# instead of letting it surface days later in the binding step.
#
# Anything Verify finds is recorded on a `VerifyResult` rather than raised:
# normal failures (broken Info.plist, missing slice, static binary) flow
# through the same data path as the success printer, and `main()` decides
# the exit code from the aggregate. `VerifyError` is reserved for verify
# code crashing on its own — that's the path that gets a Python traceback
# per REWRITE_DESIGN.md §7.


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

    # Fatal #3: at least 2 slices (device + simulator).
    if len(available) < 2:
        fatal.append(
            f"only {len(available)} slice(s) in Info.plist; "
            "expected ≥ 2 (device + simulator)"
        )
        # Don't `return` — we still want the per-slice diagnostics below
        # so the user sees the binary linkage issue alongside the slice
        # count complaint instead of one issue at a time.

    # Fatal #4: every slice's binary must be dynamically linked.
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


# ============================================================================
# --- CLI ---
# ============================================================================


def parse_args(argv: Optional[Sequence[str]] = None) -> Tuple[argparse.Namespace, argparse.ArgumentParser]:
    parser = argparse.ArgumentParser(
        prog="spm-to-xcframework",
        description=(
            "Build xcframeworks from Swift Package Manager packages.\n"
            "Supports Swift, Objective-C, and mixed-language library products."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "EXAMPLES\n"
            "  spm-to-xcframework https://github.com/Alamofire/Alamofire.git -v 5.10.2\n"
            "  spm-to-xcframework https://github.com/kean/Nuke.git -v 12.8.0 -o ./nuke-fw\n"
            "  spm-to-xcframework ./local-package -o ./output --product MyLib\n"
            "  spm-to-xcframework https://github.com/stripe/stripe-ios.git -v 25.6.2 \\\n"
            "      --product Stripe --target StripeCore --target StripeUICore\n"
            "  spm-to-xcframework https://github.com/nicklockwood/iCarousel.git -v 1.8.3 --binary\n"
        ),
    )
    parser.add_argument(
        "package_source",
        nargs="?",
        help="Git URL or local filesystem path to the SPM package",
    )
    parser.add_argument("-v", "--version", dest="version", default="",
                        help="Git tag to check out (required for remote URLs)")
    parser.add_argument("-o", "--output", dest="output", default="./xcframeworks",
                        help="Output directory (default: ./xcframeworks)")
    parser.add_argument("-p", "--product", dest="products", action="append", default=[],
                        help="Build only this product (repeatable)")
    parser.add_argument("-t", "--target", dest="targets", action="append", default=[],
                        help="Build an SPM target not exposed as a .library() product (repeatable)")
    parser.add_argument("--binary", action="store_true",
                        help="Download pre-built xcframeworks from binary SPM targets")
    parser.add_argument("--revision", default=None,
                        help="Verify git tag resolves to this commit SHA before building")
    parser.add_argument("--min-ios", default="15.0",
                        help="Minimum iOS deployment target (default: 15.0)")
    parser.add_argument("--include-deps", action="store_true",
                        help="Also build xcframeworks for transitive dependencies")
    parser.add_argument("--verbose", action="store_true",
                        help="Show full build output")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be built without building")
    parser.add_argument("--keep-work", action="store_true",
                        help="Keep temporary work directory (for debugging)")
    parser.add_argument(
        "--no-cleanup-stale",
        action="store_true",
        help=(
            "Skip cleanup of stale xcframeworks from prior runs this time, "
            "but keep them tracked in the manifest so a subsequent normal "
            "run will clean them."
        ),
    )

    # Session-1-only flag for exploration. Not removed in later sessions —
    # it remains a useful diagnostic.
    parser.add_argument("--inspect-only", action="store_true",
                        help="Run Fetch + Inspect and print the parsed Package model, then exit.")

    ns = parser.parse_args(argv)
    return ns, parser


def _config_from_args(ns: argparse.Namespace) -> Config:
    return Config(
        package_source=ns.package_source,
        user_version=ns.version or "",
        resolved_version=ns.version or "",  # may be rewritten in main()
        output_dir=Path(ns.output).expanduser(),
        product_filters=list(ns.products or []),
        target_filters=list(ns.targets or []),
        revision=ns.revision,
        min_ios=ns.min_ios,
        include_deps=ns.include_deps,
        binary_mode=ns.binary,
        verbose=ns.verbose,
        dry_run=ns.dry_run,
        keep_work=ns.keep_work,
        no_cleanup_stale=ns.no_cleanup_stale,
        inspect_only=ns.inspect_only,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    ns, parser = parse_args(argv)

    if not ns.package_source:
        parser.print_usage(sys.stderr)
        print("Error: package source is required.", file=sys.stderr)
        return 2

    config = _config_from_args(ns)

    # Argument-injection hardening. Reject shapes that could be
    # misinterpreted by downstream `git` invocations before we do any
    # filesystem work. Surfaced as a clean FetchError through the same
    # exit-code path as other user-facing phase errors.
    try:
        _validate_package_source(config.package_source)
        _validate_git_ref(config.user_version, field="version")
        if config.revision is not None:
            _validate_git_ref(config.revision, field="revision")
    except FetchError as exc:
        print(_wrap(f"Error (fetch): {exc}", "red"), file=sys.stderr)
        return exc.exit_code

    if config.binary_mode and config.target_filters:
        die("--target is a source-build escape hatch and cannot be combined with --binary.")

    # Resolve the version tag now, before any clones, so we can populate
    # both user_version and resolved_version (bug 1 fix). Binary mode
    # doesn't clone the vendor repo — it uses SPM's resolver instead —
    # so skip normalization there. `discover_binary_artifacts` strips a
    # leading `v` from user_version so the `exact:` field gets the bare
    # semver SPM requires; SPM then handles the v-prefix fallback when
    # matching against actual git tags.
    if config.is_remote and config.user_version and not config.binary_mode:
        resolved, rewritten = normalize_version_tag(config.package_source, config.user_version)
        if rewritten:
            warn(f"Tag '{config.user_version}' not found, using '{resolved}'")
        config.resolved_version = resolved

    # Allocate work dir.
    work_dir = Path(tempfile.mkdtemp(prefix="spm2xc-"))
    config.work_dir = work_dir
    keep = config.keep_work
    try:
        try:
            if config.binary_mode:
                return _run_binary_mode(config)
            return _run_source_mode(config)
        except _USER_FACING_ERRORS as exc:
            # User-facing phase errors (Fetch, Inspect, Plan): print a clean
            # one-line "Error (<phase>): <msg>" and exit with the
            # phase-specific code. No traceback. See REWRITE_DESIGN.md §7.
            phase = _phase_label_for(exc)
            print(_wrap(f"Error ({phase}): {exc}", "red"), file=sys.stderr)
            return exc.exit_code
        except _BUG_CLASS_ERRORS:
            # Tool bugs (Prepare, Verify) and uncaught exceptions deliberately
            # bubble up so the traceback lands in the user's terminal — these
            # are not their problem to interpret. See REWRITE_DESIGN.md §7.
            raise
        # ExecuteError sits in the middle: surface a clean error without a
        # traceback, but still capture enough context that the user can find
        # the xcresult bundle. The structured handling lives in session 4
        # where the executor is wired up.

    finally:
        if keep:
            dim(f"Work directory retained: {work_dir}")
        else:
            shutil.rmtree(work_dir, ignore_errors=True)


def _finalize_with_verify(
    executed: Sequence[ExecutedUnit],
    config: Config,
    *,
    old_manifest: Optional[OutputManifest] = None,
) -> int:
    """Run Verify against the executed units and print the final summary.

    Returns the exit code main() should use: 0 iff every planned unit
    passed strict verification, otherwise `VerifyError.exit_code` (8).
    Dependency xcframeworks (`--include-deps`) are folded into the verify
    pass alongside the primary build units so they get the same strict
    treatment.

    `old_manifest` is the manifest that was read BEFORE Execute ran
    (or None for callers that don't want cross-run cleanup — notably
    the test suite's direct-finalize tests). When provided AND every
    unit passes Verify, stale entries from the old manifest that
    aren't in the verified-produced set are removed from disk, and a
    fresh manifest is written atomically. A failed-verify run leaves
    both the old manifest AND the old artifacts completely untouched,
    preserving the user's last known-good state.
    """
    units: List[ExecutedUnit] = list(executed)
    # `--include-deps` builds extra xcframeworks under the same output
    # directory; promote each to its own ExecutedUnit so Verify treats
    # them with the same rigour. Each dep carries an `expected_language`
    # derived from the Package target that backed it (if any), so Verify
    # gates its Swift/ObjC/Mixed fatal checks on the plan-time
    # expectation instead of post-hoc detection — closing the
    # mixed-language silent-pass hole for dep artifacts too.
    #
    # Multiple parents can share the same dependency xcframework (e.g. two
    # Stripe modules both depending on StripeCore). Dedupe by resolved
    # path so we don't verify and report the same artifact twice. When a
    # dep shows up under more than one parent with different expected
    # languages (e.g. a Mixed target vs N/A for a non-classifiable one),
    # prefer the more specific signal — Mixed wins over Swift/ObjC wins
    # over N/A — so a single ambiguous parent can't weaken the contract.
    _language_specificity = {
        Language.MIXED: 3,
        Language.SWIFT: 2,
        Language.OBJC: 2,
        Language.NA: 0,
        "": 0,
    }
    seen_dep_paths: Dict[Path, ExecutedUnit] = {}
    for parent in executed:
        for dep in parent.dependency_xcframeworks:
            resolved = dep.path.resolve()
            existing = seen_dep_paths.get(resolved)
            if existing is None:
                new_unit = ExecutedUnit(
                    name=dep.path.stem,
                    xcframework_path=dep.path,
                    framework_name=dep.path.stem,
                    framework_type=detect_framework_type(dep.path),
                    expected_language=dep.expected_language,
                    is_binary_copy=False,
                )
                seen_dep_paths[resolved] = new_unit
                units.append(new_unit)
                continue
            # Already verified — only upgrade the expected_language if
            # the new signal is strictly more specific.
            new_rank = _language_specificity.get(dep.expected_language, 0)
            old_rank = _language_specificity.get(existing.expected_language, 0)
            if new_rank > old_rank:
                existing.expected_language = dep.expected_language

    results = verify_output(units, config.output_dir)
    print_verify_summary(results, config.output_dir)
    if any(not r.passed for r in results):
        # Verify failed: leave the prior manifest AND the prior on-disk
        # artifacts completely untouched. The user's last known-good
        # state is preserved; we do NOT overwrite the manifest with a
        # partial / failing run, and we do NOT clean stale siblings.
        return VerifyError.exit_code

    # Every unit passed strict verify. Compute the "verified-produced"
    # set from `VerifyResult.passed` entries (NOT from plan.build_units
    # — a failed-then-skipped unit must not leak into the manifest).
    verified_produced: Set[str] = {
        r.xcframework_path.name for r in results if r.passed
    }
    # Classify each verified artifact as primary or dependency. Primary
    # = an entry in the original `executed` list (top-level build unit).
    # Dependency = an entry that only showed up via the dep-dedupe loop
    # above. We built `units` as `list(executed) + dep_units`, so
    # cross-reference by xcframework path name.
    primary_names: Set[str] = set()
    for u in executed:
        if u.xcframework_path is not None:
            primary_names.add(u.xcframework_path.name)
    new_entries: List[ManifestEntry] = []
    for r in results:
        if not r.passed:
            continue
        name = r.xcframework_path.name
        kind = (
            _MANIFEST_KIND_PRIMARY
            if name in primary_names
            else _MANIFEST_KIND_DEPENDENCY
        )
        new_entries.append(ManifestEntry(name=name, kind=kind))

    # Cleanup + manifest write. Both operations use the verified-
    # produced set as the single source of truth — so a failed unit
    # can never block cleanup of its stale siblings, and the written
    # manifest reflects only what actually shipped.
    if old_manifest is not None:
        if config.no_cleanup_stale:
            # `--no-cleanup-stale` means "delay cleanup by one run":
            # don't delete stale entries from disk, AND merge them into
            # the new manifest so they remain tool-tracked. A subsequent
            # run without the flag will see them in the manifest and
            # clean them normally.
            existing = {e.name for e in new_entries}
            for entry in old_manifest.entries:
                if entry.name in existing:
                    continue
                # Only keep entries whose on-disk target still exists;
                # a user who manually deleted one shouldn't have it
                # resurrected in the new manifest.
                if not (config.output_dir / entry.name).exists():
                    continue
                new_entries.append(entry)
        else:
            cleaned = _cleanup_stale_manifest_entries(
                config.output_dir,
                old_manifest.entries,
                verified_produced,
            )
            for name in cleaned:
                dim(f"  Cleaned stale xcframework: {name}")

    try:
        _write_output_manifest(
            config.output_dir,
            new_entries,
            package_source=config.package_source,
            package_version=config.user_version,
        )
    except OSError as exc:
        # The manifest write is best-effort: if it fails (disk full,
        # permission error), the run itself has succeeded and we
        # shouldn't flip that to a failure. Warn so the user knows
        # next-run cleanup won't find these artifacts.
        warn(f"Could not write output manifest: {exc}")
    return 0


def _run_source_mode(config: Config) -> int:
    """Source-mode pipeline: Fetch → Inspect → Plan → Prepare → Execute → Verify."""
    source_dir = fetch_source(config)
    staged_dir = stage_source(config, source_dir)
    package = inspect_package(config, staged_dir)

    if config.inspect_only:
        print_package(package)
        return 0

    plan = plan_source_build(config, package)
    for w in plan.warnings:
        warn(w)
    print_plan(plan, package=package, config=config)

    if config.dry_run:
        return 0

    # Read the prior run's manifest BEFORE Execute writes anything. The
    # content is kept in memory only; the manifest file on disk is
    # untouched until finalize succeeds. A missing/malformed manifest
    # flattens to empty — no cross-run cleanup, same as a first run.
    old_manifest = _read_output_manifest(config.output_dir)

    prepared = prepare(staged_dir, plan, verbose=config.verbose)
    executed = execute_source_plan(prepared, config)
    return _finalize_with_verify(executed, config, old_manifest=old_manifest)


def _run_binary_mode(config: Config) -> int:
    """Binary-mode pipeline: discover_binary_artifacts → Plan → Execute → Verify.

    Binary mode skips Inspect/Plan-as-source and the Prepare phase
    entirely; the planner only needs the list of `BinaryArtifact` records
    that `discover_binary_artifacts` discovered during Fetch, and Execute
    just copies the surviving artifacts into `output_dir`. Verify still
    applies the same strict per-unit checks — that's the §5.5 plistlib
    catch for any AppleDouble ghost that slipped past Fetch's
    `__MACOSX` pruning.
    """
    if config.inspect_only:
        die("--inspect-only is not supported for --binary.")

    artifacts = discover_binary_artifacts(config)
    plan = plan_binary_build(config, artifacts)
    for w in plan.warnings:
        warn(w)
    print_plan(plan, package=None, config=config)

    if config.dry_run:
        return 0

    # Same cross-run hygiene as source mode — read the prior manifest
    # before Execute touches anything, defer cleanup until Verify passes.
    old_manifest = _read_output_manifest(config.output_dir)

    executed = execute_binary_plan(plan, config)
    return _finalize_with_verify(executed, config, old_manifest=old_manifest)


# Phase classification for main()'s exception handler. Stays in sync with
# the design's "user-facing vs tool-bug" split (§7), but the taxonomy is
# more fine-grained than the base phase classes: `PrepareError` and
# `VerifyError` are each split into a `*UserError` variant (user's
# manifest or invocation was bad — clean one-line message) and a `*Bug`
# variant (tool invariant violation — traceback). The user-facing tuple
# lists the specific classes that take the clean message path so the
# handler never catches a genuine bug just because it inherits from the
# base phase class.
_USER_FACING_ERRORS = (
    FetchError,
    InspectError,
    PlanError,
    PrepareUserError,
    ExecuteError,
    VerifyUserError,
)
_BUG_CLASS_ERRORS = (PrepareBug, VerifyBug)


def _phase_label_for(exc: SpmToXcframeworkError) -> str:
    if isinstance(exc, FetchError):
        return "fetch"
    if isinstance(exc, InspectError):
        return "inspect"
    if isinstance(exc, PlanError):
        return "plan"
    if isinstance(exc, PrepareError):
        return "prepare"
    if isinstance(exc, ExecuteError):
        return "execute"
    if isinstance(exc, VerifyError):
        return "verify"
    return "unknown"


if __name__ == "__main__":
    sys.exit(main())
