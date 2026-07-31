"""Orchestrator configuration, loaded from the environment.

The orchestrator is the on-demand engine + per-user identity router that sits
behind Cloudflare Access: it maps an authenticated developer email to a private,
hardened Lursor container (starting it on first request, stopping it when idle),
and reverse-proxies HTTP + WebSocket traffic to it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _b(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _i(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _s(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    # --- Instance image & placement ---------------------------------------
    # The Lursor server image (Dockerfile in the repo root). Local dev uses the
    # tag you built ("lursor:dev"); prod points at the registry image.
    instance_image: str = field(default_factory=lambda: _s("ORCH_INSTANCE_IMAGE", "lursor:dev"))
    # All instances + the orchestrator share this user-defined bridge network so
    # the orchestrator can reach a container by name without publishing host ports.
    docker_network: str = field(default_factory=lambda: _s("ORCH_DOCKER_NETWORK", "lursor-net"))
    # The single port each Lursor container exposes (Caddy front).
    instance_port: int = field(default_factory=lambda: _i("ORCH_INSTANCE_PORT", 8080))
    container_prefix: str = field(default_factory=lambda: _s("ORCH_CONTAINER_PREFIX", "lursor-usr-"))
    volume_prefix: str = field(default_factory=lambda: _s("ORCH_VOLUME_PREFIX", "lursor-data-"))

    # --- Lifecycle --------------------------------------------------------
    idle_timeout_s: int = field(default_factory=lambda: _i("ORCH_IDLE_TIMEOUT_S", 1200))  # 20 min
    reap_interval_s: int = field(default_factory=lambda: _i("ORCH_REAP_INTERVAL_S", 60))
    start_timeout_s: int = field(default_factory=lambda: _i("ORCH_START_TIMEOUT_S", 60))

    # --- Per-instance runtime env & hardening -----------------------------
    # Shared OpenRouter key handed to every instance (internal-MVP; for SaaS move
    # to per-user keys / a key-injecting egress proxy). Exfiltratable by design.
    openrouter_api_key: str = field(default_factory=lambda: _s("OPENROUTER_API_KEY", ""))
    instance_mem_limit: str = field(default_factory=lambda: _s("ORCH_INSTANCE_MEM_LIMIT", "2g"))
    instance_pids_limit: int = field(default_factory=lambda: _i("ORCH_INSTANCE_PIDS_LIMIT", 512))
    instance_nano_cpus: int = field(default_factory=lambda: _i("ORCH_INSTANCE_NANO_CPUS", 2_000_000_000))  # 2 CPUs
    # gVisor etc. — set to "runsc" in prod once the runtime is installed.
    instance_runtime: str = field(default_factory=lambda: _s("ORCH_INSTANCE_RUNTIME", ""))

    # --- Identity (Cloudflare Access) -------------------------------------
    # Verify the Cf-Access-Jwt-Assertion header against the team's JWKS. Required
    # in production. team domain e.g. "plccode.cloudflareaccess.com".
    access_team_domain: str = field(default_factory=lambda: _s("ORCH_ACCESS_TEAM_DOMAIN", ""))
    access_aud: str = field(default_factory=lambda: _s("ORCH_ACCESS_AUD", ""))
    # DANGEROUS local-only escape hatch: trust the Cf-Access-Authenticated-User-Email
    # / X-Dev-Email header WITHOUT verifying a JWT. Never enable in production.
    dev_trust_header: bool = field(default_factory=lambda: _b("ORCH_DEV_TRUST_HEADER", False))

    # --- Control API (hub → orchestrator, server-to-server) ---------------
    control_token: str = field(default_factory=lambda: _s("ORCH_CONTROL_TOKEN", ""))

    def container_name(self, instance_id: str) -> str:
        return f"{self.container_prefix}{instance_id}"

    def volume_name(self, instance_id: str) -> str:
        return f"{self.volume_prefix}{instance_id}"


settings = Settings()
