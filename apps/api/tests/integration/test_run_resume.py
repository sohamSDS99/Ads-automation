"""PRD §15 NF3: resume after a crash re-executes zero completed nodes."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import NodeRun, NodeRunStatus, Run, RunStatus
from tests.integration.conftest import ApiClient
from tests.integration.runs_support import BRIEF, CRITIQUE, execute, launch
from tests.openrouter_fake import FakeOpenRouter, completion


class WorkerKilled(BaseException):
    """Not an `Exception`: nothing in the executor may catch this, exactly like a SIGKILL."""


def kill(_request: httpx.Request) -> httpx.Response:
    raise WorkerKilled


async def rows_for(db: AsyncSession, run_id: uuid.UUID, node_id: str) -> list[NodeRun]:
    result = await db.execute(
        sa.select(NodeRun)
        .where(NodeRun.run_id == run_id, NodeRun.node_id == node_id)
        .order_by(NodeRun.attempt)
    )
    return list(result.scalars().all())


async def test_a_run_killed_mid_dag_resumes_without_re_executing_the_finished_node(
    admin: ApiClient, project: Any, db: AsyncSession
) -> None:
    created = await launch(admin, project.id)
    run_id = uuid.UUID(created["id"])

    dying = FakeOpenRouter()
    dying.queue(completion(BRIEF), kill)
    with pytest.raises(WorkerKilled):
        await execute(run_id, dying)

    # The checkpoint survived the kill; the run never reached a terminal state.
    first_node = await rows_for(db, run_id, "0.1")
    assert [row.status for row in first_node] == [NodeRunStatus.SUCCEEDED]
    finished_at = first_node[0].finished_at
    run = await db.get(Run, run_id)
    assert run is not None
    await db.refresh(run)
    assert run.status is RunStatus.RUNNING

    resumed = FakeOpenRouter()
    resumed.queue(completion(CRITIQUE))
    result = await execute(run_id, resumed)

    assert result.status is RunStatus.SUCCEEDED
    assert len(resumed.requests) == 1, "the completed node must not be paid for twice"
    assert result.nodes_executed == 1

    after = await rows_for(db, run_id, "0.1")
    assert len(after) == 1, "a resumed run must not open a second attempt of a succeeded node"
    assert after[0].finished_at == finished_at


async def test_the_node_that_was_in_flight_is_closed_out_rather_than_left_running(
    admin: ApiClient, project: Any, db: AsyncSession
) -> None:
    """A node stuck at `running` would show as in-flight in the console forever."""
    created = await launch(admin, project.id)
    run_id = uuid.UUID(created["id"])

    dying = FakeOpenRouter()
    dying.queue(completion(BRIEF), kill)
    with pytest.raises(WorkerKilled):
        await execute(run_id, dying)

    in_flight = await rows_for(db, run_id, "0.2")
    assert [row.status for row in in_flight] == [NodeRunStatus.RUNNING]

    resumed = FakeOpenRouter()
    resumed.queue(completion(CRITIQUE))
    await execute(run_id, resumed)

    for row in await rows_for(db, run_id, "0.2"):
        await db.refresh(row)
    rows = await rows_for(db, run_id, "0.2")
    assert [row.status for row in rows] == [NodeRunStatus.FAILED, NodeRunStatus.SUCCEEDED]
    assert rows[0].error["code"] == "crashed"

    state = (await admin.get(f"/runs/{run_id}")).json()
    assert [node["status"] for node in state["nodes"]] == ["succeeded", "succeeded"]
    assert state["nodes"][1]["attempt"] == 2
