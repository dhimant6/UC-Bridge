"""Build a connector from a connection profile.

The only place that decides between a cassette and a live transport, so the only
place to read when asking "could this possibly have touched a real system?".

Two rules it enforces:

**A live connector declares its cassettes non-synthetic**, because it has none —
it is reading the real platform. That is what lifts ``LAB_ONLY``, and it lifts on
its own rather than through a flag. The readiness gate itself is identical in
both modes.

**An unimplemented transport refuses loudly**, naming what would have to be built
and decided, rather than returning a client that fails later against a production
system.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from ucm_bridge.api.connections import (
    ConnectionProfile,
    RuntimeMode,
    TransportKind,
    TransportNotImplemented,
)
from ucm_bridge.connectors.base import Connector
from ucm_bridge.connectors.credentials import CredentialBroker
from ucm_bridge.connectors.cucm import CucmConnector
from ucm_bridge.connectors.errors import CredentialError
from ucm_bridge.connectors.genesys import GenesysCloudConnector
from ucm_bridge.connectors.slack import SlackConnector
from ucm_bridge.vendor.axl import KNOWN_SCHEMA_VERSIONS, ZeepAxlTransport
from ucm_bridge.vendor.rest import (
    GENESYS_PAGINATION,
    GRAPH_PAGINATION,
    SLACK_PAGINATION,
    HttpxRestTransport,
    PaginationStyle,
)

#: Base URLs used when a profile does not override them, and a profile always
#: may: Genesys is regional, and a Teams tenant in a sovereign cloud is not on
#: graph.microsoft.com.
DEFAULT_BASE_URL: dict[str, str] = {
    "microsoft-teams": "https://graph.microsoft.com/v1.0",
    "slack": "https://slack.com/api",
    "genesys-cloud": "https://api.mypurecloud.com",
}

PAGINATION: dict[str, PaginationStyle] = {
    "microsoft-teams": GRAPH_PAGINATION,
    "slack": SLACK_PAGINATION,
    "genesys-cloud": GENESYS_PAGINATION,
}

#: Fields a bearer token might arrive under, most specific first.
TOKEN_FIELDS = ("token", "access_token", "client_secret", "password")


def _token_provider(
    profile: ConnectionProfile, broker: CredentialBroker
) -> Callable[[], Awaitable[str]]:
    """Resolve the bearer token per call rather than once at construction.

    Tokens expire. A connector that caches one for the length of a migration run
    fails halfway through a wave, which is the worst possible time.
    """
    ref = profile.credential
    assert ref is not None  # guaranteed by ConnectionProfile's validator

    async def provide() -> str:
        bundle = await broker.resolve(ref, tenant_id=profile.tenant_id)
        for field in TOKEN_FIELDS:
            value = bundle.values.get(field)
            if value:
                return value
        raise CredentialError(
            f"Credential {ref.path!r} carries none of {list(TOKEN_FIELDS)}, so the REST "
            f"transport for {profile.connection_id!r} has nothing to authenticate with."
        )

    return provide


def _rest_transport(profile: ConnectionProfile, broker: CredentialBroker) -> HttpxRestTransport:
    endpoint = profile.endpoint
    assert endpoint is not None
    base_url = endpoint.address or DEFAULT_BASE_URL.get(profile.connector_id)
    if not base_url:
        raise TransportNotImplemented(
            f"Connection {profile.connection_id!r} has no address and there is no default "
            f"base URL for {profile.connector_id}."
        )
    return HttpxRestTransport(
        base_url=base_url,
        pagination=PAGINATION.get(profile.connector_id, PaginationStyle()),
        token_provider=_token_provider(profile, broker),
    )


def _refuse(profile: ConnectionProfile, what: str, because: str) -> TransportNotImplemented:
    return TransportNotImplemented(
        f"Connection {profile.connection_id!r} asks for {what}, which this build declares "
        f"but has not implemented. {because} Until then the connection can be configured "
        "and inspected, but not opened."
    )


async def build_live_connector(
    profile: ConnectionProfile, broker: CredentialBroker
) -> Connector:
    """Build a connector wired to the real system named by ``profile``.

    Async because credentials resolve through the broker, and CUCM needs them
    before the SOAP client can be constructed at all.
    """
    if profile.mode is not RuntimeMode.LIVE:
        raise ValueError(
            f"{profile.connection_id!r} is a {profile.mode.value} profile; "
            "build_live_connector is for LIVE profiles only."
        )

    endpoint = profile.endpoint
    assert endpoint is not None
    kind = endpoint.transport
    common: dict[str, Any] = {
        "instance_id": profile.instance_id,
        "tenant_id": profile.tenant_id,
        "credential_ref": profile.credential,
        "credentials": broker,
        # There is no cassette. This is what lifts LAB_ONLY, and it lifts because
        # the connector genuinely reads a real system, not because a flag was set.
        "cassette_is_synthetic": False,
    }

    if profile.connector_id == "cisco-cucm":
        return await _build_cucm(profile, broker, common)

    if profile.connector_id == "microsoft-teams":
        # Graph covers extraction. Every Teams *write* is a cmdlet —
        # Set-CsPhoneNumberAssignment — so it needs the sidecar that does not exist.
        raise _refuse(
            profile,
            "a live Teams connector",
            "Graph extraction would work over the REST transport, but every Teams write is a "
            "PowerShell cmdlet and SidecarPowerShellBridge is unimplemented: a containerised "
            "PowerShell 7 service and the HTTP contract it speaks have to be built and agreed "
            "first. This is the single blocker on migrating anything into Teams.",
        )

    if profile.connector_id == "microsoft-sfb-server":
        raise _refuse(
            profile,
            "a live Skype for Business connector",
            "SfB is remote PowerShell over WinRM, and SidecarPowerShellBridge is unimplemented.",
        )

    if profile.connector_id == "avaya-aura":
        raise _refuse(
            profile,
            "a live Avaya Aura connector",
            "Communication Manager is SAT over SSH and SshSatSession is unimplemented: the "
            "terminal dialogue, its pagination and its timeouts have to be pinned against a "
            "real CM first. System Manager REST is additionally unverified, so Aura would stay "
            "UNVERIFIED even once SSH works.",
        )

    if profile.connector_id == "slack":
        if kind is not TransportKind.REST:
            raise _refuse(profile, f"a {kind.value} transport for Slack", "Slack is REST.")
        return SlackConnector(api=_rest_transport(profile, broker), **common)

    if profile.connector_id == "genesys-cloud":
        if kind is not TransportKind.REST:
            raise _refuse(profile, f"a {kind.value} transport for Genesys", "Genesys is REST.")
        return GenesysCloudConnector(api=_rest_transport(profile, broker), **common)

    raise TransportNotImplemented(f"No live factory for connector {profile.connector_id!r}.")


async def _build_cucm(
    profile: ConnectionProfile, broker: CredentialBroker, common: dict[str, Any]
) -> Connector:
    endpoint = profile.endpoint
    assert endpoint is not None
    if endpoint.transport is not TransportKind.AXL_SOAP:
        raise _refuse(
            profile, f"a {endpoint.transport.value} transport for CUCM", "CUCM speaks AXL."
        )

    # The WSDL is per release and is downloaded from the publisher's admin UI.
    # zeep reads it rather than assuming field names, which is the whole reason
    # this transport is trustworthy, so its absence is a hard error.
    wsdl_path = endpoint.options.get("wsdl_path")
    if not wsdl_path:
        raise TransportNotImplemented(
            f"Connection {profile.connection_id!r} has no 'wsdl_path' option. The AXL "
            "transport reads the WSDL for the negotiated schema version instead of assuming "
            "field names; download AXLAPI.wsdl for your CUCM release from Cisco Unified CM "
            "Administration → Application → Plugins, and point wsdl_path at it."
        )

    schema_version = endpoint.options.get("schema_version", "14.0")
    if schema_version not in KNOWN_SCHEMA_VERSIONS:
        raise TransportNotImplemented(
            f"AXL schema version {schema_version!r} has not been reviewed for this connector. "
            f"Known: {list(KNOWN_SCHEMA_VERSIONS)}. Negotiate with getCCMVersion and add the "
            "version deliberately rather than guessing."
        )

    ref = profile.credential
    assert ref is not None
    bundle = await broker.resolve(ref, tenant_id=profile.tenant_id)
    transport = ZeepAxlTransport(
        host=endpoint.address or profile.instance_id,
        username=bundle.require("username"),
        password=bundle.require("password"),
        wsdl_path=wsdl_path,
        schema_version=schema_version,
        verify_tls=endpoint.verify_tls,
    )
    return CucmConnector(transport, **common)


def unimplemented_reason(profile: ConnectionProfile) -> str | None:
    """Why this connection cannot be opened, without trying to open it.

    Derived from the same table the factory branches on, so the Connections
    screen cannot promise something ``build_live_connector`` would refuse.
    """
    if not profile.is_live:
        return None
    return profile.blocking_reason()


__all__ = ["build_live_connector", "unimplemented_reason"]
