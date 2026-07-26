"""The Phase 0 proof.

A User + Line + E164Number + EmergencyLocation survive the full pipeline:

    source estate -> Extract -> canonical -> plan -> dry run -> Apply
                  -> target estate -> Extract -> reconcile against source

and the properties the acceptance criteria demand hold along the way: zero
writes to the source, a mandatory dry run, an idempotent re-run, resumability,
and a rollback bundle that actually restores the target.
"""

from __future__ import annotations

import pytest
from tests.conftest import production_authorization

from ucm_bridge.canonical import FidelityLevel
from ucm_bridge.canonical.endpoints import Line
from ucm_bridge.canonical.identity import User
from ucm_bridge.canonical.numbering import E164Number
from ucm_bridge.canonical.policy import EmergencyLocation
from ucm_bridge.connectors.contracts import ExecutionMode, ExtractRequest, OperationStatus
from ucm_bridge.connectors.reference import MemoryPBXConnector
from ucm_bridge.pipeline.planner import build_apply_plan
from ucm_bridge.pipeline.reconcile import reconcile

KEY_FOR = MemoryPBXConnector.natural_key_for


async def extract_all(connector: MemoryPBXConnector, request: ExtractRequest):
    return await connector.extract_snapshot(request)


def plan_for(entities, *, plan_id: str = "plan-0001"):
    result = build_apply_plan(
        entities,
        plan_id=plan_id,
        tenant_id="contoso",
        estate_id="contoso-target",
        key_for=KEY_FOR,
    )
    assert result.is_fully_resolved, f"unresolved references: {result.unresolved_references}"
    return result.plan


# --------------------------------------------------------------------------- #
# Extract
# --------------------------------------------------------------------------- #


async def test_extract_produces_all_four_entity_kinds(source_connector, extract_request) -> None:
    snapshot = await extract_all(source_connector, extract_request)
    counts = snapshot.counts_by_kind()

    assert counts == {"E164Number": 5, "EmergencyLocation": 2, "Line": 5, "User": 4}
    assert snapshot.verify_checksums() == []
    assert snapshot.read_only is True


async def test_discovery_never_writes_to_the_source(
    source_connector, source_estate, extract_request
) -> None:
    before = source_estate.state_fingerprint()

    await extract_all(source_connector, extract_request)
    await extract_all(source_connector, extract_request)  # safely re-runnable

    assert source_estate.write_count == 0
    assert source_estate.state_fingerprint() == before


async def test_extract_is_deterministic(source_connector, extract_request) -> None:
    first = await extract_all(source_connector, extract_request)
    second = await extract_all(source_connector, extract_request)
    assert first.snapshot_digest == second.snapshot_digest
    assert first.diff(second).is_empty


async def test_extract_streams_in_pages(source_connector, extract_request) -> None:
    batches = [batch async for batch in source_connector.extract(extract_request)]
    assert len(batches) > 1, "page_size=3 over 16 entities must produce multiple batches"
    assert batches[-1].is_final
    assert all(b.cursor is not None for b in batches[:-1])


async def test_extract_flags_the_user_with_no_external_number(
    source_connector, extract_request
) -> None:
    snapshot = await extract_all(source_connector, extract_request)
    warehouse = next(
        e
        for e in snapshot.entities
        if isinstance(e, User) and e.user_principal_name == "warehouse"
    )
    assert warehouse.primary_number_ref is None
    assert any("no external number" in w for w in snapshot.warnings)
    assert any(
        d.attribute == "primary_number_ref" for d in warehouse.fidelity.degraded_attributes
    )


# --------------------------------------------------------------------------- #
# Fidelity honesty
# --------------------------------------------------------------------------- #


async def test_fidelity_report_declares_what_is_lost(source_connector, extract_request) -> None:
    snapshot = await extract_all(source_connector, extract_request)
    report = snapshot.fidelity_report()

    # Numbers carry across intact; users never do, because calling permission does not.
    assert report["E164Number"][FidelityLevel.LOSSLESS.value] == 5
    assert report["User"][FidelityLevel.DEGRADED.value] == 4

    shared = next(
        e for e in snapshot.entities if isinstance(e, Line) and e.directory_number == "5199"
    )
    assert shared.fidelity.level is FidelityLevel.DEGRADED
    loss = next(
        d for d in shared.fidelity.degraded_attributes if d.attribute == "shared_appearance_ref"
    )
    assert "must be rebuilt by hand" in loss.target_behaviour

    # Unvalidated London address is called out rather than assumed fine.
    london = next(
        e
        for e in snapshot.entities
        if isinstance(e, EmergencyLocation) and e.site_code == "LON-BR"
    )
    assert london.fidelity.level is FidelityLevel.DEGRADED
    assert {d.attribute for d in london.fidelity.degraded_attributes} == {
        "is_validated",
        "civic_address.sub_unit",
    }

    assert snapshot.manual_effort_minutes() > 0
    assert snapshot.unassessed() == []


# --------------------------------------------------------------------------- #
# Dry run
# --------------------------------------------------------------------------- #


async def test_dry_run_writes_nothing_and_describes_every_call(
    source_connector, target_connector, target_estate, extract_request
) -> None:
    snapshot = await extract_all(source_connector, extract_request)
    plan = plan_for(snapshot.entities)

    receipt = await target_connector.dry_run(plan)

    assert target_estate.write_count == 0
    assert target_estate.total_records() == 0
    assert receipt.would_change_count == len(plan.operations)
    assert all(p.api_call.startswith("MemoryPBX:upsert(") for p in receipt.previews)
    assert all(p.current_target_state is None for p in receipt.previews)
    assert any("without a validation authority" in w for w in receipt.warnings)


async def test_dry_run_is_the_default_mode(source_connector, target_connector, extract_request):
    snapshot = await extract_all(source_connector, extract_request)
    plan = plan_for(snapshot.entities)

    from ucm_bridge.connectors.contracts import ApplyAuthorization

    default = ApplyAuthorization(requested_by="operator", correlation_id="c1")
    assert default.mode is ExecutionMode.DRY_RUN

    report = await target_connector.apply(plan, default)
    assert report.mode is ExecutionMode.DRY_RUN
    assert all(r.status is OperationStatus.PREVIEWED for r in report.results)


# --------------------------------------------------------------------------- #
# The round trip
# --------------------------------------------------------------------------- #


async def test_full_round_trip_reconciles(
    source_connector, source_estate, target_connector, target_estate, extract_request
) -> None:
    source_snapshot = await extract_all(source_connector, extract_request)
    plan = plan_for(source_snapshot.entities)

    receipt = await target_connector.dry_run(plan)
    authorization = production_authorization(plan, receipt)
    report = await target_connector.apply(plan, authorization)

    assert report.failures() == []
    assert report.changed_count == len(plan.operations)

    target_snapshot = await target_connector.extract_snapshot(
        ExtractRequest(
            run_id="run-verify",
            tenant_id="contoso",
            estate_id="contoso-target",
            page_size=100,
        )
    )

    result = reconcile(
        source_snapshot.entities,
        target_snapshot.entities,
        source_key_for=KEY_FOR,
        target_key_for=KEY_FOR,
    )
    assert result.passed, result.failures()
    assert result.source_counts == result.target_counts

    # And the source was never touched.
    assert source_estate.write_count == 0


async def test_round_trip_preserves_the_four_required_entities(
    source_connector, target_connector, extract_request
) -> None:
    source_snapshot = await extract_all(source_connector, extract_request)
    plan = plan_for(source_snapshot.entities)
    receipt = await target_connector.dry_run(plan)
    await target_connector.apply(plan, production_authorization(plan, receipt))

    target_snapshot = await target_connector.extract_snapshot(
        ExtractRequest(run_id="v", tenant_id="contoso", estate_id="contoso-target")
    )
    by_kind = {e.kind: e for e in target_snapshot.entities}

    user = next(
        e
        for e in target_snapshot.entities
        if isinstance(e, User) and e.user_principal_name == "amueller"
    )
    assert user.email == "anna.mueller@contoso.example"
    assert user.site_code == "MUC-HQ"
    assert user.telephony_enabled

    number = next(
        e
        for e in target_snapshot.entities
        if isinstance(e, E164Number) and e.e164 == "+498912345101"
    )
    assert number.assigned_to_ref == user.canonical_id, "reference resolved back to the same user"

    line = next(
        e for e in target_snapshot.entities if isinstance(e, Line) and e.directory_number == "5101"
    )
    assert line.owner_ref == user.canonical_id
    assert line.e164_ref == number.canonical_id

    location = next(
        e
        for e in target_snapshot.entities
        if isinstance(e, EmergencyLocation) and e.site_code == "MUC-HQ"
    )
    assert location.civic_address.is_dispatchable
    assert location.is_validated
    assert location.validation_authority == "Deutsche Telekom"

    assert set(by_kind) == {"User", "Line", "E164Number", "EmergencyLocation"}


async def test_reconciliation_passes_while_fidelity_still_reports_the_shared_line_loss(
    source_connector, target_connector, extract_request
) -> None:
    """Reconciliation and fidelity answer different questions, and both are needed.

    Every canonical object arrives intact, so reconciliation passes. The shared
    appearance on extension 5199 was still lost, and only the fidelity report
    says so. A platform that reported just the first number would be lying by
    omission.
    """
    source_snapshot = await extract_all(source_connector, extract_request)
    plan = plan_for(source_snapshot.entities)
    receipt = await target_connector.dry_run(plan)
    await target_connector.apply(plan, production_authorization(plan, receipt))

    target_snapshot = await target_connector.extract_snapshot(
        ExtractRequest(run_id="v", tenant_id="contoso", estate_id="contoso-target")
    )
    assert reconcile(
        source_snapshot.entities,
        target_snapshot.entities,
        source_key_for=KEY_FOR,
        target_key_for=KEY_FOR,
    ).passed

    source_line = next(
        e for e in source_snapshot.entities if isinstance(e, Line) and e.directory_number == "5199"
    )
    assert source_line.source_ref is not None
    assert source_line.source_ref.native_attributes["shared_with"] == ["bschmidt"]
    assert source_line.fidelity.level is FidelityLevel.DEGRADED


# --------------------------------------------------------------------------- #
# Idempotency, resumability, rollback
# --------------------------------------------------------------------------- #


async def test_rerunning_an_identical_plan_changes_nothing(
    source_connector, target_connector, target_estate, extract_request
) -> None:
    snapshot = await extract_all(source_connector, extract_request)
    plan = plan_for(snapshot.entities)
    receipt = await target_connector.dry_run(plan)

    first = await target_connector.apply(plan, production_authorization(plan, receipt))
    assert first.changed_count == len(plan.operations)
    writes_after_first = target_estate.write_count

    second_receipt = await target_connector.dry_run(plan)
    assert second_receipt.would_change_count == 0, "dry run must predict the no-op"

    second = await target_connector.apply(plan, production_authorization(plan, second_receipt))

    assert second.is_no_op
    assert all(r.status is OperationStatus.SKIPPED_NO_CHANGE for r in second.results)
    assert target_estate.write_count == writes_after_first, "no write was issued on the re-run"


async def test_run_resumes_after_a_checkpoint(
    source_connector, target_connector, target_estate, extract_request
) -> None:
    snapshot = await extract_all(source_connector, extract_request)
    plan = plan_for(snapshot.entities)
    receipt = await target_connector.dry_run(plan)

    ordered = plan.operations_in_dependency_order()
    halfway = ordered[len(ordered) // 2].op_id

    # Simulate a run that died: apply only the first half by resuming from the end.
    partial_plan = plan.model_copy(
        update={"operations": ordered[: len(ordered) // 2 + 1]}
    ).seal()
    partial_receipt = await target_connector.dry_run(partial_plan)
    await target_connector.apply(
        partial_plan, production_authorization(partial_plan, partial_receipt)
    )
    partial_records = target_estate.total_records()
    assert partial_records < len(ordered)

    # Resume the full plan after the checkpoint; the remainder completes.
    resumed = await target_connector.apply(
        plan, production_authorization(plan, receipt), resume_after_op_id=halfway
    )
    assert resumed.failures() == []
    assert target_estate.total_records() == len(ordered)


async def test_rollback_bundle_restores_the_target(
    source_connector, target_connector, target_estate, extract_request
) -> None:
    snapshot = await extract_all(source_connector, extract_request)
    plan = plan_for(snapshot.entities)
    receipt = await target_connector.dry_run(plan)

    report = await target_connector.apply(plan, production_authorization(plan, receipt))
    assert target_estate.total_records() == len(plan.operations)

    bundle = report.rollback_bundle
    assert bundle is not None
    assert bundle.is_complete
    assert len(bundle.operations) == len(plan.operations)

    # Inverse operations are in reverse dependency order: dependents undone first.
    forward = [op.op_id for op in plan.operations_in_dependency_order()]
    inverse = [op.op_id.removeprefix("rollback:") for op in bundle.operations]
    assert inverse == list(reversed(forward))

    for op in bundle.operations:
        await target_connector._execute_operation(op)
    assert target_estate.total_records() == 0


# --------------------------------------------------------------------------- #
# Resilience
# --------------------------------------------------------------------------- #


async def test_transient_failures_are_retried(
    source_connector, target_connector, target_estate, extract_request
) -> None:
    snapshot = await extract_all(source_connector, extract_request)
    plan = plan_for(snapshot.entities)
    receipt = await target_connector.dry_run(plan)

    target_estate.transient_failures["amueller"] = 2  # fails twice, succeeds on the third

    report = await target_connector.apply(plan, production_authorization(plan, receipt))

    user_result = next(r for r in report.results if r.target_native_key == "amueller")
    assert user_result.status is OperationStatus.SUCCEEDED
    assert user_result.attempts == 3
    assert report.failures() == []


async def test_a_permanent_failure_is_quarantined_and_the_wave_continues(
    source_connector, target_connector, target_estate, extract_request
) -> None:
    snapshot = await extract_all(source_connector, extract_request)
    plan = plan_for(snapshot.entities)
    receipt = await target_connector.dry_run(plan)

    target_estate.fail_on_write["amueller"] = "OBJECT_LOCKED"

    report = await target_connector.apply(plan, production_authorization(plan, receipt))

    statuses = {r.op_id: r.status for r in report.results}
    quarantined = [op for op, s in statuses.items() if s is OperationStatus.QUARANTINED]
    blocked = [op for op, s in statuses.items() if s is OperationStatus.BLOCKED_BY_DEPENDENCY]
    succeeded = [op for op, s in statuses.items() if s is OperationStatus.SUCCEEDED]

    assert len(quarantined) == 1
    assert blocked, "Anna's line depends on Anna and must be held back"
    assert succeeded, "unrelated objects must still migrate"

    # Everyone else made it.
    assert "bschmidt" in target_estate.users
    assert "amueller" not in target_estate.users


async def test_eventually_consistent_writes_are_confirmed_before_being_called_success(
    source_connector, target_estate, extract_request
) -> None:
    from tests.conftest import _no_sleep, read_write_ref

    snapshot = await extract_all(source_connector, extract_request)
    target_estate.replication_delay_reads = 2
    connector = MemoryPBXConnector(
        target_estate,
        tenant_id="contoso",
        credential_ref=read_write_ref(),
        sleep=_no_sleep,
    )
    assert connector.capabilities().eventual_consistency.is_eventually_consistent

    plan = plan_for(snapshot.entities)
    receipt = await connector.dry_run(plan)
    report = await connector.apply(plan, production_authorization(plan, receipt))

    assert report.failures() == []
    assert all(
        r.confirmed for r in report.results if r.status is OperationStatus.SUCCEEDED
    ), "every write on an eventually consistent platform must be confirmed by re-reading"


@pytest.mark.parametrize("estate_name", ["memorypbx-a", "memorypbx-b"])
async def test_natural_keys_are_stable_across_instances(estate_name: str) -> None:
    """Canonical ids differ per instance; natural keys do not. That is what reconciliation needs."""
    from tests.conftest import _no_sleep, read_only_ref

    from ucm_bridge.connectors.reference import build_demo_estate

    connector = MemoryPBXConnector(
        build_demo_estate(estate_name),
        tenant_id="contoso",
        credential_ref=read_only_ref(),
        sleep=_no_sleep,
    )
    snapshot = await connector.extract_snapshot(
        ExtractRequest(run_id="r", tenant_id="contoso", estate_id=estate_name)
    )
    keys = {KEY_FOR(e) for e in snapshot.entities}
    assert "amueller" in keys
    assert "+498912345101" in keys
    assert "MUC-HQ" in keys
    assert "MUC-HQ:5101" in keys
