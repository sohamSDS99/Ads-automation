"""The positive half of the run matrix: who may actually launch and control a run."""

from __future__ import annotations

from typing import Any

import pytest

from tests.integration.conftest import ApiClient
from tests.integration.runs_support import execute, launch, script_two_node_run
from tests.openrouter_fake import FakeOpenRouter


@pytest.mark.parametrize("role", ["operator", "approver", "viewer"])
async def test_only_run_execute_holders_may_launch(
    admin: ApiClient, project: Any, signed_in_as: Any, role: str
) -> None:
    caller = await signed_in_as(role)
    response = await caller.post(f"/projects/{project.id}/runs", json={})

    if role == "operator":
        assert response.status_code == 201
    else:
        assert response.status_code == 403
        assert response.json()["missing_permission"] == "run_execute"


@pytest.mark.parametrize("role", ["operator", "approver", "viewer"])
async def test_every_role_can_read_a_run_its_workspace_owns(
    admin: ApiClient, project: Any, signed_in_as: Any, role: str
) -> None:
    created = await launch(admin, project.id)
    fake = FakeOpenRouter()
    script_two_node_run(fake)
    await execute(created["id"], fake)

    caller = await signed_in_as(role)
    assert (await caller.get(f"/runs/{created['id']}")).status_code == 200
    assert (await caller.get(f"/runs/{created['id']}/nodes/0.1")).status_code == 200
    assert (await caller.get(f"/runs/{created['id']}/events")).status_code == 200


async def test_an_approver_cannot_cancel_a_run(
    admin: ApiClient, project: Any, signed_in_as: Any
) -> None:
    """Deciding a gate is not the same permission as steering the run."""
    created = await launch(admin, project.id)
    approver = await signed_in_as("approver")

    response = await approver.post(f"/runs/{created['id']}/cancel")
    assert response.status_code == 403

    assert (await admin.get(f"/runs/{created['id']}")).json()["status"] == "queued"
