"""Reference connector: a worked example of the connector contract."""

from ucm_bridge.connectors.reference.connector import (
    CONNECTOR_ID,
    CONNECTOR_VERSION,
    MemoryPBXConnector,
)
from ucm_bridge.connectors.reference.platform import (
    MemoryPBXEstate,
    MemoryPBXFault,
    build_demo_estate,
)

__all__ = [
    "CONNECTOR_ID",
    "CONNECTOR_VERSION",
    "MemoryPBXConnector",
    "MemoryPBXEstate",
    "MemoryPBXFault",
    "build_demo_estate",
]
