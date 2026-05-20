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
    plan_binary_build,
    plan_source_build,
    print_plan,
)
from .log import _wrap, die, dim, info, warn
from .model import ExecutedUnit, Language
from .platforms import _enabled_platforms
from .prepare import prepare
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
    parser.add_argument("--min-ios", default="15.0",
                        help="Minimum iOS deployment target (default: 15.0). Pass --no-ios to skip iOS.")
    parser.add_argument("--no-ios", action="store_true",
                        help="Skip iOS entirely; requires at least one other --min-* flag.")
    parser.add_argument("--min-macos", default=None,
                        help="Minimum macOS deployment target (e.g. 11.0). Adds the macOS slice.")
    parser.add_argument("--min-maccatalyst", default=None,
                        help="Minimum Mac Catalyst deployment target (e.g. 15.0). Adds the Mac Catalyst slice.")
    parser.add_argument("--min-tvos", default=None,
                        help="Minimum tvOS deployment target (e.g. 15.0). Adds tvOS device + simulator slices.")
    parser.add_argument("--min-watchos", default=None,
                        help="Minimum watchOS deployment target (e.g. 8.0). Adds watchOS device + simulator slices.")
    parser.add_argument("--min-visionos", default=None,
                        help="Minimum visionOS deployment target (e.g. 1.0). Adds visionOS device + simulator slices.")
    parser.add_argument("--include-deps", action="store_true",
                        help="Also build xcframeworks for transitive dependencies (iOS-only in v1)")
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
        include_deps=ns.include_deps,
        binary_mode=ns.binary,
        verbose=ns.verbose,
        dry_run=ns.dry_run,
        keep_work=ns.keep_work,
        no_cleanup_stale=ns.no_cleanup_stale,
        no_dedup_overlap=ns.no_dedup_overlap,
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

    # Validate that the user has selected at least one platform.
    enabled = _enabled_platforms(config)
    if not enabled:
        die("No platforms selected. Pass at least one of --min-ios, --min-macos, "
            "--min-maccatalyst, --min-tvos, --min-watchos, --min-visionos. "
            "(--no-ios disables iOS, so combine it with one of the others.)")

    # --include-deps is iOS-only in v1. Fail-fast if iOS is disabled; warn
    # if iOS is enabled alongside non-iOS platforms.
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
    source_dir = fetch_source(config)
    staged_dir = stage_source(config, source_dir)
    package = inspect_package(config, staged_dir)

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
