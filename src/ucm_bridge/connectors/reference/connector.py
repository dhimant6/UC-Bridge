"""Reference connector for the fake MemoryPBX platform.

Its job is to prove the contract, not to be useful: it round-trips
``User``, ``Line``, ``E164Number``, and ``EmergencyLocation`` through the
canonical model in both directions, and it exercises every guardrail in
:class:`~ucm_bridge.connectors.base.Connector`.

Read it as the worked example a real connector should follow, in particular:

* fidelity is asserted explicitly per entity, with named degraded attributes -
  nothing is claimed LOSSLESS without a reason;
* unmapped source fields are retained in ``source_ref.native_attributes`` and
  declared in the assessment, rather than dropped;
* ``_preview_operation`` decides ``would_change`` by comparing the desired
  record to the current one, which is what makes re-running a plan a no-op;
* ``_capture_pre_state`` and ``_invert_operation`` are implemented, so rollback
  is real rather than declared.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import date
from typing import Any, ClassVar

from ucm_bridge.canonical.base import (
    CanonicalEntity,
    DegradedAttribute,
    FidelityAssessment,
    Platform,
    SourceRef,
    TargetRef,
)
from ucm_bridge.canonical.endpoints import Line
from ucm_bridge.canonical.identity import User
from ucm_bridge.canonical.numbering import (
    E164Number,
    NumberAssignmentKind,
    NumberAssignmentState,
    NumberType,
)
from ucm_bridge.canonical.policy import CivicAddress, EmergencyLocation, NetworkIdentifiers
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
from ucm_bridge.connectors.errors import (
    ConnectorError,
    ObjectConflict,
    TransientPlatformError,
)
from ucm_bridge.connectors.reference.platform import MemoryPBXEstate, MemoryPBXFault

CONNECTOR_ID = "reference-memorypbx"
CONNECTOR_VERSION = "0.1.0"

#: Which canonical kind lives in which MemoryPBX collection.
_COLLECTION_FOR_KIND = {
    "User": "users",
    "Line": "lines",
    "E164Number": "numbers",
    "EmergencyLocation": "sites",
}


class MemoryPBXConnector(Connector):
    """Extract and Apply against an in-memory fake estate."""

    connector_id: ClassVar[str] = CONNECTOR_ID
    platform: ClassVar[Platform] = Platform.REFERENCE_MEMORYPBX

    def __init__(
        self,
        estate: MemoryPBXEstate,
        *,
        tenant_id: str,
        credential_ref: CredentialRef | None = None,
        credentials: CredentialBroker | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            instance_id=estate.instance_id,
            tenant_id=tenant_id,
            credential_ref=credential_ref
            or CredentialRef(
                provider="env",
                path="memorypbx",
                kind=CredentialKind.API_TOKEN,
                scope=CredentialScope.READ_ONLY,
            ),
            credentials=credentials or CredentialBroker([]),
            **kwargs,
        )
        self.estate = estate

    # ------------------------------------------------------------------ #
    # Capabilities / connectivity
    # ------------------------------------------------------------------ #

    def capabilities(self) -> CapabilityManifest:
        verbs = [WriteVerb.CREATE, WriteVerb.UPDATE, WriteVerb.DELETE]
        return CapabilityManifest(
            connector_id=CONNECTOR_ID,
            connector_version=CONNECTOR_VERSION,
            platform=self.platform,
            display_name="MemoryPBX (reference)",
            api_surfaces=[
                APISurface(
                    name="MemoryPBX in-process API",
                    version="1",
                    transport="in-process",
                    verified_at=date(2026, 7, 26),
                    verification_method="the API is defined in this repository",
                )
            ],
            entities=[
                EntityCapability(
                    entity_kind="EmergencyLocation",
                    can_extract=True,
                    can_apply=True,
                    supported_verbs=verbs,
                    fidelity_notes=(
                        "Civic address and ELIN carry across. Locations without a validation "
                        "authority are reported DEGRADED, because an unvalidated dispatchable "
                        "address is not evidence of anything."
                    ),
                    required_permissions=["sites:read", "sites:write"],
                ),
                EntityCapability(
                    entity_kind="E164Number",
                    can_extract=True,
                    can_apply=True,
                    supported_verbs=verbs,
                    fidelity_notes="Number, type, site, and assignment carry across intact.",
                    required_permissions=["numbers:read", "numbers:write"],
                ),
                EntityCapability(
                    entity_kind="User",
                    can_extract=True,
                    can_apply=True,
                    supported_verbs=verbs,
                    known_gaps=[
                        "cor_class has no canonical home until CallingPermission is extracted",
                        "extension is carried on Line, not on User",
                    ],
                    required_permissions=["users:read", "users:write"],
                ),
                EntityCapability(
                    entity_kind="Line",
                    can_extract=True,
                    can_apply=True,
                    supported_verbs=verbs,
                    known_gaps=[
                        "shared_with requires SharedLineAppearance, which this connector "
                        "does not yet extract"
                    ],
                    required_permissions=["lines:read", "lines:write"],
                ),
            ],
            credential_requirements=[
                CredentialRequirement(
                    purpose="memorypbx-api",
                    kind=CredentialKind.API_TOKEN,
                    minimum_scope=CredentialScope.READ_ONLY,
                )
            ],
            rate_limits=RateLimitPolicy(
                max_concurrent_requests=4,
                max_attempts=3,
                initial_backoff_seconds=0.01,
                max_backoff_seconds=0.05,
            ),
            eventual_consistency=EventualConsistencyPolicy(
                is_eventually_consistent=self.estate.replication_delay_reads > 0,
                confirm_poll_interval_seconds=0.01,
                confirm_poll_timeout_seconds=0.2,
                confirm_required_for_kinds=(
                    sorted(_COLLECTION_FOR_KIND) if self.estate.replication_delay_reads > 0 else []
                ),
            ),
            supports_dry_run=True,
            supports_rollback=True,
            air_gap_capable=True,
        )

    async def test_connection(self) -> ConnectionTestResult:
        return ConnectionTestResult(
            connector_id=CONNECTOR_ID,
            reachable=True,
            authenticated=True,
            scope=self.credential_ref.scope.value,
            platform_version="MemoryPBX/1",
            granted_permissions=["users:read", "lines:read", "numbers:read", "sites:read"],
            messages=[f"estate {self.estate.instance_id}: {self.estate.total_records()} records"],
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
        if isinstance(entity, Line):
            return f"{entity.site_code}:{entity.directory_number}"
        return None

    # ------------------------------------------------------------------ #
    # Extract
    # ------------------------------------------------------------------ #

    async def _extract_batches(self, request: ExtractRequest) -> AsyncIterator[ExtractBatch]:
        wanted = set(request.entity_kinds or _COLLECTION_FOR_KIND)

        # Ordered so that referenced entities are minted before their referrers.
        # The fourth element is the record's native key field, used to impose a
        # deterministic emission order: two discovery runs of an unchanged estate
        # must produce byte-identical snapshots.
        builders = (
            ("EmergencyLocation", "sites", self._site_to_location, "site_code"),
            ("E164Number", "numbers", self._number_to_e164, "e164"),
            ("User", "users", self._user_to_canonical, "username"),
            ("Line", "lines", self._line_to_canonical, "line_id"),
        )

        emitted: list[CanonicalEntity] = []
        warnings: list[str] = []

        for kind, collection, builder, key_field in builders:
            if kind not in wanted:
                continue
            for record in sorted(
                self.estate.list_records(collection), key=lambda r: str(r[key_field])
            ):
                if request.site_codes and record.get("site_code") not in request.site_codes:
                    continue
                entity = builder(record)
                emitted.append(entity)

        for emitted_entity in emitted:
            if isinstance(emitted_entity, User) and emitted_entity.primary_number_ref is None:
                warnings.append(
                    f"User {emitted_entity.user_principal_name} has no external number; this is "
                    "a blocker for any target that requires E.164 assignment."
                )

        # Page the results to exercise the streaming contract.
        page_size = max(1, request.page_size)
        pages = [emitted[i : i + page_size] for i in range(0, len(emitted), page_size)] or [[]]
        for index, page in enumerate(pages):
            is_final = index == len(pages) - 1
            yield ExtractBatch(
                run_id=request.run_id,
                sequence=index,
                entities=page,
                warnings=warnings if is_final else [],
                is_final=is_final,
                cursor=None if is_final else f"page:{index + 1}",
            )

    def _source_ref(self, native_type: str, native_key: str, record: dict[str, Any]) -> SourceRef:
        return SourceRef(
            platform=self.platform,
            instance_id=self.instance_id,
            native_type=native_type,
            native_key=native_key,
            native_attributes=dict(record),
            api_surface=f"MemoryPBX:list_{native_type}",
        )

    def _id(self, kind: str, native_key: str) -> str:
        return CanonicalEntity.mint_canonical_id(
            self.platform, kind, native_key, instance_id=self.instance_id
        )

    def _site_to_location(self, record: dict[str, Any]) -> EmergencyLocation:
        site_code = record["site_code"]
        elin = record.get("elin")
        validated = bool(record.get("validated_by") and record.get("validated_on"))

        address = CivicAddress(
            country=record["country"],
            house_number=record.get("house_number"),
            street_name=record.get("street"),
            city=record.get("city"),
            postal_code=record.get("postal_code"),
            sub_unit=record.get("sub_unit"),
        )

        degraded: list[DegradedAttribute] = []
        if not validated:
            degraded.append(
                DegradedAttribute(
                    attribute="is_validated",
                    reason="the source holds no validation authority or date for this address",
                    target_behaviour=(
                        "The address will be created unvalidated. Emergency calls may be routed "
                        "to the wrong PSAP until it is validated with the carrier."
                    ),
                )
            )
        if not address.sub_unit:
            degraded.append(
                DegradedAttribute(
                    attribute="civic_address.sub_unit",
                    reason="no floor, wing, or room recorded at source",
                    target_behaviour=(
                        "Responders receive a building address with no internal location."
                    ),
                )
            )

        fidelity = (
            FidelityAssessment.lossless(
                "Civic address, ELIN, and network identifiers all have direct equivalents.",
                assessed_by=CONNECTOR_ID,
            )
            if not degraded
            else FidelityAssessment.degraded(
                "Emergency location carries across with gaps that affect dispatch accuracy.",
                degraded,
                assessed_by=CONNECTOR_ID,
            )
        )

        return EmergencyLocation(
            canonical_id=self._id("EmergencyLocation", site_code),
            display_name=record.get("name"),
            name=record.get("name") or site_code,
            site_code=site_code,
            civic_address=address,
            elin_number_ref=self._id("E164Number", elin) if elin else None,
            is_validated=validated,
            validation_authority=record.get("validated_by") if validated else None,
            validated_at=(
                _as_datetime(record.get("validated_on")) if validated else None
            ),
            network_identifiers=NetworkIdentifiers(subnets=list(record.get("subnets") or [])),
            supports_dynamic_location=bool(record.get("dynamic_location")),
            source_ref=self._source_ref("sites", site_code, record),
            fidelity=fidelity,
        )

    def _number_to_e164(self, record: dict[str, Any]) -> E164Number:
        e164 = record["e164"]
        assigned_to = record.get("assigned_to")
        number_type = NumberType(record.get("number_type", "DID"))

        return E164Number(
            canonical_id=self._id("E164Number", e164),
            display_name=e164,
            e164=e164,
            number_type=number_type,
            site_code=record.get("site_code"),
            assignment_state=(
                NumberAssignmentState.ASSIGNED if assigned_to else NumberAssignmentState.UNASSIGNED
            ),
            assignment_kind=(
                NumberAssignmentKind(record.get("assignment_kind") or "USER")
                if assigned_to
                else NumberAssignmentKind.UNASSIGNED
            ),
            assigned_to_ref=self._id("User", assigned_to) if assigned_to else None,
            emergency_location_ref=(
                self._id("EmergencyLocation", record["site_code"])
                if record.get("site_code")
                else None
            ),
            source_ref=self._source_ref("numbers", e164, record),
            fidelity=FidelityAssessment.lossless(
                "Number, type, site, assignment state, and emergency location all map directly.",
                assessed_by=CONNECTOR_ID,
            ),
        )

    def _user_to_canonical(self, record: dict[str, Any]) -> User:
        username = record["username"]
        did = record.get("did")

        degraded = [
            DegradedAttribute(
                attribute="policy_refs",
                reason="cor_class has no canonical home until CallingPermission is extracted",
                source_value=str(record.get("cor_class")),
                target_behaviour=(
                    "The user is created without a calling permission and inherits the target's "
                    "global default, which may be more or less permissive than the source."
                ),
            )
        ]
        if not did:
            degraded.append(
                DegradedAttribute(
                    attribute="primary_number_ref",
                    reason="the user has an internal extension but no external number",
                    target_behaviour=(
                        "The user cannot be enabled for PSTN calling on a target that requires "
                        "E.164 assignment; they need a number or an internal-only disposition."
                    ),
                )
            )

        return User(
            canonical_id=self._id("User", username),
            display_name=f"{record.get('first_name', '')} {record.get('last_name', '')}".strip()
            or username,
            user_principal_name=username,
            email=record.get("email"),
            given_name=record.get("first_name"),
            surname=record.get("last_name"),
            department=record.get("department"),
            site_code=record.get("site_code"),
            enabled=bool(record.get("enabled", True)),
            telephony_enabled=bool(record.get("voice_enabled", False)),
            primary_number_ref=self._id("E164Number", did) if did else None,
            source_ref=self._source_ref("users", username, record),
            fidelity=FidelityAssessment.degraded(
                "User identity carries across; calling permission does not.",
                degraded,
                assessed_by=CONNECTOR_ID,
                manual_effort_minutes=5,
            ),
        )

    def _line_to_canonical(self, record: dict[str, Any]) -> Line:
        line_id = record["line_id"]
        owner = record.get("owner")
        did = record.get("did")
        shared_with = list(record.get("shared_with") or [])

        degraded: list[DegradedAttribute] = []
        if shared_with:
            degraded.append(
                DegradedAttribute(
                    attribute="shared_appearance_ref",
                    reason="this connector does not yet extract SharedLineAppearance",
                    source_value=", ".join(shared_with),
                    target_behaviour=(
                        "The line is created as a single appearance owned by one user. The other "
                        f"{len(shared_with)} appearance(s) are lost and must be rebuilt by hand."
                    ),
                )
            )

        fidelity = (
            FidelityAssessment.degraded(
                "Line carries across, but its shared appearances do not.",
                degraded,
                assessed_by=CONNECTOR_ID,
                manual_effort_minutes=15,
            )
            if degraded
            else FidelityAssessment.lossless(
                "Directory number, label, owner, button position, and DID all map directly.",
                assessed_by=CONNECTOR_ID,
            )
        )

        return Line(
            canonical_id=self._id("Line", line_id),
            display_name=record.get("label"),
            directory_number=record["extension"],
            site_code=record.get("site_code"),
            label=record.get("label"),
            line_index=int(record.get("button", 1)),
            owner_ref=self._id("User", owner) if owner else None,
            e164_ref=self._id("E164Number", did) if did else None,
            source_ref=self._source_ref("lines", line_id, record),
            fidelity=fidelity,
        )

    # ------------------------------------------------------------------ #
    # Apply
    # ------------------------------------------------------------------ #

    def _target_record(self, operation: WriteOperation) -> tuple[str, str, dict[str, Any]]:
        """(collection, native key, desired record) for an operation."""
        collection = _COLLECTION_FOR_KIND[operation.entity_kind]
        attributes: dict[str, Any] = operation.payload.get("attributes", {})
        references: dict[str, Any] = operation.payload.get("references", {})
        key = operation.payload.get("natural_key")
        if not key:
            raise ObjectConflict(
                f"Operation {operation.op_id} has no natural_key; the planner could not derive "
                f"a target key for {operation.entity_kind}.",
                connector_id=CONNECTOR_ID,
            )

        # Rollback operations carry the target state directly rather than a
        # canonical payload: a DELETE needs no record, and a restore already has
        # the exact record captured before the write.
        if operation.verb is WriteVerb.DELETE:
            return collection, key, {}
        restore = operation.payload.get("restore_record")
        if restore is not None:
            return collection, key, dict(restore)

        builders = {
            "EmergencyLocation": _location_record,
            "E164Number": _number_record,
            "User": _user_record,
            "Line": _line_record,
        }
        return collection, key, builders[operation.entity_kind](attributes, references)

    async def _preview_operation(self, operation: WriteOperation) -> OperationPreview:
        collection, key, desired = self._target_record(operation)
        current = self.estate.get_record(collection, key)

        warnings: list[str] = []
        if operation.entity_kind == "EmergencyLocation" and not desired.get("validated_by"):
            warnings.append(
                f"Emergency location {key} will be written without a validation authority."
            )

        return OperationPreview(
            op_id=operation.op_id,
            verb=operation.verb,
            target_native_type=collection,
            target_native_key=key,
            api_call=f"MemoryPBX:upsert({collection}, {key!r})",
            current_target_state=current,
            proposed_state=desired,
            would_change=current != desired,
            warnings=warnings,
        )

    async def _capture_pre_state(self, operation: WriteOperation) -> dict[str, Any] | None:
        collection, key, _ = self._target_record(operation)
        return self.estate.get_record(collection, key)

    async def _execute_operation(self, operation: WriteOperation) -> OperationResult:
        collection, key, desired = self._target_record(operation)
        try:
            if operation.verb is WriteVerb.DELETE:
                self.estate.delete(collection, key)
                post: dict[str, Any] | None = None
            else:
                post = self.estate.upsert(collection, key, desired)
        except MemoryPBXFault as fault:
            raise _translate(fault) from fault

        return OperationResult(
            op_id=operation.op_id,
            status=OperationStatus.SUCCEEDED,
            target_native_key=key,
            target_native_type=collection,
            post_state=post,
        )

    async def _confirm_operation(
        self, operation: WriteOperation, result: OperationResult
    ) -> bool:
        collection, key, _ = self._target_record(operation)
        return self.estate.get_record(collection, key) is not None

    def _invert_operation(
        self, operation: WriteOperation, result: OperationResult
    ) -> WriteOperation | None:
        """Undo: restore the captured pre-state, or delete what we created."""
        if result.pre_state is None:
            return WriteOperation(
                op_id=f"rollback:{operation.op_id}",
                verb=WriteVerb.DELETE,
                entity_kind=operation.entity_kind,
                canonical_id=operation.canonical_id,
                idempotency_key=f"rollback:{operation.idempotency_key}",
                payload={
                    "attributes": {},
                    "references": {},
                    "natural_key": result.target_native_key,
                },
                site_code=operation.site_code,
                description=f"Delete {operation.entity_kind} {result.target_native_key} "
                "created by this run",
            )
        return WriteOperation(
            op_id=f"rollback:{operation.op_id}",
            verb=WriteVerb.UPDATE,
            entity_kind=operation.entity_kind,
            canonical_id=operation.canonical_id,
            idempotency_key=f"rollback:{operation.idempotency_key}",
            payload={
                "attributes": {},
                "references": {},
                "natural_key": result.target_native_key,
                "restore_record": result.pre_state,
            },
            site_code=operation.site_code,
            description=f"Restore prior state of {operation.entity_kind} "
            f"{result.target_native_key}",
        )

    def target_ref_for(self, collection: str, key: str, *, dry_run: bool) -> TargetRef:
        return TargetRef(
            platform=self.platform,
            instance_id=self.instance_id,
            native_type=collection,
            native_key=key,
            dry_run=dry_run,
            correlation_id=str(uuid.uuid4()),
        )


# --------------------------------------------------------------------------- #
# Canonical -> MemoryPBX record builders
# --------------------------------------------------------------------------- #


def _location_record(attributes: dict[str, Any], references: dict[str, Any]) -> dict[str, Any]:
    address = attributes.get("civic_address", {})
    return {
        "site_code": attributes["site_code"],
        "name": attributes.get("name"),
        "country": address.get("country"),
        "house_number": address.get("house_number"),
        "street": address.get("street_name"),
        "city": address.get("city"),
        "postal_code": address.get("postal_code"),
        "sub_unit": address.get("sub_unit"),
        "elin": references.get("elin_number_ref"),
        "validated_by": attributes.get("validation_authority"),
        "validated_on": _date_part(attributes.get("validated_at")),
        "subnets": list((attributes.get("network_identifiers") or {}).get("subnets") or []),
        "dynamic_location": bool(attributes.get("supports_dynamic_location", False)),
    }


def _number_record(attributes: dict[str, Any], references: dict[str, Any]) -> dict[str, Any]:
    assigned_to = references.get("assigned_to_ref")
    return {
        "e164": attributes["e164"],
        "site_code": attributes.get("site_code"),
        "number_type": attributes.get("number_type", "DID"),
        "assigned_to": assigned_to,
        "assignment_kind": attributes.get("assignment_kind") if assigned_to else None,
    }


def _user_record(attributes: dict[str, Any], references: dict[str, Any]) -> dict[str, Any]:
    return {
        "username": attributes["user_principal_name"],
        "first_name": attributes.get("given_name"),
        "last_name": attributes.get("surname"),
        "email": attributes.get("email"),
        "department": attributes.get("department"),
        "site_code": attributes.get("site_code"),
        "did": references.get("primary_number_ref"),
        "enabled": bool(attributes.get("enabled", True)),
        "voice_enabled": bool(attributes.get("telephony_enabled", False)),
    }


def _line_record(attributes: dict[str, Any], references: dict[str, Any]) -> dict[str, Any]:
    site_code = attributes.get("site_code")
    extension = attributes["directory_number"]
    return {
        "line_id": f"{site_code}:{extension}",
        "extension": extension,
        "site_code": site_code,
        "label": attributes.get("label"),
        "owner": references.get("owner_ref"),
        "did": references.get("e164_ref"),
        "button": int(attributes.get("line_index", 1)),
    }


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _translate(fault: MemoryPBXFault) -> ConnectorError:
    if fault.code == "THROTTLED":
        return TransientPlatformError(
            str(fault), connector_id=CONNECTOR_ID, details={"code": fault.code}
        )
    return ObjectConflict(str(fault), connector_id=CONNECTOR_ID, details={"code": fault.code})


def _as_datetime(value: Any) -> Any:
    from datetime import UTC, datetime

    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value)).replace(tzinfo=UTC)


def _date_part(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)[:10]
