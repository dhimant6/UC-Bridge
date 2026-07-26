"""Immutable, append-only audit log (§4.7).

Every write the platform performs is recorded with who, what, when, which
object, before/after, correlation id, and the dry-run flag.

"Immutable" is enforced rather than asserted: each record carries the hash of
its predecessor, so the log is a chain. Deleting or editing a record breaks the
chain from that point on, and :meth:`AuditLog.verify` finds it. This does not
stop an attacker with database access from rewriting the whole chain, and it is
not claimed to — it makes silent, partial tampering detectable, which is what a
compliance reviewer actually needs.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ucm_bridge.canonical.base import digest_of, utcnow

GENESIS_HASH = "sha256:" + "0" * 64


class AuditAction(StrEnum):
    DISCOVERY_STARTED = "DISCOVERY_STARTED"
    DISCOVERY_COMPLETED = "DISCOVERY_COMPLETED"
    ASSESSMENT_RUN = "ASSESSMENT_RUN"
    PROFILE_APPLIED = "PROFILE_APPLIED"
    MAPPING_OVERRIDDEN = "MAPPING_OVERRIDDEN"
    PLAN_CREATED = "PLAN_CREATED"
    DRY_RUN_COMPLETED = "DRY_RUN_COMPLETED"
    APPROVAL_GRANTED = "APPROVAL_GRANTED"
    EMERGENCY_CONFIRMED = "EMERGENCY_CONFIRMED"
    RUN_STARTED = "RUN_STARTED"
    OBJECT_WRITTEN = "OBJECT_WRITTEN"
    OBJECT_SKIPPED = "OBJECT_SKIPPED"
    OBJECT_QUARANTINED = "OBJECT_QUARANTINED"
    RUN_PAUSED = "RUN_PAUSED"
    RUN_RESUMED = "RUN_RESUMED"
    RUN_COMPLETED = "RUN_COMPLETED"
    ROLLBACK_STARTED = "ROLLBACK_STARTED"
    ROLLBACK_COMPLETED = "ROLLBACK_COMPLETED"
    RAW_SQL_EXECUTED = "RAW_SQL_EXECUTED"
    VALIDATION_RUN = "VALIDATION_RUN"
    CHANGE_WINDOW_OVERRIDDEN = "CHANGE_WINDOW_OVERRIDDEN"
    FINDING_WAIVED = "FINDING_WAIVED"


class AuditRecord(BaseModel):
    """One immutable entry. Never updated after append."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int = Field(ge=0)
    at: datetime = Field(default_factory=utcnow)
    tenant_id: str
    actor: str = Field(description="User principal, or 'system:<component>'.")
    action: AuditAction
    correlation_id: str | None = None
    run_id: str | None = None
    plan_id: str | None = None

    entity_kind: str | None = None
    canonical_id: str | None = None
    target_native_key: str | None = None

    before: Any = None
    after: Any = None
    detail: str | None = None

    dry_run: bool = Field(
        default=True,
        description="Whether this record describes a simulated or a real change. The most "
        "important field in the log for anyone auditing what actually happened.",
    )
    raw_sql_used: bool = False

    previous_hash: str = GENESIS_HASH
    record_hash: str = ""

    def content(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"record_hash"})

    def compute_hash(self) -> str:
        return digest_of(self.content())


class TamperDetected(Exception):
    """The audit chain does not verify."""


class AuditLog:
    """An append-only, hash-chained log.

    In-memory plus optional JSON-lines persistence. Production would put this in
    a partitioned append-only Postgres table with no UPDATE or DELETE grant on
    the application role; the chain is defence in depth behind that, not a
    replacement for it.
    """

    def __init__(self, *, path: Path | None = None) -> None:
        self._records: list[AuditRecord] = []
        self._path = path
        if path is not None and path.is_file():
            self._records = [
                AuditRecord.model_validate_json(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

    def __len__(self) -> int:
        return len(self._records)

    def __iter__(self) -> Iterator[AuditRecord]:
        return iter(self._records)

    @property
    def head_hash(self) -> str:
        return self._records[-1].record_hash if self._records else GENESIS_HASH

    def append(self, **fields: Any) -> AuditRecord:
        draft = AuditRecord(
            sequence=len(self._records),
            previous_hash=self.head_hash,
            **fields,
        )
        record = draft.model_copy(update={"record_hash": draft.compute_hash()})
        self._records.append(record)
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(record.model_dump_json() + "\n")
        return record

    def verify(self) -> None:
        """Raise if the chain has been broken. Silence means it verifies."""
        expected_previous = GENESIS_HASH
        for index, record in enumerate(self._records):
            if record.sequence != index:
                raise TamperDetected(
                    f"Record at position {index} claims sequence {record.sequence}; "
                    "a record has been removed or reordered."
                )
            if record.previous_hash != expected_previous:
                raise TamperDetected(
                    f"Record {record.sequence} chains to {record.previous_hash}, but the "
                    f"previous record hashes to {expected_previous}."
                )
            if record.record_hash != record.compute_hash():
                raise TamperDetected(
                    f"Record {record.sequence} has been modified after it was written."
                )
            expected_previous = record.record_hash

    # -- queries used by the Audit Explorer screen ----------------------- #

    def records(self) -> list[AuditRecord]:
        return list(self._records)

    def search(
        self,
        *,
        tenant_id: str | None = None,
        actor: str | None = None,
        action: AuditAction | None = None,
        canonical_id: str | None = None,
        correlation_id: str | None = None,
        run_id: str | None = None,
        include_dry_runs: bool = True,
        since: datetime | None = None,
    ) -> list[AuditRecord]:
        def keep(record: AuditRecord) -> bool:
            if tenant_id and record.tenant_id != tenant_id:
                return False
            if actor and record.actor != actor:
                return False
            if action and record.action is not action:
                return False
            if canonical_id and record.canonical_id != canonical_id:
                return False
            if correlation_id and record.correlation_id != correlation_id:
                return False
            if run_id and record.run_id != run_id:
                return False
            if not include_dry_runs and record.dry_run:
                return False
            return not (since and record.at < since)

        return [r for r in self._records if keep(r)]

    def real_changes(self, *, tenant_id: str | None = None) -> list[AuditRecord]:
        """Only records describing changes that actually happened."""
        return [
            r
            for r in self.search(tenant_id=tenant_id, include_dry_runs=False)
            if r.action
            in (
                AuditAction.OBJECT_WRITTEN,
                AuditAction.ROLLBACK_COMPLETED,
                AuditAction.RAW_SQL_EXECUTED,
            )
        ]

    def history_of(self, canonical_id: str) -> list[AuditRecord]:
        return self.search(canonical_id=canonical_id)


def evidence_pack(log: AuditLog, *, run_id: str) -> dict[str, Any]:
    """Exportable evidence for a change-advisory board or compliance sign-off."""
    records = log.search(run_id=run_id)
    real = [r for r in records if not r.dry_run]
    return {
        "run_id": run_id,
        "generated_at": utcnow().isoformat(),
        "record_count": len(records),
        "real_change_count": len(real),
        "chain_head": log.head_hash,
        "chain_verified": _verifies(log),
        "approvals": [
            r.model_dump(mode="json")
            for r in records
            if r.action is AuditAction.APPROVAL_GRANTED
        ],
        "emergency_confirmations": [
            r.model_dump(mode="json")
            for r in records
            if r.action is AuditAction.EMERGENCY_CONFIRMED
        ],
        "change_window_overrides": [
            r.model_dump(mode="json")
            for r in records
            if r.action is AuditAction.CHANGE_WINDOW_OVERRIDDEN
        ],
        "raw_sql_uses": [
            r.model_dump(mode="json") for r in records if r.raw_sql_used
        ],
        "records": [r.model_dump(mode="json") for r in records],
    }


def _verifies(log: AuditLog) -> bool:
    try:
        log.verify()
    except TamperDetected:
        return False
    return True


def append_many(log: AuditLog, entries: Iterable[dict[str, Any]]) -> list[AuditRecord]:
    return [log.append(**entry) for entry in entries]
