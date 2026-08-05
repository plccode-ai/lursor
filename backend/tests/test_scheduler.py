"""The tick loop and the startup pass, against a real database.

Both are driven directly (``scheduler.tick`` / ``scheduler.reconcile``) rather than
through timers, and the launcher is stubbed in most tests — what is under test is
the *decision*: fire, skip, report as missed, or record an error. The one test that
does launch for real is the regression guard on the ``api/chat`` refactor.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from pydantic_ai.models.test import TestModel
from pydantic_ai_backends import LocalBackend
from pydantic_deep import create_deep_agent, create_default_deps
from sqlmodel import select

from app.agents import scheduler
from app.agents.chat_run_manager import chat_run_manager
from app.db.models import Schedule, ScheduleFireStatus, ScheduleRun, ScheduleRunType, Thread
from app.db.session import async_session_factory


def _fake_deep_agent(row, workspace_path, *args, **kwargs):
    """An offline deep agent (``TestModel``, no tools) — see ``test_goal_chat``."""
    backend = LocalBackend(root_dir=str(workspace_path))
    agent = create_deep_agent(
        model=TestModel(call_tools=[]),
        backend=backend,
        include_subagents=False,
        include_plan=False,
        web_search=False,
        web_fetch=False,
        tool_search=False,
    )
    return agent, create_default_deps(backend)


async def _targets(client: AsyncClient, name: str) -> tuple[str, str]:
    """(workspace_id, agent_id) a schedule can point at."""
    agent = (await client.post("/agents", json={"name": f"{name}Agent"})).json()
    ws = (await client.post("/workspaces", json={"name": f"{name}WS"})).json()
    return ws["id"], agent["id"]


async def _make_schedule(client: AsyncClient, name: str, **overrides) -> Schedule:
    """Insert a schedule directly, so ``next_fire_at`` can be set to the past."""
    workspace_id, agent_id = await _targets(client, name)
    fields = {
        "name": name,
        "workspace_id": workspace_id,
        "agent_id": agent_id,
        "cron": "0 9 * * *",
        "timezone": "UTC",
        "prompt": "check the deps",
        "next_fire_at": datetime.now(UTC) - timedelta(minutes=1),
    }
    fields.update(overrides)
    async with async_session_factory() as session:
        row = Schedule(**fields)
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


async def _runs(schedule_id: str) -> list[ScheduleRun]:
    async with async_session_factory() as session:
        return list(
            (
                await session.execute(
                    select(ScheduleRun)
                    .where(ScheduleRun.schedule_id == schedule_id)
                    .order_by(ScheduleRun.fired_at)
                )
            )
            .scalars()
            .all()
        )


async def _reload(schedule_id: str) -> Schedule:
    async with async_session_factory() as session:
        row = await session.get(Schedule, schedule_id)
        assert row is not None
        return row


def _stub_launcher(monkeypatch, *, fail: bool = False) -> list[dict]:
    """Replace the launcher; return the list of calls it recorded."""
    calls: list[dict] = []

    async def fake_start(session, *, thread, prompt, run_type):
        calls.append({"thread_id": thread.id, "prompt": prompt, "run_type": run_type})
        if fail:
            raise RuntimeError("the agent could not be built")

    monkeypatch.setattr("app.api.chat.start_scheduled_run", fake_start)
    return calls


# --- the tick ------------------------------------------------------------------


async def test_due_schedule_launches_exactly_once(client: AsyncClient, monkeypatch):
    calls = _stub_launcher(monkeypatch)
    row = await _make_schedule(client, "Nightly")

    assert await scheduler.tick() == 1
    # The second tick must find nothing: the first rolled next_fire_at forward.
    assert await scheduler.tick() == 0
    assert len(calls) == 1
    assert calls[0]["prompt"] == "check the deps"

    history = await _runs(row.id)
    assert [r.status for r in history] == [ScheduleFireStatus.launched]
    assert history[0].thread_id == calls[0]["thread_id"]

    refreshed = await _reload(row.id)
    assert refreshed.last_fired_at is not None
    assert refreshed.next_fire_at is not None
    assert refreshed.next_fire_at.replace(tzinfo=UTC) > datetime.now(UTC)


async def test_disabled_schedule_never_fires(client: AsyncClient, monkeypatch):
    calls = _stub_launcher(monkeypatch)
    row = await _make_schedule(client, "Paused", enabled=False)

    assert await scheduler.tick() == 0
    assert calls == []
    assert await _runs(row.id) == []


async def test_future_schedule_is_not_due(client: AsyncClient, monkeypatch):
    calls = _stub_launcher(monkeypatch)
    await _make_schedule(
        client, "Later", next_fire_at=datetime.now(UTC) + timedelta(hours=1)
    )
    assert await scheduler.tick() == 0
    assert calls == []


async def test_fire_creates_a_thread_stamped_with_the_schedule(
    client: AsyncClient, monkeypatch
):
    _stub_launcher(monkeypatch)
    row = await _make_schedule(client, "Stamped")
    await scheduler.tick()

    async with async_session_factory() as session:
        threads = list(
            (
                await session.execute(select(Thread).where(Thread.schedule_id == row.id))
            )
            .scalars()
            .all()
        )
    assert len(threads) == 1
    # Titled deterministically up front, so the LLM auto-titler is never involved.
    assert threads[0].title.startswith("Stamped — ")
    assert threads[0].workspace_id == row.workspace_id
    assert threads[0].agent_id == row.agent_id

    # It shows up in the workspace's conversation list like any other thread — the
    # sidebar's running badge and unread mark *are* that list, so leaving it out
    # would hide the only signal that an overnight run happened. `schedule_id` is
    # what lets the row be marked as machine-started.
    listed = (await client.get(f"/threads?workspace_id={row.workspace_id}")).json()
    assert [t["id"] for t in listed] == [threads[0].id]
    assert listed[0]["schedule_id"] == row.id

    # Fetching by id is never filtered: that is how the chat surface resolves a
    # conversation the list might not carry, and so where its title and agent
    # come from.
    fetched = await client.get(f"/threads/{threads[0].id}")
    assert fetched.status_code == 200
    assert fetched.json()["schedule_id"] == row.id
    assert fetched.json()["agent_id"] == row.agent_id

    # One schedule's runs, for the Schedules page.
    by_schedule = (await client.get(f"/threads?schedule_id={row.id}")).json()
    assert [t["id"] for t in by_schedule] == [threads[0].id]

    # And the escape hatch for when the volume becomes the problem.
    excluded = (
        await client.get(
            f"/threads?workspace_id={row.workspace_id}&include_scheduled=false"
        )
    ).json()
    assert excluded == []


async def test_goal_fire_stamps_the_objective_onto_its_thread(
    client: AsyncClient, monkeypatch
):
    _stub_launcher(monkeypatch)
    row = await _make_schedule(
        client,
        "GoalJob",
        run_type=ScheduleRunType.goal,
        prompt="keep the docs current",
        success_criteria="every doc matches the code",
        max_iterations=7,
    )
    await scheduler.tick()

    async with async_session_factory() as session:
        thread = (
            (await session.execute(select(Thread).where(Thread.schedule_id == row.id)))
            .scalars()
            .one()
        )
    # The existing goal machinery reads all three off the thread, unchanged.
    assert thread.goal == "keep the docs current"
    assert thread.success_criteria == "every doc matches the code"
    assert thread.max_iterations == 7


async def test_a_live_previous_fire_is_skipped_not_stacked(
    client: AsyncClient, monkeypatch
):
    """A nightly goal run that takes six hours must not start a second copy."""
    _stub_launcher(monkeypatch)
    row = await _make_schedule(client, "Slow")
    await scheduler.tick()
    first = (await _runs(row.id))[0]
    assert first.thread_id is not None

    # Make that run look live to the registry, then make the schedule due again.
    chat_run_manager._status[first.thread_id] = "running"
    try:
        async with async_session_factory() as session:
            due = await session.get(Schedule, row.id)
            due.next_fire_at = datetime.now(UTC) - timedelta(seconds=1)
            session.add(due)
            await session.commit()
        assert await scheduler.tick() == 1
    finally:
        chat_run_manager._status.pop(first.thread_id, None)

    history = await _runs(row.id)
    assert [r.status for r in history] == [
        ScheduleFireStatus.launched,
        ScheduleFireStatus.skipped,
    ]
    assert history[1].detail
    # Skipping still rolls the clock forward — a skipped fire must not stay due.
    assert (await _reload(row.id)).next_fire_at.replace(tzinfo=UTC) > datetime.now(UTC)


async def test_a_failing_launch_records_an_error_and_the_loop_survives(
    client: AsyncClient, monkeypatch
):
    _stub_launcher(monkeypatch, fail=True)
    row = await _make_schedule(client, "Broken")

    assert await scheduler.tick() == 1
    history = await _runs(row.id)
    assert [r.status for r in history] == [ScheduleFireStatus.error]
    assert "the agent could not be built" in history[0].detail
    # The tick still rolled forward, so the failure isn't retried every 30s forever.
    assert (await _reload(row.id)).next_fire_at.replace(tzinfo=UTC) > datetime.now(UTC)


async def test_an_unusable_cron_parks_the_row_instead_of_wedging_the_loop(
    client: AsyncClient, monkeypatch
):
    """Only reachable for a row edited outside the API, which validates on write."""
    _stub_launcher(monkeypatch)
    row = await _make_schedule(client, "Corrupt", cron="not a cron")

    assert await scheduler.tick() == 1
    assert (await _runs(row.id))[0].status == ScheduleFireStatus.launched
    # Never due again, rather than raising on every tick for the process's life.
    assert (await _reload(row.id)).next_fire_at is None
    assert await scheduler.tick() == 0


# --- claiming the slot ---------------------------------------------------------


async def test_a_slot_can_only_be_claimed_once(client: AsyncClient, monkeypatch):
    """The compare-and-swap that stops two firers acting on one cron line."""
    _stub_launcher(monkeypatch)
    row = await _make_schedule(client, "Contended")

    async with async_session_factory() as first, async_session_factory() as second:
        a = await first.get(Schedule, row.id)
        b = await second.get(Schedule, row.id)
        # Both readers saw the same due slot, exactly as two firers would.
        assert a.next_fire_at == b.next_fire_at

        now = datetime.now(UTC)
        assert await scheduler.claim(first, a, now=now) is True
        # The loser's ``next_fire_at`` no longer matches what is stored, so its
        # conditional update touches nothing.
        assert await scheduler.claim(second, b, now=now) is False

    # And the winner's clock is the one that landed.
    assert (await _reload(row.id)).next_fire_at.replace(tzinfo=UTC) > datetime.now(UTC)


async def test_concurrent_ticks_launch_a_due_schedule_exactly_once(
    client: AsyncClient, monkeypatch
):
    """Two firing paths racing on one due row must produce one agent run.

    This is the scenario a hosted deployment creates: an idle instance is woken
    shortly before a fire is due, so the wake and the instance's own tick can both
    find the row due within the same second.
    """
    import asyncio

    calls = _stub_launcher(monkeypatch)
    row = await _make_schedule(client, "Raced")

    results = await asyncio.gather(scheduler.tick(), scheduler.tick())

    # Exactly one tick claimed the slot; the other found it already taken.
    assert sorted(results) == [0, 1]
    assert len(calls) == 1
    assert [r.status for r in await _runs(row.id)] == [ScheduleFireStatus.launched]


# --- the startup pass ----------------------------------------------------------


async def test_startup_reports_missed_fires_and_launches_nothing(
    client: AsyncClient, monkeypatch
):
    """Opening the app after a weekend must not fire a burst of billable runs."""
    calls = _stub_launcher(monkeypatch)
    three_days_ago = datetime.now(UTC) - timedelta(days=3)
    row = await _make_schedule(client, "Weekend", next_fire_at=three_days_ago)

    async with async_session_factory() as session:
        assert await scheduler.reconcile(session) == 1

    assert calls == []
    history = await _runs(row.id)
    assert [r.status for r in history] == [ScheduleFireStatus.missed]
    # The stale slot plus each daily occurrence since.
    assert history[0].missed_count == 4
    assert history[0].detail

    refreshed = await _reload(row.id)
    assert refreshed.next_fire_at.replace(tzinfo=UTC) > datetime.now(UTC)
    # And with the clock rolled forward, the next tick has nothing to do.
    assert await scheduler.tick() == 0


async def test_startup_only_computes_a_clock_for_a_fresh_schedule(
    client: AsyncClient, monkeypatch
):
    _stub_launcher(monkeypatch)
    row = await _make_schedule(client, "Fresh", next_fire_at=None)

    async with async_session_factory() as session:
        assert await scheduler.reconcile(session) == 0

    assert await _runs(row.id) == []
    assert (await _reload(row.id)).next_fire_at is not None


async def test_startup_ignores_disabled_schedules(client: AsyncClient, monkeypatch):
    _stub_launcher(monkeypatch)
    row = await _make_schedule(
        client, "Off", enabled=False, next_fire_at=datetime.now(UTC) - timedelta(days=9)
    )
    async with async_session_factory() as session:
        assert await scheduler.reconcile(session) == 0
    assert await _runs(row.id) == []


# --- the launch path (the §3 refactor's regression guard) ----------------------


async def test_start_scheduled_run_persists_a_cron_turn_and_registers_the_run(
    client: AsyncClient, monkeypatch
):
    """The real launcher, against a real DB with a stubbed model.

    This is the test that catches a regression in the ``api/chat`` extraction: it
    asserts a headless fire persists its synthetic turn, actually reaches
    ``chat_run_manager``, and streams an assistant reply into the thread — with no
    HTTP request behind any of it.
    """
    monkeypatch.setattr("app.api.chat.build_deep_agent", _fake_deep_agent)
    row = await _make_schedule(client, "RealRun", prompt="summarize today's commits")

    assert await scheduler.tick() == 1
    history = await _runs(row.id)
    assert [r.status for r in history] == [ScheduleFireStatus.launched]
    thread_id = history[0].thread_id
    assert thread_id

    # The run is detached, so wait for the manager to report it terminal.
    task = chat_run_manager._tasks.get(thread_id)
    if task is not None:
        await task
    assert not chat_run_manager.is_running(thread_id)

    messages = (await client.get(f"/threads/{thread_id}/messages")).json()
    user_turns = [m for m in messages if m["role"] == "user"]
    assert len(user_turns) == 1
    assert user_turns[0]["content"] == "summarize today's commits"
    # Tagged so the transcript reads as machine-originated, not as a typed message.
    assert user_turns[0]["kind"] == "cron"
    assert any(m["role"] == "assistant" and m["content"] for m in messages)

    # Spend is attributed to the schedule, so Analytics can break it out.
    usage = (await client.get("/analytics/summary?kind=cron")).json()
    assert usage["total_tokens"] > 0
