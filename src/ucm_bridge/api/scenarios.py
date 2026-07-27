"""Demo scenarios: the estates the control plane can actually serve.

There is no database yet (ADR-0002 leaves storage undecided), so the API serves
live objects built from the same cassettes the test suite uses. That is a
deliberate limit, not a mock: every number on a screen is produced by the real
discovery, assessment, mapping, planning, execution, validation, and audit code
paths. Nothing here fabricates a result the library would not produce.

Two scenarios, chosen because between them they show both honest outcomes:

``contoso-cucm``
    Cisco CUCM to Microsoft Teams, driven by the committed cassettes. It runs
    the whole pipeline to dry-run and is then *correctly refused* a production
    write, because the Teams cassettes are hand-authored rather than captured.
    The UI shows the refusal rather than hiding it.

``contoso-legacy``
    The reference in-memory platform, both ends. Its API surface is genuinely
    verified, so it clears the readiness gate and a run can be executed,
    resumed, validated, and rolled back for real.
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path

from ucm_bridge.canonical.base import CanonicalEntity
from ucm_bridge.canonical.numbering import E164Number
from ucm_bridge.canonical.snapshot import EstateSnapshot
from ucm_bridge.connectors.avaya import AvayaAuraConnector
from ucm_bridge.connectors.base import Connector
from ucm_bridge.connectors.capabilities import WriteVerb
from ucm_bridge.connectors.credentials import CredentialKind, CredentialRef, CredentialScope
from ucm_bridge.connectors.cucm import CucmConnector
from ucm_bridge.connectors.genesys import GenesysCloudConnector
from ucm_bridge.connectors.reference import MemoryPBXConnector, MemoryPBXEstate, build_demo_estate
from ucm_bridge.connectors.sfb import SFB_CMDLETS, SkypeForBusinessConnector
from ucm_bridge.connectors.slack import SlackConnector
from ucm_bridge.connectors.teams import TeamsConnector
from ucm_bridge.mapping import MappingProfile, MappingRule, NumberPlan, RuleMatch, RuleSet
from ucm_bridge.mapping.normalisation import SiteNumberRule
from ucm_bridge.pipeline.planner import KeyResolver
from ucm_bridge.vendor.axl import CassetteAxlTransport
from ucm_bridge.vendor.cassette import Cassette
from ucm_bridge.vendor.msgraph import TEAMS_CMDLETS
from ucm_bridge.vendor.powershell import CassettePowerShellBridge
from ucm_bridge.vendor.rest import (
    GENESYS_PAGINATION,
    GRAPH_PAGINATION,
    SLACK_PAGINATION,
    CassetteRestTransport,
)
from ucm_bridge.vendor.sat import CassetteSatSession

#: Where the vendor cassettes live. They ship in ``tests/`` because that is what
#: they are — recorded fixtures — and the API replays the same ones rather than
#: keeping a second, divergent copy.
CASSETTE_DIR_ENV = "UCM_BRIDGE_CASSETTE_DIR"


def cassette_dir() -> Path:
    override = os.environ.get(CASSETTE_DIR_ENV)
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / "tests" / "cassettes"


class Scenario(ABC):
    """One end-to-end estate the UI can drive."""

    estate_id: str
    name: str
    summary: str
    tenant_id: str
    direction: str
    source_estate_id: str
    target_estate_id: str
    verb: WriteVerb = WriteVerb.CREATE

    #: Set where the source's workloads have no write path into this target, so
    #: the pipeline stops at assessment. Stated up front rather than discovered
    #: as an empty plan, because an empty plan looks like a bug and this is not
    #: one — it is the target's manifest being honest about what it cannot take.
    no_write_path: str | None = None

    @abstractmethod
    def build_source(self) -> Connector:
        """A fresh source connector. Never written to, in any mode."""

    @abstractmethod
    def build_target(self) -> Connector:
        """A fresh target connector, including whatever state it accumulates."""

    def profile(self) -> MappingProfile | None:
        """The mapping profile applied between extract and plan, if any."""
        return None

    def key_for(self) -> KeyResolver:
        return self.build_target().natural_key_for

    def plan_inputs(
        self, snapshot: EstateSnapshot
    ) -> tuple[list[CanonicalEntity], list[CanonicalEntity]]:
        """Split a transformed snapshot into entities to write, and context.

        Context entities are resolvable so references land, but are never
        planned. See ``build_apply_plan``.
        """
        return list(snapshot.entities), []


class CucmToTeamsScenario(Scenario):
    """The headline path, and the one that proves the readiness gate bites."""

    estate_id = "contoso-cucm"
    name = "Contoso — CUCM to Teams Phone"
    summary = (
        "A Cisco CUCM cluster migrating to Microsoft Teams Phone. Runs to dry-run; "
        "the production write is refused because the Teams cassettes are synthetic."
    )
    tenant_id = "contoso"
    direction = "on-prem to cloud"
    source_estate_id = "contoso-cucm"
    target_estate_id = "contoso-teams"
    verb = WriteVerb.ASSIGN

    def build_source(self) -> Connector:
        return CucmConnector(
            CassetteAxlTransport(Cassette.load(cassette_dir() / "cucm-discovery.json")),
            instance_id="cluster-muc-1",
            tenant_id=self.tenant_id,
            credential_ref=CredentialRef(
                provider="vault",
                path="cucm/ro",
                kind=CredentialKind.USERNAME_PASSWORD,
                scope=CredentialScope.READ_ONLY,
            ),
            cdr_last_activity={"amueller": datetime(2026, 7, 20, tzinfo=UTC)},
        )

    def build_target(self) -> Connector:
        return _teams_target(self.tenant_id)

    def profile(self) -> MappingProfile:
        """CUCM has no site field, so a rule derives one before normalisation."""
        return MappingProfile(
            profile_id="contoso-teams",
            name="Contoso CUCM to Teams",
            tenant_id=self.tenant_id,
            target_platform="microsoft.teams",
            rules=RuleSet(
                rules=[
                    MappingRule(
                        id="muc-site",
                        when=RuleMatch(entity="Extension", pattern=r"5\d{3}"),
                        then={"site_code": "MUC-HQ"},
                        description="Munich extensions are 5xxx.",
                    ),
                    MappingRule(
                        id="lon-site",
                        when=RuleMatch(entity="Extension", pattern=r"7\d{3}"),
                        then={"site_code": "LON-BR"},
                        description="London extensions are 7xxx.",
                    ),
                ]
            ),
            number_plan=NumberPlan(
                name="contoso",
                rules=[
                    SiteNumberRule(
                        site_code="MUC-HQ",
                        internal_pattern=r"5\d{3}",
                        e164_prefix="+498912345",
                    ),
                    SiteNumberRule(
                        site_code="LON-BR",
                        internal_pattern=r"7\d{3}",
                        e164_prefix="+442071838",
                    ),
                ],
            ),
        )

    def plan_inputs(
        self, snapshot: EstateSnapshot
    ) -> tuple[list[CanonicalEntity], list[CanonicalEntity]]:
        return _assignable_numbers(snapshot)


def _teams_target(tenant_id: str) -> Connector:
    """A Teams tenant. Shared, because four sources migrate into the same one."""
    cassette = Cassette.load(cassette_dir() / "teams-tenant.json")
    return TeamsConnector(
        graph=CassetteRestTransport(
            cassette,
            base_url="https://graph.microsoft.com/v1.0",
            pagination=GRAPH_PAGINATION,
        ),
        powershell=CassettePowerShellBridge(TEAMS_CMDLETS, cassette),
        instance_id="contoso.onmicrosoft.com",
        tenant_id=tenant_id,
        credential_ref=CredentialRef(
            provider="vault",
            path="teams/rw",
            kind=CredentialKind.CLIENT_CREDENTIALS,
            scope=CredentialScope.READ_WRITE,
        ),
    )


def _assignable_numbers(
    snapshot: EstateSnapshot,
) -> tuple[list[CanonicalEntity], list[CanonicalEntity]]:
    """Numbers with an owner, plus everything else as resolvable context.

    Teams assigns numbers the tenant already holds; it does not create users and
    does not create numbers. An ownerless number — a shared line, a hunt pilot,
    an analogue service — has nobody to assign to, so it is excluded here. The
    estate report already flagged each one, so this is a filter, not a drop.
    """
    assignable: list[CanonicalEntity] = [
        e for e in snapshot.entities if isinstance(e, E164Number) and e.assigned_to_ref
    ]
    assignable_ids = {e.canonical_id for e in assignable}
    context = [e for e in snapshot.entities if e.canonical_id not in assignable_ids]
    return assignable, context


class AvayaToTeamsScenario(Scenario):
    """Avaya Aura to Teams. The same shape as CUCM, over a very different wire.

    Communication Manager has no API worth the name: this is SAT, a fixed-width
    terminal form, parsed by column position. That it lands in the same canonical
    model as an AXL SOAP response is the whole argument for the hub-and-spoke.
    """

    estate_id = "contoso-avaya"
    name = "Contoso — Avaya Aura to Teams Phone"
    summary = (
        "An Avaya Communication Manager estate read over SAT terminal screens and normalised "
        "onto E.164, migrating to Teams Phone. Reaches minted numbers; stops short of a write "
        "for want of an identity source."
    )
    tenant_id = "contoso"
    direction = "on-prem to cloud"
    source_estate_id = "contoso-avaya"
    target_estate_id = "contoso-teams"
    verb = WriteVerb.ASSIGN
    no_write_path = (
        "SAT describes stations, not people. Extension 5101 is a port, a button template and a "
        "coverage path — nowhere in Communication Manager does it say who sits at it. Aura's "
        "identity source is System Manager, and this build carries no verified SMGR REST path: "
        "the endpoints differ materially between releases and none has been checked against a "
        "real system, so the connector declares them unverified rather than guessing. Teams "
        "assigns a number to a user, so without that identity there is nobody to assign to and "
        "the numbers stay unowned. Everything up to and including normalisation is real; "
        "capturing an SMGR cassette from a lab is what closes the last step."
    )

    def build_source(self) -> Connector:
        screens: dict[str, str] = json.loads(
            (cassette_dir() / "avaya-cm-sat.json").read_text(encoding="utf-8")
        )["screens"]
        return AvayaAuraConnector(
            sat=CassetteSatSession(screens),
            instance_id="cm-muc-01",
            tenant_id=self.tenant_id,
            credential_ref=CredentialRef(
                provider="vault",
                path="avaya/sat-ro",
                kind=CredentialKind.USERNAME_PASSWORD,
                scope=CredentialScope.READ_ONLY,
            ),
        )

    def build_target(self) -> Connector:
        return _teams_target(self.tenant_id)

    def profile(self) -> MappingProfile:
        """Aura's stations are 5xxx in Munich, same as the CUCM cluster.

        Deliberately a separate profile rather than a shared one: two estates
        agreeing on a numbering convention today is a coincidence, and wiring
        them to the same object would turn that coincidence into a dependency.
        """
        return MappingProfile(
            profile_id="contoso-avaya-teams",
            name="Contoso Avaya Aura to Teams",
            tenant_id=self.tenant_id,
            target_platform="microsoft.teams",
            rules=RuleSet(
                rules=[
                    MappingRule(
                        id="muc-site",
                        when=RuleMatch(entity="Extension", pattern=r"5\d{3}"),
                        then={"site_code": "MUC-HQ"},
                        description="Munich stations are 5xxx.",
                    ),
                ]
            ),
            number_plan=NumberPlan(
                name="contoso-avaya",
                rules=[
                    SiteNumberRule(
                        site_code="MUC-HQ",
                        internal_pattern=r"5\d{3}",
                        e164_prefix="+498912345",
                    ),
                ],
            ),
        )

    def plan_inputs(
        self, snapshot: EstateSnapshot
    ) -> tuple[list[CanonicalEntity], list[CanonicalEntity]]:
        return _assignable_numbers(snapshot)


class SfbToTeamsScenario(Scenario):
    """Skype for Business Server to Teams: the classic in-place upgrade."""

    estate_id = "contoso-sfb"
    name = "Contoso — Skype for Business Server to Teams"
    summary = (
        "An on-prem Skype for Business pool assessed for a Teams upgrade. SfB carries "
        "policies, call queues, and dial-plan normalisation rules but no numbering "
        "objects, so this runs to assessment rather than to a write."
    )
    tenant_id = "contoso"
    direction = "on-prem to cloud"
    source_estate_id = "contoso-sfb"
    target_estate_id = "contoso-teams"
    verb = WriteVerb.ASSIGN
    no_write_path = (
        "Skype for Business Server exposes users, calling policies, dial-plan "
        "normalisation rules and call queues. Teams Phone's connector applies E.164 "
        "numbers, licence assignments and voice routing policies. Nothing SfB extracts "
        "is a kind Teams can be asked to write, so the plan is empty by construction — "
        "the value here is the assessment and the fidelity report, which say what the "
        "upgrade will cost before anyone schedules it."
    )

    def build_source(self) -> Connector:
        return SkypeForBusinessConnector(
            powershell=CassettePowerShellBridge(
                SFB_CMDLETS, Cassette.load(cassette_dir() / "sfb-topology.json")
            ),
            instance_id="sfb-muc",
            tenant_id=self.tenant_id,
            credential_ref=CredentialRef(
                provider="vault",
                path="sfb/rtc-ro",
                kind=CredentialKind.USERNAME_PASSWORD,
                scope=CredentialScope.READ_ONLY,
            ),
        )

    def build_target(self) -> Connector:
        return _teams_target(self.tenant_id)


class SlackToTeamsScenario(Scenario):
    """Slack to Teams. The fidelity story, not the write story."""

    estate_id = "contoso-slack"
    name = "Contoso — Slack to Teams collaboration"
    summary = (
        "A Slack workspace assessed for consolidation into Teams. Slack is the estate "
        "with the most declared UNMAPPABLE kinds in the build, which is the point: the "
        "losses are enumerated up front rather than found during the cutover."
    )
    tenant_id = "contoso"
    direction = "cloud to cloud"
    source_estate_id = "contoso-slack"
    target_estate_id = "contoso-teams"
    verb = WriteVerb.ASSIGN
    no_write_path = (
        "Slack extracts collaboration objects — channels, memberships, groups, message "
        "archives. The Teams connector in this build is a Teams *Phone* connector and "
        "applies telephony objects only. Channel migration is a different write surface "
        "that has not been built, and the manifest says so rather than implying it might "
        "work. Twenty-three Slack entity kinds are additionally declared UNMAPPABLE: "
        "they have no Teams equivalent at all."
    )

    def build_source(self) -> Connector:
        return SlackConnector(
            api=CassetteRestTransport(
                Cassette.load(cassette_dir() / "slack-workspace.json"),
                base_url="https://slack.com/api",
                pagination=SLACK_PAGINATION,
            ),
            instance_id="contoso-workspace",
            tenant_id=self.tenant_id,
            credential_ref=CredentialRef(
                provider="vault",
                path="slack/ro",
                kind=CredentialKind.API_TOKEN,
                scope=CredentialScope.READ_ONLY,
            ),
        )

    def build_target(self) -> Connector:
        return _teams_target(self.tenant_id)


class GenesysToTeamsScenario(Scenario):
    """Genesys Cloud CX: the contact centre, which is nobody's phone system."""

    estate_id = "contoso-genesys"
    name = "Contoso — Genesys Cloud CX contact centre"
    summary = (
        "A Genesys Cloud contact centre: queues, skills, routing strategies, agent "
        "profiles and recording policy. Assessed against a Teams target to show what a "
        "split-target migration has to route elsewhere."
    )
    tenant_id = "contoso"
    direction = "cloud to cloud"
    source_estate_id = "contoso-genesys"
    target_estate_id = "contoso-teams"
    verb = WriteVerb.ASSIGN
    no_write_path = (
        "Contact-centre workloads do not migrate into Teams Phone. Queues, skills-based "
        "routing and recording policy have no Teams equivalent, which is exactly the case "
        "split-target routing exists for: telephony to Teams, contact centre to a contact "
        "-centre platform. The Genesys connector is extract-only in this build, so it is "
        "a source for that assessment and never a target."
    )

    def build_source(self) -> Connector:
        return GenesysCloudConnector(
            api=CassetteRestTransport(
                Cassette.load(cassette_dir() / "genesys-org.json"),
                base_url="https://api.mypurecloud.com",
                pagination=GENESYS_PAGINATION,
            ),
            instance_id="contoso-genesys",
            tenant_id=self.tenant_id,
            credential_ref=CredentialRef(
                provider="vault",
                path="genesys/ro",
                kind=CredentialKind.CLIENT_CREDENTIALS,
                scope=CredentialScope.READ_ONLY,
            ),
        )

    def build_target(self) -> Connector:
        return _teams_target(self.tenant_id)


class ReferencePlatformScenario(Scenario):
    """Both ends verified, so this one really executes, resumes, and rolls back."""

    estate_id = "contoso-legacy"
    name = "Contoso — reference platform round trip"
    summary = (
        "The in-memory reference platform at both ends. Its API surface is verified, "
        "so a production run can be executed, validated, and rolled back for real."
    )
    tenant_id = "contoso"
    direction = "like for like"
    source_estate_id = "contoso-legacy"
    target_estate_id = "contoso-target"

    def __init__(self) -> None:
        self._source_estate: MemoryPBXEstate = build_demo_estate("memorypbx-source")
        self._target_estate: MemoryPBXEstate = MemoryPBXEstate(instance_id="memorypbx-target")

    @property
    def source_estate(self) -> MemoryPBXEstate:
        return self._source_estate

    @property
    def target_estate(self) -> MemoryPBXEstate:
        return self._target_estate

    def build_source(self) -> Connector:
        return MemoryPBXConnector(
            self._source_estate,
            tenant_id=self.tenant_id,
            credential_ref=CredentialRef(
                provider="env",
                path="memorypbx-source",
                kind=CredentialKind.API_TOKEN,
                scope=CredentialScope.READ_ONLY,
            ),
            sleep=_no_sleep,
        )

    def build_target(self) -> Connector:
        return MemoryPBXConnector(
            self._target_estate,
            tenant_id=self.tenant_id,
            credential_ref=CredentialRef(
                provider="env",
                path="memorypbx-target",
                kind=CredentialKind.API_TOKEN,
                scope=CredentialScope.READ_WRITE,
            ),
            sleep=_no_sleep,
        )


async def _no_sleep(_seconds: float) -> None:
    """Collapse backoff and confirm-poll delays. A demo must not stall on them."""
    return None


def build_scenarios() -> list[Scenario]:
    """Every connector in the build, as something the console can actually drive.

    Ordered so the two that reach a write come first. The other four are sources
    only, which is a property of their manifests rather than of this list: you
    migrate off Aura and Skype for Business, and Slack and Genesys are read
    surfaces for planning a split target.
    """
    return [
        CucmToTeamsScenario(),
        AvayaToTeamsScenario(),
        ReferencePlatformScenario(),
        SfbToTeamsScenario(),
        SlackToTeamsScenario(),
        GenesysToTeamsScenario(),
    ]
