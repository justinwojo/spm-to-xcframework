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
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, List, NoReturn, Optional, Sequence, Tuple

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
    Prepare. Filled in by session 2."""

    kind: str  # "force_dynamic" | "add_synthetic_library"
    product_name: str
    targets: List[str] = field(default_factory=list)


@dataclass
class BuildUnit:
    """One unit of work the executor will run. Filled in by session 2."""

    name: str
    scheme: str
    framework_name: str
    language: str
    archive_strategy: str  # "Archive" | "StaticPromote" | "CopyArtifact"
    source_targets: List[str] = field(default_factory=list)


@dataclass
class Plan:
    """Output of the planner. Stub in session 1."""

    stage: StageSpec = field(default_factory=StageSpec)
    package_swift_edits: List[PackageSwiftEdit] = field(default_factory=list)
    build_units: List[BuildUnit] = field(default_factory=list)


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
# --- Phase 2: Plan (stub) ---
# ============================================================================


def plan_source_build(config: Config, package: Package) -> Plan:
    """Stub for session 2."""
    raise NotImplementedError("Plan phase is implemented in session 2.")


def plan_binary_build(config: Config) -> Plan:
    """Stub for session 2."""
    raise NotImplementedError("Plan phase is implemented in session 2.")


# ============================================================================
# --- Phase 3: Prepare (stub) ---
# ============================================================================


def apply_package_swift_edits(staged_dir: Path, edits: List[PackageSwiftEdit]) -> None:
    """Stub for session 3."""
    raise NotImplementedError("Prepare phase is implemented in session 3.")


# ============================================================================
# --- Phase 4: Execute (stub) ---
# ============================================================================


def execute_source_plan(config: Config, plan: Plan) -> None:
    """Stub for session 3 / 4."""
    raise NotImplementedError("Execute phase is implemented in sessions 3+4.")


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
        ("MiniMixed fetch+stage+inspect (real swift)", _selftest_minimixed_fetch_integration, True),
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
    # both user_version and resolved_version (bug 1 fix).
    if config.is_remote and config.user_version:
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
            source_dir = fetch_source(config)
            staged_dir = stage_source(config, source_dir)
            package = inspect_package(config, staged_dir)
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

        # Session 1: nothing past Inspect is wired up. Always print the
        # package; emit an extra notice when the user wasn't expecting an
        # inspect-only run.
        print_package(package)
        if not config.inspect_only:
            warn(
                "\nNote: this is a Session 1 build of the Python rewrite — "
                "Plan / Prepare / Execute / Verify are stubs. Use "
                "--inspect-only to silence this notice. The legacy bash "
                "spm-to-xcframework still handles full builds until the "
                "rewrite lands."
            )
        return 0

    finally:
        if keep:
            dim(f"Work directory retained: {work_dir}")
        else:
            shutil.rmtree(work_dir, ignore_errors=True)


# Phase classification for main()'s exception handler. Stays in sync with
# the design's "user-facing vs tool-bug" split (§7).
_USER_FACING_ERRORS = (FetchError, InspectError, PlanError)
_BUG_CLASS_ERRORS = (PrepareError,)


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
