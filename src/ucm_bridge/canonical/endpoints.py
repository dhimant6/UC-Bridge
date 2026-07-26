"""Endpoint domain: physical and soft devices, and the lines that appear on them."""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from ucm_bridge.canonical.base import CanonicalEntity, CanonicalId
from ucm_bridge.canonical.registry import canonical_entity

DOMAIN = "endpoints"


class DeviceType(StrEnum):
    HARD_PHONE = "HARD_PHONE"
    SOFT_PHONE = "SOFT_PHONE"
    ROOM_SYSTEM = "ROOM_SYSTEM"
    CONFERENCE_PHONE = "CONFERENCE_PHONE"
    DECT = "DECT"
    ATA = "ATA"
    """Analogue Telephone Adapter fronting analogue endpoints."""
    ANALOGUE = "ANALOGUE"
    """Fax, lift phone, door entry, alarm panel, overhead paging. Rarely migrates cleanly."""
    PAGING = "PAGING"
    FAX = "FAX"
    MOBILE_CLIENT = "MOBILE_CLIENT"
    SIP_GENERIC = "SIP_GENERIC"


class RegistrationState(StrEnum):
    REGISTERED = "REGISTERED"
    UNREGISTERED = "UNREGISTERED"
    REJECTED = "REJECTED"
    PARTIALLY_REGISTERED = "PARTIALLY_REGISTERED"
    UNKNOWN = "UNKNOWN"


class SignallingProtocol(StrEnum):
    SIP = "SIP"
    SCCP = "SCCP"
    H323 = "H323"
    MGCP = "MGCP"
    H248 = "H248"
    PROPRIETARY = "PROPRIETARY"
    ANALOGUE = "ANALOGUE"


class RingSetting(StrEnum):
    RING = "RING"
    FLASH_ONLY = "FLASH_ONLY"
    BEEP_ONLY = "BEEP_ONLY"
    DISABLE = "DISABLE"
    RING_ONCE = "RING_ONCE"


@canonical_entity
class Device(CanonicalEntity):
    """Any endpoint that registers, or that occupies a port."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["Device"] = "Device"

    device_name: str = Field(description="Native device name, e.g. 'SEP001122334455'.")
    mac_address: str | None = Field(
        default=None, description="Normalised uppercase hex, no separators."
    )
    vendor: str | None = None
    model: str | None = None
    device_type: DeviceType = DeviceType.HARD_PHONE
    protocol: SignallingProtocol = SignallingProtocol.SIP
    owner_ref: CanonicalId | None = None
    site_code: str | None = None

    device_pool_ref: CanonicalId | None = None
    sip_profile_ref: CanonicalId | None = None
    button_template_ref: CanonicalId | None = None
    firmware_ref: CanonicalId | None = None
    line_refs: list[CanonicalId] = Field(
        default_factory=list, description="Ordered by button position."
    )
    security_profile: str | None = None
    registration_state: RegistrationState = RegistrationState.UNKNOWN
    last_registered_at: date | None = None

    analogue_port: str | None = Field(
        default=None, description="Gateway slot/subunit/port for analogue endpoints."
    )
    gateway_ref: CanonicalId | None = None

    replacement_required: bool = Field(
        default=False,
        description="Set by assessment: the model cannot register against the target and must "
        "be replaced or reflashed. Drives hardware budget in the estate report.",
    )
    target_equivalent_model: str | None = Field(
        default=None, description="Suggested replacement, where one exists."
    )


@canonical_entity
class DeviceProfile(CanonicalEntity):
    """A device configuration not bound to hardware: the Extension Mobility payload."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["DeviceProfile"] = "DeviceProfile"

    name: str
    model: str | None = None
    user_ref: CanonicalId | None = None
    line_refs: list[CanonicalId] = Field(default_factory=list)
    button_template_ref: CanonicalId | None = None
    description: str | None = None


@canonical_entity
class DevicePool(CanonicalEntity):
    """Grouping that carries regional, redundancy, and survivability settings."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["DevicePool"] = "DevicePool"

    name: str
    site_code: str | None = None
    region: str | None = None
    timezone: str | None = None
    date_time_group: str | None = None
    srst_reference: str | None = Field(
        default=None,
        description="Survivable Remote Site Telephony fallback. Has no cloud equivalent; a "
        "site relying on it loses local survivability on cutover.",
    )
    media_resource_group_list: str | None = None
    calling_permission_ref: CanonicalId | None = None
    device_count: int | None = Field(default=None, ge=0)


@canonical_entity
class SIPProfile(CanonicalEntity):
    """SIP behaviour knobs applied to devices and trunks."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["SIPProfile"] = "SIPProfile"

    name: str
    early_offer_enabled: bool | None = None
    options_ping_enabled: bool | None = None
    options_ping_interval_seconds: int | None = Field(default=None, ge=0)
    mtp_required: bool | None = None
    rtp_port_range_start: int | None = Field(default=None, ge=0, le=65535)
    rtp_port_range_end: int | None = Field(default=None, ge=0, le=65535)
    timer_settings: dict[str, int] = Field(
        default_factory=dict, description="SIP timers in milliseconds, keyed by native timer name."
    )
    additional_settings: dict[str, str] = Field(
        default_factory=dict,
        description="Remaining vendor knobs, retained rather than dropped. Anything left here "
        "at apply time is reported as a degraded attribute.",
    )


@canonical_entity
class Line(CanonicalEntity):
    """A directory number as it appears on a device or profile.

    Distinct from Extension: an Extension is the dialable string; a Line is one
    appearance of it, with its own label, forwarding, and ring behaviour.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["Line"] = "Line"

    directory_number: str
    site_code: str | None = Field(
        default=None,
        description="Owning site. Directory numbers are only unique within a site on most "
        "on-prem estates, so the natural key is usually site + number.",
    )
    partition_ref: CanonicalId | None = None
    extension_ref: CanonicalId | None = None
    e164_ref: CanonicalId | None = None
    owner_ref: CanonicalId | None = None
    device_ref: CanonicalId | None = None
    line_index: int = Field(default=1, ge=1, description="Button position on the device.")

    label: str | None = None
    alerting_name: str | None = None
    display_name: str | None = Field(
        default=None, description="Caller ID name presented on internal calls."
    )
    external_presentation_number_ref: CanonicalId | None = None

    max_num_calls: int | None = Field(default=None, ge=1)
    busy_trigger: int | None = Field(default=None, ge=1)
    ring_setting_active: RingSetting = RingSetting.RING
    ring_setting_idle: RingSetting = RingSetting.RING

    shared_appearance_ref: CanonicalId | None = None
    forwarding_rule_refs: list[CanonicalId] = Field(default_factory=list)
    voicemail_box_ref: CanonicalId | None = None
    voicemail_profile: str | None = None
    calling_permission_ref: CanonicalId | None = None

    @property
    def is_shared(self) -> bool:
        return self.shared_appearance_ref is not None


@canonical_entity
class SharedLineAppearance(CanonicalEntity):
    """One directory number appearing on multiple devices.

    A dependency-integrity anchor: every user holding an appearance of the same
    line must migrate in the same wave, or the line breaks mid-cutover.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["SharedLineAppearance"] = "SharedLineAppearance"

    directory_number: str
    partition_ref: CanonicalId | None = None
    line_refs: list[CanonicalId] = Field(default_factory=list)
    device_refs: list[CanonicalId] = Field(default_factory=list)
    user_refs: list[CanonicalId] = Field(default_factory=list)
    appearance_count: int = Field(default=0, ge=0)
    privacy_enabled: bool = False
    barge_enabled: bool = False
    remote_hold_supported: bool = True


class ButtonAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=1)
    feature: str = Field(description="'Line', 'SpeedDial', 'BLF', 'Intercom', 'Park', 'None'.")
    label: str | None = None
    target_ref: CanonicalId | None = None
    target_value: str | None = None


@canonical_entity
class ButtonTemplate(CanonicalEntity):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["ButtonTemplate"] = "ButtonTemplate"

    name: str
    model: str | None = None
    buttons: list[ButtonAssignment] = Field(default_factory=list)


@canonical_entity
class Firmware(CanonicalEntity):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["Firmware"] = "Firmware"

    model: str
    version: str
    load_id: str | None = None
    protocol: SignallingProtocol | None = None
    device_count: int | None = Field(default=None, ge=0)
    supported_on_target: bool | None = Field(
        default=None,
        description="None until assessed. False means the fleet needs a firmware campaign "
        "before cutover, which is a schedule item, not a footnote.",
    )
    required_minimum_for_target: str | None = None
