"""Coloured logging helpers used by every phase.

Output goes through `print()` — there's no logging.Logger object — so
phase code can rely on ordering matching call order. Colours are
disabled when `NO_COLOR` is set in the environment or stdout is not a
TTY (the standard convention for CLI tooling).
"""
from __future__ import annotations

import os
import sys
from typing import NoReturn

# ANSI color codes. Disabled when NO_COLOR is set or stdout is not a TTY.
_ANSI = {
    "red": "\033[0;31m",
    "green": "\033[0;32m",
    "yellow": "\033[0;33m",
    "cyan": "\033[0;36m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "reset": "\033[0m",
}


# When True, `info`/`success`/`dim`/`bold` route to stderr instead of
# stdout. Used by `--dry-run-json` (set by `cli.main()` before the
# pipeline runs) so stdout stays a clean JSON document the campaign
# tooling can pipe straight into `jq`. Read-only outside the CLI entry
# point.
_LOG_STDOUT_TO_STDERR = False


def set_log_stdout_to_stderr(enabled: bool) -> None:
    """Route info/success/dim/bold to stderr instead of stdout. The CLI
    flips this on for --dry-run-json so the only stdout writes are the
    JSON document at the end. Lives as a setter (not a direct attribute
    mutation) so it survives the build_single_file.py concatenation step
    — there is no `log` submodule to reach into in the single-file
    artifact, but the setter is a module-global function either way.
    """
    global _LOG_STDOUT_TO_STDERR
    _LOG_STDOUT_TO_STDERR = enabled


def _stdout() -> "object":
    return sys.stderr if _LOG_STDOUT_TO_STDERR else sys.stdout


def _color_enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return _stdout().isatty()


def _wrap(text: str, color: str) -> str:
    if not _color_enabled():
        return text
    return f"{_ANSI[color]}{text}{_ANSI['reset']}"


def out(msg: str) -> None:
    """Plain (uncoloured) user-facing output, routed through `_stdout()`.

    Use this when the line is part of a structured human-readable
    rendering that shouldn't carry inline colour (e.g. `print_package`
    for `--inspect-only`). The route still respects
    `set_log_stdout_to_stderr` so `--dry-run-json` keeps stdout clean.

    Codex review P3 catch (2026-05-22): without this helper,
    `print_package` used raw `print()` and would leak human-readable
    text onto stdout under `--inspect-only --dry-run-json`, breaking
    the "stdout is a clean JSON document" contract for that flag
    combination.
    """
    print(msg, file=_stdout())


def info(msg: str) -> None:
    print(_wrap(msg, "cyan"), file=_stdout())


def success(msg: str) -> None:
    print(_wrap(msg, "green"), file=_stdout())


def warn(msg: str) -> None:
    print(_wrap(msg, "yellow"), file=sys.stderr)


def dim(msg: str) -> None:
    print(_wrap(msg, "dim"), file=_stdout())


def bold(msg: str) -> None:
    print(_wrap(msg, "bold"), file=_stdout())


def die(msg: str) -> NoReturn:
    """Print an error message and exit with status 1.

    Reserved for top-level usage / argument errors. Phase code should raise
    a typed error from the hierarchy below; main() catches and translates.
    """
    print(_wrap(f"Error: {msg}", "red"), file=sys.stderr)
    sys.exit(1)


# Verbose logger — gated on Config.verbose at call sites.
def verbose_log(verbose: bool, msg: str) -> None:
    if verbose:
        print(_wrap(msg, "dim"), file=_stdout())
