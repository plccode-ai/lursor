

# Lursor

**The self-hosted control room for AI agents.**

Build an agent, point it at a folder on your disk, and watch it work — with a live
terminal, file tree, git diff, and dev-server preview beside every conversation.
Your machine, your keys, your models.

[Install](#install) · [How it works](#how-it-works) · [Security](#security) · [Design record](AGENTS.md)

[![License](https://img.shields.io/badge/license-Apache%202.0-blue?style=for-the-badge)](LICENSE)
[![Release](https://img.shields.io/github/v/release/JonathanConn/lursor?style=for-the-badge&color=black)](https://github.com/JonathanConn/lursor/releases)

</div>


## What you get

**A workspace, not a chat box.** Every agent is rooted in a real directory on your
disk. Open a thread against it and the same window gives you a live PTY (job
control, full-screen apps; POSIX only), the file browser, the working-tree diff for
every repo underneath, and any dev server the agent started — auto-detected, URL
surfaced.

**Agents you actually build.** Full CRUD over agents, skills, subagents, and tools
— per-agent model, instructions, and feature flags (todo, subagents, skills,
memory, web search, thinking level).

**Work that finishes without you.** Plan → refine → execute, plus goal mode: a
self-continuing loop that drafts a plan, optionally waits for approval, then works
turn after turn until an independent evaluator judges the objective met.

**Standing orders.** Put an agent on a cron expression in its own timezone, so 9am
survives DST. Each fire opens a fresh conversation as a single turn or a full goal
run. Anything due while Lursor was closed is reported, never silently replayed.

**Runs that outlive the context window.** Long conversations summarize their own
history before the window fills. Every agent and subagent can tune how full the
window gets first, and how much history is summarized versus kept verbatim.

**Streaming that shows the work.** Assistant tokens and tool calls stream live over
AG-UI SSE. Threads self-name via a fast model; `/compact` folds up long ones.

**Your bill, itemized.** Token-usage and cost rollups per model, per workspace, per
day.

**Any model.** OpenRouter out of the box; add custom providers, or drive local
models — pull, serve, VRAM — through a **laios** daemon.

Also in the box: file upload, GitHub integration on the changes panel, and a seeded
library of prompt templates.

## Where Lursor sits

- **Editor agents** (Cursor, Cline, Claude Code) live inside one repo and one
  editor session. Lursor runs beside them: many agents, many workspaces, standing
  schedules, and a UI that persists after you close the terminal.
- **Hosted agent platforms** hold your keys, your source, and your history. Lursor
  is a local binary talking to a local SQLite file. Nothing leaves your machine
  except the model calls you configured.
- **Chat UIs** give you a message box. Lursor gives the agent a shell, a
  filesystem, and a git working tree — and shows you all three while it works.

## Install

The desktop app bundles its own backend — a frozen, self-contained Python
interpreter that Lursor starts and stops for you. No Python, `uv`, `bun`, or
manual server required.

```bash
curl -fsSL https://raw.githubusercontent.com/JonathanConn/lursor/main/scripts/install.sh | sh
```

This downloads the prebuilt app for your OS/arch from GitHub Releases, verifies
it against the published SHA-256, and installs it — `Lursor.app` into
`/Applications` on macOS, or `Lursor.AppImage` into `~/.local/bin` plus an
app-menu entry on Linux. Re-running it upgrades in place.

Builds aren't code-signed yet, so the installer clears the macOS quarantine flag
for you. That's also why this script — not Homebrew — is the macOS install path:
Homebrew dropped `--no-quarantine` in 4.7, so a cask can't do the same. A tap
lands once releases are signed and notarized.

To update an existing install — checks your version against the latest release
and does nothing if you're current:

```bash
curl -fsSL https://raw.githubusercontent.com/JonathanConn/lursor/main/scripts/update.sh | sh
```

Pin a version with `LURSOR_VERSION=1.2.3`, change the Linux install dir with
`LURSOR_PREFIX`, and remove it again with:

```bash
curl -fsSL https://raw.githubusercontent.com/JonathanConn/lursor/main/scripts/install.sh | sh -s -- --uninstall
```

Requires macOS on Apple Silicon or Linux x86_64; Windows and Intel macOS aren't
built yet — run from source there. On first launch, paste an
[OpenRouter key](https://openrouter.ai/keys) or point at a local
OpenAI-compatible endpoint. [docs/INSTALL.md](docs/INSTALL.md) covers the
walkthrough, where your data lives, and how updates work.

## Run from source

One command, both processes:

```bash
pnpm dev                              # backend + frontend in the browser
pnpm dev:electron                     # ... in the Electron desktop shell
pnpm dev:debug                        # ... and auto-open Chrome DevTools
```

`npm run dev` and `bun run dev` work too — the root `package.json` has no
dependencies, it just delegates to the script that does the work:

```bash
./scripts/dev.sh                      # backend + frontend in the browser
./scripts/dev.sh --electron           # ... in the Electron desktop shell
./scripts/dev.sh --electron --debug   # ... and auto-open Chrome DevTools
```

Ctrl-C stops both. Or run them separately:

```bash
# backend
cd backend
uv sync --extra dev
cp .env.example .env      # add your OPENROUTER_API_KEY
uv run uvicorn app.main:app --reload --port 8791

# frontend
cd frontend
bun install
cp .env.example .env      # VITE_API_BASE defaults to http://localhost:8791/api
bun run dev
```

Then open the Vite URL (default `http://localhost:8899`).
[docs/ELECTRON.md](docs/ELECTRON.md) covers how the desktop app is wired and how to
package a distributable.

## How it works

| Layer | What it is |
| --- | --- |
| Agent engine | **[pydantic-deepagents](https://github.com/vstorm-co/pydantic-deepagents)** — planning, filesystem, subagents, skills, memory, on Pydantic AI |
| Backend | **FastAPI + SQLite** |
| Frontend | **Vite + React + Tailwind + shadcn/ui**, also shipped as an **Electron** app |
| Streaming | **[AG-UI](https://github.com/ag-ui-protocol/ag-ui)** over SSE, via Pydantic AI's first-party adapter |
| Models | **OpenRouter**, custom providers, or local models via **laios** |

```
lursor/
  backend/     FastAPI + pydantic-deepagents + SQLite   (see backend/README.md)
  frontend/    Vite + React + Tailwind + shadcn/ui       (see frontend/README.md)
               also runs as an Electron desktop app     (see docs/ELECTRON.md)
  docs/        Desktop and release docs
               design record lives in AGENTS.md at the repo root
  scripts/     dev.sh — run backend + frontend together
```

## Security

**Lursor is single-user and local-first: there is no authentication, and the
backend is as privileged as a shell on your machine.** It hands agents a real PTY
and unsandboxed command execution, stores provider API keys unencrypted in SQLite,
and accepts requests from any origin. Keep it bound to `127.0.0.1` — the desktop
app already does; `scripts/dev.sh` binds `0.0.0.0` for LAN convenience — and never
expose the port to the internet or an untrusted network.

[SECURITY.md](SECURITY.md) has the full threat model, safe-operation notes, and how
to report a vulnerability privately.

## Status

MVP, actively growing. [AGENTS.md](AGENTS.md) is the design record — the
architecture, the subsystem-by-subsystem decisions, the invariants worth knowing
before you change anything, and what is deliberately *not* built. Auth,
multi-tenancy, and Docker sandboxing remain intentionally deferred.

Issues and PRs welcome. If you're changing behaviour, read AGENTS.md first — it
will tell you whether the thing you're about to "fix" is load-bearing.

## License

[Apache License 2.0](LICENSE).
