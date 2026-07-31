"""Per-user Lursor container lifecycle.

One container per developer, each with its own Docker volume mounted at
`/data` (LURSOR_DATA_DIR — the SQLite DB + workspaces + skills). Containers are
started on demand and stopped when idle; the volume persists across stops so a
returning user keeps their state. All Lursor instances are treated as
compromised-by-design (they run unsandboxed shell/PTY), so they get dropped
capabilities, no host mounts, resource limits, and a private network.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass

import docker
import httpx
from docker.errors import NotFound

from .config import settings

log = logging.getLogger("orchestrator.manager")

# Label keys stamped on every managed container so we can find/reconcile them
# (e.g. after an orchestrator restart) and never touch unrelated containers.
LBL_MANAGED = "ai.plccode.lursor.managed"
LBL_EMAIL = "ai.plccode.lursor.email"
LBL_INSTANCE = "ai.plccode.lursor.instance"


@dataclass
class Instance:
    instance_id: str
    email: str
    container_name: str
    last_active: float


class Manager:
    def __init__(self) -> None:
        self._docker = docker.from_env()
        self._http = httpx.AsyncClient(timeout=5.0)
        # Per-instance lock so concurrent first-requests don't double-create.
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        # In-memory activity clock (authoritative for idle reaping this process).
        self._last_active: dict[str, float] = {}

    # --- network -----------------------------------------------------------
    def ensure_network(self) -> None:
        try:
            self._docker.networks.get(settings.docker_network)
        except NotFound:
            log.info("creating docker network %s", settings.docker_network)
            self._docker.networks.create(settings.docker_network, driver="bridge")

    # --- start on demand ---------------------------------------------------
    async def ensure_running(self, email: str, instance_id: str) -> str:
        """Ensure the user's container is up and healthy; return its base URL."""
        async with self._locks[instance_id]:
            base_url = await asyncio.to_thread(self._ensure_running_sync, email, instance_id)
        self._last_active[instance_id] = time.time()
        return base_url

    def _ensure_running_sync(self, email: str, instance_id: str) -> str:
        name = settings.container_name(instance_id)
        try:
            c = self._docker.containers.get(name)
            if c.status != "running":
                log.info("starting stopped instance %s (%s)", instance_id, email)
                c.start()
        except NotFound:
            log.info("creating instance %s (%s)", instance_id, email)
            c = self._create(email, instance_id, name)
        return f"http://{name}:{settings.instance_port}"

    def _create(self, email: str, instance_id: str, name: str):
        env = {
            "LURSOR_DATA_DIR": "/data",
            "BROWSER_QA_ENABLED": "false",
        }
        if settings.openrouter_api_key:
            env["OPENROUTER_API_KEY"] = settings.openrouter_api_key

        kwargs: dict = dict(
            image=settings.instance_image,
            name=name,
            detach=True,
            environment=env,
            labels={
                LBL_MANAGED: "true",
                LBL_EMAIL: email,
                LBL_INSTANCE: instance_id,
            },
            network=settings.docker_network,
            volumes={settings.volume_name(instance_id): {"bind": "/data", "mode": "rw"}},
            # Hardening: the container is the only sandbox.
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            mem_limit=settings.instance_mem_limit,
            pids_limit=settings.instance_pids_limit,
            nano_cpus=settings.instance_nano_cpus,
            restart_policy={"Name": "no"},
        )
        if settings.instance_runtime:
            kwargs["runtime"] = settings.instance_runtime  # e.g. "runsc" (gVisor)
        return self._docker.containers.run(**kwargs)

    async def wait_healthy(self, base_url: str) -> bool:
        """Poll /api/health until the instance answers or the start budget runs out."""
        deadline = time.time() + settings.start_timeout_s
        url = f"{base_url}/api/health"
        while time.time() < deadline:
            try:
                r = await self._http.get(url)
                if r.status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.5)
        return False

    def touch(self, instance_id: str) -> None:
        self._last_active[instance_id] = time.time()

    # --- control / introspection ------------------------------------------
    def list_instances(self) -> list[Instance]:
        out: list[Instance] = []
        for c in self._docker.containers.list(all=True, filters={"label": f"{LBL_MANAGED}=true"}):
            iid = c.labels.get(LBL_INSTANCE, "")
            out.append(
                Instance(
                    instance_id=iid,
                    email=c.labels.get(LBL_EMAIL, ""),
                    container_name=c.name,
                    last_active=self._last_active.get(iid, 0.0),
                )
            )
        return out

    async def stop(self, instance_id: str) -> bool:
        return await asyncio.to_thread(self._stop_sync, instance_id)

    def _stop_sync(self, instance_id: str) -> bool:
        name = settings.container_name(instance_id)
        try:
            c = self._docker.containers.get(name)
        except NotFound:
            return False
        log.info("stopping instance %s", instance_id)
        c.stop(timeout=10)
        c.remove()  # volume is kept; container is disposable
        self._last_active.pop(instance_id, None)
        return True

    # --- idle reaper -------------------------------------------------------
    async def reaper(self) -> None:
        while True:
            await asyncio.sleep(settings.reap_interval_s)
            try:
                await self._reap_once()
            except Exception:  # noqa: BLE001 — never let the loop die
                log.exception("reaper pass failed")

    async def _reap_once(self) -> None:
        now = time.time()
        for inst in self.list_instances():
            # Only reap running containers we've seen activity for; unknown
            # last_active (0) means "started before this process" — give it a
            # grace clock rather than killing immediately.
            last = self._last_active.get(inst.instance_id)
            if last is None:
                self._last_active[inst.instance_id] = now
                continue
            if now - last > settings.idle_timeout_s:
                log.info("reaping idle instance %s (idle %ds)", inst.instance_id, int(now - last))
                await self.stop(inst.instance_id)

    async def aclose(self) -> None:
        await self._http.aclose()


manager = Manager()
