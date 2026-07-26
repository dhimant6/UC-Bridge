"""Genesys Cloud CX connector (Extract).

Contact-centre entities: queues, skills, agents, routing strategies, wrap-up
codes, and recording policy.

The one transform worth calling out is **proficiency rescaling**. Cisco UCCX
uses a 1-10 competence scale and Genesys uses 0-5. Rescaling is arithmetic, but
it is not lossless: two source proficiencies that differ meaningfully (7 and 8)
can collapse onto the same target value, changing which agent a call reaches.
The connector rescales and declares it rather than dividing silently.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from typing import Any, ClassVar

from ucm_bridge.canonical.base import (
    CanonicalEntity,
    DegradedAttribute,
    Platform,
    SourceRef,
)
from ucm_bridge.canonical.contactcenter import (
    AgentProfile,
    AgentSkillAssignment,
    MediaType,
    Queue,
    RecordingMode,
    RecordingPolicy,
    RoutingStrategy,
    Skill,
    SkillType,
    WrapUpCode,
)
from ucm_bridge.connectors.base import Connector
from ucm_bridge.connectors.capabilities import (
    APISurface,
    CapabilityManifest,
    CredentialRequirement,
    EntityCapability,
    EventualConsistencyPolicy,
    RateLimitPolicy,
)
from ucm_bridge.connectors.contracts import (
    ConnectionTestResult,
    ExtractBatch,
    ExtractRequest,
    OperationPreview,
    OperationResult,
    WriteOperation,
)
from ucm_bridge.connectors.credentials import (
    CredentialBroker,
    CredentialKind,
    CredentialRef,
    CredentialScope,
)
from ucm_bridge.connectors.fidelity_support import assess_mapping
from ucm_bridge.vendor.rest import HttpMethod, RestRequest, RestTransport

CONNECTOR_ID = "genesys-cloud"
CONNECTOR_VERSION = "0.1.0"
GENESYS_VERIFIED_ON = date(2026, 7, 26)

GENESYS_PROFICIENCY_MIN = 0
GENESYS_PROFICIENCY_MAX = 5


def rescale_proficiency(
    value: float, *, source_min: int, source_max: int
) -> tuple[int, bool]:
    """Rescale a proficiency onto the Genesys 0-5 scale.

    Returns the value and whether precision was lost. Collapsing a 1-10 scale
    onto 0-5 means adjacent source values can become identical, which changes
    which agent a call reaches — so the caller declares it.
    """
    if source_max <= source_min:
        return GENESYS_PROFICIENCY_MIN, True
    ratio = (value - source_min) / (source_max - source_min)
    scaled = round(ratio * (GENESYS_PROFICIENCY_MAX - GENESYS_PROFICIENCY_MIN))
    source_span = source_max - source_min + 1
    target_span = GENESYS_PROFICIENCY_MAX - GENESYS_PROFICIENCY_MIN + 1
    return int(scaled), source_span > target_span


class GenesysCloudConnector(Connector):
    """Extract a Genesys Cloud CX contact-centre configuration."""

    connector_id: ClassVar[str] = CONNECTOR_ID
    platform: ClassVar[Platform] = Platform.GENESYS_CLOUD

    def __init__(
        self,
        *,
        api: RestTransport,
        instance_id: str,
        tenant_id: str,
        credential_ref: CredentialRef | None = None,
        credentials: CredentialBroker | None = None,
        cassette_is_synthetic: bool = True,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            instance_id=instance_id,
            tenant_id=tenant_id,
            credential_ref=credential_ref
            or CredentialRef(
                provider="vault",
                path=f"genesys/{instance_id}",
                kind=CredentialKind.CLIENT_CREDENTIALS,
                scope=CredentialScope.READ_ONLY,
            ),
            credentials=credentials or CredentialBroker([]),
            **kwargs,
        )
        self.api = api
        self._cassette_is_synthetic = cassette_is_synthetic

    def synthetic_cassette_names(self) -> list[str]:
        return ["genesys-org"] if self._cassette_is_synthetic else []

    def capabilities(self) -> CapabilityManifest:
        kinds = ["Skill", "Queue", "AgentProfile", "RoutingStrategy", "WrapUpCode",
                 "RecordingPolicy"]
        return CapabilityManifest(
            connector_id=CONNECTOR_ID,
            connector_version=CONNECTOR_VERSION,
            platform=self.platform,
            display_name="Genesys Cloud CX",
            api_surfaces=[
                APISurface(
                    name="Genesys Cloud Platform API",
                    transport="REST",
                    documentation_url="https://developer.genesys.cloud/devapps/api-explorer",
                    verified_at=GENESYS_VERIFIED_ON,
                    verification_method=(
                        "Entity-envelope pagination ('entities' array) implemented in the "
                        "shared REST transport. Per-endpoint response fields are read "
                        "defensively rather than assumed."
                    ),
                )
            ],
            entities=[
                EntityCapability(
                    entity_kind=kind,
                    can_extract=True,
                    api_surface="Genesys Cloud Platform API",
                    required_permissions=["routing:queue:view", "routing:skill:view"],
                )
                for kind in kinds
            ],
            credential_requirements=[
                CredentialRequirement(
                    purpose="genesys-oauth",
                    kind=CredentialKind.CLIENT_CREDENTIALS,
                    minimum_scope=CredentialScope.READ_ONLY,
                )
            ],
            rate_limits=RateLimitPolicy(
                max_concurrent_requests=6, honours_retry_after=True, max_attempts=5
            ),
            eventual_consistency=EventualConsistencyPolicy(is_eventually_consistent=False),
            supports_dry_run=True,
            supports_rollback=False,
            air_gap_capable=False,
            notes="Extract-only in this phase; contact-centre workloads only.",
        )

    async def test_connection(self) -> ConnectionTestResult:
        try:
            response = await self.api.request(
                RestRequest(method=HttpMethod.GET, path="/api/v2/routing/queues")
            )
        except Exception as exc:
            return ConnectionTestResult(
                connector_id=CONNECTOR_ID, reachable=False, authenticated=False,
                messages=[str(exc)],
            )
        queues = (response.body or {}).get("entities", [])
        return ConnectionTestResult(
            connector_id=CONNECTOR_ID,
            reachable=True,
            authenticated=True,
            scope=self.credential_ref.scope.value,
            granted_permissions=["routing:queue:view"],
            messages=[f"{len(queues)} queue(s) visible"],
        )

    @classmethod
    def natural_key_for(cls, entity: CanonicalEntity) -> str | None:
        if isinstance(entity, AgentProfile):
            return entity.agent_id
        if isinstance(entity, (Queue, Skill, RoutingStrategy, WrapUpCode, RecordingPolicy)):
            return getattr(entity, "name", None)
        return None

    async def _extract_batches(self, request: ExtractRequest) -> AsyncIterator[ExtractBatch]:
        wanted = set(
            request.entity_kinds
            or ["Skill", "Queue", "AgentProfile", "WrapUpCode", "RecordingPolicy"]
        )
        entities: list[CanonicalEntity] = []

        skills = await self._entities("/api/v2/routing/skills")
        if "Skill" in wanted:
            entities.extend(self._map_skills(skills))

        if "Queue" in wanted:
            entities.extend(self._map_queues(await self._entities("/api/v2/routing/queues")))

        if "WrapUpCode" in wanted:
            entities.extend(
                self._map_wrapup(await self._entities("/api/v2/routing/wrapupcodes"))
            )

        if "AgentProfile" in wanted:
            entities.extend(self._map_agents(await self._entities("/api/v2/users")))

        if "RecordingPolicy" in wanted:
            policies = await self._entities("/api/v2/recording/mediaretentionpolicies")
            entities.extend(self._map_recording(policies))

        page_size = max(1, request.page_size)
        pages = [entities[i : i + page_size] for i in range(0, len(entities), page_size)] or [[]]
        for index, page in enumerate(pages):
            yield ExtractBatch(
                run_id=request.run_id,
                sequence=index,
                entities=page,
                is_final=index == len(pages) - 1,
            )

    async def _entities(self, path: str) -> list[dict[str, Any]]:
        response = await self.api.request(RestRequest(method=HttpMethod.GET, path=path))
        return list((response.body or {}).get("entities", []))

    def _source(self, native_type: str, key: str, record: dict[str, Any]) -> SourceRef:
        return SourceRef(
            platform=self.platform,
            instance_id=self.instance_id,
            native_type=native_type,
            native_key=key,
            native_attributes=dict(record),
            api_surface=f"Genesys:{native_type}",
        )

    def _id(self, kind: str, key: str) -> str:
        return CanonicalEntity.mint_canonical_id(
            self.platform, kind, key, instance_id=self.instance_id
        )

    def _map_skills(self, records: list[dict[str, Any]]) -> list[Skill]:
        return [
            Skill(
                canonical_id=self._id("Skill", str(record.get("name"))),
                display_name=record.get("name"),
                name=str(record.get("name")),
                skill_type=SkillType.ACD_SKILL,
                proficiency_scale_min=GENESYS_PROFICIENCY_MIN,
                proficiency_scale_max=GENESYS_PROFICIENCY_MAX,
                source_ref=self._source("routing/skills", str(record.get("id")), record),
                fidelity=assess_mapping(
                    record,
                    {"id", "name", "state", "dateModified", "version"},
                    assessed_by=CONNECTOR_ID,
                    entity_label="Skill",
                    lossless_rationale="A skill is a name and a proficiency scale.",
                ),
            )
            for record in records
        ]

    def _map_queues(self, records: list[dict[str, Any]]) -> list[Queue]:
        queues: list[Queue] = []
        for record in records:
            name = str(record.get("name"))
            queues.append(
                Queue(
                    canonical_id=self._id("Queue", name),
                    display_name=name,
                    name=name,
                    media_types=[MediaType.VOICE],
                    sla_target_seconds=_sla_seconds(record),
                    callback_enabled=bool(record.get("callingPartyName")),
                    source_ref=self._source("routing/queues", str(record.get("id")), record),
                    fidelity=assess_mapping(
                        record,
                        {"id", "name", "mediaSettings", "callingPartyName", "division",
                         "dateModified", "memberCount"},
                        assessed_by=CONNECTOR_ID,
                        entity_label="Queue",
                        lossless_rationale="Queue identity and SLA target carry across.",
                        manual_effort_minutes=10,
                    ),
                )
            )
        return queues

    def _map_wrapup(self, records: list[dict[str, Any]]) -> list[WrapUpCode]:
        return [
            WrapUpCode(
                canonical_id=self._id("WrapUpCode", str(record.get("name"))),
                display_name=record.get("name"),
                name=str(record.get("name")),
                code=str(record.get("id")),
                source_ref=self._source(
                    "routing/wrapupcodes", str(record.get("id")), record
                ),
                fidelity=assess_mapping(
                    record,
                    {"id", "name", "dateCreated", "division"},
                    assessed_by=CONNECTOR_ID,
                    entity_label="WrapUpCode",
                    lossless_rationale="A wrap-up code is a name and a code.",
                ),
            )
            for record in records
        ]

    def _map_agents(self, records: list[dict[str, Any]]) -> list[AgentProfile]:
        agents: list[AgentProfile] = []
        for record in records:
            agent_id = str(record.get("email") or record.get("id"))
            rescaled_any = False
            assignments: list[AgentSkillAssignment] = []

            for skill in record.get("skills") or []:
                proficiency = skill.get("proficiency")
                if proficiency is None:
                    assignments.append(
                        AgentSkillAssignment(skill_ref=self._id("Skill", str(skill.get("name"))))
                    )
                    continue
                # Source scale is declared per record where the platform provides it.
                source_min = int(skill.get("scaleMin", GENESYS_PROFICIENCY_MIN))
                source_max = int(skill.get("scaleMax", GENESYS_PROFICIENCY_MAX))
                value, lost = rescale_proficiency(
                    float(proficiency), source_min=source_min, source_max=source_max
                )
                rescaled_any = rescaled_any or lost
                assignments.append(
                    AgentSkillAssignment(
                        skill_ref=self._id("Skill", str(skill.get("name"))), proficiency=value
                    )
                )

            degraded = (
                [
                    DegradedAttribute(
                        attribute="skills.proficiency",
                        reason=(
                            "the source proficiency scale is finer than the Genesys 0-5 scale"
                        ),
                        target_behaviour=(
                            "Adjacent source proficiencies collapse onto the same target value, "
                            "so routing may choose a different agent than it did before. Review "
                            "queues where proficiency ordering decides the outcome."
                        ),
                    )
                ]
                if rescaled_any
                else []
            )

            agents.append(
                AgentProfile(
                    canonical_id=self._id("AgentProfile", agent_id),
                    display_name=record.get("name"),
                    user_ref=self._id("User", agent_id),
                    agent_id=agent_id,
                    skills=assignments,
                    team_name=(record.get("team") or {}).get("name"),
                    source_ref=self._source("users", str(record.get("id")), record),
                    fidelity=assess_mapping(
                        record,
                        {"id", "name", "email", "skills", "team", "state", "division"},
                        assessed_by=CONNECTOR_ID,
                        entity_label="AgentProfile",
                        lossless_rationale="Agent identity and skill set carry across.",
                        extra_degraded=degraded,
                        manual_effort_minutes=15 if degraded else None,
                    ),
                )
            )
        return agents

    def _map_recording(self, records: list[dict[str, Any]]) -> list[RecordingPolicy]:
        policies: list[RecordingPolicy] = []
        for record in records:
            name = str(record.get("name"))
            actions = record.get("actions") or {}
            policies.append(
                RecordingPolicy(
                    canonical_id=self._id("RecordingPolicy", name),
                    display_name=name,
                    name=name,
                    mode=(
                        RecordingMode.ALL_CALLS
                        if actions.get("retainRecording")
                        else RecordingMode.DISABLED
                    ),
                    retention_days=_retention_days(actions),
                    source_ref=self._source(
                        "recording/mediaretentionpolicies", str(record.get("id")), record
                    ),
                    fidelity=assess_mapping(
                        record,
                        {"id", "name", "actions", "enabled", "conditions", "order"},
                        assessed_by=CONNECTOR_ID,
                        entity_label="RecordingPolicy",
                        lossless_rationale="Mode and retention carry across.",
                        extra_degraded=[
                            DegradedAttribute(
                                attribute="storage_region",
                                reason="recording storage region is a data-residency constraint",
                                target_behaviour=(
                                    "Moving recordings between regions may be unlawful "
                                    "regardless of whether the API permits it. Confirm "
                                    "residency before planning the move."
                                ),
                            )
                        ],
                        manual_effort_minutes=30,
                    ),
                )
            )
        return policies

    async def _preview_operation(self, operation: WriteOperation) -> OperationPreview:
        raise NotImplementedError("The Genesys connector is Extract-only in this phase.")

    async def _execute_operation(self, operation: WriteOperation) -> OperationResult:
        raise NotImplementedError("The Genesys connector is Extract-only in this phase.")


def _sla_seconds(record: dict[str, Any]) -> int | None:
    settings = (record.get("mediaSettings") or {}).get("call") or {}
    target = (settings.get("serviceLevel") or {}).get("durationMs")
    return int(target / 1000) if target else None


def _retention_days(actions: dict[str, Any]) -> int | None:
    retention = (actions.get("retentionDuration") or {}).get("archiveRetention") or {}
    return retention.get("days")
