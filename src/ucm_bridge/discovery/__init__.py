"""Discovery and estate reporting (§4.1)."""

from ucm_bridge.discovery.service import (
    DiscoveryService,
    EstateReport,
    ModelBreakdown,
    OrphanFinding,
    build_estate_report,
    render_estate_report_markdown,
)

__all__ = [
    "DiscoveryService",
    "EstateReport",
    "ModelBreakdown",
    "OrphanFinding",
    "build_estate_report",
    "render_estate_report_markdown",
]
