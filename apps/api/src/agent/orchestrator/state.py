"""Run state: what is checkpointed in Postgres, and what coordinates in Redis.

PRD §18 law 5 — "everything resumable. Persist a NodeRun before moving to the
next node" — is this module's whole job. Every state transition commits before
the executor takes another step, so a worker that dies between two nodes loses
nothing but the node it was in the middle of.

Two pieces of run state live in Redis rather than Postgres because they are
coordination, not history: the per-project **run lock** (PRD §15 NF5d, two
operators launching the same project) and the **cancel flag** (PRD §7.2 item 6,
cooperative cancellation checked between nodes).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import NodeRun, NodeRunStatus, Project, Run, RunStage, RunStatus
from agent.llm.ledger import RunLedger, quantize_money

log = structlog.get_logger(__name__)

#: How long a project stays locked without a heartbeat. Longer than the 45
#: minutes NF1 allows a full run, so a healthy run never loses its own lock; the
#: TTL only matters when a worker dies, and P8's reaper is what cleans up after
#: that properly.
RUN_LOCK_TTL_SECONDS = 2 * 60 * 60

#: Cancellation outlives the run it belongs to by a margin, then expires so a
#: dead flag cannot cancel a later run that reuses nothing but the id.
CANCEL_TTL_SECONDS = 24 * 60 * 60

TERMINAL_STATUSES = frozenset({RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED})


def utcnow() -> datetime:
    return datetime.now(UTC)


def lock_key(project_id: uuid.UUID, stage: RunStage = RunStage.RESEARCH) -> str:
    """Where one project's lock for one pipeline lives.

    Three keys, not one, and deliberately so: research, planning and
    guidelines are different work on the same project and none should keep
    another waiting (Stage 02 PRD §4.2 E4, Stage 03 PRD §8.3). The research key
    keeps its Stage 01 spelling because locks held in a live deployment must
    survive the deploy that introduces this function.

    Matched explicitly rather than by falling through to the research key. The
    fall-through spelling reads as a sensible default and is not one: a new
    `RunStage` would silently share the research lock, and the symptom — two
    unrelated pipelines blocking each other on one project — looks like a
    concurrency bug rather than a missing branch.
    """
    if stage is RunStage.PLAN:
        return f"project:{project_id}:plan_lock"
    if stage is RunStage.GUIDELINE:
        return f"project:{project_id}:guideline_lock"
    if stage is RunStage.CREATIVE:
        return f"project:{project_id}:creative_lock"
    return f"run:lock:project:{project_id}"


def lock_ttl(stage: RunStage) -> int:
    """How long one pipeline's lock lives without a refresh.

    Creative gets `creative_lock_ttl_seconds` (three hours): its runs wait on
    video renders that are polled rather than pushed, and a lock that lapsed
    mid-render would let a second run start on the same project. Every other
    stage keeps the two hours it always had.
    """
    if stage is RunStage.CREATIVE:
        from agent.config import get_settings

        return get_settings().creative_lock_ttl_seconds
    return RUN_LOCK_TTL_SECONDS


def cancel_key(run_id: uuid.UUID) -> str:
    return f"run:{run_id}:cancel"


@dataclass(frozen=True, slots=True)
class LockHolder:
    """Who is already running this project, for the 409 body (PRD §16)."""

    run_id: uuid.UUID
    user_id: uuid.UUID | None
    user_name: str

    def as_dict(self) -> dict[str, str | None]:
        return {
            "run_id": str(self.run_id),
            "user_id": str(self.user_id) if self.user_id else None,
            "user_name": self.user_name,
        }


class RunLock:
    """One run per project per pipeline at a time.

    `stage` picks the key. It is a constructor argument rather than a
    per-method one because a single `RunLock` is never asked about both
    pipelines: every caller has a `Run` in hand, or is launching one, and
    already knows which it is.
    """

    def __init__(self, redis: Redis, stage: RunStage = RunStage.RESEARCH) -> None:
        self._redis = redis
        self._stage = stage

    async def acquire(self, project_id: uuid.UUID, holder: LockHolder) -> LockHolder | None:
        """Take the lock, or return whoever already has it."""
        acquired = await self._redis.set(
            lock_key(project_id, self._stage),
            json.dumps(holder.as_dict()),
            nx=True,
            ex=lock_ttl(self._stage),
        )
        if acquired:
            return None
        current = await self.holder(project_id)
        if current is None:
            # It expired between the SET and the GET. One more try, then give up
            # and let the caller see a conflict rather than loop.
            acquired = await self._redis.set(
                lock_key(project_id, self._stage),
                json.dumps(holder.as_dict()),
                nx=True,
                ex=lock_ttl(self._stage),
            )
            if acquired:
                return None
            current = await self.holder(project_id)
        return current

    async def holder(self, project_id: uuid.UUID) -> LockHolder | None:
        raw = await self._redis.get(lock_key(project_id, self._stage))
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return LockHolder(
                run_id=uuid.UUID(data["run_id"]),
                user_id=uuid.UUID(data["user_id"]) if data.get("user_id") else None,
                user_name=str(data.get("user_name") or ""),
            )
        except (ValueError, KeyError, TypeError):  # pragma: no cover — corrupt value
            return None

    async def refresh(self, project_id: uuid.UUID, run_id: uuid.UUID) -> None:
        """Extend the TTL while a long run is still making progress."""
        current = await self.holder(project_id)
        if current is not None and current.run_id == run_id:
            await self._redis.expire(lock_key(project_id, self._stage), lock_ttl(self._stage))

    async def release(self, project_id: uuid.UUID, run_id: uuid.UUID) -> None:
        """Release only if this run still holds it — never another run's lock."""
        current = await self.holder(project_id)
        if current is None or current.run_id != run_id:
            return
        await self._redis.delete(lock_key(project_id, self._stage))


class CancelFlag:
    """Cooperative cancellation. Set by the API, read by the executor between nodes."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def request(self, run_id: uuid.UUID) -> None:
        await self._redis.set(cancel_key(run_id), "1", ex=CANCEL_TTL_SECONDS)

    async def is_set(self, run_id: uuid.UUID) -> bool:
        return bool(await self._redis.exists(cancel_key(run_id)))

    async def clear(self, run_id: uuid.UUID) -> None:
        await self._redis.delete(cancel_key(run_id))


class RunStore:
    """Every read and write of run state, committed as it goes."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # -- reads -------------------------------------------------------------

    async def load(self, run_id: uuid.UUID) -> tuple[Run, Project] | None:
        run = await self.db.get(Run, run_id)
        if run is None:
            return None
        project = await self.db.get(Project, run.project_id)
        if project is None:  # pragma: no cover — FK is ON DELETE CASCADE
            return None
        return run, project

    async def node_runs(self, run_id: uuid.UUID) -> list[NodeRun]:
        result = await self.db.execute(
            sa.select(NodeRun)
            .where(NodeRun.run_id == run_id)
            .order_by(NodeRun.node_id, NodeRun.attempt)
        )
        return list(result.scalars().all())

    async def latest_by_node(self, run_id: uuid.UUID) -> dict[str, NodeRun]:
        """The newest attempt per node — what the console and the resume path read."""
        latest: dict[str, NodeRun] = {}
        for node_run in await self.node_runs(run_id):
            current = latest.get(node_run.node_id)
            if current is None or node_run.attempt >= current.attempt:
                latest[node_run.node_id] = node_run
        return latest

    async def cached_output(
        self, *, project_id: uuid.UUID, node_id: str, input_hash: str
    ) -> NodeRun | None:
        """A previous succeeded run of this node with the same inputs (PRD §7.1)."""
        result = await self.db.execute(
            sa.select(NodeRun)
            .join(Run, Run.id == NodeRun.run_id)
            .where(
                Run.project_id == project_id,
                NodeRun.node_id == node_id,
                NodeRun.input_hash == input_hash,
                NodeRun.status == NodeRunStatus.SUCCEEDED,
            )
            .order_by(NodeRun.finished_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    # -- writes ------------------------------------------------------------

    async def mark_running(self, run: Run) -> None:
        run.status = RunStatus.RUNNING
        if run.started_at is None:
            run.started_at = utcnow()
        run.error = None
        await self.db.commit()

    async def start_node(self, *, run_id: uuid.UUID, node_id: str, attempt: int) -> NodeRun:
        node_run = NodeRun(
            run_id=run_id,
            node_id=node_id,
            status=NodeRunStatus.RUNNING,
            attempt=attempt,
            started_at=utcnow(),
        )
        self.db.add(node_run)
        await self.db.commit()
        return node_run

    async def finish_node(
        self,
        node_run: NodeRun,
        *,
        status: NodeRunStatus,
        output: dict[str, Any] | None = None,
        evidence_ids: list[uuid.UUID] | None = None,
        model: str | None = None,
        prompt: str | None = None,
        input_hash: str | None = None,
        token_in: int = 0,
        token_out: int = 0,
        cost_usd: Decimal = Decimal(0),
        latency_ms: int | None = None,
        error: dict[str, Any] | None = None,
    ) -> NodeRun:
        """The checkpoint. Nothing moves on until this has committed."""
        node_run.status = status
        node_run.output = output
        node_run.evidence_ids = evidence_ids or []
        node_run.model = model
        node_run.prompt = prompt
        node_run.input_hash = input_hash
        node_run.token_in = token_in
        node_run.token_out = token_out
        node_run.cost_usd = quantize_money(cost_usd)
        node_run.latency_ms = latency_ms
        node_run.error = error
        node_run.finished_at = utcnow()
        await self.db.commit()
        return node_run

    async def record_skipped(self, *, run_id: uuid.UUID, node_ids: list[str], reason: str) -> None:
        """Mark a branch the run will not reach — a failed parent, a cancel, a budget abort.

        A node that was mid-flight when the run stopped already owns a `NodeRun`
        row for this attempt, so it is *moved* to skipped rather than inserted
        again — `uq_node_run_attempt` would reject the duplicate, and an
        IntegrityError while recording an abort would hide the abort.
        """
        if not node_ids:
            return
        latest = await self.latest_by_node(run_id)
        stamped = {"code": "skipped", "reason": reason}
        for node_id in node_ids:
            existing = latest.get(node_id)
            if existing is not None:
                if existing.status in (NodeRunStatus.QUEUED, NodeRunStatus.RUNNING):
                    existing.status = NodeRunStatus.SKIPPED
                    existing.error = stamped
                    existing.finished_at = utcnow()
                # A terminal row already says what happened to this node.
                continue
            self.db.add(
                NodeRun(
                    run_id=run_id,
                    node_id=node_id,
                    status=NodeRunStatus.SKIPPED,
                    attempt=1,
                    started_at=utcnow(),
                    finished_at=utcnow(),
                    error=stamped,
                )
            )
        await self.db.commit()

    async def fail_stale_running(self, run_id: uuid.UUID) -> list[str]:
        """Close out nodes left `running` by a worker that died.

        Resume would otherwise leave them showing as in-flight forever in the
        console, and `latest_by_node` would keep reporting a node as running
        while its replacement attempt was already succeeding.

        Safe because a project holds a run lock and arq dedupes by job id, so
        two workers are never inside the same run at once.
        """
        result = await self.db.execute(
            sa.select(NodeRun).where(
                NodeRun.run_id == run_id, NodeRun.status == NodeRunStatus.RUNNING
            )
        )
        stale = list(result.scalars().all())
        for node_run in stale:
            node_run.status = NodeRunStatus.FAILED
            node_run.finished_at = utcnow()
            node_run.error = {"code": "crashed", "message": "the worker stopped mid-node"}
        if stale:
            await self.db.commit()
        return sorted({node_run.node_id for node_run in stale})

    async def rollup(self, run: Run, ledger: RunLedger) -> None:
        """Roll the ledger up onto the run (PRD §8.3)."""
        run.cost_usd = quantize_money(ledger.spent_usd)
        run.token_in = ledger.token_in
        run.token_out = ledger.token_out
        await self.db.commit()

    async def finish_run(
        self, run: Run, *, status: RunStatus, error: dict[str, Any] | None = None
    ) -> None:
        run.status = status
        run.error = error
        run.finished_at = utcnow()
        await self.db.commit()

    async def reset_failed(self, run_id: uuid.UUID) -> list[str]:
        """Clear failed and skipped nodes so a retry re-enters the executor cleanly.

        Succeeded nodes are untouched — that is what makes `retry-failed` cost
        only the failure (PRD §15 NF3).
        """
        result = await self.db.execute(
            sa.select(NodeRun).where(
                NodeRun.run_id == run_id,
                NodeRun.status.in_([NodeRunStatus.FAILED, NodeRunStatus.SKIPPED]),
            )
        )
        node_ids = sorted({node_run.node_id for node_run in result.scalars().all()})
        if node_ids:
            await self.db.execute(
                sa.delete(NodeRun).where(
                    NodeRun.run_id == run_id,
                    NodeRun.status.in_([NodeRunStatus.FAILED, NodeRunStatus.SKIPPED]),
                )
            )
            await self.db.commit()
        return node_ids
