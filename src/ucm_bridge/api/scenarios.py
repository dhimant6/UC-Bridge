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

import os
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path

from ucm_bridge.canonical.base import CanonicalEntity
from ucm_bridge.canonical.numbering import E164Number
from ucm_bridge.canonical.snapshot import EstateSnapshot
from ucm_bridge.connectors.base import Connector
from ucm_bridge.connectors.capabilities import WriteVerb
from ucm_bridge.connectors.credentials import CredentialKind, CredentialRef, CredentialScope
from ucm_bridge.connectors.cucm import CucmConnector
from ucm_bridge.connectors.reference import MemoryPBXConnector, MemoryPBXEstate, build_demo_estate
from ucm_bridge.connectors.teams import TeamsConnector
from ucm_bridge.mapping import MappingProfile, MappingRule, NumberPlan, RuleMatch, RuleSet
from ucm_bridge.mapping.normalisation import SiteNumberRule
from ucm_bridge.pipeline.planner import KeyResolver
from ucm_bridge.vendor.axl import CassetteAxlTransport
from ucm_bridge.vendor.cassette import Cassette
from ucm_bridge.vendor.msgraph import TEAMS_CMDLETS
from ucm_bridge.vendor.powershell import CassettePowerShellBridge
from ucm_bridge.vendor.rest import GRAPH_PAGINATION, CassetteRestTransport

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
        cassette = Cassette.load(cassette_dir() / "teams-tenant.json")
        return TeamsConnector(
            graph=CassetteRestTransport(
                cassette,
                base_url="https://graph.microsoft.com/v1.0",
                pagination=GRAPH_PAGINATION,
            ),
            powershell=CassettePowerShellBridge(TEAMS_CMDLETS, cassette),
            instance_id="contoso.onmicrosoft.com",
            tenant_id=self.tenant_id,
            credential_ref=CredentialRef(
                provider="vault",
                path="teams/rw",
                kind=CredentialKind.CLIENT_CREDENTIALS,
                scope=CredentialScope.READ_WRITE,
            ),
        )

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
        """Teams assigns numbers it already holds; it does not create users.

        Numbers with no owner — the shared line, the hunt pilot — cannot be
        assigned to anyone and are excluded here. The estate report already
        flagged them, so this is a filter, not a silent drop.
        """
        assignable: list[CanonicalEntity] = [
            e for e in snapshot.entities if isinstance(e, E164Number) and e.assigned_to_ref
        ]
        assignable_ids = {e.canonical_id for e in assignable}
        context = [e for e in snapshot.entities if e.canonical_id not in assignable_ids]
        return assignable, context


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
    return [CucmToTeamsScenario(), ReferencePlatformScenario()]
