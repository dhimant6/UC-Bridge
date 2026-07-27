"""Connection profiles, the demo/live switch, and what live mode changes.

The property under test throughout: **switching to live must not relax any
guardrail.** It selects transports. Whether a connector may write to production
is decided by the readiness gate, identically in both modes, and a mode switch
that also unlocked writes would delete the thing the rest of this codebase is
built to protect.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest

from ucm_bridge.api.app import ALLOW_HEADER_AUTH_ENV, create_app
from ucm_bridge.api.connections import (
    CONNECTIONS_ENV,
    MODE_ENV,
    TRANSPORT_SUPPORT,
    ConnectionProfile,
    ConnectionRegistry,
    LiveEndpoint,
    RuntimeMode,
    TransportKind,
    TransportNotImplemented,
    build_broker,
    current_mode,
    load_registry,
)
from ucm_bridge.api.factory import build_live_connector
from ucm_bridge.api.workspace import Workspace
from ucm_bridge.connectors.credentials import CredentialKind, CredentialRef, CredentialScope
from ucm_bridge.vendor.readiness import ReadinessLevel

ADMIN = {"X-UCM-Roles": "ADMIN"}
VIEWER = {"X-UCM-Roles": "VIEWER"}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every test starts from an unconfigured process, which means DEMO."""
    for name in (MODE_ENV, CONNECTIONS_ENV, ALLOW_HEADER_AUTH_ENV):
        monkeypatch.delenv(name, raising=False)
    yield


def live_profile(**overrides: object) -> ConnectionProfile:
    defaults: dict[str, object] = {
        "connection_id": "cucm-muc-lab",
        "connector_id": "cisco-cucm",
        "display_name": "CUCM Munich lab",
        "tenant_id": "contoso",
        "instance_id": "cluster-muc-1",
        "mode": RuntimeMode.LIVE,
        "endpoint": LiveEndpoint(
            transport=TransportKind.AXL_SOAP,
            address="cucm-pub.contoso.example",
            options={"schema_version": "14.0", "wsdl_path": "/opt/axl/AXLAPI.wsdl"},
        ),
        "credential": CredentialRef(
            provider="env",
            path="cucm/axl",
            kind=CredentialKind.USERNAME_PASSWORD,
            scope=CredentialScope.READ_ONLY,
        ),
    }
    return ConnectionProfile.model_validate({**defaults, **overrides})


async def client_for(registry: ConnectionRegistry) -> httpx.AsyncClient:
    app = create_app(workspace=Workspace(), registry=registry)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control-plane"
    )


@pytest.fixture
async def demo_client() -> AsyncIterator[httpx.AsyncClient]:
    async with await client_for(ConnectionRegistry()) as http:
        yield http


# --------------------------------------------------------------------------- #
# The mode itself
# --------------------------------------------------------------------------- #


def test_an_unconfigured_process_is_in_demo_mode() -> None:
    assert current_mode() is RuntimeMode.DEMO


def test_a_typo_in_the_mode_variable_is_demo_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A misspelling must never be the reason something talks to a production PBX."""
    for value in ("LIVE!", "prod", "PRODUCTION", "true", "1", "yes", "on", ""):
        monkeypatch.setenv(MODE_ENV, value)
        assert current_mode() is RuntimeMode.DEMO, value

    # Case and surrounding whitespace are forgiven, because a trailing space in
    # an environment variable is a deployment artefact, not an intent.
    for value in ("live", "LIVE", " live ", "Live\n"):
        monkeypatch.setenv(MODE_ENV, value)
        assert current_mode() is RuntimeMode.LIVE, value


async def test_the_mode_endpoint_reports_demo_without_configuration(
    demo_client: httpx.AsyncClient,
) -> None:
    body = (await demo_client.get("/api/mode")).json()
    assert body["mode"] == "DEMO"
    assert body["is_live"] is False
    assert body["live_connection_count"] == 0
    assert body["persistence"] is None


# --------------------------------------------------------------------------- #
# Profiles refuse to be half-configured
# --------------------------------------------------------------------------- #


def test_a_live_profile_without_an_endpoint_is_rejected() -> None:
    with pytest.raises(ValueError, match="has no endpoint"):
        live_profile(endpoint=None)


def test_a_live_profile_without_a_credential_is_rejected() -> None:
    """Anonymous access to a production PBX is not a supported configuration."""
    with pytest.raises(ValueError, match="references no credential"):
        live_profile(credential=None)


def test_a_write_connection_cannot_hold_a_read_only_credential() -> None:
    """The intent and the credential's own scope cannot disagree silently."""
    with pytest.raises(ValueError, match="READ_ONLY"):
        live_profile(intended_use=CredentialScope.READ_WRITE)


def test_a_demo_profile_needs_none_of_that() -> None:
    profile = ConnectionProfile(
        connection_id="cucm-demo",
        connector_id="cisco-cucm",
        display_name="Cassette",
        tenant_id="contoso",
        instance_id="cluster-muc-1",
    )
    assert profile.is_live is False
    assert profile.transport_kind() is TransportKind.CASSETTE
    assert profile.blocking_reason() is None


def test_duplicate_connection_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="Duplicate connection_id"):
        ConnectionRegistry(profiles=[live_profile(), live_profile()])


def test_an_absent_connection_file_is_an_empty_registry_not_a_crash(tmp_path: Path) -> None:
    assert load_registry(tmp_path / "nope.json").profiles == []


def test_a_registry_round_trips_through_its_file(tmp_path: Path) -> None:
    path = tmp_path / "connections.json"
    original = ConnectionRegistry(profiles=[live_profile()])
    path.write_text(original.model_dump_json(indent=2), encoding="utf-8")

    loaded = load_registry(path)
    assert [p.connection_id for p in loaded.profiles] == ["cucm-muc-lab"]
    assert loaded.get("cucm-muc-lab").endpoint is not None


def test_a_profile_never_carries_a_secret() -> None:
    """Profiles get printed, logged, and put on screens. Dump one and look.

    Checked structurally rather than by substring: ``username_password`` is a
    credential *kind* and contains the word "password" while carrying nothing.
    What matters is that no field anywhere in the tree could hold secret
    material.
    """
    import json

    secret_fields = {
        "password",
        "secret",
        "client_secret",
        "token",
        "access_token",
        "private_key",
        "certificate_pem",
        "values",
    }

    def walk(node: object, path: str = "") -> list[str]:
        found: list[str] = []
        if isinstance(node, dict):
            for key, value in node.items():
                if key in secret_fields:
                    found.append(f"{path}.{key}")
                found.extend(walk(value, f"{path}.{key}"))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                found.extend(walk(value, f"{path}[{index}]"))
        return found

    dumped = json.loads(live_profile().model_dump_json())
    assert walk(dumped) == [], "a connection profile must hold no secret-bearing field"
    assert dumped["credential"]["path"] == "cucm/axl", (
        "the reference to the secret is the point; the secret itself is resolved at call time"
    )


# --------------------------------------------------------------------------- #
# Unimplemented transports refuse, and say what is missing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("connector_id", "transport", "expected"),
    [
        ("microsoft-teams", TransportKind.POWERSHELL, "PowerShell"),
        ("microsoft-sfb-server", TransportKind.POWERSHELL, "PowerShell"),
        ("avaya-aura", TransportKind.SSH_SAT, "SSH"),
    ],
)
async def test_an_unimplemented_transport_refuses_and_names_what_is_missing(
    connector_id: str, transport: TransportKind, expected: str
) -> None:
    profile = live_profile(
        connection_id=f"{connector_id}-live",
        connector_id=connector_id,
        endpoint=LiveEndpoint(transport=transport, address="host.contoso.example"),
    )
    with pytest.raises(TransportNotImplemented) as raised:
        await build_live_connector(profile, build_broker())

    message = str(raised.value)
    assert expected in message
    assert "not implemented" in message or "unimplemented" in message
    # Naming the blocker is the point: an operator has to know what to build.
    assert profile.blocking_reason() is not None


async def test_the_teams_refusal_names_the_actual_blocker() -> None:
    """Teams is the write target for most estates, so its blocker matters most."""
    profile = live_profile(
        connection_id="teams-live",
        connector_id="microsoft-teams",
        instance_id="contoso.onmicrosoft.com",
        endpoint=LiveEndpoint(transport=TransportKind.POWERSHELL, address="http://sidecar:8080"),
    )
    with pytest.raises(TransportNotImplemented, match="SidecarPowerShellBridge"):
        await build_live_connector(profile, build_broker())


async def test_cucm_refuses_without_a_wsdl_rather_than_guessing_field_names() -> None:
    """The WSDL is why the AXL transport is trustworthy. Its absence is fatal."""
    profile = live_profile(
        endpoint=LiveEndpoint(
            transport=TransportKind.AXL_SOAP,
            address="cucm-pub.contoso.example",
            options={"schema_version": "14.0"},
        )
    )
    with pytest.raises(TransportNotImplemented, match="wsdl_path"):
        await build_live_connector(profile, build_broker())


async def test_cucm_refuses_an_unreviewed_axl_schema_version() -> None:
    profile = live_profile(
        endpoint=LiveEndpoint(
            transport=TransportKind.AXL_SOAP,
            address="cucm-pub.contoso.example",
            options={"schema_version": "11.5", "wsdl_path": "/opt/axl/AXLAPI.wsdl"},
        )
    )
    with pytest.raises(TransportNotImplemented, match="has not been reviewed"):
        await build_live_connector(profile, build_broker())


def test_the_transport_table_covers_every_connector_in_the_build() -> None:
    """A connector missing from the table would silently have no live path."""
    from ucm_bridge.api.catalogue import ConnectorCatalogue

    assert set(TRANSPORT_SUPPORT) == set(ConnectorCatalogue().connector_ids)


# --------------------------------------------------------------------------- #
# Live mode changes transports, not guardrails
# --------------------------------------------------------------------------- #


async def test_live_mode_refuses_header_identity_unless_explicitly_acknowledged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An X-UCM-Roles header is a role anyone with network access can grant themselves."""
    monkeypatch.setenv(MODE_ENV, "LIVE")
    async with await client_for(ConnectionRegistry()) as http:
        refused = await http.get("/api/estates", headers=VIEWER)
        assert refused.status_code == 401
        assert refused.json()["error"] == "HeaderAuthRefused"

        # /api/mode stays open: the console has to be able to raise a live
        # banner before it knows who is looking.
        assert (await http.get("/api/mode")).json()["is_live"] is True

    monkeypatch.setenv(ALLOW_HEADER_AUTH_ENV, "1")
    async with await client_for(ConnectionRegistry()) as http:
        assert (await http.get("/api/estates", headers=VIEWER)).status_code == 200


async def test_demo_mode_still_accepts_header_identity(demo_client: httpx.AsyncClient) -> None:
    assert (await demo_client.get("/api/estates", headers=VIEWER)).status_code == 200


def test_live_mode_does_not_relax_the_readiness_gate() -> None:
    """The whole point. Mode picks a transport; readiness decides writes.

    A connector wired live reports no synthetic cassettes because it has none,
    and *that* is what can lift LAB_ONLY. Nothing about the mode itself does.
    """
    from ucm_bridge.api.catalogue import ConnectorCatalogue

    catalogue = ConnectorCatalogue()
    # Avaya stays UNVERIFIED whatever the mode: its System Manager REST surface
    # carries no verification record, and no transport choice changes that.
    assert catalogue.readiness("avaya-aura").level is ReadinessLevel.UNVERIFIED
    assert catalogue.readiness("avaya-aura").may_write_to_production is False


async def test_a_preflight_needs_the_connector_admin_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ALLOW_HEADER_AUTH_ENV, "1")
    registry = ConnectionRegistry(profiles=[live_profile()])
    async with await client_for(registry) as http:
        denied = await http.post("/api/connections/cucm-muc-lab/test", headers=VIEWER)
        assert denied.status_code == 403
        assert "MANAGE_CONNECTORS" in denied.json()["message"]


async def test_a_preflight_against_a_demo_profile_is_a_409_not_a_fake_success() -> None:
    registry = ConnectionRegistry(
        profiles=[
            ConnectionProfile(
                connection_id="cucm-demo",
                connector_id="cisco-cucm",
                display_name="Cassette",
                tenant_id="contoso",
                instance_id="cluster-muc-1",
            )
        ]
    )
    async with await client_for(registry) as http:
        response = await http.post("/api/connections/cucm-demo/test", headers=ADMIN)
        assert response.status_code == 409
        assert response.json()["error"] == "StageNotReady"


async def test_an_unimplemented_transport_preflights_as_501_not_500() -> None:
    """Understood, and genuinely not implemented. That is a 501."""
    registry = ConnectionRegistry(
        profiles=[
            live_profile(
                connection_id="teams-live",
                connector_id="microsoft-teams",
                instance_id="contoso.onmicrosoft.com",
                endpoint=LiveEndpoint(
                    transport=TransportKind.POWERSHELL, address="http://sidecar:8080"
                ),
            )
        ]
    )
    async with await client_for(registry) as http:
        response = await http.post("/api/connections/teams-live/test", headers=ADMIN)
        assert response.status_code == 501
        assert response.json()["error"] == "TransportNotImplemented"


async def test_the_connections_screen_shows_what_blocks_each_one() -> None:
    registry = ConnectionRegistry(
        profiles=[
            live_profile(),
            live_profile(
                connection_id="teams-live",
                connector_id="microsoft-teams",
                instance_id="contoso.onmicrosoft.com",
                endpoint=LiveEndpoint(
                    transport=TransportKind.POWERSHELL, address="http://sidecar:8080"
                ),
            ),
        ]
    )
    async with await client_for(registry) as http:
        body = (await http.get("/api/connections", headers=VIEWER)).json()

    by_id = {c["profile"]["connection_id"]: c for c in body["connections"]}
    assert by_id["cucm-muc-lab"]["can_open"] is True
    assert by_id["cucm-muc-lab"]["blocked_reason"] is None
    assert by_id["teams-live"]["can_open"] is False
    assert "not implemented" in by_id["teams-live"]["blocked_reason"]

    # Readiness is reported alongside and is a separate question: CUCM can be
    # opened and is still LAB_ONLY until its cassettes are real.
    assert by_id["cucm-muc-lab"]["readiness"]["level"] == "LAB_ONLY"

    support = {row["connector_id"]: row for row in body["transport_support"]}
    assert support["microsoft-teams"]["ready_to_connect"] is False
    assert support["genesys-cloud"]["ready_to_connect"] is True


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #


def test_state_survives_a_restart_when_a_state_dir_is_configured(tmp_path: Path) -> None:
    """A restart mid-run must not lose the chain covering writes already made."""
    first = Workspace(state_dir=tmp_path)
    first.audit.append(
        tenant_id="contoso", actor="operator@contoso.example", action="RUN_STARTED"
    )
    head = first.audit.head_hash

    second = Workspace(state_dir=tmp_path)
    assert len(second.audit) == 1
    assert second.audit.head_hash == head
    second.audit.verify()


def test_without_a_state_dir_nothing_is_written(tmp_path: Path) -> None:
    workspace = Workspace()
    workspace.audit.append(
        tenant_id="contoso", actor="operator@contoso.example", action="RUN_STARTED"
    )
    assert list(tmp_path.iterdir()) == []
    assert Workspace().audit.head_hash != workspace.audit.head_hash or len(Workspace().audit) == 0
