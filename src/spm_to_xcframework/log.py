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


def _color_enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def _wrap(text: str, color: str) -> str:
    if not _color_enabled():
        return text
    return f"{_ANSI[color]}{text}{_ANSI['reset']}"


def info(msg: str) -> None:
    print(_wrap(msg, "cyan"))


def success(msg: str) -> None:
    print(_wrap(msg, "green"))


def warn(msg: str) -> None:
    print(_wrap(msg, "yellow"), file=sys.stderr)


def dim(msg: str) -> None:
    print(_wrap(msg, "dim"))


def bold(msg: str) -> None:
    print(_wrap(msg, "bold"))


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
        print(_wrap(msg, "dim"))
