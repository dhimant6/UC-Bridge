"""Genesys Cloud CX connector (contact-centre entities)."""

from ucm_bridge.connectors.genesys.connector import (
    CONNECTOR_ID,
    CONNECTOR_VERSION,
    GenesysCloudConnector,
    rescale_proficiency,
)

__all__ = [
    "CONNECTOR_ID",
    "CONNECTOR_VERSION",
    "GenesysCloudConnector",
    "rescale_proficiency",
]
