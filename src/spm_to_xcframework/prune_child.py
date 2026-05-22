"""Strip foreign-manifest noise from a transitive child's Package.swift
before its `swift package resolve` runs.

The orchestrator `_run_source_mode_with_transitives` builds each transitive
package as a child `_run_source_mode` invocation rooted at that package's
checkout. That re-rooting re-evaluates the child's Package.swift as a
standalone manifest, which surfaces dependencies the umbrella's resolve
never paid for — most commonly `swift-docc-plugin`, `carton`, `swift-format`,
and other dev/CI tooling that the foreign package author appends to
`package.dependencies` under `#if os(macOS)` / `#if compiler(>=…)` / etc.
Some of these have huge transitive graphs (carton pulls swift-nio,
swift-syntax, swift-tools-support-core, …) that explode a fast child run
into 10+ minutes of git clones.

Two orthogonal pruning passes, applied in order, both gated by mandatory
re-dump-package validation so a botched edit fails loudly:

  Pass A (`_strip_top_level_package_mutation_blocks`):
    Remove top-level `#if … #endif` blocks whose body touches
    `package.dependencies` or `package.targets`. "Top-level" means at the
    file's outermost scope — brace/paren depth zero at the `#` token.
    Conditional dev-tooling injections live here; legitimate platform-
    conditional source compilation lives *inside* a target's
    `swiftSettings:` and is unaffected.

  Pass C (`_prune_top_level_package_entries`):
    Walk the child's dump-package output from the products the umbrella
    actually consumes → their REGULAR targets → those targets' external
    `.product(name:, package:)` deps. Build a set of identities the
    child genuinely needs to compile the requested products, then drop
    any `.package(url:)` entry from the manifest's `dependencies:` array
    whose identity isn't in that set. This catches unconditional dev
    deps that Pass A leaves alone (e.g. swift-clocks declaring
    `swift-docc-plugin` outside any `#if`).

If either pass produces a manifest `dump-package` rejects, we raise
PrepareUserError; the orchestrator falls back to logging and skipping
the transitive (or aborts depending on `--best-effort-transitives`).
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .errors import PrepareUserError
from .log import verbose_log
from .prepare import (
    _assert_no_unsupported_swift_constructs,
    _balanced_close,
    _make_code_token_view,
)


def _identity_from_url(url: str) -> str:
    """SPM identity = last URL path component, `.git` stripped, lowercased.

    SPM normalises identities this way internally for `.product(name:,
    package: <identity>)` references, so this mirrors the matching used
    elsewhere in the codebase (e.g. `_discover_transitive_packages`
    in inspect.py).
    """
    s = url.strip().rstrip("/")
    if not s:
        return ""
    last = s.rsplit("/", 1)[-1]
    if last.endswith(".git"):
        last = last[:-4]
    return last.lower()


def _is_word_boundary(text: str, idx: int) -> bool:
    """True iff `idx` is past end-of-text or the char there isn't an
    identifier continuation char. Used to confirm `#if` isn't actually
    the start of `#iffoo` or `#ifdef` (the latter doesn't exist in
    Swift but a defensive check is cheap).
    """
    if idx >= len(text):
        return True
    c = text[idx]
    return not (c.isalnum() or c == "_")


def _scan_keyword_after_hash(view: str, hash_idx: int) -> Tuple[str, int]:
    """Given a `#` at view[hash_idx], return (keyword, end_idx) where
    `keyword` is the directive keyword (e.g. "if", "endif", "elseif")
    and `end_idx` points one past the keyword. Skips whitespace between
    `#` and the keyword. Returns ("", hash_idx + 1) on no match.
    """
    j = hash_idx + 1
    n = len(view)
    while j < n and view[j] in " \t":
        j += 1
    start = j
    while j < n and (view[j].isalnum() or view[j] == "_"):
        j += 1
    return view[start:j], j


def _find_matching_endif(view: str, hash_idx: int) -> int:
    """Given a `#if` at `view[hash_idx]`, return the index of the matching
    `#endif`'s `#`. Handles nested `#if`/`#elseif`/`#else`/`#endif`.
    Returns -1 if not found.
    """
    n = len(view)
    kw, i = _scan_keyword_after_hash(view, hash_idx)
    if kw != "if":
        return -1
    nesting = 1
    while i < n:
        if view[i] != "#":
            i += 1
            continue
        kw, after = _scan_keyword_after_hash(view, i)
        if kw == "if":
            nesting += 1
            i = after
            continue
        if kw == "endif":
            nesting -= 1
            if nesting == 0:
                return i
            i = after
            continue
        # else / elseif keep nesting, skip past.
        i = after if after > i else i + 1
    return -1


def _strip_top_level_package_mutation_blocks(source: str) -> Tuple[str, List[str]]:
    """Pass A: drop top-level `#if … #endif` blocks whose body mentions
    `package.dependencies` or `package.targets`.

    Returns (edited_source, removal_reasons). Reasons are human-readable
    strings the caller can verbose-log.
    """
    view = _make_code_token_view(source)
    n = len(view)
    depth = 0
    i = 0
    spans: List[Tuple[int, int, str]] = []  # (start, end_exclusive, reason)
    while i < n:
        c = view[i]
        if c in "({[":
            depth += 1
            i += 1
            continue
        if c in ")}]":
            depth -= 1
            i += 1
            continue
        if c == "#" and depth == 0:
            kw, after = _scan_keyword_after_hash(view, i)
            if kw == "if" and _is_word_boundary(view, after):
                endif_idx = _find_matching_endif(view, i)
                if endif_idx == -1:
                    i += 1
                    continue
                # Slice the body from the source (use source to inspect, since
                # _make_code_token_view blanks strings/comments but keeps
                # identifiers). We want to know if the body references
                # `package.dependencies` or `package.targets` as code.
                body_end = _scan_keyword_after_hash(view, endif_idx)[1]
                body_view = view[after:endif_idx]
                if (
                    "package.dependencies" in body_view
                    or "package.targets" in body_view
                ):
                    # Compute the actual removal span in `source`, swallowing
                    # the trailing newline (and the leading newline if the
                    # `#if` started its own line) to keep the file tidy.
                    start = i
                    if start > 0 and source[start - 1] == "\n":
                        start -= 1
                    end = body_end
                    if end < len(source) and source[end] == "\n":
                        end += 1
                    spans.append(
                        (start, end, f"#if block mutating package.* (at offset {i})")
                    )
                    i = body_end
                    continue
        i += 1

    if not spans:
        return source, []

    out_parts: List[str] = []
    reasons: List[str] = []
    cursor = 0
    for start, end, reason in spans:
        out_parts.append(source[cursor:start])
        cursor = end
        reasons.append(reason)
    out_parts.append(source[cursor:])
    return "".join(out_parts), reasons


def _find_package_call(source: str) -> Optional[Tuple[int, int]]:
    """Locate the top-level `Package(...)` constructor call. Returns
    (open_paren_idx, close_paren_idx_inclusive) or None if not found.
    """
    view = _make_code_token_view(source)
    # Look for `Package` token at file scope. Be tolerant of leading
    # whitespace between identifier and `(`.
    for m in re.finditer(r"\bPackage\b", view):
        j = m.end()
        n = len(view)
        while j < n and view[j] in " \t\r\n":
            j += 1
        if j < n and view[j] == "(":
            close = _balanced_close(source, j)
            if close != -1:
                return (j, close)
    return None


def _find_dependencies_array_in_package_call(
    source: str,
) -> Optional[Tuple[int, int]]:
    """Within the top-level `Package(...)`, find the `dependencies: [...]`
    argument array. Returns (open_bracket_idx, close_bracket_idx_inclusive)
    or None if the argument isn't present.
    """
    pkg_call = _find_package_call(source)
    if not pkg_call:
        return None
    open_paren, close_paren = pkg_call
    view = _make_code_token_view(source)
    inside_start = open_paren + 1
    inside_end = close_paren  # exclusive of `)`
    depth = 0
    i = inside_start
    while i < inside_end:
        c = view[i]
        if c in "({[":
            depth += 1
            i += 1
            continue
        if c in ")}]":
            depth -= 1
            i += 1
            continue
        if depth == 0:
            # Match `dependencies:` label at the start of an arg position.
            if (
                view.startswith("dependencies", i)
                and (i + 12 >= inside_end or not (view[i + 12].isalnum() or view[i + 12] == "_"))
            ):
                j = i + 12
                while j < inside_end and view[j] in " \t\r\n":
                    j += 1
                if j < inside_end and view[j] == ":":
                    j += 1
                    while j < inside_end and view[j] in " \t\r\n":
                        j += 1
                    if j < inside_end and view[j] == "[":
                        close = _balanced_close(source, j)
                        if close == -1:
                            return None
                        return (j, close)
                    return None
        i += 1
    return None


_PACKAGE_ENTRY_URL_RE = re.compile(r'url\s*:\s*"([^"]+)"')

_TARGET_NAME_RE = re.compile(r'name\s*:\s*"([^"]+)"')


def _find_targets_array_in_package_call(
    source: str,
) -> Optional[Tuple[int, int]]:
    """Within the top-level `Package(...)`, find the `targets: [...]`
    argument array. Returns (open_bracket_idx, close_bracket_idx_inclusive)
    or None if the argument isn't a literal array (e.g. a manifest that
    writes `targets: libraryTargets + testTargets`).
    """
    pkg_call = _find_package_call(source)
    if not pkg_call:
        return None
    open_paren, close_paren = pkg_call
    view = _make_code_token_view(source)
    inside_start = open_paren + 1
    inside_end = close_paren
    depth = 0
    i = inside_start
    while i < inside_end:
        c = view[i]
        if c in "({[":
            depth += 1
            i += 1
            continue
        if c in ")}]":
            depth -= 1
            i += 1
            continue
        if depth == 0:
            if (
                view.startswith("targets", i)
                and (i + 7 >= inside_end or not (view[i + 7].isalnum() or view[i + 7] == "_"))
            ):
                j = i + 7
                while j < inside_end and view[j] in " \t\r\n":
                    j += 1
                if j < inside_end and view[j] == ":":
                    j += 1
                    while j < inside_end and view[j] in " \t\r\n":
                        j += 1
                    if j < inside_end and view[j] == "[":
                        close = _balanced_close(source, j)
                        if close == -1:
                            return None
                        return (j, close)
                    return None
        i += 1
    return None


def _strip_test_targets(source: str) -> Tuple[str, List[str]]:
    """Pass B: in the top-level `Package(...) targets: [...]` array,
    drop every `.testTarget(...)` entry. Test targets are never built
    for xcframework production, and they're the most common source of
    stray dev-only deps (swift-macro-testing, snapshot-testing) that
    pollute the dependency closure and break `swift package dump-package`
    after Pass C prunes their backing `.package(url:)` entries.

    Surfaced by swift-perception (transitive of TCA): its
    `PerceptionMacrosTests` referenced `.product(name: "MacroTesting",
    package: "swift-macro-testing")`, and Pass C dropped the
    `swift-macro-testing` package, leaving a dangling reference that
    invalidated the manifest.
    """
    arr = _find_targets_array_in_package_call(source)
    if not arr:
        return source, []
    open_b, close_b = arr
    view = _make_code_token_view(source)
    inside_start = open_b + 1
    inside_end = close_b

    spans: List[Tuple[int, int, str]] = []
    depth = 0
    i = inside_start
    while i < inside_end:
        c = view[i]
        if c in "({[":
            depth += 1
            i += 1
            continue
        if c in ")}]":
            depth -= 1
            i += 1
            continue
        if depth == 0 and c == ".":
            j = i + 1
            while j < inside_end and view[j] in " \t\r\n":
                j += 1
            if view.startswith("testTarget", j) and (
                j + 10 >= inside_end
                or not (view[j + 10].isalnum() or view[j + 10] == "_")
            ):
                k = j + 10
                while k < inside_end and view[k] in " \t\r\n":
                    k += 1
                if k < inside_end and view[k] == "(":
                    close_paren = _balanced_close(source, k)
                    if close_paren == -1 or close_paren >= inside_end:
                        i += 1
                        continue
                    entry_src = source[i : close_paren + 1]
                    name_match = _TARGET_NAME_RE.search(entry_src)
                    target_name = name_match.group(1) if name_match else "?"
                    entry_end = close_paren + 1
                    scan = entry_end
                    while scan < inside_end and source[scan] in " \t":
                        scan += 1
                    if scan < inside_end and source[scan] == ",":
                        entry_end = scan + 1
                    scan2 = entry_end
                    while scan2 < inside_end and source[scan2] in " \t":
                        scan2 += 1
                    if scan2 < inside_end and source[scan2] == "\n":
                        entry_end = scan2 + 1
                    line_start = source.rfind("\n", inside_start, i) + 1
                    if source[line_start:i].strip() == "":
                        entry_start = line_start
                    else:
                        entry_start = i
                    spans.append(
                        (entry_start, entry_end, f".testTarget({target_name})")
                    )
                    i = close_paren + 1
                    continue
        i += 1

    if not spans:
        return source, []

    out_parts: List[str] = []
    reasons: List[str] = []
    cursor = 0
    for start, end, reason in spans:
        out_parts.append(source[cursor:start])
        cursor = end
        reasons.append(reason)
    out_parts.append(source[cursor:])
    return "".join(out_parts), reasons


def _prune_top_level_package_entries(
    source: str, needed_identities: Set[str]
) -> Tuple[str, List[str]]:
    """Pass C: in the top-level `Package(...) dependencies: [...]`,
    drop `.package(url:)` entries whose identity isn't in
    `needed_identities`.

    Returns (edited_source, removal_reasons). If the dependencies array
    isn't found, no-op.
    """
    arr = _find_dependencies_array_in_package_call(source)
    if not arr:
        return source, []
    open_b, close_b = arr  # close_b points at the `]`
    view = _make_code_token_view(source)
    inside_start = open_b + 1
    inside_end = close_b

    spans: List[Tuple[int, int, str]] = []
    depth = 0
    i = inside_start
    while i < inside_end:
        c = view[i]
        if c in "({[":
            depth += 1
            i += 1
            continue
        if c in ")}]":
            depth -= 1
            i += 1
            continue
        if depth == 0 and c == ".":
            # Looking for `.package(`
            j = i + 1
            while j < inside_end and view[j] in " \t\r\n":
                j += 1
            if view.startswith("package", j) and (
                j + 7 >= inside_end or not (view[j + 7].isalnum() or view[j + 7] == "_")
            ):
                k = j + 7
                while k < inside_end and view[k] in " \t\r\n":
                    k += 1
                if k < inside_end and view[k] == "(":
                    close_paren = _balanced_close(source, k)
                    if close_paren == -1 or close_paren >= inside_end:
                        i += 1
                        continue
                    entry_src = source[i : close_paren + 1]
                    m = _PACKAGE_ENTRY_URL_RE.search(entry_src)
                    if m:
                        identity = _identity_from_url(m.group(1))
                        if identity and identity not in needed_identities:
                            # Determine removal span: entry + trailing comma
                            # + trailing newline + leading whitespace if the
                            # entry was on its own line.
                            entry_end = close_paren + 1
                            scan = entry_end
                            while scan < inside_end and source[scan] in " \t":
                                scan += 1
                            if scan < inside_end and source[scan] == ",":
                                entry_end = scan + 1
                            scan2 = entry_end
                            while scan2 < inside_end and source[scan2] in " \t":
                                scan2 += 1
                            if scan2 < inside_end and source[scan2] == "\n":
                                entry_end = scan2 + 1
                            line_start = source.rfind("\n", inside_start, i) + 1
                            if source[line_start:i].strip() == "":
                                entry_start = line_start
                            else:
                                entry_start = i
                            spans.append(
                                (entry_start, entry_end, f".package({identity})")
                            )
                    i = close_paren + 1
                    continue
        i += 1

    if not spans:
        return source, []

    out_parts: List[str] = []
    reasons: List[str] = []
    cursor = 0
    for start, end, reason in spans:
        out_parts.append(source[cursor:start])
        cursor = end
        reasons.append(reason)
    out_parts.append(source[cursor:])
    return "".join(out_parts), reasons


def _dump_package_json(dir_path: Path) -> dict:
    """Local wrapper around `swift package dump-package` that returns
    parsed JSON or raises PrepareUserError on any non-zero exit / parse
    failure. Inspect's `dump_package` does the same thing but returns
    typed shards; here we just need the raw JSON to walk product/target
    closures.
    """
    cp = subprocess.run(
        ["swift", "package", "dump-package"],
        cwd=str(dir_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if cp.returncode != 0:
        tail = "\n".join((cp.stderr or "").rstrip().splitlines()[-5:])
        raise PrepareUserError(
            "prune-child: swift package dump-package failed:\n"
            + (tail or "  (no stderr)")
        )
    try:
        return json.loads(cp.stdout)
    except json.JSONDecodeError as exc:
        raise PrepareUserError(
            f"prune-child: could not parse dump-package JSON: {exc}"
        ) from exc


def _collect_needed_identities(
    raw_dump: dict, allowed_products: List[str]
) -> Set[str]:
    """From the child's dump-package, walk
        allowed_products → product.targets → target.dependencies (`product` shape)
        and recursively into internal target deps (`byName`, `target`)
        → external identity
    Return the lowercased identity set the child genuinely needs.
    """
    targets_by_name: Dict[str, dict] = {}
    for t in raw_dump.get("targets", []) or []:
        if isinstance(t, dict) and isinstance(t.get("name"), str):
            targets_by_name[t["name"]] = t

    allowed_lower = {p.lower() for p in allowed_products if isinstance(p, str)}
    needed: Set[str] = set()
    visited: Set[str] = set()

    def visit(tname: str) -> None:
        if tname in visited:
            return
        visited.add(tname)
        t = targets_by_name.get(tname)
        if not t:
            return
        for dep in t.get("dependencies", []) or []:
            if not isinstance(dep, dict):
                continue
            prod = dep.get("product")
            if isinstance(prod, list) and len(prod) >= 2 and isinstance(prod[1], str):
                needed.add(prod[1].lower())
                continue
            for key in ("byName", "target"):
                v = dep.get(key)
                if isinstance(v, list) and v and isinstance(v[0], str):
                    visit(v[0])

    for prod in raw_dump.get("products", []) or []:
        if not isinstance(prod, dict):
            continue
        name = prod.get("name")
        if not isinstance(name, str):
            continue
        # Empty allowed list = no filter (build all products). Honor that
        # so a buggy caller doesn't accidentally strip every dep.
        if allowed_lower and name.lower() not in allowed_lower:
            continue
        for tn in prod.get("targets", []) or []:
            if isinstance(tn, str):
                visit(tn)
    return needed


def prune_child_manifest_for_products(
    pre_staged_dir: Path,
    allowed_products: List[str],
    *,
    verbose: bool = False,
) -> None:
    """Apply Pass A then Pass C to `pre_staged_dir/Package.swift` in place.

    Validation contract: every write is followed by a `dump-package`
    round-trip. If a pass produces a manifest SPM rejects, we restore
    the manifest to the most recent validated state (Pass-A's output if
    Pass C fails; the original text if Pass A fails) and raise
    PrepareUserError so the orchestrator can decide whether to abort or
    skip-with-warning (per `--best-effort-transitives`). Keeping Pass
    A's output on a Pass-C failure preserves a strictly-better manifest
    for any post-mortem of the pre-staged dir, and the orchestrator
    abandons the dir anyway in the raise-path so there's no
    downstream-state risk to the partial rollback.
    """
    pkg_swift = pre_staged_dir / "Package.swift"
    if not pkg_swift.is_file():
        return
    # `pre_staged_dir` is copied from `.build/checkouts/<pkg>/`, which SPM
    # makes read-only. Re-chmod Package.swift before we try to rewrite it.
    try:
        pkg_swift.chmod(pkg_swift.stat().st_mode | stat.S_IWUSR)
    except OSError:
        pass
    raw = pkg_swift.read_text()

    # Bail (no-op) if the manifest uses constructs the walker can't reason
    # about — better to ship the foreign package's deps as-is than to
    # produce a silently-broken edit.
    try:
        _assert_no_unsupported_swift_constructs(raw)
    except PrepareUserError as exc:
        verbose_log(
            verbose,
            f"  prune-child: skipping prune (unsupported manifest construct: {exc})",
        )
        return

    original = raw

    # Pass A: strip top-level #if blocks mutating package.dependencies/targets.
    after_a, reasons_a = _strip_top_level_package_mutation_blocks(raw)
    if reasons_a:
        pkg_swift.write_text(after_a)
        try:
            raw_dump = _dump_package_json(pre_staged_dir)
        except PrepareUserError:
            pkg_swift.write_text(original)
            raise
        for r in reasons_a:
            verbose_log(verbose, f"  prune-child: pass-A stripped {r}")
    else:
        raw_dump = _dump_package_json(pre_staged_dir)

    # Pass B: strip .testTarget(...) declarations from the targets array.
    # Test targets are never built for xcframework production, and dropping
    # them upfront prevents Pass C from leaving dangling .product(...)
    # references when it prunes test-only deps.
    after_b, reasons_b = _strip_test_targets(after_a)
    if reasons_b:
        pkg_swift.write_text(after_b)
        try:
            raw_dump = _dump_package_json(pre_staged_dir)
        except PrepareUserError:
            pkg_swift.write_text(after_a)
            raise
        for r in reasons_b:
            verbose_log(verbose, f"  prune-child: pass-B stripped {r}")
    else:
        after_b = after_a

    # Pass C: drop unused .package(url:) entries.
    needed = _collect_needed_identities(raw_dump, allowed_products)
    after_c, reasons_c = _prune_top_level_package_entries(after_b, needed)
    if reasons_c:
        pkg_swift.write_text(after_c)
        try:
            _dump_package_json(pre_staged_dir)
        except PrepareUserError:
            # Roll back to Pass-B's output (last validated state).
            pkg_swift.write_text(after_b)
            raise
        for r in reasons_c:
            verbose_log(verbose, f"  prune-child: pass-C stripped {r}")
