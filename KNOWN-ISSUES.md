# Known Issues

Defects observed while converting a 120-library SPM corpus (Xcode 26.3, 2026-07).
Raw evidence (logs, batch reports, result.json per library) lives in
`/Users/wojo/Dev/internal-binding-testing/corpus-sweep/` — finding IDs below (X-00N)
match `corpus-sweep/findings.md`.

Most of the corpus findings (X-001, X-003, X-004, X-005, X-006) are now fixed and
carry manifest-/packaging-layer regression tests. The remaining entries are an
upstream/toolchain limitation the tool now surfaces with a diagnostic rather than a
bare failure (X-002) and the library-evolution wall it can only detect, not remove.

## Fixed (2026-07)

- **X-001 — sibling product packaged without its internal C-module dependency.**
  swift-numerics / swift-algorithms: a pure-C shim target (`_NumericsShims`) that a
  Swift product's `private.swiftinterface` imports was dropped, because the language
  map collapses every ClangTarget to ObjC and the auto-synth sibling promotion only
  admitted pure-Swift helpers. Inspect now sub-classifies a ClangTarget as pure-C
  (`Target.clang_is_pure_c`, from `describe`'s per-target source extensions) and the
  planner promotes pure-C shims into their own synthetic `.library(type: .dynamic)`
  build unit while still skipping genuine ObjC/C++/mixed helpers (WCDB `objc-core`)
  and anything declaring its own linker settings. Verified: `swift-numerics` dry-run
  now plans a `_NumericsShims.xcframework` synthetic-library unit.

- **X-003 — `prepare` stripped load-bearing post-init `package.dependencies` mutations.**
  Dependency declarations added or mutated after the `Package(...)` initializer are
  now preserved (or the referencing targets pruned), so packages like swift-navigation
  and grpc-swift resolve instead of failing at `dump-package`.

- **X-004 — injected ObjC modulemap used the hyphenated product name.**
  swift-markdown-ui → cmark-gfm: the injected modulemap named the module by its
  hyphenated product name instead of the real clang module name. The name is now
  sanitized to a C99 identifier (`cmark-gfm` → `cmark_gfm`), and the generated
  bridge-header lookup matches the sanitized name swiftc actually emits.

- **X-005 — dependency products referenced by bare name (`byName`) were never built.**
  Macaw (`dependencies: ["SWXMLHash"]`), CocoaMQTT (`MqttCocoaAsyncSocket`, `Starscream`):
  a bare-string dep carries no package identity in `dump-package`, so discovery
  silently dropped it and shipped an umbrella whose dependency module was never built.
  Inspect now harvests bare-name deps of regular targets and resolves each against the
  DIRECT dependency packages' product inventories (unique match only; ambiguous/unknown
  left unresolved), folding a resolved name into the transitive-build set exactly like a
  `.product(name:, package:)` reference. Verified: Macaw `--dry-run-json` now reports
  `transitive_packages: ["swxmlhash"]`.

- **X-006 — mixed (C/ObjC + Swift) framework packaged without its C public headers.**
  Yams: the produced framework's `Headers/` lacked the internal C target's public
  headers (`yaml.h`), leaving the Swift umbrella header's `#import` dangling. Header
  collection now folds in a mixed framework's internal C-dependency header dirs
  (Yams's `Sources/CYaml/include`) alongside the primary target's, so `yaml.h` ships.

## X-002 — transitive dynamic-library synthesis fails for static-only products (best-effort: diagnostic delivered)

swift-crypto 4.5.1: `synth_dynamic_library` for transitive swift-asn1 cannot
build it as a dynamic iOS framework (static-only SPM product). `-p Crypto`,
`--best-effort-transitives`, and `--no-transitive-products` all documented as
failing in the corpus batch report B02. The failure now produces a diagnostic
that names the static-only-transitive cause instead of a bare build error; the
underlying build still cannot be produced from this manifest shape.

## Environmental context (not tool bugs, but affects triage)

Xcode 26.3 cannot build certain packages as library-evolution dynamic iOS
frameworks at all; these look like tool failures but aren't. The tool now scans
xcodebuild output for the signature diagnostics and prints an "upstream/toolchain
limitation, not a spm-to-xcframework bug" message distinguishing them from a
genuine conversion bug:
- swift-collections ≥ 1.5 (`@_lifetime`/`~Escapable`) — poisons anything pinning it
  (swift-nio graph, TCA/Point-Free graph, swift-async-algorithms, sqlite-data).
- `@_spi` protocol requirements without defaults (Apollo), `@inlinable` storage
  (swift-log 1.14), `@_alwaysEmitIntoClient` class inits (Defaults).
- `used before 'self.init' call` initializer-resilience walls (the swift-crypto →
  swift-asn1 static-only X-002 case).
