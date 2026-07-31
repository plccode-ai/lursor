"""On-demand, per-developer Lursor orchestrator + identity router.

Data path:   browser → Cloudflare Access → (tunnel) → THIS → user's container
Control path: hub → THIS /_orch/* (bearer token) → start/stop/list instances

Every proxied request resolves the caller's identity (Cloudflare Access email),
ensures their container is running, and forwards HTTP/WS to it. Idle containers
are reaped; their volumes persist.
"""

from __future__ import annotations

import contextlib
import hmac
import logging

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from starlette.websockets import WebSocket

from .auth import AuthError, email_from_request, instance_id_for
from .config import settings
from .manager import manager
from .proxy import proxy_http, proxy_ws

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("orchestrator")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    manager.ensure_network()
    import asyncio

    reaper = asyncio.create_task(manager.reaper())
    if settings.dev_trust_header:
        log.warning("ORCH_DEV_TRUST_HEADER is ON — identity is UNVERIFIED. Local dev only.")
    try:
        yield
    finally:
        reaper.cancel()
        with contextlib.suppress(Exception):
            await reaper
        await manager.aclose()


app = FastAPI(title="lursor-orchestrator", lifespan=lifespan)


# --- control plane (hub → orchestrator, bearer token) --------------------
def require_control(authorization: str = Header(default="")) -> None:
    if not settings.control_token:
        raise HTTPException(503, "control API disabled (ORCH_CONTROL_TOKEN unset)")
    expected = f"Bearer {settings.control_token}"
    if not hmac.compare_digest(authorization, expected):
        raise HTTPException(401, "bad control token")


class StartReq(BaseModel):
    email: str


class StopReq(BaseModel):
    email: str | None = None
    instance_id: str | None = None


@app.get("/_orch/health")
async def health() -> dict:
    return {"status": "ok", "service": "lursor-orchestrator"}


@app.get("/_orch/instances", dependencies=[Depends(require_control)])
async def instances() -> list[dict]:
    return [i.__dict__ for i in manager.list_instances()]


@app.post("/_orch/instances/start", dependencies=[Depends(require_control)])
async def start(req: StartReq) -> dict:
    email = req.email.strip().lower()
    iid = instance_id_for(email)
    base = await manager.ensure_running(email, iid)
    ok = await manager.wait_healthy(base)
    if not ok:
        raise HTTPException(504, "instance did not become healthy in time")
    return {"ok": True, "instance_id": iid, "email": email}


@app.post("/_orch/instances/stop", dependencies=[Depends(require_control)])
async def stop(req: StopReq) -> dict:
    iid = req.instance_id or (instance_id_for(req.email.strip().lower()) if req.email else None)
    if not iid:
        raise HTTPException(400, "provide email or instance_id")
    stopped = await manager.stop(iid)
    return {"ok": stopped, "instance_id": iid}


# --- data plane (developer browser → their instance) ---------------------
async def _resolve(headers) -> tuple[str, str]:
    try:
        email = email_from_request(headers)
    except AuthError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return email, instance_id_for(email)


@app.websocket("/{path:path}")
async def ws_proxy(ws: WebSocket) -> None:
    try:
        email, iid = await _resolve(ws.headers)
    except HTTPException:
        await ws.close(code=1008)  # policy violation
        return
    base = await manager.ensure_running(email, iid)
    if not await manager.wait_healthy(base):
        await ws.close(code=1011)
        return
    manager.touch(iid)
    ws_base = "ws://" + base.split("://", 1)[1]
    upstream = ws_base + ws.url.path
    if ws.url.query:
        upstream += "?" + ws.url.query
    await proxy_ws(ws, upstream)


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def http_proxy(request: Request) -> Response:
    email, iid = await _resolve(request.headers)
    base = await manager.ensure_running(email, iid)
    if not await manager.wait_healthy(base):
        return JSONResponse({"error": "instance did not become healthy"}, status_code=504)
    manager.touch(iid)
    return await proxy_http(request, base)
