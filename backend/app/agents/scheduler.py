"""Fires :class:`Schedule` rows on time, in-process.

One ``asyncio`` task, one ``asyncio.sleep``, and an exception handler that logs
and keeps going — the shape of ``preview_service._poll_loop``. Each tick asks the
database one indexed question ("which enabled schedules are due?") and, for each
answer, opens a fresh conversation and hands it to
``chat.start_scheduled_run``. Nothing else here knows how a run works.

Three properties are worth stating because they are the whole design:

**The app's lifetime is the scheduler's lifetime.** Electron spawns this backend
on launch and kills it on quit, so a laptop closed over a weekend *will* miss
fires. Missed fires are **reported, never replayed** (see :func:`reconcile`):
opening the app after a weekend must not launch a burst of billable agent runs
nobody asked for at that moment.

**A schedule never stacks on itself.** If the previous fire is still running when
the next comes due, the new one is recorded ``skipped``. A nightly goal run that
takes six hours must not start a second copy at 3am.

**``next_fire_at`` always rolls forward from *now*, not from the slot that was
due.** Laptop sleep, an NTP jump, or a slow tick can all leave a stale
``next_fire_at``; rolling forward from the current instant makes a late tick fire
once instead of quietly catching up.

Reload safety: a due slot is **claimed** before any work happens (see
:func:`claim`) — a compare-and-swap that moves ``next_fire_at`` forward and only
lets the winner fire. That makes the tick safe against a second firing path (a
hosted deployment wakes an idle instance to fire, so the wake and the local tick
can both see the same row due) and against ``uvicorn --workers > 1``, which would
otherwise multiply this loop into one launch per worker.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.cron import MAX_ELAPSED_COUNT, CronError, elapsed_occurrences, next_fire
from app.db.models import (
    Schedule,
    ScheduleFireStatus,
    ScheduleRun,
    ScheduleRunType,
    Thread,
)
from app.db.session import async_session_factory

logger = logging.getLogger(__name__)

# Cron's finest granularity is a minute, so a 30s tick fires a job at most 30s
# late. Tighter buys nothing; looser risks stepping over a minute entirely.
TICK_SECONDS = 30.0

_task: asyncio.Task[None] | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(moment: datetime) -> datetime:
    """Treat a datetime read back from SQLite (which drops tzinfo) as UTC."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def fire_title(schedule: Schedule, moment: datetime) -> str:
    """Deterministic title for the conversation one fire opens.

    Named up front — from the schedule plus the fire's *local* time, which is the
    clock the user set it by — so the LLM auto-titler is never involved. Local
    rather than UTC because "Nightly deps — Jul 28, 02:00" has to match what the
    schedule's form said it would do.
    """
    stamp = _aware(moment)
    suffix = "UTC"
    with contextlib.suppress(ZoneInfoNotFoundError, ValueError):
        stamp = stamp.astimezone(ZoneInfo(schedule.timezone))
        suffix = ""
    # ``%b %d`` rather than a zero-stripped day: ``%-d`` is a glibc/BSD extension,
    # and this string ends up in a title that has to render the same everywhere.
    return f"{schedule.name} — {stamp.strftime('%b %d, %H:%M')}{f' {suffix}' if suffix else ''}"


def compute_next_fire(schedule: Schedule, *, after: datetime | None = None) -> datetime | None:
    """``schedule``'s next occurrence strictly after ``after`` (default: now).

    ``None`` when the row can't be scheduled at all — a cron expression or
    timezone that no longer parses. The API validates both on write, so this only
    happens to a row edited outside the app; returning ``None`` parks it (never
    due) instead of raising on every tick for the rest of the process's life.
    """
    try:
        return next_fire(schedule.cron, schedule.timezone, after or _now())
    except CronError:
        logger.warning(
            "schedule %s (%s) has an unusable cron/timezone (%r, %r) — never firing it",
            schedule.id,
            schedule.name,
            schedule.cron,
            schedule.timezone,
        )
        return None


async def reconcile(session: AsyncSession) -> int:
    """Startup pass: report the fires the app was closed for, then roll forward.

    For each enabled schedule whose stored ``next_fire_at`` is already in the past,
    count how many occurrences elapsed (capped — see ``cron.MAX_ELAPSED_COUNT``),
    write one ``missed`` history row recording that count, and move
    ``next_fire_at`` to the next future occurrence. **Nothing runs.**

    A schedule with no ``next_fire_at`` yet (just created, or previously disabled)
    simply gets one computed. Returns how many ``missed`` rows were written.

    Call this from startup only, where "in the past" unambiguously means "the
    process wasn't alive for it" — the same reasoning as
    ``chat.reconcile_interrupted_runs``. Running it mid-flight would invent misses
    for fires the tick is about to handle.
    """
    now = _now()
    missed = 0
    schedules = (
        (await session.execute(select(Schedule).where(Schedule.enabled == True)))  # noqa: E712
        .scalars()
        .all()
    )
    for schedule in schedules:
        previous = _aware(schedule.next_fire_at) if schedule.next_fire_at else None
        if previous is not None and previous <= now:
            count = 0
            with contextlib.suppress(CronError):
                # Count from one occurrence *before* the stale slot so the slot
                # itself is included: it was due and it did not run.
                count = elapsed_occurrences(
                    schedule.cron, schedule.timezone, since=previous, until=now
                ) + 1
            session.add(
                ScheduleRun(
                    schedule_id=schedule.id,
                    fired_at=now,
                    status=ScheduleFireStatus.missed,
                    missed_count=min(count, MAX_ELAPSED_COUNT),
                    detail=(
                        "Lursor was not running when this was due. Missed fires are "
                        "reported, never replayed."
                    ),
                )
            )
            missed += 1
        schedule.next_fire_at = compute_next_fire(schedule, after=now)
        schedule.updated_at = now
        session.add(schedule)
    await session.commit()
    if missed:
        logger.info("scheduler: %d schedule(s) missed fires while the app was closed", missed)
    return missed


async def _is_previous_fire_live(session: AsyncSession, schedule_id: str) -> str | None:
    """The thread id of this schedule's still-running previous fire, if any.

    Checks the most recent ``launched`` row against the live run registry, so
    "still running" means what the run manager says rather than what a status
    column claims — the registry is in-memory and therefore always current.
    """
    from app.agents.chat_run_manager import chat_run_manager

    row = (
        await session.execute(
            select(ScheduleRun)
            .where(
                ScheduleRun.schedule_id == schedule_id,
                ScheduleRun.status == ScheduleFireStatus.launched,
            )
            .order_by(ScheduleRun.fired_at.desc())
            .limit(1)
        )
    ).scalars().first()
    if row is None or not row.thread_id:
        return None
    return row.thread_id if chat_run_manager.is_running(row.thread_id) else None


async def claim(session: AsyncSession, schedule: Schedule, *, now: datetime) -> bool:
    """Atomically take ownership of ``schedule``'s due slot. ``True`` if we won it.

    A conditional update — "move ``next_fire_at`` forward, but only if it is still
    the value I read" — so exactly one caller can act on a given slot. Whoever
    loses sees ``rowcount == 0`` and must not fire.

    This exists because firing is no longer single-sourced. A hosted deployment
    reaps idle instances and wakes them shortly before a fire is due, so the wake
    path and this process's own tick can both find the same row due within the
    same second; without the swap that is two agent runs off one cron line. It is
    also the cross-process lock ``uvicorn --workers > 1`` needs.

    The slot rolls forward *before* the work rather than after, which also means a
    fire that crashes outright still leaves the row not-due — the property
    :func:`fire` documents, now guaranteed even if :func:`fire` never returns.
    """
    previous = schedule.next_fire_at
    upcoming = compute_next_fire(schedule, after=now)
    # ``previous`` is used exactly as it was loaded (never tz-normalized): the
    # comparison has to match the stored representation byte for byte, and SQLite
    # holds these as naive strings.
    result = await session.execute(
        update(Schedule)
        .where(Schedule.id == schedule.id, Schedule.next_fire_at == previous)
        .values(next_fire_at=upcoming, updated_at=now)
    )
    await session.commit()
    if result.rowcount != 1:
        logger.info(
            "scheduler: lost the race for schedule %s (%s) — another firer has it",
            schedule.id,
            schedule.name,
        )
        return False
    # ``expire_on_commit`` is False, so the in-memory row still holds the slot we
    # just consumed. Sync it, or anything reading it back sees a stale clock.
    schedule.next_fire_at = upcoming
    schedule.updated_at = now
    return True


async def fire(
    session: AsyncSession,
    schedule: Schedule,
    *,
    now: datetime,
    roll_forward: bool = True,
) -> ScheduleRun:
    """Run one due schedule: open a conversation and launch it, or record why not.

    Always writes exactly one :class:`ScheduleRun` and always leaves
    ``next_fire_at`` in the future, whatever happened — a fire that failed must not
    stay due, or the loop would retry it every 30 seconds forever.

    ``roll_forward=False`` says the caller already moved the clock (it claimed the
    slot — see :func:`claim`) and this must not advance it a second time. The tick
    passes False; "run now", which fires a schedule that was never due, keeps the
    default and consumes the upcoming slot exactly as it always has.
    """
    # Import here: ``api.chat`` imports a good part of the agent stack, and the
    # scheduler is imported from ``main`` during app construction.
    from app.api.chat import start_scheduled_run

    outcome = ScheduleRun(schedule_id=schedule.id, fired_at=now)
    live_thread = await _is_previous_fire_live(session, schedule.id)
    if live_thread is not None:
        outcome.status = ScheduleFireStatus.skipped
        outcome.thread_id = live_thread
        outcome.detail = "The previous fire was still running."
        logger.info("scheduler: skipping %s — previous fire still running", schedule.name)
    else:
        thread = Thread(
            title=fire_title(schedule, now),
            workspace_id=schedule.workspace_id,
            agent_id=schedule.agent_id,
            schedule_id=schedule.id,
            # A goal fire's objective and cap live on the thread, because that is
            # where the existing goal machinery reads them from.
            goal=schedule.prompt if schedule.run_type is ScheduleRunType.goal else "",
            success_criteria=(
                (schedule.success_criteria or schedule.prompt)
                if schedule.run_type is ScheduleRunType.goal
                else ""
            ),
            max_iterations=schedule.max_iterations,
        )
        session.add(thread)
        await session.commit()
        try:
            await start_scheduled_run(
                session, thread=thread, prompt=schedule.prompt, run_type=schedule.run_type
            )
        except Exception as exc:  # noqa: BLE001 — one bad schedule must not stop the loop
            logger.exception("scheduler: failed to launch schedule %s", schedule.name)
            outcome.status = ScheduleFireStatus.error
            outcome.thread_id = thread.id
            outcome.detail = f"{type(exc).__name__}: {exc}"
        else:
            outcome.status = ScheduleFireStatus.launched
            outcome.thread_id = thread.id
            schedule.last_fired_at = now

    session.add(outcome)
    if roll_forward:
        # Forward from *now*, not from the slot that was due: a tick delayed past
        # one or more occurrences then fires once rather than catching up silently.
        schedule.next_fire_at = compute_next_fire(schedule, after=_now())
    schedule.updated_at = now
    session.add(schedule)
    await session.commit()
    return outcome


async def tick() -> int:
    """One pass over the due schedules. Returns how many were acted on.

    Each schedule is claimed before it is fired, so a slot another firer already
    took is silently left alone and not counted. Each fire then runs inside its own
    guard, so a row that blows up is logged and recorded and the rest of the batch
    still runs. Exported (rather than buried in the loop) so tests can drive it with
    a real database and no timers.
    """
    now = _now()
    async with async_session_factory() as session:
        due = (
            (
                await session.execute(
                    select(Schedule)
                    .where(
                        Schedule.enabled == True,  # noqa: E712
                        Schedule.next_fire_at != None,  # noqa: E711
                        Schedule.next_fire_at <= now,
                    )
                    .order_by(Schedule.next_fire_at)
                )
            )
            .scalars()
            .all()
        )
        fired = 0
        for schedule in due:
            try:
                if not await claim(session, schedule, now=now):
                    # Another firer took this slot. Not ours, not an error.
                    continue
                fired += 1
                await fire(session, schedule, now=now, roll_forward=False)
            except Exception:  # noqa: BLE001 — the tick task must be unkillable
                logger.exception("scheduler: tick failed for schedule %s", schedule.id)
                await session.rollback()
    return fired


async def _loop() -> None:
    while True:
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover — never let a tick wedge the scheduler
            logger.exception("scheduler: tick raised; continuing")
        await asyncio.sleep(TICK_SECONDS)


async def start() -> None:
    """Reconcile missed fires, then start the tick task. Idempotent."""
    global _task
    async with async_session_factory() as session:
        await reconcile(session)
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())


async def stop() -> None:
    """Cancel the tick task. In-flight runs are left alone.

    ``ChatRunManager`` owns a run's lifecycle and
    ``chat.reconcile_interrupted_runs`` cleans up whatever a killed process left
    mid-flight, so there is nothing for the scheduler to tear down here.
    """
    global _task
    task, _task = _task, None
    if task is None or task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
