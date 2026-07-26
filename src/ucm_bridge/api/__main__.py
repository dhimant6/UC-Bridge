"""``python -m ucm_bridge.api`` — serve the control plane and the console."""

from __future__ import annotations

import argparse
import os


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the UCM-Bridge control plane.")
    parser.add_argument("--host", default=os.environ.get("UCM_BRIDGE_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("PORT", "8000"))
    )
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    try:
        import uvicorn
    except ModuleNotFoundError as exc:  # pragma: no cover - install-time guidance
        raise SystemExit(
            'uvicorn is not installed. Install the API extra: pip install -e ".[api]"'
        ) from exc

    uvicorn.run(
        "ucm_bridge.api.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=os.environ.get("UCM_BRIDGE_LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
