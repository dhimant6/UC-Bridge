"""Policy domain: what users are allowed to do, and where they physically are.

``EmergencyLocation`` (the brief's ``LocationInformation`` / LIS / ELIN / civic
address entity) is the most safety-critical object in the model. It carries an
explicit human confirmation record because the guardrail is that emergency
calling configuration is never migrated silently.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ucm_bridge.canonical.base import CanonicalEntity, CanonicalId
from ucm_bridge.canonical.registry import canonical_entity

DOMAIN = "policy"


class RetentionScope(StrEnum):
    CHAT = "CHAT"
    CHANNEL_MESSAGES = "CHANNEL_MESSAGES"
    VOICEMAIL = "VOICEMAIL"
    CALL_RECORDINGS = "CALL_RECORDINGS"
    MEETING_RECORDINGS = "MEETING_RECORDINGS"
    TRANSCRIPTS = "TRANSCRIPTS"


class RetentionAction(StrEnum):
    DELETE = "DELETE"
    ARCHIVE = "ARCHIVE"
    RETAIN_INDEFINITELY = "RETAIN_INDEFINITELY"


class ComplianceRecordingMode(StrEnum):
    ALWAYS_REQUIRED = "ALWAYS_REQUIRED"
    """Regulatory: the call must not connect if the recorder is unavailable."""
    REQUIRED_BEST_EFFORT = "REQUIRED_BEST_EFFORT"
    USER_CHOICE = "USER_CHOICE"
    DISABLED = "DISABLED"


class EmergencyNotificationMode(StrEnum):
    NOTIFICATION_ONLY = "NOTIFICATION_ONLY"
    CONFERENCE_MUTED = "CONFERENCE_MUTED"
    CONFERENCE_UNMUTED = "CONFERENCE_UNMUTED"
    NONE = "NONE"


class ScreenSharingMode(StrEnum):
    ENTIRE_SCREEN = "ENTIRE_SCREEN"
    SINGLE_APPLICATION = "SINGLE_APPLICATION"
    DISABLED = "DISABLED"


@canonical_entity
class CallingPolicy(CanonicalEntity):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["CallingPolicy"] = "CallingPolicy"

    name: str
    description: str | None = None
    allow_private_calling: bool = True
    allow_call_forwarding_to_user: bool = True
    allow_call_forwarding_to_phone: bool = True
    allow_voicemail: bool = True
    allow_delegation: bool = True
    allow_call_groups: bool = True
    busy_on_busy: bool | None = None
    music_on_hold_enabled: bool | None = None
    allow_web_pstn_calling: bool | None = None
    international_calling_allowed: bool | None = None
    preventing_toll_bypass: bool | None = None
    is_global_default: bool = False
    derived_from: str | None = Field(
        default=None,
        description="Native construct, e.g. 'CUCM:CSS+COR', 'SfB:CsVoicePolicy'. Kept so the "
        "reverse transform can aim at the right on-prem construct.",
    )


@canonical_entity
class MeetingPolicy(CanonicalEntity):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["MeetingPolicy"] = "MeetingPolicy"

    name: str
    description: str | None = None
    allow_recording: bool = True
    allow_transcription: bool | None = None
    auto_admitted_users: str | None = Field(
        default=None, description="Lobby bypass scope, e.g. 'EveryoneInCompany'."
    )
    allow_anonymous_join: bool | None = None
    allow_anonymous_start: bool | None = None
    screen_sharing_mode: ScreenSharingMode | None = None
    max_participants: int | None = Field(default=None, ge=0)
    allow_external_participants: bool | None = None
    is_global_default: bool = False


@canonical_entity
class MessagingPolicy(CanonicalEntity):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["MessagingPolicy"] = "MessagingPolicy"

    name: str
    description: str | None = None
    allow_user_chat: bool = True
    allow_user_edit_messages: bool | None = None
    allow_user_delete_messages: bool | None = None
    allow_owner_delete_messages: bool | None = None
    read_receipts_mode: str | None = None
    allow_external_chat: bool | None = None
    allow_file_sharing: bool | None = None
    retention_policy_ref: CanonicalId | None = None
    is_global_default: bool = False


@canonical_entity
class ComplianceRecordingPolicy(CanonicalEntity):
    """Regulatory call recording (MiFID II, Dodd-Frank, FCA). Rarely maps cleanly."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["ComplianceRecordingPolicy"] = "ComplianceRecordingPolicy"

    name: str
    mode: ComplianceRecordingMode = ComplianceRecordingMode.DISABLED
    scope_refs: list[CanonicalId] = Field(default_factory=list)
    recorder_vendor: str | None = None
    recorder_integration: str | None = Field(
        default=None, description="How the recorder attaches, e.g. 'SIPREC', 'policy-based bot'."
    )
    required_by_regulation: str | None = Field(
        default=None, description="Name the regulation. It changes who signs off."
    )
    notification_mode: str | None = None
    retention_policy_ref: CanonicalId | None = None
    target_equivalent_available: bool | None = Field(
        default=None,
        description="None until assessed. False is a migration blocker, not a degradation: "
        "an unrecorded regulated call is an unlawful call.",
    )
    affected_user_count: int | None = Field(default=None, ge=0)


@canonical_entity
class RetentionPolicy(CanonicalEntity):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["RetentionPolicy"] = "RetentionPolicy"

    name: str
    scopes: list[RetentionScope] = Field(default_factory=list)
    retention_days: int | None = Field(default=None, ge=0)
    action_on_expiry: RetentionAction = RetentionAction.DELETE
    legal_hold_exempt: bool = False
    applies_to_group_refs: list[CanonicalId] = Field(default_factory=list)
    description: str | None = None


@canonical_entity
class EmergencyCallingPolicy(CanonicalEntity):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["EmergencyCallingPolicy"] = "EmergencyCallingPolicy"

    name: str
    notification_mode: EmergencyNotificationMode = EmergencyNotificationMode.NONE
    notification_group_refs: list[CanonicalId] = Field(default_factory=list)
    notification_dial_out_number_ref: CanonicalId | None = None
    external_lookup_enabled: bool = False
    dial_string: str | None = None
    dial_mask: str | None = None
    site_code: str | None = None
    is_global_default: bool = False


class CivicAddress(BaseModel):
    """A dispatchable street address. Not a mailing address: a fire engine must find it."""

    model_config = ConfigDict(extra="forbid")

    country: str = Field(min_length=2, max_length=2, description="ISO 3166-1 alpha-2.")
    house_number: str | None = None
    street_name: str | None = None
    city: str | None = None
    state_or_province: str | None = None
    postal_code: str | None = None
    sub_unit: str | None = Field(
        default=None,
        description="Floor, wing, room. The difference between a found and a lost casualty.",
    )
    full_text: str | None = Field(
        default=None, description="Verbatim source address string, retained for comparison."
    )
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)

    @property
    def is_dispatchable(self) -> bool:
        """Minimum viable for dispatch: a street, a settlement, and a country."""
        return bool(self.street_name and self.city and self.country)


class NetworkIdentifiers(BaseModel):
    """Network signals that place a nomadic endpoint at a location automatically."""

    model_config = ConfigDict(extra="forbid")

    subnets: list[str] = Field(default_factory=list)
    wifi_bssids: list[str] = Field(default_factory=list)
    switch_chassis_ids: list[str] = Field(default_factory=list)
    switch_ports: list[str] = Field(default_factory=list)


@canonical_entity
class EmergencyLocation(CanonicalEntity):
    """LIS / ELIN / civic address record for one place.

    The brief calls this ``LocationInformation``; the class is named for what it
    is used for. ``kind`` is ``"EmergencyLocation"`` and that string is the
    stable wire identity.

    Two invariants are enforced here rather than in the execution engine, because
    an engine bug must not be able to route around them:

    * A location claiming to be validated must say who validated it and when.
    * A location may not be marked ``confirmed_for_migration`` without a named
      confirmer and a timestamp. The execution engine refuses to apply any plan
      touching an unconfirmed location.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["EmergencyLocation"] = "EmergencyLocation"

    name: str
    site_code: str = Field(
        description="Ties the location to users, numbers, and rooms. The unit of per-site "
        "emergency confirmation."
    )
    civic_address: CivicAddress
    elin_number_ref: CanonicalId | None = Field(
        default=None, description="Callback number the PSAP sees. Often shared across a floor."
    )
    emergency_calling_policy_ref: CanonicalId | None = None

    is_validated: bool = Field(
        default=False,
        description="Validated by the carrier or emergency-services database, not by us.",
    )
    validation_authority: str | None = None
    validated_at: datetime | None = None

    network_identifiers: NetworkIdentifiers = Field(default_factory=NetworkIdentifiers)
    supports_dynamic_location: bool = Field(
        default=False,
        description="Whether nomadic users at this location get an automatic address. Where "
        "false, remote and hot-desking users need a manual disposition.",
    )

    assigned_user_refs: list[CanonicalId] = Field(default_factory=list)
    assigned_number_refs: list[CanonicalId] = Field(default_factory=list)

    confirmed_for_migration: bool = Field(
        default=False,
        description="Explicit human sign-off that this location's emergency configuration is "
        "correct for the target. Never set programmatically.",
    )
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None

    @model_validator(mode="after")
    def _confirmations_have_provenance(self) -> Self:
        if self.is_validated and not (self.validation_authority and self.validated_at):
            raise ValueError(
                "is_validated requires validation_authority and validated_at; an unattributed "
                "validation claim is not evidence."
            )
        if self.confirmed_for_migration and not (self.confirmed_by and self.confirmed_at):
            raise ValueError(
                "confirmed_for_migration requires confirmed_by and confirmed_at. Emergency "
                "configuration is never confirmed anonymously."
            )
        return self
