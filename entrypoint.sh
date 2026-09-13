#!/bin/sh
set -eu

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
LOG_LEVEL="$(echo "${LOG_LEVEL:-info}" | tr '[:upper:]' '[:lower:]')"

start_uvicorn() {
    p="$1"
    uvicorn miriam_agent.cli:app --host "$HOST" --port "$p" --log-level "$LOG_LEVEL" &
}

start_uvicorn "$PORT"
if [ "$PORT" != "3000" ]; then
    start_uvicorn 3000
fi

trap 'kill $(jobs -p) 2>/dev/null' TERM INT
wait