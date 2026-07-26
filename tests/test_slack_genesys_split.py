"""Phase 6: Slack, Genesys, and split-target routing."""

from __future__ import annotations

from pathlib import Path

import pytest

from ucm_bridge.canonical.base import CanonicalEntity, FidelityLevel
from ucm_bridge.canonical.collaboration import ChannelType, ChatChannel, MessageArchive
from ucm_bridge.canonical.contactcenter import AgentProfile, Queue, RecordingPolicy, Skill
from ucm_bridge.canonical.identity import User
from ucm_bridge.canonical.messaging import ExportAvailability
from ucm_bridge.connectors.contracts import ExtractRequest
from ucm_bridge.connectors.genesys import GenesysCloudConnector, rescale_proficiency
from ucm_bridge.connectors.slack import ENTERPRISE_GRID, SlackConnector
from ucm_bridge.connectors.teams import TeamsConnector
from ucm_bridge.pipeline import describe_split, plan_split_target
from ucm_bridge.vendor.cassette import Cassette
from ucm_bridge.vendor.msgraph import TEAMS_CMDLETS
from ucm_bridge.vendor.powershell import CassettePowerShellBridge
from ucm_bridge.vendor.rest import (
    GENESYS_PAGINATION,
    GRAPH_PAGINATION,
    SLACK_PAGINATION,
    CassetteRestTransport,
)

CASSETTES = Path(__file__).parent / "cassettes"


def slack_connector(plan_tier: str = "Slack Business+") -> SlackConnector:
    cassette = Cassette.load(CASSETTES / "slack-workspace.json")
    return SlackConnector(
        api=CassetteRestTransport(
            cassette, base_url="https://slack.com/api", pagination=SLACK_PAGINATION
        ),
        instance_id="contoso-workspace",
        tenant_id="contoso",
        plan_tier=plan_tier,
    )


@pytest.fixture
def slack() -> SlackConnector:
    return slack_connector()


@pytest.fixture
def genesys() -> GenesysCloudConnector:
    cassette = Cassette.load(CASSETTES / "genesys-org.json")
    return GenesysCloudConnector(
        api=CassetteRestTransport(
            cassette, base_url="https://api.mypurecloud.de", pagination=GENESYS_PAGINATION
        ),
        instance_id="contoso-genesys",
        tenant_id="contoso",
    )


@pytest.fixture
def teams() -> TeamsConnector:
    cassette = Cassette.load(CASSETTES / "teams-tenant.json")
    return TeamsConnector(
        graph=CassetteRestTransport(
            cassette, base_url="https://graph.microsoft.com/v1.0", pagination=GRAPH_PAGINATION
        ),
        powershell=CassettePowerShellBridge(TEAMS_CMDLETS, cassette),
        instance_id="contoso.onmicrosoft.com",
        tenant_id="contoso",
    )


async def extract(connector, estate_id: str):
    return await connector.extract_snapshot(
        ExtractRequest(run_id="r", tenant_id="contoso", estate_id=estate_id)
    )


# --------------------------------------------------------------------------- #
# Slack
# --------------------------------------------------------------------------- #


async def test_slack_declares_the_whole_voice_space_unmappable(slack: SlackConnector) -> None:
    """This declaration is what lets the planner keep voice away from Slack."""
    unmappable = slack.capabilities().unmappable_kinds()
    assert {"E164Number", "Line", "SIPTrunk", "EmergencyLocation", "CallQueue"} <= unmappable


async def test_slack_extracts_collaboration_entities(slack: SlackConnector) -> None:
    snapshot = await extract(slack, "contoso-slack")
    counts = snapshot.counts_by_kind()

    assert counts["User"] == 2  # the bot is excluded
    assert counts["Group"] == 1
    assert counts["ChatChannel"] == 3
    assert counts["ChannelMembership"] == 3


async def test_slack_channel_types_are_distinguished(slack: SlackConnector) -> None:
    snapshot = await extract(slack, "contoso-slack")
    channels = {c.name: c for c in snapshot.entities if isinstance(c, ChatChannel)}

    assert channels["general"].channel_type is ChannelType.PUBLIC
    assert channels["general"].is_general
    assert channels["finance-private"].channel_type is ChannelType.PRIVATE
    assert channels["vendor-shared"].channel_type is ChannelType.SHARED
    assert channels["vendor-shared"].externally_shared


async def test_history_export_is_unavailable_below_enterprise_grid(
    slack: SlackConnector,
) -> None:
    """Naming the required tier is more useful to a customer than 'unsupported'."""
    snapshot = await extract(slack, "contoso-slack")
    archive = next(e for e in snapshot.entities if isinstance(e, MessageArchive))

    assert archive.export_availability is ExportAvailability.UNAVAILABLE_PLAN_TIER
    assert archive.required_plan_tier == ENTERPRISE_GRID
    assert archive.fidelity.level is FidelityLevel.UNMAPPABLE
    assert any("Enterprise Grid" in w for w in snapshot.warnings)


async def test_history_export_is_available_on_enterprise_grid() -> None:
    connector = slack_connector(plan_tier=ENTERPRISE_GRID)
    snapshot = await extract(connector, "contoso-slack")
    archive = next(e for e in snapshot.entities if isinstance(e, MessageArchive))

    assert archive.export_availability is ExportAvailability.AVAILABLE
    assert archive.fidelity.level is FidelityLevel.DEGRADED
    # Even when export works, threading does not survive a cross-platform import.
    assert any(
        "Thread structure" in d.target_behaviour
        for d in archive.fidelity.degraded_attributes
    )


async def test_slack_connection_test_names_the_tier_limitation(slack: SlackConnector) -> None:
    result = await slack.test_connection()
    assert result.reachable
    assert any(ENTERPRISE_GRID in m for m in result.messages)


# --------------------------------------------------------------------------- #
# Genesys
# --------------------------------------------------------------------------- #


def test_proficiency_rescaling_reports_lost_precision() -> None:
    value, lost = rescale_proficiency(9, source_min=1, source_max=10)
    assert value == 4
    assert lost, "a 1-10 scale collapsing onto 0-5 loses distinctions"

    value, lost = rescale_proficiency(3, source_min=0, source_max=5)
    assert value == 3
    assert not lost, "a same-scale value is not a rescale"


async def test_genesys_extracts_contact_centre_entities(
    genesys: GenesysCloudConnector,
) -> None:
    snapshot = await extract(genesys, "contoso-genesys")
    counts = snapshot.counts_by_kind()

    assert counts["Skill"] == 2
    assert counts["Queue"] == 1
    assert counts["AgentProfile"] == 2
    assert counts["WrapUpCode"] == 2
    assert counts["RecordingPolicy"] == 1


async def test_queue_sla_is_converted_from_milliseconds(
    genesys: GenesysCloudConnector,
) -> None:
    snapshot = await extract(genesys, "contoso-genesys")
    queue = next(e for e in snapshot.entities if isinstance(e, Queue))
    assert queue.sla_target_seconds == 20


async def test_rescaled_agent_proficiency_is_declared_degraded(
    genesys: GenesysCloudConnector,
) -> None:
    snapshot = await extract(genesys, "contoso-genesys")
    agents = {a.agent_id: a for a in snapshot.entities if isinstance(a, AgentProfile)}

    anna = agents["anna.mueller@contoso.example"]
    assert anna.fidelity.level is FidelityLevel.DEGRADED
    loss = next(
        d for d in anna.fidelity.degraded_attributes if d.attribute == "skills.proficiency"
    )
    assert "different agent" in loss.target_behaviour

    # Cerys' skills were already on the Genesys scale, so nothing was rescaled.
    cerys = agents["cerys.jones@contoso.example"]
    assert not any(
        d.attribute == "skills.proficiency" for d in cerys.fidelity.degraded_attributes
    )


async def test_recording_policy_flags_data_residency(
    genesys: GenesysCloudConnector,
) -> None:
    snapshot = await extract(genesys, "contoso-genesys")
    policy = next(e for e in snapshot.entities if isinstance(e, RecordingPolicy))

    assert policy.retention_days == 1825
    loss = next(
        d for d in policy.fidelity.degraded_attributes if d.attribute == "storage_region"
    )
    assert "unlawful" in loss.target_behaviour


async def test_genesys_skills_declare_their_scale(genesys: GenesysCloudConnector) -> None:
    snapshot = await extract(genesys, "contoso-genesys")
    skill = next(e for e in snapshot.entities if isinstance(e, Skill))
    assert skill.proficiency_scale_min == 0
    assert skill.proficiency_scale_max == 5


# --------------------------------------------------------------------------- #
# Split-target routing
# --------------------------------------------------------------------------- #


async def test_voice_goes_to_teams_and_collaboration_to_slack(
    slack: SlackConnector, teams: TeamsConnector
) -> None:
    slack_snapshot = await extract(slack, "contoso-slack")
    teams_snapshot = await extract(teams, "contoso-teams")
    entities: list[CanonicalEntity] = [*slack_snapshot.entities, *teams_snapshot.entities]

    plan = plan_split_target(
        entities,
        [teams.capabilities(), slack.capabilities()],
        preference=["microsoft-teams", "slack"],
    )

    voice_target = plan.target_for(
        next(e.canonical_id for e in teams_snapshot.entities if e.kind == "E164Number")
    )
    assert voice_target == "microsoft-teams"


async def test_slack_never_receives_voice_even_when_preferred(
    slack: SlackConnector, teams: TeamsConnector
) -> None:
    """Slack's UNMAPPABLE declaration must beat the caller's preference order."""
    teams_snapshot = await extract(teams, "contoso-teams")
    numbers = [e for e in teams_snapshot.entities if e.kind == "E164Number"]

    plan = plan_split_target(
        numbers,
        [slack.capabilities(), teams.capabilities()],
        preference=["slack", "microsoft-teams"],  # Slack preferred, and still refused
    )
    assert plan.assignments.get("slack") is None
    assert len(plan.assignments["microsoft-teams"]) == len(numbers)


async def test_a_workload_no_target_can_take_is_orphaned_not_dropped(
    slack: SlackConnector,
) -> None:
    """Silence here would lose a workload. It has to be loud."""
    slack_snapshot = await extract(slack, "contoso-slack")
    channels = [e for e in slack_snapshot.entities if isinstance(e, ChatChannel)]

    # Slack is Extract-only, so nothing here can apply a ChatChannel.
    plan = plan_split_target(channels, [slack.capabilities()])

    assert not plan.is_complete
    orphan = next(o for o in plan.orphans if o.entity_kind == "ChatChannel")
    assert orphan.count == 3
    assert "nowhere to go" in orphan.reason
    assert "does not support applying" in orphan.rejected_by["slack"]


async def test_an_unmappable_rejection_is_explained_differently_from_unsupported(
    slack: SlackConnector, genesys: GenesysCloudConnector
) -> None:
    teams_like_number = next(
        e
        for e in (await extract(genesys, "contoso-genesys")).entities
        if isinstance(e, Queue)
    )
    plan = plan_split_target([teams_like_number], [slack.capabilities()])
    orphan = plan.orphans[0]
    assert "does not support applying" in orphan.rejected_by["slack"]

    from ucm_bridge.canonical.numbering import E164Number

    number = E164Number(canonical_id="n-1", e164="+498912345101")
    plan = plan_split_target([number], [slack.capabilities()])
    assert "declares E164Number UNMAPPABLE" in plan.orphans[0].rejected_by["slack"]


async def test_split_routing_renders_a_readable_summary(
    slack: SlackConnector, teams: TeamsConnector
) -> None:
    teams_snapshot = await extract(teams, "contoso-teams")
    slack_snapshot = await extract(slack, "contoso-slack")
    manifests = [teams.capabilities(), slack.capabilities()]

    plan = plan_split_target(
        [*teams_snapshot.entities, *slack_snapshot.entities],
        manifests,
        preference=["microsoft-teams", "slack"],
    )
    markdown = describe_split(plan, manifests)

    assert "# Split-target routing" in markdown
    assert "Microsoft Teams Phone" in markdown
    assert "Orphaned workloads" in markdown


def test_a_single_capable_target_wins_even_if_not_preferred() -> None:
    from ucm_bridge.canonical.base import Platform as P
    from ucm_bridge.connectors.capabilities import (
        CapabilityManifest,
        EntityCapability,
        WriteVerb,
    )

    only_target = CapabilityManifest(
        connector_id="only",
        connector_version="1",
        platform=P.GENERIC_SIP,
        display_name="Only",
        entities=[
            EntityCapability(
                entity_kind="User", can_apply=True, supported_verbs=[WriteVerb.CREATE]
            )
        ],
    )
    other = CapabilityManifest(
        connector_id="other", connector_version="1", platform=P.SLACK, display_name="Other"
    )
    user = User(canonical_id="u-1", user_principal_name="a@b.example")

    plan = plan_split_target([user], [only_target, other], preference=["other"])
    assert plan.assignments == {"only": ["u-1"]}
    assert plan.is_complete
