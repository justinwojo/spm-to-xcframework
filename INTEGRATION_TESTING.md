# Integration Testing Plan

Date: 2026-05-20

This document specifies a battle-testing harness for `spm-to-xcframework` that exercises the tool against a curated list of real public SPM packages. It exists because the current test suite (`src/spm_to_xcframework_tests.py`, ~8,225 lines) is entirely snapshot-driven — it parses JSON fixtures and asserts on the planner's decisions, but **never invokes `xcodebuild` against a real package**. That gap is the largest remaining stability risk.

This doc is self-contained: a fresh Claude session (or human collaborator) should be able to read just this file and stand up the harness end-to-end.

## TL;DR

Stand up `tests/integration/` with a YAML-defined package matrix, a `run_integration.py` runner that clones + builds each entry and asserts the produced `.xcframework` verifies, and a results report (Markdown + JSON). Run it manually before releases and on a nightly schedule once stable. Start with 10 packages chosen for shape diversity; grow the list as real bugs surface.

## Why this is needed

What the snapshot tests catch:
- Planner regressions (build unit decisions, language detection, dedup-overlap)
- Manifest parsing (`dump-package` / `describe` schema drift)
- Pure-logic bugs in Plan and Inspect

What the snapshot tests do **not** catch:
- Xcode/Swift toolchain regressions (Apple ships a new Xcode → our injection passes break silently)
- xcodebuild flag interactions (a `BUILD_LIBRARY_FOR_DISTRIBUTION` edge case, a new `-create-xcframework` quirk)
- Real-world `Package.swift` shapes we haven't snapshotted
- The static→dynamic promotion's `clang -dynamiclib` re-link surviving against an unfamiliar ObjC package
- Bridge clang module shim walking against an unfamiliar `.swiftinterface` consumer
- Resource bundle injection against packages with `.copy(...)` / `.process(...)` we haven't seen
- The verify gate's strictness against output that *looks* fine but breaks downstream

The downstream — [swift-bindings](https://github.com/justinwojo/swift-bindings) and its .NET P/Invoke consumers — is unforgiving. A silently-static slice or a missing `.swiftinterface` produces a broken NuGet package, often discovered weeks later. Real-package integration tests are the cheapest way to catch these before a release tag.

## Package matrix

The seed list, chosen for **shape diversity**, not popularity. Each entry exercises at least one code path that pure-snapshot tests cannot.

| # | Package | Pin | Shape exercised |
|---|---|---|---|
| 1 | [Alamofire/Alamofire](https://github.com/Alamofire/Alamofire) | 5.10.2 | Pure Swift, single library product — sanity baseline |
| 2 | [groue/GRDB.swift](https://github.com/groue/GRDB.swift) | v6.29.3 | Swift + bundled SQLite system library (`systemLibrary` target) |
| 3 | [kean/Nuke](https://github.com/kean/Nuke) | 12.8.0 | Multi-product Swift (Nuke + NukeUI + NukeExtensions) |
| 4 | [stripe/stripe-ios](https://github.com/stripe/stripe-ios) | 24.0.0 | Multi-product with **internal target dependencies** — exercises dedup-overlap and the `edit_replace_with_binary_target` surgery |
| 5 | [jdg/MBProgressHUD](https://github.com/jdg/MBProgressHUD) | 1.2.0 | **ObjC-only** — exercises synthetic-dynamic product or the `promote_static_to_framework` re-link |
| 6 | [Tencent/wcdb](https://github.com/Tencent/wcdb) | 2.1.10 | **Bridge clang modules** (WCDB_Private case) — the hardest injection pass |
| 7 | [airbnb/lottie-spm](https://github.com/airbnb/lottie-spm) | 4.5.0 | **Mixed-language** (Swift + ObjC), popular, widely-vendored |
| 8 | [getsentry/sentry-cocoa](https://github.com/getsentry/sentry-cocoa) | 8.40.1 | **Multi-platform** (iOS / macOS / tvOS / watchOS / visionOS) |
| 9 | [firebase/firebase-ios-sdk](https://github.com/firebase/firebase-ios-sdk) | 11.6.0 | **Binary-only monster** — regression test for `--binary` mode + auto-detect (commit `1220806`) |
| 10 | [apple/swift-collections](https://github.com/apple/swift-collections) | 1.1.4 | **Apple-maintained** — library-evolution edge cases, exercises `.swiftinterface` emission against the swift-collections ABI |

Stretch additions if/when the above are green:

- [SnapKit/SnapKit](https://github.com/SnapKit/SnapKit) — small Swift, fast smoke test
- [pointfreeco/swift-composable-architecture](https://github.com/pointfreeco/swift-composable-architecture) — many transitive deps, exercises `--include-deps`
- [realm/realm-swift](https://github.com/realm/realm-swift) — large, mixed binary + source, multi-product
- [BlinkID/blinkid-ios](https://github.com/BlinkID/blinkid-ios) — already referenced in snapshot tests; promote to real build

### How to choose what to add

A new package earns a slot only if it exercises a code path the existing matrix does not. "It's popular" is not sufficient. When triaging a real downstream bug, the package that triggered it gets added with a comment pointing to the bug — that's the durable feedback loop.

## Harness shape

### Layout

```
tests/integration/
  packages.yaml           # matrix definition (the table above, machine-readable)
  run_integration.py      # the runner
  expected/               # per-package expected-shape assertions (optional, see below)
    alamofire.yaml
    grdb.yaml
    ...
  reports/                # gitignored; runner writes here
    2026-05-20T14-30-00/
      summary.md
      summary.json
      <package>/
        verify.log
        xcodebuild.log
        xcframework-tree.txt
```

### `packages.yaml` schema

```yaml
- name: alamofire
  url: https://github.com/Alamofire/Alamofire
  version: 5.10.2
  revision: a1b2c3d...        # optional, SHA pin for --revision
  args: []                    # extra flags passed to spm-to-xcframework
  platforms: [ios]            # what to assert was built
  expected_products:
    - Alamofire
  notes: Sanity baseline; if this fails, the toolchain is the suspect.

- name: stripe-ios
  url: https://github.com/stripe/stripe-ios
  version: 24.0.0
  args: ["--no-dedup-overlap"]   # known to need this; revisit
  platforms: [ios]
  expected_products:
    - Stripe
    - StripeCore
    - StripePaymentSheet
  notes: Internal target deps. Regression test for dedup-overlap edit.
```

### Runner contract (`run_integration.py`)

For each package in `packages.yaml`:

1. **Fetch + build** by invoking the local `spm-to-xcframework` binary as a subprocess with the package URL, version, and any extra args. Output to a temp dir, never the user's `./xcframeworks`.
2. **Capture** the full stdout/stderr to `reports/<run>/<name>/xcodebuild.log`.
3. **Assert** on the produced `.xcframework` set:
   - Every expected product is present
   - Every slice's `Info.plist` declares the expected platforms
   - Every slice's binary passes `file <bin> | grep "dynamically linked"` (the verify contract from `_check_binary_dynamic`)
   - Swift slices ship `.swiftinterface` in `Modules/`
   - ObjC slices ship `Headers/` + `Modules/module.modulemap`
4. **Save** a tree dump of each produced xcframework for diffing across runs.
5. **Report** pass/fail per package + aggregate. JSON for machine consumption, Markdown for humans.

Exit code: 0 if all expected packages pass, non-zero otherwise. A package marked `known_broken: true` in the YAML does not fail the run but is highlighted in the report.

### Invocation

```
python tests/integration/run_integration.py                    # run everything
python tests/integration/run_integration.py --only alamofire   # one package
python tests/integration/run_integration.py --parallel 4       # parallel slices, sequential packages (xcodebuild already parallelizes inside)
python tests/integration/run_integration.py --update-expected  # capture current shape into expected/ (for new packages)
```

The runner is **not** a pytest plugin. It's a standalone script. Reason: xcodebuild runs are minutes long, the failure modes are physical (no disk, no Xcode, wrong simulator), and pytest's report format buries that signal. A purpose-built runner with a clear Markdown report is more useful here.

## Pass/fail contract

A package "passes" when:

1. `spm-to-xcframework` exits 0.
2. Every `expected_products` entry has a corresponding `.xcframework` in the output dir.
3. Each produced `.xcframework` passes the tool's own `Verify` phase (we trust our own contract — if Verify says it's good, the test is satisfied).
4. Cross-run diff of the xcframework tree shape shows no unexplained drift (added/removed files trigger a review prompt).

A package is "known broken" when it has `known_broken: true` and a `broken_reason:` comment. These do not fail CI but are listed in the report. Use sparingly — every entry is a bug we've accepted not to fix.

## Reporting

`summary.md` (human-readable):

```
# Integration test run — 2026-05-20 14:30:00 UTC

Toolchain: Xcode 26.3 (15A507), Swift 6.2.4
spm-to-xcframework: ffbc7fd (main)

## Results: 9/10 passed (1 known broken)

| Package | Result | Duration | Notes |
|---|---|---|---|
| alamofire | PASS | 1m 12s | |
| grdb | PASS | 2m 03s | |
| nuke | PASS | 1m 48s | |
| stripe-ios | PASS | 4m 22s | |
| mbprogresshud | PASS | 0m 41s | promoted via synthetic-dynamic |
| wcdb | FAIL | 3m 15s | bridge clang module WCDB_Private missing — see verify.log |
| lottie-spm | PASS | 2m 30s | |
| sentry-cocoa | PASS | 5m 10s | 5 platforms |
| firebase-ios-sdk | PASS | 0m 18s | auto-detected binary-only |
| swift-collections | KNOWN_BROKEN | 1m 02s | library-evolution swiftinterface mismatch (#TBD) |
```

`summary.json` (machine-readable, for trend tracking).

## Where this runs

**Phase 1 (manual)**: developers run it locally before tagging a release. The script is the deliverable.

**Phase 2 (nightly)**: once the matrix is stable, add a GitHub Actions workflow (`macos-latest` runner has Xcode) on a `cron` trigger. Notifications on red.

**Phase 3 (pre-merge, optional)**: add as a required check on PRs that touch the Execute or Verify phases. Probably overkill — most PRs don't need a 25-minute build matrix.

Skip Phase 2 until Phase 1 has been green for a few weeks running.

## Cost considerations

- Each package: ~1–5 minutes of xcodebuild time (longer for Sentry, Stripe, Firebase).
- Full matrix: ~25–35 minutes on an M-series Mac, longer on a GitHub-hosted runner.
- Disk: keep `--keep-work` off; clean up temp dirs in a `finally` block.
- Network: package clones cached via SPM's own mirror cache when possible.

A GitHub Actions `macos-latest` runner is ~$0.16/minute — call it $5–8 per nightly run. Acceptable.

## Maintenance

**Adding a package**: append to `packages.yaml`, run `--only <name> --update-expected` to capture the shape, commit both.

**Removing a package**: delete the YAML entry and the `expected/<name>.yaml`. Note why in the commit message — future-you will want to know.

**A package breaks because Apple shipped a new Xcode**: this is the system working. Investigate, fix the tool (or open an Apple feedback), update the expected shape if the new behavior is intentional.

**A package breaks because the package itself changed**: pin to the older version, file a follow-up to bump the pin once we understand the new shape.

**The full run gets too slow**: shard into "fast" (Alamofire, MBProgressHUD, Firebase, swift-collections) for quick sanity and "full" for nightly. Don't reduce coverage to save time — add shards instead.

## Implementation order

A reasonable execution sequence for a fresh session:

1. Scaffold `tests/integration/` with `packages.yaml` (just Alamofire) and a minimal `run_integration.py` that builds it and asserts the xcframework exists.
2. Add the Verify-contract assertions (dynamic Mach-O, `.swiftinterface` presence, etc.).
3. Add the Markdown + JSON report writer.
4. Add the remaining 9 packages one at a time, running each in isolation first.
5. Wire up `--only`, `--update-expected`, `--parallel` flags.
6. Document in README how to run.
7. (Later) GitHub Actions workflow for nightly.

Each step is a clean commit. Steps 1–3 can land before any new packages are added.

## Open questions

1. **Should `expected/` snapshot the full xcframework tree, or only the platform/product set?** Full tree gives stronger drift detection but creates noisy diffs whenever Xcode's build output changes (e.g., new debug-symbol layout in a new Xcode point release). Probably start with platform/product set and add tree snapshots later if drift becomes a real issue.
2. **Where do builds run for the nightly?** GitHub Actions `macos-latest` works but lags Apple by weeks on new Xcode releases — exactly when we most want the signal. A self-hosted Mac mini on the latest Xcode beta is the gold standard. Defer until we have a regression we wish we'd caught.
3. **How to handle packages that require credentials** (private repos, paid SDKs)? Out of scope for the public matrix; if needed, an extension point in the runner for `packages.private.yaml` (gitignored) that loads credentials from the environment.
4. **Should the runner compare the produced `.xcframework` against a *previous* known-good output**, not just verify it passes its own gate? More expensive (requires storing baselines somewhere) but catches subtle drift the verify gate doesn't notice. Defer until the basic harness has caught its first bug.

## Related

- [REFACTOR_PROPOSAL.md](REFACTOR_PROPOSAL.md) — the architectural refactor doc. Integration testing is complementary: refactor changes how the tool is built; integration tests prove the build still works.
- `src/spm_to_xcframework_tests.py` — the existing snapshot suite. Keep both; they catch different bugs.
