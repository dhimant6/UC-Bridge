"""Guardrails. Each of these failures would be an outage, a lawsuit, or both.

Every test here asserts that the platform *refuses*, and that the refusal cannot
be reached around by a connector author or a caller.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tests.conftest import _no_sleep, production_authorization, read_only_ref

from ucm_bridge.canonical import FidelityLevel
from ucm_bridge.connectors.base import Connector
from ucm_bridge.connectors.capabilities import WriteVerb
from ucm_bridge.connectors.contracts import (
    ApplyAuthorization,
    ApplyPlan,
    Approval,
    ChangeWindow,
    EmergencyConfirmation,
    ExecutionMode,
    ExtractRequest,
    WriteOperation,
)
from ucm_bridge.connectors.errors import (
    ApprovalRequired,
    ChangeWindowClosed,
    ContractViolation,
    DryRunRequired,
    EmergencyConfirmationRequired,
    PlanDigestMismatch,
    SourceWriteAttempted,
    UnmappableEntityWrite,
    UnsupportedEntityKind,
)
from ucm_bridge.connectors.reference import MemoryPBXConnector
from ucm_bridge.pipeline.planner import build_apply_plan

KEY_FOR = MemoryPBXConnector.natural_key_for


async def _snapshot_and_plan(source_connector, extract_request):
    snapshot = await source_connector.extract_snapshot(extract_request)
    plan = build_apply_plan(
        snapshot.entities,
        plan_id="plan-guard",
        tenant_id="contoso",
        estate_id="contoso-target",
        key_for=KEY_FOR,
    ).plan
    return snapshot, plan


# --------------------------------------------------------------------------- #
# Zero writes to source
# --------------------------------------------------------------------------- #


async def test_production_write_through_a_read_only_credential_is_refused(
    source_connector, source_estate, extract_request, target_estate
) -> None:
    _, plan = await _snapshot_and_plan(source_connector, extract_request)

    # A connector holding a READ_ONLY credential, pointed at a writable estate.
    read_only_connector = MemoryPBXConnector(
        target_estate,
        tenant_id="contoso",
        credential_ref=read_only_ref(),
        sleep=_no_sleep,
    )
    receipt = await read_only_connector.dry_run(plan)

    with pytest.raises(SourceWriteAttempted, match="READ_ONLY"):
        await read_only_connector.apply(plan, production_authorization(plan, receipt))

    assert target_estate.write_count == 0
    assert source_estate.write_count == 0


async def test_the_source_estate_itself_refuses_writes(source_estate) -> None:
    """Defence in depth: the platform-side read-only flag, standing in for a read-only account."""
    from ucm_bridge.connectors.reference import MemoryPBXFault

    source_estate.read_only = True
    with pytest.raises(MemoryPBXFault, match="read-only"):
        source_estate.upsert("users", "intruder", {"username": "intruder"})


# --------------------------------------------------------------------------- #
# Dry run is mandatory
# --------------------------------------------------------------------------- #


def test_production_authorization_without_a_dry_run_is_rejected() -> None:
    with pytest.raises(DryRunRequired):
        ApplyAuthorization(
            mode=ExecutionMode.PRODUCTION,
            requested_by="operator",
            correlation_id="c1",
            approvals=[Approval(approver="a"), Approval(approver="b")],
            change_window=_open_window(),
        )


async def test_a_dry_run_for_a_different_plan_does_not_authorise_this_one(
    source_connector, target_connector, extract_request
) -> None:
    _, plan = await _snapshot_and_plan(source_connector, extract_request)
    receipt = await target_connector.dry_run(plan)

    # The plan is edited after approval: one operation dropped.
    edited = plan.model_copy(update={"operations": plan.operations[:-1]}).seal()

    with pytest.raises(PlanDigestMismatch, match="changed after it was previewed"):
        await target_connector.apply(edited, production_authorization(edited, receipt))


async def test_an_unchanged_replan_keeps_its_approval(
    source_connector, target_connector, extract_request
) -> None:
    """The digest covers operations only, so regenerating an identical plan is still approved."""
    snapshot, plan = await _snapshot_and_plan(source_connector, extract_request)
    receipt = await target_connector.dry_run(plan)

    replanned = build_apply_plan(
        snapshot.entities,
        plan_id="plan-regenerated",
        tenant_id="contoso",
        estate_id="contoso-target",
        key_for=KEY_FOR,
    ).plan
    assert replanned.plan_digest == plan.plan_digest

    report = await target_connector.apply(replanned, production_authorization(replanned, receipt))
    assert report.failures() == []


# --------------------------------------------------------------------------- #
# Two-person approval and change windows
# --------------------------------------------------------------------------- #


def test_one_approver_is_not_enough(dry_receipt_stub) -> None:
    with pytest.raises(ApprovalRequired, match="two distinct approvers"):
        ApplyAuthorization(
            mode=ExecutionMode.PRODUCTION,
            requested_by="operator",
            correlation_id="c1",
            dry_run_receipt=dry_receipt_stub,
            approvals=[Approval(approver="a"), Approval(approver="a")],
            change_window=_open_window(),
        )


def test_writes_outside_the_change_window_are_refused(dry_receipt_stub) -> None:
    past = datetime.now(UTC) - timedelta(days=1)
    with pytest.raises(ChangeWindowClosed, match="outside the approved change window"):
        ApplyAuthorization(
            mode=ExecutionMode.PRODUCTION,
            requested_by="operator",
            correlation_id="c1",
            dry_run_receipt=dry_receipt_stub,
            approvals=[Approval(approver="a"), Approval(approver="b")],
            change_window=ChangeWindow(start=past, end=past + timedelta(hours=1)),
        )


def test_an_override_is_allowed_but_must_be_attributed(dry_receipt_stub) -> None:
    past = datetime.now(UTC) - timedelta(days=1)
    window = ChangeWindow(start=past, end=past + timedelta(hours=1))

    with pytest.raises(ChangeWindowClosed):
        ApplyAuthorization(
            mode=ExecutionMode.PRODUCTION,
            requested_by="operator",
            correlation_id="c1",
            dry_run_receipt=dry_receipt_stub,
            approvals=[Approval(approver="a"), Approval(approver="b")],
            change_window=window,
            window_override_reason="P1 incident recovery",  # reason but no approver
        )

    authorised = ApplyAuthorization(
        mode=ExecutionMode.PRODUCTION,
        requested_by="operator",
        correlation_id="c1",
        dry_run_receipt=dry_receipt_stub,
        approvals=[Approval(approver="a"), Approval(approver="b")],
        change_window=window,
        window_override_reason="P1 incident recovery",
        window_override_by="duty.manager@contoso.example",
    )
    assert authorised.window_override_by == "duty.manager@contoso.example"


# --------------------------------------------------------------------------- #
# Emergency calling
# --------------------------------------------------------------------------- #


async def test_emergency_configuration_is_never_migrated_silently(
    source_connector, target_connector, target_estate, extract_request
) -> None:
    _, plan = await _snapshot_and_plan(source_connector, extract_request)
    receipt = await target_connector.dry_run(plan)

    assert plan.emergency_sites() == {"LON-BR", "MUC-HQ"}

    # Confirm only one of the two affected sites.
    partial = production_authorization(plan, receipt, sites=["MUC-HQ"])
    with pytest.raises(EmergencyConfirmationRequired, match=r"\['LON-BR'\]"):
        await target_connector.apply(plan, partial)

    assert target_estate.write_count == 0


def test_an_emergency_confirmation_must_actually_confirm_something() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="confirms nothing"):
        EmergencyConfirmation(site_code="MUC-HQ", confirmed_by="someone")


# --------------------------------------------------------------------------- #
# Capability and fidelity enforcement
# --------------------------------------------------------------------------- #


async def test_a_connector_cannot_be_asked_to_write_kinds_it_never_declared(
    target_connector,
) -> None:
    plan = ApplyPlan(
        plan_id="p",
        tenant_id="contoso",
        estate_id="e",
        operations=[
            WriteOperation(
                op_id="op1",
                verb=WriteVerb.CREATE,
                entity_kind="AutoAttendant",
                canonical_id="x",
                idempotency_key="k",
                payload={"natural_key": "aa"},
            )
        ],
    ).seal()

    with pytest.raises(UnsupportedEntityKind, match="AutoAttendant"):
        await target_connector.dry_run(plan)


async def test_a_connector_cannot_be_asked_to_extract_kinds_it_never_declared(
    source_connector,
) -> None:
    with pytest.raises(UnsupportedEntityKind, match="CallQueue"):
        async for _ in source_connector.extract(
            ExtractRequest(
                run_id="r",
                tenant_id="contoso",
                estate_id="e",
                entity_kinds=["User", "CallQueue"],
            )
        ):
            pass


async def test_unmappable_entities_are_never_written(
    source_connector, target_connector, extract_request
) -> None:
    _, plan = await _snapshot_and_plan(source_connector, extract_request)
    poisoned = plan.model_copy(
        update={
            "operations": [
                plan.operations[0].model_copy(update={"fidelity": FidelityLevel.UNMAPPABLE}),
                *plan.operations[1:],
            ]
        }
    ).seal()

    with pytest.raises(UnmappableEntityWrite, match="manual work, not writes"):
        await target_connector.dry_run(poisoned)


async def test_the_planner_excludes_unmappable_entities_by_default(
    source_connector, extract_request
) -> None:
    from ucm_bridge.canonical.identity import User

    snapshot = await source_connector.extract_snapshot(extract_request)
    victim = next(e for e in snapshot.entities if isinstance(e, User))
    victim.fidelity = victim.fidelity.model_copy(
        update={
            "level": FidelityLevel.UNMAPPABLE,
            "manual_effort_minutes": 20,
            "degraded_attributes": [],
        }
    )

    result = build_apply_plan(
        snapshot.entities,
        plan_id="p",
        tenant_id="contoso",
        estate_id="e",
        key_for=KEY_FOR,
    )
    assert victim.canonical_id in result.skipped_unmappable
    assert all(op.canonical_id != victim.canonical_id for op in result.plan.operations)
    # References to it are reported rather than written as dangling pointers.
    assert any(r.referenced_id == victim.canonical_id for r in result.unresolved_references)


# --------------------------------------------------------------------------- #
# The contract itself
# --------------------------------------------------------------------------- #


def test_a_connector_cannot_override_the_guarded_methods() -> None:
    with pytest.raises(ContractViolation, match="which is final"):

        class Sneaky(MemoryPBXConnector):  # type: ignore[misc]
            async def apply(self, plan, authorization, **kwargs):  # type: ignore[override]
                return None


def test_a_connector_must_declare_its_identity() -> None:
    from ucm_bridge.canonical.base import Platform

    with pytest.raises(ContractViolation, match="connector_id"):

        class Anonymous(Connector):
            platform = Platform.GENERIC_SIP

            def capabilities(self):  # type: ignore[override]
                raise NotImplementedError

            async def test_connection(self):  # type: ignore[override]
                raise NotImplementedError

            def _extract_batches(self, request):  # type: ignore[override]
                raise NotImplementedError

            async def _preview_operation(self, operation):  # type: ignore[override]
                raise NotImplementedError

            async def _execute_operation(self, operation):  # type: ignore[override]
                raise NotImplementedError


def test_a_manifest_declaring_writes_must_support_dry_run() -> None:
    from pydantic import ValidationError

    from ucm_bridge.canonical.base import Platform
    from ucm_bridge.connectors.capabilities import CapabilityManifest, EntityCapability

    with pytest.raises(ValidationError, match="Dry run is mandatory"):
        CapabilityManifest(
            connector_id="reckless",
            connector_version="0.1.0",
            platform=Platform.GENERIC_SIP,
            display_name="Reckless",
            supports_dry_run=False,
            entities=[
                EntityCapability(
                    entity_kind="User",
                    can_apply=True,
                    supported_verbs=[WriteVerb.CREATE],
                )
            ],
        )


# --------------------------------------------------------------------------- #
# Secrets
# --------------------------------------------------------------------------- #


def test_secrets_are_redacted_everywhere_they_could_leak() -> None:
    from ucm_bridge.connectors.credentials import CredentialKind, CredentialScope, SecretBundle

    bundle = SecretBundle(
        kind=CredentialKind.USERNAME_PASSWORD,
        scope=CredentialScope.READ_ONLY,
        values={"username": "svc_axl_ro", "password": "hunter2"},
    )

    assert "hunter2" not in repr(bundle)
    assert "hunter2" not in str(bundle)
    assert "hunter2" not in bundle.model_dump_json()
    assert bundle.require("password") == "hunter2"


async def test_the_dev_file_credential_provider_refuses_outside_dev(tmp_path) -> None:
    from ucm_bridge.connectors.credentials import (
        CredentialRef,
        LocalFileCredentialProvider,
    )
    from ucm_bridge.connectors.errors import CredentialError

    path = tmp_path / "creds.json"
    path.write_text('{"cucm": {"username": "u", "password": "p"}}', encoding="utf-8")

    provider = LocalFileCredentialProvider(path, environment="production")
    with pytest.raises(CredentialError, match="development-only"):
        await provider.resolve(CredentialRef(provider="file", path="cucm"))

    dev = LocalFileCredentialProvider(path, environment="dev")
    bundle = await dev.resolve(CredentialRef(provider="file", path="cucm"))
    assert bundle.require("username") == "u"


async def test_credentials_cannot_cross_tenant_boundaries() -> None:
    from ucm_bridge.connectors.credentials import (
        CredentialBroker,
        CredentialRef,
        EnvCredentialProvider,
    )
    from ucm_bridge.connectors.errors import CredentialError

    broker = CredentialBroker([EnvCredentialProvider({})])
    ref = CredentialRef(provider="env", path="cucm", tenant_id="contoso")

    with pytest.raises(CredentialError, match="cannot be resolved on behalf of tenant"):
        await broker.resolve(ref, tenant_id="fabrikam")


async def test_the_vault_provider_fails_loudly_rather_than_guessing() -> None:
    from ucm_bridge.connectors.credentials import CredentialRef, VaultCredentialProvider

    provider = VaultCredentialProvider("https://vault.internal")
    with pytest.raises(NotImplementedError, match="documented Vault version"):
        await provider.resolve(CredentialRef(provider="vault", path="kv/cucm"))


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _open_window() -> ChangeWindow:
    now = datetime.now(UTC)
    return ChangeWindow(start=now - timedelta(hours=1), end=now + timedelta(hours=1))


@pytest.fixture
def dry_receipt_stub():
    from ucm_bridge.connectors.contracts import DryRunReceipt

    return DryRunReceipt(
        receipt_id="dr-1",
        plan_id="p",
        plan_digest="sha256:deadbeef",
        connector_id="reference-memorypbx",
    )
