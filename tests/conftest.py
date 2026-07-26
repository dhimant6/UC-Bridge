"""Shared fixtures for the Phase 0 suite."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ucm_bridge.connectors.contracts import (
    ApplyAuthorization,
    ApplyPlan,
    Approval,
    ChangeWindow,
    DryRunReceipt,
    EmergencyConfirmation,
    ExecutionMode,
    ExtractRequest,
)
from ucm_bridge.connectors.credentials import (
    CredentialKind,
    CredentialRef,
    CredentialScope,
)
from ucm_bridge.connectors.reference import (
    MemoryPBXConnector,
    MemoryPBXEstate,
    build_demo_estate,
)


def read_only_ref() -> CredentialRef:
    return CredentialRef(
        provider="env",
        path="memorypbx-source",
        kind=CredentialKind.API_TOKEN,
        scope=CredentialScope.READ_ONLY,
    )


def read_write_ref() -> CredentialRef:
    return CredentialRef(
        provider="env",
        path="memorypbx-target",
        kind=CredentialKind.API_TOKEN,
        scope=CredentialScope.READ_WRITE,
    )


async def _no_sleep(_seconds: float) -> None:
    """Collapse backoff and confirm-poll delays so tests stay fast."""
    return None


@pytest.fixture
def source_estate() -> MemoryPBXEstate:
    return build_demo_estate("memorypbx-source")


@pytest.fixture
def target_estate() -> MemoryPBXEstate:
    return MemoryPBXEstate(instance_id="memorypbx-target")


@pytest.fixture
def source_connector(source_estate: MemoryPBXEstate) -> MemoryPBXConnector:
    return MemoryPBXConnector(
        source_estate,
        tenant_id="contoso",
        credential_ref=read_only_ref(),
        sleep=_no_sleep,
    )


@pytest.fixture
def target_connector(target_estate: MemoryPBXEstate) -> MemoryPBXConnector:
    return MemoryPBXConnector(
        target_estate,
        tenant_id="contoso",
        credential_ref=read_write_ref(),
        sleep=_no_sleep,
    )


@pytest.fixture
def extract_request() -> ExtractRequest:
    return ExtractRequest(
        run_id="run-0001",
        tenant_id="contoso",
        estate_id="contoso-legacy",
        page_size=3,
    )


def production_authorization(
    plan: ApplyPlan,
    receipt: DryRunReceipt,
    *,
    sites: list[str] | None = None,
    approvers: tuple[str, ...] = ("planner@contoso.example", "approver@contoso.example"),
    window: ChangeWindow | None = None,
) -> ApplyAuthorization:
    """A fully evidenced production authorization for ``plan``."""
    now = datetime.now(UTC)
    confirmations = [
        EmergencyConfirmation(
            site_code=site,
            confirmed_by="telecoms.lead@contoso.example",
            civic_address_verified=True,
            elin_verified=True,
        )
        for site in (sites if sites is not None else sorted(plan.emergency_sites()))
    ]
    return ApplyAuthorization(
        mode=ExecutionMode.PRODUCTION,
        requested_by="operator@contoso.example",
        correlation_id="corr-0001",
        dry_run_receipt=receipt,
        approvals=[Approval(approver=name) for name in approvers],
        change_window=window
        or ChangeWindow(
            start=now - timedelta(hours=1),
            end=now + timedelta(hours=1),
            reference="CHG0042311",
        ),
        emergency_confirmations=confirmations,
    )
