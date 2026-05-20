"""Phase 3 — Prepare: mutate Package.swift via guarded string surgery.

Prepare is the riskiest phase (§5.3). It mutates Package.swift via
string surgery and then verifies the result by re-running real
`swift package dump-package` and asserting against the planner's
expectations. Three guardrails (per design):

  1. Edits are *whitelisted*. Prepare never decides what to edit; it
     consumes Plan.package_swift_edits verbatim.
  2. Edits are *span-scoped*. We locate one .library(...) call by exact
     `name: "X"` substring match, walk balanced parens to find its
     extent, then edit only inside that span.
  3. Mandatory round-trip validation. After every edit lands, we
     re-dump the manifest and assert linkage / product membership match
     the plan. A failure here raises PrepareError with a unified diff
     and the failed assertions.

Anything that gets past the round-trip validator is by construction
safe for Execute to consume.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from .errors import InspectError, PrepareBug, PrepareUserError
from .inspect import dump_package
from .log import info, success, verbose_log
from .model import Linkage, Package, Platform, Plan, PreparedPlan, Product, Target


def _strip_swift_comments(text: str) -> str:
    """Return `text` with `//` line comments and `/* */` block comments
    replaced by equal-length spans of spaces (preserving newlines). The
    length/offset preservation keeps any downstream index math valid,
    and preserving newlines keeps line-counting error messages honest.

    This is NOT a full Swift tokenizer — it only tracks double-quoted
    string state so comment markers inside a regular string literal
    don't trigger. The file-wide `_assert_no_unsupported_swift_constructs`
    gate runs AFTER this stripper and guards against the advanced
    string shapes (raw strings, triple-quotes, interpolation) that
    could otherwise fool the state machine.

    Behavior on unterminated comments / strings: we stop stripping at
    the unterminated boundary and keep the rest of the text verbatim.
    This conservative fall-through means a weird-shaped file is still
    checked against the unsupported-construct triggers in both its
    stripped-comment view and whatever trailing span the stripper
    couldn't classify.
    """
    n = len(text)
    out: List[str] = []
    i = 0
    while i < n:
        c = text[i]
        # Double-quoted string: copy verbatim, honoring `\\` escapes.
        if c == '"':
            out.append(c)
            i += 1
            while i < n:
                cc = text[i]
                out.append(cc)
                if cc == "\\" and i + 1 < n:
                    out.append(text[i + 1])
                    i += 2
                    continue
                if cc == '"':
                    i += 1
                    break
                if cc == "\n":
                    # Unterminated string — bail out. Copy the rest
                    # verbatim so the trigger-token scanner still sees
                    # anything suspicious downstream.
                    i += 1
                    break
                i += 1
            continue
        # Line comment: replace span with spaces up to (but not including)
        # the newline.
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            end = text.find("\n", i + 2)
            if end == -1:
                end = n
            out.append(" " * (end - i))
            i = end
            continue
        # Block comment: replace span with spaces, but preserve newlines
        # so line numbers don't shift. Swift block comments NEST, so we
        # track depth rather than stopping at the first `*/`.
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            depth = 1
            j = i + 2
            while j < n and depth > 0:
                if j + 1 < n and text[j] == "/" and text[j + 1] == "*":
                    depth += 1
                    j += 2
                    continue
                if j + 1 < n and text[j] == "*" and text[j + 1] == "/":
                    depth -= 1
                    j += 2
                    continue
                j += 1
            if depth > 0:
                # Unterminated (possibly nested) block comment.
                # Conservative fallthrough: copy the rest as-is and
                # let the trigger-token scan re-check.
                out.append(text[i:])
                i = n
                break
            stop = j
            for ch in text[i:stop]:
                out.append("\n" if ch == "\n" else " ")
            i = stop
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _make_code_token_view(text: str) -> str:
    """Return `text` with `//` line comments, `/* */` block comments,
    AND `"..."` string-literal spans (delimiters + body) replaced by
    equal-length spans of spaces. Newlines are preserved so line
    counters stay honest, and every offset in the returned string
    indexes the same Swift construct as in `text`.

    Use this when searching for top-level code tokens like `.target(`
    or `.binaryTarget(` that must NOT match inside any non-code region
    of a Package.swift. Stripping comments alone (via
    `_strip_swift_comments`) is not enough — a valid string literal
    whose body contains `.target(name: \"X\", ...)` text would
    otherwise be picked up as a candidate, the rewrite would refuse
    to land because the body's `name:` doesn't sit at depth 0 of a
    real call, and the user sees a spurious PrepareUserError on a
    well-formed manifest.

    Limitations match `_strip_swift_comments`: only `"..."` strings are
    tracked. `#\"...\"#` raw strings, `\"\"\"...\"\"\"` multi-line
    strings, and `\\(...)` interpolation aren't supported here — they
    would be rejected upstream by
    `_assert_no_unsupported_swift_constructs` before any prepare-time
    edit runs.

    On an unterminated string or block comment, falls through
    conservatively: copies the suspicious tail verbatim. The trigger
    scanner upstream is the layer that turns this into a hard error,
    so a soft fall-through here keeps the helper a pure offset-faithful
    view rather than another error site.
    """
    n = len(text)
    out: List[str] = []
    i = 0
    while i < n:
        c = text[i]
        # Double-quoted string: blank delimiters + body with spaces, keep newlines.
        if c == '"':
            j = i + 1
            terminated = False
            while j < n:
                cc = text[j]
                if cc == "\\" and j + 1 < n:
                    j += 2
                    continue
                if cc == '"':
                    j += 1
                    terminated = True
                    break
                if cc == "\n":
                    # Unterminated — stop the string here, leave the
                    # newline for outer processing.
                    break
                j += 1
            if not terminated and j < n and text[j] == "\n":
                # Blank the string opener through j-1 (the chars before \n);
                # leave the newline itself untouched so the line ends.
                for k in range(i, j):
                    out.append("\n" if text[k] == "\n" else " ")
                i = j
                continue
            for k in range(i, j):
                out.append("\n" if text[k] == "\n" else " ")
            i = j
            continue
        # Line comment: blank up to (but not including) the newline.
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            end = text.find("\n", i + 2)
            if end == -1:
                end = n
            out.append(" " * (end - i))
            i = end
            continue
        # Block comment: blank delimiters + body with spaces, preserving newlines.
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            depth = 1
            j = i + 2
            while j < n and depth > 0:
                if j + 1 < n and text[j] == "/" and text[j + 1] == "*":
                    depth += 1
                    j += 2
                    continue
                if j + 1 < n and text[j] == "*" and text[j + 1] == "/":
                    depth -= 1
                    j += 2
                    continue
                j += 1
            if depth > 0:
                out.append(text[i:])
                i = n
                break
            stop = j
            for ch in text[i:stop]:
                out.append("\n" if ch == "\n" else " ")
            i = stop
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _assert_no_unsupported_swift_constructs(text: str) -> None:
    """Fail loudly if `text` contains Swift constructs the balanced-paren
    walker can't reason about.

    The walker handles double-quoted strings (with backslash escapes),
    line comments (//) and block comments. It does NOT handle Swift raw
    strings (#"..."#), multi-line triple-quoted strings, or string
    interpolation: parens inside an interpolated expression would fool
    the depth counter, and unescaped quotes inside a raw string would
    confuse the string-skip state.

    To avoid flagging false positives on doc comments that legitimately
    mention these constructs (e.g. `/// Uses #"..."# internally`), we
    scan a comment-stripped view of the manifest. Real code uses of
    the constructs still fire; mentions inside `//`, `/* */`, or `///`
    doc comments pass through untouched.

    The check remains a heuristic gate, not a full parser. Known
    limitations:
      - Doc comments inside regular strings are stripped, since the
        stripper follows the string-state machine. This is the same
        behavior as the downstream `_balanced_close` walker.
      - A string literal like `let s = "#\\"hi\\"#"` looks the same
        to the scanner as a real raw-string use, so the check will
        reject it. Real manifests don't write strings like this.
    """
    scanned = _strip_swift_comments(text)
    if '#"' in scanned:
        raise PrepareUserError(
            "Package.swift uses Swift raw string literals (`#\"...\"#`), "
            "which the balanced-paren walker doesn't understand. "
            "File a bug if this needs to be supported."
        )
    if '"""' in scanned:
        raise PrepareUserError(
            "Package.swift uses Swift triple-quoted strings (`\"\"\"`), "
            "which the balanced-paren walker doesn't understand. "
            "File a bug if this needs to be supported."
        )
    if '\\(' in scanned:
        raise PrepareUserError(
            "Package.swift uses Swift string interpolation (`\\(...)`), "
            "which the balanced-paren walker doesn't understand. "
            "File a bug if this needs to be supported."
        )


def _balanced_close(text: str, open_idx: int) -> int:
    """Walk text from `open_idx` (which must point at one of `(`, `[`, `{`)
    to the matching closing bracket, returning its index. Skips over Swift
    string literals (`"..."` with `\\"` escapes), `// ...` line comments, and
    `/* ... */` block comments. Returns -1 if no matching close is found.

    Does NOT handle Swift multi-line triple-quoted strings, raw strings,
    or string interpolation. Callers should run
    `_assert_no_unsupported_swift_constructs` on the full manifest text
    before invoking this walker so unsupported syntax fails loudly with a
    targeted PrepareError instead of being silently mis-parsed.
    """
    if open_idx < 0 or open_idx >= len(text):
        return -1
    open_ch = text[open_idx]
    pair = {"(": ")", "[": "]", "{": "}"}
    if open_ch not in pair:
        return -1
    close_ch = pair[open_ch]

    depth = 0
    i = open_idx
    n = len(text)
    while i < n:
        c = text[i]
        # Line comment: skip to end-of-line.
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            nl = text.find("\n", i + 2)
            if nl == -1:
                return -1
            i = nl + 1
            continue
        # Block comment: skip past the matched close. Swift block
        # comments NEST, so we track depth rather than stopping at
        # the first `*/`.
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            block_depth = 1
            i += 2
            while i < n and block_depth > 0:
                if i + 1 < n and text[i] == "/" and text[i + 1] == "*":
                    block_depth += 1
                    i += 2
                    continue
                if i + 1 < n and text[i] == "*" and text[i + 1] == "/":
                    block_depth -= 1
                    i += 2
                    continue
                i += 1
            if block_depth > 0:
                return -1
            continue
        # String literal: skip to closing quote, honoring `\\"` escapes.
        if c == '"':
            i += 1
            while i < n:
                cc = text[i]
                if cc == "\\" and i + 1 < n:
                    i += 2
                    continue
                if cc == '"':
                    i += 1
                    break
                if cc == "\n":
                    # Unterminated string — give up rather than misparse.
                    return -1
                i += 1
            continue
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _swift_string_literal(s: str) -> str:
    """Quote `s` as a Swift string literal that's safe inside Package.swift.

    Escapes the two characters that can break a `"..."` literal — backslash
    and double-quote. Newlines, raw strings, and interpolation are
    deliberately not handled: Package.swift values that need them aren't
    inputs we'll ever pass through here (target names and on-disk
    xcframework paths).
    """
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


_TARGET_CALL_KIND_RE = re.compile(r"\.(target|executableTarget|testTarget)\s*\(")
_BINARY_TARGET_CALL_RE = re.compile(r"\.binaryTarget\s*\(")
_LIBRARY_CALL_RE = re.compile(r"\.library\s*\(")
_TOP_LEVEL_NAME_LABEL_RE = re.compile(
    r'\bname\s*:\s*"((?:[^"\\\n]|\\.)*)"'
)


def _flatten_to_top_level(span: str) -> str:
    """Collapse `span` to a string preserving only its depth-0
    characters. Nested paren / bracket / brace bodies (including their
    delimiters) are replaced with a single space, while strings and
    comments at depth 0 are kept verbatim so a regex looking for a
    labelled string argument still sees its literal value.

    The returned string has different offsets from `span`. Callers that
    need offsets back into the original buffer must do their own
    bookkeeping; the helper is only meant for "does the top-level
    argument list say `name: \"X\"`?" style queries.

    This is the depth-0 sieve underpinning `_top_level_name_label`,
    which `_find_target_call_for_name` and `_has_binary_target_with_name`
    use to ignore `name:` references nested inside a target's
    `dependencies:` array (e.g. `.target(name: "Dep")` expressions).
    """
    out: List[str] = []
    i = 0
    n = len(span)
    while i < n:
        c = span[i]
        if c == "/" and i + 1 < n and span[i + 1] == "/":
            nl = span.find("\n", i + 2)
            i = nl + 1 if nl != -1 else n
            continue
        if c == "/" and i + 1 < n and span[i + 1] == "*":
            block_depth = 1
            i += 2
            while i < n and block_depth > 0:
                if i + 1 < n and span[i] == "/" and span[i + 1] == "*":
                    block_depth += 1
                    i += 2
                    continue
                if i + 1 < n and span[i] == "*" and span[i + 1] == "/":
                    block_depth -= 1
                    i += 2
                    continue
                i += 1
            continue
        if c == '"':
            j = i + 1
            while j < n:
                cc = span[j]
                if cc == "\\" and j + 1 < n:
                    j += 2
                    continue
                if cc == '"':
                    j += 1
                    break
                if cc == "\n":
                    break
                j += 1
            out.append(span[i:j])
            i = j
            continue
        if c in "([{":
            close = _balanced_close(span, i)
            if close == -1:
                # Malformed nested scope — bail out cleanly so the
                # caller's regex sees what we collected so far rather
                # than raising. `_balanced_close == -1` already implies
                # a manifest the rest of the pipeline will reject.
                break
            out.append(" ")
            i = close + 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _top_level_name_label(span: str) -> Optional[str]:
    """Return the value of the `name:` keyword argument at the top
    level of `span`, or None if no such top-level label is present.

    "Top level" means depth 0 — not inside any nested
    paren/bracket/brace. So a call body like
    `name: "A", dependencies: [.target(name: "Foo")]` returns "A",
    not "Foo": the inner `name:` is at depth 2 (`[` then `(`) and
    gets stripped by `_flatten_to_top_level` before the regex runs.

    Used by `_find_target_call_for_name` and
    `_has_binary_target_with_name` to verify a candidate call's OWN
    name argument before accepting the match. Regression for the bug
    where the planner's substitution edited the wrong target because
    a sibling's `dependencies:` list happened to mention the target's
    name as a `.target(name: "X")` expression.
    """
    flat = _flatten_to_top_level(span)
    m = _TOP_LEVEL_NAME_LABEL_RE.search(flat)
    if not m:
        return None
    return m.group(1)


def _find_target_call_for_name(
    text: str, target_name: str
) -> Tuple[int, int, str]:
    """Locate the `.target(name: "<target_name>", ...)` call (or
    `.executableTarget` / `.testTarget`) whose own top-level `name:`
    argument matches exactly.

    Returns `(call_start_idx, close_paren_idx, kind_used)` where
    `call_start_idx` is the offset of the leading `.` and
    `close_paren_idx` is the offset of the matching `)`. `kind_used` is
    one of `"target"`, `"executableTarget"`, `"testTarget"`.

    Returns `(-1, -1, "")` if no matching call is found. Deliberately
    skips `.binaryTarget(...)` and `.systemLibrary(...)` — substituting
    them is either pointless (already binary) or unsupported (not a real
    target we can build into an xcframework).

    Two filtering disciplines are stacked:

    1. Candidate discovery runs against a code-only view of the
       manifest (`_make_code_token_view`, which blanks `//` line
       comments, `/* */` block comments, AND `"..."` string-literal
       spans into equal-length spaces while preserving offsets). A
       commented-out OR string-literal-embedded `.target(name: "X")`
       cannot match — its leading `.target(` was blanked. (Codex P2
       round-2 regression: a commented decl before the real one
       caused the edit to rewrite the comment. Codex P2 round-3
       regression: a string literal whose body contained
       `.target(name: "X", ...)` text caused the finder to stop on
       the string and raise a spurious PrepareUserError.)

    2. The match's own top-level `name:` argument is verified via
       `_top_level_name_label`. A sibling target whose `dependencies:`
       array contains `.target(name: "Foo")` would otherwise falsely
       match when searching for "Foo". (Codex P1 regression.)
    """
    code_view = _make_code_token_view(text)
    pos = 0
    while True:
        m = _TARGET_CALL_KIND_RE.search(code_view, pos)
        if not m:
            return -1, -1, ""
        kind = m.group(1)
        kind_start = m.start()
        open_idx = m.end() - 1
        close_idx = _balanced_close(text, open_idx)
        if close_idx == -1:
            raise PrepareUserError(
                f"Could not find balanced `)` for .{kind}( at offset "
                f"{open_idx}; the manifest may be malformed."
            )
        span = text[open_idx + 1 : close_idx]
        if _top_level_name_label(span) == target_name:
            return kind_start, close_idx, kind
        pos = close_idx + 1


def _has_binary_target_with_name(text: str, target_name: str) -> bool:
    """True iff `text` contains a `.binaryTarget(name: "<target_name>", ...)`
    call whose top-level `name:` argument matches exactly. Same
    code-only + depth-0 discipline as `_find_target_call_for_name`:
    a stray `.binaryTarget(name: "X")` mentioned inside another call's
    body, sitting inside a `//`/`/* */` comment, or embedded in a
    `"..."` string literal can't false-positive the idempotency check.
    """
    code_view = _make_code_token_view(text)
    pos = 0
    while True:
        m = _BINARY_TARGET_CALL_RE.search(code_view, pos)
        if not m:
            return False
        open_idx = m.end() - 1
        close_idx = _balanced_close(text, open_idx)
        if close_idx == -1:
            return False
        span = text[open_idx + 1 : close_idx]
        if _top_level_name_label(span) == target_name:
            return True
        pos = close_idx + 1


def edit_replace_with_binary_target(
    manifest_text: str, target_name: str, xcframework_path: str
) -> str:
    """Rewrite the `.target(name: T, ...)` (or `.executableTarget` /
    `.testTarget`) declaration for `target_name` to a
    `.binaryTarget(name: T, path: P)` referring to `xcframework_path`.

    This is the dedup-overlap edit (REWRITE_DESIGN.md §5.4): when an
    umbrella product depends on a sibling target that we've already
    built into a dynamic xcframework, swap the source target for a
    binary one. SPM then resolves the umbrella's link to that
    pre-existing dylib instead of re-compiling and statically embedding
    every sibling's symbols.

    Idempotent: if the manifest already declares `.binaryTarget(name: T,
    ...)` and no remaining `.target(name: T, ...)`, the function
    returns the text unchanged. The reverse asymmetry — both shapes
    present for the same name — is treated as a malformed manifest
    (likely corruption from a half-applied prior edit) and raises
    `PrepareUserError`.

    Parameters:
      manifest_text     — full Package.swift text to edit
      target_name       — exact name of the target to substitute
      xcframework_path  — path the new `.binaryTarget` will point at;
                          may be absolute or relative to the package
                          root (SPM accepts both)

    Returns the edited manifest text. Caller writes it back.
    """
    binary_present = _has_binary_target_with_name(manifest_text, target_name)
    kind_start, close_idx, _kind = _find_target_call_for_name(
        manifest_text, target_name
    )

    if kind_start == -1:
        if binary_present:
            return manifest_text  # already a binaryTarget; idempotent no-op
        raise PrepareUserError(
            f"replace_with_binary_target: no `.target(name: {target_name!r}, "
            f"...)` (or .executableTarget/.testTarget) found in the manifest. "
            f"There is also no existing `.binaryTarget(name: {target_name!r}, "
            f"...)` — nothing matches the substitution request."
        )

    if binary_present:
        raise PrepareUserError(
            f"replace_with_binary_target: manifest contains BOTH a source "
            f"target and a `.binaryTarget` for {target_name!r}. The manifest "
            f"is in a half-edited state — a prior dedup-overlap run likely "
            f"failed mid-edit. Restore from .original-Package.swift and try "
            f"again."
        )

    name_literal = _swift_string_literal(target_name)
    path_literal = _swift_string_literal(xcframework_path)
    replacement = f".binaryTarget(name: {name_literal}, path: {path_literal})"
    return manifest_text[:kind_start] + replacement + manifest_text[close_idx + 1 :]


def _find_library_call_for_name(
    text: str, product_name: str
) -> Tuple[int, int]:
    """Locate the `.library(name: "<product_name>", ...)` call whose
    own top-level `name:` argument matches exactly.

    Returns `(open_paren_idx, close_paren_idx)` — the offsets of the
    matching `(` and `)`. Returns `(-1, -1)` if no matching library
    declaration is found.

    Same code-only + depth-0 filtering as `_find_target_call_for_name`
    so a `name:` reference inside a nested `targets:` array, a comment,
    or a string literal cannot false-match.
    """
    code_view = _make_code_token_view(text)
    pos = 0
    while True:
        m = _LIBRARY_CALL_RE.search(code_view, pos)
        if not m:
            return -1, -1
        open_idx = m.end() - 1
        close_idx = _balanced_close(text, open_idx)
        if close_idx == -1:
            raise PrepareUserError(
                f"Could not find balanced `)` for .library( at offset "
                f"{open_idx}; the manifest may be malformed."
            )
        span = text[open_idx + 1 : close_idx]
        if _top_level_name_label(span) == product_name:
            return open_idx, close_idx
        pos = close_idx + 1


_TYPE_DYNAMIC_ARG_RE = re.compile(
    r"(?P<lead>[,\s]*)type\s*:\s*\.dynamic\s*(?P<trail>,?)"
)


def _depth_zero_view(span: str) -> str:
    """Return a view of `span` where every character that is NOT at the
    top level (depth 0) of the span's call body is replaced by a single
    space. Comment bodies and string-literal bodies are also blanked.
    Length and per-character offsets are preserved one-to-one with
    `span`.

    This is the offset-preserving analogue of `_flatten_to_top_level`:
    `flatten` collapses nested scopes into single spaces (losing
    offsets), this preserves alignment so the caller can splice based
    on regex match indices into the ORIGINAL `span`.

    Used by `edit_demote_synthetic_product` to find the `type: .dynamic`
    argument's depth-0 location without false-matching a literal
    `type: .dynamic` that might appear inside a nested `targets:` array
    (defensive — unlikely in practice but cheap to guard against).
    """
    out = list(span)
    n = len(span)
    i = 0
    depth = 0
    while i < n:
        c = span[i]
        if c == "/" and i + 1 < n and span[i + 1] == "/":
            nl = span.find("\n", i + 2)
            end = nl if nl != -1 else n
            for j in range(i, end):
                out[j] = " "
            i = end
            continue
        if c == "/" and i + 1 < n and span[i + 1] == "*":
            block_depth = 1
            j = i + 2
            while j < n and block_depth > 0:
                if j + 1 < n and span[j] == "/" and span[j + 1] == "*":
                    block_depth += 1
                    j += 2
                    continue
                if j + 1 < n and span[j] == "*" and span[j + 1] == "/":
                    block_depth -= 1
                    j += 2
                    continue
                j += 1
            for k in range(i, j):
                out[k] = " "
            i = j
            continue
        if c == '"':
            j = i + 1
            while j < n:
                cc = span[j]
                if cc == "\\" and j + 1 < n:
                    j += 2
                    continue
                if cc == '"':
                    j += 1
                    break
                if cc == "\n":
                    break
                j += 1
            for k in range(i, j):
                out[k] = " "
            i = j
            continue
        if c in "([{":
            close = _balanced_close(span, i)
            if close == -1:
                # Malformed nesting — bail out cleanly. Subsequent regex
                # search will fail to find a match and the caller will
                # fall through to its idempotent no-op branch.
                break
            # Keep the open/close delimiters themselves at depth 0;
            # blank their interior so any depth-N content is invisible.
            for k in range(i + 1, close):
                if out[k] != "\n":
                    out[k] = " "
            i = close  # process the closing delimiter next iteration
            continue
        if depth != 0 and c != "\n":
            out[i] = " "
        i += 1
    return "".join(out)


def edit_demote_synthetic_product(
    manifest_text: str, product_name: str
) -> str:
    """Strip the `type: .dynamic` argument from the synthetic
    `.library(name: "<product_name>", type: .dynamic, targets: [...])`
    declaration, leaving the "automatic-library" shape
    `.library(name: "<product_name>", targets: [...])`.

    Why we need this: when the planner emits a `synth_dynamic_library`
    edit for product P over target T, then T is built into its own
    xcframework, dedup-overlap wants to rewrite later units'
    `.target(name: T, ...)` references to `.binaryTarget(name: T, ...)`.
    SPM rejects the resulting manifest because the surviving
    `.library(type: .dynamic, targets: [T])` product now points at a
    binary-only target:

        "invalid type for binary product; products referencing only
         binary targets must be executable or automatic library
         products"

    Demoting the product to "automatic library" (no `type:` argument)
    keeps the product alive — its name continues to appear in
    `swift package describe`, downstream consumers can still depend on
    it by product name — and unblocks the binaryTarget substitution.
    The product's emitted linkage becomes whatever SPM decides
    ("automatic"), but the linkage doesn't matter at consumer time:
    the consumer is linking the binary we already produced, not
    re-building T's sources.

    Idempotent: if the product is already in automatic-library shape
    (no top-level `type: .dynamic` argument inside the call body),
    the function returns the text unchanged. Raises `PrepareUserError`
    if no `.library(name: "<product_name>", ...)` is found.

    Parameters:
      manifest_text — full Package.swift text to edit
      product_name  — exact name of the product (the synthetic name,
                      e.g. `FooDynamic`)

    Returns the edited manifest text. Caller writes it back.
    """
    open_idx, close_idx = _find_library_call_for_name(
        manifest_text, product_name
    )
    if open_idx == -1:
        raise PrepareUserError(
            f"demote_synthetic_product: no `.library(name: {product_name!r}, "
            f"...)` declaration found in the manifest."
        )

    span = manifest_text[open_idx + 1 : close_idx]
    # Use the offset-preserving depth-0 view (NOT `_flatten_to_top_level`,
    # which collapses nested scopes into single spaces and breaks
    # offset alignment). A `type: .dynamic` literal nested inside the
    # call body (e.g. a hypothetical defensive case where some user
    # supplies a nested closure argument) won't false-match. The view
    # also blanks string-literal bodies into spaces — so we re-derive
    # the lead text from `span` (not from `m.group`), or the blanked
    # `"<name>"` between the previous arg and `type:` would be spliced
    # back in as raw whitespace.
    view = _depth_zero_view(span)
    m = _TYPE_DYNAMIC_ARG_RE.search(view)
    if m is None:
        return manifest_text  # already automatic-shape; idempotent no-op
    arg_start = m.start()
    arg_end = m.end()
    lead_in_span = span[arg_start : arg_start + len(m.group("lead"))]
    has_trailing_comma = bool(m.group("trail"))
    # Two argument-position cases:
    #
    # 1. `type:` is followed by another sibling (it captured a trailing
    #    comma). The separator BEFORE `type:` (a comma plus indent for
    #    the SPM multiline shape, or a `, ` for a single-line manifest)
    #    must stay — it now precedes the next sibling. We strip the
    #    trailing horizontal whitespace from that lead so the now-empty
    #    line doesn't leave phantom indentation. The comma+newline (and
    #    the preceding argument's text) survive.
    # 2. `type:` is the LAST argument (no trailing comma). The lead
    #    contains the comma that separated `type:` from the previous
    #    sibling — drop the entire lead so the previous sibling
    #    becomes the last argument and gets the closing `)`.
    if has_trailing_comma:
        new_span = (
            span[:arg_start] + lead_in_span.rstrip(" \t") + span[arg_end:]
        )
    else:
        new_span = span[:arg_start] + span[arg_end:]

    return (
        manifest_text[: open_idx + 1] + new_span + manifest_text[close_idx:]
    )


def _swift_toolchain_version() -> Optional[Tuple[int, int, int]]:
    """Return the Swift toolchain `(major, minor, patch)`, or None if it
    can't be parsed.

    Used by `_select_active_manifest` to mirror SPM's manifest-selection
    rule: SPM picks the highest `Package@swift-X.Y[.Z].swift` whose version
    is `<=` the active toolchain version, falling back to `Package.swift`.
    """
    try:
        cp = subprocess.run(
            ["swift", "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (FileNotFoundError, OSError):
        return None
    if cp.returncode != 0:
        return None
    m = re.search(r"Swift version (\d+)\.(\d+)(?:\.(\d+))?", cp.stdout or "")
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


_TOOLS_VERSION_RE = re.compile(
    r"//\s*swift-tools-version[: ]\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?"
)


def _read_declared_tools_version(path: Path) -> Optional[Tuple[int, int, int]]:
    """Parse the `// swift-tools-version: X.Y[.Z]` line from a manifest.

    SPM mandates this line as the very first line of every Package.swift
    (base or version-specific). Returns None on missing/unparseable lines.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for _ in range(8):
                line = fh.readline()
                if not line:
                    break
                m = _TOOLS_VERSION_RE.search(line)
                if m:
                    return (
                        int(m.group(1)),
                        int(m.group(2) or 0),
                        int(m.group(3) or 0),
                    )
    except OSError:
        return None
    return None


def _select_active_manifest(staged_dir: Path) -> Path:
    """Pick the Package.swift file SPM will actually read for this toolchain.

    Mirrors SPM's actual selection rule (SE-0152): for every candidate
    manifest in the package root (`Package.swift` and any sibling
    `Package@swift-X.Y[.Z].swift`), parse the `// swift-tools-version` line
    declared *inside* that file and pick the file whose declared
    tools-version is the highest one still `<=` the active toolchain.

    The filename's `@swift-X.Y` suffix is just a sort hint, not the
    selection key — Alamofire 5.10.2 ships `Package.swift` with
    `swift-tools-version: 6.0` plus `Package@swift-5.10.swift` (5.10) plus
    `Package@swift-5.9.swift` (5.9), and on a 6.x toolchain SPM picks
    `Package.swift` because 6.0 is the highest fitting tools-version.
    Selecting by filename alone — as the original implementation did —
    edits the wrong file and the round-trip validator catches it as a
    silent no-op when `dump-package` re-reads the real active manifest.

    Falls back to `Package.swift` if the toolchain version can't be parsed
    or if no manifest declares a tools-version we can read.
    """
    # Late-bind through the package namespace so tests that monkey-patch
    # `spm_to_xcframework._swift_toolchain_version` are honored at call
    # time. A direct local reference would freeze the toolchain probe
    # into this module's globals and silently ignore the patch.
    from . import _swift_toolchain_version  # noqa: F811 — late-bound shadow

    base = staged_dir / "Package.swift"
    tc = _swift_toolchain_version()
    if tc is None:
        return base

    candidates: List[Path] = []
    if base.is_file():
        candidates.append(base)
    candidates.extend(sorted(staged_dir.glob("Package@swift-*.swift")))

    best: Optional[Tuple[Tuple[int, int, int], Path]] = None
    for candidate in candidates:
        declared = _read_declared_tools_version(candidate)
        if declared is None:
            continue
        if declared > tc:
            continue
        # Tie-break by declared version, then prefer the un-suffixed
        # `Package.swift` over `Package@swift-X.Y.swift` on identical
        # tools-versions (matches SPM, which treats the base manifest as
        # canonical when both declare the same version).
        if best is None or declared > best[0] or (
            declared == best[0] and candidate.name == "Package.swift"
        ):
            best = (declared, candidate)
    return best[1] if best is not None else base


def _invoke_swift_add_product(
    staged_dir: Path, product_name: str, targets: Sequence[str]
) -> None:
    """Call `swift package add-product <name> --type dynamic-library --targets t1 t2 ...`
    in `staged_dir`.

    The targets argv must be SPACE-separated, NOT comma-separated. SwiftPM's
    `--targets <targets> ...` declaration is a variadic value list, so
    `--targets A,B,C` is parsed as the single literal `"A,B,C"` and ends up
    written to Package.swift as `targets: ["A,B,C"]`, which then fails
    `swift package resolve` with "no such target". `--targets A B C`
    produces `targets: ["A", "B", "C"]`, which is what we want.

    Failure surface: the subprocess can fail for several reasons — the
    package's tools-version is too old to support `add-product`, a product
    with the requested name already exists, or the manifest is malformed.
    We surface stderr verbatim so the user can read the SPM error.

    Tools-version-too-old is the one failure mode the user can fix
    without filing a planner bug (bump the package's
    `swift-tools-version:` line, or fall back to the legacy regex-based
    surgery on an older spm-to-xcframework). That case is routed as
    `PrepareUserError` so the CLI renders the clean
    `Error (prepare): ...` envelope instead of a Python traceback.
    Everything else stays `PrepareBug`. (Grok final-review Low #2.)
    """
    argv = [
        "swift",
        "package",
        "add-product",
        product_name,
        "--type",
        "dynamic-library",
        "--targets",
        *list(targets),
    ]
    cp = subprocess.run(
        argv,
        cwd=str(staged_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if cp.returncode != 0:
        tail = "\n".join((cp.stderr or "").rstrip().splitlines()[-15:])
        stderr_lower = (cp.stderr or "").lower()
        if (
            "unknown command 'add-product'" in stderr_lower
            or "unknown subcommand 'add-product'" in stderr_lower
            or ("add-product" in stderr_lower and "requires" in stderr_lower)
            or ("tools-version" in stderr_lower and "add-product" in stderr_lower)
        ):
            raise PrepareUserError(
                f"`swift package add-product` is not available in this "
                f"toolchain for {staged_dir}. The package's "
                f"swift-tools-version may be too old (add-product was added "
                f"in 5.2) — bump the manifest to a supported version, or "
                f"pin to an older spm-to-xcframework that used regex-based "
                f"Package.swift edits.\n" + (tail or "  (no stderr)")
            )
        raise PrepareBug(
            f"`swift package add-product {product_name}` failed in "
            f"{staged_dir}:\n" + (tail or "  (no stderr)")
        )


def apply_package_swift_edits(staged_dir: Path, plan: Plan) -> str:
    """Apply `plan.package_swift_edits` to the active Package.swift in
    `staged_dir`.

    "Active" means the manifest SPM actually reads for this toolchain —
    if the package ships a `Package@swift-X.Y.swift` whose version is
    `<=` the active Swift toolchain, that file is mutated *instead of*
    `Package.swift`. This matches what `swift package dump-package` and
    `xcodebuild` see, so the planner's edit decisions (which were derived
    from the same dump) line up with the file we mutate.

    All synth_* edits are applied by shelling out to `swift package
    add-product`, which mutates the active manifest in place. We snapshot
    the original first so the round-trip validator can render a unified
    diff on failure.

    Side effects:
      - Writes `.original-<basename>` next to the active manifest as a
        debugging artifact (only if not already present from a prior run).
      - Invokes `swift package add-product` once per synth_* edit.

    Returns the original (pre-edit) manifest text. The caller passes that
    to `validate_prepared_manifest` so a validation failure can render a
    diff against the same baseline this function used.
    """
    manifest_path = _select_active_manifest(staged_dir)
    if not manifest_path.is_file():
        raise PrepareBug(f"No Package.swift at {manifest_path}")

    original_text = manifest_path.read_text()
    # Sanity check: the manifest must not use Swift constructs the
    # `replace_with_binary_target` walker can't reason about. Even though
    # the synth_* edits run through `swift package add-product` (which
    # parses the manifest properly), Execute may later apply a
    # `replace_with_binary_target` edit using our balanced-paren walker,
    # so we still need to gate raw strings / triple-quotes / interpolation
    # up front and abort with a clear message.
    _assert_no_unsupported_swift_constructs(original_text)

    # Snapshot the original for debugging. Don't clobber a prior snapshot:
    # if a previous run failed mid-Prepare, the .original from that run is
    # the *real* original and overwriting it would lose information.
    snapshot_path = manifest_path.parent / f".original-{manifest_path.name}"
    if not snapshot_path.exists():
        snapshot_path.write_text(original_text)

    # Apply edits in plan order so the post-edit products array reflects
    # planner sequencing. synth_dynamic_library and synth_library are both
    # implemented by the same `swift package add-product` call; the only
    # difference is naming policy at plan time.
    for edit in plan.package_swift_edits:
        if edit.kind in ("synth_dynamic_library", "synth_library"):
            _invoke_swift_add_product(
                staged_dir, edit.product_name, edit.targets
            )

    return original_text


def validate_prepared_manifest(
    staged_dir: Path,
    plan: Plan,
    original_text: str,
) -> Tuple[dict, List[Product], List[Target], List[Platform], str, str]:
    """Round-trip the edited Package.swift through `swift package dump-package`
    and assert every planner-requested edit landed correctly.

    On success, returns the parsed dump shards (the same shape `dump_package`
    returns) so the caller can build a Package model with whatever schemes
    list it wants attached. On failure, raises PrepareError.

    Asserts (per design §5.3):
      1. The dump still parses (handled by raising InspectError → caught
         and re-raised as PrepareError with diff context).
      2. Every `synth_dynamic_library` / `synth_library` edit produced a
         product with the synthetic name, linkage DYNAMIC, and the
         requested target list.
      3. Every build unit the planner intends to archive (i.e. not a
         copy-artifact) corresponds to a present product in the dumped
         manifest — looked up by `unit.scheme` because that is the name
         xcodebuild actually builds. For `synth_dynamic_library` units,
         `scheme` is the synthetic product name; the framework rename to
         `unit.framework_name` happens in Execute, not here.

    On any failure, raises PrepareError with the failed assertion(s), a
    unified diff between the pre-edit and post-edit manifests, and a JSON
    snapshot of any directly-implicated post-edit products.
    """
    manifest_path = _select_active_manifest(staged_dir)
    edited_text = manifest_path.read_text() if manifest_path.is_file() else ""

    try:
        raw, products, targets, platforms, name, tools_version = dump_package(staged_dir)
    except InspectError as exc:
        diff = _unified_diff(original_text, edited_text, label=manifest_path.name)
        raise PrepareBug(
            f"Round-trip validation failed: edited {manifest_path.name} no longer "
            f"parses through `swift package dump-package`.\n\nUnderlying "
            f"error:\n  {exc}\n\nUnified diff (original → edited):\n{diff}"
        ) from exc

    products_by_name = {p.name: p for p in products}
    failures: List[str] = []
    implicated: List[Product] = []

    for edit in plan.package_swift_edits:
        if edit.kind not in ("synth_dynamic_library", "synth_library"):
            continue
        prod = products_by_name.get(edit.product_name)
        if prod is None:
            failures.append(
                f"{edit.kind}: product {edit.product_name!r} is absent from "
                f"the post-edit dumped manifest (the synthetic .library() "
                f"entry didn't take effect)"
            )
            continue
        if prod.linkage != Linkage.DYNAMIC:
            failures.append(
                f"{edit.kind}: product {edit.product_name!r} has linkage "
                f"{prod.linkage!r}; expected {Linkage.DYNAMIC!r}"
            )
            implicated.append(prod)
        expected_targets = list(edit.targets)
        if list(prod.targets) != expected_targets:
            failures.append(
                f"{edit.kind}: product {edit.product_name!r} targets are "
                f"{prod.targets!r}; expected {expected_targets!r}"
            )
            implicated.append(prod)

    # Cross-check: every non-copy-artifact build unit must correspond to a
    # present product. Try `unit.framework_name` first (the original
    # product name, present for already-dynamic units and still present
    # alongside the synthetic for synth_dynamic_library units). Fall back
    # to `unit.scheme` so synth_library units (where the new product
    # equals the requested target name) and synth_dynamic_library units
    # (where the synthetic product is what xcodebuild actually builds)
    # are both accepted. For non-synth units whose scheme is the
    # `<product>-Package` variant, the framework_name branch already
    # matched; the scheme fallback never trips on those.
    for unit in plan.build_units:
        if unit.archive_strategy == "copy-artifact":
            continue
        if unit.framework_name in products_by_name:
            continue
        if unit.scheme in products_by_name:
            continue
        failures.append(
            f"build_unit {unit.name!r}: neither framework_name "
            f"{unit.framework_name!r} nor scheme {unit.scheme!r} is "
            f"present in the post-edit dumped manifest (planner expected "
            f"this product to exist)"
        )

    if failures:
        diff = _unified_diff(original_text, edited_text, label=manifest_path.name)
        impl_block = ""
        if implicated:
            seen = set()
            unique = []
            for p in implicated:
                if p.name in seen:
                    continue
                seen.add(p.name)
                unique.append(p)
            impl_lines = [
                f"  - {p.name}: linkage={p.linkage}, targets={p.targets}"
                for p in unique
            ]
            impl_block = "\n\nImplicated post-edit products:\n" + "\n".join(impl_lines)
        bullet = "\n".join(f"  - {f}" for f in failures)
        raise PrepareBug(
            "Round-trip validation failed:\n"
            + bullet
            + impl_block
            + "\n\nUnified diff (original → edited):\n"
            + diff
        )

    # Re-resolve dependencies whenever Prepare added a synthetic library
    # — new products can pull new transitive deps that need
    # .build/checkouts. Both synth_dynamic_library and synth_library
    # qualify.
    if any(
        e.kind in ("synth_dynamic_library", "synth_library")
        for e in plan.package_swift_edits
    ):
        cp = subprocess.run(
            ["swift", "package", "resolve"],
            cwd=str(staged_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if cp.returncode != 0:
            tail = "\n".join((cp.stderr or "").rstrip().splitlines()[-10:])
            raise PrepareBug(
                "swift package resolve failed after Prepare added synthetic "
                "libraries:\n" + (tail or "  (no stderr)")
            )

    return raw, products, targets, platforms, name, tools_version


def _unified_diff(original: str, edited: str, label: str = "Package.swift") -> str:
    """Render a unified diff between two manifest texts. Used in
    PrepareError messages so users (and the test harness) can see exactly
    what surgery Prepare attempted.

    Imported lazily because difflib is rarely needed at runtime — the
    happy path doesn't render diffs at all.
    """
    import difflib

    return "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            edited.splitlines(keepends=True),
            fromfile=f"{label} (pre-edit)",
            tofile=f"{label} (post-edit)",
            n=3,
        )
    )


def prepare(staged_dir: Path, plan: Plan, *, verbose: bool = False) -> PreparedPlan:
    """Top-level Prepare entry point.

    Applies the planner's whitelisted edits to `staged_dir/Package.swift`
    and runs the mandatory round-trip validator. Returns a PreparedPlan
    that wraps the original Plan and the post-edit Package model. Schemes
    from the inspect-time discovery are preserved on the returned Package
    (synthetic libraries don't need pre-discovered schemes — xcodebuild
    auto-generates them at archive time against our clean staged dir).
    """
    info("Preparing Package.swift edits...")
    active_manifest = _select_active_manifest(staged_dir)
    if active_manifest.name != "Package.swift":
        verbose_log(
            verbose,
            f"  Active manifest: {active_manifest.name} (selected by toolchain)",
        )
    no_op = not plan.package_swift_edits
    if no_op:
        verbose_log(verbose, "  No Package.swift edits requested by planner.")
        # Re-dump anyway so PreparedPlan.package reflects whatever the
        # current manifest says — Execute reads it for diagnostic context.
        raw, products, targets, platforms, name, tv = dump_package(staged_dir)
        return PreparedPlan(
            plan=plan,
            package=Package(
                name=name,
                tools_version=tv,
                platforms=platforms,
                products=products,
                targets=targets,
                schemes=[],
                raw_dump=raw,
                staged_dir=staged_dir,
            ),
        )

    for edit in plan.package_swift_edits:
        if edit.kind in ("synth_dynamic_library", "synth_library"):
            verbose_log(
                verbose,
                f"  edit: {edit.kind} {edit.product_name} → "
                f"targets={edit.targets}",
            )

    original = apply_package_swift_edits(staged_dir, plan)
    raw, products, targets, platforms, name, tv = validate_prepared_manifest(
        staged_dir, plan, original
    )
    success(f"  Prepare validated {len(plan.package_swift_edits)} edit(s) ✓")
    return PreparedPlan(
        plan=plan,
        package=Package(
            name=name,
            tools_version=tv,
            platforms=platforms,
            products=products,
            targets=targets,
            # The schemes the planner saw at inspect time still describe the
            # pre-edit manifest, but synthetic-library schemes are
            # auto-generated by xcodebuild at archive time against our clean
            # staged dir, so the build unit's `scheme` field is what Execute
            # actually consumes. We leave schemes empty here rather than
            # carrying a stale list across the Prepare boundary.
            schemes=[],
            raw_dump=raw,
            staged_dir=staged_dir,
        ),
    )
