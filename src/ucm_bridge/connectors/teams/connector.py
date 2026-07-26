"""Microsoft Teams Phone connector (Extract + Apply).

Three Teams-specific behaviours shape this connector, and getting any of them
wrong produces a migration that reports success and fails validation:

**Licence ordering.** A number cannot be assigned to a user who has no Teams
Phone licence, and a licence takes time to provision. The connector therefore
emits licence assignment as a *separate operation that the number assignment
depends on*, and confirms the licence landed before the number write runs.

**Eventual consistency.** Teams writes are not immediately readable. The
manifest declares the tenant eventually consistent, so the base class
confirm-polls every write before calling it a success rather than assuming
read-your-write.

**Two APIs, one object.** Identity and licensing come from Graph; voice
configuration comes from Teams PowerShell. A user is assembled from both, and
the connector says which surface each attribute came from.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, ClassVar

from ucm_bridge.canonical.base import (
    CanonicalEntity,
    DegradedAttribute,
    FidelityAssessment,
    Platform,
    SourceRef,
    TargetRef,
)
from ucm_bridge.canonical.identity import LicenseAssignment, User
from ucm_bridge.canonical.numbering import (
    AcquisitionModel,
    E164Number,
    NumberAssignmentKind,
    NumberAssignmentState,
    NumberType,
)
from ucm_bridge.canonical.policy import (
    CivicAddress,
    EmergencyLocation,
)
from ucm_bridge.canonical.trunking import VoiceRoutingPolicy
from ucm_bridge.connectors.base import Connector
from ucm_bridge.connectors.capabilities import (
    APISurface,
    CapabilityManifest,
    CredentialRequirement,
    EntityCapability,
    EventualConsistencyPolicy,
    RateLimitPolicy,
    WriteVerb,
)
from ucm_bridge.connectors.contracts import (
    ConnectionTestResult,
    ExtractBatch,
    ExtractRequest,
    OperationPreview,
    OperationResult,
    OperationStatus,
    WriteOperation,
)
from ucm_bridge.connectors.credentials import (
    CredentialBroker,
    CredentialKind,
    CredentialRef,
    CredentialScope,
)
from ucm_bridge.connectors.errors import ConnectorError, ObjectConflict
from ucm_bridge.connectors.fidelity_support import assess_mapping
from ucm_bridge.vendor.msgraph import (
    GRAPH_READ_SCOPES,
    GRAPH_VERIFIED_ON,
    TEAMS_CMDLETS,
    TEAMS_PS_VERIFIED_ON,
    assign_licence,
    list_subscribed_skus,
    list_users,
    unverified_cmdlets,
)
from ucm_bridge.vendor.powershell import PowerShellBridge, PowerShellCommand
from ucm_bridge.vendor.rest import RestTransport

CONNECTOR_ID = "microsoft-teams"
CONNECTOR_VERSION = "0.1.0"

#: Service plans that indicate a seat can actually make PSTN calls. Used to
#: detect licence shortfall rather than to assert Microsoft's catalogue.
PHONE_SYSTEM_SERVICE_PLANS: frozenset[str] = frozenset({"MCOEV", "MCOEV_VIRTUALUSER"})

_NUMBER_TYPE_TO_ACQUISITION = {
    "DirectRouting": AcquisitionModel.DIRECT_ROUTING,
    "CallingPlan": AcquisitionModel.CALLING_PLAN,
    "OperatorConnect": AcquisitionModel.OPERATOR_CONNECT,
}
_ACQUISITION_TO_NUMBER_TYPE = {v: k for k, v in _NUMBER_TYPE_TO_ACQUISITION.items()}


class TeamsConnector(Connector):
    """Read and write a Microsoft 365 tenant's Teams Phone configuration."""

    connector_id: ClassVar[str] = CONNECTOR_ID
    platform: ClassVar[Platform] = Platform.MICROSOFT_TEAMS

    def __init__(
        self,
        *,
        graph: RestTransport,
        powershell: PowerShellBridge,
        instance_id: str,
        tenant_id: str,
        credential_ref: CredentialRef | None = None,
        credentials: CredentialBroker | None = None,
        phone_system_sku_id: str | None = None,
        cassette_is_synthetic: bool = True,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            instance_id=instance_id,
            tenant_id=tenant_id,
            credential_ref=credential_ref
            or CredentialRef(
                provider="vault",
                path=f"teams/{instance_id}",
                kind=CredentialKind.CLIENT_CREDENTIALS,
                scope=CredentialScope.READ_ONLY,
            ),
            credentials=credentials or CredentialBroker([]),
            **kwargs,
        )
        self.graph = graph
        self.powershell = powershell
        self.phone_system_sku_id = phone_system_sku_id
        self._cassette_is_synthetic = cassette_is_synthetic
        #: Populated during apply so number writes can check the licence landed.
        self._licensed_users: set[str] = set()

    # ------------------------------------------------------------------ #
    # Manifest
    # ------------------------------------------------------------------ #

    def synthetic_cassette_names(self) -> list[str]:
        return ["teams-tenant"] if self._cassette_is_synthetic else []

    def capabilities(self) -> CapabilityManifest:
        return CapabilityManifest(
            connector_id=CONNECTOR_ID,
            connector_version=CONNECTOR_VERSION,
            platform=self.platform,
            display_name="Microsoft Teams Phone",
            api_surfaces=[
                APISurface(
                    name="Microsoft Graph",
                    version="v1.0",
                    transport="REST",
                    documentation_url="https://learn.microsoft.com/en-us/graph/api/overview",
                    verified_at=GRAPH_VERIFIED_ON,
                    verification_method=(
                        "GET /subscribedSkus and POST /users/{id}/assignLicense request shapes "
                        "checked against the v1.0 reference. removeLicenses is required even "
                        "when empty."
                    ),
                ),
                APISurface(
                    name="Teams PowerShell",
                    version="3.0.0+",
                    transport="PowerShell",
                    documentation_url=(
                        "https://learn.microsoft.com/en-us/powershell/module/microsoftteams"
                    ),
                    verified_at=TEAMS_PS_VERIFIED_ON,
                    verification_method=(
                        "Set-CsPhoneNumberAssignment signature checked against Microsoft Learn "
                        "(-Identity, -TelephoneNumber, -NumberType, -LocationId). Cmdlets whose "
                        "signatures were not checked are declared unverified in vendor/msgraph.py."
                    ),
                    notes=(
                        "Unverified cmdlets in the catalogue: "
                        + ", ".join(unverified_cmdlets(list(TEAMS_CMDLETS))) or "none"
                    ),
                ),
            ],
            entities=[
                EntityCapability(
                    entity_kind="User",
                    can_extract=True,
                    # Teams does not provision users. They arrive from Entra ID,
                    # by directory sync or by the customer's joiner process, and
                    # this connector's job starts once they exist. Declaring a
                    # write capability it does not have would let the planner
                    # build a plan that fails at the first operation.
                    can_apply=False,
                    api_surface="Microsoft Graph",
                    known_gaps=[
                        "User provisioning is out of scope; identity comes from Entra ID",
                        "On-premises-synced users cannot have their number set here",
                    ],
                    required_permissions=list(GRAPH_READ_SCOPES),
                ),
                EntityCapability(
                    entity_kind="LicenseAssignment",
                    can_extract=True,
                    can_apply=True,
                    supported_verbs=[WriteVerb.ASSIGN, WriteVerb.UNASSIGN],
                    api_surface="Microsoft Graph",
                    fidelity_notes=(
                        "Licence assignment must complete and provision before a number can be "
                        "assigned. Modelled as an explicit dependency, not a sleep."
                    ),
                    required_permissions=["User.ReadWrite.All", "Organization.Read.All"],
                ),
                EntityCapability(
                    entity_kind="E164Number",
                    can_extract=True,
                    can_apply=True,
                    supported_verbs=[WriteVerb.ASSIGN, WriteVerb.UNASSIGN],
                    api_surface="Teams PowerShell",
                    known_gaps=[
                        "A number cannot be assigned without an emergency location on most "
                        "number types"
                    ],
                    required_permissions=["Teams Communications Administrator"],
                ),
                EntityCapability(
                    entity_kind="EmergencyLocation",
                    can_extract=True,
                    api_surface="Teams PowerShell",
                    known_gaps=[
                        "New-CsOnlineLisLocation signature is unverified, so creating locations "
                        "is not enabled"
                    ],
                    required_permissions=["Teams Communications Administrator"],
                ),
                EntityCapability(
                    entity_kind="VoiceRoutingPolicy",
                    can_extract=True,
                    can_apply=True,
                    supported_verbs=[WriteVerb.CREATE, WriteVerb.ASSIGN],
                    api_surface="Teams PowerShell",
                    required_permissions=["Teams Communications Administrator"],
                ),
            ],
            credential_requirements=[
                CredentialRequirement(
                    purpose="graph-app",
                    kind=CredentialKind.CLIENT_CREDENTIALS,
                    minimum_scope=CredentialScope.READ_ONLY,
                    required_roles=list(GRAPH_READ_SCOPES),
                    notes="Entra ID app registration with client credentials, not a password.",
                ),
                CredentialRequirement(
                    purpose="teams-powershell",
                    kind=CredentialKind.CERTIFICATE,
                    minimum_scope=CredentialScope.READ_ONLY,
                    required_roles=["Teams Communications Administrator"],
                ),
            ],
            rate_limits=RateLimitPolicy(
                max_concurrent_requests=8,
                honours_retry_after=True,
                initial_backoff_seconds=2.0,
                max_backoff_seconds=120.0,
                max_attempts=6,
                batch_size=20,
            ),
            # The single most important line in this manifest. Teams writes are
            # not immediately readable and assuming otherwise produces migrations
            # that report success and then fail validation.
            eventual_consistency=EventualConsistencyPolicy(
                is_eventually_consistent=True,
                confirm_poll_interval_seconds=5.0,
                confirm_poll_timeout_seconds=600.0,
                confirm_required_for_kinds=["LicenseAssignment", "E164Number", "User"],
            ),
            supports_dry_run=True,
            supports_rollback=True,
            air_gap_capable=False,
            notes="Requires outbound access to Graph and the Teams service endpoints.",
        )

    async def test_connection(self) -> ConnectionTestResult:
        messages: list[str] = []
        missing: list[str] = []
        try:
            response = await self.graph.request(list_subscribed_skus())
            skus = (response.body or {}).get("value", [])
            messages.append(f"{len(skus)} subscribed SKU(s) visible")
        except ConnectorError as exc:
            return ConnectionTestResult(
                connector_id=CONNECTOR_ID, reachable=False, authenticated=False,
                messages=[f"Graph unreachable: {exc}"],
            )

        unverified = unverified_cmdlets(list(TEAMS_CMDLETS))
        if unverified:
            messages.append(
                f"{len(unverified)} cmdlet signature(s) unverified: {', '.join(unverified)}"
            )
        return ConnectionTestResult(
            connector_id=CONNECTOR_ID,
            reachable=True,
            authenticated=True,
            scope=self.credential_ref.scope.value,
            platform_version="Graph v1.0",
            granted_permissions=list(GRAPH_READ_SCOPES),
            missing_permissions=missing,
            messages=messages,
        )

    # ------------------------------------------------------------------ #
    # Natural keys
    # ------------------------------------------------------------------ #

    @classmethod
    def natural_key_for(cls, entity: CanonicalEntity) -> str | None:
        if isinstance(entity, User):
            return entity.user_principal_name
        if isinstance(entity, E164Number):
            return entity.e164
        if isinstance(entity, EmergencyLocation):
            return entity.site_code
        if isinstance(entity, LicenseAssignment):
            return f"{entity.principal_ref}|{entity.sku_id}"
        if isinstance(entity, VoiceRoutingPolicy):
            return entity.name
        return None

    # ------------------------------------------------------------------ #
    # Extract
    # ------------------------------------------------------------------ #

    async def _extract_batches(self, request: ExtractRequest) -> AsyncIterator[ExtractBatch]:
        wanted = set(
            request.entity_kinds
            or ["EmergencyLocation", "E164Number", "User", "LicenseAssignment",
                "VoiceRoutingPolicy"]
        )
        entities: list[CanonicalEntity] = []
        warnings: list[str] = []

        if "EmergencyLocation" in wanted:
            locations = await self._invoke("Get-CsOnlineLisLocation", {})
            entities.extend(self._map_locations(locations))

        if "E164Number" in wanted:
            numbers = await self._invoke("Get-CsPhoneNumberAssignment", {})
            entities.extend(self._map_numbers(numbers, warnings))

        if "VoiceRoutingPolicy" in wanted:
            policies = await self._invoke("Get-CsOnlineVoiceRoutingPolicy", {})
            entities.extend(self._map_voice_routing_policies(policies))

        graph_users: list[dict[str, Any]] = []
        if wanted & {"User", "LicenseAssignment"}:
            response = await self.graph.request(list_users())
            graph_users = list((response.body or {}).get("value", []))

        voice_by_upn: dict[str, dict[str, Any]] = {}
        if "User" in wanted:
            for record in await self._invoke("Get-CsOnlineUser", {}):
                upn = str(record.get("UserPrincipalName", "")).lower()
                if upn:
                    voice_by_upn[upn] = record
            entities.extend(self._map_users(graph_users, voice_by_upn, warnings))

        if "LicenseAssignment" in wanted:
            sku_response = await self.graph.request(list_subscribed_skus())
            skus = list((sku_response.body or {}).get("value", []))
            entities.extend(self._map_licences(graph_users, skus))

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

    async def _invoke(self, cmdlet: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        result = await self.powershell.invoke(
            PowerShellCommand(cmdlet=cmdlet, parameters=parameters, module="MicrosoftTeams")
        )
        if result is None:
            return []
        if isinstance(result, dict):
            return [result]
        return [r for r in result if isinstance(r, dict)]

    def _source(self, native_type: str, native_key: str, record: dict[str, Any],
                *, api: str) -> SourceRef:
        return SourceRef(
            platform=self.platform,
            instance_id=self.instance_id,
            native_type=native_type,
            native_key=native_key,
            native_attributes=dict(record),
            api_surface=api,
        )

    def _id(self, kind: str, native_key: str) -> str:
        return CanonicalEntity.mint_canonical_id(
            self.platform, kind, native_key, instance_id=self.instance_id
        )

    # -- mappers --------------------------------------------------------- #

    def _map_locations(self, records: list[dict[str, Any]]) -> list[EmergencyLocation]:
        mapped: list[EmergencyLocation] = []
        for record in records:
            location_id = str(record.get("LocationId") or "").strip()
            if not location_id:
                continue
            site_code = str(record.get("Description") or record.get("City") or location_id)
            country = str(record.get("CountryOrRegion") or "").strip()[:2].upper() or "XX"
            address = CivicAddress(
                country=country,
                house_number=_str(record.get("HouseNumber")),
                street_name=_str(record.get("StreetName")),
                city=_str(record.get("City")),
                state_or_province=_str(record.get("StateOrProvince")),
                postal_code=_str(record.get("PostalCode")),
                sub_unit=_str(record.get("Location")),
            )
            validated = bool(record.get("ValidationStatus") == "Validated")
            degraded: list[DegradedAttribute] = []
            if not validated:
                degraded.append(
                    DegradedAttribute(
                        attribute="is_validated",
                        reason="the tenant reports this address as not validated",
                        target_behaviour=(
                            "Emergency calls from numbers at this location may not reach the "
                            "correct PSAP."
                        ),
                    )
                )
            mapped.append(
                EmergencyLocation(
                    canonical_id=self._id("EmergencyLocation", site_code),
                    display_name=site_code,
                    name=site_code,
                    site_code=site_code,
                    civic_address=address,
                    is_validated=validated,
                    validation_authority="Microsoft LIS" if validated else None,
                    validated_at=_utc(record.get("ValidatedAt")) if validated else None,
                    source_ref=self._source("LisLocation", location_id, record,
                                            api="TeamsPS:Get-CsOnlineLisLocation"),
                    fidelity=assess_mapping(
                        record,
                        {"LocationId", "Description", "City", "CountryOrRegion", "HouseNumber",
                         "StreetName", "StateOrProvince", "PostalCode", "Location",
                         "ValidationStatus", "ValidatedAt"},
                        assessed_by=CONNECTOR_ID,
                        entity_label="EmergencyLocation",
                        lossless_rationale="Civic address and validation state carry across.",
                        extra_degraded=degraded,
                        manual_effort_minutes=15 if degraded else None,
                    ),
                )
            )
        return mapped

    def _map_numbers(
        self, records: list[dict[str, Any]], warnings: list[str]
    ) -> list[E164Number]:
        mapped: list[E164Number] = []
        for record in records:
            e164 = _str(record.get("TelephoneNumber"))
            if not e164:
                continue
            if not e164.startswith("+"):
                e164 = f"+{e164.lstrip('+')}"
            assigned_to = _str(record.get("AssignedPstnTargetId"))
            number_type = _str(record.get("NumberType")) or "DirectRouting"
            location_id = _str(record.get("LocationId"))

            if not location_id:
                warnings.append(
                    f"{e164} has no emergency location assigned in the tenant."
                )

            mapped.append(
                E164Number(
                    canonical_id=self._id("E164Number", e164),
                    display_name=e164,
                    e164=e164,
                    number_type=(
                        NumberType.SERVICE_NUMBER
                        if _str(record.get("Capability")) == "ConferenceAssignment"
                        else NumberType.DID
                    ),
                    acquisition_model=_NUMBER_TYPE_TO_ACQUISITION.get(
                        number_type, AcquisitionModel.UNKNOWN
                    ),
                    assignment_state=(
                        NumberAssignmentState.ASSIGNED
                        if assigned_to
                        else NumberAssignmentState.UNASSIGNED
                    ),
                    assignment_kind=(
                        NumberAssignmentKind.USER if assigned_to
                        else NumberAssignmentKind.UNASSIGNED
                    ),
                    assigned_to_ref=self._id("User", assigned_to) if assigned_to else None,
                    emergency_location_ref=(
                        self._id("EmergencyLocation", location_id) if location_id else None
                    ),
                    source_ref=self._source("PhoneNumberAssignment", e164, record,
                                            api="TeamsPS:Get-CsPhoneNumberAssignment"),
                    fidelity=assess_mapping(
                        record,
                        {"TelephoneNumber", "AssignedPstnTargetId", "NumberType", "LocationId",
                         "Capability", "PstnAssignmentStatus"},
                        assessed_by=CONNECTOR_ID,
                        entity_label="E164Number",
                        lossless_rationale=(
                            "Number, acquisition model, assignment, and emergency location "
                            "all carry across."
                        ),
                    ),
                )
            )
        return mapped

    def _map_users(
        self,
        graph_users: list[dict[str, Any]],
        voice_by_upn: dict[str, dict[str, Any]],
        warnings: list[str],
    ) -> list[User]:
        mapped: list[User] = []
        for record in graph_users:
            upn = _str(record.get("userPrincipalName"))
            if not upn:
                continue
            voice = voice_by_upn.get(upn.lower(), {})
            line_uri = _str(voice.get("LineURI")) or _str(voice.get("OnPremLineURI"))
            e164 = _e164_from_line_uri(line_uri)

            degraded: list[DegradedAttribute] = []
            if voice.get("OnPremLineURI") and not voice.get("LineURI"):
                degraded.append(
                    DegradedAttribute(
                        attribute="primary_number_ref",
                        reason="the number is set in on-premises AD and synced into Microsoft 365",
                        source_value=line_uri,
                        target_behaviour=(
                            "Set-CsPhoneNumberAssignment cannot change this number. The value "
                            "must be cleared in on-premises AD and allowed to sync before the "
                            "cloud can own it."
                        ),
                    )
                )
                warnings.append(
                    f"{upn} has an on-premises-sourced LineURI and cannot be reassigned."
                )

            mapped.append(
                User(
                    canonical_id=self._id("User", upn),
                    display_name=_str(record.get("displayName")) or upn,
                    user_principal_name=upn,
                    email=_str(record.get("mail")),
                    given_name=_str(record.get("givenName")),
                    surname=_str(record.get("surname")),
                    department=_str(record.get("department")),
                    job_title=_str(record.get("jobTitle")),
                    employee_id=_str(record.get("employeeId")),
                    site_code=_str(record.get("officeLocation")),
                    enabled=bool(record.get("accountEnabled", True)),
                    telephony_enabled=bool(voice.get("EnterpriseVoiceEnabled", False)),
                    primary_number_ref=self._id("E164Number", e164) if e164 else None,
                    preferred_language=_str(record.get("preferredLanguage")),
                    source_ref=self._source("user", upn, {**record, **voice},
                                            api="Graph:/users + TeamsPS:Get-CsOnlineUser"),
                    fidelity=assess_mapping(
                        {**record, **voice},
                        {"id", "userPrincipalName", "displayName", "givenName", "surname",
                         "mail", "department", "jobTitle", "employeeId", "officeLocation",
                         "accountEnabled", "preferredLanguage", "usageLocation",
                         "UserPrincipalName", "EnterpriseVoiceEnabled", "LineURI",
                         "OnPremLineURI"},
                        assessed_by=CONNECTOR_ID,
                        entity_label="User",
                        lossless_rationale="Identity and voice enablement both carry across.",
                        extra_degraded=degraded,
                        manual_effort_minutes=10 if degraded else None,
                    ),
                )
            )
        return mapped

    def _map_licences(
        self, graph_users: list[dict[str, Any]], skus: list[dict[str, Any]]
    ) -> list[LicenseAssignment]:
        sku_names = {
            _str(s.get("skuId")): _str(s.get("skuPartNumber")) for s in skus if s.get("skuId")
        }
        sku_plans = {
            _str(s.get("skuId")): [
                _str(p.get("servicePlanName"))
                for p in (s.get("servicePlans") or [])
                if p.get("servicePlanName")
            ]
            for s in skus
            if s.get("skuId")
        }

        mapped: list[LicenseAssignment] = []
        for record in graph_users:
            upn = _str(record.get("userPrincipalName"))
            if not upn:
                continue
            for assigned in record.get("assignedLicenses") or []:
                sku_id = _str(assigned.get("skuId"))
                if not sku_id:
                    continue
                plans = sku_plans.get(sku_id, [])
                capabilities = [
                    "phone_system" for p in plans if p in PHONE_SYSTEM_SERVICE_PLANS
                ]
                mapped.append(
                    LicenseAssignment(
                        canonical_id=self._id("LicenseAssignment", f"{upn}|{sku_id}"),
                        display_name=f"{sku_names.get(sku_id) or sku_id} -> {upn}",
                        principal_ref=self._id("User", upn),
                        sku_id=sku_id,
                        sku_name=sku_names.get(sku_id),
                        disabled_service_plans=[
                            _str(p) or "" for p in assigned.get("disabledPlans") or []
                        ],
                        required_for=capabilities,
                        source_ref=self._source("assignedLicense", f"{upn}|{sku_id}",
                                                dict(assigned), api="Graph:/users"),
                        fidelity=FidelityAssessment.lossless(
                            "A licence assignment is a SKU, a principal, and a disabled-plan "
                            "list; all three carry across.",
                            assessed_by=CONNECTOR_ID,
                        ),
                    )
                )
        return mapped

    def _map_voice_routing_policies(
        self, records: list[dict[str, Any]]
    ) -> list[VoiceRoutingPolicy]:
        mapped: list[VoiceRoutingPolicy] = []
        for record in records:
            identity = _str(record.get("Identity")) or _str(record.get("Name"))
            if not identity:
                continue
            name = identity.replace("Tag:", "")
            usages = [str(u) for u in (record.get("OnlinePstnUsages") or [])]
            mapped.append(
                VoiceRoutingPolicy(
                    canonical_id=self._id("VoiceRoutingPolicy", name),
                    display_name=name,
                    name=name,
                    description=_str(record.get("Description")),
                    is_global_default=name.lower() == "global",
                    pstn_usage_refs=[self._id("PSTNUsage", u) for u in usages],
                    source_ref=self._source("CsOnlineVoiceRoutingPolicy", name, record,
                                            api="TeamsPS:Get-CsOnlineVoiceRoutingPolicy"),
                    fidelity=assess_mapping(
                        record,
                        {"Identity", "Name", "Description", "OnlinePstnUsages"},
                        assessed_by=CONNECTOR_ID,
                        entity_label="VoiceRoutingPolicy",
                        lossless_rationale="Name and ordered PSTN usage list carry across.",
                    ),
                )
            )
        return mapped

    # ------------------------------------------------------------------ #
    # Apply
    # ------------------------------------------------------------------ #

    def _command_for(self, operation: WriteOperation) -> PowerShellCommand | None:
        """The Teams PowerShell command an operation would issue, or None for Graph."""
        attributes = operation.payload.get("attributes", {})
        references = operation.payload.get("references", {})

        if operation.entity_kind == "E164Number":
            identity = references.get("assigned_to_ref")
            if not identity:
                # Not a connector gap: Teams assigns numbers to a user or a
                # resource account, and this number has neither. Shared lines,
                # hunt pilots, and analogue services arrive here.
                raise ObjectConflict(
                    f"{attributes.get('e164')} has no assignee, so there is nothing to "
                    "assign it to. Give it a resource account, or exclude it from the "
                    "user-assignment plan and handle it as a service number.",
                    connector_id=CONNECTOR_ID,
                    native_key=str(attributes.get("e164")),
                )
            number_type = _ACQUISITION_TO_NUMBER_TYPE.get(
                AcquisitionModel(attributes.get("acquisition_model", "UNKNOWN")),
                "DirectRouting",
            )
            parameters: dict[str, Any] = {
                "Identity": identity,
                "TelephoneNumber": attributes["e164"],
                "NumberType": number_type,
            }
            location = references.get("emergency_location_ref")
            if location:
                parameters["LocationId"] = location
            return PowerShellCommand(
                cmdlet="Set-CsPhoneNumberAssignment",
                parameters=parameters,
                module="MicrosoftTeams",
            )

        if operation.entity_kind == "VoiceRoutingPolicy":
            return PowerShellCommand(
                cmdlet="New-CsOnlineVoiceRoutingPolicy",
                parameters={
                    "Identity": attributes["name"],
                    "OnlinePstnUsages": references.get("pstn_usage_refs", []),
                },
                module="MicrosoftTeams",
            )
        return None

    async def _preview_operation(self, operation: WriteOperation) -> OperationPreview:
        attributes = operation.payload.get("attributes", {})
        references = operation.payload.get("references", {})
        key = operation.payload.get("natural_key")
        warnings: list[str] = []

        if operation.entity_kind == "LicenseAssignment":
            request = assign_licence(
                references.get("principal_ref", ""),
                add_sku_ids=[attributes.get("sku_id", "")],
            )
            current = await self._current_licences(references.get("principal_ref", ""))
            would_change = attributes.get("sku_id") not in current
            return OperationPreview(
                op_id=operation.op_id,
                verb=operation.verb,
                target_native_type="assignedLicense",
                target_native_key=key,
                api_call=f"Graph POST {request.path}",
                current_target_state={"assignedSkuIds": sorted(current)},
                proposed_state=request.body or {},
                would_change=would_change,
                warnings=warnings,
            )

        command = self._command_for(operation)
        if command is None:
            raise ObjectConflict(
                f"{operation.entity_kind} has no Teams write path in this connector "
                f"(operation {operation.op_id}).",
                connector_id=CONNECTOR_ID,
            )

        if operation.entity_kind == "E164Number":
            if not references.get("emergency_location_ref"):
                warnings.append(
                    f"{attributes.get('e164')} would be assigned with no emergency location."
                )
            identity = command.parameters["Identity"]
            if identity not in self._licensed_users:
                warnings.append(
                    f"{identity} has no confirmed Teams Phone licence in this run; the "
                    "assignment will fail unless the licence operation runs first."
                )

        current_state = await self._current_number_assignment(command)
        return OperationPreview(
            op_id=operation.op_id,
            verb=operation.verb,
            target_native_type=operation.entity_kind,
            target_native_key=key,
            api_call=command.preview(),
            current_target_state=current_state,
            proposed_state=command.parameters,
            would_change=current_state != command.parameters,
            warnings=warnings,
        )

    async def _current_licences(self, user_id: str) -> set[str]:
        if not user_id:
            return set()
        try:
            response = await self.graph.request(
                list_users(select=["userPrincipalName", "assignedLicenses"])
            )
        except ConnectorError:
            return set()
        for record in (response.body or {}).get("value", []):
            if _str(record.get("userPrincipalName")) == user_id:
                return {
                    _str(a.get("skuId")) or "" for a in record.get("assignedLicenses") or []
                }
        return set()

    async def _current_number_assignment(
        self, command: PowerShellCommand
    ) -> dict[str, Any] | None:
        number = command.parameters.get("TelephoneNumber")
        if not number:
            return None
        try:
            records = await self._invoke(
                "Get-CsPhoneNumberAssignment", {"TelephoneNumber": number}
            )
        except Exception:
            return None
        if not records:
            return None
        record = records[0]
        assigned = _str(record.get("AssignedPstnTargetId"))
        if not assigned:
            return None
        return {
            "Identity": assigned,
            "TelephoneNumber": _str(record.get("TelephoneNumber")),
            "NumberType": _str(record.get("NumberType")),
            **({"LocationId": _str(record.get("LocationId"))} if record.get("LocationId") else {}),
        }

    async def _execute_operation(self, operation: WriteOperation) -> OperationResult:
        attributes = operation.payload.get("attributes", {})
        references = operation.payload.get("references", {})
        key = str(operation.payload.get("natural_key") or "")

        if operation.entity_kind == "LicenseAssignment":
            principal = references.get("principal_ref", "")
            await self.graph.request(
                assign_licence(principal, add_sku_ids=[attributes.get("sku_id", "")])
            )
            self._licensed_users.add(principal)
            return OperationResult(
                op_id=operation.op_id,
                status=OperationStatus.SUCCEEDED,
                target_native_key=key,
                target_native_type="assignedLicense",
                post_state={"skuId": attributes.get("sku_id"), "principal": principal},
            )

        command = self._command_for(operation)
        if command is None:
            raise ObjectConflict(
                f"No write path for {operation.entity_kind}", connector_id=CONNECTOR_ID
            )
        await self.powershell.invoke(command)
        return OperationResult(
            op_id=operation.op_id,
            status=OperationStatus.SUCCEEDED,
            target_native_key=key,
            target_native_type=operation.entity_kind,
            post_state=dict(command.parameters),
        )

    async def _capture_pre_state(self, operation: WriteOperation) -> dict[str, Any] | None:
        preview = await self._preview_operation(operation)
        return preview.current_target_state

    async def _confirm_operation(
        self, operation: WriteOperation, result: OperationResult
    ) -> bool:
        """Re-read to confirm. Teams is eventually consistent; assuming otherwise lies."""
        if operation.entity_kind == "LicenseAssignment":
            principal = operation.payload.get("references", {}).get("principal_ref", "")
            sku = operation.payload.get("attributes", {}).get("sku_id")
            return sku in await self._current_licences(principal)

        command = self._command_for(operation)
        if command is None:
            return True
        current = await self._current_number_assignment(command)
        return current is not None and current.get("Identity") == command.parameters.get(
            "Identity"
        )

    def _invert_operation(
        self, operation: WriteOperation, result: OperationResult
    ) -> WriteOperation | None:
        if operation.entity_kind == "E164Number":
            return operation.model_copy(
                update={
                    "op_id": f"rollback:{operation.op_id}",
                    "verb": WriteVerb.UNASSIGN,
                    "idempotency_key": f"rollback:{operation.idempotency_key}",
                    "description": f"Release {operation.payload.get('natural_key')}",
                }
            )
        if operation.entity_kind == "LicenseAssignment":
            return operation.model_copy(
                update={
                    "op_id": f"rollback:{operation.op_id}",
                    "verb": WriteVerb.UNASSIGN,
                    "idempotency_key": f"rollback:{operation.idempotency_key}",
                    "description": f"Unassign {operation.payload.get('natural_key')}",
                }
            )
        return None

    def target_ref_for(self, native_type: str, key: str, *, dry_run: bool) -> TargetRef:
        return TargetRef(
            platform=self.platform,
            instance_id=self.instance_id,
            native_type=native_type,
            native_key=key,
            dry_run=dry_run,
        )


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _str(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _utc(value: Any) -> Any:
    from datetime import UTC, datetime

    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _e164_from_line_uri(line_uri: str | None) -> str | None:
    """``tel:+442071838750;ext=8750`` -> ``+442071838750``."""
    if not line_uri:
        return None
    value = line_uri.strip()
    if value.lower().startswith("tel:"):
        value = value[4:]
    value = value.split(";", 1)[0]
    return value if value.startswith("+") else None
