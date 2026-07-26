"""Phase 1: CUCM discovery against a recorded AXL cassette."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ucm_bridge.assessment import (
    RuleContext,
    Severity,
    assess,
    render_assessment_markdown,
)
from ucm_bridge.canonical.dialplan import CallingPermission, Partition
from ucm_bridge.canonical.endpoints import Device, DeviceType
from ucm_bridge.canonical.identity import User
from ucm_bridge.canonical.trunking import SIPTrunk
from ucm_bridge.connectors.credentials import (
    CredentialKind,
    CredentialRef,
    CredentialScope,
)
from ucm_bridge.connectors.cucm import CucmConnector
from ucm_bridge.connectors.errors import RateLimited, SourceWriteAttempted
from ucm_bridge.discovery import (
    DiscoveryService,
    build_estate_report,
    render_estate_report_markdown,
)
from ucm_bridge.vendor.axl import CassetteAxlTransport, UnknownAxlOperation
from ucm_bridge.vendor.cassette import Cassette, CassetteMiss
from ucm_bridge.vendor.readiness import (
    NotProductionReady,
    ReadinessLevel,
    assert_production_ready,
)

CASSETTE_PATH = Path(__file__).parent / "cassettes" / "cucm-discovery.json"


@pytest.fixture
def cassette() -> Cassette:
    return Cassette.load(CASSETTE_PATH)


@pytest.fixture
def cucm(cassette: Cassette) -> CucmConnector:
    return CucmConnector(
        CassetteAxlTransport(cassette, schema_version="14.0"),
        instance_id="cluster-muc-1",
        tenant_id="contoso",
        credential_ref=CredentialRef(
            provider="vault",
            path="cucm/cluster-muc-1",
            kind=CredentialKind.USERNAME_PASSWORD,
            scope=CredentialScope.READ_ONLY,
        ),
        cdr_last_activity={
            "amueller": datetime(2026, 7, 20, 9, 15, tzinfo=UTC),
            "bschmidt": datetime(2026, 7, 24, 16, 2, tzinfo=UTC),
            "cjones": datetime(2026, 7, 25, 11, 40, tzinfo=UTC),
            # dormant.user deliberately absent
        },
    )


async def discover(cucm: CucmConnector):
    return await DiscoveryService(cucm).run(
        run_id="run-1", tenant_id="contoso", estate_id="contoso-cucm", snapshot_id="snap-1"
    )


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #


async def test_connection_test_reports_the_cluster_version(cucm: CucmConnector) -> None:
    result = await cucm.test_connection()
    assert result.ok
    assert result.platform_version is not None
    assert result.platform_version.startswith("14.")


async def test_discovery_maps_every_declared_entity_kind(cucm: CucmConnector) -> None:
    snapshot, _ = await discover(cucm)
    counts = snapshot.counts_by_kind()

    assert counts["Partition"] == 4
    assert counts["CallingPermission"] == 2
    assert counts["User"] == 4
    assert counts["Device"] == 5
    assert counts["SIPTrunk"] == 1
    assert snapshot.verify_checksums() == []


async def test_css_partition_ordering_is_preserved(cucm: CucmConnector) -> None:
    """Order is call routing. An unordered set would silently change behaviour."""
    snapshot, _ = await discover(cucm)
    css = next(
        e
        for e in snapshot.entities
        if isinstance(e, CallingPermission) and e.name == "CSS_MUC_International"
    )
    by_id = {p.canonical_id: p for p in snapshot.entities if isinstance(p, Partition)}
    partitions = [by_id[ref].name for ref in css.permitted_partition_refs]
    assert partitions == ["PT_MUC_Internal", "PT_PSTN_National"]


async def test_analogue_and_end_of_life_devices_are_flagged(cucm: CucmConnector) -> None:
    snapshot, _ = await discover(cucm)
    devices = {d.device_name: d for d in snapshot.entities if isinstance(d, Device)}

    assert devices["ANALOG_LIFT_MUC_1"].device_type is DeviceType.ANALOGUE
    assert devices["ANALOG_LIFT_MUC_1"].replacement_required
    assert devices["ANALOG_FAX_MUC_ACCT"].device_type is DeviceType.ANALOGUE

    # A 7960 is end-of-life; an 8841 is not.
    assert devices["SEP00AABBCCDDEE"].replacement_required
    assert not devices["SEP001122334455"].replacement_required

    assert devices["CSFCJONES"].device_type is DeviceType.SOFT_PHONE
    assert devices["SEP001122334455"].mac_address == "001122334455"


async def test_sip_trunk_destinations_keep_their_failover_order(cucm: CucmConnector) -> None:
    snapshot, _ = await discover(cucm)
    trunk = next(e for e in snapshot.entities if isinstance(e, SIPTrunk))
    assert [d.host for d in trunk.destinations] == ["10.60.1.10", "10.60.1.11"]
    assert [d.sort_order for d in trunk.destinations] == [1, 2]


async def test_unmapped_axl_attributes_are_retained_not_dropped(cucm: CucmConnector) -> None:
    snapshot, _ = await discover(cucm)
    pool = next(
        e
        for e in snapshot.entities
        if e.kind == "DevicePool" and e.display_name == "DP_MUC_HQ"
    )
    assert pool.source_ref is not None
    assert pool.source_ref.native_attributes["srstName"] == "SRST_MUC"
    # SRST has no cloud equivalent and the connector says so rather than dropping it.
    assert any(d.attribute == "srst_reference" for d in pool.fidelity.degraded_attributes)


async def test_cdr_absence_marks_a_seat_dormant(cucm: CucmConnector) -> None:
    snapshot, report = await discover(cucm)
    dormant = next(
        u
        for u in snapshot.entities
        if isinstance(u, User) and u.user_principal_name == "dormant.user"
    )
    assert dormant.last_call_activity_at is None
    assert "dormant.user" in report.dormant_extensions


# --------------------------------------------------------------------------- #
# Read-only guarantee
# --------------------------------------------------------------------------- #


async def test_discovery_issues_no_write_operations(cucm: CucmConnector, cassette) -> None:
    transport = cucm.transport
    await discover(cucm)
    # The allow-list is the mechanism; assert nothing raw-SQL happened either.
    assert transport.raw_sql_calls == []


async def test_an_undeclared_axl_call_is_refused(cucm: CucmConnector) -> None:
    with pytest.raises(UnknownAxlOperation, match="not on the reviewed allow-list"):
        await cucm.transport.call("removeEverything", {})


async def test_cucm_cannot_write_to_production_with_a_read_only_credential(
    cucm: CucmConnector,
) -> None:
    from ucm_bridge.connectors.contracts import ApplyPlan

    plan = ApplyPlan(plan_id="p", tenant_id="contoso", estate_id="e", operations=[]).seal()
    receipt = await cucm.dry_run(plan)

    from tests.conftest import production_authorization

    with pytest.raises(SourceWriteAttempted):
        await cucm.apply(plan, production_authorization(plan, receipt))


async def test_a_synthetic_cassette_blocks_production_writes(cucm: CucmConnector) -> None:
    """Hand-authored fixtures are honest test data, not evidence the API behaves this way."""
    readiness = cucm.readiness()
    assert readiness.level is ReadinessLevel.LAB_ONLY
    assert not readiness.may_write_to_production
    with pytest.raises(NotProductionReady, match="LAB_ONLY"):
        assert_production_ready(readiness)


# --------------------------------------------------------------------------- #
# Cassette machinery
# --------------------------------------------------------------------------- #


async def test_a_call_the_cassette_does_not_know_fails_loudly(cucm: CucmConnector) -> None:
    """Never silently fall through to a live call in a test suite."""
    with pytest.raises(CassetteMiss, match="no recording"):
        await cucm.transport.call("listPhone", {"searchCriteria": {"name": "nope"}})


async def test_the_cassette_can_reproduce_throttling(cucm: CucmConnector) -> None:
    with pytest.raises(RateLimited) as caught:
        await cucm.transport.call(
            "listPhone", {"searchCriteria": {"name": "THROTTLE_TEST"}, "returnedTags": {}}
        )
    assert caught.value.retryable


# --------------------------------------------------------------------------- #
# Estate report
# --------------------------------------------------------------------------- #


async def test_estate_report_surfaces_the_things_that_break_cutovers(cucm: CucmConnector) -> None:
    _, report = await discover(cucm)

    assert report.user_count == 4
    assert report.device_count == 5
    assert report.analogue_endpoint_count == 2
    assert report.devices_requiring_replacement == 3  # 2 analogue + 1 end-of-life

    # PT_Unused_2011 is referenced by nothing.
    assert "PT_Unused_2011" in report.unused_partitions

    assert report.dial_plan_complexity_score > 0
    assert "RoutePattern" in report.complexity_drivers
    assert report.estimated_manual_effort_minutes > 0


async def test_estate_report_renders_to_markdown(cucm: CucmConnector) -> None:
    _, report = await discover(cucm)
    markdown = render_estate_report_markdown(report)

    assert "# Estate report: contoso-cucm" in markdown
    assert "Cisco 7960" in markdown
    assert "Complexity score" in markdown


async def test_report_is_stable_across_reruns(cucm: CucmConnector) -> None:
    first_snapshot, _ = await discover(cucm)
    second_snapshot, _ = await discover(cucm)
    assert first_snapshot.snapshot_digest == second_snapshot.snapshot_digest
    assert build_estate_report(first_snapshot).entity_counts == build_estate_report(
        second_snapshot
    ).entity_counts


# --------------------------------------------------------------------------- #
# Assessment
# --------------------------------------------------------------------------- #


async def test_assessment_blocks_on_missing_e164_and_emergency_data(cucm: CucmConnector) -> None:
    snapshot, _ = await discover(cucm)
    report = assess(RuleContext(snapshot=snapshot, target_platform="microsoft.teams"))

    assert not report.is_ready_to_plan
    blocker_ids = {f.rule_id for f in report.blockers}
    # Every extension lacks an E.164 mapping in a raw CUCM estate.
    assert "NUM-001" in blocker_ids

    finding = next(f for f in report.findings if f.rule_id == "NUM-001")
    assert finding.affected_count == 5
    assert "site prefix rule" in finding.remediation


async def test_analogue_endpoints_raise_a_high_finding(cucm: CucmConnector) -> None:
    snapshot, _ = await discover(cucm)
    report = assess(RuleContext(snapshot=snapshot, target_platform="microsoft.teams"))

    finding = next(f for f in report.findings if f.rule_id == "EPT-001")
    assert finding.severity is Severity.HIGH
    assert finding.affected_count == 2
    assert "lift" in finding.detail.lower()


async def test_an_on_prem_target_does_not_demand_e164(cucm: CucmConnector) -> None:
    """The same estate assessed for an on-prem target has different blockers."""
    snapshot, _ = await discover(cucm)
    report = assess(
        RuleContext(
            snapshot=snapshot,
            target_platform="avaya.aura",
            target_requires_e164=False,
            target_supports_analogue=True,
        )
    )
    assert "NUM-001" not in {f.rule_id for f in report.findings}
    assert "EPT-001" not in {f.rule_id for f in report.findings}


async def test_blockers_cannot_be_waived(cucm: CucmConnector) -> None:
    snapshot, _ = await discover(cucm)
    report = assess(RuleContext(snapshot=snapshot, target_platform="microsoft.teams"))
    blocker = report.blockers[0]

    with pytest.raises(ValueError, match="cannot be waived"):
        blocker.waive(by="someone@contoso.example", reason="we accept the risk")


async def test_a_non_blocking_finding_can_be_waived_with_attribution(cucm: CucmConnector) -> None:
    snapshot, _ = await discover(cucm)
    report = assess(RuleContext(snapshot=snapshot, target_platform="microsoft.teams"))
    low = next(f for f in report.findings if f.severity is Severity.LOW)

    waived = low.waive(by="telecoms.lead@contoso.example", reason="seasonal staff, keep the seat")
    assert waived.status.value == "WAIVED"
    assert waived.waived_by == "telecoms.lead@contoso.example"


async def test_assessment_renders_to_markdown(cucm: CucmConnector) -> None:
    snapshot, _ = await discover(cucm)
    report = assess(RuleContext(snapshot=snapshot, target_platform="microsoft.teams"))
    markdown = render_assessment_markdown(report)
    assert "NOT READY TO PLAN" in markdown
    assert "BLOCKER" in markdown
