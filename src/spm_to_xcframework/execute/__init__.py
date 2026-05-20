"""Phase 4 — Execute · sub-package re-exports.

The 11 sibling sub-modules cover one Execute concern each (archive
driver, static→dynamic promote, four injection passes, xcframework
merge, dedup-overlap rewriter, per-unit driver, binary-mode copier).
This `__init__` re-exports the public entry points so callers can
write `from .execute import execute_source_plan` without knowing the
exact sub-module layout.
"""
from __future__ import annotations

from .archive import (
    read_xcresult_errors,
    run_xcodebuild_archive,
)
from .binary_promote import (
    execute_binary_plan,
    promote_binary_xcframework_static_to_dynamic,
)
from .create_xcframework import (
    create_xcframework,
    detect_framework_type,
)
from .inject_clang_bridge import inject_bridge_clang_modules
from .inject_clang_system import inject_system_clang_modules
from .inject_objc import inject_objc_headers
from .inject_resources import inject_resource_bundles
from .inject_swiftmodule import inject_swiftmodule
from .rename_framework import rename_framework_bundle
from .run_unit import execute_source_plan

__all__ = [
    "create_xcframework",
    "detect_framework_type",
    "execute_binary_plan",
    "execute_source_plan",
    "inject_bridge_clang_modules",
    "inject_objc_headers",
    "inject_resource_bundles",
    "inject_swiftmodule",
    "inject_system_clang_modules",
    "promote_binary_xcframework_static_to_dynamic",
    "read_xcresult_errors",
    "rename_framework_bundle",
    "run_xcodebuild_archive",
]
