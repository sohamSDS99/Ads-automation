"""H1 halts the run for one named person (PRD §8.4, law 23).

The S3-P3 exit criterion in its strongest form: not "a task row appears" but
"the run stops, the task is assigned to the identity G6 named, and nobody else
can act on it". A person-task that any approver could pick up is an approval
wearing a different table.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import HumanTask, HumanTaskBlocking, HumanTaskStatus, Run, RunStatus
from tests.integration.conftest import ApiClient
from tests.integration.guideline_gates import (
    advance,
    as_client,
    owner_ids,
    patch_gateway,
    run_to_the_gates,
)
from tests.openrouter_fake import FakeOpenRouter


async def test_the_run_halts_on_h1_for_the_named_legal_owner(
    admin: ApiClient,
    db: AsyncSession,
    project_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeOpenRouter()
    patch_gateway(monkeypatch, fake)
    run_id, cast = await run_to_the_gates(admin, db, project_id, fake)
    # G6 names the owners; G5 cannot even exist until it has (§11's DAG edges),
    # and 3.2.3 hangs off 3.5.1 — so the gates are decided in sequence before
    # H1 can open at all.
    approver = await as_client(cast["approver"])
    while await advance(admin, approver, run_id, db, fake):
        pass

    await db.rollback()
    task = (
        (await db.execute(sa.select(HumanTask).where(HumanTask.guideline_run_id == run_id)))
        .scalars()
        .first()
    )
    assert task is not None, "3.2.3 did not open H1"
    assert task.task_key == "H1"
    assert task.status is HumanTaskStatus.PENDING
    # H1 blocks publish, not launch: a register with no terminal decisions
    # licenses nothing, so the ruleset would ship inert.
    assert task.blocking_for is HumanTaskBlocking.PUBLISH

    owners = await owner_ids(admin, db)
    assert str(task.assignee_id) == owners["legal"]

    run = (await db.execute(sa.select(Run).where(Run.id == run_id))).scalar_one()
    assert run.status is RunStatus.AWAITING_HUMAN_TASK


async def test_the_task_names_exactly_one_person(
    admin: ApiClient,
    db: AsyncSession,
    project_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`assignee_id` is NOT NULL in DDL, and this is why.

    A task with no assignee is a task that falls back to a role, and every role
    an administrator does not hold is one they hold anyway.
    """
    fake = FakeOpenRouter()
    patch_gateway(monkeypatch, fake)
    run_id, cast = await run_to_the_gates(admin, db, project_id, fake)
    # G6 names the owners; G5 cannot even exist until it has (§11's DAG edges),
    # and 3.2.3 hangs off 3.5.1 — so the gates are decided in sequence before
    # H1 can open at all.
    approver = await as_client(cast["approver"])
    while await advance(admin, approver, run_id, db, fake):
        pass

    await db.rollback()
    tasks = (
        (await db.execute(sa.select(HumanTask).where(HumanTask.guideline_run_id == run_id)))
        .scalars()
        .all()
    )
    assert len(tasks) == 1
    assert tasks[0].assignee_id is not None
