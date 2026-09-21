"""The run API end to end, over the real 1.1.2 → 1.1.4 chain (PRD §17 P1 acceptance).

P1 proved this with two throwaway nodes. P3 deleted them, so the same assertions
now ride on nodes that ship — which means the pandas rollups and the label merge
are on the path too, not only the executor.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import AuditLog, NodeRun, NodeRunStatus, RunMode, RunStatus
from tests.integration.conftest import ApiClient
from tests.integration.runs_support import (
    execute,
    launch,
    launch_chain,
    script_two_node_run,
)
from tests.openrouter_fake import FakeOpenRouter


async def test_a_launched_run_executes_both_nodes_and_rolls_up_its_cost(
    admin: ApiClient, project: Any, db: AsyncSession
) -> None:
    created = await launch_chain(admin, project.id)
    assert created["status"] == RunStatus.QUEUED
    assert [node["id"] for node in created["nodes"]] == ["1.1.2", "1.1.4"]
    assert created["edges"] == [{"source": "1.1.2", "target": "1.1.4"}]
    assert all(node["status"] is None for node in created["nodes"])

    fake = FakeOpenRouter()
    script_two_node_run(fake)
    result = await execute(created["id"], fake)

    assert result.status is RunStatus.SUCCEEDED
    assert result.nodes_executed == 2

    state = (await admin.get(f"/runs/{created['id']}")).json()
    assert state["status"] == RunStatus.SUCCEEDED
    assert [node["status"] for node in state["nodes"]] == [
        NodeRunStatus.SUCCEEDED,
        NodeRunStatus.SUCCEEDED,
    ]
    # 100 prompt + 50 completion tokens per node, priced by the catalogue.
    # Both nodes are EXTRACT, so both route to gemini flash.
    assert Decimal(state["cost_usd"]) == Decimal("0.0004")
    assert state["token_in"] == 200
    assert state["token_out"] == 100
    assert state["nodes"][0]["model"] == "google/gemini-2.5-flash"
    assert state["nodes"][1]["model"] == "google/gemini-2.5-flash"


async def test_the_node_detail_endpoint_returns_output_prompt_and_metrics(
    admin: ApiClient, project: Any
) -> None:
    created = await launch_chain(admin, project.id)
    fake = FakeOpenRouter()
    script_two_node_run(fake)
    await execute(created["id"], fake)

    detail = (await admin.get(f"/runs/{created['id']}/nodes/1.1.2")).json()
    assert detail["status"] == NodeRunStatus.SUCCEEDED
    assert detail["attempt"] == 1
    assert detail["input_hash"]
    assert len(detail["evidence_ids"]) == 4, "the node must record the CRM rows it read"
    # The labels came from the model; every number came from pandas.
    labels = [segment["label"] for segment in detail["output"]["segments"]]
    assert labels == ["German plant operators", "US chemical distributors"]
    assert [item["share_of_revenue_pct"] for item in detail["output"]["segments"]] == [60.0, 40.0]
    # The prompt is stored, not re-rendered: a repair pass or a model
    # substitution would make a re-render a different prompt from the one sent.
    assert "sdsmanager.com" in detail["prompt"]
    assert detail["latency_ms"] is not None


async def test_a_dependent_node_reads_its_dependency_output(admin: ApiClient, project: Any) -> None:
    created = await launch_chain(admin, project.id)
    fake = FakeOpenRouter()
    script_two_node_run(fake)
    await execute(created["id"], fake)

    downstream = (await admin.get(f"/runs/{created['id']}/nodes/1.1.4")).json()["prompt"]
    assert "German plant operators" in downstream, "1.1.4 must see 1.1.2's output, not re-derive it"


async def test_launching_writes_an_audit_row_naming_the_actor(
    admin: ApiClient, project: Any, db: AsyncSession, workspace: Any
) -> None:
    created = await launch(admin, project.id)

    result = await db.execute(sa.select(AuditLog).where(AuditLog.action == "run.launched"))
    row = result.scalar_one()
    assert row.actor_id is not None
    assert row.target_id == uuid.UUID(created["id"])
    assert row.meta["mode"] == RunMode.FULL


async def test_a_partial_run_is_widened_to_the_nodes_it_depends_on(
    admin: ApiClient, project: Any
) -> None:
    """Selecting 1.1.4 alone would be unexecutable; the API widens rather than rejects."""
    created = await launch(admin, project.id, mode="partial", node_ids=["1.1.4"])
    assert created["mode"] == RunMode.PARTIAL
    assert created["selected_node_ids"] == ["1.1.2", "1.1.4"]


async def test_an_unknown_node_id_is_rejected_with_the_registered_set(
    admin: ApiClient, project: Any
) -> None:
    response = await admin.post(
        f"/projects/{project.id}/runs", json={"mode": "partial", "node_ids": ["9.9"]}
    )
    assert response.status_code == 422
    body = response.json()
    assert "9.9" in body["detail"]
    # Derived, not transcribed: what this route owes the caller is the set it
    # actually knows about, and PRD §10's census is checked in `test_dag.py`.
    # Research only — `POST /projects/{id}/runs` launches the research DAG, and
    # offering plan node ids here would be offering nodes it cannot run.
    from agent.db.models import RunStage
    from agent.orchestrator.registry import get_registry

    assert body["registered_nodes"] == list(get_registry().for_stage(RunStage.RESEARCH).ids)


async def test_reuse_cache_skips_a_node_whose_inputs_have_not_changed(
    admin: ApiClient, project: Any, db: AsyncSession
) -> None:
    first = await launch_chain(admin, project.id)
    fake = FakeOpenRouter()
    script_two_node_run(fake)
    await execute(first["id"], fake)
    assert len(fake.requests) == 2

    second = await launch_chain(admin, project.id, reuse_cache=True)
    reuse = FakeOpenRouter()
    result = await execute(second["id"], reuse)

    assert result.status is RunStatus.SUCCEEDED
    assert reuse.requests == [], "identical inputs must not be paid for twice"

    detail = (await admin.get(f"/runs/{second['id']}/nodes/1.1.2")).json()
    assert detail["output"]["segments"][0]["label"] == "German plant operators"
    assert Decimal((await admin.get(f"/runs/{second['id']}")).json()["cost_usd"]) == Decimal(0)


async def test_a_second_execution_of_a_finished_run_is_a_no_op(
    admin: ApiClient, project: Any, db: AsyncSession
) -> None:
    """arq redelivery must not re-run a run that already ended."""
    created = await launch_chain(admin, project.id)
    fake = FakeOpenRouter()
    script_two_node_run(fake)
    await execute(created["id"], fake)

    again = await execute(created["id"], FakeOpenRouter())
    assert again.nodes_executed == 0

    rows = (
        await db.execute(
            sa.select(sa.func.count())
            .select_from(NodeRun)
            .where(NodeRun.run_id == uuid.UUID(created["id"]))
        )
    ).scalar_one()
    assert rows == 2


async def test_run_reads_are_scoped_to_the_callers_workspace(
    admin: ApiClient, project: Any, db: AsyncSession
) -> None:
    """Every query goes through WorkspaceScopedRepo (PRD §18 law 6).

    The workspace table is a singleton, so there is no second workspace to sign
    into — the boundary is asserted where it is implemented instead.
    """
    from agent.db.repos import RunRepo

    created = await launch_chain(admin, project.id)
    run_id = uuid.UUID(created["id"])

    assert await RunRepo(db, project.workspace_id).get(run_id) is not None
    assert await RunRepo(db, uuid.uuid4()).get(run_id) is None
