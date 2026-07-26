"""Mapping profiles: the reusable, reviewable unit of per-customer mapping.

A profile bundles the number plan, the rule set, and the human overrides for one
customer or tenant, and applies them to a canonical snapshot to produce a
*transformed* snapshot ready for planning.

Two properties the rest of the platform depends on:

* **Deterministic.** Same snapshot + same profile = same output, every time.
  No timestamps, no iteration-order dependence, no randomness.
* **Fully logged.** Every attribute the profile changes is recorded in the
  entity's ``transform_log`` with the rule or override that caused it, so the
  mapping workbench can show why a value is what it is.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from ucm_bridge.canonical.base import (
    CanonicalEntity,
    DegradedAttribute,
    FidelityAssessment,
    FidelityLevel,
    TransformOperation,
    utcnow,
)
from ucm_bridge.canonical.numbering import (
    E164Number,
    Extension,
    NumberAssignmentKind,
    NumberAssignmentState,
)
from ucm_bridge.canonical.snapshot import EstateSnapshot, SnapshotKind
from ucm_bridge.mapping.automap import MappingCandidate
from ucm_bridge.mapping.normalisation import (
    Collision,
    NormalisationOutcome,
    NumberPlan,
    Overlap,
)
from ucm_bridge.mapping.rules import RuleSet


class MappingOverride(BaseModel):
    """A human decision that beats whatever the rules produced."""

    model_config = ConfigDict(extra="forbid")

    canonical_id: str
    attribute: str
    value: str
    set_by: str
    set_at: datetime = Field(default_factory=utcnow)
    reason: str | None = None


class MappingProfile(BaseModel):
    """Everything needed to transform one customer's estate, saved and reusable."""

    model_config = ConfigDict(extra="forbid")

    profile_id: str
    name: str
    tenant_id: str
    target_platform: str
    number_plan: NumberPlan = Field(default_factory=NumberPlan)
    rules: RuleSet = Field(default_factory=RuleSet)
    overrides: list[MappingOverride] = Field(default_factory=list)
    accepted_candidates: list[MappingCandidate] = Field(
        default_factory=list,
        description="Auto-mapping suggestions a reviewer has accepted, kept so the profile "
        "reproduces the same result next run.",
    )
    description: str | None = None

    def overrides_for(self, canonical_id: str) -> dict[str, MappingOverride]:
        return {o.attribute: o for o in self.overrides if o.canonical_id == canonical_id}


class TransformIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_id: str
    kind: str
    attribute: str | None = None
    problem: str
    detail: str


class TransformResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot: EstateSnapshot
    issues: list[TransformIssue] = Field(default_factory=list)
    overlaps: list[Overlap] = Field(default_factory=list)
    collisions: list[Collision] = Field(default_factory=list)
    numbers_created: int = 0
    rules_fired: dict[str, int] = Field(default_factory=dict)

    @property
    def is_clean(self) -> bool:
        """No structural problems. The planner refuses to proceed when false."""
        return not (self.issues or self.overlaps or self.collisions)


def apply_profile(
    snapshot: EstateSnapshot,
    profile: MappingProfile,
    *,
    transformed_snapshot_id: str | None = None,
) -> TransformResult:
    """Run a mapping profile over a snapshot, producing a transformed snapshot."""
    issues: list[TransformIssue] = []
    rules_fired: dict[str, int] = {}

    # Structural checks first. An ambiguous number plan makes every number it
    # produces untrustworthy, so it is caught before anything is generated.
    overlaps = profile.number_plan.detect_overlaps()

    entities: list[CanonicalEntity] = [e.model_copy(deep=True) for e in snapshot.entities]
    by_id = {e.canonical_id: e for e in entities}
    new_numbers: list[E164Number] = []

    # 1. Number normalisation.
    extension_inputs: list[tuple[str, str | None]] = []
    for entity in entities:
        if not isinstance(entity, Extension):
            continue
        extension_inputs.append((entity.digits, entity.site_code))
        if entity.e164_ref is not None:
            continue

        result = profile.number_plan.normalise(entity.digits, entity.site_code)
        if result.outcome is NormalisationOutcome.NORMALISED and result.e164:
            number = _mint_number(entity, result.e164, profile)
            new_numbers.append(number)
            entity.e164_ref = number.canonical_id
            entity.log(
                TransformOperation.NORMALISE,
                actor=f"profile:{profile.profile_id}",
                summary=f"Extension {entity.digits} normalised to {result.e164}",
                attribute="e164_ref",
                after=result.e164,
            )
        elif result.outcome is not NormalisationOutcome.ALREADY_E164:
            issues.append(
                TransformIssue(
                    canonical_id=entity.canonical_id,
                    kind=entity.kind,
                    attribute="e164_ref",
                    problem=result.outcome.value,
                    detail=result.detail or "Normalisation did not produce a number.",
                )
            )

    collisions = profile.number_plan.detect_collisions(extension_inputs)

    # 2. Declarative rules.
    for entity in entities:
        for outcome in profile.rules.evaluate(entity):
            rules_fired[outcome.rule_id] = rules_fired.get(outcome.rule_id, 0) + 1
            for attribute, value in outcome.assignments.items():
                applied = _assign(entity, attribute, value)
                if not applied:
                    issues.append(
                        TransformIssue(
                            canonical_id=entity.canonical_id,
                            kind=entity.kind,
                            attribute=attribute,
                            problem="UNKNOWN_ATTRIBUTE",
                            detail=(
                                f"Rule {outcome.rule_id} assigns {attribute!r}, which is not a "
                                f"field on {entity.kind}."
                            ),
                        )
                    )
                    continue
                entity.log(
                    TransformOperation.MAP,
                    actor=f"profile:{profile.profile_id}",
                    summary=f"Rule {outcome.rule_id} set {attribute}",
                    attribute=attribute,
                    after=value,
                    rule_ref=outcome.rule_id,
                )

    # 3. Human overrides last: a person's decision beats a rule's.
    for override in profile.overrides:
        target = by_id.get(override.canonical_id)
        if target is None:
            issues.append(
                TransformIssue(
                    canonical_id=override.canonical_id,
                    kind="?",
                    attribute=override.attribute,
                    problem="ORPHAN_OVERRIDE",
                    detail="Override targets an object that is not in this snapshot.",
                )
            )
            continue
        before = getattr(target, override.attribute, None)
        if not _assign(target, override.attribute, override.value):
            issues.append(
                TransformIssue(
                    canonical_id=override.canonical_id,
                    kind=target.kind,
                    attribute=override.attribute,
                    problem="UNKNOWN_ATTRIBUTE",
                    detail=f"{override.attribute!r} is not a field on {target.kind}.",
                )
            )
            continue
        target.log(
            TransformOperation.OVERRIDE,
            actor=override.set_by,
            summary=override.reason or "Manual override in the mapping workbench",
            attribute=override.attribute,
            before=before,
            after=override.value,
        )

    combined = [*entities, *new_numbers]
    for entity in combined:
        entity.checksum = None
        entity.seal()

    transformed = EstateSnapshot.build(
        snapshot_id=transformed_snapshot_id or f"{snapshot.snapshot_id}-transformed",
        tenant_id=snapshot.tenant_id,
        estate_id=snapshot.estate_id,
        entities=combined,
        snapshot_kind=SnapshotKind.TRANSFORMED,
        platforms=list(snapshot.platforms),
        connector_versions=dict(snapshot.connector_versions),
        run_id=snapshot.run_id,
        read_only=False,
        warnings=list(snapshot.warnings),
    )

    return TransformResult(
        snapshot=transformed,
        issues=issues,
        overlaps=overlaps,
        collisions=collisions,
        numbers_created=len(new_numbers),
        rules_fired=dict(sorted(rules_fired.items())),
    )


def _mint_number(extension: Extension, e164: str, profile: MappingProfile) -> E164Number:
    """Create the E164Number an extension normalised to.

    Marked DEGRADED deliberately: the number was *derived* from a prefix table,
    not read from the source, so nobody has confirmed the carrier actually
    delivers it. That is a real risk and the fidelity report should say so.
    """
    canonical_id = E164Number.mint_canonical_id(
        "derived", "E164Number", e164, instance_id=profile.profile_id
    )
    return E164Number(
        canonical_id=canonical_id,
        display_name=e164,
        e164=e164,
        site_code=extension.site_code,
        extension_ref=extension.canonical_id,
        assigned_to_ref=extension.owner_ref,
        assignment_state=(
            NumberAssignmentState.ASSIGNED if extension.owner_ref
            else NumberAssignmentState.UNASSIGNED
        ),
        assignment_kind=(
            NumberAssignmentKind.USER if extension.owner_ref
            else NumberAssignmentKind.UNASSIGNED
        ),
        fidelity=FidelityAssessment(
            level=FidelityLevel.DEGRADED,
            rationale="Number derived from the site prefix table, not read from the source.",
            degraded_attributes=[
                DegradedAttribute(
                    attribute="e164",
                    reason=(
                        "the source holds no external number for this extension; this value "
                        "was computed by the normalisation engine"
                    ),
                    source_value=extension.digits,
                    target_behaviour=(
                        "The number will be provisioned as calculated. If the carrier does not "
                        "actually deliver this DID, inbound calls fail silently. Confirm the "
                        "range with the carrier before cutover."
                    ),
                )
            ],
            manual_effort_minutes=2,
            assessed_by=f"profile:{profile.profile_id}",
            assessed_at=utcnow(),
        ),
    ).log(
        TransformOperation.NORMALISE,
        actor=f"profile:{profile.profile_id}",
        summary=f"Derived from extension {extension.digits}",
        attribute="e164",
        after=e164,
    )


def _assign(entity: CanonicalEntity, attribute: str, value: str) -> bool:
    """Set a scalar attribute if the entity actually has it. Returns success."""
    if attribute not in type(entity).model_fields:
        return False
    try:
        setattr(entity, attribute, value)
    except Exception:
        return False
    return True
