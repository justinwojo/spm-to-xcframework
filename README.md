# spm-to-xcframework

Build or download xcframeworks from Swift Package Manager packages — Swift, Objective-C, or mixed.

Takes an SPM package URL (or local path) and produces ready-to-use xcframeworks for each library product. Source packages are built into device (arm64) and simulator slices with `BUILD_LIBRARY_FOR_DISTRIBUTION=YES` for ABI stability. Packages that use SPM binary targets can also be downloaded directly with `--binary`.

Framework type (Swift, ObjC, Mixed) is auto-detected and reported in the build output.

## Install

```bash
# Clone and add to PATH
git clone https://github.com/justinwojo/spm-to-xcframework.git
export PATH="$PWD/spm-to-xcframework:$PATH"
```

Or just run it directly — it's a single self-contained script with no dependencies beyond Xcode and the Python 3 that ships with macOS.

`spm-to-xcframework` is implemented in **Python 3.9+** and uses only the Python standard library. No `pip install` step.

### Requirements

- macOS with Xcode installed (provides `xcodebuild`, `swift`)
- `python3` (system Python on macOS is sufficient — Python 3.9 or later)

## Usage

```
spm-to-xcframework <package-url-or-path> --version <ver> [options]
```

### Options

| Flag | Description |
|------|-------------|
| `-v, --version <ver>` | Git tag to check out (required for remote URLs) |
| `-o, --output <dir>` | Output directory (default: `./xcframeworks`) |
| `-p, --product <name>` | Build only these products (repeatable; default: all library products) |
| `-t, --target <name>` | Build an SPM target that isn't exposed as a `.library()` product (repeatable). Escape hatch — see warning below. |
| `--binary` | Download pre-built xcframeworks from binary SPM targets instead of building from source (remote URLs only) |
| `--revision <sha>` | Verify the git tag resolves to this full 40-character commit SHA before fetching (supply-chain security) |
| `--min-ios <ver>` | Minimum iOS deployment target for source builds (default: `15.0`) |
| `--include-deps` | Also build xcframeworks for transitive dependencies in source-build mode |
| `--verbose` | Show full xcodebuild output |
| `--dry-run` | Show what would be produced without completing the final build/copy step. In binary mode this still resolves artifacts so the reported set is exact. |
| `--keep-work` | Keep temporary work directory (for debugging) |
| `-h, --help` | Show help |

## Examples

```bash
# Build all products from Alamofire (Swift)
spm-to-xcframework https://github.com/Alamofire/Alamofire.git -v 5.10.2

# Build just the Nuke product, output to custom dir
spm-to-xcframework https://github.com/kean/Nuke.git -v 12.8.0 -o ./nuke-fw

# Build from a local package (Swift, ObjC, or mixed)
spm-to-xcframework ./MyPackage -o ./output

# Build multiple specific products from a large package
spm-to-xcframework https://github.com/stripe/stripe-ios.git -v 25.6.2 \
    --product Stripe --product StripePayments

# Mix products and internal-only targets — Stripe ships StripeCore/StripeUICore as
# .target(...) rather than .library(...), so --target is the only way to build them
# (see the "--target escape hatch" section below for the caveat)
spm-to-xcframework https://github.com/stripe/stripe-ios.git -v 25.6.2 \
    --product Stripe --target StripeCore --target StripeUICore

# Build an ObjC library with a static SPM product (auto-promoted to dynamic)
spm-to-xcframework https://github.com/jdg/MBProgressHUD.git -v 1.2.0

# Download pre-built binary xcframeworks (no source build)
spm-to-xcframework https://github.com/nicklockwood/iCarousel.git -v 1.8.3 --binary

# Verify tag SHA before building (supply-chain security)
spm-to-xcframework https://github.com/kean/Nuke.git -v 12.8.0 \
    --revision a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2

# See what would be built without building
spm-to-xcframework https://github.com/kean/Nuke.git -v 12.8.0 --dry-run
```

## How it works

### Source builds (default)

1. **Normalizes the tag name** for remote packages. Tags with a `v` prefix are resolved automatically, so `-v 1.2.3` works even when the actual tag is `v1.2.3`.
2. **Verifies revision** if `--revision` is provided — runs `git ls-remote` before fetching any source, handles annotated tags, and fails with a clear mismatch error.
3. **Clones** the package at the resolved tag (or copies a local path).
4. **Discovers** library products via `swift package dump-package` — works for Swift, ObjC, and mixed-language targets. Additional SPM targets passed via `--target` are verified against the package's `targets[]` array and queued alongside the products.
5. **Resolves** build schemes via `xcodebuild -list` against the staged copy. Sibling `.xcodeproj`/`.xcworkspace` files are pruned during staging so xcodebuild always picks SPM-generated schemes — no special handling needed for packages that ship multiple Xcode projects (e.g. GRDB's `GRDB.xcodeproj` + `GRDBCustom.xcodeproj`).
6. **Patches** `Package.swift` to set the requested library products to `type: .dynamic`. Only the specific products you asked for are touched — internal dependency targets and `.systemLibrary(...)` products (e.g. GRDB's `GRDBSQLite`) are left alone, and the round-trip validator runs `swift package dump-package` against the edited manifest to confirm every requested edit actually took effect.
7. **Builds** device and simulator archives in parallel via `xcodebuild archive`, with:
   - `BUILD_LIBRARY_FOR_DISTRIBUTION=YES` — ABI stability + swiftinterface emission
   - `SKIP_INSTALL=NO` — framework included in archive products
8. **Promotes** static archives to dynamic frameworks when needed — some ObjC-only packages (e.g. MBProgressHUD) produce `.a` files even when patched to `.dynamic`. The tool detects this, re-links the static archive as a dynamic library via `clang -dynamiclib`, infers system framework dependencies from source imports, and wraps the result in a `.framework` bundle.
9. **Injects** `.swiftmodule`/`.swiftinterface` from DerivedData when missing from the framework bundle (common with SPM dynamic libraries).
10. **Injects** ObjC public headers and modulemaps from the source tree for ObjC/mixed targets that don't include them in archive output.
11. **Assembles** xcframeworks via `xcodebuild -create-xcframework`.
12. **Detects** framework type (Swift, ObjC, or Mixed) based on content:
    - **Swift**: Has `.swiftinterface` files
    - **ObjC**: Has public headers + modulemap, no Swift interfaces
    - **Mixed**: Has both Swift interfaces and ObjC headers
13. **Verifies** every produced xcframework with strict per-unit checks. The build only reports success when all of these pass for every output:
    - The `Info.plist` parses through `plistlib` (catches AppleDouble `__MACOSX` ghost xcframeworks).
    - At least 2 slices (device + simulator) appear in `AvailableLibraries`.
    - Every slice's binary is a dynamically-linked Mach-O (no static archives masquerading as frameworks).
    - Swift/Mixed frameworks ship at least one `.swiftinterface` file.
    - ObjC/Mixed frameworks ship public headers under `Headers/` and a `module.modulemap`.
    - Failures are reported per-unit; the tool exits non-zero with a summary that names every failing output.

### Binary mode (`--binary`)

Some libraries (e.g. BlinkID, Firebase) distribute pre-built xcframeworks through SPM binary targets. Binary mode downloads these without building from source:

1. Normalizes the tag name for `v`-prefixed repositories, and verifies `--revision` if provided.
2. Creates a temporary `Package.swift` that depends on the target repo.
3. Runs `swift package resolve` to download binary artifacts.
4. Locates xcframeworks in `.build/artifacts/`, pruning `__MACOSX` AppleDouble ghosts that some vendor zips ship alongside the real artifacts (BlinkID 7.6.x is the canonical example).
5. Validates `--product` filters against the resolved artifact names.
6. Reports the filtered set that matches the request.
7. Copies the matching xcframeworks to the output directory and runs the same strict per-unit verify pass that source mode uses.

Product filtering (`--product`), revision verification (`--revision`), and dry-run all work in binary mode. In binary dry-run mode, the tool still resolves artifacts so it can validate the requested products and show the exact filtered set, but it does not copy anything to the output directory.

### `--target` escape hatch

Some packages declare important modules as `.target(...)` in `Package.swift` without exposing them as `.library(...)` products. stripe-ios is the canonical example: `StripeCore`, `StripeUICore`, `Stripe3DS2`, and `StripeCameraCore` are all plain targets, so `--product StripeCore` fails with "No library products matching filter". The `--target` flag tells the tool to inject a synthetic `.library(name: "<name>", type: .dynamic, targets: ["<name>"])` entry into `Package.swift` for each requested target, then build the synthesized product like any other library product.

Because the synthetic library is a real `.library(...)` declaration with `type: .dynamic`, only the requested target is forced dynamic — internal C/ObjC dependency targets keep their natural build type. This makes `--target` safe for packages like Firebase that bundle static helper libs (nanopb, leveldb, GoogleUtilities) inside their target graph. The tool refuses to synthesize a library for `.binaryTarget(...)` targets, so accidental misuse fails with a clear planner error.

Note: the older `MACH_O_TYPE=mh_dylib` global override is gone. Synthetic libraries replaced it because the global override broke any package whose internal targets produced object files or static archives.

### Always-clean build tree

Every source-mode build runs against a freshly-staged copy of the package with `.git`, `.build`, `DerivedData`, `node_modules`, and any sibling `.xcodeproj`/`.xcworkspace` files pruned. Pruning the Xcode projects forces `xcodebuild` to use SPM-generated schemes, which sidesteps both the "multiple projects with the current extension" error (GRDB ships `GRDB.xcodeproj` + `GRDBCustom.xcodeproj`) and the "does not contain a scheme" wording mismatch that the legacy bash had to grep around.

## Output

```
$ spm-to-xcframework https://github.com/Alamofire/Alamofire.git -v 5.10.2 -o ./output

Cloning https://github.com/Alamofire/Alamofire.git @ 5.10.2
Staging package into clean working tree...
Inspecting package...
Planning source build...

Plan for Alamofire @ 5.10.2  (source mode)
  Build units:
    [1] Alamofire         scheme=Alamofire         language=Swift  → Alamofire.xcframework
    [2] AlamofireDynamic  scheme=AlamofireDynamic  language=Swift  → AlamofireDynamic.xcframework
  Package.swift edits:
    - force_dynamic: Alamofire → targets=[Alamofire]

Preparing Package.swift edits...
  Prepare validated 1 edit(s) ✓
Executing 2 build unit(s)...
  Building Alamofire — device (arm64) + simulator (parallel)...
  ...
  Alamofire.xcframework ready [Swift]
  Building AlamofireDynamic — device (arm64) + simulator (parallel)...
  ...
  AlamofireDynamic.xcframework ready [Swift]

=== Summary ===
  Built: 2    Verified: 2    Failed: 0

Output: /path/to/output

Xcframeworks:
  Alamofire.xcframework         (16.0M) [Swift]
  AlamofireDynamic.xcframework  (16.0M) [Swift]
```

## Using with Swift.Bindings

The xcframeworks produced by this tool are ready for .NET binding generation with Swift.Bindings. The binding generator auto-detects framework type from the xcframework contents:

- **Swift** xcframeworks: P/Invoke bindings via ABI JSON
- **ObjC** xcframeworks: `ApiDefinition.cs` + `StructsAndEnums.cs` via clang AST
- **Mixed** xcframeworks: both pipelines, two-project output

## Known limitations

- Packages with very old `swift-tools-version` (< 5.0) fail at package resolution
- SPM-only products forced to dynamic linking can fail when system framework linkage is missing from the package manifest — these are typically redundant dynamic variants (e.g. `AlamofireDynamic`, `Lottie-Dynamic`)
- Packages that don't support library evolution (`-enable-library-evolution`) may produce xcframeworks without `.swiftinterface` files — Swift binding generation requires these, but ObjC binding generation is unaffected
- ObjC-only SPM targets must declare public headers via `publicHeadersPath` in `Package.swift` for headers to appear in the xcframework
- `--binary` only works with remote packages that distribute binary xcframeworks via SPM binary targets — packages with a mix of binary and source targets will only resolve the binary artifacts
- `--revision` requires the full 40-character commit SHA; short SHAs are rejected

## License

MIT
