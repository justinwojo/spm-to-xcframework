#!/usr/bin/env python3
"""spm-to-xcframework — Build xcframeworks from Swift Package Manager packages.

This is the Python rewrite of the historical bash script. Session 1 of the
rewrite (see REWRITE_DESIGN.md §11) covers Fetch and Inspect only:

    spm-to-xcframework <package-url-or-path> --version <ver> --inspect-only

The Plan, Prepare, Execute, and Verify phases are deliberately stubbed; this
file is intentionally half-built between sessions. The CLI surface is the
final one and the dataclasses + error hierarchy are the ones the later
phases will hang off.

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
    """Manifest editing failed validation. Treated as a *tool bug* — the
    planner produced an edit list that Prepare could not safely apply, or
    the round-trip validator caught a regression. Surfaced with a diff
    plus the failed assertions."""

    exit_code = 6


class ExecuteError(SpmToXcframeworkError):
    """xcodebuild (or downstream tooling) failed. Surfaced with the
    parsed xcresult plus the top N errors per target."""

    exit_code = 7


class VerifyError(SpmToXcframeworkError):
    """Output xcframeworks are missing, malformed, or fail strict
    type-specific checks (no swiftinterface for a Swift unit, no
    headers for an ObjC unit, etc.)."""

    exit_code = 8


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
    framework_type: str = ""              # "Swift" / "ObjC" / "Mixed" / "Unknown" — for summary printing
    is_binary_copy: bool = False
    dependency_xcframeworks: List[Path] = field(default_factory=list)


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
    cp = _git(["ls-remote", "--tags", url, f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"])
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
    cp = _git(["ls-remote", "--tags", url, f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"])
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
        cp = _git(
            [
                "clone",
                "--depth",
                "1",
                "--branch",
                config.resolved_version,
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


def _assert_no_unsupported_swift_constructs(text: str) -> None:
    """Fail loudly if `text` contains Swift constructs the balanced-paren
    walker can't reason about.

    The walker handles double-quoted strings (with backslash escapes),
    line comments (//) and block comments. It does NOT handle Swift raw
    strings (#"..."#), multi-line triple-quoted strings, or string
    interpolation: parens inside an interpolated expression would fool
    the depth counter, and unescaped quotes inside a raw string would
    confuse the string-skip state.

    Real-world Package.swift files we've seen never use these inside
    .library(...) argument lists or products: [...] arrays, but if a
    future manifest does, silently mis-parsing would corrupt the edit. We
    raise a targeted PrepareError up-front so the user gets a clear error
    instead of an opaque round-trip mismatch.

    Cheap heuristic: a substring scan. The few false positives this might
    flag (e.g. doc comments mentioning these constructs) aren't worth the
    cost of a full Swift tokenizer.
    """
    if '#"' in text:
        raise PrepareError(
            "Package.swift uses Swift raw string literals (`#\"...\"#`), "
            "which the balanced-paren walker doesn't understand. "
            "File a bug if this needs to be supported."
        )
    if '"""' in text:
        raise PrepareError(
            "Package.swift uses Swift triple-quoted strings (`\"\"\"`), "
            "which the balanced-paren walker doesn't understand. "
            "File a bug if this needs to be supported."
        )
    if '\\(' in text:
        raise PrepareError(
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
        # Block comment: skip to */
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            close = text.find("*/", i + 2)
            if close == -1:
                return -1
            i = close + 2
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
            raise PrepareError(
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
        raise PrepareError(
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
        raise PrepareError(
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
        raise PrepareError(
            "add_synthetic_library: could not locate `products:` array in "
            "manifest. The planner emitted an `add_synthetic_library` edit "
            "for a package without a products section."
        )
    if close_bracket == -1:
        raise PrepareError(
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


def _select_active_manifest(staged_dir: Path) -> Path:
    """Pick the Package.swift file SPM will actually read for this toolchain.

    Mirrors SPM's selection rule: `Package@swift-X.Y[.Z].swift` is preferred
    over `Package.swift` when its version is `<=` the toolchain version, and
    the highest matching version wins. The patch component matters: if both
    `Package@swift-5.9.swift` and `Package@swift-5.9.1.swift` exist, the
    `.1` variant wins on a 5.9.1+ toolchain (and is filtered on 5.9.0).

    Falls back to `Package.swift` if no version-specific manifest applies
    (or the toolchain version can't be parsed).
    """
    base = staged_dir / "Package.swift"
    tc = _swift_toolchain_version()
    if tc is None:
        return base
    best: Optional[Tuple[Tuple[int, int, int], Path]] = None
    pat = re.compile(r"^Package@swift-(\d+)(?:\.(\d+))?(?:\.(\d+))?\.swift$")
    for candidate in staged_dir.glob("Package@swift-*.swift"):
        m = pat.match(candidate.name)
        if not m:
            continue
        cand = (int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0))
        if cand <= tc and (best is None or cand > best[0]):
            best = (cand, candidate)
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
        raise PrepareError(f"No Package.swift at {manifest_path}")

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
        raise PrepareError(
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
        raise PrepareError(
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
            raise PrepareError(
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
        # behaviour: only walk one level deep, and only via `byName`
        # (which is the dump-package shape for an internal target dep).
        raw = raw_targets_by_name.get(tname)
        if raw is None:
            continue
        for dep in raw.get("dependencies", []) or []:
            if not isinstance(dep, dict):
                continue
            by_name = dep.get("byName")
            if isinstance(by_name, list) and by_name and isinstance(by_name[0], str):
                dep_name = by_name[0]
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

    def headers_dir_for(target_name: str) -> Optional[Path]:
        target = package.target_by_name(target_name)
        if target is None or not target.public_headers_path:
            return None
        target_path = target.path or _default_target_path(target.name, target.kind)
        if not target_path:
            return None
        full_path = package.staged_dir / target_path / target.public_headers_path
        if not full_path.is_dir():
            return None
        # Has at least one .h file (recursively).
        for _root, _dirs, files in os.walk(full_path):
            if any(f.endswith(".h") for f in files):
                return full_path
        return None

    product_match: Optional[Path] = None
    any_match: Optional[Path] = None
    for tname in product_targets:
        d = headers_dir_for(tname)
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

    # First-level dependencies of direct product targets.
    dep_fw_match: Optional[Path] = None
    dep_product_match: Optional[Path] = None
    dep_any_match: Optional[Path] = None
    for tname in product_targets:
        raw = raw_targets_by_name.get(tname)
        if raw is None:
            continue
        for dep in raw.get("dependencies", []) or []:
            if not isinstance(dep, dict):
                continue
            by_name = dep.get("byName")
            if not (isinstance(by_name, list) and by_name and isinstance(by_name[0], str)):
                continue
            dep_name = by_name[0]
            d = headers_dir_for(dep_name)
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
        text = (
            f"framework module {fw_name} {{\n"
            f"  umbrella header \"{fw_name}.h\"\n"
            f"  export *\n"
            f"  module * {{ export * }}\n"
            f"}}\n"
        )
    else:
        header_names = sorted(p.name for p in headers_dir.glob("*.h"))
        lines = [f"framework module {fw_name} {{"]
        for h in header_names:
            lines.append(f"  header \"{h}\"")
        lines.append("  export *")
        lines.append("}")
        text = "\n".join(lines) + "\n"
    (modules_dir / "module.modulemap").write_text(text)


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

    # SPM convention (legacy 1411-1420): headers may be in a subdirectory
    # named after the module (e.g., Public/FirebaseCore/*.h) or directly
    # in the public headers dir.
    module_subdir = headers_dir / fw_name
    copied = 0
    if module_subdir.is_dir():
        for h in module_subdir.rglob("*.h"):
            if h.is_file():
                shutil.copy2(h, headers_target / h.name)
                copied += 1
    else:
        for h in headers_dir.glob("*.h"):
            if h.is_file():
                shutil.copy2(h, headers_target / h.name)
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


def detect_framework_type(xcfw_path: Path) -> str:
    """Classify an xcframework as Swift / ObjC / Mixed / Unknown by walking
    its contents. Used by Execute's per-unit summary line and (in
    Session 5) by the Verify phase. Same logic as the legacy bash
    detect_framework_type."""
    has_swift = False
    has_objc = False
    if xcfw_path.is_dir():
        for path in xcfw_path.rglob("*"):
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
) -> List[Path]:
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
    """
    products_dir = device_slice.archive_path / "Products"
    if not products_dir.is_dir():
        return []
    built: List[Path] = []
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
        built.append(dep_xcframework)
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
                is_binary_copy=True,
            )
        )
    return results


# ============================================================================
# --- Phase 5: Verify (stub) ---
# ============================================================================


def verify_output(config: Config, plan: Plan) -> None:
    """Stub for session 5."""
    raise NotImplementedError("Verify phase is implemented in session 5.")


# ============================================================================
# --- Self-test ---
# ============================================================================
#
# Two-mode self-test per REWRITE_DESIGN.md §9:
#   --self-test=fast → snapshot fixtures only, no swift / no network.
#   --self-test      → fast checks + Fetch integration against testdata/.
#
# Each test is a tiny function that asserts via raise; the harness counts
# passes/failures and exits non-zero on any failure. No third-party deps.

# --- Snapshot fixtures ----------------------------------------------------
#
# These are minimised snapshots of `swift package dump-package` for real
# packages, embedded as Python literals so the fast self-test can run
# without invoking swift. Schema captured against Xcode 26.2; the fields
# the parser cares about are stable across recent SPM versions.

NUKE_DUMP_SNAPSHOT: dict = {
    "name": "Nuke",
    "toolsVersion": {"_version": "5.6.0"},
    "platforms": [
        {"options": [], "platformName": "ios", "version": "13.0"},
        {"options": [], "platformName": "macos", "version": "10.15"},
    ],
    "products": [
        {"name": "Nuke", "type": {"library": ["automatic"]}, "targets": ["Nuke"]},
        {"name": "NukeUI", "type": {"library": ["automatic"]}, "targets": ["NukeUI"]},
    ],
    "targets": [
        {"name": "Nuke", "type": "regular", "path": None, "publicHeadersPath": None, "dependencies": []},
        {"name": "NukeUI", "type": "regular", "path": None, "publicHeadersPath": None, "dependencies": [{"byName": ["Nuke", None]}]},
    ],
}

GRDB_DUMP_SNAPSHOT: dict = {
    "name": "GRDB",
    "toolsVersion": {"_version": "6.1.0"},
    "platforms": [
        {"options": [], "platformName": "ios", "version": "13.0"},
    ],
    "products": [
        {"name": "GRDBSQLite", "type": {"library": ["automatic"]}, "targets": ["GRDBSQLite"]},
        {"name": "GRDB", "type": {"library": ["automatic"]}, "targets": ["GRDB"]},
        {"name": "GRDB-dynamic", "type": {"library": ["dynamic"]}, "targets": ["GRDB"]},
    ],
    "targets": [
        {"name": "GRDBSQLite", "type": "system", "path": None, "publicHeadersPath": None, "dependencies": []},
        {"name": "GRDB", "type": "regular", "path": "GRDB", "publicHeadersPath": None, "dependencies": [{"target": ["GRDBSQLite", None]}]},
        {"name": "GRDBTests", "type": "test", "path": "Tests", "publicHeadersPath": None, "dependencies": []},
    ],
}

# Stripe's shape exercises the --target escape hatch: the `Stripe` library
# product is the only exposed product in this slice of the real dump, and
# the other 11 frameworks (StripeCore, StripeUICore, …) are internal
# targets that downstream consumers reach via --target. Only the subset
# used by the planner tests is included here — the real Stripe package
# has a dozen more targets.
STRIPE_DUMP_SNAPSHOT: dict = {
    "name": "stripe-ios",
    "toolsVersion": {"_version": "5.7.0"},
    "platforms": [
        {"options": [], "platformName": "ios", "version": "13.0"},
    ],
    "products": [
        {"name": "Stripe", "type": {"library": ["automatic"]}, "targets": ["Stripe"]},
        {"name": "StripePayments", "type": {"library": ["automatic"]}, "targets": ["StripePayments"]},
        {"name": "StripePaymentSheet", "type": {"library": ["automatic"]}, "targets": ["StripePaymentSheet"]},
    ],
    "targets": [
        {"name": "Stripe", "type": "regular", "path": "Stripe/StripeiOS", "publicHeadersPath": None,
         "dependencies": [{"byName": ["StripeCore", None]}, {"byName": ["StripePayments", None]}, {"byName": ["StripeApplePay", None]}]},
        {"name": "StripeCore", "type": "regular", "path": "StripeCore/StripeCore", "publicHeadersPath": None,
         "dependencies": []},
        {"name": "StripeUICore", "type": "regular", "path": "StripeUICore/StripeUICore", "publicHeadersPath": None,
         "dependencies": [{"byName": ["StripeCore", None]}]},
        {"name": "StripePayments", "type": "regular", "path": "StripePayments/StripePayments", "publicHeadersPath": None,
         "dependencies": [{"byName": ["StripeCore", None]}]},
        {"name": "StripePaymentSheet", "type": "regular", "path": "StripePaymentSheet/StripePaymentSheet",
         "publicHeadersPath": None,
         "dependencies": [{"byName": ["StripeCore", None]}, {"byName": ["StripeUICore", None]}, {"byName": ["StripePayments", None]}]},
        # A binary target — confirms the planner refuses to synthesize
        # a library for it.
        {"name": "Stripe3DS2", "type": "binary", "path": None, "publicHeadersPath": None, "dependencies": []},
    ],
}

# Alamofire's interesting shape is "automatic + already-dynamic on the same
# target", which session 2's planner needs to handle without double-patching.
ALAMOFIRE_DUMP_SNAPSHOT: dict = {
    "name": "Alamofire",
    "toolsVersion": {"_version": "5.3.0"},
    "platforms": [
        {"options": [], "platformName": "ios", "version": "13.0"},
    ],
    "products": [
        {"name": "Alamofire", "type": {"library": ["automatic"]}, "targets": ["Alamofire"]},
        {"name": "AlamofireDynamic", "type": {"library": ["dynamic"]}, "targets": ["Alamofire"]},
    ],
    "targets": [
        {"name": "Alamofire", "type": "regular", "path": "Source", "publicHeadersPath": None, "dependencies": []},
        {"name": "AlamofireTests", "type": "test", "path": "Tests", "publicHeadersPath": None, "dependencies": []},
    ],
}


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _selftest_parse_nuke() -> None:
    raw, products, targets, platforms, name, tv = _parse_dump(NUKE_DUMP_SNAPSHOT)
    _assert(name == "Nuke", f"name was {name!r}")
    _assert(tv == "5.6.0", f"tools_version was {tv!r}")
    _assert(len(platforms) == 2, f"platforms count was {len(platforms)}")
    _assert(platforms[0].name == "ios", f"first platform was {platforms[0].name!r}")
    _assert(len(products) == 2, f"product count was {len(products)}")
    _assert(products[0].name == "Nuke", f"first product was {products[0].name!r}")
    _assert(products[0].linkage == Linkage.AUTOMATIC, f"linkage was {products[0].linkage!r}")
    _assert(len(targets) == 2, f"target count was {len(targets)}")
    _assert(targets[0].kind == TargetKind.REGULAR, f"target kind was {targets[0].kind!r}")


def _selftest_parse_grdb_skips_systems_correctly() -> None:
    raw, products, targets, platforms, name, tv = _parse_dump(GRDB_DUMP_SNAPSHOT)
    _assert(name == "GRDB", f"name was {name!r}")
    # Three products: GRDBSQLite (system), GRDB (automatic), GRDB-dynamic (dynamic)
    _assert(len(products) == 3, f"expected 3 products, got {len(products)}")
    by_name = {p.name: p for p in products}
    _assert(by_name["GRDBSQLite"].linkage == Linkage.AUTOMATIC,
            "GRDBSQLite product linkage should still parse as 'automatic' (it's the *backing target* that's system)")
    _assert(by_name["GRDB"].linkage == Linkage.AUTOMATIC, "GRDB linkage")
    _assert(by_name["GRDB-dynamic"].linkage == Linkage.DYNAMIC, "GRDB-dynamic linkage")
    # Targets: system + regular + test
    by_tname = {t.name: t for t in targets}
    _assert(by_tname["GRDBSQLite"].kind == TargetKind.SYSTEM, "GRDBSQLite target kind")
    _assert(by_tname["GRDB"].kind == TargetKind.REGULAR, "GRDB target kind")
    _assert(by_tname["GRDBTests"].kind == TargetKind.TEST, "GRDBTests target kind")
    # The test target should record its dependency-by-target.
    _assert(by_tname["GRDB"].dependencies == ["GRDBSQLite"], "GRDB target deps")


def _mk_package_from_snapshot(snap: dict, *, schemes: Optional[List[str]] = None) -> Package:
    """Wrap `_parse_dump` → Package for use in planner fixtures. Targets
    are returned with `language == N/A` (we don't have a real filesystem
    to scan); individual tests set the languages they care about.
    """
    raw, products, targets, platforms, name, tv = _parse_dump(snap)
    return Package(
        name=name,
        tools_version=tv,
        platforms=platforms,
        products=products,
        targets=targets,
        schemes=list(schemes or []),
        raw_dump=raw,
        staged_dir=Path("/tmp/fake-staged"),
    )


def _selftest_parse_alamofire_already_dynamic() -> None:
    raw, products, targets, platforms, name, tv = _parse_dump(ALAMOFIRE_DUMP_SNAPSHOT)
    _assert(name == "Alamofire", f"name was {name!r}")
    by_name = {p.name: p for p in products}
    _assert("AlamofireDynamic" in by_name, "missing AlamofireDynamic product")
    _assert(
        by_name["AlamofireDynamic"].linkage == Linkage.DYNAMIC,
        f"AlamofireDynamic should report dynamic, got {by_name['AlamofireDynamic'].linkage}",
    )
    _assert(
        by_name["Alamofire"].linkage == Linkage.AUTOMATIC,
        f"Alamofire should report automatic, got {by_name['Alamofire'].linkage}",
    )


def _selftest_linkage_decoder() -> None:
    _assert(_parse_linkage({"library": ["dynamic"]}) == Linkage.DYNAMIC, "dynamic decode")
    _assert(_parse_linkage({"library": ["automatic"]}) == Linkage.AUTOMATIC, "automatic decode")
    _assert(_parse_linkage({"library": ["static"]}) == Linkage.STATIC, "static decode")
    _assert(_parse_linkage({"executable": None}) is None, "executables are dropped")
    _assert(_parse_linkage({"plugin": None}) is None, "plugins are dropped")
    _assert(_parse_linkage({"library": []}) == Linkage.AUTOMATIC, "empty library list defaults to automatic")
    _assert(_parse_linkage({"library": ["whatever"]}) == Linkage.UNKNOWN, "unknown library variant")


def _selftest_target_kind_decoder() -> None:
    _assert(_parse_target_kind("regular") == TargetKind.REGULAR, "regular")
    _assert(_parse_target_kind("system") == TargetKind.SYSTEM, "system")
    _assert(_parse_target_kind("test") == TargetKind.TEST, "test")
    _assert(_parse_target_kind("plugin") == TargetKind.PLUGIN, "plugin")
    _assert(_parse_target_kind("nonsense") == TargetKind.UNKNOWN, "fallback to unknown")
    _assert(_parse_target_kind(None) == TargetKind.UNKNOWN, "non-string falls back")


def _selftest_default_target_path() -> None:
    _assert(_default_target_path("Foo", TargetKind.REGULAR) == "Sources/Foo", "regular default")
    _assert(_default_target_path("FooTests", TargetKind.TEST) == "Tests/FooTests", "test default")


def _selftest_dependency_parser() -> None:
    deps = _parse_dependencies([
        {"byName": ["A", None]},
        {"target": ["B", None]},
        {"product": ["C", "PkgC", None, None]},
        {"weird": ["ignored"]},
    ])
    _assert(deps == ["A", "B", "C"], f"deps were {deps}")


def _selftest_toxic_filter() -> None:
    _assert(_is_toxic_entry(".git"), ".git")
    _assert(_is_toxic_entry(".build"), ".build")
    _assert(_is_toxic_entry("DerivedData"), "DerivedData")
    _assert(_is_toxic_entry("node_modules"), "node_modules")
    _assert(_is_toxic_entry("Foo.xcodeproj"), "xcodeproj")
    _assert(_is_toxic_entry("Foo.xcworkspace"), "xcworkspace")
    _assert(not _is_toxic_entry("Sources"), "Sources should not be toxic")
    _assert(not _is_toxic_entry("Package.swift"), "Package.swift should not be toxic")


def _selftest_language_counter(tmp_root: Path) -> None:
    """Synthetic file tree → confirm Swift / ObjC / Mixed / NA classify
    correctly. Uses a temp dir, no fixture file dependency."""
    base = tmp_root / "lang_counter"
    base.mkdir()

    swift_only = base / "swift_only"
    swift_only.mkdir()
    (swift_only / "Foo.swift").write_text("// swift")
    s, o = _count_source_files(swift_only)
    _assert(s == 1 and o == 0, f"swift-only got ({s},{o})")

    objc_only = base / "objc_only"
    objc_only.mkdir()
    (objc_only / "Bar.m").write_text("// m")
    (objc_only / "Bar.h").write_text("// h")  # .h counts toward objc per design §5.1
    s, o = _count_source_files(objc_only)
    _assert(s == 0 and o == 2, f"objc-only got ({s},{o})")

    # Header-only ObjC target (umbrella framework shape).
    headers_only = base / "headers_only"
    headers_only.mkdir()
    (headers_only / "Public.h").write_text("// h")
    s, o = _count_source_files(headers_only)
    _assert(s == 0 and o == 1, f"headers-only got ({s},{o})")

    mixed = base / "mixed"
    mixed.mkdir()
    (mixed / "X.swift").write_text("// swift")
    (mixed / "Y.mm").write_text("// mm")
    s, o = _count_source_files(mixed)
    _assert(s == 1 and o == 1, f"mixed got ({s},{o})")

    # Tests/ skip pruning
    skipper = base / "skipper"
    skipper.mkdir()
    (skipper / "X.swift").write_text("// swift")
    tests = skipper / "Tests"
    tests.mkdir()
    (tests / "Junk.swift").write_text("// swift")
    s, o = _count_source_files(skipper)
    _assert(s == 1 and o == 0, f"skip-tests got ({s},{o}); Tests/ should be pruned")


def _selftest_minimixed_fetch_integration() -> None:
    """Full Fetch integration against testdata/MiniMixed.

    Verifies the inclusion-by-default + toxic-exclusion staging rule:
      - Sources/, Package.swift, etc. land in staged/
      - MiniMixed.xcodeproj is excluded
      - Sources/MiniSwift/Excluded.txt (per package's `exclude:` list)
        gets removed by the second pass
    """
    repo_root = Path(__file__).resolve().parent
    fixture = repo_root / "testdata" / "MiniMixed"
    if not (fixture / "Package.swift").is_file():
        raise AssertionError(f"Fixture missing: {fixture}")

    with tempfile.TemporaryDirectory(prefix="spm2xc-selftest-") as tmp:
        config = Config(
            package_source=str(fixture),
            user_version="",
            resolved_version="",
            work_dir=Path(tmp),
        )
        source_dir = fetch_source(config)
        _assert((source_dir / "Package.swift").is_file(), "fetch_source did not land Package.swift")
        _assert((source_dir / "MiniMixed.xcodeproj").is_dir(),
                "fetch_source should preserve xcodeproj in source/ (only staging strips it)")

        staged = stage_source(config, source_dir)
        _assert((staged / "Package.swift").is_file(), "Package.swift missing in staged/")
        _assert(not (staged / "MiniMixed.xcodeproj").exists(),
                "xcodeproj should NOT be in staged/")
        _assert((staged / "Sources" / "MiniSwift" / "MiniSwift.swift").is_file(),
                "Swift source missing in staged/")
        _assert((staged / "Sources" / "MiniObjC" / "MiniObjC.m").is_file(),
                "ObjC source missing in staged/")
        _assert(not (staged / "Sources" / "MiniSwift" / "Excluded.txt").exists(),
                "Excluded.txt should have been removed by the second-pass exclude cleanup")

        # Inspect against the staged dir (the test exercises the full Fetch
        # → Inspect contract — including dump-package, language scan, and
        # scheme discovery — against a real swift toolchain).
        pkg = inspect_package(config, staged)
        _assert(pkg.name == "MiniMixed", f"package name was {pkg.name!r}")
        _assert(len(pkg.products) == 3, f"expected 3 products, got {len(pkg.products)}")
        by_name = {p.name: p for p in pkg.products}
        _assert("MiniSwift" in by_name and "MiniObjC" in by_name and "MiniMixed" in by_name,
                f"missing expected product, got {list(by_name.keys())}")
        for p in pkg.products:
            _assert(p.linkage == Linkage.AUTOMATIC, f"{p.name} linkage was {p.linkage!r}")

        # Language classification
        lang_by_target = {t.name: t.language for t in pkg.targets}
        _assert(lang_by_target.get("MiniSwift") == Language.SWIFT,
                f"MiniSwift language was {lang_by_target.get('MiniSwift')!r}")
        _assert(lang_by_target.get("MiniObjC") == Language.OBJC,
                f"MiniObjC language was {lang_by_target.get('MiniObjC')!r}")
        _assert(lang_by_target.get("MiniMixed") == Language.MIXED,
                f"MiniMixed language was {lang_by_target.get('MiniMixed')!r}")


# --- Planner self-tests ---------------------------------------------------


def _selftest_scheme_resolver() -> None:
    # 1. exact wins over -Package and iOS suffixes
    _assert(
        resolve_scheme("GRDB", ["GRDB", "GRDB-dynamic", "GRDB-Package"]) == "GRDB",
        "exact match should win over -Package form",
    )
    # 2. case-insensitive exact
    _assert(
        resolve_scheme("grdb", ["GRDB"]) == "GRDB",
        "case-insensitive exact match",
    )
    # 3. -Package fallback
    _assert(
        resolve_scheme("Nuke", ["Nuke-Package"]) == "Nuke-Package",
        "-Package fallback",
    )
    # 4. iOS suffix variants
    _assert(resolve_scheme("Foo", ["Foo iOS"]) == "Foo iOS", "Foo iOS")
    _assert(resolve_scheme("Foo", ["Foo-iOS"]) == "Foo-iOS", "Foo-iOS")
    _assert(resolve_scheme("Foo", ["Foo (iOS)"]) == "Foo (iOS)", "Foo (iOS)")
    # 5. final fallback: returns the product name unchanged
    _assert(resolve_scheme("Nothing", []) == "Nothing", "empty schemes")
    _assert(
        resolve_scheme("Nothing", ["SomethingElse"]) == "Nothing",
        "no candidate matches",
    )


def _selftest_planner_grdb() -> None:
    """GRDB: force_dynamic on GRDB, NO force_dynamic on GRDB-dynamic,
    GRDBSQLite dropped entirely because it's a system-target wrapper.
    """
    pkg = _mk_package_from_snapshot(
        GRDB_DUMP_SNAPSHOT,
        schemes=["GRDB", "GRDB-dynamic", "GRDB-Package"],
    )
    config = Config(
        package_source="https://github.com/groue/GRDB.swift.git",
        user_version="7.9.0",
        resolved_version="v7.9.0",
    )
    plan = plan_source_build(config, pkg)

    names = [bu.name for bu in plan.build_units]
    _assert("GRDB" in names, f"GRDB must be built; got {names}")
    _assert("GRDB-dynamic" in names, f"GRDB-dynamic must be built; got {names}")
    _assert("GRDBSQLite" not in names, f"GRDBSQLite must be skipped; got {names}")

    edits = {(e.kind, e.product_name) for e in plan.package_swift_edits}
    _assert(
        ("force_dynamic", "GRDB") in edits,
        f"force_dynamic on GRDB missing; got {edits}",
    )
    _assert(
        ("force_dynamic", "GRDB-dynamic") not in edits,
        f"must not force_dynamic the already-dynamic GRDB-dynamic; got {edits}",
    )
    _assert(
        ("force_dynamic", "GRDBSQLite") not in edits,
        "must not emit force_dynamic for the skipped system product",
    )

    skipped_names = [n for n, _ in plan.skipped]
    _assert(
        skipped_names == ["GRDBSQLite"],
        f"expected GRDBSQLite in skipped list; got {plan.skipped}",
    )

    # Scheme resolution: GRDB should pick the literal scheme, not
    # GRDB-Package. This is the session-1 inversion noted in the brief.
    grdb_bu = next(bu for bu in plan.build_units if bu.name == "GRDB")
    _assert(
        grdb_bu.scheme == "GRDB",
        f"GRDB scheme should be literal 'GRDB' not '{grdb_bu.scheme}'",
    )


def _selftest_planner_alamofire() -> None:
    """Alamofire-shape: two products over the same target — plan both,
    but only force_dynamic the non-dynamic one."""
    pkg = _mk_package_from_snapshot(
        ALAMOFIRE_DUMP_SNAPSHOT,
        schemes=["Alamofire", "AlamofireDynamic", "Alamofire-Package"],
    )
    config = Config(
        package_source="https://github.com/Alamofire/Alamofire.git",
        user_version="5.10.2",
        resolved_version="5.10.2",
    )
    plan = plan_source_build(config, pkg)

    names = {bu.name for bu in plan.build_units}
    _assert(
        names == {"Alamofire", "AlamofireDynamic"},
        f"Alamofire should plan both build units, got {names}",
    )

    edits = {(e.kind, e.product_name) for e in plan.package_swift_edits}
    _assert(
        ("force_dynamic", "Alamofire") in edits,
        f"force_dynamic on Alamofire; got {edits}",
    )
    _assert(
        ("force_dynamic", "AlamofireDynamic") not in edits,
        f"must not force_dynamic the already-dynamic AlamofireDynamic; got {edits}",
    )


def _selftest_planner_stripe_synthetic_libraries() -> None:
    """Stripe: --product Stripe narrows the product set to one, --target
    StripeCore / --target StripeUICore add two synthetic libraries, for
    three build units total.
    """
    pkg = _mk_package_from_snapshot(STRIPE_DUMP_SNAPSHOT, schemes=[])
    config = Config(
        package_source="https://github.com/stripe/stripe-ios.git",
        user_version="25.6.2",
        resolved_version="25.6.2",
        product_filters=["Stripe"],
        target_filters=["StripeCore", "StripeUICore"],
    )
    plan = plan_source_build(config, pkg)

    names = {bu.name for bu in plan.build_units}
    _assert(
        names == {"Stripe", "StripeCore", "StripeUICore"},
        f"expected 3 build units {{Stripe, StripeCore, StripeUICore}}, got {names}",
    )
    _assert(
        len(plan.build_units) == 3,
        f"expected exactly 3 units, got {len(plan.build_units)}",
    )

    by_name = {bu.name: bu for bu in plan.build_units}
    _assert(by_name["StripeCore"].synthetic, "StripeCore should be marked synthetic")
    _assert(by_name["StripeUICore"].synthetic, "StripeUICore should be marked synthetic")
    _assert(
        not by_name["Stripe"].synthetic,
        "Stripe is a real product, not synthetic",
    )

    synthetic_edits = {
        e.product_name for e in plan.package_swift_edits
        if e.kind == "add_synthetic_library"
    }
    _assert(
        synthetic_edits == {"StripeCore", "StripeUICore"},
        f"synthetic edits should be {{StripeCore, StripeUICore}}, got {synthetic_edits}",
    )
    force_names = {
        e.product_name for e in plan.package_swift_edits
        if e.kind == "force_dynamic"
    }
    _assert(
        "Stripe" in force_names,
        f"Stripe should get force_dynamic; got {force_names}",
    )
    _assert(
        "StripeCore" not in force_names,
        "synthetic library StripeCore should NOT get force_dynamic "
        "(the synthetic edit is already .dynamic)",
    )
    _assert(
        "StripeUICore" not in force_names,
        "synthetic library StripeUICore should NOT get force_dynamic",
    )


def _selftest_planner_stripe_rejects_binary_target() -> None:
    """--target on a TargetKind.BINARY target (Stripe3DS2 in this fixture)
    must fail with PlanError."""
    pkg = _mk_package_from_snapshot(STRIPE_DUMP_SNAPSHOT, schemes=[])
    config = Config(
        package_source="https://github.com/stripe/stripe-ios.git",
        user_version="25.6.2",
        target_filters=["Stripe3DS2"],
    )
    try:
        plan_source_build(config, pkg)
    except PlanError as exc:
        _assert(
            "Stripe3DS2" in str(exc),
            f"PlanError should mention Stripe3DS2, got: {exc}",
        )
        return
    raise AssertionError("plan_source_build should have raised PlanError for a binary target")


def _selftest_planner_target_matching_existing_product_uses_existing() -> None:
    """If --target T matches an existing .library(name: T, ...) product,
    the planner must warn and reuse the existing product rather than
    synthesize a duplicate."""
    # Use GRDB: `--target GRDB` matches the existing GRDB product.
    pkg = _mk_package_from_snapshot(
        GRDB_DUMP_SNAPSHOT,
        schemes=["GRDB", "GRDB-dynamic", "GRDB-Package"],
    )
    config = Config(
        package_source="https://github.com/groue/GRDB.swift.git",
        user_version="7.9.0",
        product_filters=["GRDB"],           # narrow products to just GRDB
        target_filters=["GRDB"],            # and ALSO pass --target GRDB
    )
    plan = plan_source_build(config, pkg)
    # The planner records the warning on plan.warnings rather than
    # writing to stderr (§5.2 purity contract). Assert it's present.
    _assert(
        any("already exposed" in w for w in plan.warnings),
        f"expected warning about existing product; got {plan.warnings}",
    )

    # No synthetic edit should be added — the existing product is reused.
    synthetic = [e for e in plan.package_swift_edits if e.kind == "add_synthetic_library"]
    _assert(
        not synthetic,
        f"no synthetic library should be added when --target matches an "
        f"existing product; got {synthetic}",
    )
    # Exactly one GRDB build unit.
    grdb_units = [bu for bu in plan.build_units if bu.name == "GRDB"]
    _assert(
        len(grdb_units) == 1,
        f"exactly one GRDB build unit expected, got {len(grdb_units)}",
    )
    _assert(not grdb_units[0].synthetic, "reused GRDB must not be marked synthetic")


def _selftest_planner_target_reinstates_filtered_product() -> None:
    """If `--product` excludes a product but `--target T` names that same
    product, the planner must re-include it exactly once and emit a
    force_dynamic edit for it (since it's still a non-dynamic product).
    This covers the otherwise-untested branch where the existing product
    hasn't already been planned.
    """
    # Use Alamofire: --product AlamofireDynamic narrows to just the
    # already-dynamic one, then --target Alamofire forces the non-dynamic
    # regular product back in via the "existing product" branch.
    pkg = _mk_package_from_snapshot(
        ALAMOFIRE_DUMP_SNAPSHOT,
        schemes=["Alamofire", "AlamofireDynamic"],
    )
    config = Config(
        package_source="https://github.com/Alamofire/Alamofire.git",
        user_version="5.10.2",
        product_filters=["AlamofireDynamic"],
        target_filters=["Alamofire"],
    )
    plan = plan_source_build(config, pkg)

    names = [bu.name for bu in plan.build_units]
    _assert(
        sorted(names) == ["Alamofire", "AlamofireDynamic"],
        f"expected both Alamofire and AlamofireDynamic, got {names}",
    )
    # Alamofire appears exactly once (not duplicated as synthetic).
    _assert(
        names.count("Alamofire") == 1,
        f"Alamofire should appear once, got {names}",
    )
    by_name = {bu.name: bu for bu in plan.build_units}
    _assert(
        not by_name["Alamofire"].synthetic,
        "reinstated Alamofire should not be marked synthetic",
    )
    # Force_dynamic on Alamofire, but not on AlamofireDynamic.
    force_names = {
        e.product_name for e in plan.package_swift_edits
        if e.kind == "force_dynamic"
    }
    _assert(
        force_names == {"Alamofire"},
        f"force_dynamic should be exactly {{Alamofire}}, got {force_names}",
    )
    # And no synthetic edits — we reused the existing product.
    synthetic = [e for e in plan.package_swift_edits if e.kind == "add_synthetic_library"]
    _assert(not synthetic, f"no synthetic edits expected, got {synthetic}")


def _selftest_planner_binary_dedupes_duplicate_artifacts() -> None:
    """Duplicate BinaryArtifact entries (same product_name) collapse to a
    single build unit and the dropped copies land on plan.skipped."""
    artifacts = [
        BinaryArtifact("BlinkID", Path("/fake/a/BlinkID.xcframework")),
        BinaryArtifact("BlinkID", Path("/fake/b/BlinkID.xcframework")),
        BinaryArtifact("BlinkID", Path("/fake/c/BlinkID.xcframework")),
    ]
    config = Config(
        package_source="https://github.com/BlinkID/blinkid-swift-package.git",
        user_version="7.6.2",
        binary_mode=True,
    )
    plan = plan_binary_build(config, artifacts)
    _assert(
        len(plan.build_units) == 1,
        f"expected 1 build unit after dedupe, got {len(plan.build_units)}",
    )
    _assert(
        len(plan.skipped) == 2,
        f"expected 2 skipped duplicates, got {plan.skipped}",
    )
    for name, reason in plan.skipped:
        _assert(name == "BlinkID", f"skipped name should be BlinkID, got {name}")
        _assert("duplicate" in reason, f"skipped reason should mention duplicate: {reason}")


def _selftest_derive_package_label() -> None:
    """SCP-style and URL forms both round-trip to a bare repo name."""
    _assert(
        _derive_package_label("https://github.com/groue/GRDB.swift.git") == "GRDB.swift",
        "https URL",
    )
    _assert(
        _derive_package_label("git@github.com:groue/GRDB.swift.git") == "GRDB.swift",
        "SCP-style URL",
    )
    _assert(
        _derive_package_label("ssh://git@github.com/groue/GRDB.swift.git") == "GRDB.swift",
        "ssh URL",
    )
    _assert(
        _derive_package_label("/Users/me/local-pkg") == "local-pkg",
        "local path",
    )
    _assert(
        _derive_package_label("/Users/me/local-pkg/") == "local-pkg",
        "local path with trailing slash",
    )


def _selftest_planner_unmatched_product_filter() -> None:
    """--product listing a name that doesn't exist in the package raises PlanError."""
    pkg = _mk_package_from_snapshot(GRDB_DUMP_SNAPSHOT, schemes=[])
    config = Config(
        package_source="https://github.com/groue/GRDB.swift.git",
        user_version="7.9.0",
        product_filters=["NotAProduct"],
    )
    try:
        plan_source_build(config, pkg)
    except PlanError as exc:
        _assert("NotAProduct" in str(exc), f"error mentions NotAProduct: {exc}")
        return
    raise AssertionError("expected PlanError for unknown product filter")


def _selftest_planner_binary_filter() -> None:
    """Binary-mode planner filters a synthetic artifact list by --product.
    Also confirms the archive_strategy lands as 'copy-artifact' and
    --target is rejected in binary mode.
    """
    artifacts = [
        BinaryArtifact("BlinkID", Path("/fake/BlinkID/BlinkID.xcframework")),
        BinaryArtifact("BlinkIDVerify", Path("/fake/BlinkIDVerify/BlinkIDVerify.xcframework")),
        BinaryArtifact("BlinkIDUX", Path("/fake/BlinkIDUX/BlinkIDUX.xcframework")),
    ]

    # Filter to just BlinkID
    config = Config(
        package_source="https://github.com/BlinkID/blinkid-swift-package.git",
        user_version="7.6.2",
        binary_mode=True,
        product_filters=["BlinkID"],
    )
    plan = plan_binary_build(config, artifacts)
    _assert(plan.binary_mode, "binary_mode flag should be set")
    _assert(
        len(plan.build_units) == 1,
        f"expected 1 build unit, got {len(plan.build_units)}",
    )
    unit = plan.build_units[0]
    _assert(unit.name == "BlinkID", f"expected BlinkID, got {unit.name}")
    _assert(
        unit.archive_strategy == "copy-artifact",
        f"expected copy-artifact, got {unit.archive_strategy}",
    )
    _assert(unit.artifact_path == Path("/fake/BlinkID/BlinkID.xcframework"),
            f"artifact_path mismatch: {unit.artifact_path}")

    # No filter → all three
    config_all = Config(
        package_source="https://github.com/BlinkID/blinkid-swift-package.git",
        user_version="7.6.2",
        binary_mode=True,
    )
    plan_all = plan_binary_build(config_all, artifacts)
    _assert(
        len(plan_all.build_units) == 3,
        f"expected 3 build units, got {len(plan_all.build_units)}",
    )

    # Unknown product → PlanError
    config_bad = Config(
        package_source="https://github.com/BlinkID/blinkid-swift-package.git",
        user_version="7.6.2",
        binary_mode=True,
        product_filters=["NotThere"],
    )
    try:
        plan_binary_build(config_bad, artifacts)
    except PlanError as exc:
        _assert("NotThere" in str(exc), f"error message: {exc}")
    else:
        raise AssertionError("expected PlanError for unmatched --product")

    # --target in binary mode → PlanError
    config_target = Config(
        package_source="https://github.com/BlinkID/blinkid-swift-package.git",
        user_version="7.6.2",
        binary_mode=True,
        target_filters=["Dummy"],
    )
    try:
        plan_binary_build(config_target, artifacts)
    except PlanError as exc:
        _assert("target" in str(exc).lower(), f"error message: {exc}")
    else:
        raise AssertionError("expected PlanError for --target in binary mode")


def _selftest_planner_language_inference() -> None:
    """Language on a build unit is the union of its target languages."""
    snap = {
        "name": "LangTest",
        "toolsVersion": {"_version": "5.7.0"},
        "platforms": [{"options": [], "platformName": "ios", "version": "15.0"}],
        "products": [
            {"name": "SwiftLib", "type": {"library": ["automatic"]}, "targets": ["SwiftOnly"]},
            {"name": "ObjCLib", "type": {"library": ["automatic"]}, "targets": ["ObjCOnly"]},
            {"name": "MixedLib", "type": {"library": ["automatic"]}, "targets": ["SwiftOnly", "ObjCOnly"]},
        ],
        "targets": [
            {"name": "SwiftOnly", "type": "regular", "path": None, "publicHeadersPath": None, "dependencies": []},
            {"name": "ObjCOnly", "type": "regular", "path": None, "publicHeadersPath": None, "dependencies": []},
        ],
    }
    pkg = _mk_package_from_snapshot(snap, schemes=[])
    # Language is populated by scan_target_languages in the real flow;
    # for the unit test we set it directly.
    pkg.target_by_name("SwiftOnly").language = Language.SWIFT  # type: ignore[union-attr]
    pkg.target_by_name("ObjCOnly").language = Language.OBJC    # type: ignore[union-attr]

    config = Config(package_source="/fake", user_version="")
    plan = plan_source_build(config, pkg)

    by_name = {bu.name: bu for bu in plan.build_units}
    _assert(by_name["SwiftLib"].language == Language.SWIFT, f"SwiftLib: {by_name['SwiftLib'].language}")
    _assert(by_name["ObjCLib"].language == Language.OBJC, f"ObjCLib: {by_name['ObjCLib'].language}")
    _assert(
        by_name["MixedLib"].language == Language.MIXED,
        f"MixedLib: {by_name['MixedLib'].language}",
    )


# --- Prepare self-tests ---------------------------------------------------
#
# Two layers:
#   1. String-level unit tests for the manifest editors. Fast (no swift).
#   2. Round-trip integration tests for the editors + planner edits. Each
#      writes a real Package.swift to a temp dir, applies edits, runs real
#      `swift package dump-package`, and asserts the post-edit product
#      list matches expectations. Gated by `requires_swift=True`.

# Real Package.swift fixtures, embedded as Python literals so the tests
# don't depend on a network or a checked-out clone of GRDB / Stripe / etc.
# These are the EXACT contents fetched from the upstream repos at the
# pinned versions used by the swift-dotnet-packages matrix; their hazards
# are documented in REWRITE_DESIGN.md and the Session 2 brief.

# GRDB v7.9.0: three .library() declarations on the same target. The
# session-2 brief calls out the hazard explicitly: edit_force_dynamic must
# locate by exact `name: "GRDB"`, not by walking forward from the first
# `.library(`, or it will edit GRDB-dynamic instead.
GRDB_PACKAGE_SWIFT_FIXTURE = '''// swift-tools-version:6.1
// The swift-tools-version declares the minimum version of Swift required to build this package.

import Foundation
import PackageDescription

let package = Package(
    name: "GRDB",
    defaultLocalization: "en", // for tests
    platforms: [
        .iOS(.v13),
        .macOS(.v10_15),
        .tvOS(.v13),
        .watchOS(.v7),
    ],
    products: [
        .library(name: "GRDBSQLite", targets: ["GRDBSQLite"]),
        .library(name: "GRDB", targets: ["GRDB"]),
        .library(name: "GRDB-dynamic", type: .dynamic, targets: ["GRDB"]),
    ],
    targets: [
        .systemLibrary(
            name: "GRDBSQLite",
            providers: [.apt(["libsqlite3-dev"])]),
        .target(
            name: "GRDB",
            dependencies: [
                .target(name: "GRDBSQLite"),
            ],
            path: "GRDB"),
    ]
)
'''

# Alamofire 5.10.2: same target backs both `Alamofire` (automatic) and
# `AlamofireDynamic` (already-dynamic). Tests that the editor doesn't
# double-patch the already-dynamic one and doesn't accidentally clobber it
# while editing the regular one.
ALAMOFIRE_PACKAGE_SWIFT_FIXTURE = '''// swift-tools-version: 6.0
import PackageDescription

let package = Package(name: "Alamofire",
                      platforms: [.macOS(.v10_13),
                                  .iOS(.v12),
                                  .tvOS(.v12),
                                  .watchOS(.v4)],
                      products: [
                          .library(name: "Alamofire", targets: ["Alamofire"]),
                          .library(name: "AlamofireDynamic", type: .dynamic, targets: ["Alamofire"])
                      ],
                      targets: [.target(name: "Alamofire",
                                        path: "Source")])
'''

# Stripe 25.6.2 (trimmed): the Stripe library is buried inside several
# multi-line `.library(...)` and `.target(...)` calls with nested arrays.
# The full real manifest has 14 .library entries; this trimmed version
# preserves the multi-line shape and the no-trailing-comma `]` close.
# Targets included only by reference; dump-package doesn't validate paths.
STRIPE_PACKAGE_SWIFT_FIXTURE = '''// swift-tools-version:5.7
import PackageDescription

let package = Package(
    name: "Stripe",
    defaultLocalization: "en",
    platforms: [
        .iOS(.v13)
    ],
    products: [
        .library(
            name: "Stripe",
            targets: ["Stripe"]
        ),
        .library(
            name: "StripePayments",
            targets: ["StripePayments"]
        ),
        .library(
            name: "StripeFinancialConnections",
            targets: ["StripeFinancialConnections"]
        ),
        .library(
            name: "StripeConnect",
            targets: ["StripeConnect"]
        )
    ],
    targets: [
        .target(
            name: "Stripe",
            dependencies: ["StripeCore", "StripePayments"],
            path: "Stripe/StripeiOS"
        ),
        .target(
            name: "StripeCore",
            path: "StripeCore/StripeCore"
        ),
        .target(
            name: "StripePayments",
            dependencies: ["StripeCore"],
            path: "StripePayments/StripePayments"
        ),
        .target(
            name: "StripeFinancialConnections",
            dependencies: ["StripeCore"],
            path: "StripeFinancialConnections/StripeFinancialConnections"
        ),
        .target(
            name: "StripeConnect",
            dependencies: ["StripeCore", "StripeFinancialConnections"],
            path: "StripeConnect/StripeConnect"
        )
    ]
)
'''

# A small fixture that mixes a system library with a regular target. The
# planner skips the system product entirely and Prepare must therefore
# leave it untouched. Used to confirm the validator doesn't false-positive
# on packages that contain system targets.
SYSTEM_LIB_PACKAGE_SWIFT_FIXTURE = '''// swift-tools-version:5.7
import PackageDescription

let package = Package(
    name: "MixedSystem",
    platforms: [.iOS(.v13)],
    products: [
        .library(name: "Sqlite3", targets: ["Sqlite3"]),
        .library(name: "Wrapper", targets: ["Wrapper"]),
    ],
    targets: [
        .systemLibrary(
            name: "Sqlite3",
            providers: [.apt(["libsqlite3-dev"])]),
        .target(
            name: "Wrapper",
            dependencies: ["Sqlite3"],
            path: "Sources/Wrapper"),
    ]
)
'''


def _selftest_balanced_close_basic() -> None:
    s = "(abc)"
    _assert(_balanced_close(s, 0) == 4, f"_balanced_close basic: {_balanced_close(s, 0)}")
    s = "((a)(b))"
    _assert(_balanced_close(s, 0) == 7, f"nested: {_balanced_close(s, 0)}")
    s = "(a)(b)"
    _assert(_balanced_close(s, 0) == 2, f"first call ends at 2")
    _assert(_balanced_close(s, 3) == 5, f"second call")
    # Mismatched
    _assert(_balanced_close("(a", 0) == -1, "no close → -1")


def _selftest_balanced_close_strings() -> None:
    # String literals containing parens must be skipped.
    s = '(name: "foo)bar")'
    _assert(_balanced_close(s, 0) == len(s) - 1,
            f"string-with-paren: {_balanced_close(s, 0)} vs {len(s)-1}")
    # Escaped quotes inside strings must not end the string early.
    s = '(name: "a\\"b)c", x: 1)'
    _assert(_balanced_close(s, 0) == len(s) - 1,
            f"escaped-quote: {_balanced_close(s, 0)} vs {len(s)-1}")


def _selftest_balanced_close_comments() -> None:
    # Block comment with a `)` inside.
    s = "(a /* ) */ b)"
    _assert(_balanced_close(s, 0) == len(s) - 1,
            f"block-comment-with-paren: {_balanced_close(s, 0)} vs {len(s)-1}")
    # Line comment with a `)` inside, terminated by newline.
    s = "(a // ) ignored\n  b)"
    _assert(_balanced_close(s, 0) == len(s) - 1,
            f"line-comment-with-paren: {_balanced_close(s, 0)} vs {len(s)-1}")


def _selftest_edit_force_dynamic_grdb_targets_correct_library() -> None:
    """The CRITICAL hazard: GRDB has two libraries whose names start with
    'GRDB'. force_dynamic('GRDB') must edit the middle one ('GRDB'), NOT
    the third one ('GRDB-dynamic') and NOT GRDBSQLite. This is the bug
    the session-2 worker called out by name."""
    out = edit_force_dynamic(GRDB_PACKAGE_SWIFT_FIXTURE, "GRDB")
    # The GRDB line must now contain `type: .dynamic`.
    grdb_line = [
        ln for ln in out.splitlines()
        if 'name: "GRDB",' in ln and ".library(" in ln
    ]
    _assert(len(grdb_line) == 1, f"expected exactly one GRDB .library line, got {len(grdb_line)}")
    _assert("type: .dynamic" in grdb_line[0],
            f"GRDB line missing type: .dynamic — {grdb_line[0]}")
    # GRDB-dynamic line must be unchanged: still contains the same shape.
    grdb_dyn_line = [
        ln for ln in out.splitlines()
        if 'name: "GRDB-dynamic"' in ln
    ]
    _assert(len(grdb_dyn_line) == 1, "GRDB-dynamic line missing")
    _assert(
        grdb_dyn_line[0].count("type: .dynamic") == 1,
        f"GRDB-dynamic line should still have exactly one type: .dynamic — {grdb_dyn_line[0]}",
    )
    # GRDBSQLite line must be untouched (still no type: clause).
    grdb_sqlite_line = [
        ln for ln in out.splitlines()
        if 'name: "GRDBSQLite"' in ln and ".library(" in ln
    ]
    _assert(len(grdb_sqlite_line) == 1, "GRDBSQLite line missing")
    _assert(
        "type:" not in grdb_sqlite_line[0],
        f"GRDBSQLite line should NOT have a type: clause — {grdb_sqlite_line[0]}",
    )


def _selftest_edit_force_dynamic_already_dynamic_is_noop() -> None:
    """Defensive: if the edit is somehow applied to an already-dynamic
    library, it must not introduce a duplicate clause or otherwise
    corrupt the manifest."""
    out = edit_force_dynamic(GRDB_PACKAGE_SWIFT_FIXTURE, "GRDB-dynamic")
    _assert(out == GRDB_PACKAGE_SWIFT_FIXTURE,
            "force_dynamic on already-dynamic product should be a no-op")


def _selftest_edit_force_dynamic_replaces_existing_type() -> None:
    """If a library already has `type: .static`, the edit replaces it
    with `type: .dynamic` rather than appending a second `type:` clause."""
    src = '''let p = Package(
    products: [
        .library(name: "Foo", type: .static, targets: ["Foo"]),
    ]
)
'''
    out = edit_force_dynamic(src, "Foo")
    _assert("type: .dynamic" in out, f"expected .dynamic in output: {out}")
    _assert(".static" not in out, f"static not removed: {out}")
    # Make sure exactly one type: clause exists.
    _assert(out.count("type:") == 1, f"expected exactly one type: clause, got {out.count('type:')}")


def _selftest_edit_force_dynamic_multiline_arguments() -> None:
    """Stripe-shape: the .library(...) call uses multi-line arguments. The
    balanced-paren walker must navigate them correctly."""
    out = edit_force_dynamic(STRIPE_PACKAGE_SWIFT_FIXTURE, "Stripe")
    # The Stripe library should now have type: .dynamic injected after
    # the name line. The simplest assertion: the modified text contains
    # `name: "Stripe", type: .dynamic` (with whatever exact spacing the
    # editor uses).
    _assert('name: "Stripe", type: .dynamic' in out,
            f"Stripe edit didn't land — searched for 'name: \"Stripe\", type: .dynamic'")
    # And StripePayments must remain untouched (its `name:` line is on
    # its own line, no type: clause should be added).
    _assert('name: "StripePayments",\n            targets:' in out,
            "StripePayments was unexpectedly modified")


def _selftest_edit_force_dynamic_missing_product_raises() -> None:
    """Deliberate-failure path: requesting force_dynamic on a name that
    doesn't exist must raise PrepareError, not silently no-op."""
    try:
        edit_force_dynamic(GRDB_PACKAGE_SWIFT_FIXTURE, "DoesNotExist")
    except PrepareError as exc:
        _assert("DoesNotExist" in str(exc), f"error mentions name: {exc}")
        return
    raise AssertionError("expected PrepareError for missing product")


def _selftest_edit_add_synthetic_library_no_trailing_comma() -> None:
    """Stripe-shape `]` (no trailing comma after last entry): the editor
    must insert a leading comma + new entry before the close bracket."""
    out = edit_add_synthetic_library(STRIPE_PACKAGE_SWIFT_FIXTURE, "StripeCore", ["StripeCore"])
    _assert(
        '.library(name: "StripeCore", type: .dynamic, targets: ["StripeCore"])' in out,
        "synthetic library entry missing",
    )
    # The previous last entry's closing `)` should now be followed by a `,`.
    # Find the last existing entry's close paren in the modified text.
    idx = out.find('.library(\n            name: "StripeConnect"')
    _assert(idx != -1, "StripeConnect entry should still be there")
    after = out[idx:]
    # The first `)` after StripeConnect's open should be followed by `,`.
    rp = after.find(")")
    _assert(rp != -1, "StripeConnect close paren missing")
    _assert(after[rp + 1] == ",", f"expected `,` after StripeConnect's `)`, got {after[rp+1]!r}")


def _selftest_edit_add_synthetic_library_with_trailing_comma() -> None:
    """GRDB-shape `,]`: the editor must NOT add a duplicate comma."""
    out = edit_add_synthetic_library(GRDB_PACKAGE_SWIFT_FIXTURE, "MyExtra", ["GRDB"])
    _assert(
        '.library(name: "MyExtra", type: .dynamic, targets: ["GRDB"])' in out,
        "synthetic library entry missing",
    )
    # GRDB-dynamic line ends with `,` — check we didn't double up.
    grdb_dyn_idx = out.find('.library(name: "GRDB-dynamic"')
    _assert(grdb_dyn_idx != -1, "GRDB-dynamic line should still be there")
    rp = out.find(")", grdb_dyn_idx)
    _assert(out[rp + 1] == ",", f"GRDB-dynamic should still end with `,`, got {out[rp+1]!r}")
    _assert(out[rp + 2] != ",", f"no double comma — got {out[rp+1:rp+3]!r}")


def _selftest_edit_add_synthetic_library_empty_array() -> None:
    """An empty `products: []` should accept a new entry without
    corrupting the brackets."""
    src = '''let p = Package(
    name: "Empty",
    products: [],
    targets: []
)
'''
    out = edit_add_synthetic_library(src, "Foo", ["Foo"])
    _assert('.library(name: "Foo", type: .dynamic, targets: ["Foo"])' in out,
            f"missing entry: {out}")
    # `[]` was empty; after edit, products list should be a valid array.
    # Specifically the `[]` close bracket must still be present somewhere.
    _assert("]," in out and "products: [" in out, f"shape broken: {out}")


def _selftest_parse_xcresult_build_results() -> None:
    """The xcresulttool parser extracts target/message/source from the
    Xcode 16+ build-results JSON shape, drops malformed entries, and
    honors the `limit` parameter."""
    fixture = {
        "actionTitle": "Build",
        "destination": {"deviceId": "x", "deviceName": "iPhone", "architecture": "arm64", "modelName": "x", "osVersion": "18.0"},
        "startTime": 0.0,
        "endTime": 1.0,
        "status": "failed",
        "errorCount": 3,
        "warningCount": 0,
        "analyzerWarningCount": 0,
        "analyzerWarnings": [],
        "warnings": [],
        "errors": [
            {
                "issueType": "Swift Compiler Error",
                "message": "cannot find 'Foo' in scope",
                "targetName": "MyTarget",
                "sourceURL": "file:///x/y.swift",
            },
            {
                "issueType": "Swift Compiler Error",
                "message": "expected expression",
                "targetName": "MyTarget",
            },
            {
                # malformed entry — no message field — should be picked
                # up but produce empty strings, not crash.
                "issueType": "Misc",
            },
            "garbage non-dict entry — must be silently dropped",
        ],
    }
    out = _parse_xcresult_build_results(fixture, limit=10)
    _assert(len(out) == 3, f"expected 3 parsed errors, got {len(out)}")
    _assert(out[0]["target"] == "MyTarget", f"first target: {out[0]}")
    _assert("Foo" in out[0]["message"], f"first message: {out[0]}")
    _assert("y.swift" in out[0]["source"], f"first source: {out[0]}")
    # Limit honored
    out2 = _parse_xcresult_build_results(fixture, limit=2)
    _assert(len(out2) == 2, f"limit=2 returned {len(out2)}")
    # Bad shapes return []
    _assert(_parse_xcresult_build_results({}, limit=5) == [], "empty dict")
    _assert(_parse_xcresult_build_results({"errors": "not a list"}, limit=5) == [], "non-list errors")
    _assert(_parse_xcresult_build_results("not a dict", limit=5) == [], "non-dict input")


def _selftest_unsupported_swift_constructs() -> None:
    """The Prepare safety net rejects Package.swift files containing Swift
    constructs the balanced-paren walker can't reason about: raw strings,
    triple-quoted strings, and string interpolation. Each variant should
    raise PrepareError with a targeted message rather than allowing the
    walker to silently mis-parse.
    """
    # Plain manifests pass through.
    _assert_no_unsupported_swift_constructs(
        '// swift-tools-version:5.7\nlet x = "ok"\n'
    )

    def _expect_raise(text: str, hint: str) -> None:
        try:
            _assert_no_unsupported_swift_constructs(text)
        except PrepareError as exc:
            _assert(hint in str(exc), f"expected '{hint}' in error, got: {exc}")
            return
        raise AssertionError(f"expected PrepareError for {hint!r}")

    _expect_raise('let x = #"hi"#\n', "raw string")
    _expect_raise('let x = """\nhi\n"""\n', "triple-quoted")
    _expect_raise('let x = "name=\\(foo)"\n', "interpolation")


def _selftest_select_active_manifest() -> None:
    """The active-manifest selector mirrors SPM's rule: pick the highest
    Package@swift-X.Y[.Z].swift whose version is <= the toolchain version,
    falling back to Package.swift. Without `swift` on PATH (or with an
    unparseable version) it falls back to Package.swift unconditionally.

    Verifies that the patch component breaks ties correctly: a 5.9.1
    variant wins over a bare 5.9 on a 5.9.1+ toolchain, but loses on 5.9.0.

    We can't easily mock the toolchain version inside this fast test, so
    we monkey-patch _swift_toolchain_version for the duration of the call.
    """
    import tempfile
    global _swift_toolchain_version  # noqa: PLW0603
    saved = _swift_toolchain_version
    try:
        with tempfile.TemporaryDirectory(prefix="spm2x-active-manifest-") as tmp:
            d = Path(tmp)
            (d / "Package.swift").write_text("// base\n")
            (d / "Package@swift-5.9.swift").write_text("// 5.9\n")
            (d / "Package@swift-5.9.1.swift").write_text("// 5.9.1\n")
            (d / "Package@swift-5.10.swift").write_text("// 5.10\n")
            (d / "Package@swift-6.0.swift").write_text("// 6.0\n")

            _swift_toolchain_version = lambda: (6, 2, 0)  # noqa: E731
            picked = _select_active_manifest(d)
            _assert(picked.name == "Package@swift-6.0.swift", f"toolchain 6.2 picked {picked.name}")

            _swift_toolchain_version = lambda: (5, 9, 5)  # noqa: E731
            picked = _select_active_manifest(d)
            _assert(picked.name == "Package@swift-5.9.1.swift",
                    f"toolchain 5.9.5 should pick the .1 patch variant, picked {picked.name}")

            _swift_toolchain_version = lambda: (5, 9, 0)  # noqa: E731
            picked = _select_active_manifest(d)
            _assert(picked.name == "Package@swift-5.9.swift",
                    f"toolchain 5.9.0 should reject the .1 patch variant, picked {picked.name}")

            _swift_toolchain_version = lambda: (5, 7, 0)  # noqa: E731
            picked = _select_active_manifest(d)
            _assert(picked.name == "Package.swift", f"toolchain 5.7 picked {picked.name}")

            _swift_toolchain_version = lambda: None  # noqa: E731
            picked = _select_active_manifest(d)
            _assert(picked.name == "Package.swift", f"unknown toolchain picked {picked.name}")
    finally:
        _swift_toolchain_version = saved


# --- Execute self-tests ---------------------------------------------------
#
# Session 4 introduces the parallel slice builder, static-promote,
# swiftmodule + ObjC header injection, and binary copy. Most of those
# functions touch xcodebuild / lipo / clang and aren't directly unit
# testable, so the self-tests below focus on the pure helpers (path
# layout, framework type detection, ObjC header lookup against the
# raw_dump model, modulemap generation) and on the round-trip fixture
# for `edit_force_dynamic` -> `swift package dump-package`.

_FOO_PACKAGE_SWIFT_FIXTURE = """// swift-tools-version:5.7
import PackageDescription

let package = Package(
    name: "Foo",
    platforms: [.iOS(.v15)],
    products: [
        .library(name: "Foo", targets: ["Foo"]),
    ],
    targets: [
        .target(name: "Foo", path: "Sources/Foo"),
    ]
)
"""


def _selftest_slice_paths_unique() -> None:
    """Device + simulator slice paths must be disjoint so the parallel
    builder can run both at once without stomping on each other's
    archives, derived data, xcresult bundles, or log files."""
    work = Path("/tmp/spm2xc-fake-work")
    dev = _slice_paths(work, "MyUnit", "arm64")
    sim = _slice_paths(work, "MyUnit", "simulator")
    for d, s in zip(dev, sim):
        _assert(d != s, f"slice paths collided: device={d} sim={s}")
    # Within each slice the four paths must also be distinct (no two
    # files at the same target). Catches accidental dedupe in the
    # path-derivation logic.
    _assert(len(set(dev)) == 4, f"device slice has duplicate paths: {dev}")
    _assert(len(set(sim)) == 4, f"sim slice has duplicate paths: {sim}")


def _selftest_detect_framework_type_swift_objc_mixed(tmp_root: Path) -> None:
    """detect_framework_type classifies a synthetic xcframework tree."""
    base = tmp_root / "fwtype"
    base.mkdir()
    # Swift only.
    swift_xc = base / "Swift.xcframework"
    sw_modules = swift_xc / "ios-arm64" / "Swift.framework" / "Modules" / "Swift.swiftmodule"
    sw_modules.mkdir(parents=True)
    (sw_modules / "arm64.swiftinterface").write_text("// interface")
    _assert(detect_framework_type(swift_xc) == "Swift",
            f"Swift xcfw misclassified: {detect_framework_type(swift_xc)}")
    # ObjC only.
    objc_xc = base / "ObjC.xcframework"
    obj_headers = objc_xc / "ios-arm64" / "ObjC.framework" / "Headers"
    obj_headers.mkdir(parents=True)
    (obj_headers / "ObjC.h").write_text("// header")
    _assert(detect_framework_type(objc_xc) == "ObjC",
            f"ObjC xcfw misclassified: {detect_framework_type(objc_xc)}")
    # Mixed.
    mixed_xc = base / "Mixed.xcframework"
    mx_modules = mixed_xc / "ios-arm64" / "Mixed.framework" / "Modules" / "Mixed.swiftmodule"
    mx_modules.mkdir(parents=True)
    (mx_modules / "arm64.swiftinterface").write_text("// interface")
    mx_headers = mixed_xc / "ios-arm64" / "Mixed.framework" / "Headers"
    mx_headers.mkdir(parents=True)
    (mx_headers / "Mixed.h").write_text("// header")
    _assert(detect_framework_type(mixed_xc) == "Mixed",
            f"Mixed xcfw misclassified: {detect_framework_type(mixed_xc)}")
    # Auto-generated -Swift.h must NOT be counted as ObjC.
    bridge_xc = base / "Bridge.xcframework"
    br_modules = bridge_xc / "ios-arm64" / "Bridge.framework" / "Modules" / "Bridge.swiftmodule"
    br_modules.mkdir(parents=True)
    (br_modules / "arm64.swiftinterface").write_text("// interface")
    br_headers = bridge_xc / "ios-arm64" / "Bridge.framework" / "Headers"
    br_headers.mkdir(parents=True)
    (br_headers / "Bridge-Swift.h").write_text("// generated bridge")
    _assert(detect_framework_type(bridge_xc) == "Swift",
            f"Bridge xcfw misclassified (Swift-only with bridge header): {detect_framework_type(bridge_xc)}")
    # Empty -> Unknown.
    empty_xc = base / "Empty.xcframework"
    empty_xc.mkdir()
    _assert(detect_framework_type(empty_xc) == "Unknown",
            f"Empty xcfw misclassified: {detect_framework_type(empty_xc)}")


def _selftest_inject_objc_headers_with_umbrella(tmp_root: Path) -> None:
    """End-to-end: synthetic ObjC target with a `<fw>.h` umbrella header
    + per-class headers; inject_objc_headers must copy them into the
    framework Headers/ dir and generate a module.modulemap that
    references the umbrella header."""
    base = tmp_root / "objc_inject_umbrella"
    staged = base / "staged"
    staged.mkdir(parents=True)
    target_dir = staged / "Sources" / "MyObjC"
    public_headers = target_dir / "include"
    public_headers.mkdir(parents=True)
    (public_headers / "MyObjC.h").write_text("// umbrella")
    (public_headers / "MyObjCHelper.h").write_text("// helper")
    (target_dir / "MyObjC.m").write_text("// impl")

    raw_dump = {
        "name": "MyObjC",
        "products": [
            {"name": "MyObjC", "type": {"library": ["automatic"]}, "targets": ["MyObjC"]},
        ],
        "targets": [
            {
                "name": "MyObjC",
                "type": "regular",
                "path": "Sources/MyObjC",
                "publicHeadersPath": "include",
                "dependencies": [],
            },
        ],
    }
    package = Package(
        name="MyObjC",
        tools_version="5.7.0",
        platforms=[],
        products=[Product(name="MyObjC", linkage=Linkage.AUTOMATIC, targets=["MyObjC"])],
        targets=[Target(
            name="MyObjC",
            kind=TargetKind.REGULAR,
            path="Sources/MyObjC",
            public_headers_path="include",
            dependencies=[],
            exclude=[],
            language=Language.OBJC,
        )],
        schemes=[],
        raw_dump=raw_dump,
        staged_dir=staged,
    )

    fw = base / "MyObjC.framework"
    fw.mkdir()
    injected = inject_objc_headers(
        package=package,
        product_name="MyObjC",
        fw_name="MyObjC",
        fw_path=fw,
        verbose=False,
    )
    _assert(injected, "inject_objc_headers should have returned True")
    _assert((fw / "Headers" / "MyObjC.h").is_file(), "umbrella header missing")
    _assert((fw / "Headers" / "MyObjCHelper.h").is_file(), "helper header missing")
    modulemap = (fw / "Modules" / "module.modulemap").read_text()
    _assert("umbrella header \"MyObjC.h\"" in modulemap,
            f"modulemap missing umbrella header reference:\n{modulemap}")
    _assert("framework module MyObjC" in modulemap,
            f"modulemap missing framework module decl:\n{modulemap}")

    # Idempotency: a second call must be a no-op (returns False, doesn't
    # blow up because Headers/*.h is already present).
    second = inject_objc_headers(
        package=package,
        product_name="MyObjC",
        fw_name="MyObjC",
        fw_path=fw,
        verbose=False,
    )
    _assert(not second, "inject_objc_headers should be idempotent")


def _selftest_inject_objc_headers_explicit_modulemap(tmp_root: Path) -> None:
    """No umbrella header → modulemap should explicitly list every header."""
    base = tmp_root / "objc_inject_explicit"
    staged = base / "staged"
    staged.mkdir(parents=True)
    target_dir = staged / "Sources" / "Bar"
    public_headers = target_dir / "include"
    public_headers.mkdir(parents=True)
    (public_headers / "Alpha.h").write_text("// a")
    (public_headers / "Beta.h").write_text("// b")
    (target_dir / "Bar.m").write_text("// impl")

    raw_dump = {
        "name": "Bar",
        "products": [{"name": "Bar", "type": {"library": ["automatic"]}, "targets": ["Bar"]}],
        "targets": [
            {
                "name": "Bar",
                "type": "regular",
                "path": "Sources/Bar",
                "publicHeadersPath": "include",
                "dependencies": [],
            }
        ],
    }
    package = Package(
        name="Bar",
        tools_version="5.7.0",
        platforms=[],
        products=[Product(name="Bar", linkage=Linkage.AUTOMATIC, targets=["Bar"])],
        targets=[Target(
            name="Bar",
            kind=TargetKind.REGULAR,
            path="Sources/Bar",
            public_headers_path="include",
            dependencies=[],
            exclude=[],
            language=Language.OBJC,
        )],
        schemes=[],
        raw_dump=raw_dump,
        staged_dir=staged,
    )

    fw = base / "Bar.framework"
    fw.mkdir()
    injected = inject_objc_headers(
        package=package,
        product_name="Bar",
        fw_name="Bar",
        fw_path=fw,
        verbose=False,
    )
    _assert(injected, "explicit-modulemap inject should succeed")
    modulemap = (fw / "Modules" / "module.modulemap").read_text()
    _assert("umbrella header" not in modulemap,
            f"explicit modulemap should not use umbrella:\n{modulemap}")
    _assert("header \"Alpha.h\"" in modulemap, f"missing Alpha header line:\n{modulemap}")
    _assert("header \"Beta.h\"" in modulemap, f"missing Beta header line:\n{modulemap}")


def _selftest_detect_system_frameworks_linker_settings(tmp_root: Path) -> None:
    """detect_system_frameworks unions linker settings with source-tree
    imports. This test exercises the linker-settings half: a target with
    no source files but a `linkedFramework` setting must still be picked
    up."""
    base = tmp_root / "detect_linker"
    staged = base / "staged"
    target_dir = staged / "Sources" / "Linked"
    target_dir.mkdir(parents=True)
    raw_dump = {
        "products": [{"name": "Linked", "type": {"library": ["automatic"]}, "targets": ["Linked"]}],
        "targets": [
            {
                "name": "Linked",
                "type": "regular",
                "path": "Sources/Linked",
                "publicHeadersPath": None,
                "dependencies": [],
                "settings": [
                    {"tool": "linker", "kind": {"linkedFramework": "CoreLocation"}},
                    {"tool": "linker", "kind": {"linkedFramework": "Security"}},
                    {"tool": "swift", "kind": {"define": "FOO"}},
                ],
            }
        ],
    }
    package = Package(
        name="Linked",
        tools_version="5.7.0",
        platforms=[],
        products=[Product(name="Linked", linkage=Linkage.AUTOMATIC, targets=["Linked"])],
        targets=[Target(
            name="Linked",
            kind=TargetKind.REGULAR,
            path="Sources/Linked",
            public_headers_path=None,
            dependencies=[],
            exclude=[],
            language=Language.SWIFT,
        )],
        schemes=[],
        raw_dump=raw_dump,
        staged_dir=staged,
    )
    fws = detect_system_frameworks(package, "Linked")
    _assert(fws == ["CoreLocation", "Security"],
            f"linker frameworks not detected: {fws}")


def _selftest_detect_system_frameworks_source_imports(tmp_root: Path) -> None:
    """detect_system_frameworks scans target source files for
    `#import <Framework/...>` and `@import Framework` lines. The Tests/
    subdirectory must be excluded so a Demo app's UIKit import doesn't
    leak into the library's framework list."""
    base = tmp_root / "detect_source"
    staged = base / "staged"
    target_dir = staged / "Sources" / "Scan"
    target_dir.mkdir(parents=True)
    (target_dir / "Header.h").write_text(
        "#import <UIKit/UIKit.h>\n"
        "@import CoreFoundation;\n"
    )
    (target_dir / "Impl.m").write_text(
        "#import <CoreGraphics/CoreGraphics.h>\n"
    )
    tests = target_dir / "Tests"
    tests.mkdir()
    (tests / "Junk.m").write_text("#import <CoreData/CoreData.h>\n")
    raw_dump = {
        "products": [{"name": "Scan", "type": {"library": ["automatic"]}, "targets": ["Scan"]}],
        "targets": [
            {
                "name": "Scan",
                "type": "regular",
                "path": "Sources/Scan",
                "publicHeadersPath": None,
                "dependencies": [],
            }
        ],
    }
    package = Package(
        name="Scan",
        tools_version="5.7.0",
        platforms=[],
        products=[Product(name="Scan", linkage=Linkage.AUTOMATIC, targets=["Scan"])],
        targets=[Target(
            name="Scan",
            kind=TargetKind.REGULAR,
            path="Sources/Scan",
            public_headers_path=None,
            dependencies=[],
            exclude=[],
            language=Language.OBJC,
        )],
        schemes=[],
        raw_dump=raw_dump,
        staged_dir=staged,
    )
    fws = detect_system_frameworks(package, "Scan")
    _assert("UIKit" in fws, f"UIKit missing from detected frameworks: {fws}")
    _assert("CoreFoundation" in fws, f"CoreFoundation missing from detected frameworks: {fws}")
    _assert("CoreGraphics" in fws, f"CoreGraphics missing from detected frameworks: {fws}")
    _assert("CoreData" not in fws,
            f"CoreData should not appear (Tests/ subdir should be pruned): {fws}")
    _assert("Scan" not in fws, f"Scan should be removed as a self-reference: {fws}")


def _selftest_find_objc_headers_dir_priority(tmp_root: Path) -> None:
    """fw_name match wins over product_name match wins over any-target."""
    base = tmp_root / "headers_priority"
    staged = base / "staged"

    # Three targets, each with its own publicHeadersPath:
    #   - First (would-be-first-match)   includes one header.
    #   - Second (matches product_name) includes one header.
    #   - Third (matches fw_name)       includes one header.
    for tname, header in [("FirstAny", "first.h"), ("Prod", "prod.h"), ("MyFW", "fw.h")]:
        d = staged / "Sources" / tname / "include"
        d.mkdir(parents=True)
        (d / header).write_text("// h")

    raw_dump = {
        "products": [
            {
                "name": "Prod",
                "type": {"library": ["automatic"]},
                "targets": ["FirstAny", "Prod", "MyFW"],
            }
        ],
        "targets": [
            {
                "name": tname,
                "type": "regular",
                "path": f"Sources/{tname}",
                "publicHeadersPath": "include",
                "dependencies": [],
            }
            for tname in ("FirstAny", "Prod", "MyFW")
        ],
    }
    package = Package(
        name="Prod",
        tools_version="5.7.0",
        platforms=[],
        products=[Product(name="Prod", linkage=Linkage.AUTOMATIC,
                          targets=["FirstAny", "Prod", "MyFW"])],
        targets=[
            Target(name=tname, kind=TargetKind.REGULAR,
                   path=f"Sources/{tname}", public_headers_path="include",
                   dependencies=[], exclude=[], language=Language.OBJC)
            for tname in ("FirstAny", "Prod", "MyFW")
        ],
        schemes=[],
        raw_dump=raw_dump,
        staged_dir=staged,
    )

    # fw_name match wins (MyFW)
    found = _find_objc_headers_dir(package, product_name="Prod", fw_name="MyFW")
    _assert(found is not None and found.parent.name == "MyFW",
            f"fw_name priority broken: {found}")
    # No fw_name match → product_name match wins (Prod)
    found = _find_objc_headers_dir(package, product_name="Prod", fw_name="NoSuch")
    _assert(found is not None and found.parent.name == "Prod",
            f"product_name priority broken: {found}")


def _selftest_archive_static_lib_path_picks_first(tmp_root: Path) -> None:
    """_archive_static_lib_path returns the first lib*.a it finds, sorted."""
    base = tmp_root / "static_pick"
    products = base / "Products" / "usr" / "local" / "lib"
    products.mkdir(parents=True)
    (products / "libBeta.a").write_text("")
    (products / "libAlpha.a").write_text("")
    found = _archive_static_lib_path(base)
    _assert(found is not None and found.name == "libAlpha.a",
            f"expected libAlpha.a (sorted), got {found}")


def _selftest_archive_framework_path_recursive(tmp_root: Path) -> None:
    """_archive_framework_path finds X.framework anywhere under Products/."""
    base = tmp_root / "fw_locate"
    deep = base / "Products" / "usr" / "local" / "lib" / "MyFW.framework"
    deep.mkdir(parents=True)
    found = _archive_framework_path(base, "MyFW")
    _assert(found is not None and found == deep,
            f"expected to find MyFW.framework, got {found}")
    _assert(_archive_framework_path(base, "Other") is None,
            "should return None when name doesn't match")


# --- Round-trip self-tests ------------------------------------------------
#
# These all require a real `swift` toolchain on PATH and write fixtures
# to a temp dir before invoking `swift package dump-package`. They're the
# core pre-merge gate for Prepare per REWRITE_DESIGN.md §9.


def _roundtrip_apply_and_dump(
    fixture_text: str,
    edits: List[PackageSwiftEdit],
    *,
    plan: Optional[Plan] = None,
) -> Tuple[Path, Plan, dict]:
    """Helper: write the fixture, apply the planner-style edits via
    apply_package_swift_edits, then run `swift package dump-package` and
    return (staged_dir, plan, dumped_json).

    The caller is responsible for cleaning up `staged_dir` (test harness
    runs everything inside a top-level temp dir).
    """
    tmp = Path(tempfile.mkdtemp(prefix="spm2xc-prep-"))
    (tmp / "Package.swift").write_text(fixture_text)
    if plan is None:
        plan = Plan()
        plan.package_swift_edits = list(edits)
    else:
        plan.package_swift_edits = list(edits)
    apply_package_swift_edits(tmp, plan)
    cp = subprocess.run(
        ["swift", "package", "dump-package"],
        cwd=str(tmp),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if cp.returncode != 0:
        raise AssertionError(
            f"swift package dump-package failed after edits:\n"
            f"  {cp.stderr.strip()}\n  Edited Package.swift:\n"
            f"{(tmp / 'Package.swift').read_text()}"
        )
    return tmp, plan, json.loads(cp.stdout)


def _roundtrip_grdb() -> None:
    """GRDB: force_dynamic on GRDB only. Verify GRDB ends up dynamic;
    GRDBSQLite + GRDB-dynamic remain unchanged."""
    edits = [PackageSwiftEdit(kind="force_dynamic", product_name="GRDB", targets=["GRDB"])]
    staged, plan, dump = _roundtrip_apply_and_dump(GRDB_PACKAGE_SWIFT_FIXTURE, edits)
    try:
        prods = {p["name"]: p for p in dump["products"]}
        _assert(prods["GRDB"]["type"]["library"][0] == "dynamic",
                f"GRDB linkage: {prods['GRDB']['type']}")
        _assert(prods["GRDB-dynamic"]["type"]["library"][0] == "dynamic",
                f"GRDB-dynamic linkage: {prods['GRDB-dynamic']['type']}")
        _assert(prods["GRDBSQLite"]["type"]["library"][0] == "automatic",
                f"GRDBSQLite linkage: {prods['GRDBSQLite']['type']}")
    finally:
        shutil.rmtree(staged, ignore_errors=True)


def _roundtrip_alamofire() -> None:
    """Alamofire: force_dynamic on Alamofire (the automatic one). Verify
    BOTH products survive and AlamofireDynamic isn't double-patched."""
    edits = [PackageSwiftEdit(kind="force_dynamic", product_name="Alamofire", targets=["Alamofire"])]
    staged, plan, dump = _roundtrip_apply_and_dump(ALAMOFIRE_PACKAGE_SWIFT_FIXTURE, edits)
    try:
        prods = {p["name"]: p for p in dump["products"]}
        _assert(set(prods.keys()) == {"Alamofire", "AlamofireDynamic"},
                f"Alamofire products: {sorted(prods.keys())}")
        _assert(prods["Alamofire"]["type"]["library"][0] == "dynamic",
                f"Alamofire linkage: {prods['Alamofire']['type']}")
        _assert(prods["AlamofireDynamic"]["type"]["library"][0] == "dynamic",
                f"AlamofireDynamic linkage: {prods['AlamofireDynamic']['type']}")
    finally:
        shutil.rmtree(staged, ignore_errors=True)


def _roundtrip_stripe_force_dynamic_and_synthetic() -> None:
    """Stripe: force_dynamic on Stripe + add_synthetic_library StripeCore.
    Verify both edits land and the existing 4 products survive."""
    edits = [
        PackageSwiftEdit(kind="force_dynamic", product_name="Stripe", targets=["Stripe"]),
        PackageSwiftEdit(kind="add_synthetic_library", product_name="StripeCore", targets=["StripeCore"]),
    ]
    staged, plan, dump = _roundtrip_apply_and_dump(STRIPE_PACKAGE_SWIFT_FIXTURE, edits)
    try:
        prods = {p["name"]: p for p in dump["products"]}
        # All 4 original + 1 synthetic
        expected = {"Stripe", "StripePayments", "StripeFinancialConnections", "StripeConnect", "StripeCore"}
        _assert(set(prods.keys()) == expected,
                f"Stripe products: {sorted(prods.keys())}; expected {sorted(expected)}")
        _assert(prods["Stripe"]["type"]["library"][0] == "dynamic",
                f"Stripe linkage: {prods['Stripe']['type']}")
        _assert(prods["StripeCore"]["type"]["library"][0] == "dynamic",
                f"StripeCore linkage: {prods['StripeCore']['type']}")
        _assert(prods["StripeCore"]["targets"] == ["StripeCore"],
                f"StripeCore targets: {prods['StripeCore']['targets']}")
        # Untouched products should still be automatic
        _assert(prods["StripePayments"]["type"]["library"][0] == "automatic",
                f"StripePayments linkage: {prods['StripePayments']['type']}")
    finally:
        shutil.rmtree(staged, ignore_errors=True)


def _roundtrip_system_library_left_alone() -> None:
    """A package with a system library + a regular target: the planner
    skips the system one, so Prepare only force_dynamics the regular
    one. Validator must accept this without complaint."""
    edits = [PackageSwiftEdit(kind="force_dynamic", product_name="Wrapper", targets=["Wrapper"])]
    staged, plan, dump = _roundtrip_apply_and_dump(SYSTEM_LIB_PACKAGE_SWIFT_FIXTURE, edits)
    try:
        prods = {p["name"]: p for p in dump["products"]}
        _assert(prods["Wrapper"]["type"]["library"][0] == "dynamic",
                f"Wrapper linkage: {prods['Wrapper']['type']}")
        # Sqlite3 left alone — still automatic.
        _assert(prods["Sqlite3"]["type"]["library"][0] == "automatic",
                f"Sqlite3 linkage: {prods['Sqlite3']['type']}")
    finally:
        shutil.rmtree(staged, ignore_errors=True)


def _roundtrip_validator_catches_missing_product() -> None:
    """The mandatory round-trip validator must raise PrepareError when
    the planner asks Prepare to force_dynamic a product that doesn't
    exist in the manifest. The exception is raised by the editor (it can't
    find the .library() call), but the test confirms the path is wired up
    end-to-end through `prepare()`."""
    tmp = Path(tempfile.mkdtemp(prefix="spm2xc-prep-bad-"))
    try:
        (tmp / "Package.swift").write_text(GRDB_PACKAGE_SWIFT_FIXTURE)
        plan = Plan()
        plan.package_swift_edits = [
            PackageSwiftEdit(kind="force_dynamic", product_name="DoesNotExist",
                             targets=["DoesNotExist"]),
        ]
        try:
            prepare(tmp, plan, verbose=False)
        except PrepareError as exc:
            _assert("DoesNotExist" in str(exc),
                    f"PrepareError should mention DoesNotExist: {exc}")
            return
        raise AssertionError("expected PrepareError for non-existent product")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _roundtrip_foo_force_dynamic() -> None:
    """Minimal Foo fixture: force_dynamic on the lone .library product
    must produce a Package.swift that survives `swift package dump-package`
    and ends up dynamic in the dumped JSON. This is the smallest possible
    end-to-end gate for edit_force_dynamic against a real swift toolchain."""
    edits = [PackageSwiftEdit(kind="force_dynamic", product_name="Foo", targets=["Foo"])]
    tmp = Path(tempfile.mkdtemp(prefix="spm2xc-prep-foo-"))
    try:
        (tmp / "Package.swift").write_text(_FOO_PACKAGE_SWIFT_FIXTURE)
        sources = tmp / "Sources" / "Foo"
        sources.mkdir(parents=True)
        (sources / "Foo.swift").write_text("public enum Foo { public static let answer = 42 }\n")
        plan = Plan()
        plan.package_swift_edits = list(edits)
        apply_package_swift_edits(tmp, plan)
        cp = subprocess.run(
            ["swift", "package", "dump-package"],
            cwd=str(tmp),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if cp.returncode != 0:
            raise AssertionError(
                f"swift package dump-package failed on Foo fixture:\n"
                f"  {cp.stderr.strip()}\n  Edited Package.swift:\n"
                f"{(tmp / 'Package.swift').read_text()}"
            )
        dump = json.loads(cp.stdout)
        prods = {p["name"]: p for p in dump["products"]}
        _assert("Foo" in prods, f"Foo product missing from dump: {sorted(prods.keys())}")
        _assert(prods["Foo"]["type"]["library"][0] == "dynamic",
                f"Foo linkage: {prods['Foo']['type']}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _roundtrip_full_prepare_grdb() -> None:
    """End-to-end Prepare on a GRDB-shaped Plan: confirms validator
    accepts the planner's exact edit list (force_dynamic on GRDB only,
    leaving GRDB-dynamic and GRDBSQLite alone). This is the gate that
    catches edit/planner drift across sessions."""
    tmp = Path(tempfile.mkdtemp(prefix="spm2xc-prep-grdb-"))
    try:
        (tmp / "Package.swift").write_text(GRDB_PACKAGE_SWIFT_FIXTURE)
        plan = Plan()
        plan.package_swift_edits = [
            PackageSwiftEdit(kind="force_dynamic", product_name="GRDB", targets=["GRDB"]),
        ]
        plan.build_units = [
            BuildUnit(
                name="GRDB",
                scheme="GRDB",
                framework_name="GRDB",
                language=Language.SWIFT,
                archive_strategy="archive",
                source_targets=["GRDB"],
            ),
            BuildUnit(
                name="GRDB-dynamic",
                scheme="GRDB-dynamic",
                framework_name="GRDB-dynamic",
                language=Language.SWIFT,
                archive_strategy="archive",
                source_targets=["GRDB"],
            ),
        ]
        prepared = prepare(tmp, plan, verbose=False)
        prods = {p.name: p for p in prepared.package.products}
        _assert(prods["GRDB"].linkage == Linkage.DYNAMIC,
                f"GRDB linkage: {prods['GRDB'].linkage}")
        _assert(prods["GRDB-dynamic"].linkage == Linkage.DYNAMIC,
                f"GRDB-dynamic linkage: {prods['GRDB-dynamic'].linkage}")
        # GRDBSQLite was not in build_units (planner skipped it), so the
        # validator's "every build_unit's product is present" check
        # doesn't fire on it. The product itself is still in the dump.
        _assert("GRDBSQLite" in prods, "GRDBSQLite missing from dumped products")
        _assert(prods["GRDBSQLite"].linkage == Linkage.AUTOMATIC,
                f"GRDBSQLite should be untouched, got {prods['GRDBSQLite'].linkage}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# Test registry. Tuples of (name, callable, requires_swift). The fast mode
# only runs entries with requires_swift=False.
def _all_tests(tmp_root: Path) -> List[Tuple[str, Callable[[], None], bool]]:
    return [
        ("parse Nuke snapshot", _selftest_parse_nuke, False),
        ("parse GRDB snapshot (system + dynamic)", _selftest_parse_grdb_skips_systems_correctly, False),
        ("parse Alamofire snapshot (already-dynamic)", _selftest_parse_alamofire_already_dynamic, False),
        ("linkage decoder edge cases", _selftest_linkage_decoder, False),
        ("target kind decoder edge cases", _selftest_target_kind_decoder, False),
        ("default target path", _selftest_default_target_path, False),
        ("dependency parser shapes", _selftest_dependency_parser, False),
        ("toxic-entry filter", _selftest_toxic_filter, False),
        ("language counter (synthetic tree)", lambda: _selftest_language_counter(tmp_root), False),
        ("scheme resolver", _selftest_scheme_resolver, False),
        ("planner: GRDB (force_dynamic + skip system + leave dynamic alone)",
         _selftest_planner_grdb, False),
        ("planner: Alamofire (regular + already-dynamic over same target)",
         _selftest_planner_alamofire, False),
        ("planner: Stripe (--product + --target synthetic libraries)",
         _selftest_planner_stripe_synthetic_libraries, False),
        ("planner: Stripe3DS2 binary target rejection",
         _selftest_planner_stripe_rejects_binary_target, False),
        ("planner: --target reuses existing .library() product",
         _selftest_planner_target_matching_existing_product_uses_existing, False),
        ("planner: --target reinstates product filtered out by --product",
         _selftest_planner_target_reinstates_filtered_product, False),
        ("planner: unmatched --product filter raises PlanError",
         _selftest_planner_unmatched_product_filter, False),
        ("planner: BlinkID binary filter + copy-artifact strategy",
         _selftest_planner_binary_filter, False),
        ("planner: binary dedupe of duplicate artifacts",
         _selftest_planner_binary_dedupes_duplicate_artifacts, False),
        ("planner: language inference (Swift / ObjC / Mixed)",
         _selftest_planner_language_inference, False),
        ("print_plan label derivation (SCP / URL / local)",
         _selftest_derive_package_label, False),
        ("balanced paren walker basic", _selftest_balanced_close_basic, False),
        ("balanced paren walker strings", _selftest_balanced_close_strings, False),
        ("balanced paren walker comments", _selftest_balanced_close_comments, False),
        ("edit_force_dynamic GRDB three-library hazard",
         _selftest_edit_force_dynamic_grdb_targets_correct_library, False),
        ("edit_force_dynamic already-dynamic no-op",
         _selftest_edit_force_dynamic_already_dynamic_is_noop, False),
        ("edit_force_dynamic replaces existing type",
         _selftest_edit_force_dynamic_replaces_existing_type, False),
        ("edit_force_dynamic multiline arguments (Stripe)",
         _selftest_edit_force_dynamic_multiline_arguments, False),
        ("edit_force_dynamic missing product raises",
         _selftest_edit_force_dynamic_missing_product_raises, False),
        ("edit_add_synthetic_library no trailing comma (Stripe)",
         _selftest_edit_add_synthetic_library_no_trailing_comma, False),
        ("edit_add_synthetic_library with trailing comma (GRDB)",
         _selftest_edit_add_synthetic_library_with_trailing_comma, False),
        ("edit_add_synthetic_library empty products array",
         _selftest_edit_add_synthetic_library_empty_array, False),
        ("xcresulttool build-results parser",
         _selftest_parse_xcresult_build_results, False),
        ("unsupported Swift constructs guard",
         _selftest_unsupported_swift_constructs, False),
        ("active manifest selector (Package@swift-X.Y)",
         _selftest_select_active_manifest, False),
        ("execute: slice paths are unique", _selftest_slice_paths_unique, False),
        ("execute: detect_framework_type Swift/ObjC/Mixed/Bridge",
         lambda: _selftest_detect_framework_type_swift_objc_mixed(tmp_root), False),
        ("execute: inject_objc_headers umbrella + idempotency",
         lambda: _selftest_inject_objc_headers_with_umbrella(tmp_root), False),
        ("execute: inject_objc_headers explicit modulemap",
         lambda: _selftest_inject_objc_headers_explicit_modulemap(tmp_root), False),
        ("execute: detect_system_frameworks linker settings",
         lambda: _selftest_detect_system_frameworks_linker_settings(tmp_root), False),
        ("execute: detect_system_frameworks source imports + Tests/ pruning",
         lambda: _selftest_detect_system_frameworks_source_imports(tmp_root), False),
        ("execute: ObjC headers dir priority (fw_name > product_name > any)",
         lambda: _selftest_find_objc_headers_dir_priority(tmp_root), False),
        ("execute: archive static lib path picks first sorted",
         lambda: _selftest_archive_static_lib_path_picks_first(tmp_root), False),
        ("execute: archive framework path recursive search",
         lambda: _selftest_archive_framework_path_recursive(tmp_root), False),
        ("MiniMixed fetch+stage+inspect (real swift)", _selftest_minimixed_fetch_integration, True),
        ("round-trip: GRDB (force_dynamic + skip system)", _roundtrip_grdb, True),
        ("round-trip: Alamofire (force_dynamic regular, leave dynamic)",
         _roundtrip_alamofire, True),
        ("round-trip: Stripe (force_dynamic + add_synthetic_library)",
         _roundtrip_stripe_force_dynamic_and_synthetic, True),
        ("round-trip: system library left alone",
         _roundtrip_system_library_left_alone, True),
        ("round-trip: validator catches missing product (PrepareError)",
         _roundtrip_validator_catches_missing_product, True),
        ("round-trip: full prepare() flow on GRDB", _roundtrip_full_prepare_grdb, True),
        ("round-trip: Foo minimal fixture force_dynamic", _roundtrip_foo_force_dynamic, True),
    ]


def run_self_test(fast: bool) -> int:
    """Run all (or fast-mode) self-tests. Returns shell exit code."""
    bold(f"Running self-test ({'fast' if fast else 'full'})...")
    passed = 0
    failed = 0
    failures: List[Tuple[str, str]] = []
    with tempfile.TemporaryDirectory(prefix="spm2xc-selftest-tmp-") as tmp:
        tmp_root = Path(tmp)
        for name, fn, needs_swift in _all_tests(tmp_root):
            if fast and needs_swift:
                dim(f"  - {name} ... SKIP (fast mode)")
                continue
            try:
                fn()
                success(f"  ✓ {name}")
                passed += 1
            except Exception as exc:
                print(_wrap(f"  ✗ {name}: {exc}", "red"))
                failures.append((name, str(exc)))
                failed += 1
    print()
    if failed:
        print(_wrap(f"{passed} passed, {failed} failed", "red"))
        for name, msg in failures:
            print(_wrap(f"  - {name}: {msg}", "red"))
        return 1
    success(f"{passed} passed, 0 failed")
    return 0


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

    # Session-1-only flag for exploration. Not removed in later sessions —
    # it remains a useful diagnostic.
    parser.add_argument("--inspect-only", action="store_true",
                        help="Run Fetch + Inspect and print the parsed Package model, then exit.")

    # Self-test runner. Two modes: --self-test (default = full incl. swift)
    # and --self-test=fast (snapshots only).
    parser.add_argument("--self-test", nargs="?", const="full", default=None,
                        choices=("full", "fast"),
                        help="Run the self-test suite. --self-test=fast skips tests "
                             "that require the swift toolchain.")

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
        inspect_only=ns.inspect_only,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    ns, parser = parse_args(argv)

    # Self-test mode short-circuits everything else.
    if ns.self_test is not None:
        return run_self_test(fast=(ns.self_test == "fast"))

    if not ns.package_source:
        parser.print_usage(sys.stderr)
        print("Error: package source is required.", file=sys.stderr)
        return 2

    config = _config_from_args(ns)

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


def _print_session4_summary(executed: Sequence[ExecutedUnit]) -> None:
    """Print a Session-4 results banner. Session 5 will replace this with
    the formal Verify summary; until then this gives the user the same
    `=== Summary ===` shape the legacy bash uses, scoped to whatever the
    Execute phase actually produced."""
    bold("\n=== Build summary ===")
    primary_paths: List[Path] = []
    dep_paths: List[Path] = []
    for unit in executed:
        if unit.xcframework_path is None:
            print(f"  - {unit.name}: (no xcframework produced)")
            continue
        label = f"[{unit.framework_type}]" if unit.framework_type else ""
        print(f"  ✓ {unit.xcframework_path.name} {label}".rstrip())
        primary_paths.append(unit.xcframework_path)
        for dep in unit.dependency_xcframeworks:
            print(f"    + dependency: {dep.name}")
            dep_paths.append(dep)
    success(
        f"\nBuilt {len(primary_paths)} xcframework(s)"
        + (f" plus {len(dep_paths)} dependency xcframework(s)" if dep_paths else "")
    )


def _run_source_mode(config: Config) -> int:
    """Source-mode pipeline: Fetch → Inspect → Plan → Prepare → Execute → Verify.

    Session 4 lights up the full Execute path: parallel device + sim
    archives, static→dynamic promotion, swiftmodule + ObjC header
    injection, xcframework merge, and `--include-deps` walking. Session 5
    will add the strict Verify phase on top.
    """
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

    prepared = prepare(staged_dir, plan, verbose=config.verbose)
    executed = execute_source_plan(prepared, config)
    _print_session4_summary(executed)
    return 0


def _run_binary_mode(config: Config) -> int:
    """Binary-mode pipeline: discover_binary_artifacts → Plan → Execute.

    Binary mode skips Inspect/Plan-as-source and the Prepare phase
    entirely; the planner only needs the list of `BinaryArtifact` records
    that `discover_binary_artifacts` discovered during Fetch, and Execute
    just copies the surviving artifacts into `output_dir`.
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

    executed = execute_binary_plan(plan, config)
    _print_session4_summary(executed)
    return 0


# Phase classification for main()'s exception handler. Stays in sync with
# the design's "user-facing vs tool-bug" split (§7). ExecuteError sits in
# the user-facing tier because the message body already includes the
# parsed xcresult diagnostics + the build log path; a Python traceback on
# top of that would just be noise.
_USER_FACING_ERRORS = (FetchError, InspectError, PlanError, ExecuteError)
# VerifyError is bug-class because Verify is the post-build sanity check —
# any failure there means our plan was wrong, not that the user did
# something wrong, so we want the traceback. Keep this in sync with
# REWRITE_DESIGN.md §7.
_BUG_CLASS_ERRORS = (PrepareError, VerifyError)


def _phase_label_for(exc: SpmToXcframeworkError) -> str:
    if isinstance(exc, FetchError):
        return "fetch"
    if isinstance(exc, InspectError):
        return "inspect"
    if isinstance(exc, PlanError):
        return "plan"
    if isinstance(exc, ExecuteError):
        return "execute"
    if isinstance(exc, VerifyError):
        return "verify"
    return "unknown"


if __name__ == "__main__":
    sys.exit(main())
