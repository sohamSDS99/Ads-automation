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

import json
import time
import uuid
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import (
    Approval,
    ApprovalStatus,
    Evidence,
    EvidenceSource,
    NodeRunStatus,
    PlanCalc,
    Report,
    Run,
    RunStage,
    RunStatus,
    RunTrigger,
)
from agent.nodes.plan import stage_2_2
from agent.planning import demand
from tests.integration.conftest import ApiClient
from tests.integration.runs_support import execute, seed_crm
from tests.openrouter_fake import FakeOpenRouter
from tests.report_support import golden_payload

pytestmark = pytest.mark.anyio

#: Same shape as the 2.1 suite's, so the ceiling behind the targets is the one
#: that file already hand-checks.
WON = tuple(
    {
        "account_name": f"Acme {index}",
        "industry": "Chemicals",
        "deal_value": value,
        "outcome": "won",
    }
    for index, value in enumerate((26_000, 22_000, 18_000, 14_000))
)
LOST = ({"account_name": "Lost Co", "industry": "Chemicals", "close_reason": "price"},)

#: The account's own search history. 4% CTR, 3% CVR — the rates every forecast
#: row in this file is rated at, and the reason none of them is a planning
#: default.
CAMPAIGN_PERF = [
    {
        "campaign": "Brand",
        "channel": "SEARCH",
        "month": "2026-08",
        "impressions": 400_000,
        "clicks": 16_000,
        "conversions": 480,
        "cost": 96_000.0,
    }
]
IMPRESSION_SHARE = [
    {"keyword": "sds management software", "impression_share": 0.35, "impressions": 200_000}
]


def answers() -> dict[str, Any]:
    """One scripted answer per output model across 2.1 and 2.2. No figures."""
    return {
        "TaxonomyDraft": {
            "actions": [
                {
                    "name": "Qualified lead",
                    "ads_action_id": None,
                    "category": "qualified_lead",
                    "counting": "one_per_click",
                    "value_model": "fixed",
                    "value_basis": "target_cpl",
                    "primary": True,
                    "include_in_conversions": True,
                    "rationale": "Sales works every one of these.",
                    "evidence_ids": [],
                }
            ],
            "deprecate": [],
            "ranking": ["Qualified lead"],
        },
        "MethodNotes": {
            "method_notes": "Gross profit over the CAC ratio, times the observed close rate.",
            "caveats": ["One industry carries the whole book."],
        },
        "CampaignTargetsDraft": {
            "objectives": [
                {
                    "campaign_ref": "nonbrand-gb-lead-gen",
                    "objective": "lead_gen",
                    "primary_kpi": "cpl",
                    "segment_ref": "Chemicals",
                    "basis": "Every closed-won deal is a chemicals account.",
                    "ramp": [{"month": 1, "phase": "learning"}, {"month": 2, "phase": "steady"}],
                    "confidence": "medium",
                    "evidence_ids": [],
                },
                {
                    "campaign_ref": "nonbrand-de-lead-gen",
                    "objective": "lead_gen",
                    "primary_kpi": "cpl",
                    "segment_ref": "Chemicals",
                    "basis": "The German demand is the same buyer.",
                    "ramp": [{"month": 1, "phase": "learning"}],
                    "confidence": "low",
                    "evidence_ids": [],
                },
            ],
            "north_star": {
                "metric": "cpl",
                "period": "monthly",
                "segment_ref": None,
                "rationale": "One number for the account.",
            },
        },
        "LeadDefinitionDraft": {
            "qualified_lead": {
                "required_signals": ["a compliance obligation"],
                "disqualifiers": ["sole trader"],
                "scoring": [
                    {"signal": "a compliance obligation", "weight": 5, "source_field": "industry"}
                ],
                "threshold": 5,
            },
            "sla_response_hours": 4,
            "routing": [{"segment": "Chemicals", "owner": "EMEA desk"}],
            "observed_rejection_reasons": ["price"],
            "notes": "Sales rejects sole traders on sight.",
        },
        # -- 2.2 ------------------------------------------------------------
        "ForecastNotes": {
            "method_notes": "Search volume at the impression-share target, rated on our own CTR.",
            "caveats": ["A forecast is not a promise."],
        },
        "CapacityDraft": {
            "assignments": [],  # filled per test by `assign_every_cluster`
            "notes": "Every cluster lands in the campaign for its market.",
        },
        "ScenariosDraft": {
            "narratives": [
                {
                    "name": name,
                    "case_for": f"the case for {name}",
                    "case_against": f"the case against {name}",
                }
                for name in ("cautious", "expected", "aggressive")
            ],
            "notes": "All three share a CPA under linear scaling.",
        },
        "ScenarioChoice": {
            "chosen_scenario": "expected",
            "rationale": "The forecast CPA is inside the target.",
            "what_would_change_it": "A measured CPC above the research's range.",
        },
        "RulesDraft": {
            "rules": [
                {
                    "id": "R1",
                    "trigger_metric": "cpa",
                    "comparison": "above",
                    "threshold_basis": "forecast_cpa",
                    "from_campaign": "nonbrand-gb-lead-gen",
                    "to_campaign": "nonbrand-de-lead-gen",
                    "shift_size": "standard",
                    "requires_human": False,
                    "rationale": "Stop paying over the forecast for the same lead.",
                }
            ],
            "review_cadence": "monthly",
            "notes": "One rule while the account is small.",
        },
    }


async def seed_account(project_id: uuid.UUID) -> None:
    """The `campaign_perf` and `keyword_impression_share` rows 2.2.1 rates on.

    Written straight to the store rather than pulled: this suite is about what
    the plan does with account history, and `tests/test_google_ads_forecast.py`
    proves the pull itself.
    """
    from agent.db.models import Project
    from agent.db.session import get_sessionmaker
    from agent.evidence.normalize import EvidenceDraft
    from agent.evidence.store import EvidenceStore

    drafts = [
        EvidenceDraft(source="google_ads", kind=demand.CAMPAIGN_PERF, payload=dict(row))
        for row in CAMPAIGN_PERF
    ] + [
        EvidenceDraft(source="google_ads", kind=demand.KEYWORD_IMPRESSION_SHARE, payload=dict(row))
        for row in IMPRESSION_SHARE
    ]
    async with get_sessionmaker()() as session:
        project = await session.get(Project, project_id)
        assert project is not None
        await EvidenceStore(session, project.workspace_id).write(
            drafts, project_id=project_id, run_id=None, embed=False
        )
        await session.commit()


@pytest.fixture
def fake_openrouter() -> FakeOpenRouter:
    return FakeOpenRouter()


@pytest_asyncio.fixture
async def accepted(
    admin: ApiClient, db: AsyncSession, project: Any, workspace_id: uuid.UUID
) -> dict[str, Any]:
    """A finished research run, accepted, with the CRM and account history."""
    from agent.export.contract import ResearchReport

    me = (await admin.get("/auth/me")).json()
    user_id = uuid.UUID(me["id"])

    run = Run(
        workspace_id=workspace_id,
        project_id=project.id,
        triggered_by=user_id,
        trigger=RunTrigger.MANUAL,
        status=RunStatus.SUCCEEDED,
        stage=RunStage.RESEARCH,
    )
    db.add(run)
    await db.flush()

    payload = golden_payload()
    payload["project_id"] = str(project.id)
    payload["run_id"] = str(run.id)
    payload["launch_readiness"] = "go"
    parsed = ResearchReport.model_validate(payload)
    db.add(
        Report(
            run_id=run.id,
            schema_version="1.0",
            payload=json.loads(parsed.model_dump_json()),
            markdown="# report",
        )
    )
    await db.commit()

    await seed_crm(project.id, won=WON, lost=LOST)
    await seed_account(project.id)
    response = await admin.post(f"/runs/{run.id}/accept", json={})
    assert response.status_code == 201, response.text
    return {
        "project_id": project.id,
        "research_run_id": run.id,
        "user_id": user_id,
        "workspace_id": workspace_id,
    }


def assign_every_cluster(script: dict[str, Any], clusters: list[dict[str, str]]) -> None:
    """Point every cluster at the campaign for its market.

    Built from what 2.2.1 actually found rather than hard-coded: the golden
    report's clusters come from its own keyword-to-page map, and a fixed list
    here would silently stop covering them the day that map changes.
    """
    script["CapacityDraft"] = {
        **script["CapacityDraft"],
        "assignments": [
            {
                "cluster": row["cluster"],
                "market": row["market"],
                "campaign_ref": (
                    "nonbrand-de-lead-gen" if row["market"] == "DE" else "nonbrand-gb-lead-gen"
                ),
                "rationale": "the campaign for this market",
            }
            for row in clusters
        ],
    }


async def run_to_g1(
    admin: ApiClient, accepted: dict[str, Any], fake: FakeOpenRouter
) -> tuple[uuid.UUID, dict[str, Any]]:
    """Start the plan and execute until G1 and G2 halt it."""
    from tests.integration.runs_support import by_output_model

    script = answers()
    by_output_model(fake, script)
    response = await admin.post(f"/projects/{accepted['project_id']}/plan/runs")
    assert response.status_code == 202, response.text
    plan_run_id = uuid.UUID(response.json()["run_id"])
    result = await execute(plan_run_id, fake)
    assert result.error is None, result.error
    assert result.status is RunStatus.AWAITING_APPROVAL
    return plan_run_id, script


async def run_to_g3(
    admin: ApiClient, accepted: dict[str, Any], fake: FakeOpenRouter
) -> tuple[uuid.UUID, dict[str, Any]]:
    """Approve G1 and run the budget branch until G3 halts it.

    The shape of the phase: 2.2.2 depends on the gate, so there is no budget to
    decide until a marketing lead has decided the targets.
    """
    plan_run_id, script = await run_to_g1(admin, accepted, fake)

    forecast = (await admin.get(f"/runs/{plan_run_id}")).json()
    node = next(item for item in forecast["nodes"] if item["id"] == "2.2.1")
    assert node["status"] == NodeRunStatus.SUCCEEDED, "2.2.1 runs before the gate, off 2.1.1"

    output = (await admin.get(f"/runs/{plan_run_id}/nodes/2.2.1")).json()["output"]
    assign_every_cluster(
        script,
        [
            {"cluster": cluster, "market": market}
            for cluster, market in sorted(
                {(row["cluster"], row["market"]) for row in output["forecast"]}
            )
        ],
    )
    from tests.integration.runs_support import by_output_model

    by_output_model(fake, script)

    inbox = (await admin.get(f"/approvals?run_id={plan_run_id}")).json()["items"]
    g1 = next(item for item in inbox if item["gate_key"] == "G1")
    decided = await admin.post(f"/approvals/{g1['id']}", json={"decision": "approve"})
    assert decided.status_code == 200, decided.text

    result = await execute(plan_run_id, fake)
    assert result.error is None, result.error
    return plan_run_id, script


async def gate_g3(admin: ApiClient, plan_run_id: uuid.UUID) -> dict[str, Any]:
    inbox = (await admin.get(f"/approvals?run_id={plan_run_id}")).json()["items"]
    pending = [item for item in inbox if item["gate_key"] == "G3"]
    assert pending, f"no G3 gate in {[item['gate_key'] for item in inbox]}"
    return pending[0]


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
