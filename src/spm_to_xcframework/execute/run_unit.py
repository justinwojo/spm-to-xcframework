"""Phase 4 — Execute · per-unit driver + source-plan orchestrator.

End-to-end pipeline for one source-mode build unit:

  1. Parallel archive for every enabled platform slice.
  2. For `synth_dynamic_library` units (scheme != framework_name),
     rename each slice's framework bundle from the synthetic scheme
     to the final framework name. (Plain dynamic units are already
     correctly named by xcodebuild.)
  3. Per-slice injection: swiftmodule, ObjC headers, resource bundles.
  4. Merge slices via `xcodebuild -create-xcframework`.
  5. Post-merge xcframework-wide injection: `.systemLibrary` Clang
     module shims and project-shipped bridge modulemaps.
  6. (Optional) `--include-deps` dependency xcframeworks (iOS-only).

`execute_source_plan` walks every source-mode unit in the plan,
sequencing units in topological order over internal target deps so
`--dedup-overlap` (default) can rewrite already-built siblings to
`.binaryTarget` between unit builds.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Set

from ..errors import ExecuteError
from ..log import bold, info, success, verbose_log, warn
from ..config import Config
from ..model import BuildUnit, DependencyXcframework, ExecutedUnit, PreparedPlan
from ..plan import compute_internal_target_deps, topo_order_units
from ..platforms import (
    PlatformSlice,
    _expected_slice_classes,
    _selected_slices,
)
from .archive import _archive_all_parallel
from .create_xcframework import (
    _build_dependency_xcframeworks,
    create_xcframework,
    detect_framework_type,
)
from .dedup import (
    _apply_dedup_overlap_substitutions,
    _apply_phantom_helper_dep_augmentation,
    _compute_dedup_substitutions,
    _compute_phantom_helper_deps,
    _demote_synth_product_in_manifest,
    _synth_dynamic_protected_targets,
)
from .inject_clang_bridge import inject_bridge_clang_modules
from .inject_clang_system import inject_system_clang_modules
from .inject_objc import inject_objc_headers
from .inject_resources import inject_resource_bundles
from .inject_swiftmodule import inject_swiftmodule
from .rename_framework import rename_framework_bundle


def _run_one_unit(
    unit: BuildUnit,
    *,
    prepared: PreparedPlan,
    config: Config,
) -> ExecutedUnit:
    """End-to-end Execute pipeline for a single source-mode build unit.

    1. Parallel archive for every enabled platform slice.
    2. For synth_dynamic_library units (scheme != framework_name),
       rename each slice's framework bundle from the synthetic scheme
       to the final framework name.
    3. Inject swiftmodule + ObjC headers + resource bundles per slice.
    4. Merge all slices via `xcodebuild -create-xcframework`.
    5. (Optional) walk dependency frameworks if `--include-deps` is set
       — iOS-only in v1; non-iOS slices don't carry dep artifacts.
    """
    assert config.work_dir is not None
    work_dir = config.work_dir
    staged_dir = prepared.package.staged_dir

    selected = _selected_slices(config)
    if not selected:
        raise ExecuteError(
            "no platforms selected; pass --min-ios / --min-macos / "
            "--min-tvos / --min-maccatalyst / --min-watchos / --min-visionos"
        )

    archive_slices = _archive_all_parallel(
        unit,
        selected=selected,
        staged_dir=staged_dir,
        work_dir=work_dir,
        verbose=config.verbose,
    )

    for sid, s in archive_slices.items():
        if s.framework_path is None:
            raise ExecuteError(
                f"{unit.name}: {sid} archive completed but "
                f"{unit.scheme}.framework not found under "
                f"{s.archive_path}/Products/. The synthetic dynamic-library "
                f"product did not produce a framework bundle."
            )

    # synth_dynamic_library rename: xcodebuild emitted
    # `<scheme>.framework` for each slice; consumers expect
    # `<framework_name>.framework`. Rename in place before any injection
    # pass runs so downstream code sees the final name.
    if unit.scheme != unit.framework_name:
        for sid, s in archive_slices.items():
            s.framework_path = rename_framework_bundle(
                s.framework_path,
                new_name=unit.framework_name,
                verbose=config.verbose,
            )

    primary_id, primary_slice = next(iter(archive_slices.items()))
    fw_name = primary_slice.framework_path.stem
    success(
        f"  {unit.name}: {primary_id} "
        f"{primary_slice.framework_path.relative_to(primary_slice.archive_path.parent)}"
    )

    # Injection passes — every slice. The `variant` label is the slice_id
    # so user-visible logs identify which platform the diagnostic
    # belongs to.
    for sid, s in archive_slices.items():
        inject_swiftmodule(
            fw_path=s.framework_path,
            fw_name=fw_name,
            scheme=unit.scheme,
            dd_path=s.dd_path,
            variant=sid,
            verbose=config.verbose,
            extra_module_names=unit.source_targets,
        )
        inject_objc_headers(
            package=prepared.package,
            product_name=unit.name,
            fw_name=fw_name,
            fw_path=s.framework_path,
            verbose=config.verbose,
        )
        inject_resource_bundles(
            fw_path=s.framework_path,
            fw_name=fw_name,
            dd_path=s.dd_path,
            variant=sid,
            verbose=config.verbose,
        )

    output_xcframework = config.output_dir / f"{unit.name}.xcframework"
    info(f"  Creating {unit.name}.xcframework...")
    create_xcframework(
        output_xcframework=output_xcframework,
        frameworks=[s.framework_path for s in archive_slices.values()],
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
    # Bundle any project-shipped clang modules the swiftinterface imports
    # but the primary framework's modulemap doesn't declare (e.g. WCDB's
    # `WCDB_Private` shipped via `src/bridge/module.modulemap`). Distinct
    # from the `.systemLibrary` pass above: that one walks the parsed
    # SPM model, this one matches by parsing the framework's emitted
    # swiftinterface against modulemaps actually shipped in the package
    # source tree.
    inject_bridge_clang_modules(
        xcframework_path=output_xcframework,
        package=prepared.package,
        fw_name=fw_name,
        verbose=config.verbose,
    )
    fw_type = detect_framework_type(output_xcframework)
    success(f"  {unit.name}.xcframework ready [{fw_type}]")

    dep_xcframeworks: List[DependencyXcframework] = []
    if prepared.plan.include_deps:
        ios_dev = archive_slices.get("ios-arm64")
        ios_sim = archive_slices.get("ios-simulator")
        if ios_dev is not None and ios_sim is not None:
            dep_xcframeworks = _build_dependency_xcframeworks(
                unit=unit,
                package=prepared.package,
                device_slice=ios_dev,
                sim_slice=ios_sim,
                primary_fw_name=fw_name,
                output_dir=config.output_dir,
                verbose=config.verbose,
            )
        else:
            # iOS not selected — main() already rejects this case before
            # Execute runs, so reaching here is a programmer error.
            warn(
                f"  {unit.name}: --include-deps skipped (iOS device + "
                "simulator slices not in this build)"
            )

    return ExecutedUnit(
        name=unit.name,
        slices=list(archive_slices.values()),
        xcframework_path=output_xcframework,
        framework_name=fw_name,
        framework_type=fw_type,
        expected_language=unit.language,
        expected_slice_classes=_expected_slice_classes(config),
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

    Build-unit ordering: when dedup-overlap is enabled (default), units
    are walked in topological order over their internal target deps so
    each leaf builds before any sibling that imports it. Between unit
    builds, already-built siblings get rewritten in `Package.swift` from
    `.target(...)` to `.binaryTarget(path: ...)` pointing at the freshly
    produced xcframework. The umbrella product (e.g. Stripe) then links
    dynamically against the sibling xcframeworks instead of statically
    embedding their Mach-O. See REWRITE_DESIGN.md §5.4 dedup-overlap.

    `--no-dedup-overlap` reverts to the legacy behavior: planner-order
    walk, no inter-unit manifest mutation, sibling targets get statically
    re-compiled into every umbrella product that imports them.
    """
    if config.work_dir is None:
        raise ExecuteError("internal error: config.work_dir was not allocated before Execute")
    archive_units = [u for u in prepared.plan.build_units if u.archive_strategy != "copy-artifact"]
    if not archive_units:
        return []

    if config.no_dedup_overlap:
        ordered_units: List[BuildUnit] = list(archive_units)
    else:
        ordered_units = topo_order_units(archive_units, prepared.package)

    target_to_unit: Dict[str, BuildUnit] = {}
    for u in ordered_units:
        for t in u.source_targets:
            target_to_unit.setdefault(t, u)

    target_deps = (
        compute_internal_target_deps(prepared.package)
        if not config.no_dedup_overlap
        else {}
    )
    # Mutable: starts populated from the planner's synth_* edits, then
    # shrinks every time a synth unit finishes building (and we demote
    # its product to automatic-library shape, removing the SPM
    # binary-only-product invariant violation that previously blocked
    # `.binaryTarget` substitution for its targets). See Codex round-3
    # High: a static "never dedup protected targets" rule left
    # duplicate-symbol consumer links unfixed.
    synth_dynamic_protected = (
        _synth_dynamic_protected_targets(prepared.plan.package_swift_edits)
        if not config.no_dedup_overlap
        else set()
    )
    # Build the unit → planner-injected synth product mapping once.
    # synth_dynamic_library edits: unit.scheme == edit.product_name
    # (and unit.framework_name is the original product name, post-
    # rename). synth_library (--target T) edits: unit.synthetic and
    # unit.scheme == edit.product_name == T. Either way, the planner
    # never emits two synth edits for one unit, so an "encounter the
    # match, break" loop suffices.
    unit_to_synth_product: Dict[str, str] = {}
    unit_to_synth_targets: Dict[str, List[str]] = {}
    if not config.no_dedup_overlap:
        edits_by_product = {
            e.product_name: e for e in prepared.plan.package_swift_edits
            if e.kind in ("synth_dynamic_library", "synth_library")
        }
        for u in ordered_units:
            edit = edits_by_product.get(u.scheme)
            if edit is not None:
                unit_to_synth_product[u.name] = edit.product_name
                unit_to_synth_targets[u.name] = list(edit.targets)

    bold(f"\nExecuting {len(ordered_units)} build unit(s)...")
    config.output_dir.mkdir(parents=True, exist_ok=True)
    results: List[ExecutedUnit] = []
    built_by_unit: Dict[str, ExecutedUnit] = {}
    staged_dir = prepared.package.staged_dir
    # Set of every sibling target name that has been binary-substituted on
    # any prior unit's dedup-overlap pass. Used by the phantom-helper
    # augmentation: a helper that was promoted to a sibling xcframework
    # by an earlier unit needs to be added to the current unit's umbrella
    # deps even if it isn't part of THIS pass's substitutions list (the
    # pass dedupes same-name re-substitutions, so an InternalCollections-
    # Utilities that became a binary target two units ago doesn't show up
    # again — but it's still a sibling whose slice the umbrella needs).
    substituted_target_names: Set[str] = set()

    for unit in ordered_units:
        if not config.no_dedup_overlap:
            substitutions = _compute_dedup_substitutions(
                unit=unit,
                target_to_unit=target_to_unit,
                built_by_unit=built_by_unit,
                target_deps=target_deps,
                synth_dynamic_protected=synth_dynamic_protected,
            )
            if substitutions:
                _apply_dedup_overlap_substitutions(
                    staged_dir=staged_dir,
                    substitutions=substitutions,
                    unit_name=unit.name,
                    verbose=config.verbose,
                )
                for name, _path in substitutions:
                    substituted_target_names.add(name)
            # Inject "phantom helper" deps on the umbrella's source
            # target — substituted sibling targets that are in the
            # transitive but not the direct dep closure. Without this,
            # SPM doesn't add the helper's binary-target slice to the
            # umbrella's `FRAMEWORK_SEARCH_PATHS`, and the umbrella
            # compile fails to resolve `import <Helper>` calls embedded
            # in sibling `.swiftinterface` files. Runs unconditionally
            # (even when this unit had zero new substitutions): the
            # umbrella may still need helpers that were registered on
            # earlier units' passes. See
            # `_compute_phantom_helper_deps` for the predicate.
            phantom_helpers = _compute_phantom_helper_deps(
                unit=unit,
                package=prepared.package,
                substituted_target_names=substituted_target_names,
                target_deps=target_deps,
            )
            if phantom_helpers:
                _apply_phantom_helper_dep_augmentation(
                    staged_dir=staged_dir,
                    phantom_helpers=phantom_helpers,
                    unit_name=unit.name,
                    verbose=config.verbose,
                )

        executed = _run_one_unit(unit, prepared=prepared, config=config)
        results.append(executed)
        built_by_unit[unit.name] = executed

        # Post-build: if this unit owns a planner-injected synth
        # product, demote it from `.dynamic` to automatic-library and
        # drop its targets from the protected set so the NEXT unit's
        # dedup pass can substitute them with `.binaryTarget`. The
        # demote is what makes the manifest valid for binary-only
        # products — without it, the surviving
        # `.library(type: .dynamic, targets: [T])` would crash SPM's
        # next manifest parse the moment we rewrite T to .binaryTarget.
        if not config.no_dedup_overlap and unit.name in unit_to_synth_product:
            _demote_synth_product_in_manifest(
                staged_dir=staged_dir,
                product_name=unit_to_synth_product[unit.name],
                unit_name=unit.name,
                verbose=config.verbose,
            )
            for t in unit_to_synth_targets[unit.name]:
                synth_dynamic_protected.discard(t)
    return results
