"""Assessment and readiness (§4.2): a rules engine over a canonical snapshot.

Findings are the product's main output before anyone touches a target system.
Each carries a severity, the objects it affects, and what to do about it — a
finding a customer cannot act on is noise.

Severity has a specific meaning here:

* ``BLOCKER``  — the migration cannot proceed for these objects. Emergency
  calling gaps and missing compliance-recording equivalents are blockers, never
  warnings, because the failure mode is someone getting hurt or an unlawful call.
* ``HIGH``     — will break something noticeable at cutover.
* ``MEDIUM``   — degradation users will notice and complain about.
* ``LOW``      — housekeeping; usually cheaper to fix than to migrate.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime
from enum import StrEnum
from typing import TypeVar

from pydantic import BaseModel, ConfigDict, Field

from ucm_bridge.canonical.base import CanonicalEntity, FidelityLevel, utcnow
from ucm_bridge.canonical.callhandling import AutoAttendant, Delegation, HuntGroup
from ucm_bridge.canonical.endpoints import Device, DeviceType, Firmware, SharedLineAppearance
from ucm_bridge.canonical.identity import User
from ucm_bridge.canonical.numbering import E164Number, Extension
from ucm_bridge.canonical.policy import ComplianceRecordingPolicy, EmergencyLocation
from ucm_bridge.canonical.snapshot import EstateSnapshot


class Severity(StrEnum):
    BLOCKER = "BLOCKER"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


_SEVERITY_ORDER = {s: i for i, s in enumerate(Severity)}

_E = TypeVar("_E", bound=CanonicalEntity)


class FindingStatus(StrEnum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"
    WAIVED = "WAIVED"
    """Accepted by a named human with a reason. Waiving a BLOCKER is refused."""


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str
    title: str
    severity: Severity
    detail: str
    remediation: str
    affected_ids: list[str] = Field(default_factory=list)
    affected_sample: list[str] = Field(
        default_factory=list, description="Human-readable examples, capped for readability."
    )
    affected_count: int = 0
    target_platform: str | None = None
    status: FindingStatus = FindingStatus.OPEN
    assignee: str | None = None
    waived_by: str | None = None
    waived_reason: str | None = None
    detected_at: datetime = Field(default_factory=utcnow)

    def waive(self, *, by: str, reason: str) -> Finding:
        """Accept a finding as a known risk.

        A BLOCKER cannot be waived here. If a customer genuinely accepts an
        emergency-calling gap, that is a decision made outside this tool, in
        writing, by someone with the authority to make it.
        """
        if self.severity is Severity.BLOCKER:
            raise ValueError(
                f"{self.rule_id} is a BLOCKER and cannot be waived in the tool. "
                "Blockers are resolved or the affected objects are excluded from the plan."
            )
        return self.model_copy(
            update={
                "status": FindingStatus.WAIVED,
                "waived_by": by,
                "waived_reason": reason,
            }
        )


class AssessmentReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: str
    estate_id: str
    target_platform: str | None = None
    generated_at: datetime = Field(default_factory=utcnow)
    findings: list[Finding] = Field(default_factory=list)

    @property
    def blockers(self) -> list[Finding]:
        return [
            f
            for f in self.findings
            if f.severity is Severity.BLOCKER and f.status is not FindingStatus.RESOLVED
        ]

    @property
    def is_ready_to_plan(self) -> bool:
        return not self.blockers

    def counts_by_severity(self) -> dict[str, int]:
        counts = {s.value: 0 for s in Severity}
        for finding in self.findings:
            counts[finding.severity.value] += 1
        return counts

    def sorted_findings(self) -> list[Finding]:
        return sorted(
            self.findings, key=lambda f: (_SEVERITY_ORDER[f.severity], -f.affected_count, f.rule_id)
        )


class RuleContext(BaseModel):
    """What a rule is allowed to know about the migration it is assessing."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    snapshot: EstateSnapshot
    target_platform: str | None = None
    target_requires_e164: bool = Field(
        default=True,
        description="True for Teams Phone and most cloud targets. False for an on-prem target "
        "where an extension with no DID is perfectly normal.",
    )
    target_supports_shared_lines: bool = True
    target_supports_extension_mobility: bool = False
    target_supports_analogue: bool = False
    ivr_complexity_limit: int = 40

    def of(self, entity_type: type[_E]) -> list[_E]:
        return [e for e in self.snapshot.entities if isinstance(e, entity_type)]


Rule = Callable[[RuleContext], Finding | None]

RULES: list[Rule] = []


def rule(func: Rule) -> Rule:
    RULES.append(func)
    return func


def _finding(
    *,
    rule_id: str,
    title: str,
    severity: Severity,
    detail: str,
    remediation: str,
    affected: Iterable[CanonicalEntity],
    context: RuleContext,
) -> Finding | None:
    items = list(affected)
    if not items:
        return None
    return Finding(
        rule_id=rule_id,
        title=title,
        severity=severity,
        detail=detail,
        remediation=remediation,
        affected_ids=[e.canonical_id for e in items],
        affected_sample=[(e.display_name or e.canonical_id) for e in items[:15]],
        affected_count=len(items),
        target_platform=context.target_platform,
    )


# --------------------------------------------------------------------------- #
# Emergency calling. These are blockers, always.
# --------------------------------------------------------------------------- #


@rule
def numbers_without_emergency_location(context: RuleContext) -> Finding | None:
    affected = [
        n
        for n in context.of(E164Number)
        if n.emergency_location_ref is None and n.number_type.value != "ELIN"
    ]
    return _finding(
        rule_id="EMG-001",
        title="Numbers with no emergency location",
        severity=Severity.BLOCKER,
        detail=(
            "These numbers have no associated dispatchable location. A caller dialling "
            "emergency services from one of them cannot be located."
        ),
        remediation=(
            "Assign an EmergencyLocation with a validated civic address to every number "
            "before it is migrated. This is a hard failure, not a warning."
        ),
        affected=affected,
        context=context,
    )


@rule
def locations_without_dispatchable_address(context: RuleContext) -> Finding | None:
    affected = [
        loc for loc in context.of(EmergencyLocation) if not loc.civic_address.is_dispatchable
    ]
    return _finding(
        rule_id="EMG-002",
        title="Emergency locations without a dispatchable address",
        severity=Severity.BLOCKER,
        detail=(
            "A location is missing street, city, or country. Emergency services cannot "
            "dispatch to a partial address."
        ),
        remediation="Complete the civic address and have the carrier validate it.",
        affected=affected,
        context=context,
    )


@rule
def unvalidated_emergency_locations(context: RuleContext) -> Finding | None:
    affected = [loc for loc in context.of(EmergencyLocation) if not loc.is_validated]
    return _finding(
        rule_id="EMG-003",
        title="Emergency locations not validated with the carrier",
        severity=Severity.HIGH,
        detail=(
            "The address looks complete but no carrier or emergency-services database has "
            "confirmed it. An address that fails validation at the PSAP is not usable."
        ),
        remediation="Submit each address for carrier validation and record the authority and date.",
        affected=affected,
        context=context,
    )


@rule
def nomadic_users_without_dynamic_location(context: RuleContext) -> Finding | None:
    static_sites = {
        loc.site_code for loc in context.of(EmergencyLocation) if not loc.supports_dynamic_location
    }
    if not static_sites:
        return None
    affected = [
        u
        for u in context.of(User)
        if u.telephony_enabled and u.site_code in static_sites
    ]
    return _finding(
        rule_id="EMG-004",
        title="Users at sites without dynamic emergency location",
        severity=Severity.HIGH,
        detail=(
            "These users' sites have no network-based location discovery. If they work "
            "remotely or hot-desk, emergency calls will report the wrong address — which is "
            "more dangerous than reporting none."
        ),
        remediation=(
            "Configure network identifiers (subnets, WiFi BSSIDs, switch ports) for the site, "
            "or require users to confirm their location in the client."
        ),
        affected=affected,
        context=context,
    )


# --------------------------------------------------------------------------- #
# Numbering
# --------------------------------------------------------------------------- #


@rule
def extensions_without_e164(context: RuleContext) -> Finding | None:
    if not context.target_requires_e164:
        return None
    affected = [e for e in context.of(Extension) if e.e164_ref is None]
    return _finding(
        rule_id="NUM-001",
        title="Extensions with no E.164 mapping",
        severity=Severity.BLOCKER,
        detail=(
            "The target requires every voice-enabled object to hold an E.164 number. These "
            "extensions have none, so the users behind them cannot be enabled for calling."
        ),
        remediation=(
            "Either allocate DIDs and define a site prefix rule in the number-normalisation "
            "engine, or move these users to an internal-only disposition and record that "
            "decision."
        ),
        affected=affected,
        context=context,
    )


@rule
def duplicate_directory_numbers(context: RuleContext) -> Finding | None:
    seen: dict[str, list[Extension]] = {}
    for extension in context.of(Extension):
        seen.setdefault(extension.digits, []).append(extension)
    affected = [e for group in seen.values() if len(group) > 1 for e in group]
    return _finding(
        rule_id="NUM-002",
        title="Duplicate extensions across partitions",
        severity=Severity.HIGH,
        detail=(
            "The same dialable string exists in more than one partition. That is legal "
            "on-premises and a collision on a flat cloud numbering plan."
        ),
        remediation="Renumber one side, or keep them separated with distinct E.164 mappings.",
        affected=affected,
        context=context,
    )


@rule
def non_e164_numbers(context: RuleContext) -> Finding | None:
    affected = [n for n in context.of(E164Number) if not n.e164.startswith("+")]
    return _finding(
        rule_id="NUM-003",
        title="Numbers not in E.164 format",
        severity=Severity.MEDIUM,
        detail="Numbers stored in national or dialable form rather than strict E.164.",
        remediation="Normalise with the per-site prefix tables before planning.",
        affected=affected,
        context=context,
    )


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #


@rule
def analogue_endpoints(context: RuleContext) -> Finding | None:
    if context.target_supports_analogue:
        return None
    affected = [
        d
        for d in context.of(Device)
        if d.device_type in (DeviceType.ANALOGUE, DeviceType.FAX, DeviceType.PAGING, DeviceType.ATA)
    ]
    return _finding(
        rule_id="EPT-001",
        title="Analogue endpoints with no cloud equivalent",
        severity=Severity.HIGH,
        detail=(
            "Fax lines, lift phones, door entry, alarm lines, and overhead paging do not "
            "migrate to a cloud PBX. These are the classic cutover breakers: they are "
            "invisible until the day the old system is switched off."
        ),
        remediation=(
            "For each one, decide: retain a local gateway, move to an ATA against the new "
            "platform, replace with an IP equivalent, or withdraw the service. Lift and alarm "
            "lines usually have a legal requirement attached — check before assuming."
        ),
        affected=affected,
        context=context,
    )


@rule
def devices_needing_replacement(context: RuleContext) -> Finding | None:
    affected = [d for d in context.of(Device) if d.replacement_required]
    return _finding(
        rule_id="EPT-002",
        title="Handsets with no supported path onto the target",
        severity=Severity.HIGH,
        detail=(
            "These models cannot register against the target platform. This is hardware "
            "budget and a logistics exercise, not a configuration change."
        ),
        remediation=(
            "Price replacements or move the users to softphones. Order lead times are often "
            "longer than the migration itself, so this drives the schedule."
        ),
        affected=affected,
        context=context,
    )


@rule
def firmware_below_target_minimum(context: RuleContext) -> Finding | None:
    affected = [f for f in context.of(Firmware) if f.supported_on_target is False]
    return _finding(
        rule_id="EPT-003",
        title="Firmware below the target's minimum",
        severity=Severity.MEDIUM,
        detail="Devices on these loads need upgrading before they will register.",
        remediation="Run a firmware campaign ahead of the first wave; schedule it as its own task.",
        affected=affected,
        context=context,
    )


# --------------------------------------------------------------------------- #
# Call handling
# --------------------------------------------------------------------------- #


@rule
def shared_lines_degrade(context: RuleContext) -> Finding | None:
    if context.target_supports_shared_lines:
        return None
    affected = context.of(SharedLineAppearance)
    return _finding(
        rule_id="CAL-001",
        title="Shared line appearances degrade on the target",
        severity=Severity.MEDIUM,
        detail=(
            "The target has no equivalent of a directory number appearing on several devices "
            "with barge and privacy semantics."
        ),
        remediation=(
            "Model these as call groups or delegation, and walk the affected users through "
            "the behaviour change before cutover rather than after."
        ),
        affected=affected,
        context=context,
    )


@rule
def boss_admin_delegation(context: RuleContext) -> Finding | None:
    affected = context.of(Delegation)
    return _finding(
        rule_id="CAL-002",
        title="Boss/admin delegation needs rebuilding",
        severity=Severity.MEDIUM,
        detail=(
            "Delegation semantics differ between platforms, and these relationships are "
            "highly visible: they belong to executives and their assistants."
        ),
        remediation=(
            "Rebuild on the target and test each pair explicitly. Every delegate and "
            "principal must land in the same wave."
        ),
        affected=affected,
        context=context,
    )


@rule
def ivr_beyond_target_expressiveness(context: RuleContext) -> Finding | None:
    affected = [
        aa
        for aa in context.of(AutoAttendant)
        if aa.complexity_score is not None and aa.complexity_score > context.ivr_complexity_limit
    ]
    return _finding(
        rule_id="CAL-003",
        title="IVR flows exceed the target's expressiveness",
        severity=Severity.HIGH,
        detail=(
            "These flows came from vectors or scripts with logic the target's declarative "
            "menu model cannot represent — data dips, time-of-day arithmetic, conditional "
            "branching on caller attributes."
        ),
        remediation=(
            "Redesign on the target, or route these numbers to a contact-centre platform that "
            "can express the logic. Budget design time, not migration time."
        ),
        affected=affected,
        context=context,
    )


@rule
def hunt_groups_flatten(context: RuleContext) -> Finding | None:
    affected = [h for h in context.of(HuntGroup) if len(h.line_group_refs) > 1]
    return _finding(
        rule_id="CAL-004",
        title="Multi-stage hunt chains flatten into a single queue",
        severity=Severity.MEDIUM,
        detail=(
            "Chained line groups give ordered escalation. A cloud call queue has one agent "
            "set, so the escalation stages collapse."
        ),
        remediation="Re-specify overflow and timeout behaviour per queue with the business owner.",
        affected=affected,
        context=context,
    )


@rule
def extension_mobility_has_no_equivalent(context: RuleContext) -> Finding | None:
    if context.target_supports_extension_mobility:
        return None
    affected = [e for e in context.snapshot.entities if e.kind == "ExtensionMobilityProfile"]
    return _finding(
        rule_id="CAL-005",
        title="Extension mobility has no target equivalent",
        severity=Severity.MEDIUM,
        detail="Hot-desking by signing in to a handset does not exist on the target.",
        remediation=(
            "Users sign in to a client instead. Shared-desk areas may need hot-desk-capable "
            "handsets or a booking system; this is a workplace change, not a config change."
        ),
        affected=affected,
        context=context,
    )


# --------------------------------------------------------------------------- #
# Compliance and licensing
# --------------------------------------------------------------------------- #


@rule
def compliance_recording_without_equivalent(context: RuleContext) -> Finding | None:
    affected = [
        p
        for p in context.of(ComplianceRecordingPolicy)
        if p.target_equivalent_available is False
    ]
    return _finding(
        rule_id="CMP-001",
        title="Compliance recording with no target equivalent",
        severity=Severity.BLOCKER,
        detail=(
            "These users are recorded under a regulatory obligation and the target cannot "
            "reproduce it. An unrecorded regulated call is an unlawful call, so this is a "
            "blocker rather than a degradation."
        ),
        remediation=(
            "Procure a certified recording integration for the target, or hold these users on "
            "the source platform until one exists. Involve compliance, not just IT."
        ),
        affected=affected,
        context=context,
    )


@rule
def unassessed_entities(context: RuleContext) -> Finding | None:
    affected = context.snapshot.unassessed()
    return _finding(
        rule_id="FID-001",
        title="Entities with no fidelity assessment",
        severity=Severity.HIGH,
        detail=(
            "These objects have never been evaluated, so the fidelity report understates what "
            "will be lost. An unassessed object is not a safe object."
        ),
        remediation="Extend the connector's mapping coverage or add an explicit assessment rule.",
        affected=affected,
        context=context,
    )


@rule
def unmappable_entities(context: RuleContext) -> Finding | None:
    affected = [
        e for e in context.snapshot.entities if e.fidelity.level is FidelityLevel.UNMAPPABLE
    ]
    return _finding(
        rule_id="FID-002",
        title="Entities that cannot migrate at all",
        severity=Severity.HIGH,
        detail=(
            "No target equivalent exists. These are excluded from plans by design and become "
            "manual work."
        ),
        remediation=(
            "Review the estimated manual effort and decide whether to rebuild, replace, or "
            "retire each one."
        ),
        affected=affected,
        context=context,
    )


@rule
def dormant_seats(context: RuleContext) -> Finding | None:
    affected = [
        u
        for u in context.of(User)
        if u.telephony_enabled and u.last_call_activity_at is None
    ]
    return _finding(
        rule_id="LIC-001",
        title="Voice-enabled users with no call activity",
        severity=Severity.LOW,
        detail=(
            "These seats show no CDR activity over the sampling window. Migrating them buys "
            "licences nobody uses."
        ),
        remediation=(
            "Confirm with the business, then exclude from the migration and reclaim the seat. "
            "Check for shared or seasonal use before deprovisioning anyone."
        ),
        affected=affected,
        context=context,
    )


def assess(context: RuleContext) -> AssessmentReport:
    """Run every registered rule over a snapshot."""
    findings = [finding for r in RULES if (finding := r(context)) is not None]
    return AssessmentReport(
        snapshot_id=context.snapshot.snapshot_id,
        estate_id=context.snapshot.estate_id,
        target_platform=context.target_platform,
        findings=findings,
    )


def render_assessment_markdown(report: AssessmentReport) -> str:
    counts = report.counts_by_severity()
    lines = [
        f"# Assessment: {report.estate_id}",
        "",
        f"_Snapshot `{report.snapshot_id}`"
        + (f", target {report.target_platform}" if report.target_platform else "")
        + "_",
        "",
        ("**NOT READY TO PLAN** — blockers must be resolved first."
         if not report.is_ready_to_plan else "**No blockers.**"),
        "",
        "| Severity | Findings |",
        "|---|---:|",
    ]
    lines.extend(f"| {sev} | {count} |" for sev, count in counts.items() if count)
    lines.append("")

    for finding in report.sorted_findings():
        lines += [
            f"## [{finding.severity.value}] {finding.title} (`{finding.rule_id}`)",
            "",
            f"**Affected:** {finding.affected_count} object(s)",
            "",
            finding.detail,
            "",
            f"**Remediation.** {finding.remediation}",
            "",
        ]
        if finding.affected_sample:
            lines.append("Examples: " + ", ".join(f"`{s}`" for s in finding.affected_sample))
            lines.append("")

    return "\n".join(lines)
