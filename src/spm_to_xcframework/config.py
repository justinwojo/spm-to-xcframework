"""Parsed CLI options as a `Config` dataclass.

The single source of truth for user intent. `cli.parse_args()` produces
an argparse `Namespace`, `cli._config_from_args()` turns it into one of
these, and every downstream phase reads only what it needs from the
instance.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


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
    # Two-piece per-platform state.
    #
    # `include_<plat>` is the set-membership signal: True means "build a
    # slice for this platform." iOS defaults to True (the project's
    # downstream consumer is .NET MAUI, which is iOS-first); all others
    # default False. The CLI flips them on via the bare-flag opt-in
    # (--macos / --maccatalyst / --tvos / --watchos / --visionos) — and
    # any explicit --min-<plat> VERSION also implies inclusion, so an
    # existing `--min-macos 11` invocation still adds macOS without
    # requiring users to also pass --macos. `--no-ios` turns include_ios
    # off.
    #
    # `min_<plat>` is the resolved deployment-target version, populated
    # by `_autodetect_min_versions` after Inspect (source mode) or by
    # the fallback-only pass in `_run_binary_mode`. Resolution order:
    # user-explicit `--min-<plat>` > `Package.platforms[]` declaration >
    # `_PLATFORM_FALLBACK_VERSIONS[plat]`. Stays None for platforms whose
    # `include_<plat>` is False — downstream slice-walkers key off
    # truthiness post-resolution.
    include_ios: bool = True
    include_macos: bool = False
    include_maccatalyst: bool = False
    include_tvos: bool = False
    include_watchos: bool = False
    include_visionos: bool = False
    min_ios: Optional[str] = None
    min_macos: Optional[str] = None
    min_maccatalyst: Optional[str] = None
    min_tvos: Optional[str] = None
    min_watchos: Optional[str] = None
    min_visionos: Optional[str] = None
    include_deps: bool = False
    binary_mode: bool = False
    verbose: bool = False
    dry_run: bool = False
    # Companion flag to dry_run: when True, the CLI prints the resolved
    # Plan as JSON (via `Plan.to_json()`) instead of the human-readable
    # rendering, and routes informational log messages to stderr so stdout
    # is a clean JSON document — designed for piping into tooling. Implies
    # dry_run.
    dry_run_json: bool = False
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
    # Populated by `_run_source_mode_with_transitives` immediately before
    # the umbrella's `_source_mode_after_inspect` runs: maps each
    # external product name P (from `package.transitive_packages[*]
    # .referenced_products`) to the absolute path of the sibling
    # `<P>.xcframework` the transitive child just deposited under
    # `--output`. The umbrella's Plan reads this dict and, for every P
    # present, emits a `consume_external_sibling` PackageSwiftEdit so
    # Prepare can splice `.binaryTarget(name: P, path: ...)` into the
    # manifest (via the existing overlay mechanism) and rewrite every
    # `.product(name: P, package: ...)` reference in target deps to the
    # bare string `"P"`. Without this rewrite, dedup-overlap's substitution
    # of internal sibling targets to `.binaryTarget` orphans the external
    # `.product(...)` deps the original target used to declare — SPM
    # then declines to resolve/build those products, and any other unit
    # that imports them (directly or via a sibling's swiftinterface) fails
    # with "Unable to find module dependency: ...". Empty dict means
    # nothing to inject (default; matches single-package runs and any
    # transitive-free umbrella). Never set this from the CLI.
    prebuilt_sibling_xcframeworks: Dict[str, Path] = field(default_factory=dict)
    # Companion to `prebuilt_sibling_xcframeworks`: maps each product
    # name to its owning transitive package's SPM identity (e.g.
    # "IssueReporting" → "xctest-dynamic-overlay"). Populated alongside
    # the path map; only entries whose owning package is known from
    # `package.transitive_packages[*].referenced_products` get an
    # identity. Products auto-injected purely because they were freshly
    # built as siblings (e.g. `IssueReportingPackageSupport`, which the
    # umbrella's source never references but appears in the consumed
    # `IssueReporting.framework`'s swiftinterface as a public re-export)
    # are absent from this map — Prepare treats that case as "inject the
    # binaryTarget overlay only; skip `.product()` rewrite and
    # `.package(url:)` strip for this product, because the strip is
    # identity-based and a sibling product from the same package
    # already triggers it".
    prebuilt_sibling_identities: Dict[str, str] = field(default_factory=dict)

    @property
    def is_remote(self) -> bool:
        s = self.package_source
        return (
            s.startswith("http://")
            or s.startswith("https://")
            or s.startswith("git@")
            or s.startswith("ssh://")
        )
