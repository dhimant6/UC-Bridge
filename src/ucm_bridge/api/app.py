"""The control-plane HTTP surface.

Thin by design. Every endpoint is a call into the library plus serialisation;
no business rule lives here. That matters because the guardrails are enforced
in the library, and an API that re-implemented any of them would eventually
disagree with the thing that actually runs.

Errors are mapped, not swallowed:

============================  ======  ================================
Exception                     Status  Meaning
============================  ======  ================================
``PermissionDenied``          403     The role does not carry the right
``CrossTenantAccess``         403     Scoped to another tenant
``StageNotReady``             409     A prerequisite stage has not run
``GuardrailViolation``        422     A safety rule refused the request
``KeyError``                  404     No such estate, run, or connector
============================  ======  ================================

A 422 from a guardrail is a *successful* outcome for this product. The UI
renders those refusals as first-class results rather than as failures.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

from ucm_bridge import __version__
from ucm_bridge.api.catalogue import ConnectorCatalogue
from ucm_bridge.api.workspace import StageNotReady, Workspace, production_authorization
from ucm_bridge.assessment import Finding, render_assessment_markdown
from ucm_bridge.audit import AuditAction, TamperDetected, evidence_pack
from ucm_bridge.canonical.base import CanonicalEntity
from ucm_bridge.connectors.errors import GuardrailViolation
from ucm_bridge.discovery import render_estate_report_markdown
from ucm_bridge.execution import RunSummary
from ucm_bridge.mapping import mapping_summary, suggest_all
from ucm_bridge.runbook import build_runbook, render_runbook_markdown
from ucm_bridge.tenancy import (
    ROLE_PERMISSIONS,
    CrossTenantAccess,
    Permission,
    PermissionDenied,
    Role,
    TenantContext,
)
from ucm_bridge.validation import render_validation_markdown
from ucm_bridge.waves import (
    GroupingStrategy,
    coexistence_requirements,
    move_user,
    render_wave_plan_markdown,
)

DEMO_PRINCIPAL = "demo@contoso.example"
DEFAULT_ROLES = (Role.PLANNER,)


class BlockerCannotBeWaived(Exception):
    """``Finding.waive`` refused. Translated here so it reads like every other refusal.

    The library raises a plain ``ValueError`` for this, which would otherwise
    arrive as a 500 — the one status that would make a working guardrail look
    like a bug.
    """


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #


class AssessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_platform: str | None = None


class WaiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    by: str
    reason: str


class WavePlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: GroupingStrategy = GroupingStrategy.SITE
    max_wave_size: int | None = Field(default=None, ge=1)


class MoveUserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_key: str
    to_wave_id: str


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    wave_id: str | None = None


class ExecuteRequest(BaseModel):
    """What the approval screen collects before a production write."""

    model_config = ConfigDict(extra="forbid")

    requested_by: str = "operator@contoso.example"
    approvers: list[str] = Field(
        default_factory=lambda: ["planner@contoso.example", "approver@contoso.example"],
        description="Two distinct principals. One is refused by the guardrail, on purpose.",
    )
    correlation_id: str = "corr-ui-0001"
    window_start: datetime | None = None
    window_end: datetime | None = None
    change_reference: str | None = "CHG0042311"
    window_override_reason: str | None = None
    window_override_by: str | None = None
    confirmed_sites: list[str] | None = Field(
        default=None,
        description="Emergency sites confirmed per site. None means 'confirm every site "
        "the plan touches'; an empty list demonstrates the refusal.",
    )
    confirmed_by: str | None = None
    run_id: str | None = None
    resume: bool = False


# --------------------------------------------------------------------------- #
# Response shapes that are not just a library model
# --------------------------------------------------------------------------- #


class EntityRow(BaseModel):
    """One row of the virtualised entity table. Deliberately small."""

    model_config = ConfigDict(extra="forbid")

    canonical_id: str
    kind: str
    domain: str
    display_name: str | None
    fidelity: str
    is_assessed: bool
    degraded_count: int
    unmapped_count: int
    manual_effort_minutes: int | None
    native_key: str | None
    native_type: str | None
    platform: str | None


class EntityPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int
    offset: int
    limit: int
    kinds: list[str]
    rows: list[EntityRow]


class StageState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    estate_id: str
    name: str
    summary: str
    tenant_id: str
    direction: str
    source_connector_id: str
    target_connector_id: str
    source_estate_id: str
    target_estate_id: str
    write_verb: str
    has_mapping_profile: bool
    stages: dict[str, bool]
    headline: str | None
    source_readiness: str
    target_readiness: str
    target_may_write_to_production: bool
    run_ids: list[str]


class SessionInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    principal: str
    tenant_id: str
    roles: list[str]
    permissions: list[str]
    role_catalogue: dict[str, list[str]]
    version: str


def _row(entity: CanonicalEntity) -> EntityRow:
    source = entity.source_ref
    target = entity.target_ref
    ref = target or source
    return EntityRow(
        canonical_id=entity.canonical_id,
        kind=entity.kind,
        domain=type(entity).domain,
        display_name=entity.display_name,
        fidelity=entity.fidelity.level.value,
        is_assessed=entity.fidelity.is_assessed,
        degraded_count=len(entity.fidelity.degraded_attributes),
        unmapped_count=len(entity.fidelity.unmapped_source_attributes),
        manual_effort_minutes=entity.fidelity.manual_effort_minutes,
        native_key=ref.native_key if ref else None,
        native_type=ref.native_type if ref else None,
        platform=ref.platform.value if ref else None,
    )


def _problem(status: int, kind: str, message: str, **extra: Any) -> JSONResponse:
    """One error shape for the whole surface, so the console can branch on it."""
    return JSONResponse(status_code=status, content={"error": kind, "message": message, **extra})


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #


def tenant_context(
    x_ucm_roles: Annotated[str | None, Header()] = None,
    x_ucm_principal: Annotated[str | None, Header()] = None,
) -> TenantContext:
    """Demo identity, taken from headers so the UI can switch roles.

    A real deployment resolves this from an OIDC token. Keeping it in one
    dependency means swapping it later touches exactly this function.

    It lives at module scope rather than inside :func:`create_app` because this
    module uses postponed annotations, and FastAPI resolves a route's type hints
    against module globals — a closure-local alias is invisible to it and every
    ``ctx`` parameter would silently become a query parameter.
    """
    names = [part.strip() for part in (x_ucm_roles or "").split(",") if part.strip()]
    try:
        roles = frozenset(Role(name.upper()) for name in names) or frozenset(DEFAULT_ROLES)
    except ValueError as exc:
        raise HTTPException(400, f"Unknown role: {exc}") from exc
    return TenantContext(
        tenant_id="contoso",
        principal=x_ucm_principal or DEMO_PRINCIPAL,
        roles=roles,
    )


Ctx = Annotated[TenantContext, Depends(tenant_context)]


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #


def create_app(*, workspace: Workspace | None = None, static_dir: Path | None = None) -> FastAPI:
    app = FastAPI(
        title="UCM-Bridge control plane",
        version=__version__,
        summary="Bidirectional Unified Communications migration and coexistence platform.",
        description=__doc__,
        # Under /api with everything else, so the console owns the rest of the
        # path space and the docs link cannot collide with a client route.
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )
    space = workspace or Workspace()
    catalogue = ConnectorCatalogue()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # -- errors --------------------------------------------------------- #

    @app.exception_handler(PermissionDenied)
    async def _denied(_request: Request, exc: PermissionDenied) -> JSONResponse:
        return _problem(403, "PermissionDenied", str(exc))

    @app.exception_handler(CrossTenantAccess)
    async def _cross_tenant(_request: Request, exc: CrossTenantAccess) -> JSONResponse:
        return _problem(403, "CrossTenantAccess", str(exc))

    @app.exception_handler(StageNotReady)
    async def _not_ready(_request: Request, exc: StageNotReady) -> JSONResponse:
        return _problem(409, "StageNotReady", str(exc), needs=exc.needs, before=exc.before)

    @app.exception_handler(GuardrailViolation)
    async def _guardrail(_request: Request, exc: GuardrailViolation) -> JSONResponse:
        # 422, not 500. A guardrail refusing a write is the system working.
        return _problem(422, type(exc).__name__, str(exc), guardrail=True)

    @app.exception_handler(BlockerCannotBeWaived)
    async def _blocker(_request: Request, exc: BlockerCannotBeWaived) -> JSONResponse:
        return _problem(422, "BlockerCannotBeWaived", str(exc), guardrail=True)

    @app.exception_handler(TamperDetected)
    async def _tampered(_request: Request, exc: TamperDetected) -> JSONResponse:
        return _problem(422, "TamperDetected", str(exc), guardrail=True)

    @app.exception_handler(KeyError)
    async def _missing(_request: Request, exc: KeyError) -> JSONResponse:
        return _problem(404, "NotFound", str(exc.args[0] if exc.args else exc))

    # -- meta ----------------------------------------------------------- #

    @app.get("/api/health", tags=["meta"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/api/session", tags=["meta"], response_model=SessionInfo)
    async def session_info(ctx: Ctx) -> SessionInfo:
        return SessionInfo(
            principal=ctx.principal,
            tenant_id=ctx.tenant_id,
            roles=sorted(r.value for r in ctx.roles),
            permissions=sorted(p.value for p in ctx.permissions),
            role_catalogue={
                role.value: sorted(p.value for p in perms)
                for role, perms in ROLE_PERMISSIONS.items()
            },
            version=__version__,
        )

    # -- 1. estates and discovery --------------------------------------- #

    def _state(estate_id: str) -> StageState:
        session = space.session(estate_id)
        scenario = session.scenario
        readiness = session.readiness()
        return StageState(
            estate_id=scenario.estate_id,
            name=scenario.name,
            summary=scenario.summary,
            tenant_id=scenario.tenant_id,
            direction=scenario.direction,
            source_connector_id=session.source.connector_id,
            target_connector_id=session.target.connector_id,
            source_estate_id=scenario.source_estate_id,
            target_estate_id=scenario.target_estate_id,
            write_verb=scenario.verb.value,
            has_mapping_profile=scenario.profile() is not None,
            stages=session.stages(),
            headline=session.report.headline() if session.report else None,
            source_readiness=readiness["source"].level.value,
            target_readiness=readiness["target"].level.value,
            target_may_write_to_production=readiness["target"].may_write_to_production,
            run_ids=list(session.run_ids),
        )

    @app.get("/api/estates", tags=["estate"], response_model=list[StageState])
    async def list_estates(ctx: Ctx) -> list[StageState]:
        ctx.require(Permission.READ_ESTATE)
        return [_state(estate_id) for estate_id in space.sessions]

    @app.get("/api/estates/{estate_id}", tags=["estate"], response_model=StageState)
    async def get_estate(estate_id: str, ctx: Ctx) -> StageState:
        ctx.require(Permission.READ_ESTATE)
        return _state(estate_id)

    @app.post("/api/estates/{estate_id}/discover", tags=["estate"])
    async def discover(estate_id: str, ctx: Ctx) -> dict[str, Any]:
        ctx.require(Permission.RUN_DISCOVERY)
        snapshot, report = await space.discover(estate_id)
        return {
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_digest": snapshot.snapshot_digest,
            "entity_count": len(snapshot),
            "report": report.model_dump(mode="json"),
        }

    @app.get("/api/estates/{estate_id}/report", tags=["estate"])
    async def estate_report(estate_id: str, ctx: Ctx) -> dict[str, Any]:
        ctx.require(Permission.READ_ESTATE)
        session = space.session(estate_id)
        if session.report is None:
            raise StageNotReady("discovery", "the estate report")
        return session.report.model_dump(mode="json")

    @app.get(
        "/api/estates/{estate_id}/report.md",
        tags=["estate"],
        response_class=PlainTextResponse,
    )
    async def estate_report_markdown(estate_id: str, ctx: Ctx) -> str:
        ctx.require(Permission.READ_ESTATE)
        session = space.session(estate_id)
        if session.report is None:
            raise StageNotReady("discovery", "the estate report")
        return render_estate_report_markdown(session.report)

    @app.get("/api/estates/{estate_id}/entities", tags=["estate"], response_model=EntityPage)
    async def entities(
        estate_id: str,
        ctx: Ctx,
        kind: Annotated[str | None, Query()] = None,
        q: Annotated[str | None, Query(description="Substring of id or display name.")] = None,
        fidelity: Annotated[str | None, Query()] = None,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=2000)] = 200,
        transformed: Annotated[bool, Query()] = True,
    ) -> EntityPage:
        ctx.require(Permission.READ_ESTATE)
        session = space.session(estate_id)
        snapshot = (session.effective_snapshot if transformed else session.snapshot) or None
        if snapshot is None:
            raise StageNotReady("discovery", "the entity list")

        rows = list(snapshot.entities)
        kinds = sorted({e.kind for e in rows})
        if kind:
            rows = [e for e in rows if e.kind == kind]
        if fidelity:
            rows = [e for e in rows if e.fidelity.level.value == fidelity]
        if q:
            needle = q.casefold()
            rows = [
                e
                for e in rows
                if needle in e.canonical_id.casefold()
                or needle in (e.display_name or "").casefold()
            ]
        return EntityPage(
            total=len(rows),
            offset=offset,
            limit=limit,
            kinds=kinds,
            rows=[_row(e) for e in rows[offset : offset + limit]],
        )

    @app.get("/api/estates/{estate_id}/entities/{canonical_id}", tags=["estate"])
    async def entity_detail(estate_id: str, canonical_id: str, ctx: Ctx) -> dict[str, Any]:
        ctx.require(Permission.READ_ESTATE)
        session = space.session(estate_id)
        snapshot = session.effective_snapshot
        if snapshot is None:
            raise StageNotReady("discovery", "an entity")
        entity = snapshot.by_id().get(canonical_id)
        if entity is None:
            raise KeyError(f"No entity {canonical_id!r} in this snapshot.")
        return {
            "entity": entity.model_dump(mode="json"),
            "content_view": entity.content_view(),
            "references": entity.reference_fields(),
            "history": [r.model_dump(mode="json") for r in space.audit.history_of(canonical_id)],
        }

    @app.post("/api/estates/{estate_id}/reset", tags=["estate"])
    async def reset(estate_id: str, ctx: Ctx) -> StageState:
        ctx.require(Permission.RUN_DISCOVERY)
        space.reset(estate_id)
        return _state(estate_id)

    # -- 2. assessment --------------------------------------------------- #

    @app.post("/api/estates/{estate_id}/assess", tags=["assessment"])
    async def run_assessment(estate_id: str, body: AssessRequest, ctx: Ctx) -> dict[str, Any]:
        ctx.require(Permission.READ_ESTATE)
        report = space.assess(estate_id, target_platform=body.target_platform)
        return report.model_dump(mode="json")

    @app.get("/api/estates/{estate_id}/assessment", tags=["assessment"])
    async def get_assessment(estate_id: str, ctx: Ctx) -> dict[str, Any]:
        ctx.require(Permission.READ_ESTATE)
        session = space.session(estate_id)
        if session.assessment is None:
            raise StageNotReady("an assessment", "reading it")
        payload = session.assessment.model_dump(mode="json")
        payload["is_ready_to_plan"] = session.assessment.is_ready_to_plan
        payload["counts_by_severity"] = session.assessment.counts_by_severity()
        return payload

    @app.get(
        "/api/estates/{estate_id}/assessment.md",
        tags=["assessment"],
        response_class=PlainTextResponse,
    )
    async def assessment_markdown(estate_id: str, ctx: Ctx) -> str:
        ctx.require(Permission.READ_ESTATE)
        session = space.session(estate_id)
        if session.assessment is None:
            raise StageNotReady("an assessment", "rendering it")
        return render_assessment_markdown(session.assessment)

    @app.post("/api/estates/{estate_id}/assessment/{rule_id}/waive", tags=["assessment"])
    async def waive(estate_id: str, rule_id: str, body: WaiveRequest, ctx: Ctx) -> dict[str, Any]:
        """Waive a finding. An emergency-calling BLOCKER refuses, by design."""
        ctx.require(Permission.APPROVE_PLAN)
        session = space.session(estate_id)
        if session.assessment is None:
            raise StageNotReady("an assessment", "waiving a finding")
        findings: list[Finding] = []
        matched: Finding | None = None
        for finding in session.assessment.findings:
            if finding.rule_id != rule_id:
                findings.append(finding)
                continue
            try:
                matched = finding.waive(by=body.by, reason=body.reason)
            except ValueError as exc:
                # A BLOCKER refuses to be waived. That is the rule working, so it
                # is reported the same way every other guardrail refusal is.
                raise BlockerCannotBeWaived(str(exc)) from exc
            findings.append(matched)
        if matched is None:
            raise KeyError(f"No finding {rule_id!r} in this assessment.")
        session.assessment = session.assessment.model_copy(update={"findings": findings})
        space.audit.append(
            tenant_id=session.scenario.tenant_id,
            actor=body.by,
            action=AuditAction.FINDING_WAIVED,
            detail=f"{rule_id}: {body.reason}",
        )
        return matched.model_dump(mode="json")

    # -- 3. mapping ------------------------------------------------------ #

    @app.post("/api/estates/{estate_id}/map", tags=["mapping"])
    async def run_mapping(estate_id: str, ctx: Ctx) -> dict[str, Any]:
        ctx.require(Permission.EDIT_MAPPING)
        space.map(estate_id)
        return await mapping_view(estate_id, ctx)

    @app.get("/api/estates/{estate_id}/mapping", tags=["mapping"])
    async def mapping_view(estate_id: str, ctx: Ctx) -> dict[str, Any]:
        ctx.require(Permission.READ_ESTATE)
        session = space.session(estate_id)
        profile = session.scenario.profile()
        snapshot = session.require_snapshot("the mapping view")

        # Auto-mapping answers "which object on the target is this one?", so it
        # has to score against the target's own inventory. Capped because the
        # comparison is O(sources x targets) and a screen does not need all of it.
        target_entities = (await space.refresh_target_snapshot(estate_id)).entities
        candidates = suggest_all(snapshot.entities[:300], target_entities)
        payload: dict[str, Any] = {
            "profile": profile.model_dump(mode="json") if profile else None,
            "has_profile": profile is not None,
            "transform": None,
            "automap": {
                "summary": mapping_summary(candidates),
                "candidates": [c.model_dump(mode="json") for c in candidates[:200]],
            },
        }
        if profile is not None:
            plan = profile.number_plan
            payload["number_plan"] = {
                "overlaps": [o.model_dump(mode="json") for o in plan.detect_overlaps()],
                "rules": [r.model_dump(mode="json") for r in plan.rules],
            }
        if session.transform is not None:
            transform = session.transform
            payload["transform"] = {
                "is_clean": transform.is_clean,
                "issues": [i.model_dump(mode="json") for i in transform.issues],
                "overlaps": [o.model_dump(mode="json") for o in transform.overlaps],
                "collisions": [c.model_dump(mode="json") for c in transform.collisions],
                "numbers_created": transform.numbers_created,
                "rules_fired": transform.rules_fired,
                "fidelity_by_kind": transform.snapshot.fidelity_report(),
                "entity_count": len(transform.snapshot),
            }
        return payload

    # -- 4. waves and runbooks ------------------------------------------- #

    @app.post("/api/estates/{estate_id}/waves", tags=["waves"])
    async def build_waves(estate_id: str, body: WavePlanRequest, ctx: Ctx) -> dict[str, Any]:
        ctx.require(Permission.BUILD_PLAN)
        plan = space.plan_waves(
            estate_id, strategy=body.strategy, max_wave_size=body.max_wave_size
        )
        return _wave_payload(estate_id, plan)

    @app.get("/api/estates/{estate_id}/waves", tags=["waves"])
    async def get_waves(estate_id: str, ctx: Ctx) -> dict[str, Any]:
        ctx.require(Permission.READ_ESTATE)
        session = space.session(estate_id)
        if session.wave_plan is None:
            raise StageNotReady("wave planning", "reading the wave plan")
        return _wave_payload(estate_id, session.wave_plan)

    def _wave_payload(estate_id: str, plan: Any) -> dict[str, Any]:
        session = space.session(estate_id)
        snapshot = session.effective_snapshot
        requirements = (
            coexistence_requirements(plan, snapshot) if snapshot is not None else []
        )
        return {
            "plan": plan.model_dump(mode="json"),
            "is_valid": plan.is_valid,
            "summary": plan.summary(),
            "coexistence": [r.model_dump(mode="json") for r in requirements],
        }

    @app.post("/api/estates/{estate_id}/waves/move", tags=["waves"])
    async def move(estate_id: str, body: MoveUserRequest, ctx: Ctx) -> dict[str, Any]:
        """Move a user between waves. Splitting a dependency cluster is reported."""
        ctx.require(Permission.BUILD_PLAN)
        session = space.session(estate_id)
        if session.wave_plan is None:
            raise StageNotReady("wave planning", "moving a user")
        session.wave_plan = move_user(
            session.wave_plan, user_key=body.user_key, to_wave_id=body.to_wave_id
        )
        return _wave_payload(estate_id, session.wave_plan)

    @app.get(
        "/api/estates/{estate_id}/waves/{wave_id}/runbook",
        tags=["waves"],
        response_class=PlainTextResponse,
    )
    async def runbook(estate_id: str, wave_id: str, ctx: Ctx) -> str:
        ctx.require(Permission.READ_ESTATE)
        session = space.session(estate_id)
        if session.wave_plan is None:
            raise StageNotReady("wave planning", "a runbook")
        wave = next((w for w in session.wave_plan.waves if w.wave_id == wave_id), None)
        if wave is None:
            raise KeyError(f"No wave {wave_id!r} in this plan.")
        plan = session.require_plan("a runbook")
        coexistence = next(
            (
                r
                for r in coexistence_requirements(
                    session.wave_plan, session.effective_snapshot
                )
                if r.wave_id == wave_id
            ),
            None,
        ) if session.effective_snapshot is not None else None
        return render_runbook_markdown(
            build_runbook(
                wave=wave,
                plan=plan,
                dry_run=session.receipt,
                assessment=session.assessment,
                coexistence=coexistence,
                change_reference="CHG0042311",
            )
        )

    @app.get(
        "/api/estates/{estate_id}/waves.md", tags=["waves"], response_class=PlainTextResponse
    )
    async def waves_markdown(estate_id: str, ctx: Ctx) -> str:
        ctx.require(Permission.READ_ESTATE)
        session = space.session(estate_id)
        if session.wave_plan is None:
            raise StageNotReady("wave planning", "rendering it")
        return render_wave_plan_markdown(session.wave_plan)

    # -- 5. plan and dry run --------------------------------------------- #

    @app.post("/api/estates/{estate_id}/plan", tags=["plan"])
    async def build_plan(estate_id: str, body: PlanRequest, ctx: Ctx) -> dict[str, Any]:
        ctx.require(Permission.BUILD_PLAN)
        result = space.build_plan(estate_id, wave_id=body.wave_id)
        return _plan_payload(result)

    @app.get("/api/estates/{estate_id}/plan", tags=["plan"])
    async def get_plan(estate_id: str, ctx: Ctx) -> dict[str, Any]:
        ctx.require(Permission.READ_ESTATE)
        session = space.session(estate_id)
        if session.plan_result is None:
            raise StageNotReady("plan build", "reading the plan")
        return _plan_payload(session.plan_result)

    def _plan_payload(result: Any) -> dict[str, Any]:
        plan = result.plan
        return {
            "plan": plan.model_dump(mode="json"),
            "plan_digest": plan.plan_digest or plan.compute_digest(),
            "operation_count": len(plan.operations),
            "emergency_sites": sorted(plan.emergency_sites()),
            "unmappable_operations": [
                op.model_dump(mode="json") for op in plan.unmappable_operations()
            ],
            "unresolved_references": [
                u.model_dump(mode="json") for u in result.unresolved_references
            ],
            "skipped_unmappable": list(result.skipped_unmappable),
            "is_fully_resolved": result.is_fully_resolved,
        }

    @app.post("/api/estates/{estate_id}/dry-run", tags=["plan"])
    async def dry_run(estate_id: str, ctx: Ctx) -> dict[str, Any]:
        ctx.require(Permission.RUN_DRY_RUN)
        receipt = await space.dry_run(estate_id, requested_by=ctx.principal)
        return receipt.model_dump(mode="json")

    @app.get("/api/estates/{estate_id}/dry-run", tags=["plan"])
    async def get_dry_run(estate_id: str, ctx: Ctx) -> dict[str, Any]:
        ctx.require(Permission.READ_ESTATE)
        session = space.session(estate_id)
        return session.require_receipt("reading it").model_dump(mode="json")

    # -- 6. runs ---------------------------------------------------------- #

    def _run_payload(summary: RunSummary) -> dict[str, Any]:
        payload = summary.model_dump(mode="json")
        payload["progress"] = summary.progress
        payload["succeeded"] = summary.succeeded
        payload["has_rollback_bundle"] = summary.rollback_bundle is not None
        return payload

    @app.get("/api/runs", tags=["runs"])
    async def list_runs(ctx: Ctx) -> list[dict[str, Any]]:
        ctx.require(Permission.READ_ESTATE)
        records = space.runs.list_runs(tenant_id=ctx.tenant_id)
        return [
            {
                **record.model_dump(mode="json", exclude={"rollback_bundle_json"}),
                "has_rollback_bundle": record.rollback_bundle_json is not None,
                "estate_id": next(
                    (
                        s.scenario.estate_id
                        for s in space.sessions.values()
                        if record.run_id in s.run_ids
                    ),
                    None,
                ),
            }
            for record in records
        ]

    @app.get("/api/runs/{run_id}", tags=["runs"])
    async def get_run(run_id: str, ctx: Ctx) -> dict[str, Any]:
        ctx.require(Permission.READ_ESTATE)
        record = space.runs.get(run_id)
        if record is None:
            raise KeyError(f"No run {run_id!r}.")
        return {
            **record.model_dump(mode="json", exclude={"rollback_bundle_json"}),
            "has_rollback_bundle": record.rollback_bundle_json is not None,
            "audit": [
                r.model_dump(mode="json") for r in space.audit.search(run_id=run_id)
            ],
        }

    @app.post("/api/estates/{estate_id}/runs", tags=["runs"])
    async def execute(estate_id: str, body: ExecuteRequest, ctx: Ctx) -> dict[str, Any]:
        """Execute a plan in PRODUCTION mode.

        Every refusal on the way — missing receipt, one approver, closed window,
        unconfirmed emergency site, an unverified connector — arrives here as a
        422 carrying the guardrail's own message.
        """
        ctx.require(Permission.EXECUTE_PRODUCTION)
        session = space.session(estate_id)
        plan = session.require_plan("an execution")
        receipt = session.require_receipt("an execution")
        authorization = production_authorization(
            plan,
            receipt,
            requested_by=body.requested_by,
            approvers=body.approvers,
            correlation_id=body.correlation_id,
            window_start=body.window_start,
            window_end=body.window_end,
            change_reference=body.change_reference,
            window_override_reason=body.window_override_reason,
            window_override_by=body.window_override_by,
            confirmed_sites=body.confirmed_sites,
            confirmed_by=body.confirmed_by,
        )
        summary = await space.execute(
            estate_id, authorization=authorization, run_id=body.run_id, resume=body.resume
        )
        return _run_payload(summary)

    @app.post("/api/estates/{estate_id}/runs/{run_id}/rollback", tags=["runs"])
    async def rollback(
        estate_id: str, run_id: str, body: ExecuteRequest, ctx: Ctx
    ) -> dict[str, Any]:
        ctx.require(Permission.ROLLBACK)
        session = space.session(estate_id)
        plan = session.require_plan("a rollback")
        receipt = session.require_receipt("a rollback")
        authorization = production_authorization(
            plan,
            receipt,
            requested_by=body.requested_by,
            approvers=body.approvers,
            correlation_id=body.correlation_id,
            window_start=body.window_start,
            window_end=body.window_end,
            change_reference=body.change_reference,
            window_override_reason=body.window_override_reason,
            window_override_by=body.window_override_by,
            confirmed_sites=body.confirmed_sites,
            confirmed_by=body.confirmed_by,
        )
        summary = await space.rollback(estate_id, run_id=run_id, authorization=authorization)
        return _run_payload(summary)

    # -- 7. validation ---------------------------------------------------- #

    @app.post("/api/estates/{estate_id}/validate", tags=["validation"])
    async def validate(estate_id: str, ctx: Ctx) -> dict[str, Any]:
        ctx.require(Permission.READ_ESTATE)
        report = await space.validate(estate_id)
        return _validation_payload(report)

    @app.get("/api/estates/{estate_id}/validation", tags=["validation"])
    async def get_validation(estate_id: str, ctx: Ctx) -> dict[str, Any]:
        ctx.require(Permission.READ_ESTATE)
        session = space.session(estate_id)
        if session.validation is None:
            raise StageNotReady("validation", "reading it")
        return _validation_payload(session.validation)

    def _validation_payload(report: Any) -> dict[str, Any]:
        payload: dict[str, Any] = report.model_dump(mode="json")
        payload["passed"] = report.passed
        payload["safe_to_sign_off"] = report.safe_to_sign_off
        payload["counts"] = report.counts()
        payload["markdown"] = render_validation_markdown(report)
        if report.reconciliation is not None:
            # ``passed`` is a property and ``counts_by_status`` a method, so
            # neither survives model_dump. Both are what the screen leads with.
            payload["reconciliation"]["passed"] = report.reconciliation.passed
            payload["reconciliation"]["counts_by_status"] = (
                report.reconciliation.counts_by_status()
            )
            payload["reconciliation"]["summary"] = report.reconciliation.summary()
        return payload

    # -- 8. audit --------------------------------------------------------- #

    @app.get("/api/audit", tags=["audit"])
    async def audit_search(
        ctx: Ctx,
        action: Annotated[AuditAction | None, Query()] = None,
        run_id: Annotated[str | None, Query()] = None,
        canonical_id: Annotated[str | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=5000)] = 500,
        real_changes_only: Annotated[bool, Query()] = False,
    ) -> dict[str, Any]:
        ctx.require(Permission.READ_AUDIT)
        if real_changes_only:
            records = space.audit.real_changes(tenant_id=ctx.tenant_id)
        else:
            records = space.audit.search(
                action=action, run_id=run_id, canonical_id=canonical_id, tenant_id=ctx.tenant_id
            )
        return {
            "total": len(records),
            "head_hash": space.audit.head_hash,
            "chain_length": len(space.audit),
            "records": [r.model_dump(mode="json") for r in records[-limit:]],
        }

    @app.get("/api/audit/verify", tags=["audit"])
    async def audit_verify(ctx: Ctx) -> dict[str, Any]:
        """Recompute the whole hash chain. A tampered log raises a 422."""
        ctx.require(Permission.READ_AUDIT)
        space.audit.verify()
        return {
            "verified": True,
            "chain_length": len(space.audit),
            "head_hash": space.audit.head_hash,
        }

    @app.get("/api/audit/evidence/{run_id}", tags=["audit"])
    async def evidence(run_id: str, ctx: Ctx) -> dict[str, Any]:
        ctx.require(Permission.READ_AUDIT)
        return evidence_pack(space.audit, run_id=run_id)

    # -- 9. connectors ---------------------------------------------------- #

    @app.get("/api/connectors", tags=["connectors"])
    async def connectors(ctx: Ctx) -> list[dict[str, Any]]:
        ctx.require(Permission.READ_ESTATE)
        return [
            {
                "manifest": manifest.model_dump(mode="json"),
                "readiness": readiness.model_dump(mode="json"),
                "may_write_to_production": readiness.may_write_to_production,
                "extractable_kinds": sorted(manifest.extractable_kinds()),
                "appliable_kinds": sorted(manifest.appliable_kinds()),
                "unmappable_kinds": sorted(manifest.unmappable_kinds()),
                "unverified_api_surfaces": [
                    s.model_dump(mode="json") for s in manifest.unverified_api_surfaces()
                ],
            }
            for manifest, readiness in catalogue.entries()
        ]

    @app.get("/api/connectors/{connector_id}", tags=["connectors"])
    async def connector_detail(connector_id: str, ctx: Ctx) -> dict[str, Any]:
        ctx.require(Permission.READ_ESTATE)
        try:
            manifest = catalogue.manifest(connector_id)
        except KeyError:
            raise KeyError(
                f"No connector {connector_id!r}. Known: {catalogue.connector_ids}"
            ) from None
        readiness = catalogue.readiness(connector_id)
        return {
            "manifest": manifest.model_dump(mode="json"),
            "readiness": readiness.model_dump(mode="json"),
        }

    # -- the built UI ----------------------------------------------------- #

    directory = static_dir or _default_static_dir()
    if directory is not None and directory.is_dir():
        app.mount("/", SpaFiles(directory=directory, html=True), name="ui")

    return app


class SpaFiles(StaticFiles):
    """Serve the built SPA, falling back to ``index.html`` for client routes.

    Without this a browser refresh on ``/waves`` would 404: the path is a client
    route, not a file. API paths are mounted first and are unaffected.

    Note that ``StaticFiles`` *raises* for a missing file rather than returning a
    404 response, so catching the exception is the only thing that works here.
    """

    async def get_response(self, path: str, scope: Any) -> Response:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404:
                raise
            # StaticFiles hands this back joined with the OS separator, so on
            # Windows the value is ``api\\thing``. Normalise before comparing.
            normalised = path.replace("\\", "/")
            if normalised.split("/", 1)[0] == "api":
                # The mount catches everything the API router did not match, so
                # without this an unknown /api path would answer 200 with the
                # SPA's HTML. A client checking only the status would treat a
                # typo'd endpoint as a successful call.
                return _problem(404, "NotFound", f"No API endpoint at /{normalised}.")
            return await super().get_response("index.html", scope)


def _default_static_dir() -> Path | None:
    candidate = Path(__file__).resolve().parent / "static"
    return candidate if candidate.is_dir() else None


app = create_app()

__all__ = ["app", "create_app"]
