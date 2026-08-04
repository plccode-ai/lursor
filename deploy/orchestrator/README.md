# Lursor orchestrator (on-demand, per-developer)

The on-demand engine + identity router that makes single-tenant Lursor usable by
a team. It sits behind **Cloudflare Access**, maps each authenticated developer
email to a private, hardened Lursor container (starting it on first request,
stopping it when idle), and reverse-proxies HTTP + WebSocket traffic to it.

```
browser → Cloudflare Access → cloudflared → orchestrator → lursor-usr-<hash> (own /data volume)
hub     → /_orch/* (bearer)  ───────────────┘  (pre-warm / stop / list)
```

## Why it exists
Lursor has **no auth and no tenancy**, and runs **unsandboxed** shell/PTY. So each
developer gets their **own** container (isolation = the sandbox) and all authN/Z
happens at the edge. The orchestrator never trusts the app; it only ever routes an
already-authenticated identity to that identity's instance.

## Endpoints
- `GET  /_orch/health` — liveness (unauthenticated).
- `GET  /_orch/instances` — list managed instances *(bearer)*.
- `POST /_orch/instances/start` `{ "email": "..." }` — pre-warm a user's instance *(bearer)*.
- `POST /_orch/instances/stop`  `{ "email" | "instance_id" }` — stop it, keep the volume *(bearer)*.
- everything else — the **data plane**: resolve identity → ensure running → proxy HTTP/WS.

Identity comes from the verified `Cf-Access-Jwt-Assertion` JWT (`email` claim). In
local dev, `ORCH_DEV_TRUST_HEADER=1` trusts an `X-Dev-Email` /
`Cf-Access-Authenticated-User-Email` header instead — **never enable in prod**.

## Run locally
```bash
# build the instance image first (repo root)
docker build -t lursor:dev ../..
# then bring up the orchestrator (dev-trust mode)
OPENROUTER_API_KEY=sk-or-... docker compose up --build
# hit it as two different developers:
curl -H 'X-Dev-Email: alice@plccode.ai' http://localhost:9000/api/health
curl -H 'X-Dev-Email: bob@plccode.ai'   http://localhost:9000/api/health
curl -H 'Authorization: Bearer dev-control-token' http://localhost:9000/_orch/instances
```

## Production notes
- Set `ORCH_ACCESS_TEAM_DOMAIN` + `ORCH_ACCESS_AUD`; unset `ORCH_DEV_TRUST_HEADER`.
- Front the Docker socket with a scoped `docker-socket-proxy` (raw socket = root).
- Set `ORCH_INSTANCE_RUNTIME=runsc` (gVisor) and add egress restrictions.
- Move off the shared `OPENROUTER_API_KEY` to per-user keys / a key-injecting proxy.
