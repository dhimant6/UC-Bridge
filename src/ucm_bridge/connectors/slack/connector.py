"""Slack connector (Extract).

Slack has no telephony model at all, and saying so precisely is the most useful
thing this connector does. Its manifest declares the whole numbering, dial-plan,
and trunking space ``UNMAPPABLE``, which is what lets the split-target planner
route voice workloads to Teams or Genesys and collaboration to Slack rather than
discovering the problem per-object at apply time.

The second thing it does carefully is message history. The Discovery and Export
APIs are Enterprise Grid features. On any other plan the history simply cannot
be exported, and the connector reports that as a named, tier-specific
limitation before a customer commits — not as a failure during cutover.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from typing import Any, ClassVar

from ucm_bridge.canonical.base import (
    CanonicalEntity,
    DegradedAttribute,
    FidelityAssessment,
    FidelityLevel,
    Platform,
    SourceRef,
)
from ucm_bridge.canonical.collaboration import (
    ChannelMembership,
    ChannelType,
    ChatChannel,
    MembershipRole,
    MessageArchive,
)
from ucm_bridge.canonical.identity import Group, GroupType, User
from ucm_bridge.canonical.messaging import ExportAvailability
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

CONNECTOR_ID = "slack"
CONNECTOR_VERSION = "0.1.0"
SLACK_VERIFIED_ON = date(2026, 7, 26)

#: Slack has no concept of these. Declaring them keeps voice out of a Slack plan.
UNMAPPABLE_KINDS: tuple[str, ...] = (
    "E164Number",
    "Extension",
    "DIDRange",
    "NumberBlock",
    "EmergencyNumber",
    "EmergencyLocation",
    "Partition",
    "CallingPermission",
    "RoutePattern",
    "TranslationPattern",
    "RouteList",
    "RouteGroup",
    "SIPTrunk",
    "Gateway",
    "VoiceRoutingPolicy",
    "PSTNUsage",
    "VoiceRoute",
    "Line",
    "Device",
    "HuntGroup",
    "CallQueue",
    "AutoAttendant",
    "VoicemailBox",
)

ENTERPRISE_GRID = "Slack Enterprise Grid"


class SlackConnector(Connector):
    """Extract Slack users, groups, channels, and (where permitted) history."""

    connector_id: ClassVar[str] = CONNECTOR_ID
    platform: ClassVar[Platform] = Platform.SLACK

    def __init__(
        self,
        *,
        api: RestTransport,
        instance_id: str,
        tenant_id: str,
        plan_tier: str = "Slack Business+",
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
                path=f"slack/{instance_id}",
                kind=CredentialKind.API_TOKEN,
                scope=CredentialScope.READ_ONLY,
            ),
            credentials=credentials or CredentialBroker([]),
            **kwargs,
        )
        self.api = api
        self.plan_tier = plan_tier
        self._cassette_is_synthetic = cassette_is_synthetic

    @property
    def history_export_available(self) -> bool:
        """Discovery/Export APIs are an Enterprise Grid feature."""
        return self.plan_tier.strip().lower() == ENTERPRISE_GRID.lower()

    def synthetic_cassette_names(self) -> list[str]:
        return ["slack-workspace"] if self._cassette_is_synthetic else []

    def capabilities(self) -> CapabilityManifest:
        unmappable = [
            EntityCapability(
                entity_kind=kind,
                can_extract=False,
                can_apply=False,
                expected_fidelity=FidelityLevel.UNMAPPABLE,
                fidelity_notes=(
                    "Slack has no telephony model. Routing a voice workload here would "
                    "silently drop it."
                ),
            )
            for kind in UNMAPPABLE_KINDS
        ]
        return CapabilityManifest(
            connector_id=CONNECTOR_ID,
            connector_version=CONNECTOR_VERSION,
            platform=self.platform,
            display_name="Slack",
            api_surfaces=[
                APISurface(
                    name="Slack Web API",
                    transport="REST",
                    documentation_url="https://api.slack.com/methods",
                    verified_at=SLACK_VERIFIED_ON,
                    verification_method=(
                        "Cursor pagination via response_metadata.next_cursor, which the shared "
                        "REST transport implements. Individual method response shapes are read "
                        "defensively."
                    ),
                ),
                APISurface(
                    name="Slack Discovery API",
                    transport="REST",
                    notes=(
                        f"Enterprise Grid only. This workspace is on {self.plan_tier!r}, so "
                        f"history export is "
                        f"{'available' if self.history_export_available else 'NOT available'}."
                    ),
                ),
            ],
            entities=[
                EntityCapability(
                    entity_kind="User",
                    can_extract=True,
                    api_surface="Slack Web API",
                    required_permissions=["users:read", "users:read.email"],
                ),
                EntityCapability(
                    entity_kind="Group",
                    can_extract=True,
                    api_surface="Slack Web API",
                    required_permissions=["usergroups:read"],
                ),
                EntityCapability(
                    entity_kind="ChatChannel",
                    can_extract=True,
                    api_surface="Slack Web API",
                    required_permissions=["channels:read", "groups:read"],
                ),
                EntityCapability(
                    entity_kind="ChannelMembership",
                    can_extract=True,
                    api_surface="Slack Web API",
                    required_permissions=["channels:read"],
                ),
                EntityCapability(
                    entity_kind="MessageArchive",
                    can_extract=True,
                    api_surface="Slack Discovery API",
                    known_gaps=(
                        []
                        if self.history_export_available
                        else [f"History export requires {ENTERPRISE_GRID}"]
                    ),
                    required_permissions=["discovery:read"],
                ),
                *unmappable,
            ],
            credential_requirements=[
                CredentialRequirement(
                    purpose="slack-token",
                    kind=CredentialKind.API_TOKEN,
                    minimum_scope=CredentialScope.READ_ONLY,
                )
            ],
            rate_limits=RateLimitPolicy(
                max_concurrent_requests=4,
                requests_per_second=1,
                honours_retry_after=True,
                max_attempts=6,
            ),
            eventual_consistency=EventualConsistencyPolicy(is_eventually_consistent=False),
            supports_dry_run=True,
            supports_rollback=False,
            air_gap_capable=False,
            notes="Extract-only. Collaboration workloads only; voice belongs elsewhere.",
        )

    async def test_connection(self) -> ConnectionTestResult:
        messages = [f"Plan tier: {self.plan_tier}"]
        if not self.history_export_available:
            messages.append(
                f"Message history cannot be exported on this plan; {ENTERPRISE_GRID} is "
                "required for the Discovery API."
            )
        try:
            await self.api.request(RestRequest(method=HttpMethod.GET, path="/users.list"))
        except Exception as exc:
            return ConnectionTestResult(
                connector_id=CONNECTOR_ID, reachable=False, authenticated=False,
                messages=[*messages, str(exc)],
            )
        return ConnectionTestResult(
            connector_id=CONNECTOR_ID,
            reachable=True,
            authenticated=True,
            scope=self.credential_ref.scope.value,
            granted_permissions=["users:read", "channels:read"],
            messages=messages,
        )

    @classmethod
    def natural_key_for(cls, entity: CanonicalEntity) -> str | None:
        if isinstance(entity, User):
            return entity.user_principal_name
        if isinstance(entity, (Group, ChatChannel)):
            return getattr(entity, "name", None)
        if isinstance(entity, ChannelMembership):
            return f"{entity.channel_ref}|{entity.member_ref}"
        if isinstance(entity, MessageArchive):
            return entity.conversation_label
        return None

    # ------------------------------------------------------------------ #
    # Extract
    # ------------------------------------------------------------------ #

    async def _extract_batches(self, request: ExtractRequest) -> AsyncIterator[ExtractBatch]:
        wanted = set(
            request.entity_kinds
            or ["User", "Group", "ChatChannel", "ChannelMembership", "MessageArchive"]
        )
        entities: list[CanonicalEntity] = []
        warnings: list[str] = []

        if "User" in wanted:
            response = await self.api.request(
                RestRequest(method=HttpMethod.GET, path="/users.list")
            )
            entities.extend(self._map_users((response.body or {}).get("members", [])))

        if "Group" in wanted:
            response = await self.api.request(
                RestRequest(method=HttpMethod.GET, path="/usergroups.list")
            )
            entities.extend(self._map_groups((response.body or {}).get("usergroups", [])))

        if wanted & {"ChatChannel", "ChannelMembership", "MessageArchive"}:
            response = await self.api.request(
                RestRequest(method=HttpMethod.GET, path="/conversations.list")
            )
            channels = (response.body or {}).get("channels", [])
            if "ChatChannel" in wanted:
                entities.extend(self._map_channels(channels))
            if "ChannelMembership" in wanted:
                entities.extend(self._map_memberships(channels))
            if "MessageArchive" in wanted:
                entities.extend(self._map_archives(channels, warnings))

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

    def _source(self, native_type: str, key: str, record: dict[str, Any]) -> SourceRef:
        return SourceRef(
            platform=self.platform,
            instance_id=self.instance_id,
            native_type=native_type,
            native_key=key,
            native_attributes=dict(record),
            api_surface=f"Slack:{native_type}",
        )

    def _id(self, kind: str, key: str) -> str:
        return CanonicalEntity.mint_canonical_id(
            self.platform, kind, key, instance_id=self.instance_id
        )

    def _map_users(self, records: list[dict[str, Any]]) -> list[User]:
        users: list[User] = []
        for record in records:
            if record.get("is_bot") or record.get("deleted"):
                continue
            profile = record.get("profile") or {}
            email = profile.get("email")
            key = email or str(record.get("name") or record.get("id"))
            users.append(
                User(
                    canonical_id=self._id("User", key),
                    display_name=profile.get("real_name") or record.get("name"),
                    user_principal_name=key,
                    email=email,
                    given_name=profile.get("first_name"),
                    surname=profile.get("last_name"),
                    job_title=profile.get("title"),
                    enabled=not record.get("deleted", False),
                    telephony_enabled=False,
                    timezone=record.get("tz"),
                    source_ref=self._source("users.list", str(record.get("id")), record),
                    fidelity=assess_mapping(
                        {**record, **profile},
                        {"id", "name", "deleted", "tz", "is_bot", "profile", "email",
                         "real_name", "first_name", "last_name", "title"},
                        assessed_by=CONNECTOR_ID,
                        entity_label="User",
                        lossless_rationale="Slack identity attributes carry across.",
                    ),
                )
            )
        return users

    def _map_groups(self, records: list[dict[str, Any]]) -> list[Group]:
        return [
            Group(
                canonical_id=self._id("Group", str(record.get("handle") or record.get("id"))),
                display_name=record.get("name"),
                name=str(record.get("name") or record.get("handle")),
                description=record.get("description"),
                group_type=GroupType.SLACK_USER_GROUP,
                source_ref=self._source("usergroups.list", str(record.get("id")), record),
                fidelity=assess_mapping(
                    record,
                    {"id", "handle", "name", "description", "users", "date_create"},
                    assessed_by=CONNECTOR_ID,
                    entity_label="UserGroup",
                    lossless_rationale="Name, handle, and description carry across.",
                ),
            )
            for record in records
        ]

    def _map_channels(self, records: list[dict[str, Any]]) -> list[ChatChannel]:
        channels: list[ChatChannel] = []
        for record in records:
            name = str(record.get("name") or record.get("id"))
            channels.append(
                ChatChannel(
                    canonical_id=self._id("ChatChannel", name),
                    display_name=f"#{name}",
                    name=name,
                    channel_type=_channel_type(record),
                    topic=(record.get("topic") or {}).get("value"),
                    purpose=(record.get("purpose") or {}).get("value"),
                    created_at=_epoch(record.get("created")),
                    archived=bool(record.get("is_archived")),
                    is_general=bool(record.get("is_general")),
                    member_count=record.get("num_members"),
                    externally_shared=bool(record.get("is_ext_shared")),
                    message_archive_ref=self._id("MessageArchive", name),
                    source_ref=self._source("conversations.list", str(record.get("id")), record),
                    fidelity=assess_mapping(
                        record,
                        {"id", "name", "topic", "purpose", "created", "is_archived",
                         "is_general", "num_members", "is_ext_shared", "is_private",
                         "is_mpim", "is_im", "members"},
                        assessed_by=CONNECTOR_ID,
                        entity_label="Channel",
                        lossless_rationale="Channel metadata carries across.",
                    ),
                )
            )
        return channels

    def _map_memberships(self, records: list[dict[str, Any]]) -> list[ChannelMembership]:
        memberships: list[ChannelMembership] = []
        for record in records:
            name = str(record.get("name") or record.get("id"))
            channel_ref = self._id("ChatChannel", name)
            for member in record.get("members") or []:
                memberships.append(
                    ChannelMembership(
                        canonical_id=self._id("ChannelMembership", f"{name}|{member}"),
                        display_name=f"{name} <- {member}",
                        channel_ref=channel_ref,
                        member_ref=self._id("User", str(member)),
                        role=MembershipRole.MEMBER,
                        source_ref=self._source(
                            "conversations.members", f"{name}|{member}", {"member": member}
                        ),
                        fidelity=FidelityAssessment.lossless(
                            "A membership is a channel, a member, and a role.",
                            assessed_by=CONNECTOR_ID,
                        ),
                    )
                )
        return memberships

    def _map_archives(
        self, records: list[dict[str, Any]], warnings: list[str]
    ) -> list[MessageArchive]:
        availability = (
            ExportAvailability.AVAILABLE
            if self.history_export_available
            else ExportAvailability.UNAVAILABLE_PLAN_TIER
        )
        limitation = (
            None
            if self.history_export_available
            else (
                f"Message history export needs the Discovery API, which is a "
                f"{ENTERPRISE_GRID} feature. This workspace is on {self.plan_tier}."
            )
        )
        if limitation:
            warnings.append(
                f"Message history for {len(records)} channel(s) cannot be exported: {limitation}"
            )

        archives: list[MessageArchive] = []
        for record in records:
            name = str(record.get("name") or record.get("id"))
            archives.append(
                MessageArchive(
                    canonical_id=self._id("MessageArchive", name),
                    display_name=f"#{name} history",
                    channel_ref=self._id("ChatChannel", name),
                    conversation_label=name,
                    export_availability=availability,
                    export_limitation=limitation,
                    required_plan_tier=None if self.history_export_available else ENTERPRISE_GRID,
                    contains_personal_data=True,
                    source_ref=self._source(
                        "conversations.history", name, {"channel": name}
                    ),
                    fidelity=FidelityAssessment(
                        level=(
                            FidelityLevel.DEGRADED
                            if self.history_export_available
                            else FidelityLevel.UNMAPPABLE
                        ),
                        rationale=(
                            "History can be exported but threading and reactions rarely "
                            "survive a cross-platform import."
                            if self.history_export_available
                            else f"History cannot leave this workspace without {ENTERPRISE_GRID}."
                        ),
                        degraded_attributes=(
                            [_threading_loss()] if self.history_export_available else []
                        ),
                        # An unexportable channel is not "no work". Somebody has to
                        # decide what happens to the history: retain the workspace
                        # read-only, upgrade the plan for the export, or accept the
                        # loss in writing. That decision is per channel and it is
                        # what this estimate covers.
                        manual_effort_minutes=(
                            None if self.history_export_available else 30
                        ),
                        assessed_by=CONNECTOR_ID,
                        assessed_at=datetime.now(UTC),
                    ),
                )
            )
        return archives

    async def _preview_operation(self, operation: WriteOperation) -> OperationPreview:
        raise NotImplementedError("The Slack connector is Extract-only.")

    async def _execute_operation(self, operation: WriteOperation) -> OperationResult:
        raise NotImplementedError("The Slack connector is Extract-only.")


def _threading_loss() -> DegradedAttribute:
    return DegradedAttribute(
        attribute="message_count",
        reason="threads, reactions, and edits have no common cross-platform representation",
        target_behaviour=(
            "Messages import as a flat transcript. Thread structure and reactions are lost, "
            "which matters most where a thread carries a decision."
        ),
    )


def _channel_type(record: dict[str, Any]) -> ChannelType:
    if record.get("is_ext_shared"):
        return ChannelType.SHARED
    if record.get("is_mpim"):
        return ChannelType.GROUP_DIRECT_MESSAGE
    if record.get("is_im"):
        return ChannelType.DIRECT_MESSAGE
    if record.get("is_private"):
        return ChannelType.PRIVATE
    return ChannelType.PUBLIC


def _epoch(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=UTC)
    except (TypeError, ValueError):
        return None
