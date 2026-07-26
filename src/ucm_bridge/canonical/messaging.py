"""Messaging domain: voicemail boxes, greetings, MWI, and message payload export.

Message bodies are personal data. The canonical model never inlines audio or
transcripts; it carries object-storage keys plus counts, and records explicitly
whether export was even possible on the source plan. A repatriation that
silently loses voicemail is a compliance incident, so 'unknown' is not an
acceptable value for ``export_available`` at plan time.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from ucm_bridge.canonical.base import CanonicalEntity, CanonicalId
from ucm_bridge.canonical.registry import canonical_entity

DOMAIN = "messaging"


class GreetingType(StrEnum):
    STANDARD = "STANDARD"
    BUSY = "BUSY"
    CLOSED = "CLOSED"
    HOLIDAY = "HOLIDAY"
    INTERNAL = "INTERNAL"
    ALTERNATE = "ALTERNATE"
    EXTENDED_ABSENCE = "EXTENDED_ABSENCE"


class GreetingSource(StrEnum):
    RECORDED_AUDIO = "RECORDED_AUDIO"
    TEXT_TO_SPEECH = "TEXT_TO_SPEECH"
    SYSTEM_DEFAULT = "SYSTEM_DEFAULT"


class MWIMethod(StrEnum):
    SIP_NOTIFY = "SIP_NOTIFY"
    UNSOLICITED_NOTIFY = "UNSOLICITED_NOTIFY"
    DN_BASED = "DN_BASED"
    STUTTER_DIAL_TONE = "STUTTER_DIAL_TONE"
    NONE = "NONE"


class ExportAvailability(StrEnum):
    """Whether bulk export of message payloads is possible. Never guess this."""

    AVAILABLE = "AVAILABLE"
    UNAVAILABLE_PLAN_TIER = "UNAVAILABLE_PLAN_TIER"
    """The customer's licence tier does not expose the export API at all."""
    UNAVAILABLE_NO_API = "UNAVAILABLE_NO_API"
    UNAVAILABLE_PERMISSION = "UNAVAILABLE_PERMISSION"
    PARTIAL = "PARTIAL"
    NOT_YET_DETERMINED = "NOT_YET_DETERMINED"
    """Valid during discovery. Must be resolved before a plan may be approved."""


class Greeting(BaseModel):
    model_config = ConfigDict(extra="forbid")

    greeting_type: GreetingType
    enabled: bool = True
    source: GreetingSource = GreetingSource.SYSTEM_DEFAULT
    audio_object_key: str | None = Field(
        default=None, description="Object-storage key. Audio is never inlined in the model."
    )
    text: str | None = Field(default=None, description="TTS text, where the source uses TTS.")
    duration_seconds: float | None = Field(default=None, ge=0)
    language: str | None = None
    callers_can_skip: bool | None = None


@canonical_entity
class VoicemailBox(CanonicalEntity):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["VoicemailBox"] = "VoicemailBox"

    mailbox_id: str
    owner_ref: CanonicalId | None = None
    extension_ref: CanonicalId | None = None
    e164_ref: CanonicalId | None = None
    display_name_override: str | None = None

    greeting_set_ref: CanonicalId | None = None
    mwi_config_ref: CanonicalId | None = None
    message_store_ref: CanonicalId | None = None
    transcription_setting_ref: CanonicalId | None = None

    quota_bytes: int | None = Field(default=None, ge=0)
    used_bytes: int | None = Field(default=None, ge=0)
    message_count: int | None = Field(default=None, ge=0)
    retention_days: int | None = Field(default=None, ge=0)
    language: str | None = None
    timezone: str | None = None
    pin_reset_required_on_migration: bool = Field(
        default=True,
        description="PINs are never migrated: they are hashed at source and would be a "
        "credential transfer if they were not. Users get a reset.",
    )
    notification_targets: list[str] = Field(default_factory=list)


@canonical_entity
class GreetingSet(CanonicalEntity):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["GreetingSet"] = "GreetingSet"

    mailbox_ref: CanonicalId | None = None
    greetings: list[Greeting] = Field(default_factory=list)
    default_language: str | None = None


@canonical_entity
class MWIConfig(CanonicalEntity):
    """Message Waiting Indicator mechanics. Method rarely survives a platform change."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["MWIConfig"] = "MWIConfig"

    mailbox_ref: CanonicalId | None = None
    method: MWIMethod = MWIMethod.SIP_NOTIFY
    mwi_on_dn: str | None = None
    mwi_off_dn: str | None = None
    partition_ref: CanonicalId | None = None
    enabled: bool = True


@canonical_entity
class MessageStore(CanonicalEntity):
    """The actual voicemail payload set for one mailbox, as an export manifest."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["MessageStore"] = "MessageStore"

    mailbox_ref: CanonicalId | None = None
    message_count: int = Field(default=0, ge=0)
    total_bytes: int | None = Field(default=None, ge=0)
    oldest_message_at: datetime | None = None
    newest_message_at: datetime | None = None
    export_availability: ExportAvailability = ExportAvailability.NOT_YET_DETERMINED
    export_limitation: str | None = Field(
        default=None,
        description="Human-readable reason shown in the UI when export is not fully possible. "
        "Required whenever availability is not AVAILABLE.",
    )
    export_format: str | None = Field(default=None, description="e.g. 'wav+metadata.json'.")
    export_object_prefix: str | None = None
    exported_count: int = Field(default=0, ge=0)
    contains_personal_data: bool = Field(
        default=True,
        description="Always true in practice. Drives encryption-at-rest and retention handling.",
    )


@canonical_entity
class TranscriptionSetting(CanonicalEntity):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["TranscriptionSetting"] = "TranscriptionSetting"

    scope_ref: CanonicalId | None = Field(
        default=None, description="Mailbox, user, or org-wide policy this applies to."
    )
    enabled: bool = False
    provider: str | None = None
    language: str | None = None
    profanity_masking: bool | None = None
    redaction_enabled: bool | None = None
