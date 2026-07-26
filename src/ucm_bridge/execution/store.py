"""Durable run state: the checkpoint that makes a run resumable.

Deliberately a narrow interface. The engine only needs to put and get a
``RunRecord``, which means the same engine code runs against an in-memory store
in tests, a JSON file on an air-gapped collector, Postgres in the control plane,
and Temporal's own durable state in production — without the engine knowing
which.

The checkpoint discipline that matters: a record is written **after** each
operation completes, never before. A crash between the write and the checkpoint
re-runs one operation, which idempotency makes harmless. A crash the other way
round would skip an operation silently, which it would not.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ucm_bridge.canonical.base import utcnow


class RunRecord(BaseModel):
    """Persisted state of one migration run."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    plan_id: str
    tenant_id: str
    connector_id: str
    mode: str
    state: str
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None

    total_operations: int = 0
    completed_op_ids: list[str] = Field(default_factory=list)
    checkpoint_op_id: str | None = Field(
        default=None,
        description="Last operation known to be durably complete. Resumption starts after it.",
    )
    counts: dict[str, int] = Field(default_factory=dict)
    rollback_bundle_json: str | None = None
    pause_requested: bool = False
    failure_reason: str | None = None


class RunStore(ABC):
    """Where run checkpoints live."""

    @abstractmethod
    def put(self, record: RunRecord) -> None: ...

    @abstractmethod
    def get(self, run_id: str) -> RunRecord | None: ...

    @abstractmethod
    def list_runs(self, *, tenant_id: str | None = None) -> list[RunRecord]: ...


class InMemoryRunStore(RunStore):
    """For tests and single-process use. State dies with the process, by design."""

    def __init__(self) -> None:
        self._records: dict[str, RunRecord] = {}

    def put(self, record: RunRecord) -> None:
        self._records[record.run_id] = record

    def get(self, run_id: str) -> RunRecord | None:
        return self._records.get(run_id)

    def list_runs(self, *, tenant_id: str | None = None) -> list[RunRecord]:
        records = list(self._records.values())
        if tenant_id is not None:
            records = [r for r in records if r.tenant_id == tenant_id]
        return sorted(records, key=lambda r: r.started_at)


class JsonFileRunStore(RunStore):
    """One JSON file per run. Enough for an air-gapped collector agent.

    Writes are atomic via a temporary file and replace, so a crash mid-write
    leaves the previous checkpoint intact rather than a truncated file. A
    half-written checkpoint is worse than a stale one: it would make a resumable
    run unresumable.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, run_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in run_id)
        return self.directory / f"{safe}.json"

    def put(self, record: RunRecord) -> None:
        path = self._path(record.run_id)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(record.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(path)

    def get(self, run_id: str) -> RunRecord | None:
        path = self._path(run_id)
        if not path.is_file():
            return None
        return RunRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def list_runs(self, *, tenant_id: str | None = None) -> list[RunRecord]:
        records: list[RunRecord] = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                record = RunRecord.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if tenant_id is None or record.tenant_id == tenant_id:
                records.append(record)
        return sorted(records, key=lambda r: r.started_at)


class TemporalRunStore(RunStore):
    """Reads run state from Temporal workflow history.

    Unimplemented on purpose. Under Temporal the workflow *is* the durable
    state, so this adapter exists to expose it to the Run Console rather than to
    duplicate it. Writing it against a guessed workflow shape would produce a
    reader for a workflow nobody has written yet.
    """

    def __init__(self, *, namespace: str, task_queue: str) -> None:
        self.namespace = namespace
        self.task_queue = task_queue

    def put(self, record: RunRecord) -> None:
        raise NotImplementedError(
            "Under Temporal the workflow owns run state; the engine does not write "
            "checkpoints separately. Implement this once the workflow contract exists."
        )

    def get(self, run_id: str) -> RunRecord | None:
        raise NotImplementedError(
            "Implement against the Temporal workflow query API once the workflow exists."
        )

    def list_runs(self, *, tenant_id: str | None = None) -> list[RunRecord]:
        raise NotImplementedError(
            "Implement against the Temporal visibility API once the workflow exists."
        )


def export_runs(store: RunStore, *, tenant_id: str | None = None) -> str:
    """Run history as JSON, for the Run Console and the evidence pack."""
    return json.dumps(
        [r.model_dump(mode="json") for r in store.list_runs(tenant_id=tenant_id)],
        indent=2,
    )
