# syntax=docker/dockerfile:1

# Single-origin server image for hosting Lursor (not the Electron desktop build).
# One container serves the SPA and reverse-proxies /api (+ WebSockets) to the
# FastAPI backend behind Caddy on ONE port (8080), so it sits cleanly behind one
# Cloudflare hostname. Designed to run ONE instance per user (LURSOR_DATA_DIR).
#
# Build:  docker build -t lursor .
# Run:    docker run --rm -p 8080:8080 -e OPENROUTER_API_KEY=sk-or-... \
#             -v lursor-data-<user>:/data lursor

# ---------------------------------------------------------------------------
# Stage 1 — build the SPA. node base (so esbuild's + electron's install scripts
# have `node`), bun for the frozen install from bun.lock. VITE_API_BASE=same-origin
# makes the client derive its API base from the page origin at runtime (absolute,
# WebSocket-safe, hostname-agnostic). --base=/ overrides the Electron-oriented
# "./" so assets load under SPA deep-link fallback.
# ---------------------------------------------------------------------------
FROM node:22-bookworm-slim AS frontend
RUN npm install -g bun@1
WORKDIR /app/frontend
ENV ELECTRON_SKIP_BINARY_DOWNLOAD=1
COPY frontend/package.json frontend/bun.lock ./
# The root postinstall (electron/patch-dev-name.cjs) must exist during install;
# it no-ops on non-macOS. Copy it before install to keep the dep layer cached.
COPY frontend/electron ./electron
RUN bun install --frozen-lockfile
COPY frontend/ ./
ENV VITE_API_BASE=same-origin
RUN bunx vite build --base=/
# → /app/frontend/dist

# ---------------------------------------------------------------------------
# Stage 2 — install the backend into a venv with uv (frozen from uv.lock).
# `git` is required both for the pinned git dependency at install time and by the
# app at runtime (diffs/clone/push).
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS backend
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates && rm -rf /var/lib/apt/lists/*
ENV UV_PYTHON_PREFERENCE=system \
    UV_LINK_MODE=copy
WORKDIR /app/backend
# Deps first (cached) — no project yet, so the local package isn't built here.
COPY backend/pyproject.toml backend/uv.lock backend/.python-version ./
RUN uv sync --frozen --no-dev --no-install-project
# Now the source, then install the project itself into the same venv.
COPY backend/ ./
RUN uv sync --frozen --no-dev

# ---------------------------------------------------------------------------
# Stage 3 — runtime. Backend venv + built SPA + Caddy, run as non-root under tini.
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates tini && rm -rf /var/lib/apt/lists/*
COPY --from=caddy:2 /usr/bin/caddy /usr/local/bin/caddy
# The upstream caddy binary ships with a file capability (cap_net_bind_service).
# Under our per-instance hardening (cap_drop=ALL + no-new-privileges) the kernel
# refuses to exec a binary that requests file caps ("Operation not permitted").
# We bind :8080 (no privileged port), so strip the xattr by copying without it.
RUN cp /usr/local/bin/caddy /usr/local/bin/caddy.stripped \
 && rm /usr/local/bin/caddy \
 && mv /usr/local/bin/caddy.stripped /usr/local/bin/caddy \
 && chmod 0755 /usr/local/bin/caddy

# Backend (venv lives at /app/backend/.venv — same path as build, so shebangs work).
COPY --from=backend /app/backend /app/backend
# Built SPA.
COPY --from=frontend /app/frontend/dist /srv/www
# Proxy config + entrypoint.
COPY Caddyfile /etc/caddy/Caddyfile
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Non-root user; /data is the per-instance LURSOR_DATA_DIR (DB + workspaces + skills).
RUN useradd --create-home --uid 10001 lursor \
 && mkdir -p /data \
 && chown -R lursor:lursor /data /app /srv
ENV PATH="/app/backend/.venv/bin:${PATH}" \
    LURSOR_DATA_DIR=/data \
    BROWSER_QA_ENABLED=false \
    PORT=8080
WORKDIR /app/backend
USER lursor
EXPOSE 8080

# Health via the single public port so it also proves the Caddy→backend path.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4).status==200 else 1)"

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
