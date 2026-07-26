"""Phase 5: the reverse direction — ports, Direct Routing transform, reclaim."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from ucm_bridge.canonical.base import CanonicalEntity, FidelityLevel, Platform
from ucm_bridge.canonical.collaboration import MessageArchive
from ucm_bridge.canonical.dialplan import CallingPermission, RoutePattern
from ucm_bridge.canonical.identity import LicenseAssignment, User
from ucm_bridge.canonical.messaging import ExportAvailability, MessageStore
from ucm_bridge.canonical.numbering import (
    E164Number,
    NumberAssignmentKind,
    NumberAssignmentState,
    PortingRecord,
    PortOrderState,
)
from ucm_bridge.canonical.snapshot import EstateSnapshot
from ucm_bridge.canonical.trunking import (
    DirectRoutingPSTNGateway,
    PSTNUsage,
    SIPTrunk,
    VoiceRoute,
    VoiceRoutingPolicy,
)
from ucm_bridge.repatriation import (
    ExportRisk,
    IllegalPortTransition,
    assess_readiness,
    audit_export_limits,
    build_loa_packet,
    build_reclaim_plan,
    numbers_in_flight,
    plan_cutover_window,
    reclaim_plan_to_apply_plan,
    schedule_risk,
    transform_direct_routing_to_on_prem,
    transition,
    translate_number_pattern,
)

CSR = {
    "billing_telephone_number": "+498912345000",
    "account_number": "DE-99887766",
    "service_address": "Leopoldstrasse 7, 80802 Munich",
    "authorised_signatory": "Anna Mueller",
    "company_name": "Contoso GmbH",
}


def order(**overrides) -> PortingRecord:
    base = {
        "canonical_id": "port-1",
        "order_reference": "PORT-0001",
        "numbers": ["+498912345101", "+498912345102"],
        "losing_carrier": "Microsoft Calling Plan",
        "gaining_carrier": "Deutsche Telekom",
        "csr_fields": dict(CSR),
        "loa_reference": "loa/PORT-0001.pdf",
    }
    return PortingRecord(**{**base, **overrides})


# --------------------------------------------------------------------------- #
# Port order state machine
# --------------------------------------------------------------------------- #


def test_a_port_order_follows_the_legal_path() -> None:
    record = order()
    for state in (
        PortOrderState.LOA_PENDING,
        PortOrderState.SUBMITTED,
        PortOrderState.CARRIER_VALIDATING,
        PortOrderState.FOC_RECEIVED,
        PortOrderState.SCHEDULED,
        PortOrderState.IN_CUTOVER,
        PortOrderState.COMPLETED,
    ):
        record = transition(record, state)
    assert record.state is PortOrderState.COMPLETED


def test_illegal_transitions_are_refused() -> None:
    record = order()
    with pytest.raises(IllegalPortTransition, match="cannot move from DRAFT to COMPLETED"):
        transition(record, PortOrderState.COMPLETED)


def test_a_completed_order_is_terminal() -> None:
    record = order(state=PortOrderState.COMPLETED)
    with pytest.raises(IllegalPortTransition, match="terminal"):
        transition(record, PortOrderState.IN_CUTOVER)


def test_a_rejected_order_goes_back_to_draft_to_be_corrected() -> None:
    record = transition(order(state=PortOrderState.SUBMITTED), PortOrderState.REJECTED,
                        reason="Account number mismatch")
    assert record.rejection_reason == "Account number mismatch"
    assert transition(record, PortOrderState.DRAFT).state is PortOrderState.DRAFT


def test_entering_cutover_marks_the_number_dual_homed() -> None:
    """During the window the number legitimately exists in both estates."""
    record = transition(order(state=PortOrderState.SCHEDULED), PortOrderState.IN_CUTOVER)
    assert record.dual_homed_during_cutover
    assert numbers_in_flight([record]) == ["+498912345101", "+498912345102"]

    completed = transition(record, PortOrderState.COMPLETED)
    assert not completed.dual_homed_during_cutover
    assert numbers_in_flight([completed]) == []


def test_an_order_cannot_be_cancelled_once_the_carrier_has_started() -> None:
    record = order(state=PortOrderState.IN_CUTOVER)
    with pytest.raises(IllegalPortTransition):
        transition(record, PortOrderState.CANCELLED)


# --------------------------------------------------------------------------- #
# Readiness and LOA
# --------------------------------------------------------------------------- #


def test_readiness_names_exactly_what_is_missing() -> None:
    incomplete = order(csr_fields={"company_name": "Contoso GmbH"}, loa_reference=None)
    readiness = assess_readiness(incomplete)

    assert not readiness.ready_to_submit
    assert "account_number" in readiness.missing_csr_fields
    assert readiness.missing_loa
    assert any("Letter of Authority" in r for r in readiness.blocking_reasons)


def test_a_complete_order_is_ready() -> None:
    assert assess_readiness(order()).ready_to_submit


def test_a_past_foc_date_is_a_warning() -> None:
    readiness = assess_readiness(order(requested_foc_date=date.today() - timedelta(days=1)))
    assert any("in the past" in w for w in readiness.warnings)


def test_loa_generation_refuses_incomplete_data_rather_than_guessing() -> None:
    with pytest.raises(ValueError, match="incomplete order"):
        build_loa_packet(order(csr_fields={}))


def test_loa_packet_contains_what_a_carrier_needs() -> None:
    packet = build_loa_packet(order(requested_foc_date=date(2026, 9, 1)))
    text = packet.as_text()

    assert "Contoso GmbH" in text
    assert "DE-99887766" in text
    assert "+498912345101" in text
    assert "Deutsche Telekom" in text
    assert "2026-09-01" in text


def test_a_cutover_window_needs_a_confirmed_date_not_a_requested_one() -> None:
    with pytest.raises(ValueError, match="no confirmed FOC date"):
        plan_cutover_window(order(requested_foc_date=date(2026, 9, 1)))

    window = plan_cutover_window(order(confirmed_foc_date=date(2026, 9, 1)))
    assert window.duration.total_seconds() == 4 * 3600
    assert window.numbers == ["+498912345101", "+498912345102"]


def test_orders_inside_the_carrier_lead_time_are_flagged() -> None:
    soon = order(
        state=PortOrderState.SUBMITTED,
        requested_foc_date=date.today() + timedelta(days=3),
    )
    risks = schedule_risk([soon], lead_time_days=10)
    assert risks and "lead time" in risks[0]


# --------------------------------------------------------------------------- #
# Direct Routing -> SIP trunk
# --------------------------------------------------------------------------- #


def test_simple_patterns_translate_and_complex_ones_do_not() -> None:
    simple = translate_number_pattern(r"^\+49\d+$")
    assert simple.translatable
    assert simple.target_pattern == r"\+49!"

    catch_all = translate_number_pattern(r"^\+.*$")
    assert catch_all.translatable

    complex_pattern = translate_number_pattern(r"^\+(49|43|41)[1-9]\d{6,12}$")
    assert not complex_pattern.translatable
    assert "rewritten by hand" in (complex_pattern.reason or "")


def _teams_voice_config():
    gateway = DirectRoutingPSTNGateway(
        canonical_id="gw-1",
        fqdn="sbc01.contoso.example",
        media_bypass=True,
        failover_response_codes="408,503,504",
    )
    route = VoiceRoute(
        canonical_id="vr-1",
        name="EMEA-Germany",
        number_pattern=r"^\+49\d+$",
        gateway_refs=["gw-1"],
    )
    untranslatable = VoiceRoute(
        canonical_id="vr-2",
        name="EMEA-Multi",
        number_pattern=r"^\+(49|43|41)[1-9]\d{6,12}$",
        gateway_refs=["gw-1"],
    )
    usage = PSTNUsage(
        canonical_id="pu-1", name="EMEA-International", voice_route_refs=["vr-1", "vr-2"]
    )
    policy = VoiceRoutingPolicy(
        canonical_id="vrp-1", name="EMEA-International", pstn_usage_refs=["pu-1"]
    )
    return policy, usage, [route, untranslatable], gateway


def test_direct_routing_becomes_an_on_prem_dial_plan() -> None:
    policy, usage, routes, gateway = _teams_voice_config()
    result = transform_direct_routing_to_on_prem(
        policies=[policy],
        usages=[usage],
        routes=routes,
        gateways=[gateway],
        target_instance_id="cluster-muc-1",
    )

    kinds = {e.kind for e in result.entities}
    assert {"Partition", "SIPTrunk", "RouteGroup", "RouteList", "RoutePattern",
            "CallingPermission"} <= kinds

    pattern = next(e for e in result.entities if isinstance(e, RoutePattern))
    assert pattern.pattern == r"\+49!"
    assert pattern.route_target_ref is not None

    trunk = next(e for e in result.entities if isinstance(e, SIPTrunk))
    assert trunk.destinations[0].host == "sbc01.contoso.example"


def test_untranslatable_patterns_are_excluded_and_reported() -> None:
    """Guessing a route pattern silently reroutes calls, so it refuses."""
    policy, usage, routes, gateway = _teams_voice_config()
    result = transform_direct_routing_to_on_prem(
        policies=[policy],
        usages=[usage],
        routes=routes,
        gateways=[gateway],
        target_instance_id="cluster-muc-1",
    )

    assert not result.is_clean
    assert len(result.untranslatable_patterns) == 1
    assert result.untranslatable_patterns[0].source_pattern == r"^\+(49|43|41)[1-9]\d{6,12}$"
    assert any("no on-premises route" in w for w in result.warnings)

    patterns = [e.pattern for e in result.entities if isinstance(e, RoutePattern)]
    assert patterns == [r"\+49!"]


def test_the_ordering_semantics_change_is_declared() -> None:
    """Teams first-match vs CUCM longest-match is a real behavioural difference."""
    policy, usage, routes, gateway = _teams_voice_config()
    result = transform_direct_routing_to_on_prem(
        policies=[policy], usages=[usage], routes=routes, gateways=[gateway],
        target_instance_id="cluster-muc-1",
    )
    permission = next(e for e in result.entities if isinstance(e, CallingPermission))
    assert permission.fidelity.level is FidelityLevel.DEGRADED

    loss = next(
        d
        for d in permission.fidelity.degraded_attributes
        if d.attribute == "permitted_partition_refs"
    )
    assert "longest-match" in loss.reason
    assert "Test each pattern class with a real call" in loss.target_behaviour


def test_media_bypass_loss_is_declared_on_the_trunk() -> None:
    policy, usage, routes, gateway = _teams_voice_config()
    result = transform_direct_routing_to_on_prem(
        policies=[policy], usages=[usage], routes=routes, gateways=[gateway],
        target_instance_id="cluster-muc-1",
    )
    trunk = next(e for e in result.entities if isinstance(e, SIPTrunk))
    attributes = {d.attribute for d in trunk.fidelity.degraded_attributes}
    assert {"media_bypass", "failover_response_codes"} <= attributes


def test_a_missing_usage_is_reported_rather_than_skipped_silently() -> None:
    policy = VoiceRoutingPolicy(
        canonical_id="vrp-1", name="Orphan", pstn_usage_refs=["missing-usage"]
    )
    result = transform_direct_routing_to_on_prem(
        policies=[policy], usages=[], routes=[], gateways=[],
        target_instance_id="cluster-muc-1",
    )
    assert any("not in the snapshot" in w for w in result.warnings)


# --------------------------------------------------------------------------- #
# Licence reclaim
# --------------------------------------------------------------------------- #


def cloud_snapshot() -> EstateSnapshot:
    def uid(kind: str, key: str) -> str:
        return CanonicalEntity.mint_canonical_id(
            Platform.MICROSOFT_TEAMS, kind, key, instance_id="contoso"
        )

    anna = User(
        canonical_id=uid("User", "anna@contoso.example"),
        user_principal_name="anna@contoso.example",
        telephony_enabled=True,
    )
    still_cloud = User(
        canonical_id=uid("User", "zoe@contoso.example"),
        user_principal_name="zoe@contoso.example",
        telephony_enabled=True,
    )
    return EstateSnapshot.build(
        snapshot_id="cloud-1",
        tenant_id="contoso",
        estate_id="contoso-teams",
        entities=[
            anna,
            still_cloud,
            LicenseAssignment(
                canonical_id=uid("LicenseAssignment", "anna|phone"),
                principal_ref=anna.canonical_id,
                sku_id="sku-phone",
                sku_name="Teams Phone Standard",
                monthly_unit_cost=8.0,
                currency="EUR",
            ),
            LicenseAssignment(
                canonical_id=uid("LicenseAssignment", "zoe|phone"),
                principal_ref=still_cloud.canonical_id,
                sku_id="sku-phone",
                sku_name="Teams Phone Standard",
                monthly_unit_cost=8.0,
                currency="EUR",
            ),
            E164Number(
                canonical_id=uid("E164Number", "+498912345101"),
                e164="+498912345101",
                assignment_state=NumberAssignmentState.ASSIGNED,
                assignment_kind=NumberAssignmentKind.USER,
                assigned_to_ref=anna.canonical_id,
            ),
        ],
    )


def test_reclaim_only_touches_users_confirmed_migrated() -> None:
    """Reclaiming a seat from someone still on the cloud takes their phone away."""
    plan = build_reclaim_plan(
        cloud_snapshot(),
        tenant_id="contoso",
        migrated_user_keys={"anna@contoso.example"},
    )
    targets = " ".join(step.target for step in plan.steps)
    assert "anna@contoso.example" in targets
    assert "zoe@contoso.example" not in targets
    assert plan.seats_reclaimed == 1


def test_numbers_are_released_before_licences() -> None:
    """Removing the licence first can strand the number."""
    plan = build_reclaim_plan(
        cloud_snapshot(), tenant_id="contoso", migrated_user_keys={"anna@contoso.example"}
    )
    actions = [step.action for step in plan.steps]
    assert actions.index("UNASSIGN_NUMBER") < actions.index("UNASSIGN_LICENCE")


def test_reclaim_reports_the_spend_delta() -> None:
    plan = build_reclaim_plan(
        cloud_snapshot(), tenant_id="contoso", migrated_user_keys={"anna@contoso.example"}
    )
    assert plan.total_monthly_saving == 8.0
    assert plan.currency == "EUR"
    assert plan.annual_saving() == 96.0


def test_reclaim_plan_becomes_a_strictly_ordered_apply_plan() -> None:
    plan = build_reclaim_plan(
        cloud_snapshot(), tenant_id="contoso", migrated_user_keys={"anna@contoso.example"}
    )
    apply_plan = reclaim_plan_to_apply_plan(plan, plan_id="reclaim-1", estate_id="contoso-teams")

    ordered = [op.op_id for op in apply_plan.operations_in_dependency_order()]
    assert ordered[0].startswith("UNASSIGN_NUMBER")
    assert ordered[-1].startswith("UNASSIGN_LICENCE")


# --------------------------------------------------------------------------- #
# Export limits
# --------------------------------------------------------------------------- #


def test_undetermined_export_availability_blocks_the_plan() -> None:
    """'We did not check' is not something a customer can consent to."""
    snapshot = EstateSnapshot.build(
        snapshot_id="s",
        tenant_id="contoso",
        estate_id="contoso-teams",
        entities=[
            MessageArchive(
                canonical_id="archive-1",
                display_name="#finance",
                message_count=48_000,
                export_availability=ExportAvailability.NOT_YET_DETERMINED,
            )
        ],
    )
    audit = audit_export_limits(snapshot)

    assert not audit.safe_to_commit
    assert audit.undetermined[0].risk is ExportRisk.UNKNOWN
    assert audit.messages_at_risk == 48_000


def test_a_known_total_loss_is_reported_but_does_not_block() -> None:
    """A loss the customer has seen is a decision; an unknown one is a surprise."""
    snapshot = EstateSnapshot.build(
        snapshot_id="s",
        tenant_id="contoso",
        estate_id="contoso-teams",
        entities=[
            MessageArchive(
                canonical_id="archive-1",
                display_name="#finance",
                message_count=48_000,
                export_availability=ExportAvailability.UNAVAILABLE_PLAN_TIER,
                export_limitation="Discovery API needs Enterprise Grid.",
                required_plan_tier="Slack Enterprise Grid",
            ),
            MessageStore(
                canonical_id="vm-1",
                display_name="Anna's voicemail",
                message_count=37,
                export_availability=ExportAvailability.AVAILABLE,
            ),
        ],
    )
    audit = audit_export_limits(snapshot)

    assert audit.safe_to_commit
    loss = audit.total_losses[0]
    assert loss.required_plan_tier == "Slack Enterprise Grid"
    assert "Enterprise Grid" in loss.reason
    assert audit.messages_at_risk == 48_000
    assert audit.voicemails_at_risk == 0
    assert "48000 message(s)" in audit.summary()
