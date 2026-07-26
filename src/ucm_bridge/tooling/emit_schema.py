"""Emit the canonical model as JSON Schema.

Pydantic models are the source of truth (ADR-0002); these files are a build
artifact, committed so that non-Python consumers and reviewers can read the
model, and drift-checked in CI by ``tests/test_schema_drift.py``.

Usage::

    python -m ucm_bridge.tooling.emit_schema            # write schemas/
    python -m ucm_bridge.tooling.emit_schema --check    # fail if out of date
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ucm_bridge.canonical.base import CANONICAL_MODEL_VERSION
from ucm_bridge.canonical.registry import all_entity_types
from ucm_bridge.canonical.snapshot import EstateSnapshot
from ucm_bridge.connectors.capabilities import CapabilityManifest
from ucm_bridge.connectors.contracts import ApplyAuthorization, ApplyPlan, DryRunReceipt

SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
BASE_URI = "https://schemas.ucm-bridge.dev"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def schema_dir(root: Path | None = None) -> Path:
    version = CANONICAL_MODEL_VERSION.split(".")[0]
    return (root or _repo_root()) / "schemas" / f"v{version}"


def _document(model: Any, schema_id: str) -> dict[str, Any]:
    schema = model.model_json_schema(mode="serialization")
    return {
        "$schema": SCHEMA_DIALECT,
        "$id": f"{BASE_URI}/{schema_id}.json",
        "x-canonical-model-version": CANONICAL_MODEL_VERSION,
        **schema,
    }


def build_schemas() -> dict[str, dict[str, Any]]:
    """Every schema this project publishes, keyed by relative file path."""
    documents: dict[str, dict[str, Any]] = {}

    entity_types = all_entity_types()
    for kind, cls in sorted(entity_types.items()):
        documents[f"entities/{kind}.json"] = _document(cls, f"entities/{kind}")

    documents["EstateSnapshot.json"] = _document(EstateSnapshot, "EstateSnapshot")
    documents["ApplyPlan.json"] = _document(ApplyPlan, "ApplyPlan")
    documents["ApplyAuthorization.json"] = _document(ApplyAuthorization, "ApplyAuthorization")
    documents["DryRunReceipt.json"] = _document(DryRunReceipt, "DryRunReceipt")
    documents["CapabilityManifest.json"] = _document(CapabilityManifest, "CapabilityManifest")

    documents["index.json"] = {
        "$schema": SCHEMA_DIALECT,
        "$id": f"{BASE_URI}/index.json",
        "canonicalModelVersion": CANONICAL_MODEL_VERSION,
        "entityKinds": sorted(entity_types),
        "domains": _domain_index(entity_types),
    }
    return documents


def _domain_index(entity_types: dict[str, Any]) -> dict[str, list[str]]:
    domains: dict[str, list[str]] = {}
    for kind, cls in entity_types.items():
        domains.setdefault(cls.domain, []).append(kind)
    return {domain: sorted(kinds) for domain, kinds in sorted(domains.items())}


def _serialise(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_schemas(target: Path) -> list[Path]:
    written: list[Path] = []
    for relative, document in build_schemas().items():
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_serialise(document), encoding="utf-8")
        written.append(path)
    return written


def check_schemas(target: Path) -> list[str]:
    """Relative paths that are missing or out of date."""
    problems: list[str] = []
    for relative, document in build_schemas().items():
        path = target / relative
        if not path.is_file():
            problems.append(f"missing: {relative}")
        elif path.read_text(encoding="utf-8") != _serialise(document):
            problems.append(f"out of date: {relative}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Do not write; exit non-zero if the committed schemas are stale.",
    )
    parser.add_argument("--out", type=Path, default=None, help="Output directory.")
    args = parser.parse_args(argv)

    target = args.out or schema_dir()

    if args.check:
        problems = check_schemas(target)
        if problems:
            print("Canonical JSON Schema is out of date:", file=sys.stderr)
            for problem in problems:
                print(f"  {problem}", file=sys.stderr)
            print(
                "\nRegenerate with: python -m ucm_bridge.tooling.emit_schema",
                file=sys.stderr,
            )
            return 1
        print(f"Schemas up to date ({len(build_schemas())} documents).")
        return 0

    written = write_schemas(target)
    print(f"Wrote {len(written)} schema documents to {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
