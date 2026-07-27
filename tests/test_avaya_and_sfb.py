"""Phase 4: Avaya Aura (SAT + SMGR) and Skype for Business Server connectors."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ucm_bridge.canonical.base import FidelityLevel
from ucm_bridge.canonical.callhandling import CallQueue, ForwardCondition, ForwardingRule
from ucm_bridge.canonical.dialplan import CallingPermission, DigitManipulationRule
from ucm_bridge.canonical.endpoints import Device, DeviceType
from ucm_bridge.canonical.identity import User
from ucm_bridge.canonical.numbering import Extension
from ucm_bridge.canonical.policy import CallingPolicy
from ucm_bridge.connectors.avaya import AvayaAuraConnector
from ucm_bridge.connectors.contracts import ExtractRequest
from ucm_bridge.connectors.sfb import SFB_CMDLETS, SkypeForBusinessConnector, UpgradeMode
from ucm_bridge.vendor.cassette import Cassette
from ucm_bridge.vendor.powershell import CassettePowerShellBridge
from ucm_bridge.vendor.sat import CassetteSatSession, SatCommandRefused

CASSETTES = Path(__file__).parent / "cassettes"

# The SAT fixtures live in tests/cassettes/avaya-cm-sat.json so the console can
# replay the same screens the parser is proven against, rather than keeping a
# second copy that could drift.
#
# Their column widths are exact - Ext 11, Port 10, Name 26, Room 12, Cv1 5,
# Cv2 5, COR 5 - and a single misplaced space shifts every later column, which
# is precisely the failure mode the position-based parser exists to make
# visible. Edit them in the JSON, not by retyping.
SAT_SCREENS: dict[str, str] = json.loads(
    (CASSETTES / "avaya-cm-sat.json").read_text(encoding="utf-8")
)["screens"]


@pytest.fixture
def avaya() -> AvayaAuraConnector:
    return AvayaAuraConnector(
        sat=CassetteSatSession(SAT_SCREENS),
        instance_id="cm-muc-01",
        tenant_id="contoso",
    )


@pytest.fixture
def sfb() -> SkypeForBusinessConnector:
    cassette = Cassette.load(CASSETTES / "sfb-topology.json")
    return SkypeForBusinessConnector(
        powershell=CassettePowerShellBridge(SFB_CMDLETS, cassette),
        instance_id="sfb-muc",
        tenant_id="contoso",
    )


async def extract(connector, estate_id: str):
    return await connector.extract_snapshot(
        ExtractRequest(run_id="r", tenant_id="contoso", estate_id=estate_id)
    )


# --------------------------------------------------------------------------- #
# Avaya
# --------------------------------------------------------------------------- #


async def test_sat_is_read_only_by_allow_list(avaya: AvayaAuraConnector) -> None:
    """A write verb is refused by the session, not by convention."""
    with pytest.raises(SatCommandRefused, match="not read-only"):
        await avaya.sat.run("change station 5101")


async def test_avaya_extraction_maps_stations(avaya: AvayaAuraConnector) -> None:
    snapshot = await extract(avaya, "contoso-avaya")
    counts = snapshot.counts_by_kind()

    assert counts["Extension"] == 3
    assert counts["Line"] == 3
    assert counts["Device"] == 3
    assert snapshot.verify_checksums() == []


async def test_an_avaya_analogue_station_is_flagged_for_replacement(
    avaya: AvayaAuraConnector,
) -> None:
    snapshot = await extract(avaya, "contoso-avaya")
    lift = next(
        e for e in snapshot.entities if isinstance(e, Device) and "5900" in e.device_name
    )
    assert lift.device_type is DeviceType.ANALOGUE
    assert lift.replacement_required
    assert lift.analogue_port == "01A0301"
    assert any("lift" in w.lower() or "analogue" in w.lower() for w in snapshot.warnings)


async def test_a_legacy_digital_set_is_flagged(avaya: AvayaAuraConnector) -> None:
    snapshot = await extract(avaya, "contoso-avaya")
    bruno = next(
        e for e in snapshot.entities if isinstance(e, Device) and "5102" in e.device_name
    )
    assert bruno.model == "2420"
    assert bruno.replacement_required
    assert any("no SIP registration path" in d.reason for d in bruno.fidelity.degraded_attributes)


async def test_cor_becomes_a_degraded_calling_permission(avaya: AvayaAuraConnector) -> None:
    """A restriction matrix is not an ordered partition list, and the report says so."""
    snapshot = await extract(avaya, "contoso-avaya")
    permissions = [e for e in snapshot.entities if isinstance(e, CallingPermission)]

    assert {p.name for p in permissions} == {"COR 1", "COR 7"}
    cor1 = next(p for p in permissions if p.name == "COR 1")
    assert cor1.fidelity.level is FidelityLevel.DEGRADED
    assert cor1.derived_from == "Avaya:COR-1"
    loss = cor1.fidelity.degraded_attributes[0]
    assert "restriction matrix" in loss.reason
    assert "re-expressed by hand" in loss.target_behaviour


async def test_coverage_paths_become_forwarding_rules_per_trigger(
    avaya: AvayaAuraConnector,
) -> None:
    snapshot = await extract(avaya, "contoso-avaya")
    rules = [e for e in snapshot.entities if isinstance(e, ForwardingRule)]

    conditions = {r.condition for r in rules}
    assert ForwardCondition.BUSY in conditions
    assert ForwardCondition.NO_ANSWER in conditions
    # 'Active?' and 'All?' are 'n' in the fixture and must not produce rules.
    assert ForwardCondition.ALWAYS not in conditions

    no_answer = next(r for r in rules if r.condition is ForwardCondition.NO_ANSWER)
    assert no_answer.delay_seconds == 18  # 3 rings
    assert any(
        "inside and outside" in d.reason for d in no_answer.fidelity.degraded_attributes
    )


async def test_hunt_groups_are_extracted_without_pretending_to_have_members(
    avaya: AvayaAuraConnector,
) -> None:
    snapshot = await extract(avaya, "contoso-avaya")
    from ucm_bridge.canonical.callhandling import HuntGroup

    group = next(e for e in snapshot.entities if isinstance(e, HuntGroup))
    assert group.pilot_pattern == "5000"
    assert group.name == "Finance Hotline"
    assert group.line_group_refs == []
    assert any("created empty" in d.target_behaviour for d in group.fidelity.degraded_attributes)


async def test_avaya_extension_records_that_e164_is_derived_not_read(
    avaya: AvayaAuraConnector,
) -> None:
    snapshot = await extract(avaya, "contoso-avaya")
    extension = next(e for e in snapshot.entities if isinstance(e, Extension))
    assert extension.e164_ref is None
    assert any(d.attribute == "e164_ref" for d in extension.fidelity.degraded_attributes)


async def test_avaya_smgr_is_declared_unverified(avaya: AvayaAuraConnector) -> None:
    manifest = avaya.capabilities()
    unverified = [s.name for s in manifest.unverified_api_surfaces()]
    assert "System Manager REST" in unverified
    assert "CM SAT" not in unverified
    assert not avaya.readiness().may_write_to_production


async def test_avaya_connection_test_reports_the_unverified_surface(
    avaya: AvayaAuraConnector,
) -> None:
    result = await avaya.test_connection()
    assert result.reachable
    assert any("unverified" in m for m in result.messages)


# --------------------------------------------------------------------------- #
# Skype for Business
# --------------------------------------------------------------------------- #


async def test_sfb_extraction_maps_users_policies_and_rules(
    sfb: SkypeForBusinessConnector,
) -> None:
    snapshot = await extract(sfb, "contoso-sfb")
    counts = snapshot.counts_by_kind()

    assert counts["User"] == 3
    assert counts["CallingPolicy"] == 2
    assert counts["DigitManipulationRule"] == 2
    assert counts["CallQueue"] == 1


async def test_users_already_in_teams_only_mode_are_flagged(
    sfb: SkypeForBusinessConnector,
) -> None:
    """Migrating an already-upgraded user is a no-op the plan should recognise."""
    snapshot = await extract(sfb, "contoso-sfb")
    bruno = next(
        e
        for e in snapshot.entities
        if isinstance(e, User) and e.user_principal_name == "bruno.schmidt@contoso.example"
    )
    assert bruno.tags["upgrade_mode"] == UpgradeMode.TEAMS_ONLY.value
    assert any("already in Teams-only mode" in w for w in snapshot.warnings)


async def test_islands_mode_is_reported_as_a_behavioural_risk(
    sfb: SkypeForBusinessConnector,
) -> None:
    snapshot = await extract(sfb, "contoso-sfb")
    anna = next(
        e
        for e in snapshot.entities
        if isinstance(e, User) and e.user_principal_name == "anna.mueller@contoso.example"
    )
    assert anna.tags["upgrade_mode"] == UpgradeMode.ISLANDS.value
    loss = next(
        d for d in anna.fidelity.degraded_attributes if d.attribute == "telephony_enabled"
    )
    assert "ring in either client" in loss.target_behaviour


async def test_normalisation_rules_transfer_losslessly(
    sfb: SkypeForBusinessConnector,
) -> None:
    """The one dial-plan transform in this product that genuinely is lossless."""
    snapshot = await extract(sfb, "contoso-sfb")
    rules = [e for e in snapshot.entities if isinstance(e, DigitManipulationRule)]

    internal = next(r for r in rules if r.name == "MUC-4digit")
    assert internal.match_pattern == r"^(\d{4})$"
    assert internal.replacement == "+498912345$1"
    assert internal.is_internal_extension
    assert internal.fidelity.level is FidelityLevel.LOSSLESS


async def test_response_groups_always_degrade(sfb: SkypeForBusinessConnector) -> None:
    snapshot = await extract(sfb, "contoso-sfb")
    queue = next(e for e in snapshot.entities if isinstance(e, CallQueue))

    assert queue.name == "Finance Helpdesk"
    assert len(queue.agent_group_refs) == 2
    assert queue.fidelity.level is FidelityLevel.DEGRADED
    assert any("rebuilt as auto" in d.target_behaviour for d in queue.fidelity.degraded_attributes)


async def test_sfb_voice_policies_carry_their_toggles(sfb: SkypeForBusinessConnector) -> None:
    snapshot = await extract(sfb, "contoso-sfb")
    policies = {e.name: e for e in snapshot.entities if isinstance(e, CallingPolicy)}

    assert policies["EMEA-International"].allow_delegation is True
    assert policies["EMEA-National"].allow_delegation is False
    assert policies["EMEA-International"].derived_from == "SfB:CsVoicePolicy"


async def test_sfb_is_extract_only(sfb: SkypeForBusinessConnector) -> None:
    manifest = sfb.capabilities()
    assert manifest.appliable_kinds() == set()
