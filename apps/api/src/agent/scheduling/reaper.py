"""Close out runs whose worker died. PRD §16, "Worker killed".

A run in `running` with no heartbeat is not slow, it is orphaned: the process
that was executing it is gone, and nothing else will ever finish it. Left alone
it shows as in-flight in the console forever, holds the project's run lock for
the lock's full two hours, and blocks the next launch behind a run nobody is
waiting for.

What the reaper does *not* do is throw work away. Every completed `NodeRun` is
still there and still cached by input hash, so `retry-failed` re-enters the run
and re-executes only what was in flight (PRD §15 NF3). The reaper's whole job is
to make the run say what actually happened to it.

Two populations are swept, and they are different failures:

* **`running` with an expired heartbeat** — the documented case. Detected in
  under six minutes.
* **`queued` for longer than the grace period** — the row committed and the
  enqueue returned, but no worker ever picked the job up (Redis flushed, the
  queue drained during a deploy). The symptom an operator sees is identical: a
  run that never starts and a project that cannot be launched. The grace is
  deliberately much longer than the heartbeat window, because a busy queue is a
  normal reason to sit in `queued` and the reaper must never race a worker that
  is about to start.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from agent.audit import AuditAction, AuditTarget, write_audit
from agent.db.models import AuditLog, Run, RunStatus
from agent.orchestrator import approvals
from agent.orchestrator.events import EventType, RunEventStream
from agent.orchestrator.heartbeat import STALE_AFTER_SECONDS, is_alive
from agent.orchestrator.state import RunLock, RunStore

log = structlog.get_logger(__name__)

#: How long a run may sit in `queued` before it is presumed lost. An order of
#: magnitude above the heartbeat window: `max_jobs = 4` means a fifth run really
#: can wait behind four 45-minute ones, and reaping it would be a bug that only
#: shows up under load.
QUEUED_GRACE_SECONDS = 2 * 60 * 60

REAPED_ERROR = {
    "code": "reaped",
    "message": (
        "The worker running this stopped without finishing. Nothing completed was lost — "
        "re-run it to pick up from the last checkpoint."
    ),
}


@dataclass(frozen=True, slots=True)
class ReapOutcome:
    """Which runs were closed out, split by why."""

    orphaned: tuple[uuid.UUID, ...] = field(default_factory=tuple)
    never_started: tuple[uuid.UUID, ...] = field(default_factory=tuple)

    @property
    def total(self) -> int:
        return len(self.orphaned) + len(self.never_started)


async def reap_stale_runs(
    db: AsyncSession, redis: Redis, *, now: datetime | None = None
) -> ReapOutcome:
    """Fail every run whose worker is gone. Safe to call concurrently and at startup."""
    moment = now or datetime.now(UTC)
    orphaned: list[uuid.UUID] = []
    never_started: list[uuid.UUID] = []

    for run_id in await _candidates(db, RunStatus.RUNNING):
        if await is_alive(redis, run_id):
            continue
        if await _reap(db, redis, run_id, reason="worker_died", moment=moment):
            orphaned.append(run_id)

    cutoff = moment - timedelta(seconds=QUEUED_GRACE_SECONDS)
    for run_id in await _candidates(db, RunStatus.QUEUED, queued_before=cutoff):
        if await is_alive(redis, run_id):
            # A worker picked it up between the query and now, and has not yet
            # committed `running`. Leave it alone.
            continue
        if await _reap(db, redis, run_id, reason="never_started", moment=moment):
            never_started.append(run_id)

    outcome = ReapOutcome(orphaned=tuple(orphaned), never_started=tuple(never_started))
    if outcome.total:
        log.warning(
            "reaper.swept",
            orphaned=len(outcome.orphaned),
            never_started=len(outcome.never_started),
        )
    return outcome


async def _candidates(
    db: AsyncSession, status: RunStatus, *, queued_before: datetime | None = None
) -> list[uuid.UUID]:
    stmt = sa.select(Run.id).where(Run.status == status)
    if queued_before is not None:
        # `Run` has no `created_at` column — PRD §6 gives it `started_at`, which
        # a queued run has not reached. The audit row written in the same
        # transaction as the run (law 7) is therefore the only record of when it
        # was asked for, and it is guaranteed to exist for every launch.
        asked_for = (
            sa.select(AuditLog.target_id)
            .where(
                AuditLog.action == AuditAction.RUN_LAUNCHED.value,
                AuditLog.target_type == AuditTarget.RUN.value,
                AuditLog.created_at < queued_before,
                AuditLog.target_id.is_not(None),
            )
            .scalar_subquery()
        )
        stmt = stmt.where(Run.started_at.is_(None), Run.id.in_(asked_for))
    result = await db.execute(stmt)
    ids = list(result.scalars().all())
    await db.rollback()
    return ids


async def _reap(
    db: AsyncSession, redis: Redis, run_id: uuid.UUID, *, reason: str, moment: datetime
) -> bool:
    """Close one run out. Returns False if it finished on its own in the meantime."""
    store = RunStore(db)
    loaded = await store.load(run_id)
    if loaded is None:  # pragma: no cover — it was there a moment ago
        await db.rollback()
        return False
    run, _project = loaded
    if run.status not in (RunStatus.RUNNING, RunStatus.QUEUED):
        await db.rollback()
        return False

    # Read before anything commits. `finish_run` commits, which expires every
    # object in this session — and a lazy re-load fired from inside a cron job
    # raises `MissingGreenlet`, which nothing here would catch.
    project_id = run.project_id
    workspace_id = run.workspace_id

    # Nodes left `running` belong to the dead process. Closing them is what
    # stops the console showing a node as in-flight for ever, and what lets
    # `retry-failed` see them as retryable rather than as still going.
    crashed = await store.fail_stale_running(run_id)
    error = {**REAPED_ERROR, "reason": reason, "nodes_in_flight": crashed}
    await store.finish_run(run, status=RunStatus.FAILED, error=error)
    cost_usd = str(run.cost_usd)
    expired = await approvals.expire_pending(db, run_id)

    write_audit(
        db,
        workspace_id=workspace_id,
        # NULL: no person did this. PRD §15 NF5c allows exactly this shape, and
        # `meta.reason` is what makes the row answerable.
        actor_id=None,
        action=AuditAction.RUN_REAPED,
        target_type=AuditTarget.RUN,
        target_id=run_id,
        meta={
            "reason": reason,
            "project_id": str(project_id),
            "nodes_in_flight": crashed,
            "approvals_expired": expired,
            "stale_after_seconds": STALE_AFTER_SECONDS,
        },
    )
    await db.commit()

    # Only after the row is durable. Releasing the lock first would let a new
    # run start against a project whose previous run still claims to be running.
    await RunLock(redis).release(project_id, run_id)
    await RunEventStream(redis, run_id).publish(
        EventType.RUN_COMPLETED,
        run_id=str(run_id),
        status=RunStatus.FAILED,
        cost_usd=cost_usd,
        error=error,
    )
    log.warning(
        "run.reaped",
        run_id=str(run_id),
        reason=reason,
        project_id=str(project_id),
        nodes_in_flight=crashed,
        at=moment.isoformat(),
    )
    return True
