"""VCR-style cassettes: recorded vendor interactions for offline tests.

The brief's rule is that no connector merges without tests against recorded API
fixtures. This is the recording format and the replay machinery, shared by every
transport (SOAP, REST, PowerShell) so connectors are tested the same way.

A cassette matches on a *canonical digest of the request*, not on call order, so
a connector that legitimately reorders its reads does not break its tests, while
a connector that changes what it sends does — which is the point.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ucm_bridge.canonical.base import digest_of


class RecordedInteraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: str
    request: dict[str, Any] = Field(default_factory=dict)
    response: Any = None
    error: str | None = Field(
        default=None,
        description="Fully-qualified error class to raise instead of returning a response. "
        "Lets a cassette reproduce throttling and 5xx paths.",
    )
    error_message: str | None = None
    status_code: int | None = None
    retry_after_seconds: float | None = None
    notes: str | None = None

    def key(self) -> str:
        return interaction_key(self.operation, self.request)


def interaction_key(operation: str, request: dict[str, Any]) -> str:
    return f"{operation}:{digest_of(request)}"


class CassetteMiss(LookupError):
    """The connector made a call the cassette does not contain.

    Deliberately loud. A silent fallthrough to a live call in a test suite is how
    a test suite ends up writing to a production cluster.
    """


class Cassette(BaseModel):
    """A recorded conversation with one vendor platform."""

    model_config = ConfigDict(extra="forbid")

    name: str
    platform: str
    recorded_against: str | None = Field(
        default=None,
        description="What produced this recording: 'lab CUCM 15.0', 'hand-authored from "
        "vendor docs 2026-07-26'. Hand-authored cassettes are honest fixtures, not "
        "evidence that the real API behaves this way.",
    )
    is_synthetic: bool = Field(
        default=True,
        description="True when hand-authored rather than captured from a live system. "
        "Connector readiness reporting treats synthetic cassettes as unverified.",
    )
    interactions: list[RecordedInteraction] = Field(default_factory=list)

    def index(self) -> dict[str, RecordedInteraction]:
        return {i.key(): i for i in self.interactions}

    def lookup(self, operation: str, request: dict[str, Any]) -> RecordedInteraction:
        key = interaction_key(operation, request)
        found = self.index().get(key)
        if found is None:
            available = sorted({i.operation for i in self.interactions})
            raise CassetteMiss(
                f"Cassette {self.name!r} has no recording for {operation} with this request.\n"
                f"  request: {json.dumps(request, sort_keys=True, default=str)[:400]}\n"
                f"  recorded operations: {available}"
            )
        return found

    @classmethod
    def load(cls, path: Path) -> Cassette:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2) + "\n", encoding="utf-8")


def raise_recorded(interaction: RecordedInteraction) -> None:
    """Re-raise the error a cassette recorded, if any."""
    if interaction.error is None:
        return

    from ucm_bridge.connectors import errors as error_module

    error_class = getattr(error_module, interaction.error, None)
    if error_class is None:
        raise CassetteMiss(
            f"Cassette names unknown error class {interaction.error!r}; "
            "it must exist in ucm_bridge.connectors.errors"
        )
    message = interaction.error_message or f"recorded {interaction.error}"
    if interaction.error is error_module.RateLimited.__name__:
        raise error_class(message, retry_after_seconds=interaction.retry_after_seconds)
    raise error_class(message, status_code=interaction.status_code)
