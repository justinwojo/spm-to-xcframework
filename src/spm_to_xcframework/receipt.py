"""Conversion receipt — the always-written, authoritative run record.

Unlike the success-only `.spm-to-xcframework-manifest.json` (a cross-run
cleanup ledger of tool-produced basenames, see `output_manifest.py`), a
receipt is written into the output directory on EVERY build invocation —
success, partial, or failure. It is the one artifact that can prove a run
happened AND how it ended, which a directory scan can never do: a manifest
records only what succeeded, so a failed run leaves the prior manifest and
prior artifacts in place and nothing on disk distinguishes "last run
succeeded" from "last run failed and left stale artifacts behind."

Consumers (the swift-bindings corpus harness, and later the generator's
input preflight) read the receipt instead of scanning the directory for
`.xcframework` bundles. That kills the `no_primary_products` mislabel class:
a directory that contains only dependency siblings after the primary
conversion failed is classified from the receipt's `status` +
`missing_required_modules`, not from the absence of a matching directory.

Schema (v1) — the contract session 04 (converter graph closure) consumes:

    {
      "schema_version": 1,
      "tool": "spm-to-xcframework",
      "run_id": "<uuid4 hex>",
      "produced_at": "<iso8601 utc, seconds>",
      "status": "success" | "partial" | "failed",
      "package": {
        "source": "<url or path>",
        "requested_version": "<what the user asked for>",
        "resolved_version": "<tag actually used, may differ>",
        "revision": "<pinned revision or null>"
      },
      "toolchain": { "swift_version": "X.Y.Z" | null },
      "requested_slices": [ {"platform": "ios", "min_version": "15.0"}, ... ],
      "expected_primary_products": ["Alamofire", ...],
      "artifacts": [
        {"name": "Alamofire.xcframework", "module": "Alamofire",
         "kind": "primary" | "dependency"}
      ],
      "modules": {
        "produced": ["Alamofire", "AlamofireDynamic", ...],
        "missing_required": ["StripeCore", ...]
      },
      "failures": [
        {"phase": "execute", "unit": "Stripe", "root_diagnostic": "...",
         "result_bundle_path": null}
      ]
    }

`modules.missing_required` is the module-edge field session 04 needs:
which required compile modules were expected but NOT produced. On a clean
success it is empty; on any failure it names exactly the expected primary
products that never made it to disk.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from .log import warn

RECEIPT_FILENAME = ".spm-to-xcframework-receipt.json"
RECEIPT_SCHEMA_VERSION = 1

# Receipt status values. A run is `success` only when it exited cleanly AND
# this run itself produced every expected primary product (a preserved prior
# last-good does NOT count — it is not this run's output); `partial` when it
# exited cleanly but a required primary was not produced this run or a
# non-fatal failure was recorded en route (the canonical cases are a
# `--best-effort-transitives` dropped sibling and a verified artifact that
# failed to promote); `failed` for any non-zero exit, raised phase error, or
# unexpected crash.
STATUS_SUCCESS = "success"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"

# Artifact ownership, mirroring the manifest's kinds.
KIND_PRIMARY = "primary"
KIND_DEPENDENCY = "dependency"


@dataclass
class ReceiptArtifact:
    """One produced-and-verified xcframework, recorded by basename plus the
    contained module name and its ownership relative to this run's intent."""

    name: str          # bare basename, e.g. "Alamofire.xcframework"
    module: str        # module name (basename without ".xcframework")
    kind: str          # KIND_PRIMARY | KIND_DEPENDENCY

    def to_json(self) -> dict:
        return {"name": self.name, "module": self.module, "kind": self.kind}

    @classmethod
    def from_json(cls, d: dict) -> "ReceiptArtifact":
        return cls(name=d["name"], module=d["module"], kind=d["kind"])


@dataclass
class ReceiptFailure:
    """One structured failure record. `unit` is the build unit / product /
    transitive identity the failure is attributed to (None when the failure
    is not unit-scoped, e.g. an early Plan error). `result_bundle_path` is
    best-effort: xcresult bundles live under the run's work_dir and are
    deleted unless `--keep-work`, so this is frequently null rather than a
    dangling path — we never invent one."""

    phase: str                             # fetch|inspect|plan|prepare|execute|verify|promote|closure
    unit: Optional[str]
    root_diagnostic: str
    result_bundle_path: Optional[str] = None

    def to_json(self) -> dict:
        return {
            "phase": self.phase,
            "unit": self.unit,
            "root_diagnostic": self.root_diagnostic,
            "result_bundle_path": self.result_bundle_path,
        }

    @classmethod
    def from_json(cls, d: dict) -> "ReceiptFailure":
        return cls(
            phase=d["phase"],
            unit=d.get("unit"),
            root_diagnostic=d.get("root_diagnostic", ""),
            result_bundle_path=d.get("result_bundle_path"),
        )


@dataclass
class Receipt:
    """Mutable run-scoped accumulator + serializable record.

    Created once in `main()` before mode routing and shared by reference
    into every child config (the child-config factories deliberately do NOT
    reset this field, unlike `collected_entries`), so a transitive child's
    failure is visible to the top-level finalizer. Phases append to it at
    their boundaries; `main()` snapshots the environment, sets `status`, and
    writes it to the FINAL output directory in a `finally`.
    """

    run_id: str
    package_source: str
    requested_version: str = ""
    resolved_version: str = ""
    revision: Optional[str] = None
    status: str = STATUS_FAILED  # pessimistic default: only upgraded on a clean exit
    swift_toolchain: Optional[str] = None
    requested_slices: List[Tuple[str, str]] = field(default_factory=list)
    expected_primary_products: List[str] = field(default_factory=list)
    artifacts: List[ReceiptArtifact] = field(default_factory=list)
    produced_modules: List[str] = field(default_factory=list)
    missing_required_modules: List[str] = field(default_factory=list)
    failures: List[ReceiptFailure] = field(default_factory=list)
    produced_at: str = ""  # stamped at write time
    # In-memory accumulator for the import-closure gate (graph_closure.py):
    # modules a produced interface publicly imports that the produced set does
    # not provide, yet the dependency graph owns. NOT a separate serialized
    # field — the finalizer unions it INTO `missing_required_modules` so the
    # serialized schema (v1) stays exactly as documented above. Kept distinct
    # in memory only so the union is idempotent and order-stable regardless of
    # how many times the gate records into it.
    closure_missing_modules: List[str] = field(default_factory=list)

    def record_closure_missing(self, module: str) -> None:
        """De-duplicating append used by the import-closure gate."""
        if module not in self.closure_missing_modules:
            self.closure_missing_modules.append(module)

    def record_failure(
        self,
        phase: str,
        unit: Optional[str],
        root_diagnostic: str,
        result_bundle_path: Optional[str] = None,
    ) -> None:
        self.failures.append(
            ReceiptFailure(
                phase=phase,
                unit=unit,
                root_diagnostic=root_diagnostic,
                result_bundle_path=result_bundle_path,
            )
        )

    def to_json(self) -> dict:
        return {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "tool": "spm-to-xcframework",
            "run_id": self.run_id,
            "produced_at": self.produced_at,
            "status": self.status,
            "package": {
                "source": self.package_source,
                "requested_version": self.requested_version,
                "resolved_version": self.resolved_version,
                "revision": self.revision,
            },
            "toolchain": {"swift_version": self.swift_toolchain},
            "requested_slices": [
                {"platform": p, "min_version": v} for p, v in self.requested_slices
            ],
            "expected_primary_products": list(self.expected_primary_products),
            "artifacts": [a.to_json() for a in self.artifacts],
            "modules": {
                "produced": list(self.produced_modules),
                "missing_required": list(self.missing_required_modules),
            },
            "failures": [f.to_json() for f in self.failures],
        }

    @classmethod
    def from_json(cls, d: dict) -> "Receipt":
        pkg = d.get("package", {}) or {}
        tc = d.get("toolchain", {}) or {}
        mods = d.get("modules", {}) or {}
        return cls(
            run_id=d.get("run_id", ""),
            package_source=pkg.get("source", ""),
            requested_version=pkg.get("requested_version", ""),
            resolved_version=pkg.get("resolved_version", ""),
            revision=pkg.get("revision"),
            status=d.get("status", STATUS_FAILED),
            swift_toolchain=tc.get("swift_version"),
            requested_slices=[
                (s.get("platform", ""), s.get("min_version", ""))
                for s in d.get("requested_slices", [])
            ],
            expected_primary_products=list(d.get("expected_primary_products", [])),
            artifacts=[ReceiptArtifact.from_json(a) for a in d.get("artifacts", [])],
            produced_modules=list(mods.get("produced", [])),
            missing_required_modules=list(mods.get("missing_required", [])),
            failures=[ReceiptFailure.from_json(f) for f in d.get("failures", [])],
            produced_at=d.get("produced_at", ""),
        )


def write_receipt(output_dir: Path, receipt: Receipt) -> None:
    """Atomically write `receipt` into `<output_dir>/<RECEIPT_FILENAME>`.

    Stamps `produced_at` at write time. Uses a temp-file + `os.replace`
    in the same directory so a crash mid-write leaves the prior receipt (if
    any) intact rather than a truncated file. Creates `output_dir` if it is
    missing — a failed run that never produced artifacts must still be able
    to deposit its receipt as proof of the attempt.
    """
    import datetime

    receipt.produced_at = datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / RECEIPT_FILENAME
    tmp_path = output_dir / (RECEIPT_FILENAME + ".tmp")
    try:
        tmp_path.write_text(json.dumps(receipt.to_json(), indent=2) + "\n")
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def read_receipt(output_dir: Path) -> Optional[Receipt]:
    """Tolerant reader used by consumers (and tests): returns the parsed
    Receipt, or None for every unhappy-path case (missing file, unparseable
    JSON, wrong top-level shape, unknown schema version). Consumers treat
    None as "input provenance unknown" rather than branching on the cause.
    """
    path = output_dir / RECEIPT_FILENAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        warn(f"Ignoring malformed receipt {path}: {exc}")
        return None
    if not isinstance(data, dict):
        warn(f"Ignoring malformed receipt {path}: top-level is not a dict")
        return None
    version = data.get("schema_version")
    if version != RECEIPT_SCHEMA_VERSION:
        warn(
            f"Ignoring receipt {path}: unknown schema version "
            f"{version!r} (expected {RECEIPT_SCHEMA_VERSION})"
        )
        return None
    try:
        return Receipt.from_json(data)
    except (KeyError, TypeError, ValueError) as exc:
        warn(f"Ignoring malformed receipt {path}: {exc}")
        return None
