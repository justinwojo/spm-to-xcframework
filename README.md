# spm-to-xcframework

**Build or download distribution-ready `.xcframework`s from any Swift Package Manager package.** Point it at an SPM package and it produces a dynamic, ABI-stable xcframework for each library product — Swift, Objective-C, or mixed — packaged with everything a downstream consumer needs to link against it.

It was built to feed .NET MAUI / iOS bindings, which need exactly this shape of artifact, and that's still its primary use ([details below](#using-with-net-binding-generators)), but the output is just a plain, valid xcframework — equally useful any time you need one from an SPM package: vendoring a dependency, redistributing a binary, or pulling SPM code into a non-SPM build.

- **Builds** source SPM library products (Swift, Objective-C, or mixed) into dynamic xcframeworks with library evolution enabled.
- **Downloads** vendor-shipped binary xcframeworks with `--binary` — no source compile.
- **Resolves dependencies** automatically: external SPM packages your code imports are built as sibling xcframeworks so the umbrella's Swift interfaces resolve downstream.
- **Verifies** every output against strict per-unit structural checks (dynamic Mach-O, Swift interfaces, headers/modulemaps). A run that exits `0` produces xcframeworks a consumer can actually link — the same contract .NET binding generators require.

Defaults to **iOS device + simulator**; other platforms are opt-in. Framework type (Swift / ObjC / Mixed) is auto-detected per output.

## Get started

Install with [Homebrew](https://brew.sh):

```bash
brew install justinwojo/spm-to-xcframework/spm-to-xcframework
```

The three-part name pulls in the tap automatically — no separate `brew tap` step — and puts `spm-to-xcframework` on your `PATH`. Then:

```bash
# Build all library products from a remote package at a tag
spm-to-xcframework https://github.com/Alamofire/Alamofire.git -v 5.10.2

ls xcframeworks   # → Alamofire.xcframework  AlamofireDynamic.xcframework  .spm-to-xcframework-manifest.json
```

Prefer to run from source, or track the latest `main`? It's a single self-contained script — **Python 3.9+, standard library only, no `pip install`** — so a clone is all you need:

```bash
git clone https://github.com/justinwojo/spm-to-xcframework.git
cd spm-to-xcframework
export PATH="$PWD:$PATH"   # this shell; add to your shell profile to persist
./spm-to-xcframework https://github.com/Alamofire/Alamofire.git -v 5.10.2
```

The rest of this README uses the bare `spm-to-xcframework` form, assuming it's on your `PATH`.

### Requirements

- macOS with Xcode (provides `xcodebuild`, `swift`, `clang`)
- `python3` — the system Python on macOS is fine (3.9+)
- Any platform SDK a build will exercise. iOS works out of the box; `--watchos` / `--visionos` need their SDKs installed via *Xcode › Settings › Components*. See [Troubleshooting](#troubleshooting).

## What success looks like

```
$ spm-to-xcframework https://github.com/Alamofire/Alamofire.git -v 5.10.2 -o ./output
...
=== Summary ===
  Built: 2    Verified: 2    Failed: 0

Output: /path/to/output

Xcframeworks:
  Alamofire.xcframework         (26.7M) [Swift]
  AlamofireDynamic.xcframework  (26.6M) [Swift]
```

Each output directory gets the xcframeworks plus a `.spm-to-xcframework-manifest.json` the tool uses to clean up stale outputs across runs (safe to commit, ignore, or delete — it's recreated on the next successful run). **Exit code `0` means every output passed verification.** On a non-zero exit the tool names each failing output, leaves the manifest unchanged, and skips stale-output cleanup — so previously-built outputs the run isn't rebuilding stay in place. (An output that *was* being rebuilt is replaced in place, so a failed rebuild can leave that one missing; re-run to restore it.)

## Choose your command

| Your package… | Command |
|---|---|
| builds from source (the common case) | `spm-to-xcframework <git-url> -v <tag>` |
| ships pre-built binary xcframeworks | `spm-to-xcframework <git-url> -v <tag> --binary` |
| is large and you want one product | `spm-to-xcframework <git-url> -v <tag> --product <name>` |
| exposes a module as a `.target`, not a `.library` | `spm-to-xcframework <git-url> -v <tag> --target <name>` |
| should build for more than iOS | add `--macos` / `--tvos` / `--watchos` / … (see [Platform selection](#platform-selection)) |

For a typical build you only need a URL and `-v`. Everything else is optional.

## Examples

```bash
# iOS only (default) — deployment target auto-derived from Package.swift, else iOS 15.0
spm-to-xcframework https://github.com/Alamofire/Alamofire.git -v 5.10.2

# Build from a local checkout into a custom output directory
spm-to-xcframework ./MyPackage -o ./output

# Add more platforms (each version auto-derived, or pin it explicitly)
spm-to-xcframework https://github.com/Alamofire/Alamofire.git -v 5.10.2 --macos --tvos
spm-to-xcframework https://github.com/Alamofire/Alamofire.git -v 5.10.2 --min-ios 15.0 --min-macos 11.0

# Drop iOS, build macOS only
spm-to-xcframework https://github.com/Alamofire/Alamofire.git -v 5.10.2 --no-ios --macos

# Filter a large package to specific products…
spm-to-xcframework https://github.com/stripe/stripe-ios.git -v 25.6.2 \
    --product Stripe --product StripePayments

# …and reach modules shipped as plain .target(...) rather than .library(...)
spm-to-xcframework https://github.com/stripe/stripe-ios.git -v 25.6.2 \
    --product Stripe --target StripeCore --target StripeUICore

# Download pre-built xcframeworks (Firebase, iCarousel, …) instead of building
spm-to-xcframework https://github.com/nicklockwood/iCarousel.git -v 1.8.3 --binary

# Refuse to build unless the tag resolves to this exact commit (supply-chain check)
spm-to-xcframework https://github.com/kean/Nuke.git -v 12.8.0 \
    --revision a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2

# See what would be built without building
spm-to-xcframework https://github.com/kean/Nuke.git -v 12.8.0 --dry-run
```

## Options

Run `spm-to-xcframework --help` for the full list with the same grouping shown here. **For a typical build you only need a URL and `-v`** — everything past *Package selection* is optional.

### Package selection

| Flag | Description |
|------|-------------|
| `-v, --version <ver>` | Git tag to check out. Required for remote URLs. A `v`-prefix mismatch (tag is `v1.2.3`, you typed `1.2.3`) is resolved automatically. |
| `-o, --output <dir>` | Output directory (default: `./xcframeworks`). |
| `-p, --product <name>` | Build only these products (repeatable; default: all library products). |
| `-t, --target <name>` | Build an SPM target not exposed as a `.library()` product (repeatable). See [`--target`](#--target-building-a-non-library-module). |
| `--binary` | Download pre-built xcframeworks from binary SPM targets instead of building from source (remote URLs only). |
| `--revision <sha>` | Verify the resolved tag points at this full 40-character commit SHA before fetching (supply-chain check). |

### Platform selection

iOS is built by default; every other platform is opt-in. Each platform has a **bare include flag** (`--<plat>`) and an **explicit pin** (`--min-<plat> VERSION`); the pin also implies inclusion.

| Flag | Adds | Pin variant |
|------|------|-------------|
| *(default)* | iOS device + simulator | `--min-ios <ver>` |
| `--no-ios` | *drops* iOS (combine with an opt-in below) | — |
| `--macos` | macOS | `--min-macos <ver>` |
| `--maccatalyst` | Mac Catalyst | `--min-maccatalyst <ver>` |
| `--tvos` | tvOS device + simulator | `--min-tvos <ver>` |
| `--watchos` | watchOS device + simulator *(needs watchOS SDK)* | `--min-watchos <ver>` |
| `--visionos` | visionOS device + simulator *(needs visionOS SDK)* | `--min-visionos <ver>` |

**Version per included platform** — precedence is `--min-<plat> VERSION` (user-explicit) **>** `Package.platforms[]` in the manifest **>** Apple-modern fallback (`ios=15.0`, `macos=11.0`, `maccatalyst=15.0`, `tvos=15.0`, `watchos=8.0`, `visionos=1.0`). Then:

- `--no-ios` and `--min-ios` are mutually exclusive, and `--no-ios` with no other opt-in is an error.
- When `Package.swift` declares non-iOS platforms you didn't opt into, the tool logs a one-line hint naming the flags that would add them.
- In `--binary` mode there's no manifest to read, so versions resolve straight to the fallback table (an explicit `--min-<plat>` still wins).

### Advanced build behavior

Defaults here are right for almost every package — reach for these only when a build needs to deviate.

| Flag | Description |
|------|-------------|
| `--include-deps` | Also emit xcframeworks for dependency `.framework` bundles found *inside a built unit's own archive*. iOS-only; requires iOS enabled. Distinct from automatic external-package resolution — see [Dependencies](#dependencies). |
| `--no-dedup-overlap` | Disable inter-unit `.binaryTarget` substitution. By default a sibling product/target is built first and rewritten to a `.binaryTarget` before the umbrella archives, so the umbrella links it dynamically instead of statically embedding its Mach-O. This flag keeps the legacy single-shot behavior. |
| `--no-transitive-products` | Skip building a sibling xcframework for each *external* `.product(name:, package:)` the root imports. The umbrella's `.swiftinterface` may then import modules absent from the consumer's search path — safe only when the consumer doesn't link those symbols. See [Dependencies](#dependencies). |
| `--best-effort-transitives` | By default a transitive build/verify failure aborts the run. This continues with whatever transitives succeeded; failures are skipped with a warning and excluded from the manifest (not deleted — inspect before shipping). |
| `--no-cleanup-stale` | Skip stale-output cleanup this run, but keep the orphans tracked so a later normal run cleans them. |

### Diagnostics

| Flag | Description |
|------|-------------|
| `--verbose` | Show full `xcodebuild` output. |
| `--dry-run` | **Source mode:** Fetch + Inspect + Plan only — no `xcodebuild`, transitive sibling builds skipped, so the printed plan reflects the umbrella package alone. **Binary mode:** resolves artifacts so product filtering is exact, but copies nothing. |
| `--dry-run-json` | Like `--dry-run`, but emit the resolved plan as machine-readable JSON on stdout (logs route to stderr, so stdout stays a clean document). Implies `--dry-run`. |
| `--inspect-only` | Run Fetch + Inspect, print the parsed package model, then exit. Source mode only — rejected with `--binary`. |
| `--keep-work` | Keep the temporary work directory for debugging. |
| `-h, --help` | Show help. |

## Using with .NET binding generators

The xcframeworks this tool produces are ready for .NET binding generation (e.g. [Swift.Bindings](https://github.com/dotnet/runtime/tree/main/src/native/managed/cdac-tools/swift-bindings)). The generator auto-detects framework type from the xcframework contents:

- **Swift** xcframeworks → P/Invoke bindings via ABI JSON.
- **ObjC** xcframeworks → `ApiDefinition.cs` + `StructsAndEnums.cs` via clang AST.
- **Mixed** xcframeworks → both pipelines, two-project output.

A run that exits `0` verifies the **structural** requirements the generators expect: dynamic Mach-O slices for every requested platform, `.swiftinterface` files for Swift/Mixed frameworks, and public headers + a `module.modulemap` for ObjC/Mixed frameworks. The xcframework packaging is valid by construction. Binding *generation* can still fail on unsupported API surface (or a package that doesn't enable library evolution and so ships no `.swiftinterface` — see [Known limitations](#known-limitations)).

## Dependencies

When your package's targets import products from **other** SPM packages via `.product(name:, package:)`, those modules are built automatically — **no flag required**. xcodebuild static-links every product reachable from the umbrella scheme into the umbrella's dylib, so the umbrella's consumer-visible `.swiftinterface` imports those modules even though their `.swiftmodule` isn't on a downstream consumer's search path. The fix: each external dependency is built as its own xcframework in the same `--output` directory, so a run that exits `0` ships a self-contained set whose interface imports all resolve.

- **Fail-fast by default** — if a dependency fails Plan/Execute/Verify, the whole run aborts rather than ship an umbrella with dangling imports. `--best-effort-transitives` continues with whatever succeeded; `--no-transitive-products` skips the recursion entirely (a foot-gun — see above).
- **`--no-transitive-products` ≠ `--include-deps`.** The recursion (on by default) builds *external package* dependencies as siblings so the interface resolves. `--include-deps` (off by default, iOS-only) is separate: after a unit builds, it repackages any *dependency `.framework` bundles embedded inside that unit's own archive* into their own xcframeworks.

### `--target`: building a non-library module

Some packages declare important modules as `.target(...)` without exposing them as `.library(...)` products — stripe-ios ships `StripeCore`, `StripeUICore`, `Stripe3DS2`, and `StripeCameraCore` this way, so `--product StripeCore` fails with "No library products matching filter". `--target StripeCore` tells the planner to inject a synthetic `.library(name: "StripeCore", type: .dynamic, targets: ["StripeCore"])` and build it like any other product. Only the named target is forced dynamic — internal C/ObjC helper targets (nanopb, leveldb, GoogleUtilities, …) keep their natural build type, so this is safe for packages like Firebase. The tool refuses non-source targets (`.binaryTarget`, `.systemLibrary`, plugin, macro, executable, test) with a clear error.

## Troubleshooting

Symptoms show the leading prefix of the real error — actual output often continues with more context (available products, destinations, …). Match by prefix when grepping logs.

| Symptom | Likely cause / fix |
|---------|--------------------|
| `Error: --no-ios and --min-ios are mutually exclusive.` | You passed both. `--no-ios` means "skip iOS"; `--min-ios` means "use this iOS version." Drop one. |
| `Error: No platforms selected.` | `--no-ios` with no other opt-in. Add `--macos` / `--tvos` / … (or a `--min-<plat>` flag), or drop `--no-ios`. |
| `xcodebuild` reports `Unable to find a destination matching … platform:watchOS` (or visionOS) | The platform's SDK isn't installed. Add it via *Xcode › Settings › Components*, or drop the `--watchos` / `--visionos` opt-in. |
| `Error (fetch): Tag '<x>' not found` | The remote repo lacks that tag. Check `git ls-remote --tags <url>`. |
| `Error (plan): --product filter matched no products: [...]` | A `--product` name doesn't exist. Run `--inspect-only` to list declared products/targets. If it's a plain `.target(...)`, use `--target <name>`. |
| `Error (plan): Plan produced zero build units.` | `--product` filtered everything out, or the package declares only non-library products (executables, macros). |
| `Error (plan): --target '<name>': target kind is '<kind>'…` | You pointed `--target` at a `.binaryTarget`, `.systemLibrary`, plugin, macro, or test target. It only works for regular source targets (Swift, ObjC, C). |
| `Error (inspect): swift package dump-package failed … Is the swift-tools-version supported by your toolchain?` | The package's `swift-tools-version` is newer than your installed Swift. Update Xcode or pin an older tag. |
| `Verify: … expected Swift framework but zero .swiftinterface files were produced` | The package didn't build with library evolution, so no `.swiftinterface` was emitted and verification fails (the run exits non-zero). Swift binding generation requires it; a genuinely ObjC-only library is detected as ObjC and isn't subject to this check. |

## Known limitations

- Packages whose `swift-tools-version` isn't supported by the installed toolchain fail at `swift package dump-package` (the inspect error says so).
- Packages that can't build with library evolution emit no `.swiftinterface`, so a framework that should be Swift fails the verify pass (the run exits non-zero) rather than producing a usable output. ObjC binding generation is unaffected.
- ObjC-only SPM targets must declare public headers via `publicHeadersPath` in `Package.swift` for headers to appear in the xcframework.
- `--binary` only works with remote packages that distribute xcframeworks via SPM binary targets; mixed binary/source packages resolve only the binary artifacts.
- `--include-deps` requires iOS enabled, and the dependency xcframeworks it harvests are iOS-only in v1 (non-iOS slices of the primary outputs simply won't carry dep artifacts — the tool warns when this happens).
- `--revision` requires the full 40-character commit SHA; short SHAs are rejected.

## How it works

A normal run moves through six phases. The full mechanics (synthetic dynamic-library naming, the Package.swift edit/validation round-trip, injection passes, static→dynamic promotion in binary mode) live in [CONTRIBUTING.md](CONTRIBUTING.md#how-it-works-in-depth).

1. **Fetch** — normalize the tag (`v`-prefix auto-resolved), optionally verify `--revision` via `git ls-remote` before any download, clone, and stage into a clean working tree (`.git`, `.build`, `DerivedData`, sibling `.xcodeproj`/`.xcworkspace` pruned so SPM-generated schemes are used).
2. **Inspect** — `swift package dump-package` to read library products, targets, declared `platforms:`, and tools-version; resolve the platform/version set.
3. **Plan** — turn each requested product/target into a build unit, walking sibling and external dependencies. Library products that aren't already dynamic get a synthetic `.library(type: .dynamic)` wrapper.
4. **Prepare** — apply the planned `Package.swift` edits via `swift package add-product`, then a round-trip `dump-package` confirms each new product is DYNAMIC with the right targets (failures abort loudly).
5. **Execute** — `xcodebuild archive` per slice (parallel, capped at 4) with `BUILD_LIBRARY_FOR_DISTRIBUTION=YES`; dedup sibling overlap to `.binaryTarget` so umbrellas don't statically embed siblings; inject missing `.swiftinterface` / headers / modulemaps; assemble via `-create-xcframework`. In `--binary` mode this phase instead resolves and copies vendor artifacts, promoting any static-archive slices to dynamic frameworks.
6. **Verify** — strict per-unit checks; the run reports success only when **every** output passes:
   - `Info.plist` parses (catches AppleDouble `__MACOSX` ghosts).
   - Every requested `(platform, variant)` slice is present.
   - Every slice's binary is a dynamically-linked Mach-O (no static archives masquerading as frameworks).
   - Swift/Mixed frameworks ship a `.swiftinterface`; ObjC/Mixed ship public headers + a `module.modulemap`.

## Contributing

Development setup, the build process for the single-file artifact, the test suites, and the in-depth pipeline reference are in [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT
