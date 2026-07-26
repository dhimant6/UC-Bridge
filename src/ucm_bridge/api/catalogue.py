"""Every connector's capability manifest and readiness verdict.

The connectors screen exists to answer one question before anyone plans a
migration: *what is this connector actually allowed to do, and how do we know?*
Building each connector here is cheap — a manifest is static data — and it keeps
the answer derived from the connector rather than from a hand-maintained table
that would drift.
"""

from __future__ import annotations

from ucm_bridge.api.scenarios import cassette_dir
from ucm_bridge.connectors.avaya import AvayaAuraConnector
from ucm_bridge.connectors.base import Connector
from ucm_bridge.connectors.capabilities import CapabilityManifest
from ucm_bridge.connectors.cucm import CucmConnector
from ucm_bridge.connectors.genesys import GenesysCloudConnector
from ucm_bridge.connectors.reference import MemoryPBXConnector, MemoryPBXEstate
from ucm_bridge.connectors.sfb import SFB_CMDLETS, SkypeForBusinessConnector
from ucm_bridge.connectors.slack import SlackConnector
from ucm_bridge.connectors.teams import TeamsConnector
from ucm_bridge.vendor.axl import CassetteAxlTransport
from ucm_bridge.vendor.cassette import Cassette
from ucm_bridge.vendor.msgraph import TEAMS_CMDLETS
from ucm_bridge.vendor.powershell import CassettePowerShellBridge
from ucm_bridge.vendor.readiness import ConnectorReadiness
from ucm_bridge.vendor.rest import (
    GENESYS_PAGINATION,
    GRAPH_PAGINATION,
    SLACK_PAGINATION,
    CassetteRestTransport,
)
from ucm_bridge.vendor.sat import CassetteSatSession


def _build_all() -> list[Connector]:
    directory = cassette_dir()
    teams_cassette = Cassette.load(directory / "teams-tenant.json")
    return [
        MemoryPBXConnector(
            MemoryPBXEstate(instance_id="memorypbx-catalogue"),
            tenant_id="contoso",
        ),
        CucmConnector(
            CassetteAxlTransport(Cassette.load(directory / "cucm-discovery.json")),
            instance_id="cluster-muc-1",
            tenant_id="contoso",
        ),
        TeamsConnector(
            graph=CassetteRestTransport(
                teams_cassette,
                base_url="https://graph.microsoft.com/v1.0",
                pagination=GRAPH_PAGINATION,
            ),
            powershell=CassettePowerShellBridge(TEAMS_CMDLETS, teams_cassette),
            instance_id="contoso.onmicrosoft.com",
            tenant_id="contoso",
        ),
        AvayaAuraConnector(
            sat=CassetteSatSession({}),
            instance_id="cm-muc-01",
            tenant_id="contoso",
        ),
        SkypeForBusinessConnector(
            powershell=CassettePowerShellBridge(
                SFB_CMDLETS, Cassette.load(directory / "sfb-topology.json")
            ),
            instance_id="sfb-muc",
            tenant_id="contoso",
        ),
        SlackConnector(
            api=CassetteRestTransport(
                Cassette.load(directory / "slack-workspace.json"),
                base_url="https://slack.com/api",
                pagination=SLACK_PAGINATION,
            ),
            instance_id="contoso-workspace",
            tenant_id="contoso",
        ),
        GenesysCloudConnector(
            api=CassetteRestTransport(
                Cassette.load(directory / "genesys-org.json"),
                base_url="https://api.mypurecloud.com",
                pagination=GENESYS_PAGINATION,
            ),
            instance_id="contoso-genesys",
            tenant_id="contoso",
        ),
    ]


class ConnectorCatalogue:
    """Manifests and readiness for every connector in the build."""

    def __init__(self) -> None:
        self._manifests: dict[str, CapabilityManifest] = {}
        self._readiness: dict[str, ConnectorReadiness] = {}
        for connector in _build_all():
            self._manifests[connector.connector_id] = connector.capabilities()
            self._readiness[connector.connector_id] = connector.readiness()

    @property
    def connector_ids(self) -> list[str]:
        return list(self._manifests)

    def manifest(self, connector_id: str) -> CapabilityManifest:
        return self._manifests[connector_id]

    def readiness(self, connector_id: str) -> ConnectorReadiness:
        return self._readiness[connector_id]

    def entries(self) -> list[tuple[CapabilityManifest, ConnectorReadiness]]:
        return [(self._manifests[cid], self._readiness[cid]) for cid in self._manifests]
