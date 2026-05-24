# Contributing to spm-to-xcframework

This document covers everything a contributor needs: the project layout and build process, the test suites, and an in-depth reference for how the build pipeline works. For user-facing usage, see [README.md](README.md).

## Project layout

The tool is developed as a set of per-phase modules under `src/spm_to_xcframework/`, but **shipped as a single self-contained executable** so end users can run it with no install step:

| Path | Role |
|------|------|
| `src/spm_to_xcframework/` | The real source — one module per phase (`fetch.py`, `inspect.py`, `plan.py`, `prepare.py`, `execute/…`, `verify.py`, etc.), layered in a strict DAG. **Edit here.** |
| `src/build_single_file.py` | Concatenates the package, in dependency order, into the single-file artifact. |
| `src/spm_to_xcframework.py` | Generated single-file artifact. Do not hand-edit. |
| `spm-to-xcframework` (repo root) | The committed executable — a byte-for-byte copy of `src/spm_to_xcframework.py` with the shebang and `chmod +x`. What users run. Do not hand-edit. |
| `src/spm_to_xcframework_tests.py` | Snapshot self-test suite. |
| `tests/integration/` | Real-package integration harness. |

### Building the single-file artifact

After editing anything under `src/spm_to_xcframework/`, regenerate both artifacts:

```bash
python3 src/build_single_file.py
```

This stitches the modules together (stripping relative imports, deduping `from __future__ import annotations`, dropping per-module shebangs/docstrings — otherwise preserving source verbatim), writes `src/spm_to_xcframework.py`, and copies it to the root `spm-to-xcframework` with the executable bit set.

A self-test asserts the committed root script is **byte-for-byte** identical to `src/spm_to_xcframework.py`, so a PR that edits the modules without rerunning the builder fails CI. Two build-time constraints to know about:

- **No aliased relative imports.** The builder strips relative imports wholesale; aliased forms (`from .x import Y as Z`) would lose the binding in the concatenated file, so the builder rejects them outright.
- **Strict layering.** A module in `MODULE_ORDER` may only reference names defined in modules above it (or in stdlib). This mirrors the wildcard-import sequence in `spm_to_xcframework/__init__.py`.

## Testing

Two complementary suites:

- **`src/spm_to_xcframework_tests.py`** — the snapshot suite (~8,000 lines). Parses fixture JSON and asserts on the planner's decisions. Fast (under 10 seconds), runs on every commit, catches Plan / Inspect logic regressions and the build-sync / aliased-import invariants above.

  ```bash
  python3 src/spm_to_xcframework_tests.py
  ```

- **`tests/integration/`** — real-package integration suite. Each entry in `tests/integration/packages.toml` is built end-to-end with `xcodebuild`. Catches what the snapshot suite can't: Xcode/Swift toolchain regressions, real-world `Package.swift` shapes, the injection passes, and structural xcframework failures (dynamic Mach-O, `.swiftinterface` emission, packaged clang-module shims).

### Running the integration suite

```bash
# Smoke subset — the cheap fast canaries (pre-commit / per-PR)
python3 tests/integration/run_integration.py --smoke

# Full matrix — before tagging a release
python3 tests/integration/run_integration.py

# Single package
python3 tests/integration/run_integration.py --only grdb

# Multiple packages concurrently (each xcodebuild also parallelises inside)
python3 tests/integration/run_integration.py --parallel 3

# Keep produced xcframeworks under reports/<run>/<package>/work/ for inspection
python3 tests/integration/run_integration.py --only wcdb --keep-output
```

Each run writes a Markdown + JSON report to `tests/integration/reports/<UTC timestamp>/`. Exit code is non-zero if any package fails. Packages marked `known_broken = true` are highlighted but don't gate the run — each must pair with a `broken_signature` substring the runner checks against the log, so a *new* failure mode in a previously-known-broken package still trips the suite and forces fresh triage. An entry can also set `required_log_signatures` — substrings that must *all* appear before the known failure — so a regression that fails early but happens to emit the same `broken_signature` still surfaces as a real failure instead of masking as known-broken (the TCA entry relies on this to assert its sibling xcframeworks built en route).

The harness pins each entry to explicit `--min-<platform>` flags rather than auto-detect, so the same matrix produces the same slice set on any machine. Currently iOS-only — multi-platform stress is gated on every contributor having watchOS and visionOS simulator runtimes installed.

### Matrix

The authoritative matrix lives in [`tests/integration/packages.toml`](tests/integration/packages.toml) — read it there rather than relying on a copy here, which drifts as entries are added. Each `[[package]]` block carries its own documentation:

- `exercises` — the unique code path that earns the entry its slot (the contract for keeping it). Coverage spans pre-existing dynamic libraries, ObjC-only synth + modulemap generation, `systemLibrary` clang shims, multi-product Swift with sibling deps, the string-interpolation manifest walker, `--binary` discovery, static→dynamic promotion, automatic external-transitive sibling builds, `--include-deps` archive harvesting, bridge clang modules, and dedup-overlap multi-target builds.
- `smoke = true` — the fast canary subset (`--smoke`).
- `known_broken = true` — tracked against the current Xcode/Swift; surfaced in the report but doesn't gate the run. Each documents its `broken_reason` and is guarded by `broken_signature` (+ `required_log_signatures` where applicable, per above).

### Adding a package

A new entry earns a slot only if it exercises a code path no existing entry does — diversity over popularity. When triaging a real downstream bug, the package that triggered it gets added with a `notes:` line pointing to the bug; that is the durable feedback loop.

1. Add a `[[package]]` block to `tests/integration/packages.toml`. The `exercises` field documents the unique coverage — that's the contract for keeping the entry.
2. Run `python3 tests/integration/run_integration.py --only <name>` to confirm it builds.
3. Tag `smoke = true` only if the build is under ~1 minute and catches a unique fast-failure mode.
4. If the verify check flags `.swiftinterface` imports the matrix entry intentionally doesn't package (e.g. an internal sibling target excluded to keep build time bounded), list them under `allowed_unresolved = [...]` with an inline comment explaining why.

---

## How it works (in depth)

The README's [How it works](README.md#how-it-works) gives the six-phase overview. This section is the full mechanics.

### Source builds (default)

1. **Normalizes the tag** for remote packages. Tags with a `v` prefix are resolved automatically, so `-v 1.2.3` works even when the actual tag is `v1.2.3`.
2. **Verifies revision** when `--revision` is provided — runs `git ls-remote` before fetching any source, handles annotated tags, and fails with a clear mismatch error.
3. **Clones** the package at the resolved tag (or copies a local path).
4. **Stages** the clone into a clean working tree, pruning `.git`, `.build`, `DerivedData`, `node_modules`, and any sibling `.xcodeproj` / `.xcworkspace` files (see [Always-clean build tree](#always-clean-build-tree)).
5. **Inspects** the package via `swift package dump-package`. Library products, internal targets, declared `platforms:`, and `tools-version` are all parsed. The set of platforms to build is resolved at this point (see [Platform selection](#platform-selection)).
6. **Plans** the build. Each requested product becomes a build unit; transitive sibling/dependency targets are walked.
7. **Edits `Package.swift` via `swift package add-product`** rather than regex surgery on the manifest text. Two edit kinds get emitted by the planner:
   - **`synth_dynamic_library`** — for any requested library product whose linkage is not already `.dynamic`, the planner allocates a non-colliding synthetic name (`<Product>Dynamic`, falling back to `<Product>__Dynamic`, then `__Dynamic2`, … when the primary name collides — Alamofire ships both `Alamofire` and `AlamofireDynamic`, so the first synthetic gets named `Alamofire__Dynamic`). A `.library(name: <synthetic>, type: .dynamic, targets: <product's targets>)` entry is added; the build runs against `<synthetic>`, then the post-archive rename pass turns `<synthetic>.framework` into `<original>.framework` per slice. Already-dynamic siblings, system-library products, and `.binaryTarget`-only products are skipped (no synth needed / no synth possible). Binary-only whole packages auto-route to `--binary` mode before this step is reached.
   - **`synth_library`** — for each `--target T` request, an equivalent `.library(name: T, type: .dynamic, targets: [T])` entry is added so the target is exposed as a buildable product. The tool refuses to synthesize for non-source targets (binary, system, plugin, macro, executable, test) with a clear `PlanError`.

   After Prepare invokes `swift package add-product`, a round-trip `swift package dump-package` confirms each new product appears with linkage DYNAMIC and the requested target list — manifests that fail this validation fail the run loudly.
8. **Builds** every enabled slice in parallel via `xcodebuild archive`. Pool width is capped at 4 to avoid Xcode license / DerivedData contention. Each archive runs with:
   - `BUILD_LIBRARY_FOR_DISTRIBUTION=YES` — ABI stability + `.swiftinterface` emission.
   - `SKIP_INSTALL=NO` — framework included in archive products.
   - the slice's platform-specific deployment-target build setting (`IPHONEOS_DEPLOYMENT_TARGET`, `MACOSX_DEPLOYMENT_TARGET`, `TVOS_DEPLOYMENT_TARGET`, `WATCHOS_DEPLOYMENT_TARGET`, or `XROS_DEPLOYMENT_TARGET`).
9. **Deduplicates inter-unit overlap.** Before each unit's archive runs, every sibling that has already been built in this run is rewritten in `Package.swift` to a `.binaryTarget(path: <built-xcframework>)`. This stops umbrella products (e.g. Stripe) from statically embedding all their sibling targets' Mach-O — the umbrella links dynamically against the sibling xcframeworks instead. After the umbrella is done, the synthetic dynamic product is demoted back to an automatic library so the next pass can re-link against it cleanly. Pass `--no-dedup-overlap` to opt out.
10. **Injects** `.swiftmodule` / `.swiftinterface` from DerivedData when missing from the framework bundle (common with SPM dynamic libraries), then ObjC public headers and modulemaps from the source tree for ObjC/mixed targets that don't include them in archive output, plus any SwiftPM-emitted resource bundles.
11. **Assembles** xcframeworks via `xcodebuild -create-xcframework`.
12. **Bundles `.systemLibrary` clang shims** as binary-less sibling frameworks inside each xcframework slice (e.g. GRDB's `GRDBSQLite` wrapper around `<sqlite3.h>`), plus any project-shipped clang modulemaps the swiftinterface imports but the primary framework's modulemap doesn't declare (e.g. WCDB's `WCDB_Private` shipped via `src/bridge/module.modulemap`).
13. **Detects** framework type per xcframework: **Swift** (has `.swiftinterface`), **ObjC** (public headers + modulemap, no Swift interfaces), or **Mixed** (both).
14. **Verifies** every produced xcframework with strict per-unit checks. The build only reports success when all of these pass for every output:
    - The `Info.plist` parses through `plistlib` (catches AppleDouble `__MACOSX` ghost xcframeworks).
    - Every requested (platform, variant) slice appears in `AvailableLibraries` — e.g. `--min-ios 15 --min-macos 11` requires `ios-device`, `ios-simulator`, and `macos-device` to all be present. Single-slice xcframeworks (pure macOS, pure Mac Catalyst) are valid.
    - Every slice's binary is a dynamically-linked Mach-O (no static archives masquerading as frameworks).
    - Swift/Mixed frameworks ship at least one `.swiftinterface` file.
    - ObjC/Mixed frameworks ship public headers under `Headers/` and a `module.modulemap`.
    - Failures are reported per-unit; the tool exits non-zero with a summary that names every failing output.

### Binary mode (`--binary`)

Some libraries (Firebase, BlinkID, iCarousel, …) distribute pre-built xcframeworks through SPM binary targets. Binary mode downloads them without building from source:

1. Normalizes the tag and verifies `--revision` if provided.
2. Creates a temporary `Package.swift` that depends on the target repo.
3. Runs `swift package resolve` to download binary artifacts.
4. Locates xcframeworks in `.build/artifacts/`, pruning `__MACOSX` AppleDouble ghosts that some vendor zips ship alongside the real artifacts (BlinkID 7.6.x is the canonical example).
5. Validates `--product` filters against the resolved artifact names.
6. Copies the matching xcframeworks to the output directory.
7. **Promotes any static-archive slices to dynamic frameworks.** Some vendors ship xcframeworks whose individual slices are static Mach-O archives (`.a` masquerading as a framework binary), which downstream binding generators and dynamic linkers can't consume. For each affected slice, the tool re-links the static archive in place via `xcrun … clang -dynamiclib -Xlinker -all_load … -Xlinker -undefined -Xlinker dynamic_lookup`, preserving architectures and using the slice's `LibraryIdentifier`-derived clang `-m<sdk>-version-min=` flag. The framework bundle layout is rewritten as needed (including macOS's `Versions/A` layout) so the result loads cleanly.
8. Runs the same strict per-unit verify pass that source mode uses.

Product filtering (`--product`), revision verification (`--revision`), and dry-run all work in binary mode. In binary dry-run, the tool still resolves artifacts so it can validate the requested products and show the exact filtered set, but it does not copy anything to the output directory. `--target` is rejected in binary mode (source-build escape hatch only).

### Platform selection

The tool resolves a deployment-target version for every included platform after `swift package dump-package`, before scheduling any builds. The rules:

- **Default set** — iOS is built unless `--no-ios` is passed. Every other platform requires an explicit opt-in via the bare `--<plat>` flag (`--macos`, `--maccatalyst`, `--tvos`, `--watchos`, `--visionos`) or via `--min-<plat> VERSION` (which also implies inclusion). The default reflects this tool's downstream consumers (.NET MAUI / iOS) being iOS-first and not benefiting from watchOS / visionOS slices; pure-Swift consumers opt in per build.
- **Version precedence (per included platform)** — `--min-<plat> VERSION` (user-explicit) > `Package.platforms[]` declaration in the manifest > Apple-modern fallback table:
  - `ios=15.0`, `macos=11.0`, `maccatalyst=15.0`, `tvos=15.0`, `watchos=8.0`, `visionos=1.0`.
- **Mixing is allowed** — `--macos --min-tvos 17` includes both extras and pins only the tvOS version; iOS still flows through at its auto-derived or fallback version.
- **Unknown platform names in `Package.platforms[]`** (e.g. `driverkit`) are silently ignored — a tool-stability invariant against future Apple SDK additions.
- **Discovery hint** — when `Package.swift` declares non-iOS platforms you didn't opt into, the tool logs a one-liner naming the flags that would add them. Example output against a package like Nuke:

  ```
  Note: Package.swift also declares macOS, tvOS, visionOS. Pass --macos / --tvos / --visionos to include those slices.
  ```
- **`--no-ios`** — drops iOS. Combine with one or more opt-ins to build non-iOS only. A run with `--no-ios` and no other opt-in errors out with a clear "no platforms selected" message. Mutually exclusive with `--min-ios`.
- **SDK availability** — opting into `--watchos` or `--visionos` requires the SDK to be installed locally; otherwise `xcodebuild` fails with `Unable to find a destination matching the provided destination specifier`. Install via *Xcode › Settings › Components*.
- **Binary mode** — there's no `Package.swift` to read, so every included platform resolves straight to the fallback table (user-explicit `--min-<plat>` still wins). The discovery hint also no-ops in binary mode for the same reason.

### Transitive dependencies

When a package's targets depend on products from **other** SPM packages via `.product(name:, package:)`, those modules need handling too — and the tool does it automatically, no flag required.

**Why it's needed.** xcodebuild's SPM integration static-links every product reachable from the umbrella scheme into the umbrella's dylib, regardless of the product's declared `type:`. The umbrella's consumer-visible `.swiftinterface` then `import`s those modules — but their `.swiftmodule` isn't anywhere on a downstream consumer's search path. Producing a sibling xcframework for each external dependency (one fresh build per transitive checkout, one level deep) is the only reliable fix.

**What you get.** Every external dependency the root's targets import is built as its own xcframework in the same `--output` directory, alongside the primary outputs. A build that exits 0 ships a self-contained set whose `.swiftinterface` imports all resolve.

**Failure handling.** Fail-fast by default: if any transitive fails Plan, Execute, or Verify, the whole run aborts rather than ship an umbrella with dangling imports. Pass `--best-effort-transitives` to continue with whatever succeeded.

**Opting out.** `--no-transitive-products` skips the recursion entirely. This is a foot-gun — the umbrella's `.swiftinterface` will reference modules that aren't present — so use it only when you know the consumer doesn't link those transitive symbols.

`--no-transitive-products` and `--include-deps` are different mechanisms. The transitive-products recursion (above, on by default) builds *external* package dependencies as siblings so the umbrella's interface resolves. `--include-deps` (off by default, iOS-only) is a separate opt-in: after a unit builds, it repackages any *dependency `.framework` bundles that landed inside that unit's own archive* into their own xcframeworks. One reaches across packages so the build is correct; the other harvests extra artifacts from a single archive.

### `--target` escape hatch

Some packages declare important modules as `.target(...)` in `Package.swift` without exposing them as `.library(...)` products. stripe-ios is the canonical example: `StripeCore`, `StripeUICore`, `Stripe3DS2`, and `StripeCameraCore` are all plain targets, so `--product StripeCore` fails with "No library products matching filter". The `--target` flag tells the planner to emit a `synth_library` edit that injects a synthetic `.library(name: "<name>", type: .dynamic, targets: ["<name>"])` into `Package.swift` (via `swift package add-product`), then build the synthesized product like any other library product.

Because the synthetic library is a real `.library(...)` declaration with `type: .dynamic`, only the requested target is forced dynamic — internal C/ObjC dependency targets keep their natural build type. This makes `--target` safe for packages like Firebase that bundle static helper libs (nanopb, leveldb, GoogleUtilities) inside their target graph. The tool refuses to synthesize a library for non-source targets (`.binaryTarget`, `.systemLibrary`, plugin, macro, executable, test) with a clear `PlanError`.

If `--target T` names a target that already happens to be exposed as a library product, the existing product is used instead (with a warning) — `--target` will never cause duplicate planning.

### Always-clean build tree

Every source-mode build runs against a freshly-staged copy of the package with `.git`, `.build`, `DerivedData`, `node_modules`, and any sibling `.xcodeproj` / `.xcworkspace` files pruned. Pruning the Xcode projects forces `xcodebuild` to use SPM-generated schemes, which sidesteps both the "multiple projects with the current extension" error (GRDB ships `GRDB.xcodeproj` + `GRDBCustom.xcodeproj`) and the "does not contain a scheme" wording mismatch that legacy bash had to grep around.

### Stale-output cleanup

The tool drops a `.spm-to-xcframework-manifest.json` file in the output directory recording every verified xcframework a successful run owns: the primary outputs, the automatically-built external-package sibling outputs (transitive-products recursion is on by default), and any extra `.framework` bundles harvested with `--include-deps`. Before the next run finishes, anything tracked by that manifest that the new run no longer produces is removed.

Cleanup runs **only after every output passes the strict per-unit verify pass** — a failed run leaves the prior manifest and prior xcframeworks untouched so you can retry against a known-good baseline. Files in the output directory that the tool didn't put there (your own xcframeworks, READMEs, build outputs from other tools) are never touched: only entries listed in the manifest are eligible for cleanup, and entry names are constrained to plain basenames inside the output directory (no `..`, no absolute paths, no path separators).

Pass `--no-cleanup-stale` to skip cleanup for one run while keeping the orphans tracked. The preserved entries are merged into the new manifest, so a subsequent run *without* the flag will clean them naturally — opting out once doesn't leak orphans forever.

### Synthetic dynamic-library naming

The planner allocates a synthetic name for any library product that isn't already dynamic, and the name can vary when the obvious choice collides. For Alamofire (which ships both `Alamofire` and `AlamofireDynamic` products), the plan reads:

```
Plan for Alamofire @ 5.10.2  (source mode)
  Package edits:
    - synth_dynamic_library: Alamofire__Dynamic → targets=[Alamofire]
  Build units:
    [1] Alamofire         scheme=Alamofire__Dynamic  language=Swift  → Alamofire.xcframework
    [2] AlamofireDynamic  scheme=AlamofireDynamic    language=Swift  → AlamofireDynamic.xcframework
```

`AlamofireDynamic` is already taken by an existing sibling product, so the planner reaches for the `__Dynamic` fallback. The final framework on disk is still `Alamofire.xcframework`; the synthetic name is only visible in the plan and during the rename pass.

### Input validation

Source URLs and tag/revision arguments are validated before they reach `git`. Package sources must be either an absolute local path or a URL with a known remote prefix (`http://`, `https://`, `git@`, `ssh://`); tag and revision values are restricted to a small character set with a length cap, and `--` separators are passed to every git invocation so a value starting with `-` cannot be reinterpreted as a git flag.
