# One container: FastAPI serves the JSON API and the built console from the same
# origin, so there is no CORS surface in production and nothing to coordinate
# between two deployments.
#
# Stage 1 builds the UI. Stage 2 is Python only — node never reaches the runtime
# image, which keeps it at roughly a fifth of the size.

FROM node:22-alpine AS ui
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci
COPY ui/ ./
# vite.config.ts writes to ../src/ucm_bridge/api/static, which from /ui is
# /src/ucm_bridge/api/static.
RUN npm run build


FROM python:3.12-slim

# Hugging Face Spaces runs the container as uid 1000 with nothing writable at /,
# so the app lives in a directory that uid owns.
RUN useradd --create-home --uid 1000 app
WORKDIR /home/app

COPY --chown=app:app pyproject.toml README.md ./
COPY --chown=app:app src/ ./src/
# The cassettes are the demo's data. They live under tests/ because that is what
# they are — recorded fixtures — and the API replays the same ones rather than
# keeping a second copy that could drift.
COPY --chown=app:app tests/cassettes/ ./tests/cassettes/
COPY --chown=app:app --from=ui /src/ucm_bridge/api/static/ ./src/ucm_bridge/api/static/

# Install for the dependencies. PYTHONPATH then points at the source tree rather
# than site-packages, so the built assets are served from where they were just
# copied instead of depending on the wheel builder deciding to include them.
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir ".[api]"

USER app

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/home/app/src \
    UCM_BRIDGE_CASSETTE_DIR=/home/app/tests/cassettes \
    PORT=7860

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/api/health').read()"

# 0.0.0.0, not 127.0.0.1: the platform's router sits in a different network
# namespace and would not otherwise reach the process.
CMD ["sh", "-c", "uvicorn ucm_bridge.api.app:app --host 0.0.0.0 --port ${PORT}"]
