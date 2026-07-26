"""Post-migration validation (§4.6).

Distinct from "the API returned 200". A write can be accepted, replicate
successfully, and still leave a user unable to make a call — because the number
was never activated, the licence silently failed to provision, or the emergency
address was dropped.

Each check returns a pass/fail with the evidence it used, and one check is a
**hard fail** by design: a user with no emergency location is never reported as
a warning.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from ucm_bridge.canonical.base import CanonicalEntity, utcnow
from ucm_bridge.canonical.identity import LicenseAssignment, User
from ucm_bridge.canonical.messaging import VoicemailBox
from ucm_bridge.canonical.numbering import E164Number, NumberAssignmentState
from ucm_bridge.canonical.policy import EmergencyLocation
from ucm_bridge.canonical.snapshot import EstateSnapshot
from ucm_bridge.pipeline.reconcile import ReconciliationReport, reconcile


class CheckOutcome(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    HARD_FAIL = "HARD_FAIL"
    """A safety failure. The migration is not complete while any of these stand."""
    SKIPPED = "SKIPPED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class CheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_id: str
    title: str
    outcome: CheckOutcome
    detail: str
    affected_ids: list[str] = Field(default_factory=list)
    affected_sample: list[str] = Field(default_factory=list)
    expected: int | None = None
    actual: int | None = None

    @property
    def failed(self) -> bool:
        return self.outcome in (CheckOutcome.FAIL, CheckOutcome.HARD_FAIL)


class ValidationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    tenant_id: str
    generated_at: datetime = Field(default_factory=utcnow)
    checks: list[CheckResult] = Field(default_factory=list)
    reconciliation: ReconciliationReport | None = None

    @property
    def hard_failures(self) -> list[CheckResult]:
        return [c for c in self.checks if c.outcome is CheckOutcome.HARD_FAIL]

    @property
    def failures(self) -> list[CheckResult]:
        return [c for c in self.checks if c.failed]

    @property
    def passed(self) -> bool:
        return not self.failures

    @property
    def safe_to_sign_off(self) -> bool:
        """A migration with an outstanding hard failure is never signed off."""
        return not self.hard_failures

    def counts(self) -> dict[str, int]:
        counts = {o.value: 0 for o in CheckOutcome}
        for check in self.checks:
            counts[check.outcome.value] += 1
        return counts


def _result(
    check_id: str,
    title: str,
    *,
    offenders: Sequence[CanonicalEntity],
    detail_ok: str,
    detail_bad: str,
    hard: bool = False,
    expected: int | None = None,
    actual: int | None = None,
) -> CheckResult:
    if not offenders:
        return CheckResult(
            check_id=check_id,
            title=title,
            outcome=CheckOutcome.PASS,
            detail=detail_ok,
            expected=expected,
            actual=actual,
        )
    return CheckResult(
        check_id=check_id,
        title=title,
        outcome=CheckOutcome.HARD_FAIL if hard else CheckOutcome.FAIL,
        detail=detail_bad.format(count=len(offenders)),
        affected_ids=[e.canonical_id for e in offenders],
        affected_sample=[(e.display_name or e.canonical_id) for e in offenders[:15]],
        expected=expected,
        actual=actual,
    )


#: A probe that places a synthetic test call and reports success. Optional: most
#: customers will not permit one, and the platform must not pretend it ran.
TestCallProbe = Callable[[str], Awaitable[bool]]


class ValidationService:
    """Runs the post-migration checks against a freshly extracted target snapshot."""

    def __init__(self, *, test_call_probe: TestCallProbe | None = None) -> None:
        self.test_call_probe = test_call_probe

    async def validate(
        self,
        *,
        run_id: str,
        tenant_id: str,
        source: EstateSnapshot,
        target: EstateSnapshot,
        source_key_for: Callable[[CanonicalEntity], str | None],
        target_key_for: Callable[[CanonicalEntity], str | None],
        expected_phone_system_skus: set[str] | None = None,
    ) -> ValidationReport:
        checks: list[CheckResult] = []

        reconciliation = reconcile(
            source.entities,
            target.entities,
            source_key_for=source_key_for,
            target_key_for=target_key_for,
        )
        checks.append(self._object_counts(source, target))
        checks.append(self._reconciliation_check(reconciliation))
        checks.append(self._emergency_locations(target))
        checks.append(self._numbers_assigned_and_routable(target))
        checks.append(self._licences(target, expected_phone_system_skus or set()))
        checks.append(self._policies(source, target))
        checks.append(self._voicemail(source, target))
        checks.append(await self._test_calls(target))

        return ValidationReport(
            run_id=run_id,
            tenant_id=tenant_id,
            checks=checks,
            reconciliation=reconciliation,
        )

    # -- individual checks ------------------------------------------------ #

    def _object_counts(self, source: EstateSnapshot, target: EstateSnapshot) -> CheckResult:
        expected = source.counts_by_kind()
        actual = target.counts_by_kind()
        shortfalls = {
            kind: (count, actual.get(kind, 0))
            for kind, count in expected.items()
            if actual.get(kind, 0) < count
        }
        if not shortfalls:
            return CheckResult(
                check_id="VAL-001",
                title="Object counts reconcile",
                outcome=CheckOutcome.PASS,
                detail="Every entity kind is present on the target in at least equal numbers.",
                expected=len(source),
                actual=len(target),
            )
        return CheckResult(
            check_id="VAL-001",
            title="Object counts reconcile",
            outcome=CheckOutcome.FAIL,
            detail="; ".join(
                f"{kind}: expected {want}, found {got}" for kind, (want, got) in shortfalls.items()
            ),
            expected=len(source),
            actual=len(target),
        )

    def _reconciliation_check(self, report: ReconciliationReport) -> CheckResult:
        failures = report.failures()
        if not failures:
            return CheckResult(
                check_id="VAL-002",
                title="Attribute-level reconciliation",
                outcome=CheckOutcome.PASS,
                detail=f"All {len(report.results)} matched objects agree attribute by attribute.",
            )
        return CheckResult(
            check_id="VAL-002",
            title="Attribute-level reconciliation",
            outcome=CheckOutcome.FAIL,
            detail=(
                f"{len(failures)} object(s) differ between source and target: "
                + ", ".join(f"{f.kind}/{f.natural_key} ({f.status})" for f in failures[:10])
            ),
            affected_ids=[f.target_canonical_id or f.natural_key for f in failures],
            affected_sample=[f"{f.kind}/{f.natural_key}" for f in failures[:15]],
        )

    def _emergency_locations(self, target: EstateSnapshot) -> CheckResult:
        """Hard fail. A missing emergency address is never a warning."""
        locations = {
            e.canonical_id for e in target.entities if isinstance(e, EmergencyLocation)
        }
        offenders: list[CanonicalEntity] = [
            number
            for number in target.entities
            if isinstance(number, E164Number)
            and number.assignment_state is NumberAssignmentState.ASSIGNED
            and (
                number.emergency_location_ref is None
                or number.emergency_location_ref not in locations
            )
        ]
        return _result(
            "VAL-003",
            "Every assigned number has an emergency location",
            offenders=offenders,
            detail_ok="Every assigned number resolves to an emergency location on the target.",
            detail_bad=(
                "{count} assigned number(s) have no resolvable emergency location. A caller "
                "dialling emergency services from one of these cannot be located. The "
                "migration is not complete until this is zero."
            ),
            hard=True,
        )

    def _numbers_assigned_and_routable(self, target: EstateSnapshot) -> CheckResult:
        offenders = [
            number
            for number in target.entities
            if isinstance(number, E164Number)
            and number.assignment_state is NumberAssignmentState.ASSIGNED
            and (number.assigned_to_ref is None or not number.activated)
        ]
        return _result(
            "VAL-004",
            "Assigned numbers are actually assigned and active",
            offenders=offenders,
            detail_ok="Every number marked assigned has a target and is activated.",
            detail_bad=(
                "{count} number(s) claim to be assigned but have no target or are not "
                "activated. Inbound calls to these fail silently."
            ),
        )

    def _licences(self, target: EstateSnapshot, expected_skus: set[str]) -> CheckResult:
        if not expected_skus:
            return CheckResult(
                check_id="VAL-005",
                title="Licence assignment confirmed",
                outcome=CheckOutcome.NOT_APPLICABLE,
                detail="No phone-system SKUs were declared for this migration.",
            )

        licensed = {
            lic.principal_ref
            for lic in target.entities
            if isinstance(lic, LicenseAssignment) and lic.sku_id in expected_skus
        }
        offenders = [
            user
            for user in target.entities
            if isinstance(user, User)
            and user.telephony_enabled
            and user.canonical_id not in licensed
        ]
        return _result(
            "VAL-005",
            "Licence assignment confirmed",
            offenders=offenders,
            detail_ok="Every voice-enabled user holds a phone-system licence.",
            detail_bad=(
                "{count} voice-enabled user(s) hold no phone-system licence. Their calling "
                "will stop working when any grace period expires."
            ),
            expected=len(expected_skus),
        )

    def _policies(self, source: EstateSnapshot, target: EstateSnapshot) -> CheckResult:
        """Users who had a policy at source but have none on the target.

        Deliberately a comparison rather than an absolute requirement. An estate
        whose source connector never extracted policies has not lost anything,
        and reporting that as a failure would train people to ignore this check.
        """
        had_policy = {
            user.user_principal_name
            for user in source.entities
            if isinstance(user, User) and user.policy_refs
        }
        if not had_policy:
            return CheckResult(
                check_id="VAL-006",
                title="Policy assignment confirmed",
                outcome=CheckOutcome.NOT_APPLICABLE,
                detail="No source user carried a policy, so none could be lost.",
            )

        offenders = [
            user
            for user in target.entities
            if isinstance(user, User)
            and user.user_principal_name in had_policy
            and not user.policy_refs
        ]
        return _result(
            "VAL-006",
            "Policy assignment confirmed",
            offenders=offenders,
            detail_ok="Every user who had a policy at source has one on the target.",
            detail_bad=(
                "{count} user(s) had a policy at source and have none on the target. They will "
                "inherit the tenant default, which may be more permissive than their source "
                "entitlement."
            ),
            expected=len(had_policy),
        )

    def _voicemail(self, source: EstateSnapshot, target: EstateSnapshot) -> CheckResult:
        source_boxes = {
            box.mailbox_id: box for box in source.entities if isinstance(box, VoicemailBox)
        }
        if not source_boxes:
            return CheckResult(
                check_id="VAL-007",
                title="Voicemail message counts and greetings",
                outcome=CheckOutcome.NOT_APPLICABLE,
                detail="No voicemail boxes were in scope for this migration.",
            )

        target_boxes = {
            box.mailbox_id: box for box in target.entities if isinstance(box, VoicemailBox)
        }
        offenders: list[CanonicalEntity] = []
        for mailbox_id, source_box in source_boxes.items():
            target_box = target_boxes.get(mailbox_id)
            if target_box is None:
                offenders.append(source_box)
                continue
            expected_count = source_box.message_count or 0
            actual_count = target_box.message_count or 0
            greeting_lost = bool(
                source_box.greeting_set_ref and not target_box.greeting_set_ref
            )
            if actual_count < expected_count or greeting_lost:
                offenders.append(source_box)

        return _result(
            "VAL-007",
            "Voicemail message counts and greetings",
            offenders=offenders,
            detail_ok="Message counts and greeting presence match for every mailbox.",
            detail_bad=(
                "{count} mailbox(es) are missing, short on messages, or missing a greeting "
                "that existed at source."
            ),
            expected=len(source_boxes),
            actual=len(target_boxes),
        )

    async def _test_calls(self, target: EstateSnapshot) -> CheckResult:
        """Synthetic test calls, when a probe is configured.

        Absent a probe this reports SKIPPED rather than PASS. Claiming a call
        test passed when none ran would be the single most misleading thing this
        report could say.
        """
        if self.test_call_probe is None:
            return CheckResult(
                check_id="VAL-008",
                title="Synthetic test calls",
                outcome=CheckOutcome.SKIPPED,
                detail=(
                    "No test-call probe is configured, so call routing has not been verified "
                    "end to end. Configure a probe or test a sample by hand before sign-off."
                ),
            )

        numbers = [
            n
            for n in target.entities
            if isinstance(n, E164Number)
            and n.assignment_state is NumberAssignmentState.ASSIGNED
        ]
        failed: list[CanonicalEntity] = []
        for number in numbers:
            if not await self.test_call_probe(number.e164):
                failed.append(number)

        return _result(
            "VAL-008",
            "Synthetic test calls",
            offenders=failed,
            detail_ok=f"All {len(numbers)} assigned number(s) answered a synthetic test call.",
            detail_bad="{count} number(s) did not complete a synthetic test call.",
            expected=len(numbers),
            actual=len(numbers) - len(failed),
        )


def render_validation_markdown(report: ValidationReport) -> str:
    lines = [
        f"# Validation report: run {report.run_id}",
        "",
        f"_Generated {report.generated_at.isoformat()}_",
        "",
    ]
    if report.hard_failures:
        lines += [
            "## NOT SAFE TO SIGN OFF",
            "",
            "Outstanding safety failures:",
            "",
        ]
        lines += [f"- **{c.title}** — {c.detail}" for c in report.hard_failures]
        lines.append("")
    elif report.passed:
        lines += ["**All checks passed.**", ""]
    else:
        lines += ["**Checks failed** (none safety-critical).", ""]

    lines += ["| Check | Outcome | Detail |", "|---|---|---|"]
    for check in report.checks:
        detail = check.detail.replace("|", r"\|")
        lines.append(f"| {check.title} | {check.outcome.value} | {detail} |")
    lines.append("")

    if report.reconciliation is not None:
        lines += [
            "## Reconciliation",
            "",
            f"`{report.reconciliation.summary()}`",
            "",
        ]
    return "\n".join(lines)
