"""Parsed CLI options as a `Config` dataclass.

The single source of truth for user intent. `cli.parse_args()` produces
an argparse `Namespace`, `cli._config_from_args()` turns it into one of
these, and every downstream phase reads only what it needs from the
instance.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


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
    # Per-platform deployment targets. `None` means "don't build this platform".
    # All default to None; when the user passes zero --min-* flags, the
    # source-mode pipeline auto-derives the set from `Package.platforms[]`
    # after Inspect. The auto-detect falls back to iOS 15.0 when the
    # package declares no platforms at all. Binary mode (no Inspect)
    # applies the same iOS-15 fallback in `_run_binary_mode` (cli.py).
    min_ios: Optional[str] = None
    min_macos: Optional[str] = None
    min_maccatalyst: Optional[str] = None
    min_tvos: Optional[str] = None
    min_watchos: Optional[str] = None
    min_visionos: Optional[str] = None
    # True iff the user passed --no-ios. Distinguishes "user explicitly
    # opted out of iOS" from "user passed nothing and we'll auto-detect."
    # Both end up with `min_ios == None` after _config_from_args, so the
    # auto-detect needs this flag to know whether to skip iOS or fill it
    # from the package.
    no_ios: bool = False
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
    # When False (default), Execute walks build units in topological order
    # and rewrites each already-built sibling target to a `.binaryTarget`
    # in Package.swift before the next unit's archive runs. This stops
    # umbrella products like Stripe from statically embedding all their
    # transitive sibling targets' Mach-O — the umbrella links dynamically
    # against the sibling xcframeworks instead. Pass --no-dedup-overlap
    # to opt out (e.g. to reproduce legacy single-shot behavior for
    # debugging). See REWRITE_DESIGN.md §5.4 dedup-overlap.
    no_dedup_overlap: bool = False
    # When False (default), the top-level orchestrator
    # `_run_source_mode_with_transitives` recurses one level into each
    # external `.product(name:, package:)` dependency of the root's
    # REGULAR targets, building each transitive checkout as its own
    # sibling xcframework. Pass --no-transitive-products to skip the
    # recursion (the umbrella may then ship with dangling .swiftinterface
    # imports — useful only for legacy workflows where the consumer
    # ignores the dropped modules).
    no_transitive_products: bool = False
    # When False (default), a transitive sibling that fails plan/build/
    # verify aborts the whole run — shipping the umbrella with a missing
    # sibling that its .swiftinterface imports would dangle at consume
    # time. Pass --best-effort-transitives to continue with whatever
    # transitives succeeded; the umbrella runs anyway and the failed
    # transitives are skipped with a warning.
    best_effort_transitives: bool = False
    # Internal: set True for child invocations spawned by
    # `_run_source_mode_with_transitives` to suppress shared-output-
    # manifest reads/writes/cleanup (the orchestrator owns those for the
    # whole tree) and to prefix banners with `[transitive: <identity>]`.
    # Never set this from the CLI.
    child_run: bool = False
    # Output sink populated by `_finalize_with_verify` when child_run is
    # True: the orchestrator drains each child's list, merges them with
    # the umbrella's entries, and writes one manifest at the end.
    # Always empty on parent Configs.
    collected_entries: List["ManifestEntry"] = field(default_factory=list)
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
