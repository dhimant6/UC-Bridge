"""Call handling domain: how a call reaches a human once it is inside the estate.

HuntGroup and CallQueue are deliberately kept as separate entities rather than
collapsed. A CUCM hunt pilot + line group and a Teams call queue are not the same
object: the former has no agent presence model and no queued-caller experience,
the latter has no line-group chaining. Collapsing them would hide exactly the
degradation the fidelity report exists to surface.
"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from ucm_bridge.canonical.base import CanonicalEntity, CanonicalId
from ucm_bridge.canonical.dialplan import DistributionAlgorithm
from ucm_bridge.canonical.registry import canonical_entity

DOMAIN = "callhandling"


class NoAnswerAction(StrEnum):
    TRY_NEXT_MEMBER = "TRY_NEXT_MEMBER"
    STOP_HUNTING = "STOP_HUNTING"
    SKIP_REMAINING = "SKIP_REMAINING"


class OverflowAction(StrEnum):
    DISCONNECT = "DISCONNECT"
    FORWARD_TO_USER = "FORWARD_TO_USER"
    FORWARD_TO_NUMBER = "FORWARD_TO_NUMBER"
    FORWARD_TO_VOICEMAIL = "FORWARD_TO_VOICEMAIL"
    FORWARD_TO_QUEUE = "FORWARD_TO_QUEUE"
    FORWARD_TO_AUTO_ATTENDANT = "FORWARD_TO_AUTO_ATTENDANT"
    QUEUE = "QUEUE"


class ForwardCondition(StrEnum):
    ALWAYS = "ALWAYS"
    BUSY = "BUSY"
    NO_ANSWER = "NO_ANSWER"
    UNREGISTERED = "UNREGISTERED"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    DND = "DND"


class DelegationPermission(StrEnum):
    MAKE_CALLS = "MAKE_CALLS"
    RECEIVE_CALLS = "RECEIVE_CALLS"
    CHANGE_SETTINGS = "CHANGE_SETTINGS"
    MANAGE_DELEGATES = "MANAGE_DELEGATES"


class MenuAction(StrEnum):
    TRANSFER_TO_USER = "TRANSFER_TO_USER"
    TRANSFER_TO_QUEUE = "TRANSFER_TO_QUEUE"
    TRANSFER_TO_ATTENDANT = "TRANSFER_TO_ATTENDANT"
    TRANSFER_TO_EXTERNAL = "TRANSFER_TO_EXTERNAL"
    TRANSFER_TO_VOICEMAIL = "TRANSFER_TO_VOICEMAIL"
    PLAY_PROMPT = "PLAY_PROMPT"
    DIRECTORY_SEARCH = "DIRECTORY_SEARCH"
    REPEAT_MENU = "REPEAT_MENU"
    DISCONNECT = "DISCONNECT"
    RUN_SUBFLOW = "RUN_SUBFLOW"
    """Vector / script logic with no declarative equivalent. Always DEGRADED at minimum."""


class OverflowRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    threshold: int = Field(ge=0, description="Callers waiting, or seconds, per rule_basis.")
    rule_basis: Literal["CALLS_WAITING", "WAIT_SECONDS"] = "CALLS_WAITING"
    action: OverflowAction = OverflowAction.DISCONNECT
    target_ref: CanonicalId | None = None
    target_value: str | None = None
    shared_voicemail_transcription: bool | None = None


@canonical_entity
class LineGroup(CanonicalEntity):
    """An ordered set of lines with a hunting algorithm. Building block of a HuntGroup."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["LineGroup"] = "LineGroup"

    name: str
    distribution_algorithm: DistributionAlgorithm = DistributionAlgorithm.TOP_DOWN
    member_line_refs: list[CanonicalId] = Field(default_factory=list)
    rna_reversion_timeout_seconds: int | None = Field(default=None, ge=0)
    on_no_answer: NoAnswerAction = NoAnswerAction.TRY_NEXT_MEMBER
    on_busy: NoAnswerAction = NoAnswerAction.TRY_NEXT_MEMBER
    on_not_available: NoAnswerAction = NoAnswerAction.TRY_NEXT_MEMBER
    auto_logout_on_rna: bool = False


@canonical_entity
class HuntGroup(CanonicalEntity):
    """A pilot number fronting a chain of line groups (CUCM hunt pilot, Avaya hunt group)."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["HuntGroup"] = "HuntGroup"

    name: str
    pilot_pattern: str
    partition_ref: CanonicalId | None = None
    e164_ref: CanonicalId | None = None
    line_group_refs: list[CanonicalId] = Field(
        default_factory=list, description="Ordered hunt chain."
    )
    max_hunt_timer_seconds: int | None = Field(default=None, ge=0)
    forward_on_hunt_no_answer_ref: CanonicalId | None = None
    forward_on_hunt_busy_ref: CanonicalId | None = None
    queuing_enabled: bool = False
    display_name: str | None = None
    site_code: str | None = None


@canonical_entity
class CallQueue(CanonicalEntity):
    """A cloud-style queue with an agent model and a caller-waiting experience."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["CallQueue"] = "CallQueue"

    name: str
    resource_account_ref: CanonicalId | None = None
    e164_ref: CanonicalId | None = None
    routing_method: DistributionAlgorithm = DistributionAlgorithm.TOP_DOWN
    agent_refs: list[CanonicalId] = Field(default_factory=list)
    agent_group_refs: list[CanonicalId] = Field(default_factory=list)
    agent_alert_time_seconds: int | None = Field(default=None, ge=0)
    presence_based_routing: bool = False
    allow_opt_out: bool = True
    conference_mode: bool | None = None

    greeting_prompt_ref: CanonicalId | None = None
    music_on_hold_ref: CanonicalId | None = None
    overflow: OverflowRule | None = None
    timeout: OverflowRule | None = None
    no_agents_rule: OverflowRule | None = None
    language: str | None = None
    site_code: str | None = None


@canonical_entity
class PickupGroup(CanonicalEntity):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["PickupGroup"] = "PickupGroup"

    name: str
    pickup_number: str | None = None
    partition_ref: CanonicalId | None = None
    member_line_refs: list[CanonicalId] = Field(default_factory=list)
    associated_group_refs: list[CanonicalId] = Field(
        default_factory=list, description="Other groups reachable via directed/other-group pickup."
    )
    notification_policy: str | None = None


class MenuOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dtmf: str = Field(pattern=r"^[0-9*#]$|^TIMEOUT$|^INVALID$")
    action: MenuAction
    target_ref: CanonicalId | None = None
    target_value: str | None = None
    voice_response: str | None = Field(
        default=None, description="Spoken alternative to the DTMF key, where supported."
    )
    description: str | None = None


@canonical_entity
class AutoAttendant(CanonicalEntity):
    """A menu tree. Vectors and UCCX scripts land here, usually DEGRADED."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["AutoAttendant"] = "AutoAttendant"

    name: str
    resource_account_ref: CanonicalId | None = None
    e164_ref: CanonicalId | None = None
    language: str | None = None
    timezone: str | None = None
    operator_ref: CanonicalId | None = None

    business_hours_schedule_ref: CanonicalId | None = None
    holiday_schedule_refs: list[CanonicalId] = Field(default_factory=list)
    business_hours_greeting_ref: CanonicalId | None = None
    after_hours_greeting_ref: CanonicalId | None = None
    holiday_greeting_ref: CanonicalId | None = None

    business_hours_options: list[MenuOption] = Field(default_factory=list)
    after_hours_options: list[MenuOption] = Field(default_factory=list)
    directory_search_enabled: bool = False
    directory_search_scope_ref: CanonicalId | None = None

    source_flow_reference: str | None = Field(
        default=None,
        description="Native vector number / UCCX script name / RGS workflow id this was "
        "derived from. Kept because the derivation is usually lossy and an engineer "
        "will need to open the original.",
    )
    complexity_score: int | None = Field(
        default=None,
        ge=0,
        description="Heuristic node count of the source flow. Above the target's expressiveness "
        "threshold the assessment engine raises a blocker.",
    )


@canonical_entity
class CallPark(CanonicalEntity):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["CallPark"] = "CallPark"

    name: str | None = None
    park_pattern: str = Field(description="Single slot or a range, e.g. '810X'.")
    partition_ref: CanonicalId | None = None
    site_code: str | None = None
    reversion_timeout_seconds: int | None = Field(default=None, ge=0)
    reversion_target_ref: CanonicalId | None = None
    park_monitoring_enabled: bool = False
    is_directed_park: bool = False


@canonical_entity
class Intercom(CanonicalEntity):
    """Whisper/auto-answer path between two endpoints. Frequently UNMAPPABLE on cloud."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["Intercom"] = "Intercom"

    directory_number: str
    partition_ref: CanonicalId | None = None
    label: str | None = None
    default_activated_device_ref: CanonicalId | None = None
    speed_dial_target: str | None = None
    owner_ref: CanonicalId | None = None


@canonical_entity
class ForwardingRule(CanonicalEntity):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["ForwardingRule"] = "ForwardingRule"

    owner_ref: CanonicalId | None = Field(
        default=None, description="The Line or User the rule belongs to."
    )
    condition: ForwardCondition = ForwardCondition.ALWAYS
    enabled: bool = True
    destination: str | None = None
    destination_number_ref: CanonicalId | None = None
    to_voicemail: bool = False
    delay_seconds: int | None = Field(default=None, ge=0)
    calling_permission_ref: CanonicalId | None = None
    applies_to_internal: bool = True
    applies_to_external: bool = True


@canonical_entity
class SimRing(CanonicalEntity):
    """Simultaneous ring / EC500 style mobility."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["SimRing"] = "SimRing"

    owner_ref: CanonicalId
    enabled: bool = True
    destinations: list[str] = Field(default_factory=list)
    destination_number_refs: list[CanonicalId] = Field(default_factory=list)
    delay_seconds: int | None = Field(default=None, ge=0)
    ring_duration_seconds: int | None = Field(default=None, ge=0)
    answer_confirmation_required: bool = False
    time_schedule_ref: CanonicalId | None = None


@canonical_entity
class Delegation(CanonicalEntity):
    """Boss/admin relationship. A hard wave-planning dependency in both directions."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["Delegation"] = "Delegation"

    principal_ref: CanonicalId = Field(description="The delegator (the 'boss').")
    delegate_refs: list[CanonicalId] = Field(default_factory=list)
    permissions: list[DelegationPermission] = Field(default_factory=list)
    shared_line_ref: CanonicalId | None = None
    notify_delegator_on_answer: bool = False
    delegate_can_view_calendar: bool | None = None


@canonical_entity
class SpeedDial(CanonicalEntity):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["SpeedDial"] = "SpeedDial"

    owner_ref: CanonicalId | None = None
    device_ref: CanonicalId | None = None
    index: int = Field(ge=1)
    label: str | None = None
    destination: str
    is_blf: bool = Field(
        default=False, description="Busy Lamp Field monitoring rather than a plain dial target."
    )


@canonical_entity
class ExtensionMobilityProfile(CanonicalEntity):
    """Hot-desking association. No native cloud equivalent on most targets."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["ExtensionMobilityProfile"] = "ExtensionMobilityProfile"

    user_ref: CanonicalId
    device_profile_ref: CanonicalId | None = None
    default_profile_ref: CanonicalId | None = None
    home_cluster: bool = True
    max_login_duration_seconds: int | None = Field(default=None, ge=0)
    cross_cluster_enabled: bool = False
