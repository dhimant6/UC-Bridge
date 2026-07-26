"""Trunking domain: how calls leave the estate.

The cloud->on-prem transform lives largely here: a Teams VoiceRoutingPolicy plus
its PSTNUsage records plus their VoiceRoutes must become CUCM route patterns over
a RouteList/RouteGroup/SIPTrunk chain, or Avaya ARS entries over trunk groups.
Modelling usages and routes as first-class entities (rather than folding them
into the policy) is what makes that transform mechanical instead of bespoke.
"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from ucm_bridge.canonical.base import CanonicalEntity, CanonicalId
from ucm_bridge.canonical.dialplan import PatternDirection
from ucm_bridge.canonical.registry import canonical_entity

DOMAIN = "trunking"


class TransportProtocol(StrEnum):
    UDP = "UDP"
    TCP = "TCP"
    TLS = "TLS"


class GatewayType(StrEnum):
    MGCP = "MGCP"
    H323 = "H323"
    SIP = "SIP"
    SRST = "SRST"
    ANALOGUE = "ANALOGUE"
    PRI = "PRI"
    BRI = "BRI"


class TrunkDirection(StrEnum):
    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"
    BIDIRECTIONAL = "BIDIRECTIONAL"


class SIPDestination(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str
    port: int = Field(default=5060, ge=1, le=65535)
    sort_order: int = Field(default=1, ge=1)
    transport: TransportProtocol = TransportProtocol.TCP


@canonical_entity
class SIPTrunk(CanonicalEntity):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["SIPTrunk"] = "SIPTrunk"

    name: str
    description: str | None = None
    destinations: list[SIPDestination] = Field(default_factory=list)
    direction: TrunkDirection = TrunkDirection.BIDIRECTIONAL
    sip_profile_ref: CanonicalId | None = None
    security_profile: str | None = None
    inbound_calling_permission_ref: CanonicalId | None = None
    outbound_calling_permission_ref: CanonicalId | None = None
    rerouting_permission_ref: CanonicalId | None = None
    significant_digits: int | None = Field(default=None, ge=0)
    srtp_allowed: bool = False
    digest_authentication: bool = False
    media_termination_point_required: bool = False
    run_on_all_active_nodes: bool = False
    site_code: str | None = None
    device_pool_ref: CanonicalId | None = None
    carrier_ref: CanonicalId | None = None


@canonical_entity
class Gateway(CanonicalEntity):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["Gateway"] = "Gateway"

    name: str
    gateway_type: GatewayType = GatewayType.SIP
    model: str | None = None
    vendor: str | None = None
    site_code: str | None = None
    device_pool_ref: CanonicalId | None = None
    management_address: str | None = None
    analogue_port_count: int | None = Field(
        default=None,
        ge=0,
        description="Non-zero means analogue endpoints depend on this gateway; those endpoints "
        "need an explicit disposition before the gateway can be decommissioned.",
    )
    digital_span_count: int | None = Field(default=None, ge=0)
    attached_device_refs: list[CanonicalId] = Field(default_factory=list)
    carrier_ref: CanonicalId | None = None


@canonical_entity
class DirectRoutingPSTNGateway(CanonicalEntity):
    """An SBC paired to a cloud tenant for Direct Routing."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["DirectRoutingPSTNGateway"] = "DirectRoutingPSTNGateway"

    fqdn: str
    sip_signaling_port: int = Field(default=5061, ge=1, le=65535)
    enabled: bool = True
    send_sip_options: bool = True
    forward_call_history: bool = False
    forward_pai: bool = Field(
        default=False, description="Forward P-Asserted-Identity. Matters for compliance recording."
    )
    media_bypass: bool = False
    bypass_mode: str | None = None
    failover_response_codes: str | None = None
    failover_time_seconds: int | None = Field(default=None, ge=0)
    max_concurrent_sessions: int | None = Field(default=None, ge=0)
    gateway_site_id: str | None = None
    sbc_profile_ref: CanonicalId | None = None


@canonical_entity
class VoiceRoute(CanonicalEntity):
    """A number pattern mapped to an ordered set of gateways.

    Not in the brief's minimum entity set; added because PSTNUsage references
    routes and the Teams->CUCM transform cannot be expressed without them.
    Recorded as a deliberate addition in ADR-0001.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["VoiceRoute"] = "VoiceRoute"

    name: str
    number_pattern: str = Field(description="Regex or dial pattern in the source's own syntax.")
    priority: int = Field(default=1, description="Lower evaluates first.")
    gateway_refs: list[CanonicalId] = Field(default_factory=list)
    pstn_usage_refs: list[CanonicalId] = Field(default_factory=list)
    description: str | None = None


@canonical_entity
class PSTNUsage(CanonicalEntity):
    """A named capability token linking a policy to the routes it may use."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["PSTNUsage"] = "PSTNUsage"

    name: str
    voice_route_refs: list[CanonicalId] = Field(
        default_factory=list, description="Ordered; first matching route wins."
    )
    description: str | None = None


@canonical_entity
class VoiceRoutingPolicy(CanonicalEntity):
    """Per-user outbound calling entitlement, expressed as an ordered usage list."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["VoiceRoutingPolicy"] = "VoiceRoutingPolicy"

    name: str
    pstn_usage_refs: list[CanonicalId] = Field(default_factory=list)
    description: str | None = None
    is_global_default: bool = False
    equivalent_permission_ref: CanonicalId | None = Field(
        default=None,
        description="The CallingPermission (CSS/COR) this corresponds to on the on-prem side. "
        "Populated by the mapping engine and used by the reverse transform.",
    )


@canonical_entity
class Carrier(CanonicalEntity):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["Carrier"] = "Carrier"

    name: str
    account_id: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    supported_countries: list[str] = Field(
        default_factory=list, description="ISO 3166-1 alpha-2 codes."
    )
    port_lead_time_days: int | None = Field(
        default=None, ge=0, description="Feeds the cutover schedule; ports are the long pole."
    )
    loa_requirements: list[str] = Field(default_factory=list)
    supports_operator_connect: bool | None = None


@canonical_entity
class SBCProfile(CanonicalEntity):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    domain: ClassVar[str] = DOMAIN
    kind: Literal["SBCProfile"] = "SBCProfile"

    name: str
    vendor: str | None = None
    model: str | None = None
    software_version: str | None = None
    fqdn: str | None = None
    tls_certificate_subject: str | None = None
    tls_certificate_expires_on: str | None = None
    media_bypass_supported: bool | None = None
    sip_options_interval_seconds: int | None = Field(default=None, ge=0)
    supported_codecs: list[str] = Field(default_factory=list)
    direction: PatternDirection = PatternDirection.BOTH
    site_code: str | None = None
