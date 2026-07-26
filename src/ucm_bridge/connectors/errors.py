"""Connector error taxonomy.

Split deliberately into three families because they are handled differently:

* :class:`GuardrailViolation` - the platform refused to do something unsafe.
  Never retried, never quarantined, always surfaced. A bug that produces one of
  these is a bug worth failing the build over.
* :class:`ConnectorError` and friends - the vendor platform misbehaved.
  Retryable or quarantinable per subclass.
* :class:`CapabilityError` - the connector was asked for something it never
  claimed it could do. A planning bug, caught before any I/O.
"""

from __future__ import annotations

from typing import Any


class UCMBridgeError(Exception):
    """Base for everything this platform raises."""


# --------------------------------------------------------------------------- #
# Guardrails
# --------------------------------------------------------------------------- #


class GuardrailViolation(UCMBridgeError):
    """A safety invariant was about to be broken. Not retryable. Not suppressible."""


class SourceWriteAttempted(GuardrailViolation):
    """Something tried to write through a read-only (source) credential scope."""


class DryRunRequired(GuardrailViolation):
    """Production write attempted without a completed dry run of the same plan."""


class PlanDigestMismatch(GuardrailViolation):
    """The dry-run receipt refers to a different plan than the one being applied.

    Raised when a plan is edited after approval. Approving plan A and executing
    plan B is the failure mode this exists to make impossible.
    """


class ApprovalRequired(GuardrailViolation):
    """Production write attempted without the required two distinct approvers."""


class ChangeWindowClosed(GuardrailViolation):
    """Write attempted outside the approved change window, with no recorded override."""


class EmergencyConfirmationRequired(GuardrailViolation):
    """A plan touches emergency calling configuration for an unconfirmed site.

    Emergency configuration is never migrated silently; every affected site
    needs an explicit, attributed confirmation.
    """


class UnmappableEntityWrite(GuardrailViolation):
    """An entity assessed UNMAPPABLE was included in a write plan."""


# --------------------------------------------------------------------------- #
# Capability / contract errors
# --------------------------------------------------------------------------- #


class CapabilityError(UCMBridgeError):
    """The connector was asked to do something outside its declared manifest."""

    def __init__(self, connector_id: str, message: str) -> None:
        super().__init__(f"[{connector_id}] {message}")
        self.connector_id = connector_id


class UnsupportedEntityKind(CapabilityError):
    pass


class ContractViolation(UCMBridgeError):
    """A connector implementation broke the base-class contract."""


# --------------------------------------------------------------------------- #
# Platform errors
# --------------------------------------------------------------------------- #


class ConnectorError(UCMBridgeError):
    """A vendor platform returned an error."""

    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        connector_id: str | None = None,
        native_key: str | None = None,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.connector_id = connector_id
        self.native_key = native_key
        self.status_code = status_code
        self.details = details or {}


class AuthenticationError(ConnectorError):
    """Credentials rejected. Not retryable; a retry loop here locks accounts out."""


class AuthorizationError(ConnectorError):
    """Authenticated but not permitted. Usually a missing API scope or role."""


class RateLimited(ConnectorError):
    """Throttled by the platform. Retry after ``retry_after_seconds``."""

    retryable = True

    def __init__(self, message: str, *, retry_after_seconds: float | None = None, **kwargs: Any):
        super().__init__(message, **kwargs)
        self.retry_after_seconds = retry_after_seconds


class TransientPlatformError(ConnectorError):
    """5xx, socket reset, cluster failover. Retry with backoff."""

    retryable = True


class ObjectConflict(ConnectorError):
    """The target object exists in an unexpected state; needs human adjudication."""


class ReplicationLagTimeout(ConnectorError):
    """A write succeeded but never became readable within the confirm-poll budget.

    Distinct from a failure: the object may well exist. Quarantined for
    verification rather than blindly retried, since retrying can duplicate.
    """

    retryable = False


class CredentialError(UCMBridgeError):
    """The credential provider could not supply a usable secret."""
