"""Person-tasks — the acts the agent cannot perform for anybody (PRD §8.4).

The twin of `orchestrator/approvals.py`, and deliberately not a flavour of it.
An approval says *the agent proposed and a human confirmed*, and any holder of
`required_role` may confirm it — which means an administrator can, because an
administrator holds every role. A person-task says *the agent cannot do this at
all*: one named assignee, no role fallback, no admin override, and for H1 a
step-up-authenticated signature on top.

Collapsing the two would leave the run table unable to tell them apart and the
inbox offering the wrong control to the wrong person. So they are two tables,
two run statuses, and two parking functions that look similar on purpose and
must not be merged.
"""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import (
    HumanTask,
    HumanTaskBlocking,
    HumanTaskStatus,
    Run,
    RunStatus,
)

log = structlog.get_logger(__name__)

#: A task in one of these is still waiting on its person.
OUTSTANDING = (HumanTaskStatus.PENDING, HumanTaskStatus.IN_PROGRESS, HumanTaskStatus.BLOCKED)


async def open_task(
    db: AsyncSession,
    *,
    run: Run,
    project_id: uuid.UUID,
    node_id: str,
    task_key: str,
    assignee_id: uuid.UUID,
    title: str,
    instructions: str,
    required_artifacts: dict[str, Any],
    blocking_for: HumanTaskBlocking,
) -> HumanTask:
    """Create the task, or return the one already open for this (run, key).

    Idempotent on `(guideline_run_id, task_key)` because a resumed run
    re-executes the node that opens it, and a second task would put the same
    question in front of the same person twice with two places to answer it.
    """
    existing = (
        (
            await db.execute(
                sa.select(HumanTask).where(
                    HumanTask.guideline_run_id == run.id,
                    HumanTask.task_key == task_key,
                    HumanTask.status.in_(OUTSTANDING),
                )
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return existing

    task = HumanTask(
        workspace_id=run.workspace_id,
        project_id=project_id,
        guideline_run_id=run.id,
        node_id=node_id,
        task_key=task_key,
        title=title,
        instructions=instructions,
        # NOT NULL in DDL as well as here. A task with no assignee is a task
        # that falls back to a role, and that fallback is the whole of law 23.
        assignee_id=assignee_id,
        required_artifacts=required_artifacts,
        status=HumanTaskStatus.PENDING,
        blocking_for=blocking_for,
    )
    db.add(task)
    await db.flush()
    return task


async def pending_count(db: AsyncSession, run_id: uuid.UUID) -> int:
    result = await db.execute(
        sa.select(sa.func.count())
        .select_from(HumanTask)
        .where(HumanTask.guideline_run_id == run_id, HumanTask.status.in_(OUTSTANDING))
    )
    return int(result.scalar() or 0)


async def park(db: AsyncSession, run: Run) -> bool:
    """Try to end this pass as `awaiting_human_task`. False means "keep executing".

    The row lock mirrors `approvals.park` and for the same reason: a submission
    committed while the executor was finishing its last wave must be visible
    here, or the run parks on a task that is already done and nothing wakes it.
    """
    await db.execute(sa.select(Run.id).where(Run.id == run.id).with_for_update())
    outstanding = await pending_count(db, run.id)
    if outstanding == 0:
        await db.commit()
        log.info("human_task.park_declined", run_id=str(run.id), reason="all tasks submitted")
        return False
    run.status = RunStatus.AWAITING_HUMAN_TASK
    run.finished_at = None
    run.error = None
    await db.commit()
    log.info("run.awaiting_human_task", run_id=str(run.id), pending=outstanding)
    return True


async def complete(
    db: AsyncSession,
    task: HumanTask,
    *,
    by: uuid.UUID,
    payload: dict[str, Any],
    attachment_paths: list[str] | None = None,
) -> HumanTask:
    """Mark a task done by its assignee, and only by its assignee.

    The identity check is asserted here as well as in the route. A helper that
    trusted its caller would make the route the only thing standing between an
    administrator and a signature, and one check is not two layers.
    """
    if task.assignee_id != by:
        raise PermissionError(
            f"human task {task.id} is assigned to {task.assignee_id} and was submitted by "
            f"{by}. A person-task has no role fallback and no admin override."
        )
    task.status = HumanTaskStatus.COMPLETED
    task.completed_by = by
    task.completed_at = sa.func.now()
    task.submitted_payload = payload
    if attachment_paths:
        task.attachment_paths = attachment_paths
    return task
