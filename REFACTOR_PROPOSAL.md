# Architecture Refactor Proposal

Date: 2026-05-20

This document captures the output of a three-way research swarm (Claude Code, Codex CLI, Grok CLI) on whether `spm-to-xcframework` should be rewritten, ported, or left alone, and proposes a concrete, ordered work plan. It is written to be self-contained: a fresh Claude session (or human collaborator) should be able to read just this file and pick up the work.

## TL;DR

The 6-phase pipeline architecture is correct — keep it. Python is still defensible but no longer the obviously-best choice; a Swift rewrite would eliminate ~1,000–1,500 lines of accidental complexity (regex-based `Package.swift` editing, `dump-package` JSON re-parsing) but is not required. **The single highest-leverage change, language-independent, is to split the 7,744-line single file into per-phase modules.** Do that first; every other improvement becomes a clean per-module decision afterward.

Recommended order of work: **A → B → (C or D, depending on B's outcome) → E → F.** See the [Proposed Work](#proposed-work-aandashf-ordered-by-leverage) section.

**Update 2026-05-20**: Preliminary spikes for B confirmed it works empirically (see [Spike findings](#spike-findings-2026-05-20)). The synthetic dynamic-library + post-build rename pattern produces real dynamic Mach-O for both Swift and ObjC packages directly via xcodebuild, with no `clang -dynamiclib` re-link. Estimated deletion from B is now ~750 lines (not the original ~400). C may not be needed at all.

## Spike findings (2026-05-20)

Five cheap experiments run against fresh test packages to validate the B–F propositions before committing to the larger A modular split. All ran on this box: Xcode 26.3 / Swift 6.2.4.

### Spike 1: `swift package add-product --type dynamic-library` exists and works

Confirmed. The subcommand emits valid Swift syntax (`.library(name: "X", type: .dynamic, targets: [...])`), preserves existing manifest formatting and comments, and the result round-trips through `swift package dump-package` cleanly.

### Spike 2: Synthetic dynamic re-export library — works, with one wrinkle

Adding a second `.library()` with `type: .dynamic` next to an existing static library product, both pointing at the same target, **does** produce a buildable scheme that xcodebuild archives to a real `.framework` with a dynamic Mach-O. The synthetic product MUST have a different name from the original — `xcodebuild` silently drops duplicate-named products with the message *"ignoring duplicate product 'X' (dynamic library)"*. So the pattern is:

```
swift package add-product XDynamic --type dynamic-library --targets X
```

Then build the `XDynamic` scheme. The resulting `XDynamic.framework` needs a post-build rename to `X.framework` if downstream consumers expect the original module name (matters for `.NET P/Invoke` `DllImport("X")` ergonomics). Rename steps: bundle dir, executable file, `install_name_tool -id @rpath/X.framework/X`, Info.plist (`CFBundleExecutable`, `CFBundleName`, `CFBundleIdentifier`), re-codesign. Estimate: ~50 lines.

### Spike 3: `BUILD_LIBRARY_FOR_DISTRIBUTION=YES` does NOT auto-place `.swiftinterface` inside the framework

Verified on Xcode 26.3. The `.swiftinterface` files (and per-arch `.swiftmodule`) are generated correctly in DerivedData under `Build/Intermediates.noindex/ArchiveIntermediates/<scheme>/BuildProductsPath/Release-iphonesimulator/<target>.swiftmodule/` but xcodebuild's archive step does not copy them into the framework's `Modules/` directory. **`inject_swiftmodule` (line 4491) is NOT vestigial; it remains essential.**

### Spike 4: The synthetic dynamic product trick eliminates `promote_static_to_framework` for SPM packages

Big finding. Tested against an ObjC-only test package (`.m` + `.h` + Foundation only, the kind that previously fell through to the `clang -dynamiclib` re-link). The synthetic `.library(type: .dynamic, targets: [...])` plus `xcodebuild archive -scheme ObjcPkgDynamic` produced a real dynamic Mach-O universal binary directly — no `MACH_O_TYPE` override needed, no re-link needed:

```
ObjcPkgDynamic: Mach-O universal binary with 2 architectures: 
  [x86_64: Mach-O 64-bit dynamically linked shared library x86_64]
  [arm64:  Mach-O 64-bit dynamically linked shared library arm64]
LC_ID_DYLIB: @rpath/ObjcPkgDynamic.framework/ObjcPkgDynamic
```

So **`promote_static_to_framework` (line 4279, ~200 lines) is largely replaceable** when the source is an SPM package. The function may still be needed for the static-archive-inside-vendor-xcframework case (Kidoz 10.1.5 promotion via `promote_binary_xcframework_static_to_dynamic` at line 6277) — that path is independent.

`MACH_O_TYPE=mh_dylib` itself was never tested in isolation because the synthetic-product approach made it unnecessary.

### Spike 5: `swift package describe --type json` is more useful than `dump-package` for Phase 1

`describe` returns the SPM-resolved view: target `module_type` (`ClangTarget` / `SwiftTarget` / `MixedLanguageTarget`), resolved `path`, `product_memberships` (reverse-mapping target → containing products), and explicit `sources: [...]` lists. This directly answers what `scan_target_languages` (line 1317) and `_count_source_files` (line 1365) currently compute by hand — ~150 lines of manual source-tree walking. `dump-package` is still useful for traits / pkgConfig / `swiftLanguageVersions` that `describe` doesn't expose, so we'd use both.

### End-to-end demo

Built an ObjC SPM package → synthetic dynamic product → archive device + simulator → `xcodebuild -create-xcframework` directly from the two `.framework` bundles → produced a valid `.xcframework` with dynamic Mach-O slices and a correct `Info.plist`. **No regex Package.swift surgery, no `clang -dynamiclib`, no manual swiftmodule placement was required for the dynamic-Mach-O part.** The injection passes (`inject_objc_headers`, `inject_swiftmodule`, modulemap generation) are still needed to populate the framework's ABI surface for non-Xcode consumers — that complexity is essential and survives.

### Revised LOC delta from B

| Function | Lines today | After B | Delta |
|---|---|---|---|
| `edit_force_dynamic` + supporting regex/balanced-paren walker | ~400 | 0 | −400 |
| `promote_static_to_framework` (SPM case) | ~200 | 0 | −200 |
| `_count_source_files` + `scan_target_languages` | ~150 | 0 | −150 |
| New: `swift package add-product` orchestration + framework rename helper | 0 | ~80 | +80 |
| **Net** |  |  | **~−670 lines** |

Plus the `.swiftinterface` extraction in Inspect (`describe --type json` parsing replaces the ad-hoc source walk) shrinks Phase 1 by another ~100 lines if we go all-in on the new subcommand.

### Open risks for B (test before committing)

- Packages with `linkerSettings` / `unsafeFlags` / custom build settings — does the synthetic library inherit them through the target?
- Packages with multiple targets per product — does the synthetic library work when re-exporting a multi-target product?
- Mixed-language targets — already known broken in SPM ("feature not supported"), not B's problem.
- The post-build rename + re-codesign pattern needs to be tested against the existing `verify_output` contract.

These are testable inside the existing `src/spm_to_xcframework_tests.py` framework. None observed as blockers in the spike.

### Conclusion

Order of work remains **A → B → …** but with confidence: B is no longer hypothetical, and the deletion is bigger than originally estimated. C (SwiftSyntax helper) is probably unnecessary once B lands. D, E, F unchanged.

## Context for a fresh session

### What this tool does

`spm-to-xcframework` is a Python 3 CLI that takes a Swift Package Manager URL or local path and produces distribution-grade XCFrameworks. The primary downstream is **.NET binding generation** via the sibling tool [swift-bindings](https://github.com/justinwojo/swift-bindings) — the bindings README explicitly says "SPM → XCFramework is out of scope, use spm-to-xcframework". Secondary use case is generic "any SPM package → XCFramework" for any other consumer.

The pipeline has six explicit phases declared in the script header:

```
Fetch → Inspect → Plan → Prepare → Execute → Verify
```

- **Fetch**: clone/copy + stage a clean source tree (prune `.git`, `.build`, sibling `.xcodeproj`/`.xcworkspace`)
- **Inspect**: parse `swift package dump-package` JSON once into a typed `Package` model
- **Plan**: filters + decide build units + whitelist `Package.swift` edits (pure logic, no I/O)
- **Prepare**: apply edits to `Package.swift`, then re-run `dump-package` to validate the manifest still parses to the planned shape
- **Execute**: `xcodebuild archive` per slice (device + simulator in parallel), then post-process — inject `.swiftmodule`/`.swiftinterface`, ObjC headers, generated modulemaps, system-clang modules, bridge clang modules, resource bundles; promote static `.a` to dynamic `.framework`; stitch with `xcodebuild -create-xcframework`
- **Verify**: strict per-unit. Every output must ship ≥2 dynamically-linked Mach-O slices with the expected ABI surface (Swift interface, ObjC headers + modulemap, or both for mixed)

### Why it exists

Apple does not support producing XCFrameworks directly from Swift Packages. Apple developer engineer in [forum thread 650163](https://developer.apple.com/forums/thread/650163): *"While creating XCFrameworks from Swift packages may work in some limited cases, we don't support it today."* SwiftPM's `swift build` / `swift package` provide no XCFramework emission. `xcodebuild -create-xcframework` only packages already-built inputs. The bulk of this tool's code is the gap between "what SPM + xcodebuild produce for Xcode app consumption" and "what a non-Swift consumer (.NET P/Invoke, clang AST tools, etc.) requires from an arbitrary public SPM package."

### Codebase shape

- `/spm-to-xcframework` — 7,744-line single Python 3 file at repo root (the executable). stdlib-only.
- `/src/spm_to_xcframework.py` — identical copy, used for development.
- `/src/spm_to_xcframework_tests.py` — 8,225-line test suite.
- `/src/build_single_file.py` — stitches the single-file distributable (currently a byte-identical copy).

### Phase line counts

| Phase | Start | End | Lines |
|---|---|---|---|
| Logging / errors / config / model | 1 | 580 | 580 |
| 0 Fetch | 581 | 1,108 | **528** |
| 1 Inspect | 1,109 | 1,490 | **382** |
| 2 Plan | 1,491 | 2,213 | **723** |
| 3 Prepare | 2,214 | 3,381 | **1,168** |
| 4 Execute | 3,382 | 6,560 | **3,179** |
| Output manifest hygiene | 6,561 | 6,771 | 211 |
| 5 Verify | 6,772 | 7,240 | **469** |
| CLI | 7,241 | 7,744 | 504 |

Execute alone is ~41% of the file. Prepare is ~15%. Together they account for >55% of the code.

## Investigation summary (May 2026)

### Question asked

Open-ended architecture consultation: is Python still the right language, is the architecture still the right architecture, what existing tools (Scipio, swift-create-xcframework, Tuist, Bazel rules_apple, Carthage, others) could replace part or all of the custom logic, how much of the 7,700 lines is essential complexity vs. accidental complexity, and what alternative architectures rank best for the .NET bindings primary downstream.

The prompt was carefully written to avoid leading questions — both Codex and Grok were asked to come back with independent assessments.

### Sources consulted

- **Codex CLI** (research mode) — full repo access, ran `xcodebuild`/`swift package` directly on this machine, web search enabled. Codex session id: `019e45fe-d559-7e51-bdad-4cfcd5ebf9cb`. Full transcript was at `/private/tmp/codex-review-spm-to-xcframework-20260520-102504-r1.md` (ephemeral).
- **Grok CLI** (research mode) — same access. Grok session id: `019e45fe-c22a-7cd1-acfb-cc3a92af52e9`. Full transcript was at `/private/tmp/grok-cli-review-spm-to-xcframework-20260520-102504-r1.md` (ephemeral).
- **Claude direct research** — WebSearch / WebFetch (Scipio, swift-create-xcframework, Tuist, Carthage, SwiftSyntax, Apple developer forums, SE-0482), direct code reading, direct testing of `xcodebuild -create-xcframework -help`, `swift package --help` on the current installed Xcode 26.3 / Swift 6.2.4.

### Verdict from all three

All three independently converged on the same conclusions:

1. **The 6-phase pipeline architecture is correct.** Don't redesign it.
2. **The reconstruction logic in Phase 4 Execute is predominantly essential complexity**, not accidental. SPM/xcodebuild does not emit distribution-ready dynamic `.framework` bundles for arbitrary packages; someone has to finish the job.
3. **Apple has no first-party path** for SPM → XCFramework and no public roadmap to add one.
4. **Scipio (giginet/Scipio)** is the closest active alternative but serves a different use case (iOS app dep caching) and does not implement the static-to-dynamic promotion, bridge clang module shim walking, or strict dynamic-Mach-O verify that this tool's primary downstream requires.
5. **Don't rewrite just to change languages.** The essential complexity floor is what it is; a rewrite re-creates it under a different name.

Where the reviewers diverged slightly:

- **Codex** was more open to using Scipio as a build substrate with a thin custom repair/verify layer on top (would require a spike to validate).
- **Grok** was more skeptical of any Scipio-based path, citing Scipio's open issues showing the same ObjC/header gaps the current tool already solves.
- **Codex** called the single-file Python constraint "actively harmful." **Grok** was more diplomatic but agreed it was accidental tax.

User decision (recorded 2026-05-20): not switching to Scipio. Keeping our own tool, prefers the control. Focus is on the existing design and what to improve within it.

## Key findings

### Essential vs accidental complexity, by phase

**Mostly essential complexity (don't expect to delete it):**

- **Execute / injection passes** (~1,300 lines): `promote_static_to_framework` (4279), `inject_swiftmodule` (4491), `inject_objc_headers` (4968), `inject_system_clang_modules` (4844), `inject_bridge_clang_modules` (5212), `inject_resource_bundles` (5399), `promote_binary_xcframework_static_to_dynamic` (6277), `_check_binary_dynamic` (6790). Each pass exists because a real-world SPM package required it. Removing any of them regresses the primary downstream.
- **Verify** (469 lines): strict contract enforcement. A static slice silently produces a broken NuGet downstream. The verify gate is the product.
- **Fetch** (528 lines): git handling, revision verification, source staging. All necessary.
- **Plan** core logic (~400 of the 723 lines): build unit topology, system-only / binary-only / synthetic library detection, dedup-overlap rewriting.

**Mostly accidental complexity (genuine deletion targets):**

- **Prepare / manifest surgery** (~700 of the 1,168 lines): hand-rolled Swift comment stripper, balanced-paren walker, regex-based `.library()` call finder. This is the largest accidental-complexity wedge. SwiftSyntax (Swift), or possibly `swift package add-product` (CLI), would replace it.
- **Inspect / dump-package re-parsing** (~200 of the 382 lines): turning JSON back into typed dataclasses that libSwiftPM already exposes natively.
- **Plan duplication of package model** (~300 of the 723 lines): re-implementing semantics SwiftPM already implements.
- **Single-file 7,744-line layout**: no LOC reduction, but it prevents per-phase isolation and makes the essential complexity harder to test and read.

### Python vs Swift

**Honest read**: Python is no longer the *obviously-best* choice but is still defensible.

- **Python costs**: ~1,000–1,500 lines of accidental complexity from regex-based manifest editing and JSON re-parsing of `dump-package`. Single-file constraint enforces a flat layout.
- **Swift wins**: libSwiftPM gives typed package models directly; SwiftSyntax gives AST-level manifest editing.
- **Swift costs**: lose Python's fast edit-run iteration; lose the existing 8,225-line test suite; install / distribution story is heavier (need a compiled binary or a SPM package the user has to build).
- **Wash**: Xcode is already a hard runtime dependency either way; "no toolchain dep" is not a Python advantage in practice. Subprocess orchestration around `xcodebuild`/`clang`/`lipo` is the same in any language.

If greenfielding in 2026 with these goals, Swift would probably win. Given the current state and a working test suite, switching languages now is a high-risk move for a moderate LOC win. **Recommendation: stay in Python for the foreseeable future; revisit only after the modular split (A) is done.**

### Why Scipio doesn't change the answer

Scipio (giginet/Scipio, v0.32.1 Mar 2026, Swift 6.0–6.3, uses libSwiftPM + xcodebuild + caching) is purpose-built for **iOS app teams caching their SPM dependencies as XCFrameworks for faster app builds.** Different center of gravity:

- Scipio defaults to library-evolution **off** (causes breakage on swift-collections / swift-nio). This tool requires it **on** for `.swiftinterface` emission.
- Scipio does not have explicit static-to-dynamic promotion for ObjC-only products. .NET P/Invoke cannot consume static `.a`.
- Scipio does not walk project-shipped `module.modulemap` files to materialize bridge clang module shims (the `WCDB_Private` case). Consumers rebuilding `.swiftinterface` with a different swiftc need those modules resolvable inside the xcframework.
- Scipio does not enforce a "every slice must be dynamic Mach-O" verify contract.
- Scipio requires a `Package.swift` you authored (no "feed it a URL" UX, no version pinning + SHA verification).

For the primary downstream (.NET bindings), Scipio's gaps are not cosmetic — they're load-bearing requirements this tool exists to solve.

## Proposed work (A–F, ordered by leverage)

### A. Modularize the single file. DO THIS FIRST.

The single highest-leverage change. Zero behavior change, zero migration risk. Same code, same tests, dramatically better navigability.

Target layout:

```
src/spm_to_xcframework/
  __init__.py
  __main__.py          # entry point — `python -m spm_to_xcframework`
  cli.py               (~500 lines, currently lines 7241–7744)
  config.py            (~70 lines, currently lines 199–266)
  errors.py            (~95 lines, currently lines 107–197)
  log.py               (~45 lines, currently lines 42–106)
  model.py             (~320 lines, currently lines 266–580)
  fetch.py             (~530 lines, currently Phase 0)
  inspect.py           (~380 lines, currently Phase 1)
  plan.py              (~720 lines, currently Phase 2)
  prepare.py           (~1,170 lines, currently Phase 3)
  execute/
    __init__.py
    archive.py         # xcodebuild driver, parallel slice runner
    promote.py         # promote_static_to_framework
    inject_swiftmodule.py
    inject_objc.py     # inject_objc_headers + modulemap generation
    inject_clang_system.py
    inject_clang_bridge.py
    inject_resources.py
    create_xcframework.py
    binary_promote.py  # promote_binary_xcframework_static_to_dynamic
    dedup.py           # _compute_dedup_substitutions
    run_unit.py        # _run_one_unit orchestrator
  output_manifest.py   (~210 lines)
  verify.py            (~470 lines, currently Phase 5)
```

`/spm-to-xcframework` (the executable at repo root) becomes a thin wrapper: `import sys; from src.spm_to_xcframework.cli import main; sys.exit(main(sys.argv[1:]))`. Or — since the repo ships `build_single_file.py` for a reason — keep stitching the modules into a single distributable file at build time.

Migration approach: per-phase mechanical move. Start with the leaves (logging, errors, config, model dataclasses), then inspect, then fetch, then verify, then plan, then prepare, then execute (move the injection passes one at a time). Tests stay green throughout because nothing changes except import paths.

Update `src/build_single_file.py` to concatenate the modules in dependency order (it's currently a byte-copy because there's only one file).

Expected outcome: ~20 files averaging ~400 lines. No code change, no behavior change, no test change beyond import statements.

### B. Use `swift package add-product` for Phase 3 manifest edits. **Spike validated 2026-05-20.**

`swift package` on Xcode 26.3 / Swift 6.2.4 exposes:

- `swift package add-product` ← confirmed working with `--type dynamic-library`
- `swift package add-target`
- `swift package add-target-dependency`
- `swift package add-setting`
- `swift package add-dependency`

Current Phase 3 edits (whitelist in `Plan.package_swift_edits`):

1. `edit_force_dynamic` — flip an existing `.library()` product's `type` to `.dynamic`. **Replaced by**: `swift package add-product XDynamic --type dynamic-library --targets X` (synthetic re-export, build the new scheme, post-build rename `XDynamic.framework` → `X.framework`). See [Spike findings](#spike-findings-2026-05-20). ~400 lines deleted.
2. `edit_add_synthetic_library` — wrap a `--target Foo` into a synthetic `.library(name: "Foo", targets: ["Foo"])`. **Trivially replaced by** `swift package add-product Foo --type library --targets Foo`. Estimated ~150 lines deleted.
3. `edit_replace_with_binary_target` — replace a `.target()` with a `.binaryTarget(path: "…")` for dedup-overlap (Stripe, etc.). **No first-party subcommand** (`swift package add-binary-target` doesn't exist). Still requires surgery, but the scope is narrowed to one edit type and the existing balanced-paren walker can be reused. ~300 lines stays.

Plus a free win on Phase 1: `swift package describe --type json` exposes `module_type` and `sources` per target, replacing `scan_target_languages` (line 1317) + `_count_source_files` (line 1365). ~150 lines deleted.

Net deletion: **~700 lines across Phase 1 and Phase 3.** New code: ~80 lines of `add-product` orchestration + framework rename helper.

Implementation sketch:

```
# Plan emits a typed sequence of operations:
edits = [
    SynthDynamicLibrary(product_name="MBProgressHUD", target_names=["MBProgressHUD"]),
    SynthLibrary(product_name="MyTarget", target_names=["MyTarget"]),     # --target mode
    ReplaceWithBinaryTarget(...),                                          # still surgery
]

# Prepare applies them:
for e in edits:
    if isinstance(e, SynthDynamicLibrary):
        run(["swift", "package", "add-product", f"{e.product_name}Dynamic",
             "--type", "dynamic-library", "--targets", *e.target_names])
    elif isinstance(e, SynthLibrary):
        run(["swift", "package", "add-product", e.product_name,
             "--type", "library", "--targets", *e.target_names])
    elif isinstance(e, ReplaceWithBinaryTarget):
        apply_binary_target_surgery(...)  # existing logic, narrower scope

# Execute targets the synthetic scheme:
xcodebuild_archive(scheme=f"{product}Dynamic", ...)

# Post-build, rename to drop "Dynamic" suffix:
rename_framework(archive_path, from_=f"{product}Dynamic", to=product)
```

### C. Tiny SwiftSyntax helper binary for Prepare. *Probably not needed after B.*

The original rationale: replace the regex/balanced-paren walker with a ~200-line Swift binary using [SwiftSyntax](https://github.com/swiftlang/swift-syntax) called from Python via subprocess.

After B's spike: most of Phase 3's Python surgery is replaced by `swift package add-product` invocations, not by AST manipulation. The only edit that still requires manual manipulation is `edit_replace_with_binary_target` (~300 lines, dedup-overlap case). If that one edit is the only remaining customer for AST surgery, a SwiftSyntax binary is overkill — the existing balanced-paren walker is fine for one edit type.

Defer C indefinitely. Revisit only if `edit_replace_with_binary_target` proves brittle in maintenance.

### D. Extract the injection passes into a separately-importable library.

The Execute phase's injection passes (`inject_swiftmodule`, `inject_objc_headers`, `inject_system_clang_modules`, `inject_bridge_clang_modules`, `inject_resources`, `promote_static_to_framework`, `promote_binary_xcframework_static_to_dynamic`) are self-contained operations: *"given a `.framework` path and some inputs, mutate it."*

After A (modular split), promote `src/spm_to_xcframework/execute/` into a real package with its own public API. Two consumers:

1. The orchestrator (this tool's CLI).
2. Anyone else who has a `.framework` from some other source and needs to finish it for redistribution (vendored framework reshipping, etc.).

Low priority — only worth doing if there's a real second consumer or if testing the passes in isolation gets meaningfully cleaner. After A is done it might be enough.

### E. Content-addressed build cache.

Currently every run is a fresh `swift package resolve` + `xcodebuild archive`. Cache key: hash of `(Package.resolved SHA, xcodebuild settings, tool version, requested platforms, requested products)`. On a hit, copy the cached `.xcframework` to the output dir and skip Execute entirely.

Free win for iterating on consumers (CI runs, repeated binding regeneration). Maybe ~150 lines including hash computation + cache directory hygiene. Should sit at the boundary between Plan and Execute.

### F. Serializable Plan.

`Plan` is already a typed dataclass tree. Make it `.to_json()` / `.from_json()` so you can:

1. Run Fetch + Inspect + Plan only, dump the plan as JSON, inspect/tweak it for a weird package, then run Execute against the saved plan.
2. Replay a build deterministically.
3. Make `--dry-run` output something a human can audit and a machine can diff.

Major debugging unlock for long-tail packages. Probably ~100 lines plus serialization tests.

## Explicitly ruled out

- **Full Swift rewrite as Step 1** — too risky given the downstream is already running on this tool. Revisit only after A.
- **Migrating to Scipio** — different use case, missing the load-bearing reconstruction passes. (User confirmed: keeping our own tool.)
- **Bazel / Tuist** — wrong ecosystem fit for a portable "feed it a URL" CLI.
- **Coercing `xcodebuild` to do the post-processing** — Apple has confirmed they don't support this and three independent investigations found no flag combination that eliminates the need for the injection passes.
- **Rewriting just to reduce LOC** — the essential complexity floor is real. A rewrite recreates it.

## Open questions / suggested spikes

1. **Does `swift package add-product` actually cover Phase 3 edits 1 and 2?** (B above — 1-hour spike, would unlock A's biggest deletion.)
2. **What is the minimum viable SwiftSyntax helper?** Spike a single-edit version (force `.library()` to dynamic) before committing to C.
3. **What does our test suite cover today?** Before A, audit `src/spm_to_xcframework_tests.py` (8,225 lines) for coverage on each phase and each injection pass. The modular split is safer if we know which passes already have isolated tests.
4. **Do any of the open `.md` issue docs in this repo (`MACOS_SLICE_BUG.md`, `MULTI_PLATFORM_DESIGN.md`, etc., visible in `git status` at session start but later deleted) inform the priority order?** They may be in a recent stash or untracked elsewhere; check if any are relevant before starting work.

## References

### Code locations

- Main executable: `/spm-to-xcframework` (7,744 lines)
- Source: `/src/spm_to_xcframework.py` (identical)
- Tests: `/src/spm_to_xcframework_tests.py` (8,225 lines)
- Build: `/src/build_single_file.py`
- Test data: `/testdata/` (currently just `MiniMixed/`)

### Function landmarks (line numbers in current file)

- `fetch_source` → 789
- `inspect_package` → 1431
- `plan_source_build` → 1815
- `apply_package_swift_edits` → 3085
- `edit_force_dynamic` → 2584
- `validate_prepared_manifest` → 3149
- `run_xcodebuild_archive` → 3451
- `promote_static_to_framework` → 4279
- `inject_swiftmodule` → 4491
- `inject_objc_headers` → 4968
- `inject_system_clang_modules` → 4844
- `inject_bridge_clang_modules` → 5212
- `inject_resource_bundles` → 5399
- `create_xcframework` → 5449
- `_run_one_unit` → 5786
- `promote_binary_xcframework_static_to_dynamic` → 6277
- `_verify_one_unit` → 6859
- `main` → 7360

### External tools researched

- [giginet/Scipio](https://github.com/giginet/Scipio) — active alternative, different center of gravity (iOS app dep caching)
- [segment-integrations/swift-create-xcframework](https://github.com/segment-integrations/swift-create-xcframework) — simpler wrapper, no ObjC/static promotion
- [unsignedapps/swift-create-xcframework](https://github.com/unsignedapps/swift-create-xcframework) — archived April 2024
- [Tuist](https://tuist.dev) — module cache, project generator, not packaging
- [Bazel rules_apple](https://github.com/bazelbuild/rules_apple) — wrong ecosystem fit
- [Carthage](https://github.com/Carthage/Carthage) — `--use-xcframeworks` exists, but Xcode-project oriented, not SPM
- [SwiftSyntax](https://github.com/swiftlang/swift-syntax) — would replace Phase 3 surgery

### First-party CLI surface (Xcode 26.3 / Swift 6.2.4, May 2026)

- `xcodebuild -create-xcframework` accepts `-archive`, `-framework`, `-library`, `-headers`, `-debug-symbols`, `-allow-internal-distribution`. Packages already-built inputs only.
- `swift package` subcommands: `add-dependency`, `add-product`, `add-target`, `add-target-dependency`, `add-setting`, `dump-package`, `describe`, `resolve`, `compute-checksum`, `archive-source`, `experimental-audit-binary-artifact`, `migrate`. None emit XCFrameworks.
- `swift build --build-system <native|swiftbuild|xcode>` — different build engines but none change the fundamental emission contract.
- No native SPM → XCFramework verb on the roadmap (SwiftPM issue #7035 still open).

### Authoritative external statements

- Apple developer-tools engineer in [Apple Developer Forums thread 650163](https://developer.apple.com/forums/thread/650163): *"While creating XCFrameworks from Swift packages may work in some limited cases, we don't support it today."*
- [Apple — Distributing binary frameworks as Swift packages](https://developer.apple.com/documentation/xcode/distributing-binary-frameworks-as-swift-packages) — canonical path is "archive each framework, then `xcodebuild -create-xcframework`," which is what this tool already does.

### Downstream

- [swift-bindings](https://github.com/justinwojo/swift-bindings) at `/Users/wojo/Dev/swift-bindings`. README states: *"Swift Bindings requires a compiled `.xcframework` as input… This is intentionally out of scope for Swift Bindings… I maintain a standalone script for this: spm-to-xcframework."*

The hard contract this tool must satisfy for that consumer:

- Every slice's binary must be a dynamic Mach-O (verified by `_check_binary_dynamic` via `file`).
- Swift products must ship usable `.swiftinterface` (requires `BUILD_LIBRARY_FOR_DISTRIBUTION=YES` and the swiftmodule injection pass).
- ObjC / mixed products must ship `Headers/` + `Modules/module.modulemap` consumable by `clang -ast-dump=json`.
- Bridge clang modules referenced from `.swiftinterface` must be resolvable inside the xcframework (the WCDB_Private case).
