"""Discovery: a safely re-runnable, read-only crawl of a source estate.

Produces a canonical snapshot plus the estate report described in §4.1. Two
properties are load-bearing:

* **It never writes.** The connector base already refuses production writes
  under a read-only credential; discovery additionally never constructs an
  authorization at all, so there is no code path from here to a write.
* **It is re-runnable.** Two runs against an unchanged estate produce identical
  snapshot digests, which is what makes the diff between runs meaningful.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from ucm_bridge.canonical.base import CanonicalEntity, FidelityLevel, utcnow
from ucm_bridge.canonical.endpoints import Device, DeviceType, Line
from ucm_bridge.canonical.identity import User
from ucm_bridge.canonical.numbering import E164Number, Extension
from ucm_bridge.canonical.snapshot import EstateSnapshot, SnapshotKind
from ucm_bridge.connectors.base import Connector
from ucm_bridge.connectors.contracts import ExtractRequest


class ModelBreakdown(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    count: int
    device_type: str
    replacement_required: bool = False


class OrphanFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    canonical_id: str
    display_name: str | None = None
    reason: str


class EstateReport(BaseModel):
    """The §4.1 estate report: what is actually out there, and how bad is it."""

    model_config = ConfigDict(extra="forbid")

    estate_id: str
    snapshot_id: str
    generated_at: datetime = Field(default_factory=utcnow)

    entity_counts: dict[str, int] = Field(default_factory=dict)
    user_count: int = 0
    telephony_enabled_user_count: int = 0
    device_count: int = 0
    device_models: list[ModelBreakdown] = Field(default_factory=list)
    devices_requiring_replacement: int = 0
    analogue_endpoint_count: int = 0

    extension_count: int = 0
    extensions_without_e164: int = 0
    non_e164_numbers: list[str] = Field(default_factory=list)
    duplicate_directory_numbers: list[str] = Field(default_factory=list)
    dormant_extensions: list[str] = Field(default_factory=list)

    unused_partitions: list[str] = Field(default_factory=list)
    orphans: list[OrphanFinding] = Field(default_factory=list)

    dial_plan_complexity_score: int = 0
    complexity_drivers: dict[str, int] = Field(default_factory=dict)

    fidelity_by_kind: dict[str, dict[str, int]] = Field(default_factory=dict)
    unassessed_count: int = 0
    estimated_manual_effort_minutes: int = 0

    raw_sql_reads: int = Field(
        default=0,
        description="Count of objects read via a raw-SQL escape hatch. Non-zero is not wrong, "
        "but it is worth a reviewer's attention.",
    )
    warnings: list[str] = Field(default_factory=list)

    def headline(self) -> str:
        return (
            f"{self.user_count} users, {self.device_count} devices, "
            f"{self.extension_count} extensions; dial-plan complexity "
            f"{self.dial_plan_complexity_score}"
        )


#: Weights for the dial-plan complexity score. Deliberately crude and explicit:
#: a score is only useful if a human can see why it is what it is.
COMPLEXITY_WEIGHTS: dict[str, int] = {
    "Partition": 1,
    "CallingPermission": 2,
    "RoutePattern": 2,
    "TranslationPattern": 3,
    "DigitManipulationRule": 1,
    "HuntGroup": 3,
    "LineGroup": 2,
    "AutoAttendant": 5,
    "CallPark": 1,
    "Intercom": 2,
    "SharedLineAppearance": 2,
    "ExtensionMobilityProfile": 3,
}


class DiscoveryService:
    """Runs a read-only crawl and builds the estate report."""

    def __init__(self, connector: Connector) -> None:
        self.connector = connector

    async def run(
        self,
        *,
        run_id: str,
        tenant_id: str,
        estate_id: str,
        entity_kinds: list[str] | None = None,
        site_codes: list[str] | None = None,
        page_size: int = 500,
        snapshot_id: str | None = None,
    ) -> tuple[EstateSnapshot, EstateReport]:
        request = ExtractRequest(
            run_id=run_id,
            tenant_id=tenant_id,
            estate_id=estate_id,
            entity_kinds=entity_kinds,
            site_codes=site_codes,
            page_size=page_size,
            # Discovery never pulls message bodies. Voicemail and chat are personal
            # data; exporting them is a separate, consented step.
            include_message_payloads=False,
        )
        snapshot = await self.connector.extract_snapshot(
            request, snapshot_id=snapshot_id, snapshot_kind=SnapshotKind.DISCOVERY
        )
        report = build_estate_report(snapshot)
        return snapshot, report


def build_estate_report(snapshot: EstateSnapshot) -> EstateReport:
    entities = list(snapshot.entities)
    by_id = snapshot.by_id()

    users = [e for e in entities if isinstance(e, User)]
    devices = [e for e in entities if isinstance(e, Device)]
    extensions = [e for e in entities if isinstance(e, Extension)]
    lines = [e for e in entities if isinstance(e, Line)]
    numbers = [e for e in entities if isinstance(e, E164Number)]

    model_counts: Counter[str] = Counter()
    model_meta: dict[str, tuple[str, bool]] = {}
    for device in devices:
        label = device.model or "unknown"
        model_counts[label] += 1
        model_meta[label] = (device.device_type.value, device.replacement_required)

    # Duplicate DNs: the same dialable string appearing in more than one partition
    # is legal in CUCM and a collision on a flat cloud numbering plan.
    dn_counts: Counter[str] = Counter(line.directory_number for line in lines)
    duplicates = sorted(dn for dn, count in dn_counts.items() if count > 1)

    non_e164 = sorted(
        {n.e164 for n in numbers if not n.e164.startswith("+")}
        | {
            line.directory_number
            for line in lines
            if line.e164_ref is None and len(line.directory_number) > 7
        }
    )

    without_e164 = [e for e in extensions if e.e164_ref is None]

    dormant = sorted(
        u.user_principal_name
        for u in users
        if u.telephony_enabled and u.last_call_activity_at is None
    )

    referenced_partitions = {
        ref
        for entity in entities
        for field, values in entity.reference_fields().items()
        if field.endswith(("partition_ref", "partition_refs"))
        for ref in values
    }
    unused_partitions = sorted(
        (e.display_name or e.canonical_id)
        for e in entities
        if e.kind == "Partition" and e.canonical_id not in referenced_partitions
    )

    orphans = _find_orphans(entities, by_id)

    drivers = {
        kind: COMPLEXITY_WEIGHTS[kind] * count
        for kind, count in Counter(e.kind for e in entities).items()
        if kind in COMPLEXITY_WEIGHTS
    }

    raw_sql_reads = sum(
        1 for e in entities if e.source_ref is not None and e.source_ref.raw_sql_used
    )

    warnings = list(snapshot.warnings)
    if duplicates:
        warnings.append(
            f"{len(duplicates)} directory number(s) appear in more than one partition. "
            "These collide on a flat cloud numbering plan and must be resolved before cutover."
        )
    analogue = [d for d in devices if d.device_type is DeviceType.ANALOGUE]
    if analogue:
        warnings.append(
            f"{len(analogue)} analogue endpoint(s) found (fax, lift, door entry, paging). "
            "These do not migrate to a cloud PBX and need an explicit disposition."
        )

    return EstateReport(
        estate_id=snapshot.estate_id,
        snapshot_id=snapshot.snapshot_id,
        entity_counts=snapshot.counts_by_kind(),
        user_count=len(users),
        telephony_enabled_user_count=sum(1 for u in users if u.telephony_enabled),
        device_count=len(devices),
        device_models=[
            ModelBreakdown(
                model=model,
                count=count,
                device_type=model_meta[model][0],
                replacement_required=model_meta[model][1],
            )
            for model, count in sorted(model_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        devices_requiring_replacement=sum(1 for d in devices if d.replacement_required),
        analogue_endpoint_count=len(analogue),
        extension_count=len(extensions),
        extensions_without_e164=len(without_e164),
        non_e164_numbers=non_e164[:200],
        duplicate_directory_numbers=duplicates,
        dormant_extensions=dormant,
        unused_partitions=unused_partitions,
        orphans=orphans,
        dial_plan_complexity_score=sum(drivers.values()),
        complexity_drivers=dict(sorted(drivers.items())),
        fidelity_by_kind=snapshot.fidelity_report(),
        unassessed_count=len(snapshot.unassessed()),
        estimated_manual_effort_minutes=snapshot.manual_effort_minutes(),
        raw_sql_reads=raw_sql_reads,
        warnings=warnings,
    )


def _find_orphans(
    entities: list[CanonicalEntity], by_id: dict[str, CanonicalEntity]
) -> list[OrphanFinding]:
    """Objects pointing at something that is not in the estate.

    Dangling references are usually the residue of a half-finished change years
    ago, and they break a migration because the transform has nothing to resolve.
    """
    findings: list[OrphanFinding] = []
    counts: defaultdict[str, int] = defaultdict(int)

    for entity in entities:
        for field, values in entity.reference_fields().items():
            for referenced in values:
                if referenced in by_id:
                    continue
                counts[entity.kind] += 1
                if counts[entity.kind] > 50:  # keep the report readable
                    continue
                findings.append(
                    OrphanFinding(
                        kind=entity.kind,
                        canonical_id=entity.canonical_id,
                        display_name=entity.display_name,
                        reason=f"{field} points at {referenced}, which is not in this snapshot",
                    )
                )
    return findings


def render_estate_report_markdown(report: EstateReport) -> str:
    """Human-readable estate report for the CAB pack and the console."""
    lines: list[str] = [
        f"# Estate report: {report.estate_id}",
        "",
        f"_Snapshot `{report.snapshot_id}`, generated {report.generated_at.isoformat()}_",
        "",
        f"**{report.headline()}**",
        "",
        "## Inventory",
        "",
        "| Entity | Count |",
        "|---|---:|",
    ]
    lines.extend(f"| {kind} | {count} |" for kind, count in report.entity_counts.items())

    lines += [
        "",
        "## Endpoints",
        "",
        f"- Devices: **{report.device_count}**",
        f"- Requiring replacement: **{report.devices_requiring_replacement}**",
        f"- Analogue (fax / lift / door / paging): **{report.analogue_endpoint_count}**",
        "",
        "| Model | Count | Type | Replace |",
        "|---|---:|---|---|",
    ]
    lines.extend(
        f"| {m.model} | {m.count} | {m.device_type} | {'yes' if m.replacement_required else 'no'} |"
        for m in report.device_models
    )

    lines += [
        "",
        "## Numbering",
        "",
        f"- Extensions: **{report.extension_count}**",
        f"- Without an E.164 mapping: **{report.extensions_without_e164}** "
        "(a blocker for Teams Phone)",
        f"- Duplicate directory numbers: **{len(report.duplicate_directory_numbers)}**",
        f"- Dormant (no CDR activity): **{len(report.dormant_extensions)}**",
        "",
        "## Dial plan",
        "",
        f"Complexity score: **{report.dial_plan_complexity_score}**",
        "",
        "| Driver | Contribution |",
        "|---|---:|",
    ]
    lines.extend(f"| {k} | {v} |" for k, v in report.complexity_drivers.items())

    lines += [
        "",
        f"- Unused partitions: **{len(report.unused_partitions)}**",
        f"- Dangling references: **{len(report.orphans)}**",
        "",
        "## Fidelity",
        "",
        "| Entity | LOSSLESS | DEGRADED | UNMAPPABLE |",
        "|---|---:|---:|---:|",
    ]
    for kind, buckets in report.fidelity_by_kind.items():
        lines.append(
            f"| {kind} | {buckets.get(FidelityLevel.LOSSLESS.value, 0)} "
            f"| {buckets.get(FidelityLevel.DEGRADED.value, 0)} "
            f"| {buckets.get(FidelityLevel.UNMAPPABLE.value, 0)} |"
        )

    lines += [
        "",
        f"Estimated manual remediation: **{report.estimated_manual_effort_minutes} minutes** "
        f"({report.estimated_manual_effort_minutes / 60:.1f} hours)",
        "",
    ]
    if report.unassessed_count:
        lines.append(
            f"> {report.unassessed_count} entities have no fidelity assessment. A plan cannot "
            "be approved while this is non-zero."
        )
        lines.append("")
    if report.raw_sql_reads:
        lines.append(f"> {report.raw_sql_reads} object(s) were read via raw SQL.")
        lines.append("")
    if report.warnings:
        lines.append("## Warnings")
        lines.append("")
        lines.extend(f"- {w}" for w in report.warnings)
        lines.append("")

    return "\n".join(lines)
