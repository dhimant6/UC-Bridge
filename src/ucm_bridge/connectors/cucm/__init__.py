"""Cisco Unified Communications Manager connector."""

from ucm_bridge.connectors.cucm.connector import (
    CONNECTOR_ID,
    CONNECTOR_VERSION,
    CucmConnector,
    rows,
)

__all__ = ["CONNECTOR_ID", "CONNECTOR_VERSION", "CucmConnector", "rows"]
