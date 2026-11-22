# Import-closure synthesis — design & work plan

**Status:** approved scope (owner, 2026-07-22). This is the one converter investment funded from
the swift-bindings next-impact review; the per-SDK converter pilots (Sentry/Adyen/Braintree/
Datadog/PostHog mechanisms) were explicitly **declined** — those vendors ship official binary
xcframeworks, so a .NET consumer should feed the vendor artifact to the binding generator
directly. This tool's real audience is source-only SPM packages, and that is what this feature
serves.

**Goal in one sentence:** when the closure gate finds that a produced interface publicly imports
a module owned by an *internal* package target that was never exposed as a product, synthesize a
dynamic product for that target, build it into staging, and re-check — iterating to a bounded
fixed point — instead of refusing.

## Evidence

Both canaries build **all** their products cleanly and then fail the closure gate on exactly one
internal target (corpus receipts, 2026-07 S09 sweep):

| Package | Built/Verified | Missing module | Receipt diagnostic |
|---|---|---|---|
| FlexLayout 2.2.3 | 2/2, Failed: 0 | `FlexLayoutYogaKit` | "required module … imported by the produced interface but no xcframework was produced for it" (phase `closure`) |
| Pulse 5.2.3 | 3/3, Failed: 0 | `PulseObjCHelpers` | same shape |

Each conversion is one synthesized module away from success. The refusal itself was the correct
prior step (fail loudly beats shipping an import-open set — graph_closure.py:30-33 states this
boundary deliberately); this feature is the next step: *produce* the closure the gate already
computes.

## What already exists (reuse, don't rebuild)

- **Detection is done.** `compute_missing_closure_modules` (graph_closure.py:332) yields each
  hole with graph ownership (`_graph_ownership`, :235 — internal target names are already in the
  owned set) and a classified reason (`_classify_reason`, :296: upstream failure / `static_only`
  / owned-but-absent).
- **Synthesis machinery is done.** Pass 3 of planning (`_auto_synth_sibling_units`, plan.py:768)
  already promotes internal REGULAR targets to synthetic dynamic products, with name allocation
  (`_allocate_synthetic_product_name`, plan.py:330) and manifest injection via
  `swift package add-product` (`_apply_single_synth_edit`, prepare.py:4007;
  `_invoke_swift_add_product`, prepare.py:4060), round-trip-validated (prepare.py:4835).
- **A fixpoint precedent exists**: `_expand_cross_sibling_referenced_products` (cli.py:1884)
  already iterates to a fixed point over cross-sibling product references.
- **Staging/promotion needs no change.** The closure gate (`_run_import_closure_gate`,
  cli.py:947, invoked at cli.py:1171) runs inside the run-scoped staging dir, before
  `_promote_staged_artifacts` (cli.py:780). A synthesis loop that builds into staging and only
  exits 0 when closed keeps promotion atomic for free — partial synthesis never contaminates
  final output.

## The two real gaps

1. **Pass 3 is one-shot and plan-time.** It walks internal deps statically from already-planned
   units; it never re-inspects the *built* swiftinterfaces, so an import the source-level walk
   missed (re-exports, conditional imports, modulemap identities) is only discovered by the gate,
   after which the run dies.
2. **Pass 3's eligibility gate excludes exactly the canaries.** It promotes only pure-Swift
   helpers or pure-C shims (plan.py:899-906) and skips targets with linker settings
   (plan.py:739). `FlexLayoutYogaKit` and `PulseObjCHelpers` are ObjC/Mixed internal targets —
   consciously left out today ("users can still opt skipped helpers in via `--target T`",
   plan.py:882-883). The loop is the automated form of that opt-in.

## Design

### The loop

Replace the hard refusal in `_run_import_closure_gate` (cli.py:1000) with a bounded fixed point,
still entirely pre-promotion:

1. Run `compute_missing_closure_modules` on the staged set (unchanged).
2. For each hole, map the module back to its owning `Target` via the already-computed ownership
   model (module name ↔ target name ↔ modulemap top-level module — reuse the identity logic in
   `provided_modules_in_xcframework`/`_graph_ownership`; a module that cannot be mapped to
   exactly one owned target is a refusal, not a guess).
3. For each *eligible* target (below): allocate a synthetic product name, apply the
   `add-product` edit, build + verify the new unit into the same staging dir, injecting
   interfaces/headers exactly as existing units do.
4. Re-run step 1. Terminate when: closed (success), no progress (a round synthesized nothing new
   or the missing set didn't shrink), an iteration cap is hit (default: the package's internal
   REGULAR-target count, hard-capped at ~8), or a hole is ineligible (refusal).
5. Every round is receipt-visible: discovered module, mapped target, synthesized product name,
   build outcome, and — on refusal — the same classified reason the gate reports today.

Cycle safety: SPM target graphs are acyclic, but track synthesized-target identities anyway so a
mapping bug can never loop; a repeated identity is a hard no-progress stop.

### Eligibility (conservative, explicit refusals for the rest)

- **Eligible:** internal `REGULAR` targets — pure Swift, pure C, **and (new) ObjC/Mixed** —
  reachable in the package's own graph. Extending past pass 3's pure-Swift/pure-C gate is the
  point of the feature; it is what the canaries need. C++ internals (Yoga-style) are eligible as
  long as they build as a dynamic framework — library evolution constrains only Swift.
- **Refuse with a precise receipt reason** (never retry): `static_only` modules (X-002's wall —
  the classifier already names them, graph_closure.py:277); tool/plugin/macro targets;
  executable-only products; `BINARY` targets without a usable slice; targets whose build trips
  the Xcode library-evolution walls (surface the existing diagnostics.py classification);
  unmappable modules. Targets with linker settings (plan.py:739) stay refused in the first
  iteration of this feature; revisit only if a real package needs them.
- **Never** rewrite upstream source, strip resilience, or disable library evolution to force a
  target through (the existing hard rule stands).

### X-007 interaction (must handle, not optional)

KNOWN-ISSUES X-007: a mixed-framework umbrella `#import <Primary/dep.h>` dangles when the C/ObjC
dependency ships as a *sibling* framework — and its own root-cause hypothesis is that the
header-fold "does not run for a synthesized dynamic product whose name/target does not match the
fold's product_name filter." Synthesizing ObjC/Mixed internal products is exactly the shape that
reproduces X-007. This work must reconcile umbrella imports for synthesized siblings (qualify the
import to the sibling module, or fold headers where the primary demands it) and should close or
materially advance X-007 in the process. Yams→CYaml and TelemetryDeck→TelemetryClient are the
X-007 repro cases to check alongside the two canaries.

### Receipt schema

New fields are additive under `schema_version` 1 **only if** `read_receipt`'s version check
(receipt.py:288) is left as-is and the new fields are optional on read (`from_json` already
ignores unknown keys, receipt.py:216). Decision for the implementer: keep v1 + optional
`closure_synthesis` block (preferred — downstream readers in the corpus harness pin v1), and
record per-round entries: `{round, module, target, synthesized_product, outcome,
refusal_reason?}`. Do not repurpose existing fields; `missing_required` keeps its meaning (holes
remaining at exit).

### Out of scope (this feature)

- Lifting the external-transitive depth cap (cli.py:1775-1782). Different axis: that cap governs
  *external package* children; this loop governs *in-package internal targets*. If a real hole
  maps to a depth-2 external product, refuse with the existing reason — don't grow both frontiers
  in one change.
- The per-SDK mechanisms (manifest pruning for Sentry, binary artifact discovery for Adyen,
  minimum-OS product selection for Braintree, vendored C/C++ headers for PostHog, the libarclite
  floor for Hue) — declined, see header. Their receipts already say what blocked them; that is
  the intended steady state unless a real user asks.
- Any change to `--best-effort-transitives` semantics (it still means "warn and stay partial";
  synthesis runs before that fallback is consulted).

## Tests

Extend the existing "Import-closure completeness gate self-tests" section
(src/spm_to_xcframework_tests.py:12961+, registry ~:14054), same idiom (`_gc_make_package` /
`_gc_make_xcframework` synthetic fixtures, `(description, callable, is_slow)` registry entries):

- fast: hole→target mapping (including modulemap-identity and unmappable→refusal); eligibility
  matrix (Swift / C / ObjC / static-only / plugin / linker-settings); no-progress and cap
  termination; receipt rounds recorded; refusal reasons preserved; synthesized identity never
  repeats.
- slow (integration, tests/integration/packages.toml): **FlexLayout and Pulse end-to-end** —
  convert → import-closed set promoted atomically → receipt shows the synthesis rounds; plus one
  X-007 case (Yams) to prove the umbrella reconciliation.
- regression: a package that is closed today converts byte-identically (loop runs zero rounds).

## Validation

- `python3 src/spm_to_xcframework_tests.py --fast` — all green, including the new section.
- Slow legs: FlexLayout 2.2.3 and Pulse 5.2.3 conversions succeed and verify; Yams umbrella
  imports resolve.
- Corpus follow-up (separate, in swift-bindings/internal-binding-testing): rerun the two
  packages through `run_library.py` so the S09 `convert_failed` rows flip; any *generator*-side
  failure that then surfaces is new, honest signal — record it, don't chase it here.
- Update KNOWN-ISSUES.md: X-007 status per outcome; add a row only if a new irreducible wall is
  discovered.

## Sizing

One to two focused sessions: the loop + eligibility + receipt in one; the ObjC/Mixed + X-007
umbrella reconciliation is the risk pocket and may take the second. Everything else in this doc
is reuse of existing machinery.
