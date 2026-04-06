# spm-to-xcframework Rewrite Design

**Status:** Draft — awaiting agent-team execution.
**Source of truth for:** the session workers that will implement this rewrite.
**Related:** `/Users/wojo/Dev/swift-dotnet-packages/SPM_TO_XCFRAMEWORK_NOTES.md`
(root-cause analysis that motivated the rewrite — read this first if you are
a worker).

The notes document explains *why* the current tool fails for ~2 of 16
real-world libraries. This document specifies *what* to build instead and
*how* to ship it. Every "why" paragraph here is load-bearing — do not drop
it during implementation.

---

## 1. Goals

1. **Rock-solid for any SPM library.** The 16-library test matrix in
   `swift-dotnet-packages/libraries/` must all build cleanly without
   library-specific workarounds. Adding a 17th library should not require
   patching the tool.
2. **Structured decisions, not stderr scraping.** Every decision the tool
   makes must come from a typed model of the package, not from
   regex-walking `Package.swift` or grepping xcodebuild stderr. New
   xcodebuild error wording must not be able to break us.
3. **Single-file distribution.** The tool must remain installable by
   downloading one file, chmod-ing it, and running it. The downstream
   pin mechanism in `scripts/ensure-spm-to-xcframework.sh` (commit SHA +
   sha256) must continue to work unchanged.
4. **Stdlib-only dependencies.** Beyond what Xcode already requires
   (`xcodebuild`, `swift`, `xcrun`), the only runtime dependency may be
   `python3` (already required by the current tool).
5. **CLI and output back-compat.** All flags the downstream wrapper
   script currently passes must still work: `--version`, `--output`,
   `--product` (repeatable), `--target` (repeatable), `--binary`,
   `--revision`, `--min-ios`, `--verbose`, `--dry-run`, `--keep-work`,
   `--include-deps`. Output layout (`<OUTPUT>/<Framework>.xcframework`)
   must be unchanged.

## 2. Non-goals

- Multi-platform output. iOS only. macOS / tvOS / watchOS / visionOS are
  explicitly out of scope — the current tool is iOS-only and every
  consumer in `swift-dotnet-packages` is iOS.
- Reimplementing `xcodebuild -create-xcframework`. We continue to shell
  out to Apple's merge step.
- Preserving bash as the implementation language. See §4.
- Backwards compat with the bash tool's intermediate outputs (work dirs,
  derived data paths, etc.). Downstream doesn't care — only the final
  xcframework contents and the CLI surface.

## 3. Test matrix

Every session's acceptance bar is "the change keeps this matrix green, or
improves what it can build." The matrix is the 16 libraries in
`/Users/wojo/Dev/swift-dotnet-packages/libraries/` plus the failure cases
called out in the notes document.

| # | Library          | Mode   | Shape                                    | Why it exercises the tool                                              |
|---|------------------|--------|------------------------------------------|------------------------------------------------------------------------|
| 1 | Nuke             | source | single Swift product                    | boring case — must keep working                                        |
| 2 | Lottie           | source | single Swift product                    | boring case                                                            |
| 3 | Kingfisher       | source | single Swift product                    | boring case                                                            |
| 4 | Alamofire        | source | `Alamofire` automatic + `AlamofireDynamic` | already-dynamic product must not be double-patched                     |
| 5 | MBProgressHUD    | source | single ObjC product                      | static→dynamic promotion                                               |
| 6 | BlinkIDUX        | source | ObjC product with header tree            | ObjC public-header injection                                           |
| 7 | Stripe (Stripe) | source | `.library(Stripe)` + 11 `--target` modules | --target escape hatch; 12 builds in one run; internal C deps           |
| 8 | GRDB             | source | multi-xcodeproj + `GRDBSQLite` system lib + `GRDB-dynamic` already-dynamic | bugs 4 + 5 from notes doc                                              |
| 9 | BlinkID          | binary | artifactbundle with `__MACOSX` ghost      | bug 3 from notes doc                                                   |
|10 | iCarousel        | binary | single binary product                    | boring binary case                                                     |
|11 | Mappedin         | manual | provisioned out of band                  | (not tool's responsibility — covered by wrapper)                       |

For libraries not listed here the acceptance bar is simply
"builds successfully in source mode using defaults."

Sessions that touch `build_source`-equivalent code must run at least one
source-mode library. Sessions that touch binary mode must run BlinkID.
Sessions that touch the planner must cover the GRDB + Stripe shapes at
minimum.

## 4. Language choice: Python, stdlib-only, single file

The notes document lays out three options: Python, Swift, or
"bash with better decomposition." **We choose Python.**

- **Single-file distribution preserved.** One `spm-to-xcframework` file
  with a `#!/usr/bin/env python3` shebang. Same name as today — the
  downstream pin in `scripts/ensure-spm-to-xcframework.sh` is a URL
  `.../spm-to-xcframework` that doesn't change. Only the sha256 needs
  to be bumped after the rewrite lands.
- **Stdlib-only.** `argparse`, `dataclasses`, `json`, `subprocess`,
  `concurrent.futures`, `pathlib`, `shutil`, `re`, `hashlib`,
  `tempfile`, `logging`, `plistlib`, `urllib` — everything we need
  is in the stdlib since Python 3.9. macOS ships Python 3.9+ on
  every supported release (Monterey and later). No `pip install`
  anywhere.
- **Why not Swift.** Heaviest distribution cost. Requires either
  per-host compilation (users need a toolchain just to get the tool)
  or precompiled binaries per arch (breaks single-file, complicates
  the pin mechanism). The current tool's single-file distribution is
  a real operational win — don't give it up.
- **Why not bash.** The current tool already shells out to `python3`
  in eight places to parse JSON. That's the author's tell that the
  structured-inspection phase doesn't fit in bash. Moving the whole
  tool to Python deletes the shell/python impedance mismatch rather
  than adding more of it.
- **Python version floor.** We target Python 3.9 (shipped with
  macOS Monterey, November 2021) and use only features available in
  3.9 — no `match` statements (3.10), no `tomllib` (3.11). CI can
  enforce this with `python3 -c "import ast; ast.parse(open('spm-to-xcframework').read(), feature_version=(3,9))"`.

## 5. Architecture: Fetch → Inspect → Plan → Prepare → Execute → Verify

Six phases (Fetch got promoted out of Inspect after design review — see
§5.0). Each phase has a typed input, a typed output, and a narrow set of
side effects. Bugs become "our plan was wrong" instead of "we didn't
recognize a new error string."

**Staging happens in Fetch, not Prepare.** This is a deliberate shift
from the first draft of this design. If we staged after inspecting, the
Inspect phase would see a different filesystem than Execute would build
against, and any drift between the two (a missing resource bundle, an
xcodeproj that silently shadows an SPM target) would only surface at
build time. Staging first means `swift package dump-package`,
`xcodebuild -list`, scheme discovery, and `xcodebuild archive` all
operate on the same bytes. The pristine clone is still kept around
under `WORK_DIR/source` for `--keep-work` debugging; execution never
touches it after Fetch.

```
CLI args ──► parse_args ──► Config
                              │
                              ▼
                    ┌──────────────────┐
                    │   0. FETCH       │  clone/copy → stage into clean dir
                    │   Config → Staged │   (side effect: WORK_DIR/source, WORK_DIR/staged)
                    └──────────────────┘
                              │
                              ▼
                    ┌──────────────────┐
                    │   1. INSPECT     │  dump-package + scheme list + lang scan
                    │  Staged → Package │   (no filesystem edits — read-only on staged)
                    └──────────────────┘
                              │
                              ▼
                    ┌──────────────────┐
                    │   2. PLAN        │  pure function
                    │  Config+Package  │   no side effects
                    │      → Plan      │
                    └──────────────────┘
                              │
                              ▼
                    ┌──────────────────┐
                    │   3. PREPARE     │  manifest edits + round-trip validation
                    │  Plan+Staged →   │   (side effect: WORK_DIR/staged/Package.swift)
                    │    PreparedPlan  │
                    └──────────────────┘
                              │
                              ▼
                    ┌──────────────────┐
                    │   4. EXECUTE     │  xcodebuild archive, -create-xcframework
                    │  Prepared→Output │   inject .swiftinterface / ObjC headers
                    └──────────────────┘   (side effect: OUTPUT_DIR)
                              │
                              ▼
                    ┌──────────────────┐
                    │   5. VERIFY      │  strict per-unit checks on Output
                    │  Output → Report │
                    └──────────────────┘
```

### 5.0 Fetch (includes staging)

**Input:** `Config`.
**Output:** `WORK_DIR/source` (pristine) and `WORK_DIR/staged`
(the clean staged copy that every downstream phase operates on).
**Side effects:** network clone, filesystem copy.

Steps:

1. Resolve `--version` to an actual git tag (handle both bare-semver
   and `v`-prefix). **Preserve the user-supplied value separately**
   from the resolved tag — binary mode's SPM `exact:` field needs
   the bare semver; git operations need the resolved tag. (Bug 1 in
   notes.)
2. Verify `--revision` against `git ls-remote --tags url refs/tags/T
   refs/tags/T^{}` before cloning. Full 40-char SHA only.
3. Clone (remote) or copy (local path) into `WORK_DIR/source`.
4. **Stage** into `WORK_DIR/staged` by copying everything under
   `WORK_DIR/source` *except* a small set of toxic build-system
   artifacts. This is an **inclusion-by-default, exclusion-list**
   rule — we copy everything SPM might need (resources, bundles,
   `.docc` catalogs, `Package.resolved`, nonstandard source layouts)
   and only drop paths we know are harmful:
   - `.git/`
   - `.build/`
   - `DerivedData/`
   - `*.xcodeproj/`
   - `*.xcworkspace/`
   - `node_modules/` (rare, but some packages carry JS tooling)
   - any top-level path listed in the package's own `exclude:`
     (we can't parse this without dumping first, so this is a
     second-pass exclusion — see "Fetch is two steps" below)
5. Run `swift package resolve` against `WORK_DIR/staged` so
   downstream `xcodebuild archive` calls don't need network or
   parallel resolution.

**Fetch is two steps** because we need `Package.swift` parsed to know
the author's `exclude:` list, but we also need to exclude toxic
xcodeprojs before parsing to avoid triggering xcodebuild's "multiple
projects" error path. Resolution: copy step 4 minus the `exclude:`
rule → run `dump-package` on that staged dir → delete any extra
`exclude:` paths from the staged dir. The `exclude:` step is
post-hoc cleanup, not pre-filter.

### 5.1 Inspect

**Input:** `WORK_DIR/staged` (the clean staged copy from Fetch).
**Output:** `Package` — a typed model of the SPM package.
**Side effects:** runs `swift package dump-package`, walks staged
directories to classify target languages, runs `xcodebuild -list -json`.
**Read-only on the filesystem.**

**Must do:**

- Parse `swift package dump-package` output into a Package dataclass
  tree. The schema (confirmed on GRDB + Alamofire, Xcode 26.2):

  ```
  Package
    ├── name: str
    ├── toolsVersion: str
    ├── platforms: List[Platform{name, version}]
    ├── products: List[Product{name, type: Linkage, targets: List[str]}]
    └── targets:  List[Target{name, type: TargetKind, path, publicHeadersPath, dependencies, settings}]

  Linkage = Automatic | Dynamic | Static | Executable | Plugin | Snippet
  TargetKind = Regular | Test | System | Binary | Plugin | Macro
  ```

  Key discriminators:
  - `product.type` is JSON `{"library": ["automatic"|"dynamic"|"static"]}`.
    Non-library products (`executable`, `plugin`, `snippet`) are keyed
    differently. We only care about libraries.
  - `target.type` is a flat string: `"regular" | "test" | "system" | "binary" | "plugin" | "macro"`.
- **Classify each target's source language.** Walk the target path
  (excluding `Tests`, `Demo`, `Example`, `Examples`, `Samples`,
  `Playground`) under the **staged** directory and count:
  - `.swift` → Swift
  - `.m`, `.mm`, `.h` (without matching `.swift`) → ObjC
  - both → Mixed
  - none (system / binary) → N/A
  Store this on the target. The current tool does this ad-hoc; we do
  it once during inspect.
- **Derive scheme candidates.** Run `xcodebuild -list -json` against
  **`WORK_DIR/staged`** (which has no xcodeproj by construction, so
  we get the SPM-auto-generated scheme list without the
  multi-xcodeproj ambiguity). Record the list of schemes, but **do
  not commit to which scheme to use for which product yet** — that
  is the planner's job.

**Must NOT do:**

- Modify `Package.swift`. (That's Prepare.)
- Run `xcodebuild archive`. (That's Execute.)
- Read anything from `WORK_DIR/source` after Fetch. Always operate
  on `WORK_DIR/staged` so inspection and execution see the same
  filesystem.

### 5.2 Plan

**Input:** `Config`, `Package`.
**Output:** `Plan` — a list of `BuildUnit`s plus global preparation
instructions.
**Side effects:** none. Pure function.

```
Plan
  ├── stage: StageSpec
  │     ├── include: include-globs (always Package.swift, Sources, Tests, Package.resolved, resources, etc.)
  │     └── exclude: exclude-globs (always *.xcodeproj, *.xcworkspace, .build, .git, etc.)
  ├── package_swift_edits: List[PackageSwiftEdit]
  │     ├── add_synthetic_library(name, targets, type=.dynamic)     ← --target escape hatch
  │     └── force_dynamic_library(product_name)                     ← only if not already dynamic
  └── build_units: List[BuildUnit]
        ├── name: str             (what the user asked for)
        ├── scheme: str           (scheme name to pass to xcodebuild)
        ├── framework_name: str   (binary name inside the resulting .framework)
        ├── language: Swift|ObjC|Mixed
        ├── archive_strategy: Archive | StaticPromote
        └── source_targets: List[str]  (for header / system-framework inference)
```

**Key planning rules:**

1. **Already-dynamic products are not patched.** If the dumped product's
   linkage is already `dynamic`, emit no `force_dynamic_library` edit
   for it. (GRDB `GRDB-dynamic`, Alamofire `AlamofireDynamic`.)
2. **System-library products are never patched.** If every backing
   target of a library product has `TargetKind.System`, skip that
   product entirely — it cannot be built as a dynamic framework, and
   patching it hits "system library product shouldn't have a type
   and contain only one target" (bug 5 in notes). Log a clear skip
   reason.
3. **`--target` is implemented via synthetic library products, NOT
   `MACH_O_TYPE=mh_dylib`.** For each `--target T`, the planner adds a
   `PackageSwiftEdit.add_synthetic_library(name=T, targets=[T], type=.dynamic)`.
   The downstream build treats it as a normal library product. This
   eliminates the current tool's global `MACH_O_TYPE=mh_dylib`
   override entirely, and with it the caveat that `--target` breaks
   Firebase-shaped packages. The synthetic-library approach is safe
   because only the named target becomes dynamic; its internal C/ObjC
   dependency targets keep their natural build type. The edit is only
   rejected for `TargetKind.System`, `TargetKind.Binary`, and
   `TargetKind.Plugin/Macro` — fail fast with a clear error in those
   cases. If the target is already exposed as a `.library()` product,
   emit a warning and use the existing product instead of synthesizing
   a duplicate.
4. **Scheme resolution.** For each build unit, pick a scheme from the
   inspected scheme list:
   - exact match on the product name
   - case-insensitive exact match
   - product name plus one of the standard suffixes (` iOS`, `-iOS`,
     `-Package`)
   - fall back to the product name (xcodebuild will auto-generate a
     scheme against our staged-clean directory — see next point)
5. **Staging strategy: exclude `*.xcodeproj` and `*.xcworkspace` by
   default.** xcodebuild has no "ignore xcodeprojs" flag, so the
   multi-xcodeproj / wrong-scheme-in-xcodeproj recovery path of the
   current tool becomes the default. We always build against an
   xcodebuild-auto-generated scheme derived from `Package.swift`
   alone. This side-steps bug 4 (GRDB multi-xcodeproj) and also
   eliminates the class of failures where a vendor xcodeproj ships
   different build settings than the SPM build would use.

   Exception: if a package has **only** a `.xcodeproj` (no
   `Package.swift`), we have nothing to stage — fail with a clear
   error telling the user this tool only supports SPM packages.

6. **Language detection per build unit.** Copied from inspect:
   - Swift-only → no ObjC header injection, expect `.swiftinterface`.
   - Mixed → both swiftmodule injection and ObjC header injection.
   - ObjC-only → ObjC header injection; static→dynamic promotion may
     be needed.
7. **`--include-deps` planning.** After Execute runs and frameworks
   are in the archive, additional frameworks found in
   `Products/Library/Frameworks/` become secondary build units. The
   planner can't enumerate them ahead of time (we don't know until
   we build). Leave a flag on the Plan to enable post-execute scanning.

### 5.3 Prepare

**Input:** `Plan`, `WORK_DIR/staged` (already staged from Fetch).
**Output:** `PreparedPlan` — the `Plan` annotated with the actual
post-edit manifest state, guaranteed to round-trip through
`swift package dump-package`.
**Side effects:** mutates `WORK_DIR/staged/Package.swift`.

**Prepare is the riskiest phase.** Package.swift editing is
inherently source-text surgery against arbitrary Swift formatting.
Every structural promise in this design collapses if Prepare
produces a manifest that doesn't parse or doesn't produce the
products the planner expected. Three guardrails:

1. **Edits are whitelisted by the planner.** Prepare never decides
   which products to edit; it applies the exact list from
   `Plan.package_swift_edits`. If the planner said "don't touch
   GRDBSQLite," Prepare has no code path that could accidentally
   touch it.
2. **Edits are span-scoped.** The `.library(name: "X"…)` call for
   product X is located by string-searching for the exact
   `name: "X"` substring, then walking balanced parens to find
   the call's extent, then editing *only* inside that span. The
   algorithm never sees the rest of the file.
3. **Mandatory round-trip validation.** After all edits land,
   Prepare runs `swift package dump-package` on the modified
   staged directory and asserts:
   - the dump still parses
   - every product with a `force_dynamic` edit now has
     `type.library[0] == "dynamic"`
   - every `add_synthetic_library(name=T)` edit appears in the
     dumped products list with linkage `"dynamic"` and
     `targets == [T]`
   - the product count equals the planner's expected count
     (no accidental duplication, no accidental deletion)
   If any assertion fails, Prepare raises `PrepareError` with the
   diff between pre-edit and post-edit Package.swift and the list
   of failed assertions. **This is the gate that catches every
   manifest-editing bug class by construction.**

Steps:

1. Apply `package_swift_edits` to
   `WORK_DIR/staged/Package.swift`, in this order:
   - `force_dynamic_library(product_name)` — locate the
     `.library(` call whose `name: "product_name"` matches, walk
     balanced parens to find the closing `)`, edit within that
     slice only: insert `, type: .dynamic` after the `name:`
     clause, or replace any existing `type: .X` with
     `type: .dynamic`.
   - `add_synthetic_library(name, targets)` — insert a new
     `.library(name: "T", type: .dynamic, targets: ["T"])` entry
     immediately before the closing `]` of the `products:` array.
2. Write `.original-Package.swift` alongside the edited manifest as
   a debugging artifact.
3. Run the mandatory round-trip validation above. On success, build
   a `PreparedPlan` that wraps the original `Plan` and adds the
   parsed post-edit `Package`.
4. Re-run `swift package resolve` if any synthetic library was added
   (new products can pull in new dependency resolution state).

### 5.4 Execute

**Input:** `Plan`, `WORK_DIR/staged`.
**Output:** `<OUTPUT_DIR>/<Framework>.xcframework` for each build unit
plus (optionally) dependency xcframeworks.
**Side effects:** runs xcodebuild.

Per build unit:

1. **Parallel device + simulator archive.**
   ```
   xcodebuild archive \
       -scheme <scheme> \
       -destination 'generic/platform=iOS'         (or 'iOS Simulator')
       -archivePath WORK_DIR/archives/<name>-<slice>.xcarchive \
       -derivedDataPath WORK_DIR/dd/<name>/<slice> \
       -resultBundlePath WORK_DIR/results/<name>-<slice>.xcresult \
       BUILD_LIBRARY_FOR_DISTRIBUTION=YES \
       SKIP_INSTALL=NO \
       IPHONEOS_DEPLOYMENT_TARGET=<min_ios> \
       GCC_TREAT_WARNINGS_AS_ERRORS=NO \
       SWIFT_TREAT_WARNINGS_AS_ERRORS=NO \
       OTHER_SWIFT_FLAGS=-no-verify-emitted-module-interface \
       -skipPackagePluginValidation \
       -skipMacroValidation
   ```
   **No `MACH_O_TYPE=mh_dylib`.** Dynamic linkage is always handled at
   the Package.swift layer by the planner, never at the xcodebuild CLI
   layer.

2. **On failure, read the xcresult bundle, not stderr.**
   ```
   xcrun xcresulttool get build-results --path WORK_DIR/results/<name>-<slice>.xcresult
   ```
   This returns JSON with `issues.errors[]`, each with `targetName`,
   `message`, `sourceURL`. Classify the errors and surface them with
   the build unit name, target name, and the top 3 error messages.
   Do **not** grep stderr to make decisions. The only side-effect-string
   match we ever do is "was there ANY output on stderr?" for
   informational purposes.

3. **Locate the built framework.** Look in
   `<archive>/Products/Library/Frameworks/<framework_name>.framework`
   first (per `fw_name` resolution rule inherited from current tool).
   If absent, look for a static archive at
   `<archive>/Products/usr/local/lib/lib<something>.a` and trigger the
   StaticPromote strategy.

4. **StaticPromote strategy** (when xcodebuild produced `.a`):
   - Detect architectures with `lipo -archs`.
   - Detect system framework imports by scanning target sources
     (`#import <Framework/…>`, `@import Framework`) and by reading
     linker settings from the Package.swift dump. Both sources, union
     of results. This already works in the current tool — port it.
   - Re-link with `clang -dynamiclib` plus detected frameworks plus
     `-Xlinker -undefined dynamic_lookup` as a safety net.
   - Wrap result in a minimal `.framework` bundle with generated
     `Info.plist`.

5. **Inject `.swiftmodule`/`.swiftinterface` from DerivedData** if the
   built framework lacks them. Same logic as current tool.

6. **Inject ObjC headers** from `publicHeadersPath` in the package
   dump, if the framework's `Headers/` directory is empty or missing.
   Generate a `module.modulemap` (umbrella header if one exists, else
   list all headers explicitly).

7. **Merge slices via `xcodebuild -create-xcframework`.**

8. **`--include-deps`:** after the primary framework is built, scan
   the device archive for additional `.framework` bundles that aren't
   the primary, and build an xcframework for each. Port from current
   tool.

### 5.5 Verify

**Input:** `PreparedPlan`, `<OUTPUT_DIR>`.
**Output:** printed summary; non-zero exit if any required unit is
missing or malformed.
**Side effects:** none.

**Verify is strict, not advisory.** The downstream .NET binding
generator cannot consume an xcframework that's missing
`.swiftinterface` (Swift path) or public headers + modulemap (ObjC
path). The current tool treats these as warnings and still reports
success; the rewrite treats them as fatal for the affected unit.
Reporting "built 5, failed 0" when three of them can't be bound is
a lie.

For each planned build unit, the following are **fatal per unit** —
the unit counts as failed and the overall exit code is non-zero:

1. `<OUTPUT_DIR>/<Framework>.xcframework` does not exist.
2. `Info.plist` does not exist, or does not parse as a plist via
   `plistlib`. This is the catch for any `__MACOSX`-class ghost that
   slipped past Fetch/Execute filtering — the ghost's `Info.plist`
   is an AppleDouble resource fork, not a real plist.
3. Fewer than two slices present (need device + sim).
4. Any slice binary is not `dynamically linked` per
   `subprocess.run(['file', binary])` output. Static archives
   masquerading as frameworks fail downstream.
5. Swift/Mixed unit: zero `.swiftinterface` files under the slice.
6. ObjC/Mixed unit: zero public `.h` files under the slice's
   `Headers/` directory, OR no `module.modulemap` present.

The following are **warnings** (non-fatal, printed for visibility):

- Framework type detected as "Unknown" (no swiftinterface, no
  headers, but binary is present — rare, probably a custom module
  type).
- `.abi.json` missing for a Swift unit (the binding generator
  regenerates it at bind time, so not a blocker — but worth
  surfacing).
- Size outliers (xcframework above N hundred MB — not a failure,
  just a sanity signal).

Verify also prints the framework type label (`[Swift]`, `[ObjC]`,
`[Mixed]`) for every successfully-verified unit, matching the
current tool's output format.

Exit code: 0 iff every planned unit passed strict verification.
Otherwise non-zero with a machine-readable summary.

## 6. Binary mode

Binary mode is the same pipeline, just with a different flavor of
each phase. **It is deliberately less "typed" than source mode**: we
don't have a package to inspect, we have a vendor repo to resolve
and a filesystem to walk afterward. The structured boundary for
binary mode is "the list of xcframeworks discovered under
`.build/artifacts/` after `swift package resolve` completes,"
rather than a `Package` dataclass. This is a secondary model but
still far better than the current tool's stderr-and-find approach.

- **Fetch:** skip. There is nothing to clone or stage — the vendor's
  repo is pulled via SPM's resolver instead.
- **Inspect:** synthesize a resolver `Package.swift` in
  `WORK_DIR/binary-resolve/` that depends on the vendor repo by
  exact semver. **Always feed the bare semver (`USER_VERSION`) to
  SPM's `exact:` field, never the normalized git tag.** Bug 1 fix.
  Run `swift package resolve`. The "package" for inspect purposes
  is the list of `.xcframework` directories discovered under
  `.build/artifacts/` after resolve, walked via `os.walk` with
  `__MACOSX` pruning (bug 3 fix). Each discovered xcframework is
  treated as a `BinaryArtifact` record.
- **Plan:** filter `BinaryArtifact` records by `--product`. Each
  surviving artifact becomes a build unit whose execute strategy
  is `CopyArtifact` instead of `Archive`. `--target` is rejected
  (it's a source-build escape hatch) with a clear error.
- **Prepare:** no-op. There is no manifest to edit.
- **Execute:** copy each planned artifact into `<OUTPUT_DIR>`.
  Fail fast if the source or destination has `__MACOSX` in its
  path.
- **Verify:** same strict rules as source mode. The plist parse
  in §5.5 is the final gate that catches any AppleDouble ghost
  that slipped through earlier pruning.

## 7. Error handling philosophy

1. **Classify errors by phase.**
   - **Inspect errors** (can't clone, can't parse Package.swift, tag
     doesn't exist) are user-facing and printed without a Python
     traceback.
   - **Plan errors** (filter matched nothing, --target on a binary
     target, etc.) are user-facing and printed with a suggested fix.
   - **Prepare errors** (failed to edit Package.swift, staging
     collision) are bugs in this tool and print a traceback.
   - **Execute errors** (xcodebuild failed) are surfaced with the
     parsed xcresult and a short "top 3 errors from target X"
     summary. Full log path printed so the user can inspect it.
   - **Verify errors** (missing output) are fatal and list what's
     missing.
2. **`--verbose` controls output verbosity, not error handling.**
   Error handling is deterministic; `--verbose` just switches the
   xcodebuild output from "tail -5" to "streamed live".
3. **No sys.exit() below main().** Use exceptions with a small
   hierarchy: `InspectError`, `PlanError`, `PrepareError`,
   `ExecuteError`, `VerifyError`. `main()` catches them and maps to
   exit codes (3/4/5/6/7) + clean messages.

## 8. Code layout inside the single file

One file, sectioned by ASCII header comments. Order:

```
#!/usr/bin/env python3
"""Module docstring — CLI usage."""

# --- Imports (stdlib only) ---
# --- Logging + color output ---
# --- Errors ---
# --- Config (dataclass from argparse) ---
# --- Model: Package, Product, Target, Platform, Linkage, TargetKind ---
# --- Model: Plan, BuildUnit, StageSpec, PackageSwiftEdit ---
# --- Phase 1: Inspect ---
#       fetch_source()
#       verify_revision()
#       normalize_version_tag()
#       dump_package()
#       scan_target_languages()
#       discover_schemes()
# --- Phase 2: Plan ---
#       plan_source_build()
#       plan_binary_build()
#       resolve_scheme()
#       filter_products()
# --- Phase 3: Prepare ---
#       stage_package()
#       apply_package_swift_edits()
#       edit_force_dynamic()
#       edit_add_synthetic_library()
# --- Phase 4: Execute ---
#       execute_source_plan()
#       execute_binary_plan()
#       run_xcodebuild_archive()
#       read_xcresult_errors()
#       promote_static_to_framework()
#       detect_system_frameworks()
#       inject_swiftmodule()
#       inject_objc_headers()
#       create_xcframework()
# --- Phase 5: Verify ---
#       verify_output()
#       detect_framework_type()
# --- CLI ---
#       parse_args()
#       main()

if __name__ == "__main__":
    main()
```

Target length: 1500–2000 lines including doc comments. The current
bash is 1736 lines — if the Python rewrite is shorter than 1200, we
probably dropped something; if it's longer than 2500, we
over-engineered. Use judgment.

## 9. Testing strategy

**No third-party test framework** — we want zero runtime dependencies.
Testing happens three ways:

1. **Snapshot fixtures** run via `--self-test=fast`. A hidden CLI flag
   that exercises the Inspect and Plan phases against static JSON
   fixtures embedded as Python string constants (snapshots of
   `swift package dump-package` for GRDB, Stripe, BlinkID binary,
   Alamofire). Each session adds fixtures relevant to its phase. This
   mode does not run xcodebuild, doesn't invoke the real `swift`
   toolchain, and doesn't touch the network — it's safe to run in
   any CI.
2. **Manifest round-trip checks** run via `--self-test` (the default).
   For each of a small set of embedded `Package.swift` fixtures (GRDB
   snippet, Stripe snippet, Alamofire snippet, a system-library
   snippet), the test harness:
   - writes the fixture to a temp dir
   - runs Prepare's `apply_package_swift_edits` against planner-synthetic
     edit lists
   - invokes the real `swift package dump-package` on the edited
     manifest
   - asserts the post-edit product list matches the planner's
     expectation
   This catches the entire class of manifest-editing bugs the current
   tool suffers from. It requires `swift` on PATH (which is already a
   runtime requirement of the tool) but not the full Xcode archive
   pipeline.
3. **Integration matrix** — running the tool end-to-end against the
   16-library matrix in `swift-dotnet-packages`. This is what the
   human actually runs. Sessions that touch the pipeline must run at
   least one integration case from the matrix before committing, and
   document the result in the commit message.

The `--self-test` flag:
- Default: runs all snapshot checks + manifest round-trip checks.
- `--self-test=fast`: snapshot checks only (no real swift invocation).
- Prints a summary (`N passed, 0 failed`).
- Exits non-zero on any failure.
- Does **not** run xcodebuild or touch the network in either mode.

## 10. Migration path for the downstream repo

After the rewrite lands on `main` of this repo:

1. Bump `scripts/ensure-spm-to-xcframework.sh` in `swift-dotnet-packages`
   to the new commit SHA and its new sha256. The pin mechanism is
   contents-agnostic, so bash → Python is transparent to it.
2. Delete the inline `build_binary()` fallback in
   `scripts/build-xcframework.sh` (it's only there because of bug 1 +
   bug 3; both are now fixed upstream).
3. Flip `libraries/GRDB/library.json` from `manual` back to `source`
   (bugs 4 + 5 are fixed).
4. Run the full matrix (`scripts/build-xcframework.sh` for each
   library), confirm all 16 build cleanly.
5. Remove the "Known upstream gaps" section of
   `swift-dotnet-packages/CLAUDE.md`.

Downstream migration is **not** in scope for the rewrite sessions. It
happens afterwards, as a separate commit in `swift-dotnet-packages`.

---

## 11. Sessions

Each session is one agent-team worker. Sessions are sequential — later
sessions depend on earlier ones landing. Each worker commits with a
`Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>`
trailer.

**Ground rules for every session:**

- **Start fresh, don't modify the bash script.** The rewrite lives at
  `spm-to-xcframework.py` inside the repo during development. The
  final session renames/replaces to become `spm-to-xcframework`.
  During development, the old bash script stays untouched so it can
  still serve as a reference implementation.
- **Each session must leave the repo green.** `python3 -m py_compile
  spm-to-xcframework.py` must succeed. `./spm-to-xcframework.py
  --self-test` must pass. `./spm-to-xcframework.py --help` must show
  useful output.
- **Each session must run at least one integration case** from the
  matrix that exercises the phase(s) it added. Put the result (pass
  or fail) in the commit body.
- **Each session must `/ai-pair-programming`** before committing.
  Send the changed file(s) and the session description. Address the
  feedback that's correct; document the feedback that's skipped and
  why.
- **Uncommitted local edits to the bash script must NOT be carried
  forward.** The working tree has drafted fixes for bug 3 and bug 4
  from the notes document — ignore them. The rewrite fixes those
  bugs by design; those patches become moot.

---

### Session 1 — Scaffold + Fetch + Inspect phases

**Deliverable:** `spm-to-xcframework.py` containing:

- Shebang, module docstring, stdlib imports.
- Logging helpers (`die`, `info`, `warn`, `success`, `dim`,
  `verbose_log`) with ANSI color support that respects `NO_COLOR`
  and `not sys.stdout.isatty()`.
- `Config` dataclass populated from `parse_args()` — every CLI flag
  from §1 goal 5.
- `Package` / `Product` / `Target` / `Platform` / `Linkage` / `TargetKind`
  dataclasses per §5.1.
- Error classes (`InspectError`, `PlanError`, `PrepareError`,
  `ExecuteError`, `VerifyError`).
- **Fetch-phase functions per §5.0:** `verify_revision`,
  `normalize_version_tag`, `fetch_source`, `stage_source`. The
  `stage_source` step is inclusion-by-default with the toxic-artifact
  exclusion list (`.git`, `.build`, `DerivedData`, `*.xcodeproj`,
  `*.xcworkspace`, `node_modules`), and does the two-pass
  `exclude:` cleanup described in §5.0 after dumping the package
  once.
- **Inspect-phase functions per §5.1:** `dump_package`,
  `scan_target_languages`, `discover_schemes`. All operate on
  `WORK_DIR/staged`, never on `WORK_DIR/source`.
- `--self-test=fast` harness with snapshot fixtures for at minimum
  Nuke (trivial) and GRDB (has system target + GRDB-dynamic). Tests
  assert: product count, product linkage classification, target type
  classification, scheme discovery result, language classification.
- `--self-test` (default) runs the fast checks plus basic Fetch
  integration against a tiny local SPM package fixture committed
  under `testdata/` to exercise the stage-and-exclude logic.
- A minimal `main()` that runs parse_args + fetch + inspect and
  prints the Package model. Wire an `--inspect-only` flag to exit
  after Inspect for exploration.

**Acceptance:**
- `./spm-to-xcframework.py --self-test` green (both fast and full
  modes).
- `./spm-to-xcframework.py https://github.com/kean/Nuke.git -v 12.8.0 --inspect-only`
  prints the parsed package without error and confirms the staged
  dir has no `*.xcodeproj`.
- `./spm-to-xcframework.py https://github.com/groue/GRDB.swift.git -v 7.9.0 --inspect-only`
  succeeds despite the multi-xcodeproj source — the staged dir
  strips both `GRDB.xcodeproj` and `GRDBCustom.xcodeproj`.
- Matrix cases executed: Nuke, GRDB (inspect only, no build).

**Out of scope:** Plan, Prepare, Execute, Verify. The file is
intentionally half-built at the end of this session.

---

### Session 2 — Plan phase + dry-run output

**Deliverable:** extended `spm-to-xcframework.py` with:

- `Plan` / `BuildUnit` / `StageSpec` / `PackageSwiftEdit` dataclasses
  per §5.2.
- `plan_source_build(config, package) -> Plan`.
- `plan_binary_build(config, resolved_artifacts) -> Plan` (minimal
  — just enough to record the list of artifacts to copy; Execute
  phase 4 will wire it up).
- Planner rules: skip already-dynamic products for the
  force_dynamic edit, refuse to synthesize a library for system /
  binary / plugin / macro targets, reject `--target` in binary mode.
- Scheme resolution using the data from `discover_schemes`.
- Language detection transfer from inspected targets to build units.
- `--dry-run` wired to the Plan: prints the list of build units,
  their schemes, their framework names, and any package.swift edits
  that would be applied.
- `--self-test` additions: snapshot planner tests for GRDB (must
  skip GRDBSQLite, must NOT patch GRDB-dynamic), Stripe (must
  synthesize libraries for the 11 --target modules), BlinkID binary
  (must accept `--product BlinkID` and plan it as a copy).

**Acceptance:**
- `./spm-to-xcframework.py --self-test` green (expanded fixtures).
- `./spm-to-xcframework.py https://github.com/groue/GRDB.swift.git -v 7.9.0 --dry-run`
  prints a plan that:
  - includes `GRDB` with a `force_dynamic` edit
  - includes `GRDB-dynamic` with NO force_dynamic edit (already dynamic)
  - does NOT include `GRDBSQLite`
- `./spm-to-xcframework.py https://github.com/stripe/stripe-ios.git -v 25.6.2
  --product Stripe --target StripeCore --target StripeUICore --dry-run`
  prints a plan that includes 3 build units, with synthetic library
  edits for StripeCore and StripeUICore.
- Matrix cases executed: GRDB dry-run, Stripe dry-run, BlinkID
  binary dry-run.

**Out of scope:** Prepare, Execute, Verify. `--dry-run` exits after
printing the plan.

---

### Session 3 — Prepare + Execute (source mode, single slice)

**Deliverable:** extended `spm-to-xcframework.py` with:

- Prepare phase per §5.3: `apply_package_swift_edits`,
  `edit_force_dynamic`, `edit_add_synthetic_library`, and the
  **mandatory round-trip validator** `validate_prepared_manifest`.
  All edits go through the planner's whitelist; the validator
  re-runs `swift package dump-package` and asserts product linkage
  matches planner expectations. A failure here raises `PrepareError`
  with a diff between pre-edit and post-edit manifest.
- Package.swift editing helpers that operate on the planner's
  whitelist, not on a regex walk. For `force_dynamic`, locate the
  `.library(name: "<whitelisted name>"…)` call, walk balanced parens
  to find its extent, and edit only inside that slice. For
  `add_synthetic_library`, insert a new `.library(...)` entry
  immediately before the closing `]` of the `products:` array.
- Execute phase (source mode, single slice first): `run_xcodebuild_archive`
  for `generic/platform=iOS` only, driving it to completion for one
  build unit. Framework location + move into place. **No parallelism
  yet** — that comes in session 4. No static promotion, no ObjC
  injection, no swiftmodule injection yet. Session 3's execute output
  is "one .xcarchive per build unit with the primary .framework
  inside."
- `read_xcresult_errors` — call `xcrun xcresulttool get build-results`
  and parse the JSON to return the first N errors with target +
  message. Wire it into `ExecuteError`.
- `--self-test` additions: the manifest round-trip tests described
  in §9 item 2. For each embedded fixture (GRDB snippet, Stripe
  snippet, Alamofire snippet, system-library snippet), the test
  writes the fixture to a temp dir, applies planner-synthetic edits,
  runs real `swift package dump-package`, and asserts the post-edit
  product list matches expectations. This is the core pre-merge
  coverage for Prepare — it must pass before the commit lands.
- Wire up `main()` for source mode so it runs all of Fetch → Inspect
  → Plan → Prepare → partial-Execute and reports what was produced.

**Acceptance:**
- `./spm-to-xcframework.py --self-test` green (includes the new
  manifest round-trip tests, which actually invoke swift).
- End-to-end on Nuke: produces a `Nuke-ios-arm64.xcarchive` with
  `Nuke.framework` inside. No xcframework yet (that's session 4).
- End-to-end on GRDB: the SPM-mode build (no xcodeprojs in staged
  dir, thanks to Fetch) produces a `GRDB-ios-arm64.xcarchive`.
  Verify the planner correctly force_dynamic'd `GRDB`, skipped
  `GRDB-dynamic` (already dynamic), and skipped `GRDBSQLite`
  (system target). Confirm by reading the post-edit Package.swift
  and asserting the expected shape.
- End-to-end on Stripe with `--target StripeCore`: produces a
  `StripeCore-ios-arm64.xcarchive` via synthetic library. **Grep
  the xcodebuild invocation log for `MACH_O_TYPE` and assert it
  does NOT appear** — this proves the escape hatch works without
  the old global override.
- Prepare's round-trip validator catches a deliberately-broken
  edit (e.g., force_dynamic on a non-existent product) and raises
  `PrepareError` — add a test for this.
- Matrix cases executed: Nuke, GRDB, Stripe (StripeCore only — no
  need to run all 12 targets yet).

**Out of scope:** simulator slice, parallelism, xcframework creation,
injection passes, binary mode, Verify.

---

### Session 4 — Parallel slices, xcframework merge, injection, binary mode

**Deliverable:** extended `spm-to-xcframework.py` with:

- Parallel device + simulator archive via
  `concurrent.futures.ThreadPoolExecutor(max_workers=2)`. Both
  futures complete before the function returns; output for each is
  captured to separate log files and tailed to the terminal after
  both settle (to avoid interleaving).
- `promote_static_to_framework` port. Same algorithm as current tool
  (lipo archs, clang -dynamiclib, Info.plist generation,
  detect_system_frameworks). Triggered only when Execute can't find
  the primary .framework and finds a single .a instead.
- `inject_swiftmodule` port. Triggered when the built framework
  lacks `.swiftinterface` but DerivedData has a matching
  `.swiftmodule` directory.
- `inject_objc_headers` port. Uses the publicHeadersPath + source
  tree walk from the package dump, generates a module.modulemap.
- `create_xcframework` — just wraps `xcodebuild -create-xcframework`
  with device + sim inputs.
- Binary mode Execute: `execute_binary_plan` creates the resolver
  Package.swift, runs `swift package resolve`, walks
  `.build/artifacts` with `os.walk` + `__MACOSX` prune, copies each
  xcframework to the output dir. Validates `--product` filter.
- `--self-test` additions: fixture for a Package.swift with a
  `.library(name: "Foo", targets: ["Foo"])` and assert that after
  `edit_force_dynamic` the resulting file can be round-tripped
  through `swift package dump-package` (requires actually invoking
  swift in a temp dir — gate behind an env var so CI without Xcode
  can skip it).
- `--include-deps` wiring — scan archives for extra frameworks after
  the primary build, build xcframeworks for each non-primary one.

**Acceptance:**
- `./spm-to-xcframework.py --self-test` green.
- End-to-end full success on Nuke: produces
  `<OUTPUT>/Nuke.xcframework` with device + sim slices, Swift
  interfaces present, dynamic binary.
- End-to-end full success on MBProgressHUD: static→dynamic
  promotion produces a valid `.xcframework`.
- End-to-end full success on GRDB: both GRDB and GRDB-dynamic
  xcframeworks produced (where GRDB is patched, GRDB-dynamic is
  left alone).
- End-to-end full success on Stripe with `--product Stripe --target
  StripeCore --target StripeUICore`: 3 xcframeworks, no
  `MACH_O_TYPE=mh_dylib` anywhere in the xcodebuild invocations
  (grep WORK_DIR logs to confirm).
- End-to-end full success on BlinkID binary: `BlinkID.xcframework`
  produced with a real `Info.plist` (not a `._Info.plist` AppleDouble
  ghost).
- Matrix cases executed: Nuke, MBProgressHUD, GRDB, Stripe (3
  products), BlinkID binary.

**Out of scope:** Verify phase (exists as minimal hand-checks for
session 4, gets formalized in session 5); migration of downstream;
retirement of the bash script.

---

### Session 5 — Verify phase, full matrix, replace bash script

**Deliverable:** final `spm-to-xcframework.py` that becomes the new
`spm-to-xcframework`:

- Verify phase per §5.5. Plist parsing via `plistlib` — this is the
  catch for any remaining `__MACOSX`-class ghost.
- `detect_framework_type` port, consumed by Verify and by the
  summary printer.
- Final summary output matching the current tool's visual format
  (`=== Summary ===`, counts, per-xcframework size + type label).
- Full end-to-end run through the 16-library matrix in
  `/Users/wojo/Dev/swift-dotnet-packages`. Document the result of
  each in the commit message.
- **Replace the bash script.** `git mv spm-to-xcframework
  spm-to-xcframework.bash.bak` then `git mv spm-to-xcframework.py
  spm-to-xcframework`. Delete the `.bash.bak` file in the same
  commit (the bash script is preserved in git history; no reason to
  keep a second copy in the working tree). Update `README.md` with:
  - "implemented in Python" one-liner
  - updated requirements (Python 3.9+ — already required)
  - any flag changes (there should be none; back-compat is a goal)
- Delete `TODO.md` if its entries are all implemented (or update it
  to reflect what's left).

**Acceptance:**
- `./spm-to-xcframework --self-test` green (note: no `.py`
  extension anymore).
- `./spm-to-xcframework --help` matches the CLI surface of the
  current tool (same flags, same output format).
- Full matrix: all source-mode libraries in
  `swift-dotnet-packages/libraries/` build cleanly. BlinkID binary
  builds cleanly. Document any library that fails with a root-cause
  note — they should all pass, but if one doesn't, the design must
  evolve, not be papered over.
- A single final commit on the rewrite branch, ready to tag and
  pin downstream.

**Out of scope:** downstream pin bump + workaround rollback (that's a
separate PR in `swift-dotnet-packages` after this lands).

---

## 12. What success looks like

When this design is fully executed, the following should all be true:

1. The repo's `spm-to-xcframework` is a Python 3.9+ single-file script
   that is roughly the same length as the bash it replaced, with the
   same CLI surface, the same output format, and the same single-file
   distribution story.
2. All five bugs documented in
   `SPM_TO_XCFRAMEWORK_NOTES.md` are fixed structurally (not patched).
3. Running the 16-library matrix in `swift-dotnet-packages/libraries/`
   produces every expected xcframework, without any library-specific
   workarounds, and without the `scripts/build-xcframework.sh` inline
   binary-mode fallback.
4. `--target` works without `MACH_O_TYPE=mh_dylib`, so the
   Firebase-shaped-packages caveat in the current README is obsolete.
5. A hypothetical 17th library with a previously-unseen Package.swift
   shape either works or fails with a clear, phase-specific error
   message that points at the root cause, not at a missing grep.
