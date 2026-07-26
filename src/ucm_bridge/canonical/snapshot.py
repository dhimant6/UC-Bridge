"""The estate snapshot: a versioned, diffable, replayable canonical dump.

Every discovery run and every migration run persists one of these. It is the
unit of audit, the unit of replay, and the input to the diff view in the
Discovery Runs screen.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SerializeAsAny

from ucm_bridge.canonical.base import (
    CANONICAL_MODEL_VERSION,
    CanonicalEntity,
    FidelityLevel,
    Platform,
    digest_of,
    utcnow,
)
from ucm_bridge.canonical.registry import entity_adapter


class SnapshotKind(StrEnum):
    DISCOVERY = "DISCOVERY"
    """Read-only crawl of a source estate."""
    PRE_APPLY_TARGET = "PRE_APPLY_TARGET"
    """Target state captured before a write run, so rollback has something to aim at."""
    POST_APPLY_TARGET = "POST_APPLY_TARGET"
    TRANSFORMED = "TRANSFORMED"
    """Canonical output of the mapping engine, before any target write."""


class ChangeType(StrEnum):
    ADDED = "ADDED"
    REMOVED = "REMOVED"
    MODIFIED = "MODIFIED"


class EntityChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    change_type: ChangeType
    canonical_id: str
    kind: str
    display_name: str | None = None
    before_checksum: str | None = None
    after_checksum: str | None = None


class SnapshotDiff(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_snapshot_id: str
    to_snapshot_id: str
    changes: list[EntityChange] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """True when nothing changed. This is the idempotency proof (§9)."""
        return not self.changes

    def counts(self) -> dict[str, int]:
        return dict(Counter(c.change_type.value for c in self.changes))


class EstateSnapshot(BaseModel):
    """An immutable-by-convention capture of an estate in canonical form."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    snapshot_id: str
    tenant_id: str
    estate_id: str = Field(description="Which customer estate: a CUCM cluster, a Teams tenant.")
    snapshot_kind: SnapshotKind = SnapshotKind.DISCOVERY
    model_version: str = CANONICAL_MODEL_VERSION
    created_at: datetime = Field(default_factory=utcnow)

    platforms: list[Platform] = Field(default_factory=list)
    connector_versions: dict[str, str] = Field(
        default_factory=dict,
        description="connector_id -> version. A snapshot is only reproducible against the "
        "connector build that produced it.",
    )
    run_id: str | None = None
    read_only: bool = Field(
        default=True,
        description="Discovery snapshots are produced without writing to the source. Always "
        "true for SnapshotKind.DISCOVERY.",
    )

    # SerializeAsAny is load-bearing: without it Pydantic serialises by the declared
    # type and silently drops every subclass field, turning a full snapshot into a
    # list of bare base entities.
    entities: list[SerializeAsAny[CanonicalEntity]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    snapshot_digest: str | None = None

    # -- construction ------------------------------------------------------ #

    def seal(self) -> EstateSnapshot:
        """Seal every entity, then digest the whole snapshot."""
        for entity in self.entities:
            if entity.checksum is None:
                entity.seal()
        self.snapshot_digest = digest_of(
            sorted(f"{e.canonical_id}:{e.checksum}" for e in self.entities)
        )
        return self

    # -- access ------------------------------------------------------------ #

    def by_id(self) -> dict[str, CanonicalEntity]:
        return {e.canonical_id: e for e in self.entities}

    def of_kind(self, kind: str) -> list[CanonicalEntity]:
        return [e for e in self.entities if e.kind == kind]

    def counts_by_kind(self) -> dict[str, int]:
        return dict(sorted(Counter(e.kind for e in self.entities).items()))

    def counts_by_fidelity(self) -> dict[str, int]:
        return dict(sorted(Counter(e.fidelity.level.value for e in self.entities).items()))

    def fidelity_report(self) -> dict[str, dict[str, int]]:
        """Per-entity-kind fidelity breakdown: the core of §4.2's fidelity report."""
        report: dict[str, dict[str, int]] = {}
        for entity in self.entities:
            bucket = report.setdefault(
                entity.kind, {level.value: 0 for level in FidelityLevel}
            )
            bucket[entity.fidelity.level.value] += 1
        return dict(sorted(report.items()))

    def unassessed(self) -> list[CanonicalEntity]:
        return [e for e in self.entities if not e.fidelity.is_assessed]

    def manual_effort_minutes(self) -> int:
        return sum(e.fidelity.manual_effort_minutes or 0 for e in self.entities)

    def verify_checksums(self) -> list[str]:
        """canonical_ids whose content no longer matches their sealed checksum."""
        return [e.canonical_id for e in self.entities if not e.verify_checksum()]

    def __iter__(self) -> Iterator[CanonicalEntity]:  # type: ignore[override]
        return iter(self.entities)

    def __len__(self) -> int:
        return len(self.entities)

    # -- diffing ----------------------------------------------------------- #

    def diff(self, other: EstateSnapshot) -> SnapshotDiff:
        """Structural diff against a later snapshot, keyed on canonical_id."""
        mine = self.by_id()
        theirs = other.by_id()
        changes: list[EntityChange] = []

        for canonical_id in sorted(set(mine) | set(theirs)):
            before = mine.get(canonical_id)
            after = theirs.get(canonical_id)
            if before is None and after is not None:
                changes.append(
                    EntityChange(
                        change_type=ChangeType.ADDED,
                        canonical_id=canonical_id,
                        kind=after.kind,
                        display_name=after.display_name,
                        after_checksum=after.checksum or after.compute_checksum(),
                    )
                )
            elif after is None and before is not None:
                changes.append(
                    EntityChange(
                        change_type=ChangeType.REMOVED,
                        canonical_id=canonical_id,
                        kind=before.kind,
                        display_name=before.display_name,
                        before_checksum=before.checksum or before.compute_checksum(),
                    )
                )
            elif before is not None and after is not None:
                b = before.checksum or before.compute_checksum()
                a = after.checksum or after.compute_checksum()
                if a != b:
                    changes.append(
                        EntityChange(
                            change_type=ChangeType.MODIFIED,
                            canonical_id=canonical_id,
                            kind=after.kind,
                            display_name=after.display_name,
                            before_checksum=b,
                            after_checksum=a,
                        )
                    )
        return SnapshotDiff(
            from_snapshot_id=self.snapshot_id, to_snapshot_id=other.snapshot_id, changes=changes
        )

    def semantic_index(self) -> dict[str, str]:
        """canonical_id -> platform-neutral digest, for source/target reconciliation."""
        return {e.canonical_id: e.semantic_digest() for e in self.entities}

    # -- serialisation ----------------------------------------------------- #

    def to_json(self) -> str:
        return self.model_dump_json(indent=2)

    @classmethod
    def from_obj(cls, payload: dict[str, Any]) -> EstateSnapshot:
        """Rehydrate, resolving each entity to its concrete class via the discriminator."""
        adapter = entity_adapter()
        raw_entities: Sequence[Any] = payload.get("entities", [])
        rebuilt = [
            item if isinstance(item, CanonicalEntity) else adapter.validate_python(item)
            for item in raw_entities
        ]
        return cls.model_validate({**payload, "entities": rebuilt})

    @classmethod
    def build(
        cls,
        *,
        snapshot_id: str,
        tenant_id: str,
        estate_id: str,
        entities: Iterable[CanonicalEntity],
        snapshot_kind: SnapshotKind = SnapshotKind.DISCOVERY,
        **extra: Any,
    ) -> EstateSnapshot:
        snapshot = cls(
            snapshot_id=snapshot_id,
            tenant_id=tenant_id,
            estate_id=estate_id,
            snapshot_kind=snapshot_kind,
            entities=list(entities),
            **extra,
        )
        return snapshot.seal()
