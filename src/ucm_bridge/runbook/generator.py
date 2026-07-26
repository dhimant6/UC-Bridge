"""Per-wave cutover runbook generation (§4.7).

A runbook is what someone follows at 06:00 on a Saturday, so it is written for
that person rather than for a reader who already knows the system. Every
generated runbook contains four things:

* **Pre-checks** that must pass before anyone touches the target.
* **Steps** in execution order, with the exact API call each will issue.
* **Verification** to run afterwards.
* **Rollback trigger criteria** — decided in advance, in daylight, rather than
  argued about at 03:00 with users offline.

The last one is the point. A runbook without pre-agreed abort criteria is a
document that makes people feel prepared without making them prepared.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from ucm_bridge.assessment.engine import AssessmentReport, Severity
from ucm_bridge.canonical.base import utcnow
from ucm_bridge.connectors.contracts import (
    ApplyPlan,
    DryRunReceipt,
)
from ucm_bridge.waves.planner import CoexistenceRequirement, Wave


class RunbookStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: int
    title: str
    detail: str
    api_call: str | None = None
    expected_result: str | None = None
    rollback_note: str | None = None


class RollbackTrigger(BaseModel):
    """A pre-agreed condition that aborts the cutover."""

    model_config = ConfigDict(extra="forbid")

    condition: str
    action: str
    decided_by_role: str = "Change Approver"


#: Fixed triggers that apply to every wave. Deliberately absolute: a runbook
#: that lets the room decide at 3am whether 20% failures is acceptable has not
#: made a decision, it has deferred one.
STANDARD_ROLLBACK_TRIGGERS: tuple[RollbackTrigger, ...] = (
    RollbackTrigger(
        condition="Any user in this wave cannot dial emergency services.",
        action="Abort immediately and roll back the whole wave. Do not continue to "
        "diagnose with users in a non-working state.",
        decided_by_role="Anyone on the bridge",
    ),
    RollbackTrigger(
        condition="More than 10% of the wave's operations quarantine.",
        action="Pause the run, triage the quarantined objects, and decide whether to "
        "resume or roll back. Do not let the run continue in the background.",
    ),
    RollbackTrigger(
        condition="Post-migration validation reports any HARD_FAIL.",
        action="Roll back the affected objects. A hard fail is a safety failure, not a "
        "defect to fix on Monday.",
    ),
    RollbackTrigger(
        condition="Inbound calls to a migrated number fail a test call.",
        action="Roll back that user. If more than three users are affected, roll back "
        "the wave: the fault is systemic.",
    ),
    RollbackTrigger(
        condition="The change window expires with the run incomplete.",
        action="Pause at the checkpoint. Resume in the next approved window rather "
        "than overrunning; a resumable run is why the checkpoint exists.",
    ),
)


class Runbook(BaseModel):
    model_config = ConfigDict(extra="forbid")

    wave_id: str
    wave_name: str
    plan_id: str
    generated_at: datetime = Field(default_factory=utcnow)
    user_count: int = 0

    pre_checks: list[str] = Field(default_factory=list)
    steps: list[RunbookStep] = Field(default_factory=list)
    verification: list[str] = Field(default_factory=list)
    rollback_triggers: list[RollbackTrigger] = Field(default_factory=list)
    coexistence_notes: list[str] = Field(default_factory=list)
    open_blockers: list[str] = Field(default_factory=list)

    @property
    def is_executable(self) -> bool:
        """A runbook with open blockers documents a cutover that must not happen."""
        return not self.open_blockers


def build_runbook(
    *,
    wave: Wave,
    plan: ApplyPlan,
    dry_run: DryRunReceipt | None = None,
    assessment: AssessmentReport | None = None,
    coexistence: CoexistenceRequirement | None = None,
    change_reference: str | None = None,
) -> Runbook:
    """Generate the runbook for one wave from the plan that will actually run."""
    blockers = [
        f"[{finding.rule_id}] {finding.title} — {finding.affected_count} object(s)"
        for finding in (assessment.blockers if assessment else [])
    ]

    pre_checks = [
        "Confirm the change window is open and the change record is approved"
        + (f" ({change_reference})" if change_reference else "."),
        "Confirm two approvers have signed off on this exact plan digest: "
        f"`{plan.plan_digest}`.",
        "Confirm a dry run of this plan has completed"
        + (f" (receipt `{dry_run.receipt_id}`)." if dry_run else " — none recorded."),
        "Confirm per-site emergency calling has been confirmed for: "
        + (", ".join(sorted(plan.emergency_sites())) or "no sites affected")
        + ".",
        "Confirm the source estate is in a known state: no other change in flight.",
        "Confirm the rollback bundle will be captured (connector supports rollback).",
        "Have the service desk briefed and the bridge open before the first write.",
    ]
    if assessment and assessment.blockers:
        pre_checks.insert(
            0,
            "**STOP.** This wave has unresolved assessment blockers listed below. "
            "Do not begin.",
        )

    steps: list[RunbookStep] = []
    previews = {p.op_id: p for p in (dry_run.previews if dry_run else [])}
    for index, operation in enumerate(plan.operations_in_dependency_order(), start=1):
        preview = previews.get(operation.op_id)
        steps.append(
            RunbookStep(
                number=index,
                title=f"{operation.verb.value} {operation.entity_kind}"
                + (f" — {operation.description}" if operation.description else ""),
                detail=(
                    f"Idempotency key `{operation.idempotency_key}`. "
                    + (
                        f"Depends on: {', '.join(operation.depends_on)}."
                        if operation.depends_on
                        else "No dependencies."
                    )
                ),
                api_call=preview.api_call if preview else None,
                expected_result=(
                    "No change — the target already matches."
                    if preview and not preview.would_change
                    else "Object created or updated, then confirmed by re-read."
                ),
                rollback_note=(
                    "Inverse operation is captured automatically before the write."
                ),
            )
        )

    verification = [
        "Run the validation report for this wave and confirm zero HARD_FAIL results.",
        "Confirm every migrated user holds an emergency location.",
        "Place a test call out and in for a sample of at least three users per site.",
        "Confirm voicemail delivers and the MWI lights.",
        "Confirm hunt groups and shared lines behave as before, with the whole "
        "cluster tested together rather than one member.",
        "Check the audit log verifies (`AuditLog.verify()`) and export the evidence pack.",
    ]

    coexistence_notes: list[str] = []
    if coexistence is not None:
        coexistence_notes.append(coexistence.detail)
        if coexistence.interop_numbers:
            sample = ", ".join(coexistence.interop_numbers[:10])
            more = (
                f" and {len(coexistence.interop_numbers) - 10} more"
                if len(coexistence.interop_numbers) > 10
                else ""
            )
            coexistence_notes.append(
                f"Interop routing must be in place before this wave for: {sample}{more}."
            )

    return Runbook(
        wave_id=wave.wave_id,
        wave_name=wave.name,
        plan_id=plan.plan_id,
        user_count=wave.size,
        pre_checks=pre_checks,
        steps=steps,
        verification=verification,
        rollback_triggers=list(STANDARD_ROLLBACK_TRIGGERS),
        coexistence_notes=coexistence_notes,
        open_blockers=blockers,
    )


def render_runbook_markdown(runbook: Runbook) -> str:
    lines = [
        f"# Cutover runbook: {runbook.wave_name}",
        "",
        f"Wave `{runbook.wave_id}` · plan `{runbook.plan_id}` · {runbook.user_count} user(s)",
        f"Generated {runbook.generated_at.isoformat()}",
        "",
    ]

    if not runbook.is_executable:
        lines += [
            "## DO NOT RUN",
            "",
            "This wave has unresolved blockers:",
            "",
            *[f"- {blocker}" for blocker in runbook.open_blockers],
            "",
            "Resolve them, regenerate the plan, and re-run the dry run before cutover.",
            "",
        ]

    lines += ["## Pre-checks", ""]
    lines += [f"- [ ] {check}" for check in runbook.pre_checks]
    lines.append("")

    if runbook.coexistence_notes:
        lines += ["## Coexistence", ""]
        lines += [f"- {note}" for note in runbook.coexistence_notes]
        lines.append("")

    lines += ["## Rollback triggers", "", "Agreed in advance. If any occurs, act on it.", ""]
    lines += ["| Condition | Action | Decided by |", "|---|---|---|"]
    lines += [
        f"| {t.condition} | {t.action} | {t.decided_by_role} |"
        for t in runbook.rollback_triggers
    ]
    lines.append("")

    lines += ["## Steps", ""]
    for step in runbook.steps:
        lines += [f"### {step.number}. {step.title}", "", step.detail, ""]
        if step.api_call:
            lines += ["```", step.api_call, "```", ""]
        if step.expected_result:
            lines += [f"Expected: {step.expected_result}", ""]

    lines += ["## Verification", ""]
    lines += [f"- [ ] {check}" for check in runbook.verification]
    lines.append("")

    return "\n".join(lines)


def summarise_severity(assessment: AssessmentReport | None) -> str:
    if assessment is None:
        return "no assessment attached"
    counts = assessment.counts_by_severity()
    return ", ".join(
        f"{severity}={counts[severity]}"
        for severity in (s.value for s in Severity)
        if counts.get(severity)
    ) or "no findings"
