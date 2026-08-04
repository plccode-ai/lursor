"""Application settings, loaded from environment / .env file.

Single source of truth for runtime configuration. Add new settings here rather
than reading ``os.environ`` throughout the codebase.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo-root/backend directory, used to resolve default relative paths.
BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Environment-driven configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Server ---
    app_name: str = "Lursor"
    debug: bool = True
    # Origins allowed to call the API (the Vite dev server by default). This fork
    # serves its dev UI on :8899 (moved off upstream's :8888 to coexist with the
    # PLCcode admin app); :8888 kept for upstream parity.
    cors_origins: list[str] = [
        "http://localhost:8899",
        "http://127.0.0.1:8899",
        "http://localhost:8888",
        "http://127.0.0.1:8888",
    ]

    # --- Data root ---
    # When set (env ``LURSOR_DATA_DIR``), every on-disk path that isn't explicitly
    # overridden is rebased under this directory. The packaged desktop app sets
    # this to a writable location (``~/.lursor``) because the app bundle itself is
    # read-only — the DB in particular must not live inside the bundle. Left unset
    # for source/dev runs, so existing behaviour (DB next to the backend, data
    # under ``~/.lursor``) is preserved.
    data_dir: Path | None = Field(
        default=None,
        validation_alias=AliasChoices("LURSOR_DATA_DIR", "data_dir"),
    )

    # --- Database ---
    database_url: str = f"sqlite+aiosqlite:///{BACKEND_DIR / 'lursor.db'}"

    # --- Workspaces ---
    # Root directory under which each workspace gets its own folder (named by its
    # id) unless a custom location is supplied. This folder becomes the deep
    # agent's filesystem root when it runs.
    workspaces_dir: Path = Path.home() / ".lursor" / "workspaces"

    # --- Skills ---
    # Root directory under which each skill is a self-contained folder following
    # the Anthropic skill standard: a ``SKILL.md`` (YAML frontmatter + markdown
    # body) plus optional bundled resource files and ``scripts/``. This directory
    # is the source of truth for skill content; the ``skills`` DB table is a
    # rebuildable index (see ``app/skills/store.py`` and ``api/skills.py``).
    skills_dir: Path = Path.home() / ".lursor" / "skills"

    # Workspace-relative directories scanned for repo-committed skills, in
    # precedence order (later roots lose a slug collision). The first is Lursor's
    # own convention and the only one it will create; the rest are read in place
    # because other tools own them. Adding another convention is a config line.
    #
    # ``.agents/skills`` is the tool-agnostic standard (Cursor, opencode, Copilot,
    # Amp and OpenClaw all read it); the rest are each tool's own dotfolder, kept
    # so a repo that predates the standard still lights up.
    #
    # A bare ``skills/`` is included because plenty of repos keep them at the top
    # level rather than under a tool's dotfolder (it is also OpenClaw's workspace
    # convention). It costs nothing when wrong: a folder is only a skill if it
    # holds a ``SKILL.md``, so a ``skills/`` full of anything else stays invisible.
    local_skill_roots: list[str] = [
        ".agents/skills",
        ".claude/skills",
        ".cursor/skills",
        ".codex/skills",
        ".github/skills",
        ".opencode/skills",
        "skills",
    ]

    # Absolute (``~``-expanded) directories of personal skills owned by other
    # tools. In scope for every workspace, at the lowest precedence. Read-only:
    # Lursor never creates these or rebuilds a folder inside one.
    #
    # ``~/.agents/skills`` leads because it is the cross-tool standard — the same
    # convention as our own ``.agents/skills`` — and is therefore the one place a
    # user would expect every agent, Lursor included, to look. The rest are the
    # per-tool homes: Claude Code, Cursor, Codex, Amp (XDG), opencode, OpenClaw,
    # Hermes, Gemini CLI, Antigravity (``~/.gemini/config``) and Copilot CLI.
    #
    # Listing a root a user doesn't have is free: non-existent roots are skipped
    # by ``store.user_skill_roots()``, so this is a menu, not a requirement.
    user_skill_roots: list[str] = [
        "~/.agents/skills",
        "~/.claude/skills",
        "~/.cursor/skills",
        "~/.codex/skills",
        "~/.config/agents/skills",
        "~/.config/opencode/skills",
        "~/.openclaw/skills",
        "~/.hermes/skills",
        "~/.gemini/skills",
        "~/.gemini/config/skills",
        "~/.copilot/skills",
    ]

    # Symlink every skill discovered in a personal root into the catalog, so it
    # shows up in the Skill Studio's file tree (and terminal, and chat) alongside
    # the ones written here — without copying anything, so the file an agent edits
    # is still the file Claude Code reads.
    #
    # This is what makes discovery *managed* rather than merely visible: nothing
    # has to be ingested first. Turning it off leaves personal skills readable and
    # assignable exactly as before, just not present in the catalog directory —
    # ``POST /skills/{id}/link`` then does it one at a time.
    #
    # A folder whose slug the catalog already holds is deliberately left alone:
    # it is currently shadowed by that skill (closest layer wins), and linking it
    # under a suffixed slug would quietly turn one active skill into two.
    auto_link_user_skills: bool = True

    # --- Agents ---
    # Default model used when an agent row does not specify one.
    # Models are served through OpenRouter (prefix "openrouter:").
    default_model: str = "openrouter:qwen/qwen3.7-max"
    # Global default model for ``/compact`` conversation summarization. Compaction
    # is a cheap, throwaway task, so it runs on a small/fast cloud model rather
    # than the (possibly heavy or offline) thread agent's model. An explicit
    # ``AppConfig.compaction_model`` override still wins over this.
    default_compaction_model: str = "openrouter:google/gemini-2.5-flash"
    # How full the context window gets before compaction fires, as a fraction of
    # the model's token budget. This is the *default*; an agent (or subagent) row
    # with ``compaction_threshold`` set overrides it for its own runs. See
    # ``agents/context_budget.py``.
    #
    # Deliberately below pydantic-deep's own 0.9: models degrade well before their
    # stated window is full, and compaction itself needs headroom — at 0.9 a single
    # large tool result can carry the next request past the limit before the
    # summarizer ever gets to run. Raise it per agent for a model that holds up.
    default_compaction_threshold: float = 0.7
    # How much of the history compaction folds into the summary, as a fraction of
    # the token budget: 1.0 summarizes everything (the library's own default and
    # what ``/compact`` has always done), 0.7 summarizes the oldest share and
    # leaves the newest 30% of the budget's worth of messages verbatim. Overridden
    # per row by ``compaction_ratio``.
    default_compaction_ratio: float = 1.0
    # Model used to auto-name a conversation from its first user message. Naming
    # is a tiny, one-shot task fired in the background, so it runs on the smallest
    # fast model rather than the thread agent's (possibly heavy or offline) model.
    default_title_model: str = "openrouter:google/gemini-2.5-flash-lite"
    openrouter_api_key: str | None = None
    # Base URL for OpenRouter's REST API; "/models" is appended to list models.
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # Seconds a streaming model response may go without sending *any* bytes
    # before it's treated as dead. This is httpx's per-read timeout, so it
    # resets on every chunk: a generation that keeps emitting tokens runs as
    # long as it likes, and only a stream that genuinely stops mid-flight
    # trips it. Bounds the two waits that legitimately produce no bytes —
    # gateway queueing and prefill — so it needs headroom over the slowest
    # expected time-to-first-token, not over total generation time.
    #
    # Set because a dropped TLS connection to a remote model gateway strands a
    # run forever otherwise: no FIN arrives, so the client waits on a socket
    # that will never speak again and the turn never fails, never retries, and
    # never returns. Raise it if a slow/queued cluster trips it during prefill.
    model_stream_stall_timeout: float = 300.0

    # --- Web search ---
    # Optional API-key fallbacks for the paid search providers. A key saved on
    # the Settings page (stored on ``AppConfig``) takes precedence over these.
    # The active provider itself is chosen in the UI, not here.
    tavily_api_key: str | None = None
    exa_api_key: str | None = None

    # --- Memory (Hindsight provider) ---
    # Connection fallbacks for the "hindsight" memory provider (see
    # ``agents/hindsight.py``). A value saved on the Settings page (stored on
    # ``AppConfig``) takes precedence over these, exactly like the search keys
    # above. The provider itself is chosen in the UI, not here — so setting only
    # these changes nothing until the provider is switched to "hindsight".
    #
    # ``hindsight_base_url`` may be the hosted API or a self-hosted instance; the
    # Docker image serves the API on :8888 and its own UI on :9999.
    hindsight_base_url: str | None = None
    hindsight_api_key: str | None = None
    # Bank every agent reads and writes. One shared bank for the whole app;
    # isolation between workspaces comes from Hindsight tags, not from separate
    # banks, so pointing this at a bank another tool already fills works as-is.
    hindsight_bank_id: str = "lursor"

    # --- Media / vision ---
    # Where user-attached chat media (images) are stored, one subfolder per
    # thread. Kept out of the DB so message rows stay small.
    media_dir: Path = Path.home() / ".lursor" / "media"
    # Vision-capable model the `view_image` tool calls (via OpenRouter) to answer
    # questions about an image. Runs as an isolated one-shot sub-call so image
    # bytes never enter a text-only agent's context, and lets any agent inspect
    # images regardless of whether its own chat model supports image input. No
    # "openrouter:" prefix — this hits OpenRouter's chat API directly.
    vision_model: str = "google/gemini-2.5-flash-lite"

    # --- Browser QA ---
    # Give executing agents a headless browser so they can *see* and test the web
    # app they build: `view_app` screenshots are analysed by the vision model,
    # console/network errors are captured, and the agent can drive the page
    # (click/type). It also feeds the goal-mode evaluator a live screenshot so
    # completion is judged on what actually rendered, not the transcript. Chromium
    # is downloaded automatically on first use (no user setup). Turn this off to
    # remove the browser tools and visual goal verification entirely.
    browser_qa_enabled: bool = True
    # Run the QA browser without a visible window. Almost always True on a server;
    # set False locally to watch the agent drive the page.
    browser_qa_headless: bool = True

    # --- laios control plane ---
    # Used to auto-seed a "local" laios connection on startup when Lursor runs
    # alongside a daemon (the supervisor injects these). LAIOS_MASTER_KEY takes
    # precedence; otherwise the master_key is parsed from the daemon config file.
    laios_url: str | None = None  # e.g. "http://127.0.0.1:7420"
    laios_master_key: str | None = None
    # Fallback source for the master_key when the env var is unset.
    laios_config_path: str = "~/.laios/config/laios.toml"

    @model_validator(mode="after")
    def _rebase_under_data_dir(self) -> Settings:
        """Rebase writable paths under ``data_dir`` when it is set.

        Only fields the caller did *not* set explicitly (via env / init) are
        rebased, so an explicit ``DATABASE_URL``/``WORKSPACES_DIR``/etc. still
        wins. This is how the packaged app points all writable state at a
        location outside the read-only bundle without the backend caring whether
        it runs frozen or from source.
        """
        if self.data_dir is None:
            return self

        root = self.data_dir.expanduser()
        provided = self.model_fields_set
        if "workspaces_dir" not in provided:
            self.workspaces_dir = root / "workspaces"
        if "skills_dir" not in provided:
            self.skills_dir = root / "skills"
        if "media_dir" not in provided:
            self.media_dir = root / "media"
        if "database_url" not in provided:
            self.database_url = f"sqlite+aiosqlite:///{root / 'lursor.db'}"
        return self

    def ensure_dirs(self) -> None:
        """Create on-disk directories the app relies on."""
        if self.data_dir is not None:
            self.data_dir.expanduser().mkdir(parents=True, exist_ok=True)
        self.workspaces_dir.mkdir(parents=True, exist_ok=True)
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.skills_dir.mkdir(parents=True, exist_ok=True)

    def apply_env(self) -> None:
        """Export provider keys so Pydantic AI's model providers can read them."""
        import os

        if self.openrouter_api_key:
            os.environ.setdefault("OPENROUTER_API_KEY", self.openrouter_api_key)


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
