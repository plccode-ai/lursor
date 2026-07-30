#!/usr/bin/env bash
# Run backend (FastAPI) and frontend together for local development.
# Ctrl-C stops both.
#
# Usage:
#   ./scripts/dev.sh            backend + frontend (Vite) in the browser
#   ./scripts/dev.sh --electron backend + frontend inside the Electron desktop shell
#   ./scripts/dev.sh --electron --debug   ... and auto-open Chrome DevTools
#
# Env overrides:
#   LURSOR_UV_EXTRAS   space-separated backend extras to sync (default "dev",
#                      e.g. "dev hindsight"; set to "" for no extras)
#   LURSOR_SKIP_INSTALL=1  skip the dependency install step
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

FRONTEND_CMD="dev"
FRONTEND_LABEL="frontend on :8899"
ELECTRON=0
DEBUG=0

for arg in "$@"; do
  case "$arg" in
    --electron) ELECTRON=1 ;;
    --debug) DEBUG=1 ;;
    *) echo "error: unknown option '$arg' (expected --electron and/or --debug)" >&2; exit 1 ;;
  esac
done

if [[ "$ELECTRON" == "1" ]]; then
  FRONTEND_CMD="electron:dev"
  FRONTEND_LABEL="frontend (Electron) — Vite on :8899"
fi

# Electron's main process reads this to decide whether to auto-open DevTools.
if [[ "$DEBUG" == "1" ]]; then
  export LURSOR_DEVTOOLS=1
  FRONTEND_LABEL="$FRONTEND_LABEL (DevTools)"
else
  export LURSOR_DEVTOOLS=0
fi

cleanup() {
  trap - INT TERM
  kill 0 2>/dev/null || true
}
trap cleanup INT TERM

if [[ "${LURSOR_SKIP_INSTALL:-}" == "1" ]]; then
  echo "Skipping dependency install (LURSOR_SKIP_INSTALL=1)."
else
  command -v uv >/dev/null 2>&1 || { echo "error: uv is required (https://docs.astral.sh/uv/)" >&2; exit 1; }
  command -v bun >/dev/null 2>&1 || { echo "error: bun is required (https://bun.sh)" >&2; exit 1; }

  echo "Installing backend dependencies ..."
  UV_EXTRA_ARGS=()
  for extra in ${LURSOR_UV_EXTRAS-dev}; do
    UV_EXTRA_ARGS+=(--extra "$extra")
  done
  # --inexact leaves packages that aren't part of the requested extras in place,
  # so a dev run doesn't uninstall optional providers (e.g. the hindsight extra)
  # from an existing venv.
  (cd "$ROOT_DIR/backend" && uv sync --inexact ${UV_EXTRA_ARGS[@]+"${UV_EXTRA_ARGS[@]}"})

  echo "Installing frontend dependencies ..."
  (cd "$ROOT_DIR/frontend" && bun install)
fi

echo "Starting backend on :8791 ..."
# Bind to 0.0.0.0 so the API is reachable from other devices on the LAN (e.g.
# a phone hitting http://<this-machine-ip>:8791), not just localhost.
(cd "$ROOT_DIR/backend" && uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8791) &

echo "Starting $FRONTEND_LABEL ..."
(cd "$ROOT_DIR/frontend" && bun run "$FRONTEND_CMD") &

wait
