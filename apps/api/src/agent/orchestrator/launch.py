"""Starting a run, in one place.

Until P8 there was exactly one caller — `POST /projects/{id}/runs` — so the
sequence lived in the route. The scheduler is the second caller, and a second
copy of "take the lock, write the row, audit it, enqueue it, and unwind all of
that if the queue is down" is precisely the kind of duplication that let P6's
wizard write a settings key nothing read. So the sequence moved here and the
route calls it.

The order is load-bearing and is the same for both callers:

1. Write the `Run` row and flush, so it has an id.
2. Take the project lock *with that id* — the loser never sees a run that is
   about to be discarded (PRD §15 NF5d).
3. Audit in the same transaction as the row (PRD §18 law 7).
4. Commit. Only then enqueue: a job that starts against an uncommitted run
   finds nothing.
5. If the enqueue fails, fail the run and release the lock, or the project is
   unrunnable until the lock's two-hour TTL expires.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from agent.audit import AuditAction, AuditTarget, write_audit
from agent.db.models import Project, Run, RunMode, RunStage, RunStatus, RunTrigger
from agent.db.repos import RunRepo
from agent.orchestrator.events import EventType, RunEventStream
from agent.orchestrator.state import LockHolder, RunLock, RunStore
from agent.queue import enqueue_run

log = structlog.get_logger(__name__)


class ProjectBusy(RuntimeError):
    """Another run holds this project's lock. Carries the holder for the 409 body."""

    def __init__(self, holder: LockHolder) -> None:
        super().__init__(f"project is already running as {holder.run_id}")
        self.holder = holder


class QueueUnavailable(RuntimeError):
    """The row is committed but arq would not take the job."""

    def __init__(self, run: Run, cause: Exception) -> None:
        super().__init__(str(cause))
        self.run = run
        self.cause = cause


@dataclass(frozen=True, slots=True)
class LaunchRequest:
    """Everything a launch needs that does not come from the project itself."""

    project: Project
    workspace_id: uuid.UUID
    trigger: RunTrigger
    #: NULL for `trigger=schedule`, and only then (PRD §6, §15 NF5c).
    actor_id: uuid.UUID | None
    #: Shown in the 409 body and the run header. "Schedule" for cron runs.
    actor_name: str
    node_ids: set[str] = field(default_factory=set)
    reuse_cache: bool = True
    ip: str | None = None
    #: Which pipeline to run. A plan launch takes a different lock, a
    #: different DAG and a different audit action; everything else about
    #: "take the lock, write the row, audit it, enqueue it, and unwind all of
    #: that if the queue is down" is identical, which is why it stays here
    #: rather than becoming a second copy in `routes_plan`.
    stage: RunStage = RunStage.RESEARCH
    #: The accepted research run a plan consumes. Required for `stage=plan`
    #: and forbidden otherwise — `ck_run_plan_has_source` is the backstop.
    source_run_id: uuid.UUID | None = None
    #: sha256 of the `PlanInput` or `GuidelineInput` this run was built from.
    input_hash: str | None = None
    #: The bindings that actually resolved, for `stage=guideline`. Required
    #: there and forbidden elsewhere — `ck_run_guideline_has_bindings` is the
    #: backstop, and it insists on a JSON object rather than merely not-NULL.
    bindings: dict[str, str | int | None] | None = None
    #: Extra fields folded into the audit row — how the scheduler records which
    #: schedule fired.
    audit_meta: dict[str, str | None] = field(default_factory=dict)


async def launch(db: AsyncSession, redis: Redis, request: LaunchRequest) -> Run:
    """Create, lock, record and enqueue one run. Raises `ProjectBusy` or `QueueUnavailable`."""
    project = request.project
    lock = RunLock(redis, request.stage)
    runs = RunRepo(db, request.workspace_id)
    previous = await runs.latest_succeeded(project.id, stage=request.stage)

    run = Run(
        project_id=project.id,
        stage=request.stage,
        source_run_id=request.source_run_id,
        input_hash=request.input_hash,
        bindings=request.bindings,
        triggered_by=request.actor_id,
        trigger=request.trigger,
        status=RunStatus.QUEUED,
        mode=RunMode.PARTIAL if request.node_ids else RunMode.FULL,
        node_filter=(
            {"node_ids": sorted(request.node_ids), "reuse_cache": request.reuse_cache}
            if request.node_ids
            else {"reuse_cache": request.reuse_cache}
        ),
        # What this run is a delta against. Set at launch rather than at report
        # time so the pointer records the run that was actually current when
        # this one started, not whichever run happens to be newest later.
        parent_run_id=previous.id if previous else None,
    )
    runs.add(run)
    await db.flush()

    holder = await lock.acquire(
        project.id,
        LockHolder(run_id=run.id, user_id=request.actor_id, user_name=request.actor_name),
    )
    if holder is not None:
        await db.rollback()
        raise ProjectBusy(holder)

    write_audit(
        db,
        workspace_id=request.workspace_id,
        actor_id=request.actor_id,
        action={
            RunStage.PLAN: AuditAction.PLAN_STARTED,
            RunStage.GUIDELINE: AuditAction.GUIDELINE_STARTED,
        }.get(request.stage, AuditAction.RUN_LAUNCHED),
        target_type=AuditTarget.RUN,
        target_id=run.id,
        meta={
            "project_id": str(project.id),
            "stage": request.stage.value,
            "source_run_id": str(request.source_run_id) if request.source_run_id else None,
            "input_hash": request.input_hash,
            "bindings": request.bindings,
            "trigger": request.trigger.value,
            "mode": run.mode.value,
            "node_ids": sorted(request.node_ids) if request.node_ids else None,
            "reuse_cache": request.reuse_cache,
            "parent_run_id": str(run.parent_run_id) if run.parent_run_id else None,
            **request.audit_meta,
        },
        ip=request.ip,
    )
    try:
        await db.commit()
    except Exception:
        await lock.release(project.id, run.id)
        raise

    events = RunEventStream(redis, run.id)
    await events.publish(EventType.RUN_STATUS, run_id=str(run.id), status=RunStatus.QUEUED)

    try:
        await enqueue_run(run.id)
    except Exception as exc:  # noqa: BLE001 — the row exists; report it rather than 500 blind
        log.error("run.enqueue_failed", run_id=str(run.id), error=str(exc))
        await RunStore(db).finish_run(
            run,
            status=RunStatus.FAILED,
            error={"code": "enqueue_failed", "message": str(exc)},
        )
        await lock.release(project.id, run.id)
        raise QueueUnavailable(run, exc) from exc

    return run
