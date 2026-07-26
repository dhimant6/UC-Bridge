"""Avaya Aura connector (Extract).

Reads Communication Manager via SAT screens and System Manager via REST, and
maps Avaya's own concepts onto the canonical model explicitly rather than by
analogy:

* **COR/COS -> CallingPermission.** A Class of Restriction is a numbered
  restriction matrix, not an ordered partition list. The mapping keeps the
  number and the restriction summary and marks the result DEGRADED, because the
  target's permission model cannot express "COR 1 may not call COR 7".
* **Coverage path -> ForwardingRule chain.** A coverage path is an ordered set
  of points with per-criterion triggers (busy, don't answer, all). It becomes
  several forwarding rules, and the "number of rings" and inside/outside
  distinction usually do not survive.
* **Vectors -> AutoAttendant.** Marked DEGRADED at best; vector logic is
  programmatic and the canonical model is declarative by design.

SAT is Extract-only here, enforced by a read-only verb allow-list in the
session rather than by convention.
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
from ucm_bridge.canonical.callhandling import (
    ForwardCondition,
    ForwardingRule,
    HuntGroup,
)
from ucm_bridge.canonical.dialplan import CallingPermission, PermissionClass
from ucm_bridge.canonical.endpoints import Device, DeviceType, Line, SignallingProtocol
from ucm_bridge.canonical.identity import User
from ucm_bridge.canonical.numbering import Extension
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
from ucm_bridge.vendor.sat import (
    SatSession,
    form_to_dict,
    parse_sat_form,
    parse_sat_table,
)

CONNECTOR_ID = "avaya-aura"
CONNECTOR_VERSION = "0.1.0"
SAT_VERIFIED_ON = date(2026, 7, 26)

#: Avaya set types with no SIP path onto a cloud target.
LEGACY_SET_TYPES: frozenset[str] = frozenset(
    {"2420", "2410", "6408D+", "6416D+", "6424D+", "8410D", "8434D", "CALLR-INT"}
)
ANALOGUE_SET_TYPES: frozenset[str] = frozenset({"ANALOG", "ANALOGUE", "500", "2500"})


class AvayaAuraConnector(Connector):
    """Extract a Communication Manager / System Manager estate."""

    connector_id: ClassVar[str] = CONNECTOR_ID
    platform: ClassVar[Platform] = Platform.AVAYA_AURA

    def __init__(
        self,
        *,
        sat: SatSession,
        smgr: RestTransport | None = None,
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
                path=f"avaya/{instance_id}",
                kind=CredentialKind.SSH_KEY,
                scope=CredentialScope.READ_ONLY,
            ),
            credentials=credentials or CredentialBroker([]),
            **kwargs,
        )
        self.sat = sat
        self.smgr = smgr
        self._cassette_is_synthetic = cassette_is_synthetic

    # ------------------------------------------------------------------ #
    # Manifest
    # ------------------------------------------------------------------ #

    def synthetic_cassette_names(self) -> list[str]:
        return ["avaya-aura"] if self._cassette_is_synthetic else []

    def capabilities(self) -> CapabilityManifest:
        return CapabilityManifest(
            connector_id=CONNECTOR_ID,
            connector_version=CONNECTOR_VERSION,
            platform=self.platform,
            display_name="Avaya Aura (CM + System Manager)",
            api_surfaces=[
                APISurface(
                    name="CM SAT",
                    transport="SSH terminal",
                    verified_at=SAT_VERIFIED_ON,
                    verification_method=(
                        "Screen layout rules (column-separated labels, ':' and '?' "
                        "terminators, 'Page N of M' headers) implemented as a structural "
                        "parser and tested against representative screens. Individual field "
                        "names vary by CM release and are read, not assumed."
                    ),
                    notes="Read-only verbs only: display, list, status.",
                ),
                APISurface(
                    name="System Manager REST",
                    transport="REST",
                    notes=(
                        "Endpoint paths NOT verified. The SMGR user API differs materially "
                        "between releases and must be checked against the target deployment."
                    ),
                ),
            ],
            entities=[
                EntityCapability(
                    entity_kind="Extension",
                    can_extract=True,
                    api_surface="CM SAT",
                    required_permissions=["SAT read-only login"],
                ),
                EntityCapability(
                    entity_kind="Line",
                    can_extract=True,
                    api_surface="CM SAT",
                    required_permissions=["SAT read-only login"],
                ),
                EntityCapability(
                    entity_kind="Device",
                    can_extract=True,
                    api_surface="CM SAT",
                    known_gaps=["Button layout beyond page 1 is not read in this phase"],
                    required_permissions=["SAT read-only login"],
                ),
                EntityCapability(
                    entity_kind="CallingPermission",
                    can_extract=True,
                    api_surface="CM SAT",
                    fidelity_notes=(
                        "COR is a restriction matrix, not an ordered partition list. Always "
                        "DEGRADED."
                    ),
                    required_permissions=["SAT read-only login"],
                ),
                EntityCapability(
                    entity_kind="ForwardingRule",
                    can_extract=True,
                    api_surface="CM SAT",
                    fidelity_notes="Derived from coverage paths; ring counts rarely survive.",
                    required_permissions=["SAT read-only login"],
                ),
                EntityCapability(
                    entity_kind="HuntGroup",
                    can_extract=True,
                    api_surface="CM SAT",
                    required_permissions=["SAT read-only login"],
                ),
                EntityCapability(
                    entity_kind="User",
                    can_extract=True,
                    api_surface="System Manager REST",
                    known_gaps=["Communication profiles are not fully mapped in this phase"],
                    required_permissions=["SMGR read-only role"],
                ),
            ],
            credential_requirements=[
                CredentialRequirement(
                    purpose="sat-ssh",
                    kind=CredentialKind.SSH_KEY,
                    minimum_scope=CredentialScope.READ_ONLY,
                    notes="A SAT login restricted to display/list/status.",
                ),
                CredentialRequirement(
                    purpose="smgr",
                    kind=CredentialKind.USERNAME_PASSWORD,
                    minimum_scope=CredentialScope.READ_ONLY,
                ),
            ],
            # A SAT session is a terminal, not an API: one command at a time.
            rate_limits=RateLimitPolicy(
                max_concurrent_requests=1,
                initial_backoff_seconds=2.0,
                max_attempts=3,
            ),
            eventual_consistency=EventualConsistencyPolicy(is_eventually_consistent=False),
            supports_dry_run=True,
            supports_rollback=False,
            air_gap_capable=True,
            notes="Extract-only. Avaya write support is not in scope.",
        )

    async def test_connection(self) -> ConnectionTestResult:
        try:
            screen = await self.sat.run("status system 1")
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
            granted_permissions=["SAT read-only login"],
            messages=[
                f"SAT responded with {len(screen.splitlines())} line(s)",
                "System Manager REST endpoints are unverified; user extraction is best-effort.",
            ],
        )

    @classmethod
    def natural_key_for(cls, entity: CanonicalEntity) -> str | None:
        if isinstance(entity, Extension):
            return entity.digits
        if isinstance(entity, Line):
            return entity.directory_number
        if isinstance(entity, Device):
            return entity.device_name
        if isinstance(entity, CallingPermission):
            return entity.name
        if isinstance(entity, HuntGroup):
            return entity.pilot_pattern
        if isinstance(entity, User):
            return entity.user_principal_name
        return None

    # ------------------------------------------------------------------ #
    # Extract
    # ------------------------------------------------------------------ #

    async def _extract_batches(self, request: ExtractRequest) -> AsyncIterator[ExtractBatch]:
        wanted = set(
            request.entity_kinds
            or ["CallingPermission", "Extension", "Line", "Device", "ForwardingRule",
                "HuntGroup", "User"]
        )
        entities: list[CanonicalEntity] = []
        warnings: list[str] = []

        stations = parse_sat_table(await self.sat.run("list station"))

        if "CallingPermission" in wanted:
            entities.extend(await self._extract_cors(stations))

        for row in stations.rows:
            extension = row.get("Ext", "").strip()
            if not extension:
                continue
            form = parse_sat_form(await self.sat.run(f"display station {extension}"))
            native = form_to_dict(form)

            if "Extension" in wanted:
                entities.append(self._extension(extension, form, native))
            if "Line" in wanted:
                entities.append(self._line(extension, form, native))
            if "Device" in wanted:
                device = self._device(extension, form, native, warnings)
                if device is not None:
                    entities.append(device)

        if "ForwardingRule" in wanted:
            entities.extend(await self._extract_coverage(stations))

        if "HuntGroup" in wanted:
            entities.extend(await self._extract_hunt_groups())

        if "User" in wanted and self.smgr is not None:
            entities.extend(await self._extract_smgr_users(warnings))

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

    # -- helpers --------------------------------------------------------- #

    def _source(self, native_type: str, key: str, record: dict[str, Any], *, api: str) -> SourceRef:
        return SourceRef(
            platform=self.platform,
            instance_id=self.instance_id,
            native_type=native_type,
            native_key=key,
            native_attributes=record,
            api_surface=api,
        )

    def _id(self, kind: str, key: str) -> str:
        return CanonicalEntity.mint_canonical_id(
            self.platform, kind, key, instance_id=self.instance_id
        )

    def _extension(self, digits: str, form: Any, native: dict[str, Any]) -> Extension:
        return Extension(
            canonical_id=self._id("Extension", digits),
            display_name=digits,
            digits=digits,
            description=form.get("Name"),
            owner_ref=None,
            source_ref=self._source("station", digits, native, api="SAT:display station"),
            fidelity=assess_mapping(
                native,
                {"Extension", "Name", "Type", "Port", "COR", "COS", "TN"},
                assessed_by=CONNECTOR_ID,
                entity_label="Extension",
                lossless_rationale="An extension is its digits.",
                extra_degraded=[
                    DegradedAttribute(
                        attribute="e164_ref",
                        reason="CM stores no external number on the station",
                        target_behaviour=(
                            "The E.164 mapping is derived by the normalisation engine, not "
                            "read from the source."
                        ),
                    )
                ],
            ),
        )

    def _line(self, digits: str, form: Any, native: dict[str, Any]) -> Line:
        cor = form.get("COR")
        return Line(
            canonical_id=self._id("Line", digits),
            display_name=form.get("Name") or digits,
            directory_number=digits,
            label=form.get("Name"),
            alerting_name=form.get("Name"),
            extension_ref=self._id("Extension", digits),
            calling_permission_ref=self._id("CallingPermission", f"COR {cor}") if cor else None,
            source_ref=self._source("station", digits, native, api="SAT:display station"),
            fidelity=assess_mapping(
                native,
                {"Extension", "Name", "Type", "Port", "COR"},
                assessed_by=CONNECTOR_ID,
                entity_label="Line",
                lossless_rationale="Number and display name carry across.",
                manual_effort_minutes=5,
            ),
        )

    def _device(
        self, digits: str, form: Any, native: dict[str, Any], warnings: list[str]
    ) -> Device | None:
        set_type = (form.get("Type") or "").strip()
        port = (form.get("Port") or "").strip()
        if not set_type:
            return None

        upper = set_type.upper()
        is_analogue = upper in ANALOGUE_SET_TYPES
        is_legacy = upper in LEGACY_SET_TYPES

        degraded: list[DegradedAttribute] = []
        if is_analogue:
            degraded.append(
                DegradedAttribute(
                    attribute="device_type",
                    reason="analogue station on a CM port board",
                    source_value=set_type,
                    target_behaviour=(
                        "Analogue stations do not migrate to a cloud PBX. This one needs an "
                        "ATA, a retained gateway, or a service withdrawal decision."
                    ),
                )
            )
            warnings.append(f"Station {digits} is an analogue set ({set_type}).")
        elif is_legacy:
            degraded.append(
                DegradedAttribute(
                    attribute="model",
                    reason=f"{set_type} is a digital set with no SIP registration path",
                    source_value=set_type,
                    target_behaviour="The handset must be replaced before cutover.",
                )
            )

        return Device(
            canonical_id=self._id("Device", f"station-{digits}"),
            display_name=form.get("Name") or digits,
            device_name=f"station-{digits}",
            vendor="Avaya",
            model=set_type,
            device_type=(
                DeviceType.ANALOGUE
                if is_analogue
                else DeviceType.HARD_PHONE
            ),
            protocol=SignallingProtocol.ANALOGUE if is_analogue else SignallingProtocol.PROPRIETARY,
            analogue_port=port if is_analogue else None,
            line_refs=[self._id("Line", digits)],
            replacement_required=is_analogue or is_legacy,
            source_ref=self._source("station", digits, native, api="SAT:display station"),
            fidelity=assess_mapping(
                native,
                {"Extension", "Name", "Type", "Port"},
                assessed_by=CONNECTOR_ID,
                entity_label="Device",
                lossless_rationale="Set type and port carry across.",
                extra_degraded=degraded,
                manual_effort_minutes=20 if degraded else None,
            ),
        )

    async def _extract_cors(self, stations: Any) -> list[CallingPermission]:
        """One CallingPermission per distinct COR seen on a station."""
        numbers = sorted({row.get("COR", "").strip() for row in stations.rows} - {""})
        permissions: list[CallingPermission] = []
        for number in numbers:
            name = f"COR {number}"
            record = {"COR": number, "source": "list station"}
            permissions.append(
                CallingPermission(
                    canonical_id=self._id("CallingPermission", name),
                    display_name=name,
                    name=name,
                    permission_class=PermissionClass.CUSTOM,
                    derived_from=f"Avaya:COR-{number}",
                    source_ref=self._source("cor", name, record, api="SAT:list station"),
                    fidelity=assess_mapping(
                        record,
                        {"COR", "source"},
                        assessed_by=CONNECTOR_ID,
                        entity_label="COR",
                        lossless_rationale="A COR always degrades; this is never reached.",
                        extra_degraded=[
                            DegradedAttribute(
                                attribute="permitted_partition_refs",
                                reason=(
                                    "a COR is a restriction matrix between numbered classes, "
                                    "not an ordered list of reachable partitions"
                                ),
                                source_value=name,
                                target_behaviour=(
                                    "The target gets a named permission with no members until "
                                    "a mapping rule assigns them. Restrictions of the form "
                                    "'COR 1 may not call COR 7' have no target equivalent and "
                                    "must be re-expressed by hand."
                                ),
                            )
                        ],
                        manual_effort_minutes=30,
                    ),
                )
            )
        return permissions

    async def _extract_coverage(self, stations: Any) -> list[ForwardingRule]:
        """Coverage paths become forwarding rules, one per trigger criterion."""
        paths = sorted({row.get("Cv1", "").strip() for row in stations.rows} - {""})
        rules: list[ForwardingRule] = []

        for path in paths:
            form = parse_sat_form(await self.sat.run(f"display coverage path {path}"))
            native = form_to_dict(form)
            rings = form.get_int("Number of Rings")

            for label, condition in (
                ("Busy", ForwardCondition.BUSY),
                ("Don't Answer", ForwardCondition.NO_ANSWER),
                ("All", ForwardCondition.ALWAYS),
            ):
                if form.get_bool(label) is not True:
                    continue
                key = f"coverage-{path}-{condition.value}"
                rules.append(
                    ForwardingRule(
                        canonical_id=self._id("ForwardingRule", key),
                        display_name=f"Coverage path {path}: {condition.value}",
                        condition=condition,
                        enabled=True,
                        to_voicemail=True,
                        delay_seconds=(rings * 6) if rings else None,
                        source_ref=self._source(
                            "coverage-path", key, native, api="SAT:display coverage path"
                        ),
                        fidelity=assess_mapping(
                            native,
                            {"Coverage Path Number", "Number of Rings", "Busy", "Don't Answer",
                             "All", "Active", "Hunt after Coverage"},
                            assessed_by=CONNECTOR_ID,
                            entity_label="CoveragePath",
                            lossless_rationale="Coverage always degrades; never reached.",
                            extra_degraded=[
                                DegradedAttribute(
                                    attribute="applies_to_internal",
                                    reason=(
                                        "CM applies coverage criteria separately to inside and "
                                        "outside calls; the target has one rule per condition"
                                    ),
                                    target_behaviour=(
                                        "The inside/outside distinction is lost. Where they "
                                        "differed at source, one behaviour is chosen and the "
                                        "other silently changes."
                                    ),
                                )
                            ],
                            manual_effort_minutes=10,
                        ),
                    )
                )
        return rules

    async def _extract_hunt_groups(self) -> list[HuntGroup]:
        table = parse_sat_table(await self.sat.run("list hunt-group"))
        groups: list[HuntGroup] = []
        for row in table.rows:
            number = row.get("Grp No", "").strip() or row.get("Group", "").strip()
            extension = row.get("Grp Ext", "").strip() or row.get("Ext", "").strip()
            if not extension:
                continue
            name = row.get("Grp Name", "").strip() or f"Hunt group {number}"
            groups.append(
                HuntGroup(
                    canonical_id=self._id("HuntGroup", extension),
                    display_name=name,
                    name=name,
                    pilot_pattern=extension,
                    source_ref=self._source(
                        "hunt-group", extension, dict(row), api="SAT:list hunt-group"
                    ),
                    fidelity=assess_mapping(
                        dict(row),
                        {"Grp No", "Group", "Grp Ext", "Ext", "Grp Name"},
                        assessed_by=CONNECTOR_ID,
                        entity_label="HuntGroup",
                        lossless_rationale="Hunt groups always degrade; never reached.",
                        extra_degraded=[
                            DegradedAttribute(
                                attribute="line_group_refs",
                                reason="membership needs a per-group display, not the list screen",
                                target_behaviour=(
                                    "The group is created empty; agents must be added from a "
                                    "later per-group pass or by hand."
                                ),
                            )
                        ],
                        manual_effort_minutes=15,
                    ),
                )
            )
        return groups

    async def _extract_smgr_users(self, warnings: list[str]) -> list[User]:
        """Users from System Manager.

        The SMGR REST path is unverified, so a failure here is reported as a
        warning and the run continues with CM data rather than aborting.
        """
        assert self.smgr is not None
        try:
            response = await self.smgr.request(
                RestRequest(method=HttpMethod.GET, path="/SMGR/administrativeusers/users")
            )
        except Exception as exc:
            warnings.append(
                f"System Manager user extraction failed ({exc}). The SMGR REST path is "
                "unverified and differs between releases; CM data was still collected."
            )
            return []

        users: list[User] = []
        for record in (response.body or {}).get("users", []):
            login = str(record.get("loginName") or "").strip()
            if not login:
                continue
            users.append(
                User(
                    canonical_id=self._id("User", login),
                    display_name=str(record.get("displayName") or login),
                    user_principal_name=login,
                    given_name=record.get("firstName"),
                    surname=record.get("lastName"),
                    email=record.get("email"),
                    primary_extension_ref=(
                        self._id("Extension", str(record["extension"]))
                        if record.get("extension")
                        else None
                    ),
                    telephony_enabled=bool(record.get("extension")),
                    source_ref=self._source(
                        "smgr-user", login, dict(record), api="SMGR:/administrativeusers/users"
                    ),
                    fidelity=assess_mapping(
                        dict(record),
                        {"loginName", "displayName", "firstName", "lastName", "email",
                         "extension"},
                        assessed_by=CONNECTOR_ID,
                        entity_label="User",
                        lossless_rationale="Identity attributes carry across.",
                        extra_degraded=[
                            DegradedAttribute(
                                attribute="policy_refs",
                                reason="Aura communication profiles are not mapped in this phase",
                                target_behaviour=(
                                    "The user arrives without an entitlement profile and "
                                    "inherits the target default."
                                ),
                            )
                        ],
                        manual_effort_minutes=5,
                    ),
                )
            )
        return users

    # ------------------------------------------------------------------ #
    # Apply - not in scope
    # ------------------------------------------------------------------ #

    async def _preview_operation(self, operation: WriteOperation) -> OperationPreview:
        raise NotImplementedError(
            "The Avaya connector is Extract-only. Writing to CM means issuing 'change' "
            "commands through a terminal session, which needs a different safety model "
            "than an API and is not in scope."
        )

    async def _execute_operation(self, operation: WriteOperation) -> OperationResult:
        raise NotImplementedError("The Avaya connector is Extract-only.")
