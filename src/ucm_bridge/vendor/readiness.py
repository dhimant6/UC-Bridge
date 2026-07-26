"""Connector readiness: is this connector safe to point at a production system?

The brief's guardrail is that vendor APIs move and assumptions from memory are
not acceptable. This module turns that from a rule people remember into a check
the build runs.

A connector is **production-ready** only when every API surface it declares
carries a ``verified_at`` date and a ``verification_method``, and its cassettes
were captured from a real system rather than hand-authored. Anything else is
**lab-only**: usable for development and dry runs, refused for production
writes by :func:`assert_production_ready`.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from ucm_bridge.connectors.capabilities import CapabilityManifest
from ucm_bridge.connectors.errors import GuardrailViolation


class ReadinessLevel(StrEnum):
    PRODUCTION_READY = "PRODUCTION_READY"
    """Every API surface verified against a live system or vendor documentation,
    with real recorded cassettes."""

    LAB_ONLY = "LAB_ONLY"
    """Signatures checked against vendor documentation, but not exercised against a
    real system. Safe to dry-run; refused for production writes."""

    UNVERIFIED = "UNVERIFIED"
    """One or more API surfaces carry no verification record at all."""


class NotProductionReady(GuardrailViolation):
    """A production write was attempted with a connector that is not verified."""


class ConnectorReadiness(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector_id: str
    level: ReadinessLevel
    verified_surfaces: list[str] = Field(default_factory=list)
    unverified_surfaces: list[str] = Field(default_factory=list)
    synthetic_cassettes: list[str] = Field(default_factory=list)
    oldest_verification: date | None = None
    notes: list[str] = Field(default_factory=list)

    @property
    def may_write_to_production(self) -> bool:
        return self.level is ReadinessLevel.PRODUCTION_READY


def assess_readiness(
    manifest: CapabilityManifest, *, synthetic_cassettes: list[str] | None = None
) -> ConnectorReadiness:
    synthetic = sorted(synthetic_cassettes or [])
    verified = [s.name for s in manifest.api_surfaces if s.is_verified]
    unverified = [s.name for s in manifest.unverified_api_surfaces()]

    dates = [s.verified_at for s in manifest.api_surfaces if s.verified_at is not None]
    notes: list[str] = []

    if unverified:
        level = ReadinessLevel.UNVERIFIED
        notes.append(
            f"{len(unverified)} API surface(s) carry no verification record: {unverified}. "
            "Check them against vendor documentation and record verified_at."
        )
    elif synthetic:
        level = ReadinessLevel.LAB_ONLY
        notes.append(
            f"Signatures are documented but cassettes are hand-authored ({synthetic}). "
            "Capture recordings from a real system before production writes."
        )
    else:
        level = ReadinessLevel.PRODUCTION_READY

    return ConnectorReadiness(
        connector_id=manifest.connector_id,
        level=level,
        verified_surfaces=sorted(verified),
        unverified_surfaces=sorted(unverified),
        synthetic_cassettes=synthetic,
        oldest_verification=min(dates) if dates else None,
        notes=notes,
    )


def assert_production_ready(readiness: ConnectorReadiness) -> None:
    """Refuse a production write from an unverified connector.

    This is the mechanical version of "a plausible-looking wrong AXL request
    against a production publisher is a genuine outage".
    """
    if readiness.may_write_to_production:
        return
    raise NotProductionReady(
        f"Connector {readiness.connector_id} is {readiness.level.value} and must not perform "
        f"production writes.\n"
        + "\n".join(f"  - {note}" for note in readiness.notes)
    )
