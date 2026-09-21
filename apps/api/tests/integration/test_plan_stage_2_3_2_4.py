"""Stages 2.3 and 2.4 through the real executor (S2-P4).

The unit suites drive one node at a time with a hand-built context. This drives
the whole plan DAG on a real database, through all four gates, and asserts PRD
§21's four exit criteria for S2-P4 on the structure a real run produced:

1. a full campaign -> ad group -> keyword tree
2. every generated name passes `validator_regex`
3. brand terms appear only in the brand campaign
4. no keyword appears twice

It reuses `test_plan_stage_2_2`'s fixtures rather than re-declaring them: the
accepted research report, the seeded CRM and account history and the walk to G3
are the same twenty seconds of setup, and a second copy of them would be a
second thing to keep in step with the golden report.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import (
    ApprovalRequiredRole,
    ApprovalStatus,
    NodeRunStatus,
    PlanCalc,
    RunStatus,
)
from tests.integration.conftest import ApiClient
from tests.integration.plan_gates import gate_g3, run_to_g3
from tests.integration.runs_support import execute
from tests.openrouter_fake import FakeOpenRouter

pytestmark = pytest.mark.anyio

PLAN_NODES_2_3_2_4 = ("2.3.1", "2.3.2", "2.3.3", "2.4.1", "2.4.2", "2.4.3")


async def run_to_g4(
    admin: ApiClient, accepted: dict[str, Any], fake: FakeOpenRouter
) -> tuple[uuid.UUID, dict[str, Any]]:
    """Approve the budget at G3 and execute until the channel slate halts at G4."""
    plan_run_id, _ = await run_to_g3(admin, accepted, fake)
    budget = await gate_g3(admin, plan_run_id)
    decided = await admin.post(f"/approvals/{budget['id']}", json={"decision": "approve"})
    assert decided.status_code == 200, decided.text

    result = await execute(plan_run_id, fake)
    assert result.error is None, result.error

    pending = (await admin.get("/approvals?status=pending")).json()
    gate = next(
        (row for row in pending["items"] if row.get("gate_key") == "G4"),
        None,
    )
    assert gate is not None, f"G4 was not raised; pending: {pending['items']}"
    return plan_run_id, gate


async def run_past_g4(
    admin: ApiClient, accepted: dict[str, Any], fake: FakeOpenRouter
) -> uuid.UUID:
    plan_run_id, gate = await run_to_g4(admin, accepted, fake)
    decided = await admin.post(f"/approvals/{gate['id']}", json={"decision": "approve"})
    assert decided.status_code == 200, decided.text
    result = await execute(plan_run_id, fake)
    assert result.error is None, result.error
    return plan_run_id


async def output_of(admin: ApiClient, plan_run_id: uuid.UUID, node_id: str) -> dict[str, Any]:
    response = await admin.get(f"/runs/{plan_run_id}/nodes/{node_id}")
    assert response.status_code == 200, response.text
    return response.json()["output"]


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------


async def test_the_slate_halts_the_run_on_g4_for_an_approver(
    admin: ApiClient, accepted: dict[str, Any], fake_openrouter: FakeOpenRouter
) -> None:
    """G4 is the fourth and last gate. Law 16 is "four gates, no more"."""
    plan_run_id, gate = await run_to_g4(admin, accepted, fake_openrouter)

    assert gate["gate_key"] == "G4"
    assert gate["required_role"] == ApprovalRequiredRole.APPROVER.value
    assert gate["status"] == ApprovalStatus.PENDING.value

    state = (await admin.get(f"/runs/{plan_run_id}")).json()
    assert state["status"] == RunStatus.AWAITING_APPROVAL.value


async def test_every_share_on_the_gate_card_resolves_to_a_plan_calc_row(
    admin: ApiClient,
    accepted: dict[str, Any],
    fake_openrouter: FakeOpenRouter,
    db: AsyncSession,
) -> None:
    """Law 14, end to end: the percentages a human signs were computed, not written."""
    plan_run_id, gate = await run_to_g4(admin, accepted, fake_openrouter)
    proposal = gate["proposal"]

    cited = {uuid.UUID(item) for item in proposal["calc_evidence_ids"]}
    assert cited
    rows = (
        (
            await db.execute(
                sa.select(PlanCalc).where(
                    PlanCalc.plan_run_id == plan_run_id, PlanCalc.evidence_id.in_(cited)
                )
            )
        )
        .scalars()
        .all()
    )
    assert {row.evidence_id for row in rows} == cited
    assert all(row.formula_id and row.inputs_hash and row.calc_version for row in rows)
    assert proposal["slate"]
    assert all(entry["est_share_of_budget_pct"] >= 0 for entry in proposal["slate"])


async def test_approving_g4_runs_the_rest_of_2_3_and_all_of_2_4(
    admin: ApiClient, accepted: dict[str, Any], fake_openrouter: FakeOpenRouter
) -> None:
    plan_run_id = await run_past_g4(admin, accepted, fake_openrouter)
    state = (await admin.get(f"/runs/{plan_run_id}")).json()
    by_node = {node["id"]: node["status"] for node in state["nodes"]}
    for node_id in PLAN_NODES_2_3_2_4:
        assert by_node.get(node_id) == NodeRunStatus.SUCCEEDED, f"{node_id}: {by_node.get(node_id)}"


# ---------------------------------------------------------------------------
# PRD §21's four exit criteria, on a structure a real run produced
# ---------------------------------------------------------------------------


async def test_the_run_produces_a_campaign_ad_group_keyword_tree(
    admin: ApiClient, accepted: dict[str, Any], fake_openrouter: FakeOpenRouter
) -> None:
    plan_run_id = await run_past_g4(admin, accepted, fake_openrouter)
    structure = await output_of(admin, plan_run_id, "2.4.2")

    assert structure["campaigns"], "no campaign was built"
    keyworded = [campaign for campaign in structure["campaigns"] if campaign["ad_groups"]]
    assert keyworded, f"no campaign carries an ad group; gaps: {structure['open_gaps']}"
    for campaign in keyworded:
        assert campaign["monthly_budget_usd"] > 0
        assert campaign["daily_budget_usd"] > 0
        for group in campaign["ad_groups"]:
            assert group["landing_url"]
            assert group["keywords"], f"{group['name']} has no keyword"


async def test_every_generated_name_passes_the_conventions_own_regex(
    admin: ApiClient, accepted: dict[str, Any], fake_openrouter: FakeOpenRouter
) -> None:
    """§12 invariant 5, against the regex node 2.4.1 compiled in the same run."""
    plan_run_id = await run_past_g4(admin, accepted, fake_openrouter)
    convention = await output_of(admin, plan_run_id, "2.4.1")
    structure = await output_of(admin, plan_run_id, "2.4.2")

    validator = re.compile(convention["validator_regex"])
    names = [
        *(campaign["name"] for campaign in structure["campaigns"]),
        *(group["name"] for campaign in structure["campaigns"] for group in campaign["ad_groups"]),
    ]
    assert names
    assert [name for name in names if not validator.match(name)] == []
    assert structure["invalid_names"] == []


async def test_a_brand_term_is_in_the_brand_campaign_and_negative_everywhere_else(
    admin: ApiClient, accepted: dict[str, Any], fake_openrouter: FakeOpenRouter
) -> None:
    plan_run_id = await run_past_g4(admin, accepted, fake_openrouter)
    brand = await output_of(admin, plan_run_id, "2.3.3")
    structure = await output_of(admin, plan_run_id, "2.4.2")

    brand_ref = brand["brand_campaign"]["campaign_ref"]
    terms = {term["term"] for term in brand["brand_terms"]}
    assert terms

    for campaign in structure["campaigns"]:
        carried = {
            keyword["term"] for group in campaign["ad_groups"] for keyword in group["keywords"]
        }
        if campaign["campaign_ref"] == brand_ref:
            continue
        assert not (carried & terms), f"{campaign['name']} carries a brand term"
        assert terms <= set(campaign["negatives"]), f"{campaign['name']} lacks the negatives"


async def test_no_keyword_appears_in_two_ad_groups(
    admin: ApiClient, accepted: dict[str, Any], fake_openrouter: FakeOpenRouter
) -> None:
    plan_run_id = await run_past_g4(admin, accepted, fake_openrouter)
    structure = await output_of(admin, plan_run_id, "2.4.2")

    seen = [
        keyword["term"]
        for campaign in structure["campaigns"]
        for group in campaign["ad_groups"]
        for keyword in group["keywords"]
    ]
    assert seen
    assert len(seen) == len(set(seen)), f"duplicated: {structure['duplicate_terms']}"
    assert structure["duplicate_terms"] == []


# ---------------------------------------------------------------------------
# the rest of what 2.3 and 2.4 owe
# ---------------------------------------------------------------------------


async def test_the_structure_verdict_is_computed_over_the_built_tree(
    admin: ApiClient, accepted: dict[str, Any], fake_openrouter: FakeOpenRouter
) -> None:
    """2.4.3 counts the ad groups that exist, which 2.2.2 structurally could not."""
    plan_run_id = await run_past_g4(admin, accepted, fake_openrouter)
    structure = await output_of(admin, plan_run_id, "2.4.2")
    checked = await output_of(admin, plan_run_id, "2.4.3")

    built = {
        campaign["campaign_ref"]: len(campaign["ad_groups"]) for campaign in structure["campaigns"]
    }
    assert checked["campaigns"]
    assert checked["structure_verdict"] in {"sound", "needs_merge", "too_thin"}
    for row in checked["campaigns"]:
        assert row["action"] in {"ship", "merge_into", "split", "defer"}
        if row["ad_group_count"] is not None:
            assert row["ad_group_count"] == built[row["ref"]]
    assert checked["calc_evidence_ids"]


async def test_the_collision_check_reports_whether_it_ran(
    admin: ApiClient, accepted: dict[str, Any], fake_openrouter: FakeOpenRouter
) -> None:
    """PRD §18: with no live account there is no collision report, not an empty one."""
    plan_run_id = await run_past_g4(admin, accepted, fake_openrouter)
    convention = await output_of(admin, plan_run_id, "2.4.1")

    assert convention["collision_check"] in {"checked", "skipped"}
    if convention["collision_check"] == "skipped":
        assert "account_snapshot_unavailable" in convention["open_gaps"]
        assert convention["collisions"] == []


async def test_the_overlap_report_names_every_pair_including_the_zeroes(
    admin: ApiClient, accepted: dict[str, Any], fake_openrouter: FakeOpenRouter
) -> None:
    """A missing pair is indistinguishable from a pair nobody checked."""
    plan_run_id = await run_past_g4(admin, accepted, fake_openrouter)
    slate = await output_of(admin, plan_run_id, "2.3.1")
    boundaries = await output_of(admin, plan_run_id, "2.3.2")

    entries = len(slate["slate"])
    assert len(boundaries["overlap"]) == entries * (entries - 1) // 2
    assert boundaries["pmax"]["brand_exclusion_required"] in {True, False}
    assert boundaries["calc_evidence_ids"]
