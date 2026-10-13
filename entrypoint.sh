#!/bin/sh
set -eu

HOST="${HOST:-0.0.0.0}"
# Port contract (Gap 2). AtlasFlow probes the deployment health check at
# http://<vm>:<PORT>/ every 15s, where PORT defaults to 3000 when the project's
# environment does not set it. This image listens on PORT and defaults it to
# 8000 (matching Dockerfile EXPOSE and docker-compose.yml). An unset PORT
# therefore leaves uvicorn on 8000 while the platform probes 3000 and the
# deploy can never go healthy. Set PORT=8000 in the AtlasFlow environment; the
# boot log below names the port actually used so a mismatch is visible in the
# deploy log instead of silent. Only ONE uvicorn process is started per
# container so the in-process ledger fallback cannot diverge (G22).
# Multi-instance is scaled by replicas, not by forking inside the container.
if [ -z "${PORT:-}" ]; then
  PORT_WAS_SET="PORT was NOT set — defaulting to 8000. AtlasFlow probes PORT and defaults to 3000, so set PORT=8000 in the project environment or the deploy will never go healthy"
else
  PORT_WAS_SET="PORT was set explicitly (${PORT}); confirm the AtlasFlow env sets the same value"
fi
PORT="${PORT:-8000}"
LOG_LEVEL="$(echo "${LOG_LEVEL:-info}" | tr '[:upper:]' '[:lower:]')"
GRACEFUL_TIMEOUT="${GRACEFUL_TIMEOUT:-30}"

echo "[entrypoint] listening on ${HOST}:${PORT} — ${PORT_WAS_SET}" >&2

# Lifespan drain: on SIGTERM/SIGINT, uvicorn handles graceful shutdown
# (drains in-flight requests up to --timeout-graceful-shutdown).
# Migrations are applied by the deploy job (alembic upgrade head) before containers start.
# This entrypoint only starts the app — it does not run migrations.
exec uvicorn miriam_agent.cli:app --host "$HOST" --port "$PORT" --log-level "$LOG_LEVEL" --timeout-graceful-shutdown "$GRACEFUL_TIMEOUT"
