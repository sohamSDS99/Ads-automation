"""S2-P3's exit criteria, end to end (Stage 02 PRD §21).

    "Three scenarios generate from real demand data; recalc returns a new
     forecast in <= 2 s for 40 campaigns; an edit that breaks the envelope
     returns `422` with the delta; forecast-service failure degrades without
     stopping the run."

Every clause of that sentence is a test here, driven through the real executor,
the real `DerivedWriter`, the real approvals API and a real Postgres. Only the
model provider is scripted — and it is scripted with answers that contain **no
figures**, because a node that got a number from the model could not have,
there being none to get.

Three things the unit suite cannot prove and this file does. The **gate resumes
into 2.2**: 2.2.2 depends on gate G1, so the budget branch only exists once a
marketing lead has decided the targets, and a test that stubbed the gate would
never find that out. The **recalc endpoint writes a real `PlanCalc` row**, which
is a unique constraint and therefore a property of the database rather than of a
stub writer. And the **envelope guard sits on the decide path**, where it has to
be: `approvals.decide` writes `edited_proposal` straight onto the `NodeRun`
output without re-executing the node, so a split that does not balance would be
read by everything downstream as if it did.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import (
    Approval,
    ApprovalStatus,
    Evidence,
    EvidenceSource,
    NodeRunStatus,
    PlanCalc,
    RunStatus,
)
from agent.nodes.plan import stage_2_2
from tests.integration.conftest import ApiClient
from tests.integration.plan_gates import gate_g3, run_to_g1, run_to_g3
from tests.integration.runs_support import execute
from tests.openrouter_fake import FakeOpenRouter

pytestmark = pytest.mark.anyio

#: Same shape as the 2.1 suite's, so the ceiling behind the targets is the one
#: that file already hand-checks.

# ---------------------------------------------------------------------------
# "three scenarios generate from real demand data"
# ---------------------------------------------------------------------------


async def test_the_budget_branch_runs_and_halts_on_g3(
    admin: ApiClient, accepted: dict[str, Any], fake_openrouter: FakeOpenRouter
) -> None:
    plan_run_id, _ = await run_to_g3(admin, accepted, fake_openrouter)

    state = (await admin.get(f"/runs/{plan_run_id}")).json()
    by_node = {node["id"]: node["status"] for node in state["nodes"]}
    assert by_node["2.2.1"] == NodeRunStatus.SUCCEEDED
    assert by_node["2.2.2"] == NodeRunStatus.SUCCEEDED
    assert by_node["2.2.3"] == NodeRunStatus.SUCCEEDED
    assert by_node["2.2.4"] == NodeRunStatus.AWAITING_APPROVAL
    # 2.2.5 is below the gate and must not have run on an unapproved budget.
    # Absent, not `skipped`: a gate halts its branch without failing it, so the
    # nodes under it have no `NodeRun` at all until the gate is decided.
    assert "2.2.5" not in by_node or by_node["2.2.5"] != NodeRunStatus.SUCCEEDED
    assert state["status"] == RunStatus.AWAITING_APPROVAL


async def test_three_scenarios_come_out_of_the_real_demand_map(
    admin: ApiClient, accepted: dict[str, Any], fake_openrouter: FakeOpenRouter
) -> None:
    plan_run_id, _ = await run_to_g3(admin, accepted, fake_openrouter)
    scenarios = (await admin.get(f"/runs/{plan_run_id}/nodes/2.2.3")).json()["output"]

    assert [item["name"] for item in scenarios["scenarios"]] == [
        "cautious",
        "expected",
        "aggressive",
    ]
    assert all(item["allocation"] for item in scenarios["scenarios"])
    assert scenarios["recommended"] in {"cautious", "expected", "aggressive"}
    assert scenarios["forecast_monthly_usd"] > 0


async def test_the_forecast_is_rated_on_the_accounts_own_history(
    admin: ApiClient, accepted: dict[str, Any], fake_openrouter: FakeOpenRouter
) -> None:
    """Not the planning defaults. The account history seeded above is the whole
    difference between a measured forecast and an assumed one."""
    plan_run_id, _ = await run_to_g3(admin, accepted, fake_openrouter)
    forecast = (await admin.get(f"/runs/{plan_run_id}/nodes/2.2.1")).json()["output"]

    assert {row["ctr_pct"] for row in forecast["forecast"]} == {4.0}
    assert {row["cvr_pct"] for row in forecast["forecast"]} == {3.0}
    assert forecast["impression_share_headroom_pct"] == 65.0
    assert not any("planning default" in gap for gap in forecast["open_gaps"])


async def test_the_gate_is_labelled_g3_and_routes_to_an_approver(
    admin: ApiClient, accepted: dict[str, Any], fake_openrouter: FakeOpenRouter, db: AsyncSession
) -> None:
    plan_run_id, _ = await run_to_g3(admin, accepted, fake_openrouter)
    rows = (
        (
            await db.execute(
                sa.select(Approval).where(
                    Approval.run_id == plan_run_id, Approval.node_id == "2.2.4"
                )
            )
        )
        .scalars()
        .all()
    )

    assert [row.gate_key for row in rows] == ["G3"]
    assert rows[0].status is ApprovalStatus.PENDING
    assert rows[0].proposal["envelope"]["monthly_cap_usd"] > 0


async def test_every_number_on_the_budget_gate_resolves_to_a_plan_calc_row(
    admin: ApiClient, accepted: dict[str, Any], fake_openrouter: FakeOpenRouter, db: AsyncSession
) -> None:
    """PT1, for the branch this phase built."""
    plan_run_id, _ = await run_to_g3(admin, accepted, fake_openrouter)
    gate = await gate_g3(admin, plan_run_id)

    cited = {uuid.UUID(item) for item in gate["proposal"]["calc_evidence_ids"]}
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


async def test_the_forecast_is_one_calculation_shared_across_the_stage(
    admin: ApiClient, accepted: dict[str, Any], fake_openrouter: FakeOpenRouter, db: AsyncSession
) -> None:
    """`DerivedWriter` dedupes on `(plan_run_id, formula_id, inputs_hash)`, so
    four nodes citing one forecast is a property of a unique constraint rather
    than of a stub writer."""
    plan_run_id, _ = await run_to_g3(admin, accepted, fake_openrouter)
    rows = (
        (
            await db.execute(
                sa.select(PlanCalc).where(
                    PlanCalc.plan_run_id == plan_run_id,
                    PlanCalc.formula_id == stage_2_2.TRAFFIC,
                )
            )
        )
        .scalars()
        .all()
    )

    assert len(rows) == 1


# ---------------------------------------------------------------------------
# "forecast-service failure degrades without stopping the run"
# ---------------------------------------------------------------------------


async def test_a_run_with_no_google_ads_account_still_produces_a_budget(
    admin: ApiClient,
    db: AsyncSession,
    project: Any,
    workspace_id: uuid.UUID,
    accepted: dict[str, Any],
    fake_openrouter: FakeOpenRouter,
) -> None:
    """PRD §18: "Google Ads account not connected at all — 2.2.1 runs on Stage
    01 data only". It runs, and the gap names what it ran on instead."""
    await db.execute(
        sa.delete(Evidence).where(
            Evidence.project_id == accepted["project_id"],
            Evidence.source == EvidenceSource.GOOGLE_ADS,
        )
    )
    await db.commit()

    plan_run_id, _ = await run_to_g3(admin, accepted, fake_openrouter)
    forecast = (await admin.get(f"/runs/{plan_run_id}/nodes/2.2.1")).json()["output"]
    gate = await gate_g3(admin, plan_run_id)

    assert forecast["status"] == "ok"
    assert any("planning default" in gap for gap in forecast["open_gaps"])
    assert forecast["impression_share_headroom_pct"] is None
    assert gate["proposal"]["envelope"]["monthly_cap_usd"] > 0


# ---------------------------------------------------------------------------
# "recalc returns a new forecast in <= 2 s for 40 campaigns"
# ---------------------------------------------------------------------------


async def test_recalc_re_forecasts_an_edit_without_moving_the_run(
    admin: ApiClient, accepted: dict[str, Any], fake_openrouter: FakeOpenRouter, db: AsyncSession
) -> None:
    plan_run_id, _ = await run_to_g3(admin, accepted, fake_openrouter)
    gate = await gate_g3(admin, plan_run_id)
    lines = gate["proposal"]["allocation"]
    envelope = gate["proposal"]["envelope"]["monthly_cap_usd"]

    first = lines[0]
    response = await admin.post(
        f"/approvals/{gate['id']}/recalc",
        json={
            "allocation": [
                {
                    "campaign_ref": first["campaign_ref"],
                    "market": first["market"],
                    "funnel_stage": first["funnel_stage"],
                    "usd": round(first["usd"] + 500, 2),
                }
            ]
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["requested_usd"] == pytest.approx(envelope + 500, abs=0.05)
    assert body["delta_usd"] == pytest.approx(500.0, abs=0.05)
    assert body["envelope_breach"] is True
    assert body["calc_evidence_id"]

    # Nothing moved: the gate is still open and the run is still parked.
    still = await gate_g3(admin, plan_run_id)
    assert still["status"] == ApprovalStatus.PENDING
    state = (await admin.get(f"/runs/{plan_run_id}")).json()
    assert state["status"] == RunStatus.AWAITING_APPROVAL


async def test_recalc_writes_the_plan_calc_row_its_figures_cite(
    admin: ApiClient, accepted: dict[str, Any], fake_openrouter: FakeOpenRouter, db: AsyncSession
) -> None:
    """The documented deviation from §16 rule 3, and the reason for it.

    Without this row an approved *edited* allocation would reach the plan
    citing the draft split — a citation that resolves but does not justify —
    and PT1 would be quietly false for every gate anyone edited.
    """
    plan_run_id, _ = await run_to_g3(admin, accepted, fake_openrouter)
    gate = await gate_g3(admin, plan_run_id)
    first = gate["proposal"]["allocation"][0]
    edit = {
        "campaign_ref": first["campaign_ref"],
        "market": first["market"],
        "funnel_stage": first["funnel_stage"],
        "usd": round(first["usd"] + 100, 2),
    }

    body = (await admin.post(f"/approvals/{gate['id']}/recalc", json={"allocation": [edit]})).json()
    row = (
        await db.execute(
            sa.select(PlanCalc).where(
                PlanCalc.plan_run_id == plan_run_id,
                PlanCalc.evidence_id == uuid.UUID(body["calc_evidence_id"]),
            )
        )
    ).scalar_one()

    assert row.formula_id == "allocation.whatif_v1"
    assert row.node_id == "2.2.4"
    assert row.inputs_hash

    # Repeating the same what-if resolves to the same row rather than writing a
    # second one — which is what bounds a budget owner dragging a slider.
    again = (
        await db.execute(
            sa.select(sa.func.count())
            .select_from(PlanCalc)
            .where(
                PlanCalc.plan_run_id == plan_run_id,
                PlanCalc.formula_id == "allocation.whatif_v1",
            )
        )
    ).scalar_one()
    repeated = await admin.post(f"/approvals/{gate['id']}/recalc", json={"allocation": [edit]})
    assert repeated.status_code == 200
    after = (
        await db.execute(
            sa.select(sa.func.count())
            .select_from(PlanCalc)
            .where(
                PlanCalc.plan_run_id == plan_run_id,
                PlanCalc.formula_id == "allocation.whatif_v1",
            )
        )
    ).scalar_one()
    assert after == again


async def test_recalc_stores_its_working_so_a_second_approver_sees_it(
    admin: ApiClient, accepted: dict[str, Any], fake_openrouter: FakeOpenRouter, db: AsyncSession
) -> None:
    plan_run_id, _ = await run_to_g3(admin, accepted, fake_openrouter)
    gate = await gate_g3(admin, plan_run_id)
    first = gate["proposal"]["allocation"][0]

    await admin.post(
        f"/approvals/{gate['id']}/recalc",
        json={
            "allocation": [
                {
                    "campaign_ref": first["campaign_ref"],
                    "market": first["market"],
                    "funnel_stage": first["funnel_stage"],
                    "usd": round(first["usd"] + 250, 2),
                }
            ]
        },
    )
    row = await db.get(Approval, uuid.UUID(gate["id"]))
    assert row is not None
    await db.refresh(row)

    assert row.recalc_state is not None
    assert row.recalc_state["delta_usd"] == pytest.approx(250.0, abs=0.05)
    assert row.status is ApprovalStatus.PENDING

    # And it reaches the second approver, which is the whole point of storing
    # it. The column was written from the first release of `/recalc` and served
    # by nothing, so the card could only ever show the draft — a budget visibly
    # edited with no account of what the edit buys.
    reopened = await gate_g3(admin, plan_run_id)
    assert reopened["recalc_state"] is not None
    assert reopened["recalc_state"]["delta_usd"] == pytest.approx(250.0, abs=0.05)
    assert reopened["recalc_state"]["allocation"][0]["requested_usd"] == pytest.approx(
        first["usd"] + 250, abs=0.05
    )
    assert reopened["recalc_state"]["calc_evidence_id"]


async def test_a_gate_with_no_what_if_carries_no_recalc_state(
    admin: ApiClient, accepted: dict[str, Any], fake_openrouter: FakeOpenRouter
) -> None:
    """Null, not an empty object: the card tells them apart.

    Restoring `{}` into the editor would read as "somebody recalculated and it
    came back empty", which is a different thing from "nobody has asked yet".
    """
    plan_run_id, _ = await run_to_g3(admin, accepted, fake_openrouter)
    gate = await gate_g3(admin, plan_run_id)
    assert gate["recalc_state"] is None


async def test_recalc_of_forty_campaigns_across_four_markets_is_under_two_seconds(
    admin: ApiClient, accepted: dict[str, Any], fake_openrouter: FakeOpenRouter, db: AsyncSession
) -> None:
    """PF3, measured rather than asserted by construction.

    The proposal is widened to 160 units in the database first — this fixture's
    demand map produces a handful — because the threshold is about the size of
    the frame, not about how the frame came to be that size.
    """
    plan_run_id, _ = await run_to_g3(admin, accepted, fake_openrouter)
    gate = await gate_g3(admin, plan_run_id)
    row = await db.get(Approval, uuid.UUID(gate["id"]))
    assert row is not None

    template = gate["proposal"]["allocation"][0]
    wide = [
        {
            **template,
            "campaign_ref": f"campaign-{index:02d}",
            "market": market,
            "usd": 1_000.0,
            "forecast_cpa_usd": 200.0,
            "avg_cpc_usd": 4.0,
            "max_spend_usd": 1_800.0,
        }
        for index in range(40)
        for market in ("GB", "DE", "FR", "US")
    ]
    proposal = {
        **gate["proposal"],
        "allocation": wide,
        "envelope": {**gate["proposal"]["envelope"], "monthly_cap_usd": 160_000.0},
    }
    row.proposal = proposal
    await db.commit()

    edits = [
        {
            "campaign_ref": line["campaign_ref"],
            "market": line["market"],
            "funnel_stage": line["funnel_stage"],
            "usd": 1_000.0,
        }
        for line in wide
    ]
    started = time.perf_counter()
    response = await admin.post(f"/approvals/{gate['id']}/recalc", json={"allocation": edits})
    elapsed = time.perf_counter() - started

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["allocation"]) == 160
    assert body["requested_usd"] == 160_000.0
    assert body["envelope_breach"] is False
    assert elapsed < 2.0, f"recalc of 160 units took {elapsed:.3f}s"


async def test_recalc_names_a_line_the_proposal_does_not_contain(
    admin: ApiClient, accepted: dict[str, Any], fake_openrouter: FakeOpenRouter
) -> None:
    plan_run_id, _ = await run_to_g3(admin, accepted, fake_openrouter)
    gate = await gate_g3(admin, plan_run_id)

    body = (
        await admin.post(
            f"/approvals/{gate['id']}/recalc",
            json={
                "allocation": [
                    {
                        "campaign_ref": "a-campaign-nobody-planned",
                        "market": "US",
                        "funnel_stage": "bofu",
                        "usd": 5_000,
                    }
                ]
            },
        )
    ).json()

    assert body["unknown_lines"] == ["a-campaign-nobody-planned/US/bofu"]


async def test_recalc_is_refused_on_a_gate_that_carries_no_budget(
    admin: ApiClient, accepted: dict[str, Any], fake_openrouter: FakeOpenRouter
) -> None:
    plan_run_id, _ = await run_to_g1(admin, accepted, fake_openrouter)
    inbox = (await admin.get(f"/approvals?run_id={plan_run_id}")).json()["items"]
    g2 = next(item for item in inbox if item["gate_key"] == "G2")

    response = await admin.post(
        f"/approvals/{g2['id']}/recalc",
        json={
            "allocation": [{"campaign_ref": "x", "market": "GB", "funnel_stage": "bofu", "usd": 1}]
        },
    )

    assert response.status_code == 422
    assert response.json()["gate_key"] == "G2"


async def test_a_viewer_may_see_the_working_but_not_decide(
    admin: ApiClient, accepted: dict[str, Any], fake_openrouter: FakeOpenRouter, signed_in_as: Any
) -> None:
    """`/recalc` is `READ`: a viewer may follow the reasoning behind a decision
    it cannot make. Nothing it does mutates the plan."""
    plan_run_id, _ = await run_to_g3(admin, accepted, fake_openrouter)
    gate = await gate_g3(admin, plan_run_id)
    first = gate["proposal"]["allocation"][0]
    viewer = await signed_in_as("viewer")

    seen = await viewer.post(
        f"/approvals/{gate['id']}/recalc",
        json={
            "allocation": [
                {
                    "campaign_ref": first["campaign_ref"],
                    "market": first["market"],
                    "funnel_stage": first["funnel_stage"],
                    "usd": first["usd"],
                }
            ]
        },
    )
    refused = await viewer.post(f"/approvals/{gate['id']}", json={"decision": "approve"})

    assert seen.status_code == 200, seen.text
    assert refused.status_code == 403


# ---------------------------------------------------------------------------
# "an edit that breaks the envelope returns 422 with the delta"
# ---------------------------------------------------------------------------


async def test_an_edit_that_misses_the_envelope_is_refused_with_the_delta(
    admin: ApiClient, accepted: dict[str, Any], fake_openrouter: FakeOpenRouter, db: AsyncSession
) -> None:
    plan_run_id, _ = await run_to_g3(admin, accepted, fake_openrouter)
    gate = await gate_g3(admin, plan_run_id)
    proposal = gate["proposal"]
    envelope = proposal["envelope"]["monthly_cap_usd"]

    broken = {
        **proposal,
        "allocation": [
            {
                **proposal["allocation"][0],
                "usd": round(proposal["allocation"][0]["usd"] + 5_000, 2),
            },
            *proposal["allocation"][1:],
        ],
    }
    response = await admin.post(
        f"/approvals/{gate['id']}",
        json={"decision": "approve", "edited_proposal": broken},
    )

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["delta_usd"] == pytest.approx(5_000.0, abs=0.05)
    assert body["envelope_usd"] == pytest.approx(envelope, abs=0.05)
    assert body["tolerance_pct"] == 0.5
    assert "over" in body["detail"]

    # "No partial write": the gate is untouched and the run has not resumed.
    row = await db.get(Approval, uuid.UUID(gate["id"]))
    assert row is not None
    await db.refresh(row)
    assert row.status is ApprovalStatus.PENDING
    assert row.edited_proposal is None


async def test_a_balanced_edit_is_accepted_and_resumes_the_branch(
    admin: ApiClient, accepted: dict[str, Any], fake_openrouter: FakeOpenRouter, db: AsyncSession
) -> None:
    """The other half: the guard refuses a split that does not add up, not any
    edit at all. Moving money between two lines is exactly what G3 is for."""
    plan_run_id, _ = await run_to_g3(admin, accepted, fake_openrouter)
    gate = await gate_g3(admin, plan_run_id)
    proposal = gate["proposal"]
    lines = proposal["allocation"]
    if len(lines) < 2:
        pytest.skip("this fixture's demand map produced a single allocation line")

    moved = round(min(lines[0]["usd"], lines[1]["usd"]) / 2, 2)
    balanced = {
        **proposal,
        "allocation": [
            {**lines[0], "usd": round(lines[0]["usd"] - moved, 2)},
            {**lines[1], "usd": round(lines[1]["usd"] + moved, 2)},
            *lines[2:],
        ],
    }
    response = await admin.post(
        f"/approvals/{gate['id']}",
        json={"decision": "approve", "edited_proposal": balanced, "note": "Shifted to Germany."},
    )

    assert response.status_code == 200, response.text
    assert response.json()["approval"]["status"] == ApprovalStatus.APPROVED

    row = await db.get(Approval, uuid.UUID(gate["id"]))
    assert row is not None
    await db.refresh(row)
    assert row.edited_proposal is not None
    assert row.edited_proposal["allocation"][1]["usd"] == round(lines[1]["usd"] + moved, 2)


async def test_the_editor_s_round_trip_is_accepted_and_cites_its_what_if(
    admin: ApiClient, accepted: dict[str, Any], fake_openrouter: FakeOpenRouter, db: AsyncSession
) -> None:
    """Recalculate, then approve what it returned — the allocation editor's path.

    The editor refuses to approve an edited split until a what-if answers the
    figures on screen, and then sends the proposal built out of that response:
    the server's own re-forecast rows, `edits_applied` naming what moved, and
    the what-if's `PlanCalc` row first in `calc_evidence_ids`. That last part is
    what `/recalc` asks callers to do and what PT1 rests on — an approved edit
    that kept only the draft's ids would cite a calculation of a split that was
    replaced.

    Pinned here rather than in the browser check because it is a contract, and a
    contract that is only exercised by a screenshot is a contract that breaks on
    a refactor nobody screenshots.
    """
    plan_run_id, _ = await run_to_g3(admin, accepted, fake_openrouter)
    gate = await gate_g3(admin, plan_run_id)
    proposal = gate["proposal"]
    lines = proposal["allocation"]
    if len(lines) < 2:
        pytest.skip("this fixture's demand map produced a single allocation line")

    moved = round(min(lines[0]["usd"], lines[1]["usd"]) / 2, 2)
    edited = [
        {**_unit(lines[0]), "usd": round(lines[0]["usd"] - moved, 2)},
        {**_unit(lines[1]), "usd": round(lines[1]["usd"] + moved, 2)},
    ]

    whatif = (
        await admin.post(
            f"/approvals/{gate['id']}/recalc",
            json={"allocation": edited, "envelope_usd": proposal["envelope"]["monthly_cap_usd"]},
        )
    ).json()
    assert whatif["envelope_breach"] is False, whatif
    assert whatif["calc_evidence_id"]

    # Exactly what `buildEditedProposal` assembles in the web app.
    by_unit = {
        (row["campaign_ref"], row["market"], row["funnel_stage"]): row
        for row in whatif["allocation"]
    }
    allocation = []
    for line in lines:
        row = by_unit.get((line["campaign_ref"], line["market"], line["funnel_stage"]))
        allocation.append(
            line
            if row is None
            else {
                **line,
                "usd": row["requested_usd"],
                "pct": row["pct"],
                "est_conv": row["est_conv"],
                "est_clicks": row["est_clicks"],
                "below_floor": row["below_floor"],
                "cap_applied": row["cap_applied"],
            }
        )
    body = {
        **proposal,
        "allocation": allocation,
        "edits_applied": [
            {**_unit(lines[0]), "from_usd": lines[0]["usd"], "to_usd": allocation[0]["usd"]},
            {**_unit(lines[1]), "from_usd": lines[1]["usd"], "to_usd": allocation[1]["usd"]},
        ],
        "calc_evidence_ids": [whatif["calc_evidence_id"], *proposal["calc_evidence_ids"]],
    }

    response = await admin.post(
        f"/approvals/{gate['id']}",
        json={"decision": "approve", "edited_proposal": body, "note": "Moved it to Germany."},
    )
    assert response.status_code == 200, response.text

    row = await db.get(Approval, uuid.UUID(gate["id"]))
    assert row is not None
    await db.refresh(row)
    assert row.edited_proposal is not None
    # The figures that were approved are the ones the what-if produced...
    assert row.edited_proposal["allocation"][1]["usd"] == pytest.approx(
        lines[1]["usd"] + moved, abs=0.01
    )
    # ...and they cite the calculation that produced them.
    assert row.edited_proposal["calc_evidence_ids"][0] == whatif["calc_evidence_id"]
    assert len(row.edited_proposal["edits_applied"]) == 2

    # The citation resolves to a row belonging to this plan run, which is the
    # half of PT1 an id alone does not prove.
    cited = (
        (
            await db.execute(
                sa.select(PlanCalc).where(
                    PlanCalc.plan_run_id == plan_run_id,
                    PlanCalc.formula_id == "allocation.whatif_v1",
                )
            )
        )
        .scalars()
        .all()
    )
    assert cited, "the what-if wrote no PlanCalc row for the edited split to cite"


def _unit(line: dict[str, Any]) -> dict[str, Any]:
    return {
        "campaign_ref": line["campaign_ref"],
        "market": line["market"],
        "funnel_stage": line["funnel_stage"],
    }


async def test_an_approved_budget_lets_2_2_5_write_its_rules(
    admin: ApiClient, accepted: dict[str, Any], fake_openrouter: FakeOpenRouter
) -> None:
    """The branch below the gate, and the thing that proves the gate resumed it.

    2.2.5 re-checks the **approved** allocation rather than the forecast, which
    is a different `PlanCalc` row and the one a reallocation rule must not push
    a campaign below.
    """
    plan_run_id, _ = await run_to_g3(admin, accepted, fake_openrouter)
    gate = await gate_g3(admin, plan_run_id)

    decided = await admin.post(f"/approvals/{gate['id']}", json={"decision": "approve"})
    assert decided.status_code == 200, decided.text
    result = await execute(plan_run_id, fake_openrouter)
    assert result.error is None, result.error

    state = (await admin.get(f"/runs/{plan_run_id}")).json()
    by_node = {node["id"]: node["status"] for node in state["nodes"]}
    assert by_node["2.2.5"] == NodeRunStatus.SUCCEEDED

    rules = (await admin.get(f"/runs/{plan_run_id}/nodes/2.2.5")).json()["output"]
    assert rules["review_cadence"] == "monthly"
    for rule in rules["rules"]:
        # Every figure on a rule came from a constant or a calculation.
        assert rule["lookback_days"] == 14.0
        assert rule["cooldown_days"] == 14.0
        assert rule["max_shift_pct"] in {0.0, 10.0, 20.0}
    assert rules["calc_evidence_ids"]
