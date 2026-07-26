"""Phase 7: wave planning, runbooks, multi-tenancy, and the collector agent."""

from __future__ import annotations

import pytest

from ucm_bridge.canonical.base import CanonicalEntity, Platform
from ucm_bridge.canonical.callhandling import Delegation, LineGroup
from ucm_bridge.canonical.endpoints import Line, SharedLineAppearance
from ucm_bridge.canonical.identity import User
from ucm_bridge.canonical.snapshot import EstateSnapshot
from ucm_bridge.collector import (
    CollectorAgent,
    InMemoryControlPlane,
    JobKind,
    JobState,
    lease,
)
from ucm_bridge.pipeline.planner import build_apply_plan
from ucm_bridge.runbook import build_runbook, render_runbook_markdown
from ucm_bridge.tenancy import (
    CrossTenantAccess,
    Permission,
    PermissionDenied,
    Role,
    TenantContext,
    scoped,
    two_person_rule_satisfied,
)
from ucm_bridge.waves import (
    DependencyKind,
    GroupingStrategy,
    Wave,
    coexistence_requirements,
    find_dependency_clusters,
    merge_clusters,
    move_user,
    plan_waves,
    render_wave_plan_markdown,
    validate_waves,
)


def uid(kind: str, key: str) -> str:
    return CanonicalEntity.mint_canonical_id(
        Platform.CISCO_CUCM, kind, key, instance_id="c1"
    )


def _user(upn: str, site: str, department: str = "Finance") -> User:
    return User(
        canonical_id=uid("User", upn),
        user_principal_name=upn,
        site_code=site,
        department=department,
        telephony_enabled=True,
    )


def _line(number: str, owner: str) -> Line:
    return Line(
        canonical_id=uid("Line", number),
        directory_number=number,
        owner_ref=uid("User", owner),
    )


def estate() -> EstateSnapshot:
    """Two sites, with a shared line and a delegation that straddle them."""
    return EstateSnapshot.build(
        snapshot_id="s1",
        tenant_id="contoso",
        estate_id="contoso-cucm",
        entities=[
            _user("anna@x", "MUC-HQ"),
            _user("bruno@x", "MUC-HQ"),
            _user("cerys@x", "LON-BR", "Sales"),
            _user("dieter@x", "LON-BR", "Sales"),
            # Independent of every cluster, so repair cannot collapse the plan
            # into a single wave and there is somewhere to move a user to.
            _user("erik@x", "BER-BR", "Operations"),
            _line("5101", "anna@x"),
            _line("5102", "bruno@x"),
            _line("7301", "cerys@x"),
            SharedLineAppearance(
                canonical_id=uid("SharedLineAppearance", "5199"),
                directory_number="5199",
                user_refs=[uid("User", "anna@x"), uid("User", "cerys@x")],
                appearance_count=2,
            ),
            Delegation(
                canonical_id=uid("Delegation", "bruno@x"),
                principal_ref=uid("User", "bruno@x"),
                delegate_refs=[uid("User", "dieter@x")],
            ),
        ],
    )


# --------------------------------------------------------------------------- #
# Dependency clusters
# --------------------------------------------------------------------------- #


def test_shared_lines_and_delegations_become_clusters() -> None:
    clusters = {c.cluster_id: c for c in find_dependency_clusters(estate())}

    assert clusters["shared-line:5199"].user_keys == {"anna@x", "cerys@x"}
    assert clusters["shared-line:5199"].kind is DependencyKind.SHARED_LINE
    assert clusters["delegation:bruno@x"].user_keys == {"bruno@x", "dieter@x"}


def test_overlapping_clusters_merge_transitively() -> None:
    """A shares a line with B, B is in a group with C: all three must move together."""
    snapshot = EstateSnapshot.build(
        snapshot_id="s",
        tenant_id="t",
        estate_id="e",
        entities=[
            _user("a@x", "S1"),
            _user("b@x", "S1"),
            _user("c@x", "S1"),
            _line("100", "b@x"),
            _line("101", "c@x"),
            SharedLineAppearance(
                canonical_id=uid("SharedLineAppearance", "199"),
                directory_number="199",
                user_refs=[uid("User", "a@x"), uid("User", "b@x")],
            ),
            LineGroup(
                canonical_id=uid("LineGroup", "LG1"),
                name="LG1",
                member_line_refs=[uid("Line", "100"), uid("Line", "101")],
            ),
        ],
    )
    merged = merge_clusters(find_dependency_clusters(snapshot))
    assert len(merged) == 1
    assert merged[0].user_keys == {"a@x", "b@x", "c@x"}


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #


def test_site_grouping_repairs_clusters_that_straddle_sites() -> None:
    """Anna (Munich) and Cerys (London) share a line, so they cannot be split."""
    plan = plan_waves(estate(), strategy=GroupingStrategy.SITE)

    assert plan.is_valid, [v.consequence for v in plan.violations]
    anna_wave = plan.wave_of("anna@x")
    cerys_wave = plan.wave_of("cerys@x")
    assert anna_wave is not None and anna_wave.wave_id == (cerys_wave and cerys_wave.wave_id)


def test_without_repair_a_split_cluster_is_reported_with_its_consequence() -> None:
    plan = plan_waves(estate(), strategy=GroupingStrategy.SITE, repair_violations=False)

    assert not plan.is_valid
    violation = next(v for v in plan.violations if v.cluster_id == "shared-line:5199")
    # Anna and Cerys are at different sites, so site grouping separates them.
    assert len(violation.split_across) == 2
    assert sorted(k for keys in violation.split_across.values() for k in keys) == [
        "anna@x",
        "cerys@x",
    ]
    assert "goes dead without warning" in violation.consequence


def test_moving_a_user_out_of_a_cluster_invalidates_the_plan() -> None:
    """The drag-and-drop operation in the wave planner must re-validate."""
    plan = plan_waves(estate(), strategy=GroupingStrategy.SITE)
    assert plan.is_valid

    target = next(w for w in plan.waves if "anna@x" not in w.user_keys)
    broken = move_user(plan, user_key="anna@x", to_wave_id=target.wave_id)

    assert not broken.is_valid
    assert any(v.kind is DependencyKind.SHARED_LINE for v in broken.violations)


def test_max_wave_size_splits_groups_but_still_keeps_clusters_intact() -> None:
    plan = plan_waves(estate(), strategy=GroupingStrategy.DEPARTMENT, max_wave_size=1)
    assert plan.is_valid, [v.cluster_id for v in plan.violations]
    # The clusters force merging, so waves are larger than the requested cap.
    assert any(wave.size > 1 for wave in plan.waves)


def test_validate_waves_flags_an_unassigned_cluster_member() -> None:
    clusters = merge_clusters(find_dependency_clusters(estate()))
    waves = [Wave(wave_id="w1", name="w1", sequence=1, user_keys=["anna@x"])]
    violations = validate_waves(waves, clusters)

    split = next(v for v in violations if v.cluster_id == "shared-line:5199")
    assert "<unassigned>" in split.split_across


def test_wave_plan_renders_to_markdown() -> None:
    markdown = render_wave_plan_markdown(
        plan_waves(estate(), strategy=GroupingStrategy.SITE, repair_violations=False)
    )
    assert "PLAN REJECTED" in markdown
    assert "shared-line:5199" in markdown


# --------------------------------------------------------------------------- #
# Coexistence
# --------------------------------------------------------------------------- #


def test_coexistence_requirements_shrink_as_waves_complete() -> None:
    snapshot = estate()
    plan = plan_waves(snapshot, strategy=GroupingStrategy.SITE)
    requirements = coexistence_requirements(plan, snapshot)

    assert requirements[0].remaining_user_count > 0
    assert requirements[-1].remaining_user_count == 0
    assert "final wave" in requirements[-1].detail
    assert requirements[0].migrated_user_count < requirements[-1].migrated_user_count


# --------------------------------------------------------------------------- #
# Runbook
# --------------------------------------------------------------------------- #


async def test_runbook_contains_pre_agreed_rollback_triggers(
    source_connector, target_connector, extract_request
) -> None:
    from ucm_bridge.connectors.reference import MemoryPBXConnector

    snapshot = await source_connector.extract_snapshot(extract_request)
    plan = build_apply_plan(
        snapshot.entities,
        plan_id="plan-rb",
        tenant_id="contoso",
        estate_id="contoso-target",
        key_for=MemoryPBXConnector.natural_key_for,
    ).plan
    receipt = await target_connector.dry_run(plan)
    wave = Wave(wave_id="w1", name="MUC-HQ", sequence=1, user_keys=["anna@x"])

    runbook = build_runbook(wave=wave, plan=plan, dry_run=receipt, change_reference="CHG001")

    assert runbook.is_executable
    conditions = " ".join(t.condition for t in runbook.rollback_triggers)
    assert "emergency services" in conditions
    assert any(t.decided_by_role == "Anyone on the bridge" for t in runbook.rollback_triggers)

    # Pre-checks must pin the exact plan digest that was approved.
    assert any(plan.plan_digest in check for check in runbook.pre_checks)
    # Every step carries the exact call it will issue.
    assert all(step.api_call for step in runbook.steps)


async def test_a_runbook_with_open_blockers_says_do_not_run(
    source_connector, target_connector, extract_request
) -> None:
    from ucm_bridge.assessment import RuleContext, assess
    from ucm_bridge.connectors.reference import MemoryPBXConnector

    snapshot = await source_connector.extract_snapshot(extract_request)
    plan = build_apply_plan(
        snapshot.entities,
        plan_id="plan-rb",
        tenant_id="contoso",
        estate_id="contoso-target",
        key_for=MemoryPBXConnector.natural_key_for,
    ).plan
    receipt = await target_connector.dry_run(plan)

    # The reference estate is clean, so introduce the failure the runbook must
    # refuse to run past: an assigned number with no emergency location.
    from ucm_bridge.canonical.numbering import E164Number, NumberAssignmentState

    number = next(
        e
        for e in snapshot.entities
        if isinstance(e, E164Number)
        and e.assignment_state is NumberAssignmentState.ASSIGNED
    )
    number.emergency_location_ref = None

    assessment = assess(RuleContext(snapshot=snapshot, target_platform="microsoft.teams"))
    assert any(f.rule_id == "EMG-001" for f in assessment.blockers)

    runbook = build_runbook(
        wave=Wave(wave_id="w1", name="MUC-HQ", sequence=1, user_keys=["anna@x"]),
        plan=plan,
        dry_run=receipt,
        assessment=assessment,
    )
    markdown = render_runbook_markdown(runbook)

    assert not runbook.is_executable
    assert "DO NOT RUN" in markdown
    assert "STOP" in " ".join(runbook.pre_checks)


# --------------------------------------------------------------------------- #
# Tenancy and RBAC
# --------------------------------------------------------------------------- #


def test_approver_and_operator_are_disjoint() -> None:
    """One person holding both defeats the two-person rule on their own."""
    approver = TenantContext(tenant_id="t", principal="a@x", roles=frozenset({Role.APPROVER}))
    operator = TenantContext(tenant_id="t", principal="o@x", roles=frozenset({Role.OPERATOR}))

    assert approver.has(Permission.APPROVE_PLAN)
    assert not approver.has(Permission.EXECUTE_PRODUCTION)
    assert operator.has(Permission.EXECUTE_PRODUCTION)
    assert not operator.has(Permission.APPROVE_PLAN)


def test_permission_denied_names_the_roles_that_would_grant_it() -> None:
    viewer = TenantContext(tenant_id="t", principal="v@x", roles=frozenset({Role.VIEWER}))
    with pytest.raises(PermissionDenied, match="EXECUTE_PRODUCTION"):
        viewer.require(Permission.EXECUTE_PRODUCTION)


def test_cross_tenant_access_raises_rather_than_returning_nothing() -> None:
    """Silently filtering makes an isolation bug look like an empty result set."""
    context = TenantContext(tenant_id="contoso", principal="p@x", roles=frozenset({Role.VIEWER}))

    with pytest.raises(CrossTenantAccess, match="cannot access"):
        context.require_tenant("fabrikam")

    with pytest.raises(CrossTenantAccess, match="isolation failure"):
        scoped(context, [{"tenant": "fabrikam"}], tenant_of=lambda item: item["tenant"])


def test_a_partner_principal_can_switch_between_permitted_tenants() -> None:
    partner = TenantContext(
        tenant_id="contoso",
        principal="consultant@partner.example",
        roles=frozenset({Role.PLANNER}),
        accessible_tenant_ids=frozenset({"contoso", "fabrikam"}),
    )
    assert partner.for_tenant("fabrikam").tenant_id == "fabrikam"
    with pytest.raises(CrossTenantAccess):
        partner.for_tenant("northwind")


def test_the_requester_cannot_be_one_of_their_own_approvers() -> None:
    assert not two_person_rule_satisfied(requester="a@x", approvers=["a@x", "b@x"])
    assert two_person_rule_satisfied(requester="a@x", approvers=["b@x", "c@x"])


# --------------------------------------------------------------------------- #
# Collector agent
# --------------------------------------------------------------------------- #


async def test_the_collector_runs_a_discovery_job_and_uploads_the_snapshot(
    source_connector,
) -> None:
    control_plane = InMemoryControlPlane(
        [
            lease(
                job_id="job-1",
                tenant_id="contoso",
                estate_id="contoso-legacy",
                connector_id=source_connector.connector_id,
            )
        ]
    )
    agent = CollectorAgent(
        agent_id="collector-muc-01",
        control_plane=control_plane,
        connectors={source_connector.connector_id: source_connector},
    )

    result = await agent.run_once()

    assert result is not None
    assert result.state is JobState.COMPLETED
    assert result.entity_count > 0
    assert control_plane.snapshots["job-1"].snapshot_digest == result.snapshot_digest
    assert control_plane.heartbeats == [("collector-muc-01", "job-1")]


async def test_a_read_only_collector_refuses_an_apply_job(source_connector) -> None:
    control_plane = InMemoryControlPlane(
        [
            lease(
                job_id="job-w",
                tenant_id="contoso",
                estate_id="e",
                connector_id=source_connector.connector_id,
                kind=JobKind.APPLY,
            )
        ]
    )
    agent = CollectorAgent(
        agent_id="a",
        control_plane=control_plane,
        connectors={source_connector.connector_id: source_connector},
    )

    result = await agent.run_once()
    assert result is not None
    assert result.state is JobState.FAILED
    assert "read-only" in (result.error or "")


async def test_an_expired_lease_is_not_run_late(source_connector) -> None:
    """A stale plan run late is worse than a job that goes back on the queue."""
    control_plane = InMemoryControlPlane(
        [
            lease(
                job_id="job-old",
                tenant_id="contoso",
                estate_id="e",
                connector_id=source_connector.connector_id,
                lease_seconds=-10,
            )
        ]
    )
    agent = CollectorAgent(
        agent_id="a",
        control_plane=control_plane,
        connectors={source_connector.connector_id: source_connector},
    )

    result = await agent.run_once()
    assert result is not None
    assert result.state is JobState.LEASE_EXPIRED


async def test_the_collector_only_leases_jobs_it_has_a_connector_for(
    source_connector,
) -> None:
    control_plane = InMemoryControlPlane(
        [lease(job_id="j", tenant_id="t", estate_id="e", connector_id="cisco-cucm")]
    )
    agent = CollectorAgent(
        agent_id="a",
        control_plane=control_plane,
        connectors={source_connector.connector_id: source_connector},
    )
    assert await agent.run_once() is None
    assert len(control_plane.pending) == 1


async def test_a_connectivity_test_job_reports_without_extracting(
    source_connector,
) -> None:
    control_plane = InMemoryControlPlane(
        [
            lease(
                job_id="job-t",
                tenant_id="contoso",
                estate_id="e",
                connector_id=source_connector.connector_id,
                kind=JobKind.CONNECTIVITY_TEST,
            )
        ]
    )
    agent = CollectorAgent(
        agent_id="a",
        control_plane=control_plane,
        connectors={source_connector.connector_id: source_connector},
    )

    result = await agent.run_once()
    assert result is not None
    assert result.state is JobState.COMPLETED
    assert result.entity_count == 0
    assert "job-t" not in control_plane.snapshots
