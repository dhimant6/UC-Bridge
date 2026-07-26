"""A minimal, deterministic plan builder.

Phase 0 scope: enough to prove the connector contract round-trips. It turns a set
of canonical entities into an :class:`ApplyPlan` whose operations are ordered by
their real dependencies. The Phase 3 execution engine replaces the scheduling
parts of this, not the two ideas it establishes:

**Reference resolution.** A source ``canonical_id`` means nothing on the target.
Every canonical reference is resolved at plan time into the *target's* natural
key via the target connector's :meth:`~ucm_bridge.connectors.base.Connector.natural_key_for`.
References that cannot be resolved are reported, never written as dangling
pointers.

**Cycle-free ordering.** Canonical references are genuinely cyclic - a User
points at its primary E164Number and that number points back at its User. Rather
than failing on the cycle or guessing, dependency edges are only emitted from
later kinds to earlier ones in a fixed precedence list. The result is
deterministic and the back-reference is simply settled by the second write.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from ucm_bridge.canonical.base import CanonicalEntity, FidelityLevel
from ucm_bridge.connectors.capabilities import WriteVerb
from ucm_bridge.connectors.contracts import ApplyPlan, WriteOperation

KIND_PRECEDENCE: tuple[str, ...] = (
    # Locations first: a number cannot be safely created without one.
    "EmergencyCallingPolicy",
    "EmergencyLocation",
    "EmergencyNumber",
    # Policies and structural scaffolding.
    "RetentionPolicy",
    "CallingPolicy",
    "MeetingPolicy",
    "MessagingPolicy",
    "ComplianceRecordingPolicy",
    "TimeSchedule",
    "OrgUnit",
    "Carrier",
    "Partition",
    "CallingPermission",
    # Numbering.
    "NumberBlock",
    "DIDRange",
    "E164Number",
    "Extension",
    # Identity.
    "EntitlementProfile",
    "Group",
    "User",
    "ServiceAccount",
    "LicenseAssignment",
    # Trunking and routing.
    "SBCProfile",
    "DirectRoutingPSTNGateway",
    "Gateway",
    "SIPTrunk",
    "RouteGroup",
    "RouteList",
    "VoiceRoute",
    "PSTNUsage",
    "VoiceRoutingPolicy",
    "RoutePattern",
    "TranslationPattern",
    "DigitManipulationRule",
    # Endpoints.
    "DevicePool",
    "SIPProfile",
    "ButtonTemplate",
    "Firmware",
    "Device",
    "DeviceProfile",
    "Line",
    "SharedLineAppearance",
    # Call handling.
    "LineGroup",
    "HuntGroup",
    "CallQueue",
    "AutoAttendant",
    "PickupGroup",
    "CallPark",
    "Intercom",
    "ForwardingRule",
    "SimRing",
    "Delegation",
    "SpeedDial",
    "ExtensionMobilityProfile",
    # Messaging.
    "VoicemailBox",
    "GreetingSet",
    "MWIConfig",
    "MessageStore",
    "TranscriptionSetting",
)

_UNRANKED = len(KIND_PRECEDENCE) + 1000


def precedence(kind: str) -> int:
    try:
        return KIND_PRECEDENCE.index(kind)
    except ValueError:
        return _UNRANKED


class KeyResolver(Protocol):
    """Anything that can derive a target-native key from a canonical entity."""

    def __call__(self, entity: CanonicalEntity) -> str | None: ...


class UnresolvedReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_id: str
    kind: str
    field: str
    referenced_id: str
    reason: str


class PlanBuildResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: ApplyPlan
    unresolved_references: list[UnresolvedReference] = Field(default_factory=list)
    skipped_unmappable: list[str] = Field(
        default_factory=list,
        description="canonical_ids assessed UNMAPPABLE, excluded from the plan by design. They "
        "appear in the manual-work section of the assessment instead.",
    )

    @property
    def is_fully_resolved(self) -> bool:
        return not self.unresolved_references


def _reference_fields(entity: CanonicalEntity) -> dict[str, list[str]]:
    """Canonical reference fields on an entity: ``*_ref`` and ``*_refs``."""
    found: dict[str, list[str]] = {}
    for name in type(entity).model_fields:
        if not (name.endswith("_ref") or name.endswith("_refs")):
            continue
        value = getattr(entity, name, None)
        if value is None:
            continue
        if isinstance(value, str):
            found[name] = [value]
        elif isinstance(value, list) and value and all(isinstance(v, str) for v in value):
            found[name] = list(value)
    return found


def _attributes(entity: CanonicalEntity) -> dict[str, Any]:
    """Content fields only: no envelope, no references."""
    envelope = {
        "kind",
        "canonical_id",
        "source_ref",
        "target_ref",
        "fidelity",
        "transform_log",
        "checksum",
        "tags",
    }
    dumped = entity.model_dump(mode="json", exclude_none=True)
    return {
        k: v
        for k, v in dumped.items()
        if k not in envelope and not (k.endswith("_ref") or k.endswith("_refs"))
    }


def build_apply_plan(
    entities: Iterable[CanonicalEntity],
    *,
    plan_id: str,
    tenant_id: str,
    estate_id: str,
    key_for: KeyResolver,
    wave_id: str | None = None,
    verb: WriteVerb = WriteVerb.CREATE,
    include_unmappable: bool = False,
    context_entities: Iterable[CanonicalEntity] | None = None,
) -> PlanBuildResult:
    """Build a dependency-ordered plan from canonical entities.

    ``verb`` defaults to CREATE with upsert semantics: the operation's
    idempotency key identifies the intended target object, and a connector that
    finds it already correct reports SKIPPED_NO_CHANGE rather than writing
    again. That is what makes re-running an identical plan a no-op.

    ``context_entities`` are resolvable but never planned. They exist for the
    common case where an operation refers to something the target already has:
    a number is assigned to a user Teams did not create and will not create.
    Without this the reference would be reported unresolved, and adding the user
    to the plan would ask a connector to write an object it cannot write.
    """
    planned_input: list[CanonicalEntity] = list(entities)
    context = list(context_entities or [])
    all_entities: list[CanonicalEntity] = [*planned_input, *context]
    by_id = {e.canonical_id: e for e in all_entities}
    context_ids = {e.canonical_id for e in context}

    skipped: list[str] = []
    planned: list[CanonicalEntity] = []
    for entity in planned_input:
        if entity.fidelity.level is FidelityLevel.UNMAPPABLE and not include_unmappable:
            skipped.append(entity.canonical_id)
        else:
            planned.append(entity)

    planned.sort(key=lambda e: (precedence(e.kind), e.canonical_id))

    planned_ids = {e.canonical_id for e in planned}
    unresolved: list[UnresolvedReference] = []
    operations: list[WriteOperation] = []

    for entity in planned:
        references: dict[str, Any] = {}
        depends_on: list[str] = []

        for field, referenced_ids in _reference_fields(entity).items():
            resolved: list[str] = []
            for referenced_id in referenced_ids:
                target = by_id.get(referenced_id)
                if target is None:
                    unresolved.append(
                        UnresolvedReference(
                            canonical_id=entity.canonical_id,
                            kind=entity.kind,
                            field=field,
                            referenced_id=referenced_id,
                            reason="referenced entity is not present in this entity set",
                        )
                    )
                    continue
                if referenced_id not in planned_ids and referenced_id not in context_ids:
                    unresolved.append(
                        UnresolvedReference(
                            canonical_id=entity.canonical_id,
                            kind=entity.kind,
                            field=field,
                            referenced_id=referenced_id,
                            reason=f"referenced {target.kind} is excluded from the plan "
                            f"({target.fidelity.level.value})",
                        )
                    )
                    continue
                native_key = key_for(target)
                if native_key is None:
                    unresolved.append(
                        UnresolvedReference(
                            canonical_id=entity.canonical_id,
                            kind=entity.kind,
                            field=field,
                            referenced_id=referenced_id,
                            reason=f"target connector cannot derive a native key for "
                            f"{target.kind}",
                        )
                    )
                    continue

                resolved.append(native_key)
                # Only depend on strictly-earlier kinds, which breaks the genuine
                # cycles in the canonical reference graph deterministically.
                if (
                    referenced_id not in context_ids
                    and precedence(target.kind) < precedence(entity.kind)
                ):
                    depends_on.append(_op_id(target))

            if resolved:
                references[field] = resolved if field.endswith("_refs") else resolved[0]

        natural_key = key_for(entity)
        operations.append(
            WriteOperation(
                op_id=_op_id(entity),
                verb=verb,
                entity_kind=entity.kind,
                canonical_id=entity.canonical_id,
                # Deliberately free of plan_id and run_id: the key identifies the
                # target object and the intent, not the attempt. A retry under a
                # new plan must collide with the original write, not duplicate it.
                idempotency_key=f"{verb.value}:{entity.kind}:{natural_key or entity.canonical_id}",
                payload={
                    "attributes": _attributes(entity),
                    "references": references,
                    "natural_key": natural_key,
                },
                depends_on=sorted(set(depends_on)),
                site_code=getattr(entity, "site_code", None),
                fidelity=entity.fidelity.level,
                description=f"{verb.value} {entity.kind} {entity.display_name or ''}".strip(),
            )
        )

    plan = ApplyPlan(
        plan_id=plan_id,
        tenant_id=tenant_id,
        estate_id=estate_id,
        wave_id=wave_id,
        operations=operations,
    ).seal()

    return PlanBuildResult(
        plan=plan, unresolved_references=unresolved, skipped_unmappable=sorted(skipped)
    )


def _op_id(entity: CanonicalEntity) -> str:
    return f"{entity.kind}:{entity.canonical_id}"


def dependency_levels(plan: ApplyPlan) -> list[list[str]]:
    """Group op_ids into levels that could be executed concurrently.

    Not used by the Phase 0 sequential executor; here because the Phase 3 engine
    needs it and it belongs with the ordering logic it depends on.
    """
    ordered: Sequence[WriteOperation] = plan.operations_in_dependency_order()
    level_of: dict[str, int] = {}
    for op in ordered:
        level_of[op.op_id] = (
            max((level_of[d] for d in op.depends_on), default=-1) + 1
        )
    levels: dict[int, list[str]] = {}
    for op_id, level in level_of.items():
        levels.setdefault(level, []).append(op_id)
    return [sorted(levels[i]) for i in sorted(levels)]
