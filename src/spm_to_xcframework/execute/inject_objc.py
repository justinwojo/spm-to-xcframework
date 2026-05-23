"""Phase 4 — Execute · public ObjC header injection.

Ports the legacy bash inject_objc_headers + module.modulemap synth
passes. Decides where to find a target's public ObjC header roots
(explicit `publicHeadersPath`, SPM's default `include/` layout, or the
Stripe-style umbrella-header-at-target-root pattern), then copies them
into the framework's `Headers/` directory and synthesises a Clang
`module.modulemap` so consumers see the framework as a Clang module.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Set

from ..inspect import _raw_internal_dep_names
from ..log import dim, verbose_log
from ..model import Package, _default_target_path

from .inject_swiftmodule import _ensure_root_symlink, _framework_content_root


def _has_h_files_recursive(path: Path) -> bool:
    """True iff `path` contains at least one `.h` file at any depth.

    Used to filter out directories that exist but contain only Swift
    sources or subdirectories with no public ObjC surface — those
    aren't real header roots even when they happen to be named `include`
    or carry an explicit `publicHeadersPath`.
    """
    for _root, _dirs, files in os.walk(path):
        for name in files:
            if name.endswith(".h"):
                return True
    return False


def _find_objc_headers_dir(
    package: Package,
    product_name: str,
    fw_name: str,
) -> Optional[Path]:
    """Find a directory of public ObjC headers for the named product.

    Priority (matches legacy bash 1352-1397):
      1. A direct product target whose name == fw_name with a
         publicHeadersPath that exists and contains *.h files.
      2. A direct product target whose name == product_name.
      3. Any direct product target with headers.
      4. First-level dependencies of direct product targets, with the
         same fw_name > product_name > anything-with-headers priority.

    Returns the absolute path of the public-headers directory, or None.
    """
    raw_targets_by_name: Dict[str, dict] = {}
    for raw_t in package.raw_dump.get("targets", []) or []:
        if isinstance(raw_t, dict) and isinstance(raw_t.get("name"), str):
            raw_targets_by_name[raw_t["name"]] = raw_t

    product_targets: List[str] = []
    for raw_p in package.raw_dump.get("products", []) or []:
        if isinstance(raw_p, dict) and raw_p.get("name") == product_name:
            tlist = raw_p.get("targets")
            if isinstance(tlist, list):
                product_targets = [t for t in tlist if isinstance(t, str)]
            break
    if not product_targets and product_name in raw_targets_by_name:
        product_targets = [product_name]
    if not product_targets:
        return None

    # Targets that any product in this package exposes as part of its
    # own xcframework. Used to gate implicit-layout fallback in the dep
    # walk: a dep that backs another product is "separately packaged"
    # — its public surface lives in that product's xcframework, so
    # bleeding its `include/` or umbrella header up into the parent
    # frame would leak headers across xcframework boundaries
    # (Stripe3DS2 → StripePayments). A dep that no product references
    # is "internal" — only ever folded into the parent — so its
    # default-layout headers belong to the parent.
    separately_packaged: Set[str] = set()
    for raw_p in package.raw_dump.get("products", []) or []:
        if not isinstance(raw_p, dict):
            continue
        tlist = raw_p.get("targets")
        if not isinstance(tlist, list):
            continue
        for t in tlist:
            if isinstance(t, str):
                separately_packaged.add(t)

    def headers_dir_for(target_name: str, allow_implicit_layouts: bool) -> Optional[Path]:
        target = package.target_by_name(target_name)
        if target is None:
            return None
        target_path = target.path or _default_target_path(target.name, target.kind)
        if not target_path:
            return None
        target_dir = package.staged_dir / target_path
        # 1. Explicit `publicHeadersPath` always wins (the legacy contract).
        #    Empty string is also explicit: SPM treats `publicHeadersPath: ""`
        #    as "the target's own root directory is the public headers
        #    dir," which is how AppAuth-iOS ships its ObjC API (every .h
        #    sits next to its .m at the target root with no `include/`
        #    subdir and no umbrella header). Without this branch the
        #    empty string is falsy in Python, so we fell through to the
        #    implicit-layout fallback, found neither `include/` nor an
        #    `<TargetName>.h` umbrella, and gave up — leaving AppAuth /
        #    AppAuthCore xcframeworks with no Headers/ and Verify
        #    rejecting them as broken ObjC frameworks.
        if target.public_headers_path is not None:
            rel = target.public_headers_path
            full_path = target_dir if rel == "" else target_dir / rel
            if full_path.is_dir() and _has_h_files_recursive(full_path):
                return full_path
            return None
        # 2. Implicit layouts are gated by the caller. Direct product
        #    targets always opt in. Deps opt in only when the dep is
        #    not separately packaged — i.e. no product in this package
        #    exposes it as its own xcframework. A separately-packaged
        #    dep (Stripe3DS2 underneath StripePayments) ships its
        #    public surface in its own xcframework, so accepting an
        #    `include/` or umbrella here would duplicate-leak those
        #    headers into the parent.
        if not allow_implicit_layouts:
            return None
        # 2a. SPM default for ObjC targets: `<target_path>/include/`.
        include_dir = target_dir / "include"
        if include_dir.is_dir() and _has_h_files_recursive(include_dir):
            return include_dir
        # 2b. Umbrella-header-at-target-root pattern. Stripe ships every
        #    product this way: e.g. `StripePayments.h` sits at the top of
        #    `StripePayments/StripePayments/` with no `include/` subdir,
        #    next to ~220 .swift files. Even when the umbrella itself is
        #    a stub (just FOUNDATION_EXPORT version constants), the
        #    Headers/ + module.modulemap surface is what lets ObjC
        #    consumers `#import <StripePayments/StripePayments.h>` —
        #    skipping this branch for Swift-classified targets would
        #    silently drop that surface. Accept the two umbrella forms
        #    only — `<TargetName>.h` (the common Stripe target pattern)
        #    and `<TargetName>-umbrella.h` (used by the Stripe product
        #    target itself). Anything else risks over-injection:
        #    `inject_objc_headers` walks the returned dir recursively
        #    and copies every .h, so accepting a generic non-`-Swift.h`
        #    would leak private/internal top-level headers into the
        #    framework's public surface.
        for candidate in (f"{target.name}.h", f"{target.name}-umbrella.h"):
            if (target_dir / candidate).is_file():
                return target_dir
        return None

    product_match: Optional[Path] = None
    any_match: Optional[Path] = None
    for tname in product_targets:
        d = headers_dir_for(tname, allow_implicit_layouts=True)
        if d is None:
            continue
        if tname == fw_name:
            return d
        if tname == product_name:
            if product_match is None:
                product_match = d
        else:
            if any_match is None:
                any_match = d
    if product_match is not None:
        return product_match
    if any_match is not None:
        return any_match

    # First-level dependencies of direct product targets. Accepts both
    # `byName` and `target` dep shapes — the GRDB-style `.target()`
    # form would otherwise go unvisited and public headers from
    # depended-on ObjC targets would be missed (Codex [P1]). Implicit
    # layouts are gated by `separately_packaged`: a dep that backs its
    # own product gets its own xcframework, so we require an explicit
    # `publicHeadersPath` to opt those headers into the parent (this is
    # what stops Stripe3DS2's `include/` from leaking up into
    # StripePayments). An internal-only dep — one no product
    # references — gets the same `include/` and umbrella fallbacks as
    # a direct product target, since the parent is the only place it
    # ships.
    dep_fw_match: Optional[Path] = None
    dep_product_match: Optional[Path] = None
    dep_any_match: Optional[Path] = None
    seen_deps: Set[str] = set()
    for tname in product_targets:
        raw = raw_targets_by_name.get(tname)
        if raw is None:
            continue
        for dep_name in _raw_internal_dep_names(raw):
            if dep_name in seen_deps:
                continue
            seen_deps.add(dep_name)
            allow_implicit = dep_name not in separately_packaged
            d = headers_dir_for(dep_name, allow_implicit_layouts=allow_implicit)
            if d is None:
                continue
            if dep_name == fw_name and dep_fw_match is None:
                dep_fw_match = d
            elif dep_name == product_name and dep_product_match is None:
                dep_product_match = d
            elif dep_any_match is None:
                dep_any_match = d
    if dep_fw_match is not None:
        return dep_fw_match
    if dep_product_match is not None:
        return dep_product_match
    return dep_any_match


def _generate_modulemap(fw_path: Path, fw_name: str) -> None:
    """Write `Modules/module.modulemap` for a framework that has ObjC
    headers but no module map. Uses an umbrella header if `<fw_name>.h`
    exists, otherwise lists every header explicitly. Same shape as the
    legacy bash 1430-1450.
    """
    content_root = _framework_content_root(fw_path)
    modules_dir = content_root / "Modules"
    modules_dir.mkdir(parents=True, exist_ok=True)
    headers_dir = content_root / "Headers"
    umbrella = headers_dir / f"{fw_name}.h"
    if umbrella.is_file():
        # The `module * { export * }` line tells Clang to walk the
        # Headers/ tree on its own, so nested layouts work without
        # having to enumerate every file here.
        text = (
            f"framework module {fw_name} {{\n"
            f"  umbrella header \"{fw_name}.h\"\n"
            f"  export *\n"
            f"  module * {{ export * }}\n"
            f"}}\n"
        )
    else:
        # Walk recursively so nested headers (e.g. Headers/Sub/Foo.h)
        # land in the modulemap too — otherwise Clang only sees the
        # top-level .h files and `#import <Module/Sub/Foo.h>` fails to
        # resolve at bind time. Relative paths are POSIX-style to
        # match Clang's own module-map syntax.
        header_rel_paths = sorted(
            p.relative_to(headers_dir).as_posix()
            for p in headers_dir.rglob("*.h")
            if p.is_file()
        )
        lines = [f"framework module {fw_name} {{"]
        for rel in header_rel_paths:
            lines.append(f"  header \"{rel}\"")
        lines.append("  export *")
        lines.append("}")
        text = "\n".join(lines) + "\n"
    (modules_dir / "module.modulemap").write_text(text)


def inject_objc_headers(
    *,
    package: Package,
    product_name: str,
    fw_name: str,
    fw_path: Path,
    verbose: bool,
) -> bool:
    """Copy public ObjC headers + a generated modulemap into a framework
    bundle. No-op if the framework already has `*.h` headers (excluding
    the auto-generated `*-Swift.h` bridge header).

    Returns True iff anything was injected.
    """
    headers_target = _framework_content_root(fw_path) / "Headers"
    if headers_target.is_dir():
        for p in headers_target.glob("*.h"):
            if not p.name.endswith("-Swift.h"):
                verbose_log(verbose, "  Public headers already present in framework")
                return False

    headers_dir = _find_objc_headers_dir(package, product_name, fw_name)
    if headers_dir is None:
        verbose_log(verbose, f"  No ObjC public headers found in source tree for {fw_name}")
        return False

    dim(f"  Injecting ObjC headers ({fw_name})")
    headers_target.mkdir(parents=True, exist_ok=True)

    # SPM convention (legacy 1411-1420): headers may live in a
    # subdirectory named after the module (e.g., Public/FirebaseCore/*.h)
    # or directly in the public headers dir. Pick the scan base
    # accordingly, then walk it recursively and copy each header into
    # `Headers/<relative path>` — preserving subdirectories is required
    # because (a) `#import <Module/Sub/Header.h>` needs the physical
    # path to exist inside Headers/, and (b) a flat copy would silently
    # overwrite same-named headers that live in different subfolders
    # (Codex [P2]).
    module_subdir = headers_dir / fw_name
    scan_base = module_subdir if module_subdir.is_dir() else headers_dir
    copied = 0
    for h in sorted(scan_base.rglob("*.h")):
        if not h.is_file():
            continue
        rel = h.relative_to(scan_base)
        dest = headers_target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(h, dest)
        copied += 1

    if copied == 0:
        verbose_log(verbose, f"  No headers copied from {headers_dir}")
        return False

    _generate_modulemap(fw_path, fw_name)
    _ensure_root_symlink(fw_path, "Headers")
    _ensure_root_symlink(fw_path, "Modules")
    verbose_log(verbose, f"  Injected {copied} header(s) + modulemap")
    return True


def inject_pure_swift_clang_modulemap(
    *,
    fw_path: Path,
    fw_name: str,
    dd_path: Path,
    variant: str,
    verbose: bool,
) -> bool:
    """For a pure-Swift framework, expose its `@objc`-bridged surface to
    Clang `@import` consumers by synthesizing a minimal Clang modulemap
    referencing the Swift-generated `<fw_name>-Swift.h`.

    SPM builds with `BUILD_LIBRARY_FOR_DISTRIBUTION=YES` emit a
    `<Name>.swiftmodule/<arch>.swiftinterface` but NO Clang
    `module.modulemap` for pure-Swift targets. Swift consumers don't
    need one (swiftc finds the framework by name + swiftmodule
    directory), but Objective-C consumers that do `@import <Name>` or
    `#import <Name/Name-Swift.h>` fail with `Module '<Name>' not found`
    because Clang has no module declaration to bind. Real-world trigger:
    GoogleSignIn-iOS's `GIDEMMSupport.h` does `@import GTMAppAuth`; with
    no modulemap inside GTMAppAuth.framework the umbrella build fails.

    No-op when:
    - The framework already has `Modules/module.modulemap` (ObjC pass
      already generated one, or xcodebuild emitted one for a mixed
      target).
    - The framework has no `Modules/*.swiftmodule/` (not a Swift
      framework — nothing to expose).
    - No `<fw_name>-Swift.h` is found in DerivedData (Swift target
      exposes nothing to ObjC, so a modulemap referencing the bridge
      header would point at a nonexistent file).

    `requires objc` matches what Xcode itself emits for Swift-built
    `module.modulemap` — the bridge header is full of `@interface` /
    `@protocol` declarations only valid when ObjC interop is enabled.
    """
    content_root = _framework_content_root(fw_path)
    modules_dir = content_root / "Modules"
    existing_modulemap = modules_dir / "module.modulemap"
    if existing_modulemap.is_file():
        return False

    swiftmodule_dirs = list(modules_dir.glob("*.swiftmodule")) if modules_dir.is_dir() else []
    if not any(d.is_dir() for d in swiftmodule_dirs):
        return False

    bridge_header_name = f"{fw_name}-Swift.h"
    bridge_header_src: Optional[Path] = None
    if dd_path.is_dir():
        for candidate in dd_path.rglob(bridge_header_name):
            if candidate.is_file():
                bridge_header_src = candidate
                break
    if bridge_header_src is None:
        verbose_log(
            verbose,
            f"  No {bridge_header_name} in DerivedData; skipping pure-Swift "
            f"modulemap synth ({variant})",
        )
        return False

    headers_dir = content_root / "Headers"
    headers_dir.mkdir(parents=True, exist_ok=True)
    bridge_header_dest = headers_dir / bridge_header_name
    if not bridge_header_dest.is_file():
        shutil.copy2(bridge_header_src, bridge_header_dest)

    modules_dir.mkdir(parents=True, exist_ok=True)
    text = (
        f"framework module {fw_name} {{\n"
        f"  header \"{bridge_header_name}\"\n"
        f"  requires objc\n"
        f"}}\n"
    )
    existing_modulemap.write_text(text)

    _ensure_root_symlink(fw_path, "Headers")
    _ensure_root_symlink(fw_path, "Modules")
    dim(f"  Injected pure-Swift Clang modulemap ({variant}): {fw_name}")
    return True
