"""Typed model objects shared across phases.

The model is read-only after Inspect populates it. Closed enums are
implemented as classes-of-strings rather than `enum.Enum` so JSON
dumping is trivial and call sites don't pay the `.value` tax. Membership
is enforced via the `_VALUES` tuple at parse time.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


class Linkage:
    AUTOMATIC = "automatic"
    DYNAMIC = "dynamic"
    STATIC = "static"
    EXECUTABLE = "executable"  # not a library product, but parsed for completeness
    PLUGIN = "plugin"
    SNIPPET = "snippet"
    UNKNOWN = "unknown"  # the dump-package payload was a shape we don't recognize

    _VALUES = (AUTOMATIC, DYNAMIC, STATIC, EXECUTABLE, PLUGIN, SNIPPET, UNKNOWN)


class TargetKind:
    REGULAR = "regular"
    TEST = "test"
    SYSTEM = "system"
    BINARY = "binary"
    PLUGIN = "plugin"
    MACRO = "macro"
    EXECUTABLE = "executable"
    UNKNOWN = "unknown"

    _VALUES = (REGULAR, TEST, SYSTEM, BINARY, PLUGIN, MACRO, EXECUTABLE, UNKNOWN)


class Language:
    """Per-target source-language classification, derived by walking the
    target's source tree under WORK_DIR/staged."""

    SWIFT = "Swift"
    OBJC = "ObjC"
    MIXED = "Mixed"
    NA = "N/A"  # system / binary / plugin / unknown — nothing to scan

    _VALUES = (SWIFT, OBJC, MIXED, NA)


@dataclass
class Platform:
    name: str            # "ios", "macos", "tvos", ...
    version: str         # "15.0"


@dataclass
class Product:
    name: str
    linkage: str         # one of Linkage._VALUES
    targets: List[str]   # backing target names


@dataclass
class Target:
    name: str
    kind: str            # one of TargetKind._VALUES
    path: Optional[str]  # relative to package root; None means SPM default
    public_headers_path: Optional[str]
    dependencies: List[str]
    exclude: List[str]
    language: str = Language.NA  # filled in by scan_target_languages()
    source_file_count: int = 0   # diagnostics; len(describe.targets[i].sources)


@dataclass
class TransitivePackageInfo:
    """A directly-referenced external dependency package: its SPM identity
    (as used in `.product(name: X, package: identity)` from the root's
    targets), the resolved checkout path under `<staged>/.build/checkouts/`,
    and its dumped library products.

    Populated by Inspect for every package identity that any REGULAR target
    in the root package references via `.product(name:, package:)`. The
    planner reads from this pre-computed data so it stays subprocess-free
    (Plan purity contract).
    """

    identity: str                    # SPM identity, e.g. "swift-clocks"
    checkout_path: Path              # absolute, under .build/checkouts/
    products: List["Product"]        # parsed via _parse_dump on the checkout
    tools_version: str               # for active-manifest selection in Prepare
    # The exact product names the root's REGULAR targets imported from this
    # identity via `.product(name: X, package: <identity>)`. Order preserved;
    # duplicates removed. The orchestrator passes these to the child run as
    # `Config.product_filters` so the child only builds what the umbrella
    # actually consumes, and the child's manifest pruner uses them to
    # compute the closure of external `.package(url:)` deps that must be
    # kept in the pre-staged Package.swift. The orchestrator may further
    # trim this list to only those products imported by IN-CLOSURE root
    # targets before handing it to the child.
    referenced_products: List[str] = field(default_factory=list)
    # Root-package target names that reference this transitive identity via
    # `.product(name:, package:)`. The orchestrator intersects this set with
    # the user's selected-target closure (product_filters + target_filters,
    # expanded by internal dep edges) to drop transitives reached only from
    # unrelated regular helper/example targets — avoiding fail-fast aborts on
    # external dependencies that the umbrella product would never link.
    referencing_root_targets: List[str] = field(default_factory=list)
    # Per-product attribution: product name -> ordered list of root REGULAR
    # target names that reference that specific product. Populated by Inspect
    # so the orchestrator can trim `referenced_products` down to only those
    # products an IN-CLOSURE root target actually imports — without this, a
    # package referenced by two different root targets via two different
    # products would force the child to build BOTH products even if only one
    # caller is in the user's selection.
    product_to_root_targets: Dict[str, List[str]] = field(default_factory=dict)
    # True iff every (non-system) product is backed only by binaryTarget
    # targets — the transitive analogue of the umbrella's binary-only
    # auto-switch. When True AND `origin_url` + `head_tag` are populated,
    # the orchestrator routes the child through `_run_binary_mode` against
    # the upstream URL+tag instead of cloning the pre-staged checkout.
    # Without this, mapbox-maps-ios-binary's `turf-swift` and adjust-ios-
    # sdk's `adjust_signature_sdk` transitives fail Plan with a binary-only-
    # on-local-path refusal. Populated by Inspect.
    is_binary_only: bool = False
    # Names of every `.binaryTarget(...)` target this transitive declares.
    # Lets the orchestrator detect "mixed-mode package, but the umbrella
    # only references the binary side" cases that whole-package
    # `is_binary_only` misses — see `_referenced_products_are_all_binary`
    # for the Amplitude-Swift v1.18.3 example. Populated by Inspect from
    # the same target dump used to compute `is_binary_only`.
    binary_target_names: List[str] = field(default_factory=list)
    # The transitive's upstream git remote URL, recovered from `git config
    # --get remote.origin.url` on `checkout_path`. None if the checkout
    # has no `.git` dir (rare — SPM's normal `.build/checkouts/<id>/` does
    # have one) or the remote was renamed. Used by the binary-only
    # auto-switch to point Fetch at the upstream URL instead of the local
    # pre-staged dir.
    origin_url: Optional[str] = None
    # The exact tag the transitive's HEAD points at, recovered from `git
    # describe --tags --exact-match HEAD`. None if HEAD is not on a tag
    # (revision-pinned packages, branch-tracking deps). Used by the
    # binary-only auto-switch to populate Fetch's `--version <tag>`.
    head_tag: Optional[str] = None


@dataclass
class Package:
    """Typed snapshot of `swift package dump-package` for the staged
    package. Read-only after Inspect; the planner consumes it."""

    name: str
    tools_version: str
    platforms: List[Platform]
    products: List[Product]
    targets: List[Target]
    schemes: List[str]              # from xcodebuild -list -json against staged
    raw_dump: dict                  # untouched dump-package JSON, for debugging
    staged_dir: Path
    # Direct (1st-level) external dependency packages whose products the
    # root's REGULAR targets reference via `.product(name:, package:)`.
    # Populated by Inspect when the root has external product deps, empty
    # for self-contained packages. Consumed by the top-level orchestrator
    # `_run_source_mode_with_transitives` to drive in-process recursion —
    # one child `_run_source_mode` call per identity, building each
    # transitive checkout as its own xcframework alongside the umbrella.
    transitive_packages: List["TransitivePackageInfo"] = field(default_factory=list)

    def target_by_name(self, name: str) -> Optional[Target]:
        for t in self.targets:
            if t.name == name:
                return t
        return None

    def transitive_package_by_identity(
        self, identity: str
    ) -> Optional["TransitivePackageInfo"]:
        """Case-insensitive lookup: SPM normalises identities to lowercase
        in `dump-package`'s `.product` shape, but the value the manifest
        passes to `package:` is whatever the author wrote in their
        `.package(...)` declaration.
        """
        target_lower = identity.lower()
        for tp in self.transitive_packages:
            if tp.identity.lower() == target_lower:
                return tp
        return None


# --- Plan, BuildUnit, StageSpec, PackageSwiftEdit ----------------------------
#
# These are stubs for Session 2; defined here so Session 1's code already
# imports the right type names and the file structure stays stable.


@dataclass
class StageSpec:
    """Inclusion-by-default + exclusion-list staging rules. The actual
    list of toxic top-level entries lives in TOXIC_TOP_LEVEL below; this
    type exists so the planner can override per-package if it ever needs
    to. (Not used in session 1, but referenced by REWRITE_DESIGN.md §5.2.)"""

    exclude_globs: List[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {"exclude_globs": list(self.exclude_globs)}

    @classmethod
    def from_json(cls, d: dict) -> "StageSpec":
        return cls(exclude_globs=list(d.get("exclude_globs", [])))


@dataclass
class PackageSwiftEdit:
    """Whitelisted Package.swift edit, produced by Plan and consumed by
    Prepare. Three source-mode kinds (a fourth kind,
    replace_with_binary_target, is generated and applied dynamically
    inside Execute for dedup-overlap and never travels through this
    struct):

      - "synth_dynamic_library"   — add a new `.library(name: P, type:
                                    .dynamic, targets: [...])` to wrap an
                                    existing product whose source-mode
                                    linkage we want flipped to dynamic.
                                    `product_name` is the synthetic
                                    product name written into the manifest
                                    (collision-avoided by the planner;
                                    often differs from the rename target
                                    on BuildUnit.framework_name).
      - "synth_library"           — add a new `.library(name: T, type:
                                    .dynamic, targets: [T])` for the
                                    `--target T` escape hatch. By
                                    construction T is not an existing
                                    product name, so no rename is needed
                                    and `BuildUnit.scheme ==
                                    BuildUnit.framework_name`.
      - "consume_external_sibling" — consume a pre-built transitive
                                    sibling `.xcframework` from the
                                    output dir. Injects `.binaryTarget(
                                    name: P, path: ...)` into
                                    `Package.targets` via the overlay
                                    mechanism and rewrites every
                                    `.product(name: P, package: <ident>)`
                                    reference inside `.target` /
                                    `.executableTarget` / `.testTarget`
                                    dependency arrays to the bare string
                                    `"P"` so SPM resolves the dep against
                                    the injected binaryTarget instead of
                                    the now-orphaned external package.
                                    Emitted by the planner only when the
                                    orchestrator pre-populates
                                    `Config.prebuilt_sibling_xcframeworks`
                                    in the umbrella's Plan input. Carries
                                    `product_name` (external product
                                    name), `package_identity` (SPM
                                    identity used in the `.product(...)`
                                    `package:` argument), and
                                    `xcframework_path` (absolute path to
                                    the sibling xcframework; Prepare
                                    relativizes it for the manifest).

    The two synth_* kinds are implemented by Prepare as a
    `swift package add-product` subprocess invocation, then a
    round-trip dump-package validation that the new product appears
    with linkage DYNAMIC and the requested target list. The
    consume_external_sibling kind runs as a text edit followed by the
    same dump-package round-trip.
    """

    kind: str  # "synth_dynamic_library" | "synth_library" | "consume_external_sibling"
    product_name: str
    targets: List[str] = field(default_factory=list)
    # Populated only for `consume_external_sibling`:
    package_identity: Optional[str] = None
    xcframework_path: Optional[Path] = None

    def to_json(self) -> dict:
        return {
            "kind": self.kind,
            "product_name": self.product_name,
            "targets": list(self.targets),
            "package_identity": self.package_identity,
            "xcframework_path": (
                str(self.xcframework_path)
                if self.xcframework_path is not None
                else None
            ),
        }

    @classmethod
    def from_json(cls, d: dict) -> "PackageSwiftEdit":
        xcfx = d.get("xcframework_path")
        return cls(
            kind=d["kind"],
            product_name=d["product_name"],
            targets=list(d.get("targets", [])),
            package_identity=d.get("package_identity"),
            xcframework_path=Path(xcfx) if xcfx is not None else None,
        )


@dataclass
class BuildUnit:
    """One unit of work the executor will run.

    `archive_strategy` discriminates the execute path:
      - "archive"         — run `xcodebuild archive` against the scheme.
                            Source-mode default; the post-archive rename
                            handles `synth_dynamic_library` units whose
                            scheme name differs from the final framework
                            name.
      - "copy-artifact"   — binary mode: the xcframework already exists on
                            disk at `artifact_path`; Execute just copies it.

    (Pre-B there was a third "static-promote" strategy for the source path
    where xcodebuild emitted a `.a` instead of a `.framework`; that path
    was removed when the synthetic-dynamic-library product trick made it
    unnecessary. The clang `-dynamiclib` re-link still exists, but only
    inside `binary_promote.promote_binary_xcframework_static_to_dynamic`
    for vendor-shipped binary xcframeworks — it doesn't flow through the
    archive_strategy enum.)
    """

    name: str
    scheme: str
    framework_name: str
    language: str
    archive_strategy: str
    source_targets: List[str] = field(default_factory=list)
    # True iff this unit exists because of `--target T` — i.e. the plan
    # injects a synthetic `.library()` entry for it. Affects dry-run labels
    # and Verify's per-unit error messages.
    synthetic: bool = False
    # Populated only for archive_strategy == "copy-artifact". Absolute path
    # to the xcframework discovered under `.build/artifacts/` by Fetch.
    artifact_path: Optional[Path] = None
    # Macro target names this unit's transitive internal dep closure
    # reaches. Populated by Plan from `compute_internal_target_deps`.
    # Execute looks each name up in `Plan.macros` to find the pre-built
    # plugin executable path and threads
    # `-Xfrontend -load-plugin-executable -Xfrontend <path>#<name>`
    # into the unit's OTHER_SWIFT_FLAGS so swiftc can expand
    # `#externalMacro(module: name, ...)` calls.
    macro_deps: List[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "scheme": self.scheme,
            "framework_name": self.framework_name,
            "language": self.language,
            "archive_strategy": self.archive_strategy,
            "source_targets": list(self.source_targets),
            "synthetic": self.synthetic,
            "artifact_path": (
                str(self.artifact_path) if self.artifact_path is not None else None
            ),
            "macro_deps": list(self.macro_deps),
        }

    @classmethod
    def from_json(cls, d: dict) -> "BuildUnit":
        artifact = d.get("artifact_path")
        return cls(
            name=d["name"],
            scheme=d["scheme"],
            framework_name=d["framework_name"],
            language=d["language"],
            archive_strategy=d["archive_strategy"],
            source_targets=list(d.get("source_targets", [])),
            synthetic=bool(d.get("synthetic", False)),
            artifact_path=Path(artifact) if artifact is not None else None,
            macro_deps=list(d.get("macro_deps", [])),
        )


@dataclass
class MacroSupport:
    """Pre-built compiler-plugin executable for a `.macro(...)` target.

    Plan emits one MacroSupport per macro target reachable from the
    transitive internal dep closure of any planned build unit. Prepare
    materialises the executable via
    `swift build -c release --product <macro_target_name>` and fills in
    `plugin_executable_path`. Execute threads
    `-Xfrontend -load-plugin-executable -Xfrontend <path>#<macro_target_name>`
    into per-unit `OTHER_SWIFT_FLAGS` so swiftc can expand
    `#externalMacro(module: macro_target_name, ...)` calls.

    The compiler-plugin shuffle is necessary because xcodebuild's SPM
    integration doesn't propagate `.product(name:, package:)` deps
    declared on `.macro(...)` targets into the generated Xcode project —
    when archive runs, the macro target builds as an isolated Swift
    compilation that can't find swift-syntax modules and dies with
    `Unable to find module dependency: 'SwiftSyntax'`. Pre-building via
    `swift build` (which handles dep resolution natively, no Xcode-
    project layer) sidesteps the gap; the manifest-text edit strips the
    macro name from regular targets' dep arrays so xcodebuild stops
    trying to build the macro target at all.

    By construction the macro target name equals the module name a
    consumer references in `#externalMacro(module:)` — SPM macro
    targets are compiled as same-named executable plugins.
    """

    macro_target_name: str
    plugin_executable_path: Optional[Path] = None

    def to_json(self) -> dict:
        return {
            "macro_target_name": self.macro_target_name,
            "plugin_executable_path": (
                str(self.plugin_executable_path)
                if self.plugin_executable_path is not None
                else None
            ),
        }

    @classmethod
    def from_json(cls, d: dict) -> "MacroSupport":
        plugin = d.get("plugin_executable_path")
        return cls(
            macro_target_name=d["macro_target_name"],
            plugin_executable_path=Path(plugin) if plugin is not None else None,
        )


@dataclass
class BinaryArtifact:
    """A pre-built xcframework discovered via binary-mode SPM resolve.

    The planner takes a list of these (from `discover_binary_artifacts`)
    and filters by --product. Each surviving record becomes a build unit
    whose execute strategy is "copy-artifact".
    """

    product_name: str  # the xcframework name without the .xcframework suffix
    path: Path         # absolute path to the .xcframework directory


@dataclass
class Plan:
    """Output of the planner: a typed description of what the downstream
    phases will do. Pure data — no side effects, no filesystem handles
    beyond what inspect already gave us."""

    stage: StageSpec = field(default_factory=StageSpec)
    package_swift_edits: List[PackageSwiftEdit] = field(default_factory=list)
    build_units: List[BuildUnit] = field(default_factory=list)
    # Products the planner dropped, with a human-readable reason. Printed
    # by --dry-run and surfaced in the final run summary.
    skipped: List[Tuple[str, str]] = field(default_factory=list)
    # Planner diagnostics that aren't errors — surfaced to stderr by the
    # caller after planning completes. Kept as data so the planner stays
    # a pure function (§5.2 contract).
    warnings: List[str] = field(default_factory=list)
    # --include-deps flag forwarded to Execute. The planner can't enumerate
    # transitive deps ahead of time (they only exist after xcodebuild runs),
    # so this is just a gate.
    include_deps: bool = False
    # True for binary-mode plans. Execute uses this to skip xcodebuild
    # entirely. The planner is the only thing that sets it.
    binary_mode: bool = False
    # Macro target descriptors reachable from any planned build unit's
    # transitive internal dep closure. Plan populates `macro_target_name`;
    # Prepare materialises `plugin_executable_path`; Execute reads both
    # to thread `-load-plugin-executable` flags into per-unit
    # `OTHER_SWIFT_FLAGS`. See `MacroSupport` for the rationale.
    macros: List["MacroSupport"] = field(default_factory=list)

    # Bump when the JSON shape changes in a way that can't be read back by
    # the previous shape. Used to fail loudly rather than silently drift.
    JSON_SCHEMA_VERSION = 1

    def to_json(self) -> dict:
        return {
            "schema_version": self.JSON_SCHEMA_VERSION,
            "stage": self.stage.to_json(),
            "package_swift_edits": [e.to_json() for e in self.package_swift_edits],
            "build_units": [bu.to_json() for bu in self.build_units],
            "skipped": [[name, reason] for name, reason in self.skipped],
            "warnings": list(self.warnings),
            "include_deps": self.include_deps,
            "binary_mode": self.binary_mode,
            "macros": [m.to_json() for m in self.macros],
        }

    @classmethod
    def from_json(cls, d: dict) -> "Plan":
        sv = d.get("schema_version", 1)
        if sv != cls.JSON_SCHEMA_VERSION:
            raise ValueError(
                f"Plan JSON schema version {sv!r} is not supported "
                f"(expected {cls.JSON_SCHEMA_VERSION})"
            )
        return cls(
            stage=StageSpec.from_json(d.get("stage", {})),
            package_swift_edits=[
                PackageSwiftEdit.from_json(e) for e in d.get("package_swift_edits", [])
            ],
            build_units=[BuildUnit.from_json(bu) for bu in d.get("build_units", [])],
            skipped=[(name, reason) for name, reason in d.get("skipped", [])],
            warnings=list(d.get("warnings", [])),
            include_deps=bool(d.get("include_deps", False)),
            binary_mode=bool(d.get("binary_mode", False)),
            macros=[MacroSupport.from_json(m) for m in d.get("macros", [])],
        )


@dataclass
class PreparedPlan:
    """Output of Prepare. Wraps the original Plan together with the typed
    Package model that resulted from re-parsing the edited active manifest.

    Existence of a PreparedPlan is a contract: the active manifest under
    `package.staged_dir` (`Package.swift` or whichever
    `Package@swift-X.Y.swift` SPM picks for the toolchain) parses cleanly
    through `swift package dump-package` and every planner-requested edit
    appears in the dumped output the way the planner expected. Execute can
    rely on that invariant — it never re-validates the manifest itself.
    """

    plan: Plan
    package: Package


@dataclass
class ArchiveSlice:
    """One (build unit, slice) pair's outputs from xcodebuild archive.

    A "slice" is one (platform, arch-class) build, e.g. ios-arm64, the
    iOS simulator fat archive, macos, mac-catalyst. All slices for a
    unit archive in parallel via `_archive_all_parallel`; the located
    framework path is recorded here so Execute's rename and injection
    passes don't need to re-walk the archive.
    """

    arch_suffix: str              # slice id (e.g. "ios-arm64", "macos")
    sdk_name: str                 # "iphoneos" / "iphonesimulator" / "macosx"
    archive_path: Path            # .xcarchive directory
    dd_path: Path                 # derived data path
    log_path: Path                # build log
    result_bundle_path: Path      # xcresult bundle
    framework_path: Optional[Path] = None


@dataclass
class DependencyXcframework:
    """One dependency xcframework built alongside a primary unit via
    `--include-deps`.

    Pairs the on-disk path with the expected language classification
    (derived from the matching target in the Package model, if one
    exists) so that `_finalize_with_verify` can thread the same
    plan-time expected-language signal into Verify for deps that the
    primary build units already get. Without this, dependency artifacts
    would fall back to post-hoc detection and the mixed-language
    silent-pass hole would still apply to them.
    """

    path: Path
    expected_language: str = ""  # Language._VALUES; "" / "N/A" → post-hoc fallback


@dataclass
class ExecutedUnit:
    """Output of Execute for a single build unit.

    Holds the archive slices produced for this unit (one per enabled
    platform-arch pair) and the final xcframework path the merge step
    produced. `slices` is empty for binary-mode units (`copy-artifact`),
    where only `xcframework_path` is set, pointing at the copied artifact
    in `<output_dir>/`.
    """

    name: str                     # build unit name (matches plan.build_units[i].name)
    slices: List[ArchiveSlice] = field(default_factory=list)
    xcframework_path: Optional[Path] = None
    framework_name: Optional[str] = None  # the resolved <fw_name>, may differ from unit.name
    framework_type: str = ""              # "Swift" / "ObjC" / "Mixed" / "Unknown" — post-hoc detection from disk, for summary printing
    # Language the planner said this unit was supposed to produce. One of
    # Language._VALUES ("Swift" / "ObjC" / "Mixed" / "N/A"). Carried through
    # from `BuildUnit.language` so Verify can gate its language-specific
    # fatal checks on what the plan intended, not on what survived in the
    # partially-built artifact. Empty string for legacy callers; Verify
    # treats empty / "N/A" as "fall back to post-hoc detection".
    expected_language: str = ""
    # Per-(platform, variant) slice coverage the user asked for. Variants
    # are "device" / "simulator" / "maccatalyst". Verify enforces that the
    # xcframework's AvailableLibraries carries an entry for every pair —
    # critical for binary mode where we copy a vendor artifact unchanged
    # and have no other way to know whether the requested coverage is
    # actually present (e.g. an iOS-device-only artifact would otherwise
    # silently satisfy `--min-ios`). Empty list is the legacy default and
    # disables the check.
    expected_slice_classes: List[Tuple[str, str]] = field(default_factory=list)
    is_binary_copy: bool = False
    dependency_xcframeworks: List[DependencyXcframework] = field(default_factory=list)


@dataclass
class VerifyResult:
    """One xcframework's strict-verify outcome (REWRITE_DESIGN.md §5.5).

    Verify is per-unit. A unit either `passed` (every fatal check cleared)
    or it didn't (one or more entries in `fatal_issues`). `warnings` are
    advisory and never cause `passed=False`. `framework_type` is reported
    for the summary printer's `[Swift|ObjC|Mixed|Unknown]` label.

    Verify never raises for "the user's xcframework is broken" — it
    records the failure here and lets `main()` translate the aggregate
    into an exit code. The only thing that surfaces as a `VerifyError`
    exception is verify code itself crashing (e.g. unreadable output
    directory) — see REWRITE_DESIGN.md §7.
    """

    unit_name: str
    framework_name: str
    xcframework_path: Path
    framework_type: str            # "Swift" | "ObjC" | "Mixed" | "Unknown"
    size_bytes: int
    passed: bool
    fatal_issues: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


# --- TargetKind helpers ------------------------------------------------------


def _default_target_path(target_name: str, target_kind: str) -> Optional[str]:
    """SPM's default path for a target when `path:` isn't set.

    Only returns a path for kinds that actually have a buildable source
    tree under `Sources/` or `Tests/`. System / binary / plugin / macro
    targets either point at host headers, prebuilt artifacts, or compiler
    plugins, so they have no default source path and we return None.

    For regular and executable targets it's `Sources/<name>`; for tests
    it's `Tests/<name>`. We don't try to resolve the case where SPM picks
    an alternate `Source/<name>` (Alamofire) — those targets always
    declare `path:` explicitly, so dump-package returns the value verbatim.
    """
    if not target_name:
        return None
    if target_kind == TargetKind.TEST:
        return f"Tests/{target_name}"
    if target_kind in (TargetKind.REGULAR, TargetKind.EXECUTABLE):
        return f"Sources/{target_name}"
    return None
