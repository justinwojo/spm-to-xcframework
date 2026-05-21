# Roadmap

Date: 2026-05-21

Forward-looking work plan for `spm-to-xcframework`. The mission: **95%+ of arbitrary SPM packages produce a valid xcframework on the first try**, with a UX that lets a first-time user succeed without reading the source.

This doc supersedes the forward-looking sections of [REFACTOR_PROPOSAL.md](REFACTOR_PROPOSAL.md) (whose A/B already shipped) and [INTEGRATION_TESTING.md](INTEGRATION_TESTING.md) (whose harness is live). Both remain as historical / operational reference.

---

## Status of prior proposals

| Item | Source | Status |
|---|---|---|
| A. Modularize single file | REFACTOR_PROPOSAL.md | **Done** (`ffbc7fd`) |
| B. `swift package add-product` for Phase 3 edits | REFACTOR_PROPOSAL.md | **Done** (`ffbc7fd`) |
| C. SwiftSyntax helper binary | REFACTOR_PROPOSAL.md | **Deferred indefinitely** — per proposal itself, B made it unnecessary |
| D. Injection passes as separate library | REFACTOR_PROPOSAL.md | Open — see P2 below |
| E. Content-addressed build cache | REFACTOR_PROPOSAL.md | Open — see P2 below |
| F. Serializable Plan / `--dry-run` | REFACTOR_PROPOSAL.md | Open — see P1 below |
| Integration matrix scaffolding | INTEGRATION_TESTING.md | **Done** — 10/10 passing |
| Nightly CI for integration matrix | INTEGRATION_TESTING.md (Phase 2) | Open — see P1 below |
| Pre-merge integration check | INTEGRATION_TESTING.md (Phase 3) | Intentionally deferred (cost) |

---

## P0 — Must-have for the mission

### Swift macros / compiler plugins

Modern SPM packages increasingly ship Swift macros (`@Observable`, `@Model`, swift-syntax consumers, point-free libraries, etc.). These are declared as `.macro(...)` targets and `.plugin(...)` build-tool plugins, which the current tool doesn't model. Almost certainly the **#1 reason a 2025-era SPM package fails today** if it's not already in our matrix.

What's involved:
- Recognise `.macro` and `.plugin` targets in Inspect / Plan
- Decide what shipping them means in the xcframework output — macros are host-architecture compiler plugins, not iOS/macOS runtime artifacts; they probably need to be either built-and-bundled-as-tools or surfaced as a "this package needs a macro plugin at build time" constraint on the consumer
- The consumer's `.swiftinterface` references the macro module, so the slice-set has to satisfy whatever the bindings tooling expects

Risk: deep. Likely a multi-day investigation before code lands. Spike first.

### Actionable error messages

Today's failure modes (xcodebuild exit code, raw stderr, `dump-package` parse errors, missing-product-from-Plan, etc.) print useful information but rarely tell a first-time user **what to do next**. For the "easy to use" half of the mission, this is as important as coverage.

Targets:
- When prepare/execute fails for a known shape, emit a labelled diagnosis ("this package uses a custom-target wrapper — the overlay edit failed because X"). The internal exception classes (`PrepareUserError`, `ExecuteError`) already have the structure; the surface needs a translation layer.
- When `swift package dump-package` fails, surface the *first* manifest error verbatim, with a "try this" suggestion when the pattern is recognisable.
- When verify fails, name the missing artifact (e.g., "`Foo.framework/Modules/module.modulemap` not present") rather than just "verify failed".

Risk: moderate. Touches many code paths but each touch is small. Iterative.

---

## P1 — High-leverage, well-scoped

### Stress-test matrix expansion

Add packages that exercise paths the current 10-entry matrix doesn't reach. Candidates ordered by ROI:

1. **`apple/swift-syntax`** — ~31 internal targets. **Added 2026-05-21**; surfaced the resilience boundary issue and now passes flag-free via the try-and-fallback shipped below.
2. **`apple/swift-async-algorithms`** — depends on swift-collections, validates `--include-deps` chain into the recent fix transitively.
3. **`onevcat/Kingfisher`** — popular pure-Swift image library; broadens "common consumer packages" coverage.
4. **`apple/swift-argument-parser`** — small, common, fast smoke test that pure-Swift baseline still works.

Each follows the policy in INTEGRATION_TESTING.md: earns a slot only if it exercises a path the matrix doesn't.

Risk: low — additions are cheap to try and reveal information either way.

### ~~Auto-detect dedup-overlap incompatibility~~ — Shipped 2026-05-21

**Surfaced by swift-syntax 2026-05-21.** Dedup-overlap's `.target → .binaryTarget` rewrite creates a library-evolution resilience boundary: from the umbrella's perspective, the rewritten sibling becomes an externally-resilient module. When the umbrella does exhaustive switches over the sibling's enums (without `@unknown default`), Swift emits hard errors at `.swiftinterface` emit time. swift-syntax's SwiftParser → SwiftSyntax relationship is the canonical case (e.g., `switch firstArgument` over `RawSameTypeRequirementSyntax.LeftType`, switches over `Keyword`).

**Resolution**: try-and-fallback, implemented in `execute/run_unit.py`. Per-unit, before the archive runs, we snapshot the active manifest. On `ExecuteError` we check for the canonical "may have additional unknown values" diagnostic — if it matches AND this unit applied at least one dedup substitution, we restore the snapshot, drop the unit's substitutions from `substituted_target_names`, and re-archive once. Unrelated failures surface unchanged. Trade-off when the fallback fires: the umbrella statically embeds the sibling's symbols, so a downstream consumer linking both that umbrella xcframework AND the sibling xcframework will see duplicate symbols (matches the behavior of `--no-dedup-overlap` globally, which was the prior workaround).

The flag still exists as the explicit opt-out and silences the per-unit warning.

### Serializable Plan + `--dry-run`

(F from REFACTOR_PROPOSAL.md, merged with the UX `--dry-run` ask.)

`Plan` is already a typed dataclass tree. Add `.to_json()` / `.from_json()` and a `--dry-run` flag that runs Fetch + Inspect + Plan only, prints the resolved plan, and exits before Prepare. Unlocks:
- A "what's about to happen" preview for first-time users
- Debugging long-tail packages without burning a 5-minute xcodebuild
- Replayable / inspectable plans for support requests ("paste your `--dry-run` output")

Risk: low. ~100 lines + serialization tests.

### Nightly integration CI

(Phase 2 of INTEGRATION_TESTING.md.)

GitHub Actions on `macos-latest`, cron-triggered, runs the full matrix and posts results. Primary value: catch Apple-Xcode-shipped regressions before a release tag.

Risk: low, but cost ($5–8/run) means defer until matrix has been green for a few weeks under normal dev churn. We're at "passing right now" — wait at least a couple more PR cycles before turning it on.

---

## P2 — Nice-to-have / deferred

### Homebrew distribution

User has flagged this as explicitly last priority. Distributes the existing single-file build artifact via a tap. Probably one afternoon of work once we're ready.

Risk: low. Mechanical.

### Content-addressed build cache

(E from REFACTOR_PROPOSAL.md.)

Hash `(Package.resolved SHA, xcodebuild settings, tool version, requested platforms, requested products)`. On a hit, copy the cached `.xcframework` and skip Execute. Free win for CI runs and repeated binding regeneration; ~150 lines.

Risk: low. Independent of all other items.

### Injection passes as separately-importable library

(D from REFACTOR_PROPOSAL.md.)

After the modular split, the injection passes (`inject_swiftmodule`, `inject_objc_headers`, `inject_clang_bridge`, `inject_resources`, `binary_promote`) are already self-contained. Promoting `src/spm_to_xcframework/execute/` to a public API only matters if a second consumer materialises (e.g., a "finish my hand-vendored framework" tool). Until then, no action.

Risk: zero unless triggered by external demand.

---

## Suggested sequencing

Greedy: start with **P0 actionable error messages** (cheap, iterative, immediate user-visible improvement) while spiking **Swift macros** in parallel. Land **stress-test additions** as the spike runs — they're cheap and may surface adjacent gaps. **Serializable plan / `--dry-run`** lands next as a UX capstone. **Nightly CI** turns on after a few weeks of green matrix runs. P2 items wait for either external demand or genuine free cycles.

When picking up: confirm the matrix is still 10/10 first (`python3 tests/integration/run_integration.py`) so the baseline is known-green before adding scope.
