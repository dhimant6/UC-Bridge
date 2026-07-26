"""Phase 3: durable execution, audit chain, and post-migration validation."""

from __future__ import annotations

import pytest
from tests.conftest import production_authorization

from ucm_bridge.audit import AuditAction, AuditLog, TamperDetected, evidence_pack
from ucm_bridge.canonical.numbering import E164Number, NumberAssignmentState
from ucm_bridge.connectors.contracts import ExecutionMode, ExtractRequest
from ucm_bridge.connectors.reference import MemoryPBXConnector
from ucm_bridge.execution import (
    ExecutionEngine,
    InMemoryRunStore,
    JsonFileRunStore,
    RunState,
)
from ucm_bridge.pipeline.planner import build_apply_plan
from ucm_bridge.validation import CheckOutcome, ValidationService, render_validation_markdown

KEY_FOR = MemoryPBXConnector.natural_key_for


def plan_for(entities, plan_id: str = "plan-exec"):
    return build_apply_plan(
        entities,
        plan_id=plan_id,
        tenant_id="contoso",
        estate_id="contoso-target",
        key_for=KEY_FOR,
    ).plan


@pytest.fixture
def audit() -> AuditLog:
    return AuditLog()


def engine_for(connector, audit, store=None) -> ExecutionEngine:
    return ExecutionEngine(
        connector, store=store or InMemoryRunStore(), audit=audit, tenant_id="contoso"
    )


async def source_snapshot(source_connector, extract_request):
    return await source_connector.extract_snapshot(extract_request)


# --------------------------------------------------------------------------- #
# Audit chain
# --------------------------------------------------------------------------- #


def test_audit_records_chain_together(audit: AuditLog) -> None:
    audit.append(tenant_id="contoso", actor="a@x", action=AuditAction.RUN_STARTED)
    audit.append(tenant_id="contoso", actor="a@x", action=AuditAction.OBJECT_WRITTEN)
    audit.append(tenant_id="contoso", actor="a@x", action=AuditAction.RUN_COMPLETED)

    audit.verify()
    assert len(audit) == 3
    assert audit.records()[1].previous_hash == audit.records()[0].record_hash


def test_editing_a_record_breaks_the_chain(audit: AuditLog) -> None:
    audit.append(tenant_id="contoso", actor="a@x", action=AuditAction.RUN_STARTED)
    audit.append(
        tenant_id="contoso", actor="a@x", action=AuditAction.OBJECT_WRITTEN, detail="original"
    )
    audit.append(tenant_id="contoso", actor="a@x", action=AuditAction.RUN_COMPLETED)

    tampered = audit.records()[1].model_copy(update={"detail": "rewritten"})
    audit._records[1] = tampered

    with pytest.raises(TamperDetected, match="modified after it was written"):
        audit.verify()


def test_removing_a_record_breaks_the_chain(audit: AuditLog) -> None:
    for _ in range(3):
        audit.append(tenant_id="contoso", actor="a@x", action=AuditAction.OBJECT_WRITTEN)
    del audit._records[1]

    with pytest.raises(TamperDetected):
        audit.verify()


def test_audit_records_survive_a_restart(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    first = AuditLog(path=path)
    first.append(tenant_id="contoso", actor="a@x", action=AuditAction.RUN_STARTED)
    first.append(tenant_id="contoso", actor="a@x", action=AuditAction.RUN_COMPLETED)

    reopened = AuditLog(path=path)
    assert len(reopened) == 2
    reopened.verify()


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #


async def test_every_write_appears_in_the_audit_log_with_before_and_after(
    source_connector, target_connector, extract_request, audit
) -> None:
    snapshot = await source_snapshot(source_connector, extract_request)
    plan = plan_for(snapshot.entities)
    receipt = await target_connector.dry_run(plan)
    authorization = production_authorization(plan, receipt)

    summary = await engine_for(target_connector, audit).execute(
        plan, authorization, run_id="run-1"
    )

    assert summary.state is RunState.COMPLETED
    writes = audit.search(action=AuditAction.OBJECT_WRITTEN, run_id="run-1")
    assert len(writes) == len(plan.operations)
    assert all(not w.dry_run for w in writes)
    assert all(w.canonical_id for w in writes)
    audit.verify()


async def test_dry_runs_are_marked_as_such_in_the_audit_log(
    source_connector, target_connector, extract_request, audit
) -> None:
    from ucm_bridge.connectors.contracts import ApplyAuthorization

    snapshot = await source_snapshot(source_connector, extract_request)
    plan = plan_for(snapshot.entities)

    await engine_for(target_connector, audit).execute(
        plan,
        ApplyAuthorization(
            mode=ExecutionMode.DRY_RUN, requested_by="planner@x", correlation_id="c"
        ),
        run_id="run-dry",
    )
    records = audit.search(run_id="run-dry")
    assert records
    assert all(r.dry_run for r in records)
    assert audit.real_changes() == []


async def test_a_run_resumes_from_its_checkpoint(
    source_connector, target_connector, target_estate, extract_request, audit
) -> None:
    """Simulates a process that died after three operations, then restarts."""
    from ucm_bridge.execution.store import RunRecord

    snapshot = await source_snapshot(source_connector, extract_request)
    plan = plan_for(snapshot.entities)
    receipt = await target_connector.dry_run(plan)
    ordered = plan.operations_in_dependency_order()
    store = InMemoryRunStore()

    # Three operations landed on the target, then the process was killed before
    # the run could finish.
    for operation in ordered[:3]:
        await target_connector._execute_operation(operation)
    assert target_estate.total_records() == 3

    store.put(
        RunRecord(
            run_id="run-resume",
            plan_id=plan.plan_id,
            tenant_id="contoso",
            connector_id=target_connector.connector_id,
            mode=ExecutionMode.PRODUCTION.value,
            state=RunState.RUNNING.value,
            total_operations=len(ordered),
            checkpoint_op_id=ordered[2].op_id,
            completed_op_ids=[op.op_id for op in ordered[:3]],
        )
    )

    resumed = await engine_for(target_connector, audit, store).execute(
        plan, production_authorization(plan, receipt), run_id="run-resume", resume=True
    )

    assert resumed.state is RunState.COMPLETED
    assert target_estate.total_records() == len(ordered)
    assert resumed.completed_operations == len(ordered)
    # The already-completed operations were not re-audited on resume.
    assert len(audit.search(action=AuditAction.OBJECT_WRITTEN)) == len(ordered) - 3


async def test_resuming_a_run_with_a_different_plan_is_refused(
    source_connector, target_connector, extract_request, audit
) -> None:
    """A stored checkpoint refers to operations that must exist in the plan."""
    from ucm_bridge.execution.store import RunRecord

    snapshot = await source_snapshot(source_connector, extract_request)
    plan = plan_for(snapshot.entities, plan_id="plan-a")
    receipt = await target_connector.dry_run(plan)
    store = InMemoryRunStore()
    store.put(
        RunRecord(
            run_id="run-x",
            plan_id="plan-b",
            tenant_id="contoso",
            connector_id=target_connector.connector_id,
            mode=ExecutionMode.PRODUCTION.value,
            state=RunState.RUNNING.value,
        )
    )
    with pytest.raises(ValueError, match="Use a new run id"):
        await engine_for(target_connector, audit, store).execute(
            plan, production_authorization(plan, receipt), run_id="run-x", resume=True
        )


async def test_re_executing_a_completed_run_is_a_no_op(
    source_connector, target_connector, extract_request, audit
) -> None:
    snapshot = await source_snapshot(source_connector, extract_request)
    plan = plan_for(snapshot.entities)
    receipt = await target_connector.dry_run(plan)
    store = InMemoryRunStore()
    engine = engine_for(target_connector, audit, store)

    first = await engine.execute(
        plan, production_authorization(plan, receipt), run_id="run-once"
    )
    audit_count = len(audit)

    second = await engine.execute(
        plan, production_authorization(plan, receipt), run_id="run-once", resume=True
    )
    assert first.state is second.state
    assert len(audit) == audit_count, "a completed run must not re-audit"


async def test_a_guardrail_refusal_is_recorded_and_leaves_the_run_failed(
    source_connector, target_connector, extract_request, audit
) -> None:
    from ucm_bridge.connectors.errors import EmergencyConfirmationRequired

    snapshot = await source_snapshot(source_connector, extract_request)
    plan = plan_for(snapshot.entities)
    receipt = await target_connector.dry_run(plan)
    store = InMemoryRunStore()

    with pytest.raises(EmergencyConfirmationRequired):
        await engine_for(target_connector, audit, store).execute(
            plan,
            production_authorization(plan, receipt, sites=["MUC-HQ"]),  # LON-BR unconfirmed
            run_id="run-refused",
        )

    record = store.get("run-refused")
    assert record is not None
    assert record.state == RunState.FAILED.value
    assert "LON-BR" in (record.failure_reason or "")


async def test_rollback_restores_the_target_and_is_audited(
    source_connector, target_connector, target_estate, extract_request, audit
) -> None:
    snapshot = await source_snapshot(source_connector, extract_request)
    plan = plan_for(snapshot.entities)
    receipt = await target_connector.dry_run(plan)
    authorization = production_authorization(plan, receipt)
    engine = engine_for(target_connector, audit)

    summary = await engine.execute(plan, authorization, run_id="run-rb")
    assert target_estate.total_records() > 0
    assert summary.rollback_bundle is not None

    rolled = await engine.rollback(summary.rollback_bundle, authorization, run_id="run-rb")
    assert rolled.state is RunState.ROLLED_BACK
    assert target_estate.total_records() == 0
    assert audit.search(action=AuditAction.ROLLBACK_COMPLETED)
    audit.verify()


async def test_checkpoints_survive_a_process_restart(
    source_connector, target_connector, extract_request, audit, tmp_path
) -> None:
    snapshot = await source_snapshot(source_connector, extract_request)
    plan = plan_for(snapshot.entities)
    receipt = await target_connector.dry_run(plan)

    store = JsonFileRunStore(tmp_path / "runs")
    await engine_for(target_connector, audit, store).execute(
        plan, production_authorization(plan, receipt), run_id="run-durable"
    )

    reopened = JsonFileRunStore(tmp_path / "runs")
    record = reopened.get("run-durable")
    assert record is not None
    assert record.state == RunState.COMPLETED.value
    assert len(record.completed_op_ids) == len(plan.operations)


async def test_evidence_pack_contains_the_approvals_and_confirmations(
    source_connector, target_connector, extract_request, audit
) -> None:
    snapshot = await source_snapshot(source_connector, extract_request)
    plan = plan_for(snapshot.entities)
    receipt = await target_connector.dry_run(plan)
    authorization = production_authorization(plan, receipt)

    engine = engine_for(target_connector, audit)
    for approval in authorization.approvals:
        audit.append(
            tenant_id="contoso",
            actor=approval.approver,
            action=AuditAction.APPROVAL_GRANTED,
            run_id="run-pack",
            plan_id=plan.plan_id,
            dry_run=False,
        )
    for confirmation in authorization.emergency_confirmations:
        audit.append(
            tenant_id="contoso",
            actor=confirmation.confirmed_by,
            action=AuditAction.EMERGENCY_CONFIRMED,
            run_id="run-pack",
            detail=f"site {confirmation.site_code}",
            dry_run=False,
        )
    await engine.execute(plan, authorization, run_id="run-pack")

    pack = evidence_pack(audit, run_id="run-pack")
    assert pack["chain_verified"] is True
    assert len(pack["approvals"]) == 2
    assert len(pack["emergency_confirmations"]) == 2
    assert pack["real_change_count"] > 0


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


async def migrate(source_connector, target_connector, extract_request, audit):
    snapshot = await source_snapshot(source_connector, extract_request)
    plan = plan_for(snapshot.entities)
    receipt = await target_connector.dry_run(plan)
    await engine_for(target_connector, audit).execute(
        plan, production_authorization(plan, receipt), run_id="run-val"
    )
    target = await target_connector.extract_snapshot(
        ExtractRequest(run_id="verify", tenant_id="contoso", estate_id="contoso-target")
    )
    return snapshot, target


async def test_validation_passes_after_a_clean_migration(
    source_connector, target_connector, extract_request, audit
) -> None:
    source, target = await migrate(source_connector, target_connector, extract_request, audit)

    report = await ValidationService().validate(
        run_id="run-val",
        tenant_id="contoso",
        source=source,
        target=target,
        source_key_for=KEY_FOR,
        target_key_for=KEY_FOR,
    )
    assert report.safe_to_sign_off
    assert not report.failures, [f.detail for f in report.failures]


async def test_a_missing_emergency_location_is_a_hard_fail_not_a_warning(
    source_connector, target_connector, extract_request, audit
) -> None:
    source, target = await migrate(source_connector, target_connector, extract_request, audit)

    # Strip the emergency location from one assigned number.
    number = next(
        e
        for e in target.entities
        if isinstance(e, E164Number)
        and e.assignment_state is NumberAssignmentState.ASSIGNED
    )
    number.emergency_location_ref = None

    report = await ValidationService().validate(
        run_id="run-val",
        tenant_id="contoso",
        source=source,
        target=target,
        source_key_for=KEY_FOR,
        target_key_for=KEY_FOR,
    )
    check = next(c for c in report.checks if c.check_id == "VAL-003")
    assert check.outcome is CheckOutcome.HARD_FAIL
    assert not report.safe_to_sign_off
    assert "cannot be located" in check.detail


async def test_test_calls_report_skipped_rather_than_passed_when_no_probe_exists(
    source_connector, target_connector, extract_request, audit
) -> None:
    source, target = await migrate(source_connector, target_connector, extract_request, audit)
    report = await ValidationService().validate(
        run_id="run-val",
        tenant_id="contoso",
        source=source,
        target=target,
        source_key_for=KEY_FOR,
        target_key_for=KEY_FOR,
    )
    check = next(c for c in report.checks if c.check_id == "VAL-008")
    assert check.outcome is CheckOutcome.SKIPPED
    assert "has not been verified" in check.detail


async def test_a_failing_test_call_probe_is_reported(
    source_connector, target_connector, extract_request, audit
) -> None:
    source, target = await migrate(source_connector, target_connector, extract_request, audit)

    async def probe(e164: str) -> bool:
        return not e164.endswith("101")

    report = await ValidationService(test_call_probe=probe).validate(
        run_id="run-val",
        tenant_id="contoso",
        source=source,
        target=target,
        source_key_for=KEY_FOR,
        target_key_for=KEY_FOR,
    )
    check = next(c for c in report.checks if c.check_id == "VAL-008")
    assert check.outcome is CheckOutcome.FAIL
    assert check.actual is not None and check.expected is not None
    assert check.actual < check.expected


async def test_validation_renders_to_markdown(
    source_connector, target_connector, extract_request, audit
) -> None:
    source, target = await migrate(source_connector, target_connector, extract_request, audit)
    report = await ValidationService().validate(
        run_id="run-val",
        tenant_id="contoso",
        source=source,
        target=target,
        source_key_for=KEY_FOR,
        target_key_for=KEY_FOR,
    )
    markdown = render_validation_markdown(report)
    assert "# Validation report: run run-val" in markdown
    assert "Reconciliation" in markdown
