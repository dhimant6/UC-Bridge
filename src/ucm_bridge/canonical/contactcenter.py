"""Contact centre domain: UCCX/UCCE, Avaya vectors/ACD, Genesys Cloud CX."""

from __future__ import annotations

from datetime import time
from enum import StrEnum
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from ucm_bridge.canonical.base import CanonicalEntity, CanonicalId
from ucm_bridge.canonical.dialplan import Weekday
from ucm_bridge.canonical.registry import canonical_entity

DOMAIN = "contactcenter"


class MediaType(StrEnum):
    VOICE = "VOICE"
    EMAIL = "EMAIL"
    CHAT = "CHAT"
    SMS = "SMS"
    SOCIAL = "SOCIAL"
    CALLBACK = "CALLBACK"


class SkillType(StrEnum):
    ACD_SKILL = "ACD_SKILL"
    LANGUAGE = "LANGUAGE"
    PROFICIENCY = "PROFICIENCY"
    COMPETENCY = "COMPETENCY"


class RoutingMethod(StrEnum):
    STANDARD = "STANDARD"
    SKILLS_BASED = "SKILLS_BASED"
    BULLSEYE = "BULLSEYE"
    PREFERRED_AGENT = "PREFERRED_AGENT"
    PREDICTIVE = "PREDICTIVE"
    CONDITIONAL_GROUP = "CONDITIONAL_GROUP"


class RecordingMode(StrEnum):
    ALL_CALLS = "ALL_CALLS"
    PERCENTAGE = "PERCENTAGE"
    ON_DEMAND = "ON_DEMAND"
    SELECTIVE = "SELECTIVE"
    DISABLED = "DISABLED"


class AgentSkillAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_ref: CanonicalId
    proficiency: int | None = Field(
        default=None, description="Interpreted against the skill's own proficiency_scale."
    )


class BullseyeRing(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ring_number: int = Field(ge=1)
    expansion_after_seconds: int = Field(ge=0)
    skill_refs_relaxed: list[CanonicalId] = Field(default_factory=list)


class OpenPeriod(BaseModel):
    model_config = ConfigDict(extra="forbid")

    days: list[Weekday] = Field(min_length=1)
    start: time
    end: time


class PromptVariant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    locale: str
    tts_text: str | None = None
    audio_object_key: str | None = None
    duration_seconds: float | None = Field(default=None, ge=0)


@canonical_entity
class Skill(CanonicalEntity):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["Skill"] = "Skill"

    name: str
    skill_type: SkillType = SkillType.ACD_SKILL
    description: str | None = None
    proficiency_scale_min: int | None = None
    proficiency_scale_max: int | None = Field(
        default=None,
        description="Scales differ across platforms (Cisco 1-10, Genesys 0-5). Rescaling is a "
        "declared DEGRADED transform, not a silent divide.",
    )


@canonical_entity
class Queue(CanonicalEntity):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["Queue"] = "Queue"

    name: str
    media_types: list[MediaType] = Field(default_factory=lambda: [MediaType.VOICE])
    skill_refs: list[CanonicalId] = Field(default_factory=list)
    routing_strategy_ref: CanonicalId | None = None
    member_agent_refs: list[CanonicalId] = Field(default_factory=list)
    wrapup_code_refs: list[CanonicalId] = Field(default_factory=list)
    schedule_group_ref: CanonicalId | None = None
    recording_policy_ref: CanonicalId | None = None

    sla_target_seconds: int | None = Field(default=None, ge=0)
    sla_target_percentage: int | None = Field(default=None, ge=0, le=100)
    max_wait_seconds: int | None = Field(default=None, ge=0)
    overflow_target_ref: CanonicalId | None = None
    whisper_prompt_ref: CanonicalId | None = None
    music_on_hold_ref: CanonicalId | None = None
    callback_enabled: bool = False
    e164_ref: CanonicalId | None = None


@canonical_entity
class AgentProfile(CanonicalEntity):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["AgentProfile"] = "AgentProfile"

    user_ref: CanonicalId
    agent_id: str | None = None
    skills: list[AgentSkillAssignment] = Field(default_factory=list)
    queue_refs: list[CanonicalId] = Field(default_factory=list)
    supervisor_ref: CanonicalId | None = None
    roles: list[str] = Field(default_factory=list)
    default_wrapup_seconds: int | None = Field(default=None, ge=0)
    extension_ref: CanonicalId | None = None
    auto_answer: bool = False
    team_name: str | None = None


@canonical_entity
class RoutingStrategy(CanonicalEntity):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["RoutingStrategy"] = "RoutingStrategy"

    name: str
    method: RoutingMethod = RoutingMethod.STANDARD
    bullseye_rings: list[BullseyeRing] = Field(default_factory=list)
    evaluation_method: str | None = None
    source_script_reference: str | None = Field(
        default=None,
        description="UCCX script name, Avaya vector number, or Genesys flow id. Flow logic is "
        "not fully representable here by design; this points at the original.",
    )
    complexity_score: int | None = Field(default=None, ge=0)


@canonical_entity
class WrapUpCode(CanonicalEntity):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["WrapUpCode"] = "WrapUpCode"

    name: str
    code: str | None = None
    category: str | None = None
    description: str | None = None


@canonical_entity
class ScheduleGroup(CanonicalEntity):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["ScheduleGroup"] = "ScheduleGroup"

    name: str
    timezone: str
    open_periods: list[OpenPeriod] = Field(default_factory=list)
    holiday_schedule_refs: list[CanonicalId] = Field(default_factory=list)
    emergency_group: bool = False


@canonical_entity
class Prompt(CanonicalEntity):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["Prompt"] = "Prompt"

    name: str
    variants: list[PromptVariant] = Field(default_factory=list)
    usage: str | None = Field(default=None, description="'greeting', 'hold', 'whisper', 'menu'.")


@canonical_entity
class RecordingPolicy(CanonicalEntity):
    """Contact-centre recording. Distinct from ComplianceRecordingPolicy in the policy domain."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["RecordingPolicy"] = "RecordingPolicy"

    name: str
    scope_refs: list[CanonicalId] = Field(default_factory=list)
    mode: RecordingMode = RecordingMode.DISABLED
    percentage: int | None = Field(default=None, ge=0, le=100)
    retention_days: int | None = Field(default=None, ge=0)
    storage_region: str | None = Field(
        default=None,
        description="Data-residency constraint. Moving recordings across regions can itself be "
        "the blocker, independent of API capability.",
    )
    consent_announcement_required: bool = False
    pause_resume_supported: bool | None = None
    encryption_at_rest: bool | None = None
    recording_count: int | None = Field(default=None, ge=0)
    total_bytes: int | None = Field(default=None, ge=0)
