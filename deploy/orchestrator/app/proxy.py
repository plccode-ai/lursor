"""Reverse proxy for the data path.

HTTP is streamed both ways (Lursor uses SSE on /api/threads/*/stream, so the
response must not be buffered). WebSockets (terminal PTY, preview/file/git
watchers) are pumped bidirectionally. Hop-by-hop headers are stripped per
RFC 7230.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
import websockets
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.websockets import WebSocket, WebSocketDisconnect

log = logging.getLogger("orchestrator.proxy")

# Headers that must not be forwarded across a proxy hop.
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}

# Long timeout: agent turns stream for minutes. No read timeout on the body.
_client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=None, write=None, pool=None))


def _filter(headers: dict) -> dict:
    return {k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP}


async def proxy_http(request: Request, base_url: str) -> Response:
    url = base_url + request.url.path
    if request.url.query:
        url += "?" + request.url.query

    req = _client.build_request(
        request.method,
        url,
        headers=_filter(dict(request.headers)),
        content=request.stream(),
    )
    try:
        upstream = await _client.send(req, stream=True)
    except httpx.HTTPError as exc:
        log.warning("upstream HTTP error for %s: %s", url, exc)
        return Response("upstream unavailable", status_code=502)

    async def body():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(
        body(),
        status_code=upstream.status_code,
        headers=_filter(dict(upstream.headers)),
        media_type=upstream.headers.get("content-type"),
    )


async def proxy_ws(client_ws: WebSocket, upstream_url: str) -> None:
    # Forward cookies/auth-relevant headers; drop WS handshake + hop-by-hop ones
    # (the websockets client sets its own Upgrade/Key/Version).
    fwd = {
        k: v
        for k, v in client_ws.headers.items()
        if k.lower() not in HOP_BY_HOP
        and not k.lower().startswith("sec-websocket")
    }
    try:
        upstream = await websockets.connect(upstream_url, additional_headers=fwd, max_size=None)
    except Exception as exc:  # noqa: BLE001
        log.warning("upstream WS connect failed for %s: %s", upstream_url, exc)
        await client_ws.close(code=1011)
        return

    await client_ws.accept()

    async def c2u() -> None:
        try:
            while True:
                msg = await client_ws.receive()
                t = msg.get("type")
                if t == "websocket.disconnect":
                    break
                if msg.get("text") is not None:
                    await upstream.send(msg["text"])
                elif msg.get("bytes") is not None:
                    await upstream.send(msg["bytes"])
        except WebSocketDisconnect:
            pass

    async def u2c() -> None:
        try:
            async for message in upstream:
                if isinstance(message, str):
                    await client_ws.send_text(message)
                else:
                    await client_ws.send_bytes(message)
        except websockets.ConnectionClosed:
            pass

    task_c2u = asyncio.create_task(c2u())
    task_u2c = asyncio.create_task(u2c())
    done, pending = await asyncio.wait({task_c2u, task_u2c}, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    await upstream.close()
    try:
        await client_ws.close()
    except RuntimeError:
        pass
