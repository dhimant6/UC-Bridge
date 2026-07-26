"""Collaboration domain: channels, membership, message archives, room systems."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import ClassVar, Literal

from pydantic import ConfigDict, Field

from ucm_bridge.canonical.base import CanonicalEntity, CanonicalId
from ucm_bridge.canonical.messaging import ExportAvailability
from ucm_bridge.canonical.registry import canonical_entity

DOMAIN = "collaboration"


class ChannelType(StrEnum):
    PUBLIC = "PUBLIC"
    PRIVATE = "PRIVATE"
    SHARED = "SHARED"
    """Slack Connect / Teams shared channel: has external members, so export scope differs."""
    MULTI_WORKSPACE = "MULTI_WORKSPACE"
    DIRECT_MESSAGE = "DIRECT_MESSAGE"
    GROUP_DIRECT_MESSAGE = "GROUP_DIRECT_MESSAGE"


class MembershipRole(StrEnum):
    OWNER = "OWNER"
    MEMBER = "MEMBER"
    GUEST = "GUEST"
    EXTERNAL = "EXTERNAL"


class RoomSystemType(StrEnum):
    MTR_WINDOWS = "MTR_WINDOWS"
    MTR_ANDROID = "MTR_ANDROID"
    CISCO_ROOM = "CISCO_ROOM"
    ZOOM_ROOM = "ZOOM_ROOM"
    POLY = "POLY"
    OTHER = "OTHER"


class CVIProvider(StrEnum):
    PEXIP = "PEXIP"
    CISCO_WEBEX = "CISCO_WEBEX"
    BLUEJEANS = "BLUEJEANS"
    OTHER = "OTHER"


@canonical_entity
class ChatChannel(CanonicalEntity):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["ChatChannel"] = "ChatChannel"

    name: str
    channel_type: ChannelType = ChannelType.PUBLIC
    workspace_ref: CanonicalId | None = Field(
        default=None, description="Slack workspace or Teams team this channel belongs to."
    )
    topic: str | None = None
    purpose: str | None = None
    created_at: datetime | None = None
    archived: bool = False
    is_general: bool = Field(
        default=False, description="The default channel; usually cannot be renamed or deleted."
    )
    member_count: int | None = Field(default=None, ge=0)
    message_count: int | None = Field(default=None, ge=0)
    externally_shared: bool = False
    message_archive_ref: CanonicalId | None = None


@canonical_entity
class ChannelMembership(CanonicalEntity):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["ChannelMembership"] = "ChannelMembership"

    channel_ref: CanonicalId
    member_ref: CanonicalId
    role: MembershipRole = MembershipRole.MEMBER
    joined_at: datetime | None = None
    is_external: bool = False


@canonical_entity
class MessageArchive(CanonicalEntity):
    """Export manifest for a conversation's history. Payload lives in object storage.

    ``export_availability`` is the field that stops a repatriation quietly
    dropping five years of chat: it must be resolved away from
    NOT_YET_DETERMINED before a plan can be approved.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["MessageArchive"] = "MessageArchive"

    channel_ref: CanonicalId | None = None
    conversation_label: str | None = None
    message_count: int | None = Field(default=None, ge=0)
    first_message_at: datetime | None = None
    last_message_at: datetime | None = None
    export_availability: ExportAvailability = ExportAvailability.NOT_YET_DETERMINED
    export_limitation: str | None = Field(
        default=None,
        description="Required whenever availability is not AVAILABLE. Shown verbatim in the UI.",
    )
    required_plan_tier: str | None = Field(
        default=None,
        description="e.g. 'Slack Enterprise Grid' for Discovery API access. Naming the tier is "
        "more useful to the customer than 'unsupported'.",
    )
    export_format: str | None = None
    export_object_prefix: str | None = None
    exported_message_count: int = Field(default=0, ge=0)
    attachment_count: int | None = Field(default=None, ge=0)
    contains_personal_data: bool = True
    legal_hold: bool = False


@canonical_entity
class FileAttachment(CanonicalEntity):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["FileAttachment"] = "FileAttachment"

    filename: str
    mime_type: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    channel_ref: CanonicalId | None = None
    message_archive_ref: CanonicalId | None = None
    uploaded_by_ref: CanonicalId | None = None
    uploaded_at: datetime | None = None
    object_key: str | None = None
    export_availability: ExportAvailability = ExportAvailability.NOT_YET_DETERMINED
    contains_personal_data: bool = True


@canonical_entity
class MeetingRoom(CanonicalEntity):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["MeetingRoom"] = "MeetingRoom"

    name: str
    room_type: RoomSystemType = RoomSystemType.OTHER
    resource_account_ref: CanonicalId | None = None
    device_ref: CanonicalId | None = None
    capacity: int | None = Field(default=None, ge=0)
    site_code: str | None = None
    emergency_location_ref: CanonicalId | None = None
    room_list: str | None = None
    calendar_processing_mode: str | None = Field(
        default=None, description="e.g. 'AutoAccept'. Governs whether the room self-books."
    )
    peripherals: list[str] = Field(default_factory=list)
    e164_ref: CanonicalId | None = None
    cvi_profile_ref: CanonicalId | None = None


@canonical_entity
class CVIProfile(CanonicalEntity):
    """Cloud Video Interop: lets legacy SIP/H.323 room kit join cloud meetings."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["CVIProfile"] = "CVIProfile"

    name: str
    provider: CVIProvider = CVIProvider.OTHER
    tenant_key: str | None = None
    sip_domain: str | None = None
    dial_string_template: str | None = Field(
        default=None, description="e.g. '{conference_id}@{sip_domain}'."
    )
    one_touch_join_enabled: bool = False
    licensed_seats: int | None = Field(default=None, ge=0)
    assigned_room_refs: list[CanonicalId] = Field(default_factory=list)
