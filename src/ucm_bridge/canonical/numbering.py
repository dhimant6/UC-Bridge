"""Numbering domain: extensions, E.164 numbers, ranges, porting, emergency dial strings."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import ClassVar, Literal

from pydantic import ConfigDict, Field, model_validator

from ucm_bridge.canonical.base import E164, CanonicalEntity, CanonicalId
from ucm_bridge.canonical.registry import canonical_entity

DOMAIN = "numbering"


class NumberType(StrEnum):
    DID = "DID"
    MAIN_NUMBER = "MAIN_NUMBER"
    SERVICE_NUMBER = "SERVICE_NUMBER"
    """Numbers for auto attendants, call queues, conference bridges."""
    TOLL_FREE = "TOLL_FREE"
    ELIN = "ELIN"
    """Emergency Location Identification Number: callback number presented to the PSAP."""
    FAX = "FAX"
    ANALOGUE_SERVICE = "ANALOGUE_SERVICE"
    """Lift phones, alarm lines, door entry, overhead paging. Classic cutover breakers."""


class NumberAssignmentState(StrEnum):
    UNASSIGNED = "UNASSIGNED"
    ASSIGNED = "ASSIGNED"
    RESERVED = "RESERVED"
    PORTING_OUT = "PORTING_OUT"
    PORTING_IN = "PORTING_IN"
    DUAL_HOMED = "DUAL_HOMED"
    """Exists in both estates during a cutover window. Legal state, but time-boxed."""


class NumberAssignmentKind(StrEnum):
    USER = "USER"
    RESOURCE_ACCOUNT = "RESOURCE_ACCOUNT"
    AUTO_ATTENDANT = "AUTO_ATTENDANT"
    CALL_QUEUE = "CALL_QUEUE"
    DEVICE = "DEVICE"
    CONFERENCE_BRIDGE = "CONFERENCE_BRIDGE"
    UNASSIGNED = "UNASSIGNED"


class AcquisitionModel(StrEnum):
    """How the number is provisioned on the target. Drives very different apply logic."""

    CALLING_PLAN = "CALLING_PLAN"
    OPERATOR_CONNECT = "OPERATOR_CONNECT"
    DIRECT_ROUTING = "DIRECT_ROUTING"
    ON_PREM_TRUNK = "ON_PREM_TRUNK"
    UNKNOWN = "UNKNOWN"


@canonical_entity
class E164Number(CanonicalEntity):
    """A globally routable number in strict E.164 form."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["E164Number"] = "E164Number"

    e164: E164
    country_code: str | None = Field(default=None, description="ISO 3166-1 alpha-2, e.g. 'DE'.")
    number_type: NumberType = NumberType.DID
    assignment_state: NumberAssignmentState = NumberAssignmentState.UNASSIGNED
    assignment_kind: NumberAssignmentKind = NumberAssignmentKind.UNASSIGNED
    assigned_to_ref: CanonicalId | None = None
    extension_ref: CanonicalId | None = None
    site_code: str | None = None
    carrier_ref: CanonicalId | None = None
    acquisition_model: AcquisitionModel = AcquisitionModel.UNKNOWN
    emergency_location_ref: CanonicalId | None = Field(
        default=None,
        description="Required before this number may be applied to any target that offers "
        "emergency calling. A missing value is a hard validation failure, never a warning.",
    )
    range_ref: CanonicalId | None = None
    activated: bool = True

    @model_validator(mode="after")
    def _assignment_consistency(self) -> E164Number:
        assigned = self.assignment_state in (
            NumberAssignmentState.ASSIGNED,
            NumberAssignmentState.DUAL_HOMED,
        )
        if assigned and self.assignment_kind is NumberAssignmentKind.UNASSIGNED:
            raise ValueError(
                f"{self.e164} is {self.assignment_state} but assignment_kind is UNASSIGNED; "
                "an assigned number must say what it is assigned to."
            )
        return self


@canonical_entity
class Extension(CanonicalEntity):
    """An internal dialable string. Not globally routable on its own."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["Extension"] = "Extension"

    digits: str = Field(pattern=r"^[0-9*#]{1,32}$")
    site_code: str | None = None
    partition_ref: CanonicalId | None = None
    e164_ref: CanonicalId | None = Field(
        default=None,
        description="None means this extension has no external number. That is a blocker for "
        "Teams Phone and is reported as such by the assessment engine.",
    )
    owner_ref: CanonicalId | None = None
    is_shared: bool = False
    urgent: bool = False
    description: str | None = None

    @property
    def length(self) -> int:
        return len(self.digits)


@canonical_entity
class DIDRange(CanonicalEntity):
    """A contiguous block of E.164 numbers obtained from a carrier."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["DIDRange"] = "DIDRange"

    start_e164: E164
    end_e164: E164
    count: int = Field(ge=1)
    carrier_ref: CanonicalId | None = None
    site_code: str | None = None
    purpose: str | None = None
    utilised_count: int | None = Field(
        default=None, ge=0, description="Populated by discovery; drives DN utilisation reporting."
    )


@canonical_entity
class NumberBlock(CanonicalEntity):
    """A dial-plan-level block of internal numbers, mapped to an E.164 prefix per site.

    This is the anchor for the number-normalisation engine: extension 4xxxx at
    site MUC-HQ becomes +4989xxxxx because a NumberBlock says so.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["NumberBlock"] = "NumberBlock"

    site_code: str
    internal_prefix: str = Field(description="Leading digits of the internal range, e.g. '4'.")
    extension_length: int = Field(ge=1, le=32)
    e164_prefix: str = Field(
        pattern=r"^\+[1-9]\d{0,14}$",
        description="External prefix the internal digits append to, e.g. '+4989'.",
    )
    strip_digits: int = Field(
        default=0, ge=0, description="Internal digits discarded before appending to the prefix."
    )
    region: str | None = None
    carrier_ref: CanonicalId | None = None
    overlaps_with_refs: list[CanonicalId] = Field(
        default_factory=list,
        description="Populated by the overlap detector. Non-empty means the dial plan is "
        "ambiguous and normalisation cannot be trusted until resolved.",
    )


class PortOrderState(StrEnum):
    """Port order lifecycle. Deliberately explicit: a stuck port is a stuck cutover."""

    DRAFT = "DRAFT"
    LOA_PENDING = "LOA_PENDING"
    SUBMITTED = "SUBMITTED"
    CARRIER_VALIDATING = "CARRIER_VALIDATING"
    REJECTED = "REJECTED"
    FOC_RECEIVED = "FOC_RECEIVED"
    """Firm Order Commitment: the losing carrier has agreed a date."""
    SCHEDULED = "SCHEDULED"
    IN_CUTOVER = "IN_CUTOVER"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


@canonical_entity
class PortingRecord(CanonicalEntity):
    """A number-porting order. Central to cloud -> on-prem repatriation (§3.7)."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["PortingRecord"] = "PortingRecord"

    order_reference: str | None = None
    number_refs: list[CanonicalId] = Field(default_factory=list)
    numbers: list[E164] = Field(
        default_factory=list, description="Denormalised for the LOA packet and carrier CSR form."
    )
    losing_carrier: str | None = None
    gaining_carrier: str | None = None
    state: PortOrderState = PortOrderState.DRAFT
    loa_reference: str | None = Field(
        default=None, description="Letter of Authority document id in object storage."
    )
    csr_fields: dict[str, str] = Field(
        default_factory=dict,
        description="Customer Service Record data the carrier requires: billing telephone "
        "number, account number, service address, authorised signatory.",
    )
    requested_foc_date: date | None = None
    confirmed_foc_date: date | None = None
    cutover_window_start: datetime | None = None
    cutover_window_end: datetime | None = None
    dual_homed_during_cutover: bool = Field(
        default=False,
        description="True while the number is live in both estates. Coexistence routing must "
        "account for this or calls fork.",
    )
    rejection_reason: str | None = None
    notes: str | None = None


@canonical_entity
class EmergencyNumber(CanonicalEntity):
    """An emergency dial string and how it is recognised and translated."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["EmergencyNumber"] = "EmergencyNumber"

    dial_string: str = Field(description="What the user dials: '911', '112', '999', '000'.")
    country_code: str | None = Field(default=None, description="ISO 3166-1 alpha-2.")
    site_code: str | None = None
    translated_to: str | None = Field(
        default=None, description="Digits actually sent to the PSTN after manipulation."
    )
    requires_outside_line_prefix: bool = Field(
        default=False,
        description="True where users must dial 9 first. Both forms must be routable; "
        "'9911' failing is a documented cause of fatal outcomes.",
    )
    elin_number_ref: CanonicalId | None = None
    notification_group_refs: list[CanonicalId] = Field(
        default_factory=list, description="Security desk / reception notified on an emergency call."
    )
    routing_policy_ref: CanonicalId | None = None
