"""Licence reclaim and data-export limits for cloud-to-on-prem repatriation.

Two things a repatriation must get right, both of which are easy to get wrong
quietly:

**Stop paying for what you no longer use.** After cutover the cloud seats are
still billed until somebody unassigns them. This emits an explicit
unassignment plan, ordered so a number is released before the licence that
entitles it — the reverse of the assignment order, because unassigning the
licence first can strand the number.

**Know what cannot come back before committing.** Cloud platforms restrict bulk
export of chat, voicemail, and recordings, often by licence tier. A repatriation
that silently loses five years of Teams chat is a legal problem, not a technical
one, so the export audit is a gate: any archive whose availability is still
undetermined blocks the plan.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from ucm_bridge.canonical.base import CanonicalEntity
from ucm_bridge.canonical.collaboration import FileAttachment, MessageArchive
from ucm_bridge.canonical.identity import LicenseAssignment, User
from ucm_bridge.canonical.messaging import ExportAvailability, MessageStore
from ucm_bridge.canonical.numbering import E164Number, NumberAssignmentState
from ucm_bridge.canonical.snapshot import EstateSnapshot
from ucm_bridge.connectors.capabilities import WriteVerb
from ucm_bridge.connectors.contracts import ApplyPlan, WriteOperation


class ReclaimStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order: int
    action: str
    target: str
    detail: str
    monthly_saving: float | None = None
    currency: str | None = None


class ReclaimPlan(BaseModel):
    """What to unassign, in what order, and what it saves."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    steps: list[ReclaimStep] = Field(default_factory=list)
    total_monthly_saving: float = 0.0
    currency: str | None = None
    seats_reclaimed: int = 0
    numbers_released: int = 0
    blocked: list[str] = Field(default_factory=list)

    def annual_saving(self) -> float:
        return self.total_monthly_saving * 12


def build_reclaim_plan(
    target_snapshot: EstateSnapshot,
    *,
    tenant_id: str,
    migrated_user_keys: set[str],
    reclaimable_sku_ids: set[str] | None = None,
) -> ReclaimPlan:
    """Plan the post-cutover unassignment of cloud seats and numbers.

    Only users confirmed migrated are touched. Reclaiming a seat from someone
    still on the cloud platform takes their phone away.
    """
    users = {
        u.canonical_id: u for u in target_snapshot.entities if isinstance(u, User)
    }
    migrated_ids = {
        u.canonical_id
        for u in users.values()
        if u.user_principal_name in migrated_user_keys
    }

    steps: list[ReclaimStep] = []
    blocked: list[str] = []
    total = 0.0
    currency: str | None = None
    order = 0

    # 1. Release numbers first. A licence removed before its number can leave the
    #    number stranded in an unassignable state.
    numbers_released = 0
    for number in target_snapshot.entities:
        if not isinstance(number, E164Number):
            continue
        if number.assignment_state is not NumberAssignmentState.ASSIGNED:
            continue
        if number.assigned_to_ref not in migrated_ids:
            continue
        order += 1
        numbers_released += 1
        steps.append(
            ReclaimStep(
                order=order,
                action="UNASSIGN_NUMBER",
                target=number.e164,
                detail=(
                    f"Release {number.e164} from "
                    f"{users[number.assigned_to_ref].user_principal_name} once the port has "
                    "completed. Releasing before the port completes loses the number."
                ),
            )
        )

    # 2. Then the licences.
    seats = 0
    for licence in target_snapshot.entities:
        if not isinstance(licence, LicenseAssignment):
            continue
        if licence.principal_ref not in migrated_ids:
            continue
        if reclaimable_sku_ids is not None and licence.sku_id not in reclaimable_sku_ids:
            continue

        principal = users.get(licence.principal_ref)
        if principal is None:
            blocked.append(
                f"Licence {licence.sku_name or licence.sku_id} references a principal that is "
                "not in the snapshot; it was not added to the plan."
            )
            continue

        order += 1
        seats += 1
        saving = licence.monthly_unit_cost
        if saving:
            total += saving
            currency = currency or licence.currency

        steps.append(
            ReclaimStep(
                order=order,
                action="UNASSIGN_LICENCE",
                target=f"{licence.sku_name or licence.sku_id} / {principal.user_principal_name}",
                detail=(
                    "Unassign after the user is confirmed working on the on-premises platform. "
                    "Group-inherited licences must be removed from the group, not the user."
                    if licence.assignment_source.value == "GROUP_INHERITED"
                    else "Unassign after the user is confirmed working on-premises."
                ),
                monthly_saving=saving,
                currency=licence.currency,
            )
        )

    return ReclaimPlan(
        tenant_id=tenant_id,
        steps=steps,
        total_monthly_saving=round(total, 2),
        currency=currency,
        seats_reclaimed=seats,
        numbers_released=numbers_released,
        blocked=blocked,
    )


def reclaim_plan_to_apply_plan(
    plan: ReclaimPlan, *, plan_id: str, estate_id: str
) -> ApplyPlan:
    """Turn a reclaim plan into an executable ApplyPlan of unassign operations."""
    operations: list[WriteOperation] = []
    previous: str | None = None

    for step in plan.steps:
        kind = "E164Number" if step.action == "UNASSIGN_NUMBER" else "LicenseAssignment"
        op_id = f"{step.action}:{step.target}"
        operations.append(
            WriteOperation(
                op_id=op_id,
                verb=WriteVerb.UNASSIGN,
                entity_kind=kind,
                canonical_id=op_id,
                idempotency_key=f"{WriteVerb.UNASSIGN.value}:{kind}:{step.target}",
                payload={"attributes": {}, "references": {}, "natural_key": step.target},
                # Strictly sequential: numbers before licences, and the plan's own
                # order is the safe order.
                depends_on=[previous] if previous else [],
                description=step.detail,
            )
        )
        previous = op_id

    return ApplyPlan(
        plan_id=plan_id,
        tenant_id=plan.tenant_id,
        estate_id=estate_id,
        operations=operations,
    ).seal()


# --------------------------------------------------------------------------- #
# Export limits
# --------------------------------------------------------------------------- #


class ExportRisk(StrEnum):
    NONE = "NONE"
    PARTIAL = "PARTIAL"
    TOTAL_LOSS = "TOTAL_LOSS"
    UNKNOWN = "UNKNOWN"
    """Availability was never determined. Blocks the plan: 'we did not check' is
    not an answer a customer can consent to."""


class ExportFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    canonical_id: str
    label: str
    risk: ExportRisk
    item_count: int | None = None
    reason: str
    required_plan_tier: str | None = None


class ExportAudit(BaseModel):
    """What can and cannot be brought back, decided before the customer commits."""

    model_config = ConfigDict(extra="forbid")

    findings: list[ExportFinding] = Field(default_factory=list)
    messages_at_risk: int = 0
    voicemails_at_risk: int = 0
    files_at_risk: int = 0

    @property
    def undetermined(self) -> list[ExportFinding]:
        return [f for f in self.findings if f.risk is ExportRisk.UNKNOWN]

    @property
    def total_losses(self) -> list[ExportFinding]:
        return [f for f in self.findings if f.risk is ExportRisk.TOTAL_LOSS]

    @property
    def safe_to_commit(self) -> bool:
        """False while anything is undetermined.

        A total loss the customer has seen and accepted is a decision. An
        undetermined one is a surprise waiting to happen, so it blocks.
        """
        return not self.undetermined

    def summary(self) -> str:
        return (
            f"{len(self.total_losses)} total loss(es), {len(self.undetermined)} undetermined; "
            f"{self.messages_at_risk} message(s), {self.voicemails_at_risk} voicemail(s), "
            f"{self.files_at_risk} file(s) at risk"
        )


_RISK_BY_AVAILABILITY = {
    ExportAvailability.AVAILABLE: ExportRisk.NONE,
    ExportAvailability.PARTIAL: ExportRisk.PARTIAL,
    ExportAvailability.UNAVAILABLE_PLAN_TIER: ExportRisk.TOTAL_LOSS,
    ExportAvailability.UNAVAILABLE_NO_API: ExportRisk.TOTAL_LOSS,
    ExportAvailability.UNAVAILABLE_PERMISSION: ExportRisk.TOTAL_LOSS,
    ExportAvailability.NOT_YET_DETERMINED: ExportRisk.UNKNOWN,
}


def audit_export_limits(snapshot: EstateSnapshot) -> ExportAudit:
    """Report what cannot be repatriated, before anyone commits to the migration."""
    findings: list[ExportFinding] = []
    messages = voicemails = files = 0

    for entity in snapshot.entities:
        risk, count, reason, tier = _classify(entity)
        if risk is None:
            continue
        findings.append(
            ExportFinding(
                kind=entity.kind,
                canonical_id=entity.canonical_id,
                label=entity.display_name or entity.canonical_id,
                risk=risk,
                item_count=count,
                reason=reason,
                required_plan_tier=tier,
            )
        )
        if risk in (ExportRisk.TOTAL_LOSS, ExportRisk.PARTIAL, ExportRisk.UNKNOWN):
            if isinstance(entity, MessageArchive):
                messages += count or 0
            elif isinstance(entity, MessageStore):
                voicemails += count or 0
            elif isinstance(entity, FileAttachment):
                files += 1

    return ExportAudit(
        findings=findings,
        messages_at_risk=messages,
        voicemails_at_risk=voicemails,
        files_at_risk=files,
    )


def _classify(
    entity: CanonicalEntity,
) -> tuple[ExportRisk | None, int | None, str, str | None]:
    if isinstance(entity, MessageArchive):
        risk = _RISK_BY_AVAILABILITY[entity.export_availability]
        return (
            risk,
            entity.message_count,
            entity.export_limitation
            or (
                "Export availability has not been determined for this conversation."
                if risk is ExportRisk.UNKNOWN
                else f"Export is {entity.export_availability.value}."
            ),
            entity.required_plan_tier,
        )
    if isinstance(entity, MessageStore):
        risk = _RISK_BY_AVAILABILITY[entity.export_availability]
        return (
            risk,
            entity.message_count,
            entity.export_limitation
            or f"Voicemail export is {entity.export_availability.value}.",
            None,
        )
    if isinstance(entity, FileAttachment):
        risk = _RISK_BY_AVAILABILITY[entity.export_availability]
        return risk, 1, f"File export is {entity.export_availability.value}.", None
    return None, None, "", None
