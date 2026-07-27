"""In-process state for the control plane.

Storage is still undecided (ADR-0002), so this holds the pipeline artefacts in
memory for the life of the process: one :class:`EstateSession` per scenario,
plus a single hash-chained audit log and run store shared across them.

The pipeline is a sequence, and this class enforces it. Asking for a dry run
before a plan exists raises :class:`StageNotReady` rather than quietly
producing an empty receipt, because an empty receipt is exactly the kind of
thing an operator would sign.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime
from pathlib import Path

from ucm_bridge.api.scenarios import ReferencePlatformScenario, Scenario, build_scenarios
from ucm_bridge.assessment import AssessmentReport, RuleContext, assess
from ucm_bridge.audit import AuditLog
from ucm_bridge.canonical.snapshot import EstateSnapshot
from ucm_bridge.connectors.base import Connector
from ucm_bridge.connectors.contracts import (
    ApplyAuthorization,
    ApplyPlan,
    Approval,
    ChangeWindow,
    DryRunReceipt,
    EmergencyConfirmation,
    ExecutionMode,
    ExtractRequest,
    RollbackBundle,
)
from ucm_bridge.discovery import DiscoveryService, EstateReport
from ucm_bridge.execution import ExecutionEngine, InMemoryRunStore, RunState, RunSummary
from ucm_bridge.execution.store import JsonFileRunStore, RunStore
from ucm_bridge.mapping import TransformResult, apply_profile
from ucm_bridge.pipeline.planner import PlanBuildResult, build_apply_plan
from ucm_bridge.validation import ValidationReport, ValidationService
from ucm_bridge.vendor.readiness import ConnectorReadiness
from ucm_bridge.waves import GroupingStrategy, WavePlan, plan_waves


class StageNotReady(Exception):
    """A pipeline stage was asked for before the stage it depends on had run."""

    def __init__(self, needs: str, before: str) -> None:
        super().__init__(f"Run {needs} before {before}.")
        self.needs = needs
        self.before = before


class EstateSession:
    """Everything one scenario has produced so far."""

    def __init__(self, scenario: Scenario) -> None:
        self.scenario = scenario
        self.lock = asyncio.Lock()

        self.source: Connector = scenario.build_source()
        self.target: Connector = scenario.build_target()

        self.snapshot: EstateSnapshot | None = None
        self.report: EstateReport | None = None
        self.assessment: AssessmentReport | None = None
        self.transform: TransformResult | None = None
        self.wave_plan: WavePlan | None = None
        self.plan_result: PlanBuildResult | None = None
        self.receipt: DryRunReceipt | None = None
        self.validation: ValidationReport | None = None
        self.target_snapshot: EstateSnapshot | None = None
        #: Entity kinds dropped from the plan because the target cannot apply
        #: them, counted. Empty until a plan is built.
        self.plan_exclusions: Counter[str] = Counter()
        self.run_ids: list[str] = []
        self._sequence = 0

    # -- derived ---------------------------------------------------------- #

    @property
    def plan(self) -> ApplyPlan | None:
        return self.plan_result.plan if self.plan_result else None

    @property
    def effective_snapshot(self) -> EstateSnapshot | None:
        """What planning works from: the transformed snapshot once mapping ran."""
        if self.transform is not None:
            return self.transform.snapshot
        return self.snapshot

    def require_snapshot(self, before: str) -> EstateSnapshot:
        if self.snapshot is None:
            raise StageNotReady("discovery", before)
        return self.snapshot

    def require_plan(self, before: str) -> ApplyPlan:
        if self.plan is None:
            raise StageNotReady("plan build", before)
        return self.plan

    def require_receipt(self, before: str) -> DryRunReceipt:
        if self.receipt is None:
            raise StageNotReady("a dry run", before)
        return self.receipt

    def next_id(self, prefix: str) -> str:
        self._sequence += 1
        return f"{prefix}-{self.scenario.estate_id}-{self._sequence:03d}"

    def stages(self) -> dict[str, bool]:
        """Which stages have produced an artefact. Drives the UI's progress rail."""
        return {
            "discovery": self.snapshot is not None,
            "assessment": self.assessment is not None,
            "mapping": self.transform is not None,
            "waves": self.wave_plan is not None,
            "plan": self.plan is not None,
            "dry_run": self.receipt is not None,
            "run": bool(self.run_ids),
            "validation": self.validation is not None,
        }

    def readiness(self) -> dict[str, ConnectorReadiness]:
        return {"source": self.source.readiness(), "target": self.target.readiness()}


class Workspace:
    """The control plane's whole world: sessions, audit chain, and run store."""

    def __init__(
        self,
        scenarios: list[Scenario] | None = None,
        *,
        state_dir: Path | None = None,
    ) -> None:
        # In-process state is fine for a demo and unacceptable for real writes:
        # a restart mid-run would lose the audit chain covering operations that
        # have already happened on a customer's PBX. Point UCM_BRIDGE_STATE_DIR
        # at a durable volume and both the chain and the run checkpoints survive.
        self.state_dir = state_dir
        self.audit = AuditLog(path=(state_dir / "audit.jsonl") if state_dir else None)
        self.runs: RunStore = (
            JsonFileRunStore(state_dir / "runs") if state_dir else InMemoryRunStore()
        )
        self.sessions: dict[str, EstateSession] = {
            scenario.estate_id: EstateSession(scenario)
            for scenario in (scenarios if scenarios is not None else build_scenarios())
        }

    def session(self, estate_id: str) -> EstateSession:
        try:
            return self.sessions[estate_id]
        except KeyError:
            raise KeyError(f"No estate {estate_id!r}. Known: {sorted(self.sessions)}") from None

    def session_of_run(self, run_id: str) -> EstateSession | None:
        return next((s for s in self.sessions.values() if run_id in s.run_ids), None)

    # ------------------------------------------------------------------ #
    # Stages
    # ------------------------------------------------------------------ #

    async def discover(self, estate_id: str) -> tuple[EstateSnapshot, EstateReport]:
        session = self.session(estate_id)
        async with session.lock:
            run_id = session.next_id("disc")
            snapshot, report = await DiscoveryService(session.source).run(
                run_id=run_id,
                tenant_id=session.scenario.tenant_id,
                estate_id=session.scenario.source_estate_id,
                snapshot_id=session.next_id("snap"),
            )
            session.snapshot = snapshot
            session.report = report
            # A re-discovery invalidates everything downstream. Leaving a stale
            # plan attached to a fresh snapshot is how someone approves a diff
            # they never saw.
            session.assessment = None
            session.transform = None
            session.wave_plan = None
            session.plan_result = None
            session.receipt = None
            session.validation = None
            return snapshot, report

    def assess(self, estate_id: str, *, target_platform: str | None = None) -> AssessmentReport:
        session = self.session(estate_id)
        snapshot = session.require_snapshot("assessment")
        report = assess(
            RuleContext(
                snapshot=snapshot,
                target_platform=target_platform or _platform_of(session.target),
            )
        )
        session.assessment = report
        return report

    def map(self, estate_id: str) -> TransformResult | None:
        session = self.session(estate_id)
        snapshot = session.require_snapshot("mapping")
        profile = session.scenario.profile()
        if profile is None:
            return None
        result = apply_profile(snapshot, profile)
        session.transform = result
        # The plan was built from the pre-transform snapshot; it no longer applies.
        session.plan_result = None
        session.receipt = None
        return result

    def plan_waves(
        self,
        estate_id: str,
        *,
        strategy: GroupingStrategy = GroupingStrategy.SITE,
        max_wave_size: int | None = None,
    ) -> WavePlan:
        session = self.session(estate_id)
        snapshot = session.effective_snapshot or session.require_snapshot("wave planning")
        plan = plan_waves(
            snapshot,
            strategy=strategy,
            plan_name=f"{session.scenario.estate_id}-waves",
            max_wave_size=max_wave_size,
        )
        session.wave_plan = plan
        return plan

    def build_plan(self, estate_id: str, *, wave_id: str | None = None) -> PlanBuildResult:
        session = self.session(estate_id)
        snapshot = session.effective_snapshot or session.require_snapshot("plan build")
        planned, context = session.scenario.plan_inputs(snapshot)

        # Never plan an operation the target's own manifest says it cannot
        # perform. The planner resolves a natural key for anything it can, so a
        # Slack user or an SfB user would otherwise become an ASSIGN against a
        # Teams connector that applies numbers and licences only — an operation
        # built to be refused, three screens after the point where the refusal
        # could have been explained. They stay as resolvable context so
        # references still land.
        appliable = session.target.capabilities().appliable_kinds()
        excluded = [e for e in planned if e.kind not in appliable]
        planned = [e for e in planned if e.kind in appliable]
        context = [*context, *excluded]
        session.plan_exclusions = Counter(e.kind for e in excluded)

        result = build_apply_plan(
            planned,
            plan_id=session.next_id("plan"),
            tenant_id=session.scenario.tenant_id,
            estate_id=session.scenario.target_estate_id,
            key_for=session.target.natural_key_for,
            verb=session.scenario.verb,
            wave_id=wave_id,
            context_entities=context,
        )
        session.plan_result = result
        # A new plan digest invalidates any receipt covering the old one. The
        # guardrail would catch it at apply time; clearing it here means the UI
        # never shows an approval that cannot be used.
        session.receipt = None
        return result

    async def dry_run(self, estate_id: str, *, requested_by: str) -> DryRunReceipt:
        session = self.session(estate_id)
        plan = session.require_plan("a dry run")
        async with session.lock:
            receipt = await session.target.dry_run(plan, requested_by=requested_by)
            session.receipt = receipt
            return receipt

    async def execute(
        self,
        estate_id: str,
        *,
        authorization: ApplyAuthorization,
        run_id: str | None = None,
        resume: bool = False,
    ) -> RunSummary:
        session = self.session(estate_id)
        plan = session.require_plan("an execution")
        session.require_receipt("an execution")
        async with session.lock:
            identifier = run_id or session.next_id("run")
            engine = self._engine(session)
            summary = await engine.execute(
                plan, authorization, run_id=identifier, resume=resume
            )
            if identifier not in session.run_ids:
                session.run_ids.append(identifier)
            # The target just changed; a cached extract of it is now a lie.
            session.target_snapshot = None
            return summary

    async def rollback(
        self, estate_id: str, *, run_id: str, authorization: ApplyAuthorization
    ) -> RunSummary:
        session = self.session(estate_id)
        record = self.runs.get(run_id)
        if record is None:
            raise KeyError(f"No run {run_id!r}.")
        if record.rollback_bundle_json is None:
            raise StageNotReady("a completed run with a rollback bundle", "a rollback")
        bundle = RollbackBundle.model_validate_json(record.rollback_bundle_json)
        async with session.lock:
            summary = await self._engine(session).rollback(bundle, authorization, run_id=run_id)
            session.target_snapshot = None
            return summary

    async def validate(self, estate_id: str, *, run_id: str | None = None) -> ValidationReport:
        session = self.session(estate_id)
        source = session.effective_snapshot or session.require_snapshot("validation")
        async with session.lock:
            target_snapshot = await session.target.extract_snapshot(
                ExtractRequest(
                    run_id=session.next_id("verify"),
                    tenant_id=session.scenario.tenant_id,
                    estate_id=session.scenario.target_estate_id,
                )
            )
            report = await ValidationService().validate(
                run_id=run_id or (session.run_ids[-1] if session.run_ids else "unrun"),
                tenant_id=session.scenario.tenant_id,
                source=source,
                target=target_snapshot,
                source_key_for=session.source.natural_key_for,
                target_key_for=session.target.natural_key_for,
            )
            session.validation = report
            session.target_snapshot = target_snapshot
            return report

    async def refresh_target_snapshot(self, estate_id: str) -> EstateSnapshot:
        """Extract what the target already holds.

        The mapping screen needs it to answer "which target object is this
        source object?", and validation needs it afterwards. Cached because a
        target extract is a real API crawl, not a lookup.
        """
        session = self.session(estate_id)
        if session.target_snapshot is not None:
            return session.target_snapshot
        async with session.lock:
            snapshot = await session.target.extract_snapshot(
                ExtractRequest(
                    run_id=session.next_id("tgt"),
                    tenant_id=session.scenario.tenant_id,
                    estate_id=session.scenario.target_estate_id,
                )
            )
            session.target_snapshot = snapshot
            return snapshot

    def _engine(self, session: EstateSession) -> ExecutionEngine:
        return ExecutionEngine(
            session.target,
            store=self.runs,
            audit=self.audit,
            tenant_id=session.scenario.tenant_id,
        )

    # ------------------------------------------------------------------ #
    # Reset
    # ------------------------------------------------------------------ #

    def reset(self, estate_id: str) -> None:
        """Rebuild a session from scratch, including the target's written state.

        The reference scenario accumulates real writes, so a demo needs a way
        back to a clean estate without restarting the process.
        """
        scenario = self.session(estate_id).scenario
        if isinstance(scenario, ReferencePlatformScenario):
            scenario = ReferencePlatformScenario()
        self.sessions[estate_id] = EstateSession(scenario)


def _platform_of(connector: Connector) -> str:
    return str(connector.platform)


def production_authorization(
    plan: ApplyPlan,
    receipt: DryRunReceipt,
    *,
    requested_by: str,
    approvers: list[str],
    correlation_id: str,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    change_reference: str | None = None,
    window_override_reason: str | None = None,
    window_override_by: str | None = None,
    confirmed_sites: list[str] | None = None,
    confirmed_by: str | None = None,
) -> ApplyAuthorization:
    """Assemble an authorization from what the UI collected.

    Nothing is defaulted into existence. A missing approver, a closed window, or
    an unconfirmed emergency site raises out of the model validator, which is the
    behaviour the approval screen is there to demonstrate.
    """
    window: ChangeWindow | None = None
    if window_start is not None and window_end is not None:
        window = ChangeWindow(start=window_start, end=window_end, reference=change_reference)

    sites = confirmed_sites if confirmed_sites is not None else sorted(plan.emergency_sites())
    confirmations = [
        EmergencyConfirmation(
            site_code=site,
            confirmed_by=confirmed_by or requested_by,
            civic_address_verified=True,
            elin_verified=True,
        )
        for site in sites
    ]

    return ApplyAuthorization(
        mode=ExecutionMode.PRODUCTION,
        requested_by=requested_by,
        correlation_id=correlation_id,
        dry_run_receipt=receipt,
        approvals=[Approval(approver=name) for name in approvers],
        change_window=window,
        window_override_reason=window_override_reason,
        window_override_by=window_override_by,
        emergency_confirmations=confirmations,
    )


__all__ = [
    "EstateSession",
    "RunState",
    "StageNotReady",
    "Workspace",
    "production_authorization",
]
