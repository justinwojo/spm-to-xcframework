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

import os
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .errors import InspectError, PrepareBug, PrepareUserError, TargetCallNotFoundError
from .inspect import dump_package
from .log import info, success, verbose_log
from .model import (
    Linkage,
    MacroSupport,
    Package,
    Platform,
    Plan,
    PreparedPlan,
    Product,
    Target,
)


def _skip_triple_quoted_string(text: str, start: int) -> int:
    """Given `start` pointing at the first `"` of a triple-quote opener,
    return the index one past the closing triple-quote. Returns -1 if
    no matching closer is found.

    Caller must verify `text[start:start+3] == chr(34)*3` before calling.

    Handles `\\<x>` escape sequences. For `\\(...)` interpolation we
    recurse through `_balanced_close` to find the matching `)` — the
    embedded expression is arbitrary Swift, including its own string
    literals (single- or triple-quoted), and Swift legally allows a
    nested `\"\"\"...\"\"\"` literal inside the interpolation. Naively
    treating `\\(` as a 2-char escape would let a nested triple-quote
    masquerade as the outer closer.

    On a sequence of four or more consecutive `"`, the first three are
    the closer per Swift's lexer; the trailing quote belongs to
    whatever follows the multi-line string.
    """
    n = len(text)
    i = start + 3  # Skip opening `"""`.
    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n:
            if text[i + 1] == "(":
                close_idx = _balanced_close(text, i + 1)
                if close_idx == -1:
                    return -1
                i = close_idx + 1
                continue
            i += 2
            continue
        if c == '"' and i + 2 < n and text[i + 1] == '"' and text[i + 2] == '"':
            return i + 3
        i += 1
    return -1


def _strip_swift_comments(text: str) -> str:
    """Return `text` with `//` line comments and `/* */` block comments
    replaced by equal-length spans of spaces (preserving newlines). The
    length/offset preservation keeps any downstream index math valid,
    and preserving newlines keeps line-counting error messages honest.

    This is NOT a full Swift tokenizer — it tracks double-quoted and
    triple-quoted string state so comment markers inside a string
    literal don't trigger. The file-wide
    `_assert_no_unsupported_swift_constructs` gate runs AFTER this
    stripper and guards against the remaining advanced string shapes
    (raw strings, interpolation) that could otherwise fool the state
    machine.

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
        # Triple-quoted multi-line string: copy entire span verbatim.
        # MUST be checked before the single-quote handler since `"""`
        # begins with `"`. The body is real Swift source and the
        # stripper preserves source positions, so we keep every byte.
        if c == '"' and i + 2 < n and text[i + 1] == '"' and text[i + 2] == '"':
            close_idx = _skip_triple_quoted_string(text, i)
            if close_idx == -1:
                # Unterminated — copy the rest verbatim, matching the
                # block-comment fallthrough.
                out.append(text[i:])
                i = n
                break
            out.append(text[i:close_idx])
            i = close_idx
            continue
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

    Limitations match `_strip_swift_comments`: double-quoted (`"..."`)
    and triple-quoted multi-line strings are tracked. `#"..."#` raw
    strings aren't supported here
    — they would be rejected upstream by
    `_assert_no_unsupported_swift_constructs` before any prepare-time
    edit runs. `\\(...)` string interpolation IS handled by recursing
    through `_balanced_close` to find the closing `)` of the embedded
    expression, then resuming string mode — matching what
    `_balanced_close` itself does, so the two views agree on where
    strings end.

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
        # Triple-quoted multi-line string: blank delimiters + body with
        # spaces, preserve newlines. MUST be checked before the
        # single-quote handler since `"""` begins with `"`.
        if c == '"' and i + 2 < n and text[i + 1] == '"' and text[i + 2] == '"':
            close_idx = _skip_triple_quoted_string(text, i)
            if close_idx == -1:
                out.append(text[i:])
                i = n
                break
            for ch in text[i:close_idx]:
                out.append("\n" if ch == "\n" else " ")
            i = close_idx
            continue
        # Double-quoted string: blank delimiters + body with spaces, keep newlines.
        # Honors `\(...)` interpolation via recursion through `_balanced_close`,
        # so a string like `"Sources/\(name + "Tests")"` doesn't mis-terminate
        # on the inner `"` of the interpolation expression.
        if c == '"':
            j = i + 1
            terminated = False
            while j < n:
                cc = text[j]
                if cc == "\\" and j + 1 < n:
                    if text[j + 1] == "(":
                        close_idx = _balanced_close(text, j + 1)
                        if close_idx == -1:
                            # Unterminated interpolation — bail conservatively.
                            break
                        j = close_idx + 1
                        continue
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


def _blank_triple_quoted_bodies(text: str) -> str:
    # Return `text` with the PROSE inside every ordinary triple-quoted
    # multi-line string span replaced by spaces (newlines preserved).
    # "Prose" means the body chars between the triple-quote delimiters
    # AND outside any `\(...)` interpolation expression — interpolation
    # bodies are real Swift code and are preserved verbatim so the
    # downstream raw-string guard can still observe `#"` markers
    # nested inside them. The triple-quote delimiters themselves
    # (opener and closer) are also blanked.
    #
    # Raw triples (`#"""..."""#`, `##"""..."""##`, …) are left
    # ENTIRELY intact so the guard sees the leading `#"` and rejects
    # them — the helper can't safely walk a raw triple's body anyway
    # (raw strings close at `"""#` matching the leading hash count,
    # not at the first bare `"""`).
    #
    # Used by `_assert_no_unsupported_swift_constructs` so a `#"`
    # mention inside a legitimate `traits:` description (or any other
    # multi-line string body) does not trip the raw-string guard.
    # Single-quoted strings are intentionally left intact — the guard
    # still flags raw-string-shaped content inside them, matching the
    # documented heuristic.
    n = len(text)
    out = list(text)

    def _blank(k: int) -> None:
        if out[k] != "\n":
            out[k] = " "

    # Walker state stack. Each frame is one of:
    #   ("code", None)        — top-level or nested non-string code
    #   ("code", exit_at)     — code inside `\(...)` interpolation; on
    #                            entering an index > exit_at we pop back
    #                            to the enclosing prose context
    #   ("prose", None)       — body of an ordinary triple-quoted string
    stack: List[Tuple[str, Optional[int]]] = [("code", None)]

    i = 0
    while i < n:
        # Pop expired interpolation frames first.
        while True:
            mode, exit_at = stack[-1]
            if mode == "code" and exit_at is not None and i > exit_at:
                stack.pop()
                continue
            break

        mode, exit_at = stack[-1]
        c = text[i]

        if mode == "code":
            if c == '"' and i + 2 < n and text[i + 1] == '"' and text[i + 2] == '"':
                # Raw triple opener — leave the entire raw span intact
                # so the downstream `#"` scan sees the leading hash +
                # quote and rejects. Swift's raw-string rule: opener
                # `<N hashes>"""` matches closer `"""<N hashes>`. We
                # count the preceding hash run, scan forward for the
                # matching closer, and skip past the whole span.
                if i > 0 and text[i - 1] == "#":
                    hashes = 0
                    j = i - 1
                    while j >= 0 and text[j] == "#":
                        hashes += 1
                        j -= 1
                    end = -1
                    k = i + 3
                    while k + 2 + hashes < n:
                        if (
                            text[k] == '"'
                            and text[k + 1] == '"'
                            and text[k + 2] == '"'
                        ):
                            ok = True
                            for h in range(hashes):
                                if text[k + 3 + h] != "#":
                                    ok = False
                                    break
                            if ok:
                                end = k + 3 + hashes
                                break
                        k += 1
                    if end == -1:
                        # Unterminated raw triple — bail. The opening
                        # `#"` is still visible to the guard, so the
                        # manifest will be rejected anyway.
                        break
                    i = end
                    continue
                # Ordinary triple opener — blank the delimiters and
                # enter prose mode.
                _blank(i)
                _blank(i + 1)
                _blank(i + 2)
                stack.append(("prose", None))
                i += 3
                continue
            # Any other char in code mode is preserved as-is.
            i += 1
            continue

        # prose mode
        if c == "\\" and i + 1 < n:
            if text[i + 1] == "(":
                # Interpolation — preserve text from `\(` through the
                # matching `)` so any code (including `#"..."#`) stays
                # visible to the raw-string guard. We push a code
                # frame whose `exit_at` is the matching close paren;
                # the frame pops automatically on the next iteration
                # once `i` advances past it.
                close_idx = _balanced_close(text, i + 1)
                if close_idx == -1:
                    return "".join(out)
                stack.append(("code", close_idx))
                # The `\(` chars themselves are part of prose syntax
                # but we leave them in `out` verbatim — they don't
                # affect the `#"` scan and preserving them keeps the
                # source positions aligned with the original for
                # debugging.
                i += 2
                continue
            # Any other escape (e.g. `\"`, `\n`, `\u{...}`) is prose —
            # blank both characters.
            _blank(i)
            _blank(i + 1)
            i += 2
            continue
        if c == '"' and i + 2 < n and text[i + 1] == '"' and text[i + 2] == '"':
            # Closing triple of the current prose region.
            _blank(i)
            _blank(i + 1)
            _blank(i + 2)
            stack.pop()
            i += 3
            continue
        # Plain prose char.
        _blank(i)
        i += 1
    return "".join(out)


def _assert_no_unsupported_swift_constructs(text: str) -> None:
    """Fail loudly if `text` contains Swift constructs the balanced-paren
    walker can't reason about.

    The walker handles double-quoted strings (with backslash escapes),
    triple-quoted multi-line strings, line comments (//), block
    comments, and string interpolation (`\\(...)` via recursion
    through the walker). It does NOT handle Swift raw strings
    (`#"..."#`): unescaped quotes inside a raw string would confuse
    the string-skip state and the raw-delimiter terminator (`"#`)
    isn't tracked.

    To avoid flagging false positives on doc comments that legitimately
    mention this construct (e.g. `/// Uses #"..."# internally`), we
    scan a comment-stripped view of the manifest. Real code uses of
    the construct still fire; mentions inside `//`, `/* */`, or `///`
    doc comments pass through untouched. We also blank triple-quoted
    multi-line string bodies before scanning so a `traits:` description
    that legitimately mentions raw-string syntax in prose (e.g. a
    multi-line description containing the text `#"..."#`) doesn't
    falsely trigger.

    The check remains a heuristic gate, not a full parser. Known
    limitations:
      - Doc comments inside regular strings are stripped, since the
        stripper follows the string-state machine. This is the same
        behavior as the downstream `_balanced_close` walker.
      - A single-quoted string literal like `let s = "#\\"hi\\"#"` looks
        the same to the scanner as a real raw-string use, so the check
        will reject it. Real manifests don't write strings like this.
    """
    scanned = _blank_triple_quoted_bodies(_strip_swift_comments(text))
    if '#"' in scanned:
        raise PrepareUserError(
            "Package.swift uses Swift raw string literals (`#\"...\"#`), "
            "which the balanced-paren walker doesn't understand. "
            "File a bug if this needs to be supported."
        )
    # Triple-quoted strings (`"""..."""`) and `\\(...)` string interpolation
    # are both handled by the walker (the former via `_skip_triple_quoted_string`,
    # the latter via recursion through `_balanced_close`). Real-world manifests
    # use both heavily — swift-collections's traits carry `"""` description bodies
    # and its `Sources/\\(name)` path building uses interpolation.


def _balanced_close(text: str, open_idx: int) -> int:
    """Walk text from `open_idx` (which must point at one of `(`, `[`, `{`)
    to the matching closing bracket, returning its index. Skips over Swift
    string literals (double-quoted with `\\"` escapes, plus triple-quoted
    multi-line strings), `// ...` line comments, and `/* ... */` block
    comments. Returns -1 if no matching close is found.

    Does NOT handle Swift raw strings (`#"..."#`). `\\(...)` string
    interpolation IS handled by recursing through this same walker to
    find the closing `)` of the interpolation expression, then resuming
    string mode. Callers should still run
    `_assert_no_unsupported_swift_constructs` on the full manifest text
    so the unsupported constructs that remain fail loudly with a targeted
    PrepareError instead of being silently mis-parsed.
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
        # Triple-quoted multi-line string: skip to one past the closing
        # `"""`. MUST be checked before the single-quote handler since
        # `"""` begins with `"`. The interior is opaque text — we only
        # need to find the closer to resume bracket-balancing.
        if c == '"' and i + 2 < n and text[i + 1] == '"' and text[i + 2] == '"':
            close_idx = _skip_triple_quoted_string(text, i)
            if close_idx == -1:
                return -1
            i = close_idx
            continue
        # String literal: skip to closing quote, honoring `\\"` escapes
        # and `\(...)` interpolation. Interpolation embeds an arbitrary
        # Swift expression (which itself can contain strings, comments,
        # nested interpolation, and brackets) — we recurse through this
        # same walker to find the closing `)`, then resume string mode.
        if c == '"':
            i += 1
            while i < n:
                cc = text[i]
                if cc == "\\" and i + 1 < n:
                    if text[i + 1] == "(":
                        close_idx = _balanced_close(text, i + 1)
                        if close_idx == -1:
                            return -1
                        i = close_idx + 1
                        continue
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


# `.macro(...)` targets carry a `dependencies:` array exactly like the
# other source-target kinds, so every dependency-graph rewrite in this
# module (external-product → string-dep, orphan-`.product()` strip,
# search-path closure) must reach them too. A macro target left with a
# `.product(name:, package: ID)` reference to a package we consumed as an
# overlay (or otherwise stripped) dangles as "unknown package 'ID' in
# dependencies of target '<macro>'" at dump-package time — the exact
# swift-navigation 2.10.3 failure (SwiftNavigationMacros → swift-case-paths).
# `.macro` targets are never build units (the `--target` escape hatch
# refuses them, and dedup only substitutes single-target *built* siblings),
# so `_find_target_call_for_name` can never hand a macro target to
# `edit_replace_with_binary_target`.
_TARGET_CALL_KIND_RE = re.compile(
    r"\.(target|executableTarget|testTarget|macro)\s*\("
)
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
        # Triple-quoted multi-line string: copy verbatim. MUST be checked
        # before the single-quote handler since `"""` begins with `"`.
        if c == '"' and i + 2 < n and span[i + 1] == '"' and span[i + 2] == '"':
            close_idx = _skip_triple_quoted_string(span, i)
            if close_idx == -1:
                break
            out.append(span[i:close_idx])
            i = close_idx
            continue
        if c == '"':
            j = i + 1
            while j < n:
                cc = span[j]
                if cc == "\\" and j + 1 < n:
                    if span[j + 1] == "(":
                        close_idx = _balanced_close(span, j + 1)
                        if close_idx == -1:
                            break
                        j = close_idx + 1
                        continue
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


_PACKAGE_CALL_RE = re.compile(r"\bPackage\s*\(")
_PRODUCTS_LABEL_RE = re.compile(r"\bproducts\s*:")
_TARGETS_LABEL_RE = re.compile(r"\btargets\s*:")
_OVERLAY_BINARY_TARGET_RE = re.compile(r"\bTarget\s*\.\s*binaryTarget\s*\(")

# Sentinel comments wrap the auto-generated overlay block so a later
# dedup-overlap pass can find, parse, and extend its own previous
# output without trying to re-parse free-form Swift.
_OVERLAY_SENTINEL_BEGIN = (
    "// spm-to-xcframework dedup-overlap overlay — begin "
    "(auto-generated, do not edit)"
)
_OVERLAY_SENTINEL_END = "// spm-to-xcframework dedup-overlap overlay — end"
_OVERLAY_TARGETS_VAR = "_SPM2XC_OVERLAY_TARGETS"
_OVERLAY_NAMES_VAR = "_SPM2XC_OVERLAY_NAMES"


def edit_append_synth_product_to_package(
    manifest_text: str, product_name: str, targets: Sequence[str]
) -> str:
    """Append `.library(name: <product_name>, type: .dynamic, targets:
    [<targets>])` to the top-level `Package(...)` call's `products:`
    argument by rewriting `products: <expr>` to `products: <expr> +
    [<synth>]`.

    Fallback for `swift package add-product` failures when the manifest's
    `products:` argument is not a literal array. Real example:
    swift-collections 1.1.4 builds its product list programmatically
    (`products: _products`), which SPM rejects with "unable to find
    array literal for 'products' argument".

    The `(<expr>) + [<synth>]` rewrite is well-typed both for literal
    arrays and for any `[Product]`-typed expression, so we don't need
    to locate the closing `]` of an array — we just splice at the
    expression's end (next top-level comma or the closing `)` of the
    Package call). The original expression is wrapped in parens to defend
    against precedence surprises: a manifest written as
    `products: cond ? a : b` would, without the wrap, parse as
    `products: cond ? a : (b + [<synth>])` because `+` binds tighter than
    `?:`. The same hazard applies to `??`. The synthetic entry's type is
    always `.dynamic` because that's the only kind of synthetic library
    we currently emit via this path; `synth_library` (non-dynamic) goes
    through add-product on packages whose manifest shape allows it.
    """
    code_view = _make_code_token_view(manifest_text)
    m = _PACKAGE_CALL_RE.search(code_view)
    if not m:
        raise PrepareUserError(
            "synth-product fallback: no top-level `Package(` call found "
            "in the manifest."
        )
    pkg_open = m.end() - 1
    pkg_close = _balanced_close(manifest_text, pkg_open)
    if pkg_close == -1:
        raise PrepareUserError(
            "synth-product fallback: unmatched `(` for `Package(`; "
            "manifest may be malformed."
        )
    body_start = pkg_open + 1
    body_end = pkg_close
    body = manifest_text[body_start:body_end]
    body_dz = _depth_zero_view(body)
    lm = _PRODUCTS_LABEL_RE.search(body_dz)
    if not lm:
        raise PrepareUserError(
            "synth-product fallback: no top-level `products:` argument "
            "inside the Package(...) call. The manifest shape is not "
            "supported by this fallback."
        )
    # Bound the products expression: from just after `products:` (skipping
    # any leading horizontal whitespace) to the next top-level comma or
    # the end of the Package body. Walk back over trailing whitespace so
    # the closing `)` lands flush against the expression instead of
    # swallowing pre-comma indentation.
    expr_start_in_body = lm.end()
    while (
        expr_start_in_body < len(body)
        and body[expr_start_in_body] in " \t"
    ):
        expr_start_in_body += 1
    comma_idx = body_dz.find(",", expr_start_in_body)
    expr_end_in_body = comma_idx if comma_idx != -1 else len(body)
    while (
        expr_end_in_body > expr_start_in_body
        and body[expr_end_in_body - 1] in " \t\n"
    ):
        expr_end_in_body -= 1

    expr_start_offset = body_start + expr_start_in_body
    expr_end_offset = body_start + expr_end_in_body

    targets_list = ", ".join(_swift_string_literal(t) for t in targets)
    synth_tail = (
        f") + [.library(name: {_swift_string_literal(product_name)}, "
        f"type: .dynamic, targets: [{targets_list}])]"
    )
    # Wrap the original expression in parens, then append `+ [<synth>]`.
    # Done as a single splice so the two edits don't disturb each other's
    # offsets.
    return (
        manifest_text[:expr_start_offset]
        + "("
        + manifest_text[expr_start_offset:expr_end_offset]
        + synth_tail
        + manifest_text[expr_end_offset:]
    )


# Detect a file-scope `<var>.products = [` assignment — the post-init
# mutation shape that overwrites whatever `products:` value was passed to
# the `Package(...)` initializer. The `(?<![.\w])` lookbehind keeps us
# from matching `self.pkg.products` or any other dotted prefix; the `\1`
# capture gives us the variable name so we can emit a matching
# `<var>.products.append(...)` statement.
_POST_INIT_PRODUCTS_ASSIGN_RE = re.compile(
    r"(?<![.\w])(\w+)\.products\s*=\s*\["
)


def edit_append_synth_to_post_init_products(
    manifest_text: str, product_name: str, targets: Sequence[str]
) -> Optional[str]:
    """If `manifest_text` contains a file-scope `<var>.products = [...]`
    assignment, append our synthetic dynamic library via
    `<var>.products.append(.library(name: <product>, type: .dynamic,
    targets: [...]))` right after the assignment. Returns the edited
    manifest, or `None` if no such assignment exists (caller should fall
    through to the regular add-product flow).

    Why this exists: `swift package add-product` injects `products:` into
    the `Package(...)` initializer's argument list. When the manifest
    then overwrites `pkg.products = [...]` AFTER the initializer returns
    (PromiseKit 8.1.2's shape), the injection is silently clobbered and
    our synth library vanishes from `dump-package`. Detecting and
    appending here keeps the author's product list intact AND ensures
    our synth survives.

    The LAST matching assignment wins — sequential Swift evaluation
    means a later assignment would overwrite an earlier one, so we
    splice past the last `<var>.products = [...]` we can find.
    """
    code_view = _make_code_token_view(manifest_text)
    last_match = None
    for m in _POST_INIT_PRODUCTS_ASSIGN_RE.finditer(code_view):
        last_match = m
    if last_match is None:
        return None

    var_name = last_match.group(1)
    bracket_open = last_match.end() - 1  # points at `[`
    bracket_close = _balanced_close(manifest_text, bracket_open)
    if bracket_close == -1:
        return None

    insert_at = bracket_close + 1
    if insert_at < len(manifest_text) and manifest_text[insert_at] == ";":
        insert_at += 1

    targets_list = ", ".join(_swift_string_literal(t) for t in targets)
    append_stmt = (
        f"\n{var_name}.products.append("
        f".library(name: {_swift_string_literal(product_name)}, "
        f"type: .dynamic, targets: [{targets_list}]))"
    )
    return manifest_text[:insert_at] + append_stmt + manifest_text[insert_at:]


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
        # Raise the narrower TargetCallNotFoundError so the dedup-overlap
        # router can catch it specifically and fall back to the overlay
        # branch (manifests whose targets come from a factory wrapper like
        # RxSwift's `static func rxTarget(...) -> Target` have no literal
        # `.target(name: T, ...)` for us to find — the overlay path
        # doesn't need one).
        raise TargetCallNotFoundError(
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


def _parse_overlay_entries(block_text: str) -> Dict[str, str]:
    """Parse `Target.binaryTarget(name: "N", path: "P")` entries out of
    an overlay block's array literal. Returns name → path. Strict by
    design — the block is sentinel-bounded auto-generated text whose
    shape we fully control, so any parse failure indicates corruption.
    """
    entries: Dict[str, str] = {}
    pos = 0
    while True:
        m = _OVERLAY_BINARY_TARGET_RE.search(block_text, pos)
        if not m:
            return entries
        open_idx = m.end() - 1
        close_idx = _balanced_close(block_text, open_idx)
        if close_idx == -1:
            raise PrepareUserError(
                "overlay edit: malformed Target.binaryTarget(...) call in "
                "the existing overlay block (unbalanced parens)."
            )
        span = block_text[open_idx + 1 : close_idx]
        flat = _flatten_to_top_level(span)
        name_m = re.search(
            r'\bname\s*:\s*"((?:[^"\\\n]|\\.)*)"', flat
        )
        path_m = re.search(
            r'\bpath\s*:\s*"((?:[^"\\\n]|\\.)*)"', flat
        )
        if not name_m or not path_m:
            raise PrepareUserError(
                "overlay edit: Target.binaryTarget entry inside the overlay "
                "block is missing `name:` or `path:`."
            )
        entries[name_m.group(1)] = path_m.group(1)
        pos = close_idx + 1


def _render_overlay_block(entries: Dict[str, str]) -> str:
    """Render an overlay block from `entries` (name → path), with stable
    sort order (by name). Sentinel-wrapped so subsequent calls can find
    and rewrite it. Trailing newline ensures the block sits on its own
    lines.
    """
    lines = [_OVERLAY_SENTINEL_BEGIN]
    lines.append(f"let {_OVERLAY_TARGETS_VAR}: [Target] = [")
    for name in sorted(entries):
        lines.append(
            f"  Target.binaryTarget("
            f"name: {_swift_string_literal(name)}, "
            f"path: {_swift_string_literal(entries[name])}),"
        )
    lines.append("]")
    lines.append(
        f"let {_OVERLAY_NAMES_VAR}: Set<String> = "
        f"Set({_OVERLAY_TARGETS_VAR}.map {{ $0.name }})"
    )
    lines.append(_OVERLAY_SENTINEL_END)
    return "\n".join(lines) + "\n"


def edit_inject_or_extend_overlay_binary_targets(
    manifest_text: str, substitutions: Sequence[Tuple[str, str]]
) -> str:
    """Dedup-overlap edit for wrapper-style manifests.

    Used in place of `edit_replace_with_binary_target` when the
    manifest's target list is built through a user-defined wrapper
    (e.g. swift-collections' `CustomTarget` + `.toTarget()`), where the
    textual `.target(name: T, ...)` finder would match wrapper calls
    that have no `.binaryTarget` static method.

    The overlay intervenes one level higher: at the top-level
    `Package(targets: <expr>)` boundary. The wrapper's output is always
    a `[Target]`, so we filter same-named entries out of it and
    concatenate our own `Target.binaryTarget(...)` entries — producing
    a well-typed `[Target]` that SPM accepts.

    Two-state idempotency:
      1. First call (no sentinel block present): injects the overlay
         block immediately before the top-level `Package(...)` call AND
         wraps the `targets:` argument expression as
         `targets: (<orig>).filter { t in !_SPM2XC_OVERLAY_NAMES.contains(where: { $0 == t.name }) } + _SPM2XC_OVERLAY_TARGETS`.
      2. Subsequent calls (sentinel block present): parses the existing
         entries, merges with `substitutions` (last-wins by target
         name), and re-renders the block in place. The `targets:`
         wrap is left alone since it already references the overlay
         vars.

    `substitutions` is `[(target_name, xcframework_rel_path), ...]`.
    Paths must be relative to the package root — SPM rejects absolute
    paths in `.binaryTarget(path:)`. Caller is responsible for the
    relpath computation (same as the literal-list path in
    `_apply_dedup_overlap_substitutions`).

    Returns the edited manifest text. A call with zero substitutions
    AND no pre-existing overlay block is a no-op. A call that produces
    no net change (every substitution already in the existing block
    with the same path) returns `manifest_text` unchanged.

    Raises `PrepareUserError` if the manifest has no `Package(...)`
    call, no top-level `targets:` argument, or a malformed existing
    overlay block.
    """
    code_view = _make_code_token_view(manifest_text)

    # Locate any existing sentinel-bounded overlay block. We scan the
    # raw text rather than the code view: comments are blanked out in
    # the code view, so a sentinel COMMENT wouldn't appear there. Use
    # the raw text directly.
    block_start = manifest_text.find(_OVERLAY_SENTINEL_BEGIN)
    overlay_present = block_start != -1
    existing: Dict[str, str] = {}
    block_end = -1
    if overlay_present:
        sentinel_end = manifest_text.find(_OVERLAY_SENTINEL_END, block_start)
        if sentinel_end == -1:
            raise PrepareUserError(
                "overlay edit: found overlay begin sentinel without a "
                "matching end sentinel; the manifest is in a half-edited "
                "state."
            )
        block_end = sentinel_end + len(_OVERLAY_SENTINEL_END)
        if block_end < len(manifest_text) and manifest_text[block_end] == "\n":
            block_end += 1
        existing = _parse_overlay_entries(manifest_text[block_start:block_end])

    merged = dict(existing)
    for name, rel_path in substitutions:
        merged[name] = rel_path

    if not merged:
        # Nothing to do: no existing entries AND no new substitutions.
        return manifest_text

    if overlay_present:
        if merged == existing:
            return manifest_text
        new_block = _render_overlay_block(merged)
        return manifest_text[:block_start] + new_block + manifest_text[block_end:]

    # First-time injection: build the block, find the Package(...) call,
    # wrap its `targets:` argument expression, and splice the block in
    # immediately before the line containing `Package(`.
    new_block = _render_overlay_block(merged)

    m = _PACKAGE_CALL_RE.search(code_view)
    if not m:
        raise PrepareUserError(
            "overlay edit: no top-level `Package(` call found in the "
            "manifest."
        )
    pkg_open = m.end() - 1
    pkg_close = _balanced_close(manifest_text, pkg_open)
    if pkg_close == -1:
        raise PrepareUserError(
            "overlay edit: unmatched `(` for `Package(`; the manifest "
            "may be malformed."
        )
    body_start = pkg_open + 1
    body_end = pkg_close
    body = manifest_text[body_start:body_end]
    body_dz = _depth_zero_view(body)
    lm = _TARGETS_LABEL_RE.search(body_dz)
    if not lm:
        raise PrepareUserError(
            "overlay edit: no top-level `targets:` argument inside the "
            "Package(...) call. The manifest shape is not supported by "
            "the overlay edit."
        )
    expr_start_in_body = lm.end()
    while (
        expr_start_in_body < len(body)
        and body[expr_start_in_body] in " \t"
    ):
        expr_start_in_body += 1
    comma_idx = body_dz.find(",", expr_start_in_body)
    expr_end_in_body = comma_idx if comma_idx != -1 else len(body)
    while (
        expr_end_in_body > expr_start_in_body
        and body[expr_end_in_body - 1] in " \t\n"
    ):
        expr_end_in_body -= 1
    expr_start_offset = body_start + expr_start_in_body
    expr_end_offset = body_start + expr_end_in_body

    # Find the insertion point for the overlay block — the start of the
    # statement that opens the Package(...) call. Walk back from
    # pkg_open to the most recent newline + 1 (or BOF).
    line_start = manifest_text.rfind("\n", 0, pkg_open) + 1

    wrap_prefix = "("
    # `.contains(where: { $0 == t.name })` — NOT `.contains(t.name)`. Under
    # Swift 5.9+ the `Collection.contains<C: Collection>(_ other:)` overload
    # (Self.Element == C.Element) can shadow `Set.contains(_ member:)` when
    # the receiver expression is complex (e.g., a closure-returning array as
    # in Nimble's targets-builder shape). The type-checker then resolves to
    # the Collection-of-Collection overload and emits
    # "instance method 'contains' requires the types 'String' and
    # 'String.Element' (aka 'Character') be equivalent". `contains(where:)`
    # has no such overload, so it stays unambiguous.
    wrap_suffix = (
        f").filter {{ t in !{_OVERLAY_NAMES_VAR}.contains(where: {{ $0 == t.name }}) }} "
        f"+ {_OVERLAY_TARGETS_VAR}"
    )

    # Splice three regions in order from the end of the text so earlier
    # offsets stay valid:
    #   1. wrap_suffix immediately AFTER the targets-arg expression
    #   2. wrap_prefix immediately BEFORE it
    #   3. new_block immediately BEFORE the Package(...) statement
    # Done as a single concatenation to avoid offset drift.
    return (
        manifest_text[:line_start]
        + new_block
        + manifest_text[line_start:expr_start_offset]
        + wrap_prefix
        + manifest_text[expr_start_offset:expr_end_offset]
        + wrap_suffix
        + manifest_text[expr_end_offset:]
    )


_TARGET_LOOP_RE = re.compile(
    r"\bfor\s+(\w+)\s+in\s+package\.targets\b"
)


def edit_guard_target_loops_from_overlay(manifest_text: str) -> str:
    """Augment any top-level `for <var> in package.targets [where <expr>]`
    loop with a `_SPM2XC_OVERLAY_NAMES.contains(where: { $0 == <var>.name })`
    exclusion, so overlay-injected binaryTargets aren't subjected to
    settings mutations the post-loop applies to "all" non-system targets.

    Real-world trigger: swift-perception 1.6.0 ends its manifest with

        for target in package.targets where target.type != .system {
          target.swiftSettings = target.swiftSettings ?? []
          target.swiftSettings?.append(contentsOf: [
            .enableExperimentalFeature("StrictConcurrency"),
          ])
        }

    Once the dedup-overlap overlay injects `Target.binaryTarget(...)`
    entries into the targets array, this loop applies `swiftSettings`
    to them too. SPM rejects swiftSettings on binaryTargets — the round-
    trip validator fails with: "target 'P' is assigned a property
    'settings' which is not accepted for the binary target type".

    Behaviour:
      - No-op if the manifest has no overlay sentinel (no overlay block
        present → nothing to guard).
      - For each matched loop, AND
        `!_SPM2XC_OVERLAY_NAMES.contains(where: { $0 == <var>.name })`
        into the existing `where` clause, or insert the clause when no
        `where` is present. `.contains(where:)` is used (not the simpler
        `.contains(_:)`) for the same reason as the targets-arg wrap:
        the latter trips an overload-resolution failure under certain
        receiver-expression shapes on Swift 5.9+.
      - Idempotent: a loop whose `where` clause already mentions
        `_SPM2XC_OVERLAY_NAMES` is left untouched.
      - Skips matches inside strings or comments (uses
        `_make_code_token_view`).
      - Conservatively skips matches whose loop body opener can't be
        located (depth-tracked search for `{` past the loop header).
    """
    if _OVERLAY_SENTINEL_BEGIN not in manifest_text:
        return manifest_text

    code = _make_code_token_view(manifest_text)
    n = len(code)

    edits: List[Tuple[int, int, str]] = []
    for m in _TARGET_LOOP_RE.finditer(code):
        var_name = m.group(1)
        if var_name == _OVERLAY_NAMES_VAR:
            continue
        cursor = m.end()
        while cursor < n and code[cursor] in " \t\n":
            cursor += 1
        has_where = False
        where_expr_start = -1
        where_expr_end = -1
        if (
            code[cursor : cursor + 5] == "where"
            and (
                cursor + 5 == n
                or not (code[cursor + 5].isalnum() or code[cursor + 5] == "_")
            )
        ):
            has_where = True
            where_expr_start = cursor + 5
            while (
                where_expr_start < n and code[where_expr_start] in " \t\n"
            ):
                where_expr_start += 1
            scan = where_expr_start
            depth_paren = 0
            depth_bracket = 0
            brace_idx = -1
            while scan < n:
                ch = code[scan]
                if ch == "(":
                    depth_paren += 1
                elif ch == ")":
                    depth_paren -= 1
                elif ch == "[":
                    depth_bracket += 1
                elif ch == "]":
                    depth_bracket -= 1
                elif (
                    ch == "{" and depth_paren == 0 and depth_bracket == 0
                ):
                    brace_idx = scan
                    break
                scan += 1
            if brace_idx == -1:
                continue
            where_expr_end = brace_idx
            while (
                where_expr_end > where_expr_start
                and code[where_expr_end - 1] in " \t\n"
            ):
                where_expr_end -= 1
            existing_expr = manifest_text[where_expr_start:where_expr_end]
            if _OVERLAY_NAMES_VAR in existing_expr:
                continue
            replacement = (
                f"({existing_expr.strip()})"
                f" && !{_OVERLAY_NAMES_VAR}.contains(where: {{ $0 == {var_name}.name }})"
            )
            edits.append((where_expr_start, where_expr_end, replacement))
        else:
            insert_at = m.end()
            replacement = (
                f" where !{_OVERLAY_NAMES_VAR}.contains(where: {{ $0 == {var_name}.name }})"
            )
            edits.append((insert_at, insert_at, replacement))

    if not edits:
        return manifest_text

    out_parts: List[str] = []
    last = 0
    for start, end, text in edits:
        out_parts.append(manifest_text[last:start])
        out_parts.append(text)
        last = end
    out_parts.append(manifest_text[last:])
    return "".join(out_parts)


_DEPENDENCIES_LABEL_RE = re.compile(r"\bdependencies\s*:")


def _strip_comments_to_spaces(span: str) -> str:
    """Return a string the same length as `span` with line and block
    comment characters (delimiters + body) replaced by spaces. Strings
    and other code are preserved verbatim; newlines are preserved both
    inside and outside comments so per-line offsets stay aligned.

    Comment detection is string-aware: a `//` or `/*` that sits inside
    a string literal isn't treated as a comment start. Block comments
    nest, matching Swift's grammar.

    Used as the stripped view for the backwards tail-shape scan in
    `edit_augment_target_dependencies` so a trailing comment after the
    last element doesn't cause the splice to land inside the comment.
    """
    out = list(span)
    n = len(span)
    i = 0
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
                if out[k] != "\n":
                    out[k] = " "
            i = j
            continue
        # Triple-quoted multi-line string: skip past via the dedicated
        # helper so a `"""` opener isn't misread as an empty single-quoted
        # string followed by code. Body stays verbatim in `out` (which
        # was initialised from `list(span)`).
        if c == '"' and i + 2 < n and span[i + 1] == '"' and span[i + 2] == '"':
            close_idx = _skip_triple_quoted_string(span, i)
            if close_idx == -1:
                break
            i = close_idx
            continue
        if c == '"':
            # Skip past string literals so a // or /* inside a string
            # isn't blanked. The string body itself stays in `out`.
            j = i + 1
            while j < n:
                cc = span[j]
                if cc == "\\" and j + 1 < n:
                    if span[j + 1] == "(":
                        close_idx = _balanced_close(span, j + 1)
                        if close_idx == -1:
                            break
                        j = close_idx + 1
                        continue
                    j += 2
                    continue
                if cc == '"':
                    j += 1
                    break
                if cc == "\n":
                    break
                j += 1
            i = j
            continue
        i += 1
    return "".join(out)


def _collect_depth_zero_string_literals(span: str) -> set:
    """Return the set of RAW string-literal source spans found at depth
    0 of `span`. Walks character-by-character tracking nesting depth
    and string/comment state; only string literals encountered while
    `depth == 0` are collected.

    "Raw source spans" means the value preserves backslash escapes and
    interpolation segments verbatim — no Swift-level unescaping. For
    `"prefix \\(x) suffix"` the collected element is the literal
    string `prefix \\(x) suffix`. Callers are comparing against
    proposed dep names that they construct as plain identifiers
    (e.g. "InternalCollectionsUtilities"), so the comparison just
    needs both sides to agree on representation. The escape-preserving
    behaviour matches `_swift_string_literal`'s output, which is what
    we'd emit if we ever spliced the same name in.

    Used by `edit_augment_target_dependencies` to test whether a
    proposed dep name is already a member of the array. A naive
    `re.finditer(r'"..."', array_interior)` would falsely match a
    string nested inside `.target(name: "X")` or `.product(name: "Y")`;
    the dependency item there is the WHOLE call, and "X"/"Y" appear at
    depth 1, so they should not count as direct dep names.

    Escape handling: `\\n`, `\\"`, and string-interpolation `\\(...)` are
    consumed as part of the string. Newlines inside an unterminated
    `"..."` end the literal (matches Swift's grammar). Block comments
    nest in Swift; line comments end at `\\n`.
    """
    out: set = set()
    n = len(span)
    i = 0
    depth = 0
    while i < n:
        c = span[i]
        if c == "/" and i + 1 < n and span[i + 1] == "/":
            nl = span.find("\n", i + 2)
            i = nl if nl != -1 else n
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
            i = j
            continue
        # Triple-quoted multi-line strings are NOT collected as dep-name
        # candidates: their bodies are arbitrary multi-line text, not
        # identifiers. Skip past via the dedicated helper so a `"""`
        # opener isn't misread as an empty single-quoted string followed
        # by code (which could then leak in-body characters into the
        # next round of state).
        if c == '"' and i + 2 < n and span[i + 1] == '"' and span[i + 2] == '"':
            close_idx = _skip_triple_quoted_string(span, i)
            if close_idx == -1:
                break
            i = close_idx
            continue
        if c == '"':
            j = i + 1
            buf = []
            while j < n:
                cc = span[j]
                if cc == "\\" and j + 1 < n:
                    nxt = span[j + 1]
                    if nxt == "(":
                        close_idx = _balanced_close(span, j + 1)
                        if close_idx == -1:
                            break
                        # Interpolation: opaque to the literal value.
                        buf.append(span[j : close_idx + 1])
                        j = close_idx + 1
                        continue
                    # Other escape sequences: keep them verbatim — the
                    # caller is comparing raw source text against the
                    # dep names it wants to inject, and would have
                    # constructed any escapes the same way.
                    buf.append(span[j : j + 2])
                    j += 2
                    continue
                if cc == '"':
                    j += 1
                    if depth == 0:
                        out.add("".join(buf))
                    break
                if cc == "\n":
                    break
                buf.append(cc)
                j += 1
            i = j
            continue
        if c in "([{":
            depth += 1
            i += 1
            continue
        if c in ")]}":
            if depth > 0:
                depth -= 1
            i += 1
            continue
        i += 1
    return out


def edit_augment_target_dependencies(
    manifest_text: str,
    target_name: str,
    extra_dep_names: Sequence[str],
) -> str:
    """Append string-literal entries to the `dependencies:` array of the
    `.target(name: target_name, ...)` (or `.executableTarget` /
    `.testTarget`) call.

    Used by Execute's dedup-overlap pass to inject "phantom helper"
    deps — internal sibling targets that are in an umbrella's
    transitive dep closure but absent from its direct `dependencies:`
    list. Without the injection, SPM doesn't add the helper's binary
    target slice to the umbrella's `FRAMEWORK_SEARCH_PATHS`, and the
    consumer's compile fails to resolve `import <Helper>` calls
    embedded in another sibling's emitted `.swiftinterface`.

    Canonical case: Apple swift-collections 1.1.4. `Collections` directly
    depends on `[BitCollections, DequeModule, ...]` but each of those
    transitively depends on `InternalCollectionsUtilities` (`kind:
    .hidden`). After dedup-overlap, the siblings become `.binaryTarget`s
    and their `.swiftinterface` files retain `import
    InternalCollectionsUtilities`; SPM only adds slice dirs for binary
    targets in the direct dep list, so `Collections`'s compile can't
    find the helper. Augmenting `Collections`'s `dependencies:` with
    `"InternalCollectionsUtilities"` fixes the search-path gap without
    touching the helper module's own xcframework.

    Idempotent: an entry already present in the array as a quoted
    string literal at depth 0 is skipped. The "depth 0" qualifier matters
    because nested `.target(name: "X")` / `.product(name: "X", ...)`
    forms can mention any name as their own `name:` argument; those
    don't count as members of the outer array. Other forms (a sibling
    referenced as `.target(name: "X")` rather than `"X"`) are NOT
    treated as duplicates — the augmentation appends a plain string
    literal in that case, which SPM accepts alongside the existing
    decorated form.

    Raises `PrepareUserError` if:
      - No `.target(name: target_name, ...)` (or `.executableTarget` /
        `.testTarget`) call exists in the manifest.
      - The target's call has no top-level `dependencies:` argument.
      - The `dependencies:` expression is not an array literal (e.g.
        `dependencies: someComputedArray`) — we don't attempt to
        splice into a name-bound expression.

    Wrapper-style manifests (swift-collections' `CustomTarget.target(
    name:, dependencies:, ...)`) are supported because the regex
    `\\.(target|executableTarget|testTarget)\\s*\\(` matches both
    `CustomTarget.target(` and bare `.target(` calls. The wrapper's
    `dependencies` field is `[Target.Dependency]`-typed, which accepts
    `ExpressibleByStringLiteral` entries the same as a real
    `Target.target` call. The wrapper's `.toTarget()` forwards the list
    verbatim to `Target.target(dependencies:)`, so the injection
    propagates through to SPM's model.
    """
    if not extra_dep_names:
        return manifest_text

    kind_start, close_idx, _kind = _find_target_call_for_name(
        manifest_text, target_name
    )
    if kind_start == -1:
        raise PrepareUserError(
            f"augment_target_dependencies: no `.target(name: {target_name!r}, "
            f"...)` (or .executableTarget/.testTarget) found in the manifest."
        )

    # The call body spans (open_paren+1, close_idx).
    open_idx = manifest_text.index("(", kind_start)
    body_start = open_idx + 1
    body_end = close_idx
    body = manifest_text[body_start:body_end]
    # `_depth_zero_view` blanks string/comment bodies AND nested-scope
    # content while preserving offsets. So a `dependencies:` label
    # nested inside another arg (rare but defensible) cannot false-
    # match, and a label inside a string literal is invisible.
    body_dz = _depth_zero_view(body)

    lm = _DEPENDENCIES_LABEL_RE.search(body_dz)
    if not lm:
        raise PrepareUserError(
            f"augment_target_dependencies: no top-level `dependencies:` "
            f"argument in `.target(name: {target_name!r}, ...)` call."
        )

    # Find the `[` that opens the array literal. Skip whitespace.
    i = lm.end()
    while i < len(body) and body[i] in " \t\n":
        i += 1
    if i >= len(body) or body[i] != "[":
        raise PrepareUserError(
            f"augment_target_dependencies: `dependencies:` expression in "
            f"`.target(name: {target_name!r}, ...)` is not an array literal "
            f"(spotted `{body[i:i+10]!r}` at the position). The injection "
            f"requires a literal `[...]` so it can splice new entries."
        )
    deps_open = i  # relative to body
    deps_close = _balanced_close(body, deps_open)
    if deps_close == -1:
        raise PrepareUserError(
            f"augment_target_dependencies: unmatched `[` for `dependencies:` "
            f"array of `.target(name: {target_name!r}, ...)`."
        )

    # Idempotency: skip entries already present as depth-0 quoted strings.
    # We can't use `_depth_zero_view` here — it blanks string-literal
    # bodies as part of its comment-aware sanitisation, which would
    # leave the regex matching empty strings instead of the actual
    # names. Walk the array interior manually, tracking depth + string/
    # comment state, and capture string literals only at depth 0.
    array_interior = body[deps_open + 1 : deps_close]
    existing_strings = _collect_depth_zero_string_literals(array_interior)

    to_add = [d for d in extra_dep_names if d not in existing_strings]
    if not to_add:
        return manifest_text  # idempotent — all entries already present

    # Pick the right splice shape based on what character sits at the
    # array's logical "tail" (the last non-blank, non-comment char
    # before `]`).
    #   - `[` → empty array; insert `<entries>` (no leading comma needed)
    #   - `,` → trailing comma; insert `<entries>,` (Swift accepts the
    #     extra trailing comma; the existing one provides the separator)
    #   - anything else → content with no trailing comma; insert `,
    #     <entries>` (leading comma supplies the separator)
    #
    # We walk a comment-stripped view of `body` so a trailing line/
    # block comment after the last element doesn't fool the decision.
    # Without the strip, `["A", // note\n]` would land `tail_char='e'`,
    # taking the "anything else" branch — and the inserted ", "B""
    # would land INSIDE the line comment, becoming dead code. Strings
    # stay in the view (a string's closing `"` is a legitimate tail).
    body_for_tail = _strip_comments_to_spaces(body)
    j = deps_close - 1
    while j > deps_open and body_for_tail[j] in " \t\n":
        j -= 1
    tail_char = body_for_tail[j]
    insertion_abs = body_start + j + 1  # right after the last non-blank char

    entries_chunk = ", ".join(
        _swift_string_literal(d) for d in to_add
    )
    if tail_char == "[":
        insertion = entries_chunk
    elif tail_char == ",":
        insertion = " " + entries_chunk + ","
    else:
        insertion = ", " + entries_chunk

    return (
        manifest_text[:insertion_abs]
        + insertion
        + manifest_text[insertion_abs:]
    )


def _find_depth_zero_string_literal_positions(
    body: str, start: int, end: int, target_value: str
) -> List[Tuple[int, int]]:
    """Return `[(lit_start, lit_end), ...]` for every depth-0
    `"<target_value>"` string literal in `body[start:end]`. `lit_start`
    is the offset of the opening `"`; `lit_end` is one past the closing
    `"`. Walks depth char-by-char like
    `_collect_depth_zero_string_literals` so a `name:` argument inside
    a nested `.target(name: "X")` / `.product(name: "X", ...)` call
    (depth ≥ 1) does NOT false-match.

    Comments (`//` and nestable `/* */`), backslash escapes, and Swift
    string interpolation (`\\(...)`) are handled the same way as the
    rest of the file's walkers — interpolation recurses through
    `_balanced_close` to find the closing `)` of the embedded
    expression.
    """
    out: List[Tuple[int, int]] = []
    i = start
    depth = 0
    while i < end:
        c = body[i]
        if c == "/" and i + 1 < end and body[i + 1] == "/":
            nl = body.find("\n", i + 2)
            i = nl if nl != -1 and nl < end else end
            continue
        if c == "/" and i + 1 < end and body[i + 1] == "*":
            block_depth = 1
            j = i + 2
            while j < end and block_depth > 0:
                if j + 1 < end and body[j] == "/" and body[j + 1] == "*":
                    block_depth += 1
                    j += 2
                    continue
                if j + 1 < end and body[j] == "*" and body[j + 1] == "/":
                    block_depth -= 1
                    j += 2
                    continue
                j += 1
            i = j
            continue
        # Triple-quoted multi-line strings: not eligible matches for a
        # single-line `target_value`. Skip past via the dedicated helper
        # so a `"""` opener isn't misread as an empty single-quoted
        # string followed by code.
        if c == '"' and i + 2 < end and body[i + 1] == '"' and body[i + 2] == '"':
            close_idx = _skip_triple_quoted_string(body, i)
            if close_idx == -1 or close_idx > end:
                break
            i = close_idx
            continue
        if c == '"':
            lit_start = i
            j = i + 1
            buf: List[str] = []
            terminated = False
            while j < end:
                cc = body[j]
                if cc == "\\" and j + 1 < end:
                    nxt = body[j + 1]
                    if nxt == "(":
                        close_idx = _balanced_close(body, j + 1)
                        if close_idx == -1 or close_idx >= end:
                            break
                        buf.append(body[j : close_idx + 1])
                        j = close_idx + 1
                        continue
                    buf.append(body[j : j + 2])
                    j += 2
                    continue
                if cc == '"':
                    j += 1
                    terminated = True
                    break
                if cc == "\n":
                    break
                buf.append(cc)
                j += 1
            if terminated and depth == 0 and "".join(buf) == target_value:
                out.append((lit_start, j))
            i = j
            continue
        if c in "([{":
            depth += 1
            i += 1
            continue
        if c in ")]}":
            if depth > 0:
                depth -= 1
            i += 1
            continue
        i += 1
    return out


def _expand_dep_cut(
    body: str,
    lit_start: int,
    lit_end: int,
    interior_start: int,
    interior_end: int,
) -> Tuple[int, int]:
    """Compute `(cut_start, cut_end)` bounds that delete a string-literal
    dependency entry — including its adjacent comma and surrounding
    whitespace — from a `dependencies:` array.

    Two layout cases:
      1. Literal is alone on its line (the canonical multi-line dep
         array). Delete the whole line, including the trailing newline,
         so the array indentation stays tidy.
      2. Literal is inline with siblings (e.g. `["A", "B", "C"]`).
         Delete the literal plus an adjacent comma and a single space.
         If it's the last entry (no trailing comma), eat the PRECEDING
         comma + space so the new last entry isn't left with a dangling
         separator.

    The two cases are distinguished by whether the line — minus the
    literal and any trailing comma — contains any non-whitespace
    characters.
    """
    line_start = lit_start
    while line_start > interior_start and body[line_start - 1] != "\n":
        line_start -= 1
    line_end = lit_end
    while line_end < interior_end and body[line_end] != "\n":
        line_end += 1
    pre_lit = body[line_start:lit_start].strip()
    post_lit = body[lit_end:line_end].rstrip().rstrip(",").strip()
    if pre_lit == "" and post_lit == "":
        cut_end = line_end + 1 if (
            line_end < interior_end and body[line_end] == "\n"
        ) else line_end
        return line_start, cut_end

    cs = lit_start
    ce = lit_end
    p = ce
    while p < interior_end and body[p] == " ":
        p += 1
    if p < interior_end and body[p] == ",":
        ce = p + 1
        if ce < interior_end and body[ce] == " ":
            ce += 1
        return cs, ce
    p = cs - 1
    while p >= interior_start and body[p] == " ":
        p -= 1
    if p >= interior_start and body[p] == ",":
        cs = p
        if cs > interior_start and body[cs - 1] == " ":
            cs -= 1
    return cs, ce


def edit_strip_string_dep_from_target_dependencies(
    manifest_text: str, target_name: str, dep_name: str
) -> str:
    """Remove every depth-0 `"<dep_name>"` entry from the
    `dependencies:` array of `.target(name: target_name, ...)` (or
    `.executableTarget` / `.testTarget`).

    Idempotent: returns `manifest_text` unchanged when no matching
    literal is present, when the target has no `dependencies:` argument,
    when `dependencies:` isn't a literal array, or when the target call
    isn't found at all. Leaves decorated forms (`.target(name:
    "<dep_name>")`, `.product(name: "<dep_name>", ...)`,
    `.byName("<dep_name>")`) untouched — those aren't simple string
    literals at depth 0 and the macro-strip pass deliberately doesn't
    try to handle them.

    Used by `_apply_macro_support_edits` in Prepare. Macro target names
    must be removed from regular targets' dep arrays before xcodebuild
    archive runs: xcodebuild's SPM integration doesn't propagate the
    macro target's `.product(name:, package:)` deps into its generated
    Xcode project, so the macro target's own swiftc invocation fails to
    find swift-syntax. Pre-building the macro plugin via `swift build`
    and stripping the dep here lets xcodebuild skip the macro target
    entirely; `-load-plugin-executable` flags at archive time tell
    swiftc where to find the pre-built plugin.
    """
    kind_start, close_idx, _kind = _find_target_call_for_name(
        manifest_text, target_name
    )
    if kind_start == -1:
        return manifest_text
    open_idx = manifest_text.index("(", kind_start)
    body_start = open_idx + 1
    body_end = close_idx
    body = manifest_text[body_start:body_end]
    body_dz = _depth_zero_view(body)
    lm = _DEPENDENCIES_LABEL_RE.search(body_dz)
    if not lm:
        return manifest_text
    i = lm.end()
    while i < len(body) and body[i] in " \t\n":
        i += 1
    if i >= len(body) or body[i] != "[":
        return manifest_text
    deps_open = i
    deps_close = _balanced_close(body, deps_open)
    if deps_close == -1:
        return manifest_text
    interior_start = deps_open + 1
    interior_end = deps_close
    matches = _find_depth_zero_string_literal_positions(
        body, interior_start, interior_end, dep_name
    )
    if not matches:
        return manifest_text
    cuts: List[Tuple[int, int]] = []
    for lit_start, lit_end in matches:
        cuts.append(
            _expand_dep_cut(
                body, lit_start, lit_end, interior_start, interior_end
            )
        )
    edited_body = body
    for cs, ce in sorted(cuts, reverse=True):
        edited_body = edited_body[:cs] + edited_body[ce:]
    return manifest_text[:body_start] + edited_body + manifest_text[body_end:]


def edit_strip_string_dep_from_all_targets(
    manifest_text: str, dep_name: str, *, include_test_targets: bool = False
) -> str:
    """Strip `"<dep_name>"` from the `dependencies:` array of every
    `.target(...)` and `.executableTarget(...)` call in `manifest_text`.

    Test targets are skipped by default — they retain their references
    so the `.macro(...)` target's conditional `.testTarget` siblings (a
    common shape, including swift-case-paths' OMIT_MACRO_TESTS-gated
    block) stay valid. The `.macro(...)` declaration itself is left
    intact, so a stripped string literal still points at a real target
    in the dump-package model; `swift package dump-package` and
    `swift package resolve` both stay happy.

    Iterates over `.target(` / `.executableTarget(` (and optionally
    `.testTarget(`) call sites in a code-only view of the manifest
    (`_make_code_token_view`), extracts each call's top-level
    `name:` argument, then delegates to
    `edit_strip_string_dep_from_target_dependencies`. The helper is
    idempotent per target, so iterating across every target with the
    same `dep_name` is safe even when most targets don't reference it.
    """
    code_view = _make_code_token_view(manifest_text)
    target_names: List[str] = []
    pos = 0
    while True:
        m = _TARGET_CALL_KIND_RE.search(code_view, pos)
        if not m:
            break
        kind = m.group(1)
        if kind == "testTarget" and not include_test_targets:
            pos = m.end()
            continue
        open_idx = m.end() - 1
        close_idx = _balanced_close(manifest_text, open_idx)
        if close_idx == -1:
            break
        span = manifest_text[open_idx + 1 : close_idx]
        name = _top_level_name_label(span)
        if name is not None:
            target_names.append(name)
        pos = close_idx + 1
    text = manifest_text
    for name in target_names:
        text = edit_strip_string_dep_from_target_dependencies(
            text, name, dep_name
        )
    return text


_PRODUCT_CALL_RE = re.compile(r"\.product\s*\(")


def _depth_zero_product_calls_with_name_and_package(
    body: str,
    interior_start: int,
    interior_end: int,
    product_name: str,
    package_identity: str,
) -> List[Tuple[int, int]]:
    """Return `(call_start, call_end_inclusive)` spans for every
    `.product(name: P, package: PKG[, ...])` call that sits at depth 0
    of the slice `body[interior_start:interior_end]` AND whose top-level
    `name:` / `package:` arguments equal `product_name` /
    `package_identity` respectively.

    "Depth 0 of the slice" is the depth counter that starts at zero at
    `interior_start` — so a `.product(...)` directly inside the
    `dependencies:` array literal counts, but one that lives inside a
    nested `.when(...)` condition (depth ≥ 1 inside the array) does
    not. Mirrors the walker shape used by
    `_find_depth_zero_string_literal_positions`.

    A `.product(...)` whose `package:` argument is OMITTED is matched
    iff its `name:` matches AND `package_identity` is the empty string
    sentinel `""` — i.e. callers asking to rewrite "any package" against
    the bare-name shape. The two main spm-to-xcframework callers
    (`edit_rewrite_external_product_to_string_dep_in_all_targets` and
    its consume_external_sibling wrapper) always pass a non-empty
    `package_identity`, so the bare-name path is only relevant for unit
    tests today; it's still safer to require the explicit opt-in than to
    silently rewrite every same-named `.product(...)`.

    Returned spans are inclusive of the closing `)` so callers can do
    `body[:start] + replacement + body[end+1:]`. Spans are returned in
    ascending order so a reverse-cut loop is straightforward.
    """
    matches: List[Tuple[int, int]] = []
    i = interior_start
    depth = 0
    while i < interior_end:
        c = body[i]
        # Line comment
        if c == "/" and i + 1 < interior_end and body[i + 1] == "/":
            nl = body.find("\n", i + 2)
            i = nl if nl != -1 and nl < interior_end else interior_end
            continue
        # Block comment
        if c == "/" and i + 1 < interior_end and body[i + 1] == "*":
            block_depth = 1
            j = i + 2
            while j < interior_end and block_depth > 0:
                if (
                    j + 1 < interior_end
                    and body[j] == "/"
                    and body[j + 1] == "*"
                ):
                    block_depth += 1
                    j += 2
                    continue
                if (
                    j + 1 < interior_end
                    and body[j] == "*"
                    and body[j + 1] == "/"
                ):
                    block_depth -= 1
                    j += 2
                    continue
                j += 1
            i = j
            continue
        # Triple-quoted multi-line string: skip past via the dedicated
        # helper. MUST be checked before the single-quote handler since
        # `"""` begins with `"`.
        if c == '"' and i + 2 < interior_end and body[i + 1] == '"' and body[i + 2] == '"':
            close_idx = _skip_triple_quoted_string(body, i)
            if close_idx == -1 or close_idx > interior_end:
                break
            i = close_idx
            continue
        # String literal — skip over it without changing depth. Handles
        # `\(...)` interpolation the same way the other walkers do.
        if c == '"':
            j = i + 1
            while j < interior_end:
                cc = body[j]
                if cc == "\\" and j + 1 < interior_end:
                    if body[j + 1] == "(":
                        close_idx = _balanced_close(body, j + 1)
                        if close_idx == -1 or close_idx >= interior_end:
                            j = interior_end
                            break
                        j = close_idx + 1
                        continue
                    j += 2
                    continue
                if cc == '"':
                    j += 1
                    break
                if cc == "\n":
                    break
                j += 1
            i = j
            continue
        # Candidate `.product(` at the current depth. Must be EXACTLY
        # at depth 0 (relative to `interior_start`) — anything deeper
        # is inside a nested call we ignore.
        if (
            depth == 0
            and c == "."
            and body.startswith(".product", i)
            and (
                i + 8 >= interior_end
                or not (body[i + 8].isalnum() or body[i + 8] == "_")
            )
        ):
            k = i + 8
            while k < interior_end and body[k] in " \t\r\n":
                k += 1
            if k < interior_end and body[k] == "(":
                close_idx = _balanced_close(body, k)
                if close_idx == -1 or close_idx >= interior_end:
                    i += 1
                    continue
                call_span = body[k + 1 : close_idx]
                name_val = _top_level_keyword_string_value(call_span, "name")
                if name_val == product_name:
                    package_val = _top_level_keyword_string_value(
                        call_span, "package"
                    )
                    # `package:` is matched case-insensitively against the
                    # SPM-normalised identity. Manifests like Nimble's
                    # `package: "CwlPreconditionTesting"` or Moya's
                    # `package: "Alamofire"` carry the PascalCase package
                    # name; the identity we plan against is the lowercased
                    # URL last component ("cwlpreconditiontesting" /
                    # "alamofire"). SPM accepts either spelling at resolve
                    # time, so we mirror that here. `package_identity` is
                    # documented as already lowercased, but call .lower()
                    # on both sides defensively.
                    if (
                        package_val is not None
                        and package_val.lower() == package_identity.lower()
                    ) or (
                        package_val is None and package_identity == ""
                    ):
                        matches.append((i, close_idx))
                # Always jump past the call — even on no-match we've
                # already counted its interior via `_balanced_close`,
                # so resuming after the closing `)` keeps the depth
                # counter honest.
                i = close_idx + 1
                continue
        if c in "([{":
            depth += 1
            i += 1
            continue
        if c in ")]}":
            if depth > 0:
                depth -= 1
            i += 1
            continue
        i += 1
    return matches


def _top_level_keyword_string_value(
    span: str, keyword: str
) -> Optional[str]:
    """Return the top-level `keyword: "<value>"` string literal value
    inside an argument span (the inside of a `(...)` call), or `None`
    if the keyword is absent / its value isn't a plain string literal.

    Uses `_flatten_to_top_level` so a nested call's same-named argument
    (e.g. a `.when(name: ...)` condition inside `.product(...)`)
    doesn't shadow the top-level one. `_flatten_to_top_level` PRESERVES
    depth-0 string literals verbatim (unlike `_depth_zero_view` /
    `_make_code_token_view`, which blank them out), so the regex can
    extract the literal's value directly. The regex matches only on
    `keyword:\\s*"..."` — array, identifier, or computed expressions
    fall through and return `None`. That's intentional: callers use
    the return value to decide whether a textual rewrite is safe, and
    "value is a literal" is the strictest, most predictable check.
    """
    flat = _flatten_to_top_level(span)
    pattern = re.compile(
        r'\b' + re.escape(keyword) + r'\s*:\s*"((?:[^"\\\n]|\\.)*)"'
    )
    m = pattern.search(flat)
    if not m:
        return None
    return m.group(1)


def edit_rewrite_external_product_to_string_dep_in_target(
    manifest_text: str,
    target_name: str,
    product_name: str,
    package_identity: str,
) -> str:
    """Rewrite every depth-0 `.product(name: product_name, package:
    package_identity[, ...])` entry inside the `dependencies:` array of
    the `.target/.executableTarget/.testTarget` call for `target_name`
    to the bare string literal `"<product_name>"`.

    Used by `consume_external_sibling` (Prepare): after the orchestrator
    pre-builds an external transitive product P as a sibling xcframework
    and the overlay injects a matching `.binaryTarget(name: P, ...)`,
    SPM should resolve a target's `.product(name: P, package: PKG)`
    reference against that binaryTarget rather than the now-orphaned
    external package. The cleanest way to make SPM do that is to drop
    the `.product()` shape — which always disambiguates to an external
    package by name — and replace it with the bare string shape, which
    resolves first against in-package target names (including the
    overlay's injected `.binaryTarget`).

    Idempotent on every dimension:
      - No match: returns `manifest_text` unchanged.
      - Already string-shaped (a previous run rewrote the same dep):
        the `.product(...)` call no longer matches, the function
        returns unchanged.
      - Multiple `.product(name: P, package: PKG)` entries in one
        deps array: rewrites every one (`.product(name: P, package: PKG,
        condition: .when(...))` and its sibling `.product(name: P,
        package: PKG)` both become `"P"`).

    Decorated forms left untouched:
      - `.product(name: P)` (no `package:`) — defensively not rewritten;
        external `.product()` shapes always carry an explicit package.
      - `.product(name: P, package: <expr>)` where the package arg is
        not a string literal (variable, function call, …) — `nil` from
        `_top_level_keyword_string_value` causes the entry to be skipped.

    Mirrors `edit_strip_string_dep_from_target_dependencies`'s
    walker shape: find the target call → find its `dependencies:` array
    → walk depth-0 `.product(...)` entries → splice replacements in
    reverse offset order.
    """
    kind_start, close_idx, _kind = _find_target_call_for_name(
        manifest_text, target_name
    )
    if kind_start == -1:
        return manifest_text
    open_idx = manifest_text.index("(", kind_start)
    body_start = open_idx + 1
    body_end = close_idx
    body = manifest_text[body_start:body_end]
    body_dz = _depth_zero_view(body)
    lm = _DEPENDENCIES_LABEL_RE.search(body_dz)
    if not lm:
        return manifest_text
    i = lm.end()
    while i < len(body) and body[i] in " \t\n":
        i += 1
    if i >= len(body) or body[i] != "[":
        return manifest_text
    deps_open = i
    deps_close = _balanced_close(body, deps_open)
    if deps_close == -1:
        return manifest_text
    interior_start = deps_open + 1
    interior_end = deps_close
    matches = _depth_zero_product_calls_with_name_and_package(
        body, interior_start, interior_end, product_name, package_identity
    )
    if not matches:
        return manifest_text
    string_literal = _swift_string_literal(product_name)
    edited_body = body
    for call_start, call_close in sorted(matches, reverse=True):
        edited_body = (
            edited_body[:call_start]
            + string_literal
            + edited_body[call_close + 1 :]
        )
    return manifest_text[:body_start] + edited_body + manifest_text[body_end:]


def _collect_depth_zero_target_or_byname_call_names(span: str) -> Set[str]:
    """Return the set of `name:` string-literal arguments collected
    from every depth-0 `.target(name: "X")` or `.byName(name: "X")`
    call in `span` — the *decorated* SPM intra-package dep forms.

    Companion to `_collect_depth_zero_string_literals`. That helper
    sees only bare-string entries (`"Foo"`); this one sees the two
    decorated forms an author reaches for when they need to attach a
    `condition:` argument (e.g. `.target(name: "Foo", condition:
    .when(platforms: [.iOS]))`). SPM treats all three shapes as
    equivalent edges in the target-dep graph, so any consumer of the
    deps array contents that wants graph fidelity — notably the
    transitive BFS in `_apply_consume_external_sibling_edits` — has to
    see both shapes or risk missing a path that goes through a
    decorated dep.

    "Depth 0" means depth tracked from the start of `span`, typically
    the interior of a `dependencies: [...]` array. A
    `.target(name: "Foo")` nested inside a `.when(...)` condition
    lives at depth ≥ 1 and is ignored, mirroring
    `_collect_depth_zero_string_literals`'s contract. Comments and
    string literals are skipped without changing depth; call interiors
    are extracted via `_balanced_close` and the `name:` argument is
    pulled with `_top_level_keyword_string_value`.

    A call whose `name:` argument is not a plain string literal
    (variable, function call, conditional) yields no entry — same
    "literal-only" discipline the rest of this module enforces.
    Duplicate references across multiple decorated forms collapse
    naturally via the set.
    """
    out: Set[str] = set()
    n = len(span)
    i = 0
    depth = 0
    while i < n:
        c = span[i]
        if c == "/" and i + 1 < n and span[i + 1] == "/":
            nl = span.find("\n", i + 2)
            i = nl if nl != -1 else n
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
            i = j
            continue
        # Triple-quoted multi-line string: skip past via the dedicated
        # helper. MUST be checked before the single-quote handler since
        # `"""` begins with `"`.
        if c == '"' and i + 2 < n and span[i + 1] == '"' and span[i + 2] == '"':
            close_idx = _skip_triple_quoted_string(span, i)
            if close_idx == -1:
                break
            i = close_idx
            continue
        if c == '"':
            j = i + 1
            while j < n:
                cc = span[j]
                if cc == "\\" and j + 1 < n:
                    if span[j + 1] == "(":
                        close_idx = _balanced_close(span, j + 1)
                        if close_idx == -1:
                            j = n
                            break
                        j = close_idx + 1
                        continue
                    j += 2
                    continue
                if cc == '"':
                    j += 1
                    break
                if cc == "\n":
                    break
                j += 1
            i = j
            continue
        if depth == 0 and c == ".":
            kw_len: Optional[int] = None
            if span.startswith(".target", i):
                end = i + 7
                if end >= n or not (span[end].isalnum() or span[end] == "_"):
                    kw_len = 7
            elif span.startswith(".byName", i):
                end = i + 7
                if end >= n or not (span[end].isalnum() or span[end] == "_"):
                    kw_len = 7
            if kw_len is not None:
                k = i + kw_len
                while k < n and span[k] in " \t\r\n":
                    k += 1
                if k < n and span[k] == "(":
                    close_idx = _balanced_close(span, k)
                    if close_idx == -1:
                        i += 1
                        continue
                    call_span = span[k + 1 : close_idx]
                    name_val = _top_level_keyword_string_value(
                        call_span, "name"
                    )
                    if name_val:
                        out.add(name_val)
                    i = close_idx + 1
                    continue
        if c in "([{":
            depth += 1
            i += 1
            continue
        if c in ")]}":
            if depth > 0:
                depth -= 1
            i += 1
            continue
        i += 1
    return out


def _target_names_with_string_deps_in_set(
    manifest_text: str, query_names: Set[str]
) -> List[str]:
    """Return target names whose top-level `dependencies:` array
    references AT LEAST ONE name in `query_names` via either a bare
    string literal (`"X"`) or a decorated intra-package call
    (`.target(name: "X")` / `.byName(name: "X")`).

    Walks every `.target / .executableTarget / .testTarget` call in
    `manifest_text` (test targets included, since their search paths
    suffer the same swiftinterface-import gap if they reference a
    consumed sibling). Uses `_top_level_name_label` to peel the call's
    own `name:` argument, then unions the two depth-0 collectors
    (`_collect_depth_zero_string_literals` for bare strings and
    `_collect_depth_zero_target_or_byname_call_names` for decorated
    forms). SPM treats all three shapes as equivalent edges in the
    target-dep graph, and the transitive BFS in
    `_apply_consume_external_sibling_edits` needs to see all of them
    to follow chains like `A -> .target(name: "Mid") -> "Sibling"`.

    Targets without a top-level `dependencies:` argument, or whose
    `dependencies:` expression isn't a literal `[...]`, are skipped
    silently — there's no array to scan. Return order matches manifest
    order, with no duplicates.
    """
    if not query_names:
        return []
    code_view = _make_code_token_view(manifest_text)
    out: List[str] = []
    seen: Set[str] = set()
    pos = 0
    while True:
        m = _TARGET_CALL_KIND_RE.search(code_view, pos)
        if not m:
            break
        open_idx = m.end() - 1
        close_idx = _balanced_close(manifest_text, open_idx)
        if close_idx == -1:
            break
        body = manifest_text[open_idx + 1 : close_idx]
        target_name = _top_level_name_label(body)
        pos = close_idx + 1
        if target_name is None or target_name in seen:
            continue
        body_dz = _depth_zero_view(body)
        lm = _DEPENDENCIES_LABEL_RE.search(body_dz)
        if not lm:
            continue
        j = lm.end()
        while j < len(body) and body[j] in " \t\n":
            j += 1
        if j >= len(body) or body[j] != "[":
            continue
        deps_close = _balanced_close(body, j)
        if deps_close == -1:
            continue
        array_interior = body[j + 1 : deps_close]
        existing_strings = _collect_depth_zero_string_literals(array_interior)
        existing_calls = _collect_depth_zero_target_or_byname_call_names(
            array_interior
        )
        if (existing_strings | existing_calls) & query_names:
            out.append(target_name)
            seen.add(target_name)
    return out


def _depth_zero_product_calls_with_package_in_set(
    body: str,
    interior_start: int,
    interior_end: int,
    package_identities: Set[str],
) -> List[Tuple[int, int]]:
    """Like `_depth_zero_product_calls_with_name_and_package` but
    matches by `package:` argument alone — any product name is fair
    game. Returns `(call_start, call_end_inclusive)` spans for every
    depth-0 `.product(... package: ID ...)` where `ID` is in
    `package_identities`. Used by
    `edit_strip_orphan_product_refs_for_identities_in_target` to delete
    leftover `.product(name: X, package: ID)` calls whose owning
    package is being stripped from `Package.dependencies` — these would
    otherwise dangle as references to an unknown package.
    """
    matches: List[Tuple[int, int]] = []
    if not package_identities:
        return matches
    i = interior_start
    depth = 0
    while i < interior_end:
        c = body[i]
        if c == "/" and i + 1 < interior_end and body[i + 1] == "/":
            nl = body.find("\n", i + 2)
            i = nl if nl != -1 and nl < interior_end else interior_end
            continue
        if c == "/" and i + 1 < interior_end and body[i + 1] == "*":
            block_depth = 1
            j = i + 2
            while j < interior_end and block_depth > 0:
                if j + 1 < interior_end and body[j] == "/" and body[j + 1] == "*":
                    block_depth += 1
                    j += 2
                    continue
                if j + 1 < interior_end and body[j] == "*" and body[j + 1] == "/":
                    block_depth -= 1
                    j += 2
                    continue
                j += 1
            i = j
            continue
        # Triple-quoted multi-line string: skip past via the dedicated
        # helper. MUST be checked before the single-quote handler since
        # `"""` begins with `"`.
        if c == '"' and i + 2 < interior_end and body[i + 1] == '"' and body[i + 2] == '"':
            close_idx = _skip_triple_quoted_string(body, i)
            if close_idx == -1 or close_idx > interior_end:
                break
            i = close_idx
            continue
        if c == '"':
            j = i + 1
            while j < interior_end:
                cc = body[j]
                if cc == "\\" and j + 1 < interior_end:
                    if body[j + 1] == "(":
                        close_idx = _balanced_close(body, j + 1)
                        if close_idx == -1 or close_idx >= interior_end:
                            j = interior_end
                            break
                        j = close_idx + 1
                        continue
                    j += 2
                    continue
                if cc == '"':
                    j += 1
                    break
                if cc == "\n":
                    break
                j += 1
            i = j
            continue
        if (
            depth == 0
            and c == "."
            and body.startswith(".product", i)
            and (
                i + 8 >= interior_end
                or not (body[i + 8].isalnum() or body[i + 8] == "_")
            )
        ):
            k = i + 8
            while k < interior_end and body[k] in " \t\r\n":
                k += 1
            if k < interior_end and body[k] == "(":
                close_idx = _balanced_close(body, k)
                if close_idx == -1 or close_idx >= interior_end:
                    i += 1
                    continue
                call_span = body[k + 1 : close_idx]
                package_val = _top_level_keyword_string_value(
                    call_span, "package"
                )
                # Case-insensitive match — manifests can spell `package:`
                # with either the PascalCase name (Nimble's
                # "CwlPreconditionTesting") or the SPM-normalised lowercase
                # identity. `package_identities` carries the lowercased
                # form; normalise the manifest side to match.
                if (
                    package_val is not None
                    and package_val.lower() in package_identities
                ):
                    matches.append((i, close_idx))
                i = close_idx + 1
                continue
        if c in "([{":
            depth += 1
            i += 1
            continue
        if c in ")]}":
            if depth > 0:
                depth -= 1
            i += 1
            continue
        i += 1
    return matches


def edit_strip_orphan_product_refs_for_identities_in_target(
    manifest_text: str,
    target_name: str,
    identities: Set[str],
) -> str:
    """Remove every depth-0 `.product(name: ?, package: ID)` entry
    from `target_name`'s `dependencies:` array where `ID` is in
    `identities`. Removes the call AND any directly trailing comma +
    surrounding whitespace so the surrounding list stays well-formed
    (no `[, x, y]`, no `[x,, y]`, no stranded trailing comma).

    Companion to `edit_strip_package_deps_for_identities`: that helper
    drops the `.package(url: ...)` declarations for consumed external
    transitive packages, but a `.product(name: X, package: ID)` entry
    elsewhere in the manifest would survive and then dangle ("unknown
    package 'ID' in dependencies of target 'T'"). This helper deletes
    those dangling refs. We delete rather than rewrite because a
    dangling reference's product was NOT built as a sibling
    xcframework — converting to a string dep would just produce a
    different "unknown target" diagnostic.

    Idempotent: if no orphan matches, returns `manifest_text`
    unchanged. Safe to call on target deps arrays that aren't a
    literal `[...]` (no-op).
    """
    if not identities:
        return manifest_text
    # Normalise once at the boundary so `_depth_zero_product_calls_with_package_in_set`'s
    # case-insensitive membership check works regardless of how the
    # caller spells the identities.
    identities_lower = {ident.lower() for ident in identities}
    kind_start, close_idx, _kind = _find_target_call_for_name(
        manifest_text, target_name
    )
    if kind_start == -1:
        return manifest_text
    open_idx = manifest_text.index("(", kind_start)
    body_start = open_idx + 1
    body_end = close_idx
    body = manifest_text[body_start:body_end]
    body_dz = _depth_zero_view(body)
    lm = _DEPENDENCIES_LABEL_RE.search(body_dz)
    if not lm:
        return manifest_text
    i = lm.end()
    while i < len(body) and body[i] in " \t\n":
        i += 1
    if i >= len(body) or body[i] != "[":
        return manifest_text
    deps_open = i
    deps_close = _balanced_close(body, deps_open)
    if deps_close == -1:
        return manifest_text
    interior_start = deps_open + 1
    interior_end = deps_close
    matches = _depth_zero_product_calls_with_package_in_set(
        body, interior_start, interior_end, identities_lower
    )
    if not matches:
        return manifest_text
    edited_body = body
    for call_start, call_close in sorted(matches, reverse=True):
        # Extend the cut to consume a directly trailing comma (and any
        # whitespace before / after it) so we don't leave `[, x]` or
        # `[x,, y]` behind. If there's no trailing comma (last entry),
        # walk BACKWARD instead to chew the preceding comma so the
        # entry before us doesn't end with a stray `,`.
        cut_start = call_start
        cut_end = call_close + 1  # exclusive
        scan = cut_end
        # Skip whitespace after the call (before the comma)
        while scan < len(edited_body) and edited_body[scan] in " \t":
            scan += 1
        if scan < len(edited_body) and edited_body[scan] == ",":
            cut_end = scan + 1
            # Also consume one trailing newline + indent if present so
            # the deletion doesn't leave a blank line behind. We stop
            # at the first non-whitespace OR a second newline.
            while cut_end < len(edited_body) and edited_body[cut_end] in " \t":
                cut_end += 1
            if cut_end < len(edited_body) and edited_body[cut_end] == "\n":
                cut_end += 1
                while (
                    cut_end < len(edited_body)
                    and edited_body[cut_end] in " \t"
                ):
                    cut_end += 1
        else:
            # No trailing comma — we're the last entry. Walk backward
            # from cut_start to consume the preceding comma + its
            # leading whitespace, so the entry before us doesn't end
            # with a dangling `,`.
            scan = cut_start - 1
            while scan >= 0 and edited_body[scan] in " \t\n":
                scan -= 1
            if scan >= 0 and edited_body[scan] == ",":
                cut_start = scan
        edited_body = edited_body[:cut_start] + edited_body[cut_end:]
    return manifest_text[:body_start] + edited_body + manifest_text[body_end:]


def edit_strip_orphan_product_refs_for_identities_in_all_targets(
    manifest_text: str,
    identities: Set[str],
    *,
    include_test_targets: bool = True,
) -> str:
    """Apply `edit_strip_orphan_product_refs_for_identities_in_target`
    across every `.target` / `.executableTarget` (and, by default,
    `.testTarget`) call. Test targets ARE included by default — a
    surviving `.product(... package: ID)` reference in a test target's
    deps array breaks the manifest the same way it breaks a regular
    target's.
    """
    if not identities:
        return manifest_text
    code_view = _make_code_token_view(manifest_text)
    target_names: List[str] = []
    pos = 0
    while True:
        m = _TARGET_CALL_KIND_RE.search(code_view, pos)
        if not m:
            break
        kind = m.group(1)
        if kind == "testTarget" and not include_test_targets:
            pos = m.end()
            continue
        open_idx = m.end() - 1
        close_idx = _balanced_close(manifest_text, open_idx)
        if close_idx == -1:
            break
        span = manifest_text[open_idx + 1 : close_idx]
        name = _top_level_name_label(span)
        if name is not None:
            target_names.append(name)
        pos = close_idx + 1
    text = manifest_text
    for name in target_names:
        text = edit_strip_orphan_product_refs_for_identities_in_target(
            text, name, identities
        )
    return text


def edit_rewrite_external_product_to_string_dep_in_all_targets(
    manifest_text: str,
    product_name: str,
    package_identity: str,
    *,
    include_test_targets: bool = True,
) -> str:
    """Apply `edit_rewrite_external_product_to_string_dep_in_target`
    across every `.target` / `.executableTarget` (and, by default,
    `.testTarget`) call in the manifest.

    Unlike the macro-strip pass, test targets ARE included by default —
    a test target that imports the rewritten product needs the same
    rewrite or it would dangle once the underlying external package
    becomes orphaned. Callers that want test targets left alone (e.g.
    debugging) can pass `include_test_targets=False`.

    Iterates `_TARGET_CALL_KIND_RE` over the code-only view, peels each
    call's top-level `name:` argument, then delegates. The single-target
    helper is itself idempotent, so running this across every target is
    safe even when most targets don't reference the product.
    """
    code_view = _make_code_token_view(manifest_text)
    target_names: List[str] = []
    pos = 0
    while True:
        m = _TARGET_CALL_KIND_RE.search(code_view, pos)
        if not m:
            break
        kind = m.group(1)
        if kind == "testTarget" and not include_test_targets:
            pos = m.end()
            continue
        open_idx = m.end() - 1
        close_idx = _balanced_close(manifest_text, open_idx)
        if close_idx == -1:
            break
        span = manifest_text[open_idx + 1 : close_idx]
        name = _top_level_name_label(span)
        if name is not None:
            target_names.append(name)
        pos = close_idx + 1
    text = manifest_text
    for name in target_names:
        text = edit_rewrite_external_product_to_string_dep_in_target(
            text, name, product_name, package_identity
        )
    return text


_PACKAGE_DEP_URL_RE = re.compile(r'url\s*:\s*"([^"]+)"')


def edit_strip_package_deps_for_identities(
    manifest_text: str, identities_to_strip: Set[str]
) -> str:
    """Remove `.package(url: "...", ...)` entries from the top-level
    `Package(dependencies: [...])` array whose URL-derived identity is
    in `identities_to_strip`.

    "Identity" mirrors SPM's normalisation: last URL path component,
    `.git` suffix removed, lowercased — the same convention
    `prune_child._identity_from_url` uses. Entries without a `url:`
    argument (local `path:` packages, computed-expression entries) are
    left untouched: their identity isn't recoverable from text alone,
    and accidentally stripping a path-based dep would be hard to
    diagnose.

    Used by `_apply_consume_external_sibling_edits` after the overlay
    block injects matching `.binaryTarget(...)` entries for each
    transitive sibling. Without stripping the `.package(...)`
    declaration, SPM still tries to resolve the external package and
    the package graph fails with "multiple packages declare targets
    with a conflicting name" because the overlay's binaryTarget shares
    a name with the original transitive's target.

    Idempotent: if no entry's identity matches, returns
    `manifest_text` unchanged.
    """
    if not identities_to_strip:
        return manifest_text
    targets_lower = {ident.lower() for ident in identities_to_strip}

    arr = _find_dependencies_array_in_package_call(manifest_text)
    if arr is None:
        return manifest_text
    open_b, close_b = arr  # close_b points AT the `]`
    view = _make_code_token_view(manifest_text)
    inside_start = open_b + 1
    inside_end = close_b

    spans: List[Tuple[int, int]] = []
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
            if view.startswith("package", j) and (
                j + 7 >= inside_end
                or not (view[j + 7].isalnum() or view[j + 7] == "_")
            ):
                k = j + 7
                while k < inside_end and view[k] in " \t\r\n":
                    k += 1
                if k < inside_end and view[k] == "(":
                    close_paren = _balanced_close(manifest_text, k)
                    if close_paren == -1 or close_paren >= inside_end:
                        i += 1
                        continue
                    entry_src = manifest_text[i : close_paren + 1]
                    m = _PACKAGE_DEP_URL_RE.search(entry_src)
                    if m:
                        identity = m.group(1).strip().rstrip("/")
                        if identity:
                            last = identity.rsplit("/", 1)[-1]
                            if last.endswith(".git"):
                                last = last[:-4]
                            if last.lower() in targets_lower:
                                entry_end = close_paren + 1
                                scan = entry_end
                                while (
                                    scan < inside_end
                                    and manifest_text[scan] in " \t"
                                ):
                                    scan += 1
                                if (
                                    scan < inside_end
                                    and manifest_text[scan] == ","
                                ):
                                    entry_end = scan + 1
                                scan2 = entry_end
                                while (
                                    scan2 < inside_end
                                    and manifest_text[scan2] in " \t"
                                ):
                                    scan2 += 1
                                if (
                                    scan2 < inside_end
                                    and manifest_text[scan2] == "\n"
                                ):
                                    entry_end = scan2 + 1
                                line_start = (
                                    manifest_text.rfind(
                                        "\n", inside_start, i
                                    )
                                    + 1
                                )
                                if (
                                    manifest_text[line_start:i].strip()
                                    == ""
                                ):
                                    entry_start = line_start
                                else:
                                    entry_start = i
                                spans.append((entry_start, entry_end))
                    i = close_paren + 1
                    continue
        i += 1

    if not spans:
        return manifest_text
    out_parts: List[str] = []
    cursor = 0
    for start, end in spans:
        out_parts.append(manifest_text[cursor:start])
        cursor = end
    out_parts.append(manifest_text[cursor:])
    return "".join(out_parts)


# Both regexes look for `<var>.{dependencies,targets}.{append,+=}` —
# the file-scope post-init mutation shape. The `(?<![.\w])` lookbehind
# excludes anything where `<var>` is itself a member of a longer
# dotted chain (e.g. `self.package.targets.append(...)`) — without it,
# the engine would match starting at `package` and the splice would
# leave a stranded `self.` behind. We do NOT require any specific
# variable name: file-scope `let X = Package(...)` is the canonical
# shape but some manifests use other names (`pkg`, `p`, etc.).
_POST_INIT_APPEND_RE = re.compile(
    r"(?<![.\w])\w+\.(?:dependencies|targets)\.append\s*\("
)
_POST_INIT_PLUS_EQ_RE = re.compile(
    r"(?<![.\w])\w+\.(?:dependencies|targets)\s*\+=\s*\["
)

# `_PRODUCT_CALL_RE` (`.product(`) is already defined above; reuse it.
# This dep-declaration regex is deliberately NOT named `_PACKAGE_CALL_RE`
# — that name is taken by the `\bPackage\s*\(` *constructor* finder near
# the top of this module, and colliding on it silently breaks every
# overlay/synth pass that locates the top-level `Package(...)` call.
_PACKAGE_DEP_CALL_RE = re.compile(r"\.package\s*\(")


def _pkg_identity_from_url(url: str) -> str:
    """SPM package identity from a `url:`/`path:` string: last path
    component, `.git` suffix removed, lowercased — the same convention
    `prune_child._identity_from_url` and
    `edit_strip_package_deps_for_identities` apply inline. A local path
    like `"../swift-atomics"` normalises to `swift-atomics`. Named
    distinctly from `prune_child._identity_from_url` so the flattened
    single-file artifact carries no duplicate `def` (prune_child sits
    below prepare in MODULE_ORDER, so we can't reuse its binding)."""
    ident = url.strip().rstrip("/")
    if not ident:
        return ""
    last = ident.rsplit("/", 1)[-1]
    if last.endswith(".git"):
        last = last[:-4]
    return last.lower()


def _testtarget_spans(
    manifest_text: str, code_view: str
) -> List[Tuple[int, int]]:
    """`(open_paren_idx, close_paren_idx)` for every `.testTarget(...)`
    call, discovered against the code-only `code_view` so a commented or
    string-embedded occurrence can't match. Used to exclude test-only
    `.product()` references from load-bearing detection."""
    spans: List[Tuple[int, int]] = []
    pos = 0
    while True:
        m = _TEST_TARGET_CALL_RE.search(code_view, pos)
        if not m:
            break
        open_idx = m.end() - 1
        close_idx = _balanced_close(manifest_text, open_idx)
        if close_idx == -1:
            break
        spans.append((open_idx, close_idx))
        pos = close_idx + 1
    return spans


def _load_bearing_package_identities(manifest_text: str) -> Set[str]:
    """Lowercased identities of every package referenced by a
    `.product(name:, package: ID)` call that is NOT lexically inside a
    `.testTarget(...)` call.

    A post-init `package.dependencies` mutation that adds one of these
    identities is *load-bearing*: a consumer build compiles the target
    (or the file-scope binding) that references it, so its `.package(...)`
    declaration must survive `edit_strip_post_init_package_mutations`.
    The scan is position-based rather than target-scoped so it covers
    both manifest shapes uniformly:
      - inline refs inside a target's `dependencies:` array
        (`.product(name: "CasePathsMacrosSupport", package: "swift-case-paths")`
        in swift-navigation's `.macro` target);
      - hoisted file-scope bindings
        (`let swiftAtomics = .product(name: "Atomics", package: "swift-atomics")`
        in swift-nio), which sit outside every target call but drive real
        target deps through a `let` variable.
    Test-only references are excluded so genuinely optional tooling still
    gets stripped: swift-case-paths' `OMIT_MACRO_TESTS` block wires
    `MacroTesting` (from swift-macro-testing) into an appended
    `.testTarget`, and stripping that dep is what keeps its transitive
    xctest-dynamic-overlay from colliding with our overlay binaryTargets.
    """
    code_view = _make_code_token_view(manifest_text)
    test_spans = _testtarget_spans(manifest_text, code_view)
    identities: Set[str] = set()
    pos = 0
    while True:
        m = _PRODUCT_CALL_RE.search(code_view, pos)
        if not m:
            break
        head = m.start()
        open_idx = m.end() - 1
        close_idx = _balanced_close(manifest_text, open_idx)
        if close_idx == -1:
            break
        pos = close_idx + 1
        if any(
            ts_open <= head and close_idx <= ts_close
            for ts_open, ts_close in test_spans
        ):
            continue
        pkg = _top_level_keyword_string_value(
            manifest_text[open_idx + 1 : close_idx], "package"
        )
        if pkg:
            identities.add(pkg.lower())
    return identities


def _package_entry_identity(entry_inner: str) -> Optional[str]:
    """Best-effort SPM identity for one `.package(...)` declaration, given
    the text *inside* its parens. Tries `url:` (remote), then `path:`
    (local), then `id:` (registry). Returns None when none is a plain
    string literal — the caller keeps unidentifiable entries rather than
    strip a dep it can't name."""
    url = _top_level_keyword_string_value(entry_inner, "url")
    if url:
        return _pkg_identity_from_url(url)
    path = _top_level_keyword_string_value(entry_inner, "path")
    if path:
        return _pkg_identity_from_url(path)
    reg = _top_level_keyword_string_value(entry_inner, "id")
    if reg:
        return reg.strip().lower()
    return None


def _post_init_dep_mutation_is_load_bearing(
    manifest_text: str,
    code_view: str,
    open_idx: int,
    close_idx: int,
    load_bearing: Set[str],
) -> bool:
    """True iff the `package.dependencies` mutation whose argument list
    spans `(open_idx, close_idx)` declares at least one `.package(...)`
    entry whose identity is load-bearing. Scans for `.package(` heads in
    the code-only view (depth-agnostic — handles both `+= [ ... ]` arrays
    and `.append(contentsOf: [ ... ])`), balanced-closing each against the
    original text so nested version calls (`.upToNextMajor(from:)`) are
    stepped over cleanly."""
    pos = open_idx + 1
    while True:
        m = _PACKAGE_DEP_CALL_RE.search(code_view, pos, close_idx)
        if not m:
            return False
        p_open = m.end() - 1
        p_close = _balanced_close(manifest_text, p_open)
        if p_close == -1 or p_close > close_idx:
            return False
        ident = _package_entry_identity(manifest_text[p_open + 1 : p_close])
        if ident is not None and ident in load_bearing:
            return True
        pos = p_close + 1


def edit_strip_post_init_package_mutations(manifest_text: str) -> str:
    """Strip every file-scope `<var>.dependencies.append(...)`,
    `<var>.targets.append(...)`, `<var>.dependencies += [...]`, and
    `<var>.targets += [...]` statement from `manifest_text`.

    These method/operator shapes are legal in Package.swift only
    AFTER `let package = Package(...)` has returned — the Package()
    initializer's argument list uses positional/keyword syntax, never
    method calls or compound assignment on its own object. Spotting one
    is a reliable structural signal that the dep or target is being
    APPENDED conditionally — typically inside an `#if !os(Windows)` or
    `if ProcessInfo.processInfo.environment[...]` gate that wraps an
    optional build-time tool (swift-docc-plugin, swift-macro-testing,
    swift-snapshot-testing, swift-benchmark, example subprojects, …).

    Why strip `.targets` mutations, and *optional* `.dependencies` ones:
      A downstream consumer building runtime products (which is the
      entire mission of `spm-to-xcframework`) never needs any build-time
      helper. And when an umbrella consumes a transitive sibling whose
      targets we've replaced with binaryTarget overlays, a conditional
      append that transitively reaches the same package identity
      reintroduces it into the graph with conflicting target names —
      `xcodebuild archive` then aborts with "multiple packages declare
      targets with a conflicting name". The canonical case is
      swift-case-paths' OMIT_MACRO_TESTS-gated swift-macro-testing
      append, whose closure pulls in xctest-dynamic-overlay's
      IssueReporting/XCTestDynamicOverlay targets that we just injected
      as binaryTargets. The author's choice to put a dep behind
      `package.dependencies.append(...)` is a signal it *may* be
      optional; documentation/testing/benchmarking tooling is present
      for the author and safely absent for consumers.

    Why NOT strip *load-bearing* `.dependencies` mutations:
      Some packages declare genuinely load-bearing runtime dependencies
      after the initializer — not optional tooling. swift-nio hoists
      `let swiftAtomics = .product(name: "Atomics", package:
      "swift-atomics")` at file scope (referenced by NIOCore, NIOPosix,
      …) and only *adds* the backing `.package(url:)` in a post-init
      `package.dependencies += [...]` block (its
      `SWIFTCI_USE_LOCAL_DEPS` else-branch). grpc-swift does the same for
      swift-nio/swift-protobuf. Blanket-stripping those left every
      referencing target dangling with "unknown package 'swift-atomics'"
      at dump-package time — the X-003 regression. So a `.dependencies`
      mutation is preserved whenever ANY `.package(...)` entry it
      declares has an identity that is *referenced* by a
      `.product(package: ID)` call outside a `.testTarget` (see
      `_load_bearing_package_identities`). `.targets` mutations are still
      always stripped: an appended target is never a build unit for us,
      and the umbrella-conflict hazard above is real.

    Implementation: walks `_make_code_token_view(manifest_text)` (so
    `"` strings and `// /* */` comments don't trigger false matches),
    finds each append/`+=` head, runs `_balanced_close` against the
    original text to find the matching `)` or `]`. For `.dependencies`
    mutations it then checks the enclosed `.package()` entries against
    the load-bearing identity set and skips (preserves) the statement if
    any is load-bearing; `.targets` mutations are unconditionally
    collected. Collected statements are removed whole — including any
    trailing semicolon, any leading-whitespace-only prefix on the same
    line, and the trailing newline if it leaves the line empty. Removal
    proceeds back-to-front so earlier offsets remain valid as later
    spans collapse.

    Idempotent: when the manifest contains no qualifying calls, the
    function returns `manifest_text` unchanged. Re-running on already-
    stripped text is also a no-op.
    """
    code_view = _make_code_token_view(manifest_text)
    load_bearing = _load_bearing_package_identities(manifest_text)
    spans: List[Tuple[int, int]] = []

    for regex in (_POST_INIT_APPEND_RE, _POST_INIT_PLUS_EQ_RE):
        for m in regex.finditer(code_view):
            open_idx = m.end() - 1  # points at `(` or `[`
            close_idx = _balanced_close(manifest_text, open_idx)
            if close_idx == -1:
                continue
            # `.targets` mutations always go (never a build unit for us);
            # a `.dependencies` mutation survives when it declares any
            # load-bearing package the graph still references.
            if ".dependencies" in m.group(0) and _post_init_dep_mutation_is_load_bearing(
                manifest_text, code_view, open_idx, close_idx, load_bearing
            ):
                continue
            spans.append((m.start(), close_idx + 1))

    if not spans:
        return manifest_text

    spans.sort(key=lambda p: p[0])

    # Walk back-to-front so each splice doesn't invalidate the next
    # span's offsets. Each removal also absorbs an optional trailing
    # semicolon, any whitespace-only line prefix in front of the call,
    # and exactly one trailing newline when that leaves the line empty.
    result = manifest_text
    for start, end in reversed(spans):
        absorb_end = end
        if absorb_end < len(result) and result[absorb_end] == ";":
            absorb_end += 1

        line_start = result.rfind("\n", 0, start) + 1
        leading = result[line_start:start]
        absorb_start = line_start if leading.strip() == "" else start

        scan = absorb_end
        while scan < len(result) and result[scan] in " \t":
            scan += 1
        if scan < len(result) and result[scan] == "\n":
            absorb_end = scan + 1

        result = result[:absorb_start] + result[absorb_end:]

    return result


_TEST_TARGET_CALL_RE = re.compile(r"\.testTarget\s*\(")


def _is_array_element_context(code_view: str, call_start: int) -> bool:
    """Return True if the call at `call_start` is a direct element of an
    array literal — i.e. the preceding non-whitespace character is `[`
    (first element) or `,` (sibling element).

    A `.testTarget(...)` that appears in any other context — assigned to
    a variable (`let x: Target = .testTarget(...)`), returned from a
    function (`return Target.testTarget(...)`), or any other expression
    position — must NOT be stripped: removing the call body leaves a
    dangling `=` / `return Target` / etc. that fails
    `swift package dump-package` with a syntax error.

    Canonical cases that motivated this check:
      - SQLite.swift 0.15.5: `let testTarget: Target = .testTarget(...)`
        used as a hoisted binding in `targets: [target, testTarget]`.
      - swift-collections (transitive): `return Target.testTarget(...)`
        inside a `toTarget()` helper on a custom enum.

    Stripping only direct array elements preserves the intent (kill
    test targets that SPM's describe-call scans for source layout)
    without breaking valid but uncommon expression contexts. A test
    target hidden inside a helper that's actually invoked from the
    `targets:` array IS missed by this scope check — a documented
    gap, since detecting it requires data-flow analysis. If a future
    package surfaces this shape, the rescue is the per-target
    auto-recovery in `prepare.py` (rebuilds the manifest from
    inspected sources) rather than widening the regex.
    """
    i = call_start - 1
    # Use .isspace() rather than a literal " \t\n" set so CRLF
    # manifests don't drop us at `\r` and false-negative the context
    # check. (Codex review.)
    while i >= 0 and code_view[i].isspace():
        i -= 1
    return i >= 0 and code_view[i] in "[,"


def edit_strip_test_targets(manifest_text: str) -> str:
    """Remove every `.testTarget(...)` call that is a direct element of
    the manifest's targets array, along with the trailing comma (or
    leading comma if it's the final element).

    Scope: only direct array elements are stripped — see
    `_is_array_element_context` for why we deliberately skip calls in
    let-bindings, return statements, and other expression contexts.

    Why strip:
      Test targets contribute nothing to archive builds — we never run
      `swift test`, only `xcodebuild archive` against generated schemes
      for library products. But SPM's `swift package describe` and the
      Xcode build setup both scan every declared target's source tree,
      and a test target can have its own toolchain-incompatible shapes
      that abort the whole describe call. Canonical case: amplitude's
      analytics-connector-ios v1.3.0 declares an `AnalyticsConnectorTests`
      test target whose Tests/AnalyticsConnectorTests dir has both Swift
      and Objective-C sources — SPM refuses with `target ... contains
      mixed language source files; feature not supported`, and inspect
      bails before plan/execute ever run. Removing the test target call
      makes describe succeed and unblocks the umbrella build that
      doesn't care about tests in the first place.

    Idempotent: re-running on already-stripped manifest is a no-op.

    Code-only matching: comments and string literals are blanked via
    `_make_code_token_view` before scanning, so a stray `.testTarget(`
    inside a doc-comment or string literal can't false-positive.
    """
    code_view = _make_code_token_view(manifest_text)
    spans_to_remove: List[Tuple[int, int]] = []
    pos = 0
    while True:
        m = _TEST_TARGET_CALL_RE.search(code_view, pos)
        if not m:
            break
        call_start = m.start()
        open_idx = m.end() - 1
        close_idx = _balanced_close(manifest_text, open_idx)
        if close_idx == -1:
            # Malformed manifest — let the downstream parser surface the
            # real complaint rather than silently mutating something we
            # don't fully understand.
            break
        if not _is_array_element_context(code_view, call_start):
            # Not a direct array element — stripping would leave dangling
            # syntax (e.g. `let x: Target = ` or `return Target`).
            pos = close_idx + 1
            continue
        # Extend the removal to absorb either the trailing comma (when
        # the testTarget has siblings after it) or the leading comma
        # (when it's the last element of the array). Without this, we
        # leave a dangling `,` that breaks Swift array syntax.
        remove_start = call_start
        remove_end = close_idx + 1
        scan = remove_end
        while scan < len(manifest_text) and manifest_text[scan] in " \t":
            scan += 1
        if scan < len(manifest_text) and manifest_text[scan] == ",":
            remove_end = scan + 1
        else:
            # No trailing comma — this is the last array element. Walk
            # backward to absorb the leading comma + any whitespace
            # between it and our call start.
            back = call_start - 1
            while back >= 0 and manifest_text[back] in " \t\n":
                back -= 1
            if back >= 0 and manifest_text[back] == ",":
                remove_start = back
        spans_to_remove.append((remove_start, remove_end))
        pos = close_idx + 1

    if not spans_to_remove:
        return manifest_text
    # Apply removals in reverse so earlier offsets remain valid.
    out = manifest_text
    for start, end in reversed(spans_to_remove):
        out = out[:start] + out[end:]
    return out


_BINARY_TARGET_URL_LABEL_RE = re.compile(r"\burl\s*:")
_LIBRARY_TARGETS_LIST_RE = re.compile(
    r"\btargets\s*:\s*\[([^\[\]]*)\]", re.DOTALL
)


def edit_strip_unused_binary_targets(manifest_text: str) -> str:
    """Remove every `.binaryTarget(name: X, url: ..., checksum: ...)`
    declaration whose name X is NOT referenced by any source target's
    dependency list, plus any `.library(...)` product whose targets list
    is made entirely of stripped-binary-target names.

    Why strip:
      Some packages declare BOTH a source product and a URL-based binary
      alternative of the same library (canonical: analytics-connector-ios
      v1.3.x ships `.target("AnalyticsConnector", path: "Sources/...")`
      AND `.binaryTarget("AnalyticsConnectorFramework", url:
      ".../AnalyticsConnector.xcframework.zip")`). When we synth a
      dynamic library targeting the source target and run `xcodebuild
      archive`, SwiftPM downloads the .binaryTarget's prebuilt
      xcframework (because the manifest still declares it) and
      xcodebuild's archive populates `Products/.../AnalyticsConnector
      .framework/Modules/AnalyticsConnector.swiftmodule/` from the
      prebuilt's xcframework slice — which is named with the prebuilt's
      target triple (e.g. `arm64-apple-tvos`) and not our iOS slice.
      The resulting framework has zero iOS swiftinterfaces, so the
      umbrella build's `import AnalyticsConnector` fails with `Cannot
      find type 'AnalyticsConnector' in scope`.

    Why only URL-based:
      A `.binaryTarget(name: X, path: "Vendored.xcframework")` is a
      local artifact bundled INTO the package source tree (no SPM
      download). Stripping it would break any source target that
      `dependencies: [.target(name: X)]` against the vendored library —
      a real use case. URL-based binary targets are SPM downloads of
      external xcframeworks; if no source target depends on them they
      are "alternative distribution" products, safe to remove.

    "Depended-on" detection: scan every non-binary target call body for
    a quoted occurrence of the binary target name (excluding the
    target's own `name:` field). This intentionally over-counts (e.g.
    a target's `path: "AnalyticsConnector"` would protect a binary
    target with the same name) — over-counting is the safe direction:
    it just keeps the binary target where stripping is uncertain.

    Idempotent: re-running on already-stripped manifest is a no-op.

    Code-only matching: comments and string literals are blanked via
    `_make_code_token_view` before scanning, so a `.binaryTarget(`
    inside a doc-comment can't false-positive.
    """
    code_view = _make_code_token_view(manifest_text)

    # 1. Collect URL-based binary target spans + names.
    bin_targets: List[Tuple[int, int, str]] = []  # (start, end_exclusive, name)
    for m in _BINARY_TARGET_CALL_RE.finditer(code_view):
        open_idx = m.end() - 1
        close_idx = _balanced_close(manifest_text, open_idx)
        if close_idx == -1:
            continue
        body = manifest_text[open_idx + 1:close_idx]
        flat = _flatten_to_top_level(body)
        name_m = _TOP_LEVEL_NAME_LABEL_RE.search(flat)
        if name_m is None:
            continue
        if _BINARY_TARGET_URL_LABEL_RE.search(flat) is None:
            continue  # path-based vendored xcframework — leave alone
        bin_targets.append((m.start(), close_idx + 1, name_m.group(1)))

    if not bin_targets:
        return manifest_text

    bin_names = {name for _, _, name in bin_targets}

    # 2. Walk source target calls (.target / .executableTarget). For
    #    each, find quoted occurrences of any binary target name. If
    #    present, that binary target is considered "depended-on" and we
    #    leave it alone.
    depended: set[str] = set()
    source_target_count = 0
    for m in _TARGET_CALL_KIND_RE.finditer(code_view):
        kind = m.group(1)
        if kind == "testTarget":
            continue  # tests are stripped separately and don't pin deps
        open_idx = m.end() - 1
        close_idx = _balanced_close(manifest_text, open_idx)
        if close_idx == -1:
            continue
        # Only count calls that are direct array elements of `targets:`.
        # Excludes helper expressions like `let helper: Target = .target(...)`
        # or `private func mkTarget() -> Target { .target(...) }` whose
        # presence would falsely inflate the count and defeat the
        # pure-binary bail-out below. The depended-name walk over `body`
        # still runs for context — false positives there just preserve a
        # binary target, which is the safe direction.
        m_call_start = m.start()
        is_array_element = _is_array_element_context(code_view, m_call_start)
        if is_array_element:
            source_target_count += 1
        body = manifest_text[open_idx + 1:close_idx]
        # Exclude the target's own `name: "X"` literal from matching:
        # collapse to a flat view of the top-level body, find name, then
        # strip that literal from the body before scanning. Cheap
        # heuristic — the raw body's first `name: "<name>"` match is
        # the call's own name in every well-formed manifest.
        flat = _flatten_to_top_level(body)
        my_name_m = _TOP_LEVEL_NAME_LABEL_RE.search(flat)
        my_name = my_name_m.group(1) if my_name_m else None
        for bn in bin_names:
            if bn == my_name:
                continue
            # Search the raw body for the quoted binary target name.
            # `f'"{bn}"'` matches forms `"X"`, `byName(name: "X")`,
            # `.target(name: "X")`, `.product(name: "X", ...)`. False
            # positives (e.g. `path: "X"`) just keep the binary target.
            if f'"{bn}"' in body:
                depended.add(bn)

    # Bail out when there are zero source targets: the strip's purpose
    # is to delete an "alternative binary distribution" that competes
    # with a source target of the same library (analytics-connector-
    # ios pattern). A package with ONLY `.binaryTarget(...)` and no
    # source target is the canonical "SPM wrapper around a vendor
    # xcframework" shape (intercom-ios-sp, mapbox-maps-ios-binary).
    # Leaving the binary in place lets the binary-only auto-switch in
    # cli.py see the package as binary-only and route it through
    # --binary mode.
    if source_target_count == 0:
        return manifest_text

    # 3. Strip binary targets not in the depended set.
    targets_to_strip = [
        (s, e, n) for s, e, n in bin_targets if n not in depended
    ]
    if not targets_to_strip:
        return manifest_text
    stripped_names = {n for _, _, n in targets_to_strip}

    # 4. Find `.library(name: P, ..., targets: [...])` products whose
    #    targets list is entirely stripped binary target names — those
    #    products no longer have a valid backing target, so SPM will
    #    reject the manifest unless we strip them too.
    products_to_strip: List[Tuple[int, int]] = []
    for m in _LIBRARY_CALL_RE.finditer(code_view):
        open_idx = m.end() - 1
        close_idx = _balanced_close(manifest_text, open_idx)
        if close_idx == -1:
            continue
        body = manifest_text[open_idx + 1:close_idx]
        list_m = _LIBRARY_TARGETS_LIST_RE.search(body)
        if list_m is None:
            continue
        target_names = re.findall(r'"([^"\\\n]*)"', list_m.group(1))
        if target_names and all(t in stripped_names for t in target_names):
            products_to_strip.append((m.start(), close_idx + 1))

    # 5. Compute final removal spans with comma absorption (mirrors
    #    `edit_strip_test_targets`).
    raw_spans: List[Tuple[int, int]] = (
        [(s, e) for s, e, _ in targets_to_strip] + products_to_strip
    )
    raw_spans.sort()
    spans_to_remove: List[Tuple[int, int]] = []
    for call_start, call_end in raw_spans:
        remove_start = call_start
        remove_end = call_end
        scan = remove_end
        while scan < len(manifest_text) and manifest_text[scan] in " \t":
            scan += 1
        if scan < len(manifest_text) and manifest_text[scan] == ",":
            remove_end = scan + 1
        else:
            back = call_start - 1
            while back >= 0 and manifest_text[back] in " \t\n":
                back -= 1
            if back >= 0 and manifest_text[back] == ",":
                remove_start = back
        spans_to_remove.append((remove_start, remove_end))

    out = manifest_text
    for start, end in reversed(spans_to_remove):
        out = out[:start] + out[end:]
    return out


def _find_dependencies_array_in_package_call(
    source: str,
) -> Optional[Tuple[int, int]]:
    """Within the top-level `Package(...)`, find the `dependencies: [...]`
    argument array. Returns (open_bracket_idx, close_bracket_idx_inclusive)
    or None if the argument isn't present. Mirrors prune_child's
    helper of the same name; kept local to prepare.py so the dedup-overlap
    and macro-strip code can stay self-contained.
    """
    m = _PACKAGE_CALL_RE.search(_make_code_token_view(source))
    if not m:
        return None
    pkg_open = m.end() - 1
    pkg_close = _balanced_close(source, pkg_open)
    if pkg_close == -1:
        return None
    view = _make_code_token_view(source)
    inside_start = pkg_open + 1
    inside_end = pkg_close
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
            if view.startswith("dependencies", i) and (
                i + 12 >= inside_end
                or not (view[i + 12].isalnum() or view[i + 12] == "_")
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
        # Triple-quoted multi-line string: blank the entire span (matches
        # single-quoted handling here). MUST be checked before the
        # single-quote handler since `"""` begins with `"`.
        if c == '"' and i + 2 < n and span[i + 1] == '"' and span[i + 2] == '"':
            close_idx = _skip_triple_quoted_string(span, i)
            if close_idx == -1:
                break
            for k in range(i, close_idx):
                if out[k] != "\n":
                    out[k] = " "
            i = close_idx
            continue
        if c == '"':
            j = i + 1
            while j < n:
                cc = span[j]
                if cc == "\\" and j + 1 < n:
                    if span[j + 1] == "(":
                        close_idx = _balanced_close(span, j + 1)
                        if close_idx == -1:
                            break
                        j = close_idx + 1
                        continue
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


def _bump_tools_version_if_below(
    manifest_path: Path, min_version: Tuple[int, int]
) -> Optional[Tuple[int, int, int]]:
    """Rewrite the manifest's `// swift-tools-version: X.Y` line to
    `min_version` if currently below it. Returns the original parsed
    version if a bump was applied, else None.

    Why: `swift package add-product` requires tools-version >= 5.2; SPM's
    own error message recommends bumping to 5.5+. Old packages like
    MBProgressHUD (4.2) use only the manifest-format subset that's
    backward-compatible with 5.5, so bumping doesn't break the build —
    it just unblocks the synthetic-product edit.
    """
    declared = _read_declared_tools_version(manifest_path)
    if declared is None:
        return None
    if declared[:2] >= min_version:
        return None
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except OSError:
        return None
    lines = text.splitlines(keepends=True)
    replacement = f"// swift-tools-version: {min_version[0]}.{min_version[1]}\n"
    for i, line in enumerate(lines[:8]):
        if _TOOLS_VERSION_RE.search(line):
            lines[i] = replacement
            manifest_path.write_text("".join(lines), encoding="utf-8")
            return declared
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


def _add_product_via_active_manifest_proxy(
    staged_dir: Path,
    active_manifest: Path,
    product_name: str,
    targets: Sequence[str],
) -> None:
    """Run `swift package add-product` so the edit lands in
    `active_manifest`, even when SPM picks a version-specific manifest
    (e.g. Package@swift-5.9.swift) over the base Package.swift.

    Problem: `swift package add-product` is hard-coded to edit
    `Package.swift`. When SPM selects a version-specific manifest as
    active, the call returns 0, edits Package.swift, and leaves the
    active manifest untouched — `dump-package` (which reads the active)
    then shows no product and round-trip validation rejects the edit as
    a silent no-op. Reproduced on Nuke 12.8.0 and swift-dependencies
    1.6.3 (both ship Package.swift + Package@swift-5.9.swift).

    Fix: temporarily put the active manifest's text into Package.swift,
    run add-product (which edits Package.swift in place), then copy the
    edited content back to the active manifest and restore the original
    Package.swift.
    """
    base_manifest = staged_dir / "Package.swift"
    if active_manifest == base_manifest:
        _invoke_swift_add_product(staged_dir, product_name, targets)
        return

    base_existed = base_manifest.is_file()
    base_text_before = base_manifest.read_text() if base_existed else None
    active_text = active_manifest.read_text()
    try:
        base_manifest.write_text(active_text)
        _invoke_swift_add_product(staged_dir, product_name, targets)
        active_manifest.write_text(base_manifest.read_text())
    finally:
        # Restore the original state. If Package.swift didn't exist before
        # (legal per SE-0152: a package may ship only version-specific
        # manifests), unlink the temporary file we wrote so we don't leave
        # a ghost manifest behind that the package never had. Skipping this
        # would change the manifest set the package presents to SPM on the
        # next invocation.
        if base_text_before is not None:
            base_manifest.write_text(base_text_before)
        elif not base_existed and base_manifest.is_file():
            base_manifest.unlink()


def _apply_single_synth_edit(
    staged_dir: Path,
    active_manifest: Path,
    product_name: str,
    targets: Sequence[str],
) -> None:
    """Add one synthetic dynamic library via add-product, falling back to
    manual text surgery if SPM rejects the manifest's `products:` shape.

    SPM's add-product requires `products: [ ... ]` to be a literal array
    expression in the source. swift-collections (and other packages that
    build their product list programmatically) instead write something
    like `products: _products`, which SPM rejects with "unable to find
    array literal for 'products' argument". In that case we fall through
    to `edit_append_synth_product_to_package`, which appends the
    synthetic entry via a `+ [...]` expression that works for both
    literal arrays and non-literal expressions.

    Before either of those paths, we first check for a post-init
    `<var>.products = [...]` assignment that would silently clobber the
    add-product injection. PromiseKit 8.1.2 ships this shape: the
    `Package(...)` initializer takes no `products:` argument, then
    `pkg.products = [...]` runs at file scope and overwrites whatever
    add-product just injected. `edit_append_synth_to_post_init_products`
    handles that case by appending `<var>.products.append(.library(...))`
    after the assignment.
    """
    post_init_edited = edit_append_synth_to_post_init_products(
        active_manifest.read_text(), product_name, targets
    )
    if post_init_edited is not None:
        active_manifest.write_text(post_init_edited)
        return

    try:
        _add_product_via_active_manifest_proxy(
            staged_dir, active_manifest, product_name, targets
        )
        return
    except PrepareBug as exc:
        if "unable to find array literal" not in str(exc).lower():
            raise
        info(
            f"  swift add-product can't edit non-literal `products:`; "
            f"falling back to manual surgery for {product_name}"
        )

    edited = edit_append_synth_product_to_package(
        active_manifest.read_text(), product_name, targets
    )
    active_manifest.write_text(edited)


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


def _build_macro_plugin(
    staged_dir: Path, macro_target_name: str, *, verbose: bool
) -> Path:
    """Build a `.macro(...)` target as a host-arch compiler-plugin
    executable and return the absolute path to the binary.

    Runs `swift build -c release --product <macro_target_name>` against
    the (already-staged, already-edited) package. SPM compiles macro
    targets as host-only executables regardless of the package's
    declared platforms, so this is a pure-host build — no target-arch
    flags needed, no per-slice rebuild required.

    The resulting binary lives under
    `.build/<host-triple>/release/<macro_target_name>-tool` — the
    `-tool` suffix is what SPM emits for `.macro` products. The host
    triple varies by toolchain (`arm64-apple-macosx`,
    `x86_64-apple-macosx`, …) so we glob rather than hardcoding it.

    Raises `PrepareBug` if `swift build` fails or no matching executable
    is found after a successful build. The build is the load-bearing
    macro support step — failure here means xcodebuild archive would
    later fail with the (much more confusing) "Unable to find module
    dependency: 'SwiftSyntax'" diagnostic, so we surface the cause
    early.
    """
    verbose_log(
        verbose,
        f"  Building macro plugin {macro_target_name} (host-arch)...",
    )
    cp = subprocess.run(
        ["swift", "build", "-c", "release", "--product", macro_target_name],
        cwd=str(staged_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if cp.returncode != 0:
        tail = "\n".join((cp.stdout or "").rstrip().splitlines()[-30:])
        raise PrepareBug(
            f"`swift build --product {macro_target_name}` failed in "
            f"{staged_dir}. The macro compiler plugin could not be built; "
            f"xcodebuild archive would later report 'Unable to find module "
            f"dependency' for swift-syntax modules.\n"
            + (tail or "  (no output)")
        )
    candidates = sorted(
        staged_dir.glob(f".build/*/release/{macro_target_name}-tool")
    )
    if not candidates:
        # Some older SPM releases emit the binary without the -tool
        # suffix. Fall back so we don't fail on a successfully-built
        # plugin we just can't locate.
        candidates = sorted(
            staged_dir.glob(f".build/*/release/{macro_target_name}")
        )
    if not candidates:
        raise PrepareBug(
            f"swift build {macro_target_name} reported success but no "
            f"executable was found at "
            f"{staged_dir}/.build/*/release/{macro_target_name}-tool. "
            f"SPM may have changed its macro-plugin output layout — file a "
            f"bug with the toolchain version."
        )
    # Prefer a path that contains the current host triple if multiple
    # candidates exist (debug + release, multi-arch, …). Falls back to
    # the first match. The .resolve() converts to an absolute path,
    # which is required because xcodebuild runs in a different cwd and
    # `-load-plugin-executable` doesn't resolve relative paths.
    return candidates[0].resolve()


def _apply_macro_support_edits(
    staged_dir: Path, plan: Plan, *, verbose: bool
) -> None:
    """For each `MacroSupport` in `plan.macros`: pre-build the macro
    target as a host-arch compiler-plugin executable and strip the
    macro's name from every regular / executable target's
    `dependencies:` array in the active Package.swift.

    Why both steps:
      1. Pre-build: xcodebuild's SPM integration registers macro
         targets in its generated Xcode workspace WITHOUT their
         `.product(name:, package:)` deps. The macro target's
         swiftc invocation then fails to find swift-syntax modules
         and the whole archive aborts. `swift build` resolves deps
         natively and produces the binary.
      2. Strip dep refs: with the pre-built plugin available via
         `-load-plugin-executable`, xcodebuild no longer needs to
         compile the macro target at all. Removing the macro's name
         from regular targets' dep arrays stops xcodebuild from even
         trying. The `.macro(...)` declaration itself is left intact
         so `swift package dump-package` and `swift package resolve`
         still parse cleanly and any conditional `.testTarget` blocks
         that reference the macro target keep working.

    Mutates each `MacroSupport.plugin_executable_path` in place; the
    caller (`prepare()`) re-exposes the mutated `plan` via
    `PreparedPlan.plan`. Execute reads the paths back at archive time.
    """
    if not plan.macros:
        return

    manifest_path = _select_active_manifest(staged_dir)
    if not manifest_path.is_file():
        raise PrepareBug(
            f"_apply_macro_support_edits: active manifest not found at "
            f"{manifest_path}"
        )

    info(f"  Pre-building {len(plan.macros)} macro plugin(s)...")
    for macro in plan.macros:
        plugin_path = _build_macro_plugin(
            staged_dir, macro.macro_target_name, verbose=verbose
        )
        macro.plugin_executable_path = plugin_path
        verbose_log(
            verbose,
            f"    {macro.macro_target_name} → {plugin_path}",
        )

    text = manifest_path.read_text()
    edited = text
    for macro in plan.macros:
        edited = edit_strip_string_dep_from_all_targets(
            edited, macro.macro_target_name
        )
    if edited != text:
        manifest_path.write_text(edited)
        verbose_log(
            verbose,
            f"  Stripped macro dep references from {manifest_path.name}",
        )


_SWIFTINTERFACE_PACKAGE_DECL_KEYWORDS = (
    r"(?:struct|class|enum|protocol|func|init|var|let|typealias|"
    r"associatedtype|extension|subscript|deinit|actor|macro)\b"
)

# An optional run of `@attribute` / `@attribute(args)` tokens (each
# followed by whitespace) that can legally appear between a `package`
# keyword and the actual declaration keyword in a swiftinterface line.
# Used as part of the lookahead in `_SWIFTINTERFACE_PACKAGE_DECL_RE` and
# as the gap between `@usableFromInline` and `package` in the strip
# regexes. The `(?:\s*\([^)]*\))?` allows optional argument lists
# (no nested parens, but the swiftinterface emitter doesn't produce
# them — flat keyword + comma-separated literals only).
_SWIFTINTERFACE_DECL_MODIFIERS = (
    r"(?:final|@\w+(?:\s*\([^)]*\))?|@inlinable|nonisolated|"
    r"isolated|distributed|static|unowned|weak|mutating|nonmutating|"
    r"open|override|required|convenience|indirect|actor)"
)

# Strips a bare `@usableFromInline` annotation line that sits
# immediately ABOVE a `package` declaration we're about to upgrade to
# `public`. The attribute is only valid on internal/package
# declarations — leaving it in place after the upgrade triggers
# "@usableFromInline can only be applied to internal or package
# declarations, but struct X is public". Tolerates other attributes
# (e.g. `@inlinable`, `@propertyWrapper`) sitting between
# `@usableFromInline` and `package`. Anchored with re.MULTILINE +
# explicit `\n` so a `@usableFromInline` placed inline with the decl
# (e.g. `@usableFromInline package struct X`) is left to the second
# pass to handle on the same line.
_SWIFTINTERFACE_USABLE_FROM_INLINE_BEFORE_PACKAGE_RE = re.compile(
    r"^[ \t]*@usableFromInline[ \t]*\n"
    r"(?=(?:[ \t]*" + _SWIFTINTERFACE_DECL_MODIFIERS + r"[ \t\n]+)*"
    r"[ \t]*package\s+"
    r"(?:" + _SWIFTINTERFACE_DECL_MODIFIERS + r"\s+)*"
    + _SWIFTINTERFACE_PACKAGE_DECL_KEYWORDS + r")",
    re.MULTILINE,
)

# Same shape inline: `@usableFromInline package ...` on a single line.
# Strips just the attribute (and trailing whitespace) so the second
# pass can upgrade `package` → `public` on the same line. Tolerates
# attribute/modifier tokens between `@usableFromInline` and `package`.
_SWIFTINTERFACE_INLINE_USABLE_FROM_INLINE_BEFORE_PACKAGE_RE = re.compile(
    r"@usableFromInline[ \t]+"
    r"(?=(?:" + _SWIFTINTERFACE_DECL_MODIFIERS + r"[ \t]+)*"
    r"package\s+"
    r"(?:" + _SWIFTINTERFACE_DECL_MODIFIERS + r"\s+)*"
    + _SWIFTINTERFACE_PACKAGE_DECL_KEYWORDS + r")",
)

# Matches `package` in declaration position and captures the leading
# whitespace + any attribute/modifier prefix so the replacement
# `\1public\2` preserves them. Examples that all match:
#   - `package struct X { ... }`
#   - `  package final func foo(...)`
#   - `@propertyWrapper package struct UncheckedSendable<Value> ...`
#   - `@inlinable package init(...)`
# The lookahead anchors the keyword end (so `import package_X` or a
# `package` token inside a string literal never matches).
_SWIFTINTERFACE_PACKAGE_DECL_RE = re.compile(
    r"^([ \t]*(?:" + _SWIFTINTERFACE_DECL_MODIFIERS + r"[ \t]+)*)"
    r"package(\s+)"
    r"(?="
    r"(?:" + _SWIFTINTERFACE_DECL_MODIFIERS + r"\s+)*"
    + _SWIFTINTERFACE_PACKAGE_DECL_KEYWORDS
    + r")",
    re.MULTILINE,
)

# Promotes `package import SomeModule` → `public import SomeModule`.
# When we use a sibling's `.package.swiftinterface` as the source of
# truth and copy it over the public `.swiftinterface`, any
# `package import` lines in the file become invalid for non-same-package
# consumers reading the public interface. Promoting them to
# `public import` (the implicit default for swiftinterface imports
# anyway) keeps the import resolvable for every reader. Anchored at
# line start so a `package` keyword sitting on a continuation line
# never gets misidentified as an import access modifier.
_SWIFTINTERFACE_PACKAGE_IMPORT_RE = re.compile(
    r"^([ \t]*)package(\s+import\s+)",
    re.MULTILINE,
)


def _patch_sibling_swiftinterfaces_for_package_access(
    xcframework_path: Path,
) -> None:
    """Rewrite every `.swiftinterface` file under `xcframework_path` so
    `package`-scoped declarations become `public`, and delete every
    `arm64-apple-ios.package.swiftinterface` (and equivalents) inside
    the framework.

    Why upgrade `package` → `public`:
      Swift's `package` access modifier lets sibling modules in the same
      SPM package see each other's `package`-declared types and members.
      In the original SPM source build, IssueReporting and
      IssueReportingPackageSupport are both in the `xctest-dynamic-overlay`
      package, so IssueReporting's `@usableFromInline internal func
      _currentTest() -> IssueReportingPackageSupport._Test?` resolves
      fine — `_Test` is `package`, but both modules share the package
      boundary. After our overlay splits the package into separate
      `.binaryTarget`s, the umbrella's swiftc treats each binaryTarget as
      its own one-module package; the cross-module `_Test` lookup then
      fails with "no type named '_Test' in module
      'IssueReportingPackageSupport'" and the umbrella's archive aborts.

      Upgrading `package` to `public` in the consumed swiftinterface
      makes the type visible to any caller — the binary already exports
      the symbol with default visibility (swiftc emits `package` symbols
      with the same linkage as `public`), so nothing changes at link
      time, only at the type-resolution step swiftc performs while
      reading the interface. The trade-off: any downstream consumer of
      the SHIPPED sibling xcframework can now reach those types. We
      accept this surface-area bump because:

        1. `package` is a coarse modifier the original author chose
           specifically for intra-package use; the alternative
           (`internal`) would have hidden the type from binary interfaces
           entirely, and the author would have used it if they'd wanted
           that. The choice of `package` IMPLIES the author was OK with
           the type appearing in the binary interface.
        2. The same xcframework would otherwise be unusable.
        3. Downstream consumers don't have to use the exposed type; it
           remains underscore-prefixed, which is the established Swift
           convention for "unstable internal API, use at your own risk".

    Why the .package.swiftinterface CAN'T just be deleted:
      A `package`-only decl (e.g. `package struct UncheckedSendable`
      with NO `@usableFromInline`) lives ONLY in
      `<arch>.package.swiftinterface`. It is absent from both
      `<arch>.swiftinterface` (public surface) and
      `<arch>.private.swiftinterface` (public + @usableFromInline-internal
      surface). The cross-binaryTarget consumer reads
      `<arch>.swiftinterface`, so just promoting `package`→`public`
      in-place and deleting the package variant leaves the consumer
      with NO record of the decl — manifesting as "Unknown attribute
      'UncheckedSendable'" / "no type named 'X' in module 'Y'" /
      similar source-of-truth-missing errors. Canonical case:
      swift-case-paths' CasePathsCore re-exports `UncheckedSendable`
      to its same-package sibling CasePaths.

      We therefore use the .package.swiftinterface as the SOURCE of
      truth (it's a strict superset of .private and .swiftinterface;
      same headers, just more decls) and write the promoted content
      OVER `<arch>.swiftinterface` and `<arch>.private.swiftinterface`.
      Now every consumer reading any variant sees the full
      package-promoted-to-public surface.

    Implementation: walks `xcframework_path` recursively for
    `*.swiftmodule` directories. For each arch prefix (e.g.
    `arm64-apple-ios`), picks the most-permissive existing variant
    (package > private > public) as the source, runs four text
    passes against it, then writes the result to every variant that
    exists for that arch:

      1. `_SWIFTINTERFACE_USABLE_FROM_INLINE_BEFORE_PACKAGE_RE` strips
         a `@usableFromInline` annotation that sits on its own line
         immediately above a `package` declaration that pass 3 will
         promote. The attribute is only valid on internal/package
         decls — once we make the decl `public` the line becomes a
         compile error ("'@usableFromInline' can only be applied to
         internal or package declarations").
      2. `_SWIFTINTERFACE_INLINE_USABLE_FROM_INLINE_BEFORE_PACKAGE_RE`
         handles the same attribute when it sits inline on the same
         line as the decl (e.g. `@usableFromInline package struct X`).
      3. `_SWIFTINTERFACE_PACKAGE_DECL_RE` upgrades decl-position
         `package` tokens to `public` (anchored with a lookahead for
         the next declaration keyword — possibly preceded by other
         attributes / modifiers — so the `package` keyword in
         `import package_X` or in a string literal `"package "` can
         never match). The keyword list includes `macro`, since
         Swift 5.9 allows `package macro M = #externalMacro(...)`.
      4. `_SWIFTINTERFACE_PACKAGE_IMPORT_RE` upgrades `package import
         X` lines to `public import X`. When the source is the
         `.package.swiftinterface` (the common case), a `package
         import` line gets copied into the public interface; without
         this pass, non-same-package consumers reading `.swiftinterface`
         fail to resolve the import.

    Idempotent: re-running on an already-patched xcframework is a
    no-op (no more `package` tokens in decl position, .swiftinterface
    and .package.swiftinterface have identical content). Silently
    skips arches whose `.swiftinterface` files don't exist or aren't
    UTF-8.
    """
    if not xcframework_path.is_dir():
        return
    _PACKAGE_SUFFIX = ".package.swiftinterface"
    _PRIVATE_SUFFIX = ".private.swiftinterface"
    _PUBLIC_SUFFIX = ".swiftinterface"
    for module_dir in xcframework_path.rglob("*.swiftmodule"):
        if not module_dir.is_dir():
            continue
        # Collect arch prefixes present in this module dir. An arch
        # prefix is the filename stem before the longest matching
        # ".[*.]swiftinterface" suffix.
        arch_prefixes: Set[str] = set()
        for child in module_dir.iterdir():
            name = child.name
            if name.endswith(_PACKAGE_SUFFIX):
                arch_prefixes.add(name[: -len(_PACKAGE_SUFFIX)])
            elif name.endswith(_PRIVATE_SUFFIX):
                arch_prefixes.add(name[: -len(_PRIVATE_SUFFIX)])
            elif name.endswith(_PUBLIC_SUFFIX):
                arch_prefixes.add(name[: -len(_PUBLIC_SUFFIX)])
        for arch in arch_prefixes:
            public_path = module_dir / f"{arch}{_PUBLIC_SUFFIX}"
            private_path = module_dir / f"{arch}{_PRIVATE_SUFFIX}"
            package_path = module_dir / f"{arch}{_PACKAGE_SUFFIX}"
            # Pick the richest source. .package is a strict superset of
            # .private which is a strict superset of .swiftinterface,
            # so the most-permissive variant carries every decl we'd
            # otherwise need to merge.
            if package_path.is_file():
                source_path = package_path
            elif private_path.is_file():
                source_path = private_path
            else:
                source_path = public_path
            if not source_path.is_file():
                continue
            try:
                text = source_path.read_text()
            except (UnicodeDecodeError, FileNotFoundError):
                continue
            edited = _SWIFTINTERFACE_USABLE_FROM_INLINE_BEFORE_PACKAGE_RE.sub(
                "", text
            )
            edited = _SWIFTINTERFACE_INLINE_USABLE_FROM_INLINE_BEFORE_PACKAGE_RE.sub(
                "", edited
            )
            after_decl = _SWIFTINTERFACE_PACKAGE_DECL_RE.sub(
                r"\1public\2", edited
            )
            if after_decl == edited:
                # No `package` decls to promote — leave every variant
                # untouched. Cross-copying here would over-expose
                # @usableFromInline internals (from .private) into
                # .swiftinterface (the public ABI surface), which
                # consumers MUST NOT see. `package import` lines
                # without any `package` decl can stay in the
                # .package.swiftinterface only — non-same-package
                # consumers never read that variant.
                continue
            edited = _SWIFTINTERFACE_PACKAGE_IMPORT_RE.sub(
                r"\1public\2", after_decl
            )
            # Promote: write the rewritten (package→public) text to
            # every variant that exists for this arch. Each variant
            # ends up identical — every consumer reading any variant
            # sees the same full package-promoted-to-public surface.
            # We do NOT create variants that weren't there before;
            # swiftc only consults variants it expects to find.
            for target_path in (public_path, private_path, package_path):
                if target_path.exists() or target_path is source_path:
                    target_path.write_text(edited)


def _apply_consume_external_sibling_edits(
    staged_dir: Path, plan: Plan
) -> None:
    """Apply every `consume_external_sibling` PackageSwiftEdit on the plan.

    Two surgical text edits run against the active Package.swift:

      1. **Overlay injection** of `.binaryTarget(name: P, path: <rel>)`
         entries for each consume edit. Routed through
         `edit_inject_or_extend_overlay_binary_targets`, which:
           - Handles wrapper-style manifests (CustomTarget,
             toTarget()) and plain literal target arrays uniformly.
           - Is idempotent: re-running adds nothing if every entry is
             already in the overlay block with the same relative path.
           - Survives the dedup-overlap inter-unit edits Execute applies
             later (they share the same overlay block).
         Paths are relativized to the package root via
         `os.path.relpath` — SPM rejects absolute `.binaryTarget(path:)`.

      2. **`.product()` → `"P"` rewrite** in every target's
         `dependencies:` array. Routed through
         `edit_rewrite_external_product_to_string_dep_in_all_targets`.
         Once the overlay supplies a binaryTarget named P, SPM resolves
         a bare-string dep `"P"` to that binaryTarget; the `.product()`
         shape always disambiguates to the external package, which is
         now orphaned and would not be resolved. Test targets are
         included because a test target that imports P would dangle
         otherwise.

    Idempotency at the function level — re-running on a manifest that
    has already been edited is a no-op (the overlay block stays in
    place, the `.product(...)` calls have already been collapsed, and
    both helpers detect that and short-circuit).

    No-op when `plan.package_swift_edits` contains no consume edits.
    """
    consume_edits = [
        edit
        for edit in plan.package_swift_edits
        if edit.kind == "consume_external_sibling"
    ]
    if not consume_edits:
        return

    manifest_path = _select_active_manifest(staged_dir)
    if not manifest_path.is_file():
        raise PrepareBug(
            f"_apply_consume_external_sibling_edits: active manifest not "
            f"found at {manifest_path}"
        )

    # Before splicing the consumed siblings into the manifest, patch
    # each sibling xcframework's `.swiftinterface` files so its
    # `package`-scoped declarations are exposed as `public`. Necessary
    # when the upstream package used Swift's `package` access modifier:
    # in the original source build, sibling targets share a package
    # boundary and can see each other's `package` symbols, but our
    # overlay splits them into separate `.binaryTarget`s — to the
    # umbrella's swiftc, each binaryTarget is its own package, so a
    # `@usableFromInline internal` declaration in one sibling that
    # references a `package` type in another sibling fails type lookup
    # ("no type named '_Test' in module 'IssueReportingPackageSupport'").
    # The patch is idempotent and surgical: only declaration-position
    # `package` tokens are upgraded; the underlying binaries are
    # untouched and the symbol's runtime visibility is unchanged. Any
    # `.package.swiftinterface` files are deleted: they describe
    # intra-package surface area that no external consumer is allowed
    # to use, and leaving them present can mislead the swiftinterface
    # verifier.
    for edit in consume_edits:
        if edit.xcframework_path is not None:
            _patch_sibling_swiftinterfaces_for_package_access(
                edit.xcframework_path
            )

    package_root = staged_dir.resolve()
    substitutions: List[Tuple[str, str]] = []
    for edit in consume_edits:
        if edit.xcframework_path is None:
            raise PrepareBug(
                f"consume_external_sibling edit for product "
                f"{edit.product_name!r} is missing xcframework_path — Plan "
                f"should never emit one without it populated."
            )
        rel_path = os.path.relpath(
            edit.xcframework_path.resolve(), package_root
        )
        substitutions.append((edit.product_name, rel_path))

    text = manifest_path.read_text()
    edited = edit_inject_or_extend_overlay_binary_targets(text, substitutions)
    identities_to_strip: Set[str] = set()
    for edit in consume_edits:
        # `package_identity` is None for injection-only edits emitted by
        # Plan Phase 2 (sibling xcframeworks referenced solely via a
        # consumed sibling's swiftinterface, not by any `.product()`
        # call in the umbrella's source). For those, there's nothing to
        # rewrite and no `.package(url:)` to strip — the binaryTarget
        # overlay injection above is the whole edit.
        if edit.package_identity is None:
            continue
        edited = edit_rewrite_external_product_to_string_dep_in_all_targets(
            edited,
            edit.product_name,
            edit.package_identity,
        )
        identities_to_strip.add(edit.package_identity)

    # Close the swiftinterface search-path gap: for every target whose
    # `dependencies:` array (post-rewrite) TRANSITIVELY reaches a
    # consumed sibling — directly OR through other intra-package
    # targets — ensure it also names EVERY consumed sibling.
    #
    # Why direct-only isn't enough: in swift-case-paths, CasePaths
    # depends on CasePathsCore (intra-package) and CasePathsCore in
    # turn depends on IssueReporting / XCTestDynamicOverlay (consumed
    # siblings post-rewrite). CasePathsCore's emitted swiftinterface
    # `import IssueReporting` lines drag those modules into CasePaths'
    # compile too, but CasePaths' direct deps mention only
    # CasePathsCore — so a direct-only closure leaves CasePaths
    # without FRAMEWORK_SEARCH_PATHS for the upstream siblings and
    # `xcodebuild archive` fails with "Unable to find module
    # dependency: 'IssueReporting'". Plus, swift-case-paths' CasePaths
    # source itself does `@_spi(CurrentTestCase) import
    # XCTestDynamicOverlay` — SPM let this fly because
    # XCTestDynamicOverlay was reachable through CasePathsCore's deps;
    # we have to preserve that reachability under the overlay.
    #
    # Algorithm: BFS backward from `consumed_names`. Start with
    # `reachable = consumed_names`. Each round, find every target
    # whose deps array names at least one element of `reachable`; for
    # each new such target, augment its deps with ALL `consumed_names`
    # and add it to `reachable`. Repeat to fixpoint. The augmentation
    # itself is what feeds the next round — once T's deps contain a
    # consumed sibling, anything that string-depends on T is picked up
    # in the following iteration. Idempotent and order-independent.
    consumed_names = [edit.product_name for edit in consume_edits]
    reachable: Set[str] = set(consumed_names)
    while True:
        candidates = _target_names_with_string_deps_in_set(
            edited, reachable
        )
        progress = False
        for target_name in candidates:
            if target_name in reachable:
                continue
            edited = edit_augment_target_dependencies(
                edited, target_name, consumed_names
            )
            reachable.add(target_name)
            progress = True
        if not progress:
            break
    # Strip orphan `.product(name: ?, package: ID)` refs for every
    # identity we're about to strip from `Package.dependencies`. The
    # rewrite pass above only converts `.product()` calls for products
    # we actually built as siblings — but a transitive package can
    # export products the umbrella references from non-selected
    # targets (e.g. xctest-dynamic-overlay's `IssueReportingTestSupport`
    # is referenced by swift-dependencies' `DependenciesTestSupport`
    # target, which `--product Dependencies` excludes from the closure;
    # the orchestrator therefore trims it from `referenced_products`
    # and never builds it). Those references survive the rewrite and,
    # once we drop the package, dangle as "unknown package 'ID' in
    # dependencies of target T" at dump-package time. Deletion is the
    # right move because the target the ref lived on isn't being built
    # — there's no compile to satisfy, only the manifest needs to
    # parse. (Codex P3 r1 regression on swift-dependencies.)
    edited = edit_strip_orphan_product_refs_for_identities_in_all_targets(
        edited, identities_to_strip
    )

    # Drop the `.package(url: ..., ...)` declarations for every identity
    # we just consumed. Without this, SPM still resolves the external
    # package while the overlay-injected `.binaryTarget(name: P)` shares
    # the same target name P — SPM reports "multiple packages declare
    # targets with a conflicting name" and the build aborts before any
    # source compiles. The orchestrator builds EVERY referenced product
    # of each transitive identity as a sibling xcframework, so removing
    # the .package(url:) line never strands a still-consumed product
    # (the orphan-product strip immediately above mops up the
    # `.product()` refs whose products WEREN'T built as siblings).
    edited = edit_strip_package_deps_for_identities(
        edited, identities_to_strip
    )

    # Last edit step: guard any `for target in package.targets where ...`
    # loop the manifest emits after the Package(...) call. Once the
    # overlay injects binaryTargets into `package.targets`, a settings-
    # mutation loop that touches "all non-system targets" hits them
    # too and dump-package rejects the manifest because SPM doesn't
    # allow swiftSettings on binaryTargets. See swift-perception 1.6.0.
    edited = edit_guard_target_loops_from_overlay(edited)

    if edited != text:
        manifest_path.write_text(edited)
        info(
            f"  Consumed {len(consume_edits)} prebuilt sibling "
            f"xcframework(s): "
            + ", ".join(e.product_name for e in consume_edits)
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

    # Strip post-init `package.dependencies.append(...)` /
    # `package.targets.append(...)` (and `+=`) statements before any
    # other edit lands. These are author-acknowledged optional tooling
    # blocks (docc-plugin, macro-testing, snapshot-testing, benchmarks,
    # examples) that transitively reintroduce packages we may have
    # already replaced with overlay binaryTargets — see
    # `edit_strip_post_init_package_mutations` for the full rationale.
    # Doing this BEFORE synth_*, macro-plugin builds, AND overlay
    # injection means every subsequent step sees the clean consumer-
    # facing graph, not the author's testing graph.
    stripped_text = edit_strip_post_init_package_mutations(original_text)
    if stripped_text != original_text:
        manifest_path.write_text(stripped_text)
        info(
            "  Stripped post-init package mutations "
            "(optional build-time tooling)"
        )

    # `swift package add-product` rejects manifests below tools-version
    # 5.2 (SPM's own error message recommends 5.5). Old packages like
    # MBProgressHUD (4.2) work fine under 5.5's manifest format — just
    # bump the line before invoking add-product. Only relevant when the
    # plan actually has synth edits.
    has_synth = any(
        edit.kind in ("synth_dynamic_library", "synth_library")
        for edit in plan.package_swift_edits
    )
    if has_synth:
        bumped_from = _bump_tools_version_if_below(manifest_path, (5, 5))
        if bumped_from is not None:
            info(
                f"  Bumped swift-tools-version "
                f"{bumped_from[0]}.{bumped_from[1]} → 5.5 "
                f"in {manifest_path.name} (required for add-product)"
            )

    # Apply edits in plan order so the post-edit products array reflects
    # planner sequencing. synth_dynamic_library and synth_library are both
    # implemented by the same `swift package add-product` call; the only
    # difference is naming policy at plan time. Routed through a
    # base-manifest proxy because add-product is hard-coded to edit
    # Package.swift even when SPM is reading Package@swift-X.Y.swift,
    # and through a surgery fallback when the manifest's `products:` is
    # not a literal array.
    for edit in plan.package_swift_edits:
        if edit.kind in ("synth_dynamic_library", "synth_library"):
            _apply_single_synth_edit(
                staged_dir, manifest_path, edit.product_name, edit.targets
            )

    # NOTE: `consume_external_sibling` edits intentionally do NOT run here.
    # They have to land AFTER `_apply_macro_support_edits` invokes
    # `swift build --product <macro>` because the overlay-injected
    # `.binaryTarget(name: P)` entries can collide with transitive test
    # tooling that some manifests conditionally pull in. The canonical
    # case is swift-case-paths: its OMIT_MACRO_TESTS-gated block adds
    # `swift-macro-testing` → `swift-snapshot-testing` → `swift-custom-dump`
    # → `xctest-dynamic-overlay`, which provides its own `IssueReporting`
    # target. With the overlay in place at macro-build time, SPM aborts
    # with "multiple packages declare targets with a conflicting name".
    # The macro plugin only needs the original swift-syntax graph to
    # compile, so we defer the overlay rewrite to `prepare()`'s
    # post-macro-build step. See `prepare()`'s call sequence.

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
    targets_by_name = {t.name: t for t in targets}
    failures: List[str] = []
    implicated: List[Product] = []

    for edit in plan.package_swift_edits:
        if edit.kind == "consume_external_sibling":
            # The overlay must have injected a binaryTarget with the
            # product's name. If it didn't, the umbrella build will fail
            # later with a missing-module error; surface it here with
            # full diff context instead.
            tgt = targets_by_name.get(edit.product_name)
            if tgt is None:
                failures.append(
                    f"consume_external_sibling: target "
                    f"{edit.product_name!r} is absent from the post-edit "
                    f"dumped manifest (the overlay's .binaryTarget(...) "
                    f"injection didn't take effect)"
                )
            elif tgt.kind != "binary":
                failures.append(
                    f"consume_external_sibling: target "
                    f"{edit.product_name!r} has kind {tgt.kind!r}; "
                    f"expected {'binary'!r} (overlay injection landed "
                    f"on the wrong shape)"
                )
            continue
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
    no_op = not plan.package_swift_edits and not plan.macros
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
    # Macro support runs AFTER synth edits land. `swift build` will
    # resolve external deps (swift-syntax) as a side effect, populating
    # `.build/checkouts/` so later `swift package resolve` and
    # `xcodebuild archive` calls don't repeat the work. The manifest
    # strip is applied after the host build so a build failure leaves
    # the manifest still parseable for diagnostics.
    _apply_macro_support_edits(staged_dir, plan, verbose=verbose)
    # `consume_external_sibling` rewrites MUST land after the host-side
    # macro build above. Injecting `.binaryTarget(name: P)` entries up
    # front collides with transitive packages that some manifests
    # conditionally pull in for testing (e.g. swift-case-paths' default
    # `swift-macro-testing` → … → `xctest-dynamic-overlay` chain, which
    # owns the same target names). Running this after the macro plugin
    # has linked means SPM resolves the macro graph against the original
    # transitive set, then the overlay rewrite takes effect in time for
    # `xcodebuild archive`.
    _apply_consume_external_sibling_edits(staged_dir, plan)
    raw, products, targets, platforms, name, tv = validate_prepared_manifest(
        staged_dir, plan, original
    )
    edit_count = len(plan.package_swift_edits) + len(plan.macros)
    success(f"  Prepare validated {edit_count} edit(s) ✓")
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
