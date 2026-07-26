"""The connector contract.

Every platform implements exactly two directions against the canonical model:

* ``Extract``  - platform -> canonical  (read-only, always)
* ``Apply``    - canonical -> platform  (guarded, dry-run by default)

plus ``Capabilities`` and ``TestConnection``. Reverse migration is not a special
case: it is the same two methods with source and target swapped.

Why ``extract`` and ``apply`` are final
---------------------------------------
The guardrails in this file are the ones that stop a bug becoming an outage or a
regulatory incident: no writes through a read-only scope, no production write
without a matching dry run and two approvers, no silent emergency-configuration
change. If those checks lived in each connector, they would eventually be
skipped in one of them.

So the public methods are concrete and non-overridable (enforced in
``__init_subclass__``), and connector authors implement the underscore-prefixed
hooks that those methods call *after* the guards have passed. A connector cannot
reach the platform without going through the gate.
"""

from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, ClassVar

from ucm_bridge.canonical.base import (
    CanonicalEntity,
    FidelityLevel,
    Platform,
    utcnow,
)
from ucm_bridge.canonical.snapshot import EstateSnapshot, SnapshotKind
from ucm_bridge.connectors.capabilities import CapabilityManifest, WriteVerb
from ucm_bridge.connectors.contracts import (
    ApplyAuthorization,
    ApplyPlan,
    ApplyReport,
    ConnectionTestResult,
    DryRunReceipt,
    ExecutionMode,
    ExtractBatch,
    ExtractRequest,
    OperationPreview,
    OperationResult,
    OperationStatus,
    RollbackBundle,
    WriteOperation,
)
from ucm_bridge.connectors.credentials import (
    CredentialBroker,
    CredentialRef,
    CredentialScope,
)
from ucm_bridge.connectors.errors import (
    ConnectorError,
    ContractViolation,
    RateLimited,
    SourceWriteAttempted,
    UnmappableEntityWrite,
    UnsupportedEntityKind,
)
from ucm_bridge.vendor.readiness import (
    ConnectorReadiness,
    assert_production_ready,
    assess_readiness,
)

_FINAL_METHODS = ("extract", "extract_snapshot", "apply", "dry_run")

SleepFn = Callable[[float], Awaitable[None]]


class Connector(ABC):
    """Base class for every platform connector.

    Subclasses implement:

    * :meth:`capabilities` - the declared manifest
    * :meth:`test_connection` - reachability, auth, and permission check
    * :meth:`_extract_batches` - stream canonical entities out of the platform
    * :meth:`_preview_operation` - what a write would do, without doing it
    * :meth:`_execute_operation` - actually do it

    and may override :meth:`_capture_pre_state`, :meth:`_invert_operation`, and
    :meth:`_confirm_operation`.
    """

    connector_id: ClassVar[str]
    platform: ClassVar[Platform]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Only enforce on concrete connectors, so intermediate abstract bases can exist.
        if inspect.isabstract(cls):
            return
        for name in _FINAL_METHODS:
            if name in cls.__dict__:
                raise ContractViolation(
                    f"{cls.__name__} overrides Connector.{name}(), which is final. The guardrail "
                    f"checks live there. Implement the _-prefixed hooks instead."
                )
        for required in ("connector_id", "platform"):
            if not hasattr(cls, required):
                raise ContractViolation(f"{cls.__name__} must declare a class-level {required!r}")

    def __init__(
        self,
        *,
        instance_id: str,
        tenant_id: str,
        credential_ref: CredentialRef,
        credentials: CredentialBroker,
        sleep: SleepFn | None = None,
    ) -> None:
        self.instance_id = instance_id
        self.tenant_id = tenant_id
        self.credential_ref = credential_ref
        self.credentials = credentials
        self._sleep: SleepFn = sleep or asyncio.sleep

    # ------------------------------------------------------------------ #
    # Declared contract - implemented by subclasses
    # ------------------------------------------------------------------ #

    @abstractmethod
    def capabilities(self) -> CapabilityManifest:
        """The connector's capability manifest. Must be pure and side-effect free."""

    def synthetic_cassette_names(self) -> list[str]:
        """Cassettes that were hand-authored rather than captured from a real system.

        A non-empty list downgrades the connector to LAB_ONLY, which blocks
        production writes. Connectors declare this themselves because only the
        author knows how their fixtures were produced.
        """
        return []

    def readiness(self) -> ConnectorReadiness:
        """Whether this connector may be pointed at a production system."""
        return assess_readiness(
            self.capabilities(), synthetic_cassettes=self.synthetic_cassette_names()
        )

    @abstractmethod
    async def test_connection(self) -> ConnectionTestResult:
        """Verify reachability, authentication, and granted permissions.

        Must not modify the platform, even when holding a read-write credential.
        """

    @abstractmethod
    def _extract_batches(self, request: ExtractRequest) -> AsyncIterator[ExtractBatch]:
        """Stream canonical entities out of the platform. Read-only."""

    @abstractmethod
    async def _preview_operation(self, operation: WriteOperation) -> OperationPreview:
        """Describe what ``operation`` would do, reading the target but not writing it."""

    @abstractmethod
    async def _execute_operation(self, operation: WriteOperation) -> OperationResult:
        """Perform the write. Only ever called after every guardrail has passed."""

    # -- optional hooks ------------------------------------------------- #

    @classmethod
    def natural_key_for(cls, entity: CanonicalEntity) -> str | None:
        """This platform's native key for a canonical entity, derived from its content.

        Used by the planner to resolve canonical references into keys the target
        actually understands: a source ``canonical_id`` is meaningless on the
        target, but ``+442071838750`` or ``jdoe@contoso.com`` is not.

        Returns None when the connector cannot derive a key for that kind, which
        makes the reference unresolvable and is reported by the planner rather
        than written as a dangling pointer.
        """
        return None

    async def _capture_pre_state(self, operation: WriteOperation) -> dict[str, Any] | None:
        """Target state before the write, for rollback. None means not capturable."""
        return None

    def _invert_operation(
        self, operation: WriteOperation, result: OperationResult
    ) -> WriteOperation | None:
        """Inverse of a completed operation, or None if it cannot be inverted."""
        return None

    async def _confirm_operation(self, operation: WriteOperation, result: OperationResult) -> bool:
        """Re-read the target to confirm the write landed.

        Only called for kinds listed in the manifest's eventual-consistency
        policy. Default assumes read-your-write, which is wrong for Teams and
        right for CUCM.
        """
        return True

    # ------------------------------------------------------------------ #
    # Extract - final
    # ------------------------------------------------------------------ #

    async def extract(self, request: ExtractRequest) -> AsyncIterator[ExtractBatch]:
        """Read the estate into canonical form. Never writes to the source.

        Validates on the way out that the connector only produced entity kinds it
        declared extractable, that every entity carries provenance, and that
        nothing arrived pre-populated with a target reference.
        """
        manifest = self.capabilities()
        extractable = manifest.extractable_kinds()

        if request.entity_kinds:
            undeclared = set(request.entity_kinds) - extractable
            if undeclared:
                raise UnsupportedEntityKind(
                    self.connector_id,
                    f"asked to extract kinds it does not declare: {sorted(undeclared)}",
                )

        async for batch in self._extract_batches(request):
            for entity in batch.entities:
                self._validate_extracted(entity, extractable)
                entity.seal()
            yield batch

    def _validate_extracted(self, entity: CanonicalEntity, extractable: set[str]) -> None:
        if entity.kind not in extractable:
            raise UnsupportedEntityKind(
                self.connector_id,
                f"produced a {entity.kind} during extract but does not declare it extractable",
            )
        if entity.source_ref is None:
            raise ContractViolation(
                f"{self.connector_id}: extracted {entity.kind} {entity.canonical_id} without a "
                "source_ref. Every extracted entity must carry its provenance."
            )
        if entity.target_ref is not None:
            raise ContractViolation(
                f"{self.connector_id}: extracted {entity.kind} {entity.canonical_id} with a "
                "target_ref already set. Extract must not populate target references."
            )

    async def extract_snapshot(
        self,
        request: ExtractRequest,
        *,
        snapshot_id: str | None = None,
        snapshot_kind: SnapshotKind = SnapshotKind.DISCOVERY,
    ) -> EstateSnapshot:
        """Materialise a full extraction into one snapshot.

        Convenient, and correct for estates up to a few hundred thousand objects.
        Above that, consume :meth:`extract` batch-by-batch and stream to storage
        rather than holding the estate in memory.
        """
        entities: list[CanonicalEntity] = []
        warnings: list[str] = []
        async for batch in self.extract(request):
            entities.extend(batch.entities)
            warnings.extend(batch.warnings)

        manifest = self.capabilities()
        return EstateSnapshot.build(
            snapshot_id=snapshot_id or f"snap-{uuid.uuid4()}",
            tenant_id=request.tenant_id,
            estate_id=request.estate_id,
            entities=entities,
            snapshot_kind=snapshot_kind,
            platforms=[self.platform],
            connector_versions={manifest.connector_id: manifest.connector_version},
            run_id=request.run_id,
            read_only=True,
            warnings=warnings,
        )

    # ------------------------------------------------------------------ #
    # Apply - final
    # ------------------------------------------------------------------ #

    async def dry_run(self, plan: ApplyPlan, *, requested_by: str = "system") -> DryRunReceipt:
        """Preview a plan. Convenience wrapper over :meth:`apply` in DRY_RUN mode."""
        authorization = ApplyAuthorization(
            mode=ExecutionMode.DRY_RUN,
            requested_by=requested_by,
            correlation_id=f"dryrun-{uuid.uuid4()}",
        )
        report = await self.apply(plan, authorization)
        assert report.dry_run_receipt is not None
        return report.dry_run_receipt

    async def apply(
        self,
        plan: ApplyPlan,
        authorization: ApplyAuthorization,
        *,
        resume_after_op_id: str | None = None,
    ) -> ApplyReport:
        """Execute (or preview) a plan against this platform.

        Order of checks is deliberate: cheap, total checks that can reject the
        whole plan run before any per-object work, so a plan that was never going
        to be allowed fails immediately rather than half-way through a wave.
        """
        started_at = utcnow()
        manifest = self.capabilities()

        # 1. Does this authorization actually authorise this plan?
        authorization.assert_covers(plan)

        # 2. Never write through a read-only credential scope.
        if (
            authorization.mode is ExecutionMode.PRODUCTION
            and self.credential_ref.scope is not CredentialScope.READ_WRITE
        ):
            raise SourceWriteAttempted(
                f"{self.connector_id} holds a {self.credential_ref.scope.value} credential "
                f"({self.credential_ref.path!r}) and must not perform production writes. "
                "Source connectors are read-only by construction."
            )

        # 3. Never write to production from a connector whose API surface has not
        #    been verified. A plausible-looking wrong request against a production
        #    publisher is a real outage.
        if authorization.mode is ExecutionMode.PRODUCTION:
            assert_production_ready(self.readiness())

        # 4. Only write what the manifest claims to be able to write.
        appliable = manifest.appliable_kinds()
        undeclared = {op.entity_kind for op in plan.operations} - appliable
        if undeclared:
            raise UnsupportedEntityKind(
                self.connector_id,
                f"plan {plan.plan_id} contains kinds this connector cannot apply: "
                f"{sorted(undeclared)}",
            )
        for op in plan.operations:
            capability = manifest.capability_for(op.entity_kind)
            assert capability is not None  # guaranteed by the check above
            if op.verb not in capability.supported_verbs:
                raise UnsupportedEntityKind(
                    self.connector_id,
                    f"operation {op.op_id} uses verb {op.verb.value} on {op.entity_kind}, "
                    f"which the manifest does not support "
                    f"(supported: {[v.value for v in capability.supported_verbs]})",
                )

        # 5. An UNMAPPABLE entity has no target representation; writing one is meaningless.
        unmappable = plan.unmappable_operations()
        if unmappable:
            raise UnmappableEntityWrite(
                f"plan {plan.plan_id} contains operations on entities assessed UNMAPPABLE: "
                f"{[op.op_id for op in unmappable]}. These are manual work, not writes."
            )

        ordered = plan.operations_in_dependency_order()
        if resume_after_op_id is not None:
            ordered = self._resume_slice(ordered, resume_after_op_id)

        if authorization.mode is ExecutionMode.DRY_RUN:
            return await self._run_dry(plan, authorization, ordered, started_at, manifest)
        return await self._run_production(plan, authorization, ordered, started_at, manifest)

    @staticmethod
    def _resume_slice(
        ordered: list[WriteOperation], resume_after_op_id: str
    ) -> list[WriteOperation]:
        ids = [op.op_id for op in ordered]
        if resume_after_op_id not in ids:
            raise ContractViolation(
                f"Cannot resume after {resume_after_op_id!r}: not in this plan. The checkpoint "
                "belongs to a different plan."
            )
        return ordered[ids.index(resume_after_op_id) + 1 :]

    # -- dry run --------------------------------------------------------- #

    async def _run_dry(
        self,
        plan: ApplyPlan,
        authorization: ApplyAuthorization,
        ordered: list[WriteOperation],
        started_at: Any,
        manifest: CapabilityManifest,
    ) -> ApplyReport:
        previews: list[OperationPreview] = []
        results: list[OperationResult] = []
        for op in ordered:
            preview = await self._preview_operation(op)
            previews.append(preview)
            results.append(
                OperationResult(
                    op_id=op.op_id,
                    status=OperationStatus.PREVIEWED,
                    target_native_key=preview.target_native_key,
                    target_native_type=preview.target_native_type,
                )
            )

        receipt = DryRunReceipt(
            receipt_id=f"dr-{uuid.uuid4()}",
            plan_id=plan.plan_id,
            plan_digest=plan.plan_digest or plan.compute_digest(),
            connector_id=self.connector_id,
            previews=previews,
            would_change_count=sum(1 for p in previews if p.would_change),
            no_change_count=sum(1 for p in previews if not p.would_change),
            warnings=[w for p in previews for w in p.warnings],
        )
        return ApplyReport(
            plan_id=plan.plan_id,
            connector_id=self.connector_id,
            mode=ExecutionMode.DRY_RUN,
            correlation_id=authorization.correlation_id,
            started_at=started_at,
            finished_at=utcnow(),
            results=results,
            dry_run_receipt=receipt,
        )

    # -- production ------------------------------------------------------ #

    async def _run_production(
        self,
        plan: ApplyPlan,
        authorization: ApplyAuthorization,
        ordered: list[WriteOperation],
        started_at: Any,
        manifest: CapabilityManifest,
    ) -> ApplyReport:
        results: list[OperationResult] = []
        inverse_ops: list[WriteOperation] = []
        uninvertible: list[str] = []
        succeeded: set[str] = set()
        unusable: set[str] = set()
        checkpoint: str | None = None
        confirm_kinds = set(manifest.eventual_consistency.confirm_required_for_kinds)

        for op in ordered:
            blocked_by = [d for d in op.depends_on if d in unusable]
            if blocked_by:
                results.append(
                    OperationResult(
                        op_id=op.op_id,
                        status=OperationStatus.BLOCKED_BY_DEPENDENCY,
                        error_message=f"depends on unsuccessful operation(s): {blocked_by}",
                    )
                )
                unusable.add(op.op_id)
                continue

            # Idempotency: if the target already matches, this is not a write.
            preview = await self._preview_operation(op)
            if not preview.would_change:
                results.append(
                    OperationResult(
                        op_id=op.op_id,
                        status=OperationStatus.SKIPPED_NO_CHANGE,
                        target_native_key=preview.target_native_key,
                        target_native_type=preview.target_native_type,
                    )
                )
                succeeded.add(op.op_id)
                checkpoint = op.op_id
                continue

            pre_state = await self._capture_pre_state(op)
            result = await self._execute_with_retry(op, manifest)
            result.pre_state = pre_state

            if result.status is OperationStatus.SUCCEEDED and op.entity_kind in confirm_kinds:
                result.confirmed = await self._confirm_with_polling(op, result, manifest)
                if not result.confirmed:
                    result.status = OperationStatus.QUARANTINED
                    result.error_type = "ReplicationLagTimeout"
                    result.error_message = (
                        "Write was accepted but did not become readable within "
                        f"{manifest.eventual_consistency.confirm_poll_timeout_seconds}s. "
                        "Quarantined for verification rather than retried, because retrying "
                        "an accepted write can duplicate the object."
                    )

            results.append(result)

            if result.status is OperationStatus.SUCCEEDED:
                succeeded.add(op.op_id)
                checkpoint = op.op_id
                inverse = self._invert_operation(op, result)
                if inverse is not None:
                    inverse_ops.append(inverse)
                else:
                    uninvertible.append(op.op_id)
            else:
                # Quarantine per object; the wave continues.
                unusable.add(op.op_id)

        rollback = RollbackBundle(
            plan_id=plan.plan_id,
            correlation_id=authorization.correlation_id,
            operations=list(reversed(inverse_ops)),
            incomplete_reason=(
                f"{len(uninvertible)} operation(s) could not be inverted: {uninvertible}"
                if uninvertible
                else None
            ),
        )

        return ApplyReport(
            plan_id=plan.plan_id,
            connector_id=self.connector_id,
            mode=ExecutionMode.PRODUCTION,
            correlation_id=authorization.correlation_id,
            started_at=started_at,
            finished_at=utcnow(),
            results=results,
            dry_run_receipt=authorization.dry_run_receipt,
            rollback_bundle=rollback,
            checkpoint_cursor=checkpoint,
        )

    async def _execute_with_retry(
        self, operation: WriteOperation, manifest: CapabilityManifest
    ) -> OperationResult:
        limits = manifest.rate_limits
        backoff = limits.initial_backoff_seconds
        last_error: ConnectorError | None = None

        for attempt in range(1, limits.max_attempts + 1):
            start = time.perf_counter()
            try:
                result = await self._execute_operation(operation)
                result.attempts = attempt
                if result.duration_ms is None:
                    result.duration_ms = (time.perf_counter() - start) * 1000
                return result
            except ConnectorError as exc:
                last_error = exc
                if not exc.retryable or attempt == limits.max_attempts:
                    break
                delay = backoff
                if isinstance(exc, RateLimited) and exc.retry_after_seconds is not None:
                    # Honour the platform's own instruction over our backoff curve.
                    delay = exc.retry_after_seconds
                await self._sleep(min(delay, limits.max_backoff_seconds))
                backoff = min(backoff * 2, limits.max_backoff_seconds)

        assert last_error is not None
        return OperationResult(
            op_id=operation.op_id,
            status=OperationStatus.QUARANTINED,
            error_type=type(last_error).__name__,
            error_message=str(last_error),
            retryable=last_error.retryable,
            attempts=limits.max_attempts if last_error.retryable else 1,
            duration_ms=None,
        )

    async def _confirm_with_polling(
        self,
        operation: WriteOperation,
        result: OperationResult,
        manifest: CapabilityManifest,
    ) -> bool:
        policy = manifest.eventual_consistency
        deadline = time.monotonic() + policy.confirm_poll_timeout_seconds
        while True:
            if await self._confirm_operation(operation, result):
                return True
            if time.monotonic() >= deadline:
                return False
            await self._sleep(policy.confirm_poll_interval_seconds)


__all__ = [
    "Connector",
    "CredentialRef",
    "CredentialScope",
    "ExecutionMode",
    "FidelityLevel",
    "WriteVerb",
]
