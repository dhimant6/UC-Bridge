"""The committed JSON Schema must match the Pydantic models that generate it.

Pydantic is the source of truth (ADR-0002). The files under ``schemas/`` are a
build artifact, committed so reviewers and non-Python consumers can read the
model. This test is what stops them silently rotting.
"""

from __future__ import annotations

import json

from ucm_bridge.canonical.base import CANONICAL_MODEL_VERSION
from ucm_bridge.canonical.registry import all_entity_types
from ucm_bridge.tooling.emit_schema import build_schemas, check_schemas, schema_dir


def test_committed_schemas_are_up_to_date() -> None:
    problems = check_schemas(schema_dir())
    assert not problems, (
        "Canonical JSON Schema is stale. Regenerate with:\n"
        "  python -m ucm_bridge.tooling.emit_schema\n"
        f"Problems: {problems}"
    )


def test_every_entity_kind_has_a_schema() -> None:
    documents = build_schemas()
    for kind in all_entity_types():
        assert f"entities/{kind}.json" in documents


def test_schemas_declare_the_model_version() -> None:
    for name, document in build_schemas().items():
        if name == "index.json":
            assert document["canonicalModelVersion"] == CANONICAL_MODEL_VERSION
        else:
            assert document["x-canonical-model-version"] == CANONICAL_MODEL_VERSION


def test_entity_schemas_pin_kind_as_a_discriminator() -> None:
    documents = build_schemas()
    for kind in all_entity_types():
        schema = documents[f"entities/{kind}.json"]
        kind_property = schema["properties"]["kind"]
        assert kind_property.get("const") == kind or kind_property.get("enum") == [kind], (
            f"{kind}.kind must be pinned to a literal so the union is discriminated"
        )


def test_index_lists_every_domain() -> None:
    index = build_schemas()["index.json"]
    assert set(index["domains"]) == {
        "callhandling",
        "collaboration",
        "contactcenter",
        "dialplan",
        "endpoints",
        "identity",
        "messaging",
        "numbering",
        "policy",
        "trunking",
    }
    assert sum(len(v) for v in index["domains"].values()) == len(index["entityKinds"])


def test_schemas_are_valid_json_on_disk() -> None:
    directory = schema_dir()
    for path in directory.rglob("*.json"):
        json.loads(path.read_text(encoding="utf-8"))
