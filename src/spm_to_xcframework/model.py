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


@dataclass
class PackageSwiftEdit:
    """Whitelisted Package.swift edit, produced by Plan and consumed by
    Prepare. Two source-mode kinds (a third kind, replace_with_binary_target,
    is generated and applied dynamically inside Execute for dedup-overlap
    and never travels through this struct):

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

    Both kinds are implemented by Prepare as a `swift package add-product`
    subprocess invocation, then a round-trip dump-package validation that
    the new product appears with linkage DYNAMIC and the requested target
    list.
    """

    kind: str  # "synth_dynamic_library" | "synth_library"
    product_name: str
    targets: List[str] = field(default_factory=list)


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
