"""Slack connector (collaboration entities only)."""

from ucm_bridge.connectors.slack.connector import (
    CONNECTOR_ID,
    CONNECTOR_VERSION,
    ENTERPRISE_GRID,
    UNMAPPABLE_KINDS,
    SlackConnector,
)

__all__ = [
    "CONNECTOR_ID",
    "CONNECTOR_VERSION",
    "ENTERPRISE_GRID",
    "UNMAPPABLE_KINDS",
    "SlackConnector",
]
