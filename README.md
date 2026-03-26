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

Or just run it directly — it's a single self-contained script with no dependencies beyond Xcode.

### Requirements

- macOS with Xcode installed (provides `xcodebuild`, `swift`, `python3`)

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
    --product StripeCore --product StripePayments

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
4. **Discovers** library products via `swift package dump-package` — works for Swift, ObjC, and mixed-language targets.
5. **Resolves** build schemes via `xcodebuild -list` — handles packages with `.xcodeproj` (platform-suffixed schemes like `Alamofire iOS`) and pure SPM packages (auto-generated schemes).
6. **Patches** `Package.swift` to set all library products to `type: .dynamic` — only products become dynamic, internal dependency targets keep their natural build type.
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
13. **Validates** output with type-aware checks:
    - Swift/Mixed: warns if `.swiftinterface` files are missing
    - ObjC/Mixed: warns if public headers or modulemap are missing
    - All types: warns about static libraries, missing slices

### Binary mode (`--binary`)

Some libraries (e.g. BlinkID, Firebase) distribute pre-built xcframeworks through SPM binary targets. Binary mode downloads these without building from source:

1. Normalizes the tag name for `v`-prefixed repositories, and verifies `--revision` if provided.
2. Creates a temporary `Package.swift` that depends on the target repo.
3. Runs `swift package resolve` to download binary artifacts.
4. Locates xcframeworks in `.build/artifacts/`.
5. Validates `--product` filters against the resolved artifact names.
6. Reports the filtered set that matches the request.
7. Copies the matching xcframeworks to the output directory and validates them.

Product filtering (`--product`), revision verification (`--revision`), and dry-run all work in binary mode. In binary dry-run mode, the tool still resolves artifacts so it can validate the requested products and show the exact filtered set, but it does not copy anything to the output directory.

### Scheme fallback

When a package has both a `.xcodeproj` and `Package.swift`, some SPM-only products may not have matching Xcode schemes. The tool detects "does not contain a scheme" errors and automatically retries with the `.xcodeproj` moved aside, falling back to SPM-generated schemes.

## Output

```
$ spm-to-xcframework https://github.com/Alamofire/Alamofire.git -v 5.10.2 -o ./output

spm-to-xcframework

Cloning https://github.com/Alamofire/Alamofire.git @ 5.10.2
Resolving package...

Found 2 library product(s):
  - Alamofire
  - AlamofireDynamic

Discovering build schemes...

Building xcframeworks...

[1/2] Alamofire
  Using scheme: Alamofire iOS
  Building Alamofire — device (arm64) + simulator (parallel)...
  Creating Alamofire.xcframework...
  Alamofire.xcframework ready [Swift]

[2/2] AlamofireDynamic
  ...

=== Summary ===
  Built: 2

Output: /path/to/output

Xcframeworks:
  Alamofire.xcframework (16M) [Swift]
  AlamofireDynamic.xcframework (16M) [Swift]
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
