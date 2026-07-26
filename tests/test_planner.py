"""Planner behaviour: reference resolution, cycle breaking, and determinism."""

from __future__ import annotations

import pytest

from ucm_bridge.canonical.base import CanonicalEntity, Platform, SourceRef
from ucm_bridge.canonical.identity import User
from ucm_bridge.canonical.numbering import (
    E164Number,
    NumberAssignmentKind,
    NumberAssignmentState,
)
from ucm_bridge.connectors.reference import MemoryPBXConnector
from ucm_bridge.pipeline.planner import (
    build_apply_plan,
    dependency_levels,
    precedence,
)

KEY_FOR = MemoryPBXConnector.natural_key_for


def _source(native_type: str, native_key: str) -> SourceRef:
    return SourceRef(
        platform=Platform.REFERENCE_MEMORYPBX,
        instance_id="inst",
        native_type=native_type,
        native_key=native_key,
    )


def _mutually_referencing_pair() -> tuple[User, E164Number]:
    user_id = CanonicalEntity.mint_canonical_id(
        Platform.REFERENCE_MEMORYPBX, "User", "amueller", instance_id="inst"
    )
    number_id = CanonicalEntity.mint_canonical_id(
        Platform.REFERENCE_MEMORYPBX, "E164Number", "+498912345101", instance_id="inst"
    )
    user = User(
        canonical_id=user_id,
        user_principal_name="amueller",
        primary_number_ref=number_id,
        source_ref=_source("users", "amueller"),
    )
    number = E164Number(
        canonical_id=number_id,
        e164="+498912345101",
        assignment_state=NumberAssignmentState.ASSIGNED,
        assignment_kind=NumberAssignmentKind.USER,
        assigned_to_ref=user_id,
        source_ref=_source("numbers", "+498912345101"),
    )
    return user, number


def _plan(entities, plan_id: str = "p"):
    return build_apply_plan(
        entities, plan_id=plan_id, tenant_id="t", estate_id="e", key_for=KEY_FOR
    )


def test_mutual_references_do_not_deadlock_the_planner() -> None:
    """User <-> E164Number is a genuine cycle in the canonical graph."""
    user, number = _mutually_referencing_pair()
    result = _plan([user, number])

    assert result.is_fully_resolved
    ordered = [op.entity_kind for op in result.plan.operations_in_dependency_order()]
    assert ordered.index("E164Number") < ordered.index("User")

    # Both directions of the reference still made it into the payloads.
    number_op = next(op for op in result.plan.operations if op.entity_kind == "E164Number")
    user_op = next(op for op in result.plan.operations if op.entity_kind == "User")
    assert number_op.payload["references"]["assigned_to_ref"] == "amueller"
    assert user_op.payload["references"]["primary_number_ref"] == "+498912345101"


def test_references_are_resolved_to_target_natural_keys_not_canonical_ids() -> None:
    user, number = _mutually_referencing_pair()
    result = _plan([user, number])
    user_op = next(op for op in result.plan.operations if op.entity_kind == "User")

    resolved = user_op.payload["references"]["primary_number_ref"]
    assert resolved == "+498912345101"
    assert resolved != number.canonical_id


def test_a_reference_to_a_missing_entity_is_reported_not_written() -> None:
    user, _ = _mutually_referencing_pair()
    result = _plan([user])  # the number is absent

    assert not result.is_fully_resolved
    problem = result.unresolved_references[0]
    assert problem.field == "primary_number_ref"
    assert "not present" in problem.reason

    user_op = result.plan.operations[0]
    assert "primary_number_ref" not in user_op.payload["references"]


def test_plans_are_deterministic() -> None:
    user, number = _mutually_referencing_pair()
    first = _plan([user, number], plan_id="a").plan
    second = _plan([number, user], plan_id="b").plan  # input order reversed

    assert first.plan_digest == second.plan_digest
    assert [op.op_id for op in first.operations] == [op.op_id for op in second.operations]


def test_idempotency_keys_are_independent_of_the_plan() -> None:
    user, number = _mutually_referencing_pair()
    keys_a = {op.idempotency_key for op in _plan([user, number], plan_id="a").plan.operations}
    keys_b = {op.idempotency_key for op in _plan([user, number], plan_id="b").plan.operations}
    assert keys_a == keys_b


def test_dependency_cycles_are_reported_rather_than_silently_ordered() -> None:
    from ucm_bridge.connectors.capabilities import WriteVerb
    from ucm_bridge.connectors.contracts import ApplyPlan, WriteOperation

    plan = ApplyPlan(
        plan_id="p",
        tenant_id="t",
        estate_id="e",
        operations=[
            WriteOperation(
                op_id="a",
                verb=WriteVerb.CREATE,
                entity_kind="User",
                canonical_id="1",
                idempotency_key="k1",
                depends_on=["b"],
            ),
            WriteOperation(
                op_id="b",
                verb=WriteVerb.CREATE,
                entity_kind="User",
                canonical_id="2",
                idempotency_key="k2",
                depends_on=["a"],
            ),
        ],
    )
    with pytest.raises(ValueError, match="Dependency cycle"):
        plan.operations_in_dependency_order()


def test_emergency_entities_are_planned_before_the_numbers_that_depend_on_them() -> None:
    assert precedence("EmergencyLocation") < precedence("E164Number")
    assert precedence("E164Number") < precedence("User")
    assert precedence("User") < precedence("Line")


async def test_dependency_levels_are_a_valid_topological_grouping(
    source_connector, extract_request
) -> None:
    snapshot = await source_connector.extract_snapshot(extract_request)
    plan = _plan(snapshot.entities).plan

    levels = dependency_levels(plan)
    level_of = {op_id: index for index, group in enumerate(levels) for op_id in group}

    for op in plan.operations:
        for dependency in op.depends_on:
            assert level_of[dependency] < level_of[op.op_id]
