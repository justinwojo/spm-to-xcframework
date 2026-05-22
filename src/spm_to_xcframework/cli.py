"""Top-level CLI driver — argparse wiring, mode-routing, and exit-code
mapping for the spm-to-xcframework command.

The CLI is the only entry that knows about phase exceptions in
aggregate: it owns the user-facing vs tool-bug split per
REWRITE_DESIGN.md §7. User-facing phase errors are surfaced as a clean
one-line "Error (<phase>): <msg>" and exit with the phase's
`exit_code`; tool-bug exceptions (PrepareBug, VerifyBug) deliberately
bubble up so the user sees a full traceback.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .config import Config
from .errors import (
    ExecuteError,
    FetchError,
    InspectError,
    PlanError,
    PrepareBug,
    PrepareError,
    PrepareUserError,
    SpmToXcframeworkError,
    VerifyBug,
    VerifyError,
    VerifyUserError,
)
from .fetch import (
    _validate_git_ref,
    _validate_package_source,
    discover_binary_artifacts,
    fetch_source,
    normalize_version_tag,
    stage_source,
)
from .inspect import (
    inspect_package,
    print_package,
)
from .plan import (
    _package_is_binary_only,
    compute_internal_target_deps,
    plan_binary_build,
    plan_source_build,
    print_plan,
)
from .log import _wrap, bold, die, dim, info, set_log_stdout_to_stderr, warn
from .model import ExecutedUnit, Language, Package, TargetKind
from .platforms import _autodetect_min_versions, _enabled_platforms
from .prepare import prepare
from .prune_child import _identity_from_url, prune_child_manifest_for_products
from .execute import (
    detect_framework_type,
    execute_binary_plan,
    execute_source_plan,
)
from .output_manifest import (
    _MANIFEST_KIND_DEPENDENCY,
    _MANIFEST_KIND_PRIMARY,
    ManifestEntry,
    OutputManifest,
    _cleanup_stale_manifest_entries,
    _read_output_manifest,
    _write_output_manifest,
)
from .verify import (
    print_verify_summary,
    verify_output,
)


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
    parser.add_argument("--min-ios", default=None,
                        help="Minimum iOS deployment target (e.g. 15.0). "
                             "Default: auto-derive from Package.swift `platforms:`; "
                             "falls back to 15.0 if the package declares no platforms. "
                             "Pass --no-ios to skip iOS.")
    parser.add_argument("--no-ios", action="store_true",
                        help="Skip iOS entirely. The remaining platforms are auto-derived "
                             "from `Package.platforms[]`; pass another --min-* flag to "
                             "specify one explicitly. Mutually exclusive with --min-ios.")
    parser.add_argument("--min-macos", default=None,
                        help="Minimum macOS deployment target (e.g. 11.0). Adds the macOS slice. "
                             "Default: auto-derive from Package.swift.")
    parser.add_argument("--min-maccatalyst", default=None,
                        help="Minimum Mac Catalyst deployment target (e.g. 15.0). Adds the Mac Catalyst slice. "
                             "Default: auto-derive from Package.swift.")
    parser.add_argument("--min-tvos", default=None,
                        help="Minimum tvOS deployment target (e.g. 15.0). Adds tvOS device + simulator slices. "
                             "Default: auto-derive from Package.swift.")
    parser.add_argument("--min-watchos", default=None,
                        help="Minimum watchOS deployment target (e.g. 8.0). Adds watchOS device + simulator slices. "
                             "Default: auto-derive from Package.swift.")
    parser.add_argument("--min-visionos", default=None,
                        help="Minimum visionOS deployment target (e.g. 1.0). Adds visionOS device + simulator slices. "
                             "Default: auto-derive from Package.swift.")
    parser.add_argument("--include-deps", action="store_true",
                        help="Also build xcframeworks for transitive dependencies (iOS-only in v1)")
    parser.add_argument(
        "--no-transitive-products",
        action="store_true",
        help=(
            "Skip the in-process recursion that builds a sibling "
            "xcframework for each external `.product(name:, package:)` "
            "the root's targets depend on. The umbrella's "
            ".swiftinterface may then `import` modules whose "
            ".swiftmodule isn't on the consumer's search path; useful "
            "only when you know the consumer doesn't link against those "
            "transitive symbols."
        ),
    )
    parser.add_argument(
        "--best-effort-transitives",
        action="store_true",
        help=(
            "Default: a transitive-package build/verify failure aborts "
            "the whole run (the umbrella would ship with dangling "
            ".swiftinterface imports). Pass this flag to continue with "
            "whatever transitives succeeded; the umbrella runs anyway "
            "and failed transitives are skipped with a warning."
        ),
    )
    parser.add_argument("--verbose", action="store_true",
                        help="Show full build output")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be built without building. "
                             "Runs Fetch + Inspect + Plan only; no xcodebuild "
                             "is invoked (transitive sibling resolution is "
                             "skipped — the printed plan reflects the umbrella "
                             "package alone).")
    parser.add_argument("--dry-run-json", action="store_true",
                        help="Like --dry-run, but emit the resolved Plan as "
                             "machine-readable JSON on stdout (informational "
                             "log messages route to stderr). Implies --dry-run.")
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
    parser.add_argument(
        "--no-dedup-overlap",
        action="store_true",
        help=(
            "Disable inter-unit `.binaryTarget` substitution. By default, "
            "when one product depends on a sibling product/target in the "
            "same package, the sibling is built first and the umbrella's "
            "Package.swift is rewritten to consume it as a `.binaryTarget` "
            "before the umbrella's archive runs — this stops the umbrella "
            "from statically embedding the sibling's Mach-O. Pass this flag "
            "to keep legacy single-shot behavior."
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
        # Resolve to absolute so the path stays valid when it crosses
        # subprocess boundaries — Execute writes it into the staged
        # Package.swift's `.binaryTarget(path: ...)`, which SPM resolves
        # against the staged manifest's directory (a tempdir), not the
        # user's CWD. Relative paths like the default `./xcframeworks`
        # would resolve under that tempdir and crash xcodebuild with
        # "binary target does not contain a binary artifact". Smoke-tested
        # against `stripe-ios --target StripeCore --product Stripe`.
        output_dir=Path(ns.output).expanduser().resolve(),
        product_filters=list(ns.products or []),
        target_filters=list(ns.targets or []),
        revision=ns.revision,
        min_ios=None if ns.no_ios else ns.min_ios,
        min_macos=ns.min_macos,
        min_maccatalyst=ns.min_maccatalyst,
        min_tvos=ns.min_tvos,
        min_watchos=ns.min_watchos,
        min_visionos=ns.min_visionos,
        no_ios=bool(ns.no_ios),
        include_deps=ns.include_deps,
        binary_mode=ns.binary,
        verbose=ns.verbose,
        dry_run=ns.dry_run or ns.dry_run_json,
        dry_run_json=ns.dry_run_json,
        keep_work=ns.keep_work,
        no_cleanup_stale=ns.no_cleanup_stale,
        no_dedup_overlap=ns.no_dedup_overlap,
        no_transitive_products=ns.no_transitive_products,
        best_effort_transitives=ns.best_effort_transitives,
        inspect_only=ns.inspect_only,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    ns, parser = parse_args(argv)

    if not ns.package_source:
        parser.print_usage(sys.stderr)
        print("Error: package source is required.", file=sys.stderr)
        return 2

    # --no-ios + --min-ios is a contradiction; reject it explicitly
    # rather than silently dropping --min-ios in _config_from_args.
    if ns.no_ios and ns.min_ios is not None:
        die("--no-ios and --min-ios are mutually exclusive. "
            "Drop one or the other.")

    config = _config_from_args(ns)
    if config.dry_run_json:
        # Keep stdout clean for the JSON document; all info/bold/dim/success
        # phase chatter routes to stderr until the final json.dumps call.
        set_log_stdout_to_stderr(True)

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

    # Platform validation runs AFTER auto-detect (source mode) or AFTER the
    # binary-mode iOS-15 fallback (binary mode), since neither set is known
    # at this point in main(). See `_validate_platforms_post_autodetect`.

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
            return _run_source_mode_with_transitives(config)
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


def _verify_executed_and_collect_entries(
    executed: Sequence[ExecutedUnit],
    config: Config,
) -> Tuple[int, List[ManifestEntry]]:
    """Verify-only half of finalize.

    Runs Verify against the executed units (including their dependency
    xcframeworks promoted to first-class units), prints the per-unit
    summary, and returns a `(exit_code, entries)` pair.

    - `exit_code` is 0 iff every unit passed; otherwise `VerifyError.exit_code`
    - `entries` is the list of `ManifestEntry`s that should be written for
      this run, classified primary vs dependency. Empty when verify failed.

    No filesystem side effects on the output manifest. Callers either feed
    `entries` into `_finalize_manifest` (single-package runs and the
    top-level orchestrator) or merge them with other runs' entries before
    calling the manifest finalizer once.
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
            new_rank = _language_specificity.get(dep.expected_language, 0)
            old_rank = _language_specificity.get(existing.expected_language, 0)
            if new_rank > old_rank:
                existing.expected_language = dep.expected_language

    results = verify_output(units, config.output_dir)
    print_verify_summary(results, config.output_dir)
    if any(not r.passed for r in results):
        return VerifyError.exit_code, []

    primary_names: Set[str] = set()
    for u in executed:
        if u.xcframework_path is not None:
            primary_names.add(u.xcframework_path.name)
    entries: List[ManifestEntry] = []
    for r in results:
        if not r.passed:
            continue
        name = r.xcframework_path.name
        kind = (
            _MANIFEST_KIND_PRIMARY
            if name in primary_names
            else _MANIFEST_KIND_DEPENDENCY
        )
        entries.append(ManifestEntry(name=name, kind=kind))
    return 0, entries


def _finalize_manifest(
    config: Config,
    new_entries: Sequence[ManifestEntry],
    *,
    old_manifest: Optional[OutputManifest],
) -> None:
    """Manifest-write half of finalize.

    Given the full set of verified entries from this run (primary +
    dependency + transitive across the whole orchestrator tree), perform
    one cleanup pass against `old_manifest` and one atomic manifest write.

    Called exactly once per top-level invocation. Children of the
    `_run_source_mode_with_transitives` orchestrator must NOT call this;
    they return their entries up to the orchestrator which calls this
    after every child + the umbrella have been verified.
    """
    verified_produced: Set[str] = {e.name for e in new_entries}
    merged: List[ManifestEntry] = list(new_entries)

    if old_manifest is not None:
        if config.no_cleanup_stale:
            existing = {e.name for e in merged}
            for entry in old_manifest.entries:
                if entry.name in existing:
                    continue
                if not (config.output_dir / entry.name).exists():
                    continue
                merged.append(entry)
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
            merged,
            package_source=config.package_source,
            package_version=config.user_version,
        )
    except OSError as exc:
        warn(f"Could not write output manifest: {exc}")


def _finalize_with_verify(
    executed: Sequence[ExecutedUnit],
    config: Config,
    *,
    old_manifest: Optional[OutputManifest] = None,
) -> int:
    """Verify executed units; either finalize the manifest (top-level) or
    park entries on `config.collected_entries` (child runs).

    Standard single-package mode (`config.child_run` is False): runs
    Verify, then on success cleans up stale prior outputs and atomically
    writes a fresh manifest. On Verify failure, leaves both the prior
    manifest AND the prior on-disk artifacts untouched.

    Child mode (`config.child_run` is True): runs Verify only and stores
    the resulting `ManifestEntry`s on `config.collected_entries`. The
    orchestrator (`_run_source_mode_with_transitives`) is responsible for
    merging entries across the child + umbrella tree and writing a
    single manifest at the end. Child runs MUST NOT receive an
    `old_manifest` — the orchestrator owns the one-shot read of the
    shared manifest.
    """
    exit_code, entries = _verify_executed_and_collect_entries(executed, config)
    if config.child_run:
        if exit_code == 0:
            config.collected_entries.extend(entries)
        return exit_code
    if exit_code != 0:
        return exit_code
    _finalize_manifest(config, entries, old_manifest=old_manifest)
    return 0


def _validate_platforms_post_autodetect(config: Config) -> None:
    """Shared post-resolution validation for source mode (after
    `_autodetect_min_versions`) and binary mode (after the iOS-15
    fallback in `_run_binary_mode`).

    Enforces:
      - at least one platform is enabled
      - `--include-deps` requires iOS to be among the enabled platforms;
        warns when non-iOS slices are also enabled (deps are iOS-only in v1)
    """
    enabled = _enabled_platforms(config)
    if not enabled:
        # Reachable when --no-ios is set against a package that declares
        # no non-iOS platforms (either platforms: is empty/missing, or it
        # only declares iOS). --no-ios suppresses the iOS-15 fallback in
        # both source-mode auto-detect and the binary-mode default, so
        # the enabled set legitimately ends up empty.
        die("No platforms selected. Pass at least one of --min-ios, --min-macos, "
            "--min-maccatalyst, --min-tvos, --min-watchos, --min-visionos. "
            "(--no-ios disables iOS, so combine it with one of the others.)")

    if config.include_deps:
        if "ios" not in enabled:
            die("--include-deps requires iOS to be enabled in v1. "
                "Drop --no-ios or omit --include-deps.")
        non_ios = [p for p in enabled if p != "ios"]
        if non_ios:
            warn(
                "--include-deps: dependency xcframeworks will be iOS-only; "
                f"non-iOS slices ({', '.join(non_ios)}) won't carry dep artifacts."
            )


def _emit_dry_run_json(
    plan,
    *,
    package,
    config: Config,
) -> None:
    """Print the resolved Plan as JSON on stdout for `--dry-run-json`.

    The envelope wraps `plan.to_json()` with a small amount of context the
    campaign tooling wants for free: package label, version, mode, and the
    list of transitive SPM identities that would be built as siblings if
    the run weren't a dry-run. The transitive list is purely informational
    — the plan itself never includes `consume_external_sibling` edits in
    dry-run mode because no actual sibling xcframeworks exist on disk.
    """
    if package is not None:
        name = package.name
        transitive_idents = [tp.identity for tp in package.transitive_packages]
    else:
        name = _derive_package_label(config.package_source or "(unknown)")
        transitive_idents = []
    # Resolved per-platform deployment targets: same source of truth
    # that `print_plan` renders as "Selected slices" (see
    # plan.py:1251). Without this, JSON consumers can't recover what
    # platform set the run will actually drive xcodebuild against —
    # caught by Codex review on the 2026-05-22 dry-run-json pass.
    platforms = {
        p: getattr(config, f"min_{p}")
        for p in _enabled_platforms(config)
    }
    envelope = {
        "package": name,
        "version": config.user_version or None,
        "mode": "binary" if plan.binary_mode else "source",
        "platforms": platforms,
        "transitive_packages": transitive_idents,
        "plan": plan.to_json(),
    }
    json.dump(envelope, sys.stdout, indent=2, sort_keys=False)
    sys.stdout.write("\n")
    sys.stdout.flush()


def _source_mode_inspect(config: Config):
    """Fetch + Stage + Inspect + autodetect + post-autodetect validation.

    Returns either:
      - the tuple `(source_dir, staged_dir, package)` if the run should
        proceed to Plan, or
      - an `int` exit code when an early-exit path fires (--inspect-only,
        or transparent handoff to binary mode for binary-only packages).

    Splitting this from the rest of source mode lets
    `_run_source_mode_with_transitives` pre-inspect the umbrella once to
    discover transitives without paying for a second Fetch + Stage on the
    umbrella's actual build pass.
    """
    source_dir = fetch_source(config)
    staged_dir = stage_source(config, source_dir)
    package = inspect_package(config, staged_dir)

    # Auto-derive the platform set + deployment targets from
    # `Package.platforms[]` when the user passed no --min-* flags. Runs
    # BEFORE the --inspect-only early return so the post-autodetect
    # validator (--include-deps requires iOS, etc.) still fires for
    # inspect runs, AND before the binary-only route below so the
    # derived versions flow into binary mode too. No-op when any
    # --min-* was explicit.
    derived = _autodetect_min_versions(config, package)
    if derived:
        info(
            "Auto-detected platforms from Package.swift: "
            + ", ".join(f"{p} {v}" for p, v in derived.items())
            + " (pass any --min-* flag to override)."
        )
    _validate_platforms_post_autodetect(config)

    if config.inspect_only:
        print_package(package)
        return 0

    # Auto-route binary-only packages to --binary mode. This kicks in
    # AFTER Inspect (so the package is fully parsed) but BEFORE Plan
    # (so we don't emit the broken force_dynamic patch). Honour
    # --target as an explicit opt-out: the source-build escape hatch
    # is incompatible with a mode switch, and the user passing --target
    # is a strong "stay in source mode" signal.
    if (
        not config.target_filters
        and not config.inspect_only
        and _package_is_binary_only(package)
    ):
        if not config.is_remote:
            raise PlanError(
                "Detected a binary-only Package.swift (every product is "
                "backed only by binaryTarget targets). Auto-switching to "
                "--binary mode requires a remote package URL plus "
                "--version <tag>; this run was given a local path. Either "
                "point at the remote URL, or extract the xcframework from "
                "the upstream artifact directly."
            )
        if not config.user_version:
            raise PlanError(
                "Detected a binary-only Package.swift. Auto-switching to "
                "--binary mode requires --version <tag>."
            )
        info(
            "Detected binary-only package (all products are binaryTarget); "
            "switching to --binary mode."
        )
        config.binary_mode = True
        return _run_binary_mode(config)

    return source_dir, staged_dir, package


def _source_mode_after_inspect(
    config: Config,
    source_dir: Path,
    staged_dir: Path,
    package,
) -> int:
    """Plan → Prepare → Execute → Verify on an already-inspected package.

    The `source_dir` argument is kept for symmetry with `_source_mode_inspect`'s
    return shape even though Plan onwards never touches the source tree
    again — Stage already produced the working tree.

    Reads `old_manifest` only when NOT a child run. Children let the
    orchestrator own the one-shot manifest read and merged write.
    """
    plan = plan_source_build(config, package)
    for w in plan.warnings:
        warn(w)
    if config.dry_run_json:
        _emit_dry_run_json(plan, package=package, config=config)
    else:
        print_plan(plan, package=package, config=config)

    if config.dry_run:
        return 0

    old_manifest = (
        None if config.child_run else _read_output_manifest(config.output_dir)
    )

    prepared = prepare(staged_dir, plan, verbose=config.verbose)
    executed = execute_source_plan(prepared, config)
    return _finalize_with_verify(executed, config, old_manifest=old_manifest)


def _run_source_mode(config: Config) -> int:
    """Source-mode pipeline: Fetch → Inspect → Plan → Prepare → Execute → Verify.

    If Inspect reveals that every (non-system) product is backed solely by
    binaryTarget targets, we transparently hand off to `_run_binary_mode`.
    Source mode for such a package is fundamentally wrong: there's nothing
    to compile, and the unconditional `force_dynamic` patch would be
    rejected by SPM with "invalid type for binary product". The hand-off
    fires only when `--binary` would also have worked (remote URL +
    explicit `--version`); for the local-path case we surface a clear
    PlanError pointing at the limitation rather than silently failing
    deeper in.
    """
    inspect_result = _source_mode_inspect(config)
    if isinstance(inspect_result, int):
        return inspect_result
    source_dir, staged_dir, package = inspect_result
    return _source_mode_after_inspect(config, source_dir, staged_dir, package)


def _compute_selected_target_closure(
    config: Config, package: "Package"
) -> "Optional[Set[str]]":
    """Return the set of root REGULAR target names the user has actually
    asked to build, expanded by internal sibling deps.

    Used by `_run_source_mode_with_transitives` to filter out transitives
    referenced only by helper/example targets the umbrella build won't
    touch. Returns None when no `--product` / `--target` filter is in
    effect (the default-everything case — caller should treat that as
    "keep every transitive").

    Closure construction:
      1. Seed = (product_filters' backing targets) ∪ target_filters.
      2. Expand via `compute_internal_target_deps` (internal sibling
         edges only — external `.product(...)` deps are NOT followed,
         since those reach OTHER packages and don't belong in a same-
         package target set).
      3. Restrict to REGULAR targets (the only kind ever shipped as an
         xcframework).
    """
    if not config.product_filters and not config.target_filters:
        return None

    products_by_name = {p.name: p for p in package.products}
    targets_by_name = {t.name: t for t in package.targets}

    seed: Set[str] = set()
    for prod_name in config.product_filters or []:
        prod = products_by_name.get(prod_name)
        if prod is None:
            continue
        for tname in prod.targets:
            if tname in targets_by_name:
                seed.add(tname)
    for tname in config.target_filters or []:
        if tname in targets_by_name:
            seed.add(tname)

    deps_map = compute_internal_target_deps(package)
    closure: Set[str] = set()
    for name in seed:
        closure.add(name)
        closure.update(deps_map.get(name, set()))

    return {n for n in closure if targets_by_name[n].kind == TargetKind.REGULAR}


def _make_transitive_child_config(parent: Config, tp, child_work_dir: Path) -> Config:
    """Build a child Config for a transitive checkout.

    The child inherits the parent's output dir + platform flags +
    keep-work + verbosity, but everything that scopes the build to the
    parent's chosen products (target filters, version pinning,
    `--include-deps`'s post-archive walker, the user's revision check) is
    cleared. `product_filters` IS set, to the exact product names the
    parent's REGULAR targets imported from this identity — that
    `referenced_products` list flows down so the child only builds what
    the umbrella actually consumes. The child's `child_run` flag
    suppresses the shared-manifest read/write so the orchestrator owns
    those for the whole tree.

    Re-rooting the foreign checkout as the SPM root has a sharp edge:
    the foreign Package.swift can declare its own dev/CI dependencies
    (swift-docc-plugin, carton/wasm tooling, swift-format, …) that the
    umbrella's resolve never paid for. If we hand the raw checkout to
    Fetch, `swift package resolve` will follow those declarations and
    clone tens of MB of unrelated graphs (carton drags in swift-nio,
    swift-syntax, swift-tools-support-core). To prevent that, we copy
    the checkout into a per-child pre-staged dir and run
    `prune_child_manifest_for_products` over it: that drops top-level
    `#if … #endif` blocks mutating `package.dependencies` / `targets`
    and any unconditional `.package(url:)` entries the closure of the
    requested products doesn't actually need. The pruned dir is then
    fed to Fetch as `package_source`.

    `no_transitive_products=True` enforces the one-level recursion limit:
    children themselves do not recurse, which matches the design's
    "depth=1, visited identities" contract.
    """
    from dataclasses import replace

    pre_staged_dir = child_work_dir / "prestaged"
    if pre_staged_dir.exists():
        shutil.rmtree(pre_staged_dir)
    shutil.copytree(tp.checkout_path, pre_staged_dir, symlinks=True)
    # SPM publishes its `.build/checkouts/<pkg>/` trees with read-only
    # files (and sometimes read-only directories); the copy inherits
    # those modes. Subsequent Fetch/Stage on the child needs to write
    # into `.build/`, edit Package.swift, etc. Grant the owner write
    # access on the root pre-staged dir AND every file/dir under it.
    # os.walk doesn't yield `pre_staged_dir` itself in the children-of-
    # root loop below, so chmod it explicitly first — otherwise creating
    # new files at the top level (e.g. swift package resolve's
    # `Package.resolved`) would fail when SPM ships a read-only root.
    import stat as _stat
    try:
        top_mode = os.lstat(pre_staged_dir).st_mode
        os.chmod(pre_staged_dir, top_mode | _stat.S_IWUSR)
    except (OSError, NotImplementedError):
        pass
    for root, dirs, files in os.walk(pre_staged_dir):
        for name in dirs + files:
            p = os.path.join(root, name)
            try:
                mode = os.lstat(p).st_mode
                os.chmod(p, mode | _stat.S_IWUSR)
            except (OSError, NotImplementedError):
                pass
    prune_child_manifest_for_products(
        pre_staged_dir,
        list(tp.referenced_products),
        verbose=parent.verbose,
    )

    child = replace(
        parent,
        package_source=str(pre_staged_dir),
        user_version="",
        resolved_version="",
        product_filters=list(tp.referenced_products),
        target_filters=[],
        revision=None,
        include_deps=False,
        inspect_only=False,
        dry_run=False,
        binary_mode=False,
        no_transitive_products=True,
        best_effort_transitives=False,
        # Children build siblings from a single SPM package. SPM's
        # `package`-level access modifier (Swift 5.9+) lets sibling
        # targets see each other's `package func`/`package var` symbols
        # so long as they share a build context — which they do under
        # `swift build`/`xcodebuild archive`, but NOT when dedup-overlap
        # rewrites one sibling to a `.binaryTarget`. The rewritten
        # sibling becomes a foreign module and the parent target's
        # references to its `package` symbols fail to type-check
        # ("Cannot find '_fail' in scope" against `package func _fail`).
        # We trade dedup-overlap's static-link savings for correctness:
        # each child xcframework may statically embed its sibling deps'
        # code, which is acceptable bloat for the transitive sidecar
        # artifacts the umbrella's .swiftinterface only references for
        # module-import resolution.
        no_dedup_overlap=True,
        child_run=True,
        collected_entries=[],
        work_dir=child_work_dir,
    )
    return child


def _build_prebuilt_sibling_index(
    output_dir: Path,
    entries: Sequence["ManifestEntry"],
    package: "Package",
) -> Tuple[Dict[str, Path], Dict[str, str]]:
    """Build the two indexes the umbrella's planner consumes via
    `Config.prebuilt_sibling_xcframeworks` and
    `Config.prebuilt_sibling_identities`.

    Returns `(paths_by_name, identities_by_name)`.

    Inputs:
      - `entries` is the orchestrator's accumulated transitive run output
        (only transitive children; the umbrella's entries haven't been
        produced yet at this call site).
      - `package` is the parsed umbrella, whose
        `transitive_packages[*].referenced_products` enumerates every
        product name the umbrella's REGULAR targets reach via
        `.product(name: P, package: ident)`.

    Scope:
      Every freshly-built `<P>.xcframework` in `entries` whose file is
      on disk under `<output_dir>` ends up in `paths_by_name`. The fresh
      list is what THIS run produced — so leftover xcframeworks in
      `--output` from prior unrelated runs aren't included.

      Including products that are NOT in any transitive's
      `referenced_products` matters because a consumed sibling's
      `.swiftinterface` can reference modules the umbrella's source
      never explicitly imports — IssueReporting publicly re-exports
      `IssueReportingPackageSupport`, so `import IssueReportingPackageSupport`
      appears in IssueReporting's swiftinterface even though no target
      in swift-case-paths references it directly. xcodebuild's
      swiftinterface verifier still needs to resolve that module, so it
      must be present as a binaryTarget in the umbrella's overlay even
      though no `.product(name: …, package: …)` rewrite is needed for it.

    `identities_by_name` maps product_name → owning package identity
    when known (i.e. when that product appears in some transitive's
    `referenced_products`). For products auto-injected purely on the
    strength of being a freshly-built sibling, the identity is omitted
    — the Prepare-time edit applier treats a missing identity as
    "inject the binaryTarget overlay only; skip the `.product()`
    rewrite and `.package(url:)` strip steps". This is safe because the
    strip is identity-based: the referenced products in the same
    transitive package already trigger the strip, and the unreferenced
    siblings ride along.

    Returned paths are absolute (resolved). Prepare relativizes them
    against the staged package root when it writes the overlay block.
    """
    paths: Dict[str, Path] = {}
    identities: Dict[str, str] = {}

    for tp in package.transitive_packages:
        for prod in tp.referenced_products:
            identities.setdefault(prod, tp.identity)

    fresh_basenames = {
        e.name for e in entries if e.name.endswith(".xcframework")
    }
    resolved_output = output_dir.resolve()
    for basename in fresh_basenames:
        prod = basename[: -len(".xcframework")]
        xcfx = resolved_output / basename
        if not xcfx.exists():
            continue
        paths[prod] = xcfx

    # Drop identities for products that aren't actually in `paths` (no
    # corresponding xcframework on disk) — keeps the two indexes
    # consistent.
    identities = {k: v for k, v in identities.items() if k in paths}
    return paths, identities


_CROSS_SIBLING_PRODUCT_RE = re.compile(
    r"\.product\s*\(\s*name\s*:\s*\"([^\"\\\n]+)\"\s*,\s*package\s*:\s*\"([^\"\\\n]+)\""
)


def _expand_cross_sibling_referenced_products(transitives: List) -> List:
    """Expand each transitive's `referenced_products` by scanning every
    other transitive's checkout Package.swift for cross-sibling
    `.product(name: X, package: Y)` references.

    For every match where Y matches another transitive's identity
    (case-insensitive), X is added to that transitive's
    `referenced_products` (if not already present). Iterates to
    fixpoint so chains (A → B → C) all settle.

    Why this exists: Inspect only walks the umbrella's targets, so
    `referenced_products` lists products the umbrella's source actually
    imports. When transitive A's source imports a product X of
    transitive B but the umbrella never names X, X is missing from B's
    build set. The orchestrator strips `.package(url:)` for B from A's
    edited manifest (because *some* product of B is being consumed),
    which removes X's source-resolution path — and without
    X.xcframework as a sibling, A's `.swiftinterface` `import X` lines
    fail to resolve when A is consumed by the umbrella (or by a third
    transitive). Concrete trigger: swift-case-paths' `CasePaths` uses
    `.product(name: "XCTestDynamicOverlay", package: "xctest-dynamic-overlay")`;
    TCA's umbrella only references `IssueReporting` from
    xctest-dynamic-overlay; without expansion we never build
    XCTestDynamicOverlay.xcframework and swift-navigation's nested
    consume fails.

    Scope and safety:
      - Regex runs on the raw manifest text; each match's start
        offset is then checked against `_make_code_token_view(text)`
        (comments and string-literal bodies blanked to spaces) and
        rejected if the leading `.` of `.product` doesn't survive.
        A `.product(...)`-shaped token that appears inside a comment
        or string literal therefore can't drive an expansion.
      - Each surviving candidate is validated against the owning
        sibling's known products — a name that doesn't appear there
        is discarded (Plan would otherwise reject it as an unmatched
        product filter).
      - Both `Package.swift` and every version-specific
        `Package@swift-*.swift` are scanned. This is intentionally
        broader than Inspect's active-manifest selection: we'd rather
        build an extra harmless sibling than miss a ref that's only
        present in the toolchain-gated manifest the child will end
        up using. The declared-product gate above keeps the breadth
        safe.
      - Refs from `.testTarget(...)` blocks are deliberately kept in
        scope: test targets are stripped at prune-child time, but
        their product refs are a good signal of "this sibling
        transitively re-exports module X" — building X errs on the
        side of completeness.
      - Returns a fresh list with `_dc_replace`-d transitives whose
        `referenced_products` changed; transitives with no changes are
        returned unmodified.
    """
    if len(transitives) < 2:
        return transitives

    from dataclasses import replace as _dc_replace
    from .prepare import _make_code_token_view

    by_ident: Dict[str, object] = {
        tp.identity.lower(): tp for tp in transitives
    }
    # Set of product names declared by each sibling, used to reject
    # cross-sibling refs that the owning sibling doesn't actually expose
    # (would fail Plan's product-filter match downstream).
    declared_products: Dict[str, Set[str]] = {
        tp.identity.lower(): {p.name for p in getattr(tp, "products", [])}
        for tp in transitives
    }
    additions_log: List[Tuple[str, str, str]] = []

    changed = True
    rounds = 0
    MAX_ROUNDS = 16
    while changed and rounds < MAX_ROUNDS:
        changed = False
        rounds += 1
        for tp in list(by_ident.values()):
            manifest_texts: List[str] = []
            for manifest_path in sorted(tp.checkout_path.glob("Package*.swift")):
                try:
                    manifest_texts.append(manifest_path.read_text())
                except OSError:
                    continue
            for text in manifest_texts:
                code_view = _make_code_token_view(text)
                for m in _CROSS_SIBLING_PRODUCT_RE.finditer(text):
                    # Discard matches that fall inside a comment or string
                    # literal: in the code view those regions are blanked
                    # to spaces, so the leading `.` of `.product` won't
                    # survive.
                    if code_view[m.start()] != ".":
                        continue
                    prod, pkg = m.group(1), m.group(2)
                    key = pkg.lower()
                    if key == tp.identity.lower():
                        # Intra-package `.product()` ref — Inspect already
                        # accounts for these via the umbrella's own walk.
                        continue
                    target = by_ident.get(key)
                    if target is None:
                        continue
                    if prod in target.referenced_products:
                        continue
                    # Reject products the owning sibling doesn't declare.
                    # Plan would otherwise fail later with "no targets
                    # matched product filter <prod>" when the child config
                    # passes this name into product_filters.
                    if prod not in declared_products.get(key, set()):
                        continue
                    new_products = list(target.referenced_products) + [prod]
                    by_ident[key] = _dc_replace(
                        target, referenced_products=new_products
                    )
                    additions_log.append((target.identity, prod, tp.identity))
                    changed = True

    if additions_log:
        for owner, prod, importer in additions_log:
            bold(
                f"  cross-sibling expansion: {owner!r} += {prod!r} "
                f"(referenced by {importer!r})"
            )
    if changed and rounds >= MAX_ROUNDS:
        # The fixpoint didn't settle within the cap. Real SPM graphs are
        # shallow so saturating means something is feeding the expansion
        # endlessly (cyclic alias, manifest churn). Surface it so the
        # user can investigate instead of silently shipping a truncated
        # closure.
        bold(
            f"  cross-sibling expansion: bailed after {MAX_ROUNDS} rounds "
            f"with the closure still growing — please file an issue with "
            f"the full transitive list."
        )

    # Preserve original order from `transitives`.
    return [by_ident[tp.identity.lower()] for tp in transitives]


def _run_source_mode_with_transitives(config: Config) -> int:
    """Top-level source-mode entry point with in-process transitive recursion.

    Discovers external `.product(name:, package:)` dependencies of the
    root's REGULAR targets, builds each as its own sibling xcframework via
    a child `_run_source_mode` call against the resolved checkout, and
    then builds the umbrella against the parent run. Children write their
    artifacts into the same `--output` directory but never touch the
    shared output manifest; the orchestrator reads the old manifest once
    up front and writes a single merged manifest at the end.

    Why this exists: xcodebuild's SPM integration static-links every
    `.product(...)` reachable from the umbrella scheme into the umbrella's
    dylib regardless of the product's declared `type:`. The umbrella's
    consumer-visible `.swiftinterface` then `import`s modules whose
    `.swiftmodule` isn't anywhere on the consumer's search path. Producing
    sibling xcframeworks out-of-band (one fresh build per transitive
    checkout) is the only reliable fix.

    Failure mode is fail-fast by default: any transitive that fails Plan,
    Execute, or Verify aborts the whole run — shipping the umbrella with
    a missing sibling that its swiftinterface references would dangle at
    consume time. Pass `--best-effort-transitives` to continue with
    whatever transitives succeeded.

    Depth is hard-capped at one level: `_make_transitive_child_config`
    sets `no_transitive_products=True` on each child. The single-level
    rule covers every package in the integration matrix today; lifting it
    later only requires removing that flag and adding a visited-identity
    parameter through this function.
    """
    if config.no_transitive_products or config.child_run:
        return _run_source_mode(config)

    # Dry-run skips the transitive build loop entirely — emitting a plan
    # for the umbrella alone is the point of `--dry-run` (no xcodebuild
    # invocation, no 5-minute archives). The dry-run JSON envelope still
    # lists the transitive package identities so the campaign tooling can
    # see what would be built. The umbrella plan in dry-run lacks
    # `consume_external_sibling` edits because no sibling xcframeworks
    # exist on disk yet — that's accepted scope; dry-run is a preview, not
    # a faithful end-state simulation.
    if config.dry_run:
        return _run_source_mode(config)

    inspect_result = _source_mode_inspect(config)
    if isinstance(inspect_result, int):
        return inspect_result
    source_dir, staged_dir, package = inspect_result

    transitives = list(package.transitive_packages)
    if transitives:
        # Drop transitives reached only from root targets the user's
        # `--product`/`--target` filters exclude. `referencing_root_targets`
        # is the set of root REGULAR targets that reference the
        # transitive via `.product(name:, package:)`. The user-selected
        # closure is product backing targets ∪ explicit --target names,
        # expanded by internal sibling deps. With no filters set we keep
        # every transitive (default scope = all root REGULAR targets).
        selected_root_targets = _compute_selected_target_closure(config, package)
        if selected_root_targets is not None:
            from dataclasses import replace as _dc_replace

            kept: List = []
            dropped: List[str] = []
            product_trims: List[Tuple[str, List[str], List[str]]] = []
            for tp in transitives:
                if not tp.referencing_root_targets:
                    # Defensive: if inspect couldn't attribute the
                    # transitive, keep it rather than silently drop.
                    kept.append(tp)
                    continue
                if not any(t in selected_root_targets for t in tp.referencing_root_targets):
                    dropped.append(tp.identity)
                    continue
                # Identity-level keep: at least one selected root target
                # references this transitive. Now trim its `referenced_products`
                # to only the products imported by selected root targets, so
                # the child doesn't build (and the child planner doesn't try
                # to validate) products only consumed by deselected siblings.
                # If per-product attribution is missing (older inspect path),
                # fall back to keeping all referenced products.
                if tp.product_to_root_targets:
                    trimmed = [
                        p
                        for p in tp.referenced_products
                        if any(
                            rt in selected_root_targets
                            for rt in tp.product_to_root_targets.get(p, [])
                        )
                    ]
                    if trimmed and trimmed != list(tp.referenced_products):
                        product_trims.append(
                            (tp.identity, list(tp.referenced_products), list(trimmed))
                        )
                        tp = _dc_replace(tp, referenced_products=trimmed)
                    elif not trimmed:
                        # All referenced products belong to deselected
                        # root targets even though the union check matched
                        # (e.g. attribution races against the union list).
                        # Treat as dropped rather than building an empty
                        # child.
                        dropped.append(tp.identity)
                        continue
                kept.append(tp)
            if dropped:
                bold(
                    f"Skipping {len(dropped)} transitive(s) referenced only by "
                    f"deselected root targets: " + ", ".join(dropped)
                )
            for ident, before, after in product_trims:
                bold(
                    f"  trimming {ident!r} products to selected scope: "
                    f"{', '.join(sorted(before))} → {', '.join(sorted(after))}"
                )
            transitives = kept

    # Cross-sibling product expansion. Inspect populates
    # `tp.referenced_products` only from the umbrella's targets — so if
    # transitive A references product X of transitive B but the umbrella
    # doesn't, X is missing from B's `referenced_products` and we won't
    # build B's X.xcframework. When the orchestrator then consumes A as
    # a sibling, A's `.swiftinterface` imports X (because A's source
    # imports X) and xcodebuild fails to find module X — there's no
    # sibling overlay for it and `.package(url:)` for B has been
    # stripped from A's edited manifest (since some OTHER product of B
    # was consumed). Concrete case: swift-case-paths' `CasePaths`
    # references `.product(name: "XCTestDynamicOverlay", package:
    # "xctest-dynamic-overlay")`, but TCA's umbrella only references
    # `IssueReporting` from that package — without expansion, only
    # `IssueReporting.xcframework` ships and CasePaths.swiftinterface's
    # `import XCTestDynamicOverlay` dangles in swift-navigation's nested
    # build. The expansion runs to fixpoint to cover chains where the
    # newly-added product's owning package re-exports a third sibling.
    transitives = _expand_cross_sibling_referenced_products(transitives)

    if not transitives:
        # Self-contained package — no recursion needed.
        return _source_mode_after_inspect(config, source_dir, staged_dir, package)

    bold(
        f"Will also build {len(transitives)} transitive package(s) "
        f"as siblings: " + ", ".join(tp.identity for tp in transitives)
    )

    old_manifest = _read_output_manifest(config.output_dir)
    all_entries: List[ManifestEntry] = []
    # Seed `visited` with the root's own identity computed the same way
    # `_collect_referenced_package_products` keys its entries — by SPM
    # identity. `_identity_from_url` mirrors SPM's normalisation (last URL
    # path component, `.git` stripped, lowercased). Falling back to
    # `package.name` keeps the guard intact for local-path package
    # sources where `package_source` isn't a URL.
    root_identity = _identity_from_url(config.package_source) or package.name.lower()
    visited: Set[str] = {root_identity}

    parent_work = config.work_dir
    if parent_work is None:
        # Should never happen — main() always allocates a work_dir before
        # routing here — but fall back to a tempdir under the system tmp
        # so we don't crash on a manually-constructed Config.
        parent_work = Path(tempfile.mkdtemp(prefix="spm2xc-orchestrator-"))
        config.work_dir = parent_work

    for tp in transitives:
        ident_key = tp.identity.lower()
        if ident_key in visited:
            continue
        visited.add(ident_key)
        if not tp.checkout_path.exists():
            warn(
                f"  transitive {tp.identity!r}: checkout missing at "
                f"{tp.checkout_path}; skipping"
            )
            continue
        child_work_dir = parent_work / "transitives" / tp.identity
        child_work_dir.mkdir(parents=True, exist_ok=True)
        bold(f"\n=== transitive: {tp.identity} ===")
        # `_make_transitive_child_config` runs the pre-stage copytree,
        # the chmod walk, and the prune passes — all of which can raise
        # `PrepareUserError` (read-only checkout, broken manifest,
        # dump-package rejection of an edited file, ...). Wrap that call
        # in the same handler that protects `_run_source_mode` itself
        # so prep failures honour the fail-fast / best-effort contract
        # and are attributed to the offending transitive identity.
        try:
            child_config = _make_transitive_child_config(config, tp, child_work_dir)
            # Make earlier-built transitives visible to this child's planner.
            # Without this, a transitive whose targets reference another
            # transitive's product (e.g. swift-navigation imports CasePaths
            # from swift-case-paths) forces xcodebuild to compile the owning
            # package's macros from source in the nested workspace — where
            # swift-syntax is not resolved and the build fails. Refreshing
            # the index from `all_entries` after each successful iteration
            # lets the child consume already-built siblings as binaryTarget
            # overlays, matching what the umbrella does at the end of the
            # loop.
            (
                child_config.prebuilt_sibling_xcframeworks,
                child_config.prebuilt_sibling_identities,
            ) = _build_prebuilt_sibling_index(
                config.output_dir, all_entries, package
            )
            result = _run_source_mode(child_config)
        except _USER_FACING_ERRORS as exc:
            # Mirror main()'s clean-error path: a child's Fetch / Inspect /
            # Plan / Execute / Prepare error shouldn't crash with a
            # traceback at the orchestrator level, but it should still
            # abort the run unless the user opted into best-effort mode.
            phase = _phase_label_for(exc)
            print(
                _wrap(f"Error ({phase}, transitive {tp.identity!r}): {exc}", "red"),
                file=sys.stderr,
            )
            if config.best_effort_transitives:
                warn(
                    f"  continuing per --best-effort-transitives; "
                    f"{tp.identity!r} will not be shipped (any partial "
                    f"artifacts under {config.output_dir} are excluded from "
                    f"the manifest but not deleted — inspect before shipping)"
                )
                continue
            return exc.exit_code
        if result != 0:
            if config.best_effort_transitives:
                warn(
                    f"  transitive {tp.identity!r} failed (exit {result}); "
                    f"continuing per --best-effort-transitives (any partial "
                    f"artifacts under {config.output_dir} are excluded from "
                    f"the manifest but not deleted — inspect before shipping)"
                )
                continue
            return result
        all_entries.extend(child_config.collected_entries)

    # Build the umbrella last, as a child run so it deposits its entries
    # in the same merge bucket and skips the per-call manifest write.
    bold(f"\n=== umbrella: {package.name} ===")
    config.child_run = True
    config.collected_entries = []
    # Hand the umbrella's Plan the set of transitive sibling xcframeworks
    # we just deposited under `--output`. The planner emits one
    # `consume_external_sibling` edit per transitive product so Prepare
    # can splice `.binaryTarget(name: P, path: ...)` into the manifest
    # and collapse `.product(name: P, package: ident)` deps into bare
    # `"P"` strings — see `Config.prebuilt_sibling_xcframeworks` and
    # `_plan_external_sibling_consumption` for the full rationale.
    (
        config.prebuilt_sibling_xcframeworks,
        config.prebuilt_sibling_identities,
    ) = _build_prebuilt_sibling_index(
        config.output_dir, all_entries, package
    )
    umbrella_result = _source_mode_after_inspect(
        config, source_dir, staged_dir, package
    )
    if umbrella_result != 0:
        return umbrella_result
    all_entries.extend(config.collected_entries)

    _finalize_manifest(config, all_entries, old_manifest=old_manifest)
    return 0


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

    # Binary mode has no Inspect phase, so the source-mode auto-detect
    # can't fill in platforms from `Package.platforms[]`. Preserve
    # today's behavior: when the user passed zero --min-* flags, default
    # to iOS 15.0. (A future improvement could read platform metadata
    # from the resolved binary xcframework's Info.plist; for now,
    # explicit flags are required for non-iOS binary builds.)
    if not _enabled_platforms(config) and not config.no_ios:
        config.min_ios = "15.0"
    _validate_platforms_post_autodetect(config)

    artifacts = discover_binary_artifacts(config)
    plan = plan_binary_build(config, artifacts)
    for w in plan.warnings:
        warn(w)
    if config.dry_run_json:
        _emit_dry_run_json(plan, package=None, config=config)
    else:
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
