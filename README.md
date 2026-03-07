# spm-to-xcframework

Build xcframeworks from Swift Package Manager packages — Swift, Objective-C, or mixed.

Takes an SPM package URL (or local path) and produces ready-to-use xcframeworks for each library product. Each xcframework contains device (arm64) and simulator slices built with `BUILD_LIBRARY_FOR_DISTRIBUTION=YES` for ABI stability.

Framework type (Swift, ObjC, Mixed) is auto-detected and reported in the build output.

## Install

```bash
# Clone and add to PATH
git clone https://github.com/user/spm-to-xcframework.git
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
| `-p, --product <name>` | Build only this product (default: all library products) |
| `--min-ios <ver>` | Minimum iOS deployment target (default: `15.0`) |
| `--include-deps` | Also build xcframeworks for transitive dependencies |
| `--verbose` | Show full xcodebuild output |
| `--dry-run` | Show what would be built without building |
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

# Build Stripe with all its sub-frameworks
spm-to-xcframework https://github.com/stripe/stripe-ios.git -v 25.6.2

# Build an ObjC library with a static SPM product (auto-promoted to dynamic)
spm-to-xcframework https://github.com/jdg/MBProgressHUD.git -v 1.2.0

# See what would be built without building
spm-to-xcframework https://github.com/kean/Nuke.git -v 12.8.0 --dry-run
```

## How it works

1. **Clones** the package at the specified tag (or copies a local path)
2. **Discovers** library products via `swift package dump-package` — works for Swift, ObjC, and mixed-language targets
3. **Resolves** build schemes via `xcodebuild -list` — handles packages with `.xcodeproj` (platform-suffixed schemes like `Alamofire iOS`) and pure SPM packages (auto-generated schemes)
4. **Patches** `Package.swift` to set all library products to `type: .dynamic` — only products become dynamic, internal dependency targets keep their natural build type
5. **Builds** device and simulator archives in parallel with:
   - `BUILD_LIBRARY_FOR_DISTRIBUTION=YES` — ABI stability + swiftinterface emission
   - `SKIP_INSTALL=NO` — framework included in archive products
6. **Promotes** static archives to dynamic frameworks when needed — some ObjC-only packages (e.g. MBProgressHUD) produce `.a` files even when patched to `.dynamic`. The tool detects this, re-links the static archive as a dynamic library via `clang -dynamiclib`, infers system framework dependencies from source imports, and wraps the result in a `.framework` bundle
7. **Injects** `.swiftmodule`/`.swiftinterface` from DerivedData when missing from the framework bundle (common with SPM dynamic libraries)
8. **Assembles** xcframeworks via `xcodebuild -create-xcframework`
9. **Detects** framework type (Swift, ObjC, or Mixed) based on content:
   - **Swift**: Has `.swiftinterface` files
   - **ObjC**: Has public headers + modulemap, no Swift interfaces
   - **Mixed**: Has both Swift interfaces and ObjC headers
10. **Validates** output with type-aware checks:
   - Swift/Mixed: warns if `.swiftinterface` files are missing
   - ObjC/Mixed: warns if public headers or modulemap are missing
   - All types: warns about static libraries, missing slices

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
  Building Alamofire — device (arm64)...
  Building Alamofire — simulator...
  Creating Alamofire.xcframework...
  Alamofire.xcframework ready [Swift]

[2/2] AlamofireDynamic
  ...

=== Summary ===
  Built: 1

Output: /path/to/output

Xcframeworks:
  Alamofire.xcframework (16M) [Swift]
```

## Using with Swift.Bindings

The xcframeworks produced by this tool are ready for .NET binding generation with Swift.Bindings. The binding generator auto-detects framework type from the xcframework contents:

- **Swift** xcframeworks → P/Invoke bindings via ABI JSON
- **ObjC** xcframeworks → `ApiDefinition.cs` + `StructsAndEnums.cs` via clang AST
- **Mixed** xcframeworks → both pipelines, two-project output

## Known limitations

- Packages with very old `swift-tools-version` (< 5.0) fail at package resolution
- SPM-only products forced to dynamic linking can fail when system framework linkage is missing from the package manifest — these are typically redundant dynamic variants (e.g. `AlamofireDynamic`, `Lottie-Dynamic`)
- Packages that don't support library evolution (`-enable-library-evolution`) may produce xcframeworks without `.swiftinterface` files — Swift binding generation requires these, but ObjC binding generation is unaffected
- ObjC-only SPM targets must declare public headers via `publicHeadersPath` in `Package.swift` for headers to appear in the xcframework

## License

MIT
