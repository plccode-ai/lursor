# AGENTS.md

Working notes for anyone — human or agent — changing this repo. This is the
consolidated design record: the durable decisions, invariants and traps from the
feature plans that built Lursor. Per-feature plan docs were deleted once shipped;
`git log --diff-filter=A -- docs/` finds the original for any feature if you need
the full reasoning.

For end-user and ops docs see [`README.md`](README.md),
[`docs/INSTALL.md`](docs/INSTALL.md), [`docs/ELECTRON.md`](docs/ELECTRON.md) and
[`docs/DISTRIBUTION.md`](docs/DISTRIBUTION.md).

---

## 1. What this is

A self-hosted **agent harness**. You create agents (and subagents, skills, tools),
point them at **workspaces** (directories on disk), and chat with them — with a
live terminal, file browser, git review and dev-server preview alongside every
conversation.

The agent engine is **not ours**: [`pydantic-deepagents`](https://github.com/vstorm-co/pydantic-deepagents)
(`pydantic_deep`), pinned to a commit in `backend/pyproject.toml`, itself built on
Pydantic AI. Chat streams over the **AG-UI** protocol via Pydantic AI's
first-party adapter. That shapes almost every design decision below: when the
library's behaviour is wrong for us we **compose or wrap**, never patch — see
`agents/browser_qa.py`, `agents/context_budget.py`, `agents/deduping_backend.py`,
and the `task`-tool roster rewrite in `builder.py`.

## 2. Repo map

```
backend/          FastAPI + pydantic-deepagents + SQLite      (backend/README.md)
  app/api/        REST routers + the AG-UI chat endpoint (chat.py)
  app/agents/     agent construction and the run engine
  app/skills/     skill discovery, scope resolution, script exec
  app/envvars/    env-var layer resolution
  app/db/         SQLModel tables, async session, migrations
  tests/          pytest, offline (no API key needed)
frontend/         Vite + React 19 + Tailwind v4 + shadcn/ui  (frontend/README.md)
  src/agui/       transport + chat store + engine
  src/api/        typed REST client + TanStack Query hooks, one file per resource
  src/components/ chat/, shell/ (dock panels), layout/ (rail+panel nav), ui/
  src/pages/      one dir per destination
  electron/       desktop main + preload
docs/             INSTALL / ELECTRON / DISTRIBUTION
packaging/        Homebrew cask template (rendered by CI)
scripts/          dev.sh, install.sh, update.sh
```

## 3. Commands

```bash
pnpm dev                              # backend + frontend (npm/bun run dev work too)
pnpm dev:electron [dev:debug]         # ... in the Electron shell
# The root package.json is a dependency-free shim over these:
./scripts/dev.sh                      # backend + frontend
./scripts/dev.sh --electron [--debug] # ... in the Electron shell

cd backend
uv sync --extra dev                   # add --extra hindsight for the memory provider
uv run uvicorn app.main:app --reload --port 8791
uv run pytest                         # offline; no API key needed
uv run ruff check app tests

cd frontend
bun install                           # NOT pnpm — pnpm deadlocks in this environment
bun run dev                           # :8899
bun run build                         # tsc -b && vite build
bun run lint                          # oxlint
```

There is **no frontend test runner**. Frontend changes are verified with
`tsc -b`, `oxlint`, and a manual pass. The bar for a backend change is
`uv run pytest` green *without editing existing tests* — if an existing test needs
changing, that is a signal the change altered behaviour it shouldn't have.

## 4. Hard conventions

**UI (non-negotiable, from the global rules):**
- Every text element carries `text-foreground` or `text-muted-foreground`.
- Never absolute colours (`text-white`, `bg-gray-*`). 87 theme blocks in
  `index.css` define the full semantic token set; use it. Adding a new
  `--custom` token means 87 edits — derive from existing tokens instead
  (the nav rail does this: `bg-sidebar-accent/40`).
- Never the `container` class. Copy the surrounding page's padding
  (`px-4 py-6 sm:px-0`).
- No emoji anywhere.

**Code:**
- No `any` in TypeScript unless genuinely unavoidable.
- Backend: ruff, line length 100, `select = ["E","F","I","UP","B"]`.
- Prefer an official SDK over a hand-rolled client.

**Migrations:** SQLite + `create_all` for new tables, plus hand-rolled idempotent
blocks in `db/session.py::_apply_lightweight_migrations` (guarded by
`PRAGMA table_info`). **No Alembic.** New tables need no `ALTER` work; new columns
need one guarded block. Every migration must be idempotent across restarts, and
must be tested against a *copy* of a populated DB — see
`tests/test_*_migration.py`.

**Mobile:** one breakpoint, `md` (768px), matching `useIsMobile`. Fluid layouts —
no fixed `w-[…]`/`min-w-[…]` that can force horizontal scroll. QA anchors
360/390/430px portrait; those are anchors, not minimums. Dialogs become bottom
sheets below `md`. Respect safe-area insets (`pb-safe` etc.).

**No silent caps.** If a code path bounds something — iteration cap, truncation,
skipped fire, dropped result — it must say so in an event, a log line, or a
history row. A quiet stop reads as success.

## 5. Architecture

### Backend: the run engine

```
POST /threads/{id}/chat
  → parse request + turn intent            api/chat.py
  → persist the user turn up front
  → _build_agent_and_context(session, …)   resolves providers, subagents, deep
                                           defaults, skills + their env vars
  → build_deep_agent(row, workspace_path,…)  agents/builder.py
  → AGUIAdapter.from_request(request, agent=agent)
  → pick a driver: chat | plan | goal | execute_plan
  → chat_run_manager.start_run(thread_id, driver)   detached asyncio.Task
  → return an SSE subscription to that run
```

**`chat_run_manager` is the load-bearing abstraction.** A run is an
`asyncio.Task` owned by the manager, not by the request: it buffers encoded SSE
lines (capped at 5000, 200 finished threads retained), fans out to subscribers,
and survives browser disconnect. The HTTP response is only a *subscriber*. This
is what makes reconnect, stop, and headless (scheduled) runs all work with no
extra machinery.

Its critical invariant: `subscribe()` snapshots the buffer and registers the
queue **with no `await` in between**, or events are lost in the gap.

Routes: `POST /{id}/chat` (start + stream), `GET /{id}/stream` (reconnect,
replays), `POST /{id}/stop`, `POST /{id}/goal/interject`, `POST /{id}/compact`.
`GET /threads/active-runs` **must be declared before `/{thread_id}`** or FastAPI
routes it as a thread id.

Per-turn budget: `TURN_REQUEST_LIMIT = 150` model requests
(`builder.py`), and subagents get their own budget of the same size.

`reconcile_interrupted_runs()` runs at startup — run state is in-memory only, so
a thread the last process left mid-run would otherwise show a live status pill
forever.

### Frontend: transport → store → view

```
transport   agui/agent.ts (HttpAgent) · agui/stream-reader.ts
                → one ChatEventHandlers sink, shared by BOTH transports
state       agui/chatStore.ts   Zustand, normalized: order[] + byId{}
controller  agui/useChatEngine.ts   send/stop/queue/load/reconnect
view        components/chat/ChatTimeline → MessageRow(id) → UserBubble
                                        | AssistantGroup, in <StickToBottom>
```

The normalized store is the fix for the chat surface's four chronic defects
(render flashes, scroll detach, streaming jank, older-message flash). A streamed
token mutates `byId[assistantId]`, so **only that row re-renders** — the timeline
subscribes to `order` alone. Leaf rows are `memo`'d. `StreamingText` splits into a
stable prefix and a growing tail so at most two markdown parses exist mid-stream.
Scroll is `use-stick-to-bottom`, never hand-rolled — it pins before paint.

`useChatEngine` guards that must survive any refactor: the `loadSeq` monotonic
guard, the `sendingThreadRef`/`loadedThreadRef` dedupe guards, and
`resolveAssistantId` for models that omit message ids.

## 6. Subsystems

### Turn intents, and the plan → refine → execute flow

There are **no sticky thread modes**. `ThreadMode` survives only so rows from
older builds still load; live threads stay `chat`. Everything is a per-turn
intent on `forwardedProps.turn`:

| intent | behaviour |
| --- | --- |
| `chat` | full agent, all tools. The default. |
| `ask` | read-only, enforced by an **allowlist** tool filter (`_READONLY_TOOL_ALLOWLIST`) |
| `plan` | writes/refines a plan doc at `.agents/plan/PLAN-<slug>.md`; parks the thread in `awaiting_approval` |
| `execute_plan` | hands the finished doc to the goal loop |
| `goal` | one-off autonomous loop, condition = the message text |

The three-phase flow, with a human checkpoint at each boundary:

1. `/plan <objective>` → drafts the doc, `status = awaiting_approval`. A fresh
   `/plan` on a parked thread always starts a **new** doc.
2. A **plain** follow-up while parked = *refine that doc* (persisted as
   `kind="plan"`), not implement it. This inversion has been got wrong twice;
   it is the behaviour users expect.
3. The explicit **Execute plan** button sends `turn == "execute_plan"`:
   `goal = <plan H1 title>`, `success_criteria = the doc's ## Success Criteria`
   (falling back to the whole doc), and the loop is seeded with
   **`initial_history = []`** — the plan doc *is* the compiled context, so the
   refinement back-and-forth never reaches the model. The planning transcript
   stays visible in scrollback.

Plan mode is **instruction-gated, not tool-gated**. Gating the toolset was tried
twice and reverted twice: without the todo board and delegation, local reasoning
models (GLM/DeepSeek via vLLM) answer in prose and never call `write_file`, so a
`/plan` turn produces *no plan doc at all* — a worse failure than a plan turn
that edits a file. The allowlist also silently dropped `duckduckgo_search`. A
plan turn now sees a normal toolset and is held to planning by
`planning_instruction()`. `plan_mode` still disables browser QA and the
dev-server directive. `/ask` keeps its allowlist — there the no-write guarantee
is the feature, not a nudge.

Slash commands are **data**, in `components/chat/commands/registry.ts`. Adding
one is a single declarative entry; the parser, menu, dispatch and pill are all
generic, keyed on `command.kind` (a closed set). Nothing in the UI shell grows.
The descriptor fields mirror Claude Code's frontmatter so a future markdown
command loader is additive.

`agentScope` on a command decides whether its default agent is a **per-turn
override** (`forwardedProps.agent_id`, never persisted — `/ask`, `/goal`,
Execute plan) or a **sticky reassignment** (`PATCH thread.agent_id` — `/plan`
only). Per-turn commands used to permanently steal the thread's agent; they
must not.

### The goal loop

`agents/goal_loop.py` (`drive_goal_loop`) wraps the vendored
`pydantic_deep.goal` engine: run a turn → evaluate the transcript against the
condition → continue with `goal_continue_directive(condition, reason)` or
terminate. Terminal states: `completed` (evaluator confirmed), `blocked`
(`impossible` verdict), `failed` (iteration cap), `stopped` (user).

- The evaluator **defaults to "not met" on any error** — a transient hiccup must
  never declare premature success.
- Recitation each turn is what stops drift on long loops.
- The evaluator model resolves through Lursor's provider stack
  (`build_goal_evaluator` + `AppConfig.goal_evaluator_model`). The library's
  default is an Anthropic Haiku, and there may be no Anthropic key.
- When a preview URL exists the evaluator is wrapped with visual QA, so
  completion is judged on what actually rendered, not on the transcript.
- Steering: plain messages during a run buffer as interjections and are woven
  into the next seed.

### Compaction — two mechanisms, one pair of knobs

- **In-run** (`agents/context_budget.py`): pydantic-deep's
  `ContextManagerCapability` hard-codes compaction at 90% of the budget keeping
  nothing verbatim, and exposes no passthrough. We **mutate the capability the
  library already built** (keeping its limit warner, `compact_conversation` tool
  and history-archive search) to apply `compaction_threshold` and
  `compaction_ratio`. It also repoints the summarizer onto our stack — the
  library only inherits the primary model when it was passed as a *string*, and
  every Lursor run passes a built `Model` object, so it silently fell back to
  `anthropic:claude-haiku-4-5` and raised on the first compaction.
- **Manual `/compact`** (`agents/compaction.py`): condenses the stored
  *transcript* into a `kind="summary"` assistant message and marks the rows it
  subsumes `compacted` — kept in the DB, hidden from the UI and from model
  context. `Message.compacted` is the general in-thread context-boundary
  primitive; both history-assembly paths already filter it.

### Skills — four layers

`app/skills/resolve.py` owns scope; `app/skills/store.py` owns locations.
Lowest precedence first:

1. **user** — personal roots owned by other tools (`settings.user_skill_roots`)
2. **global** — managed skills with `is_global`
3. **workspace** — managed skills linked to this workspace
4. **local** — folders in one of the workspace's own roots
   (`settings.local_skill_roots`: `.agents/skills`, `.claude/skills`,
   `.cursor/skills`, `skills`) — committed into the repo

Closest layer wins a slug collision; your catalog beats a directory another tool
happens to populate. Roots are **configuration**, because there will be a fifth
convention.

Rules that are load-bearing:
- A managed skill lives **once**, in `~/.lursor/skills/<slug>/`. Reach is a DB
  assignment (`is_global` + `SkillWorkspaceLink`), not a location — so
  reassignment is a DB write and multi-workspace is free. Three states: global /
  N workspaces / **parked** (in the catalog, injected nowhere).
- **Foreign roots are discover-only.** `_reconcile_root(materialize=False)` means
  a row whose folder has vanished is *deleted*, never rebuilt. Pointed at a
  foreign root, the materialize path would create `.claude/` directories in repos
  that never had one and resurrect skills the user deleted in Cursor. This is the
  most important regression test in the skills suite.
- `move` (promote) is only for roots we own; everything else gets **copy**. A
  catalog entry may be a **symlink** into another tool's directory
  (`Skill.link_target`) — `delete` unlinks the link and leaves the target alone.
- `Skill.root` is **stored**, not probed: with several candidate roots per
  workspace the same slug can exist twice, and probing in order resolves an edit
  or a delete to the wrong file.
- `write_skill` **merges** frontmatter rather than replacing it — a Claude Code
  skill routinely carries `allowed-tools`/`license`/`version` and a `PATCH`
  would otherwise delete them from a file in someone's repo.
- `Skill.enabled` is checked in exactly one place (`resolve.candidates`) so env
  vars, `@`-mentions and the agent's own skill directories cannot disagree. A
  disabled row does not *shadow*: switching off a repo's `pdf` reveals the
  catalog's `pdf`.
- `reconcile()` runs on every `GET /skills`. That is up to 3N+2 directory scans;
  known and accepted, worth knowing before blaming it for a slow Skills tab.
- Widening discovery widened the **prompt-injection surface** — cloning a repo
  now loads skill instructions written by whoever wrote that repo. Accepted
  deliberately; `enabled` is the revocation path.
- `tests/conftest.py` pins `USER_SKILL_ROOTS=[]`, or the suite indexes whatever
  is in the developer's own `~/.claude/skills`.

**Skill Studio** is the catalog registered as a system `Workspace`
(`is_system` is *computed* from `path == settings.skills_dir` — no column, no
migration). Delete and path-change are refused by the API; rename is allowed.
That gives skill authoring the whole workspace surface — agent, terminal, file
tree, watcher — for the price of one row. A skill the agent writes there shows
up in the manager as **Not assigned** on the next load.

### Environment variables

`app/envvars/resolve.py`. One `EnvVar` table; each var attaches to any mix of
skills and workspaces, or is global. Precedence **global → workspace → skill**.
`key` is deliberately not unique — uniqueness is per layer, so precedence is
always well defined.

Four injection points, all additive (with no vars defined, behaviour is
byte-identical):
- The agent's shell, via `DedupingLocalBackend` overriding `execute` /
  `execute_background`. The base class takes no `env=`, so both are
  reimplemented; `tests/test_deduping_backend.py` carries parity tests against
  upstream drift.
- Per-skill script execution, via a `CallableSkillScriptExecutor` — a script
  never sees another skill's secrets.
- The system prompt lists **keys and descriptions only**. Without this the agent
  has no reason to believe a key exists and will ask the user for it.
- **Redaction on the way out**: every injected value ≥8 chars is scrubbed from
  shell and script output before it becomes tool output. The backend is the
  single choke point, so this covers the transcript, the persisted messages and
  the AG-UI stream at once.

Run-scoping uses a **`ContextVar`**, not an attribute: the `LocalBackend` is
shared per workspace across runs, so an attribute would leak one run's env into
a concurrent run. `asyncio.to_thread` copies context into the worker thread and
tasks inherit at creation, so a var set at the top of a run reaches every tool
call and every subagent of that run.

Values are **plaintext in SQLite**, matching every other secret the app holds
(`GitHubConfig.token`, `LaiosConnection.master_key`). The API is write-only:
reads return `has_value`, never the value. The interactive terminal panel is
deliberately *not* injected.

### Subagents

No "built-in override" concept — a built-in is a name, the library's description
and instructions, and an on/off switch. Overrides could express strictly less
than an ordinary subagent row and bypassed the `enabled` check.

The `task` tool's description ships `Use "general-purpose" when no specialized
subagent fits.` unconditionally, and `subagent_type: str` has no enum — so
disabling that built-in still produced `Error: Unknown subagent
'general-purpose'`. Fixed with a `PrepareTools` capability that rewrites the
`task` tool definition from the live roster and injects an `enum`, using
`dataclasses.replace` (never in-place mutation of a schema shared across runs).
The library's post-hoc validation stays as the backstop for local models that
ignore enums.

A user subagent with the same name as a built-in **shadows** it, not the reverse.

### Memory

App-wide *provider* choice (`AppConfig.memory_provider`), exactly like web
search; the per-agent `include_memory` flag stays the master on/off switch.

- `file` (default) — pydantic-deep's `MEMORY.md` in the workspace.
- `hindsight` — a [Hindsight](https://github.com/vectorize-io/hindsight) bank via
  `agents/hindsight.py`. Optional extra (`uv sync --extra hindsight`); a missing
  package or base URL **degrades to file memory with a warning**, never fails.

The two never coexist in one run — six overlapping memory tools is worse than
three. Isolation is by tag (`workspace:{id}`, using the *id* so a rename doesn't
orphan memories) with `tags_match="any_strict"`, the only variant that excludes
untagged memories. `MEMORY.md` files are left on disk, so flipping back is
lossless.

`memory_instructions` recalls on **every model request** upstream — a 150-round
turn would issue 150 recalls. Our capability caches the recalled block per agent
instance with a 120s TTL, busted by `after_tool_execute` when the agent retains
something. Privacy changes with this provider: recall/reflect send the query
string to whatever `base_url` points at, once per turn, whether or not the agent
uses memory.

### Schedules

`Schedule` + `ScheduleRun` rows, an in-process 30s `asyncio` tick
(`agents/scheduler.py`, modelled on `preview_service._poll_loop`), and
`chat.start_scheduled_run` — the headless counterpart to the chat endpoint. Both
converge on the same drivers, so a scheduled run can't drift from a manual one.

- **New thread per fire.** No unbounded context growth; each run's transcript,
  todos, diff and usage stand alone.
- **Missed fires are reported, never replayed.** A schedule whose `next_fire_at`
  is in the past gets one `missed` history row with the elapsed count and rolls
  forward. Opening the app after a weekend must never launch a burst of billable
  runs. `next_fire_at` is null while disabled, so re-enabling doesn't read as a
  pile of missed fires.
- **One run per schedule at a time** (`skipped` row otherwise).
- Timezone is an IANA name on the row; `zoneinfo` does the arithmetic, so 9am
  survives DST. `host_timezone()` reads `TZ` then the `/etc/localtime` symlink —
  `datetime.now().astimezone().tzinfo` yields an abbreviation (`EDT`) that
  `ZoneInfo` rejects, which would silently default every schedule to UTC.
- `app/cron.py` takes its reference instant as an argument everywhere and never
  reads the clock, which is what makes DST and closed-for-a-weekend ordinary
  assertions.
- `next_fire_at` rolls forward from **now**, not from the missed slot, so a slow
  tick fires once instead of catching up silently.
- Only one process, no workers — adding `--workers > 1` would multiply the loop
  and needs a lock first.
- Usage rows are tagged `kind="cron"` so unattended spend is visible in
  Analytics. Plan mode is not offered: a schedule that parks a doc nobody
  approves is a trap.
- Deleting a schedule **clears `schedule_id`** on its threads, handing them back
  to the workspace as ordinary conversations — a dangling id would make every
  run it ever produced unreachable.
- `GET /threads/{id}` stays unfiltered (asserted in `test_scheduler.py`): the
  chat page falls back to it, because resolving a scheduled thread against the
  filtered workspace list rendered the wrong state and the wrong agent.

Still open: there is no ambient signal that an overnight run finished. The
Schedules page is the only place it shows up.

### Preview and background processes

Detection **must not** ride the chat run. The first cut did, and lost: the dev
server outlives the turn and the chat SSE closes on `RUN_FINISHED`.

`agents/preview_service.py` is a long-lived per-workspace service. The chat
endpoint `register(workspace_id, backend)`s each run's backend; a poll loop scans
retained backends, parses candidate URLs (`preview_detect.parse_server_url`),
probes readiness over HTTP, and broadcasts **full snapshots** over
`WS /api/workspaces/{id}/preview/ws`. The panel keeps that socket open regardless
of chat activity.

- It tracks *all running background processes*, not just servers — a server is
  just a process that advertised a URL and passed the probe.
- Keep the most-recently-registered backend even while idle. `register` runs at
  run start, before the agent calls `run_in_background`; pruning on an empty
  first scan released the backend the dev server was about to appear in.
- First ready server auto-opens the panel once; further servers are one-tap
  chips, and a panel the user closed is not re-popped.
- `RunningProcessesBar` sits above the composer with inline output; the
  right-dock `process` panel and its pub/sub plumbing were removed as a
  duplicated surface for a read-only log tail.
- Process tracking is in-memory per backend, so a backend restart orphans
  still-running servers (they keep running, untracked). psutil-based
  rediscovery stays deferred.

### Browser visual QA

`agents/browser_qa.py` **composes** pydantic-deep's `BrowserCapability` rather
than using it directly: upstream `screenshot` returns text-only base64 (the model
cannot actually see it) and it captures no console or network. So we reuse the
vendored driving toolset (navigate/click/type/…) and own the browser lifecycle to
add `view_app` (screenshot → vision model) plus `get_console_logs` /
`get_network_errors`.

Python Playwright, no Node. Chromium auto-installs on first use (~150 MB, once).
Per-run, headless, `allowed_domains` scoped to loopback. `screenshot_url` is the
standalone path the goal evaluator uses, since the capability is run-scoped and
agent-driven.

### laios (local models)

Lursor is the **application plane**; the `laios` daemon is the **control plane**.
`api/laios.py` is a thin authenticated proxy that holds the `master_key`
server-side and forwards to the daemon's `/v1/*` API on `:7420`. All
restart/update logic lives in the daemon — Lursor stays a pure proxy. Restart is
special: the daemon dies mid-request, so a dropped connection shortly after a
`202` is expected, surfaced as `202 {restarting: true}`.

### The right dock

`hooks/use-dock-state.ts` owns the tab list (persisted per workspace in
`localStorage`); `components/shell/right-dock.tsx` renders it. **Any kind can be
open more than once** — two previews on different ports, two editors, two shells
— which forces three rules:

- **Tab ids are persisted and globally unique** (not per-session counters).
  Panel state that used to hang off the workspace id has to be keyed per tab or
  duplicates fight over one value: `lib/tab-storage.ts` namespaces it under
  `lursor:tab:<id>:*`, and `closeTab` purges that namespace. A preview also
  writes the workspace-wide key as the *default* a newly opened preview starts
  on; an explicit clear stores `""`, so a cleared tab doesn't re-inherit it.
- **Only the visible panel takes app-wide open requests** (`lib/open-file`,
  `lib/open-preview`). Hidden dock tabs stay mounted, so an unguarded panel
  would swallow the request and open the file where nobody can see it — the
  original single-tab bug, back in a new shape.
- **`ensureTab` targets active → most recently used → leftmost.** The request
  displaces whatever that tab held, so picking the leftmost would navigate a
  preview the user forgot about while the one they were working in sits
  untouched. Focus order (`mru`) is session-only state.

Tab strips show a panel-reported detail (a port, a filename) *only* while a kind
is open more than once, with an ordinal for a duplicate that has nothing to
report yet. Detail is derived from live panel state — never persisted.

### First run

`pages/onboarding/` — a five-step walkthrough at `/welcome`: bring a model, connect
GitHub, open the first workspace, create the first agent, then a summary of the
surfaces before landing in it. Full-screen, outside `AppShell` (nothing in the
sidebar or dock is useful yet). Four rules hold it together:

- **Progress is derived, never stored.** `useOnboardingStatus` reads the
  OpenRouter key, custom providers, the GitHub config, the workspace list, and the
  agents — so a step is "done" because the thing exists, not because a step was
  walked. That is what makes `/welcome` safe to revisit (Settings → General links
  to it) and invisible to installs that predate it: `OnboardingGate` silently
  marks a ready install complete instead of showing it a tour.
- **"No workspaces" is never true.** `ensure_skills_workspace` registers the
  skills catalog on every boot, so first-run detection has to filter
  `is_system` — otherwise the walkthrough thinks a workspace already exists.
- **A fresh install has no agents.** Nothing seeds one (unlike prompt templates
  and the skills catalog), and a chat with no agent can't be typed into — hence
  the agent step, without which the walkthrough would hand over a dead end. It
  prefills a name and, on a local-only install, the endpoint's own first `custom:`
  model: inheriting the app default there would name a cloud model the box has no
  key for. Never over a model the user picked themselves.
- **Only a model gates.** GitHub, the workspace, and the agent can be skipped (the
  forward control says so); the rail refuses to unlock past step one until a model
  source exists, since every other surface assumes one. LAIOS is deliberately
  absent — it needs its own daemon installed first, so it stays a post-setup
  destination.
- **The seen-flag is `localStorage`, read synchronously.** The gate short-circuits
  on it before mounting anything, so a returning user fires no extra queries;
  only an unfinished install pays for the check. Losing the flag costs nothing —
  see the first rule.

`GitHubRepoPickerDialog` takes `navigateOnClone={false}` here: it otherwise jumps
straight into the cloned workspace's chat, which would skip the last step.

Finishing hands over to `/workspaces/<id>/chat`, calling **`seedCollapsedDock`**
first: a closed, empty dock for that workspace, so the first conversation is the
whole window instead of a chat beside an empty panel. Guarded by
`hasStoredDockState`, so it is a first-visit default and never overwrites a layout
the user arranged; the rail still reopens the dock.

### Other

- **Terminal** — a real PTY per workspace over a WebSocket (`api/terminal.py`).
  POSIX only. Deliberately *not* env-injected.
- **Files** — `api/files.py` + a per-workspace watcher; Monaco, lazily loaded,
  fully editable on mobile with touch-tuned options. Tree rows carry VS Code-style
  git decorations from `GET /git/status` — deliberately *not* `/git/diff`, which
  computes a patch per changed file; the tree needs a state per path and nothing
  else. Changes roll up onto collapsed folders (`lib/git-tree-status.ts`), and
  `--ignored=matching` is what keeps the ignored set one entry per wholly-ignored
  directory instead of one per file inside `node_modules`.
- **Git / GitHub** — `api/git.py` returns `is_repo=False` for a non-repo
  (the skills catalog) and the panel renders its empty state. `api/github.py`
  holds the token server-side.
- **Prompt library** — `PromptTemplate` rows, seeded idempotently on every boot
  (`db/prompt_seed.py`). A template is **copied into** `Agent.instructions`, not
  linked, so agents stay self-contained. `agents/prompt_author.py` generates and
  improves prompts, capability-aware: it only references tools the agent
  actually has.
- **Analytics** — `UsageRecord` rows per turn, tagged with a `kind`, rolled up by
  model / workspace / day.
- **Models** — OpenRouter by default (`openrouter:` prefix), plus
  `CustomProvider` rows for OpenAI-compatible endpoints, including ones with no
  `/v1/models` (manual model lists).
- **Nav** — a 68px destination rail plus a contextual panel. `panelMode` is
  **sidebar state, not a route** (persisted to `localStorage`), so clicking an
  Activity row opens the conversation without the panel flipping back under the
  cursor. Activity has no route by design.

## 7. Invariants and traps

Each of these has already cost a debugging session.

1. **AG-UI dual transport.** A new stream event type must be wired into **both**
   the live-send path and the reconnect path. They share one
   `ChatEventHandlers` sink precisely so they can't diverge — keep it that way.
   Anything encoded through `chat_run_manager` is replay-safe for free; the
   cheapest correct move is to add no new event type at all (schedules did this).
2. **One `LocalBackend` per workspace, shared across runs.** Dev servers stay
   visible to `list_shells` and to the preview service because of it. It is also
   why run-scoped state (env) must use a `ContextVar`.
3. **`subscribe()` must not `await` between snapshotting the buffer and
   registering the queue.**
4. **Reconcile must not materialize into roots we don't own.** See Skills.
5. **The goal evaluator fails closed** (not met), never open.
6. **Cross-cache invalidation.** `threadKeys.all()` is a separate TanStack Query
   entry from `threadKeys.byWorkspace(id)`. Every invalidation and optimistic
   setter must touch both, or ATTENTION and Activity show conversations the
   workspace sections have already dropped.
7. **Declare literal routes before parameterized ones** (`/active-runs` before
   `/{thread_id}`).
8. **`GET /threads/{id}` must stay unfiltered.**
9. **Unhandled exceptions need hand-set CORS headers.** Starlette's
   `ServerErrorMiddleware` sits *outside* `CORSMiddleware`, so a bare 500 carries
   no `access-control-allow-origin` and the browser reports `TypeError: Failed to
   fetch` — every server bug reads as the backend being down. `main.py` has an
   explicit handler; don't remove it.
10. **Never patch a vendored dependency.** Compose, subclass, or wrap with a
   `PrepareTools` / `AbstractCapability`. When a fix belongs upstream, prepare it
   locally as a patch and hand it over — this repo does not open PRs against
   third-party projects.
11. **Local models are a first-class constraint.** GLM/DeepSeek via vLLM ignore
    tool enums, need the todo board to scaffold, and break on native
    `WebSearchTool` under `OpenAIChatModel`. Anything that narrows the toolset
    should be tested against them, not just against a frontier cloud model.

## 8. Desktop and distribution

The Electron app **owns its backend**: packaged builds ship a frozen standalone
CPython and spawn `uvicorn` themselves, with `LURSOR_DATA_DIR=~/.lursor` so all
writable state stays out of the read-only bundle. The port isn't known at build
time, so the resolved API base is passed to the renderer via
`additionalArguments` and re-exposed as `window.electron.apiBase`. `HashRouter`
in Electron (history routing doesn't work from `file://`), `BrowserRouter` in the
browser.

macOS release builds are signed and notarized. Notarization requires *every*
nested binary to be signed, so `scripts/sign-backend-bundle.cjs` discovers every
Mach-O under `Resources/backend` at `afterPack` rather than maintaining a
`mac.binaries` list that would rot on each dependency bump.

Platform scope: macOS arm64 and Linux x64. The frozen backend is
architecture-specific, so each arch is a full extra build. Windows is unbuilt
(the Electron main process already branches on `win32`; the bundle script is the
missing piece).

Details, secrets and the release runbook: [`docs/DISTRIBUTION.md`](docs/DISTRIBUTION.md).

## 9. Deliberately not built

**Deferred by design:** auth / multi-tenancy (every table already carries a
nullable `user_id`), Docker sandbox execution, MCP + HTTP tool wiring
(`Tool` rows are catalogued but not yet passed to agents), Alembic, encryption or
OS keychain for stored secrets, an always-on scheduler daemon, catch-up fires,
non-cron triggers, chained schedules, a budget ceiling that disables a schedule,
auto-retain of transcripts into Hindsight, terminal-panel env injection, custom
`.claude/commands/*.md` slash commands, virtualized chat timeline, backend
thread pagination, and `.cursor/rules` / `AGENTS.md` ingestion alongside skills.

**Known debt:** `api/chat.py` is ~1900 lines; moving the run engine out of
`app/api/` into `app/agents/` is the right follow-up. `Skill.scope` is a dormant
column left in place so a migration didn't have to rewrite the table.

**laios UI backlog** — the daemon has shipped features with no client surface.
The Lursor side of each is the same four layers (proxy route in `api/laios.py`,
hook in `api/laios.ts`, types, page):

- `GET /v1/models`, `GET /v1/models/{id}` — the whole model-inventory family is
  unconsumed, so the UI can't distinguish *installed on disk* from *in the
  catalog* and shows no run stats (`run_count`, `last_served_at`,
  `available_on_nodes`, `usable_recipes`, live `running_instance`).
- `GET /v1/models/partial` + `DELETE /v1/models/{id}` — reclaim orphaned or
  incomplete downloads (409 when in use).
- `POST /v1/jobs/{id}/cancel` — pull is hard-coupled to serve in
  `useServeManager.start`; there is no download-only path and no cancel.
- `EngineKind::Sglang` is missing from the frontend engine union, so sglang
  models render a broken badge and mis-classify in `serve-model-dialog.tsx`.
  Smallest of these and a real correctness bug.
- `DELETE /v1/cluster/workers/{id}` and `GET /v1/cluster/token` — the cluster
  panel is view-only; `workers[]`/`remotes[]` are typed `unknown[]`.
- Never wired: `GET /v1/metrics/summary`, `/v1/keys`, `/v1/aliases`,
  `/v1/cluster/remotes`, `POST /v1/gateway/restart`. `GET /v1/doctor` is already
  proxied by the backend with zero frontend consumers — a free diagnostics panel.

Also worth carrying upstream: `DELETE /v1/instances/{id}` exists in the daemon
and Lursor uses it, but it is absent from laios's `docs/api.md` and
`openapi.yaml`.
