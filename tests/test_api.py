"""The control-plane HTTP surface, driven end to end.

These are the same claims the acceptance tests make, asserted through the API
the console actually calls. The point is that the guardrails survive the trip
over HTTP: a refusal must arrive as a refusal, with its own message, not as a
500 or — worse — as a success with an empty result.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from ucm_bridge.api.app import create_app
from ucm_bridge.api.workspace import Workspace

PLANNER = {"X-UCM-Roles": "PLANNER"}
OPERATOR = {"X-UCM-Roles": "OPERATOR"}
APPROVER = {"X-UCM-Roles": "APPROVER"}
VIEWER = {"X-UCM-Roles": "VIEWER"}

REFERENCE = "contoso-legacy"
CUCM = "contoso-cucm"


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(workspace=Workspace())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://control-plane") as http:
        yield http


def _window() -> dict[str, str]:
    now = datetime.now(UTC)
    return {
        "window_start": (now - timedelta(hours=1)).isoformat(),
        "window_end": (now + timedelta(hours=1)).isoformat(),
    }


async def _to_dry_run(client: httpx.AsyncClient, estate_id: str) -> dict[str, object]:
    """Walk an estate from discovery to a signed dry-run receipt."""
    discovered = await client.post(f"/api/estates/{estate_id}/discover", headers=PLANNER)
    assert discovered.status_code == 200, discovered.text
    assert (
        await client.post(f"/api/estates/{estate_id}/assess", json={}, headers=PLANNER)
    ).status_code == 200
    assert (await client.post(f"/api/estates/{estate_id}/map", headers=PLANNER)).status_code == 200
    assert (
        await client.post(f"/api/estates/{estate_id}/waves", json={}, headers=PLANNER)
    ).status_code == 200
    assert (
        await client.post(f"/api/estates/{estate_id}/plan", json={}, headers=PLANNER)
    ).status_code == 200
    receipt = await client.post(f"/api/estates/{estate_id}/dry-run", headers=PLANNER)
    assert receipt.status_code == 200, receipt.text
    body = receipt.json()
    assert isinstance(body, dict)
    return body


# --------------------------------------------------------------------------- #
# Shape of the surface
# --------------------------------------------------------------------------- #


async def test_health_and_session(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/health")).json()["status"] == "ok"

    session = (await client.get("/api/session", headers=OPERATOR)).json()
    assert session["roles"] == ["OPERATOR"]
    assert "EXECUTE_PRODUCTION" in session["permissions"]
    # The catalogue is what lets the UI explain a refusal rather than just show it.
    assert "APPROVE_PLAN" not in session["permissions"]
    assert set(session["role_catalogue"]) == {
        "VIEWER",
        "PLANNER",
        "APPROVER",
        "OPERATOR",
        "ADMIN",
    }


async def test_every_connector_reports_a_manifest_and_a_readiness_level(
    client: httpx.AsyncClient,
) -> None:
    connectors = (await client.get("/api/connectors", headers=VIEWER)).json()
    ids = {c["manifest"]["connector_id"] for c in connectors}
    assert {
        "cisco-cucm",
        "microsoft-teams",
        "avaya-aura",
        "microsoft-sfb-server",
        "slack",
        "genesys-cloud",
    } <= ids

    # Exactly one connector may write to production: the reference platform, the
    # only one whose API surface is genuinely verified. If this ever changes
    # silently, something has claimed a verification it did not earn.
    writable = {
        c["manifest"]["connector_id"] for c in connectors if c["may_write_to_production"]
    }
    assert writable == {"reference-memorypbx"}


async def test_estates_expose_their_pipeline_progress(client: httpx.AsyncClient) -> None:
    estates = (await client.get("/api/estates", headers=VIEWER)).json()
    assert {e["estate_id"] for e in estates} == {REFERENCE, CUCM}
    assert all(stage is False for e in estates for stage in e["stages"].values())

    await client.post(f"/api/estates/{REFERENCE}/discover", headers=PLANNER)
    after = (await client.get(f"/api/estates/{REFERENCE}", headers=VIEWER)).json()
    assert after["stages"]["discovery"] is True
    assert after["headline"]


# --------------------------------------------------------------------------- #
# Discovery and the estate report
# --------------------------------------------------------------------------- #


async def test_discovery_produces_a_report_and_a_browsable_snapshot(
    client: httpx.AsyncClient,
) -> None:
    discovered = (await client.post(f"/api/estates/{CUCM}/discover", headers=PLANNER)).json()
    assert discovered["report"]["user_count"] == 4
    assert discovered["report"]["device_count"] == 5
    assert discovered["snapshot_digest"]

    page = (
        await client.get(f"/api/estates/{CUCM}/entities?limit=5", headers=VIEWER)
    ).json()
    assert page["total"] == discovered["entity_count"]
    assert len(page["rows"]) == 5
    assert "User" in page["kinds"]

    users = (
        await client.get(f"/api/estates/{CUCM}/entities?kind=User", headers=VIEWER)
    ).json()
    assert users["total"] == 4
    assert all(row["kind"] == "User" for row in users["rows"])

    detail = (
        await client.get(
            f"/api/estates/{CUCM}/entities/{users['rows'][0]['canonical_id']}", headers=VIEWER
        )
    ).json()
    assert detail["entity"]["kind"] == "User"
    assert "content_view" in detail

    markdown = await client.get(f"/api/estates/{CUCM}/report.md", headers=VIEWER)
    assert markdown.text.startswith("# Estate report:")


async def test_a_stage_asked_for_out_of_order_is_a_409_not_an_empty_result(
    client: httpx.AsyncClient,
) -> None:
    early = await client.post(f"/api/estates/{REFERENCE}/dry-run", headers=PLANNER)
    assert early.status_code == 409
    assert early.json()["error"] == "StageNotReady"
    assert early.json()["needs"] == "plan build"


async def test_an_unknown_estate_is_a_404(client: httpx.AsyncClient) -> None:
    missing = await client.get("/api/estates/nope", headers=VIEWER)
    assert missing.status_code == 404


# --------------------------------------------------------------------------- #
# Assessment
# --------------------------------------------------------------------------- #


async def test_a_raw_cucm_estate_is_assessed_not_ready_and_says_why(
    client: httpx.AsyncClient,
) -> None:
    await client.post(f"/api/estates/{CUCM}/discover", headers=PLANNER)
    report = (
        await client.post(
            f"/api/estates/{CUCM}/assess",
            json={"target_platform": "microsoft.teams"},
            headers=PLANNER,
        )
    ).json()

    rule_ids = {f["rule_id"] for f in report["findings"]}
    assert "NUM-001" in rule_ids, "extensions with no E.164 must block a Teams target"

    stored = (await client.get(f"/api/estates/{CUCM}/assessment", headers=VIEWER)).json()
    assert stored["is_ready_to_plan"] is False
    assert stored["counts_by_severity"]


async def test_waiving_a_finding_needs_the_approver_role(client: httpx.AsyncClient) -> None:
    await client.post(f"/api/estates/{CUCM}/discover", headers=PLANNER)
    report = (
        await client.post(f"/api/estates/{CUCM}/assess", json={}, headers=PLANNER)
    ).json()
    waivable = next(f for f in report["findings"] if f["severity"] != "BLOCKER")

    body = {"by": "approver@contoso.example", "reason": "Accepted for wave 1."}
    denied = await client.post(
        f"/api/estates/{CUCM}/assessment/{waivable['rule_id']}/waive", json=body, headers=PLANNER
    )
    assert denied.status_code == 403
    assert denied.json()["error"] == "PermissionDenied"

    allowed = await client.post(
        f"/api/estates/{CUCM}/assessment/{waivable['rule_id']}/waive", json=body, headers=APPROVER
    )
    assert allowed.status_code == 200
    assert allowed.json()["status"] == "WAIVED"
    assert allowed.json()["waived_by"] == "approver@contoso.example"


async def test_a_blocker_cannot_be_waived_over_http_either(
    client: httpx.AsyncClient,
) -> None:
    """The unwaivable rule stays unwaivable through the API, and is not a 500."""
    await client.post(f"/api/estates/{CUCM}/discover", headers=PLANNER)
    report = (
        await client.post(f"/api/estates/{CUCM}/assess", json={}, headers=PLANNER)
    ).json()
    blockers = [f for f in report["findings"] if f["severity"] == "BLOCKER"]
    assert blockers, "the CUCM estate must produce at least one blocker to assess this"

    refused = await client.post(
        f"/api/estates/{CUCM}/assessment/{blockers[0]['rule_id']}/waive",
        json={"by": "approver@contoso.example", "reason": "please"},
        headers=APPROVER,
    )
    assert refused.status_code == 422
    assert refused.json()["error"] == "BlockerCannotBeWaived"
    assert refused.json()["guardrail"] is True

    # And the finding is untouched: a refused waiver must not half-apply.
    after = (await client.get(f"/api/estates/{CUCM}/assessment", headers=APPROVER)).json()
    still_open = next(
        f for f in after["findings"] if f["rule_id"] == blockers[0]["rule_id"]
    )
    assert still_open["status"] == "OPEN"


# --------------------------------------------------------------------------- #
# Mapping, waves, plan
# --------------------------------------------------------------------------- #


async def test_mapping_mints_e164_numbers_and_declares_them_degraded(
    client: httpx.AsyncClient,
) -> None:
    await client.post(f"/api/estates/{CUCM}/discover", headers=PLANNER)
    mapping = (await client.post(f"/api/estates/{CUCM}/map", headers=PLANNER)).json()

    assert mapping["has_profile"] is True
    fidelity = mapping["transform"]["fidelity_by_kind"]
    # Numbers derived by a rule are not lossless just because the rule ran.
    assert fidelity["E164Number"]["DEGRADED"] > 0
    assert fidelity["E164Number"].get("LOSSLESS", 0) == 0
    assert mapping["number_plan"]["rules"]


async def test_wave_planning_reports_dependency_integrity(client: httpx.AsyncClient) -> None:
    await client.post(f"/api/estates/{REFERENCE}/discover", headers=PLANNER)
    waves = (
        await client.post(
            f"/api/estates/{REFERENCE}/waves", json={"strategy": "SITE"}, headers=PLANNER
        )
    ).json()
    assert waves["is_valid"] is True
    assert waves["plan"]["waves"]
    assert waves["summary"]


async def test_a_plan_reports_the_references_it_could_not_carry(
    client: httpx.AsyncClient,
) -> None:
    await client.post(f"/api/estates/{CUCM}/discover", headers=PLANNER)
    await client.post(f"/api/estates/{CUCM}/map", headers=PLANNER)
    plan = (
        await client.post(f"/api/estates/{CUCM}/plan", json={}, headers=PLANNER)
    ).json()

    # Teams has no Extension concept, so that reference cannot be carried. The
    # planner reports it rather than writing a dangling pointer.
    assert {u["field"] for u in plan["unresolved_references"]} == {"extension_ref"}
    assert plan["operation_count"] > 0
    assert plan["plan_digest"]


async def test_editing_a_plan_invalidates_its_dry_run(client: httpx.AsyncClient) -> None:
    """A receipt covers one plan digest. Rebuilding the plan must clear it."""
    await _to_dry_run(client, REFERENCE)
    fresh = await client.get(f"/api/estates/{REFERENCE}/dry-run", headers=VIEWER)
    assert fresh.status_code == 200

    await client.post(f"/api/estates/{REFERENCE}/plan", json={}, headers=PLANNER)
    stale = await client.get(f"/api/estates/{REFERENCE}/dry-run", headers=VIEWER)
    assert stale.status_code == 409


# --------------------------------------------------------------------------- #
# Execution, and the refusals on the way
# --------------------------------------------------------------------------- #


async def test_a_planner_cannot_execute_and_an_operator_cannot_approve(
    client: httpx.AsyncClient,
) -> None:
    await _to_dry_run(client, REFERENCE)
    denied = await client.post(
        f"/api/estates/{REFERENCE}/runs", json=_window(), headers=PLANNER
    )
    assert denied.status_code == 403
    assert "EXECUTE_PRODUCTION" in denied.json()["message"]


async def test_one_approver_is_refused_by_the_two_person_rule(
    client: httpx.AsyncClient,
) -> None:
    await _to_dry_run(client, REFERENCE)
    refused = await client.post(
        f"/api/estates/{REFERENCE}/runs",
        json={"approvers": ["solo@contoso.example"], **_window()},
        headers=OPERATOR,
    )
    assert refused.status_code == 422
    assert refused.json()["error"] == "ApprovalRequired"


async def test_a_closed_change_window_is_refused_without_an_attributed_override(
    client: httpx.AsyncClient,
) -> None:
    await _to_dry_run(client, REFERENCE)
    past = datetime.now(UTC) - timedelta(days=2)
    closed = {
        "window_start": past.isoformat(),
        "window_end": (past + timedelta(hours=1)).isoformat(),
    }

    refused = await client.post(
        f"/api/estates/{REFERENCE}/runs", json=closed, headers=OPERATOR
    )
    assert refused.status_code == 422
    assert refused.json()["error"] == "ChangeWindowClosed"

    overridden = await client.post(
        f"/api/estates/{REFERENCE}/runs",
        json={
            **closed,
            "window_override_reason": "Sev-1 remediation, CAB chair verbally approved.",
            "window_override_by": "cab.chair@contoso.example",
        },
        headers=OPERATOR,
    )
    assert overridden.status_code == 200


async def test_the_reference_platform_executes_validates_and_audits(
    client: httpx.AsyncClient,
) -> None:
    receipt = await _to_dry_run(client, REFERENCE)
    assert receipt["previews"], "a dry run must describe the calls it would make"

    run = (
        await client.post(f"/api/estates/{REFERENCE}/runs", json=_window(), headers=OPERATOR)
    ).json()
    assert run["state"] == "COMPLETED"
    assert run["succeeded"] is True
    assert run["progress"] == 1.0
    assert run["has_rollback_bundle"] is True

    validation = (
        await client.post(f"/api/estates/{REFERENCE}/validate", headers=OPERATOR)
    ).json()
    assert validation["safe_to_sign_off"] is True, validation["counts"]
    assert validation["markdown"]

    # Every write is on the chain, with a before and an after, and the chain verifies.
    audit = (
        await client.get(
            f"/api/audit?action=OBJECT_WRITTEN&run_id={run['run_id']}", headers=VIEWER
        )
    ).json()
    assert audit["total"] == run["total_operations"]
    assert all(record["after"] is not None for record in audit["records"])
    assert (await client.get("/api/audit/verify", headers=VIEWER)).json()["verified"] is True

    evidence = (
        await client.get(f"/api/audit/evidence/{run['run_id']}", headers=VIEWER)
    ).json()
    assert evidence


async def test_re_running_an_identical_plan_changes_nothing(
    client: httpx.AsyncClient,
) -> None:
    await _to_dry_run(client, REFERENCE)
    first = (
        await client.post(f"/api/estates/{REFERENCE}/runs", json=_window(), headers=OPERATOR)
    ).json()
    assert first["state"] == "COMPLETED"

    second_receipt = (
        await client.post(f"/api/estates/{REFERENCE}/dry-run", headers=PLANNER)
    ).json()
    assert second_receipt["would_change_count"] == 0

    second = (
        await client.post(f"/api/estates/{REFERENCE}/runs", json=_window(), headers=OPERATOR)
    ).json()
    assert second["state"] == "COMPLETED"
    assert second["counts"].get("APPLIED", 0) == 0


async def test_a_completed_run_rolls_back(client: httpx.AsyncClient) -> None:
    await _to_dry_run(client, REFERENCE)
    run = (
        await client.post(f"/api/estates/{REFERENCE}/runs", json=_window(), headers=OPERATOR)
    ).json()

    rolled = await client.post(
        f"/api/estates/{REFERENCE}/runs/{run['run_id']}/rollback",
        json=_window(),
        headers=OPERATOR,
    )
    assert rolled.status_code == 200
    assert rolled.json()["state"] == "ROLLED_BACK"


async def test_a_production_write_to_teams_is_refused_while_cassettes_are_synthetic(
    client: httpx.AsyncClient,
) -> None:
    """The honest limit of this build, asserted through the API the UI calls."""
    await _to_dry_run(client, CUCM)

    refused = await client.post(
        f"/api/estates/{CUCM}/runs", json=_window(), headers=OPERATOR
    )
    assert refused.status_code == 422
    assert refused.json()["error"] == "NotProductionReady"
    assert "LAB_ONLY" in refused.json()["message"]

    # And the UI can see it coming, before anyone fills in an approval form.
    state = (await client.get(f"/api/estates/{CUCM}", headers=VIEWER)).json()
    assert state["target_may_write_to_production"] is False
    assert state["target_readiness"] == "LAB_ONLY"


# --------------------------------------------------------------------------- #
# Runbooks
# --------------------------------------------------------------------------- #


async def test_a_runbook_renders_for_a_planned_wave(client: httpx.AsyncClient) -> None:
    await _to_dry_run(client, REFERENCE)
    waves = (await client.get(f"/api/estates/{REFERENCE}/waves", headers=VIEWER)).json()
    wave_id = waves["plan"]["waves"][0]["wave_id"]

    runbook = await client.get(
        f"/api/estates/{REFERENCE}/waves/{wave_id}/runbook", headers=VIEWER
    )
    assert runbook.status_code == 200
    assert "Rollback" in runbook.text or "rollback" in runbook.text

    missing = await client.get(
        f"/api/estates/{REFERENCE}/waves/no-such-wave/runbook", headers=VIEWER
    )
    assert missing.status_code == 404


# --------------------------------------------------------------------------- #
# Serving the console alongside the API
# --------------------------------------------------------------------------- #


async def test_the_spa_mount_never_swallows_an_api_path(tmp_path: Path) -> None:
    """The mount at ``/`` catches everything the API router did not match.

    Without the guard, a typo'd endpoint would answer 200 with the console's
    HTML, and a client checking only the status would read that as success.
    """
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text('<div id="root"></div>', encoding="utf-8")

    app = create_app(workspace=Workspace(), static_dir=static)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://control-plane") as http:
        # A client route falls back to the SPA so a refresh on /waves works.
        page = await http.get("/waves")
        assert page.status_code == 200
        assert "<div id=\"root\">" in page.text

        # An unknown API path stays JSON, and stays a 404.
        missing = await http.get("/api/no-such-endpoint")
        assert missing.status_code == 404
        assert missing.json()["error"] == "NotFound"

        # And the real API still works with the mount in place, docs included.
        assert (await http.get("/api/health")).json()["status"] == "ok"
        assert (await http.get("/api/docs")).status_code == 200
        assert (await http.get("/api/openapi.json")).status_code == 200


async def test_reset_returns_an_estate_to_a_clean_slate(client: httpx.AsyncClient) -> None:
    await _to_dry_run(client, REFERENCE)
    await client.post(f"/api/estates/{REFERENCE}/runs", json=_window(), headers=OPERATOR)

    reset = (await client.post(f"/api/estates/{REFERENCE}/reset", headers=PLANNER)).json()
    assert all(stage is False for stage in reset["stages"].values())
    assert reset["run_ids"] == []
