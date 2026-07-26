"""Request / plan / result types exchanged with connectors.

The safety-critical object here is :class:`ApplyAuthorization`. Production
writes require, with no code path around them:

* a completed dry run **of the same plan**, matched by digest rather than by
  claim, so that editing a plan after approval invalidates the approval;
* two distinct approvers;
* an open change window, or a recorded, attributed override;
* explicit per-site confirmation for every site whose emergency calling
  configuration the plan touches.

These are validated on the model itself, so an unauthorised authorization object
cannot even be constructed, let alone passed to a connector.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ucm_bridge.canonical.base import (
    CanonicalEntity,
    FidelityLevel,
    digest_of,
    utcnow,
)
from ucm_bridge.connectors.capabilities import WriteVerb
from ucm_bridge.connectors.errors import (
    ApprovalRequired,
    ChangeWindowClosed,
    DryRunRequired,
    EmergencyConfirmationRequired,
    PlanDigestMismatch,
)

EMERGENCY_SENSITIVE_KINDS: frozenset[str] = frozenset(
    {
        "EmergencyLocation",
        "EmergencyNumber",
        "EmergencyCallingPolicy",
    }
)
"""Entity kinds whose modification requires explicit per-site human confirmation."""


class ExecutionMode(StrEnum):
    DRY_RUN = "DRY_RUN"
    """Default everywhere. Produces the exact operations that would be issued."""
    PRODUCTION = "PRODUCTION"


class OperationStatus(StrEnum):
    PREVIEWED = "PREVIEWED"
    SUCCEEDED = "SUCCEEDED"
    SKIPPED_NO_CHANGE = "SKIPPED_NO_CHANGE"
    """Idempotency hit: the target already matches. The proof of §9's re-run criterion."""
    FAILED = "FAILED"
    QUARANTINED = "QUARANTINED"
    """Failed and set aside so the wave continues. Retried or escalated later."""
    BLOCKED_BY_DEPENDENCY = "BLOCKED_BY_DEPENDENCY"
    ROLLED_BACK = "ROLLED_BACK"


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #


class ExtractRequest(BaseModel):
    """A read-only crawl instruction. Safely re-runnable by construction."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    tenant_id: str
    estate_id: str
    entity_kinds: list[str] | None = Field(
        default=None, description="None means every kind the connector declares extractable."
    )
    native_key_filter: list[str] | None = Field(
        default=None, description="Restrict to specific source objects, for targeted re-reads."
    )
    site_codes: list[str] | None = None
    modified_since: datetime | None = Field(
        default=None, description="Incremental crawl, where the source API supports it."
    )
    include_native_attributes: bool = Field(
        default=True,
        description="Retain raw source attributes. Turning this off loses the evidence the "
        "assessment engine needs to report unmapped fields.",
    )
    include_message_payloads: bool = Field(
        default=False,
        description="Export voicemail/chat bodies to object storage. Off by default: this is "
        "personal data and pulling it has retention and GDPR consequences.",
    )
    page_size: int = Field(default=500, ge=1, le=10_000)
    correlation_id: str | None = None


class ExtractBatch(BaseModel):
    """One page of extracted entities, streamed so a 500k-object estate fits in memory."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    sequence: int = Field(ge=0)
    entities: list[CanonicalEntity] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    raw_sql_used: bool = Field(
        default=False,
        description="Propagated to the audit log. Raw SQL reads bypass the vendor's typed API "
        "and its schema guarantees, so their use is always recorded.",
    )
    is_final: bool = False
    cursor: str | None = Field(
        default=None,
        description="Opaque resume token. Persisting this is what makes an interrupted "
        "discovery resumable rather than restartable.",
    )


# --------------------------------------------------------------------------- #
# Change control
# --------------------------------------------------------------------------- #


class ChangeWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: datetime
    end: datetime
    timezone: str = "UTC"
    reference: str | None = Field(default=None, description="Change-record id, e.g. 'CHG0042311'.")

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end <= self.start:
            raise ValueError("Change window end must be after start")
        return self

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment <= self.end


class Approval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approver: str = Field(description="User principal. Two distinct values are required.")
    approved_at: datetime = Field(default_factory=utcnow)
    role: str | None = None
    comment: str | None = None


class EmergencyConfirmation(BaseModel):
    """Per-site sign-off that emergency calling configuration is correct on the target."""

    model_config = ConfigDict(extra="forbid")

    site_code: str
    confirmed_by: str
    confirmed_at: datetime = Field(default_factory=utcnow)
    civic_address_verified: bool = False
    elin_verified: bool = False
    notes: str | None = None

    @model_validator(mode="after")
    def _must_verify_something(self) -> Self:
        if not (self.civic_address_verified or self.elin_verified):
            raise ValueError(
                f"Emergency confirmation for site {self.site_code} verifies neither the civic "
                "address nor the ELIN, so it confirms nothing."
            )
        return self


# --------------------------------------------------------------------------- #
# Plans
# --------------------------------------------------------------------------- #


class WriteOperation(BaseModel):
    """One intended change to one target object."""

    model_config = ConfigDict(extra="forbid")

    op_id: str
    verb: WriteVerb
    entity_kind: str
    canonical_id: str
    idempotency_key: str = Field(
        description="Stable across retries and re-runs. A retry with the same key must not "
        "create a second object."
    )
    payload: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(
        default_factory=list, description="op_ids that must succeed first."
    )
    site_code: str | None = None
    fidelity: FidelityLevel = FidelityLevel.DEGRADED
    description: str | None = None

    def digest(self) -> str:
        """Content digest excluding nothing: the plan is what it says it is."""
        return digest_of(self.model_dump(mode="json"))


class ApplyPlan(BaseModel):
    """An ordered, digest-identified set of writes for one connector."""

    model_config = ConfigDict(extra="forbid")

    plan_id: str
    tenant_id: str
    estate_id: str
    wave_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    operations: list[WriteOperation] = Field(default_factory=list)
    plan_digest: str | None = None

    def compute_digest(self) -> str:
        """Digest over the operations only.

        Deliberately excludes ``created_at`` and ``plan_id`` so that regenerating
        the same plan from the same inputs yields the same digest, which is what
        allows an approval to survive a harmless replan.
        """
        return digest_of([op.model_dump(mode="json") for op in self.operations])

    def seal(self) -> ApplyPlan:
        self.plan_digest = self.compute_digest()
        return self

    def emergency_sites(self) -> set[str]:
        """Sites whose emergency configuration this plan touches."""
        return {
            op.site_code
            for op in self.operations
            if op.entity_kind in EMERGENCY_SENSITIVE_KINDS and op.site_code
        }

    def unmappable_operations(self) -> list[WriteOperation]:
        return [op for op in self.operations if op.fidelity is FidelityLevel.UNMAPPABLE]

    def operations_in_dependency_order(self) -> list[WriteOperation]:
        """Topological order. Raises on a cycle rather than silently picking an order."""
        by_id = {op.op_id: op for op in self.operations}
        ordered: list[WriteOperation] = []
        state: dict[str, int] = {}  # 0 = visiting, 1 = done

        def visit(op_id: str, trail: tuple[str, ...]) -> None:
            if state.get(op_id) == 1:
                return
            if state.get(op_id) == 0:
                raise ValueError(f"Dependency cycle in plan: {' -> '.join([*trail, op_id])}")
            op = by_id.get(op_id)
            if op is None:
                raise ValueError(f"Operation {op_id!r} depends on unknown op")
            state[op_id] = 0
            for dep in op.depends_on:
                visit(dep, (*trail, op_id))
            state[op_id] = 1
            ordered.append(op)

        for op in self.operations:
            visit(op.op_id, ())
        return ordered


class OperationPreview(BaseModel):
    """What a single operation would do, as produced by a dry run."""

    model_config = ConfigDict(extra="forbid")

    op_id: str
    verb: WriteVerb
    target_native_type: str | None = None
    target_native_key: str | None = None
    api_call: str = Field(
        description="The exact call that would be issued, e.g. "
        "'AXL:addPhone' or 'Set-CsPhoneNumberAssignment -Identity ... -PhoneNumber ...'."
    )
    current_target_state: dict[str, Any] | None = Field(
        default=None, description="None when the object does not yet exist on the target."
    )
    proposed_state: dict[str, Any] = Field(default_factory=dict)
    would_change: bool = Field(
        default=True,
        description="False means the target already matches; the real run will skip it.",
    )
    warnings: list[str] = Field(default_factory=list)


class DryRunReceipt(BaseModel):
    """Evidence that a dry run of a specific plan completed.

    Carries the plan digest so a production run can prove it is executing the
    plan that was previewed and approved, not a later edit of it.
    """

    model_config = ConfigDict(extra="forbid")

    receipt_id: str
    plan_id: str
    plan_digest: str
    connector_id: str
    produced_at: datetime = Field(default_factory=utcnow)
    previews: list[OperationPreview] = Field(default_factory=list)
    would_change_count: int = 0
    no_change_count: int = 0
    warnings: list[str] = Field(default_factory=list)


class ApplyAuthorization(BaseModel):
    """Everything required to be allowed to write.

    Constructing one of these in PRODUCTION mode without the full evidence set
    raises. There is no bypass parameter, because a bypass parameter is how
    bypasses end up in production.
    """

    model_config = ConfigDict(extra="forbid")

    mode: ExecutionMode = ExecutionMode.DRY_RUN
    requested_by: str
    correlation_id: str
    dry_run_receipt: DryRunReceipt | None = None
    approvals: list[Approval] = Field(default_factory=list)
    change_window: ChangeWindow | None = None
    window_override_reason: str | None = None
    window_override_by: str | None = None
    emergency_confirmations: list[EmergencyConfirmation] = Field(default_factory=list)
    evaluated_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _production_requires_evidence(self) -> Self:
        if self.mode is not ExecutionMode.PRODUCTION:
            return self

        if self.dry_run_receipt is None:
            raise DryRunRequired(
                "Production apply requires a completed dry-run receipt for the same plan."
            )

        approvers = {a.approver for a in self.approvals}
        if len(approvers) < 2:
            raise ApprovalRequired(
                "Production apply requires two distinct approvers; got "
                f"{sorted(approvers) or 'none'}."
            )

        overridden = bool(self.window_override_reason and self.window_override_by)
        if self.change_window is None and not overridden:
            raise ChangeWindowClosed(
                "Production apply requires an approved change window, or an override with "
                "both a reason and an attributed approver."
            )
        if (
            self.change_window is not None
            and not self.change_window.contains(self.evaluated_at)
            and not overridden
        ):
            raise ChangeWindowClosed(
                f"Now ({self.evaluated_at.isoformat()}) is outside the approved change "
                f"window {self.change_window.start.isoformat()} - "
                f"{self.change_window.end.isoformat()}, and no attributed override was given."
            )
        return self

    def confirmed_emergency_sites(self) -> set[str]:
        return {c.site_code for c in self.emergency_confirmations}

    def assert_covers(self, plan: ApplyPlan) -> None:
        """Check this authorization actually authorises *this* plan."""
        if self.mode is not ExecutionMode.PRODUCTION:
            return

        expected = plan.plan_digest or plan.compute_digest()
        receipt = self.dry_run_receipt
        assert receipt is not None  # guaranteed by the model validator
        if receipt.plan_digest != expected:
            raise PlanDigestMismatch(
                f"Dry-run receipt {receipt.receipt_id} covers plan digest "
                f"{receipt.plan_digest}, but the plan being applied digests to {expected}. "
                "The plan changed after it was previewed and approved."
            )

        missing = plan.emergency_sites() - self.confirmed_emergency_sites()
        if missing:
            raise EmergencyConfirmationRequired(
                "Emergency calling configuration is never migrated silently. Missing per-site "
                f"confirmation for: {sorted(missing)}"
            )


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


class OperationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op_id: str
    status: OperationStatus
    target_native_key: str | None = None
    target_native_type: str | None = None
    error_type: str | None = None
    error_message: str | None = None
    retryable: bool = False
    attempts: int = 1
    pre_state: dict[str, Any] | None = Field(
        default=None,
        description="Target state captured before the write. The raw material for rollback.",
    )
    post_state: dict[str, Any] | None = None
    confirmed: bool = Field(
        default=False,
        description="True only after a confirm-poll re-read succeeded on an eventually "
        "consistent platform.",
    )
    duration_ms: float | None = None


class RollbackBundle(BaseModel):
    """Inverse operations, in reverse dependency order."""

    model_config = ConfigDict(extra="forbid")

    plan_id: str
    correlation_id: str
    operations: list[WriteOperation] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=utcnow)
    incomplete_reason: str | None = Field(
        default=None,
        description="Set when some operations could not be inverted, e.g. the connector could "
        "not capture pre-state. Partial rollback is disclosed, never silent.",
    )

    @property
    def is_complete(self) -> bool:
        return self.incomplete_reason is None


class ApplyReport(BaseModel):
    """Outcome of one apply run."""

    model_config = ConfigDict(extra="forbid")

    plan_id: str
    connector_id: str
    mode: ExecutionMode
    correlation_id: str
    started_at: datetime
    finished_at: datetime | None = None
    results: list[OperationResult] = Field(default_factory=list)
    dry_run_receipt: DryRunReceipt | None = None
    rollback_bundle: RollbackBundle | None = None
    checkpoint_cursor: str | None = Field(
        default=None,
        description="Last durably completed op_id. Resumption starts after this.",
    )

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for result in self.results:
            counts[result.status.value] = counts.get(result.status.value, 0) + 1
        return counts

    @property
    def changed_count(self) -> int:
        return sum(1 for r in self.results if r.status is OperationStatus.SUCCEEDED)

    @property
    def is_no_op(self) -> bool:
        """True when nothing changed: the idempotency proof for a repeated plan."""
        return all(
            r.status in (OperationStatus.SKIPPED_NO_CHANGE, OperationStatus.PREVIEWED)
            for r in self.results
        )

    def failures(self) -> Sequence[OperationResult]:
        return [
            r
            for r in self.results
            if r.status in (OperationStatus.FAILED, OperationStatus.QUARANTINED)
        ]


class ConnectionTestResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector_id: str
    reachable: bool
    authenticated: bool
    scope: str | None = None
    platform_version: str | None = Field(
        default=None,
        description="Version reported by the platform itself. Compared against the manifest's "
        "verified API version to catch a connector pointed at an unexpected release.",
    )
    latency_ms: float | None = None
    granted_permissions: list[str] = Field(default_factory=list)
    missing_permissions: list[str] = Field(default_factory=list)
    messages: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.reachable and self.authenticated and not self.missing_permissions
