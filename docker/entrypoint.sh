#!/bin/sh
# Start the FastAPI backend (loopback only) and Caddy (the single public :8080
# front). tini (PID 1) reaps zombies. If the backend exits, kill Caddy so the
# container stops and the orchestrator/healthcheck notices instead of serving a
# dead API.
set -eu

BACKEND_HOST=127.0.0.1
BACKEND_PORT=8791

echo "lursor: starting backend on ${BACKEND_HOST}:${BACKEND_PORT} (data=${LURSOR_DATA_DIR:-unset}) ..."
uvicorn app.main:app --host "${BACKEND_HOST}" --port "${BACKEND_PORT}" &
backend_pid=$!

# When the backend dies, take the container down with it.
trap 'kill "${backend_pid}" 2>/dev/null || true' INT TERM
( wait "${backend_pid}"; echo "lursor: backend exited — stopping"; kill -TERM 1 2>/dev/null || true ) &

echo "lursor: starting Caddy on :${PORT:-8080} ..."
exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
