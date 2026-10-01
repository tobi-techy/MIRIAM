#!/bin/sh
set -eu

HOST="${HOST:-0.0.0.0}"
# AtlasFlow convention: probing port 3000 must answer; image healthcheck
# and compose probe /health (which is also liveness). Only ONE uvicorn
# process is started per container so the in-process ledger fallback
# cannot diverge (G22). Multi-instance is scaled by replicas, not by
# forking inside the container.
PORT="${PORT:-8000}"
LOG_LEVEL="$(echo "${LOG_LEVEL:-info}" | tr '[:upper:]' '[:lower:]')"
GRACEFUL_TIMEOUT="${GRACEFUL_TIMEOUT:-30}"

# Lifespan drain: on SIGTERM/SIGINT, uvicorn handles graceful shutdown
# (drains in-flight requests up to --timeout-graceful-shutdown).
exec uvicorn miriam_agent.cli:app --host "$HOST" --port "$PORT" --log-level "$LOG_LEVEL" --timeout-graceful-shutdown "$GRACEFUL_TIMEOUT"
