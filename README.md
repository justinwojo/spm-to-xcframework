# spm-to-xcframework

Build or download xcframeworks from Swift Package Manager packages — Swift, Objective-C, or mixed.

Point it at an SPM package (URL or local path) and it produces ready-to-use xcframeworks for each library product. Defaults to **iOS-only** (deployment target auto-derived from `Package.swift`, falling back to 15.0); opt into other platforms with `--macos` / `--maccatalyst` / `--tvos` / `--watchos` / `--visionos`, each with its own auto-derived version and fallback. Pin any version explicitly with `--min-<plat> VERSION` (which also implies inclusion). Packages that distribute pre-built artifacts via SPM binary targets can be downloaded directly with `--binary`.

Framework type (Swift, ObjC, Mixed) is auto-detected per output and reported in the build summary.

## Install

```bash
# Clone and add to PATH
git clone https://github.com/justinwojo/spm-to-xcframework.git
export PATH="$PWD/spm-to-xcframework:$PATH"
```

Or just run the committed root `spm-to-xcframework` directly — it's a single self-contained script with no dependencies beyond Xcode and the system Python.

`spm-to-xcframework` is implemented in **Python 3.9+** using only the standard library. No `pip install` step.

### Requirements

- macOS with Xcode installed (provides `xcodebuild`, `swift`, `clang`)
- `python3` (the system Python on macOS is sufficient — Python 3.9 or later)
- Any platform SDK the build will exercise (e.g. visionOS / watchOS via *Xcode › Settings › Components*). See [Troubleshooting](#troubleshooting).

## Quick start

```bash
# Build all library products from a remote package at a tag
spm-to-xcframework https://github.com/Alamofire/Alamofire.git -v 5.10.2

# Build from a local checkout
spm-to-xcframework ./MyPackage -o ./output

# Download pre-built xcframeworks (no source compile)
spm-to-xcframework https://github.com/nicklockwood/iCarousel.git -v 1.8.3 --binary
```

Each successful run drops the resulting xcframeworks (and a manifest used for cross-run cleanup) under `--output` (default `./xcframeworks`).

## Usage

```
spm-to-xcframework <package-url-or-path> [--version <ver>] [options]
```

### Options

**Package selection**

| Flag | Description |
|------|-------------|
| `-v, --version <ver>` | Git tag to check out. Required for remote URLs. A `v`-prefix mismatch (e.g. tag is `v1.2.3`, you typed `1.2.3`) is resolved automatically. |
| `-o, --output <dir>` | Output directory (default: `./xcframeworks`). |
| `-p, --product <name>` | Build only these products (repeatable; default: all library products). |
| `-t, --target <name>` | Build an SPM target that isn't exposed as a `.library()` product (repeatable). Escape hatch — see [`--target`](#--target-escape-hatch). |
| `--binary` | Download pre-built xcframeworks from binary SPM targets instead of building from source (remote URLs only). |
| `--revision <sha>` | Verify the resolved tag points at this full 40-character commit SHA before fetching (supply-chain check). |

**Platform selection**

iOS is on by default; other platforms are opt-in. Each platform has a bare include flag (`--<plat>`) and an explicit pin flag (`--min-<plat> VERSION`); the pin also implies inclusion.

| Flag | Description |
|------|-------------|
| `--min-ios <ver>` | Pin the iOS minimum deployment target. iOS is built by default; this flag overrides the auto-derived version. Mutually exclusive with `--no-ios`. |
| `--no-ios` | Skip iOS. Pair with `--macos` / `--maccatalyst` / `--tvos` / `--watchos` / `--visionos` (or their `--min-<plat>` variants) to build other platforms only. |
| `--macos` | Also build a macOS slice. Auto-derives the version from `Package.swift`, falling back to `11.0`. |
| `--min-macos <ver>` | Pin the macOS minimum deployment target. Implies `--macos`. |
| `--maccatalyst` | Also build a Mac Catalyst slice. Auto-derives, falling back to `15.0`. |
| `--min-maccatalyst <ver>` | Pin the Mac Catalyst minimum. Implies `--maccatalyst`. |
| `--tvos` | Also build tvOS device + simulator slices. Auto-derives, falling back to `15.0`. |
| `--min-tvos <ver>` | Pin the tvOS minimum. Implies `--tvos`. |
| `--watchos` | Also build watchOS device + simulator slices. Auto-derives, falling back to `8.0`. Requires watchOS SDK. |
| `--min-watchos <ver>` | Pin the watchOS minimum. Implies `--watchos`. |
| `--visionos` | Also build visionOS device + simulator slices. Auto-derives, falling back to `1.0`. Requires visionOS SDK. |
| `--min-visionos <ver>` | Pin the visionOS minimum. Implies `--visionos`. |

> **Platform selection at a glance**
> - **No platform flags** → iOS only, version auto-derived from `Package.swift` (falling back to `15.0`).
> - **`--<plat>` (bare flag)** → adds that platform; version auto-derived from `Package.swift`, or the per-platform fallback if the package doesn't declare it.
> - **`--min-<plat> VERSION`** → adds that platform AND pins the version explicitly. User-explicit values always win over auto-derived and fallback.
> - **`--no-ios`** → drops iOS. Combine with one or more opt-ins to build non-iOS platforms only.
> - Opting into `--watchos` or `--visionos` requires the corresponding SDK to be installed locally — install via *Xcode › Settings › Components*.
>
> Full rules: [Platform selection](#platform-selection).

**Build behavior**

| Flag | Description |
|------|-------------|
| `--include-deps` | Also build xcframeworks for transitive dependencies. iOS-only in v1; requires iOS to be enabled. |
| `--no-dedup-overlap` | Disable inter-unit `.binaryTarget` substitution. By default, when one product depends on a sibling product/target in the same package, the sibling is built first and rewritten into a `.binaryTarget` before the umbrella's archive runs — this stops the umbrella from statically embedding the sibling's Mach-O. Pass this flag to keep the legacy single-shot behavior. |
| `--no-cleanup-stale` | Skip cleanup of stale xcframeworks from prior runs this time, but keep them tracked in the manifest so a subsequent normal run will clean them. See [Stale-output cleanup](#stale-output-cleanup). |

**Diagnostics**

| Flag | Description |
|------|-------------|
| `--verbose` | Show full `xcodebuild` output. |
| `--dry-run` | Show what would be produced without completing the final build/copy step. In binary mode this still resolves artifacts so the reported set is exact. |
| `--inspect-only` | Run Fetch + Inspect and print the parsed Package model, then exit. |
| `--keep-work` | Keep the temporary work directory for debugging. |
| `-h, --help` | Show help. |

## Examples

### iOS-only (default)

```bash
# No platform flags → iOS slices only. Deployment target is auto-derived
# from Package.swift if the package declares one; otherwise iOS 15.0.
spm-to-xcframework https://github.com/Alamofire/Alamofire.git -v 5.10.2
```

### Add more platforms

```bash
# Bare --macos opts in; the deployment target is auto-derived from
# Package.swift, or falls back to macOS 11.0 if the package doesn't
# declare a macOS platforms: entry.
spm-to-xcframework https://github.com/Alamofire/Alamofire.git -v 5.10.2 --macos

# Add Mac Catalyst and tvOS too.
spm-to-xcframework https://github.com/Alamofire/Alamofire.git -v 5.10.2 \
    --macos --maccatalyst --tvos

# Pin explicit minimums. Either flag adds the platform.
spm-to-xcframework https://github.com/Alamofire/Alamofire.git -v 5.10.2 \
    --min-ios 15.0 --min-macos 11.0
```

### Non-iOS only

```bash
# Drop iOS, build macOS only (single-slice xcframework).
spm-to-xcframework https://github.com/Alamofire/Alamofire.git -v 5.10.2 \
    --no-ios --macos
```

### Filter to specific products

```bash
# A large package, two products
spm-to-xcframework https://github.com/stripe/stripe-ios.git -v 25.6.2 \
    --product Stripe --product StripePayments

# Mix products and internal-only targets — Stripe ships StripeCore / StripeUICore
# as .target(...) rather than .library(...), so --target is the only way in.
spm-to-xcframework https://github.com/stripe/stripe-ios.git -v 25.6.2 \
    --product Stripe --target StripeCore --target StripeUICore
```

### Build with dependencies

```bash
# Also emit xcframeworks for transitive deps under the same output dir
spm-to-xcframework https://github.com/kean/Nuke.git -v 12.8.0 --include-deps
```

### Binary mode

```bash
# Some libraries (Firebase, BlinkID, iCarousel) ship pre-built xcframeworks
# through SPM binary targets — download instead of building.
spm-to-xcframework https://github.com/nicklockwood/iCarousel.git -v 1.8.3 --binary
```

### Supply-chain check

```bash
# Refuse to build if the tag doesn't resolve to this exact commit
spm-to-xcframework https://github.com/kean/Nuke.git -v 12.8.0 \
    --revision a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2
```

### Other

```bash
# An ObjC library whose product declares static linkage — the planner
# injects a synthetic .library(..., type: .dynamic, ...) wrapper, builds
# that, and renames the output to MBProgressHUD.xcframework.
spm-to-xcframework https://github.com/jdg/MBProgressHUD.git -v 1.2.0

# See what would be built without building
spm-to-xcframework https://github.com/kean/Nuke.git -v 12.8.0 --dry-run

# Print the parsed Package model and exit (debug)
spm-to-xcframework https://github.com/kean/Nuke.git -v 12.8.0 --inspect-only
```

## How it works

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

- **Default set** — iOS is built unless `--no-ios` is passed. Every other platform requires an explicit opt-in via the bare `--<plat>` flag (`--macos`, `--maccatalyst`, `--tvos`, `--watchos`, `--visionos`) or via `--min-<plat> VERSION` (which also implies inclusion). The default reflects this tool's downstream consumers (.NET MAUI / Xamarin) being iOS-first and not benefiting from watchOS / visionOS slices; pure-Swift consumers opt in per build.
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

### `--target` escape hatch

Some packages declare important modules as `.target(...)` in `Package.swift` without exposing them as `.library(...)` products. stripe-ios is the canonical example: `StripeCore`, `StripeUICore`, `Stripe3DS2`, and `StripeCameraCore` are all plain targets, so `--product StripeCore` fails with "No library products matching filter". The `--target` flag tells the planner to emit a `synth_library` edit that injects a synthetic `.library(name: "<name>", type: .dynamic, targets: ["<name>"])` into `Package.swift` (via `swift package add-product`), then build the synthesized product like any other library product.

Because the synthetic library is a real `.library(...)` declaration with `type: .dynamic`, only the requested target is forced dynamic — internal C/ObjC dependency targets keep their natural build type. This makes `--target` safe for packages like Firebase that bundle static helper libs (nanopb, leveldb, GoogleUtilities) inside their target graph. The tool refuses to synthesize a library for non-source targets (`.binaryTarget`, `.systemLibrary`, plugin, macro, executable, test) with a clear `PlanError`.

If `--target T` names a target that already happens to be exposed as a library product, the existing product is used instead (with a warning) — `--target` will never cause duplicate planning.

### Always-clean build tree

Every source-mode build runs against a freshly-staged copy of the package with `.git`, `.build`, `DerivedData`, `node_modules`, and any sibling `.xcodeproj` / `.xcworkspace` files pruned. Pruning the Xcode projects forces `xcodebuild` to use SPM-generated schemes, which sidesteps both the "multiple projects with the current extension" error (GRDB ships `GRDB.xcodeproj` + `GRDBCustom.xcodeproj`) and the "does not contain a scheme" wording mismatch that legacy bash had to grep around.

### Stale-output cleanup

The tool drops a `.spm-to-xcframework-manifest.json` file in the output directory recording exactly which xcframeworks each successful run produced (primary outputs and, when `--include-deps` is set, transitive dependency outputs too). Before the next run finishes, anything tracked by that manifest that the new run no longer produces is removed.

Cleanup runs **only after every output passes the strict per-unit verify pass** — a failed run leaves the prior manifest and prior xcframeworks untouched so you can retry against a known-good baseline. Files in the output directory that the tool didn't put there (your own xcframeworks, READMEs, build outputs from other tools) are never touched: only entries listed in the manifest are eligible for cleanup, and entry names are constrained to plain basenames inside the output directory (no `..`, no absolute paths, no path separators).

Pass `--no-cleanup-stale` to skip cleanup for one run while keeping the orphans tracked. The preserved entries are merged into the new manifest, so a subsequent run *without* the flag will clean them naturally — opting out once doesn't leak orphans forever.

### Input validation

Source URLs and tag/revision arguments are validated before they reach `git`. Package sources must be either an absolute local path or a URL with a known remote prefix (`http://`, `https://`, `git@`, `ssh://`); tag and revision values are restricted to a small character set with a length cap, and `--` separators are passed to every git invocation so a value starting with `-` cannot be reinterpreted as a git flag.

## Output

A real source-mode run (some per-slice success lines and `xcodebuild` stderr noise elided, marked with `...`):

```
$ spm-to-xcframework https://github.com/Alamofire/Alamofire.git -v 5.10.2 \
    --min-ios 15.0 --min-macos 11.0 -o ./output

Cloning https://github.com/Alamofire/Alamofire.git @ 5.10.2
Staging package into clean working tree...
  Resolving package dependencies...
Inspecting package...

Plan for Alamofire @ 5.10.2  (source mode)
  Package edits:
    - synth_dynamic_library: Alamofire__Dynamic → targets=[Alamofire]
  Build units:
    [1] Alamofire         scheme=Alamofire__Dynamic  language=Swift  → Alamofire.xcframework
    [2] AlamofireDynamic  scheme=AlamofireDynamic    language=Swift  → AlamofireDynamic.xcframework
  Selected slices: ios-arm64, ios-simulator, macos
Preparing Package.swift edits...
  Prepare validated 1 edit(s) ✓

Executing 2 build unit(s)...
  Building Alamofire — ios-arm64, ios-simulator, macos (parallel)...
  ...
  Renaming Alamofire__Dynamic.framework → Alamofire.framework
  Injecting Swift module interfaces (ios-arm64)
  Creating Alamofire.xcframework...
  Alamofire.xcframework ready [Swift]
  Building AlamofireDynamic — ios-arm64, ios-simulator, macos (parallel)...
  ...
  Creating AlamofireDynamic.xcframework...
  AlamofireDynamic.xcframework ready [Swift]

=== Summary ===
  Built: 2    Verified: 2    Failed: 0

Output: /path/to/output

Xcframeworks:
  Alamofire.xcframework         (26.7M) [Swift]
  AlamofireDynamic.xcframework  (26.6M) [Swift]
```

Note how the planner allocated `Alamofire__Dynamic` for the synthetic — `AlamofireDynamic` is already taken by an existing sibling product, so the planner reached for the `__Dynamic` fallback. The final framework on disk is still `Alamofire.xcframework`; the synthetic name is only visible in the plan and during the rename pass.

The output directory also contains a `.spm-to-xcframework-manifest.json` file used to track which outputs the tool owns across runs (see [Stale-output cleanup](#stale-output-cleanup)). It is safe to commit, ignore, or delete; the tool re-creates it on the next successful run.

## Using with .NET binding generators

The xcframeworks produced by this tool are ready for .NET binding generation (e.g. [Swift.Bindings](https://github.com/dotnet/runtime/tree/main/src/native/managed/cdac-tools/swift-bindings)). The binding generator auto-detects framework type from the xcframework contents:

- **Swift** xcframeworks: P/Invoke bindings via ABI JSON.
- **ObjC** xcframeworks: `ApiDefinition.cs` + `StructsAndEnums.cs` via clang AST.
- **Mixed** xcframeworks: both pipelines, two-project output.

The strict per-unit verify pass (dynamic Mach-O check, `.swiftinterface` requirement for Swift/Mixed, header + modulemap requirement for ObjC/Mixed) means a run that exits 0 is, by construction, a run whose outputs the binding generator can consume. There is no "this might still fail in the binding step" caveat — verify is the gate.

## Troubleshooting

Symptoms below show the leading prefix of the actual error message — the real output often continues with extra context (available products, available destinations, etc.). Match by prefix when `grep`-ing logs.

| Symptom | Likely cause / fix |
|---------|--------------------|
| `Error: --no-ios and --min-ios are mutually exclusive. Drop one or the other.` | You passed both. Drop one — `--no-ios` already says "skip iOS"; `--min-ios` says "use this iOS version." |
| `Error: No platforms selected.` | You passed `--no-ios` without opting into any other platform. Add `--macos` / `--maccatalyst` / `--tvos` / `--watchos` / `--visionos` (or a `--min-<plat> VERSION` flag, which implies inclusion), or drop `--no-ios`. |
| `xcodebuild` reports `Unable to find a destination matching … { generic:1, platform:watchOS }` (or visionOS) | You opted into a platform whose SDK isn't installed locally. Install it via *Xcode › Settings › Components*, or drop the `--watchos` / `--visionos` opt-in. |
| `Error (fetch): Tag '<x>' not found` | The remote repo doesn't have that tag. Check `git ls-remote --tags <url>`. |
| `Error (plan): --product filter matched no products: [...]` | A product name in `--product` doesn't exist. Run with `--inspect-only` to see the package's declared products and targets. If the module is exposed as a plain `.target(...)` instead, use `--target <name>`. |
| `Error (plan): Plan produced zero build units.` | `--product` filtered everything out, or the package declares only non-library products (e.g. executables, macros). |
| `Error (plan): --target '<name>': target kind is '<kind>'; only regular source targets can be synthesized…` | You pointed `--target` at a `.binaryTarget`, `.systemLibrary`, plugin, macro, or test target. `--target` only works for normal source targets (Swift, ObjC, C). |
| `Error (plan): Detected a binary-only Package.swift` against a local path | Binary auto-route needs a remote URL + `--version`. Point at the upstream URL or extract the xcframework from the vendor artifact directly. |
| `Error (prepare): Package.swift uses Swift raw string literals (#"..."#) / triple-quoted strings (""") / string interpolation (\(…))` | The Package.swift uses Swift constructs the tool's heuristic walker doesn't understand. File a bug with the package URL + tag. |
| `Error (inspect): swift package dump-package failed: ... Is the swift-tools-version supported by your toolchain?` | The package's `swift-tools-version` is newer than (or incompatible with) your installed Swift. Update Xcode or pin to an older tag. |
| Swift xcframework missing `.swiftinterface` files | The package doesn't enable library evolution. ObjC binding generation still works; Swift binding generation requires `.swiftinterface`. |

## Known limitations

- Packages with `swift-tools-version` not supported by the installed toolchain fail at `swift package dump-package`. The inspect error explicitly suggests checking the tools version.
- Packages that don't support library evolution (`-enable-library-evolution`) may produce xcframeworks without `.swiftinterface` files — Swift binding generation requires these, but ObjC binding generation is unaffected.
- ObjC-only SPM targets must declare public headers via `publicHeadersPath` in `Package.swift` for headers to appear in the xcframework.
- `--binary` only works with remote packages that distribute binary xcframeworks via SPM binary targets — packages with a mix of binary and source targets will only resolve the binary artifacts.
- `--include-deps` requires iOS to be enabled; the transitive-dep xcframeworks themselves are iOS-only in v1, even when the primary outputs also build for macOS / tvOS / etc. (those non-iOS slices simply won't carry dep artifacts — the tool warns when this happens).
- `--revision` requires the full 40-character commit SHA; short SHAs are rejected.
- The planner's static-construct check (raw strings, triple-quoted strings, string interpolation in `Package.swift`) is a heuristic, not a full Swift parser; a manifest using these constructs is rejected with a clear `PrepareError` rather than silently mis-parsed.

## Testing

Two complementary suites live in this repo:

- `src/spm_to_xcframework_tests.py` — ~8,000-line snapshot suite. Parses fixture JSON and asserts on the planner's decisions. Fast (under 10 seconds), runs on every commit, catches Plan / Inspect logic regressions. Run with `python3 src/spm_to_xcframework_tests.py`.
- `tests/integration/` — real-package integration suite. Each entry in `tests/integration/packages.toml` is built end-to-end with `xcodebuild`. Catches what the snapshot suite cannot: Xcode/Swift toolchain regressions, real-world `Package.swift` shapes, the injection passes, and structural xcframework failures (dynamic Mach-O, `.swiftinterface` emission, packaged clang-module shims) that the tool's own Verify phase doesn't independently re-check.

### Running the integration suite

```bash
# Smoke subset — the cheap fast canaries (pre-commit / per-PR)
python3 tests/integration/run_integration.py --smoke

# Full matrix — before tagging a release
python3 tests/integration/run_integration.py

# Single package
python3 tests/integration/run_integration.py --only grdb

# Run multiple packages concurrently (each xcodebuild also parallelises inside)
python3 tests/integration/run_integration.py --parallel 3

# Keep produced xcframeworks under reports/<run>/<package>/work/ for inspection
python3 tests/integration/run_integration.py --only wcdb --keep-output
```

Each run writes a Markdown + JSON report to `tests/integration/reports/<UTC timestamp>/`. Exit code is non-zero if any package fails. Packages marked `known_broken = true` are highlighted in the report but don't gate the run — each must pair with a `broken_signature` substring the runner checks against the log, so a *new* failure mode in a previously-known-broken package still trips the suite and forces fresh triage.

The harness pins each entry to explicit `--min-<platform>` flags rather than auto-detect, so the same matrix produces the same slice set on any machine. Currently iOS-only — multi-platform stress is gated on every contributor having watchOS and visionOS simulator runtimes installed.

### Matrix

Ten packages, picked for code-path diversity over popularity. Entries marked ★ are in the smoke subset; entries marked ⚠ are tracked `known_broken` against the current Xcode/Swift and do not gate the run.

| Package | Tag | Exercises | Smoke |
|---|---|---|---|
| [SnapKit/SnapKit](https://github.com/SnapKit/SnapKit) | 5.7.1 | Pre-existing `.library(type: .dynamic)`, linkerSettings, `.copy` resource | ★ |
| [jdg/MBProgressHUD](https://github.com/jdg/MBProgressHUD) | 1.2.0 | ObjC-only → synth dynamic library + modulemap generation, tools-version bump | |
| [groue/GRDB.swift](https://github.com/groue/GRDB.swift) | v6.29.3 | `systemLibrary` + `inject_system_clang_modules` + resources | ★ |
| [kean/Nuke](https://github.com/kean/Nuke) | 12.8.0 | Multi-product Swift, sibling target deps, version-specific manifest, `package` strip | |
| [apple/swift-collections](https://github.com/apple/swift-collections) ⚠ | 1.1.4 | Library-evolution, string-interpolation walker, non-literal-products surgery | |
| [airbnb/lottie-spm](https://github.com/airbnb/lottie-spm) | 4.5.0 | `--binary` mode + binary discovery | |
| [getsentry/sentry-cocoa](https://github.com/getsentry/sentry-cocoa) | 8.57.0 | Static vendor xcframework → dynamic promotion, requested-platform slice filter | |
| [pointfreeco/swift-dependencies](https://github.com/pointfreeco/swift-dependencies) | 1.6.3 | `--include-deps` transitive builds, version-specific manifest | |
| [Tencent/wcdb](https://github.com/Tencent/wcdb) | v2.1.10 | Bridge clang modules (`WCDB_Private` case) | |
| [stripe/stripe-ios](https://github.com/stripe/stripe-ios) | 24.0.0 | dedup-overlap rewrite + multi-target builds | |

⚠ entries hit a remaining limitation each entry's `broken_reason:` in `packages.toml` documents — they're useful as regression markers for further fix work.

### Adding a package

A new entry earns a slot only if it exercises a code path no existing entry does — diversity over popularity. When triaging a real downstream bug, the package that triggered it gets added with a `notes:` line pointing to the bug; that is the durable feedback loop.

1. Add a `[[package]]` block to `tests/integration/packages.toml`. The `exercises` field documents the unique coverage — that's the contract for keeping the entry.
2. Run `python3 tests/integration/run_integration.py --only <name>` to confirm it builds.
3. Tag `smoke = true` only if the build is under ~1 minute and catches a unique fast-failure mode.
4. If the verify check flags `.swiftinterface` imports the matrix entry intentionally doesn't package (e.g. an internal sibling target excluded to keep build time bounded), list them under `allowed_unresolved = [...]` with an inline comment explaining why.

## License

MIT
