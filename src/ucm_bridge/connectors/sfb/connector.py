"""Skype for Business Server connector (Extract).

The common case for SfB is not a migration between two estates but an *in-place
upgrade* to Teams in the same tenant, with users moved individually. That shapes
this connector:

* It reads ``TeamsUpgradePolicy`` per user, so the planner knows who is already
  in Teams-only mode, who is in islands mode, and who has not been staged at
  all. Migrating a user who is already upgraded is a no-op the plan should
  recognise rather than attempt.
* It reads the hybrid split-domain signals (``HostingProvider``,
  ``EnterpriseVoiceEnabled`` on-premises versus online) so a coexistence estate
  is described accurately instead of being reported twice.
* Response Group Service workflows and queues map onto the canonical call-queue
  entities, always DEGRADED: RGS has agent groups with its own routing and
  business-hours model that a cloud call queue does not reproduce.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from enum import StrEnum
from typing import Any, ClassVar

from ucm_bridge.canonical.base import (
    CanonicalEntity,
    DegradedAttribute,
    Platform,
    SourceRef,
)
from ucm_bridge.canonical.callhandling import CallQueue
from ucm_bridge.canonical.dialplan import (
    DigitManipulationRule,
    PatternDirection,
)
from ucm_bridge.canonical.identity import User
from ucm_bridge.canonical.policy import CallingPolicy
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
from ucm_bridge.vendor.powershell import Cmdlet, PowerShellBridge, PowerShellCommand

CONNECTOR_ID = "microsoft-sfb-server"
CONNECTOR_VERSION = "0.1.0"
SFB_VERIFIED_ON = date(2026, 7, 26)


class UpgradeMode(StrEnum):
    """Where a user sits on the journey to Teams."""

    NOT_STAGED = "NOT_STAGED"
    ISLANDS = "ISLANDS"
    SFB_ONLY = "SFB_ONLY"
    SFB_WITH_TEAMS_COLLAB = "SFB_WITH_TEAMS_COLLAB"
    TEAMS_ONLY = "TEAMS_ONLY"
    """Already migrated. The planner should skip these, not re-migrate them."""


_MODE_BY_POLICY = {
    "UpgradeToTeams": UpgradeMode.TEAMS_ONLY,
    "Islands": UpgradeMode.ISLANDS,
    "SfBOnly": UpgradeMode.SFB_ONLY,
    "SfBWithTeamsCollab": UpgradeMode.SFB_WITH_TEAMS_COLLAB,
}


def _cmdlet(name: str, *, notes: str) -> Cmdlet:
    """SfB Get-Cs* cmdlets take no required parameters for a full enumeration.

    Marked verified only for the fact that they are read-only enumerations,
    which is the property this connector depends on. Their output *shape* varies
    by CU level and is read defensively.
    """
    return Cmdlet(
        name=name,
        module="SkypeForBusiness",
        writes=False,
        verified_source=f"SfB Server Get-Cs* enumeration convention, checked {SFB_VERIFIED_ON}",
        notes=notes,
    )


SFB_CMDLETS: dict[str, Cmdlet] = {
    c.name: c
    for c in (
        _cmdlet("Get-CsUser", notes="On-premises users with voice and policy assignments."),
        _cmdlet("Get-CsVoicePolicy", notes="Voice policies and their PSTN usages."),
        _cmdlet("Get-CsDialPlan", notes="Dial plans and their normalisation rules."),
        _cmdlet("Get-CsVoiceRoute", notes="Voice routes."),
        _cmdlet("Get-CsPstnUsage", notes="PSTN usage records."),
        _cmdlet("Get-CsRgsWorkflow", notes="Response Group Service workflows."),
        _cmdlet("Get-CsRgsQueue", notes="Response Group Service queues."),
        _cmdlet("Get-CsTeamsUpgradePolicy", notes="Per-user Teams upgrade staging."),
    )
}


class SkypeForBusinessConnector(Connector):
    """Extract an on-premises Skype for Business Server topology."""

    connector_id: ClassVar[str] = CONNECTOR_ID
    platform: ClassVar[Platform] = Platform.MICROSOFT_SFB_SERVER

    def __init__(
        self,
        *,
        powershell: PowerShellBridge,
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
                path=f"sfb/{instance_id}",
                kind=CredentialKind.USERNAME_PASSWORD,
                scope=CredentialScope.READ_ONLY,
            ),
            credentials=credentials or CredentialBroker([]),
            **kwargs,
        )
        self.powershell = powershell
        self._cassette_is_synthetic = cassette_is_synthetic

    def synthetic_cassette_names(self) -> list[str]:
        return ["sfb-topology"] if self._cassette_is_synthetic else []

    def capabilities(self) -> CapabilityManifest:
        return CapabilityManifest(
            connector_id=CONNECTOR_ID,
            connector_version=CONNECTOR_VERSION,
            platform=self.platform,
            display_name="Skype for Business Server",
            api_surfaces=[
                APISurface(
                    name="SfB Remote PowerShell",
                    transport="PowerShell",
                    verified_at=SFB_VERIFIED_ON,
                    verification_method=(
                        "Get-Cs* cmdlets are read-only enumerations; that property is what "
                        "this connector relies on. Output shape varies by CU and is read "
                        "defensively rather than assumed."
                    ),
                )
            ],
            entities=[
                EntityCapability(
                    entity_kind="User",
                    can_extract=True,
                    api_surface="SfB Remote PowerShell",
                    known_gaps=["Contact objects and conferencing policies are not mapped"],
                    required_permissions=["CsViewOnlyAdministrator"],
                ),
                EntityCapability(
                    entity_kind="CallingPolicy",
                    can_extract=True,
                    api_surface="SfB Remote PowerShell",
                    required_permissions=["CsViewOnlyAdministrator"],
                ),
                EntityCapability(
                    entity_kind="DigitManipulationRule",
                    can_extract=True,
                    api_surface="SfB Remote PowerShell",
                    fidelity_notes=(
                        "SfB normalisation rules are already regex-based, so they map to Teams "
                        "closely — the nearest thing to a lossless dial-plan transform in this "
                        "product."
                    ),
                    required_permissions=["CsViewOnlyAdministrator"],
                ),
                EntityCapability(
                    entity_kind="CallQueue",
                    can_extract=True,
                    api_surface="SfB Remote PowerShell",
                    fidelity_notes="From RGS workflows and queues. Always DEGRADED.",
                    required_permissions=["CsViewOnlyAdministrator"],
                ),
            ],
            credential_requirements=[
                CredentialRequirement(
                    purpose="sfb-remote-powershell",
                    kind=CredentialKind.USERNAME_PASSWORD,
                    minimum_scope=CredentialScope.READ_ONLY,
                    required_roles=["CsViewOnlyAdministrator"],
                )
            ],
            rate_limits=RateLimitPolicy(max_concurrent_requests=2, max_attempts=3),
            eventual_consistency=EventualConsistencyPolicy(is_eventually_consistent=False),
            supports_dry_run=True,
            supports_rollback=False,
            air_gap_capable=True,
            notes="Extract-only; the Teams side of an in-place upgrade is written by the "
            "Teams connector.",
        )

    async def test_connection(self) -> ConnectionTestResult:
        try:
            users = await self._invoke("Get-CsUser")
        except Exception as exc:
            return ConnectionTestResult(
                connector_id=CONNECTOR_ID,
                reachable=False,
                authenticated=False,
                messages=[str(exc)],
            )
        return ConnectionTestResult(
            connector_id=CONNECTOR_ID,
            reachable=True,
            authenticated=True,
            scope=self.credential_ref.scope.value,
            granted_permissions=["CsViewOnlyAdministrator"],
            messages=[f"{len(users)} on-premises user(s) visible"],
        )

    @classmethod
    def natural_key_for(cls, entity: CanonicalEntity) -> str | None:
        if isinstance(entity, User):
            return entity.user_principal_name
        if isinstance(entity, (CallingPolicy, DigitManipulationRule, CallQueue)):
            return getattr(entity, "name", None)
        return None

    # ------------------------------------------------------------------ #
    # Extract
    # ------------------------------------------------------------------ #

    async def _invoke(self, cmdlet: str, **parameters: Any) -> list[dict[str, Any]]:
        result = await self.powershell.invoke(
            PowerShellCommand(
                cmdlet=cmdlet, parameters=parameters, module="SkypeForBusiness"
            )
        )
        if result is None:
            return []
        if isinstance(result, dict):
            return [result]
        return [r for r in result if isinstance(r, dict)]

    async def _extract_batches(self, request: ExtractRequest) -> AsyncIterator[ExtractBatch]:
        wanted = set(
            request.entity_kinds
            or ["CallingPolicy", "DigitManipulationRule", "User", "CallQueue"]
        )
        entities: list[CanonicalEntity] = []
        warnings: list[str] = []

        if "CallingPolicy" in wanted:
            entities.extend(self._map_voice_policies(await self._invoke("Get-CsVoicePolicy")))

        if "DigitManipulationRule" in wanted:
            entities.extend(self._map_dial_plans(await self._invoke("Get-CsDialPlan")))

        if "User" in wanted:
            upgrade = {
                str(row.get("Identity", "")).lower(): row
                for row in await self._invoke("Get-CsTeamsUpgradePolicy")
            }
            entities.extend(
                self._map_users(await self._invoke("Get-CsUser"), upgrade, warnings)
            )

        if "CallQueue" in wanted:
            queues = await self._invoke("Get-CsRgsQueue")
            entities.extend(
                self._map_rgs(await self._invoke("Get-CsRgsWorkflow"), queues)
            )

        page_size = max(1, request.page_size)
        pages = [entities[i : i + page_size] for i in range(0, len(entities), page_size)] or [[]]
        for index, page in enumerate(pages):
            is_final = index == len(pages) - 1
            yield ExtractBatch(
                run_id=request.run_id,
                sequence=index,
                entities=page,
                warnings=warnings if is_final else [],
                is_final=is_final,
            )

    def _source(self, native_type: str, key: str, record: dict[str, Any], *, api: str) -> SourceRef:
        return SourceRef(
            platform=self.platform,
            instance_id=self.instance_id,
            native_type=native_type,
            native_key=key,
            native_attributes=dict(record),
            api_surface=api,
        )

    def _id(self, kind: str, key: str) -> str:
        return CanonicalEntity.mint_canonical_id(
            self.platform, kind, key, instance_id=self.instance_id
        )

    def _map_voice_policies(self, records: list[dict[str, Any]]) -> list[CallingPolicy]:
        policies: list[CallingPolicy] = []
        for record in records:
            identity = str(record.get("Identity", "")).replace("Tag:", "").strip()
            if not identity:
                continue
            policies.append(
                CallingPolicy(
                    canonical_id=self._id("CallingPolicy", identity),
                    display_name=identity,
                    name=identity,
                    description=record.get("Description"),
                    allow_call_forwarding_to_phone=bool(record.get("AllowCallForwarding", True)),
                    allow_delegation=bool(record.get("EnableDelegation", True)),
                    allow_voicemail=bool(record.get("EnableVoicemailEscapeTimer", True)),
                    is_global_default=identity.lower() == "global",
                    derived_from="SfB:CsVoicePolicy",
                    source_ref=self._source(
                        "CsVoicePolicy", identity, record, api="SfB:Get-CsVoicePolicy"
                    ),
                    fidelity=assess_mapping(
                        record,
                        {"Identity", "Description", "AllowCallForwarding", "EnableDelegation",
                         "EnableVoicemailEscapeTimer", "Name"},
                        assessed_by=CONNECTOR_ID,
                        entity_label="VoicePolicy",
                        lossless_rationale="Feature toggles map onto the canonical policy.",
                        manual_effort_minutes=5,
                    ),
                )
            )
        return policies

    def _map_dial_plans(self, records: list[dict[str, Any]]) -> list[DigitManipulationRule]:
        """SfB normalisation rules are regex-based, like Teams. This one maps well."""
        rules: list[DigitManipulationRule] = []
        for plan in records:
            plan_name = str(plan.get("Identity", "")).replace("Tag:", "").strip() or "Global"
            for order, rule in enumerate(plan.get("NormalizationRules") or []):
                name = str(rule.get("Name") or f"{plan_name}-{order}")
                pattern = str(rule.get("Pattern") or "")
                if not pattern:
                    continue
                key = f"{plan_name}/{name}"
                rules.append(
                    DigitManipulationRule(
                        canonical_id=self._id("DigitManipulationRule", key),
                        display_name=key,
                        name=name,
                        match_pattern=pattern,
                        replacement=str(rule.get("Translation") or ""),
                        direction=PatternDirection.OUTBOUND,
                        order=order,
                        is_internal_extension=bool(rule.get("IsInternalExtension", False)),
                        description=rule.get("Description"),
                        source_ref=self._source(
                            "CsNormalizationRule", key, dict(rule), api="SfB:Get-CsDialPlan"
                        ),
                        fidelity=assess_mapping(
                            dict(rule),
                            {"Name", "Pattern", "Translation", "Description",
                             "IsInternalExtension"},
                            assessed_by=CONNECTOR_ID,
                            entity_label="NormalizationRule",
                            lossless_rationale=(
                                "Both platforms express normalisation as an ordered regex "
                                "pattern and translation, so this transfers directly."
                            ),
                        ),
                    )
                )
        return rules

    def _map_users(
        self,
        records: list[dict[str, Any]],
        upgrade: dict[str, dict[str, Any]],
        warnings: list[str],
    ) -> list[User]:
        users: list[User] = []
        already_upgraded = 0

        for record in records:
            upn = str(record.get("UserPrincipalName") or record.get("SipAddress") or "").strip()
            upn = upn.replace("sip:", "")
            if not upn:
                continue

            policy = str(
                (upgrade.get(upn.lower()) or {}).get("TeamsUpgradeEffectiveMode")
                or (upgrade.get(upn.lower()) or {}).get("Policy")
                or ""
            )
            mode = _MODE_BY_POLICY.get(policy, UpgradeMode.NOT_STAGED)
            if mode is UpgradeMode.TEAMS_ONLY:
                already_upgraded += 1

            hosting = str(record.get("HostingProvider") or "")
            degraded: list[DegradedAttribute] = []
            if mode is UpgradeMode.ISLANDS:
                degraded.append(
                    DegradedAttribute(
                        attribute="telephony_enabled",
                        reason="the user is in Islands mode, so both clients can receive calls",
                        source_value=policy,
                        target_behaviour=(
                            "Calls may ring in either client until the user is moved to "
                            "Teams-only. Cutover must set the upgrade policy, not just the "
                            "number."
                        ),
                    )
                )

            users.append(
                User(
                    canonical_id=self._id("User", upn),
                    display_name=str(record.get("DisplayName") or upn),
                    user_principal_name=upn,
                    email=record.get("WindowsEmailAddress"),
                    enabled=bool(record.get("Enabled", True)),
                    telephony_enabled=bool(record.get("EnterpriseVoiceEnabled", False)),
                    site_code=record.get("RegistrarPool"),
                    tags=(
                        {"upgrade_mode": mode.value, "hosting_provider": hosting}
                        if hosting
                        else {"upgrade_mode": mode.value}
                    ),
                    source_ref=self._source(
                        "CsUser", upn, {**record, "TeamsUpgradeMode": mode.value},
                        api="SfB:Get-CsUser + Get-CsTeamsUpgradePolicy",
                    ),
                    fidelity=assess_mapping(
                        record,
                        {"UserPrincipalName", "SipAddress", "DisplayName",
                         "WindowsEmailAddress", "Enabled", "EnterpriseVoiceEnabled",
                         "RegistrarPool", "HostingProvider", "LineURI", "VoicePolicy",
                         "DialPlan"},
                        assessed_by=CONNECTOR_ID,
                        entity_label="User",
                        lossless_rationale="Identity and voice enablement carry across.",
                        extra_degraded=degraded,
                        manual_effort_minutes=5 if degraded else None,
                    ),
                )
            )

        if already_upgraded:
            warnings.append(
                f"{already_upgraded} user(s) are already in Teams-only mode. The planner "
                "should skip these rather than migrating them again."
            )
        return users

    def _map_rgs(
        self, workflows: list[dict[str, Any]], queues: list[dict[str, Any]]
    ) -> list[CallQueue]:
        """Response Group workflows become call queues, always with declared loss."""
        queue_by_id = {str(q.get("Identity")): q for q in queues}
        mapped: list[CallQueue] = []

        for workflow in workflows:
            name = str(workflow.get("Name") or workflow.get("Identity") or "").strip()
            if not name:
                continue
            queue = queue_by_id.get(str(workflow.get("DefaultAction_QueueId") or ""), {})
            agent_groups = list(queue.get("AgentGroupIDList") or [])

            mapped.append(
                CallQueue(
                    canonical_id=self._id("CallQueue", name),
                    display_name=name,
                    name=name,
                    e164_ref=None,
                    language=workflow.get("Language"),
                    agent_group_refs=[self._id("Group", str(g)) for g in agent_groups],
                    source_ref=self._source(
                        "CsRgsWorkflow", name, {**workflow, "queue": queue},
                        api="SfB:Get-CsRgsWorkflow + Get-CsRgsQueue",
                    ),
                    fidelity=assess_mapping(
                        workflow,
                        {"Name", "Identity", "Language", "DefaultAction_QueueId", "LineURI",
                         "Description", "Active"},
                        assessed_by=CONNECTOR_ID,
                        entity_label="ResponseGroup",
                        lossless_rationale="Never lossless; RGS always degrades.",
                        extra_degraded=[
                            DegradedAttribute(
                                attribute="routing_method",
                                reason=(
                                    "RGS has its own agent-group routing, business-hours "
                                    "handling, and interactive question-and-answer flows"
                                ),
                                target_behaviour=(
                                    "Becomes a flat call queue. Business hours, holiday sets, "
                                    "and any interactive prompts must be rebuilt as auto "
                                    "attendants on the target."
                                ),
                            )
                        ],
                        manual_effort_minutes=45,
                    ),
                )
            )
        return mapped

    async def _preview_operation(self, operation: WriteOperation) -> OperationPreview:
        raise NotImplementedError(
            "The Skype for Business connector is Extract-only. In an in-place upgrade the "
            "writes happen on the Teams side, through the Teams connector."
        )

    async def _execute_operation(self, operation: WriteOperation) -> OperationResult:
        raise NotImplementedError("The Skype for Business connector is Extract-only.")
