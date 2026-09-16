"""PRD §17 P3 acceptance: a partial run of stages 1.1 + 1.2.

    "Partial run of stages 1.1+1.2 produces validated outputs, halts on 1.1.5,
     an `approver` (not an `operator`) can resume it, decision is audit-logged"

This file proves the first half — eight nodes, validated Pydantic outputs, every
number computed in pandas rather than written by a model, and the run stopping
on the gate. `test_approvals.py` proves the second half.
"""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import Approval, ApprovalStatus, NodeRunStatus, RunStatus
from tests.integration.conftest import ApiClient
from tests.integration.runs_support import (
    by_output_model,
    execute,
    launch,
    seed_crm,
    seed_google_ads,
)
from tests.openrouter_fake import FakeOpenRouter

STAGE_NODES = ("1.1.1", "1.1.2", "1.1.3", "1.1.4", "1.1.5", "1.2.1", "1.2.2", "1.2.3")


async def run_both_stages(admin: ApiClient, project: Any) -> tuple[dict[str, Any], Any]:
    """Seed every source, launch all eight nodes, execute against scripted answers."""
    await seed_crm(project.id, lost=({"account_name": "Lost Co", "close_reason": "no budget"},))
    await seed_google_ads(project.id)
    created = await launch(admin, project.id, mode="partial", node_ids=list(STAGE_NODES))
    fake = FakeOpenRouter()
    by_output_model(fake)
    result = await execute(created["id"], fake)
    return created, result


async def test_the_run_halts_on_the_gate_with_every_other_branch_complete(
    admin: ApiClient, project: Any
) -> None:
    created, result = await run_both_stages(admin, project)

    assert result.status is RunStatus.AWAITING_APPROVAL
    assert result.awaiting == ("1.1.5",)

    state = (await admin.get(f"/runs/{created['id']}")).json()
    assert state["status"] == RunStatus.AWAITING_APPROVAL
    by_id = {node["id"]: node for node in state["nodes"]}
    assert set(by_id) == set(STAGE_NODES)

    assert by_id["1.1.5"]["status"] == NodeRunStatus.AWAITING_APPROVAL
    # Every independent branch finished. A gate halts its own branch and
    # nothing else (PRD §7.2 item 5).
    for node_id in ("1.1.1", "1.1.2", "1.1.3", "1.1.4", "1.2.1", "1.2.2", "1.2.3"):
        assert by_id[node_id]["status"] == NodeRunStatus.SUCCEEDED, node_id
    assert state["error"] is None, "a gate is not a failure"


async def test_nothing_downstream_of_the_gate_is_marked_skipped(
    admin: ApiClient, project: Any, db: AsyncSession
) -> None:
    """`skipped` means "this will never run". A waiting branch is not that."""
    created, _ = await run_both_stages(admin, project)
    state = (await admin.get(f"/runs/{created['id']}")).json()
    assert not [node for node in state["nodes"] if node["status"] == NodeRunStatus.SKIPPED]


async def test_the_gate_writes_one_pending_approval_for_the_approver_role(
    admin: ApiClient, project: Any, db: AsyncSession
) -> None:
    created, _ = await run_both_stages(admin, project)

    rows = (await db.execute(sa.select(Approval))).scalars().all()
    assert len(rows) == 1
    approval = rows[0]
    assert approval.run_id == uuid.UUID(created["id"])
    assert approval.node_id == "1.1.5"
    assert approval.status is ApprovalStatus.PENDING
    assert approval.required_role.value == "approver"
    # No assignee configured on this project, so any holder of the role may
    # decide it — the table's documented meaning for NULL.
    assert approval.assignee_id is None
    assert approval.proposal["prohibited_claims"] == ["100% compliance guaranteed"]


async def test_the_gate_emits_approval_required_on_the_stream(
    admin: ApiClient, project: Any
) -> None:
    """Read the Redis stream directly, not `GET /runs/{id}/events`.

    A paused run is not a finished one, so the SSE endpoint correctly keeps the
    connection open for whatever the resume will emit — which means reading it
    to completion in a test never returns.
    """
    from agent.orchestrator.events import EventType, RunEventStream
    from agent.redis_client import get_redis

    created, _ = await run_both_stages(admin, project)
    events = await RunEventStream(get_redis(), uuid.UUID(created["id"])).read()

    gate = [event for event in events if event.type is EventType.APPROVAL_REQUIRED]
    assert len(gate) == 1
    assert gate[0].data["node_id"] == "1.1.5"
    assert gate[0].data["required_role"] == "approver"
    assert gate[0].data["approval_id"]
    # The run is paused, not completed: no terminal frame yet.
    assert not [event for event in events if event.type is EventType.RUN_COMPLETED]


async def test_the_events_endpoint_stays_open_while_a_run_is_paused(
    admin: ApiClient, project: Any
) -> None:
    """A console watching a paused run must not be disconnected (PRD §7.3)."""
    from agent.orchestrator.state import TERMINAL_STATUSES

    created, _ = await run_both_stages(admin, project)
    state = (await admin.get(f"/runs/{created['id']}")).json()
    assert RunStatus(state["status"]) not in TERMINAL_STATUSES


async def test_every_number_in_the_icp_output_came_from_pandas(
    admin: ApiClient, project: Any
) -> None:
    """PRD §18 law 3: the model wrote the labels, `frames.py` wrote the arithmetic."""
    created, _ = await run_both_stages(admin, project)
    output = (await admin.get(f"/runs/{created['id']}/nodes/1.1.2")).json()["output"]

    segments = output["segments"]
    assert [item["label"] for item in segments] == [
        "German plant operators",
        "US chemical distributors",
    ]
    # 60,000 and 40,000 of 100,000 closed-won revenue.
    assert [item["share_of_revenue_pct"] for item in segments] == [60.0, 40.0]
    assert [item["revenue"] for item in segments] == [60000.0, 40000.0]
    assert [item["deals"] for item in segments] == [2, 2]
    assert all(item["evidence_ids"] for item in segments), "a segment must cite its rows"


async def test_the_economics_are_derived_from_the_models_assumptions_not_stated_by_it(
    admin: ApiClient, project: Any
) -> None:
    created, _ = await run_both_stages(admin, project)
    output = (await admin.get(f"/runs/{created['id']}/nodes/1.1.1")).json()["output"]

    # ACV is the measured mean of the four closed-won deals.
    assert output["economics"]["acv"] == 25000.0
    # 80% margin over 36 months, as the model assumed: 25000 * 3 * 0.8.
    assert output["economics"]["ltv_estimate"] == 60000.0
    assert output["target_cac"] == 20000.0
    assert output["payback_months"] == 12.0
    assert output["assumptions"]["gross_margin_pct"] == 80.0


async def test_the_search_term_pnl_merges_labels_onto_computed_money(
    admin: ApiClient, project: Any
) -> None:
    created, _ = await run_both_stages(admin, project)
    output = (await admin.get(f"/runs/{created['id']}/nodes/1.2.2")).json()["output"]

    assert [item["term"] for item in output["profitable_terms"]] == ["sds management software"]
    assert output["profitable_terms"][0]["roas"] == 20.0
    assert [item["term"] for item in output["wasteful_terms"]] == [
        "free sds template",
        "sds jobs",
    ]
    # The action is the model's; the cost is not.
    assert output["wasteful_terms"][0]["recommended_action"] == "negative_phrase"
    assert output["wasteful_terms"][0]["cost"] == 260.0
    # 310 of 400 spent bought nothing.
    assert output["totals"]["wasted_spend"] == 310.0
    assert output["totals"]["waste_pct"] == 77.5


async def test_a_campaign_delta_is_computed_and_only_the_verdict_is_written(
    admin: ApiClient, project: Any
) -> None:
    created, _ = await run_both_stages(admin, project)
    output = (await admin.get(f"/runs/{created['id']}/nodes/1.2.1")).json()["output"]

    assert [item["campaign"] for item in output["winners"]] == ["Generic"] or [
        item["campaign"] for item in output["winners"]
    ] == ["Brand"]
    rows = output["winners"] + output["losers"] + output["neutral"]
    brand = next(item for item in rows if item["campaign"] == "Brand")
    # 100/4 = 25.00 in the first half, 120/6 = 20.00 in the second: -20%.
    assert brand["cpa"] == 22.0
    assert brand["metric_delta"] == -20.0
    assert brand["period"] == "2025-01..2025-06"


async def test_a_node_with_no_evidence_returns_empty_rather_than_inventing(
    admin: ApiClient, project: Any
) -> None:
    """PRD §16: Google Ads unavailable → the section is empty, never hallucinated."""
    await seed_crm(project.id)
    created = await launch(admin, project.id, mode="partial", node_ids=["1.2.2"])
    fake = FakeOpenRouter()
    by_output_model(fake)
    result = await execute(created["id"], fake)

    assert result.status is RunStatus.SUCCEEDED
    assert fake.requests == [], "no evidence means no model call, not a guessed answer"
    output = (await admin.get(f"/runs/{created['id']}/nodes/1.2.2")).json()["output"]
    assert output["profitable_terms"] == []
    assert output["wasteful_terms"] == []
    assert output["coverage"] == ["search_term_pnl: unavailable"]


async def test_the_gate_records_its_prompt_and_its_citations(
    admin: ApiClient, project: Any
) -> None:
    created, _ = await run_both_stages(admin, project)
    detail = (await admin.get(f"/runs/{created['id']}/nodes/1.1.5")).json()

    assert detail["status"] == NodeRunStatus.AWAITING_APPROVAL
    assert detail["output"]["confidence"] == 0.4
    # 1.1.5 depends on 1.1.1 and has to see it, not re-derive it.
    assert "SDS Manager" in detail["prompt"]
    assert detail["evidence_ids"], "the gate read the change history and must cite it"
