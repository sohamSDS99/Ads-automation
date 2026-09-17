"""The schedule poller: one arq cron tick a minute, DB-driven.

arq's own `cron()` binds a schedule at import time. Ours live in a table an
admin edits from `/settings`, so what runs every minute is a *poll*: find the
schedules that are due, launch them, and compute when each one is next due.

Three properties this has to hold, in decreasing order of how badly it hurts to
get them wrong:

1. **A due schedule fires once.** The claim is a conditional `UPDATE … WHERE
   next_at <= now RETURNING id`, committed *before* the launch. Whichever worker
   wins that update owns the firing; the other sees zero rows and moves on. It
   is deliberately not `SELECT … FOR UPDATE` held across the launch: `launch()`
   commits, and a commit inside a loop would release the locks on every
   schedule still waiting its turn.
2. **A missed window does not stampede.** If the worker was down for a day, a
   daily schedule is one tick behind, not 24. `next_at` is recomputed from *now*,
   not from the missed slot, so a catch-up fires once and then resumes cadence.
3. **A project that is already running is not an error.** The run lock is the
   authority (PRD §15 NF5d); a schedule that lands on top of a manual run logs
   the skip and waits for its next slot. Queuing behind it would mean a
   45-minute run followed immediately by a duplicate of itself.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import sqlalchemy as sa
import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import Project, Run, RunTrigger, Schedule
from agent.orchestrator.launch import LaunchRequest, ProjectBusy, QueueUnavailable, launch
from agent.scheduling import cron

log = structlog.get_logger(__name__)

#: What the run header shows instead of a person's name (PRD §13.4 B).
SCHEDULE_ACTOR_NAME = "Schedule"

#: Past this, a firing is logged as a missed window rather than a late tick. It
#: still runs, once. Sized to outlast a deploy, not a weekend.
LATE_TOLERANCE_SECONDS = 15 * 60


class ScheduleUnrunnable(RuntimeError):
    """The schedule points at something that is no longer there."""


@dataclass(frozen=True, slots=True)
class PollOutcome:
    """What one tick did. Returned so tests can assert on it, and logged either way."""

    considered: int = 0
    launched: tuple[uuid.UUID, ...] = ()
    skipped_busy: tuple[uuid.UUID, ...] = ()
    failed: tuple[uuid.UUID, ...] = ()
    disabled: tuple[uuid.UUID, ...] = ()

    @property
    def did_something(self) -> bool:
        return bool(self.launched or self.skipped_busy or self.failed or self.disabled)


async def poll_schedules(
    db: AsyncSession, redis: Redis, *, now: datetime | None = None
) -> PollOutcome:
    """Launch every schedule that is due. Never raises — one bad row cannot stop the rest."""
    moment = now or datetime.now(UTC)
    candidates = await _due_ids(db, moment)
    if not candidates:
        return PollOutcome()

    launched: list[uuid.UUID] = []
    busy: list[uuid.UUID] = []
    failed: list[uuid.UUID] = []
    disabled: list[uuid.UUID] = []

    for schedule_id in candidates:
        try:
            claimed = await _claim(db, schedule_id, moment)
        except cron.CronError as exc:
            # The stored expression no longer parses — an expression that was
            # valid when written cannot become invalid, so this is a hand-edited
            # row. Disable it rather than retry it every minute forever.
            await _disable(db, schedule_id, reason=str(exc))
            disabled.append(schedule_id)
            continue
        if claimed is None:
            continue  # another poller took this firing, or it is no longer due

        try:
            run = await _fire(db, redis, claimed, moment)
        except ProjectBusy as conflict:
            busy.append(schedule_id)
            log.info(
                "schedule.skipped_locked",
                schedule_id=str(schedule_id),
                project_id=str(claimed.project_id),
                holder_run_id=str(conflict.holder.run_id),
            )
        except (QueueUnavailable, ScheduleUnrunnable) as exc:
            failed.append(schedule_id)
            log.warning("schedule.failed", schedule_id=str(schedule_id), error=str(exc))
        except Exception as exc:  # noqa: BLE001 — a cron tick must survive one bad row
            failed.append(schedule_id)
            log.error("schedule.unexpected_error", schedule_id=str(schedule_id), error=str(exc))
            await db.rollback()
        else:
            launched.append(run.id)
            log.info(
                "schedule.launched",
                schedule_id=str(schedule_id),
                run_id=str(run.id),
                project_id=str(claimed.project_id),
            )

    outcome = PollOutcome(
        considered=len(candidates),
        launched=tuple(launched),
        skipped_busy=tuple(busy),
        failed=tuple(failed),
        disabled=tuple(disabled),
    )
    if outcome.did_something:
        log.info(
            "schedule.poll",
            considered=outcome.considered,
            launched=len(outcome.launched),
            skipped_busy=len(outcome.skipped_busy),
            failed=len(outcome.failed),
            disabled=len(outcome.disabled),
        )
    return outcome


async def _due_ids(db: AsyncSession, moment: datetime) -> list[uuid.UUID]:
    """Everything that looks due. The claim below is what settles who runs it."""
    result = await db.execute(
        sa.select(Schedule.id)
        .where(
            Schedule.enabled.is_(True),
            Schedule.next_at.is_not(None),
            Schedule.next_at <= moment,
        )
        .order_by(Schedule.next_at)
    )
    ids = list(result.scalars().all())
    await db.rollback()  # end the read transaction; each claim opens its own
    return ids


@dataclass(frozen=True, slots=True)
class ClaimedFiring:
    """A firing this worker owns, as plain values rather than an ORM row.

    Deliberately not a `Schedule` instance. `launch()` commits on success and
    **rolls back** on `ProjectBusy`, and either one expires every object in the
    session — so reading `schedule.project_id` afterwards fires a lazy load. In
    a cron job that load happens inside an `except` block, where the resulting
    `MissingGreenlet` is not caught by anything and takes the whole tick down
    with it. Values copied at claim time cannot expire.
    """

    schedule_id: uuid.UUID
    project_id: uuid.UUID
    workspace_id: uuid.UUID
    due_at: datetime


async def _claim(
    db: AsyncSession, schedule_id: uuid.UUID, moment: datetime
) -> ClaimedFiring | None:
    """Advance `next_at` past this firing, atomically. Returns the row if we won it.

    Advancing *before* launching is the deliberate order. If the launch then
    fails, the schedule has already moved to its next slot — which is what the
    module docstring promises, and the alternative (retry every minute until it
    succeeds) turns one broken project into a per-minute log flood.
    """
    schedule = await db.get(Schedule, schedule_id)
    if schedule is None or not schedule.enabled or schedule.next_at is None:
        await db.rollback()
        return None

    # Read everything the rest of this tick needs before anything commits.
    due_at = schedule.next_at
    project_id = schedule.project_id
    workspace_id = schedule.workspace_id
    # Raises CronError to the caller, which disables the row.
    upcoming = next_at_for(schedule, after=moment)

    result = await db.execute(
        sa.update(Schedule)
        .where(
            Schedule.id == schedule_id,
            Schedule.enabled.is_(True),
            Schedule.next_at.is_not(None),
            Schedule.next_at <= moment,
        )
        .values(next_at=upcoming)
        .returning(Schedule.id)
    )
    won = result.scalar_one_or_none() is not None
    await db.commit()
    if not won:
        return None
    return ClaimedFiring(
        schedule_id=schedule_id,
        project_id=project_id,
        workspace_id=workspace_id,
        due_at=due_at,
    )


async def _disable(db: AsyncSession, schedule_id: uuid.UUID, *, reason: str) -> None:
    await db.execute(
        sa.update(Schedule).where(Schedule.id == schedule_id).values(enabled=False, next_at=None)
    )
    await db.commit()
    log.warning("schedule.disabled_unparseable", schedule_id=str(schedule_id), error=reason)


async def _fire(db: AsyncSession, redis: Redis, claimed: ClaimedFiring, moment: datetime) -> Run:
    project = await db.get(Project, claimed.project_id)
    if project is None:  # pragma: no cover — FK is ON DELETE CASCADE
        raise ScheduleUnrunnable(f"schedule {claimed.schedule_id} has no project")

    late = lateness_seconds(claimed.due_at, moment)
    if late > LATE_TOLERANCE_SECONDS:
        # Worth a line of its own: the run is about to be stamped with a
        # `started_at` well after the slot it belongs to, and this is why.
        log.info(
            "schedule.late",
            schedule_id=str(claimed.schedule_id),
            due_at=claimed.due_at.isoformat(),
            late_seconds=int(late),
        )

    run = await launch(
        db,
        redis,
        LaunchRequest(
            project=project,
            workspace_id=claimed.workspace_id,
            trigger=RunTrigger.SCHEDULE,
            # NULL, and this is the one place in the product allowed to pass
            # None here (PRD §6, §15 NF5c).
            actor_id=None,
            actor_name=SCHEDULE_ACTOR_NAME,
            audit_meta={"schedule_id": str(claimed.schedule_id)},
        ),
    )
    # An explicit UPDATE rather than an attribute assignment: `launch()` has
    # just committed, so the `Schedule` instance in this session is expired and
    # touching it would re-load it for no reason.
    await db.execute(
        sa.update(Schedule).where(Schedule.id == claimed.schedule_id).values(last_run_id=run.id)
    )
    await db.commit()
    return run


def next_at_for(schedule: Schedule, *, after: datetime) -> datetime:
    """The schedule's next firing. Shared with the API so both agree on the answer."""
    return cron.next_fire(cron.parse(schedule.cron), after=after, timezone=schedule.timezone)


def lateness_seconds(due_at: datetime | None, moment: datetime) -> float:
    """How overdue a firing was. Zero when it was on time or `due_at` is unset."""
    if due_at is None:
        return 0.0
    return max((moment - due_at).total_seconds(), 0.0)
