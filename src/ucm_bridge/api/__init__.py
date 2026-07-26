"""FastAPI control plane.

Requires the ``api`` extra::

    pip install -e ".[api,dev]"
    python -m ucm_bridge.api

Serves the JSON API under ``/api`` and, when a built UI is present at
``ucm_bridge/api/static/``, the single-page console at ``/``.
"""

from ucm_bridge.api.app import create_app
from ucm_bridge.api.workspace import StageNotReady, Workspace

__all__ = ["StageNotReady", "Workspace", "create_app"]
