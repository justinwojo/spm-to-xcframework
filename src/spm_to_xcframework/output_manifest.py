"""Output directory manifest — cross-run hygiene.

Every successful run writes a small JSON manifest into `<output_dir>/`
recording exactly which xcframework directories the tool produced. The
next run reads that manifest (before doing anything destructive) so it
can clean up stale artifacts from prior runs that the new run no longer
produces. Two safety properties (REFACTOR_PLAN.md Task 3):

  1. Cleanup NEVER happens before Verify passes on every unit in the
     current run. A failed run leaves prior state untouched so the
     user's last known-good artifacts are preserved.
  2. Cleanup only touches directories whose basenames were recorded in
     the PRIOR manifest — i.e. things this tool produced. User-owned
     files in `<output_dir>/` are ignored.

The manifest filename starts with a `.` so casual `ls` output stays
clean, and is namespaced with the tool name so it's unambiguous.
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Sequence, Set

from .log import warn


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
