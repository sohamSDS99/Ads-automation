"""What the run console asks the API for beyond `GET /runs/{id}`: who, and who else.

Both are decoration in the sense that no run depends on them, and neither is
decoration in the sense that matters — a console that cannot say who launched a
40-minute run, or that two people are about to decide the same gate, is a
console people stop trusting.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest_asyncio

from agent.db.models import RunStatus, RunTrigger
from agent.orchestrator.presence import MAX_VIEWERS, VIEWER_TTL_SECONDS, RunPresence
from agent.redis_client import get_redis
from tests.integration.conftest import ApiClient
from tests.integration.runs_support import launch_chain

# ---------------------------------------------------------------------------
# attribution
# ---------------------------------------------------------------------------


async def test_a_run_carries_the_name_of_whoever_launched_it(
    admin: ApiClient, project: Any
) -> None:
    created = await launch_chain(admin, project.id)

    response = await admin.get(f"/runs/{created['id']}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["triggered_by"] is not None
    assert body["triggered_by_name"] == "Admin"


async def test_a_scheduled_run_has_no_one_to_name(admin: ApiClient, project: Any) -> None:
    """PRD §13.4 B: the header says `Schedule` when there is no actor.

    P8 is what creates these; the run row is written directly here so the
    console's null-handling is proven before the writer exists.
    """
    from agent.db.models import Run
    from agent.db.session import get_sessionmaker

    async with get_sessionmaker()() as session:
        run = Run(
            workspace_id=project.workspace_id,
            project_id=project.id,
            status=RunStatus.QUEUED,
            trigger=RunTrigger.SCHEDULE,
            triggered_by=None,
        )
        session.add(run)
        await session.commit()
        run_id = run.id

    body = (await admin.get(f"/runs/{run_id}")).json()
    assert body["trigger"] == "schedule"
    assert body["triggered_by"] is None
    assert body["triggered_by_name"] is None


# ---------------------------------------------------------------------------
# presence
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def run_id(admin: ApiClient, project: Any) -> uuid.UUID:
    return uuid.UUID((await launch_chain(admin, project.id))["id"])


async def test_checking_in_returns_yourself(admin: ApiClient, run_id: uuid.UUID) -> None:
    response = await admin.post(f"/runs/{run_id}/presence")
    assert response.status_code == 200, response.text
    body = response.json()
    assert [viewer["name"] for viewer in body["viewers"]] == ["Admin"]
    assert body["total"] == 1


async def test_two_consoles_see_each_other(
    admin: ApiClient, signed_in_as: Any, run_id: uuid.UUID
) -> None:
    viewer = await signed_in_as("viewer")

    assert (await viewer.post(f"/runs/{run_id}/presence")).status_code == 200
    body = (await admin.post(f"/runs/{run_id}/presence")).json()

    assert [entry["name"] for entry in body["viewers"]] == ["Admin", "Viewer"]
    assert body["total"] == 2


async def test_a_console_that_stops_checking_in_ages_out(run_id: uuid.UUID) -> None:
    """The TTL is per member, which is the whole reason this is a sorted set.

    A plain Redis set expires as one key: either everybody's avatar survives or
    nobody's does. Driven through the store rather than the route because the
    only way to prove an expiry is to move time.
    """
    presence = RunPresence(get_redis(), run_id)
    left, stayed = uuid.uuid4(), uuid.uuid4()

    await presence.check_in(left, now=1000.0)
    await presence.check_in(stayed, now=1000.0)
    assert set(await presence.viewers(now=1000.0)) == {left, stayed}

    later = 1000.0 + VIEWER_TTL_SECONDS + 1
    assert await presence.check_in(stayed, now=later) == [stayed]


async def test_leaving_is_immediate(run_id: uuid.UUID) -> None:
    presence = RunPresence(get_redis(), run_id)
    watcher = uuid.uuid4()

    await presence.check_in(watcher)
    await presence.check_out(watcher)

    assert await presence.viewers() == []


async def test_presence_belongs_to_one_run(run_id: uuid.UUID) -> None:
    other = uuid.uuid4()
    watcher = uuid.uuid4()

    await RunPresence(get_redis(), run_id).check_in(watcher)

    assert await RunPresence(get_redis(), other).viewers() == []


async def test_the_avatar_row_is_capped_but_the_count_is_honest(
    admin: ApiClient, project: Any, run_id: uuid.UUID
) -> None:
    """A run with more viewers than the row can draw still reports all of them."""
    import sqlalchemy as sa

    from agent.db.models import Membership, User, UserRole, UserStatus
    from agent.db.session import get_sessionmaker

    async with get_sessionmaker()() as session:
        for index in range(MAX_VIEWERS + 3):
            watcher = User(
                email=f"crowd-{index}@example.com",
                name=f"Crowd {index:02d}",
                password_hash="x",
                status=UserStatus.ACTIVE,
            )
            session.add(watcher)
            await session.flush()
            session.add(
                Membership(
                    workspace_id=project.workspace_id,
                    user_id=watcher.id,
                    role=UserRole.VIEWER,
                    status=UserStatus.ACTIVE,
                )
            )
        await session.commit()
        watching = (await session.execute(sa.select(User.id))).scalars().all()

    presence = RunPresence(get_redis(), run_id)
    for user_id in watching:
        await presence.check_in(user_id)

    body = (await admin.post(f"/runs/{run_id}/presence")).json()
    assert body["total"] == len(watching)
    assert len(body["viewers"]) == MAX_VIEWERS
    # Capped by name order, not by whatever order Redis kept the uuids in.
    assert body["viewers"][0]["name"] == "Admin"


async def test_a_run_in_another_workspace_is_not_there_to_watch(
    admin: ApiClient, run_id: uuid.UUID
) -> None:
    del run_id
    response = await admin.post(f"/runs/{uuid.uuid4()}/presence")
    assert response.status_code == 404
