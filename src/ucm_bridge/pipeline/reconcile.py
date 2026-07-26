"""Cross-estate reconciliation (§4.6).

"The API returned 200" is not verification. This module compares what is
actually present on the target against what the source held, at attribute level,
after resolving both sides into a genuinely platform-neutral view.

The neutral view is the piece that makes the comparison meaningful: canonical
references hold ``canonical_id`` values scoped to one platform and instance, so
they are replaced with the referenced object's *natural key* before hashing. A
Munich user's ``primary_number_ref`` becomes ``+498912345101`` on both sides, and
the two estates become comparable.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ucm_bridge.canonical.base import CanonicalEntity, digest_of

UNRESOLVED = "<unresolved>"


def build_key_index(
    entities: Iterable[CanonicalEntity], key_for: Any
) -> dict[str, str]:
    """canonical_id -> natural key, for every entity a key can be derived for."""
    index: dict[str, str] = {}
    for entity in entities:
        key = key_for(entity)
        if key is not None:
            index[entity.canonical_id] = key
    return index


def neutral_view(entity: CanonicalEntity, key_index: Mapping[str, str]) -> dict[str, Any]:
    """Platform-neutral representation: content plus references as natural keys."""
    view: dict[str, Any] = dict(entity.content_view())
    for field, values in entity.reference_fields().items():
        resolved = [key_index.get(v, UNRESOLVED) for v in values]
        view[field] = resolved if field.endswith("_refs") else resolved[0]
    return view


def neutral_digest(entity: CanonicalEntity, key_index: Mapping[str, str]) -> str:
    return digest_of(neutral_view(entity, key_index))


class AttributeMismatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attribute: str
    source_value: Any = None
    target_value: Any = None


class EntityReconciliation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    natural_key: str
    status: str = Field(description="MATCHED, MISSING_ON_TARGET, EXTRA_ON_TARGET, or MISMATCHED.")
    source_canonical_id: str | None = None
    target_canonical_id: str | None = None
    mismatches: list[AttributeMismatch] = Field(default_factory=list)


class ReconciliationReport(BaseModel):
    """Object-count and attribute-level reconciliation, source versus target."""

    model_config = ConfigDict(extra="forbid")

    results: list[EntityReconciliation] = Field(default_factory=list)
    source_counts: dict[str, int] = Field(default_factory=dict)
    target_counts: dict[str, int] = Field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return all(r.status == "MATCHED" for r in self.results)

    def failures(self) -> list[EntityReconciliation]:
        return [r for r in self.results if r.status != "MATCHED"]

    def counts_by_status(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for result in self.results:
            counts[result.status] = counts.get(result.status, 0) + 1
        return counts

    def summary(self) -> str:
        parts = [f"{k}={v}" for k, v in sorted(self.counts_by_status().items())]
        return f"{'PASS' if self.passed else 'FAIL'} ({', '.join(parts)})"


def _counts(entities: Iterable[CanonicalEntity]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entity in entities:
        counts[entity.kind] = counts.get(entity.kind, 0) + 1
    return dict(sorted(counts.items()))


def reconcile(
    source_entities: Iterable[CanonicalEntity],
    target_entities: Iterable[CanonicalEntity],
    *,
    source_key_for: Any,
    target_key_for: Any,
    ignore_attributes: frozenset[str] = frozenset(),
) -> ReconciliationReport:
    """Compare two estates by (kind, natural key), then attribute by attribute.

    ``ignore_attributes`` exists for attributes that legitimately differ across
    platforms and have been reviewed - it is a declared exemption, recorded in
    the report's inputs, not a silent skip.
    """
    source = list(source_entities)
    target = list(target_entities)

    source_index = build_key_index(source, source_key_for)
    target_index = build_key_index(target, target_key_for)

    source_by_key: dict[tuple[str, str], CanonicalEntity] = {}
    for entity in source:
        key = source_key_for(entity)
        if key is not None:
            source_by_key[(entity.kind, key)] = entity

    target_by_key: dict[tuple[str, str], CanonicalEntity] = {}
    for entity in target:
        key = target_key_for(entity)
        if key is not None:
            target_by_key[(entity.kind, key)] = entity

    results: list[EntityReconciliation] = []

    for composite in sorted(set(source_by_key) | set(target_by_key)):
        kind, natural_key = composite
        src = source_by_key.get(composite)
        tgt = target_by_key.get(composite)

        if src is not None and tgt is None:
            results.append(
                EntityReconciliation(
                    kind=kind,
                    natural_key=natural_key,
                    status="MISSING_ON_TARGET",
                    source_canonical_id=src.canonical_id,
                )
            )
            continue
        if tgt is not None and src is None:
            results.append(
                EntityReconciliation(
                    kind=kind,
                    natural_key=natural_key,
                    status="EXTRA_ON_TARGET",
                    target_canonical_id=tgt.canonical_id,
                )
            )
            continue

        assert src is not None and tgt is not None
        source_view = neutral_view(src, source_index)
        target_view = neutral_view(tgt, target_index)

        mismatches = [
            AttributeMismatch(
                attribute=attribute,
                source_value=source_view.get(attribute),
                target_value=target_view.get(attribute),
            )
            for attribute in sorted(set(source_view) | set(target_view))
            if attribute not in ignore_attributes
            and source_view.get(attribute) != target_view.get(attribute)
        ]

        results.append(
            EntityReconciliation(
                kind=kind,
                natural_key=natural_key,
                status="MATCHED" if not mismatches else "MISMATCHED",
                source_canonical_id=src.canonical_id,
                target_canonical_id=tgt.canonical_id,
                mismatches=mismatches,
            )
        )

    return ReconciliationReport(
        results=results, source_counts=_counts(source), target_counts=_counts(target)
    )
