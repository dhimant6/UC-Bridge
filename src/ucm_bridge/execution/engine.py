"""The execution engine (§4.5): durable, resumable, auditable migration runs.

The connector base class already enforces the per-write guardrails. This layer
adds what a *run* needs on top of them:

* **Durability.** Progress is checkpointed to a store after every operation, so
  a run interrupted at user 8,432 of 20,000 resumes at 8,433 rather than
  restarting or duplicating.
* **Auditability.** Every operation produces an audit record with before/after
  state and the dry-run flag, chained so tampering is detectable.
* **Control.** Pause, resume, and rollback are first-class states, not
  side effects of killing a process.

Temporal is the intended production driver (ADR-0002); the engine is written
against a ``RunStore`` interface so the Temporal workflow supplies durability
without the engine knowing about it. The in-process store here is what the tests
use and what a single-node air-gapped deployment can use.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ucm_bridge.audit.log import AuditAction, AuditLog
from ucm_bridge.canonical.base import utcnow
from ucm_bridge.connectors.base import Connector
from ucm_bridge.connectors.contracts import (
    ApplyAuthorization,
    ApplyPlan,
    ApplyReport,
    ExecutionMode,
    OperationResult,
    OperationStatus,
    RollbackBundle,
)
from ucm_bridge.connectors.errors import GuardrailViolation
from ucm_bridge.execution.store import RunRecord, RunStore


class RunState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_FAILURES = "COMPLETED_WITH_FAILURES"
    FAILED = "FAILED"
    ROLLING_BACK = "ROLLING_BACK"
    ROLLED_BACK = "ROLLED_BACK"


class RunSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    plan_id: str
    tenant_id: str
    connector_id: str
    mode: ExecutionMode
    state: RunState
    started_at: datetime
    finished_at: datetime | None = None
    total_operations: int = 0
    completed_operations: int = 0
    counts: dict[str, int] = Field(default_factory=dict)
    checkpoint_op_id: str | None = None
    rollback_bundle: RollbackBundle | None = None
    failure_reason: str | None = None

    @property
    def progress(self) -> float:
        if not self.total_operations:
            return 1.0
        return self.completed_operations / self.total_operations

    @property
    def succeeded(self) -> bool:
        return self.state in (RunState.COMPLETED, RunState.ROLLED_BACK)


class RunPaused(Exception):
    """Raised internally when a pause is requested mid-run. Not an error."""


class ExecutionEngine:
    """Drives one plan against one connector, durably."""

    def __init__(
        self,
        connector: Connector,
        *,
        store: RunStore,
        audit: AuditLog,
        tenant_id: str,
    ) -> None:
        self.connector = connector
        self.store = store
        self.audit = audit
        self.tenant_id = tenant_id

    # ------------------------------------------------------------------ #
    # Running
    # ------------------------------------------------------------------ #

    async def execute(
        self,
        plan: ApplyPlan,
        authorization: ApplyAuthorization,
        *,
        run_id: str,
        resume: bool = False,
    ) -> RunSummary:
        """Run a plan, resuming from the stored checkpoint when asked to."""
        existing = self.store.get(run_id)

        if resume and existing is not None:
            if existing.plan_id != plan.plan_id:
                # Resuming run X with a different plan is not a resume, it is a
                # new run wearing an old run's id. Refusing is the only safe
                # answer: the stored checkpoint refers to operations that do not
                # exist in this plan.
                raise ValueError(
                    f"Run {run_id!r} was started for plan {existing.plan_id!r} but is being "
                    f"resumed with plan {plan.plan_id!r}. Use a new run id."
                )
            if existing.state == RunState.COMPLETED.value:
                # Re-running a completed run is a no-op, not an error: an operator
                # retrying after a network blip should not be punished for it.
                return _summary_from(existing)
            checkpoint = existing.checkpoint_op_id
            self._record(
                AuditAction.RUN_RESUMED,
                authorization,
                run_id=run_id,
                plan_id=plan.plan_id,
                detail=f"Resuming after {checkpoint or 'the start'}",
            )
        else:
            checkpoint = None

        record = RunRecord(
            run_id=run_id,
            plan_id=plan.plan_id,
            tenant_id=self.tenant_id,
            connector_id=self.connector.connector_id,
            mode=authorization.mode.value,
            state=RunState.RUNNING.value,
            started_at=existing.started_at if existing else utcnow(),
            total_operations=len(plan.operations),
            checkpoint_op_id=checkpoint,
            completed_op_ids=list(existing.completed_op_ids) if existing and resume else [],
        )
        self.store.put(record)

        self._record(
            AuditAction.RUN_STARTED,
            authorization,
            run_id=run_id,
            plan_id=plan.plan_id,
            detail=(
                f"{authorization.mode.value} run of {len(plan.operations)} operation(s) "
                f"against {self.connector.connector_id}"
            ),
        )

        try:
            report = await self.connector.apply(
                plan, authorization, resume_after_op_id=checkpoint
            )
        except GuardrailViolation as violation:
            record = record.model_copy(
                update={
                    "state": RunState.FAILED.value,
                    "finished_at": utcnow(),
                    "failure_reason": str(violation),
                }
            )
            self.store.put(record)
            self._record(
                AuditAction.RUN_COMPLETED,
                authorization,
                run_id=run_id,
                plan_id=plan.plan_id,
                detail=f"Refused by a guardrail: {violation}",
            )
            raise

        self._audit_results(report, authorization, run_id=run_id, plan=plan)

        completed = [
            r.op_id
            for r in report.results
            if r.status
            in (
                OperationStatus.SUCCEEDED,
                OperationStatus.SKIPPED_NO_CHANGE,
                OperationStatus.PREVIEWED,
            )
        ]
        previously = list(record.completed_op_ids)
        all_completed = [*previously, *[c for c in completed if c not in previously]]

        failures = report.failures()
        state = (
            RunState.COMPLETED_WITH_FAILURES
            if failures
            else RunState.COMPLETED
        )

        record = record.model_copy(
            update={
                "state": state.value,
                "finished_at": utcnow(),
                "checkpoint_op_id": report.checkpoint_cursor or record.checkpoint_op_id,
                "completed_op_ids": all_completed,
                "counts": report.counts(),
                "rollback_bundle_json": (
                    report.rollback_bundle.model_dump_json()
                    if report.rollback_bundle
                    else None
                ),
            }
        )
        self.store.put(record)

        self._record(
            AuditAction.RUN_COMPLETED,
            authorization,
            run_id=run_id,
            plan_id=plan.plan_id,
            detail=f"{state.value}: {report.counts()}",
        )

        return RunSummary(
            run_id=run_id,
            plan_id=plan.plan_id,
            tenant_id=self.tenant_id,
            connector_id=self.connector.connector_id,
            mode=authorization.mode,
            state=state,
            started_at=record.started_at,
            finished_at=record.finished_at,
            total_operations=len(plan.operations),
            completed_operations=len(all_completed),
            counts=report.counts(),
            checkpoint_op_id=record.checkpoint_op_id,
            rollback_bundle=report.rollback_bundle,
        )

    # ------------------------------------------------------------------ #
    # Rollback
    # ------------------------------------------------------------------ #

    async def rollback(
        self,
        bundle: RollbackBundle,
        authorization: ApplyAuthorization,
        *,
        run_id: str,
    ) -> RunSummary:
        """Apply a rollback bundle, in the reverse dependency order it was built in."""
        started = utcnow()
        self._record(
            AuditAction.ROLLBACK_STARTED,
            authorization,
            run_id=run_id,
            plan_id=bundle.plan_id,
            detail=(
                f"{len(bundle.operations)} inverse operation(s)"
                + ("" if bundle.is_complete else f"; INCOMPLETE: {bundle.incomplete_reason}")
            ),
        )

        results: list[OperationResult] = []
        for operation in bundle.operations:
            try:
                result = await self.connector._execute_operation(operation)
            except Exception as exc:
                result = OperationResult(
                    op_id=operation.op_id,
                    status=OperationStatus.FAILED,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
            results.append(result)
            self.audit.append(
                tenant_id=self.tenant_id,
                actor=authorization.requested_by,
                action=AuditAction.OBJECT_WRITTEN,
                correlation_id=authorization.correlation_id,
                run_id=run_id,
                plan_id=bundle.plan_id,
                entity_kind=operation.entity_kind,
                canonical_id=operation.canonical_id,
                target_native_key=result.target_native_key,
                before=result.pre_state,
                after=None,
                detail=f"rollback: {operation.description or operation.op_id}",
                dry_run=authorization.mode is ExecutionMode.DRY_RUN,
            )

        failed = [r for r in results if r.status is OperationStatus.FAILED]
        state = RunState.ROLLED_BACK if not failed else RunState.COMPLETED_WITH_FAILURES

        self._record(
            AuditAction.ROLLBACK_COMPLETED,
            authorization,
            run_id=run_id,
            plan_id=bundle.plan_id,
            detail=f"{len(results) - len(failed)}/{len(results)} inverse operations applied",
        )

        counts: dict[str, int] = {}
        for result in results:
            counts[result.status.value] = counts.get(result.status.value, 0) + 1

        return RunSummary(
            run_id=run_id,
            plan_id=bundle.plan_id,
            tenant_id=self.tenant_id,
            connector_id=self.connector.connector_id,
            mode=authorization.mode,
            state=state,
            started_at=started,
            finished_at=utcnow(),
            total_operations=len(bundle.operations),
            completed_operations=len(results) - len(failed),
            counts=counts,
            failure_reason=(
                f"{len(failed)} inverse operation(s) failed; the target is in a partially "
                "rolled-back state and needs manual review."
                if failed
                else None
            ),
        )

    # ------------------------------------------------------------------ #
    # Control
    # ------------------------------------------------------------------ #

    def request_pause(self, run_id: str, *, by: str) -> None:
        record = self.store.get(run_id)
        if record is None:
            raise KeyError(f"No run {run_id!r}")
        self.store.put(record.model_copy(update={"pause_requested": True}))
        self.audit.append(
            tenant_id=self.tenant_id,
            actor=by,
            action=AuditAction.RUN_PAUSED,
            run_id=run_id,
            detail="Pause requested",
            dry_run=False,
        )

    # ------------------------------------------------------------------ #
    # Audit helpers
    # ------------------------------------------------------------------ #

    def _record(
        self,
        action: AuditAction,
        authorization: ApplyAuthorization,
        **fields: Any,
    ) -> None:
        self.audit.append(
            tenant_id=self.tenant_id,
            actor=authorization.requested_by,
            action=action,
            correlation_id=authorization.correlation_id,
            dry_run=authorization.mode is ExecutionMode.DRY_RUN,
            **fields,
        )

    def _audit_results(
        self,
        report: ApplyReport,
        authorization: ApplyAuthorization,
        *,
        run_id: str,
        plan: ApplyPlan,
    ) -> None:
        operations = {op.op_id: op for op in plan.operations}
        dry_run = authorization.mode is ExecutionMode.DRY_RUN

        for result in report.results:
            operation = operations.get(result.op_id)
            action = {
                OperationStatus.SUCCEEDED: AuditAction.OBJECT_WRITTEN,
                OperationStatus.SKIPPED_NO_CHANGE: AuditAction.OBJECT_SKIPPED,
                OperationStatus.PREVIEWED: AuditAction.OBJECT_SKIPPED,
            }.get(result.status, AuditAction.OBJECT_QUARANTINED)

            self.audit.append(
                tenant_id=self.tenant_id,
                actor=authorization.requested_by,
                action=action,
                correlation_id=authorization.correlation_id,
                run_id=run_id,
                plan_id=plan.plan_id,
                entity_kind=operation.entity_kind if operation else None,
                canonical_id=operation.canonical_id if operation else None,
                target_native_key=result.target_native_key,
                before=result.pre_state,
                after=result.post_state,
                detail=result.error_message or result.status.value,
                dry_run=dry_run,
            )


def _summary_from(record: RunRecord) -> RunSummary:
    return RunSummary(
        run_id=record.run_id,
        plan_id=record.plan_id,
        tenant_id=record.tenant_id,
        connector_id=record.connector_id,
        mode=ExecutionMode(record.mode),
        state=RunState(record.state),
        started_at=record.started_at,
        finished_at=record.finished_at,
        total_operations=record.total_operations,
        completed_operations=len(record.completed_op_ids),
        counts=dict(record.counts),
        checkpoint_op_id=record.checkpoint_op_id,
    )
