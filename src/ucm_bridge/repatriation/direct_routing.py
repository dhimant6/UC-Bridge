"""Direct Routing -> on-premises SIP trunking.

The core cloud-to-on-prem transform. A Teams user's outbound calling is
expressed as:

    VoiceRoutingPolicy -> [PSTNUsage] -> [VoiceRoute] -> [PSTNGateway]

and the on-premises equivalent is:

    CallingPermission -> [Partition] -> [RoutePattern] -> RouteList
                                                       -> RouteGroup -> SIPTrunk

The shapes correspond closely enough to transform mechanically, which is
precisely why modelling PSTNUsage and VoiceRoute as first-class canonical
entities was worth doing (ADR-0001).

What does *not* correspond, and is declared rather than glossed:

* Teams evaluates the usage list in order and picks the first route whose regex
  matches. CUCM evaluates the whole partition space by longest match. A pattern
  set that relies on Teams ordering can route differently on CUCM.
* Teams regexes are PCRE; CUCM patterns are a digit-wildcard language. Only
  simple anchored patterns translate; anything else is flagged for a human.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

from ucm_bridge.canonical.base import (
    CanonicalEntity,
    DegradedAttribute,
    FidelityAssessment,
    FidelityLevel,
    Platform,
    utcnow,
)
from ucm_bridge.canonical.dialplan import (
    CallingPermission,
    DistributionAlgorithm,
    Partition,
    PermissionClass,
    RouteGroup,
    RouteList,
    RoutePattern,
)
from ucm_bridge.canonical.trunking import (
    DirectRoutingPSTNGateway,
    PSTNUsage,
    SIPDestination,
    SIPTrunk,
    TransportProtocol,
    VoiceRoute,
    VoiceRoutingPolicy,
)

TRANSFORM_ID = "direct-routing-to-sip"

#: Teams number patterns that translate cleanly to a CUCM route pattern.
#: Anything outside this set is a human decision, not a guess.
_SIMPLE_PREFIX = re.compile(r"^\^?\\?\+(?P<digits>\d+)(?P<tail>(?:\\d|\[0-9\]|\.)?[\*\+]?)\$?$")


class PatternTranslation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_pattern: str
    target_pattern: str | None = None
    translatable: bool = False
    reason: str | None = None


def translate_number_pattern(pattern: str) -> PatternTranslation:
    """Translate a Teams regex into a CUCM route pattern where it is safe to.

    Deliberately conservative. A regex that cannot be translated with certainty
    is reported untranslatable, because a plausible-looking wrong route pattern
    sends calls to the wrong place and nobody notices until a customer complains.
    """
    cleaned = pattern.strip()
    match = _SIMPLE_PREFIX.match(cleaned)
    if match:
        digits = match.group("digits")
        tail = match.group("tail")
        wildcard = "!" if tail else ""
        return PatternTranslation(
            source_pattern=pattern,
            target_pattern=f"\\+{digits}{wildcard}",
            translatable=True,
        )

    if cleaned in {".*", "^.*$", r"^\+.*$"}:
        return PatternTranslation(
            source_pattern=pattern,
            target_pattern=r"\+!",
            translatable=True,
            reason="Catch-all pattern mapped to a CUCM international wildcard.",
        )

    return PatternTranslation(
        source_pattern=pattern,
        translatable=False,
        reason=(
            "The pattern uses regex features with no CUCM equivalent (alternation, "
            "lookaround, or bounded repetition). It must be rewritten by hand; guessing "
            "would silently reroute calls."
        ),
    )


class DirectRoutingTransformResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entities: list[CanonicalEntity] = Field(default_factory=list)
    untranslatable_patterns: list[PatternTranslation] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not self.untranslatable_patterns


def transform_direct_routing_to_on_prem(
    *,
    policies: list[VoiceRoutingPolicy],
    usages: list[PSTNUsage],
    routes: list[VoiceRoute],
    gateways: list[DirectRoutingPSTNGateway],
    target_instance_id: str,
    partition_name: str = "PT_PSTN_Repatriated",
) -> DirectRoutingTransformResult:
    """Turn a Teams voice-routing configuration into on-premises dial-plan objects."""
    usage_by_id = {u.canonical_id: u for u in usages}
    route_by_id = {r.canonical_id: r for r in routes}
    gateway_by_id = {g.canonical_id: g for g in gateways}

    entities: list[CanonicalEntity] = []
    untranslatable: list[PatternTranslation] = []
    warnings: list[str] = []

    def mint(kind: str, key: str) -> str:
        return CanonicalEntity.mint_canonical_id(
            Platform.CISCO_CUCM, kind, key, instance_id=target_instance_id
        )

    # One partition holds every repatriated pattern. Keeping them together makes
    # the repatriated dial plan reviewable and removable as a unit.
    partition = Partition(
        canonical_id=mint("Partition", partition_name),
        display_name=partition_name,
        name=partition_name,
        description="Patterns repatriated from Teams Direct Routing.",
        fidelity=FidelityAssessment.lossless(
            "A partition created by this transform has no source to lose fidelity against.",
            assessed_by=TRANSFORM_ID,
        ),
    )
    entities.append(partition)

    # Each Direct Routing gateway becomes a SIP trunk and a single-member route group.
    trunk_by_gateway: dict[str, str] = {}
    for gateway in gateways:
        trunk_name = f"TRUNK_{_slug(gateway.fqdn)}"
        trunk = SIPTrunk(
            canonical_id=mint("SIPTrunk", trunk_name),
            display_name=trunk_name,
            name=trunk_name,
            description=f"Repatriated from Direct Routing gateway {gateway.fqdn}",
            destinations=[
                SIPDestination(
                    host=gateway.fqdn,
                    port=gateway.sip_signaling_port,
                    transport=TransportProtocol.TLS,
                )
            ],
            srtp_allowed=True,
            fidelity=FidelityAssessment(
                level=FidelityLevel.DEGRADED,
                rationale="A Direct Routing gateway and a CUCM SIP trunk are not equivalent.",
                degraded_attributes=[
                    DegradedAttribute(
                        attribute="media_bypass",
                        reason=(
                            "Teams media bypass keeps media off the Microsoft cloud; CUCM has "
                            "no equivalent setting because media never went there"
                        ),
                        source_value=str(gateway.media_bypass),
                        target_behaviour=(
                            "Media flows directly between the endpoint and the SBC. Verify the "
                            "SBC's media handling and codec list still match."
                        ),
                    ),
                    DegradedAttribute(
                        attribute="failover_response_codes",
                        reason="CUCM expresses failover through route-group ordering, not codes",
                        source_value=gateway.failover_response_codes,
                        target_behaviour=(
                            "Per-response-code failover is lost. Failover becomes positional "
                            "in the route group and must be retested."
                        ),
                    ),
                ],
                manual_effort_minutes=45,
                assessed_by=TRANSFORM_ID,
                assessed_at=utcnow(),
            ),
        )
        entities.append(trunk)
        trunk_by_gateway[gateway.canonical_id] = trunk.canonical_id

        group_name = f"RG_{_slug(gateway.fqdn)}"
        entities.append(
            RouteGroup(
                canonical_id=mint("RouteGroup", group_name),
                display_name=group_name,
                name=group_name,
                member_device_refs=[trunk.canonical_id],
                distribution_algorithm=DistributionAlgorithm.TOP_DOWN,
                fidelity=FidelityAssessment.lossless(
                    "A single-trunk route group carries no information to lose.",
                    assessed_by=TRANSFORM_ID,
                ),
            )
        )

    # Each policy becomes a calling permission over the repatriated partition.
    for policy in policies:
        permission_name = f"CSS_{_slug(policy.name)}"
        entities.append(
            CallingPermission(
                canonical_id=mint("CallingPermission", permission_name),
                display_name=permission_name,
                name=permission_name,
                description=policy.description,
                permitted_partition_refs=[partition.canonical_id],
                permission_class=PermissionClass.CUSTOM,
                derived_from="Teams:CsOnlineVoiceRoutingPolicy",
                fidelity=FidelityAssessment(
                    level=FidelityLevel.DEGRADED,
                    rationale=(
                        "Teams evaluates PSTN usages in order; CUCM evaluates the partition "
                        "space by longest match."
                    ),
                    degraded_attributes=[
                        DegradedAttribute(
                            attribute="permitted_partition_refs",
                            reason=(
                                "ordered first-match usage evaluation becomes longest-match "
                                "pattern evaluation"
                            ),
                            source_value=", ".join(policy.pstn_usage_refs),
                            target_behaviour=(
                                "Where the Teams configuration relied on usage ordering to "
                                "prefer one route over an equally-matching one, CUCM may pick "
                                "the other. Test each pattern class with a real call."
                            ),
                        )
                    ],
                    manual_effort_minutes=30,
                    assessed_by=TRANSFORM_ID,
                    assessed_at=utcnow(),
                ),
            )
        )

        # Walk policy -> usages -> routes -> gateways, preserving order.
        for priority, usage_ref in enumerate(policy.pstn_usage_refs, start=1):
            usage = usage_by_id.get(usage_ref)
            if usage is None:
                warnings.append(
                    f"Policy {policy.name} references PSTN usage {usage_ref}, which is not in "
                    "the snapshot. Its routes cannot be repatriated."
                )
                continue

            for route_ref in usage.voice_route_refs:
                route = route_by_id.get(route_ref)
                if route is None:
                    warnings.append(
                        f"PSTN usage {usage.name} references voice route {route_ref}, which is "
                        "missing from the snapshot."
                    )
                    continue

                translation = translate_number_pattern(route.number_pattern)
                if not translation.translatable or translation.target_pattern is None:
                    untranslatable.append(translation)
                    continue

                targets = [
                    trunk_by_gateway[g]
                    for g in route.gateway_refs
                    if g in trunk_by_gateway
                ]
                if not targets:
                    warnings.append(
                        f"Voice route {route.name} has no gateway that resolved to a trunk; "
                        "its route pattern will have no destination."
                    )

                list_name = f"RL_{_slug(route.name)}"
                route_list = RouteList(
                    canonical_id=mint("RouteList", list_name),
                    display_name=list_name,
                    name=list_name,
                    route_group_refs=[
                        mint("RouteGroup", f"RG_{_slug(gateway_by_id[g].fqdn)}")
                        for g in route.gateway_refs
                        if g in gateway_by_id
                    ],
                    description=f"Repatriated from Teams voice route {route.name}",
                    fidelity=FidelityAssessment.lossless(
                        "Ordered gateway preference carries across as route-group order.",
                        assessed_by=TRANSFORM_ID,
                    ),
                )
                entities.append(route_list)

                entities.append(
                    RoutePattern(
                        canonical_id=mint("RoutePattern", translation.target_pattern),
                        display_name=translation.target_pattern,
                        pattern=translation.target_pattern,
                        partition_ref=partition.canonical_id,
                        route_target_ref=route_list.canonical_id,
                        priority=priority,
                        description=(
                            f"Repatriated from Teams route {route.name} "
                            f"(source pattern {route.number_pattern})"
                        ),
                        fidelity=FidelityAssessment(
                            level=FidelityLevel.DEGRADED,
                            rationale="Regex to digit-wildcard translation is approximate.",
                            degraded_attributes=[
                                DegradedAttribute(
                                    attribute="pattern",
                                    reason=(
                                        "a PCRE pattern and a CUCM digit-wildcard pattern do "
                                        "not match identical number sets"
                                    ),
                                    source_value=route.number_pattern,
                                    target_behaviour=(
                                        f"Translated to {translation.target_pattern}. Numbers "
                                        "at the edges of the range may match differently; test "
                                        "the shortest and longest number in each range."
                                    ),
                                )
                            ],
                            manual_effort_minutes=10,
                            assessed_by=TRANSFORM_ID,
                            assessed_at=utcnow(),
                        ),
                    )
                )

    if untranslatable:
        warnings.append(
            f"{len(untranslatable)} voice route pattern(s) could not be translated and were "
            "excluded. Those number ranges will have no on-premises route until someone "
            "writes the patterns by hand."
        )

    return DirectRoutingTransformResult(
        entities=entities, untranslatable_patterns=untranslatable, warnings=warnings
    )


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").upper()[:40]
