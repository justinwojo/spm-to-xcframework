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
    _archive_framework_path,
    _archive_static_lib_path,
    _assert_no_unsupported_swift_constructs,
    _balanced_close,
    _BUG_CLASS_ERRORS,
    _check_binary_dynamic,
    _count_source_files,
    _default_target_path,
    _derive_package_label,
    _find_library_call_for_product,
    _find_objc_headers_dir,
    _finalize_with_verify,
    _format_size_iec,
    _is_toxic_entry,
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
    """Synthetic file tree → confirm Swift / ObjC / Mixed / NA classify
    correctly. Uses a temp dir, no fixture file dependency."""
    base = tmp_root / "lang_counter"
    base.mkdir()

    swift_only = base / "swift_only"
    swift_only.mkdir()
    (swift_only / "Foo.swift").write_text("// swift")
    s, o = _count_source_files(swift_only)
    _assert(s == 1 and o == 0, f"swift-only got ({s},{o})")

    objc_only = base / "objc_only"
    objc_only.mkdir()
    (objc_only / "Bar.m").write_text("// m")
    (objc_only / "Bar.h").write_text("// h")  # .h counts toward objc per design §5.1
    s, o = _count_source_files(objc_only)
    _assert(s == 0 and o == 2, f"objc-only got ({s},{o})")

    # Header-only ObjC target (umbrella framework shape).
    headers_only = base / "headers_only"
    headers_only.mkdir()
    (headers_only / "Public.h").write_text("// h")
    s, o = _count_source_files(headers_only)
    _assert(s == 0 and o == 1, f"headers-only got ({s},{o})")

    mixed = base / "mixed"
    mixed.mkdir()
    (mixed / "X.swift").write_text("// swift")
    (mixed / "Y.mm").write_text("// mm")
    s, o = _count_source_files(mixed)
    _assert(s == 1 and o == 1, f"mixed got ({s},{o})")

    # Tests/ skip pruning
    skipper = base / "skipper"
    skipper.mkdir()
    (skipper / "X.swift").write_text("// swift")
    tests = skipper / "Tests"
    tests.mkdir()
    (tests / "Junk.swift").write_text("// swift")
    s, o = _count_source_files(skipper)
    _assert(s == 1 and o == 0, f"skip-tests got ({s},{o}); Tests/ should be pruned")


def _selftest_minimixed_fetch_integration() -> None:
    """Full Fetch integration against testdata/MiniMixed.

    Verifies the inclusion-by-default + toxic-exclusion staging rule:
      - Sources/, Package.swift, etc. land in staged/
      - MiniMixed.xcodeproj is excluded
      - Sources/MiniSwift/Excluded.txt (per package's `exclude:` list)
        gets removed by the second pass
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

        # Inspect against the staged dir (the test exercises the full Fetch
        # → Inspect contract — including dump-package, language scan, and
        # scheme discovery — against a real swift toolchain).
        pkg = inspect_package(config, staged)
        _assert(pkg.name == "MiniMixed", f"package name was {pkg.name!r}")
        _assert(len(pkg.products) == 3, f"expected 3 products, got {len(pkg.products)}")
        by_name = {p.name: p for p in pkg.products}
        _assert("MiniSwift" in by_name and "MiniObjC" in by_name and "MiniMixed" in by_name,
                f"missing expected product, got {list(by_name.keys())}")
        for p in pkg.products:
            _assert(p.linkage == Linkage.AUTOMATIC, f"{p.name} linkage was {p.linkage!r}")

        # Language classification
        lang_by_target = {t.name: t.language for t in pkg.targets}
        _assert(lang_by_target.get("MiniSwift") == Language.SWIFT,
                f"MiniSwift language was {lang_by_target.get('MiniSwift')!r}")
        _assert(lang_by_target.get("MiniObjC") == Language.OBJC,
                f"MiniObjC language was {lang_by_target.get('MiniObjC')!r}")
        _assert(lang_by_target.get("MiniMixed") == Language.MIXED,
                f"MiniMixed language was {lang_by_target.get('MiniMixed')!r}")


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
    """GRDB: force_dynamic on GRDB, NO force_dynamic on GRDB-dynamic,
    GRDBSQLite dropped entirely because it's a system-target wrapper.
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

    edits = {(e.kind, e.product_name) for e in plan.package_swift_edits}
    _assert(
        ("force_dynamic", "GRDB") in edits,
        f"force_dynamic on GRDB missing; got {edits}",
    )
    _assert(
        ("force_dynamic", "GRDB-dynamic") not in edits,
        f"must not force_dynamic the already-dynamic GRDB-dynamic; got {edits}",
    )
    _assert(
        ("force_dynamic", "GRDBSQLite") not in edits,
        "must not emit force_dynamic for the skipped system product",
    )

    skipped_names = [n for n, _ in plan.skipped]
    _assert(
        skipped_names == ["GRDBSQLite"],
        f"expected GRDBSQLite in skipped list; got {plan.skipped}",
    )

    # Scheme resolution: GRDB should pick the literal scheme, not
    # GRDB-Package. This is the session-1 inversion noted in the brief.
    grdb_bu = next(bu for bu in plan.build_units if bu.name == "GRDB")
    _assert(
        grdb_bu.scheme == "GRDB",
        f"GRDB scheme should be literal 'GRDB' not '{grdb_bu.scheme}'",
    )


def _selftest_planner_alamofire() -> None:
    """Alamofire-shape: two products over the same target — plan both,
    but only force_dynamic the non-dynamic one."""
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

    edits = {(e.kind, e.product_name) for e in plan.package_swift_edits}
    _assert(
        ("force_dynamic", "Alamofire") in edits,
        f"force_dynamic on Alamofire; got {edits}",
    )
    _assert(
        ("force_dynamic", "AlamofireDynamic") not in edits,
        f"must not force_dynamic the already-dynamic AlamofireDynamic; got {edits}",
    )


def _selftest_planner_stripe_synthetic_libraries() -> None:
    """Stripe: --product Stripe narrows the product set to one, --target
    StripeCore / --target StripeUICore add two synthetic libraries, for
    three build units total.
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
        names == {"Stripe", "StripeCore", "StripeUICore"},
        f"expected 3 build units {{Stripe, StripeCore, StripeUICore}}, got {names}",
    )
    _assert(
        len(plan.build_units) == 3,
        f"expected exactly 3 units, got {len(plan.build_units)}",
    )

    by_name = {bu.name: bu for bu in plan.build_units}
    _assert(by_name["StripeCore"].synthetic, "StripeCore should be marked synthetic")
    _assert(by_name["StripeUICore"].synthetic, "StripeUICore should be marked synthetic")
    _assert(
        not by_name["Stripe"].synthetic,
        "Stripe is a real product, not synthetic",
    )

    synthetic_edits = {
        e.product_name for e in plan.package_swift_edits
        if e.kind == "add_synthetic_library"
    }
    _assert(
        synthetic_edits == {"StripeCore", "StripeUICore"},
        f"synthetic edits should be {{StripeCore, StripeUICore}}, got {synthetic_edits}",
    )
    force_names = {
        e.product_name for e in plan.package_swift_edits
        if e.kind == "force_dynamic"
    }
    _assert(
        "Stripe" in force_names,
        f"Stripe should get force_dynamic; got {force_names}",
    )
    _assert(
        "StripeCore" not in force_names,
        "synthetic library StripeCore should NOT get force_dynamic "
        "(the synthetic edit is already .dynamic)",
    )
    _assert(
        "StripeUICore" not in force_names,
        "synthetic library StripeUICore should NOT get force_dynamic",
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
        if e.kind == "add_synthetic_library"
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

    # No synthetic edit should be added — the existing product is reused.
    synthetic = [e for e in plan.package_swift_edits if e.kind == "add_synthetic_library"]
    _assert(
        not synthetic,
        f"no synthetic library should be added when --target matches an "
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
    product, the planner must re-include it exactly once and emit a
    force_dynamic edit for it (since it's still a non-dynamic product).
    This covers the otherwise-untested branch where the existing product
    hasn't already been planned.
    """
    # Use Alamofire: --product AlamofireDynamic narrows to just the
    # already-dynamic one, then --target Alamofire forces the non-dynamic
    # regular product back in via the "existing product" branch.
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
    # Alamofire appears exactly once (not duplicated as synthetic).
    _assert(
        names.count("Alamofire") == 1,
        f"Alamofire should appear once, got {names}",
    )
    by_name = {bu.name: bu for bu in plan.build_units}
    _assert(
        not by_name["Alamofire"].synthetic,
        "reinstated Alamofire should not be marked synthetic",
    )
    # Force_dynamic on Alamofire, but not on AlamofireDynamic.
    force_names = {
        e.product_name for e in plan.package_swift_edits
        if e.kind == "force_dynamic"
    }
    _assert(
        force_names == {"Alamofire"},
        f"force_dynamic should be exactly {{Alamofire}}, got {force_names}",
    )
    # And no synthetic edits — we reused the existing product.
    synthetic = [e for e in plan.package_swift_edits if e.kind == "add_synthetic_library"]
    _assert(not synthetic, f"no synthetic edits expected, got {synthetic}")


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


def _selftest_edit_force_dynamic_grdb_targets_correct_library() -> None:
    """The CRITICAL hazard: GRDB has two libraries whose names start with
    'GRDB'. force_dynamic('GRDB') must edit the middle one ('GRDB'), NOT
    the third one ('GRDB-dynamic') and NOT GRDBSQLite. This is the bug
    the session-2 worker called out by name."""
    out = edit_force_dynamic(GRDB_PACKAGE_SWIFT_FIXTURE, "GRDB")
    # The GRDB line must now contain `type: .dynamic`.
    grdb_line = [
        ln for ln in out.splitlines()
        if 'name: "GRDB",' in ln and ".library(" in ln
    ]
    _assert(len(grdb_line) == 1, f"expected exactly one GRDB .library line, got {len(grdb_line)}")
    _assert("type: .dynamic" in grdb_line[0],
            f"GRDB line missing type: .dynamic — {grdb_line[0]}")
    # GRDB-dynamic line must be unchanged: still contains the same shape.
    grdb_dyn_line = [
        ln for ln in out.splitlines()
        if 'name: "GRDB-dynamic"' in ln
    ]
    _assert(len(grdb_dyn_line) == 1, "GRDB-dynamic line missing")
    _assert(
        grdb_dyn_line[0].count("type: .dynamic") == 1,
        f"GRDB-dynamic line should still have exactly one type: .dynamic — {grdb_dyn_line[0]}",
    )
    # GRDBSQLite line must be untouched (still no type: clause).
    grdb_sqlite_line = [
        ln for ln in out.splitlines()
        if 'name: "GRDBSQLite"' in ln and ".library(" in ln
    ]
    _assert(len(grdb_sqlite_line) == 1, "GRDBSQLite line missing")
    _assert(
        "type:" not in grdb_sqlite_line[0],
        f"GRDBSQLite line should NOT have a type: clause — {grdb_sqlite_line[0]}",
    )


def _selftest_edit_force_dynamic_already_dynamic_is_noop() -> None:
    """Defensive: if the edit is somehow applied to an already-dynamic
    library, it must not introduce a duplicate clause or otherwise
    corrupt the manifest."""
    out = edit_force_dynamic(GRDB_PACKAGE_SWIFT_FIXTURE, "GRDB-dynamic")
    _assert(out == GRDB_PACKAGE_SWIFT_FIXTURE,
            "force_dynamic on already-dynamic product should be a no-op")


def _selftest_edit_force_dynamic_replaces_existing_type() -> None:
    """If a library already has `type: .static`, the edit replaces it
    with `type: .dynamic` rather than appending a second `type:` clause."""
    src = '''let p = Package(
    products: [
        .library(name: "Foo", type: .static, targets: ["Foo"]),
    ]
)
'''
    out = edit_force_dynamic(src, "Foo")
    _assert("type: .dynamic" in out, f"expected .dynamic in output: {out}")
    _assert(".static" not in out, f"static not removed: {out}")
    # Make sure exactly one type: clause exists.
    _assert(out.count("type:") == 1, f"expected exactly one type: clause, got {out.count('type:')}")


def _selftest_edit_force_dynamic_multiline_arguments() -> None:
    """Stripe-shape: the .library(...) call uses multi-line arguments. The
    balanced-paren walker must navigate them correctly."""
    out = edit_force_dynamic(STRIPE_PACKAGE_SWIFT_FIXTURE, "Stripe")
    # The Stripe library should now have type: .dynamic injected after
    # the name line. The simplest assertion: the modified text contains
    # `name: "Stripe", type: .dynamic` (with whatever exact spacing the
    # editor uses).
    _assert('name: "Stripe", type: .dynamic' in out,
            f"Stripe edit didn't land — searched for 'name: \"Stripe\", type: .dynamic'")
    # And StripePayments must remain untouched (its `name:` line is on
    # its own line, no type: clause should be added).
    _assert('name: "StripePayments",\n            targets:' in out,
            "StripePayments was unexpectedly modified")


def _selftest_edit_force_dynamic_missing_product_raises() -> None:
    """Deliberate-failure path: requesting force_dynamic on a name that
    doesn't exist must raise PrepareUserError (clean message path), not
    silently no-op and not traceback — this is the canonical `--product
    NoSuchProduct` shape."""
    try:
        edit_force_dynamic(GRDB_PACKAGE_SWIFT_FIXTURE, "DoesNotExist")
    except PrepareUserError as exc:
        _assert("DoesNotExist" in str(exc), f"error mentions name: {exc}")
        # Must also satisfy the user-facing isinstance check so main()
        # routes it through the clean-message handler.
        _assert(
            isinstance(exc, _USER_FACING_ERRORS),
            "PrepareUserError must be in _USER_FACING_ERRORS for clean exit",
        )
        return
    raise AssertionError("expected PrepareUserError for missing product")


def _selftest_edit_add_synthetic_library_no_trailing_comma() -> None:
    """Stripe-shape `]` (no trailing comma after last entry): the editor
    must insert a leading comma + new entry before the close bracket."""
    out = edit_add_synthetic_library(STRIPE_PACKAGE_SWIFT_FIXTURE, "StripeCore", ["StripeCore"])
    _assert(
        '.library(name: "StripeCore", type: .dynamic, targets: ["StripeCore"])' in out,
        "synthetic library entry missing",
    )
    # The previous last entry's closing `)` should now be followed by a `,`.
    # Find the last existing entry's close paren in the modified text.
    idx = out.find('.library(\n            name: "StripeConnect"')
    _assert(idx != -1, "StripeConnect entry should still be there")
    after = out[idx:]
    # The first `)` after StripeConnect's open should be followed by `,`.
    rp = after.find(")")
    _assert(rp != -1, "StripeConnect close paren missing")
    _assert(after[rp + 1] == ",", f"expected `,` after StripeConnect's `)`, got {after[rp+1]!r}")


def _selftest_edit_add_synthetic_library_with_trailing_comma() -> None:
    """GRDB-shape `,]`: the editor must NOT add a duplicate comma."""
    out = edit_add_synthetic_library(GRDB_PACKAGE_SWIFT_FIXTURE, "MyExtra", ["GRDB"])
    _assert(
        '.library(name: "MyExtra", type: .dynamic, targets: ["GRDB"])' in out,
        "synthetic library entry missing",
    )
    # GRDB-dynamic line ends with `,` — check we didn't double up.
    grdb_dyn_idx = out.find('.library(name: "GRDB-dynamic"')
    _assert(grdb_dyn_idx != -1, "GRDB-dynamic line should still be there")
    rp = out.find(")", grdb_dyn_idx)
    _assert(out[rp + 1] == ",", f"GRDB-dynamic should still end with `,`, got {out[rp+1]!r}")
    _assert(out[rp + 2] != ",", f"no double comma — got {out[rp+1:rp+3]!r}")


def _selftest_edit_add_synthetic_library_empty_array() -> None:
    """An empty `products: []` should accept a new entry without
    corrupting the brackets."""
    src = '''let p = Package(
    name: "Empty",
    products: [],
    targets: []
)
'''
    out = edit_add_synthetic_library(src, "Foo", ["Foo"])
    _assert('.library(name: "Foo", type: .dynamic, targets: ["Foo"])' in out,
            f"missing entry: {out}")
    # `[]` was empty; after edit, products list should be a valid array.
    # Specifically the `[]` close bracket must still be present somewhere.
    _assert("]," in out and "products: [" in out, f"shape broken: {out}")


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


def _selftest_unsupported_swift_constructs() -> None:
    """The Prepare safety net rejects Package.swift files containing Swift
    constructs the balanced-paren walker can't reason about: raw strings,
    triple-quoted strings, and string interpolation. Each variant should
    raise PrepareUserError (clean message path — the user's manifest, not
    a tool bug) with a targeted message rather than allowing the walker
    to silently mis-parse.
    """
    # Plain manifests pass through.
    _assert_no_unsupported_swift_constructs(
        '// swift-tools-version:5.7\nlet x = "ok"\n'
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
    _expect_raise('let x = """\nhi\n"""\n', "triple-quoted")
    _expect_raise('let x = "name=\\(foo)"\n', "interpolation")


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
    _expect_reject(
        '/* safe comment */\n'
        'let x = """\nreal triple\n"""\n',
        "triple-quoted",
    )
    _expect_reject(
        '/// safe doc\n'
        'let x = "hi=\\(real)"\n',
        "interpolation",
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


def _selftest_slice_paths_unique() -> None:
    """Device + simulator slice paths must be disjoint so the parallel
    builder can run both at once without stomping on each other's
    archives, derived data, xcresult bundles, or log files."""
    work = Path("/tmp/spm2xc-fake-work")
    dev = _slice_paths(work, "MyUnit", "arm64")
    sim = _slice_paths(work, "MyUnit", "simulator")
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


def _selftest_detect_system_frameworks_linker_settings(tmp_root: Path) -> None:
    """detect_system_frameworks unions linker settings with source-tree
    imports. This test exercises the linker-settings half: a target with
    no source files but a `linkedFramework` setting must still be picked
    up."""
    base = tmp_root / "detect_linker"
    staged = base / "staged"
    target_dir = staged / "Sources" / "Linked"
    target_dir.mkdir(parents=True)
    raw_dump = {
        "products": [{"name": "Linked", "type": {"library": ["automatic"]}, "targets": ["Linked"]}],
        "targets": [
            {
                "name": "Linked",
                "type": "regular",
                "path": "Sources/Linked",
                "publicHeadersPath": None,
                "dependencies": [],
                "settings": [
                    {"tool": "linker", "kind": {"linkedFramework": "CoreLocation"}},
                    {"tool": "linker", "kind": {"linkedFramework": "Security"}},
                    {"tool": "swift", "kind": {"define": "FOO"}},
                ],
            }
        ],
    }
    package = Package(
        name="Linked",
        tools_version="5.7.0",
        platforms=[],
        products=[Product(name="Linked", linkage=Linkage.AUTOMATIC, targets=["Linked"])],
        targets=[Target(
            name="Linked",
            kind=TargetKind.REGULAR,
            path="Sources/Linked",
            public_headers_path=None,
            dependencies=[],
            exclude=[],
            language=Language.SWIFT,
        )],
        schemes=[],
        raw_dump=raw_dump,
        staged_dir=staged,
    )
    fws = detect_system_frameworks(package, "Linked")
    _assert(fws == ["CoreLocation", "Security"],
            f"linker frameworks not detected: {fws}")


def _selftest_detect_system_frameworks_source_imports(tmp_root: Path) -> None:
    """detect_system_frameworks scans target source files for
    `#import <Framework/...>` and `@import Framework` lines. The Tests/
    subdirectory must be excluded so a Demo app's UIKit import doesn't
    leak into the library's framework list."""
    base = tmp_root / "detect_source"
    staged = base / "staged"
    target_dir = staged / "Sources" / "Scan"
    target_dir.mkdir(parents=True)
    (target_dir / "Header.h").write_text(
        "#import <UIKit/UIKit.h>\n"
        "@import CoreFoundation;\n"
    )
    (target_dir / "Impl.m").write_text(
        "#import <CoreGraphics/CoreGraphics.h>\n"
    )
    tests = target_dir / "Tests"
    tests.mkdir()
    (tests / "Junk.m").write_text("#import <CoreData/CoreData.h>\n")
    raw_dump = {
        "products": [{"name": "Scan", "type": {"library": ["automatic"]}, "targets": ["Scan"]}],
        "targets": [
            {
                "name": "Scan",
                "type": "regular",
                "path": "Sources/Scan",
                "publicHeadersPath": None,
                "dependencies": [],
            }
        ],
    }
    package = Package(
        name="Scan",
        tools_version="5.7.0",
        platforms=[],
        products=[Product(name="Scan", linkage=Linkage.AUTOMATIC, targets=["Scan"])],
        targets=[Target(
            name="Scan",
            kind=TargetKind.REGULAR,
            path="Sources/Scan",
            public_headers_path=None,
            dependencies=[],
            exclude=[],
            language=Language.OBJC,
        )],
        schemes=[],
        raw_dump=raw_dump,
        staged_dir=staged,
    )
    fws = detect_system_frameworks(package, "Scan")
    _assert("UIKit" in fws, f"UIKit missing from detected frameworks: {fws}")
    _assert("CoreFoundation" in fws, f"CoreFoundation missing from detected frameworks: {fws}")
    _assert("CoreGraphics" in fws, f"CoreGraphics missing from detected frameworks: {fws}")
    _assert("CoreData" not in fws,
            f"CoreData should not appear (Tests/ subdir should be pruned): {fws}")
    _assert("Scan" not in fws, f"Scan should be removed as a self-reference: {fws}")


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


def _selftest_detect_system_frameworks_follows_target_edge(tmp_root: Path) -> None:
    """Regression for Codex [P1]: `detect_system_frameworks` must walk
    first-level `target`-shape deps so system frameworks declared by
    depended-on ObjC targets still reach the clang -dynamiclib line
    during static→dynamic promotion."""
    base = tmp_root / "detect_target_edge"
    staged = base / "staged"
    shell_dir = staged / "Sources" / "Shell"
    shell_dir.mkdir(parents=True)
    guts_dir = staged / "Sources" / "Guts"
    guts_dir.mkdir(parents=True)
    # Guts imports UIKit from source AND declares a CoreLocation linker
    # setting. Both should land in the result because we walked the
    # .target() edge to reach Guts.
    (guts_dir / "Guts.m").write_text("#import <UIKit/UIKit.h>\n")

    raw_dump = {
        "products": [
            {
                "name": "Shell",
                "type": {"library": ["automatic"]},
                "targets": ["Shell"],
            }
        ],
        "targets": [
            {
                "name": "Shell",
                "type": "regular",
                "path": "Sources/Shell",
                "dependencies": [{"target": ["Guts", None]}],
            },
            {
                "name": "Guts",
                "type": "regular",
                "path": "Sources/Guts",
                "dependencies": [],
                "settings": [
                    {"tool": "linker", "kind": {"linkedFramework": "CoreLocation"}},
                ],
            },
        ],
    }
    package = Package(
        name="Shell",
        tools_version="5.7.0",
        platforms=[],
        products=[Product(name="Shell", linkage=Linkage.AUTOMATIC, targets=["Shell"])],
        targets=[
            Target(name="Shell", kind=TargetKind.REGULAR,
                   path="Sources/Shell", public_headers_path=None,
                   dependencies=["Guts"], exclude=[],
                   language=Language.OBJC),
            Target(name="Guts", kind=TargetKind.REGULAR,
                   path="Sources/Guts", public_headers_path=None,
                   dependencies=[], exclude=[], language=Language.OBJC),
        ],
        schemes=[],
        raw_dump=raw_dump,
        staged_dir=staged,
    )
    fws = detect_system_frameworks(package, "Shell")
    _assert("UIKit" in fws,
            f"source-scan walked through .target() edge should surface UIKit: {fws}")
    _assert("CoreLocation" in fws,
            f"linker settings walked through .target() edge should surface "
            f"CoreLocation: {fws}")


def _selftest_archive_static_lib_path_picks_first(tmp_root: Path) -> None:
    """_archive_static_lib_path returns the first lib*.a it finds, sorted."""
    base = tmp_root / "static_pick"
    products = base / "Products" / "usr" / "local" / "lib"
    products.mkdir(parents=True)
    (products / "libBeta.a").write_text("")
    (products / "libAlpha.a").write_text("")
    found = _archive_static_lib_path(base)
    _assert(found is not None and found.name == "libAlpha.a",
            f"expected libAlpha.a (sorted), got {found}")


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
    """A single-slice xcframework should fail the ≥2 slice check."""
    base = tmp_root / "verify_one_slice"
    base.mkdir()
    xc = _build_synthetic_xcframework(base, "Solo", flavor="swift",
                                       slices=("ios-arm64",))
    unit = ExecutedUnit(name="Solo", xcframework_path=xc, framework_name="Solo")
    mod = tool
    saved = mod._check_binary_dynamic
    try:
        mod._check_binary_dynamic = lambda _b: True
        results = verify_output([unit], base)
    finally:
        mod._check_binary_dynamic = saved
    r = results[0]
    _assert(not r.passed, "single-slice xcframework should fail verify")
    _assert(any("slice" in m for m in r.fatal_issues),
            f"expected slice-count message, got {r.fatal_issues!r}")


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
    """GRDB: force_dynamic on GRDB only. Verify GRDB ends up dynamic;
    GRDBSQLite + GRDB-dynamic remain unchanged."""
    edits = [PackageSwiftEdit(kind="force_dynamic", product_name="GRDB", targets=["GRDB"])]
    staged, plan, dump = _roundtrip_apply_and_dump(GRDB_PACKAGE_SWIFT_FIXTURE, edits)
    try:
        prods = {p["name"]: p for p in dump["products"]}
        _assert(prods["GRDB"]["type"]["library"][0] == "dynamic",
                f"GRDB linkage: {prods['GRDB']['type']}")
        _assert(prods["GRDB-dynamic"]["type"]["library"][0] == "dynamic",
                f"GRDB-dynamic linkage: {prods['GRDB-dynamic']['type']}")
        _assert(prods["GRDBSQLite"]["type"]["library"][0] == "automatic",
                f"GRDBSQLite linkage: {prods['GRDBSQLite']['type']}")
    finally:
        shutil.rmtree(staged, ignore_errors=True)


def _roundtrip_alamofire() -> None:
    """Alamofire: force_dynamic on Alamofire (the automatic one). Verify
    BOTH products survive and AlamofireDynamic isn't double-patched."""
    edits = [PackageSwiftEdit(kind="force_dynamic", product_name="Alamofire", targets=["Alamofire"])]
    staged, plan, dump = _roundtrip_apply_and_dump(ALAMOFIRE_PACKAGE_SWIFT_FIXTURE, edits)
    try:
        prods = {p["name"]: p for p in dump["products"]}
        _assert(set(prods.keys()) == {"Alamofire", "AlamofireDynamic"},
                f"Alamofire products: {sorted(prods.keys())}")
        _assert(prods["Alamofire"]["type"]["library"][0] == "dynamic",
                f"Alamofire linkage: {prods['Alamofire']['type']}")
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
            PackageSwiftEdit(kind="force_dynamic", product_name="Alamofire", targets=["Alamofire"]),
        ]
        apply_package_swift_edits(tmp, plan)

        # The base file should have been edited; the version-specific
        # siblings should still carry their DO_NOT_EDIT markers verbatim.
        base_text = (tmp / "Package.swift").read_text()
        _assert("type: .dynamic" in base_text,
                "expected force_dynamic edit in base Package.swift, "
                "got:\n" + base_text)
        sibling_5_10_after = (tmp / "Package@swift-5.10.swift").read_text()
        sibling_5_9_after = (tmp / "Package@swift-5.9.swift").read_text()
        _assert(sibling_5_10_after == sibling_5_10_before,
                "Package@swift-5.10.swift should be byte-identical, got:\n"
                + sibling_5_10_after)
        _assert(sibling_5_9_after == sibling_5_9_before,
                "Package@swift-5.9.swift should be byte-identical, got:\n"
                + sibling_5_9_after)

        # Round-trip through SPM and verify the dump now reports
        # Alamofire as dynamic. This is what the validator would do — it's
        # the test that would have caught the original bug.
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
        _assert(prods["Alamofire"]["type"]["library"][0] == "dynamic",
                f"post-edit Alamofire linkage = "
                f"{prods['Alamofire']['type']}; the edit didn't take effect")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _roundtrip_stripe_force_dynamic_and_synthetic() -> None:
    """Stripe: force_dynamic on Stripe + add_synthetic_library StripeCore.
    Verify both edits land and the existing 4 products survive."""
    edits = [
        PackageSwiftEdit(kind="force_dynamic", product_name="Stripe", targets=["Stripe"]),
        PackageSwiftEdit(kind="add_synthetic_library", product_name="StripeCore", targets=["StripeCore"]),
    ]
    staged, plan, dump = _roundtrip_apply_and_dump(STRIPE_PACKAGE_SWIFT_FIXTURE, edits)
    try:
        prods = {p["name"]: p for p in dump["products"]}
        # All 4 original + 1 synthetic
        expected = {"Stripe", "StripePayments", "StripeFinancialConnections", "StripeConnect", "StripeCore"}
        _assert(set(prods.keys()) == expected,
                f"Stripe products: {sorted(prods.keys())}; expected {sorted(expected)}")
        _assert(prods["Stripe"]["type"]["library"][0] == "dynamic",
                f"Stripe linkage: {prods['Stripe']['type']}")
        _assert(prods["StripeCore"]["type"]["library"][0] == "dynamic",
                f"StripeCore linkage: {prods['StripeCore']['type']}")
        _assert(prods["StripeCore"]["targets"] == ["StripeCore"],
                f"StripeCore targets: {prods['StripeCore']['targets']}")
        # Untouched products should still be automatic
        _assert(prods["StripePayments"]["type"]["library"][0] == "automatic",
                f"StripePayments linkage: {prods['StripePayments']['type']}")
    finally:
        shutil.rmtree(staged, ignore_errors=True)


def _roundtrip_system_library_left_alone() -> None:
    """A package with a system library + a regular target: the planner
    skips the system one, so Prepare only force_dynamics the regular
    one. Validator must accept this without complaint."""
    edits = [PackageSwiftEdit(kind="force_dynamic", product_name="Wrapper", targets=["Wrapper"])]
    staged, plan, dump = _roundtrip_apply_and_dump(SYSTEM_LIB_PACKAGE_SWIFT_FIXTURE, edits)
    try:
        prods = {p["name"]: p for p in dump["products"]}
        _assert(prods["Wrapper"]["type"]["library"][0] == "dynamic",
                f"Wrapper linkage: {prods['Wrapper']['type']}")
        # Sqlite3 left alone — still automatic.
        _assert(prods["Sqlite3"]["type"]["library"][0] == "automatic",
                f"Sqlite3 linkage: {prods['Sqlite3']['type']}")
    finally:
        shutil.rmtree(staged, ignore_errors=True)


def _roundtrip_validator_catches_missing_product() -> None:
    """The mandatory round-trip validator must raise PrepareUserError when
    the planner asks Prepare to force_dynamic a product that doesn't
    exist in the manifest — this is the canonical `--product NoSuchProduct`
    shape and must land on the clean-error path, not a traceback."""
    tmp = Path(tempfile.mkdtemp(prefix="spm2xc-prep-bad-"))
    try:
        (tmp / "Package.swift").write_text(GRDB_PACKAGE_SWIFT_FIXTURE)
        plan = Plan()
        plan.package_swift_edits = [
            PackageSwiftEdit(kind="force_dynamic", product_name="DoesNotExist",
                             targets=["DoesNotExist"]),
        ]
        try:
            prepare(tmp, plan, verbose=False)
        except PrepareUserError as exc:
            _assert("DoesNotExist" in str(exc),
                    f"PrepareUserError should mention DoesNotExist: {exc}")
            _assert(
                isinstance(exc, _USER_FACING_ERRORS),
                "PrepareUserError must be user-facing",
            )
            return
        raise AssertionError("expected PrepareUserError for non-existent product")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _roundtrip_foo_force_dynamic() -> None:
    """Minimal Foo fixture: force_dynamic on the lone .library product
    must produce a Package.swift that survives `swift package dump-package`
    and ends up dynamic in the dumped JSON. This is the smallest possible
    end-to-end gate for edit_force_dynamic against a real swift toolchain."""
    edits = [PackageSwiftEdit(kind="force_dynamic", product_name="Foo", targets=["Foo"])]
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
        _assert("Foo" in prods, f"Foo product missing from dump: {sorted(prods.keys())}")
        _assert(prods["Foo"]["type"]["library"][0] == "dynamic",
                f"Foo linkage: {prods['Foo']['type']}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _roundtrip_full_prepare_grdb() -> None:
    """End-to-end Prepare on a GRDB-shaped Plan: confirms validator
    accepts the planner's exact edit list (force_dynamic on GRDB only,
    leaving GRDB-dynamic and GRDBSQLite alone). This is the gate that
    catches edit/planner drift across sessions."""
    tmp = Path(tempfile.mkdtemp(prefix="spm2xc-prep-grdb-"))
    try:
        (tmp / "Package.swift").write_text(GRDB_PACKAGE_SWIFT_FIXTURE)
        plan = Plan()
        plan.package_swift_edits = [
            PackageSwiftEdit(kind="force_dynamic", product_name="GRDB", targets=["GRDB"]),
        ]
        plan.build_units = [
            BuildUnit(
                name="GRDB",
                scheme="GRDB",
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
        _assert(prods["GRDB"].linkage == Linkage.DYNAMIC,
                f"GRDB linkage: {prods['GRDB'].linkage}")
        _assert(prods["GRDB-dynamic"].linkage == Linkage.DYNAMIC,
                f"GRDB-dynamic linkage: {prods['GRDB-dynamic'].linkage}")
        # GRDBSQLite was not in build_units (planner skipped it), so the
        # validator's "every build_unit's product is present" check
        # doesn't fire on it. The product itself is still in the dump.
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
        ("language counter (synthetic tree)", lambda: _selftest_language_counter(tmp_root), False),
        ("scheme resolver", _selftest_scheme_resolver, False),
        ("planner: GRDB (force_dynamic + skip system + leave dynamic alone)",
         _selftest_planner_grdb, False),
        ("planner: Alamofire (regular + already-dynamic over same target)",
         _selftest_planner_alamofire, False),
        ("planner: Stripe (--product + --target synthetic libraries)",
         _selftest_planner_stripe_synthetic_libraries, False),
        ("planner: Stripe3DS2 binary target rejection",
         _selftest_planner_stripe_rejects_binary_target, False),
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
        ("edit_force_dynamic GRDB three-library hazard",
         _selftest_edit_force_dynamic_grdb_targets_correct_library, False),
        ("edit_force_dynamic already-dynamic no-op",
         _selftest_edit_force_dynamic_already_dynamic_is_noop, False),
        ("edit_force_dynamic replaces existing type",
         _selftest_edit_force_dynamic_replaces_existing_type, False),
        ("edit_force_dynamic multiline arguments (Stripe)",
         _selftest_edit_force_dynamic_multiline_arguments, False),
        ("edit_force_dynamic missing product raises",
         _selftest_edit_force_dynamic_missing_product_raises, False),
        ("edit_add_synthetic_library no trailing comma (Stripe)",
         _selftest_edit_add_synthetic_library_no_trailing_comma, False),
        ("edit_add_synthetic_library with trailing comma (GRDB)",
         _selftest_edit_add_synthetic_library_with_trailing_comma, False),
        ("edit_add_synthetic_library empty products array",
         _selftest_edit_add_synthetic_library_empty_array, False),
        ("xcresulttool build-results parser",
         _selftest_parse_xcresult_build_results, False),
        ("unsupported Swift constructs guard",
         _selftest_unsupported_swift_constructs, False),
        ("unsupported Swift constructs guard: comment-aware stripping",
         _selftest_unsupported_swift_constructs_comment_aware, False),
        ("error taxonomy: User vs Bug subclasses routed correctly",
         _selftest_error_taxonomy_split, False),
        ("active manifest selector (Package@swift-X.Y)",
         _selftest_select_active_manifest, False),
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
        ("execute: detect_system_frameworks linker settings",
         lambda: _selftest_detect_system_frameworks_linker_settings(tmp_root), False),
        ("execute: detect_system_frameworks source imports + Tests/ pruning",
         lambda: _selftest_detect_system_frameworks_source_imports(tmp_root), False),
        ("execute: ObjC headers dir priority (fw_name > product_name > any)",
         lambda: _selftest_find_objc_headers_dir_priority(tmp_root), False),
        ("execute: ObjC headers dir follows .target() dep edge (P1)",
         lambda: _selftest_find_objc_headers_dir_follows_target_edge(tmp_root), False),
        ("execute: ObjC headers dir defaults to include/ when publicHeadersPath omitted",
         lambda: _selftest_find_objc_headers_dir_defaults_to_include(tmp_root), False),
        ("execute: detect_system_frameworks follows .target() dep edge (P1)",
         lambda: _selftest_detect_system_frameworks_follows_target_edge(tmp_root), False),
        ("execute: archive static lib path picks first sorted",
         lambda: _selftest_archive_static_lib_path_picks_first(tmp_root), False),
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
        ("execute: detect_framework_type skips system shim sibling (GRDB)",
         lambda: _selftest_detect_framework_type_skips_system_shim_sibling(tmp_root), False),
        ("execute: read_xcframework_library_paths basic happy path",
         lambda: _selftest_read_xcframework_library_paths_basic(tmp_root), False),
        ("execute: read_xcframework_library_paths robust to corrupt plist",
         lambda: _selftest_read_xcframework_library_paths_robust_to_corruption(tmp_root), False),
        ("execute: pick_primary_framework_in_slice honors LibraryPath (P1)",
         lambda: _selftest_pick_primary_framework_in_slice_honors_library_path(tmp_root), False),
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
        ("verify: corrupt Info.plist (__MACOSX ghost)",
         lambda: _selftest_verify_corrupt_info_plist(tmp_root), False),
        ("verify: missing xcframework directory",
         lambda: _selftest_verify_missing_xcframework(tmp_root), False),
        ("verify: single-slice xcframework rejected",
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
        ("round-trip: GRDB (force_dynamic + skip system)", _roundtrip_grdb, True),
        ("round-trip: Alamofire (force_dynamic regular, leave dynamic)",
         _roundtrip_alamofire, True),
        ("round-trip: Alamofire multi-manifest layout (Package.swift wins over @swift-5.10)",
         _roundtrip_alamofire_multi_manifest_layout, True),
        ("round-trip: Stripe (force_dynamic + add_synthetic_library)",
         _roundtrip_stripe_force_dynamic_and_synthetic, True),
        ("round-trip: system library left alone",
         _roundtrip_system_library_left_alone, True),
        ("round-trip: validator catches missing product (PrepareError)",
         _roundtrip_validator_catches_missing_product, True),
        ("round-trip: full prepare() flow on GRDB", _roundtrip_full_prepare_grdb, True),
        ("round-trip: Foo minimal fixture force_dynamic", _roundtrip_foo_force_dynamic, True),
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
