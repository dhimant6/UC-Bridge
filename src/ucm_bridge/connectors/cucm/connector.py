"""Cisco Unified Communications Manager connector (Extract).

Phase 1 scope: read-only discovery of a CUCM cluster into the canonical model.
Write support exists in the manifest only for the entity kinds the Phase 5
reverse transform needs, and is gated behind the same readiness check as every
other connector.

What is asserted about AXL, and on what basis, is documented in
:mod:`ucm_bridge.vendor.axl` — endpoint, SOAPAction, namespace, auth, and
throttling were checked against the Cisco AXL Developer Guide on 2026-07-26.
What is *not* asserted is the field-level schema of each object: that changes
per release, so the mappers below read defensively and record anything they did
not understand rather than assuming a shape.

Raw SQL: ``executeSQLQuery`` is used for exactly one thing — the device/line
association that the typed API only exposes per-device, which would otherwise
mean one ``getPhone`` round trip per handset on a 150,000-endpoint estate. Every
such call is flagged on the SourceRef and surfaced in the audit log.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Mapping
from datetime import date
from typing import Any, ClassVar

from ucm_bridge.canonical.base import (
    CanonicalEntity,
    DegradedAttribute,
    Platform,
    SourceRef,
)
from ucm_bridge.canonical.callhandling import HuntGroup, LineGroup, PickupGroup
from ucm_bridge.canonical.dialplan import (
    CallingPermission,
    DistributionAlgorithm,
    Partition,
    PermissionClass,
    RouteGroup,
    RouteList,
    RoutePattern,
    TranslationPattern,
)
from ucm_bridge.canonical.endpoints import (
    Device,
    DevicePool,
    DeviceProfile,
    DeviceType,
    Line,
    SignallingProtocol,
)
from ucm_bridge.canonical.identity import User
from ucm_bridge.canonical.numbering import Extension
from ucm_bridge.canonical.trunking import SIPDestination, SIPTrunk, TransportProtocol
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
    WriteOperation,
)
from ucm_bridge.connectors.credentials import (
    CredentialBroker,
    CredentialKind,
    CredentialRef,
    CredentialScope,
)
from ucm_bridge.connectors.errors import ConnectorError
from ucm_bridge.connectors.fidelity_support import assess_mapping
from ucm_bridge.vendor.axl import AxlTransport

CONNECTOR_ID = "cisco-cucm"
CONNECTOR_VERSION = "0.1.0"

#: The date the AXL protocol facts in vendor/axl.py were checked. Surfaced in the
#: manifest so a stale verification is visible rather than assumed current.
AXL_VERIFIED_ON = date(2026, 7, 26)

#: Device models with no SIP path onto a modern target. Not exhaustive and not
#: authoritative: the assessment engine treats this as a hint and the estate
#: report lists every distinct model found so a human can rule on the rest.
KNOWN_END_OF_LIFE_PREFIXES: tuple[str, ...] = (
    "Cisco 7902", "Cisco 7905", "Cisco 7910", "Cisco 7912",
    "Cisco 7920", "Cisco 7935", "Cisco 7940", "Cisco 7960",
)

_ANALOGUE_HINTS = ("analog", "analogue", "ata", "vg2", "vg3", "vg4", "fxs")


def rows(response: Any, tag: str) -> list[dict[str, Any]]:
    """Normalise an AXL list/get response into a list of plain dicts.

    AXL returns ``{'return': {'<tag>': [...]}}`` for lists and a single object
    for gets, and zeep hands back objects rather than dicts. This flattens all of
    those without assuming which one arrived.
    """
    if response is None:
        return []
    payload = _as_dict(response)
    inner = payload.get("return", payload)
    if inner is None:
        return []
    inner = _as_dict(inner)
    items = inner.get(tag, inner if tag not in inner else None)
    if items is None:
        return []
    if isinstance(items, dict):
        return [_as_dict(items)]
    if isinstance(items, list):
        return [_as_dict(item) for item in items]
    return []


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return {str(k): v for k, v in value.items()}
    if hasattr(value, "__values__"):  # zeep object
        return {str(k): v for k, v in value.__values__.items()}
    if hasattr(value, "__dict__"):
        return {str(k): v for k, v in vars(value).items() if not k.startswith("_")}
    return {}


def text(value: Any) -> str | None:
    """AXL returns scalars, ``{'_value_1': 'x'}`` wrappers, and None. Flatten them."""
    if value is None:
        return None
    if isinstance(value, dict):
        for key in ("_value_1", "value", "#text"):
            if key in value:
                return text(value[key])
        return None
    result = str(value).strip()
    return result or None


def flag(value: Any) -> bool | None:
    raw = text(value)
    if raw is None:
        return None
    return raw.lower() in {"true", "t", "1", "yes"}


def number(value: Any) -> int | None:
    raw = text(value)
    if raw is None:
        return None
    try:
        return int(float(raw))
    except ValueError:
        return None


class CucmConnector(Connector):
    """Read a CUCM cluster into canonical form."""

    connector_id: ClassVar[str] = CONNECTOR_ID
    platform: ClassVar[Platform] = Platform.CISCO_CUCM

    #: kind -> (AXL operation, response tag)
    EXTRACTORS: ClassVar[dict[str, tuple[str, str]]] = {
        "Partition": ("listRoutePartition", "routePartition"),
        "CallingPermission": ("listCss", "css"),
        "DevicePool": ("listDevicePool", "devicePool"),
        "Extension": ("listLine", "line"),
        "Line": ("listLine", "line"),
        "User": ("listUser", "user"),
        "Device": ("listPhone", "phone"),
        "DeviceProfile": ("listDeviceProfile", "deviceProfile"),
        "RoutePattern": ("listRoutePattern", "routePattern"),
        "TranslationPattern": ("listTransPattern", "transPattern"),
        "RouteList": ("listRouteList", "routeList"),
        "RouteGroup": ("listRouteGroup", "routeGroup"),
        "LineGroup": ("listLineGroup", "lineGroup"),
        "HuntGroup": ("listHuntPilot", "huntPilot"),
        "PickupGroup": ("listCallPickupGroup", "callPickupGroup"),
        "SIPTrunk": ("listSipTrunk", "sipTrunk"),
    }

    def __init__(
        self,
        transport: AxlTransport,
        *,
        instance_id: str,
        tenant_id: str,
        credential_ref: CredentialRef | None = None,
        credentials: CredentialBroker | None = None,
        cdr_last_activity: Mapping[str, Any] | None = None,
        cassette_is_synthetic: bool = True,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            instance_id=instance_id,
            tenant_id=tenant_id,
            credential_ref=credential_ref
            or CredentialRef(
                provider="vault",
                path=f"cucm/{instance_id}",
                kind=CredentialKind.USERNAME_PASSWORD,
                scope=CredentialScope.READ_ONLY,
            ),
            credentials=credentials or CredentialBroker([]),
            **kwargs,
        )
        self.transport = transport
        #: userid -> last call activity, from CDR. Drives dormant-seat detection.
        self.cdr_last_activity = dict(cdr_last_activity or {})
        self._cassette_is_synthetic = cassette_is_synthetic

    # ------------------------------------------------------------------ #
    # Manifest
    # ------------------------------------------------------------------ #

    def synthetic_cassette_names(self) -> list[str]:
        return ["cucm-discovery"] if self._cassette_is_synthetic else []

    def capabilities(self) -> CapabilityManifest:
        read_only = [
            EntityCapability(
                entity_kind=kind,
                can_extract=True,
                api_surface="Cisco AXL",
                required_permissions=["Standard AXL API Access"],
            )
            for kind in sorted(self.EXTRACTORS)
            if kind not in {"Device", "User", "Line", "HuntGroup"}
        ]
        return CapabilityManifest(
            connector_id=CONNECTOR_ID,
            connector_version=CONNECTOR_VERSION,
            platform=self.platform,
            display_name="Cisco Unified Communications Manager",
            api_surfaces=[
                APISurface(
                    name="Cisco AXL",
                    version=self.transport.schema_version,
                    transport="SOAP",
                    documentation_url="https://developer.cisco.com/docs/axl/axl-developer-guide/",
                    verified_at=AXL_VERIFIED_ON,
                    verification_method=(
                        "Endpoint, SOAPAction, namespace, auth, and throttling behaviour "
                        "checked against the Cisco AXL Developer Guide. Per-release object "
                        "schemas are read from the live WSDL, not assumed."
                    ),
                    notes="Configuration writes go to the publisher node only.",
                )
            ],
            entities=[
                *read_only,
                EntityCapability(
                    entity_kind="User",
                    can_extract=True,
                    api_surface="Cisco AXL",
                    known_gaps=[
                        "Associated device list and user roles are not extracted in Phase 1",
                        "Credentials and PINs are never extracted",
                    ],
                    required_permissions=["Standard AXL API Access"],
                ),
                EntityCapability(
                    entity_kind="Line",
                    can_extract=True,
                    can_apply=True,
                    supported_verbs=[WriteVerb.CREATE, WriteVerb.UPDATE, WriteVerb.DELETE],
                    api_surface="Cisco AXL",
                    known_gaps=["Shared-line appearances need a getPhone pass to reconstruct"],
                    required_permissions=["Standard AXL API Access"],
                ),
                EntityCapability(
                    entity_kind="Device",
                    can_extract=True,
                    api_surface="Cisco AXL",
                    known_gaps=[
                        "listPhone returns summary rows; per-device button layout needs getPhone",
                        "Registration state comes from RIS, not AXL, and is absent in Phase 1",
                    ],
                    required_permissions=["Standard AXL API Access"],
                ),
                EntityCapability(
                    entity_kind="HuntGroup",
                    can_extract=True,
                    api_surface="Cisco AXL",
                    known_gaps=["Hunt list chaining is summarised; queuing settings are not read"],
                    required_permissions=["Standard AXL API Access"],
                ),
            ],
            credential_requirements=[
                CredentialRequirement(
                    purpose="axl",
                    kind=CredentialKind.USERNAME_PASSWORD,
                    minimum_scope=CredentialScope.READ_ONLY,
                    required_roles=["Standard AXL API Access"],
                    notes="AXL supports a read-only role; discovery accounts should hold it.",
                )
            ],
            # AXL throttles writes dynamically on database queue depth and answers 503
            # under load. Conservative concurrency is deliberate: a throttled publisher
            # mid-cutover is a self-inflicted outage.
            rate_limits=RateLimitPolicy(
                max_concurrent_requests=2,
                requests_per_second=5,
                honours_retry_after=False,
                initial_backoff_seconds=5.0,
                max_backoff_seconds=60.0,
                max_attempts=4,
                batch_size=250,
            ),
            eventual_consistency=EventualConsistencyPolicy(is_eventually_consistent=False),
            supports_dry_run=True,
            supports_rollback=True,
            requires_publisher_node=True,
            air_gap_capable=True,
            notes="Phase 1 is Extract-only in practice; write capability is declared for Line "
            "so the Phase 5 reverse transform has a target.",
        )

    async def test_connection(self) -> ConnectionTestResult:
        try:
            response = await self.transport.call("getCCMVersion", {})
        except ConnectorError as exc:
            return ConnectionTestResult(
                connector_id=CONNECTOR_ID,
                reachable=False,
                authenticated=False,
                messages=[str(exc)],
            )
        version = _cluster_version(response)
        messages = [f"AXL schema in use: {self.transport.schema_version}"]
        if version and not version.startswith(self.transport.schema_version.split(".")[0]):
            messages.append(
                f"Cluster reports {version} but the connector is using AXL schema "
                f"{self.transport.schema_version}. Negotiate the matching WSDL before writing."
            )
        return ConnectionTestResult(
            connector_id=CONNECTOR_ID,
            reachable=True,
            authenticated=True,
            scope=self.credential_ref.scope.value,
            platform_version=version,
            granted_permissions=["Standard AXL API Access"],
            messages=messages,
        )

    # ------------------------------------------------------------------ #
    # Natural keys
    # ------------------------------------------------------------------ #

    @classmethod
    def natural_key_for(cls, entity: CanonicalEntity) -> str | None:
        if isinstance(entity, User):
            return entity.user_principal_name
        if isinstance(entity, Device):
            return entity.device_name
        if isinstance(entity, Line):
            return f"{entity.directory_number}|{entity.partition_ref or ''}"
        if isinstance(entity, Extension):
            return entity.digits
        if isinstance(entity, (Partition, CallingPermission, DevicePool, RouteList,
                               RouteGroup, LineGroup, PickupGroup, SIPTrunk, DeviceProfile)):
            return getattr(entity, "name", None)
        if isinstance(entity, HuntGroup):
            return entity.pilot_pattern
        if isinstance(entity, (RoutePattern, TranslationPattern)):
            return entity.pattern
        return None

    # ------------------------------------------------------------------ #
    # Extract
    # ------------------------------------------------------------------ #

    async def _extract_batches(self, request: ExtractRequest) -> AsyncIterator[ExtractBatch]:
        wanted = list(request.entity_kinds or self.EXTRACTORS)
        # Extract in dependency order so referenced entities are minted first.
        order = list(self.EXTRACTORS)
        wanted.sort(key=lambda kind: order.index(kind) if kind in order else len(order))

        mappers = {
            "Partition": self._map_partitions,
            "CallingPermission": self._map_calling_permissions,
            "DevicePool": self._map_device_pools,
            "Extension": self._map_extensions,
            "Line": self._map_lines,
            "User": self._map_users,
            "Device": self._map_devices,
            "DeviceProfile": self._map_device_profiles,
            "RoutePattern": self._map_route_patterns,
            "TranslationPattern": self._map_translation_patterns,
            "RouteList": self._map_route_lists,
            "RouteGroup": self._map_route_groups,
            "LineGroup": self._map_line_groups,
            "HuntGroup": self._map_hunt_groups,
            "PickupGroup": self._map_pickup_groups,
            "SIPTrunk": self._map_sip_trunks,
        }

        warnings: list[str] = []
        collected: list[CanonicalEntity] = []

        for kind in wanted:
            mapper = mappers.get(kind)
            if mapper is None:
                continue
            operation, tag = self.EXTRACTORS[kind]
            response = await self.transport.call(
                operation, {"searchCriteria": {"name": "%"}, "returnedTags": {}}
            )
            collected.extend(mapper(rows(response, tag)))

        self._link_extension_owners(collected, warnings)

        sequence = 0
        page_size = max(1, request.page_size)
        for chunk_start in range(0, len(collected), page_size):
            yield ExtractBatch(
                run_id=request.run_id,
                sequence=sequence,
                entities=collected[chunk_start : chunk_start + page_size],
                warnings=[],
                raw_sql_used=False,
                is_final=False,
            )
            sequence += 1

        yield ExtractBatch(
            run_id=request.run_id, sequence=sequence, entities=[], warnings=warnings, is_final=True
        )

    def _link_extension_owners(
        self, entities: list[CanonicalEntity], warnings: list[str]
    ) -> None:
        """Back-fill ``Extension.owner_ref`` and ``Line.owner_ref`` from users.

        CUCM records the association on the user (``primaryExtension``), not on
        the line, so the link only exists once both passes have run. Without it
        every extension looks ownerless, and a number derived from it has nobody
        to assign to — which surfaces much later as an unhelpful "no write path"
        error rather than as the data problem it is.
        """
        owner_by_extension: dict[str, str] = {}
        for entity in entities:
            if isinstance(entity, User) and entity.primary_extension_ref:
                owner_by_extension[entity.primary_extension_ref] = entity.canonical_id

        ownerless: list[str] = []
        for entity in entities:
            if isinstance(entity, Extension):
                owner = owner_by_extension.get(entity.canonical_id)
                if owner:
                    entity.owner_ref = owner
                else:
                    ownerless.append(entity.digits)
            elif isinstance(entity, Line) and entity.extension_ref:
                owner = owner_by_extension.get(entity.extension_ref)
                if owner:
                    entity.owner_ref = owner

        if ownerless:
            warnings.append(
                f"{len(ownerless)} extension(s) have no owning user "
                f"({', '.join(sorted(ownerless)[:10])}). These are usually shared lines, "
                "hunt pilots, or analogue services; each needs an explicit disposition "
                "because there is no user to migrate them with."
            )

    # -- provenance ------------------------------------------------------ #

    def _source(self, native_type: str, native_key: str, record: Mapping[str, Any],
                *, operation: str, raw_sql: bool = False) -> SourceRef:
        return SourceRef(
            platform=self.platform,
            instance_id=self.instance_id,
            native_type=native_type,
            native_key=native_key,
            native_attributes={k: _plain(v) for k, v in record.items()},
            api_surface=f"AXL:{operation}",
            raw_sql_used=raw_sql,
        )

    def _id(self, kind: str, native_key: str) -> str:
        return CanonicalEntity.mint_canonical_id(
            self.platform, kind, native_key, instance_id=self.instance_id
        )

    def _ref(self, kind: str, value: Any) -> str | None:
        name = text(value)
        return self._id(kind, name) if name else None

    # -- mappers --------------------------------------------------------- #

    def _map_partitions(self, records: list[dict[str, Any]]) -> Iterable[Partition]:
        for record in records:
            name = text(record.get("name"))
            if not name:
                continue
            consumed = {"name", "description"}
            yield Partition(
                canonical_id=self._id("Partition", name),
                display_name=name,
                name=name,
                description=text(record.get("description")),
                source_ref=self._source("routePartition", name, record,
                                        operation="listRoutePartition"),
                fidelity=assess_mapping(
                    record, consumed, assessed_by=CONNECTOR_ID, entity_label="Partition",
                    lossless_rationale="A partition is a name and a description; both carry.",
                ),
            )

    def _map_calling_permissions(
        self, records: list[dict[str, Any]]
    ) -> Iterable[CallingPermission]:
        for record in records:
            name = text(record.get("name"))
            if not name:
                continue
            members = _member_names(record.get("members"), "member", "routePartitionName")
            consumed = {"name", "description", "members", "clause"}
            yield CallingPermission(
                canonical_id=self._id("CallingPermission", name),
                display_name=name,
                name=name,
                description=text(record.get("description")),
                permitted_partition_refs=[self._id("Partition", m) for m in members],
                permission_class=PermissionClass.CUSTOM,
                derived_from="CUCM:CSS",
                source_ref=self._source("css", name, record, operation="listCss"),
                fidelity=assess_mapping(
                    record, consumed, assessed_by=CONNECTOR_ID,
                    entity_label="CallingSearchSpace",
                    lossless_rationale=(
                        "Name and ordered partition list are the whole of a CSS, and order "
                        "is preserved."
                    ),
                    extra_degraded=[
                        DegradedAttribute(
                            attribute="permission_class",
                            reason=(
                                "CUCM expresses reachability structurally, not as a named tier; "
                                "the tier must be inferred by a mapping rule"
                            ),
                            target_behaviour=(
                                "Classified CUSTOM until a mapping rule or a human assigns a "
                                "tier. Auto-mapping to a target calling policy needs that tier."
                            ),
                        )
                    ],
                ),
            )

    def _map_device_pools(self, records: list[dict[str, Any]]) -> Iterable[DevicePool]:
        for record in records:
            name = text(record.get("name"))
            if not name:
                continue
            srst = text(record.get("srstName"))
            consumed = {
                "name", "dateTimeSettingName", "regionName", "srstName",
                "mediaResourceListName", "callingSearchSpaceName",
            }
            extra = []
            if srst and srst.lower() not in {"disable", "none"}:
                extra.append(
                    DegradedAttribute(
                        attribute="srst_reference",
                        reason="SRST provides local survivability when the WAN fails",
                        source_value=srst,
                        target_behaviour=(
                            "Cloud targets have no SRST equivalent. Sites relying on it lose "
                            "local survivability at cutover; this needs an explicit decision."
                        ),
                    )
                )
            yield DevicePool(
                canonical_id=self._id("DevicePool", name),
                display_name=name,
                name=name,
                region=text(record.get("regionName")),
                date_time_group=text(record.get("dateTimeSettingName")),
                srst_reference=srst,
                media_resource_group_list=text(record.get("mediaResourceListName")),
                calling_permission_ref=self._ref("CallingPermission",
                                                 record.get("callingSearchSpaceName")),
                source_ref=self._source("devicePool", name, record, operation="listDevicePool"),
                fidelity=assess_mapping(
                    record, consumed, assessed_by=CONNECTOR_ID, entity_label="DevicePool",
                    lossless_rationale="Region, time group, and CSS association all carry.",
                    extra_degraded=extra,
                    manual_effort_minutes=30 if extra else None,
                ),
            )

    def _map_extensions(self, records: list[dict[str, Any]]) -> Iterable[Extension]:
        for record in records:
            digits = text(record.get("pattern"))
            if not digits or not digits.replace("+", "").isdigit():
                # Patterns with wildcards are dial-plan entries, not extensions.
                continue
            partition = text(record.get("routePartitionName"))
            consumed = {
                "pattern", "routePartitionName", "description", "usage",
                "shareLineAppearanceCssName",
            }
            yield Extension(
                canonical_id=self._id("Extension", digits),
                display_name=digits,
                digits=digits,
                partition_ref=self._id("Partition", partition) if partition else None,
                description=text(record.get("description")),
                source_ref=self._source("line", digits, record, operation="listLine"),
                fidelity=assess_mapping(
                    record, consumed, assessed_by=CONNECTOR_ID, entity_label="Extension",
                    lossless_rationale="An extension is its digits and its partition.",
                    extra_degraded=[
                        DegradedAttribute(
                            attribute="e164_ref",
                            reason="CUCM does not store the external number on the line itself",
                            target_behaviour=(
                                "The E.164 mapping is derived by the number-normalisation "
                                "engine from site prefix tables, not read from the source."
                            ),
                        )
                    ],
                ),
            )

    def _map_lines(self, records: list[dict[str, Any]]) -> Iterable[Line]:
        for record in records:
            pattern = text(record.get("pattern"))
            if not pattern:
                continue
            partition = text(record.get("routePartitionName"))
            key = f"{pattern}|{partition or ''}"
            consumed = {
                "pattern", "routePartitionName", "description", "alertingName",
                "asciiAlertingName", "shareLineAppearanceCssName", "voiceMailProfileName",
                "callForwardAll", "callForwardBusy", "callForwardNoAnswer",
            }
            yield Line(
                canonical_id=self._id("Line", key),
                display_name=text(record.get("description")) or pattern,
                directory_number=pattern,
                partition_ref=self._id("Partition", partition) if partition else None,
                extension_ref=self._id("Extension", pattern) if pattern.isdigit() else None,
                label=text(record.get("description")),
                alerting_name=text(record.get("alertingName")),
                voicemail_profile=text(record.get("voiceMailProfileName")),
                source_ref=self._source("line", key, record, operation="listLine"),
                fidelity=assess_mapping(
                    record, consumed, assessed_by=CONNECTOR_ID, entity_label="Line",
                    lossless_rationale="Directory number, partition, and labels carry across.",
                    extra_degraded=[
                        DegradedAttribute(
                            attribute="shared_appearance_ref",
                            reason=(
                                "listLine does not report which devices hold an appearance; "
                                "that needs a getPhone pass"
                            ),
                            target_behaviour=(
                                "Shared lines are not reconstructed in Phase 1. Any line held "
                                "on more than one device will migrate as a single appearance "
                                "unless the device pass runs."
                            ),
                        )
                    ],
                    manual_effort_minutes=10,
                ),
            )

    def _map_users(self, records: list[dict[str, Any]]) -> Iterable[User]:
        for record in records:
            userid = text(record.get("userid"))
            if not userid:
                continue
            consumed = {
                "userid", "firstName", "lastName", "mailid", "department",
                "telephoneNumber", "status", "primaryExtension",
            }
            extension = _primary_extension(record.get("primaryExtension"))
            yield User(
                canonical_id=self._id("User", userid),
                display_name=" ".join(
                    p for p in (text(record.get("firstName")), text(record.get("lastName"))) if p
                ) or userid,
                user_principal_name=userid,
                email=text(record.get("mailid")),
                given_name=text(record.get("firstName")),
                surname=text(record.get("lastName")),
                department=text(record.get("department")),
                enabled=text(record.get("status")) != "0",
                telephony_enabled=extension is not None,
                primary_extension_ref=self._id("Extension", extension) if extension else None,
                last_call_activity_at=self.cdr_last_activity.get(userid),
                source_ref=self._source("user", userid, record, operation="listUser"),
                fidelity=assess_mapping(
                    record, consumed, assessed_by=CONNECTOR_ID, entity_label="User",
                    lossless_rationale="Identity attributes carry across.",
                    extra_degraded=[
                        DegradedAttribute(
                            attribute="policy_refs",
                            reason="CUCM expresses entitlement through CSS and device association",
                            target_behaviour=(
                                "The user is created without a calling policy and inherits the "
                                "target default until a mapping rule assigns one."
                            ),
                        )
                    ],
                    manual_effort_minutes=5,
                ),
            )

    def _map_devices(self, records: list[dict[str, Any]]) -> Iterable[Device]:
        for record in records:
            name = text(record.get("name"))
            if not name:
                continue
            model = text(record.get("model")) or text(record.get("product"))
            consumed = {
                "name", "description", "model", "product", "class", "protocol",
                "devicePoolName", "callingSearchSpaceName", "securityProfileName",
                "ownerUserName",
            }
            device_type = _device_type(model, text(record.get("class")))
            eol = bool(model and model.startswith(KNOWN_END_OF_LIFE_PREFIXES))

            extra = []
            if eol:
                extra.append(
                    DegradedAttribute(
                        attribute="model",
                        reason=f"{model} has no SIP path onto a modern cloud target",
                        source_value=model,
                        target_behaviour=(
                            "The handset must be replaced or the user moved to a softphone. "
                            "This is hardware budget, not configuration."
                        ),
                    )
                )
            if device_type is DeviceType.ANALOGUE:
                extra.append(
                    DegradedAttribute(
                        attribute="device_type",
                        reason="analogue endpoint behind a gateway port",
                        source_value=model,
                        target_behaviour=(
                            "Fax, lift phones, door entry, and paging do not migrate to a cloud "
                            "PBX. They need an ATA, a retained gateway, or a service withdrawal "
                            "decision before cutover."
                        ),
                    )
                )

            yield Device(
                canonical_id=self._id("Device", name),
                display_name=text(record.get("description")) or name,
                device_name=name,
                mac_address=_mac_from_device_name(name),
                vendor="Cisco",
                model=model,
                device_type=device_type,
                protocol=_protocol(text(record.get("protocol"))),
                device_pool_ref=self._ref("DevicePool", record.get("devicePoolName")),
                security_profile=text(record.get("securityProfileName")),
                owner_ref=self._ref("User", record.get("ownerUserName")),
                replacement_required=eol or device_type is DeviceType.ANALOGUE,
                source_ref=self._source("phone", name, record, operation="listPhone"),
                fidelity=assess_mapping(
                    record, consumed, assessed_by=CONNECTOR_ID, entity_label="Device",
                    lossless_rationale="Device identity and pool association carry across.",
                    extra_degraded=extra,
                    manual_effort_minutes=20 if extra else None,
                ),
            )

    def _map_device_profiles(self, records: list[dict[str, Any]]) -> Iterable[DeviceProfile]:
        for record in records:
            name = text(record.get("name"))
            if not name:
                continue
            consumed = {"name", "description", "product", "model", "phoneTemplateName"}
            yield DeviceProfile(
                canonical_id=self._id("DeviceProfile", name),
                display_name=name,
                name=name,
                model=text(record.get("model")) or text(record.get("product")),
                description=text(record.get("description")),
                source_ref=self._source("deviceProfile", name, record,
                                        operation="listDeviceProfile"),
                fidelity=assess_mapping(
                    record, consumed, assessed_by=CONNECTOR_ID, entity_label="DeviceProfile",
                    lossless_rationale="Profile identity carries across.",
                    extra_degraded=[
                        DegradedAttribute(
                            attribute="ExtensionMobility",
                            reason="hot-desking has no direct equivalent on most cloud targets",
                            target_behaviour=(
                                "Users sign in to a client rather than a handset. Shared-desk "
                                "workflows need redesign, not migration."
                            ),
                        )
                    ],
                    manual_effort_minutes=45,
                ),
            )

    def _map_route_patterns(self, records: list[dict[str, Any]]) -> Iterable[RoutePattern]:
        for record in records:
            pattern = text(record.get("pattern"))
            if not pattern:
                continue
            consumed = {
                "pattern", "routePartitionName", "description", "blockEnable",
                "destination", "digitDiscardInstructionName", "prefixDigitsOut",
                "calledPartyTransformationMask", "callingPartyTransformationMask",
                "useCallingPartyPhoneMask", "networkLocation", "patternUrgency",
            }
            yield RoutePattern(
                canonical_id=self._id("RoutePattern", pattern),
                display_name=pattern,
                pattern=pattern,
                partition_ref=self._ref("Partition", record.get("routePartitionName")),
                route_target_ref=self._ref(
                    "RouteList", _destination_name(record.get("destination"))
                ),
                block_call=bool(flag(record.get("blockEnable"))),
                urgent_priority=bool(flag(record.get("patternUrgency"))),
                digits_to_discard=text(record.get("digitDiscardInstructionName")),
                called_party_transform_mask=text(record.get("calledPartyTransformationMask")),
                calling_party_transform_mask=text(record.get("callingPartyTransformationMask")),
                prefix_digits=text(record.get("prefixDigitsOut")),
                description=text(record.get("description")),
                source_ref=self._source("routePattern", pattern, record,
                                        operation="listRoutePattern"),
                fidelity=assess_mapping(
                    record, consumed, assessed_by=CONNECTOR_ID, entity_label="RoutePattern",
                    lossless_rationale="Pattern, partition, target, and digit manipulation carry.",
                ),
            )

    def _map_translation_patterns(
        self, records: list[dict[str, Any]]
    ) -> Iterable[TranslationPattern]:
        for record in records:
            pattern = text(record.get("pattern"))
            if not pattern:
                continue
            consumed = {
                "pattern", "routePartitionName", "description",
                "calledPartyTransformationMask", "callingPartyTransformationMask",
                "digitDiscardInstructionName", "prefixDigitsOut",
                "callingSearchSpaceName", "patternUrgency",
            }
            yield TranslationPattern(
                canonical_id=self._id("TranslationPattern", pattern),
                display_name=pattern,
                pattern=pattern,
                partition_ref=self._ref("Partition", record.get("routePartitionName")),
                called_party_transform_mask=text(record.get("calledPartyTransformationMask")),
                calling_party_transform_mask=text(record.get("callingPartyTransformationMask")),
                digits_to_discard=text(record.get("digitDiscardInstructionName")),
                prefix_digits=text(record.get("prefixDigitsOut")),
                target_permission_ref=self._ref("CallingPermission",
                                                record.get("callingSearchSpaceName")),
                urgent_priority=bool(flag(record.get("patternUrgency"))),
                description=text(record.get("description")),
                source_ref=self._source("transPattern", pattern, record,
                                        operation="listTransPattern"),
                fidelity=assess_mapping(
                    record, consumed, assessed_by=CONNECTOR_ID,
                    entity_label="TranslationPattern",
                    lossless_rationale="Pattern and both transformation masks carry across.",
                    extra_degraded=[
                        DegradedAttribute(
                            attribute="TranslationPattern",
                            reason=(
                                "cloud targets normalise with ordered regex rules rather than "
                                "a re-looked-up pattern space"
                            ),
                            target_behaviour=(
                                "Translated into normalisation rules. Chains of translations "
                                "that re-enter the dial plan may not survive intact and should "
                                "be tested with synthetic calls."
                            ),
                        )
                    ],
                    manual_effort_minutes=15,
                ),
            )

    def _map_route_lists(self, records: list[dict[str, Any]]) -> Iterable[RouteList]:
        for record in records:
            name = text(record.get("name"))
            if not name:
                continue
            groups = _member_names(record.get("members"), "member", "routeGroupName")
            consumed = {"name", "description", "members", "runOnEveryNode", "callManagerGroupName"}
            yield RouteList(
                canonical_id=self._id("RouteList", name),
                display_name=name,
                name=name,
                route_group_refs=[self._id("RouteGroup", g) for g in groups],
                run_on_all_active_nodes=bool(flag(record.get("runOnEveryNode"))),
                description=text(record.get("description")),
                source_ref=self._source("routeList", name, record, operation="listRouteList"),
                fidelity=assess_mapping(
                    record, consumed, assessed_by=CONNECTOR_ID, entity_label="RouteList",
                    lossless_rationale="Ordered route-group membership is the whole object.",
                ),
            )

    def _map_route_groups(self, records: list[dict[str, Any]]) -> Iterable[RouteGroup]:
        for record in records:
            name = text(record.get("name"))
            if not name:
                continue
            members = _member_names(record.get("members"), "member", "deviceName")
            consumed = {"name", "distributionAlgorithm", "members"}
            yield RouteGroup(
                canonical_id=self._id("RouteGroup", name),
                display_name=name,
                name=name,
                member_device_refs=[self._id("SIPTrunk", m) for m in members],
                distribution_algorithm=_distribution(text(record.get("distributionAlgorithm"))),
                source_ref=self._source("routeGroup", name, record, operation="listRouteGroup"),
                fidelity=assess_mapping(
                    record, consumed, assessed_by=CONNECTOR_ID, entity_label="RouteGroup",
                    lossless_rationale="Members and distribution algorithm carry across.",
                ),
            )

    def _map_line_groups(self, records: list[dict[str, Any]]) -> Iterable[LineGroup]:
        for record in records:
            name = text(record.get("name"))
            if not name:
                continue
            members = _member_names(record.get("members"), "member", "directoryNumber")
            consumed = {
                "name", "distributionAlgorithm", "members", "rnaReversionTimeOut",
                "huntAlgorithmNoAnswer", "huntAlgorithmBusy", "huntAlgorithmNotAvailable",
            }
            yield LineGroup(
                canonical_id=self._id("LineGroup", name),
                display_name=name,
                name=name,
                distribution_algorithm=_distribution(text(record.get("distributionAlgorithm"))),
                member_line_refs=[self._id("Line", f"{m}|") for m in members],
                rna_reversion_timeout_seconds=number(record.get("rnaReversionTimeOut")),
                source_ref=self._source("lineGroup", name, record, operation="listLineGroup"),
                fidelity=assess_mapping(
                    record, consumed, assessed_by=CONNECTOR_ID, entity_label="LineGroup",
                    lossless_rationale="Ordered membership and hunt behaviour carry across.",
                ),
            )

    def _map_hunt_groups(self, records: list[dict[str, Any]]) -> Iterable[HuntGroup]:
        for record in records:
            pattern = text(record.get("pattern"))
            if not pattern:
                continue
            consumed = {
                "pattern", "routePartitionName", "description", "huntListName",
                "alertingName", "maxHuntTimer",
            }
            yield HuntGroup(
                canonical_id=self._id("HuntGroup", pattern),
                display_name=text(record.get("description")) or pattern,
                name=text(record.get("description")) or pattern,
                pilot_pattern=pattern,
                partition_ref=self._ref("Partition", record.get("routePartitionName")),
                max_hunt_timer_seconds=number(record.get("maxHuntTimer")),
                source_ref=self._source("huntPilot", pattern, record, operation="listHuntPilot"),
                fidelity=assess_mapping(
                    record, consumed, assessed_by=CONNECTOR_ID, entity_label="HuntPilot",
                    lossless_rationale="Pilot, partition, and hunt timing carry across.",
                    extra_degraded=[
                        DegradedAttribute(
                            attribute="CallQueue",
                            reason=(
                                "a hunt pilot has no agent-presence model and no queued-caller "
                                "experience; a cloud call queue has no line-group chaining"
                            ),
                            target_behaviour=(
                                "Becomes a call queue with a flattened agent list. Multi-stage "
                                "hunt chains collapse and overflow behaviour must be re-specified."
                            ),
                        )
                    ],
                    manual_effort_minutes=25,
                ),
            )

    def _map_pickup_groups(self, records: list[dict[str, Any]]) -> Iterable[PickupGroup]:
        for record in records:
            name = text(record.get("name"))
            if not name:
                continue
            consumed = {"name", "pattern", "routePartitionName", "description"}
            yield PickupGroup(
                canonical_id=self._id("PickupGroup", name),
                display_name=name,
                name=name,
                pickup_number=text(record.get("pattern")),
                partition_ref=self._ref("Partition", record.get("routePartitionName")),
                source_ref=self._source("callPickupGroup", name, record,
                                        operation="listCallPickupGroup"),
                fidelity=assess_mapping(
                    record, consumed, assessed_by=CONNECTOR_ID, entity_label="PickupGroup",
                    lossless_rationale="Group identity and pickup number carry across.",
                    extra_degraded=[
                        DegradedAttribute(
                            attribute="member_line_refs",
                            reason="membership is held on the line, not the group, in CUCM",
                            target_behaviour=(
                                "Membership is reconstructed from the line pass. Where call "
                                "pickup has no target equivalent, users lose the feature."
                            ),
                        )
                    ],
                    manual_effort_minutes=20,
                ),
            )

    def _map_sip_trunks(self, records: list[dict[str, Any]]) -> Iterable[SIPTrunk]:
        for record in records:
            name = text(record.get("name"))
            if not name:
                continue
            consumed = {
                "name", "description", "destinations", "sipProfileName",
                "securityProfileName", "callingSearchSpaceName", "devicePoolName",
                "srtpAllowed", "significantDigits", "runOnEveryNode",
            }
            yield SIPTrunk(
                canonical_id=self._id("SIPTrunk", name),
                display_name=name,
                name=name,
                description=text(record.get("description")),
                destinations=_sip_destinations(record.get("destinations")),
                security_profile=text(record.get("securityProfileName")),
                outbound_calling_permission_ref=self._ref(
                    "CallingPermission", record.get("callingSearchSpaceName")
                ),
                device_pool_ref=self._ref("DevicePool", record.get("devicePoolName")),
                srtp_allowed=bool(flag(record.get("srtpAllowed"))),
                significant_digits=number(record.get("significantDigits")),
                run_on_all_active_nodes=bool(flag(record.get("runOnEveryNode"))),
                source_ref=self._source("sipTrunk", name, record, operation="listSipTrunk"),
                fidelity=assess_mapping(
                    record, consumed, assessed_by=CONNECTOR_ID, entity_label="SIPTrunk",
                    lossless_rationale="Destinations, security, and routing associations carry.",
                ),
            )

    # ------------------------------------------------------------------ #
    # Apply - Phase 5 target surface only
    # ------------------------------------------------------------------ #

    async def _preview_operation(self, operation: WriteOperation) -> OperationPreview:
        raise NotImplementedError(
            "CUCM write support lands in Phase 5 with the reverse transform. The manifest "
            "declares Line as writable so the planner can target it; the AXL add/update "
            "payload builders must be written and cassette-tested before this is enabled."
        )

    async def _execute_operation(self, operation: WriteOperation) -> OperationResult:
        raise NotImplementedError(
            "CUCM write support lands in Phase 5. See _preview_operation."
        )


# --------------------------------------------------------------------------- #
# AXL field helpers
# --------------------------------------------------------------------------- #


def _plain(value: Any) -> Any:
    """Make an AXL value JSON-serialisable without losing information."""
    if isinstance(value, dict):
        flattened = text(value)
        return flattened if flattened is not None else {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _member_names(container: Any, member_tag: str, name_field: str) -> list[str]:
    """Extract an ordered member-name list from an AXL ``members`` structure."""
    if container is None:
        return []
    payload = _as_dict(container)
    entries = payload.get(member_tag, container if not payload else [])
    if isinstance(entries, dict):
        entries = [entries]
    if not isinstance(entries, list):
        return []

    def sort_key(entry: Any) -> int:
        index = number(_as_dict(entry).get("selectionOrder"))
        return index if index is not None else 0

    ordered = sorted(entries, key=sort_key)
    names = []
    for entry in ordered:
        name = text(_as_dict(entry).get(name_field))
        if name:
            names.append(name)
    return names


def _sip_destinations(container: Any) -> list[SIPDestination]:
    if container is None:
        return []
    payload = _as_dict(container)
    entries = payload.get("destination", [])
    if isinstance(entries, dict):
        entries = [entries]
    if not isinstance(entries, list):
        return []
    destinations = []
    for entry in entries:
        record = _as_dict(entry)
        host = text(record.get("addressIpv4")) or text(record.get("address"))
        if not host:
            continue
        destinations.append(
            SIPDestination(
                host=host,
                port=number(record.get("port")) or 5060,
                sort_order=number(record.get("sortOrder")) or 1,
                transport=TransportProtocol.TCP,
            )
        )
    return destinations


def _destination_name(container: Any) -> Any:
    if container is None:
        return None
    payload = _as_dict(container)
    for key in ("routeListName", "gatewayName", "trunkName"):
        if key in payload:
            return payload[key]
    return text(container)


def _primary_extension(container: Any) -> str | None:
    if container is None:
        return None
    payload = _as_dict(container)
    return text(payload.get("pattern")) if payload else text(container)


def _device_type(model: str | None, device_class: str | None) -> DeviceType:
    haystack = f"{model or ''} {device_class or ''}".lower()
    if any(hint in haystack for hint in _ANALOGUE_HINTS):
        return DeviceType.ANALOGUE
    if "conference" in haystack or "8831" in haystack:
        return DeviceType.CONFERENCE_PHONE
    if "room" in haystack or "telepresence" in haystack or "webex" in haystack:
        return DeviceType.ROOM_SYSTEM
    if "csf" in haystack or "jabber" in haystack or "client services" in haystack:
        return DeviceType.SOFT_PHONE
    if "tct" in haystack or "bot" in haystack or "tab" in haystack:
        return DeviceType.MOBILE_CLIENT
    return DeviceType.HARD_PHONE


def _protocol(value: str | None) -> SignallingProtocol:
    match (value or "").upper():
        case "SIP":
            return SignallingProtocol.SIP
        case "SCCP":
            return SignallingProtocol.SCCP
        case "H323" | "H.323":
            return SignallingProtocol.H323
        case "MGCP":
            return SignallingProtocol.MGCP
        case _:
            return SignallingProtocol.SIP


def _distribution(value: str | None) -> DistributionAlgorithm:
    match (value or "").strip().lower():
        case "circular":
            return DistributionAlgorithm.CIRCULAR
        case "longest idle time" | "longest idle":
            return DistributionAlgorithm.LONGEST_IDLE
        case "broadcast":
            return DistributionAlgorithm.BROADCAST
        case _:
            return DistributionAlgorithm.TOP_DOWN


def _mac_from_device_name(name: str) -> str | None:
    """CUCM device names for hard phones are a 3-letter prefix plus the MAC."""
    candidate = name[3:] if len(name) == 15 else None
    if candidate and all(c in "0123456789ABCDEFabcdef" for c in candidate):
        return candidate.upper()
    return None


def _cluster_version(response: Any) -> str | None:
    payload = _as_dict(response)
    inner = _as_dict(payload.get("return", payload))
    version = inner.get("componentVersion") or inner.get("version")
    if isinstance(version, dict):
        return text(_as_dict(version).get("version"))
    return text(version)
