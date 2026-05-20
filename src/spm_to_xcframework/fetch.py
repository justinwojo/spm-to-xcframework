"""Phase 0: clone or copy + stage the package source.

Contract (REWRITE_DESIGN.md §5.0):
  - every downstream phase reads from WORK_DIR/staged, never from
    WORK_DIR/source. Source is kept solely for --keep-work debugging.
  - the staged tree never contains `.git`, `.build`, DerivedData,
    node_modules, or any `.xcodeproj`/`.xcworkspace` sibling (those
    cause `xcodebuild -list` to refuse SPM-auto schemes).
  - after staging, `swift package resolve` runs once against the
    staged tree so downstream xcodebuild calls don't need network
    access or racy parallel resolution.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import List, Sequence, Tuple

from .config import Config
from .errors import FetchError, InspectError
from .log import info, success, verbose_log
from .model import BinaryArtifact, TargetKind, _default_target_path
from .platforms import _spm_platform_entries

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
    # Local import keeps the fetch ↔ inspect cycle broken — inspect.py
    # imports model._default_target_path, but the dump-package helper is
    # only needed once and at runtime, never at import time.
    from .inspect import _swift_dump_package

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

    # SPM resolve doesn't actually care about the version, but the
    # manifest needs to be self-consistent: one platform entry per
    # `--min-*` flag the user set, using the exact version string. The
    # string-form `.iOS("15.0")` sidesteps the `.v<major>` enum table
    # (`.macOS(.v10)` is not a real case for 10.15). Tools-version 5.9
    # is the floor that allows declaring `visionOS`.
    platform_entries = _spm_platform_entries(config)
    platforms_array = ", ".join(platform_entries) if platform_entries else ".iOS(\"15.0\")"

    manifest = (
        "// swift-tools-version:5.9\n"
        "import PackageDescription\n"
        "\n"
        "let package = Package(\n"
        '    name: "binary-resolver",\n'
        f"    platforms: [{platforms_array}],\n"
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
