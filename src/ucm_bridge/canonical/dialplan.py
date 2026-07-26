"""Dial plan domain: partitions, permissions, patterns, routing, schedules.

The hardest part of any UC migration and the part most prone to silent breakage.
Cisco expresses reachability as partitions + calling search spaces; Avaya as
COR/COS + ARS/AAR analysis; Teams as voice routing policies + PSTN usages +
online voice routes. All three are modelled here as an ordered permission list
over named partitions, which is the largest common denominator that does not
lose ordering semantics.
"""

from __future__ import annotations

from datetime import date as date_type
from datetime import time
from enum import StrEnum
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from ucm_bridge.canonical.base import CanonicalEntity, CanonicalId
from ucm_bridge.canonical.registry import canonical_entity

DOMAIN = "dialplan"


class PermissionClass(StrEnum):
    """Coarse reachability tier. Used for auto-mapping confidence, not for routing."""

    INTERNAL_ONLY = "INTERNAL_ONLY"
    LOCAL = "LOCAL"
    NATIONAL = "NATIONAL"
    INTERNATIONAL = "INTERNATIONAL"
    PREMIUM_ALLOWED = "PREMIUM_ALLOWED"
    EMERGENCY_ONLY = "EMERGENCY_ONLY"
    CUSTOM = "CUSTOM"


class PatternDirection(StrEnum):
    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"
    BOTH = "BOTH"


class PartyType(StrEnum):
    CALLED = "CALLED"
    CALLING = "CALLING"


class DistributionAlgorithm(StrEnum):
    TOP_DOWN = "TOP_DOWN"
    CIRCULAR = "CIRCULAR"
    LONGEST_IDLE = "LONGEST_IDLE"
    BROADCAST = "BROADCAST"
    LEAST_LOADED = "LEAST_LOADED"


@canonical_entity
class Partition(CanonicalEntity):
    """A named namespace that dialable patterns live in."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["Partition"] = "Partition"

    name: str
    description: str | None = None
    site_code: str | None = None
    time_schedule_ref: CanonicalId | None = Field(
        default=None, description="Time-of-day partition activation, where the source supports it."
    )
    member_count: int | None = Field(
        default=None, ge=0, description="Zero means an unused partition: dial-plan dead weight."
    )


@canonical_entity
class CallingPermission(CanonicalEntity):
    """Ordered set of partitions a caller may reach.

    Canonicalises CUCM Calling Search Space, Avaya COR/COS, and Teams voice
    routing policy. Order is significant and is preserved.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["CallingPermission"] = "CallingPermission"

    name: str
    description: str | None = None
    permitted_partition_refs: list[CanonicalId] = Field(
        default_factory=list, description="Ordered: first match wins on the source platforms."
    )
    permission_class: PermissionClass = PermissionClass.CUSTOM
    site_code: str | None = None
    derived_from: str | None = Field(
        default=None,
        description="Native construct this came from, e.g. 'CUCM:CSS', 'Avaya:COR-12'. Retained "
        "so a reverse migration can aim for the same construct.",
    )


@canonical_entity
class RoutePattern(CanonicalEntity):
    """A dialable pattern that sends a call somewhere outside the local namespace."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["RoutePattern"] = "RoutePattern"

    pattern: str = Field(description="Native pattern syntax, e.g. '9.[2-9]XXXXXXXXX' or '+1XXX'.")
    partition_ref: CanonicalId | None = None
    route_target_ref: CanonicalId | None = Field(
        default=None, description="RouteList, RouteGroup, SIPTrunk, or Gateway."
    )
    block_call: bool = Field(default=False, description="A deny pattern rather than a route.")
    urgent_priority: bool = Field(
        default=False, description="Route immediately without waiting for interdigit timeout."
    )
    digits_to_discard: str | None = Field(
        default=None, description="Named discard instruction, e.g. 'PreDot'."
    )
    called_party_transform_mask: str | None = None
    calling_party_transform_mask: str | None = None
    prefix_digits: str | None = None
    priority: int | None = Field(
        default=None, description="Evaluation order where the source platform makes it explicit."
    )
    direction: PatternDirection = PatternDirection.OUTBOUND
    description: str | None = None


@canonical_entity
class TranslationPattern(CanonicalEntity):
    """Rewrites digits and re-runs the lookup. No call leaves on a translation."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["TranslationPattern"] = "TranslationPattern"

    pattern: str
    partition_ref: CanonicalId | None = None
    called_party_transform_mask: str | None = None
    calling_party_transform_mask: str | None = None
    prefix_digits: str | None = None
    digits_to_discard: str | None = None
    target_permission_ref: CanonicalId | None = Field(
        default=None, description="Permission (CSS) applied to the re-lookup."
    )
    do_not_wait_for_interdigit_timeout: bool = False
    urgent_priority: bool = False
    description: str | None = None


@canonical_entity
class DigitManipulationRule(CanonicalEntity):
    """A regex-style normalisation rule, closest to a Teams/SfB normalisation rule."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["DigitManipulationRule"] = "DigitManipulationRule"

    name: str
    match_pattern: str = Field(description="Regex as understood by the source platform.")
    replacement: str
    applies_to: PartyType = PartyType.CALLED
    direction: PatternDirection = PatternDirection.OUTBOUND
    order: int = Field(default=0, description="Lower runs first.")
    is_internal_extension: bool = False
    site_code: str | None = None
    description: str | None = None


@canonical_entity
class RouteGroup(CanonicalEntity):
    """An ordered set of trunks or gateways sharing a distribution algorithm."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["RouteGroup"] = "RouteGroup"

    name: str
    member_device_refs: list[CanonicalId] = Field(default_factory=list)
    distribution_algorithm: DistributionAlgorithm = DistributionAlgorithm.TOP_DOWN


@canonical_entity
class RouteList(CanonicalEntity):
    """An ordered set of route groups: the failover chain for a route pattern."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["RouteList"] = "RouteList"

    name: str
    route_group_refs: list[CanonicalId] = Field(default_factory=list)
    run_on_all_active_nodes: bool = False
    description: str | None = None


class Weekday(StrEnum):
    MON = "MON"
    TUE = "TUE"
    WED = "WED"
    THU = "THU"
    FRI = "FRI"
    SAT = "SAT"
    SUN = "SUN"


class SchedulePeriod(BaseModel):
    """One recurring open interval within a schedule."""

    model_config = ConfigDict(extra="forbid")

    days: list[Weekday] = Field(min_length=1)
    start: time
    end: time
    spans_midnight: bool = Field(
        default=False,
        description="True when end <= start, e.g. a 22:00-06:00 night-shift window.",
    )


class HolidayDate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    date: date_type
    recurring_annually: bool = False


@canonical_entity
class TimeSchedule(CanonicalEntity):
    """Business hours / holiday schedule shared by partitions, queues, and attendants."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["TimeSchedule"] = "TimeSchedule"

    name: str
    timezone: str = Field(description="IANA tz name. A schedule without one is not portable.")
    periods: list[SchedulePeriod] = Field(default_factory=list)
    holidays: list[HolidayDate] = Field(default_factory=list)
    description: str | None = None
