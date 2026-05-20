"""Phase 4 — Execute · dedup-overlap manifest rewriting.

After a sibling build unit finishes, future units that transitively
depend on a target it owns can be re-pointed at the sibling's
xcframework via `.binaryTarget`. That avoids re-building the same
target's sources in every dependent unit and prevents duplicate-symbol
linker errors when more than one unit ships the same Swift module.

The rewriter is purely textual — it mutates the active `Package.swift`
between units, deferring to `prepare.edit_replace_with_binary_target`
for the actual splice — and the substitutions are idempotent across
runs.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, List, Mapping, Sequence, Set, Tuple

from ..errors import ExecuteError, PrepareUserError
from ..log import info, verbose_log
from ..model import BuildUnit, ExecutedUnit, PackageSwiftEdit


def _synth_dynamic_protected_targets(
    edits: Iterable[PackageSwiftEdit],
) -> Set[str]:
    """Return every target referenced by a synthetic dynamic-library
    edit — BOTH `synth_dynamic_library` (re-export of an existing
    non-dynamic product) AND `synth_library` (`--target T` escape
    hatch). Both kinds are applied through `_invoke_swift_add_product`,
    which always passes `--type dynamic-library`; the resulting product
    is `.library(name: …, type: .dynamic, targets: […])`.

    Rewriting one of these targets to `.binaryTarget(...)` mid-run
    would leave that surviving dynamic-library product pointing at a
    binary-only target — SPM rejects that shape at the next archive's
    manifest parse with:

        "invalid type for binary product; products referencing only
         binary targets must be executable or automatic library
         products"

    (See the matching comment block at `plan._is_binary_only_product`.)
    """
    protected: Set[str] = set()
    for edit in edits:
        if edit.kind in ("synth_dynamic_library", "synth_library"):
            protected.update(edit.targets)
    return protected


def _compute_dedup_substitutions(
    *,
    unit: BuildUnit,
    target_to_unit: Mapping[str, BuildUnit],
    built_by_unit: Mapping[str, ExecutedUnit],
    target_deps: Mapping[str, Set[str]],
    synth_dynamic_protected: Iterable[str] = (),
) -> List[Tuple[str, Path]]:
    """Pure helper: derive `(target_name, xcframework_path)` pairs to
    rewrite in `Package.swift` before `unit` builds.

    A substitution is emitted for each transitively-imported internal
    target T owned by a sibling build unit, when:

      1. T is not part of `unit.source_targets` (own targets are
         linked normally — substituting them would build them out of
         the wrong source).
      2. The sibling build unit owns T (and only T) — i.e. its
         `source_targets == [T]`. The xcframework that unit emits is
         named after the product (e.g. `StripeIssuing.xcframework`
         for a single-target product), so the binary's contained
         module IS T and re-pointing `.target(name: T)` at it is a
         clean 1:1 swap. Multi-target sibling units (e.g.
         `.library(name: "Kit", targets: ["A", "B"])` → one
         `Kit.xcframework` whose framework binary is the product, not
         the individual target) are SKIPPED — re-pointing
         `.target(name: "A")` at `Kit.xcframework` would cross
         module boundaries silently, and assigning the same path to
         both A and B is malformed in SPM. Codex P2 r5 regression.
      3. The sibling has already finished building (so we have a
         path to point at). If the sibling hasn't run yet — cycle in
         the unit graph, or filtered out of this run — leaving the
         source target alone is the safe fallback.
      4. T is NOT currently in `synth_dynamic_protected`. The set
         starts with every target wrapped by a planner-injected synth
         (`synth_dynamic_library` or `synth_library`) and shrinks
         after each synth unit finishes — `execute_source_plan`
         demotes the now-built synth product to automatic-library
         shape (strips `type: .dynamic`) and drops the unit's targets
         from the set. While T is still protected (its owning synth
         hasn't built yet), rewriting `.target(name: T)` to
         `.binaryTarget` would leave a still-`.dynamic` product
         pointing at a binary-only target — SPM rejects that at the
         next manifest parse. Skipping here is the conservative
         fallback; once protection drops, the next round's dedup
         pass picks T up normally. (Codex round-3 High.)

    Returned pairs are deduplicated and emitted in deterministic
    sort order (the inner loop walks `sorted(target_deps[src_t])`),
    so the audit log is stable across runs.
    """
    substitutions: List[Tuple[str, Path]] = []
    seen: Set[str] = set()
    own = set(unit.source_targets)
    protected = set(synth_dynamic_protected)
    for src_t in unit.source_targets:
        for dep_t in sorted(target_deps.get(src_t, set())):
            if dep_t in own or dep_t in seen:
                continue
            sibling = target_to_unit.get(dep_t)
            if sibling is None or sibling.name == unit.name:
                continue
            # Multi-target sibling units don't participate: their
            # xcframework was built as the product (one framework
            # binary), not as the individual target T, so it isn't a
            # valid 1:1 stand-in for `.target(name: T, ...)`. Falling
            # through here means the consumer keeps `.target(...)`
            # for T and SPM statically embeds T's symbols (legacy
            # behavior). (Codex P2 r5 regression.)
            if list(sibling.source_targets) != [dep_t]:
                continue
            sibling_executed = built_by_unit.get(sibling.name)
            if sibling_executed is None:
                continue
            if dep_t in protected:
                # The synth product that wraps dep_t hasn't been
                # demoted yet (its owning unit hasn't finished
                # building, or we're being called out of topo order).
                # Rewriting dep_t to .binaryTarget right now would
                # invalidate the surviving .library(type: .dynamic,
                # targets: [dep_t]) product — SPM rejects binary-only
                # dynamic products at the next manifest parse. The
                # post-build demote in `execute_source_plan` removes
                # dep_t from `protected` once its synth unit
                # completes; the next iteration's pass picks it up
                # then. (Codex round-3 High.)
                continue
            substitutions.append((dep_t, sibling_executed.xcframework_path))
            seen.add(dep_t)
    return substitutions


def _apply_dedup_overlap_substitutions(
    *,
    staged_dir: Path,
    substitutions: Sequence[Tuple[str, Path]],
    unit_name: str,
    verbose: bool,
) -> None:
    """Mutate the active Package.swift in `staged_dir` so each
    `(target_name, xcframework_path)` pair becomes a `.binaryTarget`
    declaration before the next unit's `xcodebuild archive` runs.

    Edits are idempotent — a target already shaped as `.binaryTarget`
    is left alone — and applied in stable order so the audit log is
    deterministic across runs. The active manifest is the same file
    `apply_package_swift_edits` chose during Prepare; we re-resolve it
    here rather than caching the path on `Config`, so a toolchain
    change between Prepare and Execute (vanishingly unlikely, but
    cheap to defend against) wouldn't desync the two phases.

    Side effect: writes the edited manifest text. The previous
    `apply_package_swift_edits` snapshot at `.original-Package.swift`
    is left untouched — that's the user's recovery handle if a
    dedup-overlap edit is later reported to be wrong.
    """
    from ..prepare import (
        _assert_no_unsupported_swift_constructs,
        _select_active_manifest,
        edit_replace_with_binary_target,
    )
    manifest_path = _select_active_manifest(staged_dir)
    if not manifest_path.is_file():
        raise ExecuteError(
            f"dedup-overlap substitution: cannot read active manifest at "
            f"{manifest_path} for unit {unit_name!r}"
        )
    text = manifest_path.read_text()
    # Re-assert the unsupported-construct guard here. `apply_package_swift_edits`
    # would have run it during Prepare — but only if the planner emitted at least
    # one edit. When Prepare takes its no-op path (no package_swift_edits), the
    # guard is skipped, and an Execute-time dedup edit on a manifest with
    # triple-quoted strings, `#"..."#` raw strings, or `\(...)` interpolation
    # would silently mis-parse via `_make_code_token_view` (which only tracks
    # `"..."` strings). Running the guard here closes that gap.
    # (Codex P2 round-4 regression.)
    try:
        _assert_no_unsupported_swift_constructs(text)
    except PrepareUserError as exc:
        raise ExecuteError(
            f"dedup-overlap substitution failed for unit {unit_name!r}: "
            f"the active Package.swift uses a Swift construct the dedup-overlap "
            f"editor can't reason about. {exc} Re-run with --no-dedup-overlap to "
            f"disable the inter-unit binaryTarget rewrite for this package."
        ) from exc
    edited = text
    applied: List[str] = []
    skipped: List[str] = []
    # SPM's `.binaryTarget(path: ...)` rejects two shapes that look
    # superficially fine: ABSOLUTE paths ("path expected to be relative
    # to package root") and paths resolved against the wrong base. We
    # have an absolute xcframework path (resolved at CLI parse time)
    # and a staged manifest in a tempdir; bridge the two with
    # `os.path.relpath`, which produces `../../..` chains as needed.
    # `dump-package` and `xcodebuild archive` both accept upward
    # `../`-relative paths (verified against `stripe-ios --target
    # StripeCore --product Stripe` smoke). Use the resolved staged_dir
    # as the base so symlinks in either direction don't desync the
    # rel-path computation.
    staged_root = staged_dir.resolve()
    for target_name, xcframework_path in substitutions:
        abs_path = Path(xcframework_path).resolve()
        rel_path = os.path.relpath(abs_path, staged_root)
        try:
            new_text = edit_replace_with_binary_target(edited, target_name, rel_path)
        except PrepareUserError as exc:
            raise ExecuteError(
                f"dedup-overlap substitution failed for unit {unit_name!r} "
                f"target {target_name!r}: {exc}"
            ) from exc
        if new_text != edited:
            applied.append(f"{target_name} -> {abs_path.name}")
            edited = new_text
        else:
            skipped.append(target_name)
    if edited != text:
        manifest_path.write_text(edited)
    if applied:
        info(
            f"  {unit_name}: dedup-overlap rewrote "
            f"{len(applied)} sibling target(s) to .binaryTarget: "
            + ", ".join(applied)
        )
    if skipped:
        verbose_log(
            verbose,
            f"  {unit_name}: dedup-overlap left {len(skipped)} target(s) "
            f"unchanged (already .binaryTarget): " + ", ".join(skipped),
        )


def _demote_synth_product_in_manifest(
    *,
    staged_dir: Path,
    product_name: str,
    unit_name: str,
    verbose: bool,
) -> bool:
    """Strip `type: .dynamic` from the planner-injected
    `.library(name: <product_name>, ...)` declaration in the active
    Package.swift. Returns True iff the manifest was actually mutated
    (False on idempotent re-entry, when the product is already in
    automatic-library shape).

    Called immediately AFTER a synth unit finishes archiving. The
    surviving automatic-library product keeps the consumer-visible
    product name alive (downstream consumers still depend on it by
    name) but no longer forces `.dynamic` linkage — which unblocks
    the next unit's `replace_with_binary_target` substitution for the
    same target. (See Codex round-3 High: simply skipping dedup for
    protected targets violated the duplicate-symbol-prevention
    invariant.)

    Idempotent: if the product is already automatic-shape (manual
    re-run, prior demote, etc.) the function returns False without
    touching the file. Raises `ExecuteError` if the demote helper
    can't locate the product or finds it in a malformed state.
    """
    from ..prepare import (
        _assert_no_unsupported_swift_constructs,
        _select_active_manifest,
        edit_demote_synthetic_product,
    )
    manifest_path = _select_active_manifest(staged_dir)
    if not manifest_path.is_file():
        raise ExecuteError(
            f"synth-product demote: cannot read active manifest at "
            f"{manifest_path} for unit {unit_name!r}"
        )
    text = manifest_path.read_text()
    try:
        _assert_no_unsupported_swift_constructs(text)
    except PrepareUserError as exc:
        raise ExecuteError(
            f"synth-product demote failed for unit {unit_name!r}: "
            f"the active Package.swift uses a Swift construct the editor "
            f"can't reason about. {exc} Re-run with --no-dedup-overlap to "
            f"disable post-build product demotion (and inter-unit "
            f"binaryTarget substitution) for this package."
        ) from exc
    try:
        edited = edit_demote_synthetic_product(text, product_name)
    except PrepareUserError as exc:
        raise ExecuteError(
            f"synth-product demote failed for unit {unit_name!r} "
            f"product {product_name!r}: {exc}"
        ) from exc
    if edited == text:
        verbose_log(
            verbose,
            f"  {unit_name}: synth product {product_name!r} already "
            f"automatic-shape (no demote needed)",
        )
        return False
    manifest_path.write_text(edited)
    info(
        f"  {unit_name}: demoted synthetic product {product_name!r} to "
        f"automatic library (unblocks dedup-overlap for its target(s))"
    )
    return True
