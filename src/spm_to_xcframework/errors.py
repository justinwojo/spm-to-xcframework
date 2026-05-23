"""Typed exception hierarchy used across the six phases.

`main()` catches `SpmToXcframeworkError` and translates it into a
phase-specific exit code. Anything else escapes as a Python traceback,
which is the right behaviour for tool bugs. Per-phase subclasses split
into `*UserError` (clean one-line message) and `*Bug` (full traceback)
where the distinction matters.
"""
from __future__ import annotations


class SpmToXcframeworkError(Exception):
    """Base class for all phase errors. main() catches these and exits with
    a phase-specific exit code; everything else is treated as a tool bug
    and traceback'd."""

    exit_code: int = 2


class FetchError(SpmToXcframeworkError):
    """Could not clone, copy, or stage the package source. User-facing:
    network failure, missing tag, missing local path, copy permission
    error."""

    exit_code = 3


class InspectError(SpmToXcframeworkError):
    """Could not parse Package.swift or interrogate the staged package.
    User-facing: unsupported swift-tools-version, malformed manifest,
    missing required toolchain output."""

    exit_code = 4


class PlanError(SpmToXcframeworkError):
    """Plan phase invariants violated. User-facing: filter matched
    nothing, --target on a binary target, --target in binary mode."""

    exit_code = 5


class PrepareError(SpmToXcframeworkError):
    """Base class for Prepare-phase failures. Split into two subclasses:
    `PrepareUserError` for clean-message user mistakes (unsupported
    manifest construct, product filter pointing at nothing, etc.) and
    `PrepareBug` for genuine invariant violations that should surface
    with a Python traceback. `PrepareError` itself is never raised —
    pick the right subclass."""

    exit_code = 6


class PrepareUserError(PrepareError):
    """The user's manifest or invocation put Prepare in a state it
    can't handle cleanly — unsupported Swift constructs, missing
    `products:` array, walker couldn't find balanced parens in a
    malformed manifest, etc. Surfaces as a one-line clean error."""


class TargetCallNotFoundError(PrepareUserError):
    """`edit_replace_with_binary_target` couldn't locate a literal
    `.target(name: T, ...)` (or `.executableTarget`/`.testTarget`) call
    to substitute, AND no existing `.binaryTarget(name: T, ...)` was
    present either. Distinct from generic `PrepareUserError` so the
    dedup-overlap router can catch it specifically and fall back to
    the overlay branch (which doesn't require a literal call to find).

    Subclass so existing `except PrepareUserError` sites still fire —
    only the dedup-overlap router needs the narrower type."""


class PrepareBug(PrepareError):
    """A real invariant violation inside Prepare — round-trip validator
    caught edit drift, the planner asked to edit a product Prepare
    materialized into nothing, post-edit dump-package parse failure,
    etc. Surfaces with a traceback."""


class ExecuteError(SpmToXcframeworkError):
    """xcodebuild (or downstream tooling) failed. Surfaced with the
    parsed xcresult plus the top N errors per target."""

    exit_code = 7


class VerifyError(SpmToXcframeworkError):
    """Base class for Verify-phase exceptions. Split into two subclasses:
    `VerifyUserError` for clean-message user mistakes (output directory
    missing because `-o` was mistyped) and `VerifyBug` for invariant
    violations inside verify code itself. `VerifyError` itself is never
    raised — pick the right subclass. The base is kept so callers that
    need `VerifyError.exit_code` (the aggregate return code for
    `_finalize_with_verify`) keep working unchanged."""

    exit_code = 8


class VerifyUserError(VerifyError):
    """The user's invocation put Verify in a state it can't work in —
    output directory missing/not-a-directory is the canonical case.
    Surfaces as a one-line clean error."""


class VerifyBug(VerifyError):
    """A real invariant violation inside Verify (e.g. the strict check
    itself crashed on a shape it should have handled). Surfaces with a
    traceback."""
