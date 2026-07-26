"""Identity domain: who exists, what they are entitled to, how they are grouped."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import ClassVar, Literal

from pydantic import ConfigDict, Field

from ucm_bridge.canonical.base import CanonicalEntity, CanonicalId
from ucm_bridge.canonical.registry import canonical_entity

DOMAIN = "identity"


class GroupType(StrEnum):
    SECURITY = "SECURITY"
    DISTRIBUTION = "DISTRIBUTION"
    MICROSOFT_365 = "MICROSOFT_365"
    TEAM = "TEAM"
    SLACK_USER_GROUP = "SLACK_USER_GROUP"
    LDAP_SYNCED = "LDAP_SYNCED"


class ServiceAccountType(StrEnum):
    RESOURCE_ACCOUNT = "RESOURCE_ACCOUNT"
    """Teams resource account backing an auto attendant or call queue."""
    APPLICATION = "APPLICATION"
    BOT = "BOT"
    DEVICE_ACCOUNT = "DEVICE_ACCOUNT"
    """Room-system / MTR sign-in account."""
    ROOM_MAILBOX = "ROOM_MAILBOX"


class LicenseAssignmentSource(StrEnum):
    DIRECT = "DIRECT"
    GROUP_INHERITED = "GROUP_INHERITED"


class IdentitySourceOfTruth(StrEnum):
    ON_PREM_AD = "ON_PREM_AD"
    ENTRA_ID = "ENTRA_ID"
    LOCAL_PLATFORM = "LOCAL_PLATFORM"
    LDAP = "LDAP"
    SCIM = "SCIM"
    HR_SYSTEM = "HR_SYSTEM"


@canonical_entity
class User(CanonicalEntity):
    """A human with telephony and/or collaboration entitlements."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["User"] = "User"

    user_principal_name: str = Field(
        description="Stable login identity: UPN, SIP URI localpart@domain, or platform username."
    )
    email: str | None = None
    given_name: str | None = None
    surname: str | None = None
    employee_id: str | None = None
    department: str | None = None
    cost_centre: str | None = Field(
        default=None, description="Used by the wave planner for chargeback-aligned grouping."
    )
    job_title: str | None = None
    site_code: str | None = Field(
        default=None,
        description="Canonical site key. Drives number normalisation, emergency location "
        "assignment, and wave grouping, so it is worth getting right.",
    )
    manager_ref: CanonicalId | None = None
    org_unit_ref: CanonicalId | None = None

    enabled: bool = True
    telephony_enabled: bool = Field(
        default=False,
        description="Whether the user has voice service, as distinct from merely existing.",
    )
    primary_extension_ref: CanonicalId | None = None
    primary_number_ref: CanonicalId | None = Field(
        default=None, description="canonical_id of the E164Number assigned as the primary line."
    )
    mailbox_ref: CanonicalId | None = None

    license_refs: list[CanonicalId] = Field(default_factory=list)
    group_refs: list[CanonicalId] = Field(default_factory=list)
    entitlement_profile_ref: CanonicalId | None = None
    policy_refs: list[CanonicalId] = Field(
        default_factory=list, description="Calling / meeting / messaging / recording policies."
    )

    source_of_truth: IdentitySourceOfTruth | None = None
    preferred_language: str | None = Field(default=None, description="BCP-47 tag, e.g. 'de-DE'.")
    timezone: str | None = Field(default=None, description="IANA tz name, e.g. 'Europe/Munich'.")
    last_call_activity_at: datetime | None = Field(
        default=None,
        description="From CDR. Absence over the sampling window marks a dormant seat, which "
        "the assessment engine flags as a licence-saving candidate.",
    )


@canonical_entity
class Group(CanonicalEntity):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["Group"] = "Group"

    name: str
    description: str | None = None
    group_type: GroupType
    mail_enabled: bool = False
    email: str | None = None
    member_refs: list[CanonicalId] = Field(default_factory=list)
    owner_refs: list[CanonicalId] = Field(default_factory=list)
    source_of_truth: IdentitySourceOfTruth | None = None
    nested_group_refs: list[CanonicalId] = Field(default_factory=list)


@canonical_entity
class OrgUnit(CanonicalEntity):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["OrgUnit"] = "OrgUnit"

    name: str
    path: str = Field(description="Hierarchical path, e.g. 'Contoso/EMEA/Munich/Finance'.")
    parent_ref: CanonicalId | None = None
    site_code: str | None = None
    member_count: int | None = Field(default=None, ge=0)


@canonical_entity
class ServiceAccount(CanonicalEntity):
    """Non-human identity: resource account, bot, room device account."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["ServiceAccount"] = "ServiceAccount"

    user_principal_name: str
    account_type: ServiceAccountType
    purpose: str | None = None
    assigned_number_ref: CanonicalId | None = None
    associated_object_ref: CanonicalId | None = Field(
        default=None,
        description="The auto attendant, call queue, or room this account fronts.",
    )
    license_refs: list[CanonicalId] = Field(
        default_factory=list,
        description="Resource accounts frequently need their own (often free) SKU before a "
        "number can be assigned. Ordering matters at apply time.",
    )
    enabled: bool = True


@canonical_entity
class LicenseAssignment(CanonicalEntity):
    """One SKU held by one principal.

    Modelled as a first-class entity rather than a list on User because the
    reverse direction needs to emit an unassignment plan (§3.7 licence reclaim)
    and because assignment ordering is a real apply-time dependency.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["LicenseAssignment"] = "LicenseAssignment"

    principal_ref: CanonicalId
    sku_id: str
    sku_name: str | None = None
    enabled_service_plans: list[str] = Field(default_factory=list)
    disabled_service_plans: list[str] = Field(default_factory=list)
    assignment_source: LicenseAssignmentSource = LicenseAssignmentSource.DIRECT
    assigned_at: datetime | None = None
    required_for: list[str] = Field(
        default_factory=list,
        description="Capabilities this SKU unlocks, e.g. ['phone_system', 'audio_conferencing']. "
        "Used to detect licence shortfall during assessment.",
    )
    monthly_unit_cost: float | None = Field(
        default=None, ge=0, description="For the licence spend delta on the executive dashboard."
    )
    currency: str | None = None
    reclaimable: bool = Field(
        default=False,
        description="Set by the reverse-direction planner: this seat can be unassigned after "
        "cutover completes.",
    )


@canonical_entity
class EntitlementProfile(CanonicalEntity):
    """A named bundle of licences and policies applied to a class of user."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["EntitlementProfile"] = "EntitlementProfile"

    name: str
    description: str | None = None
    license_skus: list[str] = Field(default_factory=list)
    policy_refs: list[CanonicalId] = Field(default_factory=list)
    feature_flags: dict[str, bool] = Field(default_factory=dict)
    applies_to_group_refs: list[CanonicalId] = Field(default_factory=list)
