"""Connection profiles: which real system a connector talks to, and how.

A profile is the *address* of a system plus a reference to its credential. It
never holds a secret — only a :class:`CredentialRef` that a
:class:`CredentialBroker` resolves at call time, so a profile is safe to commit,
print, and put on a screen.

Two modes, and the difference between them is only which transport gets built:

``DEMO``
    Cassette transports. Reaches no network, holds no credential. The default,
    and what an unconfigured process does.

``LIVE``
    Real transports against real systems.

**The mode does not touch the readiness gate.** Live mode makes a connector
*eligible* to be production-ready; verified API surfaces and non-synthetic
cassettes are still what make it *allowed*. A switch that also bypassed the gate
would delete the property the rest of this codebase exists to protect, so the
gate is evaluated exactly the same way in both modes — see
``vendor.readiness.assess_readiness``.

What live mode *does* change is that a connector built against a live transport
reports no synthetic cassettes, because there are none: it is reading the real
system. That is what lifts LAB_ONLY, and it lifts by itself.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ucm_bridge.connectors.base import Connector
from ucm_bridge.connectors.credentials import (
    CredentialBroker,
    CredentialProvider,
    CredentialRef,
    CredentialScope,
    EnvCredentialProvider,
    LocalFileCredentialProvider,
)

MODE_ENV = "UCM_BRIDGE_MODE"
CONNECTIONS_ENV = "UCM_BRIDGE_CONNECTIONS"
STATE_DIR_ENV = "UCM_BRIDGE_STATE_DIR"


class RuntimeMode(StrEnum):
    DEMO = "DEMO"
    """Cassette transports. No network, no credentials. The default."""

    LIVE = "LIVE"
    """Real transports against real systems."""


def current_mode() -> RuntimeMode:
    """The process-wide mode.

    Defaults to DEMO, and an unrecognised value is DEMO rather than an error:
    a typo in an environment variable must never be the reason something talks
    to a production PBX.
    """
    raw = (os.environ.get(MODE_ENV) or "").strip().upper()
    return RuntimeMode.LIVE if raw == RuntimeMode.LIVE.value else RuntimeMode.DEMO


class TransportKind(StrEnum):
    """How a connector reaches its platform. Decides what the factory builds."""

    CASSETTE = "CASSETTE"
    REST = "REST"
    AXL_SOAP = "AXL_SOAP"
    POWERSHELL = "POWERSHELL"
    SSH_SAT = "SSH_SAT"
    IN_PROCESS = "IN_PROCESS"


#: What each connector needs before it can be pointed at a real system, and
#: whether that transport exists yet. Keyed by connector_id.
#:
#: ``implemented`` is not a judgement about difficulty — it records whether the
#: transport can execute today. ``SidecarPowerShellBridge`` and
#: ``SshSatSession`` raise NotImplementedError with a message naming what has to
#: be decided first, because a plausible-looking client for a server nobody has
#: built is worse than an honest refusal.
TRANSPORT_SUPPORT: dict[str, dict[TransportKind, bool]] = {
    "cisco-cucm": {TransportKind.AXL_SOAP: True},
    "microsoft-teams": {TransportKind.REST: True, TransportKind.POWERSHELL: False},
    "microsoft-sfb-server": {TransportKind.POWERSHELL: False},
    "avaya-aura": {TransportKind.SSH_SAT: False, TransportKind.REST: True},
    "slack": {TransportKind.REST: True},
    "genesys-cloud": {TransportKind.REST: True},
    "reference-memorypbx": {TransportKind.IN_PROCESS: True},
}


class LiveEndpoint(BaseModel):
    """Where a live transport connects. Never a secret."""

    model_config = ConfigDict(extra="forbid")

    transport: TransportKind
    #: Base URL for REST, the AXL host for SOAP, the CM host for SAT, or the
    #: sidecar's URL for PowerShell.
    address: str | None = None
    #: AXL schema version, Genesys region, Slack enterprise id — whatever the
    #: transport needs that is not the address.
    options: dict[str, str] = Field(default_factory=dict)
    verify_tls: bool = Field(
        default=True,
        description="Turning this off is a decision someone has to make explicitly, "
        "and it is recorded on the profile where a reviewer can see it.",
    )


class ConnectionProfile(BaseModel):
    """One system this platform can talk to."""

    model_config = ConfigDict(extra="forbid")

    connection_id: str = Field(description="Stable id, e.g. 'cucm-muc-prod'.")
    connector_id: str = Field(description="Which connector drives it, e.g. 'cisco-cucm'.")
    display_name: str
    tenant_id: str
    instance_id: str = Field(
        description="Which instance of the platform: a cluster, a tenant, a workspace."
    )
    mode: RuntimeMode = RuntimeMode.DEMO
    #: Present when mode is LIVE. Absent in demo, because a cassette has no address.
    endpoint: LiveEndpoint | None = None
    #: Resolved through the broker at call time. Holds no secret material.
    credential: CredentialRef | None = None
    #: A source-side connection should hold READ_ONLY, and this is checked
    #: against the credential's own scope so the two cannot disagree silently.
    intended_use: CredentialScope = CredentialScope.READ_ONLY
    notes: str | None = None

    @model_validator(mode="after")
    def _live_needs_an_address(self) -> ConnectionProfile:
        if self.mode is RuntimeMode.LIVE:
            if self.endpoint is None:
                raise ValueError(
                    f"Connection {self.connection_id!r} is LIVE but has no endpoint. "
                    "A live connection must say what it connects to."
                )
            if self.credential is None:
                raise ValueError(
                    f"Connection {self.connection_id!r} is LIVE but references no credential. "
                    "Anonymous access to a production PBX is not a supported configuration."
                )
            if self.credential.scope is CredentialScope.READ_ONLY and (
                self.intended_use is CredentialScope.READ_WRITE
            ):
                raise ValueError(
                    f"Connection {self.connection_id!r} is intended for writes but its "
                    f"credential {self.credential.path!r} is READ_ONLY. Fix the credential "
                    "scope rather than the intent."
                )
        return self

    @property
    def is_live(self) -> bool:
        return self.mode is RuntimeMode.LIVE

    def transport_kind(self) -> TransportKind:
        if self.endpoint is not None:
            return self.endpoint.transport
        return TransportKind.CASSETTE

    def blocking_reason(self) -> str | None:
        """Why this profile cannot be used live yet, if it cannot.

        Separate from the readiness gate: this is about whether the *client*
        exists, not whether the connector has earned the right to write.
        """
        if not self.is_live:
            return None
        support = TRANSPORT_SUPPORT.get(self.connector_id, {})
        kind = self.transport_kind()
        if support.get(kind) is False:
            return (
                f"The {kind.value} transport for {self.connector_id} is declared but not "
                "implemented. It raises NotImplementedError naming what has to be decided "
                "first, rather than shipping a guessed client."
            )
        if kind not in support:
            return (
                f"{self.connector_id} has no {kind.value} transport. Supported: "
                f"{sorted(k.value for k in support)}."
            )
        return None


class ConnectionRegistry(BaseModel):
    """Every system this deployment knows how to reach."""

    model_config = ConfigDict(extra="forbid")

    profiles: list[ConnectionProfile] = Field(default_factory=list)

    @model_validator(mode="after")
    def _ids_are_unique(self) -> ConnectionRegistry:
        seen = [p.connection_id for p in self.profiles]
        duplicates = sorted({i for i in seen if seen.count(i) > 1})
        if duplicates:
            raise ValueError(f"Duplicate connection_id(s): {duplicates}")
        return self

    def get(self, connection_id: str) -> ConnectionProfile:
        for profile in self.profiles:
            if profile.connection_id == connection_id:
                return profile
        raise KeyError(
            f"No connection {connection_id!r}. Known: "
            f"{[p.connection_id for p in self.profiles]}"
        )

    def for_connector(self, connector_id: str) -> list[ConnectionProfile]:
        return [p for p in self.profiles if p.connector_id == connector_id]

    @property
    def live_profiles(self) -> list[ConnectionProfile]:
        return [p for p in self.profiles if p.is_live]


def load_registry(path: Path | None = None) -> ConnectionRegistry:
    """Read the connection file, or return an empty registry.

    An absent file is not an error: a process with no connections configured is
    a demo process, which is the default and the safe one.
    """
    location = path or (
        Path(os.environ[CONNECTIONS_ENV]) if os.environ.get(CONNECTIONS_ENV) else None
    )
    if location is None or not location.is_file():
        return ConnectionRegistry()
    return ConnectionRegistry.model_validate_json(location.read_text(encoding="utf-8"))


def build_broker(providers: list[CredentialProvider] | None = None) -> CredentialBroker:
    """Credential providers in precedence order.

    Environment first because that is what a container gets, then a local file
    for development. ``LocalFileCredentialProvider`` refuses to load unless
    ``UCM_BRIDGE_ENV=dev``, so it cannot quietly become the production backend.
    """
    if providers is not None:
        return CredentialBroker(providers)

    resolved: list[CredentialProvider] = [EnvCredentialProvider()]
    file_path = os.environ.get("UCM_BRIDGE_CREDENTIAL_FILE")
    if file_path:
        resolved.append(
            LocalFileCredentialProvider(
                Path(file_path), environment=os.environ.get("UCM_BRIDGE_ENV")
            )
        )
    return CredentialBroker(resolved)


class TransportNotImplemented(NotImplementedError):
    """A live transport this build declares but has not implemented."""


def describe_live_readiness() -> list[dict[str, Any]]:
    """What each connector needs before it can be pointed at a real system.

    Rendered by the Connections screen. Derived from ``TRANSPORT_SUPPORT`` so it
    cannot drift from what the factory will actually do.
    """
    rows: list[dict[str, Any]] = []
    for connector_id, transports in sorted(TRANSPORT_SUPPORT.items()):
        rows.append(
            {
                "connector_id": connector_id,
                "transports": [
                    {"kind": kind.value, "implemented": implemented}
                    for kind, implemented in sorted(transports.items())
                ],
                "ready_to_connect": all(transports.values()),
            }
        )
    return rows


def state_dir() -> Path | None:
    """Where runs and the audit chain persist, when persistence is configured.

    In-process state is fine for a demo and unacceptable for real writes: a
    restart mid-run would lose the audit chain covering operations that already
    happened.
    """
    raw = os.environ.get(STATE_DIR_ENV)
    if not raw:
        return None
    path = Path(raw)
    path.mkdir(parents=True, exist_ok=True)
    return path


#: A connector factory: given a profile and a broker, build a connector.
ConnectorFactory = Callable[[ConnectionProfile, CredentialBroker], Connector]


__all__ = [
    "CONNECTIONS_ENV",
    "MODE_ENV",
    "STATE_DIR_ENV",
    "TRANSPORT_SUPPORT",
    "ConnectionProfile",
    "ConnectionRegistry",
    "ConnectorFactory",
    "LiveEndpoint",
    "RuntimeMode",
    "TransportKind",
    "TransportNotImplemented",
    "build_broker",
    "current_mode",
    "describe_live_readiness",
    "load_registry",
    "state_dir",
]
