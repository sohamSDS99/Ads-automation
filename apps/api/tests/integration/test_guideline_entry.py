"""Stage 03's entry path (PRD §4.4, §16, §23).

The whole phase turns on one sentence: **C-E6 is a warning.** A project with no
research and no frozen plan is eligible, the Start button is enabled, and the
dialog names what binding would have added. Any implementation that turns that
into a blocker has rebuilt Stage 02's handshake by accident, so the assertions
below are written to fail loudly if it ever does.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import (
    AmendmentOrigin,
    AmendmentStatus,
    ContentGuideline,
    GuidelineMode,
    GuidelineStatus,
    PolicyAmendment,
    Run,
    RunStage,
    RunStatus,
    RunTrigger,
)
from tests.integration.conftest import ApiClient, make_member

pytestmark = pytest.mark.asyncio


def _codes(items: list[dict[str, Any]]) -> set[str]:
    return {item["code"] for item in items}


async def _approver(admin: ApiClient) -> tuple[str, str]:
    """C-E5 needs somebody who could hold a non-delegable signature."""
    return await make_member(admin, "approver", email="legal@example.com")


# ---------------------------------------------------------------------------
# the headline
# ---------------------------------------------------------------------------


async def test_cold_start_no_upstream_dependency(
    admin: ApiClient, project_id: uuid.UUID, db: AsyncSession
) -> None:
    """A guideline run starts on a project with no research and no plan.

    This is the test the whole phase exists to make pass. It asserts the
    absence of a handshake, so if a later phase adds an upstream precondition
    it fails here first.
    """
    await _approver(admin)

    eligibility = (await admin.get(f"/projects/{project_id}/guidelines/eligibility")).json()
    assert eligibility["eligible"] is True
    assert eligibility["blockers"] == []
    assert "running_unlinked" in _codes(eligibility["warnings"])

    response = await admin.post(f"/projects/{project_id}/guidelines/runs", json={})
    assert response.status_code == 202, response.text
    body = response.json()

    run = await db.get(Run, uuid.UUID(body["run_id"]))
    assert run is not None
    assert run.stage is RunStage.GUIDELINE
    assert run.source_run_id is None, "a cold guideline run consumes no research"
    assert run.bindings == {}, "'{}' is bound-nothing; NULL would violate the CHECK"
    assert body["mode"] == GuidelineMode.STANDALONE.value
    assert len(body["input_hash"]) == 64


async def test_a_bare_project_has_no_research_or_plan_rows(
    admin: ApiClient, project_id: uuid.UUID, db: AsyncSession
) -> None:
    """Guards the test above from passing for the wrong reason."""
    await _approver(admin)
    await admin.post(f"/projects/{project_id}/guidelines/runs", json={})
    research = (
        await db.execute(
            sa.select(sa.func.count())
            .select_from(Run)
            .where(Run.project_id == project_id, Run.stage != RunStage.GUIDELINE)
        )
    ).scalar_one()
    assert research == 0


# ---------------------------------------------------------------------------
# C-E1 .. C-E8
# ---------------------------------------------------------------------------


async def test_ce5_no_approver_is_a_blocker(admin: ApiClient, project_id: uuid.UUID) -> None:
    """C-E5. You cannot route a non-delegable signature with nobody to route it to.

    The one precondition of this stage that is a person rather than an
    upstream artifact — which is why it does not offend law 21.
    """
    eligibility = (await admin.get(f"/projects/{project_id}/guidelines/eligibility")).json()
    assert eligibility["eligible"] is False
    assert "no_eligible_owners" in _codes(eligibility["blockers"])


async def test_ce6_is_a_warning_and_never_a_blocker(
    admin: ApiClient, project_id: uuid.UUID
) -> None:
    """The load-bearing line of the PRD, asserted on its own."""
    await _approver(admin)
    eligibility = (await admin.get(f"/projects/{project_id}/guidelines/eligibility")).json()
    assert "running_unlinked" not in _codes(eligibility["blockers"])
    assert "running_unlinked" in _codes(eligibility["warnings"])
    assert eligibility["eligible"] is True


async def test_ce6_disappears_once_research_is_accepted(
    admin: ApiClient, project_id: uuid.UUID, accepted: dict[str, Any]
) -> None:
    await _approver(admin)
    eligibility = (await admin.get(f"/projects/{project_id}/guidelines/eligibility")).json()
    assert "running_unlinked" not in _codes(eligibility["warnings"])
    assert eligibility["available_bindings"]["research"] is not None


async def test_ce2_a_second_start_is_blocked_while_one_is_in_flight(
    admin: ApiClient, project_id: uuid.UUID
) -> None:
    await _approver(admin)
    first = await admin.post(f"/projects/{project_id}/guidelines/runs", json={})
    assert first.status_code == 202

    eligibility = (await admin.get(f"/projects/{project_id}/guidelines/eligibility")).json()
    assert eligibility["eligible"] is False
    assert "guideline_in_flight" in _codes(eligibility["blockers"])

    second = await admin.post(f"/projects/{project_id}/guidelines/runs", json={})
    assert second.status_code == 409
    assert second.json()["code"] == "guideline_in_flight"


async def test_ce7_a_published_version_warns_that_a_run_mints_a_major(
    admin: ApiClient,
    project_id: uuid.UUID,
    workspace_id: uuid.UUID,
    db: AsyncSession,
    admin_user: Any,
) -> None:
    await _approver(admin)
    run = Run(
        workspace_id=workspace_id,
        project_id=project_id,
        triggered_by=admin_user.id,
        trigger=RunTrigger.MANUAL,
        status=RunStatus.SUCCEEDED,
        stage=RunStage.GUIDELINE,
        bindings={},
    )
    db.add(run)
    await db.flush()
    db.add(
        ContentGuideline(
            workspace_id=workspace_id,
            project_id=project_id,
            guideline_run_id=run.id,
            schema_version="1.0",
            version_major=1,
            version_minor=3,
            status=GuidelineStatus.PUBLISHED,
            mode=GuidelineMode.STANDALONE,
        )
    )
    await db.commit()

    eligibility = (await admin.get(f"/projects/{project_id}/guidelines/eligibility")).json()
    assert eligibility["eligible"] is True, "a published version never blocks a re-run"
    assert "will_mint_major" in _codes(eligibility["warnings"])


async def test_ce8_unreviewed_amendments_warn(
    admin: ApiClient, project_id: uuid.UUID, workspace_id: uuid.UUID, db: AsyncSession
) -> None:
    await _approver(admin)
    db.add(
        PolicyAmendment(
            workspace_id=workspace_id,
            project_id=project_id,
            origin=AmendmentOrigin.POLICY_WATCH,
            status=AmendmentStatus.NEEDS_REVIEW,
        )
    )
    await db.commit()
    eligibility = (await admin.get(f"/projects/{project_id}/guidelines/eligibility")).json()
    assert eligibility["eligible"] is True
    assert "unreviewed_amendments" in _codes(eligibility["warnings"])


async def test_every_blocker_and_warning_names_this_project(
    admin: ApiClient, project_id: uuid.UUID
) -> None:
    """PRD §15.1: a whole sentence, never a generic "unavailable"."""
    eligibility = (await admin.get(f"/projects/{project_id}/guidelines/eligibility")).json()
    for item in eligibility["blockers"] + eligibility["warnings"]:
        assert len(item["detail"]) > 25, item
        assert item["fix_url"].startswith("/"), item


# ---------------------------------------------------------------------------
# bindings
# ---------------------------------------------------------------------------


async def test_a_research_binding_is_recorded_on_the_run(
    admin: ApiClient, project_id: uuid.UUID, accepted: dict[str, Any], db: AsyncSession
) -> None:
    await _approver(admin)
    eligibility = (await admin.get(f"/projects/{project_id}/guidelines/eligibility")).json()
    research_run_id = eligibility["available_bindings"]["research"]["research_run_id"]

    response = await admin.post(
        f"/projects/{project_id}/guidelines/runs",
        json={"bindings": {"research_run_id": research_run_id}},
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["mode"] == GuidelineMode.RESEARCH_LINKED.value

    run = await db.get(Run, uuid.UUID(body["run_id"]))
    assert run is not None
    assert str(run.source_run_id) == research_run_id, "the research binding also sets source_run_id"
    assert run.bindings["research_run_id"] == research_run_id


async def test_an_unresolvable_binding_still_starts_the_run(
    admin: ApiClient, project_id: uuid.UUID
) -> None:
    """The deliberate deviation from S2-P0, which returns 422 on version skew."""
    await _approver(admin)
    response = await admin.post(
        f"/projects/{project_id}/guidelines/runs",
        json={"bindings": {"research_run_id": str(uuid.uuid4())}},
    )
    assert response.status_code == 202, response.text
    assert response.json()["mode"] == GuidelineMode.STANDALONE.value


# ---------------------------------------------------------------------------
# concurrency and locks
# ---------------------------------------------------------------------------


async def test_two_concurrent_starts_create_exactly_one_run(
    admin: ApiClient, second_client: ApiClient, project_id: uuid.UUID, db: AsyncSession
) -> None:
    await _approver(admin)
    await second_client.login("admin@example.com", "quarry-lantern-98-fog")
    first, second = await asyncio.gather(
        admin.post(f"/projects/{project_id}/guidelines/runs", json={}),
        second_client.post(f"/projects/{project_id}/guidelines/runs", json={}),
    )
    statuses = sorted([first.status_code, second.status_code])
    assert statuses == [202, 409], (first.text, second.text)

    count = (
        await db.execute(
            sa.select(sa.func.count())
            .select_from(Run)
            .where(Run.project_id == project_id, Run.stage == RunStage.GUIDELINE)
        )
    ).scalar_one()
    assert count == 1


async def test_the_guideline_lock_is_its_own_key() -> None:
    """PRD §8.3: a project may run research, hold a draft plan and run
    guidelines at once. Three keys, not one."""
    from agent.orchestrator.state import lock_key

    project = uuid.uuid4()
    keys = {
        lock_key(project, RunStage.RESEARCH),
        lock_key(project, RunStage.PLAN),
        lock_key(project, RunStage.GUIDELINE),
    }
    assert len(keys) == 3
    assert lock_key(project, RunStage.GUIDELINE) == f"project:{project}:guideline_lock"


# ---------------------------------------------------------------------------
# authorization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["approver", "viewer"])
async def test_only_an_operator_or_admin_may_start_a_run(
    admin: ApiClient, second_client: ApiClient, project_id: uuid.UUID, role: str
) -> None:
    email, password = await make_member(admin, role, email=f"{role}-start@example.com")
    await second_client.login(email, password)
    response = await second_client.post(f"/projects/{project_id}/guidelines/runs", json={})
    assert response.status_code == 403


@pytest.mark.parametrize("role", ["admin", "operator", "approver", "viewer"])
async def test_every_role_may_read_eligibility(
    admin: ApiClient, second_client: ApiClient, project_id: uuid.UUID, role: str
) -> None:
    if role == "admin":
        assert (
            await admin.get(f"/projects/{project_id}/guidelines/eligibility")
        ).status_code == 200
        return
    email, password = await make_member(admin, role, email=f"{role}-read@example.com")
    await second_client.login(email, password)
    response = await second_client.get(f"/projects/{project_id}/guidelines/eligibility")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


async def test_version_history_is_empty_on_a_new_project(
    admin: ApiClient, project_id: uuid.UUID
) -> None:
    response = await admin.get(f"/projects/{project_id}/guidelines")
    assert response.status_code == 200
    assert response.json()["versions"] == []


async def test_a_project_from_another_workspace_is_not_found(admin: ApiClient) -> None:
    response = await admin.get(f"/projects/{uuid.uuid4()}/guidelines/eligibility")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# the queue hop
# ---------------------------------------------------------------------------


async def test_a_cold_guideline_run_is_executed_to_completion_by_a_real_worker(
    admin: ApiClient, project_id: uuid.UUID, db: AsyncSession
) -> None:
    """The acceptance §23 actually asks for: not accepted, *executed*.

    Every other test in this file stops at 202, and the failure they all miss
    is the same one: a guideline run that is enqueued, picked up, and then
    executed against the research DAG, or against no DAG at all because
    `RunStage.GUIDELINE` has no registered node.

    Unlike the plan equivalent this run **succeeds**, and that is the point of
    the two placeholder nodes reaching no model: a cold-start smoke test that
    needed a live OpenRouter key would not be a cold-start test.
    """
    from arq.connections import RedisSettings
    from arq.worker import Worker

    from agent.db.models import NodeRunStatus
    from agent.orchestrator.dag import get_dag
    from agent.worker import WorkerSettings
    from tests.integration.conftest import REAL_REDIS_URL

    await _approver(admin)
    started = await admin.post(f"/projects/{project_id}/guidelines/runs", json={})
    assert started.status_code == 202, started.text
    run_id = started.json()["run_id"]

    worker = Worker(
        functions=WorkerSettings.functions,
        redis_settings=RedisSettings.from_dsn(REAL_REDIS_URL),
        burst=True,
        poll_delay=0.01,
        max_jobs=1,
        handle_signals=False,
    )
    try:
        await worker.main()
    finally:
        await worker.close()

    assert worker.jobs_complete == 1, "the guideline job was never picked up"
    assert worker.jobs_failed == 0

    state = (await admin.get(f"/runs/{run_id}")).json()
    # The guideline DAG, not either of the other two. Derived from the registry
    # rather than listed, so S3-P2 replacing these nodes does not turn this red:
    # the claim is "the guideline graph", not "these two".
    shown = [node["id"] for node in state["nodes"]]
    assert sorted(shown) == sorted(get_dag(RunStage.GUIDELINE).node_ids)
    assert not set(shown) & set(get_dag(RunStage.RESEARCH).node_ids)
    assert not set(shown) & set(get_dag(RunStage.PLAN).node_ids)
    assert state["status"] == RunStatus.SUCCEEDED.value, state

    terminal = (await admin.get(f"/runs/{run_id}/nodes/3.0.2")).json()
    assert terminal["status"] == NodeRunStatus.SUCCEEDED
    assert terminal["model"] is None, "a cold smoke run must reach no model and spend nothing"
    assert terminal["output"]["mode"] == "standalone"
    assert sorted(terminal["output"]["unbound_inputs"]) == ["plan", "research"]

    # And the lock came back, so the project can be run again.
    again = (await admin.get(f"/projects/{project_id}/guidelines/eligibility")).json()
    assert again["eligible"] is True
    assert "guideline_in_flight" not in _codes(again["blockers"])
