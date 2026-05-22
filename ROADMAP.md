# Roadmap

Date: 2026-05-22 (last updated after `--dry-run` + wild-sample campaign — see "Serializable Plan + --dry-run" and "Wild-sample campaign" below)

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
| F. Serializable Plan / `--dry-run` | REFACTOR_PROPOSAL.md | **Done** (2026-05-22) — see "Serializable Plan + --dry-run" below |
| Integration matrix scaffolding | INTEGRATION_TESTING.md | **Done** — 15/15 passing + 2 known_broken pinned to upstream bugs |
| Nightly CI for integration matrix | INTEGRATION_TESTING.md (Phase 2) | Open — see P1 below |
| Pre-merge integration check | INTEGRATION_TESTING.md (Phase 3) | Intentionally deferred (cost) |

---

## P0 — Must-have for the mission

### ~~Swift macros / compiler plugins~~ — Shipped 2026-05-21

**Shipped in `28863d1`.** `.macro` targets are pre-built host-side as executable plugins, and `-load-plugin-executable` flags are routed into the consumer's build so the umbrella module compiles against macro-expanded source. The same commit closed the transitive-sibling gap that swift-syntax-class packages depend on: external packages reachable via `.product(...)` are now built as sibling xcframeworks alongside the primary, `.product` deps are rewritten to bare-string refs against the injected binaryTargets, and `package`-scoped decls / `package import` lines in sibling swiftinterfaces are promoted to `public` so cross-binaryTarget resolution works. Matrix grew from 10/10 → 12/12 with `swift-syntax` and `swift-case-paths` added as canonical macro-consuming cases.

Follow-on risks to watch as more macro-using packages land in the matrix: build-tool plugins (`.plugin(...)`, distinct from `.macro`) are not yet exercised, and macros that themselves depend on transitive binary frameworks haven't been stress-tested.

### ~~Actionable error messages~~ — Shipped 2026-05-22

**Shipped.** New `src/spm_to_xcframework/diagnostics.py` ships a substring-keyed pattern table that translates opaque xcodebuild / `swift package` failures into a two-line `Diagnosis: ... / Try: ...` block. Seven patterns cover the known surfaces: macro plugin pre-build failure (`SwiftSyntax` not found at archive time), library-evolution resilience boundary (the dedup-overlap auto-recovery surface), Xcode 26.3 / swift-collections `@_lifetime` ceiling, auto-linked library miss (`--target` / `--include-deps` hint), `binaryTarget` Swift-version mismatch, dependency cycles, and `PhaseScriptExecution` failures (most often `.plugin(...)` build-tool scripts).

`format_swift_package_failure` shapes `swift package dump-package` / `describe` failures: surfaces the first `error:` line verbatim and emits a directional tools-version hint — when the manifest is older than the toolchain ("bump the manifest"), and when it's newer than the installed Xcode ("upgrade Xcode"). The opposite-direction case was a Codex-review catch; without it the helper would have pointed users at editing a manifest they don't control.

Wired into `_format_execute_error` (xcodebuild archive failures) so the diagnosis block lands ABOVE the raw xcresult errors — the actionable signal precedes the evidence. Unmatched failures look identical to the pre-diagnostics output. Also wired into `_swift_dump_package` and `_swift_describe_package` so SPM manifest failures get the same treatment.

Verify-side missing-artifact naming (the third target below) wasn't critical-path: `verify_output` already names the missing path in its `VerifyError` / `VerifyResult` issues (e.g. `f"xcframework not found at {xcframework_path}"`, `f"Info.plist missing — ..."`). Left as-is.

Original targets, for reference:
- ~~When prepare/execute fails for a known shape, emit a labelled diagnosis.~~ Done via `diagnostics.scan` + `_format_execute_error`.
- ~~When `swift package dump-package` fails, surface the *first* manifest error verbatim with a "try this" suggestion when recognisable.~~ Done via `format_swift_package_failure`.
- ~~When verify fails, name the missing artifact rather than just "verify failed".~~ Already in place — confirmed during this work.

---

## P1 — High-leverage, well-scoped

### ~~Stress-test matrix expansion~~ — All listed candidates landed

Add packages that exercise paths the previously-12-entry matrix didn't reach. Candidates ordered by ROI:

1. ~~**`apple/swift-syntax`**~~ — ~31 internal targets. **Added 2026-05-21**; surfaced the resilience boundary issue and now passes flag-free via the try-and-fallback shipped below.
2. ~~**`pointfreeco/swift-case-paths`**~~ — **Added in `28863d1`** as a canonical macro-consuming package.
3. ~~**`apple/swift-async-algorithms`**~~ — **Added in `1d20825`** as `known_broken`. Surfaced an Xcode 26.3 + swift-collections 1.5.x `@_lifetime` ceiling; revisit on Xcode 26.4 / swift-collections 1.6.
4. ~~**`onevcat/Kingfisher`**~~ — **Added in `1d20825`**. Pure-Swift image library; broadens "common consumer packages" coverage.
5. ~~**`apple/swift-argument-parser`**~~ — **Added in `1d20825`**. Small, common, fast smoke test that pure-Swift baseline still works.
6. ~~**`pointfreeco/swift-composable-architecture`**~~ — **Added 2026-05-22** as a `known_broken` regression-test entry. Direct guard for commit `1d20825` (TCA-class transitive-graph hardening): the entry's `required_log_signatures` pins 13 sibling `*.xcframework ready (dependency)` lines, forcing the run to walk the hardened transitive paths before the same `@_lifetime` ceiling as #3 trips. Without `required_log_signatures`, an earlier failure that happened to log the same signature would silently mask as known_broken.

Matrix is now 17 entries (15 passing + 2 known_broken). Each addition followed the policy in INTEGRATION_TESTING.md: earns a slot only if it exercises a path the matrix doesn't.

Next candidates earn a slot only on the same rule — new path, new value. None queued.

Risk: low — additions are cheap to try and reveal information either way.

### ~~Auto-detect dedup-overlap incompatibility~~ — Shipped 2026-05-21

**Surfaced by swift-syntax 2026-05-21.** Dedup-overlap's `.target → .binaryTarget` rewrite creates a library-evolution resilience boundary: from the umbrella's perspective, the rewritten sibling becomes an externally-resilient module. When the umbrella does exhaustive switches over the sibling's enums (without `@unknown default`), Swift emits hard errors at `.swiftinterface` emit time. swift-syntax's SwiftParser → SwiftSyntax relationship is the canonical case (e.g., `switch firstArgument` over `RawSameTypeRequirementSyntax.LeftType`, switches over `Keyword`).

**Resolution**: try-and-fallback, implemented in `execute/run_unit.py`. Per-unit, before the archive runs, we snapshot the active manifest. On `ExecuteError` we check for the canonical "may have additional unknown values" diagnostic — if it matches AND this unit applied at least one dedup substitution, we restore the snapshot, drop the unit's substitutions from `substituted_target_names`, and re-archive once. Unrelated failures surface unchanged. Trade-off when the fallback fires: the umbrella statically embeds the sibling's symbols, so a downstream consumer linking both that umbrella xcframework AND the sibling xcframework will see duplicate symbols (matches the behavior of `--no-dedup-overlap` globally, which was the prior workaround).

The flag still exists as the explicit opt-out and silences the per-unit warning.

### ~~Serializable Plan + `--dry-run`~~ — Shipped 2026-05-22

(F from REFACTOR_PROPOSAL.md, merged with the UX `--dry-run` ask.)

`Plan` (and every sub-dataclass that hangs off it: `StageSpec`, `PackageSwiftEdit`, `BuildUnit`, `MacroSupport`) gained `.to_json()` / `.from_json()` round-trip with an explicit `JSON_SCHEMA_VERSION = 1` envelope. `--dry-run` runs Fetch + Inspect + Plan and exits with a human-readable plan render; `--dry-run-json` does the same and writes `{schema_version, package, version, mode, transitive_packages, plan}` to stdout with all log noise routed to stderr — pipe-friendly. The orchestrator skips transitive recursion under any dry-run so no xcodebuild ever fires (was previously broken — child configs reset `dry_run=False`).

Self-tests added: round-trip equality on five representative plan shapes (empty / GRDB synth_dynamic_library / Stripe two-tier / binary-mode Path artifact / synthetic `consume_external_sibling` + `MacroSupport`), schema-version-mismatch raises, `to_json()` is `json.dumps()`-able, plus two end-to-end CLI tests (success path + failure path with `PATH`-shimmed xcodebuild that exits non-zero if invoked). 260/260 self-tests green.

### Wild-sample campaign — first 95% lower-bound data point

`tests/integration/wild_sample.py` (a new harness, separate from the matrix) clones a curated list of 42 SPM packages NOT in the matrix and runs each through `--dry-run-json`. Result: **40/42 plan_ok (95.2%)**, **1 diagnosed_failure**, **1 undiagnosed_failure**, **0 timeout** (90 s per candidate). One pure-noise observation surfaced first: three candidates had bogus version pins in the harness (`swift-crypto 3.16.1`, `PromiseKit 8.1.3`, `SwiftMessages 10.1.1`) — none of which exist upstream; corrected pins resolved cleanly.

Real failures:

- **`apple/swift-protobuf` @ 1.32.0** — `swift package describe` rejects the staged copy with *"executable product 'protoc' expects target 'protoc' to be executable; an executable target requires a 'main.swift' file"*. swift-protobuf 1.32.0 exposes its `protoc` artifact-bundle (`.binaryTarget(url:, checksum:)`) as an `.executable(...)` product; SPM accepts that combination on a vanilla `swift package describe` but rejects it after our staging pass, likely because our exclude-pruning step (which deletes manifest-declared `exclude:` paths from staged) interacts with SPM's product-target resolution for the artifact-bundle. Shipped a `format_swift_package_failure` hint that names the offending product, explains the artifact-bundle/`.executable` root cause, and points at the swift-protobuf-style `PROTOBUF_NO_PROTOC=true` env-var workaround (or `--product` to target a library product instead). Deeper root-cause investigation queued — the hint makes this actionable today without forcing the staging redesign yet.

- **`realm/realm-swift` @ v20.0.3** — `swift package resolve` against the staged copy fails with *"Could not find Package.swift in this directory or any of its parent directories."* The Realm checkout uses git submodules + an unusual layout; our stage walks the source tree but the resolution context appears to land outside the staged root. Realm has historically been complex (C++/ObjC bindings on top of submoduled `realm-core`); classifying as *known-unsupported, documented* until a deliberate effort makes sense. (Lands in `undiagnosed_failure` — a candidate diagnostic pattern for "submoduled Package.swift not found" would help, but is low-priority until a second package hits the same shape.)

Harness improvement on the way through: `_DIAGNOSIS_RE` originally matched only `Diagnosis:`-shaped blocks (xcodebuild-archive failures) and would have miscategorised the new `First error:` / `Try:` blocks (`format_swift_package_failure` output). Widened to anchor on the `Try:` line so any actionable hint, regardless of producer, buckets as `diagnosed_failure`. A bare `First error:` with no `Try:` (unmatched stderr shape) still buckets as `undiagnosed_failure` — exactly the shapes that need a new pattern.

Coverage take-away: **95.2% plan-phase success on an uncurated wild sample** is the first hard number behind the 95% mission gate. Plan-phase success is necessary-but-not-sufficient for archive success, so this is an upper bound on end-to-end success — promoting the 40 wild successes to full archive runs in subsequent sessions will give the actual lower bound.

### Diagnostics alias-import regression — Caught + guarded 2026-05-22

After the diagnostics ship (`fa96866`), the matrix run revealed a `NameError: name '_scan_diagnosis' is not defined` in the single-file artifact's execute path — the build_single_file step strips relative imports wholesale (including the `from ..diagnostics import scan as _scan_diagnosis` line in `execute/archive.py`), but the call site still referenced `_scan_diagnosis`. Modular-mode tests passed (the alias was bound there); only an end-to-end archive on the single-file artifact tripped the bug. Fixed by dropping the aliases in `archive.py` (call `scan(...)` / `format_block(...)` directly), and `build_single_file.py` now rejects any aliased relative import (`from .X import Y as Z`) at build time so this can't recur silently.

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

Both P0 items are shipped (macros in `28863d1`, actionable errors in this session). **Serializable plan / `--dry-run`** is the next P1 — small, well-scoped, immediately user-visible. **Nightly CI** turns on after a few weeks of green matrix runs. P2 items wait for either external demand or genuine free cycles. New diagnostic patterns can be added to `diagnostics._PATTERNS` as fresh failure shapes show up in the wild — that's a cheap follow-on lane rather than a discrete project.

When picking up: confirm the matrix is still 15/15 passing + 2 known_broken first (`python3 tests/integration/run_integration.py`) so the baseline is known-green before adding scope. The two known_broken entries (swift-async-algorithms, swift-composable-architecture) are pinned to the Xcode 26.3 / swift-collections 1.5.x `@_lifetime` ceiling — they should flip to PASS without code changes once Xcode 26.4 or swift-collections 1.6 lifts the experimental gate.
