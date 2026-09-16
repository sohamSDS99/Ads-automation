"""PRD §7.2 item 7 and §16: the budget cap aborts a run mid-DAG."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import NodeRunStatus, Project, RunStatus
from tests.integration.conftest import ApiClient
from tests.integration.runs_support import execute, launch_chain, script_two_node_run
from tests.openrouter_fake import FakeOpenRouter


async def set_cap(db: AsyncSession, project: Project, cap: str) -> None:
    await db.execute(
        sa.update(Project)
        .where(Project.id == project.id)
        .values(settings={"max_run_cost_usd": cap})
    )
    await db.commit()


async def test_a_run_that_spends_past_its_cap_stops_and_keeps_what_it_bought(
    admin: ApiClient, project: Any, db: AsyncSession
) -> None:
    # The first node costs $0.0002 at the fixture's prices.
    await set_cap(db, project, "0.0001")
    created = await launch_chain(admin, project.id)

    fake = FakeOpenRouter()
    script_two_node_run(fake)
    result = await execute(created["id"], fake)

    assert result.status is RunStatus.FAILED
    assert len(fake.requests) == 1, "the run must stop before paying for the next node"

    state = (await admin.get(f"/runs/{created['id']}")).json()
    assert state["error"]["code"] == "budget_exceeded"
    assert Decimal(state["error"]["cap_usd"]) == Decimal("0.0001")
    assert Decimal(state["cost_usd"]) == Decimal("0.0002")
    assert [node["status"] for node in state["nodes"]] == [
        NodeRunStatus.SUCCEEDED,  # persisted: everything done is kept
        NodeRunStatus.SKIPPED,
    ]


async def test_the_environment_default_applies_when_the_project_is_silent(
    admin: ApiClient, project: Any
) -> None:
    """MAX_RUN_COST_USD is $15 in the fixture environment, so a cheap run finishes."""
    created = await launch_chain(admin, project.id)
    fake = FakeOpenRouter()
    script_two_node_run(fake)

    assert (await execute(created["id"], fake)).status is RunStatus.SUCCEEDED


async def test_a_malformed_cap_falls_back_instead_of_failing_the_run(
    admin: ApiClient, project: Any, db: AsyncSession
) -> None:
    await set_cap(db, project, "not a number")
    created = await launch_chain(admin, project.id)
    fake = FakeOpenRouter()
    script_two_node_run(fake)

    assert (await execute(created["id"], fake)).status is RunStatus.SUCCEEDED


async def test_the_lock_is_released_when_a_run_aborts(
    admin: ApiClient, project: Any, db: AsyncSession
) -> None:
    """An abort that kept the lock would leave the project unrunnable until the TTL."""
    await set_cap(db, project, "0.0001")
    created = await launch_chain(admin, project.id)
    fake = FakeOpenRouter()
    script_two_node_run(fake)
    await execute(created["id"], fake)

    second = await admin.post(f"/projects/{project.id}/runs", json={})
    assert second.status_code == 201
    assert uuid.UUID(second.json()["id"]) != uuid.UUID(created["id"])
