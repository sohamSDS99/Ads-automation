"""`GET /plans/{plan_run_id}/calcs` — the read side of law 14 (Stage 02 PRD §16).

S2-P1 writes `plan_calc` rows; nothing served them until now, so the Calc tab
of the Plan Console had no source. These tests are about what a browser gets:
the filter the tab actually sends, the workspace boundary the table cannot
enforce on its own (it has no `workspace_id` — the run it hangs off decides),
and the two shapes that are *not* errors, because an empty answer and a
failure look identical to a panel that treats both as "no data".

`stage` on `GET /runs/{id}` is asserted here too. It is the field the console
reads to refuse a run opened under the wrong route, and a response model that
quietly stopped emitting it would turn that guard off without failing anything
else.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import (
    PlanCalc,
    Run,
    RunStage,
    RunStatus,
    RunTrigger,
)
from tests.integration.conftest import ApiClient

pytestmark = pytest.mark.asyncio


async def _plan_run(
    db: AsyncSession, *, project_id: uuid.UUID, workspace_id: uuid.UUID, user_id: uuid.UUID
) -> Run:
    """A plan run. `source_run_id` is not optional — migration 0013 CHECKs it."""
    source = Run(
        workspace_id=workspace_id,
        project_id=project_id,
        triggered_by=user_id,
        trigger=RunTrigger.MANUAL,
        status=RunStatus.SUCCEEDED,
        stage=RunStage.RESEARCH,
    )
    db.add(source)
    await db.flush()

    run = Run(
        workspace_id=workspace_id,
        project_id=project_id,
        triggered_by=user_id,
        trigger=RunTrigger.MANUAL,
        status=RunStatus.RUNNING,
        stage=RunStage.PLAN,
        source_run_id=source.id,
    )
    db.add(run)
    await db.commit()
    return run


def _calc(run_id: uuid.UUID, node_id: str, formula_id: str, value: float) -> PlanCalc:
    return PlanCalc(
        plan_run_id=run_id,
        node_id=node_id,
        formula_id=formula_id,
        calc_version="2026.09.1+code.1",
        inputs={"acv_usd": 60000, "gross_margin_pct": 80},
        inputs_hash=uuid.uuid4().hex,
        result={"max_cpa_won_usd": value},
    )


@pytest_asyncio.fixture
async def research_run(
    admin: ApiClient, db: AsyncSession, project: Any, workspace_id: uuid.UUID
) -> Run:
    """A plain Stage 01 run — the thing this endpoint must answer `[]` for."""
    me = (await admin.get("/auth/me")).json()
    run = Run(
        workspace_id=workspace_id,
        project_id=project.id,
        triggered_by=uuid.UUID(me["id"]),
        trigger=RunTrigger.MANUAL,
        status=RunStatus.SUCCEEDED,
        stage=RunStage.RESEARCH,
    )
    db.add(run)
    await db.commit()
    return run


@pytest_asyncio.fixture
async def planned(
    admin: ApiClient, db: AsyncSession, project: Any, workspace_id: uuid.UUID
) -> dict[str, Any]:
    me = (await admin.get("/auth/me")).json()
    run = await _plan_run(
        db,
        project_id=project.id,
        workspace_id=workspace_id,
        user_id=uuid.UUID(me["id"]),
    )
    db.add_all(
        [
            _calc(run.id, "2.1.2", "economics.max_cpa_v1", 420.0),
            _calc(run.id, "2.1.2", "economics.target_cpl_v1", 51.0),
            _calc(run.id, "2.2.1", "forecast.clicks_v1", 1800.0),
        ]
    )
    await db.commit()
    return {"run_id": run.id, "project_id": project.id}


async def test_it_returns_every_calculation_the_run_produced(
    admin: ApiClient, planned: dict[str, Any]
) -> None:
    response = await admin.get(f"/plans/{planned['run_id']}/calcs")
    assert response.status_code == 200, response.text

    items = response.json()["items"]
    assert len(items) == 3
    # Oldest first, then by the table's unique key. These three rows are written
    # in one transaction and therefore share `created_at` — `now()` is the
    # transaction clock — so without the tiebreaker this assertion would be
    # asserting the heap order.
    assert [item["formula_id"] for item in items] == [
        "economics.max_cpa_v1",
        "economics.target_cpl_v1",
        "forecast.clicks_v1",
    ]
    first = items[0]
    assert first["node_id"] == "2.1.2"
    assert first["result"] == {"max_cpa_won_usd": 420.0}
    assert first["inputs"]["acv_usd"] == 60000
    assert first["calc_version"] == "2026.09.1+code.1"
    # Nullable by design — the calculation outlives its evidence row.
    assert first["evidence_id"] is None
    # `inputs_hash` is a dedupe key, not something a panel renders.
    assert "inputs_hash" not in first


async def test_node_id_filters_to_one_node(admin: ApiClient, planned: dict[str, Any]) -> None:
    """The filter the Calc tab actually sends: one node at a time."""
    response = await admin.get(f"/plans/{planned['run_id']}/calcs?node_id=2.1.2")
    assert response.status_code == 200, response.text

    items = response.json()["items"]
    assert len(items) == 2
    assert {item["node_id"] for item in items} == {"2.1.2"}


async def test_a_node_that_computed_nothing_is_empty_not_an_error(
    admin: ApiClient, planned: dict[str, Any]
) -> None:
    """The Calc tab renders this as "no numbers from this node", not a failure."""
    response = await admin.get(f"/plans/{planned['run_id']}/calcs?node_id=2.6.2")
    assert response.status_code == 200, response.text
    assert response.json()["items"] == []


async def test_a_research_run_has_no_calcs_and_says_so_with_an_empty_list(
    admin: ApiClient, research_run: Run
) -> None:
    """Law 14 is Stage 02's. A research run is not an error here — it is empty."""
    response = await admin.get(f"/plans/{research_run.id}/calcs")
    assert response.status_code == 200, response.text
    assert response.json()["items"] == []


async def test_an_unknown_run_is_404(admin: ApiClient) -> None:
    response = await admin.get(f"/plans/{uuid.uuid4()}/calcs")
    assert response.status_code == 404, response.text


async def test_another_workspaces_run_is_404_not_an_empty_list(
    admin: ApiClient, planned: dict[str, Any], db: AsyncSession
) -> None:
    """`plan_calc` has no `workspace_id`; the run it hangs off is the boundary.

    An empty list here would be the wrong answer twice over — it would leak
    that the id exists, and it would read to the caller as "this run computed
    nothing".
    """
    from agent.db.models import Run as RunModel
    from agent.db.models import Workspace

    # A real row, not a bare uuid: `run.workspace_id` is a foreign key, so
    # inventing an id tests the constraint rather than the boundary.
    other = Workspace(name=f"Elsewhere {uuid.uuid4().hex[:6]}")
    db.add(other)
    await db.flush()

    stolen = await db.get(RunModel, planned["run_id"])
    assert stolen is not None
    stolen.workspace_id = other.id
    await db.commit()

    response = await admin.get(f"/plans/{planned['run_id']}/calcs")
    assert response.status_code == 404, response.text


async def test_a_viewer_may_read_the_calculations(
    planned: dict[str, Any], signed_in_as: Any
) -> None:
    """§16 puts this behind READ: auditing a number is not a privileged act."""
    viewer = await signed_in_as("viewer")
    response = await viewer.get(f"/plans/{planned['run_id']}/calcs")
    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == 3


async def test_the_run_response_names_its_stage(
    admin: ApiClient, planned: dict[str, Any], research_run: Run
) -> None:
    """What the console reads to refuse a run opened under the wrong route."""
    plan = await admin.get(f"/runs/{planned['run_id']}")
    assert plan.status_code == 200, plan.text
    assert plan.json()["stage"] == "plan"

    research = await admin.get(f"/runs/{research_run.id}")
    assert research.status_code == 200, research.text
    assert research.json()["stage"] == "research"
