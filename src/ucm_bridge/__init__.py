"""UCM-Bridge: bidirectional Unified Communications migration platform.

Architecture in one line::

    Source Connector -> Canonical UC Model (vendor-neutral IR) -> Target Connector

Reverse migration is the same pipeline with source and target swapped. See
``docs/adr/0001-canonical-model.md``.
"""

from ucm_bridge.canonical.base import CANONICAL_MODEL_VERSION

__version__ = "0.1.0"

__all__ = ["CANONICAL_MODEL_VERSION", "__version__"]
