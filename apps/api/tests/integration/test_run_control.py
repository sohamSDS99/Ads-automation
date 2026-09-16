"""Cancel, the project run lock, and retry-failed."""

from __future__ import annotations

import uuid
from typing import Any

from agent.db.models import NodeRunStatus, RunStatus
from agent.orchestrator.state import CancelFlag
from agent.redis_client import get_redis
from tests.integration.conftest import ApiClient
from tests.integration.runs_support import execute, launch, script_two_node_run
from tests.openrouter_fake import FakeOpenRouter, completion

# -- cancel ------------------------------------------------------------------


async def test_cancelling_a_queued_run_ends_it_without_waiting_for_a_worker(
    admin: ApiClient, project: Any
) -> None:
    created = await launch(admin, project.id)

    response = await admin.post(f"/runs/{created['id']}/cancel")
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == RunStatus.CANCELLED
    assert [node["status"] for node in body["nodes"]] == [
        NodeRunStatus.SKIPPED,
        NodeRunStatus.SKIPPED,
    ]

    # And the worker, arriving late, does nothing.
    fake = FakeOpenRouter()
    assert (await execute(created["id"], fake)).nodes_executed == 0
    assert fake.requests == []


async def test_a_cancel_requested_before_the_first_node_stops_the_run_cold(
    admin: ApiClient, project: Any
) -> None:
    created = await launch(admin, project.id)
    run_id = uuid.UUID(created["id"])
    # Set the flag directly: cancelling through the API would also finalise the
    # queued run, and what is under test here is the executor's own check.
    await CancelFlag(get_redis()).request(run_id)

    fake = FakeOpenRouter()
    script_two_node_run(fake)
    result = await execute(run_id, fake)

    assert result.status is RunStatus.CANCELLED
    assert fake.requests == []
    state = (await admin.get(f"/runs/{run_id}")).json()
    assert state["error"]["code"] == "cancelled"
    assert all(node["status"] == NodeRunStatus.SKIPPED for node in state["nodes"])


async def test_cancelling_a_finished_run_is_a_conflict(admin: ApiClient, project: Any) -> None:
    created = await launch(admin, project.id)
    fake = FakeOpenRouter()
    script_two_node_run(fake)
    await execute(created["id"], fake)

    response = await admin.post(f"/runs/{created['id']}/cancel")
    assert response.status_code == 409
    assert "succeeded" in response.json()["detail"]


async def test_cancelling_frees_the_project_for_the_next_run(
    admin: ApiClient, project: Any
) -> None:
    created = await launch(admin, project.id)
    await admin.post(f"/runs/{created['id']}/cancel")

    assert (await admin.post(f"/projects/{project.id}/runs", json={})).status_code == 201


# -- the run lock ------------------------------------------------------------


async def test_two_operators_launching_the_same_project_produce_one_run(
    admin: ApiClient, project: Any, signed_in_as: Any
) -> None:
    """PRD §15 NF5d: the loser gets a 409 naming the holder and the run id."""
    first = await launch(admin, project.id)

    operator = await signed_in_as("operator")
    second = await operator.post(f"/projects/{project.id}/runs", json={})

    assert second.status_code == 409
    body = second.json()
    assert body["holder"]["run_id"] == first["id"]
    assert body["holder"]["user_name"] == "Admin"
    assert "Admin" in body["detail"]


async def test_the_losing_launch_leaves_no_run_row_behind(
    admin: ApiClient, project: Any, signed_in_as: Any
) -> None:
    await launch(admin, project.id)
    operator = await signed_in_as("operator")
    await operator.post(f"/projects/{project.id}/runs", json={})

    runs = (await admin.get(f"/runs/{(await _one_run_id(admin, project))}")).json()
    assert runs["status"] == RunStatus.QUEUED


async def _one_run_id(admin: ApiClient, project: Any) -> str:
    from agent.db.repos import RunRepo
    from agent.db.session import get_sessionmaker

    async with get_sessionmaker()() as session:
        runs = await RunRepo(session, project.workspace_id).for_project(project.id)
    assert len(runs) == 1, f"expected exactly one run row, found {len(runs)}"
    return str(runs[0].id)


async def test_a_finished_run_releases_the_project(admin: ApiClient, project: Any) -> None:
    created = await launch(admin, project.id)
    fake = FakeOpenRouter()
    script_two_node_run(fake)
    await execute(created["id"], fake)

    assert (await admin.post(f"/projects/{project.id}/runs", json={})).status_code == 201


# -- retry-failed ------------------------------------------------------------


async def test_retry_failed_re_runs_only_the_failed_node(
    admin: ApiClient, project: Any, monkeypatch: Any
) -> None:
    monkeypatch.setattr("agent.llm.gateway.BACKOFF_BASE_SECONDS", 0.0)
    created = await launch(admin, project.id)

    broken = FakeOpenRouter()
    broken.always(completion({"not": "a brief"}))
    assert (await execute(created["id"], broken)).status is RunStatus.FAILED

    state = (await admin.get(f"/runs/{created['id']}")).json()
    assert state["nodes"][0]["status"] == NodeRunStatus.FAILED
    assert state["nodes"][0]["attempt"] == 3, "three attempts before giving up"
    assert state["nodes"][1]["status"] == NodeRunStatus.SKIPPED

    retried = await admin.post(f"/runs/{created['id']}/retry-failed")
    assert retried.status_code == 202
    assert retried.json()["status"] == RunStatus.QUEUED

    fixed = FakeOpenRouter()
    script_two_node_run(fixed)
    result = await execute(created["id"], fixed)

    assert result.status is RunStatus.SUCCEEDED
    final = (await admin.get(f"/runs/{created['id']}")).json()
    assert [node["status"] for node in final["nodes"]] == [
        NodeRunStatus.SUCCEEDED,
        NodeRunStatus.SUCCEEDED,
    ]


async def test_retry_failed_refuses_a_run_that_is_still_going(
    admin: ApiClient, project: Any
) -> None:
    created = await launch(admin, project.id)
    response = await admin.post(f"/runs/{created['id']}/retry-failed")
    assert response.status_code == 409


async def test_retry_failed_refuses_a_run_with_nothing_to_retry(
    admin: ApiClient, project: Any
) -> None:
    created = await launch(admin, project.id)
    fake = FakeOpenRouter()
    script_two_node_run(fake)
    await execute(created["id"], fake)

    response = await admin.post(f"/runs/{created['id']}/retry-failed")
    assert response.status_code == 409
    assert "no failed nodes" in response.json()["detail"]


async def test_an_unknown_run_is_a_404_not_a_500(admin: ApiClient) -> None:
    assert (await admin.get(f"/runs/{uuid.uuid4()}")).status_code == 404


async def test_an_unknown_node_of_a_real_run_is_a_404(admin: ApiClient, project: Any) -> None:
    created = await launch(admin, project.id)
    assert (await admin.get(f"/runs/{created['id']}/nodes/0.1")).status_code == 404
    assert (await admin.get(f"/runs/{created['id']}/nodes/9.9")).status_code == 404


async def test_a_partial_run_with_no_node_ids_is_rejected(admin: ApiClient, project: Any) -> None:
    """Silently promoting it to a full run would spend the whole budget by accident."""
    response = await admin.post(f"/projects/{project.id}/runs", json={"mode": "partial"})
    assert response.status_code == 422
    assert "node_ids" in response.json()["detail"]


async def test_a_conflicting_retry_does_not_erase_the_failure_record(
    admin: ApiClient, project: Any, signed_in_as: Any, monkeypatch: Any
) -> None:
    """The lock is taken before `reset_failed` deletes anything."""
    monkeypatch.setattr("agent.llm.gateway.BACKOFF_BASE_SECONDS", 0.0)
    created = await launch(admin, project.id)
    broken = FakeOpenRouter()
    broken.always(completion({"not": "a brief"}))
    await execute(created["id"], broken)

    # Somebody else starts a different run on the same project first.
    other = await admin.post(f"/projects/{project.id}/runs", json={})
    assert other.status_code == 201

    operator = await signed_in_as("operator")
    refused = await operator.post(f"/runs/{created['id']}/retry-failed")
    assert refused.status_code == 409

    state = (await admin.get(f"/runs/{created['id']}")).json()
    assert state["nodes"][0]["status"] == NodeRunStatus.FAILED
    assert state["nodes"][0]["attempt"] == 3, "the failure record must survive a refused retry"
