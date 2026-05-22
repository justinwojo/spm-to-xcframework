#!/usr/bin/env python3
"""Developer self-test suite for `spm_to_xcframework`.

Lives alongside the source module in `src/`. Runs all the fast unit
tests (`python3 src/spm_to_xcframework_tests.py --fast`) or the full
suite including the swift-toolchain integration tests
(`python3 src/spm_to_xcframework_tests.py`).

End users never ran the old `--self-test` flag, so it's gone from the
CLI entirely. Tests are a developer-only tool and live here.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

# Import the tool module as `tool` so tests can monkey-patch module-level
# state (notably `_check_binary_dynamic` and `_swift_toolchain_version`)
# without reaching into `sys.modules`. The bulk wildcard import below
# pulls in every public + semi-private symbol the tests reference, so
# test bodies read the same as they did in the old monolithic file.
import spm_to_xcframework as tool
from spm_to_xcframework import *  # noqa: F401,F403 — test convenience
# Explicit imports of underscore-prefixed helpers the tests reach into.
# `from spm_to_xcframework import *` skips these, so we name them here.
from spm_to_xcframework import (  # noqa: F401
    _apply_dedup_overlap_substitutions,
    _apply_phantom_helper_dep_augmentation,
    _compute_phantom_helper_deps,
    _archive_framework_path,
    _assert_no_unsupported_swift_constructs,
    _balanced_close,
    _BUG_CLASS_ERRORS,
    _check_binary_dynamic,
    _compute_dedup_substitutions,
    _default_target_path,
    _derive_package_label,
    _ensure_root_symlink,
    _find_objc_headers_dir,
    _find_project_modulemap_for_module,
    _find_resource_bundles,
    _framework_content_root,
    _finalize_with_verify,
    _format_size_iec,
    _is_binary_only_product,
    _is_toxic_entry,
    _modules_declared_in_modulemap,
    _package_is_binary_only,
    _parse_dependencies,
    _parse_dump,
    _parse_linkage,
    _parse_target_kind,
    _parse_xcresult_build_results,
    _phase_label_for,
    _pick_primary_framework_in_slice,
    _promote_modulemap_to_framework_form,
    _read_output_manifest,
    _read_xcframework_library_paths,
    _select_active_manifest,
    _selected_slices,
    _enabled_platforms,
    _autodetect_min_versions,
    _spm_platform_entries,
    _platform_from_library_identifier,
    _variant_from_library_identifier,
    _variant_for_platform_slice,
    _expected_slice_classes,
    _validate_requested_platforms,
    _PLATFORM_SLICES,
    _PLATFORM_ORDER,
    PlatformSlice,
    _slice_paths,
    _swift_toolchain_version,
    _system_target_source_dir,
    _USER_FACING_ERRORS,
    _validate_git_ref,
    _validate_package_source,
    _verify_one_unit,
    _walk_system_library_target_deps,
)

# Logging helpers used by the harness.
from spm_to_xcframework import bold, dim, success, _wrap


# --- Snapshot fixtures ----------------------------------------------------
#
# These are minimised snapshots of `swift package dump-package` for real
# packages, embedded as Python literals so the fast self-test can run
# without invoking swift. Schema captured against Xcode 26.2; the fields
# the parser cares about are stable across recent SPM versions.

NUKE_DUMP_SNAPSHOT: dict = {
    "name": "Nuke",
    "toolsVersion": {"_version": "5.6.0"},
    "platforms": [
        {"options": [], "platformName": "ios", "version": "13.0"},
        {"options": [], "platformName": "macos", "version": "10.15"},
    ],
    "products": [
        {"name": "Nuke", "type": {"library": ["automatic"]}, "targets": ["Nuke"]},
        {"name": "NukeUI", "type": {"library": ["automatic"]}, "targets": ["NukeUI"]},
    ],
    "targets": [
        {"name": "Nuke", "type": "regular", "path": None, "publicHeadersPath": None, "dependencies": []},
        {"name": "NukeUI", "type": "regular", "path": None, "publicHeadersPath": None, "dependencies": [{"byName": ["Nuke", None]}]},
    ],
}

GRDB_DUMP_SNAPSHOT: dict = {
    "name": "GRDB",
    "toolsVersion": {"_version": "6.1.0"},
    "platforms": [
        {"options": [], "platformName": "ios", "version": "13.0"},
    ],
    "products": [
        {"name": "GRDBSQLite", "type": {"library": ["automatic"]}, "targets": ["GRDBSQLite"]},
        {"name": "GRDB", "type": {"library": ["automatic"]}, "targets": ["GRDB"]},
        {"name": "GRDB-dynamic", "type": {"library": ["dynamic"]}, "targets": ["GRDB"]},
    ],
    "targets": [
        {"name": "GRDBSQLite", "type": "system", "path": None, "publicHeadersPath": None, "dependencies": []},
        {"name": "GRDB", "type": "regular", "path": "GRDB", "publicHeadersPath": None, "dependencies": [{"target": ["GRDBSQLite", None]}]},
        {"name": "GRDBTests", "type": "test", "path": "Tests", "publicHeadersPath": None, "dependencies": []},
    ],
}

# Stripe's shape exercises the --target escape hatch: the `Stripe` library
# product is the only exposed product in this slice of the real dump, and
# the other 11 frameworks (StripeCore, StripeUICore, …) are internal
# targets that downstream consumers reach via --target. Only the subset
# used by the planner tests is included here — the real Stripe package
# has a dozen more targets.
STRIPE_DUMP_SNAPSHOT: dict = {
    "name": "stripe-ios",
    "toolsVersion": {"_version": "5.7.0"},
    "platforms": [
        {"options": [], "platformName": "ios", "version": "13.0"},
    ],
    "products": [
        {"name": "Stripe", "type": {"library": ["automatic"]}, "targets": ["Stripe"]},
        {"name": "StripePayments", "type": {"library": ["automatic"]}, "targets": ["StripePayments"]},
        {"name": "StripePaymentSheet", "type": {"library": ["automatic"]}, "targets": ["StripePaymentSheet"]},
    ],
    "targets": [
        {"name": "Stripe", "type": "regular", "path": "Stripe/StripeiOS", "publicHeadersPath": None,
         "dependencies": [{"byName": ["StripeCore", None]}, {"byName": ["StripePayments", None]}, {"byName": ["StripeApplePay", None]}]},
        {"name": "StripeCore", "type": "regular", "path": "StripeCore/StripeCore", "publicHeadersPath": None,
         "dependencies": []},
        {"name": "StripeUICore", "type": "regular", "path": "StripeUICore/StripeUICore", "publicHeadersPath": None,
         "dependencies": [{"byName": ["StripeCore", None]}]},
        {"name": "StripePayments", "type": "regular", "path": "StripePayments/StripePayments", "publicHeadersPath": None,
         "dependencies": [{"byName": ["StripeCore", None]}]},
        {"name": "StripePaymentSheet", "type": "regular", "path": "StripePaymentSheet/StripePaymentSheet",
         "publicHeadersPath": None,
         "dependencies": [{"byName": ["StripeCore", None]}, {"byName": ["StripeUICore", None]}, {"byName": ["StripePayments", None]}]},
        # A binary target — confirms the planner refuses to synthesize
        # a library for it.
        {"name": "Stripe3DS2", "type": "binary", "path": None, "publicHeadersPath": None, "dependencies": []},
    ],
}

# Alamofire's interesting shape is "automatic + already-dynamic on the same
# target", which session 2's planner needs to handle without double-patching.
ALAMOFIRE_DUMP_SNAPSHOT: dict = {
    "name": "Alamofire",
    "toolsVersion": {"_version": "5.3.0"},
    "platforms": [
        {"options": [], "platformName": "ios", "version": "13.0"},
    ],
    "products": [
        {"name": "Alamofire", "type": {"library": ["automatic"]}, "targets": ["Alamofire"]},
        {"name": "AlamofireDynamic", "type": {"library": ["dynamic"]}, "targets": ["Alamofire"]},
    ],
    "targets": [
        {"name": "Alamofire", "type": "regular", "path": "Source", "publicHeadersPath": None, "dependencies": []},
        {"name": "AlamofireTests", "type": "test", "path": "Tests", "publicHeadersPath": None, "dependencies": []},
    ],
}


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _selftest_parse_nuke() -> None:
    raw, products, targets, platforms, name, tv = _parse_dump(NUKE_DUMP_SNAPSHOT)
    _assert(name == "Nuke", f"name was {name!r}")
    _assert(tv == "5.6.0", f"tools_version was {tv!r}")
    _assert(len(platforms) == 2, f"platforms count was {len(platforms)}")
    _assert(platforms[0].name == "ios", f"first platform was {platforms[0].name!r}")
    _assert(len(products) == 2, f"product count was {len(products)}")
    _assert(products[0].name == "Nuke", f"first product was {products[0].name!r}")
    _assert(products[0].linkage == Linkage.AUTOMATIC, f"linkage was {products[0].linkage!r}")
    _assert(len(targets) == 2, f"target count was {len(targets)}")
    _assert(targets[0].kind == TargetKind.REGULAR, f"target kind was {targets[0].kind!r}")


def _selftest_parse_grdb_skips_systems_correctly() -> None:
    raw, products, targets, platforms, name, tv = _parse_dump(GRDB_DUMP_SNAPSHOT)
    _assert(name == "GRDB", f"name was {name!r}")
    # Three products: GRDBSQLite (system), GRDB (automatic), GRDB-dynamic (dynamic)
    _assert(len(products) == 3, f"expected 3 products, got {len(products)}")
    by_name = {p.name: p for p in products}
    _assert(by_name["GRDBSQLite"].linkage == Linkage.AUTOMATIC,
            "GRDBSQLite product linkage should still parse as 'automatic' (it's the *backing target* that's system)")
    _assert(by_name["GRDB"].linkage == Linkage.AUTOMATIC, "GRDB linkage")
    _assert(by_name["GRDB-dynamic"].linkage == Linkage.DYNAMIC, "GRDB-dynamic linkage")
    # Targets: system + regular + test
    by_tname = {t.name: t for t in targets}
    _assert(by_tname["GRDBSQLite"].kind == TargetKind.SYSTEM, "GRDBSQLite target kind")
    _assert(by_tname["GRDB"].kind == TargetKind.REGULAR, "GRDB target kind")
    _assert(by_tname["GRDBTests"].kind == TargetKind.TEST, "GRDBTests target kind")
    # The test target should record its dependency-by-target.
    _assert(by_tname["GRDB"].dependencies == ["GRDBSQLite"], "GRDB target deps")


def _mk_package_from_snapshot(snap: dict, *, schemes: Optional[List[str]] = None) -> Package:
    """Wrap `_parse_dump` → Package for use in planner fixtures. Targets
    are returned with `language == N/A` (we don't have a real filesystem
    to scan); individual tests set the languages they care about.
    """
    raw, products, targets, platforms, name, tv = _parse_dump(snap)
    return Package(
        name=name,
        tools_version=tv,
        platforms=platforms,
        products=products,
        targets=targets,
        schemes=list(schemes or []),
        raw_dump=raw,
        staged_dir=Path("/tmp/fake-staged"),
    )


def _selftest_parse_alamofire_already_dynamic() -> None:
    raw, products, targets, platforms, name, tv = _parse_dump(ALAMOFIRE_DUMP_SNAPSHOT)
    _assert(name == "Alamofire", f"name was {name!r}")
    by_name = {p.name: p for p in products}
    _assert("AlamofireDynamic" in by_name, "missing AlamofireDynamic product")
    _assert(
        by_name["AlamofireDynamic"].linkage == Linkage.DYNAMIC,
        f"AlamofireDynamic should report dynamic, got {by_name['AlamofireDynamic'].linkage}",
    )
    _assert(
        by_name["Alamofire"].linkage == Linkage.AUTOMATIC,
        f"Alamofire should report automatic, got {by_name['Alamofire'].linkage}",
    )


def _selftest_linkage_decoder() -> None:
    _assert(_parse_linkage({"library": ["dynamic"]}) == Linkage.DYNAMIC, "dynamic decode")
    _assert(_parse_linkage({"library": ["automatic"]}) == Linkage.AUTOMATIC, "automatic decode")
    _assert(_parse_linkage({"library": ["static"]}) == Linkage.STATIC, "static decode")
    _assert(_parse_linkage({"executable": None}) is None, "executables are dropped")
    _assert(_parse_linkage({"plugin": None}) is None, "plugins are dropped")
    _assert(_parse_linkage({"library": []}) == Linkage.AUTOMATIC, "empty library list defaults to automatic")
    _assert(_parse_linkage({"library": ["whatever"]}) == Linkage.UNKNOWN, "unknown library variant")


def _selftest_target_kind_decoder() -> None:
    _assert(_parse_target_kind("regular") == TargetKind.REGULAR, "regular")
    _assert(_parse_target_kind("system") == TargetKind.SYSTEM, "system")
    _assert(_parse_target_kind("test") == TargetKind.TEST, "test")
    _assert(_parse_target_kind("plugin") == TargetKind.PLUGIN, "plugin")
    _assert(_parse_target_kind("nonsense") == TargetKind.UNKNOWN, "fallback to unknown")
    _assert(_parse_target_kind(None) == TargetKind.UNKNOWN, "non-string falls back")


def _selftest_default_target_path() -> None:
    _assert(_default_target_path("Foo", TargetKind.REGULAR) == "Sources/Foo", "regular default")
    _assert(_default_target_path("FooTests", TargetKind.TEST) == "Tests/FooTests", "test default")


def _selftest_dependency_parser() -> None:
    deps = _parse_dependencies([
        {"byName": ["A", None]},
        {"target": ["B", None]},
        {"product": ["C", "PkgC", None, None]},
        {"weird": ["ignored"]},
    ])
    _assert(deps == ["A", "B", "C"], f"deps were {deps}")


def _selftest_toxic_filter() -> None:
    _assert(_is_toxic_entry(".git"), ".git")
    _assert(_is_toxic_entry(".build"), ".build")
    _assert(_is_toxic_entry("DerivedData"), "DerivedData")
    _assert(_is_toxic_entry("node_modules"), "node_modules")
    _assert(_is_toxic_entry("Foo.xcodeproj"), "xcodeproj")
    _assert(_is_toxic_entry("Foo.xcworkspace"), "xcworkspace")
    _assert(not _is_toxic_entry("Sources"), "Sources should not be toxic")
    _assert(not _is_toxic_entry("Package.swift"), "Package.swift should not be toxic")


def _selftest_validate_package_source(tmp_root: Path) -> None:
    """Argument-injection hardening (REFACTOR_PLAN Task 4): crafted
    URLs / local-path shapes must be rejected as FetchError BEFORE any
    git invocation.

    Accepts: http/https/git@/ssh remote URLs, existing local directories.
    Rejects: leading `-`, newlines / CR / NUL, empty, unknown-scheme
    strings that don't resolve to a local directory.
    """
    # Happy paths: remote URL variants.
    _validate_package_source("https://github.com/Alamofire/Alamofire.git")
    _validate_package_source("http://example.com/repo.git")
    _validate_package_source("git@github.com:owner/repo.git")
    _validate_package_source("ssh://git@github.com/owner/repo.git")
    # Happy path: local directory.
    local_dir = tmp_root / "local_pkg"
    local_dir.mkdir()
    _validate_package_source(str(local_dir))

    def _expect_reject(source: str, hint: str) -> None:
        try:
            _validate_package_source(source)
        except FetchError as exc:
            _assert(
                isinstance(exc, _USER_FACING_ERRORS),
                f"FetchError must be user-facing for {source!r}",
            )
            return
        raise AssertionError(
            f"expected FetchError for {hint}: {source!r}"
        )

    _expect_reject("", "empty package source")
    _expect_reject("-evil", "leading dash")
    _expect_reject("--upload-pack=/tmp/evil", "long-option shape")
    _expect_reject("https://evil\ncmd", "newline in URL")
    _expect_reject("https://evil\rcmd", "carriage return in URL")
    _expect_reject("https://evil\x00cmd", "null byte in URL")
    _expect_reject("not-a-url-or-path", "unknown scheme, no local dir")
    # Local path with a newline — rejected even though a filesystem
    # lookup would also fail.
    missing_with_nl = str(tmp_root / "missing\nevil")
    _expect_reject(missing_with_nl, "newline in local path")


def _selftest_validate_git_ref() -> None:
    """Tag-shape validation: real-world tags accepted, pathological
    shapes rejected with a clean FetchError."""
    # Happy paths — common tag flavors.
    for ok in (
        "5.10.2",
        "v5.10.2",
        "7.9.0",
        "release-1.2.3",
        "rc_2024.01",
        "1.2.3+build.4",
        "refs/tags/foo",
        "",  # empty refs short-circuit as "no-op"
    ):
        _validate_git_ref(ok, field="version")

    def _expect_reject(ref: str, hint: str) -> None:
        try:
            _validate_git_ref(ref, field="version")
        except FetchError as exc:
            _assert(
                isinstance(exc, _USER_FACING_ERRORS),
                f"FetchError must be user-facing for {ref!r}",
            )
            return
        raise AssertionError(f"expected FetchError for {hint}: {ref!r}")

    _expect_reject("-evil", "leading dash")
    _expect_reject("--upload-pack=/tmp/evil", "long-option shape")
    _expect_reject("1.2.3\nEVIL", "newline injection")
    _expect_reject("1.2.3\rEVIL", "carriage return injection")
    _expect_reject("1.2.3\x00EVIL", "null byte injection")
    _expect_reject("tag with space", "space not in allowed set")
    _expect_reject("a" * 500, "length cap")
    _expect_reject("tag;rm -rf /", "semicolon shell metacharacter")


def _selftest_language_counter(tmp_root: Path) -> None:
    """Real Package.swift → confirm `scan_target_languages` derives the
    right Language from `swift package describe --type json`.

    Covers the cases the old FS walker hand-coded heuristics for:
      - Swift-only target (`.target()` with .swift)
      - ObjC target (`.target()` with .m + publicHeadersPath)
      - Header-only target (ObjC umbrella; .h with no .swift / .m)
      - Swift target with a stub umbrella .h alongside the Swift
        sources (BlinkIDUX regression: must still classify as Swift,
        not Mixed — SPM classifies it as SwiftTarget because the
        manifest is `.target()`).

    Mixed-language is intentionally not exercised here: SwiftPM today
    rejects a target with both .swift and .m at manifest parse, so it
    can't be set up as a fixture. `_MODULE_TYPE_LANGUAGE` carries the
    mapping so the moment SwiftPM enables the experimental
    `MixedLanguageTarget` for real it already classifies correctly.
    """
    base = tmp_root / "lang_counter_pkg"
    base.mkdir()
    (base / "Sources" / "SwiftOnly").mkdir(parents=True)
    (base / "Sources" / "SwiftOnly" / "Foo.swift").write_text("public func a() {}\n")
    (base / "Sources" / "ObjCOnly" / "include").mkdir(parents=True)
    (base / "Sources" / "ObjCOnly" / "Bar.m").write_text("void b(void) {}\n")
    (base / "Sources" / "ObjCOnly" / "include" / "Bar.h").write_text("void b(void);\n")
    (base / "Sources" / "HeadersOnly" / "include").mkdir(parents=True)
    (base / "Sources" / "HeadersOnly" / "include" / "Public.h").write_text("void p(void);\n")
    (base / "Sources" / "HeadersOnly" / "stub.c").write_text("/* SPM requires one source */\n")
    (base / "Sources" / "SwiftWithUmbrella").mkdir(parents=True)
    (base / "Sources" / "SwiftWithUmbrella" / "X.swift").write_text("public func x() {}\n")
    (base / "Sources" / "SwiftWithUmbrella" / "SwiftWithUmbrella.h").write_text("// auto-generated umbrella stub\n")
    (base / "Package.swift").write_text(
        '// swift-tools-version: 5.9\n'
        'import PackageDescription\n'
        'let package = Package(\n'
        '    name: "LangProbe",\n'
        '    products: [\n'
        '        .library(name: "SwiftOnly", targets: ["SwiftOnly"]),\n'
        '        .library(name: "ObjCOnly", targets: ["ObjCOnly"]),\n'
        '        .library(name: "HeadersOnly", targets: ["HeadersOnly"]),\n'
        '        .library(name: "SwiftWithUmbrella", targets: ["SwiftWithUmbrella"]),\n'
        '    ],\n'
        '    targets: [\n'
        '        .target(name: "SwiftOnly"),\n'
        '        .target(name: "ObjCOnly", publicHeadersPath: "include"),\n'
        '        .target(name: "HeadersOnly", publicHeadersPath: "include"),\n'
        '        .target(name: "SwiftWithUmbrella"),\n'
        '    ]\n'
        ')\n'
    )

    targets = [
        Target(name="SwiftOnly", kind=TargetKind.REGULAR,
               path="Sources/SwiftOnly", public_headers_path=None,
               dependencies=[], exclude=[]),
        Target(name="ObjCOnly", kind=TargetKind.REGULAR,
               path="Sources/ObjCOnly", public_headers_path="include",
               dependencies=[], exclude=[]),
        Target(name="HeadersOnly", kind=TargetKind.REGULAR,
               path="Sources/HeadersOnly", public_headers_path="include",
               dependencies=[], exclude=[]),
        Target(name="SwiftWithUmbrella", kind=TargetKind.REGULAR,
               path="Sources/SwiftWithUmbrella", public_headers_path=None,
               dependencies=[], exclude=[]),
    ]
    scan_target_languages(base, targets)
    by_name = {t.name: t.language for t in targets}
    _assert(by_name["SwiftOnly"] == Language.SWIFT,
            f"SwiftOnly → {by_name['SwiftOnly']}")
    _assert(by_name["ObjCOnly"] == Language.OBJC,
            f"ObjCOnly → {by_name['ObjCOnly']}")
    _assert(by_name["HeadersOnly"] == Language.OBJC,
            f"HeadersOnly (umbrella ObjC shape) → {by_name['HeadersOnly']}")
    _assert(by_name["SwiftWithUmbrella"] == Language.SWIFT,
            "SwiftWithUmbrella must classify as Swift, not Mixed "
            "(BlinkIDUX regression — SPM ignores the stub umbrella .h "
            f"because the manifest is `.target()`); got {by_name['SwiftWithUmbrella']}")

    # source_file_count reflects describe's `sources` list (SPM-applied
    # exclude/include rules), not a raw FS walk. SwiftOnly = 1 (Foo.swift).
    swift_only_tgt = next(t for t in targets if t.name == "SwiftOnly")
    _assert(swift_only_tgt.source_file_count == 1,
            f"SwiftOnly.source_file_count={swift_only_tgt.source_file_count}; expected 1")


def _selftest_minimixed_fetch_integration() -> None:
    """Full Fetch integration against testdata/MiniMixed.

    Verifies the inclusion-by-default + toxic-exclusion staging rule:
      - Sources/, Package.swift, etc. land in staged/
      - MiniMixed.xcodeproj is excluded
      - Sources/MiniSwift/Excluded.txt (per package's `exclude:` list)
        gets removed by the second pass

    Also asserts the Inspect-phase contract for a fixture whose
    `MiniMixed` target contains both `.swift` and `.m` sources:
    `swift package describe --type json` refuses to classify
    mixed-language targets ("feature not supported"), and the new
    describe-based `scan_target_languages` surfaces that as an
    `InspectError` rather than silently falling through to a stale
    FS-walk classification. The fixture is the canonical regression
    case for that boundary.
    """
    # `__file__` now lives under `src/`, so walk up one level to the
    # repo root where `testdata/` lives.
    repo_root = Path(__file__).resolve().parent.parent
    fixture = repo_root / "testdata" / "MiniMixed"
    if not (fixture / "Package.swift").is_file():
        raise AssertionError(f"Fixture missing: {fixture}")

    with tempfile.TemporaryDirectory(prefix="spm2xc-selftest-") as tmp:
        config = Config(
            package_source=str(fixture),
            user_version="",
            resolved_version="",
            work_dir=Path(tmp),
        )
        source_dir = fetch_source(config)
        _assert((source_dir / "Package.swift").is_file(), "fetch_source did not land Package.swift")
        _assert((source_dir / "MiniMixed.xcodeproj").is_dir(),
                "fetch_source should preserve xcodeproj in source/ (only staging strips it)")

        staged = stage_source(config, source_dir)
        _assert((staged / "Package.swift").is_file(), "Package.swift missing in staged/")
        _assert(not (staged / "MiniMixed.xcodeproj").exists(),
                "xcodeproj should NOT be in staged/")
        _assert((staged / "Sources" / "MiniSwift" / "MiniSwift.swift").is_file(),
                "Swift source missing in staged/")
        _assert((staged / "Sources" / "MiniObjC" / "MiniObjC.m").is_file(),
                "ObjC source missing in staged/")

        # The second-pass exclude cleanup in `stage_source` depends on
        # `swift package dump-package` succeeding on the staged tree.
        # On sandboxed CI (sandbox-exec, restricted envs) that call can
        # fail with "sandbox_apply: Operation not permitted", in which
        # case the exclude cleanup silently no-ops. If we go straight to
        # the `Excluded.txt should have been removed` assertion in that
        # state, the user sees a misleading failure that blames the
        # cleanup logic instead of the sandboxed toolchain (Codex
        # testing note). Probe dump-package here so the actual root
        # cause surfaces first.
        dump_probe = subprocess.run(
            ["swift", "package", "dump-package"],
            cwd=str(staged),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if dump_probe.returncode != 0:
            tail = "\n  ".join(
                (dump_probe.stderr or "").rstrip().splitlines()[-5:]
            ) or "(no stderr)"
            raise AssertionError(
                "`swift package dump-package` failed on staged MiniMixed — "
                "skipping the exclude-cleanup assertion would hide the real "
                "cause. Root cause (from swift):\n  " + tail
            )
        _assert(not (staged / "Sources" / "MiniSwift" / "Excluded.txt").exists(),
                "Excluded.txt should have been removed by the second-pass "
                "exclude cleanup (dump-package succeeded, so the cleanup "
                "really did run)")

        # dump_package classifies products/targets without needing
        # describe — it must succeed even though the fixture contains a
        # mixed-language target that SPM's describe pass rejects.
        raw, products, targets, _platforms, name, _tv = dump_package(staged)
        _assert(name == "MiniMixed", f"package name was {name!r}")
        _assert(len(products) == 3, f"expected 3 products, got {len(products)}")
        by_name = {p.name: p for p in products}
        _assert("MiniSwift" in by_name and "MiniObjC" in by_name and "MiniMixed" in by_name,
                f"missing expected product, got {list(by_name.keys())}")
        for p in products:
            _assert(p.linkage == Linkage.AUTOMATIC, f"{p.name} linkage was {p.linkage!r}")

        # Mixed-language regression contract: SPM rejects the
        # MiniMixed target at describe time, so the full
        # inspect_package call must surface an InspectError whose
        # message names the unsupported feature. This is the boundary
        # the describe-based classifier is supposed to enforce — see
        # REFACTOR_PROPOSAL.md "Open risks for B" line on mixed
        # targets being a known-broken SPM feature.
        try:
            inspect_package(config, staged)
        except InspectError as exc:
            msg = str(exc)
            _assert(
                "mixed language source files" in msg
                or "feature not supported" in msg,
                f"InspectError did not name the SPM mixed-language rejection: {msg!r}",
            )
        else:
            raise AssertionError(
                "inspect_package was expected to raise InspectError on the "
                "mixed-language MiniMixed target, but it returned cleanly. "
                "Either SwiftPM started supporting mixed-language source "
                "dirs (good — update this test to assert classification) "
                "or the describe-based classifier silently dropped the "
                "rejection (bad — strict classification regression)."
            )


# --- Planner self-tests ---------------------------------------------------


def _selftest_scheme_resolver() -> None:
    # 1. exact wins over -Package and iOS suffixes
    _assert(
        resolve_scheme("GRDB", ["GRDB", "GRDB-dynamic", "GRDB-Package"]) == "GRDB",
        "exact match should win over -Package form",
    )
    # 2. case-insensitive exact
    _assert(
        resolve_scheme("grdb", ["GRDB"]) == "GRDB",
        "case-insensitive exact match",
    )
    # 3. -Package fallback
    _assert(
        resolve_scheme("Nuke", ["Nuke-Package"]) == "Nuke-Package",
        "-Package fallback",
    )
    # 4. iOS suffix variants
    _assert(resolve_scheme("Foo", ["Foo iOS"]) == "Foo iOS", "Foo iOS")
    _assert(resolve_scheme("Foo", ["Foo-iOS"]) == "Foo-iOS", "Foo-iOS")
    _assert(resolve_scheme("Foo", ["Foo (iOS)"]) == "Foo (iOS)", "Foo (iOS)")
    # 5. final fallback: returns the product name unchanged
    _assert(resolve_scheme("Nothing", []) == "Nothing", "empty schemes")
    _assert(
        resolve_scheme("Nothing", ["SomethingElse"]) == "Nothing",
        "no candidate matches",
    )


def _selftest_planner_grdb() -> None:
    """GRDB: synth_dynamic_library for the non-dynamic GRDB product (added
    as a parallel dynamic product via `swift package add-product`, then
    renamed post-archive back to GRDB.framework). The already-dynamic
    GRDB-dynamic product is built as-is with no edit. GRDBSQLite is
    dropped entirely — it's a system-target wrapper.
    """
    pkg = _mk_package_from_snapshot(
        GRDB_DUMP_SNAPSHOT,
        schemes=["GRDB", "GRDB-dynamic", "GRDB-Package"],
    )
    config = Config(
        package_source="https://github.com/groue/GRDB.swift.git",
        user_version="7.9.0",
        resolved_version="v7.9.0",
    )
    plan = plan_source_build(config, pkg)

    names = [bu.name for bu in plan.build_units]
    _assert("GRDB" in names, f"GRDB must be built; got {names}")
    _assert("GRDB-dynamic" in names, f"GRDB-dynamic must be built; got {names}")
    _assert("GRDBSQLite" not in names, f"GRDBSQLite must be skipped; got {names}")

    # Find the synthetic dynamic edit for GRDB. The exact synthetic name
    # is allocator-driven (e.g. GRDBDynamic, GRDB__Dynamic) — the
    # contract is "exactly one synth_dynamic_library whose targets
    # match GRDB's backing targets, and the build unit it backs reports
    # framework_name='GRDB'".
    synth_dyn_edits = [
        e for e in plan.package_swift_edits if e.kind == "synth_dynamic_library"
    ]
    _assert(
        len(synth_dyn_edits) == 1,
        f"expected exactly one synth_dynamic_library edit, got {synth_dyn_edits}",
    )
    grdb_edit = synth_dyn_edits[0]
    _assert(
        grdb_edit.targets == ["GRDB"],
        f"synth_dynamic_library targets should be ['GRDB']; got {grdb_edit.targets}",
    )

    # The build unit named GRDB carries the framework_name='GRDB' contract
    # (consumer-facing bundle name after the post-archive rename) while
    # scheme is the synthetic product name we added to Package.swift.
    grdb_bu = next(bu for bu in plan.build_units if bu.name == "GRDB")
    _assert(
        grdb_bu.framework_name == "GRDB",
        f"GRDB framework_name should be 'GRDB' (post-rename); got '{grdb_bu.framework_name}'",
    )
    _assert(
        grdb_bu.scheme == grdb_edit.product_name,
        f"GRDB scheme should be the synthetic product name '{grdb_edit.product_name}'; got '{grdb_bu.scheme}'",
    )

    # The already-dynamic GRDB-dynamic build unit must NOT have a
    # synthetic edit — it ships as the existing product, no renaming.
    grdb_dyn_bu = next(bu for bu in plan.build_units if bu.name == "GRDB-dynamic")
    _assert(
        grdb_dyn_bu.framework_name == "GRDB-dynamic",
        f"GRDB-dynamic must build as itself; got framework_name '{grdb_dyn_bu.framework_name}'",
    )
    _assert(
        grdb_dyn_bu.scheme == "GRDB-dynamic",
        f"GRDB-dynamic scheme should match the literal product name; got '{grdb_dyn_bu.scheme}'",
    )

    skipped_names = [n for n, _ in plan.skipped]
    _assert(
        skipped_names == ["GRDBSQLite"],
        f"expected GRDBSQLite in skipped list; got {plan.skipped}",
    )


def _selftest_planner_alamofire() -> None:
    """Alamofire-shape: two products over the same target — plan both
    build units, but only emit a synth_dynamic_library edit for the
    non-dynamic one. The synthetic name collides with the existing
    AlamofireDynamic, so the allocator falls back (Alamofire__Dynamic
    or similar)."""
    pkg = _mk_package_from_snapshot(
        ALAMOFIRE_DUMP_SNAPSHOT,
        schemes=["Alamofire", "AlamofireDynamic", "Alamofire-Package"],
    )
    config = Config(
        package_source="https://github.com/Alamofire/Alamofire.git",
        user_version="5.10.2",
        resolved_version="5.10.2",
    )
    plan = plan_source_build(config, pkg)

    names = {bu.name for bu in plan.build_units}
    _assert(
        names == {"Alamofire", "AlamofireDynamic"},
        f"Alamofire should plan both build units, got {names}",
    )

    synth_dyn_edits = [
        e for e in plan.package_swift_edits if e.kind == "synth_dynamic_library"
    ]
    _assert(
        len(synth_dyn_edits) == 1,
        f"expected exactly one synth_dynamic_library edit, got {synth_dyn_edits}",
    )
    edit = synth_dyn_edits[0]
    _assert(
        edit.targets == ["Alamofire"],
        f"edit targets should be ['Alamofire']; got {edit.targets}",
    )
    # Collision-aware naming: cannot use 'AlamofireDynamic' since it
    # already exists, so allocator picks a fallback (e.g. Alamofire__Dynamic).
    _assert(
        edit.product_name != "AlamofireDynamic" and edit.product_name != "Alamofire",
        f"synthetic name must not collide; got {edit.product_name!r}",
    )

    alamo_bu = next(bu for bu in plan.build_units if bu.name == "Alamofire")
    _assert(
        alamo_bu.framework_name == "Alamofire",
        f"Alamofire framework_name should be 'Alamofire' (post-rename); got '{alamo_bu.framework_name}'",
    )
    _assert(
        alamo_bu.scheme == edit.product_name,
        f"Alamofire scheme should be the synthetic name; got '{alamo_bu.scheme}'",
    )
    alamo_dyn_bu = next(bu for bu in plan.build_units if bu.name == "AlamofireDynamic")
    _assert(
        alamo_dyn_bu.framework_name == "AlamofireDynamic",
        f"AlamofireDynamic should ship as itself; got '{alamo_dyn_bu.framework_name}'",
    )


def _selftest_planner_stripe_synthetic_libraries() -> None:
    """Stripe: --product Stripe narrows the product set to one and
    --target StripeCore / --target StripeUICore add two synthetic
    libraries (the original 3-unit set this test pinned). On top of
    that, the auto-synth-sibling-units pass picks up StripePayments
    (Stripe statically depends on it AND it has a public product
    wrapper), so the final plan has four build units total.
    """
    pkg = _mk_package_from_snapshot(STRIPE_DUMP_SNAPSHOT, schemes=[])
    config = Config(
        package_source="https://github.com/stripe/stripe-ios.git",
        user_version="25.6.2",
        resolved_version="25.6.2",
        product_filters=["Stripe"],
        target_filters=["StripeCore", "StripeUICore"],
    )
    plan = plan_source_build(config, pkg)

    names = {bu.name for bu in plan.build_units}
    _assert(
        names == {"Stripe", "StripeCore", "StripeUICore", "StripePayments"},
        f"expected 4 build units {{Stripe, StripeCore, StripeUICore, StripePayments}}, got {names}",
    )
    _assert(
        len(plan.build_units) == 4,
        f"expected exactly 4 units, got {len(plan.build_units)}",
    )

    by_name = {bu.name: bu for bu in plan.build_units}
    _assert(by_name["StripeCore"].synthetic, "StripeCore should be marked synthetic")
    _assert(by_name["StripeUICore"].synthetic, "StripeUICore should be marked synthetic")
    _assert(
        not by_name["Stripe"].synthetic,
        "Stripe is a real product, not synthetic",
    )
    # StripePayments is a real public product — the auto-synth pass
    # reuses the product wrapper rather than synthesising one, so the
    # unit must NOT be flagged synthetic.
    _assert(
        not by_name["StripePayments"].synthetic,
        "StripePayments is a real product, not synthetic",
    )

    # synth_library: --target on a target that has no matching product
    # synthesizes a fresh dynamic library with the target's name as the
    # product name (no collision risk by construction). StripePayments
    # has a matching product so it never goes through this branch.
    synth_lib_names = {
        e.product_name for e in plan.package_swift_edits
        if e.kind == "synth_library"
    }
    _assert(
        synth_lib_names == {"StripeCore", "StripeUICore"},
        f"synth_library edits should be {{StripeCore, StripeUICore}}, got {synth_lib_names}",
    )

    # synth_dynamic_library: the existing 'Stripe' product is automatic,
    # so the planner adds a parallel dynamic product (allocator-chosen
    # name) and the build unit renames the bundle back to 'Stripe' after
    # the archive. The auto-synth pass does the same for StripePayments
    # (also automatic linkage). Two such edits total.
    synth_dyn_edits = [
        e for e in plan.package_swift_edits if e.kind == "synth_dynamic_library"
    ]
    _assert(
        len(synth_dyn_edits) == 2,
        f"expected exactly two synth_dynamic_library edits; got {synth_dyn_edits}",
    )
    edits_by_target = {tuple(e.targets): e for e in synth_dyn_edits}
    stripe_edit = edits_by_target.get(("Stripe",))
    _assert(
        stripe_edit is not None,
        f"missing synth_dynamic_library edit targeting Stripe; got {synth_dyn_edits}",
    )
    payments_edit = edits_by_target.get(("StripePayments",))
    _assert(
        payments_edit is not None,
        f"missing synth_dynamic_library edit targeting StripePayments; got {synth_dyn_edits}",
    )
    _assert(
        stripe_edit.product_name != "Stripe",
        f"synthetic name must differ from the existing Stripe product; got {stripe_edit.product_name!r}",
    )
    _assert(
        payments_edit.product_name != "StripePayments",
        f"synthetic name must differ from the existing StripePayments product; got {payments_edit.product_name!r}",
    )
    stripe_bu = by_name["Stripe"]
    _assert(
        stripe_bu.framework_name == "Stripe",
        f"Stripe build unit framework_name should be 'Stripe' (post-rename); got '{stripe_bu.framework_name}'",
    )
    _assert(
        stripe_bu.scheme == stripe_edit.product_name,
        f"Stripe build unit scheme should match the synthetic name; got '{stripe_bu.scheme}'",
    )
    payments_bu = by_name["StripePayments"]
    _assert(
        payments_bu.framework_name == "StripePayments",
        f"StripePayments build unit framework_name should be 'StripePayments'; got '{payments_bu.framework_name}'",
    )
    _assert(
        payments_bu.scheme == payments_edit.product_name,
        f"StripePayments build unit scheme should match the synthetic name; got '{payments_bu.scheme}'",
    )


def _selftest_planner_stripe_rejects_binary_target() -> None:
    """--target on a TargetKind.BINARY target (Stripe3DS2 in this fixture)
    must fail with PlanError."""
    pkg = _mk_package_from_snapshot(STRIPE_DUMP_SNAPSHOT, schemes=[])
    config = Config(
        package_source="https://github.com/stripe/stripe-ios.git",
        user_version="25.6.2",
        target_filters=["Stripe3DS2"],
    )
    try:
        plan_source_build(config, pkg)
    except PlanError as exc:
        _assert(
            "Stripe3DS2" in str(exc),
            f"PlanError should mention Stripe3DS2, got: {exc}",
        )
        return
    raise AssertionError("plan_source_build should have raised PlanError for a binary target")


# --- Binary-only product detection + planner suppression ------------------
#
# Kidoz SDK (issue #39) ships a Package.swift whose only product is backed
# by a single binaryTarget. Source mode used to die at SPM resolve with
# "invalid type for binary product" because the unconditional force_dynamic
# patch is invalid for binary-only products. The fix has two pieces, both
# tested here: (a) `_is_binary_only_product` / `_package_is_binary_only`
# classifiers, and (b) `plan_source_build` skipping force_dynamic AND
# the build unit for binary-only products inside a mixed package.


# Pure-binary Package.swift shape (one product, one binaryTarget,
# nothing else). Mirrors the Kidoz/iCarousel-as-binary shape.
KIDOZ_LIKE_DUMP_SNAPSHOT: dict = {
    "name": "kidoz-sdk-swift-package",
    "toolsVersion": {"_version": "5.7.0"},
    "platforms": [{"options": [], "platformName": "ios", "version": "13.0"}],
    "products": [
        {"name": "KidozSDK", "type": {"library": ["automatic"]}, "targets": ["KidozSDK"]},
    ],
    "targets": [
        {"name": "KidozSDK", "type": "binary", "path": None,
         "publicHeadersPath": None, "dependencies": []},
    ],
}

# Mixed: one binary product + one source product. The source product
# should plan normally (force_dynamic + BuildUnit); the binary one
# should be skipped with a clear reason.
MIXED_BINARY_AND_SOURCE_SNAPSHOT: dict = {
    "name": "MixedKit",
    "toolsVersion": {"_version": "5.7.0"},
    "platforms": [{"options": [], "platformName": "ios", "version": "15.0"}],
    "products": [
        {"name": "MixedKitBin", "type": {"library": ["automatic"]},
         "targets": ["MixedKitBin"]},
        {"name": "MixedKitSrc", "type": {"library": ["automatic"]},
         "targets": ["MixedKitSrc"]},
    ],
    "targets": [
        {"name": "MixedKitBin", "type": "binary", "path": None,
         "publicHeadersPath": None, "dependencies": []},
        {"name": "MixedKitSrc", "type": "regular",
         "path": "Sources/MixedKitSrc",
         "publicHeadersPath": None, "dependencies": []},
    ],
}


def _selftest_is_binary_only_product_classifier() -> None:
    """`_is_binary_only_product` returns True iff every backing target is
    TargetKind.BINARY. Empty target lists and missing targets are False
    (paranoid: malformed → not-binary so xcodebuild surfaces the real
    problem). Mixed-kind products are False.
    """
    pkg_pure = _mk_package_from_snapshot(KIDOZ_LIKE_DUMP_SNAPSHOT)
    kidoz = pkg_pure.products[0]
    _assert(
        _is_binary_only_product(kidoz, pkg_pure),
        "pure binary product should be classified as binary-only",
    )

    pkg_mixed = _mk_package_from_snapshot(MIXED_BINARY_AND_SOURCE_SNAPSHOT)
    by_name = {p.name: p for p in pkg_mixed.products}
    _assert(
        _is_binary_only_product(by_name["MixedKitBin"], pkg_mixed),
        "binary leg of a mixed package is still binary-only",
    )
    _assert(
        not _is_binary_only_product(by_name["MixedKitSrc"], pkg_mixed),
        "regular-target product must NOT be classified as binary-only",
    )

    # Empty target list → False (paranoid).
    empty_product = Product(name="Empty", linkage=Linkage.AUTOMATIC, targets=[])
    _assert(
        not _is_binary_only_product(empty_product, pkg_pure),
        "empty target list must not classify as binary-only",
    )

    # Target name with no matching Target object → False.
    dangling = Product(name="Dangling", linkage=Linkage.AUTOMATIC,
                       targets=["NoSuchTarget"])
    _assert(
        not _is_binary_only_product(dangling, pkg_pure),
        "undefined target name must not classify as binary-only",
    )


def _selftest_package_is_binary_only() -> None:
    """Package-level classifier: True iff every (non-system) product is
    binary-only. System-only products are skipped from the check so a
    package with one binary library + one system-shim library still
    counts as binary-only. Zero-product packages return False (no
    binaries to copy in --binary mode either).
    """
    pkg_pure = _mk_package_from_snapshot(KIDOZ_LIKE_DUMP_SNAPSHOT)
    _assert(
        _package_is_binary_only(pkg_pure),
        "pure-binary package must be classified as binary-only",
    )

    pkg_mixed = _mk_package_from_snapshot(MIXED_BINARY_AND_SOURCE_SNAPSHOT)
    _assert(
        not _package_is_binary_only(pkg_mixed),
        "mixed package (one source + one binary) must NOT be binary-only "
        "at the package level — source mode still has work to do",
    )

    # GRDB ships a system-target wrapper alongside source targets — the
    # presence of the system product must NOT short-circuit the check,
    # but it must also not block the binary-only verdict when paired
    # with binary products.
    pkg_grdb = _mk_package_from_snapshot(GRDB_DUMP_SNAPSHOT)
    _assert(
        not _package_is_binary_only(pkg_grdb),
        "GRDB (has source targets) must NOT be binary-only",
    )

    # Zero products → False.
    empty_pkg = Package(
        name="empty", tools_version="5.7", platforms=[], products=[],
        targets=[], schemes=[], raw_dump={}, staged_dir=Path("/tmp/x"),
    )
    _assert(
        not _package_is_binary_only(empty_pkg),
        "package with zero products is not binary-only "
        "(--binary mode would also find nothing to copy)",
    )

    # System-only + binary-only co-existing → still binary-only at the
    # package level. Construct a synthetic snapshot to exercise this.
    sys_plus_binary_snapshot = {
        "name": "SysPlusBinary",
        "toolsVersion": {"_version": "5.7.0"},
        "platforms": [{"options": [], "platformName": "ios", "version": "15.0"}],
        "products": [
            {"name": "SysShim", "type": {"library": ["automatic"]},
             "targets": ["SysShim"]},
            {"name": "TheBinary", "type": {"library": ["automatic"]},
             "targets": ["TheBinary"]},
        ],
        "targets": [
            {"name": "SysShim", "type": "system", "path": "Sources/SysShim",
             "publicHeadersPath": None, "dependencies": []},
            {"name": "TheBinary", "type": "binary", "path": None,
             "publicHeadersPath": None, "dependencies": []},
        ],
    }
    pkg_mixed_sys = _mk_package_from_snapshot(sys_plus_binary_snapshot)
    _assert(
        _package_is_binary_only(pkg_mixed_sys),
        "system + binary products: should still be binary-only because "
        "system-only products are dropped from the package check (the "
        "planner drops them too)",
    )


def _selftest_planner_skips_binary_only_product_in_mixed_package() -> None:
    """Mixed package (one binary product + one source product) processed
    in source mode: binary product is recorded in plan.skipped (NOT in
    plan.build_units) with a clear reason that points users at --binary.
    The source product still receives force_dynamic + a BuildUnit.

    This is the "force_dynamic patch on a binary product breaks SPM"
    fix from issue #39, plus the follow-up that source mode also can't
    archive a binaryTarget-backed product so the BuildUnit goes too.
    """
    pkg = _mk_package_from_snapshot(
        MIXED_BINARY_AND_SOURCE_SNAPSHOT,
        schemes=["MixedKitSrc", "MixedKitBin"],
    )
    config = Config(
        package_source="./mixedkit",
        user_version="",
    )
    plan = plan_source_build(config, pkg)

    # Source product plans normally.
    bu_names = [bu.name for bu in plan.build_units]
    _assert(
        bu_names == ["MixedKitSrc"],
        f"expected only the source product in build_units, got {bu_names}",
    )

    # The source product MixedKitSrc is non-dynamic, so the planner
    # adds a parallel synthetic dynamic product (allocator-chosen name)
    # that the build unit will archive then rename back to MixedKitSrc.
    synth_dyn_edits = [
        e for e in plan.package_swift_edits if e.kind == "synth_dynamic_library"
    ]
    _assert(
        len(synth_dyn_edits) == 1,
        f"source product should get a synth_dynamic_library edit; got {plan.package_swift_edits}",
    )
    _assert(
        synth_dyn_edits[0].targets == ["MixedKitSrc"],
        f"synth_dynamic_library targets should be ['MixedKitSrc']; got {synth_dyn_edits[0].targets}",
    )

    # Binary product never gets a synth edit — it gets replaced by a
    # `.binaryTarget` reference via `replace_with_binary_target` (its
    # own edit kind), or is left alone in source mode and recorded in
    # plan.skipped. The contract here: no `synth_*` edit names the
    # binary product.
    for e in plan.package_swift_edits:
        if e.kind in ("synth_dynamic_library", "synth_library"):
            _assert(
                e.product_name != "MixedKitBin",
                f"binary product MUST NOT receive synth edits; got {e}",
            )

    skipped_by_name = {n: reason for n, reason in plan.skipped}
    _assert(
        "MixedKitBin" in skipped_by_name,
        f"binary product must be recorded in plan.skipped; "
        f"got {plan.skipped}",
    )
    _assert(
        "binary" in skipped_by_name["MixedKitBin"].lower(),
        f"skip reason should mention 'binary'; "
        f"got {skipped_by_name['MixedKitBin']!r}",
    )


def _selftest_planner_pure_binary_package_reaches_planner_raises() -> None:
    """End-to-end shape: if a pure-binary package somehow reaches
    `plan_source_build` (bypassing the `_run_source_mode` auto-switch
    — e.g. through a future refactor that drops the switch, or a test
    invoking the planner directly), every product is recorded as
    skipped and the planner refuses to ship a zero-build-unit plan,
    raising PlanError. This is the belt-and-braces guard: a bad Plan
    can never reach Prepare even if the auto-switch is removed, and
    the error message points at the cause.

    Note: in the real CLI path this case is unreachable because the
    auto-switch hands off to `_run_binary_mode` *before* the planner
    runs. The test pins the contract for direct planner callers.
    """
    pkg = _mk_package_from_snapshot(KIDOZ_LIKE_DUMP_SNAPSHOT, schemes=[])
    config = Config(
        package_source="https://github.com/Kidoz-SDK/kidoz-sdk-swift-package.git",
        user_version="10.1.5",
    )
    try:
        plan_source_build(config, pkg)
    except PlanError as exc:
        msg = str(exc)
        _assert(
            "zero build units" in msg or "non-library" in msg,
            f"PlanError should mention the empty-plan reason; got: {exc}",
        )
        return
    raise AssertionError(
        "plan_source_build on a pure-binary package should have raised "
        "PlanError (the auto-switch is supposed to catch this earlier; if "
        "we reach the planner directly, refusing to ship an empty plan is "
        "the right outcome)."
    )


def _selftest_planner_rejects_executable_target() -> None:
    """Codex [P2]: --target on a TargetKind.EXECUTABLE target must fail
    with a clean PlanError at planning time, not blow up later inside
    Prepare when the synthesized `.library(…, targets: [<executable>])`
    fails the round-trip dump-package validation.
    """
    # Small synthetic snapshot: one executable target and a regular
    # library product. The executable target is the one the user
    # (mistakenly) targets via --target.
    snap: dict = {
        "name": "ToolKit",
        "toolsVersion": {"_version": "5.7.0"},
        "platforms": [{"options": [], "platformName": "ios", "version": "15.0"}],
        "products": [
            {"name": "ToolKit", "type": {"library": ["automatic"]}, "targets": ["ToolKit"]},
            {"name": "toolkit-cli", "type": {"executable": None}, "targets": ["toolkit-cli"]},
        ],
        "targets": [
            {"name": "ToolKit", "type": "regular", "path": "Sources/ToolKit",
             "publicHeadersPath": None, "dependencies": []},
            {"name": "toolkit-cli", "type": "executable", "path": "Sources/toolkit-cli",
             "publicHeadersPath": None, "dependencies": []},
        ],
    }
    pkg = _mk_package_from_snapshot(snap, schemes=[])
    config = Config(
        package_source="./toolkit",
        user_version="",
        target_filters=["toolkit-cli"],
    )
    try:
        plan_source_build(config, pkg)
    except PlanError as exc:
        msg = str(exc)
        _assert(
            "toolkit-cli" in msg,
            f"PlanError should mention the target name, got: {exc}",
        )
        _assert(
            "executable" in msg,
            f"PlanError should mention 'executable' so the user sees the "
            f"root cause, got: {exc}",
        )
        return
    raise AssertionError(
        "plan_source_build should have raised PlanError for an executable target "
        "but returned a plan instead"
    )


def _selftest_planner_duplicate_target_filters_deduped() -> None:
    """Codex [P2] coverage gap: repeated --target X --target X on the
    command line must not produce two synthetic PackageSwiftEdits / two
    BuildUnits for the same target. The planner should emit one pair
    and record a warning for the duplicate.
    """
    pkg = _mk_package_from_snapshot(STRIPE_DUMP_SNAPSHOT, schemes=[])
    config = Config(
        package_source="https://github.com/stripe/stripe-ios.git",
        user_version="25.6.2",
        # Narrow the .library() pass to a non-overlapping product so
        # we're exercising pure synthesis, not the "reinstate filtered
        # existing product" branch.
        product_filters=["StripePayments"],
        target_filters=["StripeCore", "StripeCore"],
    )
    plan = plan_source_build(config, pkg)

    synthetic_names = [
        e.product_name for e in plan.package_swift_edits
        if e.kind == "synth_library"
    ]
    _assert(
        synthetic_names.count("StripeCore") == 1,
        f"StripeCore should synthesize exactly once, got "
        f"{synthetic_names!r}",
    )
    planned_stripecore = [bu for bu in plan.build_units if bu.name == "StripeCore"]
    _assert(
        len(planned_stripecore) == 1,
        f"StripeCore should appear in build_units exactly once, got "
        f"{[bu.name for bu in plan.build_units]!r}",
    )
    _assert(
        any("specified more than once" in w for w in plan.warnings),
        f"expected a 'specified more than once' planner warning, "
        f"got {plan.warnings!r}",
    )


def _selftest_planner_target_matching_existing_product_uses_existing() -> None:
    """If --target T matches an existing .library(name: T, ...) product,
    the planner must warn and reuse the existing product rather than
    synthesize a duplicate."""
    # Use GRDB: `--target GRDB` matches the existing GRDB product.
    pkg = _mk_package_from_snapshot(
        GRDB_DUMP_SNAPSHOT,
        schemes=["GRDB", "GRDB-dynamic", "GRDB-Package"],
    )
    config = Config(
        package_source="https://github.com/groue/GRDB.swift.git",
        user_version="7.9.0",
        product_filters=["GRDB"],           # narrow products to just GRDB
        target_filters=["GRDB"],            # and ALSO pass --target GRDB
    )
    plan = plan_source_build(config, pkg)
    # The planner records the warning on plan.warnings rather than
    # writing to stderr (§5.2 purity contract). Assert it's present.
    _assert(
        any("already exposed" in w for w in plan.warnings),
        f"expected warning about existing product; got {plan.warnings}",
    )

    # No synth_library edit should be added — the existing product is reused.
    synthetic = [e for e in plan.package_swift_edits if e.kind == "synth_library"]
    _assert(
        not synthetic,
        f"no synth_library edit should be added when --target matches an "
        f"existing product; got {synthetic}",
    )
    # Exactly one GRDB build unit.
    grdb_units = [bu for bu in plan.build_units if bu.name == "GRDB"]
    _assert(
        len(grdb_units) == 1,
        f"exactly one GRDB build unit expected, got {len(grdb_units)}",
    )
    _assert(not grdb_units[0].synthetic, "reused GRDB must not be marked synthetic")


def _selftest_planner_target_reinstates_filtered_product() -> None:
    """If `--product` excludes a product but `--target T` names that same
    product, the planner must re-include it exactly once. In B's edit
    model the reinstated non-dynamic product gets a synth_dynamic_library
    edit (parallel dynamic product + post-archive rename) — there's no
    longer a separate "force_dynamic" pathway.
    """
    pkg = _mk_package_from_snapshot(
        ALAMOFIRE_DUMP_SNAPSHOT,
        schemes=["Alamofire", "AlamofireDynamic"],
    )
    config = Config(
        package_source="https://github.com/Alamofire/Alamofire.git",
        user_version="5.10.2",
        product_filters=["AlamofireDynamic"],
        target_filters=["Alamofire"],
    )
    plan = plan_source_build(config, pkg)

    names = [bu.name for bu in plan.build_units]
    _assert(
        sorted(names) == ["Alamofire", "AlamofireDynamic"],
        f"expected both Alamofire and AlamofireDynamic, got {names}",
    )
    _assert(
        names.count("Alamofire") == 1,
        f"Alamofire should appear once, got {names}",
    )
    by_name = {bu.name: bu for bu in plan.build_units}
    _assert(
        not by_name["Alamofire"].synthetic,
        "reinstated Alamofire should not be marked synthetic (this branch "
        "reuses the existing product, just routed through the dynamic-rename flow)",
    )
    # Reinstated Alamofire goes through synth_dynamic_library; the
    # already-dynamic AlamofireDynamic is untouched.
    synth_dyn_edits = [
        e for e in plan.package_swift_edits if e.kind == "synth_dynamic_library"
    ]
    _assert(
        len(synth_dyn_edits) == 1,
        f"expected one synth_dynamic_library edit for reinstated Alamofire; got {synth_dyn_edits}",
    )
    _assert(
        synth_dyn_edits[0].targets == ["Alamofire"],
        f"synth_dynamic_library targets should be ['Alamofire']; got {synth_dyn_edits[0].targets}",
    )
    _assert(
        by_name["Alamofire"].framework_name == "Alamofire",
        f"reinstated Alamofire framework_name should be 'Alamofire' (post-rename); "
        f"got '{by_name['Alamofire'].framework_name}'",
    )
    # No `synth_library` edits — we reused the existing product instead
    # of synthesizing a fresh one from the target.
    synth_lib_edits = [
        e for e in plan.package_swift_edits if e.kind == "synth_library"
    ]
    _assert(not synth_lib_edits, f"no synth_library edits expected, got {synth_lib_edits}")


def _selftest_planner_binary_dedupes_duplicate_artifacts() -> None:
    """Duplicate BinaryArtifact entries (same product_name) collapse to a
    single build unit and the dropped copies land on plan.skipped."""
    artifacts = [
        BinaryArtifact("BlinkID", Path("/fake/a/BlinkID.xcframework")),
        BinaryArtifact("BlinkID", Path("/fake/b/BlinkID.xcframework")),
        BinaryArtifact("BlinkID", Path("/fake/c/BlinkID.xcframework")),
    ]
    config = Config(
        package_source="https://github.com/BlinkID/blinkid-swift-package.git",
        user_version="7.6.2",
        binary_mode=True,
    )
    plan = plan_binary_build(config, artifacts)
    _assert(
        len(plan.build_units) == 1,
        f"expected 1 build unit after dedupe, got {len(plan.build_units)}",
    )
    _assert(
        len(plan.skipped) == 2,
        f"expected 2 skipped duplicates, got {plan.skipped}",
    )
    for name, reason in plan.skipped:
        _assert(name == "BlinkID", f"skipped name should be BlinkID, got {name}")
        _assert("duplicate" in reason, f"skipped reason should mention duplicate: {reason}")


def _selftest_derive_package_label() -> None:
    """SCP-style and URL forms both round-trip to a bare repo name."""
    _assert(
        _derive_package_label("https://github.com/groue/GRDB.swift.git") == "GRDB.swift",
        "https URL",
    )
    _assert(
        _derive_package_label("git@github.com:groue/GRDB.swift.git") == "GRDB.swift",
        "SCP-style URL",
    )
    _assert(
        _derive_package_label("ssh://git@github.com/groue/GRDB.swift.git") == "GRDB.swift",
        "ssh URL",
    )
    _assert(
        _derive_package_label("/Users/me/local-pkg") == "local-pkg",
        "local path",
    )
    _assert(
        _derive_package_label("/Users/me/local-pkg/") == "local-pkg",
        "local path with trailing slash",
    )


def _selftest_planner_unmatched_product_filter() -> None:
    """--product listing a name that doesn't exist in the package raises PlanError."""
    pkg = _mk_package_from_snapshot(GRDB_DUMP_SNAPSHOT, schemes=[])
    config = Config(
        package_source="https://github.com/groue/GRDB.swift.git",
        user_version="7.9.0",
        product_filters=["NotAProduct"],
    )
    try:
        plan_source_build(config, pkg)
    except PlanError as exc:
        _assert("NotAProduct" in str(exc), f"error mentions NotAProduct: {exc}")
        return
    raise AssertionError("expected PlanError for unknown product filter")


def _selftest_planner_binary_filter() -> None:
    """Binary-mode planner filters a synthetic artifact list by --product.
    Also confirms the archive_strategy lands as 'copy-artifact' and
    --target is rejected in binary mode.
    """
    artifacts = [
        BinaryArtifact("BlinkID", Path("/fake/BlinkID/BlinkID.xcframework")),
        BinaryArtifact("BlinkIDVerify", Path("/fake/BlinkIDVerify/BlinkIDVerify.xcframework")),
        BinaryArtifact("BlinkIDUX", Path("/fake/BlinkIDUX/BlinkIDUX.xcframework")),
    ]

    # Filter to just BlinkID
    config = Config(
        package_source="https://github.com/BlinkID/blinkid-swift-package.git",
        user_version="7.6.2",
        binary_mode=True,
        product_filters=["BlinkID"],
    )
    plan = plan_binary_build(config, artifacts)
    _assert(plan.binary_mode, "binary_mode flag should be set")
    _assert(
        len(plan.build_units) == 1,
        f"expected 1 build unit, got {len(plan.build_units)}",
    )
    unit = plan.build_units[0]
    _assert(unit.name == "BlinkID", f"expected BlinkID, got {unit.name}")
    _assert(
        unit.archive_strategy == "copy-artifact",
        f"expected copy-artifact, got {unit.archive_strategy}",
    )
    _assert(unit.artifact_path == Path("/fake/BlinkID/BlinkID.xcframework"),
            f"artifact_path mismatch: {unit.artifact_path}")

    # No filter → all three
    config_all = Config(
        package_source="https://github.com/BlinkID/blinkid-swift-package.git",
        user_version="7.6.2",
        binary_mode=True,
    )
    plan_all = plan_binary_build(config_all, artifacts)
    _assert(
        len(plan_all.build_units) == 3,
        f"expected 3 build units, got {len(plan_all.build_units)}",
    )

    # Unknown product → PlanError
    config_bad = Config(
        package_source="https://github.com/BlinkID/blinkid-swift-package.git",
        user_version="7.6.2",
        binary_mode=True,
        product_filters=["NotThere"],
    )
    try:
        plan_binary_build(config_bad, artifacts)
    except PlanError as exc:
        _assert("NotThere" in str(exc), f"error message: {exc}")
    else:
        raise AssertionError("expected PlanError for unmatched --product")

    # --target in binary mode → PlanError
    config_target = Config(
        package_source="https://github.com/BlinkID/blinkid-swift-package.git",
        user_version="7.6.2",
        binary_mode=True,
        target_filters=["Dummy"],
    )
    try:
        plan_binary_build(config_target, artifacts)
    except PlanError as exc:
        _assert("target" in str(exc).lower(), f"error message: {exc}")
    else:
        raise AssertionError("expected PlanError for --target in binary mode")


def _selftest_planner_language_inference() -> None:
    """Language on a build unit is the union of its target languages."""
    snap = {
        "name": "LangTest",
        "toolsVersion": {"_version": "5.7.0"},
        "platforms": [{"options": [], "platformName": "ios", "version": "15.0"}],
        "products": [
            {"name": "SwiftLib", "type": {"library": ["automatic"]}, "targets": ["SwiftOnly"]},
            {"name": "ObjCLib", "type": {"library": ["automatic"]}, "targets": ["ObjCOnly"]},
            {"name": "MixedLib", "type": {"library": ["automatic"]}, "targets": ["SwiftOnly", "ObjCOnly"]},
        ],
        "targets": [
            {"name": "SwiftOnly", "type": "regular", "path": None, "publicHeadersPath": None, "dependencies": []},
            {"name": "ObjCOnly", "type": "regular", "path": None, "publicHeadersPath": None, "dependencies": []},
        ],
    }
    pkg = _mk_package_from_snapshot(snap, schemes=[])
    # Language is populated by scan_target_languages in the real flow;
    # for the unit test we set it directly.
    pkg.target_by_name("SwiftOnly").language = Language.SWIFT  # type: ignore[union-attr]
    pkg.target_by_name("ObjCOnly").language = Language.OBJC    # type: ignore[union-attr]

    config = Config(package_source="/fake", user_version="")
    plan = plan_source_build(config, pkg)

    by_name = {bu.name: bu for bu in plan.build_units}
    _assert(by_name["SwiftLib"].language == Language.SWIFT, f"SwiftLib: {by_name['SwiftLib'].language}")
    _assert(by_name["ObjCLib"].language == Language.OBJC, f"ObjCLib: {by_name['ObjCLib'].language}")
    _assert(
        by_name["MixedLib"].language == Language.MIXED,
        f"MixedLib: {by_name['MixedLib'].language}",
    )


# --- Prepare self-tests ---------------------------------------------------
#
# Two layers:
#   1. String-level unit tests for the manifest editors. Fast (no swift).
#   2. Round-trip integration tests for the editors + planner edits. Each
#      writes a real Package.swift to a temp dir, applies edits, runs real
#      `swift package dump-package`, and asserts the post-edit product
#      list matches expectations. Gated by `requires_swift=True`.

# Real Package.swift fixtures, embedded as Python literals so the tests
# don't depend on a network or a checked-out clone of GRDB / Stripe / etc.
# These are the EXACT contents fetched from the upstream repos at the
# pinned versions used by the swift-dotnet-packages matrix; their hazards
# are documented in REWRITE_DESIGN.md and the Session 2 brief.

# GRDB v7.9.0: three .library() declarations on the same target. The
# session-2 brief calls out the hazard explicitly: edit_force_dynamic must
# locate by exact `name: "GRDB"`, not by walking forward from the first
# `.library(`, or it will edit GRDB-dynamic instead.
GRDB_PACKAGE_SWIFT_FIXTURE = '''// swift-tools-version:6.1
// The swift-tools-version declares the minimum version of Swift required to build this package.

import Foundation
import PackageDescription

let package = Package(
    name: "GRDB",
    defaultLocalization: "en", // for tests
    platforms: [
        .iOS(.v13),
        .macOS(.v10_15),
        .tvOS(.v13),
        .watchOS(.v7),
    ],
    products: [
        .library(name: "GRDBSQLite", targets: ["GRDBSQLite"]),
        .library(name: "GRDB", targets: ["GRDB"]),
        .library(name: "GRDB-dynamic", type: .dynamic, targets: ["GRDB"]),
    ],
    targets: [
        .systemLibrary(
            name: "GRDBSQLite",
            providers: [.apt(["libsqlite3-dev"])]),
        .target(
            name: "GRDB",
            dependencies: [
                .target(name: "GRDBSQLite"),
            ],
            path: "GRDB"),
    ]
)
'''

# Alamofire 5.10.2: same target backs both `Alamofire` (automatic) and
# `AlamofireDynamic` (already-dynamic). Tests that the editor doesn't
# double-patch the already-dynamic one and doesn't accidentally clobber it
# while editing the regular one.
ALAMOFIRE_PACKAGE_SWIFT_FIXTURE = '''// swift-tools-version: 6.0
import PackageDescription

let package = Package(name: "Alamofire",
                      platforms: [.macOS(.v10_13),
                                  .iOS(.v12),
                                  .tvOS(.v12),
                                  .watchOS(.v4)],
                      products: [
                          .library(name: "Alamofire", targets: ["Alamofire"]),
                          .library(name: "AlamofireDynamic", type: .dynamic, targets: ["Alamofire"])
                      ],
                      targets: [.target(name: "Alamofire",
                                        path: "Source")])
'''

# Stripe 25.6.2 (trimmed): the Stripe library is buried inside several
# multi-line `.library(...)` and `.target(...)` calls with nested arrays.
# The full real manifest has 14 .library entries; this trimmed version
# preserves the multi-line shape and the no-trailing-comma `]` close.
# Targets included only by reference; dump-package doesn't validate paths.
STRIPE_PACKAGE_SWIFT_FIXTURE = '''// swift-tools-version:5.7
import PackageDescription

let package = Package(
    name: "Stripe",
    defaultLocalization: "en",
    platforms: [
        .iOS(.v13)
    ],
    products: [
        .library(
            name: "Stripe",
            targets: ["Stripe"]
        ),
        .library(
            name: "StripePayments",
            targets: ["StripePayments"]
        ),
        .library(
            name: "StripeFinancialConnections",
            targets: ["StripeFinancialConnections"]
        ),
        .library(
            name: "StripeConnect",
            targets: ["StripeConnect"]
        )
    ],
    targets: [
        .target(
            name: "Stripe",
            dependencies: ["StripeCore", "StripePayments"],
            path: "Stripe/StripeiOS"
        ),
        .target(
            name: "StripeCore",
            path: "StripeCore/StripeCore"
        ),
        .target(
            name: "StripePayments",
            dependencies: ["StripeCore"],
            path: "StripePayments/StripePayments"
        ),
        .target(
            name: "StripeFinancialConnections",
            dependencies: ["StripeCore"],
            path: "StripeFinancialConnections/StripeFinancialConnections"
        ),
        .target(
            name: "StripeConnect",
            dependencies: ["StripeCore", "StripeFinancialConnections"],
            path: "StripeConnect/StripeConnect"
        )
    ]
)
'''

# A small fixture that mixes a system library with a regular target. The
# planner skips the system product entirely and Prepare must therefore
# leave it untouched. Used to confirm the validator doesn't false-positive
# on packages that contain system targets.
SYSTEM_LIB_PACKAGE_SWIFT_FIXTURE = '''// swift-tools-version:5.7
import PackageDescription

let package = Package(
    name: "MixedSystem",
    platforms: [.iOS(.v13)],
    products: [
        .library(name: "Sqlite3", targets: ["Sqlite3"]),
        .library(name: "Wrapper", targets: ["Wrapper"]),
    ],
    targets: [
        .systemLibrary(
            name: "Sqlite3",
            providers: [.apt(["libsqlite3-dev"])]),
        .target(
            name: "Wrapper",
            dependencies: ["Sqlite3"],
            path: "Sources/Wrapper"),
    ]
)
'''


def _selftest_balanced_close_basic() -> None:
    s = "(abc)"
    _assert(_balanced_close(s, 0) == 4, f"_balanced_close basic: {_balanced_close(s, 0)}")
    s = "((a)(b))"
    _assert(_balanced_close(s, 0) == 7, f"nested: {_balanced_close(s, 0)}")
    s = "(a)(b)"
    _assert(_balanced_close(s, 0) == 2, f"first call ends at 2")
    _assert(_balanced_close(s, 3) == 5, f"second call")
    # Mismatched
    _assert(_balanced_close("(a", 0) == -1, "no close → -1")


def _selftest_balanced_close_strings() -> None:
    # String literals containing parens must be skipped.
    s = '(name: "foo)bar")'
    _assert(_balanced_close(s, 0) == len(s) - 1,
            f"string-with-paren: {_balanced_close(s, 0)} vs {len(s)-1}")
    # Escaped quotes inside strings must not end the string early.
    s = '(name: "a\\"b)c", x: 1)'
    _assert(_balanced_close(s, 0) == len(s) - 1,
            f"escaped-quote: {_balanced_close(s, 0)} vs {len(s)-1}")


def _selftest_balanced_close_comments() -> None:
    # Block comment with a `)` inside.
    s = "(a /* ) */ b)"
    _assert(_balanced_close(s, 0) == len(s) - 1,
            f"block-comment-with-paren: {_balanced_close(s, 0)} vs {len(s)-1}")
    # Line comment with a `)` inside, terminated by newline.
    s = "(a // ) ignored\n  b)"
    _assert(_balanced_close(s, 0) == len(s) - 1,
            f"line-comment-with-paren: {_balanced_close(s, 0)} vs {len(s)-1}")
    # Swift block comments NEST. The first `*/` must NOT terminate the
    # outer comment, otherwise a `)` sitting between the inner close
    # and the outer close would be counted as a real paren and the
    # walker would either return the wrong index or fail to find a
    # match. Regression for the bug where `text.find("*/", ...)` made
    # the first close stop the scan.
    s = "(a /* outer /* inner */ ) still */ b)"
    _assert(_balanced_close(s, 0) == len(s) - 1,
            f"nested-block-comment: {_balanced_close(s, 0)} vs {len(s)-1}")
    # Deeply nested.
    s = "(a /* /* /* deep ) */ ) */ ) */ b)"
    _assert(_balanced_close(s, 0) == len(s) - 1,
            f"deeply-nested-block-comment: {_balanced_close(s, 0)} vs {len(s)-1}")
    # Unterminated nested block comment → -1, not a wrong-index match.
    s = "(a /* outer /* inner */ b)"
    _assert(_balanced_close(s, 0) == -1,
            f"unterminated-nested-comment should return -1, got "
            f"{_balanced_close(s, 0)}")


# ---------------------------------------------------------------------------
# Dedup-overlap helpers (compute_internal_target_deps, topo_order_units,
# edit_replace_with_binary_target). These exist so umbrella products
# don't statically embed sibling targets' Mach-O when those siblings
# also ship as their own xcframeworks. See REWRITE_DESIGN.md §5.4.
# ---------------------------------------------------------------------------


def _stripe_dump_package(snapshot: dict = None) -> "tool.Package":
    """Build a typed Package from STRIPE_DUMP_SNAPSHOT (or override) so
    helper tests don't have to repeat the parse + Package() boilerplate.
    """
    snap = snapshot or STRIPE_DUMP_SNAPSHOT
    raw, products, targets, platforms, name, tv = _parse_dump(snap)
    return tool.Package(
        name=name,
        tools_version=tv,
        platforms=platforms,
        products=products,
        targets=targets,
        schemes=[],
        raw_dump=raw,
        staged_dir=Path("/tmp/dedup-overlap-fixture"),
    )


def _selftest_compute_internal_target_deps_stripe() -> None:
    """Stripe's umbrella target reaches every sibling transitively;
    StripeCore is a leaf; the binary target Stripe3DS2 has no internal
    deps and isn't reached from anything (it's only mentioned as a
    package-product dep in the real manifest, which we filter out)."""
    pkg = _stripe_dump_package()
    deps = tool.compute_internal_target_deps(pkg)
    _assert(deps["StripeCore"] == set(),
            f"StripeCore should be a leaf, got {deps['StripeCore']!r}")
    _assert(deps["StripeUICore"] == {"StripeCore"},
            f"StripeUICore deps should be {{StripeCore}}, got {deps['StripeUICore']!r}")
    _assert(deps["StripePayments"] == {"StripeCore"},
            f"StripePayments deps wrong, got {deps['StripePayments']!r}")
    _assert(deps["Stripe"] == {"StripeCore", "StripePayments", "StripeApplePay"} or
            deps["Stripe"] >= {"StripeCore", "StripePayments"},
            f"Stripe deps wrong, got {deps['Stripe']!r}")
    _assert(deps["Stripe3DS2"] == set(),
            f"Stripe3DS2 should be a leaf, got {deps['Stripe3DS2']!r}")
    # Self-edges must never appear.
    for t, ds in deps.items():
        _assert(t not in ds, f"{t} should not be in its own dep set: {ds!r}")


def _selftest_compute_internal_target_deps_filters_external_products() -> None:
    """`product` deps reference cross-package targets we can't reach.
    They must be filtered out — only same-package target names survive
    in the returned closures."""
    snap = {
        "name": "external-deps",
        "toolsVersion": {"_version": "5.7.0"},
        "platforms": [{"options": [], "platformName": "ios", "version": "13.0"}],
        "products": [
            {"name": "Foo", "type": {"library": ["automatic"]}, "targets": ["Foo"]},
        ],
        "targets": [
            {"name": "Foo", "type": "regular", "path": "Foo", "publicHeadersPath": None,
             "dependencies": [
                 {"byName": ["Bar", None]},               # internal: kept
                 {"product": ["Alamofire", "alamofire", None, None]},  # external: dropped
             ]},
            {"name": "Bar", "type": "regular", "path": "Bar", "publicHeadersPath": None,
             "dependencies": []},
        ],
    }
    pkg = _stripe_dump_package(snap)
    deps = tool.compute_internal_target_deps(pkg)
    _assert(deps["Foo"] == {"Bar"},
            f"only Bar should be reachable, got {deps['Foo']!r}")


def _selftest_compute_internal_target_deps_tolerates_cycle() -> None:
    """A cycle in the dump-package payload must not infinite-loop —
    the function returns a finite set per node, even if SPM would
    reject the manifest at build time."""
    snap = {
        "name": "cycle",
        "toolsVersion": {"_version": "5.7.0"},
        "platforms": [{"options": [], "platformName": "ios", "version": "13.0"}],
        "products": [],
        "targets": [
            {"name": "A", "type": "regular", "path": "A", "publicHeadersPath": None,
             "dependencies": [{"byName": ["B", None]}]},
            {"name": "B", "type": "regular", "path": "B", "publicHeadersPath": None,
             "dependencies": [{"byName": ["A", None]}]},
        ],
    }
    pkg = _stripe_dump_package(snap)
    deps = tool.compute_internal_target_deps(pkg)
    # Each node only sees the OTHER, not itself; cycle is broken cleanly.
    _assert(deps["A"] == {"B"}, f"A deps {deps['A']!r}")
    _assert(deps["B"] == {"A"}, f"B deps {deps['B']!r}")


def _selftest_topo_order_units_stripe_umbrella_last() -> None:
    """The whole point of the helper: Stripe (umbrella) MUST be ordered
    after every sibling target it transitively depends on. The original
    planner order may interleave; the topo sort must place leaves first
    even if they appear later in the planner's emission order."""
    pkg = _stripe_dump_package()
    BU = tool.BuildUnit
    # Original order: Stripe (umbrella) FIRST, the ones it depends on
    # AFTER. The topo sort must re-order so leaves precede the umbrella.
    units = [
        BU(name="Stripe", scheme="Stripe", framework_name="Stripe",
           language="Swift", archive_strategy="archive", source_targets=["Stripe"]),
        BU(name="StripePayments", scheme="StripePayments",
           framework_name="StripePayments", language="Swift",
           archive_strategy="archive", source_targets=["StripePayments"]),
        BU(name="StripeUICore", scheme="StripeUICore",
           framework_name="StripeUICore", language="Swift",
           archive_strategy="archive", source_targets=["StripeUICore"]),
        BU(name="StripeCore", scheme="StripeCore",
           framework_name="StripeCore", language="Swift",
           archive_strategy="archive", source_targets=["StripeCore"]),
        BU(name="StripePaymentSheet", scheme="StripePaymentSheet",
           framework_name="StripePaymentSheet", language="Swift",
           archive_strategy="archive", source_targets=["StripePaymentSheet"]),
    ]
    ordered = tool.topo_order_units(units, pkg)
    names = [u.name for u in ordered]
    _assert(set(names) == {u.name for u in units},
            f"topo sort lost units: {names!r}")
    # Every dep must come before Stripe / PaymentSheet (which depend on it).
    idx = {n: i for i, n in enumerate(names)}
    _assert(idx["StripeCore"] < idx["Stripe"],
            f"StripeCore must precede Stripe: {names}")
    _assert(idx["StripeCore"] < idx["StripePaymentSheet"],
            f"StripeCore must precede StripePaymentSheet: {names}")
    _assert(idx["StripeUICore"] < idx["StripePaymentSheet"],
            f"StripeUICore must precede StripePaymentSheet: {names}")
    _assert(idx["StripePayments"] < idx["Stripe"],
            f"StripePayments must precede Stripe: {names}")
    _assert(idx["StripePayments"] < idx["StripePaymentSheet"],
            f"StripePayments must precede StripePaymentSheet: {names}")


def _selftest_topo_order_units_stable_on_independent_units() -> None:
    """Units with no internal-dep relation between them must keep their
    planner-assigned order. The sort is stable on original index, not a
    spurious alphabetic shuffle."""
    snap = {
        "name": "indep",
        "toolsVersion": {"_version": "5.7.0"},
        "platforms": [{"options": [], "platformName": "ios", "version": "13.0"}],
        "products": [],
        "targets": [
            {"name": "Zebra", "type": "regular", "path": "Zebra",
             "publicHeadersPath": None, "dependencies": []},
            {"name": "Apple", "type": "regular", "path": "Apple",
             "publicHeadersPath": None, "dependencies": []},
            {"name": "Mango", "type": "regular", "path": "Mango",
             "publicHeadersPath": None, "dependencies": []},
        ],
    }
    pkg = _stripe_dump_package(snap)
    BU = tool.BuildUnit
    units = [
        BU(name="Zebra", scheme="Zebra", framework_name="Zebra",
           language="Swift", archive_strategy="archive", source_targets=["Zebra"]),
        BU(name="Apple", scheme="Apple", framework_name="Apple",
           language="Swift", archive_strategy="archive", source_targets=["Apple"]),
        BU(name="Mango", scheme="Mango", framework_name="Mango",
           language="Swift", archive_strategy="archive", source_targets=["Mango"]),
    ]
    ordered = [u.name for u in tool.topo_order_units(units, pkg)]
    _assert(ordered == ["Zebra", "Apple", "Mango"],
            f"stable order broken: {ordered!r}")


def _selftest_find_target_call_for_name_skips_binary_target() -> None:
    """`.binaryTarget(name: T, ...)` must NEVER be returned by the
    target-call finder — it's not a target we can rewrite, and the
    idempotency contract of edit_replace_with_binary_target depends on
    this distinction."""
    text = '''
let package = Package(
    name: "x",
    targets: [
        .binaryTarget(name: "Foo", path: "Foo.xcframework"),
        .target(name: "Bar", path: "Bar"),
    ]
)
'''
    # Foo only exists as a binaryTarget — the finder must miss it.
    ks, ce, kind = tool._find_target_call_for_name(text, "Foo")
    _assert(ks == -1 and ce == -1 and kind == "",
            f"binaryTarget should not match _find_target_call_for_name, "
            f"got ({ks}, {ce}, {kind!r})")
    # Bar is a real .target — finder picks it up.
    ks2, ce2, kind2 = tool._find_target_call_for_name(text, "Bar")
    _assert(ks2 != -1 and kind2 == "target",
            f"Bar (.target) should match, got ({ks2}, {ce2}, {kind2!r})")


def _selftest_edit_replace_with_binary_target_basic() -> None:
    """Stripe-shape: rewrite the StripeCore target to a binaryTarget
    pointing at a freshly-built xcframework. The other targets and the
    products array are left untouched."""
    text = STRIPE_PACKAGE_SWIFT_FIXTURE
    out = edit_replace_with_binary_target(
        text, "StripeCore", "/build/StripeCore.xcframework"
    )
    # New binaryTarget present.
    _assert(
        '.binaryTarget(name: "StripeCore", path: "/build/StripeCore.xcframework")' in out,
        f"binaryTarget for StripeCore missing in:\n{out}",
    )
    # Original .target(name: "StripeCore", ...) is gone.
    # (Other StripeCore mentions — as a string in a deps list — must remain.)
    _assert(".target(\n            name: \"StripeCore\"" not in out,
            "old .target(name: StripeCore, ...) block was not removed")
    # Sibling targets that mention StripeCore as a dep are still present.
    _assert(
        '.target(\n            name: "StripePayments",\n            dependencies: ["StripeCore"]' in out,
        "StripePayments target should still reference StripeCore in deps",
    )


def _selftest_edit_replace_with_binary_target_idempotent() -> None:
    """Running the editor twice on the same target name must be a
    no-op the second time (the manifest already has a binaryTarget
    with that name)."""
    text = STRIPE_PACKAGE_SWIFT_FIXTURE
    once = edit_replace_with_binary_target(text, "StripeCore", "/X.xcframework")
    twice = edit_replace_with_binary_target(once, "StripeCore", "/X.xcframework")
    _assert(once == twice, "second edit was not a no-op")


def _selftest_edit_replace_with_binary_target_path_escaping() -> None:
    """Paths containing characters that are syntactically meaningful
    inside a Swift string literal (backslash, double-quote) must be
    escaped — otherwise the resulting Package.swift won't parse."""
    text = '''let p = Package(
    name: "x",
    targets: [.target(name: "T", path: "T")]
)
'''
    nasty = '/tmp/with"quote"/and\\backslash/T.xcframework'
    out = edit_replace_with_binary_target(text, "T", nasty)
    # The literal must contain escaped \" and escaped backslash.
    expected = (
        r'.binaryTarget(name: "T", path: "/tmp/with\"quote\"/and\\backslash/T.xcframework")'
    )
    _assert(expected in out,
            f"path escaping failed.\nWanted substring: {expected!r}\nGot:\n{out}")


def _selftest_edit_replace_with_binary_target_multiline() -> None:
    """`.target(...)` declarations span multiple lines in real
    manifests. The editor must consume the whole multi-line span (up to
    the matching `)`) and not accidentally leave a trailing fragment."""
    text = STRIPE_PACKAGE_SWIFT_FIXTURE
    out = edit_replace_with_binary_target(
        text, "StripePayments", "/build/StripePayments.xcframework"
    )
    # No leftover fragments of the old .target body.
    _assert("dependencies: [\"StripeCore\"]," not in out
            or out.count("dependencies: [\"StripeCore\"]") < text.count("dependencies: [\"StripeCore\"]"),
            "multi-line .target body fragment leaked into output")
    _assert(
        '.binaryTarget(name: "StripePayments", path: "/build/StripePayments.xcframework")' in out,
        "binaryTarget for StripePayments missing",
    )
    # The .target call we replaced is no longer present.
    _assert(".target(\n            name: \"StripePayments\"" not in out,
            "old multi-line .target(name: StripePayments, ...) was not removed cleanly")


def _selftest_edit_replace_with_binary_target_unknown_raises() -> None:
    """Asking the editor to substitute a target that's not declared
    anywhere (and isn't already a binaryTarget) must raise — the call
    site is acting on stale planner data and we want a loud failure."""
    text = STRIPE_PACKAGE_SWIFT_FIXTURE
    raised = False
    try:
        edit_replace_with_binary_target(text, "DoesNotExist", "/X.xcframework")
    except PrepareUserError:
        raised = True
    _assert(raised, "expected PrepareUserError for unknown target")


def _selftest_find_target_call_for_name_ignores_dependency_target_refs() -> None:
    """[Codex P1 regression] A target whose `dependencies:` array uses
    explicit `.target(name: "Foo")` syntax must NOT cause the finder
    to match the OUTER target's `.target(...)` call when searching
    for "Foo". The depth-0 `name:` check ensures only the outer
    target's own argument list is considered.
    """
    src = '''// swift-tools-version:5.7
import PackageDescription

let p = Package(
    name: "x",
    targets: [
        .target(
            name: "A",
            dependencies: [.target(name: "Foo")],
            path: "A"
        ),
        .target(
            name: "Foo",
            path: "Foo"
        ),
    ]
)
'''
    ks, ce, kind = tool._find_target_call_for_name(src, "Foo")
    located = src[ks:ce + 1]
    _assert(kind == "target", f"kind was {kind!r}")
    # The located call MUST be Foo's declaration (which has path: "Foo"),
    # NOT A's declaration (which has dependencies: [.target(...)]).
    _assert('name: "Foo"' in located,
            f"located span missing name: \"Foo\":\n{located}")
    _assert('path: "Foo"' in located,
            f"located span missing path: \"Foo\":\n{located}")
    _assert('name: "A"' not in located,
            f"located span wrongly includes A's declaration:\n{located}")
    _assert("dependencies:" not in located,
            f"located span wrongly includes dependencies block:\n{located}")


def _selftest_edit_replace_with_binary_target_dep_ref_does_not_clobber_sibling() -> None:
    """[Codex P1 regression — end-to-end] Substituting Foo in a
    manifest where target A depends on Foo via the explicit
    `.target(name: "Foo")` expression must rewrite ONLY the Foo
    declaration. A's declaration must remain intact, including its
    dependency reference to Foo."""
    src = '''// swift-tools-version:5.7
import PackageDescription

let p = Package(
    name: "x",
    targets: [
        .target(
            name: "A",
            dependencies: [.target(name: "Foo")],
            path: "A"
        ),
        .target(
            name: "Foo",
            path: "Foo"
        ),
    ]
)
'''
    out = edit_replace_with_binary_target(src, "Foo", "/build/Foo.xcframework")
    # Foo became a binaryTarget.
    _assert('.binaryTarget(name: "Foo", path: "/build/Foo.xcframework")' in out,
            f"binaryTarget for Foo missing:\n{out}")
    # A's outer declaration is preserved verbatim.
    _assert('.target(\n            name: "A",\n            dependencies: '
            '[.target(name: "Foo")],\n            path: "A"\n        )' in out,
            f"A's declaration was clobbered or modified:\n{out}")
    # The OLD `.target(...)` block for Foo (which had `path: "Foo"`) is gone.
    _assert('.target(\n            name: "Foo",\n            path: "Foo"\n'
            '        )' not in out,
            f"old Foo .target() block was not removed:\n{out}")
    # Idempotent across the false-positive hazard too.
    twice = edit_replace_with_binary_target(out, "Foo", "/build/Foo.xcframework")
    _assert(out == twice,
            "second edit through dep-ref manifest was not a no-op")


SWIFT_COLLECTIONS_WRAPPER_FIXTURE = '''// swift-tools-version:5.7
import PackageDescription

struct CustomTarget {
  enum Kind { case exported, hidden, test }
  static func target(kind: Kind, name: String, dependencies: [Target.Dependency] = []) -> CustomTarget {
    CustomTarget()
  }
  func toTarget() -> Target { Target.target(name: "x") }
}

let targets: [CustomTarget] = [
  .target(kind: .exported, name: "BitCollections"),
  .target(kind: .exported, name: "DequeModule"),
  .target(kind: .exported, name: "Collections", dependencies: ["BitCollections", "DequeModule"]),
]

let _targets: [Target] = targets.map { $0.toTarget() }

let package = Package(
  name: "swift-collections",
  products: [.library(name: "Collections", targets: ["Collections"])],
  targets: _targets
)
'''


def _selftest_overlay_first_call_injects_block_and_wraps_targets_arg() -> None:
    """[swift-collections] First overlay call on a wrapper-style manifest
    must (a) inject the sentinel-wrapped overlay block before the
    Package(...) constructor, (b) wrap the `targets:` argument
    expression in a filter+append, and (c) NOT mutate the original
    target literal — the wrapper stays exactly as the package author
    wrote it."""
    text = SWIFT_COLLECTIONS_WRAPPER_FIXTURE
    out = edit_inject_or_extend_overlay_binary_targets(
        text,
        [("BitCollections", "./BitCollections.xcframework")],
    )
    _assert(
        "spm-to-xcframework dedup-overlap overlay — begin" in out,
        f"overlay begin sentinel missing:\n{out}",
    )
    _assert(
        "spm-to-xcframework dedup-overlap overlay — end" in out,
        f"overlay end sentinel missing:\n{out}",
    )
    _assert(
        'Target.binaryTarget(name: "BitCollections", path: "./BitCollections.xcframework")'
        in out,
        f"BitCollections binaryTarget missing in:\n{out}",
    )
    _assert(
        "let _SPM2XC_OVERLAY_TARGETS: [Target] = [" in out,
        "overlay targets declaration missing",
    )
    _assert(
        "let _SPM2XC_OVERLAY_NAMES: Set<String> = Set(_SPM2XC_OVERLAY_TARGETS.map { $0.name })"
        in out,
        "overlay names declaration missing",
    )
    _assert(
        ").filter { !_SPM2XC_OVERLAY_NAMES.contains($0.name) } + _SPM2XC_OVERLAY_TARGETS"
        in out,
        f"targets: arg was not wrapped with the filter+append:\n{out}",
    )
    # Original wrapper expressions untouched.
    _assert(
        '.target(kind: .exported, name: "BitCollections")' in out,
        "original CustomTarget call for BitCollections was clobbered",
    )
    _assert(
        "targets.map { $0.toTarget() }" in out,
        "wrapper-driven _targets expression was clobbered",
    )
    # Overlay block lands BEFORE the Package(...) line.
    overlay_idx = out.find("let _SPM2XC_OVERLAY_TARGETS")
    pkg_idx = out.find("let package = Package(")
    _assert(
        0 < overlay_idx < pkg_idx,
        "overlay block must precede the `let package = Package(...)` statement",
    )


def _selftest_overlay_second_call_extends_block_no_rewrap() -> None:
    """A subsequent overlay call must (a) find the existing sentinel
    block and extend its array with new entries, (b) leave the
    `targets:` wrap alone (one wrap, not two), and (c) preserve
    earlier entries verbatim."""
    text = SWIFT_COLLECTIONS_WRAPPER_FIXTURE
    once = edit_inject_or_extend_overlay_binary_targets(
        text,
        [("BitCollections", "./BitCollections.xcframework")],
    )
    twice = edit_inject_or_extend_overlay_binary_targets(
        once,
        [("DequeModule", "./DequeModule.xcframework")],
    )
    # Both entries present in the overlay array.
    _assert(
        'Target.binaryTarget(name: "BitCollections", path: "./BitCollections.xcframework")'
        in twice,
        "first overlay entry (BitCollections) lost after extension",
    )
    _assert(
        'Target.binaryTarget(name: "DequeModule", path: "./DequeModule.xcframework")'
        in twice,
        "second overlay entry (DequeModule) not added",
    )
    # Exactly one wrap (the filter+append must NOT be applied twice — a
    # double wrap would mean nested `.filter { ... }.filter { ... }`).
    wrap_count = twice.count(".filter { !_SPM2XC_OVERLAY_NAMES.contains($0.name) }")
    _assert(
        wrap_count == 1,
        f"targets: argument was wrapped {wrap_count} times — expected exactly 1",
    )
    # Exactly one sentinel pair.
    _assert(
        twice.count("spm-to-xcframework dedup-overlap overlay — begin") == 1,
        "more than one overlay begin sentinel in the manifest",
    )
    _assert(
        twice.count("spm-to-xcframework dedup-overlap overlay — end") == 1,
        "more than one overlay end sentinel in the manifest",
    )


def _selftest_overlay_idempotent_same_substitution() -> None:
    """Re-applying the same substitution must be a no-op — the editor
    parses the existing entries, finds the requested (name, path) pair
    already present, and returns the manifest unchanged."""
    text = SWIFT_COLLECTIONS_WRAPPER_FIXTURE
    once = edit_inject_or_extend_overlay_binary_targets(
        text,
        [("BitCollections", "./BitCollections.xcframework")],
    )
    twice = edit_inject_or_extend_overlay_binary_targets(
        once,
        [("BitCollections", "./BitCollections.xcframework")],
    )
    _assert(once == twice, "idempotent re-apply changed the manifest")


def _selftest_overlay_last_path_wins_on_collision() -> None:
    """If the same target appears in successive overlay calls with
    different paths, the LAST path wins (the dict merge semantics).
    This shouldn't happen in production — the same xcframework is
    referenced once per build — but the contract should be
    deterministic."""
    text = SWIFT_COLLECTIONS_WRAPPER_FIXTURE
    once = edit_inject_or_extend_overlay_binary_targets(
        text, [("BitCollections", "./old.xcframework")]
    )
    twice = edit_inject_or_extend_overlay_binary_targets(
        once, [("BitCollections", "./new.xcframework")]
    )
    _assert(
        'Target.binaryTarget(name: "BitCollections", path: "./new.xcframework")'
        in twice,
        "later path didn't replace earlier path on collision",
    )
    _assert(
        "./old.xcframework" not in twice,
        f"old path survived the collision:\n{twice}",
    )


def _selftest_overlay_sorted_output_is_deterministic() -> None:
    """The overlay block's entries must render in stable (sorted) order
    so the audit log and `.original-Package.swift` diff is the same
    across runs regardless of substitution order."""
    text = SWIFT_COLLECTIONS_WRAPPER_FIXTURE
    a_then_b = edit_inject_or_extend_overlay_binary_targets(
        text,
        [
            ("DequeModule", "./Deque.xcframework"),
            ("BitCollections", "./Bit.xcframework"),
        ],
    )
    b_then_a = edit_inject_or_extend_overlay_binary_targets(
        text,
        [
            ("BitCollections", "./Bit.xcframework"),
            ("DequeModule", "./Deque.xcframework"),
        ],
    )
    _assert(a_then_b == b_then_a, "overlay output depends on substitution order")
    # BitCollections sorts before DequeModule.
    bit_idx = a_then_b.find('name: "BitCollections"')
    deque_idx = a_then_b.find('name: "DequeModule"')
    _assert(
        0 < bit_idx < deque_idx,
        "overlay entries are not sorted alphabetically by name",
    )


def _selftest_overlay_escapes_path_correctly() -> None:
    """A path containing characters that break a Swift string literal
    (backslash, double-quote) must be escaped in the overlay entry —
    same contract as `edit_replace_with_binary_target`."""
    text = SWIFT_COLLECTIONS_WRAPPER_FIXTURE
    nasty = '/tmp/with"quote"/and\\backslash/T.xcframework'
    out = edit_inject_or_extend_overlay_binary_targets(text, [("BitCollections", nasty)])
    expected = (
        r'Target.binaryTarget(name: "BitCollections", '
        r'path: "/tmp/with\"quote\"/and\\backslash/T.xcframework")'
    )
    _assert(
        expected in out,
        f"overlay path escaping wrong.\nWanted: {expected}\nGot:\n{out}",
    )


def _selftest_overlay_no_substitutions_no_overlay_is_noop() -> None:
    """Calling with an empty substitution list AND no existing overlay
    must return the manifest unchanged — there's nothing to do."""
    text = SWIFT_COLLECTIONS_WRAPPER_FIXTURE
    out = edit_inject_or_extend_overlay_binary_targets(text, [])
    _assert(out == text, "empty-substitution call mutated the manifest")


def _selftest_overlay_missing_targets_arg_raises() -> None:
    """A manifest without a top-level `targets:` argument can't be
    overlay-edited — raise loudly so the caller knows the fallback
    path doesn't apply."""
    src = '''import PackageDescription
let package = Package(name: "x", products: [], dependencies: [])
'''
    raised = False
    try:
        edit_inject_or_extend_overlay_binary_targets(
            src, [("Foo", "./Foo.xcframework")]
        )
    except PrepareUserError:
        raised = True
    _assert(raised, "expected PrepareUserError when targets: arg is absent")


def _selftest_cross_sibling_product_expansion(tmp_root: Path) -> None:
    """Sibling A references product P of sibling B via `.product(...)`,
    but the umbrella never names P. The orchestrator's
    `_expand_cross_sibling_referenced_products` must merge P into B's
    `referenced_products` so the orchestrator builds B's
    P.xcframework — without it, A.swiftinterface dangles on `import P`
    when consumed downstream.
    """
    work = tmp_root / "cross-sibling"
    work.mkdir(parents=True, exist_ok=True)
    sibling_a_dir = work / "swift-case-paths"
    sibling_b_dir = work / "xctest-dynamic-overlay"
    sibling_a_dir.mkdir(parents=True, exist_ok=True)
    sibling_b_dir.mkdir(parents=True, exist_ok=True)
    # A imports both `IssueReporting` and `XCTestDynamicOverlay` from B.
    (sibling_a_dir / "Package.swift").write_text(
        '''// swift-tools-version: 5.9
import PackageDescription
let package = Package(
  name: "swift-case-paths",
  products: [.library(name: "CasePaths", targets: ["CasePaths"])],
  dependencies: [
    .package(url: "https://github.com/pointfreeco/xctest-dynamic-overlay", from: "1.2.2"),
  ],
  targets: [
    .target(
      name: "CasePaths",
      dependencies: [
        .product(name: "IssueReporting", package: "xctest-dynamic-overlay"),
        .product(name: "XCTestDynamicOverlay", package: "xctest-dynamic-overlay"),
      ]
    ),
  ]
)
'''
    )
    (sibling_b_dir / "Package.swift").write_text(
        '''// swift-tools-version: 5.9
import PackageDescription
let package = Package(
  name: "xctest-dynamic-overlay",
  products: [
    .library(name: "IssueReporting", targets: ["IssueReporting"]),
    .library(name: "XCTestDynamicOverlay", targets: ["XCTestDynamicOverlay"]),
  ],
  targets: [
    .target(name: "IssueReporting"),
    .target(name: "XCTestDynamicOverlay"),
  ]
)
'''
    )

    Tp = tool.TransitivePackageInfo
    Product = tool.Product
    tp_a = Tp(
        identity="swift-case-paths",
        checkout_path=sibling_a_dir,
        products=[
            Product(name="CasePaths", linkage="automatic", targets=["CasePaths"]),
        ],
        tools_version="5.9",
        referenced_products=["CasePaths"],
    )
    tp_b = Tp(
        identity="xctest-dynamic-overlay",
        checkout_path=sibling_b_dir,
        products=[
            Product(name="IssueReporting", linkage="automatic", targets=["IssueReporting"]),
            Product(name="XCTestDynamicOverlay", linkage="automatic", targets=["XCTestDynamicOverlay"]),
        ],
        tools_version="5.9",
        referenced_products=["IssueReporting"],  # umbrella only references IssueReporting
    )

    expanded = tool._expand_cross_sibling_referenced_products([tp_a, tp_b])
    by_ident = {tp.identity: tp for tp in expanded}
    _assert(
        sorted(by_ident["xctest-dynamic-overlay"].referenced_products)
        == ["IssueReporting", "XCTestDynamicOverlay"],
        f"expected XCTestDynamicOverlay added to xctest-dynamic-overlay; "
        f"got {by_ident['xctest-dynamic-overlay'].referenced_products}",
    )
    # The sibling A's products should be untouched (no other sibling refs A).
    _assert(
        by_ident["swift-case-paths"].referenced_products == ["CasePaths"],
        f"unexpected change to swift-case-paths: "
        f"{by_ident['swift-case-paths'].referenced_products}",
    )

    # Idempotency: re-running on the already-expanded list should be stable.
    again = tool._expand_cross_sibling_referenced_products(expanded)
    again_by_ident = {tp.identity: tp for tp in again}
    _assert(
        sorted(again_by_ident["xctest-dynamic-overlay"].referenced_products)
        == ["IssueReporting", "XCTestDynamicOverlay"],
        "expansion is not idempotent",
    )


def _selftest_cross_sibling_expansion_ignores_unknown_packages(
    tmp_root: Path,
) -> None:
    """A `.product(name: X, package: Y)` ref whose Y is NOT in our
    transitives must be ignored — we only build siblings we already
    know about; foreign refs are SPM's job to resolve.
    """
    work = tmp_root / "cross-sibling-unknown"
    work.mkdir(parents=True, exist_ok=True)
    only_dir = work / "swift-case-paths"
    only_dir.mkdir(parents=True, exist_ok=True)
    (only_dir / "Package.swift").write_text(
        '''import PackageDescription
let package = Package(
  name: "swift-case-paths",
  targets: [
    .target(
      name: "CasePaths",
      dependencies: [
        .product(name: "Unknown", package: "some-foreign-package"),
      ]
    ),
  ]
)
'''
    )
    Tp = tool.TransitivePackageInfo
    Product = tool.Product
    only = Tp(
        identity="swift-case-paths",
        checkout_path=only_dir,
        products=[
            Product(name="CasePaths", linkage="automatic", targets=["CasePaths"]),
        ],
        tools_version="5.9",
        referenced_products=["CasePaths"],
    )
    expanded = tool._expand_cross_sibling_referenced_products([only])
    _assert(
        len(expanded) == 1
        and expanded[0].referenced_products == ["CasePaths"],
        f"foreign package ref leaked into expansion: "
        f"{expanded[0].referenced_products}",
    )


def _selftest_cross_sibling_expansion_chains_to_fixpoint(
    tmp_root: Path,
) -> None:
    """A → B → C chain: A references B's product, and B references C's
    product. After A → B adds something, the next iteration must pick
    up B → C too.
    """
    work = tmp_root / "cross-sibling-chain"
    work.mkdir(parents=True, exist_ok=True)
    a = work / "a"; a.mkdir(parents=True, exist_ok=True)
    b = work / "b"; b.mkdir(parents=True, exist_ok=True)
    c = work / "c"; c.mkdir(parents=True, exist_ok=True)
    (a / "Package.swift").write_text(
        '''import PackageDescription
let package = Package(
  name: "a",
  targets: [.target(name: "A", dependencies: [.product(name: "B1", package: "b")])]
)
'''
    )
    (b / "Package.swift").write_text(
        '''import PackageDescription
let package = Package(
  name: "b",
  targets: [
    .target(name: "B1"),
    .target(name: "B2", dependencies: [.product(name: "C1", package: "c")]),
  ]
)
'''
    )
    (c / "Package.swift").write_text("// minimal\n")
    Tp = tool.TransitivePackageInfo
    Product = tool.Product
    tp_a = Tp(
        identity="a", checkout_path=a,
        products=[Product(name="A", linkage="automatic", targets=["A"])],
        tools_version="5.9",
        referenced_products=["A"],
    )
    tp_b = Tp(
        identity="b", checkout_path=b,
        products=[
            Product(name="B1", linkage="automatic", targets=["B1"]),
            Product(name="B2", linkage="automatic", targets=["B2"]),
        ],
        tools_version="5.9",
        referenced_products=["B1"],
    )
    tp_c = Tp(
        identity="c", checkout_path=c,
        products=[Product(name="C1", linkage="automatic", targets=["C1"])],
        tools_version="5.9",
        referenced_products=[],
    )
    expanded = tool._expand_cross_sibling_referenced_products([tp_a, tp_b, tp_c])
    by_ident = {tp.identity: tp for tp in expanded}
    # B1 was already referenced; nothing to add for B from A's ref.
    # B's manifest references C's C1; expansion should add C1 to C.
    _assert(
        by_ident["c"].referenced_products == ["C1"],
        f"chained expansion failed; got c.referenced_products="
        f"{by_ident['c'].referenced_products}",
    )


def _selftest_cross_sibling_expansion_ignores_strings_and_comments(
    tmp_root: Path,
) -> None:
    """Cross-sibling expansion must NOT trigger on `.product(...)` text that
    appears inside string literals or comments. Without the code-token-view
    filter, a docstring or test fixture containing `.product(name: ...,
    package: ...)` would inject a non-existent product name into the
    target's `referenced_products` — and Plan would later reject it as an
    unmatched product filter.
    """
    work = tmp_root / "cross-sibling-strings"
    work.mkdir(parents=True, exist_ok=True)
    a_dir = work / "a"; a_dir.mkdir(parents=True, exist_ok=True)
    b_dir = work / "b"; b_dir.mkdir(parents=True, exist_ok=True)
    # A's manifest mentions a `.product(name: "Fake", package: "b")` ref but
    # ONLY inside a comment and a string literal. The real targets only
    # reference "Real".
    (a_dir / "Package.swift").write_text(
        '''// swift-tools-version: 5.9
import PackageDescription
// Example of a cross-sibling ref: .product(name: "Fake", package: "b")
let docs = """
  Use .product(name: "AlsoFake", package: "b") to import sibling products.
"""
let package = Package(
  name: "a",
  targets: [
    .target(name: "A", dependencies: [
      .product(name: "Real", package: "b"),
    ]),
  ]
)
'''
    )
    (b_dir / "Package.swift").write_text(
        '''import PackageDescription
let package = Package(
  name: "b",
  products: [.library(name: "Real", targets: ["Real"])],
  targets: [.target(name: "Real")]
)
'''
    )
    Tp = tool.TransitivePackageInfo
    Product = tool.Product
    tp_a = Tp(
        identity="a", checkout_path=a_dir,
        products=[Product(name="A", linkage="automatic", targets=["A"])],
        tools_version="5.9", referenced_products=["A"],
    )
    tp_b = Tp(
        identity="b", checkout_path=b_dir,
        products=[
            Product(name="Real", linkage="automatic", targets=["Real"]),
            # "Fake" / "AlsoFake" are intentionally NOT declared so the
            # validation arm would also reject them — but the primary
            # defense is the code-token view skipping the matches.
        ],
        tools_version="5.9", referenced_products=[],
    )
    expanded = tool._expand_cross_sibling_referenced_products([tp_a, tp_b])
    b_after = next(t for t in expanded if t.identity == "b")
    _assert(
        b_after.referenced_products == ["Real"],
        f"strings/comments leaked into cross-sibling expansion; "
        f"b.referenced_products={b_after.referenced_products}",
    )


def _selftest_cross_sibling_expansion_drops_undeclared_products(
    tmp_root: Path,
) -> None:
    """If a sibling's manifest references `.product(name: X, package: Y)`
    but Y's actual product list does not include X (drifted manifest,
    aliased target name, etc.), the expansion must NOT add X to Y's
    `referenced_products`. Plan would otherwise reject X as an unmatched
    product filter when the child config runs.
    """
    work = tmp_root / "cross-sibling-undeclared"
    work.mkdir(parents=True, exist_ok=True)
    a_dir = work / "a"; a_dir.mkdir(parents=True, exist_ok=True)
    b_dir = work / "b"; b_dir.mkdir(parents=True, exist_ok=True)
    (a_dir / "Package.swift").write_text(
        '''import PackageDescription
let package = Package(
  name: "a",
  targets: [
    .target(name: "A", dependencies: [
      .product(name: "Ghost", package: "b"),
    ]),
  ]
)
'''
    )
    (b_dir / "Package.swift").write_text("// trivial\n")
    Tp = tool.TransitivePackageInfo
    Product = tool.Product
    tp_a = Tp(
        identity="a", checkout_path=a_dir,
        products=[Product(name="A", linkage="automatic", targets=["A"])],
        tools_version="5.9", referenced_products=["A"],
    )
    tp_b = Tp(
        identity="b", checkout_path=b_dir,
        # B does NOT declare "Ghost" as a product.
        products=[Product(name="Real", linkage="automatic", targets=["Real"])],
        tools_version="5.9", referenced_products=[],
    )
    expanded = tool._expand_cross_sibling_referenced_products([tp_a, tp_b])
    b_after = next(t for t in expanded if t.identity == "b")
    _assert(
        b_after.referenced_products == [],
        f"undeclared product Ghost leaked into b.referenced_products="
        f"{b_after.referenced_products}",
    )


def _selftest_overlay_guard_target_loops_swift_perception_shape() -> None:
    """swift-perception 1.6.0 ends with `for target in package.targets
    where target.type != .system { ... swiftSettings += ... }` — once
    the overlay injects binaryTargets the post-loop tries to apply
    settings to them and SPM refuses. The guard augments the `where`
    clause so binaryTargets are skipped.
    """
    pre = '''import PackageDescription

let package = Package(
  name: "swift-perception",
  products: [.library(name: "Perception", targets: ["Perception"])],
  dependencies: [],
  targets: [
    .target(name: "Perception"),
  ]
)

for target in package.targets where target.type != .system {
  target.swiftSettings = target.swiftSettings ?? []
  target.swiftSettings?.append(contentsOf: [
    .enableExperimentalFeature("StrictConcurrency"),
  ])
}
'''
    # First inject an overlay block (otherwise guard is a no-op).
    with_overlay = edit_inject_or_extend_overlay_binary_targets(
        pre, [("Perception", "./Perception.xcframework")]
    )
    out = edit_guard_target_loops_from_overlay(with_overlay)
    _assert(
        "where (target.type != .system) && !_SPM2XC_OVERLAY_NAMES.contains(target.name)"
        in out,
        f"guard did not augment the where clause as expected:\n{out}",
    )
    # Idempotency: re-running must not double-augment.
    twice = edit_guard_target_loops_from_overlay(out)
    _assert(
        twice == out,
        f"guard is not idempotent — second pass mutated the manifest:\n{twice}",
    )


def _selftest_overlay_guard_no_overlay_is_noop() -> None:
    """The guard is a no-op when no overlay sentinel is present — no
    binaryTargets are injected, so the post-loop has nothing to guard
    against."""
    pre = '''import PackageDescription
let package = Package(name: "x", products: [], dependencies: [], targets: [])
for target in package.targets where target.type != .system {
  target.swiftSettings = []
}
'''
    out = edit_guard_target_loops_from_overlay(pre)
    _assert(out == pre, f"expected no-op without overlay sentinel; got:\n{out}")


def _selftest_overlay_guard_inserts_where_when_absent() -> None:
    """`for target in package.targets { ... }` (no where clause) should
    have a fresh where clause inserted that excludes overlay names."""
    pre = '''import PackageDescription

let package = Package(
  name: "x",
  products: [.library(name: "Foo", targets: ["Foo"])],
  dependencies: [],
  targets: [.target(name: "Foo")]
)

for target in package.targets {
  target.swiftSettings = []
}
'''
    with_overlay = edit_inject_or_extend_overlay_binary_targets(
        pre, [("Foo", "./Foo.xcframework")]
    )
    out = edit_guard_target_loops_from_overlay(with_overlay)
    _assert(
        "for target in package.targets where !_SPM2XC_OVERLAY_NAMES.contains(target.name) {"
        in out,
        f"guard did not insert where clause as expected:\n{out}",
    )


def _selftest_overlay_guard_skips_already_guarded() -> None:
    """A where clause that already references `_SPM2XC_OVERLAY_NAMES`
    must be left untouched — supports running the guard multiple times
    safely AND lets users hand-edit the manifest without us clobbering
    them."""
    pre = '''import PackageDescription
let package = Package(name: "x", products: [], dependencies: [], targets: [])
// spm-to-xcframework dedup-overlap overlay — begin (auto-generated, do not edit)
let _SPM2XC_OVERLAY_TARGETS: [Target] = []
let _SPM2XC_OVERLAY_NAMES: Set<String> = Set(_SPM2XC_OVERLAY_TARGETS.map { $0.name })
// spm-to-xcframework dedup-overlap overlay — end
for target in package.targets where target.type != .system && !_SPM2XC_OVERLAY_NAMES.contains(target.name) {
  target.swiftSettings = []
}
'''
    out = edit_guard_target_loops_from_overlay(pre)
    _assert(out == pre, f"guard mutated already-guarded loop:\n{out}")


def _selftest_overlay_guard_skips_string_literals() -> None:
    """A `for target in package.targets` substring inside a string or
    comment must not be matched — the code-token view should mask it
    out."""
    pre = '''import PackageDescription
let package = Package(name: "x", products: [], dependencies: [], targets: [])
// spm-to-xcframework dedup-overlap overlay — begin (auto-generated, do not edit)
let _SPM2XC_OVERLAY_TARGETS: [Target] = []
let _SPM2XC_OVERLAY_NAMES: Set<String> = Set(_SPM2XC_OVERLAY_TARGETS.map { $0.name })
// spm-to-xcframework dedup-overlap overlay — end
let doc = "for target in package.targets where target.type != .system { stuff }"
'''
    out = edit_guard_target_loops_from_overlay(pre)
    _assert(out == pre, f"guard matched inside a string literal:\n{out}")


def _selftest_overlay_resulting_manifest_only_one_set_decl() -> None:
    """The overlay block's `let _SPM2XC_OVERLAY_NAMES = Set(...)`
    declaration must appear exactly once even after several extension
    calls. (Double declaration would be a Swift compile error.)"""
    text = SWIFT_COLLECTIONS_WRAPPER_FIXTURE
    out = text
    for i, name in enumerate(["A", "B", "C", "D", "E"]):
        out = edit_inject_or_extend_overlay_binary_targets(
            out, [(name, f"./{name}.xcframework")]
        )
    _assert(
        out.count("let _SPM2XC_OVERLAY_NAMES: Set<String> = Set(") == 1,
        f"overlay names declaration appeared more than once across 5 calls:\n{out}",
    )
    _assert(
        out.count("let _SPM2XC_OVERLAY_TARGETS: [Target] = [") == 1,
        f"overlay targets declaration appeared more than once:\n{out}",
    )


def _selftest_dedup_routes_wrapper_manifest_through_overlay(tmp_root: Path) -> None:
    """End-to-end through `_apply_dedup_overlap_substitutions`: when
    the manifest matches `_manifest_uses_custom_target_wrapper_text`,
    the dedup applier must NOT call `edit_replace_with_binary_target`
    (which would try and fail to rewrite `CustomTarget.target(...)`
    calls) and instead route through the overlay edit. Verifies the
    Execute-level dispatch."""
    staged = tmp_root / "dedup-routes-wrapper"
    staged.mkdir(parents=True, exist_ok=True)
    manifest = staged / "Package.swift"
    manifest.write_text(SWIFT_COLLECTIONS_WRAPPER_FIXTURE)
    # Concoct a fake xcframework path the relpath logic can resolve.
    xcfw = tmp_root / "BitCollections.xcframework"
    xcfw.mkdir(parents=True, exist_ok=True)
    _apply_dedup_overlap_substitutions(
        staged_dir=staged,
        substitutions=[("BitCollections", xcfw)],
        unit_name="BitCollections",
        verbose=False,
    )
    out = manifest.read_text()
    _assert(
        "spm-to-xcframework dedup-overlap overlay — begin" in out,
        f"wrapper manifest was not routed through the overlay edit:\n{out}",
    )
    _assert(
        "let _SPM2XC_OVERLAY_TARGETS" in out,
        "overlay var was not injected by the dedup router",
    )
    _assert(
        '.target(kind: .exported, name: "BitCollections")' in out,
        "wrapper call for BitCollections was unexpectedly rewritten",
    )


# --- Phantom-helper dep augmentation (swift-collections InternalCollectionsUtilities)


def _selftest_augment_target_deps_appends_to_nonempty_array() -> None:
    """[swift-collections phantom-helper] Happy path: the umbrella's
    `dependencies:` array already lists some direct deps, and the
    augmentation appends new string-literal entries with the leading
    comma supplied. The existing entries must remain verbatim and the
    appended entry lands inside the same `[...]`."""
    src = '''// swift-tools-version:5.7
import PackageDescription

let package = Package(
    name: "swift-collections",
    products: [.library(name: "Collections", targets: ["Collections"])],
    targets: [
        .target(name: "BitCollections"),
        .target(name: "DequeModule"),
        .target(
            name: "Collections",
            dependencies: ["BitCollections", "DequeModule"]
        ),
    ]
)
'''
    out = edit_augment_target_dependencies(
        src, "Collections", ["InternalCollectionsUtilities"]
    )
    _assert(
        '"BitCollections", "DequeModule", "InternalCollectionsUtilities"' in out,
        f"augmented deps array missing the new entry:\n{out}",
    )
    # No new array introduced, the call body otherwise unchanged.
    _assert(out.count("dependencies:") == 1,
            "augmentation added a second dependencies: label")
    _assert(out.count('.target(\n            name: "Collections",') == 1,
            "Collections target call was duplicated")


def _selftest_augment_target_deps_idempotent() -> None:
    """[swift-collections phantom-helper] Re-running the augmentation
    with the same `extra_dep_names` must be a no-op once the entries
    are already present as quoted string literals."""
    src = '''// swift-tools-version:5.7
import PackageDescription
let package = Package(
    name: "p",
    targets: [
        .target(name: "Umbrella", dependencies: ["Helper"]),
    ]
)
'''
    once = edit_augment_target_dependencies(src, "Umbrella", ["Helper"])
    _assert(once == src, "augmentation should be a no-op when entry exists")

    twice = edit_augment_target_dependencies(src, "Umbrella", ["NewDep"])
    thrice = edit_augment_target_dependencies(twice, "Umbrella", ["NewDep"])
    _assert(twice == thrice,
            f"second run with same entry should be a no-op\nonce:\n{twice}\ntwice:\n{thrice}")
    _assert('"Helper", "NewDep"' in twice,
            f"new entry not appended to single-entry array:\n{twice}")


def _selftest_augment_target_deps_empty_array() -> None:
    """[swift-collections phantom-helper] If the existing `dependencies:`
    array is empty (`[]`), the entries get spliced in without a leading
    comma — Swift would reject `[, "X"]`."""
    src = '''// swift-tools-version:5.7
let package = Package(
    name: "p",
    targets: [
        .target(name: "Umbrella", dependencies: []),
    ]
)
'''
    out = edit_augment_target_dependencies(src, "Umbrella", ["A", "B"])
    _assert(
        'dependencies: ["A", "B"]' in out,
        f"empty-array splice produced wrong shape:\n{out}",
    )


def _selftest_augment_target_deps_trailing_comma_array() -> None:
    """[swift-collections phantom-helper] If the existing array ends with
    a trailing comma (`["A",]`), the augmentation preserves the comma
    structure (`["A", B,"]`-style is wrong; should be `["A", "B",]`).
    Swift accepts the second trailing comma; what matters is no double-
    comma and no missing separator."""
    src = '''// swift-tools-version:5.7
let package = Package(
    name: "p",
    targets: [
        .target(
            name: "Umbrella",
            dependencies: [
                "A",
            ]
        ),
    ]
)
'''
    out = edit_augment_target_dependencies(src, "Umbrella", ["B"])
    # The splice should produce `"A", "B",` — separator commas correct,
    # no double-comma, the trailing comma preserved.
    _assert('"A", "B",' in out,
            f"trailing-comma splice produced wrong shape:\n{out}")
    _assert(",," not in out.replace(",,)", ""),  # ignore unrelated patterns
            f"double comma in augmented manifest:\n{out}")


def _selftest_augment_target_deps_trailing_line_comment() -> None:
    """[Codex/Grok r1 HIGH] Regression: when a `//` line comment sits
    between the last element and `]`, the splice MUST NOT land inside
    the comment. Without the comment-stripped tail scan, the inserted
    literal ends up as dead code inside the comment (`// note, "B"`).
    """
    src = '''// swift-tools-version:5.7
let package = Package(
    name: "p",
    targets: [
        .target(name: "Umbrella", dependencies: [
            "A", // important note
        ]),
    ]
)
'''
    out = edit_augment_target_dependencies(src, "Umbrella", ["B"])
    # Phantom landed BEFORE the comment, not inside it.
    _assert('"A", "B",' in out,
            f"splice didn't land before the trailing line comment:\n{out}")
    _assert('// important note, "B"' not in out,
            f"splice landed INSIDE the // comment:\n{out}")
    _assert('// important note, "B",' not in out,
            f"splice landed INSIDE the // comment:\n{out}")
    # Original comment text is preserved verbatim.
    _assert('// important note' in out,
            f"original line comment was clobbered:\n{out}")


def _selftest_augment_target_deps_trailing_block_comment() -> None:
    """[Codex/Grok r1 HIGH] Regression: a `/* */` block comment between
    the last element and `]` similarly fooled the splice. Without the
    fix, `["A", /* note */]` would become `["A", /* note */, "B"]` —
    Swift sees `["A", , "B"]` after comment stripping, which is
    invalid (empty array element)."""
    src = '''// swift-tools-version:5.7
let package = Package(
    name: "p",
    targets: [
        .target(name: "Umbrella", dependencies: [
            "A", /* keep A */
        ]),
    ]
)
'''
    out = edit_augment_target_dependencies(src, "Umbrella", ["B"])
    # Splice lands BEFORE the block comment.
    _assert('"A", "B",' in out,
            f"splice didn't land before the trailing block comment:\n{out}")
    # No double-comma anywhere (no `,/* keep A */, "B"` shape).
    _assert(', /* keep A */, "B"' not in out,
            f"splice produced an empty array element via double comma:\n{out}")
    # Original block comment is preserved.
    _assert('/* keep A */' in out,
            f"original block comment was clobbered:\n{out}")


def _selftest_augment_target_deps_comment_only_interior() -> None:
    """[Codex/Grok r1] Defensive: an array whose only content is a
    comment is, semantically, an empty array. Splicing should treat
    it as such — produce a clean array literal with just the new
    entries (no spurious leading comma before the comment)."""
    src = '''// swift-tools-version:5.7
let package = Package(
    name: "p",
    targets: [
        .target(name: "Umbrella", dependencies: [/* nothing yet */]),
    ]
)
'''
    out = edit_augment_target_dependencies(src, "Umbrella", ["B"])
    # The comment is preserved; the splice landed after `[` (empty-array
    # shape) so the result is well-formed Swift.
    _assert('/* nothing yet */' in out,
            f"original comment was clobbered:\n{out}")
    _assert('"B"' in out,
            f"phantom dep not appended:\n{out}")
    # No leading comma before our entry — Swift rejects [, "B"].
    _assert('[, "B"' not in out and '[ , "B"' not in out,
            f"splice produced a leading-comma shape:\n{out}")


def _selftest_augment_target_deps_empty_extras_is_noop() -> None:
    """[swift-collections phantom-helper] `extra_dep_names=[]` returns
    the manifest unchanged without even locating the target call —
    cheapest possible path on the common case where this unit has no
    phantom helpers to inject."""
    src = '''// swift-tools-version:5.7
let package = Package(name: "p", targets: [.target(name: "T")])
'''
    out = edit_augment_target_dependencies(src, "T", [])
    _assert(out == src, "empty extras should return unchanged manifest")


def _selftest_augment_target_deps_missing_target_raises() -> None:
    """[swift-collections phantom-helper] A target name that doesn't
    match any `.target(name: ...)` decl raises PrepareUserError — the
    caller's planner-vs-manifest view is out of sync, and silently
    skipping would leave the consumer's build broken."""
    src = '''// swift-tools-version:5.7
let package = Package(
    name: "p",
    targets: [.target(name: "Real", dependencies: [])]
)
'''
    try:
        edit_augment_target_dependencies(src, "Missing", ["Phantom"])
    except PrepareUserError as exc:
        _assert("no `.target(name: 'Missing'" in str(exc),
                f"unexpected error message: {exc!r}")
        return
    raise AssertionError("expected PrepareUserError for missing target")


def _selftest_augment_target_deps_no_deps_arg_raises() -> None:
    """[swift-collections phantom-helper] A target with no
    `dependencies:` arg at all raises — the augmentation needs an
    existing array to splice into; synthesizing the whole argument is
    a different (and currently unimplemented) operation."""
    src = '''// swift-tools-version:5.7
let package = Package(
    name: "p",
    targets: [.target(name: "Bare", path: "Sources/Bare")]
)
'''
    try:
        edit_augment_target_dependencies(src, "Bare", ["X"])
    except PrepareUserError as exc:
        _assert("no top-level `dependencies:`" in str(exc),
                f"unexpected error message: {exc!r}")
        return
    raise AssertionError("expected PrepareUserError for missing dependencies: arg")


def _selftest_augment_target_deps_non_array_raises() -> None:
    """[swift-collections phantom-helper] If `dependencies:` is bound
    to an identifier (`dependencies: helperList`) rather than a literal
    array, the augmentation refuses to mutate it. Splicing into a
    name-bound expression would require synthesizing a new top-level
    binding, which is out of scope for this edit."""
    src = '''// swift-tools-version:5.7
let helperList: [Target.Dependency] = ["A"]
let package = Package(
    name: "p",
    targets: [.target(name: "T", dependencies: helperList)]
)
'''
    try:
        edit_augment_target_dependencies(src, "T", ["B"])
    except PrepareUserError as exc:
        _assert("not an array literal" in str(exc),
                f"unexpected error message: {exc!r}")
        return
    raise AssertionError("expected PrepareUserError for non-array deps expression")


def _selftest_augment_target_deps_wrapper_style_manifest() -> None:
    """[swift-collections phantom-helper] On the wrapper-style manifest
    (`CustomTarget.target(name:, dependencies:, ...)`), the regex
    `\\.(target|executableTarget|testTarget)\\s*\\(` matches the
    wrapper call too. The augmentation must splice the phantom into
    the wrapper's `dependencies:` array — Apple's swift-collections
    canonical case. The wrapper field is `[Target.Dependency]`-typed
    and forwards verbatim to `Target.target(dependencies:)` via
    `.toTarget()`."""
    out = edit_augment_target_dependencies(
        SWIFT_COLLECTIONS_WRAPPER_FIXTURE,
        "Collections",
        ["InternalCollectionsUtilities"],
    )
    _assert(
        '"BitCollections", "DequeModule", "InternalCollectionsUtilities"' in out,
        f"wrapper-style augmentation didn't land:\n{out}",
    )
    # Sibling wrapper calls (BitCollections / DequeModule) must remain
    # untouched — the finder targeted exactly the Collections call.
    _assert(
        '.target(kind: .exported, name: "BitCollections")' in out,
        "BitCollections wrapper call was unexpectedly mutated",
    )
    _assert(
        '.target(kind: .exported, name: "DequeModule")' in out,
        "DequeModule wrapper call was unexpectedly mutated",
    )


def _selftest_compute_phantom_helper_deps_basic() -> None:
    """[swift-collections phantom-helper] The classic case: umbrella
    `Collections` directly depends on `[BitCollections, DequeModule]`;
    both transitively depend on `InternalCollectionsUtilities`; the
    helper is being binary-substituted (it's in `substituted_target_names`);
    the helper is NOT in `Collections`'s direct deps. Result:
    `{Collections: [InternalCollectionsUtilities]}`.
    """
    BU = tool.BuildUnit
    umbrella = BU(name="Collections", scheme="Collections",
                  framework_name="Collections", language="Swift",
                  archive_strategy="archive", source_targets=["Collections"])
    raw = {
        "targets": [
            {"name": "Collections", "type": "regular",
             "dependencies": [{"byName": ["BitCollections", None]},
                              {"byName": ["DequeModule", None]}]},
            {"name": "BitCollections", "type": "regular",
             "dependencies": [{"byName": ["InternalCollectionsUtilities", None]}]},
            {"name": "DequeModule", "type": "regular",
             "dependencies": [{"byName": ["InternalCollectionsUtilities", None]}]},
            {"name": "InternalCollectionsUtilities", "type": "regular",
             "dependencies": []},
        ]
    }
    pkg = tool.Package(
        name="swift-collections", tools_version="5.7",
        platforms=[], products=[], targets=[], schemes=[],
        raw_dump=raw, staged_dir=Path("/tmp/fake"),
    )
    target_deps = {
        "Collections": {"BitCollections", "DequeModule",
                        "InternalCollectionsUtilities"},
        "BitCollections": {"InternalCollectionsUtilities"},
        "DequeModule": {"InternalCollectionsUtilities"},
        "InternalCollectionsUtilities": set(),
    }
    result = _compute_phantom_helper_deps(
        unit=umbrella, package=pkg,
        substituted_target_names={"BitCollections", "DequeModule",
                                  "InternalCollectionsUtilities"},
        target_deps=target_deps,
    )
    _assert(
        result == {"Collections": ["InternalCollectionsUtilities"]},
        f"expected phantom InternalCollectionsUtilities only, got {result!r}",
    )


def _selftest_compute_phantom_helper_deps_skips_direct() -> None:
    """[swift-collections phantom-helper] If the would-be phantom is
    already in the umbrella's DIRECT deps, the augmentation is
    unnecessary — SPM already adds the slice dir through the normal
    binary-target dep walk. The phantom list comes back empty for that
    unit (and the result dict is empty)."""
    BU = tool.BuildUnit
    umbrella = BU(name="Top", scheme="Top",
                  framework_name="Top", language="Swift",
                  archive_strategy="archive", source_targets=["Top"])
    raw = {
        "targets": [
            {"name": "Top", "type": "regular",
             "dependencies": [{"byName": ["Helper", None]}]},
            {"name": "Helper", "type": "regular", "dependencies": []},
        ]
    }
    pkg = tool.Package(
        name="p", tools_version="5.7",
        platforms=[], products=[], targets=[], schemes=[],
        raw_dump=raw, staged_dir=Path("/tmp/fake"),
    )
    target_deps = {"Top": {"Helper"}, "Helper": set()}
    result = _compute_phantom_helper_deps(
        unit=umbrella, package=pkg,
        substituted_target_names={"Helper"},
        target_deps=target_deps,
    )
    _assert(result == {},
            f"helper that's already a direct dep must not be a phantom, got {result!r}")


def _selftest_compute_phantom_helper_deps_only_substituted_count() -> None:
    """[swift-collections phantom-helper] A transitive dep that isn't
    being substituted (not in `substituted_target_names`) doesn't
    become a phantom — SPM's source-level dep walker handles those
    natively. Only substituted siblings need the explicit re-listing."""
    BU = tool.BuildUnit
    umbrella = BU(name="Umb", scheme="Umb",
                  framework_name="Umb", language="Swift",
                  archive_strategy="archive", source_targets=["Umb"])
    raw = {
        "targets": [
            {"name": "Umb", "type": "regular",
             "dependencies": [{"byName": ["A", None]}]},
            {"name": "A", "type": "regular",
             "dependencies": [{"byName": ["B", None]}]},
            {"name": "B", "type": "regular", "dependencies": []},
        ]
    }
    pkg = tool.Package(
        name="p", tools_version="5.7",
        platforms=[], products=[], targets=[], schemes=[],
        raw_dump=raw, staged_dir=Path("/tmp/fake"),
    )
    target_deps = {"Umb": {"A", "B"}, "A": {"B"}, "B": set()}
    # Only A is substituted; B is reachable but stays source-built.
    result = _compute_phantom_helper_deps(
        unit=umbrella, package=pkg,
        substituted_target_names={"A"},
        target_deps=target_deps,
    )
    _assert(result == {},
            f"non-substituted transitive must not be a phantom, got {result!r}")


def _selftest_compute_phantom_helper_deps_skips_self() -> None:
    """[swift-collections phantom-helper] Defensive: a target's name in
    its own transitive closure (would only happen if `target_deps`
    were ever to include `T → T`) must never be returned as a phantom
    — splicing T into its own dep array would create a cycle SPM
    rejects at resolve time."""
    BU = tool.BuildUnit
    umbrella = BU(name="Self", scheme="Self",
                  framework_name="Self", language="Swift",
                  archive_strategy="archive", source_targets=["Self"])
    raw = {
        "targets": [
            {"name": "Self", "type": "regular", "dependencies": []},
        ]
    }
    pkg = tool.Package(
        name="p", tools_version="5.7",
        platforms=[], products=[], targets=[], schemes=[],
        raw_dump=raw, staged_dir=Path("/tmp/fake"),
    )
    # Pathological: Self appears as a transitive dep of itself.
    target_deps = {"Self": {"Self"}}
    result = _compute_phantom_helper_deps(
        unit=umbrella, package=pkg,
        substituted_target_names={"Self"},
        target_deps=target_deps,
    )
    _assert(result == {},
            f"self-dep must never be returned as a phantom, got {result!r}")


def _selftest_apply_phantom_helper_dep_augmentation_e2e(tmp_root: Path) -> None:
    """[swift-collections phantom-helper] End-to-end through
    `_apply_phantom_helper_dep_augmentation`: write the wrapper-style
    fixture to a staged manifest, call the applier with
    `{Collections: ['InternalCollectionsUtilities']}`, and verify the
    on-disk manifest now has the phantom in Collections's deps array."""
    staged = tmp_root / "phantom-helper-e2e"
    staged.mkdir(parents=True, exist_ok=True)
    manifest = staged / "Package.swift"
    manifest.write_text(SWIFT_COLLECTIONS_WRAPPER_FIXTURE)
    _apply_phantom_helper_dep_augmentation(
        staged_dir=staged,
        phantom_helpers={"Collections": ["InternalCollectionsUtilities"]},
        unit_name="Collections",
        verbose=False,
    )
    out = manifest.read_text()
    _assert(
        '"BitCollections", "DequeModule", "InternalCollectionsUtilities"' in out,
        f"phantom helper wasn't appended to Collections deps array:\n{out}",
    )

    # Idempotent: re-applying must not double-append.
    _apply_phantom_helper_dep_augmentation(
        staged_dir=staged,
        phantom_helpers={"Collections": ["InternalCollectionsUtilities"]},
        unit_name="Collections",
        verbose=False,
    )
    out2 = manifest.read_text()
    _assert(out == out2,
            f"second call should be a no-op\nfirst:\n{out}\nsecond:\n{out2}")


def _selftest_apply_phantom_helper_dep_augmentation_empty_is_noop(tmp_root: Path) -> None:
    """[swift-collections phantom-helper] An empty `phantom_helpers`
    dict short-circuits before any manifest I/O — the applier never
    touches the staged_dir. The fixture file's mtime is the canary."""
    staged = tmp_root / "phantom-helper-empty"
    staged.mkdir(parents=True, exist_ok=True)
    # Deliberately leave the dir empty — no Package.swift. If the applier
    # tried to read one, it would raise ExecuteError; passing here proves
    # the empty-input branch never touched the filesystem.
    _apply_phantom_helper_dep_augmentation(
        staged_dir=staged,
        phantom_helpers={},
        unit_name="N/A",
        verbose=False,
    )


# --- _auto_synth_sibling_units language gate (WCDB regression) -------------


def _auto_synth_pkg(
    *,
    umbrella_target: str,
    sibling_target: str,
    sibling_language: str,
    sibling_settings: Optional[List[dict]] = None,
) -> "tool.Package":
    """Construct a minimal tool.Package that exercises
    `_auto_synth_sibling_units`'s internal-helper branch: one umbrella
    target with a public `.library` product, plus one internal sibling
    helper that the umbrella depends on. The sibling has no product
    wrapper, so the auto-synth logic decides whether to promote it.
    """
    raw = {
        "name": "p",
        "toolsVersion": {"_version": "5.7"},
        "platforms": [],
        "products": [
            {"name": umbrella_target,
             "type": {"library": ["automatic"]},
             "targets": [umbrella_target]},
        ],
        "targets": [
            {"name": umbrella_target, "type": "regular", "path": None,
             "publicHeadersPath": None,
             "dependencies": [{"byName": [sibling_target, None]}]},
            {"name": sibling_target, "type": "regular", "path": None,
             "publicHeadersPath": None,
             "dependencies": [],
             "settings": sibling_settings or []},
        ],
    }
    raw_, products, targets, platforms, name, tv = _parse_dump(raw)
    # `_parse_dump` doesn't fill the language field (that's a separate
    # disk scan pass). Stamp the sibling's language to whatever the
    # test wants so the auto-synth gate sees the right value.
    for t in targets:
        if t.name == sibling_target:
            t.language = sibling_language
    return tool.Package(
        name=name, tools_version=tv, platforms=platforms,
        products=products, targets=targets, schemes=[],
        raw_dump=raw_, staged_dir=Path("/tmp/auto-synth-fixture"),
    )


def _auto_synth_seed_plan(umbrella_target: str) -> "tool.Plan":
    """Seed a Plan with one BuildUnit for the umbrella target — the
    same shape the regular planner produces before
    `_auto_synth_sibling_units` runs."""
    plan = tool.Plan()
    plan.build_units.append(
        tool.BuildUnit(
            name=umbrella_target, scheme=umbrella_target,
            framework_name=umbrella_target, language="Swift",
            archive_strategy="archive",
            source_targets=[umbrella_target],
        )
    )
    return plan


def _selftest_auto_synth_promotes_swift_internal_helper() -> None:
    """[swift-collections regression] A Swift-language internal helper
    with no public product and no linker settings of its own MUST be
    auto-promoted to its own xcframework. Without promotion, the
    helper's symbols stay locked inside the umbrella binary, and any
    sibling that emits `import <Helper>` in its `.swiftinterface`
    fails to resolve the module at consumer build time.
    """
    pkg = _auto_synth_pkg(
        umbrella_target="Collections",
        sibling_target="InternalCollectionsUtilities",
        sibling_language=tool.Language.SWIFT,
    )
    plan = _auto_synth_seed_plan("Collections")
    tool._auto_synth_sibling_units(plan, pkg, taken_product_names=set())
    names = [bu.name for bu in plan.build_units]
    _assert("InternalCollectionsUtilities" in names,
            f"Swift helper should be auto-promoted; build_units={names!r}")
    # An accompanying synth_library edit must be queued too — without
    # it, the planner produces a unit Execute can't resolve.
    kinds = [(e.kind, e.product_name) for e in plan.package_swift_edits]
    _assert(("synth_library", "InternalCollectionsUtilities") in kinds,
            f"missing synth_library edit for the helper; edits={kinds!r}")


def _selftest_auto_synth_skips_objc_internal_helper() -> None:
    """[WCDB regression — primary] An ObjC-language internal helper
    with no public product and no linker settings of its own must NOT
    be auto-promoted. ObjC siblings (canonical: WCDB's `bridge`,
    `common`, `objc-core`) call into CoreFoundation but the
    `.linkedFramework("CoreFoundation")` lives on the UMBRELLA target's
    settings. Building standalone as a `.library(type: .dynamic)`
    fails at link with `Undefined symbol: _CFAllocatorGetDefault`.
    The umbrella keeps statically embedding it (pre-auto-synth
    default), and `--target bridge` remains the explicit opt-in.
    """
    pkg = _auto_synth_pkg(
        umbrella_target="WCDBSwift",
        sibling_target="bridge",
        sibling_language=tool.Language.OBJC,
    )
    plan = _auto_synth_seed_plan("WCDBSwift")
    tool._auto_synth_sibling_units(plan, pkg, taken_product_names=set())
    names = [bu.name for bu in plan.build_units]
    _assert("bridge" not in names,
            f"ObjC helper must NOT be auto-promoted; build_units={names!r}")
    kinds = [(e.kind, e.product_name) for e in plan.package_swift_edits]
    _assert(("synth_library", "bridge") not in kinds,
            f"ObjC helper edit leaked into plan; edits={kinds!r}")


def _selftest_auto_synth_skips_mixed_internal_helper() -> None:
    """[WCDB regression — defense in depth] A Mixed-language (Swift +
    ObjC) internal helper is treated as ObjC for safety: the ObjC
    parts almost certainly rely on the umbrella's link context the
    same way pure-ObjC helpers do. Skip — only PURE Swift gets the
    auto-promote benefit."""
    pkg = _auto_synth_pkg(
        umbrella_target="Top",
        sibling_target="MixedHelper",
        sibling_language=tool.Language.MIXED,
    )
    plan = _auto_synth_seed_plan("Top")
    tool._auto_synth_sibling_units(plan, pkg, taken_product_names=set())
    names = [bu.name for bu in plan.build_units]
    _assert("MixedHelper" not in names,
            f"Mixed helper must NOT be auto-promoted; build_units={names!r}")


def _selftest_auto_synth_skips_swift_with_explicit_linker_settings() -> None:
    """[WCDB regression — secondary] Even a Swift helper that declares
    its own `.linkedFramework(...)` is suspect — the declaration is
    a signal the target needs link-time context that may or may not
    be self-contained. Skip on the side of correctness; the user can
    still pass `--target T` for explicit opt-in."""
    pkg = _auto_synth_pkg(
        umbrella_target="Top",
        sibling_target="SwiftWithLink",
        sibling_language=tool.Language.SWIFT,
        sibling_settings=[
            {"tool": "linker",
             "kind": {"linkedFramework": ["Security"]}},
        ],
    )
    plan = _auto_synth_seed_plan("Top")
    tool._auto_synth_sibling_units(plan, pkg, taken_product_names=set())
    names = [bu.name for bu in plan.build_units]
    _assert("SwiftWithLink" not in names,
            "Swift helper with explicit linker settings must NOT be "
            f"auto-promoted; build_units={names!r}")


def _selftest_find_target_call_for_name_ignores_commented_calls() -> None:
    """[Codex P2 round 2 regression] A `.target(name: "Foo", ...)` that
    appears INSIDE a `//` line comment or `/* */` block comment must
    NOT be matched as a candidate. Without this gate, a commented-out
    decl that precedes the real one would be the first hit, the
    rewrite would land inside the comment, and the second pass would
    raise BOTH-shapes-present (since the commented `.binaryTarget` it
    just synthesized is still visible).
    """
    # Line-comment + real-target shape.
    src_line = '''// swift-tools-version:5.7
import PackageDescription

let p = Package(
    name: "x",
    targets: [
        // .target(name: "Foo", path: "OldFoo"),
        .target(
            name: "Foo",
            path: "Foo"
        ),
    ]
)
'''
    ks, ce, kind = tool._find_target_call_for_name(src_line, "Foo")
    located = src_line[ks:ce + 1]
    _assert(kind == "target", f"line-comment kind: {kind!r}")
    _assert('path: "Foo"' in located,
            f"line-comment finder picked the wrong span:\n{located}")
    _assert('path: "OldFoo"' not in located,
            f"line-comment finder matched the commented decl:\n{located}")

    # Block-comment + real-target shape (with nested comment to ensure
    # depth tracking).
    src_block = '''// swift-tools-version:5.7
import PackageDescription

let p = Package(
    name: "x",
    targets: [
        /* legacy:
           .target(name: "Foo", path: "OldFoo"),
           /* nested note */
        */
        .target(
            name: "Foo",
            path: "RealFoo"
        ),
    ]
)
'''
    ks2, ce2, kind2 = tool._find_target_call_for_name(src_block, "Foo")
    located2 = src_block[ks2:ce2 + 1]
    _assert(kind2 == "target", f"block-comment kind: {kind2!r}")
    _assert('path: "RealFoo"' in located2,
            f"block-comment finder picked the wrong span:\n{located2}")
    _assert('path: "OldFoo"' not in located2,
            f"block-comment finder matched the commented decl:\n{located2}")


def _selftest_has_binary_target_ignores_commented_calls() -> None:
    """[Codex P2 round 2 regression] `_has_binary_target_with_name`
    must skip `.binaryTarget(...)` calls that live inside `//` or
    `/* */` comments. Without this gate, after the dedup edit lands
    on the right target, a re-run would see the commented
    `.binaryTarget` it never wrote and incorrectly report BOTH-shapes
    state.
    """
    src_line_no = '''// swift-tools-version:5.7
let p = Package(
    name: "x",
    targets: [
        // .binaryTarget(name: "Foo", path: "/old/Foo.xcframework"),
        .target(name: "Foo", path: "Foo"),
    ]
)
'''
    _assert(tool._has_binary_target_with_name(src_line_no, "Foo") is False,
            "commented-out .binaryTarget must NOT count")

    src_block_no = '''// swift-tools-version:5.7
let p = Package(
    name: "x",
    targets: [
        /* .binaryTarget(name: "Foo", path: "/old/Foo.xcframework"), */
        .target(name: "Foo", path: "Foo"),
    ]
)
'''
    _assert(tool._has_binary_target_with_name(src_block_no, "Foo") is False,
            "block-commented .binaryTarget must NOT count")

    # Sanity: a real .binaryTarget is still detected.
    src_real = '''// swift-tools-version:5.7
let p = Package(
    name: "x",
    targets: [
        .binaryTarget(name: "Foo", path: "/build/Foo.xcframework"),
    ]
)
'''
    _assert(tool._has_binary_target_with_name(src_real, "Foo") is True,
            "real .binaryTarget must be detected")


def _selftest_edit_replace_with_binary_target_skips_commented_decl() -> None:
    """[Codex P2 round 2 regression — end-to-end] When a manifest has
    a commented-out `.target(name: "Foo", ...)` before the real one,
    `edit_replace_with_binary_target` must rewrite the real target
    and leave the comment text byte-identical. A second call must
    then be a true no-op — not raise BOTH-shapes."""
    src = '''// swift-tools-version:5.7
import PackageDescription

let p = Package(
    name: "x",
    targets: [
        // .target(name: "Foo", path: "OldFoo"),
        /* .binaryTarget(name: "Foo", path: "/legacy/Foo.xcframework"), */
        .target(
            name: "Foo",
            path: "RealFoo"
        ),
    ]
)
'''
    out = edit_replace_with_binary_target(src, "Foo", "/build/Foo.xcframework")

    # The real target is now a binaryTarget pointing at the new path.
    _assert('.binaryTarget(name: "Foo", path: "/build/Foo.xcframework")' in out,
            f"real target was not rewritten to .binaryTarget:\n{out}")
    # The commented-out lines are byte-preserved.
    _assert('// .target(name: "Foo", path: "OldFoo"),' in out,
            f"line comment was modified:\n{out}")
    _assert('/* .binaryTarget(name: "Foo", path: "/legacy/Foo.xcframework"), */' in out,
            f"block comment was modified:\n{out}")
    # The OLD `.target(...)` block for the real Foo (which had `path: "RealFoo"`) is gone.
    _assert('.target(\n            name: "Foo",\n            path: "RealFoo"\n'
            '        )' not in out,
            f"old real Foo .target() block was not removed:\n{out}")

    # Second call: must be a true no-op, not a BOTH-shapes error.
    twice = edit_replace_with_binary_target(out, "Foo", "/build/Foo.xcframework")
    _assert(twice == out,
            "second edit through commented-decl manifest was not a no-op")


def _selftest_find_target_call_for_name_ignores_string_literals() -> None:
    """[Codex P2 round 3 regression] A `.target(name: "Foo", ...)`
    mention that lives INSIDE a Swift `"..."` string literal must
    NOT be matched as a candidate. Without this gate, candidate
    discovery would stop on the string, fail to find a matching
    top-level `name:` (the string body's `name:` sits inside escaped
    quotes, so depth-0 stripping wipes the inner content but the
    outer non-call context still doesn't have `name: "Foo"`), and
    the finder would walk past — but in shapes where the in-string
    `.target(` happens to be balanced over a fragment that DOES
    look like `name: "Foo"` at depth 0 of the synthetic span,
    the function would either match the wrong span or raise on a
    malformed `)` lookup.
    """
    src = '''// swift-tools-version:5.7
import PackageDescription

let note = ".target(name: \\"Foo\\", path: \\"OldFoo\\")"

let p = Package(
    name: "x",
    targets: [
        .target(
            name: "Foo",
            path: "RealFoo"
        ),
    ]
)
'''
    ks, ce, kind = tool._find_target_call_for_name(src, "Foo")
    located = src[ks:ce + 1]
    _assert(kind == "target", f"string-literal kind: {kind!r}")
    _assert('path: "RealFoo"' in located,
            f"string-literal finder picked the wrong span:\n{located}")
    _assert('path: "OldFoo"' not in located,
            f"string-literal finder matched the in-string decl:\n{located}")


def _selftest_has_binary_target_ignores_string_literals() -> None:
    """[Codex P2 round 3 regression] `.binaryTarget(name: "Foo", ...)`
    text inside a `"..."` string literal must not register as a real
    binary target.
    """
    src = '''// swift-tools-version:5.7
let example = ".binaryTarget(name: \\"Foo\\", path: \\"/old/Foo.xcframework\\")"
let p = Package(
    name: "x",
    targets: [
        .target(name: "Foo", path: "Foo"),
    ]
)
'''
    _assert(tool._has_binary_target_with_name(src, "Foo") is False,
            "in-string .binaryTarget must NOT count as present")


def _selftest_edit_replace_with_binary_target_skips_string_literal() -> None:
    """[Codex P2 round 3 regression — end-to-end] When a manifest
    declares a string literal containing `.target(name: "Foo", ...)`
    text BEFORE the real target decl, `edit_replace_with_binary_target`
    must rewrite ONLY the real target. The string literal is
    preserved byte-identically, no spurious PrepareUserError is
    raised, and the second call is a true no-op.
    """
    src = '''// swift-tools-version:5.7
import PackageDescription

// Documentation example users may copy-paste into their own packages.
let example = ".target(name: \\"Foo\\", path: \\"OldFoo\\")"

let p = Package(
    name: "x",
    targets: [
        .target(
            name: "Foo",
            path: "RealFoo"
        ),
    ]
)
'''
    out = edit_replace_with_binary_target(src, "Foo", "/build/Foo.xcframework")

    _assert('.binaryTarget(name: "Foo", path: "/build/Foo.xcframework")' in out,
            f"real target was not rewritten to .binaryTarget:\n{out}")
    # The documentation string literal stays byte-identical.
    _assert('let example = ".target(name: \\"Foo\\", path: \\"OldFoo\\")"' in out,
            f"docstring literal was modified:\n{out}")
    # The OLD .target() block for the real Foo is gone.
    _assert('.target(\n            name: "Foo",\n            path: "RealFoo"\n'
            '        )' not in out,
            f"old real Foo .target() block was not removed:\n{out}")

    # Idempotent.
    twice = edit_replace_with_binary_target(out, "Foo", "/build/Foo.xcframework")
    _assert(twice == out,
            "second edit through string-literal manifest was not a no-op")


def _selftest_edit_demote_synthetic_product_spm_multiline() -> None:
    """[Codex round-3 High] Canonical SPM `add-product` output —
    multi-line, `type: .dynamic,` on its own line — gets cleanly
    demoted to automatic-library shape. The resulting manifest must
    parse cleanly through `swift package dump-package` (verified by a
    later integration test) AND must preserve everything else
    byte-for-byte (other products, all targets, comments).
    """
    text = (
        "// swift-tools-version: 5.9\n"
        "import PackageDescription\n"
        "\n"
        "let package = Package(\n"
        '    name: "Probe",\n'
        "    products: [\n"
        "        .library(\n"
        '            name: "FooDynamic",\n'
        "            type: .dynamic,\n"
        '            targets: [ "Foo" ]\n'
        "        ),\n"
        "    ],\n"
        "    targets: [\n"
        '        .target(name: "Foo"),\n'
        "    ]\n"
        ")\n"
    )
    out = edit_demote_synthetic_product(text, "FooDynamic")
    _assert("type: .dynamic" not in out,
            f"type: .dynamic still present after demote:\n{out}")
    _assert('name: "FooDynamic"' in out,
            f"product name was clobbered by the demote:\n{out}")
    _assert('targets: [ "Foo" ]' in out,
            f"targets array was clobbered by the demote:\n{out}")
    _assert('.target(name: "Foo")' in out,
            f"unrelated target was modified by the demote:\n{out}")


def _selftest_edit_demote_synthetic_product_single_line() -> None:
    """The demoter must also work on single-line `.library(...)`
    declarations — uncommon in SPM-generated output but well-formed
    Swift. The result is the same automatic-library shape minus
    `type: .dynamic`.
    """
    text = (
        "let package = Package(\n"
        "    products: [\n"
        '        .library(name: "FooDynamic", type: .dynamic, targets: ["Foo"]),\n'
        "    ]\n"
        ")\n"
    )
    out = edit_demote_synthetic_product(text, "FooDynamic")
    _assert("type: .dynamic" not in out,
            f"type: .dynamic still present:\n{out}")
    _assert(
        '.library(name: "FooDynamic", targets: ["Foo"])' in out,
        f"single-line shape not cleaned up properly:\n{out}",
    )


def _selftest_edit_demote_synthetic_product_idempotent() -> None:
    """Running the demoter twice — or on an already-automatic library
    — must be a no-op. Same idempotency contract as
    `edit_replace_with_binary_target`."""
    text = (
        "let package = Package(\n"
        "    products: [\n"
        '        .library(name: "Foo", targets: ["Foo"]),\n'
        "    ]\n"
        ")\n"
    )
    out = edit_demote_synthetic_product(text, "Foo")
    _assert(out == text, "demote on automatic-shape product must be a no-op")


def _selftest_edit_demote_synthetic_product_unknown_raises() -> None:
    """Demoter must raise `PrepareUserError` if the named product
    isn't present in the manifest. (Same surface as
    `edit_replace_with_binary_target` for symmetry.)"""
    text = (
        "let package = Package(\n"
        "    products: [\n"
        '        .library(name: "Foo", targets: ["Foo"]),\n'
        "    ]\n"
        ")\n"
    )
    raised = False
    try:
        edit_demote_synthetic_product(text, "DoesNotExist")
    except tool.PrepareUserError:
        raised = True
    _assert(raised, "expected PrepareUserError for missing product")


def _selftest_edit_demote_synthetic_product_ignores_target_name() -> None:
    """When a regular `.target(name: "MyProduct", ...)` and a synth
    `.library(name: "MyProduct", ...)` share a name (the `--target T`
    escape-hatch case), the demoter must target the LIBRARY call, not
    the target call. Otherwise the demote would mis-fire on the source
    target and leave the synth library's `type: .dynamic` intact.
    """
    text = (
        "let package = Package(\n"
        "    products: [\n"
        "        .library(\n"
        '            name: "MyTarget",\n'
        "            type: .dynamic,\n"
        '            targets: ["MyTarget"]\n'
        "        ),\n"
        "    ],\n"
        "    targets: [\n"
        '        .target(name: "MyTarget"),\n'
        "    ]\n"
        ")\n"
    )
    out = edit_demote_synthetic_product(text, "MyTarget")
    _assert("type: .dynamic" not in out,
            f"library's type: .dynamic was not stripped:\n{out}")
    _assert('.target(name: "MyTarget")' in out,
            f"the unrelated `.target(name: \"MyTarget\")` was clobbered:\n{out}")


def _selftest_edit_demote_synthetic_product_dumppackage_roundtrip(tmp_root: Path) -> None:
    """End-to-end: write a manifest with a synth-dynamic product
    pointing at a binary target → confirm it FAILS `dump-package`
    (the very invariant we're defending against) → demote → confirm
    it PASSES `dump-package`. Pins the Codex round-3 fix's actual
    contract: the demoted manifest is what unblocks subsequent
    `.binaryTarget` substitution.
    """
    work = tmp_root / "demote-roundtrip"
    work.mkdir()
    pkg = work / "Package.swift"
    pkg.write_text(
        "// swift-tools-version: 5.9\n"
        "import PackageDescription\n"
        "\n"
        "let package = Package(\n"
        '    name: "Probe",\n'
        "    products: [\n"
        "        .library(\n"
        '            name: "FooDynamic",\n'
        "            type: .dynamic,\n"
        '            targets: [ "Foo" ]\n'
        "        ),\n"
        "    ],\n"
        "    targets: [\n"
        '        .binaryTarget(name: "Foo", path: "Foo.xcframework"),\n'
        "    ]\n"
        ")\n"
    )
    fw = work / "Foo.xcframework"
    fw.mkdir()
    (fw / "Info.plist").write_text(
        '<?xml version="1.0"?>'
        '<plist version="1.0"><dict>'
        "<key>AvailableLibraries</key><array/>"
        "<key>CFBundlePackageType</key><string>XFWK</string>"
        "<key>XCFrameworkFormatVersion</key><string>1.0</string>"
        "</dict></plist>"
    )

    # 1) Confirm the pre-demote manifest is rejected by SPM with the
    #    documented "binary-only products must be automatic" message.
    pre = subprocess.run(
        ["swift", "package", "dump-package"],
        cwd=str(work), capture_output=True, text=True,
    )
    _assert(pre.returncode != 0,
            "expected SPM to reject .library(type: .dynamic) wrapping "
            f"a binary target, but dump-package succeeded:\n{pre.stdout[-400:]}")
    _assert("automatic library products" in pre.stderr or
            "binary product" in pre.stderr,
            f"unexpected dump-package error shape:\n{pre.stderr[-400:]}")

    # 2) Demote and re-write.
    out = edit_demote_synthetic_product(pkg.read_text(), "FooDynamic")
    pkg.write_text(out)

    # 3) Re-run dump-package; this MUST succeed.
    post = subprocess.run(
        ["swift", "package", "dump-package"],
        cwd=str(work), capture_output=True, text=True,
    )
    _assert(post.returncode == 0,
            f"post-demote dump-package failed (exit {post.returncode}):\n"
            f"stderr:\n{post.stderr[-400:]}")
    # The product itself must still appear in dump-package output (we
    # demoted, not removed).
    _assert('"FooDynamic"' in post.stdout,
            "demoted product disappeared from the dumped manifest")


def _selftest_compute_dedup_substitutions_single_target_sibling() -> None:
    """[Codex P2 r5 regression — happy path] Sibling unit with
    `source_targets == [dep_t]` IS substituted: the xcframework was
    built as that single module, so re-pointing `.target(name: T)` at
    it is a 1:1 swap. This is the Stripe shape (each sub-product is
    a single-target library).
    """
    BU = tool.BuildUnit
    sibling = BU(name="StripeCore", scheme="StripeCore",
                 framework_name="StripeCore", language="Swift",
                 archive_strategy="archive", source_targets=["StripeCore"])
    umbrella = BU(name="Stripe", scheme="Stripe",
                  framework_name="Stripe", language="Swift",
                  archive_strategy="archive", source_targets=["Stripe"])
    target_to_unit = {"StripeCore": sibling, "Stripe": umbrella}
    sibling_xcf = Path("/build/StripeCore.xcframework")
    built = {"StripeCore": tool.ExecutedUnit(
        name="StripeCore", xcframework_path=sibling_xcf,
        framework_name="StripeCore",
    )}
    deps = {"Stripe": {"StripeCore"}, "StripeCore": set()}

    subs = _compute_dedup_substitutions(
        unit=umbrella,
        target_to_unit=target_to_unit,
        built_by_unit=built,
        target_deps=deps,
    )
    _assert(subs == [("StripeCore", sibling_xcf)],
            f"expected [('StripeCore', {sibling_xcf})], got {subs!r}")


def _selftest_compute_dedup_substitutions_multi_target_sibling_skipped() -> None:
    """[Codex P2 r5 regression — the bug] Sibling unit owns MULTIPLE
    source targets (`.library(name: "Kit", targets: ["A", "B"])`).
    Its single emitted `Kit.xcframework` is not a clean 1:1
    stand-in for either A or B — the binary's contained module is
    the product, not the individual target — so substitution must
    be skipped. The consumer falls back to legacy behavior (SPM
    statically embeds A's symbols into the umbrella).
    """
    BU = tool.BuildUnit
    sibling = BU(name="Kit", scheme="Kit",
                 framework_name="Kit", language="Swift",
                 archive_strategy="archive", source_targets=["A", "B"])
    consumer = BU(name="Umbrella", scheme="Umbrella",
                  framework_name="Umbrella", language="Swift",
                  archive_strategy="archive", source_targets=["Z"])
    target_to_unit = {"A": sibling, "B": sibling, "Z": consumer}
    built = {"Kit": tool.ExecutedUnit(
        name="Kit", xcframework_path=Path("/build/Kit.xcframework"),
        framework_name="Kit",
    )}
    deps = {"Z": {"A"}, "A": set(), "B": set()}

    subs = _compute_dedup_substitutions(
        unit=consumer,
        target_to_unit=target_to_unit,
        built_by_unit=built,
        target_deps=deps,
    )
    _assert(subs == [],
            f"multi-target sibling must not be substituted, got {subs!r}")


def _selftest_compute_dedup_substitutions_aliased_single_target() -> None:
    """[Codex P2 r5 follow-up] Sibling unit's `name` differs from its
    single source_target (e.g. `.library(name: "Aliased", targets:
    ["A"])`). The 1:1 invariant is `source_targets == [dep_t]`, NOT
    name equality — substitution must still happen. The xcframework
    file is `Aliased.xcframework` whose contained framework binary
    holds the `A` module, and SPM resolves
    `.binaryTarget(name: "A", path: "Aliased.xcframework")` by
    reading the xcframework metadata.
    """
    BU = tool.BuildUnit
    sibling = BU(name="Aliased", scheme="Aliased",
                 framework_name="Aliased", language="Swift",
                 archive_strategy="archive", source_targets=["A"])
    consumer = BU(name="Umbrella", scheme="Umbrella",
                  framework_name="Umbrella", language="Swift",
                  archive_strategy="archive", source_targets=["Z"])
    target_to_unit = {"A": sibling, "Z": consumer}
    aliased_xcf = Path("/build/Aliased.xcframework")
    built = {"Aliased": tool.ExecutedUnit(
        name="Aliased", xcframework_path=aliased_xcf,
        framework_name="Aliased",
    )}
    deps = {"Z": {"A"}, "A": set()}

    subs = _compute_dedup_substitutions(
        unit=consumer,
        target_to_unit=target_to_unit,
        built_by_unit=built,
        target_deps=deps,
    )
    _assert(subs == [("A", aliased_xcf)],
            f"aliased single-target sibling must substitute, got {subs!r}")


def _selftest_compute_dedup_substitutions_unbuilt_sibling_skipped() -> None:
    """[Codex P2 r5 follow-up] Sibling unit is registered in
    `target_to_unit` but hasn't run yet (`built_by_unit` lookup
    misses). Substitution must be skipped — without an xcframework
    path on disk, there's nothing to point `.binaryTarget` at.
    """
    BU = tool.BuildUnit
    sibling = BU(name="StripeCore", scheme="StripeCore",
                 framework_name="StripeCore", language="Swift",
                 archive_strategy="archive", source_targets=["StripeCore"])
    consumer = BU(name="Stripe", scheme="Stripe",
                  framework_name="Stripe", language="Swift",
                  archive_strategy="archive", source_targets=["Stripe"])
    target_to_unit = {"StripeCore": sibling, "Stripe": consumer}
    built: Dict[str, tool.ExecutedUnit] = {}  # nothing built yet
    deps = {"Stripe": {"StripeCore"}, "StripeCore": set()}

    subs = _compute_dedup_substitutions(
        unit=consumer,
        target_to_unit=target_to_unit,
        built_by_unit=built,
        target_deps=deps,
    )
    _assert(subs == [],
            f"unbuilt sibling must not be substituted, got {subs!r}")


def _selftest_compute_dedup_substitutions_mixes_single_and_multi() -> None:
    """[Codex P2 r5 follow-up] When a consumer transitively depends on
    BOTH a single-target sibling (substitutable) AND a multi-target
    sibling (not substitutable), only the single-target dep is
    rewritten — the multi-target one is left alone for legacy
    static-embed fallback.
    """
    BU = tool.BuildUnit
    core = BU(name="Core", scheme="Core",
              framework_name="Core", language="Swift",
              archive_strategy="archive", source_targets=["Core"])
    kit = BU(name="Kit", scheme="Kit",
             framework_name="Kit", language="Swift",
             archive_strategy="archive", source_targets=["A", "B"])
    consumer = BU(name="Umbrella", scheme="Umbrella",
                  framework_name="Umbrella", language="Swift",
                  archive_strategy="archive", source_targets=["Z"])
    target_to_unit = {
        "Core": core, "A": kit, "B": kit, "Z": consumer,
    }
    core_xcf = Path("/build/Core.xcframework")
    kit_xcf = Path("/build/Kit.xcframework")
    built = {
        "Core": tool.ExecutedUnit(
            name="Core", xcframework_path=core_xcf, framework_name="Core",
        ),
        "Kit": tool.ExecutedUnit(
            name="Kit", xcframework_path=kit_xcf, framework_name="Kit",
        ),
    }
    deps = {
        "Z": {"Core", "A"},
        "Core": set(), "A": set(), "B": set(),
    }

    subs = _compute_dedup_substitutions(
        unit=consumer,
        target_to_unit=target_to_unit,
        built_by_unit=built,
        target_deps=deps,
    )
    # Sorted dep walk visits 'A' (multi-target sibling — skip) then 'Core' (substitute).
    _assert(subs == [("Core", core_xcf)],
            f"mix should yield only Core substitution, got {subs!r}")


def _selftest_compute_dedup_substitutions_synth_dynamic_target_skipped() -> None:
    """[Codex final-review High] When a sibling's manifest carries a
    `synth_dynamic_library` edit naming its single source target,
    rewriting that target to `.binaryTarget(...)` would leave the
    surviving `.library(type: .dynamic, targets: [T])` product
    pointing at a binary-only target. SPM rejects exactly that shape
    at the next archive's manifest parse:

        "invalid type for binary product; products referencing only
         binary targets must be executable or automatic library
         products"

    The substitution must be skipped (slower per-unit re-compile of T,
    but correct manifest state).
    """
    BU = tool.BuildUnit
    sibling = BU(name="StripeCore", scheme="StripeCoreDynamic",
                 framework_name="StripeCore", language="Swift",
                 archive_strategy="archive", source_targets=["StripeCore"])
    umbrella = BU(name="Stripe", scheme="StripeDynamic",
                  framework_name="Stripe", language="Swift",
                  archive_strategy="archive", source_targets=["Stripe"])
    target_to_unit = {"StripeCore": sibling, "Stripe": umbrella}
    built = {"StripeCore": tool.ExecutedUnit(
        name="StripeCore",
        xcframework_path=Path("/build/StripeCore.xcframework"),
        framework_name="StripeCore",
    )}
    deps = {"Stripe": {"StripeCore"}, "StripeCore": set()}

    subs = _compute_dedup_substitutions(
        unit=umbrella,
        target_to_unit=target_to_unit,
        built_by_unit=built,
        target_deps=deps,
        synth_dynamic_protected={"StripeCore"},
    )
    _assert(subs == [],
            "synth_dynamic_library-protected target must not be substituted; "
            f"got {subs!r}")


def _selftest_synth_dynamic_protected_targets_helper() -> None:
    """[Codex final-review round 2 High] Helper collects every target
    named by either kind of synthetic dynamic-library edit. Both
    `synth_dynamic_library` AND `synth_library` are applied through
    `_invoke_swift_add_product` with `--type dynamic-library`, so both
    produce `.library(type: .dynamic, ...)` products that SPM rejects
    when their target list contains only `.binaryTarget` entries. The
    original round-1 fix only protected `synth_dynamic_library`, which
    left `--target T` builds exposed to the same regression — this
    test pins the corrected behavior.
    """
    edits = [
        tool.PackageSwiftEdit(
            kind="synth_dynamic_library",
            product_name="StripeCoreDynamic",
            targets=["StripeCore"],
        ),
        tool.PackageSwiftEdit(
            kind="synth_library",
            product_name="MyTarget",
            targets=["MyTarget"],
        ),
        tool.PackageSwiftEdit(
            kind="synth_dynamic_library",
            product_name="KitDynamic",
            targets=["A", "B"],
        ),
    ]
    protected = tool._synth_dynamic_protected_targets(edits)
    _assert(
        protected == {"StripeCore", "A", "B", "MyTarget"},
        f"expected protected set to cover BOTH synth_dynamic_library and "
        f"synth_library targets, got {protected!r}",
    )


def _selftest_apply_dedup_overlap_substitutions_guards_unsupported_constructs(tmp_root: Path) -> None:
    """[Codex P2 round 4 regression] When `prepare()` takes its no-op
    path (no `package_swift_edits`), `apply_package_swift_edits` is
    skipped — and with it the
    `_assert_no_unsupported_swift_constructs` guard. Execute-time
    dedup substitutions must therefore re-assert the guard themselves;
    otherwise a manifest containing a `#"..."#` raw string that happens
    to mention `.target(name: "Foo", ...)` would be silently mis-parsed
    by `_make_code_token_view` (which doesn't track raw strings) and
    the dedup edit would land inside the string body.

    Raw strings remain the only construct in this guard's scope.
    Triple-quoted strings and `\\(...)` interpolation are both
    handled by the walker now — see
    `_selftest_apply_dedup_overlap_substitutions_handles_triple_quoted`
    for the positive triple-quoted case, and
    `_selftest_make_code_token_view_blanks_strings_and_comments` for
    interpolation coverage.
    """
    base = tmp_root / "p2_r4_dedup_guard"
    base.mkdir(parents=True, exist_ok=True)

    src = '''// swift-tools-version:5.7
import PackageDescription

let docs = #".target(name: "Foo", path: "OldFoo")"#

let p = Package(
    name: "x",
    targets: [
        .target(
            name: "Foo",
            path: "RealFoo"
        ),
    ]
)
'''
    staged = base / "raw_string"
    staged.mkdir(parents=True, exist_ok=True)
    manifest = staged / "Package.swift"
    manifest.write_text(src)
    original_bytes = manifest.read_bytes()
    try:
        _apply_dedup_overlap_substitutions(
            staged_dir=staged,
            substitutions=[("Foo", Path("/build/Foo.xcframework"))],
            unit_name="UnitX",
            verbose=False,
        )
        _assert(False,
                f"expected ExecuteError; manifest was rewritten:\n"
                f"{manifest.read_text()}")
    except tool.ExecuteError as exc:
        msg = str(exc)
        _assert("dedup-overlap" in msg,
                f"error message missing dedup context: {msg!r}")
        _assert(
            "Swift" in msg or "raw string" in msg,
            f"error message doesn't name the Swift construct: {msg!r}",
        )
        _assert("--no-dedup-overlap" in msg,
                f"error message missing escape hatch hint: {msg!r}")
    # Manifest on disk must be byte-identical to the original.
    _assert(manifest.read_bytes() == original_bytes,
            f"manifest was mutated despite the guard:\n"
            f"{manifest.read_text()}")


def _selftest_apply_dedup_overlap_substitutions_handles_triple_quoted(tmp_root: Path) -> None:
    """A manifest with a triple-quoted multi-line string that *happens
    to contain* `.target(name: "Foo", ...)` text must NOT cause the
    dedup-overlap edit to land inside the string body. The real
    `.target(...)` outside the string is rewritten to `.binaryTarget`;
    the triple-quoted body is preserved byte-for-byte.

    This is the positive counterpart to the raw-string guard test
    above — once `_skip_triple_quoted_string` taught the walker to
    treat `\"\"\"...\"\"\"` as opaque, the dedup edit can run safely
    on manifests like swift-collections's where multi-line strings
    appear in `traits:` descriptions.
    """
    base = tmp_root / "dedup_triple_quoted"
    base.mkdir(parents=True, exist_ok=True)
    src = '''// swift-tools-version:5.7
import PackageDescription

let docs = """
.target(name: "Foo", path: "OldFoo")
"""

let p = Package(
    name: "x",
    targets: [
        .target(
            name: "Foo",
            path: "RealFoo"
        ),
    ]
)
'''
    staged = base
    manifest = staged / "Package.swift"
    manifest.write_text(src)

    _apply_dedup_overlap_substitutions(
        staged_dir=staged,
        substitutions=[("Foo", Path("/build/Foo.xcframework"))],
        unit_name="UnitX",
        verbose=False,
    )
    after = manifest.read_text()
    # The triple-quoted docstring body must be byte-identical — no
    # in-string `.target` text was touched.
    _assert(
        '.target(name: "Foo", path: "OldFoo")' in after,
        f"in-string `.target` text was incorrectly rewritten:\n{after}",
    )
    # The real `.target(...)` call (the one with path: "RealFoo") was
    # rewritten to `.binaryTarget(...)`.
    _assert(
        '.binaryTarget' in after,
        f"expected real .target call to be rewritten to .binaryTarget:\n{after}",
    )
    _assert(
        'path: "RealFoo"' not in after,
        f"original `path: \"RealFoo\"` should be replaced by the binary path:\n{after}",
    )


def _selftest_skip_triple_quoted_string_handles_interpolation() -> None:
    """[codex-review r1 Medium] `_skip_triple_quoted_string` must recurse
    through `\\(...)` interpolation. Swift legally allows a nested
    triple-quoted string literal inside the interpolation expression of
    an outer triple-quoted string. The naive 2-char-escape treatment of
    `\\(` would let that nested `\"\"\"` masquerade as the outer closer
    and cause the walker to think the outer string ends prematurely.
    """
    triple = chr(34) * 3
    # Case 1: nested triple-quoted string inside interpolation.
    text = f'let x = {triple}\nA \\({triple}\ninner\n{triple}) B\n{triple}\nrest'
    open_idx = text.index(triple)
    close_idx = tool._skip_triple_quoted_string(text, open_idx)
    _assert(close_idx != -1, "interpolation-bearing triple-quoted string was reported unterminated")
    # The closer must be the OUTER triple-quote (the one before "rest"),
    # not the nested one inside the interpolation.
    expected_close = text.rindex(triple) + 3
    _assert(close_idx == expected_close,
            f"expected outer closer at {expected_close} (start of 'rest'); got {close_idx}")
    # Case 2: interpolation with non-string content (sanity).
    text2 = f'let y = {triple}\nA \\(1 + 2) B\n{triple}\n'
    open2 = text2.index(triple)
    close2 = tool._skip_triple_quoted_string(text2, open2)
    _assert(close2 != -1, "simple interpolation case reported unterminated")
    _assert(close2 == text2.rindex(triple) + 3,
            f"plain interpolation close mismatch: got {close2}")
    # Case 3: interpolation with unmatched paren → conservative -1.
    text3 = f'let z = {triple}\n\\(unmatched\n{triple}\n'
    open3 = text3.index(triple)
    close3 = tool._skip_triple_quoted_string(text3, open3)
    _assert(close3 == -1, f"expected -1 on unmatched interpolation paren; got {close3}")


def _selftest_apply_dedup_overlap_substitutions_triple_quoted_interpolation(tmp_root: Path) -> None:
    """[codex-review r1 Medium] Integration-flavored case: a triple-quoted
    string whose `\\(...)` interpolation contains a nested triple-quoted
    string with a `.target(name: "Foo", ...)` literal. The outer string
    must be treated as opaque end-to-end so the real `.target(...)`
    call below it is the one rewritten.
    """
    base = tmp_root / "dedup_triple_quoted_interp"
    base.mkdir(parents=True, exist_ok=True)
    triple = chr(34) * 3
    src = (
        '// swift-tools-version:5.7\n'
        'import PackageDescription\n'
        '\n'
        f'let docs = {triple}\n'
        f'prefix \\({triple}\n'
        '.target(name: "Foo", path: "InsideInterpolation")\n'
        f'{triple}.uppercased()) suffix\n'
        f'{triple}\n'
        '\n'
        'let p = Package(\n'
        '    name: "x",\n'
        '    targets: [\n'
        '        .target(\n'
        '            name: "Foo",\n'
        '            path: "RealFoo"\n'
        '        ),\n'
        '    ]\n'
        ')\n'
    )
    staged = base
    manifest = staged / "Package.swift"
    manifest.write_text(src)

    _apply_dedup_overlap_substitutions(
        staged_dir=staged,
        substitutions=[("Foo", Path("/build/Foo.xcframework"))],
        unit_name="UnitX",
        verbose=False,
    )
    after = manifest.read_text()
    _assert(
        '.target(name: "Foo", path: "InsideInterpolation")' in after,
        f"nested-interpolation in-string `.target` was incorrectly rewritten:\n{after}",
    )
    _assert(
        '.binaryTarget' in after,
        f"expected real .target call to be rewritten to .binaryTarget:\n{after}",
    )
    _assert(
        'path: "RealFoo"' not in after,
        f"original `path: \"RealFoo\"` should be replaced by the binary path:\n{after}",
    )


def _selftest_unsupported_swift_constructs_allows_rawstring_mention_in_triple_quoted_body() -> None:
    # [codex-review r1 Low] A `traits:` description (or any other
    # multi-line string body) that legitimately mentions raw-string
    # syntax in prose must NOT trip the raw-string guard. Triple-quoted
    # bodies are blanked before the `#"` scan; single-quoted strings
    # are still scanned (matching the documented heuristic).
    triple = chr(34) * 3
    src = (
        '// swift-tools-version:5.7\n'
        'import PackageDescription\n'
        f'let docs = {triple}\n'
        'See also `#"some raw text"#` — Swift docs say this is a raw string.\n'
        f'{triple}\n'
        'let p = Package(name: "x")\n'
    )
    # Should NOT raise.
    tool._assert_no_unsupported_swift_constructs(src)
    # Sanity: a REAL #" outside a triple-quoted string still raises.
    bad = '// swift-tools-version:5.7\nlet s = #"hello"#\n'
    try:
        tool._assert_no_unsupported_swift_constructs(bad)
    except tool.PrepareUserError as exc:
        _assert("raw string" in str(exc),
                f"expected raw-string error; got: {exc}")
    else:
        _assert(False, "expected PrepareUserError on real raw-string use")
    # [codex-review r2 Medium] A RAW triple-quoted string (`#"""..."""#`)
    # must still be rejected. The selective blanker must NOT hide the
    # `#"` opener when it's the start of a raw triple-quoted literal,
    # otherwise the raw-string guard silently passes a construct the
    # walker can't handle.
    raw_triple = (
        '// swift-tools-version:5.7\n'
        'let s = #' + chr(34) * 3 + '\nhello\n' + chr(34) * 3 + '#\n'
    )
    try:
        tool._assert_no_unsupported_swift_constructs(raw_triple)
    except tool.PrepareUserError as exc:
        _assert("raw string" in str(exc),
                f"expected raw-string error on raw triple; got: {exc}")
    else:
        _assert(False, "expected PrepareUserError on raw triple-quoted string")
    # [codex-review r3 Medium] A raw string `#"..."#` embedded inside an
    # interpolation expression `\(...)` of an ORDINARY triple-quoted
    # string is real Swift code and must still trip the guard. The
    # selective blanker must preserve interpolation expression text
    # so the `#"` scan sees the raw literal nested inside `\(...)`.
    embedded_raw = (
        '// swift-tools-version:5.7\n'
        'let s = ' + chr(34) * 3 + '\n\\(#"raw"#)\n' + chr(34) * 3 + '\n'
    )
    try:
        tool._assert_no_unsupported_swift_constructs(embedded_raw)
    except tool.PrepareUserError as exc:
        _assert("raw string" in str(exc),
                f"expected raw-string error on interpolation-embedded raw; got: {exc}")
    else:
        _assert(False,
                "expected PrepareUserError on raw string inside triple-quoted interpolation")
    # [grok-cli-review r3 Low] Once a raw triple-quoted span is
    # detected, the helper must skip past the WHOLE span (opener +
    # body + closer, with matching hash count) — not just one
    # character past the opener. Otherwise the closer's `"""` is
    # mis-parsed as an ordinary triple opener, pushing a phantom
    # prose frame that blanks every char after the raw triple.
    # Without this property the helper's behavior diverges from
    # its documented contract ("raw triples are left ENTIRELY
    # intact") even though the guard's safety property still holds.
    raw_then_code = (
        'let s = #' + chr(34) * 3 + '\nhello\n' + chr(34) * 3 + '#\nlet x = 1\n'
    )
    blanked = tool._blank_triple_quoted_bodies(raw_then_code)
    _assert(blanked == raw_then_code,
            "raw triple span must be preserved verbatim; "
            f"got divergence at idx {next((i for i,(a,b) in enumerate(zip(blanked, raw_then_code)) if a != b), -1)}")
    # And with a 2-hash raw triple (`##\"\"\"...\"\"\"##`), same property:
    raw2 = (
        'let s = ##' + chr(34) * 3 + '\nbody\n' + chr(34) * 3 + '##\nlet y = 2\n'
    )
    blanked2 = tool._blank_triple_quoted_bodies(raw2)
    _assert(blanked2 == raw2,
            "2-hash raw triple span must be preserved verbatim")


def _selftest_prune_child_strips_test_targets() -> None:
    # Surfaced by swift-perception (TCA transitive, 2026-05-21): the
    # package's `PerceptionMacrosTests` testTarget referenced
    # `.product(name: "MacroTesting", package: "swift-macro-testing")`.
    # Pass C correctly identified `swift-macro-testing` as unused by any
    # library product and pruned it from the dependencies array, but the
    # dangling `.product()` reference inside the testTarget then made
    # `swift package dump-package` reject the pruned manifest.
    # Pass B (`_strip_test_targets`) drops `.testTarget(...)` entries
    # from the `targets:` array upfront so Pass C runs on a manifest
    # where no testTarget references survive to dangle.
    src = (
        '// swift-tools-version: 5.9\n'
        'import PackageDescription\n'
        'let package = Package(\n'
        '  name: "swift-perception",\n'
        '  products: [.library(name: "Perception", targets: ["Perception"])],\n'
        '  dependencies: [\n'
        '    .package(url: "https://github.com/pointfreeco/swift-macro-testing", from: "0.1.0"),\n'
        '    .package(url: "https://github.com/swiftlang/swift-syntax", from: "509.0.0"),\n'
        '  ],\n'
        '  targets: [\n'
        '    .target(name: "Perception", dependencies: ["PerceptionMacros"]),\n'
        '    .testTarget(name: "PerceptionTests", dependencies: ["Perception"]),\n'
        '    .macro(name: "PerceptionMacros", dependencies: [\n'
        '      .product(name: "SwiftSyntaxMacros", package: "swift-syntax"),\n'
        '    ]),\n'
        '    .testTarget(name: "PerceptionMacrosTests", dependencies: [\n'
        '      "PerceptionMacros",\n'
        '      .product(name: "MacroTesting", package: "swift-macro-testing"),\n'
        '    ]),\n'
        '  ]\n'
        ')\n'
    )
    out, reasons = tool._strip_test_targets(src)
    _assert(".testTarget(" not in out,
            f"Pass B should drop every .testTarget(...) entry; got:\n{out}")
    _assert(".target(name: \"Perception\"" in out,
            "Pass B must not strip non-test targets")
    _assert(".macro(name: \"PerceptionMacros\"" in out,
            "Pass B must not strip macro targets")
    names = sorted(reasons)
    _assert(names == [
        ".testTarget(PerceptionMacrosTests)",
        ".testTarget(PerceptionTests)",
    ], f"unexpected removal reasons: {reasons}")
    # No-op case: a manifest with no testTargets is byte-identical.
    no_tests = (
        'let package = Package(\n'
        '  name: "x",\n'
        '  targets: [.target(name: "X")]\n'
        ')\n'
    )
    out2, reasons2 = tool._strip_test_targets(no_tests)
    _assert(out2 == no_tests and not reasons2,
            "Pass B must no-op when no testTargets present")
    # Non-literal `targets:` (e.g., `targets: libTargets + testTargets`)
    # must safely no-op rather than mangle.
    non_literal = (
        'let testTargets: [Target] = [.testTarget(name: "T")]\n'
        'let libTargets: [Target] = [.target(name: "L")]\n'
        'let package = Package(\n'
        '  name: "x",\n'
        '  targets: libTargets + testTargets\n'
        ')\n'
    )
    out3, reasons3 = tool._strip_test_targets(non_literal)
    _assert(out3 == non_literal and not reasons3,
            "Pass B must no-op on non-literal targets: expression")


def _selftest_walkers_dispatch_triple_quoted_before_single_quoted() -> None:
    # [grok-cli-review r1 Medium] Every depth-aware walker in prepare.py
    # that has its own string-skipping state machine must detect a
    # triple-quote opener BEFORE falling into the single-quote handler.
    # Otherwise the first quote of the opener is treated as an empty
    # single-quoted string and the body becomes "code" to the rest of
    # the walker — enabling false-match of .target(...) literals inside
    # what is actually opaque multi-line text.
    #
    # Test strategy: build a span containing a triple-quoted literal
    # whose body holds tokens each walker would otherwise latch onto,
    # plus a real depth-0 token outside the string. Assert the walker
    # returns only the real token / position / collection.
    triple = chr(34) * 3
    # `_flatten_to_top_level`: a triple-quoted literal at depth-0 must
    # appear verbatim in the flattened output (like a single-quoted
    # literal at depth-0 would). Anything inside paren-nested scope is
    # collapsed to a single space by design — so the test stages the
    # triple-quoted at depth-0.
    flat_in = f'let docs = {triple}\n.target(name: "Inside")\n{triple}\nlet a = b'
    flat = tool._flatten_to_top_level(flat_in)
    _assert(triple in flat,
            f"_flatten_to_top_level lost triple-quoted span: {flat!r}")
    _assert('.target(name: "Inside")' in flat,
            f"_flatten_to_top_level lost triple-quoted body: {flat!r}")

    # `_strip_comments_to_spaces`: triple-quoted body must round-trip
    # byte-identical (no comments to strip; string preserved).
    src1 = f'let d = {triple}\n.target(name: "Inside") // not a comment\n/* nor this */\n{triple}\nlet real = 1\n'
    stripped = tool._strip_comments_to_spaces(src1)
    _assert(len(stripped) == len(src1),
            "_strip_comments_to_spaces changed length")
    _assert(triple in stripped,
            "_strip_comments_to_spaces destroyed triple-quoted delimiters")
    _assert(".target(name:" in stripped,
            "_strip_comments_to_spaces should preserve string body verbatim")

    # `_collect_depth_zero_string_literals`: triple-quoted body must NOT
    # be collected (it's not a single-line dep name). A real depth-0
    # single-quoted string outside it must be collected. Both staged
    # at depth-0 to match the function's contract.
    src2 = f'{triple}\nFakeName\n{triple} "RealName"'
    lits = tool._collect_depth_zero_string_literals(src2)
    _assert("RealName" in lits,
            f"_collect_depth_zero_string_literals missed real literal: {lits!r}")
    _assert("FakeName" not in lits and "\nFakeName\n" not in lits,
            f"_collect_depth_zero_string_literals leaked triple-quoted body: {lits!r}")

    # `_find_depth_zero_string_literal_positions`: same shape, position API.
    src3 = f'{triple}\nGhost\n{triple} "Marker"'
    positions = tool._find_depth_zero_string_literal_positions(
        src3, 0, len(src3), "Marker"
    )
    _assert(len(positions) == 1,
            f"expected exactly one 'Marker' match; got {positions!r}")
    _assert(src3[positions[0][0]:positions[0][1]] == '"Marker"',
            f"position span mismatch: {src3[positions[0][0]:positions[0][1]]!r}")
    positions_ghost = tool._find_depth_zero_string_literal_positions(
        src3, 0, len(src3), "Ghost"
    )
    _assert(positions_ghost == [],
            f"should not match 'Ghost' inside a triple-quoted body: {positions_ghost!r}")

    # `_collect_depth_zero_target_or_byname_call_names`: a `.target(name:...)`
    # literal embedded inside a triple-quoted body must NOT be collected.
    src4 = (
        f'let pkg = Package(targets: [{triple}\n'
        '.target(name: "Phantom")\n'
        f'{triple}, .target(name: "Real")])'
    )
    # The function operates on the body inside `targets:[...]`. Build that span.
    open_idx = src4.index("[", src4.index("targets:"))
    close_idx = tool._balanced_close(src4, open_idx)
    body = src4[open_idx + 1:close_idx]
    names = tool._collect_depth_zero_target_or_byname_call_names(body)
    _assert("Real" in names,
            f"missed real .target name: {names!r}")
    _assert("Phantom" not in names,
            f"leaked .target name from inside triple-quoted body: {names!r}")

    # `_depth_zero_view`: the triple-quoted span must be blanked.
    src5 = f'let pkg = Package(targets: [{triple}\n.target(name: "Phantom")\n{triple}])'
    view = tool._depth_zero_view(src5)
    _assert(len(view) == len(src5),
            "_depth_zero_view length must match input")
    _assert("Phantom" not in view,
            f"_depth_zero_view leaked triple-quoted body: {view!r}")


def _selftest_make_code_token_view_blanks_strings_and_comments() -> None:
    """[Codex P2 round 3] Direct unit on `_make_code_token_view`:
    comment bodies AND string-literal spans (delimiters + body) are
    blanked into spaces, newlines are preserved, and length matches
    the input exactly so callers can index back into the original
    text using offsets discovered in the view.
    """
    src = (
        'let a = "hello"\n'
        '// .target(name: "X")\n'
        '/* .binaryTarget(name: "Y") */\n'
        '.target(name: "Real", path: "p")\n'
    )
    view = tool._make_code_token_view(src)
    _assert(len(view) == len(src),
            f"length mismatch: src={len(src)} view={len(view)}")
    # Newlines preserved.
    _assert(view.count("\n") == src.count("\n"),
            "newline count differs")
    # Strings fully blanked (no `hello` in view).
    _assert("hello" not in view, f"string body leaked into view:\n{view!r}")
    # Comment bodies fully blanked (no in-comment .target text).
    _assert(".target(name: \"X\")" not in view,
            f"line-comment leaked into view:\n{view!r}")
    _assert(".binaryTarget(name: \"Y\")" not in view,
            f"block-comment leaked into view:\n{view!r}")
    # Real `.target(` token IS visible at depth-0 of the view (the
    # blanked-string view keeps the call's leading `.target(` intact;
    # only the `"Real"` and `"p"` argument bodies are blanked).
    _assert(".target(name:" in view,
            f"real call leading token missing from view:\n{view!r}")
    # Sanity: offsets line up — the index where `.target(name:` appears
    # in the view points at the same `.target(` substring in src.
    idx = view.find(".target(name:")
    _assert(idx >= 0 and src[idx:].startswith(".target(name:"),
            f"offsets diverged between view and src at idx={idx}")


def _selftest_compute_internal_target_deps_external_product_collision() -> None:
    """[Codex P2 regression] When a target depends on an EXTERNAL
    `.product("Foo", "external-pkg")` and the same package also
    declares an internal target named Foo, the `product` edge must
    NOT be reclassified as an internal sibling dep. Only `byName` and
    `target` shapes — the dump-package payload's true internal-edge
    encodings — count toward `compute_internal_target_deps`.
    """
    snap = {
        "name": "collide",
        "toolsVersion": {"_version": "5.7.0"},
        "platforms": [{"options": [], "platformName": "ios", "version": "13.0"}],
        "products": [],
        "targets": [
            # Sibling target also named Foo. The dep walker MUST NOT
            # connect A to this Foo via the external-product edge.
            {"name": "Foo", "type": "regular", "path": "Foo",
             "publicHeadersPath": None, "dependencies": []},
            # A only depends on the EXTERNAL product also named Foo.
            {"name": "A", "type": "regular", "path": "A",
             "publicHeadersPath": None,
             "dependencies": [
                 {"product": ["Foo", "external-pkg", None, None]},
             ]},
        ],
    }
    pkg = _stripe_dump_package(snap)
    deps = tool.compute_internal_target_deps(pkg)
    _assert(deps["A"] == set(),
            f"A must have no internal deps (only external product), "
            f"got {deps['A']!r}")
    _assert(deps["Foo"] == set(),
            f"Foo is a leaf, got {deps['Foo']!r}")


def _selftest_compute_internal_target_deps_mixed_internal_and_external() -> None:
    """[Codex P2 follow-up] With BOTH a real internal `byName` edge
    AND an external `product` edge sharing the same name, only the
    internal edge survives. Verifies the walker walks raw shapes and
    de-duplicates correctly."""
    snap = {
        "name": "mixed",
        "toolsVersion": {"_version": "5.7.0"},
        "platforms": [{"options": [], "platformName": "ios", "version": "13.0"}],
        "products": [],
        "targets": [
            {"name": "Bar", "type": "regular", "path": "Bar",
             "publicHeadersPath": None, "dependencies": []},
            {"name": "Foo", "type": "regular", "path": "Foo",
             "publicHeadersPath": None, "dependencies": []},
            {"name": "A", "type": "regular", "path": "A",
             "publicHeadersPath": None,
             "dependencies": [
                 {"byName": ["Foo", None]},                                     # internal
                 {"product": ["Bar", "external-pkg", None, None]},              # external
                 {"product": ["Foo", "external-pkg", None, None]},              # external collision
                 {"target": ["Foo", None]},                                     # internal duplicate
             ]},
        ],
    }
    pkg = _stripe_dump_package(snap)
    deps = tool.compute_internal_target_deps(pkg)
    _assert(deps["A"] == {"Foo"},
            f"A should depend ONLY on internal Foo, got {deps['A']!r}")
    _assert("Bar" not in deps["A"],
            f"external Bar should not appear in A's deps, got {deps['A']!r}")


def _selftest_parse_xcresult_build_results() -> None:
    """The xcresulttool parser extracts target/message/source from the
    Xcode 16+ build-results JSON shape, drops malformed entries, and
    honors the `limit` parameter."""
    fixture = {
        "actionTitle": "Build",
        "destination": {"deviceId": "x", "deviceName": "iPhone", "architecture": "arm64", "modelName": "x", "osVersion": "18.0"},
        "startTime": 0.0,
        "endTime": 1.0,
        "status": "failed",
        "errorCount": 3,
        "warningCount": 0,
        "analyzerWarningCount": 0,
        "analyzerWarnings": [],
        "warnings": [],
        "errors": [
            {
                "issueType": "Swift Compiler Error",
                "message": "cannot find 'Foo' in scope",
                "targetName": "MyTarget",
                "sourceURL": "file:///x/y.swift",
            },
            {
                "issueType": "Swift Compiler Error",
                "message": "expected expression",
                "targetName": "MyTarget",
            },
            {
                # malformed entry — no message field — should be picked
                # up but produce empty strings, not crash.
                "issueType": "Misc",
            },
            "garbage non-dict entry — must be silently dropped",
        ],
    }
    out = _parse_xcresult_build_results(fixture, limit=10)
    _assert(len(out) == 3, f"expected 3 parsed errors, got {len(out)}")
    _assert(out[0]["target"] == "MyTarget", f"first target: {out[0]}")
    _assert("Foo" in out[0]["message"], f"first message: {out[0]}")
    _assert("y.swift" in out[0]["source"], f"first source: {out[0]}")
    # Limit honored
    out2 = _parse_xcresult_build_results(fixture, limit=2)
    _assert(len(out2) == 2, f"limit=2 returned {len(out2)}")
    # Bad shapes return []
    _assert(_parse_xcresult_build_results({}, limit=5) == [], "empty dict")
    _assert(_parse_xcresult_build_results({"errors": "not a list"}, limit=5) == [], "non-list errors")
    _assert(_parse_xcresult_build_results("not a dict", limit=5) == [], "non-dict input")


def _selftest_diagnostics_scan_known_patterns() -> None:
    """`diagnostics.scan` returns a `Diagnosis` for every entry in the
    canonical pattern table — these are the surfaces the ROADMAP names
    explicitly. Each match also exposes the substring that fired (for
    telemetry / future Codex review)."""
    from spm_to_xcframework.diagnostics import scan, format_block

    # Macro plugin pre-build failure (swift-syntax not found at archive)
    diag = scan("error: Unable to find module dependency: 'SwiftSyntax'")
    _assert(diag is not None, "macro plugin pattern must match")
    _assert("macro plugin" in diag.headline.lower(), diag.headline)
    _assert("pre-build" in diag.suggestion.lower(), diag.suggestion)
    _assert("SwiftSyntax" in diag.pattern, diag.pattern)

    # Library-evolution resilience boundary (currently auto-recovered)
    diag = scan("Switch covers known cases, but Foo may have additional unknown values")
    _assert(diag is not None, "resilience pattern must match")
    _assert("library-evolution" in diag.headline.lower(), diag.headline)
    _assert("dedup-overlap" in diag.suggestion.lower(), diag.suggestion)

    # Xcode 26.3 / swift-collections 1.5.x bug
    diag = scan(
        "error: '@_lifetime' attribute is only valid when experimental "
        "feature Lifetimes is enabled"
    )
    _assert(diag is not None, "lifetimes pattern must match")
    _assert("swift-collections" in diag.headline.lower(), diag.headline)

    # Missing sibling target → --target hint. Wording was softened per
    # Codex review (the linker diagnostic isn't always a sibling miss),
    # but `--target` and `--include-deps` must still appear so the user
    # has actionable next steps for the common SPM case.
    diag = scan("ld: warning: Could not find or use auto-linked library 'StripeCore'")
    _assert(diag is not None, "auto-linked library pattern must match")
    _assert("--target" in diag.suggestion, diag.suggestion)
    _assert("--include-deps" in diag.suggestion, diag.suggestion)

    # Vendored binary built with mismatched swiftc
    diag = scan("module 'Foo' was built with a different version of Swift")
    _assert(diag is not None, "binary-mismatch pattern must match")
    _assert("binarytarget" in diag.headline.lower(), diag.headline)

    # SPM build-tool plugin script failure. Wording was softened per
    # Codex review ("most often an SPM `.plugin(...)`") because the
    # diagnostic is generic Xcode wording that can also fire for
    # user-added Run Script phases.
    diag = scan(
        "Command PhaseScriptExecution failed with a nonzero exit code"
    )
    _assert(diag is not None, "phase-script-execution pattern must match")
    _assert("shell-script" in diag.headline.lower() or
            "build phase" in diag.headline.lower(), diag.headline)
    _assert("most often" in diag.suggestion.lower() or
            ".plugin" in diag.suggestion.lower(), diag.suggestion)

    # Case-insensitive matching: the pattern table holds lower-case
    # substrings but real xcodebuild output may capitalise differently.
    diag = scan("CYCLE IN DEPENDENCIES BETWEEN TARGETS")
    _assert(diag is not None, "scan must be case-insensitive")
    _assert("cycle" in diag.headline.lower(), diag.headline)

    # Misses return None (unknown text must fall through, never raise).
    _assert(scan("totally unrelated build failure no pattern matches") is None,
            "unknown text must return None")
    _assert(scan("") is None, "empty input returns None")

    # format_block shape contract: stable two-line "Diagnosis: ...\nTry: ..."
    diag = scan("'@_lifetime' attribute is only valid when experimental feature Lifetimes is enabled")
    block = format_block(diag)
    _assert(block.startswith("Diagnosis: "), block)
    _assert("\nTry: " in block, block)
    _assert(block.count("\n") == 1, f"block must be exactly two lines: {block!r}")


def _selftest_diagnostics_format_swift_package_failure_tools_version() -> None:
    """The SPM `swift package <cmd>` shaping helper extracts the
    proximate `error:` line and recognises the canonical tools-version
    mismatch shape so the user sees a "bump from X to Y" hint instead
    of just the raw stderr blast."""
    from spm_to_xcframework.diagnostics import format_swift_package_failure

    stderr = (
        "Building for distribution\n"
        "error: package at '/tmp/staged' is using Swift tools version 5.5.0 "
        "but the minimum required by the toolchain is 5.7.0\n"
        "  see https://example/help\n"
    )
    out = format_swift_package_failure("swift package dump-package", stderr)
    _assert(out.startswith("swift package dump-package failed."), out.splitlines()[0])
    # First error line surfaced verbatim with the `First error: ` prefix.
    _assert("First error: error: package at" in out, out)
    # Tools-version hint with both versions parsed out of the stderr.
    _assert("5.5.0" in out and "5.7.0" in out, out)
    _assert("bump the manifest" in out.lower() or "swift-tools-version" in out.lower(), out)
    # Raw stderr tail still present so the user has the same info they
    # had before — just better organised. The `---` divider separates
    # the actionable headline from the raw tail.
    _assert("---" in out, out)


def _selftest_diagnostics_format_swift_package_failure_tools_version_too_new() -> None:
    """The opposite-direction tools-version mismatch: a manifest pinned
    newer than the user's installed Xcode supports. Codex review caught
    that the original implementation only handled the manifest-too-old
    case and would give a wrong-direction hint here (telling the user
    to bump a manifest they don't control). The hint must point at
    upgrading Xcode, not editing the manifest down."""
    from spm_to_xcframework.diagnostics import format_swift_package_failure

    stderr = (
        "Resolving package graph\n"
        "error: package at '/tmp/staged' is using Swift tools version 6.0.0 "
        "but the installed version is 5.10.0\n"
    )
    out = format_swift_package_failure("swift package dump-package", stderr)
    _assert("6.0.0" in out and "5.10.0" in out, out)
    # Hint must point at upgrading Xcode, NOT at bumping the manifest down.
    _assert("upgrade xcode" in out.lower(), out)
    _assert("bump the manifest" not in out.lower(),
            f"wrong-direction hint must not fire: {out!r}")


def _selftest_diagnostics_format_swift_package_failure_unknown_shape() -> None:
    """Unrecognised stderr still gets the proximate `error:` line on top
    plus the raw tail. The point is to never make things worse — even
    when no pattern matches, the user still sees every byte they would
    have seen before."""
    from spm_to_xcframework.diagnostics import format_swift_package_failure

    stderr = (
        "Resolving package graph\n"
        "error: something completely unrecognised happened here\n"
        "more chatter\n"
    )
    out = format_swift_package_failure("swift package describe", stderr)
    _assert("First error: error: something completely unrecognised" in out, out)
    _assert("more chatter" in out, out)  # tail preserved
    # No `Try:` hint when the pattern doesn't match — silent miss.
    _assert("Try:" not in out, f"unmatched shape must not invent a Try: hint: {out!r}")


def _selftest_diagnostics_format_swift_package_failure_empty_stderr() -> None:
    """Empty stderr degrades gracefully — no first-error line, no hint,
    but the failure still surfaces (vs. raising in the formatter)."""
    from spm_to_xcframework.diagnostics import format_swift_package_failure

    out = format_swift_package_failure("swift package dump-package", "")
    _assert(out.startswith("swift package dump-package failed."), out)
    _assert("(no stderr)" in out, out)
    _assert("First error:" not in out, out)


def _selftest_diagnostics_format_execute_error_prepends_block() -> None:
    """`_format_execute_error` lands the Diagnosis/Try block above the
    raw xcresult errors when one of the patterns fires against the
    parsed error messages. Without a match, the output shape stays
    identical to the pre-diagnostics behaviour."""
    from spm_to_xcframework import _format_execute_error
    from pathlib import Path

    # Match case: the `@_lifetime` text fires the swift-collections 1.5.x
    # diagnosis. Headline must precede the `Top N error(s)` block.
    errors = [{
        "target": "InternalCollectionsUtilities",
        "message": "'@_lifetime' attribute is only valid when experimental "
                   "feature Lifetimes is enabled",
        "source": "file:///x/LifetimeOverride.swift",
        "issueType": "Swift Compiler Error",
    }]
    out = _format_execute_error(
        unit_name="OrderedCollections (ios-arm64)",
        log_path=Path("/nonexistent/build.log"),
        errors=errors,
    )
    diag_idx = out.find("Diagnosis: ")
    top_idx = out.find("Top 1 error(s) from xcresult:")
    _assert(diag_idx != -1, f"diagnosis missing: {out!r}")
    _assert(top_idx != -1, f"top-errors missing: {out!r}")
    _assert(diag_idx < top_idx, f"diagnosis must come BEFORE raw errors: {out!r}")
    _assert("Try:" in out, out)

    # Miss case: unrelated error → no diagnosis block, raw errors unchanged.
    errors = [{
        "target": "Foo",
        "message": "totally unrecognised build failure",
        "source": "",
        "issueType": "Swift Compiler Error",
    }]
    out = _format_execute_error(
        unit_name="Foo",
        log_path=Path("/nonexistent/build.log"),
        errors=errors,
    )
    _assert("Diagnosis: " not in out,
            f"unmatched error must not emit a diagnosis: {out!r}")
    _assert("Top 1 error(s) from xcresult:" in out, out)
    _assert("totally unrecognised build failure" in out, out)


def _selftest_unsupported_swift_constructs() -> None:
    """The Prepare safety net rejects Package.swift files containing Swift
    constructs the balanced-paren walker can't reason about: raw strings.
    These raise PrepareUserError (clean message path — the user's
    manifest, not a tool bug) with a targeted message rather than
    allowing the walker to silently mis-parse.

    `\\(...)` string interpolation and `\"\"\"...\"\"\"` triple-quoted
    multi-line strings are intentionally NOT rejected — the walker
    handles interpolation via recursion through itself, and triple-
    quoted strings via the `_skip_triple_quoted_string` helper (real
    manifests like swift-collections use both heavily — its `traits:`
    block uses multi-line descriptions, and its target paths use
    `"Sources/\\(name)"` interpolation).
    """
    # Plain manifests pass through.
    _assert_no_unsupported_swift_constructs(
        '// swift-tools-version:5.7\nlet x = "ok"\n'
    )
    # Interpolation pass-through: walker recurses to find the matching
    # close paren of the embedded expression and resumes string mode.
    _assert_no_unsupported_swift_constructs(
        '// swift-tools-version:5.7\nlet x = "name=\\(foo)"\n'
    )
    # Triple-quoted multi-line string pass-through: walker skips to the
    # closing `"""`. swift-collections's `.trait(description: """...""")`
    # block is the canonical real-world case.
    _assert_no_unsupported_swift_constructs(
        '// swift-tools-version:5.7\nlet x = """\nhi\n"""\n'
    )

    def _expect_raise(text: str, hint: str) -> None:
        try:
            _assert_no_unsupported_swift_constructs(text)
        except PrepareUserError as exc:
            _assert(hint in str(exc), f"expected '{hint}' in error, got: {exc}")
            _assert(
                isinstance(exc, _USER_FACING_ERRORS),
                "PrepareUserError must be user-facing (clean error path)",
            )
            return
        raise AssertionError(f"expected PrepareUserError for {hint!r}")

    _expect_raise('let x = #"hi"#\n', "raw string")


def _selftest_unsupported_swift_constructs_comment_aware() -> None:
    # REFACTOR_PLAN Task 5: the scanner strips comments before running
    # the trigger-token check, so legitimate mentions of raw-string,
    # triple-quoted, or interpolation sigils inside `///` doc
    # comments, `//` line comments, and `/* */` block comments don't
    # raise false positives. Real code uses of the same constructs
    # must still be rejected.
    # Case 1: raw-string mention inside a `///` doc comment (which is
    # itself just a `//` comment for lexer purposes). Must pass.
    _assert_no_unsupported_swift_constructs(
        '// swift-tools-version:5.7\n'
        'import PackageDescription\n'
        '/// Uses #"raw"# only as a doc-comment example.\n'
        'let package = Package(name: "Ok", products: [], targets: [])\n'
    )
    # Case 2: triple-quoted mention inside a `/* */` block comment.
    _assert_no_unsupported_swift_constructs(
        '// swift-tools-version:5.7\n'
        'import PackageDescription\n'
        '/* here is a """ triple-quoted """ example in prose */\n'
        'let package = Package(name: "Ok", products: [], targets: [])\n'
    )
    # Case 3: interpolation sigil inside a `//` line comment.
    _assert_no_unsupported_swift_constructs(
        '// swift-tools-version:5.7\n'
        'import PackageDescription\n'
        '// path uses \\(name) style interpolation — just in prose\n'
        'let package = Package(name: "Ok", products: [], targets: [])\n'
    )
    # Case 4: all three at once, mixed across comment flavors.
    _assert_no_unsupported_swift_constructs(
        '// swift-tools-version:5.7\n'
        'import PackageDescription\n'
        '/// doc: #"raw"# and """triple""" and \\(interp)\n'
        '// line: #"raw"#\n'
        '/* block: """ """ \\( */\n'
        'let package = Package(name: "Ok", products: [], targets: [])\n'
    )

    # True-positive assertions must still fire. A real raw string in
    # code is still rejected.
    def _expect_reject(text: str, hint: str) -> None:
        try:
            _assert_no_unsupported_swift_constructs(text)
        except PrepareUserError:
            return
        raise AssertionError(
            f"expected PrepareUserError for real {hint} use"
        )

    _expect_reject(
        '// comment mentioning nothing special\n'
        'let x = #"actual raw"#\n',
        "raw string",
    )
    # A real triple-quoted string is no longer a rejected construct —
    # the walker handles it via `_skip_triple_quoted_string`. Verify
    # the safety net leaves it alone even after a comment-aware strip.
    _assert_no_unsupported_swift_constructs(
        '/* safe comment */\n'
        'let x = """\nreal triple\n"""\n'
    )
    # `\(...)` interpolation is no longer a guarded construct: the walker
    # handles it via recursion. Verify the safety net leaves it alone
    # even after a comment-aware strip.
    _assert_no_unsupported_swift_constructs(
        '/// safe doc\n'
        'let x = "hi=\\(real)"\n'
    )
    # An unterminated block comment plus a real raw-string AFTER the
    # unterminated marker must still be rejected — the conservative
    # fall-through keeps the rest of the text visible to the scanner.
    _expect_reject(
        '/* unterminated comment\nlet x = #"oops"#\n',
        "unterminated comment + raw string",
    )

    # Nested block comments. Swift block comments nest, and the
    # original implementation stopped at the first `*/`, leaving the
    # outer comment's tail visible to the trigger scanner. These
    # cases lock in the nesting-aware fix.
    # Case A: nested comment legitimately mentioning a raw-string
    # sigil between the inner and outer closes — must pass.
    _assert_no_unsupported_swift_constructs(
        '// swift-tools-version:5.7\n'
        'import PackageDescription\n'
        '/* outer /* inner */ still mentioning #"raw"# */\n'
        'let package = Package(name: "Ok", products: [], targets: [])\n'
    )
    # Case B: deeply nested comment containing all three trigger
    # tokens — must pass.
    _assert_no_unsupported_swift_constructs(
        '// swift-tools-version:5.7\n'
        'import PackageDescription\n'
        '/* /* /* triple #"raw"# """ \\(interp) */ */ */\n'
        'let package = Package(name: "Ok", products: [], targets: [])\n'
    )
    # Case C: a real raw string AFTER a (correctly closed) nested
    # comment must STILL be rejected — the nesting fix must not
    # accidentally swallow live code.
    _expect_reject(
        '/* outer /* inner */ */\n'
        'let x = #"actual"#\n',
        "nested-comment + real raw string",
    )
    # Case D: unterminated nested comment + a real raw string after
    # it must still be rejected via the conservative fall-through.
    _expect_reject(
        '/* outer /* inner */ no outer close\n'
        'let x = #"oops"#\n',
        "unterminated nested comment + raw string",
    )


def _selftest_error_taxonomy_split() -> None:
    """Lock in the taxonomy refactor: PrepareUserError and VerifyUserError
    live in `_USER_FACING_ERRORS` (clean message path), PrepareBug and
    VerifyBug are in `_BUG_CLASS_ERRORS` (traceback path), and the base
    `PrepareError` / `VerifyError` classes themselves are NOT in either
    tuple — only their leaf subclasses are. This test catches a future
    regression where someone reintroduces a bare `raise PrepareError(...)`
    that would silently fall through to an uncaught exception because no
    handler targets the base class directly."""
    # User-facing errors: clean-message path.
    for cls in (PrepareUserError, VerifyUserError):
        _assert(
            issubclass(cls, _USER_FACING_ERRORS),
            f"{cls.__name__} should be user-facing (clean-message path)",
        )
        _assert(
            not issubclass(cls, _BUG_CLASS_ERRORS),
            f"{cls.__name__} must not be in _BUG_CLASS_ERRORS",
        )
    # Bug-class errors: traceback path.
    for cls in (PrepareBug, VerifyBug):
        _assert(
            issubclass(cls, _BUG_CLASS_ERRORS),
            f"{cls.__name__} should be bug-class (traceback path)",
        )
        _assert(
            not issubclass(cls, _USER_FACING_ERRORS),
            f"{cls.__name__} must not be in _USER_FACING_ERRORS",
        )
    # The base classes are NOT directly in either tuple — only leaf
    # subclasses are. This is the key invariant: an accidental
    # `raise PrepareError(...)` won't match either handler.
    _assert(
        PrepareError not in _USER_FACING_ERRORS,
        "bare PrepareError base class should not be in _USER_FACING_ERRORS",
    )
    _assert(
        PrepareError not in _BUG_CLASS_ERRORS,
        "bare PrepareError base class should not be in _BUG_CLASS_ERRORS",
    )
    _assert(
        VerifyError not in _USER_FACING_ERRORS,
        "bare VerifyError base class should not be in _USER_FACING_ERRORS",
    )
    _assert(
        VerifyError not in _BUG_CLASS_ERRORS,
        "bare VerifyError base class should not be in _BUG_CLASS_ERRORS",
    )
    # Exit codes are still addressable via the base class (call sites
    # use `VerifyError.exit_code` for the aggregate-failure path).
    _assert(VerifyError.exit_code == 8, "VerifyError.exit_code changed")
    _assert(PrepareError.exit_code == 6, "PrepareError.exit_code changed")
    # Phase-label mapping still works for every leaf subclass.
    _assert(_phase_label_for(PrepareUserError("x")) == "prepare",
            "PrepareUserError should label as 'prepare'")
    _assert(_phase_label_for(PrepareBug("x")) == "prepare",
            "PrepareBug should label as 'prepare'")
    _assert(_phase_label_for(VerifyUserError("x")) == "verify",
            "VerifyUserError should label as 'verify'")
    _assert(_phase_label_for(VerifyBug("x")) == "verify",
            "VerifyBug should label as 'verify'")


def _selftest_select_active_manifest() -> None:
    """The active-manifest selector mirrors SPM's actual rule: pick the
    manifest whose declared `// swift-tools-version` line is the highest
    one still <= the active toolchain version. The filename is just a
    sort hint, not the selection key.

    Covers:
      * the Alamofire-shaped case where `Package.swift` declares a higher
        tools-version than any `Package@swift-X.Y.swift` sibling and must
        win on a fresh toolchain (the regression motivating this fix);
      * the legacy fallback path where the base file declares the *lowest*
        tools-version and a version-specific sibling wins;
      * the patch-component tie-break (`5.9.1` vs `5.9` on 5.9.5 vs 5.9.0);
      * graceful fallback to `Package.swift` when the toolchain version is
        unknown or every candidate is unparseable.
    """
    import tempfile
    saved = tool._swift_toolchain_version
    try:
        # Scenario A: Alamofire-shaped layout. Base manifest declares 6.0;
        # version-specific files cap at 5.10. On a 6.x toolchain the base
        # file must win.
        with tempfile.TemporaryDirectory(prefix="spm2x-active-A-") as tmp:
            d = Path(tmp)
            (d / "Package.swift").write_text("// swift-tools-version: 6.0\n")
            (d / "Package@swift-5.9.swift").write_text("// swift-tools-version:5.9\n")
            (d / "Package@swift-5.10.swift").write_text("// swift-tools-version:5.10\n")

            tool._swift_toolchain_version = lambda: (6, 2, 0)  # noqa: E731
            picked = _select_active_manifest(d)
            _assert(picked.name == "Package.swift",
                    f"toolchain 6.2 with base@6.0 should pick Package.swift, "
                    f"picked {picked.name}")

            # On a 5.10.x toolchain the 5.10 sibling becomes the highest
            # one that fits.
            tool._swift_toolchain_version = lambda: (5, 10, 3)  # noqa: E731
            picked = _select_active_manifest(d)
            _assert(picked.name == "Package@swift-5.10.swift",
                    f"toolchain 5.10.3 should pick the 5.10 sibling, picked {picked.name}")

            # On a 5.9 toolchain only the 5.9 sibling fits.
            tool._swift_toolchain_version = lambda: (5, 9, 0)  # noqa: E731
            picked = _select_active_manifest(d)
            _assert(picked.name == "Package@swift-5.9.swift",
                    f"toolchain 5.9.0 should pick the 5.9 sibling, picked {picked.name}")

            # No version-specific manifest fits — base would normally win,
            # except its declared version (6.0) is also too high. Fall
            # back to Package.swift anyway (we never reject the base file
            # for being too new at runtime; SPM would surface that error
            # itself).
            tool._swift_toolchain_version = lambda: (5, 7, 0)  # noqa: E731
            picked = _select_active_manifest(d)
            _assert(picked.name == "Package.swift",
                    f"toolchain 5.7 picked {picked.name}")

        # Scenario B: legacy layout. Base declares 5.7, two siblings cap
        # at 5.9 and 5.9.1. On 5.9.5 the .1 patch wins; on 5.9.0 the bare
        # 5.9 wins; on toolchain 6.x the highest fitting sibling (5.9.1)
        # still beats the base because base's 5.7 < 5.9.1.
        with tempfile.TemporaryDirectory(prefix="spm2x-active-B-") as tmp:
            d = Path(tmp)
            (d / "Package.swift").write_text("// swift-tools-version:5.7\n")
            (d / "Package@swift-5.9.swift").write_text("// swift-tools-version:5.9\n")
            (d / "Package@swift-5.9.1.swift").write_text("// swift-tools-version:5.9.1\n")

            tool._swift_toolchain_version = lambda: (5, 9, 5)  # noqa: E731
            picked = _select_active_manifest(d)
            _assert(picked.name == "Package@swift-5.9.1.swift",
                    f"toolchain 5.9.5 should pick the .1 patch, picked {picked.name}")

            tool._swift_toolchain_version = lambda: (5, 9, 0)  # noqa: E731
            picked = _select_active_manifest(d)
            _assert(picked.name == "Package@swift-5.9.swift",
                    f"toolchain 5.9.0 should reject the .1 patch variant, picked {picked.name}")

            tool._swift_toolchain_version = lambda: (6, 2, 0)  # noqa: E731
            picked = _select_active_manifest(d)
            _assert(picked.name == "Package@swift-5.9.1.swift",
                    f"toolchain 6.2 with base@5.7 should pick the highest "
                    f"sibling (5.9.1), picked {picked.name}")

            tool._swift_toolchain_version = lambda: None  # noqa: E731
            picked = _select_active_manifest(d)
            _assert(picked.name == "Package.swift",
                    f"unknown toolchain picked {picked.name}")
    finally:
        tool._swift_toolchain_version = saved


# --- Execute self-tests ---------------------------------------------------
#
# Session 4 introduces the parallel slice builder, static-promote,
# swiftmodule + ObjC header injection, and binary copy. Most of those
# functions touch xcodebuild / lipo / clang and aren't directly unit
# testable, so the self-tests below focus on the pure helpers (path
# layout, framework type detection, ObjC header lookup against the
# raw_dump model, modulemap generation) and on the round-trip fixture
# for `edit_force_dynamic` -> `swift package dump-package`.

_FOO_PACKAGE_SWIFT_FIXTURE = """// swift-tools-version:5.7
import PackageDescription

let package = Package(
    name: "Foo",
    platforms: [.iOS(.v15)],
    products: [
        .library(name: "Foo", targets: ["Foo"]),
    ],
    targets: [
        .target(name: "Foo", path: "Sources/Foo"),
    ]
)
"""


def _selftest_platform_slices_table_integrity() -> None:
    """Every PlatformSlice's clang_min_flag_template must format cleanly
    with a sample version and produce the verified-good string for that
    (platform, device/sim) combination. The exact flag values were
    confirmed via live `xcrun --sdk <sdk> clang -mtargetos=...` probes
    by both reviewers (see MULTI_PLATFORM_PLAN.md High items 1-3).
    """
    expected = {
        "ios-arm64":            "-miphoneos-version-min=15.0",
        "ios-simulator":        "-mios-simulator-version-min=15.0",
        "macos":                "-mmacosx-version-min=11.0",
        "maccatalyst":          "-mtargetos=ios15.0-macabi",
        "tvos-arm64":           "-mtvos-version-min=15.0",
        "tvos-simulator":       "-mtvos-simulator-version-min=15.0",
        "watchos-arm64":        "-mwatchos-version-min=8.0",
        "watchos-simulator":    "-mwatchos-simulator-version-min=8.0",
        "visionos-arm64":       "-mtargetos=xros1.0",
        "visionos-simulator":   "-mtargetos=xros1.0-simulator",
    }
    samples = {
        "ios": "15.0", "macos": "11.0", "maccatalyst": "15.0",
        "tvos": "15.0", "watchos": "8.0", "visionos": "1.0",
    }
    seen: Dict[str, PlatformSlice] = {}
    for plat in _PLATFORM_ORDER:
        _assert(plat in _PLATFORM_SLICES, f"missing platform in _PLATFORM_SLICES: {plat}")
        for s in _PLATFORM_SLICES[plat]:
            _assert(s.slice_id not in seen, f"duplicate slice_id: {s.slice_id}")
            seen[s.slice_id] = s
            got = s.clang_min_flag_template.format(version=samples[plat])
            want = expected[s.slice_id]
            _assert(got == want, f"slice {s.slice_id}: got={got!r} want={want!r}")
    _assert(set(seen.keys()) == set(expected.keys()),
            f"slice_id coverage mismatch: {sorted(seen.keys())} vs {sorted(expected.keys())}")


def _selftest_selected_slices_order_and_filter() -> None:
    """_selected_slices walks _PLATFORM_ORDER, emits only enabled
    platforms, and pairs each PlatformSlice with the user-supplied
    deployment target."""
    cfg = tool.Config(
        package_source="/dev/null",
        min_ios="15.0", min_macos=None, min_maccatalyst="15.0",
        min_tvos=None, min_watchos="8.0", min_visionos="1.0",
    )
    out = _selected_slices(cfg)
    ids = [s.slice_id for s, _ in out]
    _assert(ids == [
        "ios-arm64", "ios-simulator", "maccatalyst",
        "watchos-arm64", "watchos-simulator",
        "visionos-arm64", "visionos-simulator",
    ], f"unexpected order: {ids}")
    versions = {s.slice_id: v for s, v in out}
    _assert(versions["ios-arm64"] == "15.0", f"ios-arm64 version: {versions['ios-arm64']!r}")
    _assert(versions["maccatalyst"] == "15.0", f"maccatalyst version: {versions['maccatalyst']!r}")
    _assert(versions["watchos-simulator"] == "8.0", f"watchos-simulator version: {versions['watchos-simulator']!r}")
    _assert(versions["visionos-arm64"] == "1.0", f"visionos-arm64 version: {versions['visionos-arm64']!r}")


def _selftest_selected_slices_no_ios_path() -> None:
    """--no-ios maps to min_ios=None, which must drop both iOS slices."""
    cfg = tool.Config(package_source="/dev/null", min_ios=None, min_macos="11.0")
    out = _selected_slices(cfg)
    ids = [s.slice_id for s, _ in out]
    _assert(ids == ["macos"], f"expected only macos slice, got {ids}")
    _assert(_enabled_platforms(cfg) == ["macos"],
            f"unexpected enabled platforms: {_enabled_platforms(cfg)!r}")


def _autodetect_pkg(platforms: List[Tuple[str, str]]) -> "tool.Package":
    """Helper: construct a minimal Package with the given (name, version)
    platform list and no products/targets — enough for
    `_autodetect_min_versions` to walk."""
    return tool.Package(
        name="AutodetectFixture", tools_version="5.9.0",
        platforms=[tool.Platform(name=n, version=v) for n, v in platforms],
        products=[], targets=[], schemes=[],
        raw_dump={}, staged_dir=Path("/tmp/spm2xc-autodetect-fixture"),
    )


def _selftest_autodetect_multi_platform_package() -> None:
    """No --min-* flags + multi-platform package → every declared
    platform is filled in at its declared version."""
    cfg = tool.Config(package_source="/dev/null")
    pkg = _autodetect_pkg([
        ("ios", "13.0"), ("macos", "10.15"),
        ("tvos", "15.0"), ("visionos", "1.0"),
    ])
    derived = _autodetect_min_versions(cfg, pkg)
    _assert(derived == {"ios": "13.0", "macos": "10.15",
                        "tvos": "15.0", "visionos": "1.0"},
            f"unexpected derived map: {derived!r}")
    _assert(cfg.min_ios == "13.0", f"min_ios: {cfg.min_ios!r}")
    _assert(cfg.min_macos == "10.15", f"min_macos: {cfg.min_macos!r}")
    _assert(cfg.min_tvos == "15.0", f"min_tvos: {cfg.min_tvos!r}")
    _assert(cfg.min_visionos == "1.0", f"min_visionos: {cfg.min_visionos!r}")
    _assert(cfg.min_watchos is None, "watchos must remain unset")
    _assert(cfg.min_maccatalyst is None, "maccatalyst must remain unset")


def _selftest_autodetect_explicit_flag_is_noop() -> None:
    """Any --min-* flag passed → auto-detect is a no-op. Mixed mode is
    deliberately rejected; explicit-is-explicit."""
    cfg = tool.Config(package_source="/dev/null", min_ios="17.0")
    pkg = _autodetect_pkg([("ios", "13.0"), ("macos", "10.15")])
    derived = _autodetect_min_versions(cfg, pkg)
    _assert(derived == {}, f"expected no-op, got: {derived!r}")
    _assert(cfg.min_ios == "17.0", "user's explicit --min-ios must survive")
    _assert(cfg.min_macos is None,
            "macOS must NOT auto-fill when any other flag was explicit")


def _selftest_autodetect_no_platforms_declared_falls_back_to_ios_15() -> None:
    """Package omits `platforms:` → fall back to today's iOS 15.0
    default so the common single-package case keeps working."""
    cfg = tool.Config(package_source="/dev/null")
    pkg = _autodetect_pkg([])
    derived = _autodetect_min_versions(cfg, pkg)
    _assert(derived == {"ios": "15.0"}, f"unexpected fallback: {derived!r}")
    _assert(cfg.min_ios == "15.0", f"min_ios: {cfg.min_ios!r}")


def _selftest_autodetect_no_ios_with_ios_only_package_yields_empty() -> None:
    """--no-ios + package declares only iOS → derived map empty, no
    fallback (the post-autodetect validator surfaces the error)."""
    cfg = tool.Config(package_source="/dev/null", no_ios=True)
    pkg = _autodetect_pkg([("ios", "13.0")])
    derived = _autodetect_min_versions(cfg, pkg)
    _assert(derived == {}, f"expected empty, got: {derived!r}")
    _assert(cfg.min_ios is None, "min_ios must stay None under --no-ios")


def _selftest_autodetect_no_ios_with_multi_platform_skips_ios_only() -> None:
    """--no-ios + multi-platform package → derives non-iOS platforms,
    skips the iOS entry."""
    cfg = tool.Config(package_source="/dev/null", no_ios=True)
    pkg = _autodetect_pkg([
        ("ios", "13.0"), ("macos", "11.0"), ("tvos", "15.0"),
    ])
    derived = _autodetect_min_versions(cfg, pkg)
    _assert(derived == {"macos": "11.0", "tvos": "15.0"},
            f"unexpected derived: {derived!r}")
    _assert(cfg.min_ios is None, "iOS must stay disabled under --no-ios")
    _assert(cfg.min_macos == "11.0", f"min_macos: {cfg.min_macos!r}")
    _assert(cfg.min_tvos == "15.0", f"min_tvos: {cfg.min_tvos!r}")


def _selftest_autodetect_ignores_unknown_platform_names() -> None:
    """A platformName we don't recognise (e.g. a future Apple platform)
    is skipped silently rather than crashing. Tool-stability invariant."""
    cfg = tool.Config(package_source="/dev/null")
    pkg = _autodetect_pkg([("ios", "15.0"), ("driverkit", "20.0")])
    derived = _autodetect_min_versions(cfg, pkg)
    _assert(derived == {"ios": "15.0"},
            f"unknown platform should be filtered: {derived!r}")


def _selftest_autodetect_skips_platform_with_empty_version() -> None:
    """A Platform whose version is the empty string (or otherwise
    falsy) is silently dropped — Inspect occasionally emits entries
    without a parseable version, and we'd rather fall back to iOS-15
    than crash with an empty deployment target. Guard at platforms.py
    `not p.version` continue."""
    cfg = tool.Config(package_source="/dev/null")
    pkg = _autodetect_pkg([("ios", "")])
    derived = _autodetect_min_versions(cfg, pkg)
    # ios entry was dropped → effective "no platforms declared" → iOS-15 fallback
    _assert(derived == {"ios": "15.0"},
            f"empty-version entry should be filtered, falling back to ios-15: {derived!r}")
    _assert(cfg.min_ios == "15.0", f"min_ios fallback: {cfg.min_ios!r}")


def _selftest_spm_platform_entries_string_form() -> None:
    """Binary-mode shim emits one `.<Plat>("X.Y")` entry per enabled
    platform, in fixed order. String form sidesteps the broken
    `.macOS(.v10)` enum mapping for 10.15."""
    cfg = tool.Config(
        package_source="/dev/null",
        min_ios="15.0", min_macos="10.15", min_maccatalyst="15.0",
        min_tvos="15.0", min_watchos="8.0", min_visionos="1.0",
    )
    entries = _spm_platform_entries(cfg)
    _assert(entries == [
        '.iOS("15.0")', '.macOS("10.15")', '.macCatalyst("15.0")',
        '.tvOS("15.0")', '.watchOS("8.0")', '.visionOS("1.0")',
    ], f"unexpected entries: {entries}")
    # Subset works too — only emit what's enabled.
    cfg2 = tool.Config(package_source="/dev/null", min_ios=None, min_macos="11.0")
    _assert(_spm_platform_entries(cfg2) == ['.macOS("11.0")'],
            f"unexpected single-platform entries: {_spm_platform_entries(cfg2)!r}")


def _selftest_validate_requested_platforms_rejects_undeclared() -> None:
    """Requesting a platform not in the package's declared platforms
    must fail-fast with a PlanError naming the missing platform."""
    pkg = tool.Package(
        name="Snapshot",
        tools_version="5.6.0",
        platforms=[
            tool.Platform(name="ios", version="13.0"),
            tool.Platform(name="macos", version="10.15"),
        ],
        products=[], targets=[], schemes=[],
        raw_dump={}, staged_dir=Path("/tmp/spm2xc-fake-staged"),
    )
    # iOS + macOS: declared → OK.
    cfg_ok = tool.Config(package_source="/dev/null", min_ios="15.0", min_macos="11.0")
    _validate_requested_platforms(cfg_ok, pkg)  # must not raise

    # iOS + visionOS: visionOS not declared → must raise.
    cfg_bad = tool.Config(package_source="/dev/null", min_ios="15.0", min_visionos="1.0")
    try:
        _validate_requested_platforms(cfg_bad, pkg)
    except tool.PlanError as exc:
        _assert("visionos" in str(exc).lower(),
                f"expected missing-platform message, got: {exc}")
    else:
        _assert(False, "expected PlanError for undeclared visionOS")


def _selftest_validate_requested_platforms_empty_passes_through() -> None:
    """SPM treats `platforms: []` as 'all platforms supported', so the
    validator must allow any request when the package declares none."""
    pkg = tool.Package(
        name="NoPlatforms", tools_version="5.6.0",
        platforms=[], products=[], targets=[], schemes=[],
        raw_dump={}, staged_dir=Path("/tmp/spm2xc-fake-staged"),
    )
    cfg = tool.Config(package_source="/dev/null", min_ios="15.0",
                      min_macos="11.0", min_visionos="1.0")
    _validate_requested_platforms(cfg, pkg)  # must not raise


def _selftest_parse_platforms_recognises_all_six_names() -> None:
    """`swift package dump-package` emits these exact platformName values
    for the six platforms we care about. Confirmed via live dump-package
    run by Grok."""
    raw = [
        {"platformName": "ios", "version": "15.0"},
        {"platformName": "macos", "version": "11.0"},
        {"platformName": "maccatalyst", "version": "15.0"},
        {"platformName": "tvos", "version": "15.0"},
        {"platformName": "watchos", "version": "8.0"},
        {"platformName": "visionos", "version": "1.0"},
    ]
    plats = tool._parse_platforms(raw)
    names = [p.name for p in plats]
    _assert(names == ["ios", "macos", "maccatalyst", "tvos", "watchos", "visionos"],
            f"unexpected names: {names}")


def _selftest_platform_from_library_identifier() -> None:
    """Apple's `<platform>-<archs>[-<variant>]` LibraryIdentifier shape
    must map cleanly to our internal platform IDs. visionOS uses the
    `xros` prefix in the LibraryIdentifier (not `visionos`), and the
    `-maccatalyst` suffix wins over the `ios-` prefix it shares."""
    cases = {
        "ios-arm64":                                "ios",
        "ios-arm64_x86_64-simulator":               "ios",
        "ios-arm64_x86_64-maccatalyst":             "maccatalyst",
        "macos-arm64_x86_64":                       "macos",
        "tvos-arm64":                               "tvos",
        "tvos-arm64_x86_64-simulator":              "tvos",
        "watchos-arm64_32_armv7k":                  "watchos",
        "watchos-arm64_i386_x86_64-simulator":      "watchos",
        "xros-arm64":                               "visionos",
        "xros-arm64_x86_64-simulator":              "visionos",
        "garbage":                                  None,
        "":                                         None,
    }
    for lid, want in cases.items():
        got = _platform_from_library_identifier(lid)
        _assert(got == want, f"identifier {lid!r}: got {got!r} want {want!r}")


def _selftest_variant_classification_round_trips() -> None:
    """`_variant_for_platform_slice` (PlatformSlice side) and
    `_variant_from_library_identifier` (xcframework side) must agree on
    the same taxonomy — otherwise the expected set and the covered set
    can never line up. Spot-check both sides over every platform."""
    plat_cases: Dict[str, str] = {
        "ios-arm64":            "device",
        "ios-simulator":        "simulator",
        "macos":                "device",
        "maccatalyst":          "maccatalyst",
        "tvos-arm64":           "device",
        "tvos-simulator":       "simulator",
        "watchos-arm64":        "device",
        "watchos-simulator":    "simulator",
        "visionos-arm64":       "device",
        "visionos-simulator":   "simulator",
    }
    for plat in _PLATFORM_ORDER:
        for s in _PLATFORM_SLICES[plat]:
            got = _variant_for_platform_slice(s)
            want = plat_cases[s.slice_id]
            _assert(got == want, f"slice {s.slice_id}: got {got!r} want {want!r}")
    lid_cases: Dict[str, str] = {
        "ios-arm64":                            "device",
        "ios-arm64_x86_64-simulator":           "simulator",
        "ios-arm64_x86_64-maccatalyst":         "maccatalyst",
        "macos-arm64_x86_64":                   "device",
        "tvos-arm64":                           "device",
        "tvos-arm64_x86_64-simulator":          "simulator",
        "watchos-arm64_32_armv7k":              "device",
        "watchos-arm64_i386_x86_64-simulator":  "simulator",
        "xros-arm64":                           "device",
        "xros-arm64_x86_64-simulator":          "simulator",
    }
    for lid, want in lid_cases.items():
        got = _variant_from_library_identifier(lid)
        _assert(got == want, f"identifier {lid!r}: got {got!r} want {want!r}")


def _selftest_expected_slice_classes_pairs() -> None:
    """`_expected_slice_classes(config)` must emit one (platform, variant)
    pair per slice the build pipeline drives — i.e. it must expand iOS,
    tvOS, watchOS, and visionOS into their device + simulator pairs."""
    cfg = tool.Config(
        package_source="/dev/null",
        min_ios="15.0", min_macos="11.0", min_maccatalyst="15.0",
        min_visionos="1.0",
    )
    got = _expected_slice_classes(cfg)
    _assert(got == [
        ("ios", "device"), ("ios", "simulator"),
        ("macos", "device"),
        ("maccatalyst", "maccatalyst"),
        ("visionos", "device"), ("visionos", "simulator"),
    ], f"unexpected slice classes: {got}")


def _selftest_verify_coverage_rejects_missing_platform(tmp_root: Path) -> None:
    """Binary-mode regression #1: when the unit declares
    `expected_slice_classes`, Verify must refuse to pass an xcframework
    that doesn't carry one of the requested platform families at all.
    Constructs an iOS-only artifact and asserts a fatal when the unit
    asked for both iOS and macOS."""
    base = tmp_root / "verify_coverage_missing_platform"
    base.mkdir()
    xc = _build_synthetic_xcframework(
        base, "OnlyiOS", flavor="swift",
        slices=("ios-arm64", "ios-arm64_x86_64-simulator"),
    )
    unit = ExecutedUnit(
        name="OnlyiOS", xcframework_path=xc, framework_name="OnlyiOS",
        framework_type="Swift",
        expected_slice_classes=[
            ("ios", "device"), ("ios", "simulator"),
            ("macos", "device"),
        ],
        is_binary_copy=True,
    )
    mod = tool
    saved = mod._check_binary_dynamic
    try:
        mod._check_binary_dynamic = lambda _b: True
        results = verify_output([unit], base)
    finally:
        mod._check_binary_dynamic = saved
    r = results[0]
    _assert(not r.passed, "expected coverage fatal, got pass")
    _assert(any("missing requested slice(s)" in m for m in r.fatal_issues),
            f"expected coverage fatal, got {r.fatal_issues!r}")
    _assert(any("macos-device" in m for m in r.fatal_issues),
            f"expected macos-device in fatal, got {r.fatal_issues!r}")


def _selftest_verify_coverage_rejects_missing_simulator(tmp_root: Path) -> None:
    """Binary-mode regression #2 (Codex round 3): an iOS device-only
    artifact must fail Verify when the unit also expects a simulator
    slice. Family-level coverage alone would have let this pass."""
    base = tmp_root / "verify_coverage_missing_simulator"
    base.mkdir()
    xc = _build_synthetic_xcframework(
        base, "DeviceOnly", flavor="swift",
        slices=("ios-arm64",),
    )
    unit = ExecutedUnit(
        name="DeviceOnly", xcframework_path=xc, framework_name="DeviceOnly",
        framework_type="Swift",
        expected_slice_classes=[("ios", "device"), ("ios", "simulator")],
        is_binary_copy=True,
    )
    mod = tool
    saved = mod._check_binary_dynamic
    try:
        mod._check_binary_dynamic = lambda _b: True
        results = verify_output([unit], base)
    finally:
        mod._check_binary_dynamic = saved
    r = results[0]
    _assert(not r.passed,
            f"expected coverage fatal for missing simulator; passed={r.passed} fatals={r.fatal_issues!r}")
    _assert(any("ios-simulator" in m for m in r.fatal_issues),
            f"expected ios-simulator in fatal, got {r.fatal_issues!r}")


def _selftest_verify_coverage_passes_when_all_requested_present(tmp_root: Path) -> None:
    """The mirror case: when every expected (platform, variant) pair is
    present in AvailableLibraries, the coverage check stays quiet."""
    base = tmp_root / "verify_coverage_ok"
    base.mkdir()
    xc = _build_synthetic_xcframework(
        base, "Both", flavor="swift",
        slices=("ios-arm64", "ios-arm64_x86_64-simulator", "macos-arm64_x86_64"),
    )
    unit = ExecutedUnit(
        name="Both", xcframework_path=xc, framework_name="Both",
        framework_type="Swift",
        expected_slice_classes=[
            ("ios", "device"), ("ios", "simulator"),
            ("macos", "device"),
        ],
    )
    mod = tool
    saved = mod._check_binary_dynamic
    try:
        mod._check_binary_dynamic = lambda _b: True
        results = verify_output([unit], base)
    finally:
        mod._check_binary_dynamic = saved
    r = results[0]
    _assert(r.passed, f"expected verify pass; fatals={r.fatal_issues!r}")


def _selftest_slice_paths_unique() -> None:
    """Device + simulator slice paths must be disjoint so the parallel
    builder can run both at once without stomping on each other's
    archives, derived data, xcresult bundles, or log files."""
    work = Path("/tmp/spm2xc-fake-work")
    dev = _slice_paths(work, "MyUnit", "ios-arm64")
    sim = _slice_paths(work, "MyUnit", "ios-simulator")
    for d, s in zip(dev, sim):
        _assert(d != s, f"slice paths collided: device={d} sim={s}")
    # Within each slice the four paths must also be distinct (no two
    # files at the same target). Catches accidental dedupe in the
    # path-derivation logic.
    _assert(len(set(dev)) == 4, f"device slice has duplicate paths: {dev}")
    _assert(len(set(sim)) == 4, f"sim slice has duplicate paths: {sim}")


def _selftest_detect_framework_type_swift_objc_mixed(tmp_root: Path) -> None:
    """detect_framework_type classifies a synthetic xcframework tree."""
    base = tmp_root / "fwtype"
    base.mkdir()
    # Swift only.
    swift_xc = base / "Swift.xcframework"
    sw_modules = swift_xc / "ios-arm64" / "Swift.framework" / "Modules" / "Swift.swiftmodule"
    sw_modules.mkdir(parents=True)
    (sw_modules / "arm64.swiftinterface").write_text("// interface")
    _assert(detect_framework_type(swift_xc) == "Swift",
            f"Swift xcfw misclassified: {detect_framework_type(swift_xc)}")
    # ObjC only.
    objc_xc = base / "ObjC.xcframework"
    obj_headers = objc_xc / "ios-arm64" / "ObjC.framework" / "Headers"
    obj_headers.mkdir(parents=True)
    (obj_headers / "ObjC.h").write_text("// header")
    _assert(detect_framework_type(objc_xc) == "ObjC",
            f"ObjC xcfw misclassified: {detect_framework_type(objc_xc)}")
    # Mixed.
    mixed_xc = base / "Mixed.xcframework"
    mx_modules = mixed_xc / "ios-arm64" / "Mixed.framework" / "Modules" / "Mixed.swiftmodule"
    mx_modules.mkdir(parents=True)
    (mx_modules / "arm64.swiftinterface").write_text("// interface")
    mx_headers = mixed_xc / "ios-arm64" / "Mixed.framework" / "Headers"
    mx_headers.mkdir(parents=True)
    (mx_headers / "Mixed.h").write_text("// header")
    _assert(detect_framework_type(mixed_xc) == "Mixed",
            f"Mixed xcfw misclassified: {detect_framework_type(mixed_xc)}")
    # Auto-generated -Swift.h must NOT be counted as ObjC.
    bridge_xc = base / "Bridge.xcframework"
    br_modules = bridge_xc / "ios-arm64" / "Bridge.framework" / "Modules" / "Bridge.swiftmodule"
    br_modules.mkdir(parents=True)
    (br_modules / "arm64.swiftinterface").write_text("// interface")
    br_headers = bridge_xc / "ios-arm64" / "Bridge.framework" / "Headers"
    br_headers.mkdir(parents=True)
    (br_headers / "Bridge-Swift.h").write_text("// generated bridge")
    _assert(detect_framework_type(bridge_xc) == "Swift",
            f"Bridge xcfw misclassified (Swift-only with bridge header): {detect_framework_type(bridge_xc)}")
    # Empty -> Unknown.
    empty_xc = base / "Empty.xcframework"
    empty_xc.mkdir()
    _assert(detect_framework_type(empty_xc) == "Unknown",
            f"Empty xcfw misclassified: {detect_framework_type(empty_xc)}")


def _selftest_inject_objc_headers_with_umbrella(tmp_root: Path) -> None:
    """End-to-end: synthetic ObjC target with a `<fw>.h` umbrella header
    + per-class headers; inject_objc_headers must copy them into the
    framework Headers/ dir and generate a module.modulemap that
    references the umbrella header."""
    base = tmp_root / "objc_inject_umbrella"
    staged = base / "staged"
    staged.mkdir(parents=True)
    target_dir = staged / "Sources" / "MyObjC"
    public_headers = target_dir / "include"
    public_headers.mkdir(parents=True)
    (public_headers / "MyObjC.h").write_text("// umbrella")
    (public_headers / "MyObjCHelper.h").write_text("// helper")
    (target_dir / "MyObjC.m").write_text("// impl")

    raw_dump = {
        "name": "MyObjC",
        "products": [
            {"name": "MyObjC", "type": {"library": ["automatic"]}, "targets": ["MyObjC"]},
        ],
        "targets": [
            {
                "name": "MyObjC",
                "type": "regular",
                "path": "Sources/MyObjC",
                "publicHeadersPath": "include",
                "dependencies": [],
            },
        ],
    }
    package = Package(
        name="MyObjC",
        tools_version="5.7.0",
        platforms=[],
        products=[Product(name="MyObjC", linkage=Linkage.AUTOMATIC, targets=["MyObjC"])],
        targets=[Target(
            name="MyObjC",
            kind=TargetKind.REGULAR,
            path="Sources/MyObjC",
            public_headers_path="include",
            dependencies=[],
            exclude=[],
            language=Language.OBJC,
        )],
        schemes=[],
        raw_dump=raw_dump,
        staged_dir=staged,
    )

    fw = base / "MyObjC.framework"
    fw.mkdir()
    injected = inject_objc_headers(
        package=package,
        product_name="MyObjC",
        fw_name="MyObjC",
        fw_path=fw,
        verbose=False,
    )
    _assert(injected, "inject_objc_headers should have returned True")
    _assert((fw / "Headers" / "MyObjC.h").is_file(), "umbrella header missing")
    _assert((fw / "Headers" / "MyObjCHelper.h").is_file(), "helper header missing")
    modulemap = (fw / "Modules" / "module.modulemap").read_text()
    _assert("umbrella header \"MyObjC.h\"" in modulemap,
            f"modulemap missing umbrella header reference:\n{modulemap}")
    _assert("framework module MyObjC" in modulemap,
            f"modulemap missing framework module decl:\n{modulemap}")

    # Idempotency: a second call must be a no-op (returns False, doesn't
    # blow up because Headers/*.h is already present).
    second = inject_objc_headers(
        package=package,
        product_name="MyObjC",
        fw_name="MyObjC",
        fw_path=fw,
        verbose=False,
    )
    _assert(not second, "inject_objc_headers should be idempotent")


def _selftest_inject_objc_headers_explicit_modulemap(tmp_root: Path) -> None:
    """No umbrella header → modulemap should explicitly list every header."""
    base = tmp_root / "objc_inject_explicit"
    staged = base / "staged"
    staged.mkdir(parents=True)
    target_dir = staged / "Sources" / "Bar"
    public_headers = target_dir / "include"
    public_headers.mkdir(parents=True)
    (public_headers / "Alpha.h").write_text("// a")
    (public_headers / "Beta.h").write_text("// b")
    (target_dir / "Bar.m").write_text("// impl")

    raw_dump = {
        "name": "Bar",
        "products": [{"name": "Bar", "type": {"library": ["automatic"]}, "targets": ["Bar"]}],
        "targets": [
            {
                "name": "Bar",
                "type": "regular",
                "path": "Sources/Bar",
                "publicHeadersPath": "include",
                "dependencies": [],
            }
        ],
    }
    package = Package(
        name="Bar",
        tools_version="5.7.0",
        platforms=[],
        products=[Product(name="Bar", linkage=Linkage.AUTOMATIC, targets=["Bar"])],
        targets=[Target(
            name="Bar",
            kind=TargetKind.REGULAR,
            path="Sources/Bar",
            public_headers_path="include",
            dependencies=[],
            exclude=[],
            language=Language.OBJC,
        )],
        schemes=[],
        raw_dump=raw_dump,
        staged_dir=staged,
    )

    fw = base / "Bar.framework"
    fw.mkdir()
    injected = inject_objc_headers(
        package=package,
        product_name="Bar",
        fw_name="Bar",
        fw_path=fw,
        verbose=False,
    )
    _assert(injected, "explicit-modulemap inject should succeed")
    modulemap = (fw / "Modules" / "module.modulemap").read_text()
    _assert("umbrella header" not in modulemap,
            f"explicit modulemap should not use umbrella:\n{modulemap}")
    _assert("header \"Alpha.h\"" in modulemap, f"missing Alpha header line:\n{modulemap}")
    _assert("header \"Beta.h\"" in modulemap, f"missing Beta header line:\n{modulemap}")


def _selftest_inject_objc_headers_preserves_nested_subpaths(tmp_root: Path) -> None:
    """Codex [P2]: public headers living under `<publicHeadersPath>/...`
    subdirectories must be copied into `Headers/` with their relative
    path intact. Flattening by basename would (a) break
    `#import <Module/Sub/Foo.h>` style imports and (b) silently
    overwrite same-named headers that live in different subfolders.

    Layout under test:
        Sources/Nested/include/
            Nested.h                 ← umbrella at the root
            Sub/Alpha.h              ← nested, must land at Headers/Sub/Alpha.h
            Sub/Helper.h
            Other/Alpha.h            ← same basename as Sub/Alpha.h, MUST NOT clobber
    """
    base = tmp_root / "objc_inject_nested"
    staged = base / "staged"
    public_headers = staged / "Sources" / "Nested" / "include"
    (public_headers / "Sub").mkdir(parents=True)
    (public_headers / "Other").mkdir(parents=True)
    (public_headers / "Nested.h").write_text("// umbrella")
    (public_headers / "Sub" / "Alpha.h").write_text("// sub-alpha")
    (public_headers / "Sub" / "Helper.h").write_text("// sub-helper")
    (public_headers / "Other" / "Alpha.h").write_text("// other-alpha")
    (staged / "Sources" / "Nested" / "Nested.m").write_text("// impl")

    raw_dump = {
        "name": "Nested",
        "products": [{"name": "Nested", "type": {"library": ["automatic"]},
                      "targets": ["Nested"]}],
        "targets": [
            {
                "name": "Nested",
                "type": "regular",
                "path": "Sources/Nested",
                "publicHeadersPath": "include",
                "dependencies": [],
            }
        ],
    }
    package = Package(
        name="Nested",
        tools_version="5.7.0",
        platforms=[],
        products=[Product(name="Nested", linkage=Linkage.AUTOMATIC,
                          targets=["Nested"])],
        targets=[Target(
            name="Nested",
            kind=TargetKind.REGULAR,
            path="Sources/Nested",
            public_headers_path="include",
            dependencies=[],
            exclude=[],
            language=Language.OBJC,
        )],
        schemes=[],
        raw_dump=raw_dump,
        staged_dir=staged,
    )

    fw = base / "Nested.framework"
    fw.mkdir()
    injected = inject_objc_headers(
        package=package,
        product_name="Nested",
        fw_name="Nested",
        fw_path=fw,
        verbose=False,
    )
    _assert(injected, "inject_objc_headers should have returned True")
    # The umbrella header still lands at the top of Headers/.
    _assert((fw / "Headers" / "Nested.h").is_file(),
            "umbrella header missing at Headers/Nested.h")
    # Nested headers preserve their subpath — NOT flattened to basename.
    _assert((fw / "Headers" / "Sub" / "Alpha.h").is_file(),
            "nested Sub/Alpha.h was not copied to Headers/Sub/Alpha.h "
            "(flattening regressed?)")
    _assert((fw / "Headers" / "Sub" / "Helper.h").is_file(),
            "nested Sub/Helper.h was not copied to Headers/Sub/Helper.h")
    # Same basename in a different subdir survives — if we'd flattened,
    # this would have been silently overwritten by Sub/Alpha.h.
    _assert((fw / "Headers" / "Other" / "Alpha.h").is_file(),
            "Other/Alpha.h was clobbered by Sub/Alpha.h (flattening regressed?)")
    alpha_sub = (fw / "Headers" / "Sub" / "Alpha.h").read_text()
    alpha_other = (fw / "Headers" / "Other" / "Alpha.h").read_text()
    _assert(alpha_sub == "// sub-alpha", f"Sub/Alpha.h content: {alpha_sub!r}")
    _assert(alpha_other == "// other-alpha",
            f"Other/Alpha.h content: {alpha_other!r} — basename collision "
            "silently overwrote one of the files")
    # Umbrella case: modulemap uses `module * { export * }` which walks
    # the directory on its own, so no explicit nested header list is
    # required — just confirm the umbrella line landed.
    modulemap = (fw / "Modules" / "module.modulemap").read_text()
    _assert("umbrella header \"Nested.h\"" in modulemap,
            f"modulemap missing umbrella header reference:\n{modulemap}")


def _selftest_inject_objc_headers_nested_no_umbrella(tmp_root: Path) -> None:
    """When there's no umbrella header, the generated modulemap must
    enumerate nested headers by their relative path (not just the
    basename), otherwise Clang can't resolve `<Module/Sub/Foo.h>` at
    bind time."""
    base = tmp_root / "objc_inject_nested_no_umbrella"
    staged = base / "staged"
    public_headers = staged / "Sources" / "Flat" / "include"
    (public_headers / "Sub").mkdir(parents=True)
    (public_headers / "Alpha.h").write_text("// top")
    (public_headers / "Sub" / "Beta.h").write_text("// nested")
    (staged / "Sources" / "Flat" / "Flat.m").write_text("// impl")

    raw_dump = {
        "name": "Flat",
        "products": [{"name": "Flat", "type": {"library": ["automatic"]},
                      "targets": ["Flat"]}],
        "targets": [
            {
                "name": "Flat",
                "type": "regular",
                "path": "Sources/Flat",
                "publicHeadersPath": "include",
                "dependencies": [],
            }
        ],
    }
    package = Package(
        name="Flat",
        tools_version="5.7.0",
        platforms=[],
        products=[Product(name="Flat", linkage=Linkage.AUTOMATIC, targets=["Flat"])],
        targets=[Target(
            name="Flat",
            kind=TargetKind.REGULAR,
            path="Sources/Flat",
            public_headers_path="include",
            dependencies=[],
            exclude=[],
            language=Language.OBJC,
        )],
        schemes=[],
        raw_dump=raw_dump,
        staged_dir=staged,
    )

    fw = base / "Flat.framework"
    fw.mkdir()
    injected = inject_objc_headers(
        package=package,
        product_name="Flat",
        fw_name="Flat",
        fw_path=fw,
        verbose=False,
    )
    _assert(injected, "expected injection to succeed")
    _assert((fw / "Headers" / "Alpha.h").is_file(), "top-level header missing")
    _assert((fw / "Headers" / "Sub" / "Beta.h").is_file(),
            "nested header not preserved under Headers/Sub/")
    modulemap = (fw / "Modules" / "module.modulemap").read_text()
    _assert("header \"Alpha.h\"" in modulemap,
            f"modulemap missing Alpha header line:\n{modulemap}")
    _assert("header \"Sub/Beta.h\"" in modulemap,
            f"modulemap must list nested headers with their relative path "
            f"(got:\n{modulemap})")
    _assert("umbrella header" not in modulemap,
            "explicit modulemap should not use umbrella")


def _make_archive_intermediates_bundle(
    dd_path: Path,
    *,
    scheme: str,
    sdk_dir: str,
    bundle_name: str,
    files: Optional[Dict[str, str]] = None,
) -> Path:
    """Build a synthetic dd_path/Build/Intermediates.noindex/.../<X>.bundle/
    layout matching what xcodebuild's `archive` action produces.

    Returns the path to the created bundle directory.
    """
    bundle_dir = (
        dd_path
        / "Build"
        / "Intermediates.noindex"
        / "ArchiveIntermediates"
        / scheme
        / "BuildProductsPath"
        / sdk_dir
        / bundle_name
    )
    bundle_dir.mkdir(parents=True)
    contents = files if files is not None else {
        "Info.plist": (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<plist version="1.0"><dict>'
            '<key>CFBundleIdentifier</key><string>com.example.test</string>'
            '</dict></plist>\n'
        ),
        "Assets.bin": "stub-asset-bytes",
    }
    for rel, body in contents.items():
        full = bundle_dir / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(body)
    return bundle_dir


def _selftest_inject_resource_bundles_copies_swiftpm_bundle(tmp_root: Path) -> None:
    """SwiftPM-emitted `<Package>_<Target>.bundle` directories must be
    copied from the slice's `BuildProductsPath` into the framework root,
    on both device and simulator slices, with their inner files preserved.
    This is what makes Stripe3DS2-style resource lookups
    (`+[STDSBundleLocator stdsResourcesBundle]`) succeed at runtime.
    """
    base = tmp_root / "inject_bundles_basic"
    device_dd = base / "dd-device"
    sim_dd = base / "dd-sim"
    _make_archive_intermediates_bundle(
        device_dd,
        scheme="Stripe3DS2",
        sdk_dir="Release-iphoneos",
        bundle_name="Stripe_Stripe3DS2.bundle",
        files={"PrivacyInfo.xcprivacy": "<plist/>", "image.png": "PNGSTUB"},
    )
    _make_archive_intermediates_bundle(
        sim_dd,
        scheme="Stripe3DS2",
        sdk_dir="Release-iphonesimulator",
        bundle_name="Stripe_Stripe3DS2.bundle",
        files={"PrivacyInfo.xcprivacy": "<plist/>", "image.png": "PNGSTUB"},
    )

    device_fw = base / "device" / "Stripe3DS2.framework"
    device_fw.mkdir(parents=True)
    sim_fw = base / "simulator" / "Stripe3DS2.framework"
    sim_fw.mkdir(parents=True)

    n_device = inject_resource_bundles(
        fw_path=device_fw,
        fw_name="Stripe3DS2",
        dd_path=device_dd,
        variant="device",
        verbose=False,
    )
    n_sim = inject_resource_bundles(
        fw_path=sim_fw,
        fw_name="Stripe3DS2",
        dd_path=sim_dd,
        variant="simulator",
        verbose=False,
    )
    _assert(n_device == 1, f"device should have copied 1 bundle, got {n_device}")
    _assert(n_sim == 1, f"sim should have copied 1 bundle, got {n_sim}")
    for fw in (device_fw, sim_fw):
        injected = fw / "Stripe_Stripe3DS2.bundle"
        _assert(injected.is_dir(),
                f"injected bundle missing in {fw}: expected {injected}")
        _assert((injected / "PrivacyInfo.xcprivacy").is_file(),
                "inner PrivacyInfo.xcprivacy not copied")
        _assert((injected / "image.png").is_file(),
                "inner image.png not copied")


def _selftest_inject_resource_bundles_no_resources_is_noop(tmp_root: Path) -> None:
    """When the build unit declared no `.process` / `.copy` resources,
    `BuildProductsPath` has no `*.bundle` directories. The injection step
    must return 0 and not touch the framework — most SPM packages take
    this path.
    """
    base = tmp_root / "inject_bundles_noop"
    dd_path = base / "dd"
    # Realistic empty BuildProductsPath: directory exists, no .bundle inside.
    (dd_path / "Build" / "Intermediates.noindex" / "ArchiveIntermediates"
        / "Foo" / "BuildProductsPath" / "Release-iphoneos").mkdir(parents=True)
    fw = base / "Foo.framework"
    fw.mkdir(parents=True)

    n = inject_resource_bundles(
        fw_path=fw,
        fw_name="Foo",
        dd_path=dd_path,
        variant="device",
        verbose=False,
    )
    _assert(n == 0, f"expected 0 bundles when none built, got {n}")
    # Framework directory must remain empty.
    _assert(list(fw.iterdir()) == [],
            f"framework was modified despite no bundles: {list(fw.iterdir())}")


def _selftest_inject_resource_bundles_idempotent(tmp_root: Path) -> None:
    """A second `inject_resource_bundles` call must overwrite the existing
    bundle in place (so re-runs after partial failures stay clean) and
    keep the framework's bundle current with whatever the latest archive
    produced."""
    base = tmp_root / "inject_bundles_idempotent"
    dd_path = base / "dd"
    bundle = _make_archive_intermediates_bundle(
        dd_path,
        scheme="Foo",
        sdk_dir="Release-iphoneos",
        bundle_name="Pkg_Foo.bundle",
        files={"v1.txt": "first"},
    )
    fw = base / "Foo.framework"
    fw.mkdir(parents=True)
    inject_resource_bundles(fw_path=fw, fw_name="Foo", dd_path=dd_path,
                            variant="device", verbose=False)
    _assert((fw / "Pkg_Foo.bundle" / "v1.txt").is_file(),
            "first injection did not land")

    # Simulate a second archive that updated the bundle's contents.
    shutil.rmtree(bundle)
    _make_archive_intermediates_bundle(
        dd_path,
        scheme="Foo",
        sdk_dir="Release-iphoneos",
        bundle_name="Pkg_Foo.bundle",
        files={"v2.txt": "second"},
    )
    n = inject_resource_bundles(fw_path=fw, fw_name="Foo", dd_path=dd_path,
                                variant="device", verbose=False)
    _assert(n == 1, f"second injection should report 1 copy, got {n}")
    _assert((fw / "Pkg_Foo.bundle" / "v2.txt").is_file(),
            "stale bundle not refreshed on re-injection")
    _assert(not (fw / "Pkg_Foo.bundle" / "v1.txt").exists(),
            "old bundle contents leaked through replace")


def _selftest_find_resource_bundles_dedupe_and_filter(tmp_root: Path) -> None:
    """`_find_resource_bundles` must (1) prefer the canonical
    ArchiveIntermediates path over the `Build/Products` symlinked view
    when both contain a same-named bundle, (2) ignore non-directory
    entries and unrelated artifacts, and (3) walk the per-sdk
    subdirectories under BuildProductsPath without missing any.
    """
    base = tmp_root / "find_bundles"
    dd_path = base / "dd"

    # Canonical path: two bundles under different schemes' BuildProductsPath
    # subdirs (xcodebuild can build dependent targets while archiving the
    # primary scheme — both end up here).
    archive_a = _make_archive_intermediates_bundle(
        dd_path, scheme="Primary", sdk_dir="Release-iphoneos",
        bundle_name="Pkg_A.bundle",
    )
    archive_b = _make_archive_intermediates_bundle(
        dd_path, scheme="DepBuild", sdk_dir="Release-iphoneos",
        bundle_name="Pkg_B.bundle",
    )
    # Same-named bundle visible through Build/Products/Release-iphoneos —
    # this is the symlinked view of the same artifact in the real layout.
    symlink_view_dir = dd_path / "Build" / "Products" / "Release-iphoneos"
    symlink_view_dir.mkdir(parents=True)
    (symlink_view_dir / "Pkg_A.bundle").mkdir()
    (symlink_view_dir / "Pkg_A.bundle" / "from-products.txt").write_text("dup")
    # A loose file with `.bundle` suffix MUST be ignored — only directories
    # are real bundles.
    (symlink_view_dir / "stray.bundle").write_text("not-a-bundle")

    found = _find_resource_bundles(dd_path)
    found_names = sorted(p.name for p in found)
    _assert(found_names == ["Pkg_A.bundle", "Pkg_B.bundle"],
            f"unexpected bundle list: {found_names}")
    # Pkg_A.bundle must come from ArchiveIntermediates, not the symlink view
    pkg_a = next(p for p in found if p.name == "Pkg_A.bundle")
    _assert("ArchiveIntermediates" in pkg_a.parts,
            f"Pkg_A.bundle should resolve to ArchiveIntermediates, got {pkg_a}")
    _assert(pkg_a == archive_a,
            f"expected canonical {archive_a}, got {pkg_a}")
    # Pkg_B.bundle came only from ArchiveIntermediates (no symlink view set
    # up for it); confirm we still picked it up.
    pkg_b = next(p for p in found if p.name == "Pkg_B.bundle")
    _assert(pkg_b == archive_b, f"expected {archive_b}, got {pkg_b}")


def _selftest_find_objc_headers_dir_priority(tmp_root: Path) -> None:
    """fw_name match wins over product_name match wins over any-target."""
    base = tmp_root / "headers_priority"
    staged = base / "staged"

    # Three targets, each with its own publicHeadersPath:
    #   - First (would-be-first-match)   includes one header.
    #   - Second (matches product_name) includes one header.
    #   - Third (matches fw_name)       includes one header.
    for tname, header in [("FirstAny", "first.h"), ("Prod", "prod.h"), ("MyFW", "fw.h")]:
        d = staged / "Sources" / tname / "include"
        d.mkdir(parents=True)
        (d / header).write_text("// h")

    raw_dump = {
        "products": [
            {
                "name": "Prod",
                "type": {"library": ["automatic"]},
                "targets": ["FirstAny", "Prod", "MyFW"],
            }
        ],
        "targets": [
            {
                "name": tname,
                "type": "regular",
                "path": f"Sources/{tname}",
                "publicHeadersPath": "include",
                "dependencies": [],
            }
            for tname in ("FirstAny", "Prod", "MyFW")
        ],
    }
    package = Package(
        name="Prod",
        tools_version="5.7.0",
        platforms=[],
        products=[Product(name="Prod", linkage=Linkage.AUTOMATIC,
                          targets=["FirstAny", "Prod", "MyFW"])],
        targets=[
            Target(name=tname, kind=TargetKind.REGULAR,
                   path=f"Sources/{tname}", public_headers_path="include",
                   dependencies=[], exclude=[], language=Language.OBJC)
            for tname in ("FirstAny", "Prod", "MyFW")
        ],
        schemes=[],
        raw_dump=raw_dump,
        staged_dir=staged,
    )

    # fw_name match wins (MyFW)
    found = _find_objc_headers_dir(package, product_name="Prod", fw_name="MyFW")
    _assert(found is not None and found.parent.name == "MyFW",
            f"fw_name priority broken: {found}")
    # No fw_name match → product_name match wins (Prod)
    found = _find_objc_headers_dir(package, product_name="Prod", fw_name="NoSuch")
    _assert(found is not None and found.parent.name == "Prod",
            f"product_name priority broken: {found}")


def _selftest_find_objc_headers_dir_defaults_to_include(tmp_root: Path) -> None:
    """Regression: when `publicHeadersPath` is absent from Package.swift,
    `_find_objc_headers_dir` must fall back to SPM's conventional "include"
    subdirectory. Stripe restructured its Package.swift to drop the explicit
    `publicHeadersPath` entries; without the fallback, header injection became
    a silent no-op and the Mixed-framework verifier fired 'no public .h files'.
    """
    base = tmp_root / "headers_default_include"
    staged = base / "staged"

    fw_include = staged / "Sources" / "StripeCore" / "include"
    fw_include.mkdir(parents=True)
    (fw_include / "StripeCore.h").write_text("// umbrella")

    raw_dump = {
        "products": [
            {
                "name": "StripeCore",
                "type": {"library": ["automatic"]},
                "targets": ["StripeCore"],
            }
        ],
        "targets": [
            {
                "name": "StripeCore",
                "type": "regular",
                "path": "Sources/StripeCore",
                "publicHeadersPath": None,
                "dependencies": [],
            },
        ],
    }
    package = Package(
        name="StripeCore",
        tools_version="5.7.0",
        platforms=[],
        products=[Product(name="StripeCore", linkage=Linkage.AUTOMATIC,
                          targets=["StripeCore"])],
        targets=[
            Target(name="StripeCore", kind=TargetKind.REGULAR,
                   path="Sources/StripeCore", public_headers_path=None,
                   dependencies=[], exclude=[], language=Language.OBJC),
        ],
        schemes=[],
        raw_dump=raw_dump,
        staged_dir=staged,
    )
    found = _find_objc_headers_dir(package, product_name="StripeCore",
                                    fw_name="StripeCore")
    _assert(
        found is not None and found.name == "include"
        and found.parent.name == "StripeCore",
        f"expected include/ fallback when publicHeadersPath is None, got {found!r}",
    )


def _selftest_find_objc_headers_dir_follows_target_edge(tmp_root: Path) -> None:
    """Regression for Codex [P1]: `_find_objc_headers_dir` must follow
    both `byName` and `target` dependency shapes. Uses the `target`
    shape that GRDB (and this file's embedded snapshot) emits.

    Layout: product 'Umbrella' backs one target 'Shell' that has no
    headers of its own and depends on 'Guts' (via .target(name:))
    which owns the public-header directory. Before the fix, the walker
    only looked at `byName` edges and returned None.
    """
    base = tmp_root / "headers_target_edge"
    staged = base / "staged"

    # Shell: no publicHeadersPath. Guts: publicHeadersPath with one .h.
    guts_include = staged / "Sources" / "Guts" / "include"
    guts_include.mkdir(parents=True)
    (guts_include / "Guts.h").write_text("// g")
    (staged / "Sources" / "Shell").mkdir(parents=True)

    raw_dump = {
        "products": [
            {
                "name": "Umbrella",
                "type": {"library": ["automatic"]},
                "targets": ["Shell"],
            }
        ],
        "targets": [
            {
                "name": "Shell",
                "type": "regular",
                "path": "Sources/Shell",
                "publicHeadersPath": None,
                # `.target(name: "Guts")` dump shape.
                "dependencies": [{"target": ["Guts", None]}],
            },
            {
                "name": "Guts",
                "type": "regular",
                "path": "Sources/Guts",
                "publicHeadersPath": "include",
                "dependencies": [],
            },
        ],
    }
    package = Package(
        name="Umbrella",
        tools_version="5.7.0",
        platforms=[],
        products=[Product(name="Umbrella", linkage=Linkage.AUTOMATIC,
                          targets=["Shell"])],
        targets=[
            Target(name="Shell", kind=TargetKind.REGULAR,
                   path="Sources/Shell", public_headers_path=None,
                   dependencies=["Guts"], exclude=[],
                   language=Language.OBJC),
            Target(name="Guts", kind=TargetKind.REGULAR,
                   path="Sources/Guts", public_headers_path="include",
                   dependencies=[], exclude=[], language=Language.OBJC),
        ],
        schemes=[],
        raw_dump=raw_dump,
        staged_dir=staged,
    )
    found = _find_objc_headers_dir(package, product_name="Umbrella",
                                    fw_name="Umbrella")
    _assert(
        found is not None and found.parent.name == "Guts",
        f"expected Guts include dir via .target() dep, got {found!r}",
    )


def _selftest_find_objc_headers_dir_umbrella_at_target_root(tmp_root: Path) -> None:
    """Regression: when `publicHeadersPath` is omitted and the target
    follows the Stripe-style umbrella-header pattern (one
    `<TargetName>.h` at the top of the target directory, no `include/`
    subdir), `_find_objc_headers_dir` must return the target dir
    itself.

    Background: Stripe SDK 25.x ships every product this way —
    `StripePayments/StripePayments/StripePayments.h` is the public
    surface, plus a sea of `.swift`. Before the fix the lookup
    silently returned None, which made `inject_objc_headers` a no-op
    and produced xcframeworks missing `Headers/` and
    `Modules/module.modulemap`.
    """
    base = tmp_root / "headers_umbrella_at_root"
    staged = base / "staged"

    target_dir = staged / "Sources" / "StripePayments"
    target_dir.mkdir(parents=True)
    (target_dir / "StripePayments.h").write_text("// umbrella")
    # A `-Swift.h` file at the same level should NOT be confused for
    # the umbrella — they're generated artifacts on the framework
    # output side, not source-of-truth public headers.
    (target_dir / "StripePayments-Swift.h").write_text("// generated")
    (target_dir / "API.swift").write_text("// swift")

    raw_dump = {
        "products": [
            {
                "name": "StripePayments",
                "type": {"library": ["automatic"]},
                "targets": ["StripePayments"],
            }
        ],
        "targets": [
            {
                "name": "StripePayments",
                "type": "regular",
                "path": "Sources/StripePayments",
                "publicHeadersPath": None,
                "dependencies": [],
            },
        ],
    }
    package = Package(
        name="StripePayments",
        tools_version="5.7.0",
        platforms=[],
        products=[Product(name="StripePayments", linkage=Linkage.AUTOMATIC,
                          targets=["StripePayments"])],
        targets=[
            Target(name="StripePayments", kind=TargetKind.REGULAR,
                   path="Sources/StripePayments", public_headers_path=None,
                   dependencies=[], exclude=[], language=Language.OBJC),
        ],
        schemes=[],
        raw_dump=raw_dump,
        staged_dir=staged,
    )
    found = _find_objc_headers_dir(package, product_name="StripePayments",
                                    fw_name="StripePayments")
    _assert(
        found is not None and found.name == "StripePayments"
        and found.parent.name == "Sources",
        f"expected target dir as headers root for umbrella-at-root "
        f"layout, got {found!r}",
    )


def _selftest_find_objc_headers_dir_umbrella_named_variant(tmp_root: Path) -> None:
    """Regression: the umbrella-at-target-root pattern also accepts
    `<TargetName>-umbrella.h`. Stripe's top-level `Stripe` product
    target ships `Stripe-umbrella.h` instead of `Stripe.h`, so the
    heuristic must accept both names.
    """
    base = tmp_root / "headers_umbrella_named_variant"
    staged = base / "staged"

    target_dir = staged / "Sources" / "Stripe"
    target_dir.mkdir(parents=True)
    (target_dir / "Stripe-umbrella.h").write_text("// umbrella")
    (target_dir / "API.swift").write_text("// swift")

    raw_dump = {
        "products": [
            {"name": "Stripe", "type": {"library": ["automatic"]},
             "targets": ["Stripe"]},
        ],
        "targets": [
            {"name": "Stripe", "type": "regular",
             "path": "Sources/Stripe", "publicHeadersPath": None,
             "dependencies": []},
        ],
    }
    package = Package(
        name="Stripe", tools_version="5.7.0", platforms=[],
        products=[Product(name="Stripe", linkage=Linkage.AUTOMATIC,
                          targets=["Stripe"])],
        targets=[
            Target(name="Stripe", kind=TargetKind.REGULAR,
                   path="Sources/Stripe", public_headers_path=None,
                   dependencies=[], exclude=[], language=Language.OBJC),
        ],
        schemes=[], raw_dump=raw_dump, staged_dir=staged,
    )
    found = _find_objc_headers_dir(package, product_name="Stripe",
                                    fw_name="Stripe")
    _assert(
        found is not None and found.name == "Stripe"
        and found.parent.name == "Sources",
        f"expected target dir for <Target>-umbrella.h pattern, "
        f"got {found!r}",
    )


def _selftest_find_objc_headers_dir_umbrella_rejects_unrelated_h(tmp_root: Path) -> None:
    """Regression: the umbrella heuristic must NOT pick up generic
    top-level .h files that aren't `<TargetName>.h` or
    `<TargetName>-umbrella.h`. Returning the target dir in that case
    would over-inject private/internal headers into the framework's
    public surface, since `inject_objc_headers` walks the dir
    recursively and copies every .h.
    """
    base = tmp_root / "headers_umbrella_strict"
    staged = base / "staged"

    target_dir = staged / "Sources" / "Foo"
    target_dir.mkdir(parents=True)
    # Top-level .h files exist, but none match the target name.
    (target_dir / "Helper.h").write_text("// internal")
    (target_dir / "PrivateThing.h").write_text("// internal")
    (target_dir / "API.swift").write_text("// swift")

    raw_dump = {
        "products": [
            {"name": "Foo", "type": {"library": ["automatic"]},
             "targets": ["Foo"]},
        ],
        "targets": [
            {"name": "Foo", "type": "regular",
             "path": "Sources/Foo", "publicHeadersPath": None,
             "dependencies": []},
        ],
    }
    package = Package(
        name="Foo", tools_version="5.7.0", platforms=[],
        products=[Product(name="Foo", linkage=Linkage.AUTOMATIC,
                          targets=["Foo"])],
        targets=[
            Target(name="Foo", kind=TargetKind.REGULAR,
                   path="Sources/Foo", public_headers_path=None,
                   dependencies=[], exclude=[], language=Language.OBJC),
        ],
        schemes=[], raw_dump=raw_dump, staged_dir=staged,
    )
    found = _find_objc_headers_dir(package, product_name="Foo",
                                    fw_name="Foo")
    _assert(
        found is None,
        f"expected None when no <Target>.h or <Target>-umbrella.h is "
        f"present (only generic .h), got {found!r}",
    )


def _selftest_find_objc_headers_dir_umbrella_swift_classified_still_fires(
    tmp_root: Path,
) -> None:
    """Regression: the umbrella-at-target-root branch must KEEP firing
    for Swift-classified targets — Stripe's `StripePayments` is the
    canonical case (220 .swift + 1 `<TargetName>.h` umbrella, 0 .m/.mm).

    The classifier was tightened so a target with no .m/.mm classifies
    as Swift even when stub `.h` files are present (BlinkIDUX fix). The
    umbrella branch must still fire for these targets: even when the
    umbrella file itself is only a FOUNDATION_EXPORT stub, the resulting
    `Headers/<Target>.h` + `module.modulemap` is what lets ObjC consumers
    `#import <StripePayments/StripePayments.h>` and the Swift bridge
    `-Swift.h` plug into a published Clang module. Gating this branch
    on `target.language != Language.SWIFT` would silently drop the
    Stripe surface, which is why no such gate exists.
    """
    base = tmp_root / "headers_umbrella_swift_classified"
    staged = base / "staged"

    target_dir = staged / "StripePayments" / "StripePayments"
    target_dir.mkdir(parents=True)
    (target_dir / "StripePayments.h").write_text(
        "// FOUNDATION_EXPORT double StripePaymentsVersionNumber;"
    )
    (target_dir / "API.swift").write_text("// swift")

    raw_dump = {
        "products": [
            {"name": "StripePayments", "type": {"library": ["automatic"]},
             "targets": ["StripePayments"]},
        ],
        "targets": [
            {"name": "StripePayments", "type": "regular",
             "path": "StripePayments/StripePayments",
             "publicHeadersPath": None, "dependencies": []},
        ],
    }
    package = Package(
        name="StripePayments", tools_version="5.7.0", platforms=[],
        products=[Product(name="StripePayments", linkage=Linkage.AUTOMATIC,
                          targets=["StripePayments"])],
        targets=[
            Target(name="StripePayments", kind=TargetKind.REGULAR,
                   path="StripePayments/StripePayments",
                   public_headers_path=None, dependencies=[], exclude=[],
                   language=Language.SWIFT),
        ],
        schemes=[], raw_dump=raw_dump, staged_dir=staged,
    )
    found = _find_objc_headers_dir(package, product_name="StripePayments",
                                    fw_name="StripePayments")
    _assert(
        found is not None and found.name == "StripePayments"
        and found.parent.name == "StripePayments",
        f"Swift-classified target with <Target>.h umbrella (Stripe shape) "
        f"must still return target dir so Headers/ + modulemap get "
        f"injected for ObjC consumers; got {found!r}",
    )


def _selftest_find_objc_headers_dir_dep_walk_no_implicit_leak(tmp_root: Path) -> None:
    """Regression: when the direct product target has no public
    headers and a separately-packaged dep carries an implicit-layout
    `include/`, `_find_objc_headers_dir` must NOT return the dep's
    `include/`.

    Background: Stripe3DS2 is exposed by Stripe's Package.swift as its
    own library product (`library(name: "Stripe3DS2", ...)`), so it
    gets its own xcframework. It also ships an `include/` with 33
    STDS*.h files and is reachable as a dep of StripePayments. An
    earlier overly-lenient fix walked deps with implicit-layout
    fallback enabled and silently duplicated Stripe3DS2's headers
    into StripePayments.framework. Deps that back their own product
    must opt in via an explicit `publicHeadersPath` to surface their
    headers in the parent; their default `include/` belongs to their
    own xcframework only.
    """
    base = tmp_root / "headers_dep_no_leak"
    staged = base / "staged"

    # StripePayments-style direct target: only Swift, zero .h files.
    parent_dir = staged / "Sources" / "StripePayments"
    parent_dir.mkdir(parents=True)
    (parent_dir / "API.swift").write_text("// swift only")

    # Stripe3DS2-style dep: implicit include/ layout (NO publicHeadersPath
    # declared in the dump). Must NOT leak — it's a sibling product,
    # built into its own xcframework.
    dep_include = staged / "Sources" / "Stripe3DS2" / "include"
    dep_include.mkdir(parents=True)
    (dep_include / "STDSConfigParameters.h").write_text("// h")
    (dep_include / "STDSStripe3DS2.h").write_text("// h")

    raw_dump = {
        "products": [
            {
                "name": "StripePayments",
                "type": {"library": ["automatic"]},
                "targets": ["StripePayments"],
            },
            # Stripe3DS2 is a sibling library product — this is what
            # marks the dep as "separately packaged" and turns off the
            # implicit-layout fallback for the dep walk.
            {
                "name": "Stripe3DS2",
                "type": {"library": ["automatic"]},
                "targets": ["Stripe3DS2"],
            },
        ],
        "targets": [
            {
                "name": "StripePayments",
                "type": "regular",
                "path": "Sources/StripePayments",
                "publicHeadersPath": None,
                "dependencies": [{"target": ["Stripe3DS2", None]}],
            },
            {
                "name": "Stripe3DS2",
                "type": "regular",
                "path": "Sources/Stripe3DS2",
                "publicHeadersPath": None,
                "dependencies": [],
            },
        ],
    }
    package = Package(
        name="StripePayments",
        tools_version="5.7.0",
        platforms=[],
        products=[
            Product(name="StripePayments", linkage=Linkage.AUTOMATIC,
                    targets=["StripePayments"]),
            Product(name="Stripe3DS2", linkage=Linkage.AUTOMATIC,
                    targets=["Stripe3DS2"]),
        ],
        targets=[
            Target(name="StripePayments", kind=TargetKind.REGULAR,
                   path="Sources/StripePayments", public_headers_path=None,
                   dependencies=["Stripe3DS2"], exclude=[],
                   language=Language.OBJC),
            Target(name="Stripe3DS2", kind=TargetKind.REGULAR,
                   path="Sources/Stripe3DS2", public_headers_path=None,
                   dependencies=[], exclude=[], language=Language.OBJC),
        ],
        schemes=[],
        raw_dump=raw_dump,
        staged_dir=staged,
    )
    found = _find_objc_headers_dir(package, product_name="StripePayments",
                                    fw_name="StripePayments")
    _assert(
        found is None,
        f"expected None (no headers): separately-packaged dep with "
        f"implicit include/ must not leak into parent framework, "
        f"got {found!r}",
    )


def _selftest_find_objc_headers_dir_dep_walk_internal_default_include(tmp_root: Path) -> None:
    """Regression: an internal-only dep target with the SPM default
    `Sources/<Dep>/include/` layout (no explicit `publicHeadersPath`,
    not exposed as its own product) must have its public headers
    folded into the parent framework.

    Background: the prior leak fix flipped implicit-layout fallback
    OFF for every dep. That stopped Stripe3DS2's `include/` from
    bleeding into StripePayments — but it also broke the very common
    SPM shape where a product target depends on a helper sub-target
    whose ObjC headers live in the conventional `include/` directory
    and is never separately packaged. With the unconditional gate,
    `_find_objc_headers_dir` returned None, `inject_objc_headers`
    became a no-op, and the framework shipped with no `Headers/` or
    `module.modulemap`.

    The fix distinguishes deps that back another product (separately
    packaged → don't allow implicit) from deps that no product
    references (internal → allow implicit).
    """
    base = tmp_root / "headers_dep_internal_include"
    staged = base / "staged"

    # Parent product target: Swift-only, no .h of its own.
    parent_dir = staged / "Sources" / "Widget"
    parent_dir.mkdir(parents=True)
    (parent_dir / "Widget.swift").write_text("// swift only")

    # Internal helper: SPM default include/ layout, NO explicit
    # publicHeadersPath, NOT listed in any product's targets.
    helper_include = staged / "Sources" / "WidgetCore" / "include"
    helper_include.mkdir(parents=True)
    (helper_include / "WCAttributes.h").write_text("// h")
    (helper_include / "WCMetrics.h").write_text("// h")

    raw_dump = {
        # Single product. WidgetCore is intentionally absent from the
        # products list — that's what marks it as internal so the dep
        # walk allows the implicit `include/` fallback.
        "products": [
            {
                "name": "Widget",
                "type": {"library": ["automatic"]},
                "targets": ["Widget"],
            }
        ],
        "targets": [
            {
                "name": "Widget",
                "type": "regular",
                "path": "Sources/Widget",
                "publicHeadersPath": None,
                "dependencies": [{"target": ["WidgetCore", None]}],
            },
            {
                "name": "WidgetCore",
                "type": "regular",
                "path": "Sources/WidgetCore",
                "publicHeadersPath": None,
                "dependencies": [],
            },
        ],
    }
    package = Package(
        name="Widget",
        tools_version="5.7.0",
        platforms=[],
        products=[Product(name="Widget", linkage=Linkage.AUTOMATIC,
                          targets=["Widget"])],
        targets=[
            Target(name="Widget", kind=TargetKind.REGULAR,
                   path="Sources/Widget", public_headers_path=None,
                   dependencies=["WidgetCore"], exclude=[],
                   language=Language.OBJC),
            Target(name="WidgetCore", kind=TargetKind.REGULAR,
                   path="Sources/WidgetCore", public_headers_path=None,
                   dependencies=[], exclude=[], language=Language.OBJC),
        ],
        schemes=[],
        raw_dump=raw_dump,
        staged_dir=staged,
    )
    found = _find_objc_headers_dir(package, product_name="Widget",
                                    fw_name="Widget")
    _assert(
        found is not None and found.name == "include"
        and found.parent.name == "WidgetCore",
        f"expected internal dep's default include/ to be folded into "
        f"the parent framework, got {found!r}",
    )


def _selftest_archive_framework_path_recursive(tmp_root: Path) -> None:
    """_archive_framework_path finds X.framework anywhere under Products/."""
    base = tmp_root / "fw_locate"
    deep = base / "Products" / "usr" / "local" / "lib" / "MyFW.framework"
    deep.mkdir(parents=True)
    found = _archive_framework_path(base, "MyFW")
    _assert(found is not None and found == deep,
            f"expected to find MyFW.framework, got {found}")
    _assert(_archive_framework_path(base, "Other") is None,
            "should return None when name doesn't match")


# --- Verify self-tests ----------------------------------------------------
#
# Verify is per-unit and never raises for "the user's xcframework is broken"
# (REWRITE_DESIGN.md §5.5). These tests cover the format helpers, the
# happy/sad paths of `_verify_one_unit`, and the summary printer's column
# formatting. The synthetic xcframeworks below are minimum viable trees —
# enough to satisfy `plistlib.load`, `detect_framework_type`, and the
# per-slice binary lookup; the actual binary check is monkey-patched.


def _selftest_format_size_iec() -> None:
    """`_format_size_iec` matches `du -sh` style across the K/M/G boundaries."""
    K, M, G = 1024, 1024 ** 2, 1024 ** 3
    cases = [
        (0, "0B"),
        (1, "1B"),
        (1023, "1023B"),
        (K, "1K"),
        (310 * K, "310K"),
        (M - 1, "1024K"),  # rounds up at the boundary
        (M, "1.0M"),
        (4 * M + M // 5, "4.2M"),
        (G - 1, "1024.0M"),
        (G, "1.0G"),
        (3 * G + G // 2, "3.5G"),
    ]
    for raw, want in cases:
        got = _format_size_iec(raw)
        _assert(got == want, f"_format_size_iec({raw}) = {got!r}, want {want!r}")
    # Negative is clamped (defensive — never reached in real Verify).
    _assert(_format_size_iec(-7) == "0B", "negative bytes should clamp to 0B")


def _build_synthetic_xcframework(
    base: Path,
    framework_name: str,
    *,
    flavor: str = "swift",          # "swift" | "objc" | "mixed" | "unknown"
    slices: Sequence[str] = ("ios-arm64", "ios-arm64_x86_64-simulator"),
    omit_info_plist: bool = False,
    corrupt_info_plist: bool = False,
    omit_binary_path_in_plist: bool = False,
    write_abi_json: bool = True,
) -> Path:
    """Build a fake `<name>.xcframework` rooted at `base`.

    The synthetic tree is just enough to satisfy `plistlib.load` and the
    Verify phase's structural checks. Binary linkage is decided by the
    monkey-patched `_check_binary_dynamic` in each test, not by what's
    actually in the file. Returns the xcframework path.
    """
    xc = base / f"{framework_name}.xcframework"
    xc.mkdir(parents=True, exist_ok=True)

    available: List[dict] = []
    for slice_id in slices:
        fw_dir = xc / slice_id / f"{framework_name}.framework"
        fw_dir.mkdir(parents=True, exist_ok=True)
        binary = fw_dir / framework_name
        binary.write_bytes(b"\xcf\xfa\xed\xfe" + b"\x00" * 32)  # fake Mach-O header

        if flavor in ("swift", "mixed"):
            modules = fw_dir / "Modules" / f"{framework_name}.swiftmodule"
            modules.mkdir(parents=True, exist_ok=True)
            (modules / "arm64.swiftinterface").write_text("// interface")
            if write_abi_json:
                (modules / "arm64.abi.json").write_text("{}")
        if flavor in ("objc", "mixed"):
            headers = fw_dir / "Headers"
            headers.mkdir(parents=True, exist_ok=True)
            (headers / f"{framework_name}.h").write_text("// header")
            modules_dir = fw_dir / "Modules"
            modules_dir.mkdir(parents=True, exist_ok=True)
            (modules_dir / "module.modulemap").write_text(
                f"framework module {framework_name} {{ umbrella header \"{framework_name}.h\" }}"
            )

        available.append({
            "LibraryIdentifier": slice_id,
            "LibraryPath": f"{framework_name}.framework",
            "BinaryPath": f"{framework_name}.framework/{framework_name}",
            "SupportedArchitectures": ["arm64"],
            "SupportedPlatform": "ios",
        })

    if omit_binary_path_in_plist:
        for entry in available:
            entry.pop("BinaryPath", None)

    info_plist = xc / "Info.plist"
    if corrupt_info_plist:
        info_plist.write_bytes(b"this is not a valid plist at all\x00\x01\x02")
    elif not omit_info_plist:
        with info_plist.open("wb") as fh:
            plistlib.dump({
                "AvailableLibraries": available,
                "CFBundlePackageType": "XFWK",
                "XCFrameworkFormatVersion": "1.0",
            }, fh)
    return xc


def _selftest_verify_happy_path_swift(tmp_root: Path) -> None:
    """Well-formed Swift xcframework + monkey-patched dynamic check passes."""
    base = tmp_root / "verify_happy_swift"
    base.mkdir()
    xc = _build_synthetic_xcframework(base, "Foo", flavor="swift")
    unit = ExecutedUnit(
        name="Foo",
        xcframework_path=xc,
        framework_name="Foo",
        framework_type="Swift",
    )
    mod = tool
    saved = mod._check_binary_dynamic
    try:
        mod._check_binary_dynamic = lambda _b: True
        results = verify_output([unit], base)
    finally:
        mod._check_binary_dynamic = saved
    _assert(len(results) == 1, f"expected 1 result, got {len(results)}")
    r = results[0]
    _assert(r.passed, f"expected passed=True, got fatal_issues={r.fatal_issues!r}")
    _assert(r.framework_type == "Swift",
            f"expected Swift, got {r.framework_type}")
    _assert(r.size_bytes > 0, "size_bytes should be > 0 for synthetic tree")
    _assert(not r.warnings, f"unexpected warnings: {r.warnings!r}")


# --- Binary-mode static→dynamic promotion ---------------------------------
#
# Issue #39 / Kidoz path: vendor xcframeworks that ship static archives
# masquerading as framework binaries used to fail the Verify check with
# "static archive masquerading as a framework?" even though the .xcframework
# was on disk and structurally fine. `promote_binary_xcframework_static_to_dynamic`
# rewrites those slice binaries in place. The clang invocation itself is hard
# to unit-test (would require mocking subprocess + xcrun), so we cover the
# parts that exercise pure logic: the helper classifiers, the early-exit
# no-op path when every slice is already dynamic, and the malformed-plist
# fail-soft branch.


def _selftest_promote_binary_no_op_when_all_slices_dynamic(tmp_root: Path) -> None:
    """If every slice's binary already reports dynamically-linked, the
    promote function returns an empty list without touching the tree.
    No subprocess calls (clang / lipo) are needed for this path — it's
    pure traversal + the `_check_binary_dynamic` short-circuit.
    """
    from spm_to_xcframework import promote_binary_xcframework_static_to_dynamic
    base = tmp_root / "promote_no_op"
    base.mkdir()
    xc = _build_synthetic_xcframework(
        base, "AlreadyDyn", flavor="swift",
        slices=("ios-arm64", "ios-arm64_x86_64-simulator"),
    )
    # Capture the binary bytes per slice so we can assert no mutation.
    snapshots = {}
    for slice_id in ("ios-arm64", "ios-arm64_x86_64-simulator"):
        b = xc / slice_id / "AlreadyDyn.framework" / "AlreadyDyn"
        snapshots[slice_id] = b.read_bytes()

    mod = tool
    saved = mod._check_binary_dynamic
    try:
        mod._check_binary_dynamic = lambda _b: True  # "everything is dynamic"
        promoted = promote_binary_xcframework_static_to_dynamic(xc)
    finally:
        mod._check_binary_dynamic = saved

    _assert(promoted == [],
            f"already-dynamic xcframework must produce empty promotion list, "
            f"got {promoted!r}")
    for slice_id, before in snapshots.items():
        after = (xc / slice_id / "AlreadyDyn.framework" / "AlreadyDyn").read_bytes()
        _assert(before == after,
                f"slice {slice_id} binary was modified despite being dynamic")


def _selftest_promote_binary_missing_info_plist_returns_empty(tmp_root: Path) -> None:
    """A directory that looks like an xcframework but lacks Info.plist
    is silently skipped (returns []) — the verifier surfaces the real
    issue separately. This is the fail-soft contract documented on the
    function.
    """
    from spm_to_xcframework import promote_binary_xcframework_static_to_dynamic
    base = tmp_root / "promote_no_plist"
    base.mkdir()
    xc = _build_synthetic_xcframework(base, "Hollow", flavor="swift",
                                      omit_info_plist=True)
    promoted = promote_binary_xcframework_static_to_dynamic(xc)
    _assert(promoted == [],
            f"missing Info.plist should yield empty promotion list, "
            f"got {promoted!r}")


def _selftest_promote_binary_corrupt_info_plist_returns_empty(tmp_root: Path) -> None:
    """A corrupt Info.plist (the AppleDouble ghost case) returns [] rather
    than raising. The verifier will mark the unit as fatal-per-unit
    elsewhere; promotion shouldn't crash on its way through.
    """
    from spm_to_xcframework import promote_binary_xcframework_static_to_dynamic
    base = tmp_root / "promote_corrupt_plist"
    base.mkdir()
    xc = _build_synthetic_xcframework(base, "Ghost", flavor="swift",
                                      corrupt_info_plist=True)
    promoted = promote_binary_xcframework_static_to_dynamic(xc)
    _assert(promoted == [],
            f"corrupt Info.plist should yield empty promotion list, "
            f"got {promoted!r}")


def _selftest_platform_slice_for_library_identifier() -> None:
    """The xcframework `LibraryIdentifier` → `PlatformSlice` mapping
    drives SDK + clang min-version flag selection for promotion. Cover
    the common Apple shapes (device/simulator/maccatalyst across the
    platforms we ship).
    """
    from spm_to_xcframework import _platform_slice_for_library_identifier
    # iOS device + simulator
    s = _platform_slice_for_library_identifier("ios-arm64")
    _assert(s is not None and s.sdk_name == "iphoneos",
            f"ios-arm64 → iphoneos, got {s}")
    s = _platform_slice_for_library_identifier("ios-arm64_x86_64-simulator")
    _assert(s is not None and s.sdk_name == "iphonesimulator",
            f"ios sim → iphonesimulator, got {s}")
    # macOS device + maccatalyst
    s = _platform_slice_for_library_identifier("macos-arm64_x86_64")
    _assert(s is not None and s.sdk_name == "macosx",
            f"macos → macosx, got {s}")
    s = _platform_slice_for_library_identifier("ios-arm64_x86_64-maccatalyst")
    _assert(s is not None and s.platform == "maccatalyst",
            f"maccatalyst id → maccatalyst slice, got {s}")
    # tvOS + watchOS + visionOS basic shapes
    s = _platform_slice_for_library_identifier("tvos-arm64")
    _assert(s is not None and s.sdk_name == "appletvos",
            f"tvos device → appletvos, got {s}")
    s = _platform_slice_for_library_identifier("watchos-arm64")
    _assert(s is not None and s.sdk_name == "watchos",
            f"watchos device → watchos sdk, got {s}")
    s = _platform_slice_for_library_identifier("xros-arm64")
    _assert(s is not None and s.platform == "visionos",
            f"xros → visionos platform, got {s}")
    # Unrecognised → None (callers treat as "cannot promote")
    s = _platform_slice_for_library_identifier("freebsd-amd64")
    _assert(s is None,
            f"unrecognised platform should return None, got {s}")


def _selftest_slice_minimum_deployment_target() -> None:
    """The minimum-deployment-target reader prefers the slice plist's
    explicit value (under either of the two key spellings Apple uses)
    and falls back to a sane per-platform default when absent. The
    `-undefined dynamic_lookup` safety net means the exact value is
    not load-bearing for correctness, but the test pins the fallback
    table so a future drift gets caught.
    """
    from spm_to_xcframework import _slice_minimum_deployment_target

    # Explicit value via `MinimumOSVersion`.
    _assert(
        _slice_minimum_deployment_target(
            {"MinimumOSVersion": "16.4"}, "ios") == "16.4",
        "MinimumOSVersion should win",
    )
    # Explicit value via the alternate key spelling.
    _assert(
        _slice_minimum_deployment_target(
            {"MinimumDeploymentTarget": "12.0"}, "macos") == "12.0",
        "MinimumDeploymentTarget should win when MinimumOSVersion absent",
    )
    # MinimumOSVersion takes precedence over MinimumDeploymentTarget.
    _assert(
        _slice_minimum_deployment_target(
            {"MinimumOSVersion": "17.0",
             "MinimumDeploymentTarget": "12.0"}, "ios") == "17.0",
        "MinimumOSVersion should win when both keys are present",
    )
    # Fallback table per platform.
    _assert(_slice_minimum_deployment_target({}, "ios") == "13.0",
            "ios fallback")
    _assert(_slice_minimum_deployment_target({}, "macos") == "11.0",
            "macos fallback")
    _assert(_slice_minimum_deployment_target({}, "maccatalyst") == "13.0",
            "maccatalyst fallback")
    _assert(_slice_minimum_deployment_target({}, "tvos") == "13.0",
            "tvos fallback")
    _assert(_slice_minimum_deployment_target({}, "watchos") == "6.0",
            "watchos fallback")
    _assert(_slice_minimum_deployment_target({}, "visionos") == "1.0",
            "visionos fallback")
    # Unknown platform → generic 13.0 fallback (defensive).
    _assert(_slice_minimum_deployment_target({}, "freebsd") == "13.0",
            "unknown platform falls back to 13.0")
    # Whitespace-only / empty string ignored, falls through to default.
    _assert(
        _slice_minimum_deployment_target(
            {"MinimumOSVersion": "   "}, "ios") == "13.0",
        "whitespace-only MinimumOSVersion should be treated as absent",
    )


def _selftest_verify_happy_path_objc(tmp_root: Path) -> None:
    """ObjC tree with public header + modulemap passes strict verify."""
    base = tmp_root / "verify_happy_objc"
    base.mkdir()
    xc = _build_synthetic_xcframework(base, "Bar", flavor="objc")
    unit = ExecutedUnit(name="Bar", xcframework_path=xc, framework_name="Bar")
    mod = tool
    saved = mod._check_binary_dynamic
    try:
        mod._check_binary_dynamic = lambda _b: True
        results = verify_output([unit], base)
    finally:
        mod._check_binary_dynamic = saved
    r = results[0]
    _assert(r.passed, f"ObjC verify failed: {r.fatal_issues!r}")
    _assert(r.framework_type == "ObjC",
            f"expected ObjC, got {r.framework_type}")


def _selftest_verify_corrupt_info_plist(tmp_root: Path) -> None:
    """Plistlib parse failure (the __MACOSX ghost case) is fatal-per-unit."""
    base = tmp_root / "verify_corrupt"
    base.mkdir()
    xc = _build_synthetic_xcframework(base, "Ghost", flavor="swift",
                                       corrupt_info_plist=True)
    unit = ExecutedUnit(name="Ghost", xcframework_path=xc, framework_name="Ghost")
    mod = tool
    saved = mod._check_binary_dynamic
    try:
        mod._check_binary_dynamic = lambda _b: True
        results = verify_output([unit], base)
    finally:
        mod._check_binary_dynamic = saved
    r = results[0]
    _assert(not r.passed, "corrupt plist should fail verify")
    _assert(any("Info.plist parse failed" in m or "MACOSX" in m
                for m in r.fatal_issues),
            f"expected plist parse error in fatal_issues, got {r.fatal_issues!r}")


def _selftest_verify_missing_xcframework(tmp_root: Path) -> None:
    """Missing xcframework directory is the most basic fatal."""
    base = tmp_root / "verify_missing"
    base.mkdir()
    unit = ExecutedUnit(
        name="Nope",
        xcframework_path=base / "Nope.xcframework",
        framework_name="Nope",
    )
    results = verify_output([unit], base)
    r = results[0]
    _assert(not r.passed, "missing xcframework should fail verify")
    _assert(any("not found" in m for m in r.fatal_issues),
            f"expected 'not found' in fatal_issues, got {r.fatal_issues!r}")


def _selftest_verify_one_slice_only(tmp_root: Path) -> None:
    """After multi-platform support, a single-slice xcframework is a
    legitimate output (pure macOS, pure Mac Catalyst, etc.) and must
    pass Verify. The per-slice dynamic-linkage check below the (former)
    count guard still catches the real failure modes."""
    base = tmp_root / "verify_one_slice"
    base.mkdir()
    xc = _build_synthetic_xcframework(base, "Solo", flavor="swift",
                                       slices=("macos-arm64_x86_64",))
    unit = ExecutedUnit(
        name="Solo", xcframework_path=xc, framework_name="Solo",
        framework_type="Swift",
    )
    mod = tool
    saved = mod._check_binary_dynamic
    try:
        mod._check_binary_dynamic = lambda _b: True
        results = verify_output([unit], base)
    finally:
        mod._check_binary_dynamic = saved
    r = results[0]
    _assert(r.passed,
            f"expected single-slice xcframework to pass verify; fatals={r.fatal_issues!r}")


def _selftest_verify_static_binary(tmp_root: Path) -> None:
    """Static-binary slice fails the dynamically-linked check."""
    base = tmp_root / "verify_static"
    base.mkdir()
    xc = _build_synthetic_xcframework(base, "Static", flavor="swift")
    unit = ExecutedUnit(name="Static", xcframework_path=xc, framework_name="Static")
    mod = tool
    saved = mod._check_binary_dynamic
    try:
        mod._check_binary_dynamic = lambda _b: False
        results = verify_output([unit], base)
    finally:
        mod._check_binary_dynamic = saved
    r = results[0]
    _assert(not r.passed, "static-binary xcframework should fail verify")
    _assert(any("not dynamically linked" in m for m in r.fatal_issues),
            f"expected dynamic-link complaint, got {r.fatal_issues!r}")


def _selftest_verify_swift_no_swiftinterface(tmp_root: Path) -> None:
    """Swift framework with no .swiftinterface fails the ABI-surface check."""
    base = tmp_root / "verify_no_swiftinterface"
    base.mkdir()
    xc = _build_synthetic_xcframework(base, "Empty", flavor="swift")
    # Strip every .swiftinterface to leave a "Swift" tree (.swiftmodule
    # presence still classifies it via detect_framework_type) without an
    # ABI surface.
    for p in xc.rglob("*.swiftinterface"):
        p.unlink()
    unit = ExecutedUnit(name="Empty", xcframework_path=xc, framework_name="Empty")
    mod = tool
    saved = mod._check_binary_dynamic
    try:
        mod._check_binary_dynamic = lambda _b: True
        results = verify_output([unit], base)
    finally:
        mod._check_binary_dynamic = saved
    r = results[0]
    _assert(not r.passed, "Swift framework with no .swiftinterface should fail")
    _assert(any("swiftinterface" in m for m in r.fatal_issues),
            f"expected swiftinterface complaint, got {r.fatal_issues!r}")


def _selftest_verify_objc_no_modulemap(tmp_root: Path) -> None:
    """ObjC framework with public headers but no modulemap fails strict verify.

    The synthetic ObjC tree starts with both Headers/ and module.modulemap;
    we delete the modulemap and confirm Verify lands on the
    "modulemap-missing" fatal branch while still classifying the tree as
    ObjC (because public headers under Headers/ are present).
    """
    base = tmp_root / "verify_objc_no_modulemap"
    base.mkdir()
    xc = _build_synthetic_xcframework(base, "Headless", flavor="objc")
    for p in xc.rglob("module.modulemap"):
        p.unlink()
    unit = ExecutedUnit(
        name="Headless",
        xcframework_path=xc,
        framework_name="Headless",
    )
    mod = tool
    saved = mod._check_binary_dynamic
    try:
        mod._check_binary_dynamic = lambda _b: True
        results = verify_output([unit], base)
    finally:
        mod._check_binary_dynamic = saved
    r = results[0]
    _assert(r.framework_type == "ObjC",
            f"expected ObjC, got {r.framework_type}")
    _assert(not r.passed, "ObjC framework with no modulemap should fail")
    _assert(any("modulemap" in m for m in r.fatal_issues),
            f"expected modulemap complaint, got {r.fatal_issues!r}")


def _selftest_verify_summary_format(tmp_root: Path) -> None:
    """`print_verify_summary` produces the §5.5 layout (counts, table, output)."""
    import io
    import contextlib

    base = tmp_root / "verify_summary"
    base.mkdir()
    good = _build_synthetic_xcframework(base, "Foo", flavor="swift")
    bad = _build_synthetic_xcframework(base, "Bar", flavor="swift")
    units = [
        ExecutedUnit(name="Foo", xcframework_path=good, framework_name="Foo"),
        ExecutedUnit(name="Bar", xcframework_path=bad, framework_name="Bar"),
    ]
    mod = tool
    saved = mod._check_binary_dynamic
    try:
        # First call: both pass.
        mod._check_binary_dynamic = lambda _b: True
        results = verify_output(units, base)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            print_verify_summary(results, base)
        out = buf.getvalue()
        _assert("=== Summary ===" in out, "summary header missing")
        _assert("Built: 2" in out, "Built count missing")
        _assert("Verified: 2" in out, "Verified count missing")
        _assert("Failed: 0" in out, "Failed count missing")
        _assert("Foo.xcframework" in out, "Foo row missing")
        _assert("Bar.xcframework" in out, "Bar row missing")
        _assert("[Swift]" in out, "framework type label missing")
        _assert(f"Output: {base}" in out, "output line missing")

        # Second call: one fails (static).
        def _fake_check(b: Path) -> bool:
            return "Bar" not in str(b)
        mod._check_binary_dynamic = _fake_check
        results = verify_output(units, base)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            print_verify_summary(results, base)
        out = buf.getvalue()
        _assert("Built: 2" in out, "Built count missing on partial fail")
        _assert("Verified: 1" in out, "Verified count wrong on partial fail")
        _assert("Failed: 1" in out, "Failed count wrong on partial fail")
        _assert("Failed units:" in out, "Failed units section missing")
        _assert("Bar.xcframework" in out, "Bar row should appear in failed list")
    finally:
        mod._check_binary_dynamic = saved


def _selftest_verify_binary_path_fallback(tmp_root: Path) -> None:
    """When `Info.plist` omits `BinaryPath`, the fallback reconstructs the
    correct binary location from `LibraryPath` (no nested `Frameworks/`
    duplication). Confirms the basename fix for the OpenAI-flagged bug.
    """
    base = tmp_root / "verify_binarypath_fallback"
    base.mkdir()
    xc = _build_synthetic_xcframework(
        base, "Foo", flavor="swift", omit_binary_path_in_plist=True
    )
    unit = ExecutedUnit(name="Foo", xcframework_path=xc, framework_name="Foo")
    seen_binaries: List[Path] = []

    def _capture(b: Path) -> bool:
        seen_binaries.append(b)
        return True

    mod = tool
    saved = mod._check_binary_dynamic
    try:
        mod._check_binary_dynamic = _capture
        results = verify_output([unit], base)
    finally:
        mod._check_binary_dynamic = saved
    r = results[0]
    _assert(r.passed, f"BinaryPath fallback should pass, got {r.fatal_issues!r}")
    _assert(len(seen_binaries) == 2,
            f"expected 2 slice binaries probed, got {len(seen_binaries)}: "
            f"{seen_binaries!r}")
    for binary in seen_binaries:
        # The reconstructed path must NOT have a nested Foo.framework/Foo
        # parent — that would mean the basename fix regressed.
        rel = binary.relative_to(xc)
        parts = rel.parts
        _assert(parts[1] == "Foo.framework" and parts[-1] == "Foo",
                f"unexpected reconstructed binary path: {rel}")
        _assert(parts.count("Foo.framework") == 1,
                f"binary path duplicated Foo.framework: {rel}")


def _selftest_verify_malformed_available_libraries(tmp_root: Path) -> None:
    """Various `AvailableLibraries` shapes that should each fail verify
    with a clear fatal-per-unit message rather than crashing the parser.
    """
    base = tmp_root / "verify_malformed_avail"
    base.mkdir()

    # Case 1: AvailableLibraries is not a list at all.
    bad1 = base / "Bad1.xcframework"
    bad1.mkdir()
    with (bad1 / "Info.plist").open("wb") as fh:
        plistlib.dump({"AvailableLibraries": "oops"}, fh)
    unit1 = ExecutedUnit(name="Bad1", xcframework_path=bad1, framework_name="Bad1")

    # Case 2: AvailableLibraries entries are not dicts.
    bad2 = base / "Bad2.xcframework"
    bad2.mkdir()
    with (bad2 / "Info.plist").open("wb") as fh:
        plistlib.dump({"AvailableLibraries": ["foo", "bar"]}, fh)
    unit2 = ExecutedUnit(name="Bad2", xcframework_path=bad2, framework_name="Bad2")

    # Case 3: AvailableLibraries entry is missing LibraryPath.
    bad3 = base / "Bad3.xcframework"
    bad3.mkdir()
    with (bad3 / "Info.plist").open("wb") as fh:
        plistlib.dump({
            "AvailableLibraries": [
                {"LibraryIdentifier": "ios-arm64"},
                {"LibraryIdentifier": "ios-arm64_x86_64-simulator"},
            ],
        }, fh)
    unit3 = ExecutedUnit(name="Bad3", xcframework_path=bad3, framework_name="Bad3")

    mod = tool
    saved = mod._check_binary_dynamic
    try:
        mod._check_binary_dynamic = lambda _b: True
        results = verify_output([unit1, unit2, unit3], base)
    finally:
        mod._check_binary_dynamic = saved

    r1, r2, r3 = results
    _assert(not r1.passed, "non-list AvailableLibraries should fail verify")
    _assert(any("AvailableLibraries" in m for m in r1.fatal_issues),
            f"Bad1 fatal_issues: {r1.fatal_issues!r}")
    _assert(not r2.passed, "non-dict entries should fail verify")
    _assert(any("not a dict" in m for m in r2.fatal_issues),
            f"Bad2 fatal_issues: {r2.fatal_issues!r}")
    _assert(not r3.passed, "missing LibraryPath should fail verify")
    _assert(any("missing LibraryPath" in m for m in r3.fatal_issues),
            f"Bad3 fatal_issues: {r3.fatal_issues!r}")


def _selftest_verify_missing_output_dir(tmp_root: Path) -> None:
    """Verify raises VerifyUserError when output_dir itself is missing —
    that's the canonical `-o /nope` user mistake, which must land on the
    clean-error path, not a traceback."""
    bogus = tmp_root / "does_not_exist_at_all"
    try:
        verify_output([], bogus)
    except VerifyUserError as exc:
        _assert(str(bogus) in str(exc),
                f"error should mention the missing path: {exc}")
        _assert(
            isinstance(exc, _USER_FACING_ERRORS),
            "VerifyUserError must be in _USER_FACING_ERRORS for clean exit",
        )
        return
    raise AssertionError("missing output_dir should raise VerifyUserError")


def _selftest_cli_resolves_relative_output_dir() -> None:
    """Smoke-regression: a relative `--output` must be resolved to an
    absolute path at config construction. Two failure modes this guards
    against, both surfaced by the first end-to-end `stripe-ios --target
    StripeCore --product Stripe` smoke run:
      - Without absolute resolution, dedup-overlap writes the same
        relative string into the staged Package.swift, where SPM
        resolves it against the staged manifest's directory (a tempdir,
        not the user's CWD) → "binary target does not contain a binary
        artifact".
      - The subsequent edit-site fix (`os.path.relpath` against
        `staged_dir`) depends on the source being absolute. A relative
        source would yield an unstable rel-path that's relative to
        whatever CWD `os.path.relpath` happens to see.
    """
    import argparse as _ap
    parse_args = tool.parse_args
    _config_from_args = tool._config_from_args

    # The default — Path("./xcframeworks").
    ns, _ = parse_args(["pkg-source"])
    cfg = _config_from_args(ns)
    _assert(cfg.output_dir.is_absolute(),
            f"default output_dir must be absolute; got {cfg.output_dir!r}")

    # User-supplied relative path.
    ns2, _ = parse_args(["pkg-source", "-o", "./foo/bar"])
    cfg2 = _config_from_args(ns2)
    _assert(cfg2.output_dir.is_absolute(),
            f"-o ./foo/bar must resolve to absolute; got {cfg2.output_dir!r}")
    _assert(cfg2.output_dir.name == "bar",
            f"resolved path must preserve the leaf; got {cfg2.output_dir!r}")

    # Already-absolute path passes through unchanged-shape.
    ns3, _ = parse_args(["pkg-source", "-o", "/tmp/some/place"])
    cfg3 = _config_from_args(ns3)
    _assert(cfg3.output_dir.is_absolute(),
            f"absolute -o input must stay absolute; got {cfg3.output_dir!r}")


def _selftest_verify_mixed_losing_objc_surface_fails(tmp_root: Path) -> None:
    """Regression for the Codex [P1] silent-pass hole.

    Simulates the failure mode where a Mixed-language unit's ObjC header
    injection failed at Execute time, leaving a Swift-only artifact on
    disk. The plan said Mixed (we scanned the target sources and found
    both .swift and .m), but post-hoc detection of the partial build
    would classify it as Swift and skip the ObjC-surface checks. Verify
    must still fail because the plan's expected language is Mixed.
    """
    base = tmp_root / "verify_mixed_lost_objc"
    base.mkdir()
    # Build a flavor="swift" tree — this is deliberately what would
    # survive if ObjC header injection failed on a Mixed target.
    xc = _build_synthetic_xcframework(base, "HalfMixed", flavor="swift")
    unit = ExecutedUnit(
        name="HalfMixed",
        xcframework_path=xc,
        framework_name="HalfMixed",
        # Plan-time expectation: this is a Mixed target. Verify must
        # require the ObjC surface even though detection says Swift.
        expected_language=Language.MIXED,
    )
    mod = tool
    saved = mod._check_binary_dynamic
    try:
        mod._check_binary_dynamic = lambda _b: True
        results = verify_output([unit], base)
    finally:
        mod._check_binary_dynamic = saved
    r = results[0]
    _assert(not r.passed,
            "Mixed-expected unit with no ObjC surface must fail verify "
            f"(got passed=True, warnings={r.warnings!r})")
    _assert(
        any("plan expected Mixed" in m and "Headers/" in m for m in r.fatal_issues),
        f"expected a 'plan expected Mixed … Headers/' fatal, got {r.fatal_issues!r}",
    )
    _assert(
        any("plan expected Mixed" in m and "modulemap" in m for m in r.fatal_issues),
        f"expected a 'plan expected Mixed … modulemap' fatal, got {r.fatal_issues!r}",
    )
    # The drift advisory should also surface: detection disagrees with plan.
    _assert(
        any("on-disk detection says Swift" in m for m in r.warnings),
        f"expected drift advisory in warnings, got {r.warnings!r}",
    )


def _selftest_verify_mixed_losing_swift_surface_fails(tmp_root: Path) -> None:
    """Mirror of the above for the reverse drift: plan says Mixed but
    only the ObjC surface landed. Makes sure the swiftinterface check
    still fires when detection would have said 'ObjC'."""
    base = tmp_root / "verify_mixed_lost_swift"
    base.mkdir()
    xc = _build_synthetic_xcframework(base, "HalfMixed2", flavor="objc")
    unit = ExecutedUnit(
        name="HalfMixed2",
        xcframework_path=xc,
        framework_name="HalfMixed2",
        expected_language=Language.MIXED,
    )
    mod = tool
    saved = mod._check_binary_dynamic
    try:
        mod._check_binary_dynamic = lambda _b: True
        results = verify_output([unit], base)
    finally:
        mod._check_binary_dynamic = saved
    r = results[0]
    _assert(not r.passed,
            "Mixed-expected unit with no Swift ABI surface must fail verify")
    _assert(
        any("plan expected Mixed" in m and "swiftinterface" in m
            for m in r.fatal_issues),
        f"expected 'plan expected Mixed … swiftinterface' fatal, got {r.fatal_issues!r}",
    )


def _selftest_finalize_threads_expected_lang_into_deps(tmp_root: Path) -> None:
    """Regression for Codex follow-up: dependency xcframeworks passed
    through `_finalize_with_verify` must be wrapped into ExecutedUnits
    that carry the same plan-time expected_language signal as the
    primary units. Otherwise a Mixed dep that lost its ObjC surface
    during injection would silently pass verify even though the
    primary-unit fix already closes that hole for top-level units.

    Builds a flavor="swift" tree (the broken shape: ObjC injection
    failed, so the on-disk artifact only has the Swift surface), wraps
    it as a dependency with expected_language=Mixed, and asserts that
    finalize-with-verify reports the unit as FAILED on the
    "plan expected Mixed but no public headers" fatal.
    """
    import io
    import contextlib

    base = tmp_root / "finalize_dep_mixed"
    base.mkdir()

    # Primary unit: well-formed, passes on its own.
    primary_xc = _build_synthetic_xcframework(base, "Primary", flavor="swift")
    # Dependency xcframework: broken — swift-only on disk but the
    # target it was built from is Mixed according to the package model.
    dep_xc = _build_synthetic_xcframework(base, "DepMixed", flavor="swift")

    primary_unit = ExecutedUnit(
        name="Primary",
        xcframework_path=primary_xc,
        framework_name="Primary",
        expected_language=Language.SWIFT,
        dependency_xcframeworks=[
            DependencyXcframework(path=dep_xc, expected_language=Language.MIXED),
        ],
    )

    mod = tool
    saved_check = mod._check_binary_dynamic
    try:
        mod._check_binary_dynamic = lambda _b: True
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = _finalize_with_verify(
                [primary_unit],
                Config(package_source="", user_version="",
                       output_dir=base),
            )
    finally:
        mod._check_binary_dynamic = saved_check

    _assert(
        rc == VerifyError.exit_code,
        f"_finalize_with_verify should have returned {VerifyError.exit_code} "
        f"for a Mixed-expected dep missing its ObjC surface; got {rc}",
    )
    out = buf.getvalue()
    _assert(
        "DepMixed" in out and "plan expected Mixed" in out,
        f"summary should mention the failed dep and the mixed-expectation "
        f"fatal; got:\n{out}",
    )


def _selftest_finalize_dep_dedup_upgrades_expected_lang(tmp_root: Path) -> None:
    """When the same dep xcframework is pulled in by multiple parent
    units with conflicting `expected_language`, finalize must keep the
    more specific signal — otherwise a single N/A entry could downgrade
    a Mixed expectation and reopen the silent-pass hole."""
    import io
    import contextlib

    base = tmp_root / "finalize_dep_dedup"
    base.mkdir()
    shared_dep = _build_synthetic_xcframework(base, "Shared", flavor="swift")

    # Two parent units share the same dep. The first parent saw it as
    # N/A (no classification), the second as Mixed. Finalize must
    # upgrade to Mixed so the verify fails as expected.
    parent_a = ExecutedUnit(
        name="A",
        xcframework_path=_build_synthetic_xcframework(base, "A", flavor="swift"),
        framework_name="A",
        expected_language=Language.SWIFT,
        dependency_xcframeworks=[
            DependencyXcframework(path=shared_dep, expected_language=Language.NA),
        ],
    )
    parent_b = ExecutedUnit(
        name="B",
        xcframework_path=_build_synthetic_xcframework(base, "B", flavor="swift"),
        framework_name="B",
        expected_language=Language.SWIFT,
        dependency_xcframeworks=[
            DependencyXcframework(path=shared_dep, expected_language=Language.MIXED),
        ],
    )

    mod = tool
    saved_check = mod._check_binary_dynamic
    try:
        mod._check_binary_dynamic = lambda _b: True
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = _finalize_with_verify(
                [parent_a, parent_b],
                Config(package_source="", user_version="", output_dir=base),
            )
    finally:
        mod._check_binary_dynamic = saved_check

    _assert(
        rc == VerifyError.exit_code,
        "the more-specific Mixed expectation must win during dedupe — "
        f"got rc={rc} (A+B parents passed, so the dep's Mixed-fail "
        "is the only thing that could have failed verify)",
    )
    out = buf.getvalue()
    # The shared dep must appear exactly once in the output — dedupe
    # still works; we're not double-verifying.
    _assert(
        out.count("Shared.xcframework") == 1,
        f"Shared.xcframework should appear exactly once in the summary "
        f"(dedupe preserved); got {out.count('Shared.xcframework')}: \n{out}",
    )


def _selftest_verify_expected_language_na_falls_back(tmp_root: Path) -> None:
    """Binary-mode and legacy callers leave expected_language at "" or
    N/A; Verify must fall back to post-hoc detection in that case and
    keep passing well-formed artifacts."""
    base = tmp_root / "verify_expected_na"
    base.mkdir()
    xc = _build_synthetic_xcframework(base, "BinCopy", flavor="swift")
    unit = ExecutedUnit(
        name="BinCopy",
        xcframework_path=xc,
        framework_name="BinCopy",
        expected_language=Language.NA,
        is_binary_copy=True,
    )
    mod = tool
    saved = mod._check_binary_dynamic
    try:
        mod._check_binary_dynamic = lambda _b: True
        results = verify_output([unit], base)
    finally:
        mod._check_binary_dynamic = saved
    r = results[0]
    _assert(r.passed,
            f"binary-copy Swift xcframework should pass, got {r.fatal_issues!r}")
    _assert(r.framework_type == "Swift",
            f"expected detected Swift, got {r.framework_type!r}")


# --- Output manifest / cleanup self-tests --------------------------------
#
# Regression tests for REFACTOR_PLAN.md Task 3. All synthetic — no
# xcodebuild, no network, no swift. Exercise the manifest reader/writer,
# the cleanup flow, and the `--no-cleanup-stale` "delay by one run"
# semantics. Every test builds a complete `_finalize_with_verify` call
# against a fake output directory so the Execute-start → Verify →
# cleanup → manifest-write sequence is covered end to end.


def _mk_finalized_executed_unit(
    base: Path,
    name: str,
    *,
    flavor: str = "swift",
) -> "ExecutedUnit":
    """Build a synthetic xcframework under `base` and wrap it as an
    ExecutedUnit ready to be passed into `_finalize_with_verify`."""
    xc = _build_synthetic_xcframework(base, name, flavor=flavor)
    return ExecutedUnit(
        name=name,
        xcframework_path=xc,
        framework_name=name,
        framework_type="Swift",
        expected_language=Language.SWIFT,
    )


def _write_manifest_file(
    output_dir: Path,
    entries: Sequence[Tuple[str, str]],
) -> None:
    """Low-level manifest write helper — writes the JSON file directly
    without going through `_write_output_manifest` so tests can also
    construct malformed manifests."""
    payload = {
        "version": tool._MANIFEST_VERSION,
        "tool": "spm-to-xcframework",
        "produced_at": "2026-04-06T00:00:00+00:00",
        "package_source": "test-fixture",
        "package_version": "",
        "entries": [{"name": n, "kind": k} for (n, k) in entries],
    }
    (output_dir / tool._MANIFEST_FILENAME).write_text(
        json.dumps(payload, indent=2) + "\n"
    )


def _run_finalize(
    units: Sequence["ExecutedUnit"],
    output_dir: Path,
    *,
    old_manifest: Optional["OutputManifest"] = None,
    no_cleanup_stale: bool = False,
) -> Tuple[int, str]:
    """Invoke `_finalize_with_verify` with the binary-dynamic check
    monkey-patched to True (so the synthetic xcframeworks pass), capturing
    stdout. Returns (exit_code, captured_stdout)."""
    buf = io.StringIO()
    saved = tool._check_binary_dynamic
    try:
        tool._check_binary_dynamic = lambda _b: True
        with contextlib.redirect_stdout(buf):
            rc = _finalize_with_verify(
                list(units),
                Config(
                    package_source="test",
                    user_version="",
                    output_dir=output_dir,
                    no_cleanup_stale=no_cleanup_stale,
                ),
                old_manifest=old_manifest,
            )
    finally:
        tool._check_binary_dynamic = saved
    return rc, buf.getvalue()


def _selftest_manifest_cleans_stale_primary(tmp_root: Path) -> None:
    """Happy path: prior manifest lists Old.xcframework; current run
    produces only New.xcframework; after verify passes, Old is cleaned
    and the new manifest contains only New."""
    out = tmp_root / "manifest_stale_primary"
    out.mkdir()
    # Fake the old xcframework on disk (doesn't need to be a valid
    # xcframework — the cleanup path just rmtrees by basename).
    (out / "Old.xcframework").mkdir()
    (out / "Old.xcframework" / "placeholder").write_text("old")
    _write_manifest_file(out, [("Old.xcframework", "primary")])

    # Current run: one new valid xcframework.
    new_unit = _mk_finalized_executed_unit(out, "New")
    old = _read_output_manifest(out)
    rc, _ = _run_finalize([new_unit], out, old_manifest=old)
    _assert(rc == 0, f"finalize should succeed, got rc={rc}")
    _assert(not (out / "Old.xcframework").exists(),
            "Old.xcframework should have been cleaned")
    _assert((out / "New.xcframework").exists(),
            "New.xcframework should still be present")
    # New manifest written.
    fresh = _read_output_manifest(out)
    names = {e.name for e in fresh.entries}
    _assert(names == {"New.xcframework"},
            f"new manifest should list only New, got {names}")
    kinds = {e.name: e.kind for e in fresh.entries}
    _assert(kinds["New.xcframework"] == tool._MANIFEST_KIND_PRIMARY,
            f"New should be recorded as primary, got {kinds!r}")


def _selftest_manifest_cleans_stale_dep_across_include_deps_boundary(tmp_root: Path) -> None:
    """Codex P1 lock-in: a prior `--include-deps` run left a
    `Dep.xcframework` dependency entry in the manifest. A follow-up
    plain (no `--include-deps`) run must still clean Dep.xcframework
    — cross-run cleanup is NOT gated on the current run's flags."""
    out = tmp_root / "manifest_stale_dep_boundary"
    out.mkdir()
    (out / "Dep.xcframework").mkdir()
    (out / "Dep.xcframework" / "placeholder").write_text("dep")
    _write_manifest_file(out, [
        ("Primary.xcframework", "primary"),
        ("Dep.xcframework", "dependency"),
    ])
    # Note: Primary is also on-disk from the prior run and gets
    # re-produced by the current run below, so it should survive.
    (out / "Primary.xcframework").mkdir(exist_ok=True)

    primary_unit = _mk_finalized_executed_unit(out, "Primary")
    old = _read_output_manifest(out)
    rc, _ = _run_finalize([primary_unit], out, old_manifest=old)
    _assert(rc == 0, f"finalize should succeed, got rc={rc}")
    _assert(not (out / "Dep.xcframework").exists(),
            "Dep.xcframework should have been cleaned "
            "(cross-run cleanup must not gate on --include-deps)")
    _assert((out / "Primary.xcframework").exists(),
            "Primary.xcframework should still be present")
    fresh = _read_output_manifest(out)
    names = {e.name for e in fresh.entries}
    _assert(names == {"Primary.xcframework"},
            f"manifest should list only Primary, got {names}")
    # No dependency entries in the new manifest.
    deps = [e for e in fresh.entries if e.kind == tool._MANIFEST_KIND_DEPENDENCY]
    _assert(not deps, f"new manifest should have no dep entries, got {deps!r}")


def _selftest_manifest_cleanup_deferred_on_verify_failure(tmp_root: Path) -> None:
    """Codex P1-v2 lock-in: a run whose Verify fails must leave the
    prior manifest and the prior on-disk artifacts untouched. The
    failing run does NOT degrade the user's last known-good state."""
    out = tmp_root / "manifest_verify_failure"
    out.mkdir()
    (out / "Old.xcframework").mkdir()
    (out / "Old.xcframework" / "placeholder").write_text("old")
    _write_manifest_file(out, [("Old.xcframework", "primary")])
    manifest_path = out / tool._MANIFEST_FILENAME
    manifest_bytes_before = manifest_path.read_bytes()

    # Build a synthetic xcframework that will FAIL verify — strip the
    # .swiftinterface so the swiftinterface fatal fires on a "Swift"
    # tree.
    bad_xc = _build_synthetic_xcframework(out, "New", flavor="swift")
    for p in bad_xc.rglob("*.swiftinterface"):
        p.unlink()
    new_unit = ExecutedUnit(
        name="New",
        xcframework_path=bad_xc,
        framework_name="New",
        expected_language=Language.SWIFT,
    )
    old = _read_output_manifest(out)
    rc, _ = _run_finalize([new_unit], out, old_manifest=old)
    _assert(
        rc == VerifyError.exit_code,
        f"finalize should return VerifyError.exit_code, got {rc}",
    )
    # Old artifact still on disk.
    _assert((out / "Old.xcframework").exists(),
            "Old.xcframework must NOT be cleaned when verify fails")
    # Manifest file still exactly the old one.
    _assert(
        manifest_path.read_bytes() == manifest_bytes_before,
        "manifest must NOT be overwritten when verify fails",
    )


def _selftest_manifest_user_files_untouched(tmp_root: Path) -> None:
    """A user-created file / xcframework that isn't in the manifest
    must survive finalize untouched."""
    out = tmp_root / "manifest_user_files"
    out.mkdir()
    # User's own stuff:
    (out / "UserFile.txt").write_text("hands off")
    (out / "UserOwned.xcframework").mkdir()
    (out / "UserOwned.xcframework" / "contents").write_text("mine")
    # No prior manifest.

    new_unit = _mk_finalized_executed_unit(out, "Generated")
    old = _read_output_manifest(out)
    rc, _ = _run_finalize([new_unit], out, old_manifest=old)
    _assert(rc == 0, f"finalize should succeed, got rc={rc}")
    _assert((out / "UserFile.txt").exists(),
            "UserFile.txt should be untouched")
    _assert((out / "UserOwned.xcframework").exists(),
            "UserOwned.xcframework should be untouched")
    _assert((out / "UserOwned.xcframework" / "contents").read_text() == "mine",
            "UserOwned content must be intact")
    fresh = _read_output_manifest(out)
    names = {e.name for e in fresh.entries}
    _assert(names == {"Generated.xcframework"},
            f"new manifest should list only Generated, got {names}")


def _selftest_manifest_empty_directory(tmp_root: Path) -> None:
    """First run into an empty directory: no cleanup (nothing to
    clean), manifest is written on success."""
    out = tmp_root / "manifest_empty_dir"
    out.mkdir()
    unit = _mk_finalized_executed_unit(out, "Fresh")
    old = _read_output_manifest(out)
    _assert(not old.entries, "empty dir should yield empty manifest")
    rc, _ = _run_finalize([unit], out, old_manifest=old)
    _assert(rc == 0, f"finalize should succeed, got rc={rc}")
    fresh = _read_output_manifest(out)
    names = {e.name for e in fresh.entries}
    _assert(names == {"Fresh.xcframework"},
            f"new manifest should list Fresh, got {names}")


def _selftest_manifest_missing_manifest_no_cleanup(tmp_root: Path) -> None:
    """Manifest was manually deleted but prior xcframeworks still
    exist on disk. The tool has no provenance for them and must NOT
    touch them."""
    out = tmp_root / "manifest_missing"
    out.mkdir()
    # Prior xcframeworks with no manifest → tool can't prove
    # ownership, must leave them alone.
    (out / "Orphan.xcframework").mkdir()
    (out / "Orphan.xcframework" / "placeholder").write_text("orphaned")

    new_unit = _mk_finalized_executed_unit(out, "NewOne")
    old = _read_output_manifest(out)
    rc, _ = _run_finalize([new_unit], out, old_manifest=old)
    _assert(rc == 0, f"finalize should succeed, got rc={rc}")
    _assert((out / "Orphan.xcframework").exists(),
            "Orphan.xcframework should NOT be cleaned (no provenance)")
    fresh = _read_output_manifest(out)
    names = {e.name for e in fresh.entries}
    _assert(names == {"NewOne.xcframework"},
            f"new manifest should list only NewOne, got {names}")


def _selftest_manifest_malformed_manifest(tmp_root: Path) -> None:
    """Write a manifest with invalid JSON. Read returns empty; nothing
    is cleaned; after a successful run the new manifest replaces the
    malformed one on success."""
    out = tmp_root / "manifest_malformed"
    out.mkdir()
    (out / tool._MANIFEST_FILENAME).write_text("{not valid json :::")
    (out / "PrevBuild.xcframework").mkdir()
    (out / "PrevBuild.xcframework" / "placeholder").write_text("prev")

    old = _read_output_manifest(out)
    _assert(not old.entries,
            "malformed manifest should flatten to empty OutputManifest")

    unit = _mk_finalized_executed_unit(out, "NewBuild")
    rc, _ = _run_finalize([unit], out, old_manifest=old)
    _assert(rc == 0, f"finalize should succeed, got rc={rc}")
    _assert((out / "PrevBuild.xcframework").exists(),
            "PrevBuild (no provenance) should be untouched")
    fresh = _read_output_manifest(out)
    names = {e.name for e in fresh.entries}
    _assert(names == {"NewBuild.xcframework"},
            f"new manifest should list only NewBuild, got {names}")
    # The malformed file has been replaced with a valid one.
    manifest_raw = (out / tool._MANIFEST_FILENAME).read_text()
    _assert("{not valid json :::" not in manifest_raw,
            "malformed manifest should have been replaced")


def _selftest_manifest_corrupt_entry_filtering(tmp_root: Path) -> None:
    """Manifest whose entry list mixes one valid entry with several
    suspect ones (path separator, `..`, absolute path, unknown kind).
    Reader filters the corrupt entries. Cleanup only touches the
    valid entry if it's stale. The basenames-only schema enforcement
    is load-bearing: a tampered manifest must NEVER be able to
    coerce cleanup into touching paths outside `<output_dir>`."""
    out = tmp_root / "manifest_corrupt_entries"
    out.mkdir()
    # Place the one valid legacy xcframework that should actually be
    # cleaned as stale.
    (out / "Stale.xcframework").mkdir()
    (out / "Stale.xcframework" / "placeholder").write_text("stale")
    # Also a sibling directory that corrupt entries try to target —
    # it's outside the normal xcframework shape and must never be
    # touched even though the manifest names it.
    outside = tmp_root / "OutsideTarget.xcframework"
    outside.mkdir()
    (outside / "sentinel").write_text("must-survive")

    payload = {
        "version": tool._MANIFEST_VERSION,
        "tool": "spm-to-xcframework",
        "produced_at": "2026-04-06T00:00:00+00:00",
        "package_source": "test",
        "package_version": "",
        "entries": [
            # Valid — will be seen as stale and cleaned.
            {"name": "Stale.xcframework", "kind": "primary"},
            # Path separator — rejected.
            {"name": "sub/Evil.xcframework", "kind": "primary"},
            # `..` traversal — rejected.
            {"name": "../OutsideTarget.xcframework", "kind": "primary"},
            # Absolute path — rejected.
            {"name": str(outside), "kind": "primary"},
            # Unknown kind — rejected.
            {"name": "Weird.xcframework", "kind": "mystery"},
            # Leading dot — rejected.
            {"name": ".Hidden.xcframework", "kind": "primary"},
        ],
    }
    (out / tool._MANIFEST_FILENAME).write_text(json.dumps(payload, indent=2))

    old = _read_output_manifest(out)
    # Only the one valid entry should survive the reader's filter.
    _assert(len(old.entries) == 1,
            f"reader should keep exactly 1 valid entry, got {len(old.entries)}")
    _assert(old.entries[0].name == "Stale.xcframework",
            f"only valid entry should be Stale, got {old.entries[0].name!r}")

    new_unit = _mk_finalized_executed_unit(out, "Fresh")
    rc, _ = _run_finalize([new_unit], out, old_manifest=old)
    _assert(rc == 0, f"finalize should succeed, got rc={rc}")
    # Stale (the one valid old entry) gets cleaned.
    _assert(not (out / "Stale.xcframework").exists(),
            "Stale.xcframework should have been cleaned")
    # The outside-directory target must still be intact — the corrupt
    # entry naming it was rejected at read time.
    _assert(outside.exists(),
            "OutsideTarget.xcframework (outside output_dir) must be untouched")
    _assert((outside / "sentinel").read_text() == "must-survive",
            "OutsideTarget contents must be intact")


def _selftest_manifest_no_cleanup_stale_preserves_and_tracks(tmp_root: Path) -> None:
    """`--no-cleanup-stale` preserves the old files AND merges them
    into the new manifest, so a subsequent normal run cleans them.
    Locks in the "delay cleanup by one run" semantics (REFACTOR_PLAN
    Task 3) against a future regression to "drop from manifest"."""
    out = tmp_root / "manifest_no_cleanup"
    out.mkdir()
    (out / "Old.xcframework").mkdir()
    (out / "Old.xcframework" / "placeholder").write_text("old")
    _write_manifest_file(out, [("Old.xcframework", "primary")])

    # Run 1: --no-cleanup-stale, producing New. Old must stay.
    new_unit = _mk_finalized_executed_unit(out, "New")
    old = _read_output_manifest(out)
    rc, _ = _run_finalize([new_unit], out, old_manifest=old, no_cleanup_stale=True)
    _assert(rc == 0, f"run1 finalize should succeed, got rc={rc}")
    _assert((out / "Old.xcframework").exists(),
            "Old.xcframework should be preserved by --no-cleanup-stale")
    _assert((out / "New.xcframework").exists(),
            "New.xcframework should be present")
    fresh = _read_output_manifest(out)
    names = {e.name for e in fresh.entries}
    _assert(
        names == {"Old.xcframework", "New.xcframework"},
        f"--no-cleanup-stale must merge old entries into new manifest "
        f"(sliding-window semantics), got {names}",
    )

    # Run 2: no flag, producing just New again. Old should now be
    # cleaned because it's tracked in the manifest.
    new_unit2 = _mk_finalized_executed_unit(out, "New")
    old2 = _read_output_manifest(out)
    rc2, _ = _run_finalize([new_unit2], out, old_manifest=old2)
    _assert(rc2 == 0, f"run2 finalize should succeed, got rc={rc2}")
    _assert(not (out / "Old.xcframework").exists(),
            "run2 without flag should clean Old "
            "(tracking merge in run1 made this possible)")
    fresh2 = _read_output_manifest(out)
    names2 = {e.name for e in fresh2.entries}
    _assert(names2 == {"New.xcframework"},
            f"run2 new manifest should list only New, got {names2}")


def _selftest_manifest_entry_basename_guard() -> None:
    """Unit test for `_manifest_entry_basename_ok`. Locks down the
    exact set of rejected shapes so the security-critical guard can't
    quietly regress."""
    ok_cases = [
        "Foo.xcframework",
        "Alamofire.xcframework",
        "My-Library.xcframework",
        "Name_With_Underscores.xcframework",
    ]
    bad_cases = [
        "",
        "/abs/Foo.xcframework",
        "../Foo.xcframework",
        "sub/Foo.xcframework",
        "sub\\Foo.xcframework",
        ".HiddenFoo.xcframework",
        "..",
        "a/../b.xcframework",
    ]
    for name in ok_cases:
        _assert(
            tool._manifest_entry_basename_ok(name),
            f"{name!r} should be accepted",
        )
    for name in bad_cases:
        _assert(
            not tool._manifest_entry_basename_ok(name),
            f"{name!r} should be rejected",
        )


# --- Round-trip self-tests ------------------------------------------------
#
# These all require a real `swift` toolchain on PATH and write fixtures
# to a temp dir before invoking `swift package dump-package`. They're the
# core pre-merge gate for Prepare per REWRITE_DESIGN.md §9.


def _roundtrip_apply_and_dump(
    fixture_text: str,
    edits: List[PackageSwiftEdit],
    *,
    plan: Optional[Plan] = None,
) -> Tuple[Path, Plan, dict]:
    """Helper: write the fixture, apply the planner-style edits via
    apply_package_swift_edits, then run `swift package dump-package` and
    return (staged_dir, plan, dumped_json).

    The caller is responsible for cleaning up `staged_dir` (test harness
    runs everything inside a top-level temp dir).
    """
    tmp = Path(tempfile.mkdtemp(prefix="spm2xc-prep-"))
    (tmp / "Package.swift").write_text(fixture_text)
    if plan is None:
        plan = Plan()
        plan.package_swift_edits = list(edits)
    else:
        plan.package_swift_edits = list(edits)
    apply_package_swift_edits(tmp, plan)
    cp = subprocess.run(
        ["swift", "package", "dump-package"],
        cwd=str(tmp),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if cp.returncode != 0:
        raise AssertionError(
            f"swift package dump-package failed after edits:\n"
            f"  {cp.stderr.strip()}\n  Edited Package.swift:\n"
            f"{(tmp / 'Package.swift').read_text()}"
        )
    return tmp, plan, json.loads(cp.stdout)


def _roundtrip_grdb() -> None:
    """GRDB: synth_dynamic_library spawns a parallel `GRDBDynamic` product
    via `swift package add-product`. The ORIGINAL GRDB product is left
    automatic in the manifest (the rename pass relabels the bundle
    after archive — never at the manifest level). The synthetic product
    is dynamic. GRDB-dynamic + GRDBSQLite are untouched."""
    edits = [PackageSwiftEdit(kind="synth_dynamic_library", product_name="GRDBDynamic", targets=["GRDB"])]
    staged, plan, dump = _roundtrip_apply_and_dump(GRDB_PACKAGE_SWIFT_FIXTURE, edits)
    try:
        prods = {p["name"]: p for p in dump["products"]}
        _assert("GRDBDynamic" in prods,
                f"synthetic GRDBDynamic missing: {sorted(prods.keys())}")
        _assert(prods["GRDBDynamic"]["type"]["library"][0] == "dynamic",
                f"GRDBDynamic linkage: {prods['GRDBDynamic']['type']}")
        _assert(prods["GRDBDynamic"]["targets"] == ["GRDB"],
                f"GRDBDynamic targets: {prods['GRDBDynamic']['targets']}")
        _assert(prods["GRDB"]["type"]["library"][0] == "automatic",
                f"original GRDB must stay automatic (rename is post-archive): "
                f"{prods['GRDB']['type']}")
        _assert(prods["GRDB-dynamic"]["type"]["library"][0] == "dynamic",
                f"GRDB-dynamic linkage: {prods['GRDB-dynamic']['type']}")
        _assert(prods["GRDBSQLite"]["type"]["library"][0] == "automatic",
                f"GRDBSQLite linkage: {prods['GRDBSQLite']['type']}")
    finally:
        shutil.rmtree(staged, ignore_errors=True)


def _roundtrip_alamofire() -> None:
    """Alamofire: synth_dynamic_library on Alamofire (allocator picks a
    non-colliding name since 'AlamofireDynamic' already exists). The
    synthetic product is dynamic; the original Alamofire stays automatic
    in the manifest; AlamofireDynamic is unchanged."""
    edits = [PackageSwiftEdit(kind="synth_dynamic_library", product_name="Alamofire__Dynamic", targets=["Alamofire"])]
    staged, plan, dump = _roundtrip_apply_and_dump(ALAMOFIRE_PACKAGE_SWIFT_FIXTURE, edits)
    try:
        prods = {p["name"]: p for p in dump["products"]}
        _assert(set(prods.keys()) == {"Alamofire", "AlamofireDynamic", "Alamofire__Dynamic"},
                f"Alamofire products: {sorted(prods.keys())}")
        _assert(prods["Alamofire__Dynamic"]["type"]["library"][0] == "dynamic",
                f"synthetic Alamofire__Dynamic linkage: {prods['Alamofire__Dynamic']['type']}")
        _assert(prods["Alamofire"]["type"]["library"][0] == "automatic",
                f"original Alamofire stays automatic: {prods['Alamofire']['type']}")
        _assert(prods["AlamofireDynamic"]["type"]["library"][0] == "dynamic",
                f"AlamofireDynamic linkage: {prods['AlamofireDynamic']['type']}")
    finally:
        shutil.rmtree(staged, ignore_errors=True)


def _roundtrip_alamofire_multi_manifest_layout() -> None:
    """Regression for the manifest-selection bug exposed by Alamofire 5.10.2.

    Alamofire ships THREE manifests in its package root:
      - Package.swift                 (declares tools-version 6.0)
      - Package@swift-5.10.swift      (declares tools-version 5.10)
      - Package@swift-5.9.swift       (declares tools-version 5.9)

    On a 6.x toolchain SPM uses `Package.swift` (6.0 is the highest
    declared tools-version that fits the toolchain). The legacy active-
    manifest selector picked by filename instead of by declared
    tools-version, edited the wrong file, and the edit silently no-op'd
    when `dump-package` re-read the real active manifest.

    This test reconstructs the layout in a temp dir, runs
    `apply_package_swift_edits` + `swift package dump-package`, and
    asserts that the edit ended up in `Package.swift` AND that the dumped
    product reflects the edit.
    """
    tmp = Path(tempfile.mkdtemp(prefix="spm2xc-prep-multi-"))
    try:
        # Real Alamofire-shaped layout. The base file declares 6.0 and is
        # the one SPM picks; the version-specific files are legacy
        # fallbacks for older toolchains.
        (tmp / "Package.swift").write_text(ALAMOFIRE_PACKAGE_SWIFT_FIXTURE)
        sibling_5_10 = (
            "// swift-tools-version:5.10\n"
            "// MARKER:DO_NOT_EDIT_5_10\n"
            "import PackageDescription\n"
            "let package = Package(name: \"Alamofire\",\n"
            "                      products: [\n"
            "                          .library(name: \"Alamofire\", targets: [\"Alamofire\"]),\n"
            "                          .library(name: \"AlamofireDynamic\", type: .dynamic, targets: [\"Alamofire\"]),\n"
            "                      ],\n"
            "                      targets: [.target(name: \"Alamofire\", path: \"Source\")])\n"
        )
        sibling_5_9 = (
            "// swift-tools-version:5.9\n"
            "// MARKER:DO_NOT_EDIT_5_9\n"
            "import PackageDescription\n"
            "let package = Package(name: \"Alamofire\",\n"
            "                      products: [],\n"
            "                      targets: [])\n"
        )
        (tmp / "Package@swift-5.10.swift").write_text(sibling_5_10)
        (tmp / "Package@swift-5.9.swift").write_text(sibling_5_9)
        sibling_5_10_before = (tmp / "Package@swift-5.10.swift").read_text()
        sibling_5_9_before = (tmp / "Package@swift-5.9.swift").read_text()
        # Real Alamofire ships a Source/ directory; create a stub so
        # dump-package doesn't bail out on a missing target path.
        (tmp / "Source").mkdir()
        (tmp / "Source" / "Empty.swift").write_text("// stub\n")

        plan = Plan()
        plan.package_swift_edits = [
            PackageSwiftEdit(kind="synth_dynamic_library",
                             product_name="Alamofire__Dynamic",
                             targets=["Alamofire"]),
        ]
        apply_package_swift_edits(tmp, plan)

        # Version-specific siblings must still carry their DO_NOT_EDIT
        # markers verbatim — `swift package add-product` resolves the
        # active manifest itself, so siblings can't be accidentally
        # touched.
        sibling_5_10_after = (tmp / "Package@swift-5.10.swift").read_text()
        sibling_5_9_after = (tmp / "Package@swift-5.9.swift").read_text()
        _assert(sibling_5_10_after == sibling_5_10_before,
                "Package@swift-5.10.swift should be byte-identical, got:\n"
                + sibling_5_10_after)
        _assert(sibling_5_9_after == sibling_5_9_before,
                "Package@swift-5.9.swift should be byte-identical, got:\n"
                + sibling_5_9_after)

        # Round-trip through SPM and verify the dump now contains the
        # synthetic dynamic product. This is what the validator would
        # do — it's the test that would have caught the original bug.
        cp = subprocess.run(
            ["swift", "package", "dump-package"],
            cwd=str(tmp),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if cp.returncode != 0:
            raise AssertionError(
                f"swift package dump-package failed:\n  {cp.stderr.strip()}"
            )
        dump = json.loads(cp.stdout)
        prods = {p["name"]: p for p in dump["products"]}
        _assert("Alamofire__Dynamic" in prods,
                f"synthetic Alamofire__Dynamic product missing: {sorted(prods.keys())}")
        _assert(prods["Alamofire__Dynamic"]["type"]["library"][0] == "dynamic",
                f"post-edit Alamofire__Dynamic linkage = "
                f"{prods['Alamofire__Dynamic']['type']}; the edit didn't take effect")
        _assert(prods["Alamofire"]["type"]["library"][0] == "automatic",
                "original Alamofire must stay automatic (rename is post-archive)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _roundtrip_stripe_force_dynamic_and_synthetic() -> None:
    """Stripe: synth_dynamic_library on Stripe (parallel dynamic
    product, allocator-named) + synth_library StripeCore (synthesize a
    fresh dynamic library directly from a target). Verify both
    `swift package add-product` invocations land and the existing 4
    products survive."""
    edits = [
        PackageSwiftEdit(kind="synth_dynamic_library", product_name="StripeDynamic", targets=["Stripe"]),
        PackageSwiftEdit(kind="synth_library", product_name="StripeCore", targets=["StripeCore"]),
    ]
    staged, plan, dump = _roundtrip_apply_and_dump(STRIPE_PACKAGE_SWIFT_FIXTURE, edits)
    try:
        prods = {p["name"]: p for p in dump["products"]}
        expected = {"Stripe", "StripePayments", "StripeFinancialConnections",
                    "StripeConnect", "StripeDynamic", "StripeCore"}
        _assert(set(prods.keys()) == expected,
                f"Stripe products: {sorted(prods.keys())}; expected {sorted(expected)}")
        _assert(prods["StripeDynamic"]["type"]["library"][0] == "dynamic",
                f"StripeDynamic linkage: {prods['StripeDynamic']['type']}")
        _assert(prods["StripeDynamic"]["targets"] == ["Stripe"],
                f"StripeDynamic targets: {prods['StripeDynamic']['targets']}")
        _assert(prods["StripeCore"]["type"]["library"][0] == "dynamic",
                f"StripeCore linkage: {prods['StripeCore']['type']}")
        _assert(prods["StripeCore"]["targets"] == ["StripeCore"],
                f"StripeCore targets: {prods['StripeCore']['targets']}")
        _assert(prods["Stripe"]["type"]["library"][0] == "automatic",
                f"original Stripe must stay automatic (rename is post-archive): "
                f"{prods['Stripe']['type']}")
        _assert(prods["StripePayments"]["type"]["library"][0] == "automatic",
                f"StripePayments linkage: {prods['StripePayments']['type']}")
    finally:
        shutil.rmtree(staged, ignore_errors=True)


def _roundtrip_system_library_left_alone() -> None:
    """A package with a system library + a regular target: the planner
    skips the system one, so Prepare only adds a synth product for the
    regular one. Validator must accept this without complaint."""
    edits = [PackageSwiftEdit(kind="synth_dynamic_library", product_name="WrapperDynamic", targets=["Wrapper"])]
    staged, plan, dump = _roundtrip_apply_and_dump(SYSTEM_LIB_PACKAGE_SWIFT_FIXTURE, edits)
    try:
        prods = {p["name"]: p for p in dump["products"]}
        _assert(prods["WrapperDynamic"]["type"]["library"][0] == "dynamic",
                f"WrapperDynamic linkage: {prods['WrapperDynamic']['type']}")
        _assert(prods["Wrapper"]["type"]["library"][0] == "automatic",
                f"original Wrapper stays automatic: {prods['Wrapper']['type']}")
        _assert(prods["Sqlite3"]["type"]["library"][0] == "automatic",
                f"Sqlite3 linkage: {prods['Sqlite3']['type']}")
    finally:
        shutil.rmtree(staged, ignore_errors=True)


def _roundtrip_validator_catches_missing_product() -> None:
    """The mandatory round-trip validator must raise a clean error when
    the planner asks Prepare to operate on a target that doesn't exist
    in the manifest. `swift package add-product --targets <UnknownTarget>`
    fails at the SPM layer; Prepare must surface that as a clean
    PrepareError/PrepareUserError, not a traceback."""
    tmp = Path(tempfile.mkdtemp(prefix="spm2xc-prep-bad-"))
    try:
        (tmp / "Package.swift").write_text(GRDB_PACKAGE_SWIFT_FIXTURE)
        plan = Plan()
        plan.package_swift_edits = [
            PackageSwiftEdit(kind="synth_library", product_name="DoesNotExist",
                             targets=["DoesNotExist"]),
        ]
        try:
            prepare(tmp, plan, verbose=False)
        except (PrepareUserError, PrepareError) as exc:
            _assert("DoesNotExist" in str(exc),
                    f"error should mention DoesNotExist: {exc}")
            return
        raise AssertionError("expected PrepareError for non-existent target")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _roundtrip_foo_force_dynamic() -> None:
    """Minimal Foo fixture: synth_dynamic_library on the lone .library
    product must produce a Package.swift that survives `swift package
    dump-package` with a fresh dynamic FooDynamic alongside the
    original. Smallest possible end-to-end gate for B's
    `swift package add-product` invocation against a real toolchain."""
    edits = [PackageSwiftEdit(kind="synth_dynamic_library", product_name="FooDynamic", targets=["Foo"])]
    tmp = Path(tempfile.mkdtemp(prefix="spm2xc-prep-foo-"))
    try:
        (tmp / "Package.swift").write_text(_FOO_PACKAGE_SWIFT_FIXTURE)
        sources = tmp / "Sources" / "Foo"
        sources.mkdir(parents=True)
        (sources / "Foo.swift").write_text("public enum Foo { public static let answer = 42 }\n")
        plan = Plan()
        plan.package_swift_edits = list(edits)
        apply_package_swift_edits(tmp, plan)
        cp = subprocess.run(
            ["swift", "package", "dump-package"],
            cwd=str(tmp),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if cp.returncode != 0:
            raise AssertionError(
                f"swift package dump-package failed on Foo fixture:\n"
                f"  {cp.stderr.strip()}\n  Edited Package.swift:\n"
                f"{(tmp / 'Package.swift').read_text()}"
            )
        dump = json.loads(cp.stdout)
        prods = {p["name"]: p for p in dump["products"]}
        _assert("FooDynamic" in prods, f"FooDynamic missing from dump: {sorted(prods.keys())}")
        _assert(prods["FooDynamic"]["type"]["library"][0] == "dynamic",
                f"FooDynamic linkage: {prods['FooDynamic']['type']}")
        _assert(prods["Foo"]["type"]["library"][0] == "automatic",
                "original Foo must stay automatic in manifest")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _roundtrip_full_prepare_grdb() -> None:
    """End-to-end Prepare on a GRDB-shaped Plan: confirms validator
    accepts the planner's exact edit list — synth_dynamic_library
    spawns GRDBDynamic, leaving GRDB-dynamic and GRDBSQLite alone.
    This is the gate that catches edit/planner drift across sessions."""
    tmp = Path(tempfile.mkdtemp(prefix="spm2xc-prep-grdb-"))
    try:
        (tmp / "Package.swift").write_text(GRDB_PACKAGE_SWIFT_FIXTURE)
        plan = Plan()
        plan.package_swift_edits = [
            PackageSwiftEdit(kind="synth_dynamic_library",
                             product_name="GRDBDynamic",
                             targets=["GRDB"]),
        ]
        plan.build_units = [
            BuildUnit(
                name="GRDB",
                scheme="GRDBDynamic",
                framework_name="GRDB",
                language=Language.SWIFT,
                archive_strategy="archive",
                source_targets=["GRDB"],
            ),
            BuildUnit(
                name="GRDB-dynamic",
                scheme="GRDB-dynamic",
                framework_name="GRDB-dynamic",
                language=Language.SWIFT,
                archive_strategy="archive",
                source_targets=["GRDB"],
            ),
        ]
        prepared = prepare(tmp, plan, verbose=False)
        prods = {p.name: p for p in prepared.package.products}
        _assert("GRDBDynamic" in prods,
                f"synthetic GRDBDynamic missing from dumped products: {sorted(prods.keys())}")
        _assert(prods["GRDBDynamic"].linkage == Linkage.DYNAMIC,
                f"GRDBDynamic linkage: {prods['GRDBDynamic'].linkage}")
        _assert(prods["GRDB"].linkage == Linkage.AUTOMATIC,
                f"original GRDB must stay automatic in manifest: {prods['GRDB'].linkage}")
        _assert(prods["GRDB-dynamic"].linkage == Linkage.DYNAMIC,
                f"GRDB-dynamic linkage: {prods['GRDB-dynamic'].linkage}")
        _assert("GRDBSQLite" in prods, "GRDBSQLite missing from dumped products")
        _assert(prods["GRDBSQLite"].linkage == Linkage.AUTOMATIC,
                f"GRDBSQLite should be untouched, got {prods['GRDBSQLite'].linkage}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --- inject_system_clang_modules tests ------------------------------------
#
# `.systemLibrary` targets like GRDBSQLite have no Mach-O of their own —
# they're a Clang shim around a system header (`<sqlite3.h>`). The
# planner correctly drops them as buildable products, but the parent
# Swift framework's .swiftinterface still emits `import GRDBSQLite`, so
# any consumer that has to rebuild from interface fails to resolve the
# module. The fix bundles the system target's modulemap+headers into
# each xcframework slice as a binary-less sibling shim framework.


def _selftest_promote_modulemap_to_framework_form_grdb() -> None:
    """The GRDBSQLite modulemap shape: `module Foo [system] { ... }`
    must gain a `framework ` qualifier and otherwise stay byte-identical.
    """
    src = (
        "module GRDBSQLite [system] {\n"
        "    header \"shim.h\"\n"
        "    link \"sqlite3\"\n"
        "    export *\n"
        "}\n"
    )
    out = _promote_modulemap_to_framework_form(src)
    _assert(
        out.startswith("framework module GRDBSQLite [system] {"),
        f"expected framework qualifier on first line; got:\n{out}",
    )
    # Body is preserved verbatim.
    _assert("header \"shim.h\"" in out, f"header line missing:\n{out}")
    _assert("link \"sqlite3\"" in out, f"link line missing:\n{out}")
    _assert("export *" in out, f"export line missing:\n{out}")
    # Idempotent: re-promoting an already-framework modulemap is a no-op.
    _assert(_promote_modulemap_to_framework_form(out) == out,
            "second promotion changed the text — should be idempotent")


def _selftest_promote_modulemap_to_framework_form_indented() -> None:
    """Leading whitespace on the `module` line must be preserved when we
    inject the `framework` qualifier — modulemap parsers don't care
    about indentation but the file should still look reasonable in
    diffs."""
    src = "    module Foo {\n        header \"x.h\"\n    }\n"
    out = _promote_modulemap_to_framework_form(src)
    _assert(out.startswith("    framework module Foo {"),
            f"expected indented framework decl; got:\n{out}")


def _selftest_walk_system_library_target_deps_grdb() -> None:
    """Direct dep edge: GRDB → GRDBSQLite. The walker should return
    GRDBSQLite when seeded with `["GRDB"]`."""
    package = _mk_package_from_snapshot(GRDB_DUMP_SNAPSHOT)
    found = _walk_system_library_target_deps(package, ["GRDB"])
    _assert(len(found) == 1, f"expected 1 system dep, got {len(found)}: {[t.name for t in found]}")
    _assert(found[0].name == "GRDBSQLite", f"expected GRDBSQLite, got {found[0].name}")
    _assert(found[0].kind == TargetKind.SYSTEM, f"expected SYSTEM kind, got {found[0].kind}")


def _selftest_walk_system_library_target_deps_no_system() -> None:
    """Stripe and Nuke have no `.systemLibrary` deps in our fixtures —
    the walker must return an empty list, not crash on missing edges."""
    pkg_stripe = _mk_package_from_snapshot(STRIPE_DUMP_SNAPSHOT)
    found_stripe = _walk_system_library_target_deps(pkg_stripe, ["Stripe"])
    _assert(found_stripe == [], f"Stripe should have no system deps, got {found_stripe}")

    pkg_nuke = _mk_package_from_snapshot(NUKE_DUMP_SNAPSHOT)
    found_nuke = _walk_system_library_target_deps(pkg_nuke, ["Nuke"])
    _assert(found_nuke == [], f"Nuke should have no system deps, got {found_nuke}")


def _selftest_walk_system_library_target_deps_transitive() -> None:
    """A two-hop chain: regular → regular → system. The walker must
    follow the intermediate regular target's dep edge to discover the
    system grandchild."""
    raw = {
        "name": "Chain",
        "products": [
            {"name": "Top", "type": {"library": ["automatic"]}, "targets": ["Top"]},
        ],
        "targets": [
            {"name": "Top", "type": "regular", "path": "Sources/Top",
             "publicHeadersPath": None,
             "dependencies": [{"target": ["Middle", None]}]},
            {"name": "Middle", "type": "regular", "path": "Sources/Middle",
             "publicHeadersPath": None,
             "dependencies": [{"byName": ["LeafSys", None]}]},
            {"name": "LeafSys", "type": "system", "path": None,
             "publicHeadersPath": None, "dependencies": []},
        ],
    }
    package = _mk_package_from_snapshot(raw)
    found = _walk_system_library_target_deps(package, ["Top"])
    _assert(len(found) == 1 and found[0].name == "LeafSys",
            f"expected transitive LeafSys, got {[t.name for t in found]}")


def _selftest_walk_system_library_target_deps_dedupes_diamond() -> None:
    """Diamond shape: A→B→D and A→C→D where D is a system target.
    D must appear once in the result, not twice."""
    raw = {
        "name": "Diamond",
        "products": [
            {"name": "A", "type": {"library": ["automatic"]}, "targets": ["A"]},
        ],
        "targets": [
            {"name": "A", "type": "regular", "path": "Sources/A",
             "publicHeadersPath": None,
             "dependencies": [{"byName": ["B", None]}, {"byName": ["C", None]}]},
            {"name": "B", "type": "regular", "path": "Sources/B",
             "publicHeadersPath": None,
             "dependencies": [{"byName": ["D", None]}]},
            {"name": "C", "type": "regular", "path": "Sources/C",
             "publicHeadersPath": None,
             "dependencies": [{"byName": ["D", None]}]},
            {"name": "D", "type": "system", "path": None,
             "publicHeadersPath": None, "dependencies": []},
        ],
    }
    package = _mk_package_from_snapshot(raw)
    found = _walk_system_library_target_deps(package, ["A"])
    _assert(len(found) == 1 and found[0].name == "D",
            f"expected single dedup'd D, got {[t.name for t in found]}")


def _selftest_system_target_source_dir_default(tmp_root: Path) -> None:
    """SPM convention: `Sources/<TargetName>/` when `path:` is unset."""
    base = tmp_root / "sysdir_default"
    src = base / "Sources" / "MySys"
    src.mkdir(parents=True)
    (src / "module.modulemap").write_text("module MySys [system] {}\n")
    target = Target(
        name="MySys",
        kind=TargetKind.SYSTEM,
        path=None,
        public_headers_path=None,
        dependencies=[],
        exclude=[],
    )
    found = _system_target_source_dir(target, base)
    _assert(found is not None and found.resolve() == src.resolve(),
            f"expected {src}, got {found}")


def _selftest_system_target_source_dir_explicit(tmp_root: Path) -> None:
    """When `path:` is set in Package.swift, honor it instead of the
    `Sources/<name>` default."""
    base = tmp_root / "sysdir_explicit"
    explicit = base / "vendor" / "sqlite3-shim"
    explicit.mkdir(parents=True)
    target = Target(
        name="MySys",
        kind=TargetKind.SYSTEM,
        path="vendor/sqlite3-shim",
        public_headers_path=None,
        dependencies=[],
        exclude=[],
    )
    found = _system_target_source_dir(target, base)
    _assert(found is not None and found.resolve() == explicit.resolve(),
            f"expected {explicit}, got {found}")


def _selftest_system_target_source_dir_missing(tmp_root: Path) -> None:
    """If the source dir doesn't exist on disk, return None — the caller
    warns instead of crashing on the missing modulemap."""
    base = tmp_root / "sysdir_missing"
    base.mkdir(parents=True)
    target = Target(
        name="GhostSys", kind=TargetKind.SYSTEM, path=None,
        public_headers_path=None, dependencies=[], exclude=[],
    )
    _assert(_system_target_source_dir(target, base) is None,
            "missing source dir should return None")


def _make_flat_framework(fw_path: Path) -> Path:
    """Create a minimal flat-layout (iOS-style) framework on disk."""
    fw_path.mkdir(parents=True, exist_ok=True)
    (fw_path / "Foo").write_bytes(b"\x00")  # binary stub
    (fw_path / "Info.plist").write_text(
        '<?xml version="1.0"?><plist version="1.0"><dict/></plist>'
    )
    return fw_path


def _make_versioned_framework(fw_path: Path, fw_name: str = "Foo") -> Path:
    """Create a minimal macOS-style versioned framework on disk, matching
    xcodebuild's archive output: binary + Resources/Info.plist under
    Versions/A, with Current → A and a Resources root symlink."""
    versions_a = fw_path / "Versions" / "A"
    (versions_a / "Resources").mkdir(parents=True)
    (versions_a / fw_name).write_bytes(b"\x00")  # binary stub
    (versions_a / "Resources" / "Info.plist").write_text(
        '<?xml version="1.0"?><plist version="1.0"><dict/></plist>'
    )
    (fw_path / "Versions" / "Current").symlink_to("A")
    (fw_path / fw_name).symlink_to(Path("Versions") / "Current" / fw_name)
    (fw_path / "Resources").symlink_to(Path("Versions") / "Current" / "Resources")
    return fw_path


def _selftest_framework_content_root_flat_vs_versioned(tmp_root: Path) -> None:
    """_framework_content_root must return fw_path for flat frameworks and
    Versions/A for versioned (macOS-style) frameworks. Detection key:
    presence of a real, non-symlink Versions/A directory at the root.
    """
    base = tmp_root / "content_root"
    flat_fw = _make_flat_framework(base / "Flat.framework")
    versioned_fw = _make_versioned_framework(base / "Versioned.framework")
    _assert(
        _framework_content_root(flat_fw) == flat_fw,
        f"flat framework should resolve to fw_path, got "
        f"{_framework_content_root(flat_fw)}",
    )
    expected_versioned = versioned_fw / "Versions" / "A"
    _assert(
        _framework_content_root(versioned_fw) == expected_versioned,
        f"versioned framework should resolve to Versions/A, got "
        f"{_framework_content_root(versioned_fw)}",
    )


def _selftest_ensure_root_symlink_versioned(tmp_root: Path) -> None:
    """On a versioned framework with no root Modules entry, the helper
    must create a relative symlink `Modules -> Versions/Current/Modules`.
    No-op on flat frameworks. Idempotent on already-correct symlinks.
    """
    base = tmp_root / "ensure_symlink"
    versioned_fw = _make_versioned_framework(base / "V.framework")
    # No Modules at root yet — helper should create the symlink.
    _ensure_root_symlink(versioned_fw, "Modules")
    link = versioned_fw / "Modules"
    _assert(link.is_symlink(), "Modules should be a symlink after ensure")
    _assert(
        os.readlink(link) == str(Path("Versions") / "Current" / "Modules"),
        f"unexpected symlink target: {os.readlink(link)}",
    )
    # Idempotent — second call must not raise or change the link.
    _ensure_root_symlink(versioned_fw, "Modules")
    _assert(link.is_symlink(), "Modules should remain a symlink after re-ensure")

    # Flat framework: helper must be a no-op.
    flat_fw = _make_flat_framework(base / "F.framework")
    _ensure_root_symlink(flat_fw, "Modules")
    _assert(
        not (flat_fw / "Modules").exists(),
        "ensure_root_symlink should not touch flat frameworks",
    )


def _selftest_ensure_root_symlink_migrates_real_dir(tmp_root: Path) -> None:
    """If a previous broken injection left a real `Modules/` directory at
    the framework root of a versioned framework, the helper must migrate
    that directory into `Versions/A/Modules` and replace the root with a
    symlink. This repairs frameworks produced by older builds.
    """
    base = tmp_root / "migrate_root"
    fw = _make_versioned_framework(base / "M.framework")
    # Simulate the bug: real Modules/ directory at the framework root.
    real_modules = fw / "Modules"
    real_modules.mkdir()
    (real_modules / "Foo.swiftmodule").mkdir()
    (real_modules / "Foo.swiftmodule" / "arm64.swiftinterface").write_text(
        "// iface\n"
    )

    _ensure_root_symlink(fw, "Modules")

    link = fw / "Modules"
    _assert(link.is_symlink(), "Modules at root should be a symlink after migration")
    migrated = fw / "Versions" / "A" / "Modules" / "Foo.swiftmodule" / "arm64.swiftinterface"
    _assert(migrated.is_file(), f"swiftinterface not migrated under Versions/A: {migrated}")


def _selftest_inject_swiftmodule_versioned_macos(tmp_root: Path) -> None:
    """inject_swiftmodule on a macOS-style versioned framework writes
    Modules/ under Versions/A and leaves the framework root with a
    symlink. This is the codesigning fix from MACOS_SLICE_BUG.md.
    """
    base = tmp_root / "inject_swift_macos"
    fw = _make_versioned_framework(base / "Foo.framework")
    # Synthesize a DerivedData tree containing a Foo.swiftmodule with
    # one .swiftinterface — matches what xcodebuild emits.
    dd = base / "DerivedData"
    swiftmod = dd / "Build" / "Products" / "Release" / "Foo.swiftmodule"
    swiftmod.mkdir(parents=True)
    (swiftmod / "arm64-apple-macos.swiftinterface").write_text(
        "// swift-interface-format-version: 1.0\nimport Foundation\n"
    )

    injected = inject_swiftmodule(
        fw_path=fw,
        fw_name="Foo",
        scheme="Foo",
        dd_path=dd,
        variant="macos-arm64_x86_64",
        verbose=False,
    )
    _assert(injected, "inject_swiftmodule should report injection on macOS slice")

    # The real swiftmodule must land under Versions/A/Modules, NOT at root.
    real_dest = fw / "Versions" / "A" / "Modules" / "Foo.swiftmodule" / "arm64-apple-macos.swiftinterface"
    _assert(real_dest.is_file(), f"swiftinterface not at versioned path: {real_dest}")

    # Root must hold a symlink, not a real directory — this is the
    # codesigning constraint.
    root_modules = fw / "Modules"
    _assert(root_modules.is_symlink(),
            f"root Modules must be a symlink, got real dir at {root_modules}")
    _assert(
        os.readlink(root_modules) == str(Path("Versions") / "Current" / "Modules"),
        f"root Modules symlink wrong target: {os.readlink(root_modules)}",
    )

    # Re-run must be idempotent (interfaces already present → no-op).
    injected_again = inject_swiftmodule(
        fw_path=fw,
        fw_name="Foo",
        scheme="Foo",
        dd_path=dd,
        variant="macos-arm64_x86_64",
        verbose=False,
    )
    _assert(not injected_again,
            "inject_swiftmodule should be a no-op when interfaces already present")


def _selftest_inject_swiftmodule_flat_unchanged(tmp_root: Path) -> None:
    """inject_swiftmodule on a flat (iOS-style) framework must keep the
    flat layout: Modules/ stays a real directory at the framework root.
    Regression guard so the macOS fix doesn't accidentally affect iOS.
    """
    base = tmp_root / "inject_swift_flat"
    fw = _make_flat_framework(base / "Foo.framework")
    dd = base / "DerivedData"
    swiftmod = dd / "Build" / "Products" / "Release" / "Foo.swiftmodule"
    swiftmod.mkdir(parents=True)
    (swiftmod / "arm64-apple-ios.swiftinterface").write_text(
        "// swift-interface-format-version: 1.0\nimport Foundation\n"
    )

    injected = inject_swiftmodule(
        fw_path=fw,
        fw_name="Foo",
        scheme="Foo",
        dd_path=dd,
        variant="ios-arm64",
        verbose=False,
    )
    _assert(injected, "inject_swiftmodule should report injection on iOS slice")

    root_modules = fw / "Modules"
    _assert(root_modules.is_dir(), "iOS Modules/ should exist")
    _assert(not root_modules.is_symlink(),
            "iOS Modules/ must remain a real directory, not a symlink")
    _assert((root_modules / "Foo.swiftmodule" / "arm64-apple-ios.swiftinterface").is_file(),
            "swiftinterface not written at flat-layout location")
    _assert(not (fw / "Versions").exists(),
            "flat framework must not gain a Versions/ directory")


def _selftest_inject_resource_bundles_versioned_macos(tmp_root: Path) -> None:
    """SwiftPM `.bundle` sub-bundles drop under Versions/A/Resources/
    on macOS, not at the framework root (which would break codesigning).
    """
    base = tmp_root / "inject_bundle_macos"
    fw = _make_versioned_framework(base / "Bar.framework")
    # Fake DerivedData with a single SwiftPM resource bundle under the
    # Build/Products/<config>/ fallback path that _find_resource_bundles
    # scans (`<dd>/Build/Products/*/*.bundle`).
    dd = base / "DerivedData"
    bundle_src = dd / "Build" / "Products" / "Release-macosx" / "Bar_Bar.bundle"
    bundle_src.mkdir(parents=True)
    (bundle_src / "Info.plist").write_text("<plist/>")

    n = inject_resource_bundles(
        fw_path=fw,
        fw_name="Bar",
        dd_path=dd,
        variant="macos-arm64_x86_64",
        verbose=False,
    )
    _assert(n == 1, f"expected 1 bundle injected, got {n}")

    # Bundle lands under Versions/A/Resources/, NOT at framework root.
    real_dest = fw / "Versions" / "A" / "Resources" / "Bar_Bar.bundle"
    _assert(real_dest.is_dir(), f"bundle not at versioned path: {real_dest}")
    _assert(not (fw / "Bar_Bar.bundle").is_dir() or (fw / "Bar_Bar.bundle").is_symlink(),
            "bundle must not exist as a real dir at framework root")


def _selftest_inject_system_clang_modules_grdb_shape(tmp_root: Path) -> None:
    """End-to-end on a synthetic GRDB-shaped fixture: a built xcframework
    with two slices (device + sim), a regular Swift target with a
    system-library dep, and an on-disk Sources/GRDBSQLite/ directory.
    After injection, both slices must have a sibling
    `GRDBSQLite.framework/Modules/module.modulemap` (with framework
    qualifier) and `GRDBSQLite.framework/Headers/shim.h`.
    """
    base = tmp_root / "inject_sys_grdb"
    staged = base / "staged"
    sys_src = staged / "Sources" / "GRDBSQLite"
    sys_src.mkdir(parents=True)
    (sys_src / "module.modulemap").write_text(
        "module GRDBSQLite [system] {\n"
        "    header \"shim.h\"\n"
        "    link \"sqlite3\"\n"
        "    export *\n"
        "}\n"
    )
    (sys_src / "shim.h").write_text("#include <sqlite3.h>\n")

    package = Package(
        name="GRDB",
        tools_version="6.1.0",
        platforms=[],
        products=[Product(name="GRDB", linkage=Linkage.AUTOMATIC, targets=["GRDB"])],
        targets=[
            Target(name="GRDBSQLite", kind=TargetKind.SYSTEM, path=None,
                   public_headers_path=None, dependencies=[], exclude=[]),
            Target(name="GRDB", kind=TargetKind.REGULAR, path="GRDB",
                   public_headers_path=None, dependencies=["GRDBSQLite"],
                   exclude=[]),
        ],
        schemes=[],
        raw_dump=GRDB_DUMP_SNAPSHOT,
        staged_dir=staged,
    )

    # Synthetic xcframework with two slice dirs containing an empty
    # GRDB.framework. We don't need a real binary — the helper only
    # walks slice dirs and adds siblings.
    xcfw = base / "GRDB.xcframework"
    for slice_name in ("ios-arm64", "ios-arm64_x86_64-simulator"):
        slice_dir = xcfw / slice_name
        (slice_dir / "GRDB.framework").mkdir(parents=True)
    (xcfw / "Info.plist").write_text("<plist></plist>")  # presence-only

    n = inject_system_clang_modules(
        xcframework_path=xcfw,
        package=package,
        source_targets=["GRDB"],
        verbose=False,
    )
    _assert(n == 1, f"expected 1 system shim injected, got {n}")

    for slice_name in ("ios-arm64", "ios-arm64_x86_64-simulator"):
        shim_fw = xcfw / slice_name / "GRDBSQLite.framework"
        _assert(shim_fw.is_dir(), f"missing shim framework dir in {slice_name}")
        modulemap = shim_fw / "Modules" / "module.modulemap"
        _assert(modulemap.is_file(), f"missing modulemap in {slice_name}")
        text = modulemap.read_text()
        _assert("framework module GRDBSQLite" in text,
                f"modulemap not promoted to framework form in {slice_name}:\n{text}")
        _assert("link \"sqlite3\"" in text,
                f"sqlite3 link directive lost in {slice_name}:\n{text}")
        shim_h = shim_fw / "Headers" / "shim.h"
        _assert(shim_h.is_file(), f"missing shim.h header in {slice_name}")
        _assert(shim_h.read_text() == "#include <sqlite3.h>\n",
                f"shim.h content corrupted in {slice_name}")
        # Sentinel file lets `_is_system_shim_framework` distinguish
        # this shim from real ObjC frameworks. Required so that
        # downstream language classification skips it.
        sentinel = shim_fw / ".spm-to-xcframework-system-shim"
        _assert(sentinel.is_file(), f"missing sentinel file in {slice_name}")


def _selftest_inject_system_clang_modules_idempotent(tmp_root: Path) -> None:
    """A second injection call must not regenerate or duplicate existing
    shim frameworks. The return value drops to 0 because no NEW shim
    was injected this round."""
    base = tmp_root / "inject_sys_idempotent"
    staged = base / "staged"
    sys_src = staged / "Sources" / "GRDBSQLite"
    sys_src.mkdir(parents=True)
    (sys_src / "module.modulemap").write_text(
        "module GRDBSQLite [system] { header \"shim.h\" link \"sqlite3\" export * }\n"
    )
    (sys_src / "shim.h").write_text("// shim\n")

    package = Package(
        name="GRDB", tools_version="6.1.0", platforms=[],
        products=[Product(name="GRDB", linkage=Linkage.AUTOMATIC, targets=["GRDB"])],
        targets=[
            Target(name="GRDBSQLite", kind=TargetKind.SYSTEM, path=None,
                   public_headers_path=None, dependencies=[], exclude=[]),
            Target(name="GRDB", kind=TargetKind.REGULAR, path="GRDB",
                   public_headers_path=None, dependencies=["GRDBSQLite"],
                   exclude=[]),
        ],
        schemes=[], raw_dump=GRDB_DUMP_SNAPSHOT, staged_dir=staged,
    )

    xcfw = base / "GRDB.xcframework"
    (xcfw / "ios-arm64" / "GRDB.framework").mkdir(parents=True)
    (xcfw / "Info.plist").write_text("<plist></plist>")

    first = inject_system_clang_modules(
        xcframework_path=xcfw, package=package,
        source_targets=["GRDB"], verbose=False,
    )
    _assert(first == 1, f"first call should report 1 injected, got {first}")

    # Capture the modulemap mtime so we can prove the second call doesn't
    # rewrite it.
    mm = xcfw / "ios-arm64" / "GRDBSQLite.framework" / "Modules" / "module.modulemap"
    first_mtime = mm.stat().st_mtime_ns

    second = inject_system_clang_modules(
        xcframework_path=xcfw, package=package,
        source_targets=["GRDB"], verbose=False,
    )
    _assert(second == 0, f"second call should report 0 (already-present), got {second}")
    _assert(mm.stat().st_mtime_ns == first_mtime,
            "modulemap was rewritten on second call — not idempotent")


def _selftest_inject_system_clang_modules_no_system_deps(tmp_root: Path) -> None:
    """A package with no `.systemLibrary` targets must skip injection
    entirely — no warnings, no created files."""
    base = tmp_root / "inject_sys_nodeps"
    staged = base / "staged"
    staged.mkdir(parents=True)
    package = Package(
        name="Nuke", tools_version="5.6.0", platforms=[],
        products=[Product(name="Nuke", linkage=Linkage.AUTOMATIC, targets=["Nuke"])],
        targets=[Target(name="Nuke", kind=TargetKind.REGULAR, path=None,
                        public_headers_path=None, dependencies=[], exclude=[])],
        schemes=[], raw_dump=NUKE_DUMP_SNAPSHOT, staged_dir=staged,
    )
    xcfw = base / "Nuke.xcframework"
    (xcfw / "ios-arm64" / "Nuke.framework").mkdir(parents=True)
    (xcfw / "Info.plist").write_text("<plist></plist>")

    n = inject_system_clang_modules(
        xcframework_path=xcfw, package=package,
        source_targets=["Nuke"], verbose=False,
    )
    _assert(n == 0, f"expected 0 injected for system-free package, got {n}")
    # No new sibling frameworks created.
    siblings = sorted(p.name for p in (xcfw / "ios-arm64").iterdir())
    _assert(siblings == ["Nuke.framework"],
            f"unexpected slice contents: {siblings}")


# --- inject_bridge_clang_modules tests ------------------------------------
#
# Distinct from `inject_system_clang_modules`: that one walks the SPM model
# for `.systemLibrary` targets. This one parses the framework's emitted
# swiftinterface for imports the framework's own modulemap doesn't declare,
# then matches them against project-shipped modulemaps in the package
# source tree. Original surfacing case: WCDB ships `WCDB_Private` via
# `src/bridge/module.modulemap` and the WCDBSwift swiftinterface imports
# it; without injection, consumers fail with `error: no such module
# 'WCDB_Private'` when rebuilding from the textual interface.


def _selftest_scan_swiftinterface_imports_normalizes_dotted(tmp_root: Path) -> None:
    """`import X.Sub` (submodule) and `import X` (plain) both normalize to
    the top-level module name `X`. The original regex required the line to
    end after the first identifier, so dotted imports silently dropped — a
    swiftinterface containing only `import WCDB_Private.Sub` would report
    no missing modules and the bridge-shim injection would skip."""
    base = tmp_root / "scan_imports_dotted"
    fw = base / "WCDBSwift.framework"
    swiftmod = fw / "Modules" / "WCDBSwift.swiftmodule"
    swiftmod.mkdir(parents=True)
    (swiftmod / "arm64-apple-ios.swiftinterface").write_text(
        "// swift-interface-format-version: 1.0\n"
        "import Foundation\n"
        "import WCDB_Private.Sub\n"
        "import _Concurrency\n"
    )
    (swiftmod / "arm64-apple-ios.private.swiftinterface").write_text(
        "import Other.Inner.Deep\n"
        "import Plain\n"
    )
    imports = tool._scan_swiftinterface_imports(fw)
    _assert(
        imports == {"Foundation", "WCDB_Private", "_Concurrency", "Other", "Plain"},
        f"dotted imports not normalized: {sorted(imports)}",
    )


def _selftest_inject_bridge_clang_modules_wcdb_shape(tmp_root: Path) -> None:
    """End-to-end on a WCDB-shaped fixture. Staged source tree contains a
    `bridge/` target with its own module.modulemap declaring `WCDB_Private`
    and an umbrella header. The xcframework's primary framework has a
    swiftinterface that imports `WCDB_Private` and a modulemap that does
    NOT declare it. After injection, both slices must have a sibling
    `WCDB_Private.framework` with a framework-form modulemap and the
    bridge headers."""
    base = tmp_root / "inject_bridge_wcdb"
    staged = base / "staged"
    bridge_src = staged / "src" / "bridge"
    bridge_src.mkdir(parents=True)
    (bridge_src / "module.modulemap").write_text(
        'module WCDB_Private {\n'
        '    requires objc\n'
        '    umbrella header "WCDBBridging.h"\n'
        '    export *\n'
        '}\n'
    )
    (bridge_src / "WCDBBridging.h").write_text("// umbrella\n")
    (bridge_src / "ColumnBridge.h").write_text("// column\n")
    (bridge_src / "BindingBridge.h").write_text("// binding\n")

    package = Package(
        name="WCDBSwift", tools_version="5.5", platforms=[],
        products=[Product(name="WCDBSwift", linkage=Linkage.AUTOMATIC, targets=["WCDBSwift"])],
        targets=[
            Target(name="bridge", kind=TargetKind.REGULAR, path="src/bridge",
                   public_headers_path=None, dependencies=[], exclude=[]),
            Target(name="WCDBSwift", kind=TargetKind.REGULAR, path="src/swift",
                   public_headers_path=None, dependencies=["bridge"], exclude=[]),
        ],
        schemes=[], raw_dump={}, staged_dir=staged,
    )

    xcfw = base / "WCDBSwift.xcframework"
    for slice_name in ("ios-arm64", "ios-arm64_x86_64-simulator"):
        primary = xcfw / slice_name / "WCDBSwift.framework"
        primary.mkdir(parents=True)
        # Modulemap declares the main module only (matches what
        # xcodebuild archive emits when bridge headers are auto-collected).
        (primary / "Modules").mkdir(exist_ok=True)
        (primary / "Modules" / "module.modulemap").write_text(
            'framework module WCDBSwift {\n'
            '  header "ColumnBridge.h"\n'
            '  export *\n'
            '}\n'
        )
        # Swiftinterface imports WCDB_Private — the trigger.
        swiftmod = primary / "Modules" / "WCDBSwift.swiftmodule"
        swiftmod.mkdir(parents=True)
        (swiftmod / "arm64-apple-ios.swiftinterface").write_text(
            "// swift-interface-format-version: 1.0\n"
            "import Foundation\n"
            "import Swift\n"
            "import WCDB_Private\n"
            "import _Concurrency\n"
        )
    (xcfw / "Info.plist").write_text("<plist></plist>")

    n = inject_bridge_clang_modules(
        xcframework_path=xcfw, package=package,
        fw_name="WCDBSwift", verbose=False,
    )
    _assert(n == 1, f"expected 1 bridge shim injected, got {n}")

    for slice_name in ("ios-arm64", "ios-arm64_x86_64-simulator"):
        shim_fw = xcfw / slice_name / "WCDB_Private.framework"
        _assert(shim_fw.is_dir(), f"missing shim framework dir in {slice_name}")
        modulemap = shim_fw / "Modules" / "module.modulemap"
        _assert(modulemap.is_file(), f"missing modulemap in {slice_name}")
        text = modulemap.read_text()
        _assert("framework module WCDB_Private" in text,
                f"modulemap not promoted to framework form:\n{text}")
        _assert('umbrella header "WCDBBridging.h"' in text,
                f"umbrella header lost:\n{text}")
        # All three .h files in bridge_src/ should land in Headers/
        # because the umbrella header expansion takes every sibling .h.
        for hname in ("WCDBBridging.h", "ColumnBridge.h", "BindingBridge.h"):
            h = shim_fw / "Headers" / hname
            _assert(h.is_file(), f"missing header {hname} in {slice_name}")
        # Sentinel marks this as a spm-to-xcframework-injected shim
        # so language classification skips it.
        _assert((shim_fw / ".spm-to-xcframework-system-shim").is_file(),
                f"missing sentinel file in {slice_name}")


def _selftest_inject_bridge_clang_modules_idempotent(tmp_root: Path) -> None:
    """Second call must be a no-op once shims exist. Counter drops to 0
    and existing modulemap mtime is preserved."""
    base = tmp_root / "inject_bridge_idempotent"
    staged = base / "staged"
    bridge_src = staged / "src" / "bridge"
    bridge_src.mkdir(parents=True)
    (bridge_src / "module.modulemap").write_text(
        'module WCDB_Private { umbrella header "U.h" export * }\n'
    )
    (bridge_src / "U.h").write_text("// u\n")

    package = Package(
        name="WCDBSwift", tools_version="5.5", platforms=[],
        products=[Product(name="WCDBSwift", linkage=Linkage.AUTOMATIC, targets=["WCDBSwift"])],
        targets=[Target(name="WCDBSwift", kind=TargetKind.REGULAR, path="src/swift",
                        public_headers_path=None, dependencies=[], exclude=[])],
        schemes=[], raw_dump={}, staged_dir=staged,
    )

    xcfw = base / "WCDBSwift.xcframework"
    primary = xcfw / "ios-arm64" / "WCDBSwift.framework"
    primary.mkdir(parents=True)
    (primary / "Modules").mkdir()
    (primary / "Modules" / "module.modulemap").write_text(
        'framework module WCDBSwift { export * }\n'
    )
    swiftmod = primary / "Modules" / "WCDBSwift.swiftmodule"
    swiftmod.mkdir(parents=True)
    (swiftmod / "arm64-apple-ios.swiftinterface").write_text("import WCDB_Private\n")
    (xcfw / "Info.plist").write_text("<plist></plist>")

    first = inject_bridge_clang_modules(
        xcframework_path=xcfw, package=package, fw_name="WCDBSwift", verbose=False,
    )
    _assert(first == 1, f"first call should report 1, got {first}")

    mm = xcfw / "ios-arm64" / "WCDB_Private.framework" / "Modules" / "module.modulemap"
    first_mtime = mm.stat().st_mtime_ns

    second = inject_bridge_clang_modules(
        xcframework_path=xcfw, package=package, fw_name="WCDBSwift", verbose=False,
    )
    _assert(second == 0, f"second call should report 0, got {second}")
    _assert(mm.stat().st_mtime_ns == first_mtime,
            "modulemap was rewritten on second call — not idempotent")


def _selftest_inject_bridge_clang_modules_no_missing_imports(tmp_root: Path) -> None:
    """When every swiftinterface import is either declared in the
    framework's modulemap or a Swift built-in, the pass injects nothing.
    Pure-Swift packages (no project-shipped clang modulemap) are this
    case — covers Nuke, Lottie, Kingfisher shapes."""
    base = tmp_root / "inject_bridge_pureswift"
    staged = base / "staged"
    staged.mkdir(parents=True)

    package = Package(
        name="Nuke", tools_version="5.6.0", platforms=[],
        products=[Product(name="Nuke", linkage=Linkage.AUTOMATIC, targets=["Nuke"])],
        targets=[Target(name="Nuke", kind=TargetKind.REGULAR, path=None,
                        public_headers_path=None, dependencies=[], exclude=[])],
        schemes=[], raw_dump={}, staged_dir=staged,
    )

    xcfw = base / "Nuke.xcframework"
    primary = xcfw / "ios-arm64" / "Nuke.framework"
    primary.mkdir(parents=True)
    swiftmod = primary / "Modules" / "Nuke.swiftmodule"
    swiftmod.mkdir(parents=True)
    (swiftmod / "arm64-apple-ios.swiftinterface").write_text(
        "import Foundation\nimport Swift\nimport _Concurrency\nimport UIKit\n"
    )
    (xcfw / "Info.plist").write_text("<plist></plist>")

    n = inject_bridge_clang_modules(
        xcframework_path=xcfw, package=package, fw_name="Nuke", verbose=False,
    )
    # UIKit is unknown to the finder (no modulemap in package source),
    # which is the right behavior — system frameworks are served by the
    # consumer's SDK. Foundation/Swift/_Concurrency are filtered before
    # the finder is even called.
    _assert(n == 0, f"expected 0 injected for pure-Swift package, got {n}")
    siblings = sorted(p.name for p in (xcfw / "ios-arm64").iterdir())
    _assert(siblings == ["Nuke.framework"],
            f"unexpected slice contents: {siblings}")


def _selftest_inject_bridge_clang_modules_skips_already_declared(tmp_root: Path) -> None:
    """If the framework's modulemap ALREADY declares the imported module
    name (e.g. as a submodule), no injection happens — the import is
    already resolvable."""
    base = tmp_root / "inject_bridge_already_declared"
    staged = base / "staged"
    bridge_src = staged / "bridge"
    bridge_src.mkdir(parents=True)
    (bridge_src / "module.modulemap").write_text(
        'module WCDB_Private { umbrella header "U.h" export * }\n'
    )
    (bridge_src / "U.h").write_text("// u\n")

    package = Package(
        name="X", tools_version="5.5", platforms=[],
        products=[Product(name="X", linkage=Linkage.AUTOMATIC, targets=["X"])],
        targets=[Target(name="X", kind=TargetKind.REGULAR, path="x",
                        public_headers_path=None, dependencies=[], exclude=[])],
        schemes=[], raw_dump={}, staged_dir=staged,
    )

    xcfw = base / "X.xcframework"
    primary = xcfw / "ios-arm64" / "X.framework"
    primary.mkdir(parents=True)
    (primary / "Modules").mkdir()
    # Framework's modulemap already declares WCDB_Private → no injection
    # needed even though the swiftinterface imports it.
    (primary / "Modules" / "module.modulemap").write_text(
        'framework module X { export * }\n'
        'module WCDB_Private { header "x.h" export * }\n'
    )
    swiftmod = primary / "Modules" / "X.swiftmodule"
    swiftmod.mkdir(parents=True)
    (swiftmod / "arm64-apple-ios.swiftinterface").write_text("import WCDB_Private\n")
    (xcfw / "Info.plist").write_text("<plist></plist>")

    n = inject_bridge_clang_modules(
        xcframework_path=xcfw, package=package, fw_name="X", verbose=False,
    )
    _assert(n == 0, f"expected 0 injected when already declared, got {n}")


def _selftest_find_project_modulemap_for_module_resolves_umbrella() -> None:
    """`umbrella header "X.h"` must collect every sibling .h in the same
    directory (clang's umbrella semantics). Verified directly so the
    end-to-end test can rely on this expansion."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / "staged"
        bridge = staged / "bridge"
        bridge.mkdir(parents=True)
        (bridge / "module.modulemap").write_text(
            'module M { umbrella header "U.h" export * }\n'
        )
        (bridge / "U.h").write_text("// u\n")
        (bridge / "A.h").write_text("// a\n")
        (bridge / "B.h").write_text("// b\n")
        # Non-header file must be ignored.
        (bridge / "notheader.txt").write_text("noise\n")
        package = Package(
            name="P", tools_version="5.5", platforms=[],
            products=[], targets=[], schemes=[], raw_dump={}, staged_dir=staged,
        )
        result = _find_project_modulemap_for_module(package, "M")
        _assert(result is not None, "expected modulemap match for M")
        modulemap_path, headers = result
        names = sorted(h.name for h, _rel in headers)
        _assert(names == ["A.h", "B.h", "U.h"],
                f"expected umbrella to expand to all sibling .h files, got {names}")
        # Modulemap is at bridge/, so each header's relative path is just its
        # basename (no nested directory prefix in this fixture).
        rels = sorted(str(rel) for _h, rel in headers)
        _assert(rels == ["A.h", "B.h", "U.h"],
                f"expected flat relative paths, got {rels}")


def _selftest_find_project_modulemap_for_module_symlinked_umbrella() -> None:
    """Regression test for the WCDB shape: `bridge/include/` ships a
    `module.modulemap` that names `WCDBBridging.h` as its umbrella, but
    EVERY .h in `bridge/include/` is a SYMLINK pointing into a sibling
    directory (e.g. `../cppbridge/BindingBridge.h`). An umbrella header
    expansion that calls `Path.resolve()` would dereference the umbrella
    symlink and conclude the umbrella's parent dir is `bridge/` — a
    directory containing only one `.h` — silently dropping the other 64
    headers and producing a shim framework that fails to compile because
    `#include "ObjectBridge.h"` from the umbrella can't find its sibling.

    Use logical paths (no resolve) so the umbrella's parent stays at
    `bridge/include/` regardless of where the symlinks ultimately point.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / "staged"
        bridge = staged / "bridge"
        include = bridge / "include"
        include.mkdir(parents=True)
        (include / "module.modulemap").write_text(
            'module M { umbrella header "WCDBBridging.h" export * }\n'
        )
        # Real files live in bridge/, NOT bridge/include/.
        (bridge / "WCDBBridging.h").write_text("// umbrella\n")
        (bridge / "ObjectBridge.h").write_text("// real header\n")
        (bridge / "BindingBridge.h").write_text("// real header\n")
        # bridge/include/ contains only symlinks pointing into the parent.
        (include / "WCDBBridging.h").symlink_to("../WCDBBridging.h")
        (include / "ObjectBridge.h").symlink_to("../ObjectBridge.h")
        (include / "BindingBridge.h").symlink_to("../BindingBridge.h")
        package = Package(
            name="P", tools_version="5.5", platforms=[],
            products=[], targets=[], schemes=[], raw_dump={}, staged_dir=staged,
        )
        result = _find_project_modulemap_for_module(package, "M")
        _assert(result is not None, "expected modulemap match for M")
        _, headers = result
        names = sorted(h.name for h, _rel in headers)
        _assert(names == ["BindingBridge.h", "ObjectBridge.h", "WCDBBridging.h"],
                f"expected umbrella to enumerate symlinked siblings in include/, got {names}")


def _selftest_find_project_modulemap_for_module_skips_dangling_symlinks() -> None:
    """Regression test for the WCDB shape: `bridge/include/` ships a
    handful of symlinks into `bridge/tests/` (e.g.
    `AutoAddColumnObject.h -> ..//tests/crud/AutoAddColumnObject.h`) and
    SwiftPM-style staging deletes the `tests/` subdir per the package's
    `exclude: ["tests"]` declaration. The dangling symlinks survive the
    prune but `shutil.copy2` would crash with FileNotFoundError when it
    tries to open the now-missing target. Filter them out at scan time
    so the shim only contains headers that have actual content — those
    test-only includes aren't referenced by the public umbrella chain.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / "staged"
        bridge = staged / "bridge"
        bridge.mkdir(parents=True)
        (bridge / "module.modulemap").write_text(
            'module M { umbrella header "U.h" export * }\n'
        )
        (bridge / "U.h").write_text("// umbrella\n")
        (bridge / "Real.h").write_text("// real\n")
        # Dangling symlink: target tests/ subdir was pruned.
        (bridge / "Dangling.h").symlink_to("missing/Header.h")
        package = Package(
            name="P", tools_version="5.5", platforms=[],
            products=[], targets=[], schemes=[], raw_dump={}, staged_dir=staged,
        )
        result = _find_project_modulemap_for_module(package, "M")
        _assert(result is not None, "expected modulemap match")
        _, headers = result
        names = sorted(h.name for h, _rel in headers)
        _assert(names == ["Real.h", "U.h"],
                f"dangling symlink should be filtered out, got {names}")


def _selftest_find_project_modulemap_for_module_preserves_nested_paths() -> None:
    """Modulemaps that reference headers at nested paths (e.g.
    `umbrella header "include/U.h"` or `header "sub/A.h"`) must surface
    those paths through the headers list so the bridge-shim copier can
    mirror them under `Headers/<rel>`. Flattening to basenames silently
    breaks the shim because clang resolves the modulemap's textual
    references against the Headers/ tree at consumer compile time."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / "staged"
        bridge = staged / "bridge"
        include = bridge / "include"
        sub = bridge / "sub"
        include.mkdir(parents=True)
        sub.mkdir(parents=True)
        (bridge / "module.modulemap").write_text(
            'module M {\n'
            '    umbrella header "include/U.h"\n'
            '    header "sub/A.h"\n'
            '    export *\n'
            '}\n'
        )
        (include / "U.h").write_text("// umbrella\n")
        (include / "Sibling.h").write_text("// sibling at include/\n")
        (sub / "A.h").write_text("// explicit nested header\n")
        package = Package(
            name="P", tools_version="5.5", platforms=[],
            products=[], targets=[], schemes=[], raw_dump={}, staged_dir=staged,
        )
        result = _find_project_modulemap_for_module(package, "M")
        _assert(result is not None, "expected modulemap match for M")
        _, headers = result
        rels = sorted(str(rel) for _h, rel in headers)
        _assert(rels == ["include/Sibling.h", "include/U.h", "sub/A.h"],
                f"nested header paths must be preserved relative to modulemap dir, got {rels}")


def _selftest_find_project_modulemap_for_module_returns_none_for_system() -> None:
    """When the requested module name has no matching modulemap in the
    staged tree, return None — the caller is expected to silently treat
    it as a system framework served by the consumer's SDK."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / "staged"
        staged.mkdir(parents=True)
        package = Package(
            name="P", tools_version="5.5", platforms=[],
            products=[], targets=[], schemes=[], raw_dump={}, staged_dir=staged,
        )
        _assert(_find_project_modulemap_for_module(package, "UIKit") is None,
                "expected None for unknown system module")


def _selftest_modules_declared_in_modulemap_picks_top_and_sub() -> None:
    """The declared-modules parser must catch both top-level
    `module X { ... }` and submodule `module X.Y { ... }` forms — and
    `framework module Z { ... }` for already-promoted modulemaps."""
    text = (
        'framework module Top { export * }\n'
        'module Bare { export * }\n'
        'explicit module Top.Sub { header "x.h" }\n'
    )
    declared = _modules_declared_in_modulemap(text)
    _assert("Top" in declared, f"missing Top: {declared}")
    _assert("Bare" in declared, f"missing Bare: {declared}")
    _assert("Top.Sub" in declared, f"missing Top.Sub: {declared}")


def _selftest_detect_framework_type_skips_system_shim_sibling(tmp_root: Path) -> None:
    """Regression test for the issue surfaced by GRDB integration:
    `detect_framework_type` must classify a Swift framework as Swift
    even when it ships with a sibling system Clang module shim framework
    (binary-less, ObjC-shaped) inside the same xcframework slice. The
    shim is identified by the `.spm-to-xcframework-system-shim` sentinel
    file written at injection time and skipped during language tally.

    To make sure we're really exercising the sentinel path and not
    getting the right answer by alphabetical accident, the shim is
    named `AaSysShim.framework` so it sorts BEFORE the primary
    `GRDB.framework`. Without sentinel-based skipping, the walker would
    pick AaSysShim first and report ObjC.
    """
    base = tmp_root / "detect_skips_shim"
    xcfw = base / "GRDB.xcframework"
    for slice_name in ("ios-arm64", "ios-arm64_x86_64-simulator"):
        slice_dir = xcfw / slice_name
        # Primary framework: empty binary stub + Swift surface, no shim
        # sentinel.
        primary = slice_dir / "GRDB.framework"
        primary.mkdir(parents=True)
        (primary / "GRDB").write_bytes(b"\xcf\xfa\xed\xfe")
        swiftmod = primary / "Modules" / "GRDB.swiftmodule"
        swiftmod.mkdir(parents=True)
        (swiftmod / "arm64-apple-ios.swiftinterface").write_text("// fake\n")

        # Sibling shim framework: alphabetically first to defeat the
        # accidental "first wins" path. Marked with the sentinel file.
        shim = slice_dir / "AaSysShim.framework"
        (shim / "Modules").mkdir(parents=True)
        (shim / "Headers").mkdir(parents=True)
        (shim / "Modules" / "module.modulemap").write_text(
            "framework module AaSysShim [system] { header \"shim.h\" }\n"
        )
        (shim / "Headers" / "shim.h").write_text("// shim\n")
        (shim / ".spm-to-xcframework-system-shim").write_text("sentinel\n")

    detected = detect_framework_type(xcfw)
    _assert(
        detected == "Swift",
        f"expected Swift (shim sibling should be skipped via sentinel), got {detected}",
    )


def _selftest_inject_system_clang_modules_warns_on_missing_modulemap(tmp_root: Path) -> None:
    """When the system target's source dir exists but has no
    module.modulemap, inject must skip with a warning rather than
    raising — graceful degradation matches the existing
    inject_objc_headers behavior."""
    base = tmp_root / "inject_sys_no_mm"
    staged = base / "staged"
    sys_src = staged / "Sources" / "BrokenSys"
    sys_src.mkdir(parents=True)
    # No module.modulemap intentionally.
    package = Package(
        name="X", tools_version="6.1.0", platforms=[],
        products=[Product(name="X", linkage=Linkage.AUTOMATIC, targets=["X"])],
        targets=[
            Target(name="BrokenSys", kind=TargetKind.SYSTEM, path=None,
                   public_headers_path=None, dependencies=[], exclude=[]),
            Target(name="X", kind=TargetKind.REGULAR, path="Sources/X",
                   public_headers_path=None, dependencies=["BrokenSys"], exclude=[]),
        ],
        schemes=[], raw_dump={
            "name": "X", "products": [], "targets": [
                {"name": "BrokenSys", "type": "system", "path": None,
                 "publicHeadersPath": None, "dependencies": []},
                {"name": "X", "type": "regular", "path": "Sources/X",
                 "publicHeadersPath": None,
                 "dependencies": [{"target": ["BrokenSys", None]}]},
            ]},
        staged_dir=staged,
    )
    xcfw = base / "X.xcframework"
    (xcfw / "ios-arm64" / "X.framework").mkdir(parents=True)

    n = inject_system_clang_modules(
        xcframework_path=xcfw, package=package,
        source_targets=["X"], verbose=False,
    )
    _assert(n == 0, f"expected 0 injected when modulemap is missing, got {n}")
    _assert(not (xcfw / "ios-arm64" / "BrokenSys.framework").exists(),
            "should not have created a shim framework with no modulemap")


def _selftest_inject_system_clang_modules_preserves_nested_headers(
    tmp_root: Path,
) -> None:
    """Codex [P2] regression: a `.systemLibrary` target whose modulemap
    references a header in a subdirectory (e.g. `header "Sub/shim.h"`)
    must produce a shim framework whose `Headers/` tree mirrors that
    relative path. Before the fix, headers were collected via `iterdir()`
    and copied flat, so the modulemap reference resolved to nothing and
    the shim framework was unconsumable.
    """
    base = tmp_root / "inject_sys_nested_headers"
    staged = base / "staged"
    sys_src = staged / "Sources" / "NestedSys"
    sub = sys_src / "Sub"
    sub.mkdir(parents=True)
    (sys_src / "module.modulemap").write_text(
        "module NestedSys [system] {\n"
        "    header \"Sub/shim.h\"\n"
        "    header \"Top.h\"\n"
        "    export *\n"
        "}\n"
    )
    (sub / "shim.h").write_text("// nested header\n")
    (sys_src / "Top.h").write_text("// top-level header\n")

    package = Package(
        name="X", tools_version="6.1.0", platforms=[],
        products=[Product(name="X", linkage=Linkage.AUTOMATIC, targets=["X"])],
        targets=[
            Target(name="NestedSys", kind=TargetKind.SYSTEM, path=None,
                   public_headers_path=None, dependencies=[], exclude=[]),
            Target(name="X", kind=TargetKind.REGULAR, path="Sources/X",
                   public_headers_path=None, dependencies=["NestedSys"],
                   exclude=[]),
        ],
        schemes=[], raw_dump={
            "name": "X", "products": [], "targets": [
                {"name": "NestedSys", "type": "system", "path": None,
                 "publicHeadersPath": None, "dependencies": []},
                {"name": "X", "type": "regular", "path": "Sources/X",
                 "publicHeadersPath": None,
                 "dependencies": [{"target": ["NestedSys", None]}]},
            ]},
        staged_dir=staged,
    )

    xcfw = base / "X.xcframework"
    (xcfw / "ios-arm64" / "X.framework").mkdir(parents=True)
    (xcfw / "Info.plist").write_text("<plist></plist>")

    n = inject_system_clang_modules(
        xcframework_path=xcfw, package=package,
        source_targets=["X"], verbose=False,
    )
    _assert(n == 1, f"expected 1 system shim injected, got {n}")

    shim_fw = xcfw / "ios-arm64" / "NestedSys.framework"
    _assert(shim_fw.is_dir(), "missing nested-shim framework dir")
    nested = shim_fw / "Headers" / "Sub" / "shim.h"
    _assert(nested.is_file(),
            f"nested header missing — should be at {nested.relative_to(shim_fw)}")
    _assert(nested.read_text() == "// nested header\n",
            "nested header content corrupted")
    top = shim_fw / "Headers" / "Top.h"
    _assert(top.is_file(), "top-level header missing alongside nested header")
    # The modulemap text is preserved verbatim (just promoted to
    # framework form), so the relative `header "Sub/shim.h"` must still
    # match the on-disk layout we just produced.
    mm_text = (shim_fw / "Modules" / "module.modulemap").read_text()
    _assert("header \"Sub/shim.h\"" in mm_text,
            f"nested header reference lost from modulemap:\n{mm_text}")


def _selftest_read_xcframework_library_paths_basic(tmp_root: Path) -> None:
    """Happy path: a real xcframework Info.plist with two slices, each
    pointing at a nested `LibraryPath`. The helper must return the full
    `{identifier: library_path}` map verbatim."""
    base = tmp_root / "read_libpaths_basic"
    xcfw = base / "Foo.xcframework"
    xcfw.mkdir(parents=True)
    info_plist = xcfw / "Info.plist"
    with info_plist.open("wb") as fh:
        plistlib.dump({
            "AvailableLibraries": [
                {
                    "LibraryIdentifier": "ios-arm64",
                    "LibraryPath": "Frameworks/Foo.framework",
                    "BinaryPath": "Frameworks/Foo.framework/Foo",
                    "SupportedArchitectures": ["arm64"],
                    "SupportedPlatform": "ios",
                },
                {
                    "LibraryIdentifier": "ios-arm64_x86_64-simulator",
                    "LibraryPath": "Foo.framework",
                    "BinaryPath": "Foo.framework/Foo",
                    "SupportedArchitectures": ["arm64", "x86_64"],
                    "SupportedPlatform": "ios",
                },
            ],
            "CFBundlePackageType": "XFWK",
            "XCFrameworkFormatVersion": "1.0",
        }, fh)
    paths = _read_xcframework_library_paths(xcfw)
    _assert(
        paths == {
            "ios-arm64": "Frameworks/Foo.framework",
            "ios-arm64_x86_64-simulator": "Foo.framework",
        },
        f"unexpected library paths: {paths!r}",
    )


def _selftest_read_xcframework_library_paths_robust_to_corruption(
    tmp_root: Path,
) -> None:
    """The helper must return an empty dict (caller falls back to
    direct-child scan) for: missing Info.plist, parse error, missing
    AvailableLibraries, malformed entries. None of these may raise."""
    base = tmp_root / "read_libpaths_corrupt"

    # 1. Missing plist entirely.
    xc1 = base / "missing.xcframework"
    xc1.mkdir(parents=True)
    _assert(_read_xcframework_library_paths(xc1) == {},
            "missing plist should return {}")

    # 2. Garbage bytes that crash plistlib.
    xc2 = base / "garbage.xcframework"
    xc2.mkdir(parents=True)
    (xc2 / "Info.plist").write_bytes(b"not a plist at all\x00\x01")
    _assert(_read_xcframework_library_paths(xc2) == {},
            "corrupt plist should return {}")

    # 3. Plist with no AvailableLibraries key.
    xc3 = base / "no_avail.xcframework"
    xc3.mkdir(parents=True)
    with (xc3 / "Info.plist").open("wb") as fh:
        plistlib.dump({"CFBundlePackageType": "XFWK"}, fh)
    _assert(_read_xcframework_library_paths(xc3) == {},
            "plist without AvailableLibraries should return {}")

    # 4. AvailableLibraries entries that are missing or malformed get
    #    skipped, but well-formed siblings still come through.
    xc4 = base / "partial.xcframework"
    xc4.mkdir(parents=True)
    with (xc4 / "Info.plist").open("wb") as fh:
        plistlib.dump({
            "AvailableLibraries": [
                "this is a string, not a dict",
                {"LibraryIdentifier": "ios-arm64"},  # missing LibraryPath
                {"LibraryPath": "Foo.framework"},    # missing identifier
                {"LibraryIdentifier": "ios-x86", "LibraryPath": ""},  # empty
                {
                    "LibraryIdentifier": "ios-arm64_x86_64-simulator",
                    "LibraryPath": "Foo.framework",
                },
            ],
        }, fh)
    paths = _read_xcframework_library_paths(xc4)
    _assert(
        paths == {"ios-arm64_x86_64-simulator": "Foo.framework"},
        f"partial-plist filter wrong: {paths!r}",
    )


def _selftest_pick_primary_framework_in_slice_honors_library_path(
    tmp_root: Path,
) -> None:
    """Codex [P1]: when given an explicit `LibraryPath` from the
    xcframework's `Info.plist`, `_pick_primary_framework_in_slice` must
    resolve to that nested path instead of falling back to the
    direct-child scan (which would miss it entirely and return None)."""
    base = tmp_root / "pick_primary_libpath"
    slice_dir = base / "ios-arm64"
    nested_fw = slice_dir / "Frameworks" / "Foo.framework"
    nested_fw.mkdir(parents=True)

    # Without library_path the direct-child scan returns None — the
    # framework lives nested under Frameworks/.
    _assert(
        _pick_primary_framework_in_slice(slice_dir) is None,
        "direct-child scan should NOT find a nested framework",
    )

    # With library_path the helper resolves directly to the nested path.
    picked = _pick_primary_framework_in_slice(
        slice_dir, library_path="Frameworks/Foo.framework",
    )
    _assert(
        picked == nested_fw,
        f"expected {nested_fw}, got {picked}",
    )

    # Plist disagreement with disk: library_path doesn't resolve, so
    # we should fall back to the direct-child scan rather than silently
    # returning None. Add a top-level framework so the fallback succeeds.
    top_fw = slice_dir / "Bar.framework"
    top_fw.mkdir(parents=True)
    fallback = _pick_primary_framework_in_slice(
        slice_dir, library_path="DoesNotExist/Whatever.framework",
    )
    _assert(
        fallback == top_fw,
        f"expected fallback to {top_fw}, got {fallback}",
    )


def _build_stub_dylib(dst: Path, install_name: str) -> None:
    """Compile a tiny Mach-O dylib at `dst` with `-install_name
    <install_name>` so the framework rename test has a real binary
    `install_name_tool` and `codesign --verify` can operate on. Uses
    `clang` because it's available on every macOS box with Xcode CLI
    tools (which spm-to-xcframework already requires).
    """
    src = dst.parent / f"_stub_{dst.name}.c"
    src.write_text("int spm_to_xcframework_stub(void) { return 0; }\n")
    cp = subprocess.run(
        [
            "clang", "-dynamiclib",
            "-arch", "arm64",
            "-Wl,-install_name," + install_name,
            "-o", str(dst), str(src),
        ],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    src.unlink(missing_ok=True)
    if cp.returncode != 0:
        raise AssertionError(
            f"failed to compile stub dylib for rename test:\n  {cp.stderr.strip()}"
        )


def _write_minimal_info_plist(
    plist_path: Path, *, executable: str, bundle_id: Optional[str] = None
) -> None:
    """Write a minimal binary-format Info.plist mirroring what
    xcodebuild produces for a `dynamic-library` SPM scheme.
    """
    import plistlib
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "CFBundleExecutable": executable,
        "CFBundleName": executable,
        "CFBundleIdentifier": bundle_id if bundle_id is not None else f"org.swift.{executable}",
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundlePackageType": "FMWK",
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "1",
    }
    with plist_path.open("wb") as fh:
        plistlib.dump(data, fh, fmt=plistlib.FMT_BINARY)


def _selftest_rename_framework_bundle_flat(tmp_root: Path) -> None:
    """[Grok final-review Medium #1] End-to-end rename of a flat-style
    framework (iOS/tvOS/watchOS/visionOS layout): bundle dir,
    executable, install_name, Info.plist keys and re-codesign must
    all line up after `rename_framework_bundle`.
    """
    import plistlib
    base = tmp_root / "rename_flat"
    fw = base / "FooDynamic.framework"
    fw.mkdir(parents=True)
    _build_stub_dylib(
        fw / "FooDynamic",
        install_name="@rpath/FooDynamic.framework/FooDynamic",
    )
    _write_minimal_info_plist(fw / "Info.plist", executable="FooDynamic")

    new_fw = rename_framework_bundle(fw, new_name="Foo", verbose=False)

    _assert(new_fw == base / "Foo.framework",
            f"unexpected new path: {new_fw}")
    _assert(new_fw.is_dir(), "renamed framework dir missing")
    _assert(not fw.exists(), "old framework dir should be gone")
    _assert((new_fw / "Foo").is_file(), "renamed inner executable missing")

    with (new_fw / "Info.plist").open("rb") as fh:
        plist = plistlib.load(fh)
    _assert(plist["CFBundleExecutable"] == "Foo",
            f"CFBundleExecutable={plist['CFBundleExecutable']!r}")
    _assert(plist["CFBundleName"] == "Foo",
            f"CFBundleName={plist['CFBundleName']!r}")
    _assert(plist["CFBundleIdentifier"] == "org.swift.Foo",
            f"CFBundleIdentifier should have been rewritten via substring "
            f"replace: {plist['CFBundleIdentifier']!r}")

    # install_name and codesign verify
    otool = subprocess.run(
        ["otool", "-D", str(new_fw / "Foo")],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    _assert(otool.returncode == 0,
            f"otool -D failed: {otool.stderr!r}")
    _assert("@rpath/Foo.framework/Foo" in otool.stdout,
            f"install_name not rewritten; otool said:\n{otool.stdout}")

    verify = subprocess.run(
        ["codesign", "--verify", "--strict", str(new_fw)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    _assert(verify.returncode == 0,
            f"codesign --verify failed:\n{verify.stderr}")


def _selftest_rename_framework_bundle_versioned(tmp_root: Path) -> None:
    """[Grok final-review Medium #1] End-to-end rename of a macOS-style
    versioned framework: the inner executable lives under
    `Versions/A/`, the Info.plist under `Versions/A/Resources/`, and
    the framework root carries symlinks (`<exec>`, `Resources`,
    `Versions/Current`). The rename must swap the executable-name
    symlink, leave `Versions/Current` and `Resources` alone, and the
    re-codesign at the end must verify cleanly.
    """
    import plistlib
    base = tmp_root / "rename_versioned"
    fw = base / "FooDynamic.framework"
    versions_a = fw / "Versions" / "A"
    versions_a.mkdir(parents=True)
    _build_stub_dylib(
        versions_a / "FooDynamic",
        install_name="@rpath/FooDynamic.framework/FooDynamic",
    )
    _write_minimal_info_plist(
        versions_a / "Resources" / "Info.plist",
        executable="FooDynamic",
    )
    # Standard versioned-bundle symlinks.
    (fw / "Versions" / "Current").symlink_to("A")
    (fw / "FooDynamic").symlink_to(Path("Versions") / "Current" / "FooDynamic")
    (fw / "Resources").symlink_to(Path("Versions") / "Current" / "Resources")

    new_fw = rename_framework_bundle(fw, new_name="Foo", verbose=False)

    _assert(new_fw == base / "Foo.framework",
            f"unexpected new path: {new_fw}")
    _assert((new_fw / "Versions" / "A" / "Foo").is_file(),
            "renamed inner executable missing under Versions/A/")
    _assert(not (new_fw / "Versions" / "A" / "FooDynamic").exists(),
            "old inner-executable name should be gone under Versions/A/")
    # Root-level symlink for the executable must point at the NEW name.
    root_exec_link = new_fw / "Foo"
    _assert(root_exec_link.is_symlink(),
            "root <Foo> symlink missing after rename")
    _assert(str(root_exec_link.readlink()) == "Versions/Current/Foo",
            f"root <Foo> symlink target wrong: {root_exec_link.readlink()}")
    _assert(not (new_fw / "FooDynamic").exists(),
            "root old-name symlink should be gone")
    # Content-shaped symlinks are unchanged.
    _assert((new_fw / "Versions" / "Current").is_symlink(),
            "Versions/Current symlink missing")
    _assert(str((new_fw / "Versions" / "Current").readlink()) == "A",
            "Versions/Current target should still be 'A'")
    _assert((new_fw / "Resources").is_symlink(),
            "root Resources symlink missing")

    with (new_fw / "Versions" / "A" / "Resources" / "Info.plist").open("rb") as fh:
        plist = plistlib.load(fh)
    _assert(plist["CFBundleExecutable"] == "Foo",
            f"CFBundleExecutable={plist['CFBundleExecutable']!r}")
    _assert(plist["CFBundleIdentifier"] == "org.swift.Foo",
            f"CFBundleIdentifier={plist['CFBundleIdentifier']!r}")

    verify = subprocess.run(
        ["codesign", "--verify", "--strict", str(new_fw)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    _assert(verify.returncode == 0,
            f"codesign --verify failed:\n{verify.stderr}")


def _selftest_rename_framework_bundle_preserves_custom_bundle_id(
    tmp_root: Path,
) -> None:
    """[Grok final-review Low #1 + Codex r2 Low] When the source
    framework's `CFBundleIdentifier` does NOT have SPM's default
    `org.swift.<scheme>` shape, the renamer must leave it alone —
    replacing it with the new-name-derived form would silently lose
    user state. This covers two flavours that BOTH must be
    preserved:

      (a) An obviously-custom identifier that never mentions the old
          scheme name: `com.acme.proprietary.module`.
      (b) A custom identifier that happens to contain the old scheme
          name as a substring: `com.acme.FooDynamic.module`. The
          original substring-only check would have clobbered this
          one; the tightened `startswith("org.swift.")` guard now
          preserves it.
    """
    import plistlib

    for label, bid in (
        ("acme-proprietary", "com.acme.proprietary.module"),
        ("acme-contains-scheme", "com.acme.FooDynamic.module"),
    ):
        base = tmp_root / f"rename_custom_bid_{label}"
        fw = base / "FooDynamic.framework"
        fw.mkdir(parents=True)
        _build_stub_dylib(
            fw / "FooDynamic",
            install_name="@rpath/FooDynamic.framework/FooDynamic",
        )
        _write_minimal_info_plist(
            fw / "Info.plist",
            executable="FooDynamic",
            bundle_id=bid,
        )

        new_fw = rename_framework_bundle(fw, new_name="Foo", verbose=False)
        with (new_fw / "Info.plist").open("rb") as fh:
            plist = plistlib.load(fh)
        _assert(
            plist["CFBundleIdentifier"] == bid,
            f"[{label}] custom CFBundleIdentifier was clobbered: "
            f"expected {bid!r}, got {plist['CFBundleIdentifier']!r}",
        )


def _selftest_detect_framework_type_nested_library_path(
    tmp_root: Path,
) -> None:
    """Codex [P1] regression: an xcframework whose `Info.plist` declares
    `LibraryPath = Frameworks/Foo.framework` must be classified by
    `detect_framework_type` from the nested framework's contents, not
    silently fall through to "Unknown" because the direct-child scan
    couldn't find anything.
    """
    base = tmp_root / "detect_nested_libpath"
    xcfw = base / "Foo.xcframework"
    xcfw.mkdir(parents=True)
    available = []
    for slice_id in ("ios-arm64", "ios-arm64_x86_64-simulator"):
        nested_fw = xcfw / slice_id / "Frameworks" / "Foo.framework"
        nested_fw.mkdir(parents=True)
        (nested_fw / "Foo").write_bytes(b"\xcf\xfa\xed\xfe" + b"\x00" * 32)
        modules = nested_fw / "Modules" / "Foo.swiftmodule"
        modules.mkdir(parents=True)
        (modules / "arm64.swiftinterface").write_text("// fake\n")
        available.append({
            "LibraryIdentifier": slice_id,
            "LibraryPath": "Frameworks/Foo.framework",
            "BinaryPath": "Frameworks/Foo.framework/Foo",
            "SupportedArchitectures": ["arm64"],
            "SupportedPlatform": "ios",
        })
    with (xcfw / "Info.plist").open("wb") as fh:
        plistlib.dump({
            "AvailableLibraries": available,
            "CFBundlePackageType": "XFWK",
            "XCFrameworkFormatVersion": "1.0",
        }, fh)

    detected = detect_framework_type(xcfw)
    _assert(
        detected == "Swift",
        f"expected Swift (nested LibraryPath should be honored), got {detected}",
    )


def _selftest_verify_one_unit_nested_library_path_enforces_language(
    tmp_root: Path,
) -> None:
    """Codex [P1] regression: a binary-mode (or N/A-language) unit whose
    real framework lives at `Frameworks/Foo.framework` must NOT silently
    pass `_verify_one_unit` as Unknown. Before the fix, the per-slice
    walk used direct-child scanning, missed the nested framework, and
    fell through to `expected_language = "" / "Unknown"` which has no
    surface-check requirements at all. After the fix, the walker uses
    `LibraryPath` from `Info.plist`, sees the Swift surface, and the
    detected type is Swift.
    """
    base = tmp_root / "verify_nested_libpath"
    xcfw = base / "Foo.xcframework"
    xcfw.mkdir(parents=True)
    available = []
    for slice_id in ("ios-arm64", "ios-arm64_x86_64-simulator"):
        nested_fw = xcfw / slice_id / "Frameworks" / "Foo.framework"
        nested_fw.mkdir(parents=True)
        (nested_fw / "Foo").write_bytes(b"\xcf\xfa\xed\xfe" + b"\x00" * 32)
        modules = nested_fw / "Modules" / "Foo.swiftmodule"
        modules.mkdir(parents=True)
        (modules / "arm64.swiftinterface").write_text("// fake\n")
        (modules / "arm64.abi.json").write_text("{}")
        available.append({
            "LibraryIdentifier": slice_id,
            "LibraryPath": "Frameworks/Foo.framework",
            "BinaryPath": "Frameworks/Foo.framework/Foo",
            "SupportedArchitectures": ["arm64"],
            "SupportedPlatform": "ios",
        })
    with (xcfw / "Info.plist").open("wb") as fh:
        plistlib.dump({
            "AvailableLibraries": available,
            "CFBundlePackageType": "XFWK",
            "XCFrameworkFormatVersion": "1.0",
        }, fh)

    unit = ExecutedUnit(
        name="Foo",
        xcframework_path=xcfw,
        framework_name="Foo",
        expected_language=Language.NA,
        is_binary_copy=True,
    )
    saved = tool._check_binary_dynamic
    try:
        tool._check_binary_dynamic = lambda _b: True
        results = verify_output([unit], base)
    finally:
        tool._check_binary_dynamic = saved
    r = results[0]
    _assert(
        r.framework_type == "Swift",
        f"expected detected Swift (nested LibraryPath honored), got {r.framework_type!r}",
    )
    _assert(
        r.passed,
        f"verify should pass on a well-formed nested-LibraryPath Swift unit, "
        f"got fatal_issues={r.fatal_issues!r}",
    )


# Test registry. Tuples of (name, callable, requires_swift). The fast mode
# only runs entries with requires_swift=False.
def _all_tests(tmp_root: Path) -> List[Tuple[str, Callable[[], None], bool]]:
    return [
        ("parse Nuke snapshot", _selftest_parse_nuke, False),
        ("parse GRDB snapshot (system + dynamic)", _selftest_parse_grdb_skips_systems_correctly, False),
        ("parse Alamofire snapshot (already-dynamic)", _selftest_parse_alamofire_already_dynamic, False),
        ("linkage decoder edge cases", _selftest_linkage_decoder, False),
        ("target kind decoder edge cases", _selftest_target_kind_decoder, False),
        ("default target path", _selftest_default_target_path, False),
        ("dependency parser shapes", _selftest_dependency_parser, False),
        ("toxic-entry filter", _selftest_toxic_filter, False),
        ("fetch: _validate_package_source argument-injection guard",
         lambda: _selftest_validate_package_source(tmp_root), False),
        ("fetch: _validate_git_ref tag-shape guard",
         _selftest_validate_git_ref, False),
        ("language scan via swift package describe (real swift)",
         lambda: _selftest_language_counter(tmp_root), True),
        ("scheme resolver", _selftest_scheme_resolver, False),
        ("planner: GRDB (synth_dynamic_library + skip system + leave dynamic alone)",
         _selftest_planner_grdb, False),
        ("planner: Alamofire (regular + already-dynamic over same target)",
         _selftest_planner_alamofire, False),
        ("planner: Stripe (--product + --target synthetic libraries)",
         _selftest_planner_stripe_synthetic_libraries, False),
        ("planner: Stripe3DS2 binary target rejection",
         _selftest_planner_stripe_rejects_binary_target, False),
        ("planner: _is_binary_only_product classifier (issue #39)",
         _selftest_is_binary_only_product_classifier, False),
        ("planner: _package_is_binary_only classifier (issue #39)",
         _selftest_package_is_binary_only, False),
        ("planner: mixed package — skip binary product, plan source (issue #39)",
         _selftest_planner_skips_binary_only_product_in_mixed_package, False),
        ("planner: pure-binary package reaching planner raises PlanError (issue #39)",
         _selftest_planner_pure_binary_package_reaches_planner_raises, False),
        ("planner: executable target rejection (P2)",
         _selftest_planner_rejects_executable_target, False),
        ("planner: duplicate --target filters are deduped (P2)",
         _selftest_planner_duplicate_target_filters_deduped, False),
        ("planner: --target reuses existing .library() product",
         _selftest_planner_target_matching_existing_product_uses_existing, False),
        ("planner: --target reinstates product filtered out by --product",
         _selftest_planner_target_reinstates_filtered_product, False),
        ("planner: unmatched --product filter raises PlanError",
         _selftest_planner_unmatched_product_filter, False),
        ("planner: BlinkID binary filter + copy-artifact strategy",
         _selftest_planner_binary_filter, False),
        ("planner: binary dedupe of duplicate artifacts",
         _selftest_planner_binary_dedupes_duplicate_artifacts, False),
        ("planner: language inference (Swift / ObjC / Mixed)",
         _selftest_planner_language_inference, False),
        ("print_plan label derivation (SCP / URL / local)",
         _selftest_derive_package_label, False),
        ("balanced paren walker basic", _selftest_balanced_close_basic, False),
        ("balanced paren walker strings", _selftest_balanced_close_strings, False),
        ("balanced paren walker comments", _selftest_balanced_close_comments, False),
        ("dedup-overlap: compute_internal_target_deps Stripe transitive closure",
         _selftest_compute_internal_target_deps_stripe, False),
        ("dedup-overlap: compute_internal_target_deps filters external products",
         _selftest_compute_internal_target_deps_filters_external_products, False),
        ("dedup-overlap: compute_internal_target_deps tolerates cycle",
         _selftest_compute_internal_target_deps_tolerates_cycle, False),
        ("dedup-overlap: topo_order_units Stripe umbrella last",
         _selftest_topo_order_units_stripe_umbrella_last, False),
        ("dedup-overlap: topo_order_units stable on independent units",
         _selftest_topo_order_units_stable_on_independent_units, False),
        ("dedup-overlap: _find_target_call_for_name skips .binaryTarget",
         _selftest_find_target_call_for_name_skips_binary_target, False),
        ("dedup-overlap: edit_replace_with_binary_target basic Stripe rewrite",
         _selftest_edit_replace_with_binary_target_basic, False),
        ("dedup-overlap: edit_replace_with_binary_target idempotent",
         _selftest_edit_replace_with_binary_target_idempotent, False),
        ("dedup-overlap: edit_replace_with_binary_target path escaping",
         _selftest_edit_replace_with_binary_target_path_escaping, False),
        ("dedup-overlap: edit_replace_with_binary_target multi-line .target body",
         _selftest_edit_replace_with_binary_target_multiline, False),
        ("dedup-overlap: edit_replace_with_binary_target unknown target raises",
         _selftest_edit_replace_with_binary_target_unknown_raises, False),
        ("dedup-overlap [Codex P1]: _find_target_call_for_name ignores .target(name:) inside dependencies",
         _selftest_find_target_call_for_name_ignores_dependency_target_refs, False),
        ("dedup-overlap [Codex P1]: edit_replace doesn't clobber sibling that references the target in deps",
         _selftest_edit_replace_with_binary_target_dep_ref_does_not_clobber_sibling, False),
        ("dedup-overlap overlay [swift-collections]: first call injects block + wraps targets: arg",
         _selftest_overlay_first_call_injects_block_and_wraps_targets_arg, False),
        ("dedup-overlap overlay [swift-collections]: second call extends block, no re-wrap",
         _selftest_overlay_second_call_extends_block_no_rewrap, False),
        ("dedup-overlap overlay: idempotent for repeated substitution",
         _selftest_overlay_idempotent_same_substitution, False),
        ("dedup-overlap overlay: last path wins on name collision",
         _selftest_overlay_last_path_wins_on_collision, False),
        ("dedup-overlap overlay: sorted output is deterministic across substitution orders",
         _selftest_overlay_sorted_output_is_deterministic, False),
        ("dedup-overlap overlay: path-literal escaping (backslash + quote)",
         _selftest_overlay_escapes_path_correctly, False),
        ("dedup-overlap overlay: empty substitutions + no overlay is a no-op",
         _selftest_overlay_no_substitutions_no_overlay_is_noop, False),
        ("dedup-overlap overlay: missing top-level targets: argument raises",
         _selftest_overlay_missing_targets_arg_raises, False),
        ("dedup-overlap overlay: only ONE Set/array decl after 5 extension calls",
         _selftest_overlay_resulting_manifest_only_one_set_decl, False),
        ("cross-sibling expansion: adds re-exported product (XCTestDynamicOverlay case)",
         lambda: _selftest_cross_sibling_product_expansion(tmp_root), False),
        ("cross-sibling expansion: ignores refs to unknown packages",
         lambda: _selftest_cross_sibling_expansion_ignores_unknown_packages(tmp_root), False),
        ("cross-sibling expansion: chains to fixpoint (A→B→C)",
         lambda: _selftest_cross_sibling_expansion_chains_to_fixpoint(tmp_root), False),
        ("cross-sibling expansion: ignores matches inside strings + comments",
         lambda: _selftest_cross_sibling_expansion_ignores_strings_and_comments(tmp_root), False),
        ("cross-sibling expansion: drops products the owner doesn't declare",
         lambda: _selftest_cross_sibling_expansion_drops_undeclared_products(tmp_root), False),
        ("overlay guard: augments `for target in package.targets where ...` (swift-perception shape)",
         _selftest_overlay_guard_target_loops_swift_perception_shape, False),
        ("overlay guard: no-op without overlay sentinel",
         _selftest_overlay_guard_no_overlay_is_noop, False),
        ("overlay guard: inserts `where` clause when loop has none",
         _selftest_overlay_guard_inserts_where_when_absent, False),
        ("overlay guard: skips already-guarded loops (idempotency)",
         _selftest_overlay_guard_skips_already_guarded, False),
        ("overlay guard: ignores matches inside string literals",
         _selftest_overlay_guard_skips_string_literals, False),
        ("dedup-overlap routing: wrapper-style manifest routed through overlay edit",
         lambda: _selftest_dedup_routes_wrapper_manifest_through_overlay(tmp_root), False),
        ("phantom-helper: edit_augment_target_dependencies appends to nonempty array",
         _selftest_augment_target_deps_appends_to_nonempty_array, False),
        ("phantom-helper: edit_augment_target_dependencies is idempotent",
         _selftest_augment_target_deps_idempotent, False),
        ("phantom-helper: edit_augment_target_dependencies splices into empty array",
         _selftest_augment_target_deps_empty_array, False),
        ("phantom-helper: edit_augment_target_dependencies preserves trailing comma",
         _selftest_augment_target_deps_trailing_comma_array, False),
        ("phantom-helper [Codex/Grok r1 HIGH]: splice lands before trailing // line comment",
         _selftest_augment_target_deps_trailing_line_comment, False),
        ("phantom-helper [Codex/Grok r1 HIGH]: splice lands before trailing /* */ block comment",
         _selftest_augment_target_deps_trailing_block_comment, False),
        ("phantom-helper [Codex/Grok r1]: splice on comment-only interior treats as empty array",
         _selftest_augment_target_deps_comment_only_interior, False),
        ("phantom-helper: edit_augment_target_dependencies empty extras is a no-op",
         _selftest_augment_target_deps_empty_extras_is_noop, False),
        ("phantom-helper: edit_augment_target_dependencies raises on missing target",
         _selftest_augment_target_deps_missing_target_raises, False),
        ("phantom-helper: edit_augment_target_dependencies raises on missing deps arg",
         _selftest_augment_target_deps_no_deps_arg_raises, False),
        ("phantom-helper: edit_augment_target_dependencies raises on non-array deps",
         _selftest_augment_target_deps_non_array_raises, False),
        ("phantom-helper: edit_augment_target_dependencies handles wrapper-style manifest",
         _selftest_augment_target_deps_wrapper_style_manifest, False),
        ("phantom-helper: _compute_phantom_helper_deps finds basic InternalCollectionsUtilities case",
         _selftest_compute_phantom_helper_deps_basic, False),
        ("phantom-helper: _compute_phantom_helper_deps skips already-direct deps",
         _selftest_compute_phantom_helper_deps_skips_direct, False),
        ("phantom-helper: _compute_phantom_helper_deps requires substitution",
         _selftest_compute_phantom_helper_deps_only_substituted_count, False),
        ("phantom-helper: _compute_phantom_helper_deps never returns self",
         _selftest_compute_phantom_helper_deps_skips_self, False),
        ("phantom-helper: _apply_phantom_helper_dep_augmentation end-to-end on staged manifest",
         lambda: _selftest_apply_phantom_helper_dep_augmentation_e2e(tmp_root), False),
        ("phantom-helper: _apply_phantom_helper_dep_augmentation empty input is no-op (no FS access)",
         lambda: _selftest_apply_phantom_helper_dep_augmentation_empty_is_noop(tmp_root), False),
        ("auto-synth-sibling-units: promotes Swift internal helper (swift-collections)",
         _selftest_auto_synth_promotes_swift_internal_helper, False),
        ("auto-synth-sibling-units: skips ObjC internal helper (WCDB regression)",
         _selftest_auto_synth_skips_objc_internal_helper, False),
        ("auto-synth-sibling-units: skips Mixed-language internal helper",
         _selftest_auto_synth_skips_mixed_internal_helper, False),
        ("auto-synth-sibling-units: skips Swift helper with explicit linker settings",
         _selftest_auto_synth_skips_swift_with_explicit_linker_settings, False),
        ("dedup-overlap [Codex P2 r2]: _find_target_call_for_name skips commented-out target decls",
         _selftest_find_target_call_for_name_ignores_commented_calls, False),
        ("dedup-overlap [Codex P2 r2]: _has_binary_target_with_name skips commented-out binary target decls",
         _selftest_has_binary_target_ignores_commented_calls, False),
        ("dedup-overlap [Codex P2 r2]: edit_replace skips commented decls + remains idempotent",
         _selftest_edit_replace_with_binary_target_skips_commented_decl, False),
        ("dedup-overlap [Codex P2 r3]: _find_target_call_for_name skips in-string target decls",
         _selftest_find_target_call_for_name_ignores_string_literals, False),
        ("dedup-overlap [Codex P2 r3]: _has_binary_target_with_name skips in-string binary target decls",
         _selftest_has_binary_target_ignores_string_literals, False),
        ("dedup-overlap [Codex P2 r3]: edit_replace skips string-literal decls + remains idempotent",
         _selftest_edit_replace_with_binary_target_skips_string_literal, False),
        ("dedup-overlap [Codex P2 r3]: _make_code_token_view blanks strings + comments, preserves offsets",
         _selftest_make_code_token_view_blanks_strings_and_comments, False),
        ("dedup-overlap [Codex P2 r4]: _apply_dedup_overlap_substitutions guards raw-string constructs",
         lambda: _selftest_apply_dedup_overlap_substitutions_guards_unsupported_constructs(tmp_root), False),
        ("dedup-overlap: _apply_dedup_overlap_substitutions handles triple-quoted strings",
         lambda: _selftest_apply_dedup_overlap_substitutions_handles_triple_quoted(tmp_root), False),
        ("prepare: _skip_triple_quoted_string handles \\(...) interpolation (codex-review r1)",
         _selftest_skip_triple_quoted_string_handles_interpolation, False),
        ("dedup-overlap: triple-quoted interpolation nested triple-quoted (codex-review r1)",
         lambda: _selftest_apply_dedup_overlap_substitutions_triple_quoted_interpolation(tmp_root), False),
        ("prepare: depth-aware walkers dispatch triple-quoted before single-quoted (grok-cli-review r1)",
         _selftest_walkers_dispatch_triple_quoted_before_single_quoted, False),
        ("prepare: rawstring guard ignores #\" mention inside triple-quoted body (codex-review r1)",
         _selftest_unsupported_swift_constructs_allows_rawstring_mention_in_triple_quoted_body, False),
        ("prune-child: Pass B strips .testTarget(...) entries (TCA → swift-perception, 2026-05-21)",
         _selftest_prune_child_strips_test_targets, False),
        ("dedup-overlap [Codex r3]: edit_demote_synthetic_product strips type: .dynamic (SPM multiline shape)",
         _selftest_edit_demote_synthetic_product_spm_multiline, False),
        ("dedup-overlap [Codex r3]: edit_demote_synthetic_product handles single-line library",
         _selftest_edit_demote_synthetic_product_single_line, False),
        ("dedup-overlap [Codex r3]: edit_demote_synthetic_product is idempotent on automatic shape",
         _selftest_edit_demote_synthetic_product_idempotent, False),
        ("dedup-overlap [Codex r3]: edit_demote_synthetic_product raises on unknown product",
         _selftest_edit_demote_synthetic_product_unknown_raises, False),
        ("dedup-overlap [Codex r3]: edit_demote_synthetic_product targets library, not target, on name collision",
         _selftest_edit_demote_synthetic_product_ignores_target_name, False),
        ("dedup-overlap [Codex r3]: demote round-trips through swift package dump-package",
         lambda: _selftest_edit_demote_synthetic_product_dumppackage_roundtrip(tmp_root), True),
        ("dedup-overlap [Codex P2 r5]: _compute_dedup_substitutions substitutes single-target siblings",
         _selftest_compute_dedup_substitutions_single_target_sibling, False),
        ("dedup-overlap [Codex P2 r5]: _compute_dedup_substitutions skips MULTI-target siblings",
         _selftest_compute_dedup_substitutions_multi_target_sibling_skipped, False),
        ("dedup-overlap [Codex P2 r5]: _compute_dedup_substitutions handles aliased single-target siblings",
         _selftest_compute_dedup_substitutions_aliased_single_target, False),
        ("dedup-overlap [Codex P2 r5]: _compute_dedup_substitutions skips siblings not yet built",
         _selftest_compute_dedup_substitutions_unbuilt_sibling_skipped, False),
        ("dedup-overlap [Codex P2 r5]: _compute_dedup_substitutions mixes single-target sub + multi-target skip",
         _selftest_compute_dedup_substitutions_mixes_single_and_multi, False),
        ("dedup-overlap [Codex final-review]: synth_dynamic_library targets are skipped (binary-product invariant)",
         _selftest_compute_dedup_substitutions_synth_dynamic_target_skipped, False),
        ("dedup-overlap [Codex final-review]: _synth_dynamic_protected_targets helper excludes synth_library",
         _selftest_synth_dynamic_protected_targets_helper, False),
        ("dedup-overlap [Codex P2]: external .product collision must not become an internal edge",
         _selftest_compute_internal_target_deps_external_product_collision, False),
        ("dedup-overlap [Codex P2]: mixed internal byName + external product with same name",
         _selftest_compute_internal_target_deps_mixed_internal_and_external, False),
        ("xcresulttool build-results parser",
         _selftest_parse_xcresult_build_results, False),
        ("diagnostics: scan matches known xcodebuild failure patterns",
         _selftest_diagnostics_scan_known_patterns, False),
        ("diagnostics: swift-package shaping extracts tools-version mismatch",
         _selftest_diagnostics_format_swift_package_failure_tools_version, False),
        ("diagnostics [Codex review P2]: tools-version too-new hint points at Xcode upgrade",
         _selftest_diagnostics_format_swift_package_failure_tools_version_too_new, False),
        ("diagnostics: swift-package shaping degrades silently on unknown stderr",
         _selftest_diagnostics_format_swift_package_failure_unknown_shape, False),
        ("diagnostics: swift-package shaping handles empty stderr",
         _selftest_diagnostics_format_swift_package_failure_empty_stderr, False),
        ("diagnostics: _format_execute_error prepends diagnosis above raw errors",
         _selftest_diagnostics_format_execute_error_prepends_block, False),
        ("unsupported Swift constructs guard",
         _selftest_unsupported_swift_constructs, False),
        ("unsupported Swift constructs guard: comment-aware stripping",
         _selftest_unsupported_swift_constructs_comment_aware, False),
        ("error taxonomy: User vs Bug subclasses routed correctly",
         _selftest_error_taxonomy_split, False),
        ("active manifest selector (Package@swift-X.Y)",
         _selftest_select_active_manifest, False),
        ("multi-platform: _PLATFORM_SLICES table integrity (clang flags)",
         _selftest_platform_slices_table_integrity, False),
        ("multi-platform: _selected_slices order & filter",
         _selftest_selected_slices_order_and_filter, False),
        ("multi-platform: --no-ios drops both iOS slices",
         _selftest_selected_slices_no_ios_path, False),
        ("autodetect: multi-platform package fills all declared --min-* fields",
         _selftest_autodetect_multi_platform_package, False),
        ("autodetect: any explicit --min-* flag suppresses auto-detect (no mixing)",
         _selftest_autodetect_explicit_flag_is_noop, False),
        ("autodetect: package with no platforms: falls back to iOS 15.0",
         _selftest_autodetect_no_platforms_declared_falls_back_to_ios_15, False),
        ("autodetect: --no-ios + iOS-only package yields empty (no fallback)",
         _selftest_autodetect_no_ios_with_ios_only_package_yields_empty, False),
        ("autodetect: --no-ios + multi-platform package skips iOS, fills others",
         _selftest_autodetect_no_ios_with_multi_platform_skips_ios_only, False),
        ("autodetect: unknown platform names are silently ignored",
         _selftest_autodetect_ignores_unknown_platform_names, False),
        ("autodetect: empty-version platform entries are silently skipped",
         _selftest_autodetect_skips_platform_with_empty_version, False),
        ("multi-platform: binary shim emits string-form .Plat(\"X.Y\") entries",
         _selftest_spm_platform_entries_string_form, False),
        ("multi-platform: per-package validation rejects undeclared platforms",
         _selftest_validate_requested_platforms_rejects_undeclared, False),
        ("multi-platform: per-package validation passes when package declares no platforms",
         _selftest_validate_requested_platforms_empty_passes_through, False),
        ("multi-platform: _parse_platforms recognises all six platformName values",
         _selftest_parse_platforms_recognises_all_six_names, False),
        ("multi-platform: _platform_from_library_identifier maps Apple xcframework slices",
         _selftest_platform_from_library_identifier, False),
        ("multi-platform: variant classifier round-trips PlatformSlice ↔ LibraryIdentifier",
         _selftest_variant_classification_round_trips, False),
        ("multi-platform: _expected_slice_classes expands device/simulator pairs",
         _selftest_expected_slice_classes_pairs, False),
        ("multi-platform: verify rejects xcframework missing a requested platform",
         lambda: _selftest_verify_coverage_rejects_missing_platform(tmp_root), False),
        ("multi-platform: verify rejects device-only when simulator was requested (Codex r3)",
         lambda: _selftest_verify_coverage_rejects_missing_simulator(tmp_root), False),
        ("multi-platform: verify passes when every requested slice present",
         lambda: _selftest_verify_coverage_passes_when_all_requested_present(tmp_root), False),
        ("execute: slice paths are unique", _selftest_slice_paths_unique, False),
        ("execute: detect_framework_type Swift/ObjC/Mixed/Bridge",
         lambda: _selftest_detect_framework_type_swift_objc_mixed(tmp_root), False),
        ("execute: inject_objc_headers umbrella + idempotency",
         lambda: _selftest_inject_objc_headers_with_umbrella(tmp_root), False),
        ("execute: inject_objc_headers explicit modulemap",
         lambda: _selftest_inject_objc_headers_explicit_modulemap(tmp_root), False),
        ("execute: inject_objc_headers preserves nested subpaths (P2)",
         lambda: _selftest_inject_objc_headers_preserves_nested_subpaths(tmp_root), False),
        ("execute: inject_objc_headers nested + no umbrella modulemap",
         lambda: _selftest_inject_objc_headers_nested_no_umbrella(tmp_root), False),
        ("execute: inject_resource_bundles copies SwiftPM bundle into both slices",
         lambda: _selftest_inject_resource_bundles_copies_swiftpm_bundle(tmp_root), False),
        ("execute: inject_resource_bundles is a no-op when target has no resources",
         lambda: _selftest_inject_resource_bundles_no_resources_is_noop(tmp_root), False),
        ("execute: inject_resource_bundles is idempotent / refreshes stale copies",
         lambda: _selftest_inject_resource_bundles_idempotent(tmp_root), False),
        ("execute: _find_resource_bundles dedupes ArchiveIntermediates vs Build/Products",
         lambda: _selftest_find_resource_bundles_dedupe_and_filter(tmp_root), False),
        ("execute: ObjC headers dir priority (fw_name > product_name > any)",
         lambda: _selftest_find_objc_headers_dir_priority(tmp_root), False),
        ("execute: ObjC headers dir follows .target() dep edge (P1)",
         lambda: _selftest_find_objc_headers_dir_follows_target_edge(tmp_root), False),
        ("execute: ObjC headers dir defaults to include/ when publicHeadersPath omitted",
         lambda: _selftest_find_objc_headers_dir_defaults_to_include(tmp_root), False),
        ("execute: ObjC headers dir picks umbrella-at-target-root (Stripe pattern)",
         lambda: _selftest_find_objc_headers_dir_umbrella_at_target_root(tmp_root), False),
        ("execute: ObjC headers dir accepts <Target>-umbrella.h variant",
         lambda: _selftest_find_objc_headers_dir_umbrella_named_variant(tmp_root), False),
        ("execute: ObjC headers dir rejects unrelated top-level .h",
         lambda: _selftest_find_objc_headers_dir_umbrella_rejects_unrelated_h(tmp_root), False),
        ("execute: ObjC headers dir umbrella branch fires on Swift-classified target (Stripe)",
         lambda: _selftest_find_objc_headers_dir_umbrella_swift_classified_still_fires(tmp_root), False),
        ("execute: ObjC headers dir dep walk does not leak implicit include/",
         lambda: _selftest_find_objc_headers_dir_dep_walk_no_implicit_leak(tmp_root), False),
        ("execute: ObjC headers dir dep walk folds internal default include/",
         lambda: _selftest_find_objc_headers_dir_dep_walk_internal_default_include(tmp_root), False),
        ("execute: archive framework path recursive search",
         lambda: _selftest_archive_framework_path_recursive(tmp_root), False),
        ("execute: promote_modulemap_to_framework_form (GRDBSQLite shape)",
         _selftest_promote_modulemap_to_framework_form_grdb, False),
        ("execute: promote_modulemap_to_framework_form preserves indent",
         _selftest_promote_modulemap_to_framework_form_indented, False),
        ("execute: walk_system_library_target_deps GRDB → GRDBSQLite",
         _selftest_walk_system_library_target_deps_grdb, False),
        ("execute: walk_system_library_target_deps no-op for system-free packages",
         _selftest_walk_system_library_target_deps_no_system, False),
        ("execute: walk_system_library_target_deps follows transitive chain",
         _selftest_walk_system_library_target_deps_transitive, False),
        ("execute: walk_system_library_target_deps dedupes diamond",
         _selftest_walk_system_library_target_deps_dedupes_diamond, False),
        ("execute: system_target_source_dir uses Sources/<name> default",
         lambda: _selftest_system_target_source_dir_default(tmp_root), False),
        ("execute: system_target_source_dir honors explicit path:",
         lambda: _selftest_system_target_source_dir_explicit(tmp_root), False),
        ("execute: system_target_source_dir returns None on missing dir",
         lambda: _selftest_system_target_source_dir_missing(tmp_root), False),
        ("execute: _framework_content_root flat vs versioned",
         lambda: _selftest_framework_content_root_flat_vs_versioned(tmp_root), False),
        ("execute: _ensure_root_symlink creates relative symlink on versioned framework",
         lambda: _selftest_ensure_root_symlink_versioned(tmp_root), False),
        ("execute: _ensure_root_symlink migrates real root dir into Versions/A",
         lambda: _selftest_ensure_root_symlink_migrates_real_dir(tmp_root), False),
        ("execute: inject_swiftmodule on macOS versioned framework writes under Versions/A (MACOS_SLICE_BUG)",
         lambda: _selftest_inject_swiftmodule_versioned_macos(tmp_root), False),
        ("execute: inject_swiftmodule on flat (iOS) framework keeps flat layout (regression)",
         lambda: _selftest_inject_swiftmodule_flat_unchanged(tmp_root), False),
        ("execute: inject_resource_bundles on macOS versioned framework lands under Versions/A/Resources",
         lambda: _selftest_inject_resource_bundles_versioned_macos(tmp_root), False),
        ("execute: inject_system_clang_modules end-to-end (GRDB shape)",
         lambda: _selftest_inject_system_clang_modules_grdb_shape(tmp_root), False),
        ("execute: inject_system_clang_modules idempotent on second call",
         lambda: _selftest_inject_system_clang_modules_idempotent(tmp_root), False),
        ("execute: inject_system_clang_modules no-op when no system deps",
         lambda: _selftest_inject_system_clang_modules_no_system_deps(tmp_root), False),
        ("execute: inject_system_clang_modules warns on missing modulemap",
         lambda: _selftest_inject_system_clang_modules_warns_on_missing_modulemap(tmp_root), False),
        ("execute: inject_system_clang_modules preserves nested headers (P2)",
         lambda: _selftest_inject_system_clang_modules_preserves_nested_headers(tmp_root), False),
        ("execute: scan_swiftinterface_imports normalizes dotted imports",
         lambda: _selftest_scan_swiftinterface_imports_normalizes_dotted(tmp_root), False),
        ("execute: inject_bridge_clang_modules end-to-end (WCDB shape)",
         lambda: _selftest_inject_bridge_clang_modules_wcdb_shape(tmp_root), False),
        ("execute: inject_bridge_clang_modules idempotent on second call",
         lambda: _selftest_inject_bridge_clang_modules_idempotent(tmp_root), False),
        ("execute: inject_bridge_clang_modules no-op when no missing imports",
         lambda: _selftest_inject_bridge_clang_modules_no_missing_imports(tmp_root), False),
        ("execute: inject_bridge_clang_modules skips already-declared module",
         lambda: _selftest_inject_bridge_clang_modules_skips_already_declared(tmp_root), False),
        ("execute: find_project_modulemap_for_module resolves umbrella header",
         _selftest_find_project_modulemap_for_module_resolves_umbrella, False),
        ("execute: find_project_modulemap_for_module symlinked umbrella (WCDB shape)",
         _selftest_find_project_modulemap_for_module_symlinked_umbrella, False),
        ("execute: find_project_modulemap_for_module skips dangling symlinks",
         _selftest_find_project_modulemap_for_module_skips_dangling_symlinks, False),
        ("execute: find_project_modulemap_for_module preserves nested header paths",
         _selftest_find_project_modulemap_for_module_preserves_nested_paths, False),
        ("execute: find_project_modulemap_for_module returns None for system framework",
         _selftest_find_project_modulemap_for_module_returns_none_for_system, False),
        ("execute: modules_declared_in_modulemap picks top + sub modules",
         _selftest_modules_declared_in_modulemap_picks_top_and_sub, False),
        ("execute: detect_framework_type skips system shim sibling (GRDB)",
         lambda: _selftest_detect_framework_type_skips_system_shim_sibling(tmp_root), False),
        ("execute: read_xcframework_library_paths basic happy path",
         lambda: _selftest_read_xcframework_library_paths_basic(tmp_root), False),
        ("execute: read_xcframework_library_paths robust to corrupt plist",
         lambda: _selftest_read_xcframework_library_paths_robust_to_corruption(tmp_root), False),
        ("execute: pick_primary_framework_in_slice honors LibraryPath (P1)",
         lambda: _selftest_pick_primary_framework_in_slice_honors_library_path(tmp_root), False),
        ("execute: rename_framework_bundle flat layout end-to-end (Grok M1)",
         lambda: _selftest_rename_framework_bundle_flat(tmp_root), False),
        ("execute: rename_framework_bundle versioned layout end-to-end (Grok M1)",
         lambda: _selftest_rename_framework_bundle_versioned(tmp_root), False),
        ("execute: rename_framework_bundle preserves custom CFBundleIdentifier (Grok L1)",
         lambda: _selftest_rename_framework_bundle_preserves_custom_bundle_id(tmp_root), False),
        ("execute: detect_framework_type honors nested LibraryPath (P1)",
         lambda: _selftest_detect_framework_type_nested_library_path(tmp_root), False),
        ("verify: nested LibraryPath unit enforces language surface (P1)",
         lambda: _selftest_verify_one_unit_nested_library_path_enforces_language(tmp_root), False),
        ("verify: _format_size_iec K/M/G boundaries",
         _selftest_format_size_iec, False),
        ("verify: happy path Swift xcframework",
         lambda: _selftest_verify_happy_path_swift(tmp_root), False),
        ("verify: happy path ObjC xcframework",
         lambda: _selftest_verify_happy_path_objc(tmp_root), False),
        ("promote: no-op when all slices already dynamic (issue #39)",
         lambda: _selftest_promote_binary_no_op_when_all_slices_dynamic(tmp_root), False),
        ("promote: missing Info.plist fails soft (issue #39)",
         lambda: _selftest_promote_binary_missing_info_plist_returns_empty(tmp_root), False),
        ("promote: corrupt Info.plist fails soft (issue #39)",
         lambda: _selftest_promote_binary_corrupt_info_plist_returns_empty(tmp_root), False),
        ("promote: _platform_slice_for_library_identifier (issue #39)",
         _selftest_platform_slice_for_library_identifier, False),
        ("promote: _slice_minimum_deployment_target (issue #39)",
         _selftest_slice_minimum_deployment_target, False),
        ("verify: corrupt Info.plist (__MACOSX ghost)",
         lambda: _selftest_verify_corrupt_info_plist(tmp_root), False),
        ("verify: missing xcframework directory",
         lambda: _selftest_verify_missing_xcframework(tmp_root), False),
        ("verify: single-slice xcframework accepted (multi-platform)",
         lambda: _selftest_verify_one_slice_only(tmp_root), False),
        ("verify: static binary fails dynamic-link check",
         lambda: _selftest_verify_static_binary(tmp_root), False),
        ("verify: Swift framework with no .swiftinterface",
         lambda: _selftest_verify_swift_no_swiftinterface(tmp_root), False),
        ("verify: ObjC framework with no module.modulemap",
         lambda: _selftest_verify_objc_no_modulemap(tmp_root), False),
        ("verify: print_verify_summary format (pass + fail)",
         lambda: _selftest_verify_summary_format(tmp_root), False),
        ("verify: BinaryPath fallback (Info.plist omits BinaryPath)",
         lambda: _selftest_verify_binary_path_fallback(tmp_root), False),
        ("verify: malformed AvailableLibraries shapes",
         lambda: _selftest_verify_malformed_available_libraries(tmp_root), False),
        ("verify: missing output_dir raises VerifyError",
         lambda: _selftest_verify_missing_output_dir(tmp_root), False),
        ("cli: relative --output is resolved to absolute (dedup-overlap path fix)",
         _selftest_cli_resolves_relative_output_dir, False),
        ("verify: Mixed-expected unit without ObjC surface must fail (P1)",
         lambda: _selftest_verify_mixed_losing_objc_surface_fails(tmp_root), False),
        ("verify: Mixed-expected unit without Swift surface must fail",
         lambda: _selftest_verify_mixed_losing_swift_surface_fails(tmp_root), False),
        ("verify: empty/NA expected_language falls back to post-hoc detection",
         lambda: _selftest_verify_expected_language_na_falls_back(tmp_root), False),
        ("verify: _finalize_with_verify threads expected_lang into deps (Codex follow-up)",
         lambda: _selftest_finalize_threads_expected_lang_into_deps(tmp_root), False),
        ("verify: dep dedupe upgrades to more-specific expected_language",
         lambda: _selftest_finalize_dep_dedup_upgrades_expected_lang(tmp_root), False),
        ("manifest: basename-only entry guard",
         _selftest_manifest_entry_basename_guard, False),
        ("manifest: cleans stale primary after verify passes",
         lambda: _selftest_manifest_cleans_stale_primary(tmp_root), False),
        ("manifest: cleans stale dep across --include-deps boundary (Codex P1)",
         lambda: _selftest_manifest_cleans_stale_dep_across_include_deps_boundary(tmp_root), False),
        ("manifest: cleanup deferred on verify failure (Codex P1-v2)",
         lambda: _selftest_manifest_cleanup_deferred_on_verify_failure(tmp_root), False),
        ("manifest: user-owned files untouched",
         lambda: _selftest_manifest_user_files_untouched(tmp_root), False),
        ("manifest: empty output directory first run",
         lambda: _selftest_manifest_empty_directory(tmp_root), False),
        ("manifest: missing manifest → no provenance → no cleanup",
         lambda: _selftest_manifest_missing_manifest_no_cleanup(tmp_root), False),
        ("manifest: malformed manifest treated as absent",
         lambda: _selftest_manifest_malformed_manifest(tmp_root), False),
        ("manifest: corrupt entry filtering (basenames-only schema lock)",
         lambda: _selftest_manifest_corrupt_entry_filtering(tmp_root), False),
        ("manifest: --no-cleanup-stale preserves AND keeps tracking",
         lambda: _selftest_manifest_no_cleanup_stale_preserves_and_tracks(tmp_root), False),
        ("MiniMixed fetch+stage+inspect (real swift)", _selftest_minimixed_fetch_integration, True),
        ("round-trip: GRDB (synth_dynamic_library + skip system)", _roundtrip_grdb, True),
        ("round-trip: Alamofire (synth_dynamic_library with collision-aware naming)",
         _roundtrip_alamofire, True),
        ("round-trip: Alamofire multi-manifest layout (Package.swift wins over @swift-5.10)",
         _roundtrip_alamofire_multi_manifest_layout, True),
        ("round-trip: Stripe (synth_dynamic_library + synth_library)",
         _roundtrip_stripe_force_dynamic_and_synthetic, True),
        ("round-trip: system library left alone",
         _roundtrip_system_library_left_alone, True),
        ("round-trip: validator surfaces unknown-target as clean PrepareError",
         _roundtrip_validator_catches_missing_product, True),
        ("round-trip: full prepare() flow on GRDB", _roundtrip_full_prepare_grdb, True),
        ("round-trip: Foo minimal fixture synth_dynamic_library", _roundtrip_foo_force_dynamic, True),
    ]


def run_self_test(fast: bool) -> int:
    """Run all (or fast-mode) self-tests. Returns shell exit code."""
    bold(f"Running self-test ({'fast' if fast else 'full'})...")
    passed = 0
    failed = 0
    failures: List[Tuple[str, str]] = []
    with tempfile.TemporaryDirectory(prefix="spm2xc-selftest-tmp-") as tmp:
        tmp_root = Path(tmp)
        for name, fn, needs_swift in _all_tests(tmp_root):
            if fast and needs_swift:
                dim(f"  - {name} ... SKIP (fast mode)")
                continue
            try:
                fn()
                success(f"  ✓ {name}")
                passed += 1
            except Exception as exc:
                print(_wrap(f"  ✗ {name}: {exc}", "red"))
                failures.append((name, str(exc)))
                failed += 1
    print()
    if failed:
        print(_wrap(f"{passed} passed, {failed} failed", "red"))
        for name, msg in failures:
            print(_wrap(f"  - {name}: {msg}", "red"))
        return 1
    success(f"{passed} passed, 0 failed")
    return 0




# --- Sync-check test ------------------------------------------------------
#
# The committed root `spm-to-xcframework` is a byte-for-byte copy of
# `src/spm_to_xcframework.py`, produced by `src/build_single_file.py`.
# Any drift between the two must fail the test suite loudly so a stale
# artifact can never reach users.

def _selftest_root_artifact_matches_source() -> None:
    """The committed root `spm-to-xcframework` must be byte-for-byte
    identical to `src/spm_to_xcframework.py`. If they differ, the
    builder hasn't been run since the last edit — regenerate with
    `python3 src/build_single_file.py`.
    """
    here = Path(__file__).resolve().parent
    src = here / "spm_to_xcframework.py"
    dst = here.parent / "spm-to-xcframework"
    _assert(src.is_file(), f"source module missing: {src}")
    _assert(dst.is_file(), f"root artifact missing: {dst}")
    src_bytes = src.read_bytes()
    dst_bytes = dst.read_bytes()
    _assert(
        src_bytes == dst_bytes,
        "committed root `spm-to-xcframework` is out of sync with "
        "`src/spm_to_xcframework.py` — run `python3 src/build_single_file.py` "
        f"to regenerate (source={len(src_bytes)}B, artifact={len(dst_bytes)}B)",
    )


# Re-export the test registry with the sync-check appended. We can't just
# mutate the list the existing `_all_tests` returns because callers expect
# a stable shape — make the append explicit here.
_base_all_tests = _all_tests


def _all_tests_with_sync(tmp_root: Path) -> List[Tuple[str, Callable[[], None], bool]]:
    tests = list(_base_all_tests(tmp_root))
    tests.append((
        "sync-check: committed spm-to-xcframework == src/spm_to_xcframework.py",
        _selftest_root_artifact_matches_source,
        False,
    ))
    return tests


# Shadow the original registry so `run_self_test` picks up the sync-check
# test. This keeps the runner logic untouched.
_all_tests = _all_tests_with_sync  # type: ignore[assignment]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="spm_to_xcframework_tests",
        description="Developer self-test suite for spm-to-xcframework.",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Skip tests that require the swift toolchain.",
    )
    ns = parser.parse_args(argv)
    return run_self_test(fast=ns.fast)


if __name__ == "__main__":
    sys.exit(main())
