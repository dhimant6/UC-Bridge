"""Connector capability manifests.

A manifest is a connector's public contract: what it can read, what it can
write, what it needs, what it costs, and where it is known to lose fidelity. The
planner reads manifests to route workloads (voice to Teams, collaboration to
Slack in a split-target migration) and the connector base class enforces them,
so a connector cannot quietly do more than it declared.

On API versions: ``APISurface`` records the version a connector was *verified*
against, together with how that verification was done. Vendor APIs move. A
manifest asserting an unverified version is worse than one admitting it does not
know, so ``verified_at`` is optional and its absence is reportable.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ucm_bridge.canonical.base import FidelityLevel, Platform
from ucm_bridge.connectors.credentials import CredentialKind, CredentialScope


class WriteVerb(StrEnum):
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    ASSIGN = "ASSIGN"
    UNASSIGN = "UNASSIGN"


class APISurface(BaseModel):
    """One API a connector talks to."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="e.g. 'Cisco AXL', 'Microsoft Graph', 'Teams PowerShell'.")
    version: str | None = Field(
        default=None,
        description="Version this connector targets. None means version-negotiated at runtime, "
        "which is correct for AXL (WSDL per CUCM release).",
    )
    transport: str | None = Field(default=None, description="SOAP, REST, PowerShell, SSH, SFTP.")
    documentation_url: str | None = None
    verified_at: date | None = Field(
        default=None,
        description="When the endpoints and schema used here were last checked against vendor "
        "documentation or a live system. Absence is a reportable gap, not a default.",
    )
    verification_method: str | None = Field(
        default=None, description="e.g. 'vendor docs', 'recorded cassette from lab cluster 14.0'."
    )
    notes: str | None = None

    @property
    def is_verified(self) -> bool:
        return self.verified_at is not None and self.verification_method is not None


class EntityCapability(BaseModel):
    """What a connector can do with one canonical entity kind."""

    model_config = ConfigDict(extra="forbid")

    entity_kind: str
    can_extract: bool = False
    can_apply: bool = False
    supported_verbs: list[WriteVerb] = Field(default_factory=list)
    expected_fidelity: FidelityLevel = Field(
        default=FidelityLevel.DEGRADED,
        description="Best case for this kind on this platform. Never optimistic by default; a "
        "per-object assessment can still come out worse.",
    )
    fidelity_notes: str | None = None
    known_gaps: list[str] = Field(
        default_factory=list,
        description="Attributes or behaviours this connector knowingly cannot carry. Surfaced "
        "in the assessment before the customer commits, not discovered at cutover.",
    )
    required_permissions: list[str] = Field(
        default_factory=list, description="API scopes / roles needed, e.g. 'User.Read.All'."
    )
    api_surface: str | None = Field(
        default=None, description="Which declared APISurface serves this kind."
    )

    @model_validator(mode="after")
    def _writes_need_verbs(self) -> Self:
        if self.can_apply and not self.supported_verbs:
            raise ValueError(
                f"{self.entity_kind}: can_apply is true but no supported_verbs are declared."
            )
        if self.supported_verbs and not self.can_apply:
            raise ValueError(
                f"{self.entity_kind}: supported_verbs declared but can_apply is false."
            )
        return self


class RateLimitPolicy(BaseModel):
    """How hard the connector may push the platform.

    The execution engine obeys these; they are not advisory. Getting throttled
    into a lockout on a production publisher mid-cutover is a self-inflicted
    outage.
    """

    model_config = ConfigDict(extra="forbid")

    max_concurrent_requests: int = Field(default=4, ge=1)
    requests_per_second: float | None = Field(default=None, gt=0)
    burst_capacity: int | None = Field(default=None, ge=1)
    honours_retry_after: bool = Field(
        default=True, description="Whether the platform sends a usable Retry-After / 429."
    )
    initial_backoff_seconds: float = Field(default=1.0, gt=0)
    max_backoff_seconds: float = Field(default=60.0, gt=0)
    max_attempts: int = Field(default=5, ge=1)
    batch_size: int | None = Field(
        default=None, description="Preferred bulk-operation size, where the API supports batching."
    )


class EventualConsistencyPolicy(BaseModel):
    """Confirm-poll settings for platforms where a write is not immediately readable.

    Teams is the canonical case: a licence assignment or number assignment can
    take minutes to become visible. Assuming read-your-write here produces
    migrations that report success and then fail validation.
    """

    model_config = ConfigDict(extra="forbid")

    is_eventually_consistent: bool = False
    confirm_poll_interval_seconds: float = Field(default=5.0, gt=0)
    confirm_poll_timeout_seconds: float = Field(default=300.0, gt=0)
    confirm_required_for_kinds: list[str] = Field(
        default_factory=list,
        description="Entity kinds whose writes must be confirmed by re-reading before the "
        "operation counts as successful.",
    )


class CredentialRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purpose: str = Field(description="e.g. 'axl-read', 'graph-app', 'sat-ssh'.")
    kind: CredentialKind
    minimum_scope: CredentialScope = CredentialScope.READ_ONLY
    required_roles: list[str] = Field(default_factory=list)
    notes: str | None = None


class CapabilityManifest(BaseModel):
    """The complete declared contract of one connector."""

    model_config = ConfigDict(extra="forbid")

    connector_id: str = Field(description="Stable id, e.g. 'cisco-cucm', 'microsoft-teams'.")
    connector_version: str
    platform: Platform
    display_name: str

    api_surfaces: list[APISurface] = Field(default_factory=list)
    entities: list[EntityCapability] = Field(default_factory=list)
    credential_requirements: list[CredentialRequirement] = Field(default_factory=list)
    rate_limits: RateLimitPolicy = Field(default_factory=RateLimitPolicy)
    eventual_consistency: EventualConsistencyPolicy = Field(
        default_factory=EventualConsistencyPolicy
    )

    supports_dry_run: bool = Field(
        default=True,
        description="Must be true. A connector that cannot preview a write cannot be used for "
        "production writes, because dry run is mandatory.",
    )
    supports_rollback: bool = Field(
        default=False,
        description="Whether the connector can capture pre-change state and emit inverse "
        "operations. False means rollback for this platform is manual.",
    )
    requires_publisher_node: bool = Field(
        default=False,
        description="True for CUCM: configuration writes go to the publisher only.",
    )
    air_gap_capable: bool = Field(
        default=True,
        description="Whether this connector can run from an on-prem collector agent with no "
        "outbound internet access.",
    )
    notes: str | None = None

    @model_validator(mode="after")
    def _dry_run_is_mandatory(self) -> Self:
        if not self.supports_dry_run and any(e.can_apply for e in self.entities):
            raise ValueError(
                f"{self.connector_id}: declares writable entities but supports_dry_run is false. "
                "Dry run is mandatory for every write path."
            )
        counts = Counter(e.entity_kind for e in self.entities)
        duplicates = [kind for kind, count in counts.items() if count > 1]
        if duplicates:
            raise ValueError(
                f"{self.connector_id}: duplicate entity capabilities: {sorted(duplicates)}"
            )
        return self

    # -- queries used by the base class and the planner -------------------- #

    def capability_for(self, entity_kind: str) -> EntityCapability | None:
        for cap in self.entities:
            if cap.entity_kind == entity_kind:
                return cap
        return None

    def extractable_kinds(self) -> set[str]:
        return {e.entity_kind for e in self.entities if e.can_extract}

    def appliable_kinds(self) -> set[str]:
        return {e.entity_kind for e in self.entities if e.can_apply}

    def unmappable_kinds(self) -> set[str]:
        """Kinds this platform has no concept of.

        Slack declares the whole numbering, dial-plan, and trunking domains here,
        which is what lets the planner route voice elsewhere instead of failing.
        """
        return {
            e.entity_kind
            for e in self.entities
            if e.expected_fidelity is FidelityLevel.UNMAPPABLE
        }

    def unverified_api_surfaces(self) -> list[APISurface]:
        return [s for s in self.api_surfaces if not s.is_verified]
