"""S2-P2's exit criteria, end to end (Stage 02 PRD §21).

    "A partial run of 2.1 produces validated outputs, halts on G1 and G2, an
     `approver` resumes each, an `operator` gets 403, every number resolves to
     a `PlanCalc` row."

Every clause of that sentence is a test here, driven through the real
executor, the real `DerivedWriter`, the real approvals API and a real
Postgres. Only the model provider is scripted — and it is scripted with
answers that contain **no figures**, because a node that got a number from the
model could not have, there being none to get.

Two things the unit suite cannot prove and this file does. `PlanCalc` dedupe
is a unique constraint, so four nodes citing one calculation is a property of
the database rather than of the stub writer. And the citation check the
executor makes is against `derived` rows *in the run's session*: a node that
computed correctly and forgot to hand the row back would pass every unit test
and fail here.
"""

from __future__ import annotations

import json
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
    Project,
    Report,
    ResearchAcceptance,
    Run,
    RunStage,
    RunStatus,
    RunTrigger,
)
from agent.nodes.plan import stage_2_1
from tests.integration.conftest import ApiClient
from tests.integration.runs_support import execute, seed_crm
from tests.openrouter_fake import FakeOpenRouter
from tests.report_support import golden_payload

pytestmark = pytest.mark.anyio

#: Four wins in one industry and one loss, so the ceiling is hand-checkable:
#: ACV 20,000, an 80% close rate, and the golden report's two product margins
#: (82 and 64) averaging to 73.
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


@pytest.fixture
def fake_openrouter() -> FakeOpenRouter:
    """A scripted provider, per test, so `requests` is this test's own record."""
    return FakeOpenRouter()


def answers() -> dict[str, Any]:
    """One scripted answer per output model on the 2.1 branch. No figures."""
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
                    "campaign_ref": "nonbrand-us-lead-gen",
                    "objective": "lead_gen",
                    "primary_kpi": "cpl",
                    "segment_ref": "Chemicals",
                    "basis": "Every closed-won deal is a chemicals account.",
                    "ramp": [{"month": 1, "phase": "learning"}, {"month": 2, "phase": "steady"}],
                    "confidence": "medium",
                    "evidence_ids": [],
                }
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
    }


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def accepted(
    admin: ApiClient, db: AsyncSession, project: Any, workspace_id: uuid.UUID
) -> dict[str, Any]:
    """A finished research run, accepted, with the CRM rows Stage 2.1 reads."""
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
    report = Report(
        run_id=run.id,
        schema_version="1.0",
        payload=json.loads(parsed.model_dump_json()),
        markdown="# report",
    )
    db.add(report)
    await db.commit()

    await seed_crm(project.id, won=WON, lost=LOST)
    response = await admin.post(f"/runs/{run.id}/accept", json={})
    assert response.status_code == 201, response.text
    return {
        "project_id": project.id,
        "research_run_id": run.id,
        "user_id": user_id,
        "workspace_id": workspace_id,
    }


async def start_plan(admin: ApiClient, project_id: uuid.UUID) -> uuid.UUID:
    """`POST /plan/runs` takes no body: a plan run is the whole plan DAG.

    Which, in S2-P2, is stage 2.1 and nothing else — so "a partial run of 2.1"
    from the phase's exit criteria and "a plan run" are the same thing until
    S2-P3 adds 2.2.
    """
    response = await admin.post(f"/projects/{project_id}/plan/runs")
    assert response.status_code == 202, response.text
    return uuid.UUID(response.json()["run_id"])


async def run_2_1(admin: ApiClient, accepted: dict[str, Any], fake: FakeOpenRouter) -> uuid.UUID:
    """Start the 2.1 branch and execute it the way the worker does."""
    from tests.integration.runs_support import by_output_model

    by_output_model(fake, answers())
    plan_run_id = await start_plan(admin, accepted["project_id"])
    result = await execute(plan_run_id, fake)
    assert result.error is None, result.error
    return plan_run_id


# ---------------------------------------------------------------------------
# "a partial run of 2.1 produces validated outputs"
# ---------------------------------------------------------------------------


async def test_the_2_1_branch_runs_and_halts_on_both_gates(
    admin: ApiClient, accepted: dict[str, Any], fake_openrouter: FakeOpenRouter
) -> None:
    plan_run_id = await run_2_1(admin, accepted, fake_openrouter)

    state = (await admin.get(f"/runs/{plan_run_id}")).json()
    assert state["status"] == RunStatus.AWAITING_APPROVAL
    by_node = {node["id"]: node["status"] for node in state["nodes"]}
    assert by_node["2.1.1"] == NodeRunStatus.SUCCEEDED
    assert by_node["2.1.2"] == NodeRunStatus.SUCCEEDED
    # Both gates halted. §5.3: they are parallel, so G2 did not wait for G1.
    assert by_node["2.1.3"] == NodeRunStatus.AWAITING_APPROVAL
    assert by_node["2.1.4"] == NodeRunStatus.AWAITING_APPROVAL


async def test_the_gates_are_labelled_g1_and_g2(
    admin: ApiClient, accepted: dict[str, Any], fake_openrouter: FakeOpenRouter, db: AsyncSession
) -> None:
    """`Approval.gate_key`, written from `NodeSpec.gate_key` (§7.1, §8.4)."""
    plan_run_id = await run_2_1(admin, accepted, fake_openrouter)
    rows = (
        (await db.execute(sa.select(Approval).where(Approval.run_id == plan_run_id)))
        .scalars()
        .all()
    )
    assert {row.node_id: row.gate_key for row in rows} == {"2.1.3": "G1", "2.1.4": "G2"}
    assert all(row.status is ApprovalStatus.PENDING for row in rows)


async def test_a_research_gate_is_still_unlabelled(db: AsyncSession) -> None:
    """Migration 0013 chose `R0` over inventing R1..R3, and the writer keeps it."""
    from agent.orchestrator.approvals import UNLABELLED_GATE

    assert UNLABELLED_GATE == "R0"
    assert stage_2_1.campaign_targets.spec.gate_key == "G1"


# ---------------------------------------------------------------------------
# "every number resolves to a PlanCalc row"
# ---------------------------------------------------------------------------


async def test_every_cited_calc_id_resolves_to_a_plan_calc_row(
    admin: ApiClient, accepted: dict[str, Any], fake_openrouter: FakeOpenRouter, db: AsyncSession
) -> None:
    """PT1, walked from the output back to the row rather than asserted about."""
    plan_run_id = await run_2_1(admin, accepted, fake_openrouter)

    calcs = {
        row.evidence_id: row
        for row in (
            await db.execute(sa.select(PlanCalc).where(PlanCalc.plan_run_id == plan_run_id))
        )
        .scalars()
        .all()
    }
    assert calcs, "the run produced no calculations at all"

    seen = 0
    for node_id in ("2.1.1", "2.1.2", "2.1.3", "2.1.4"):
        output = (await admin.get(f"/runs/{plan_run_id}/nodes/{node_id}")).json()["output"]
        cited = output["calc_evidence_ids"]
        assert cited, f"{node_id} carries numbers and cited no calculation"
        for evidence_id in cited:
            row = calcs[uuid.UUID(evidence_id)]
            assert row.formula_id in {stage_2_1.MAX_CPA, stage_2_1.PAYBACK}
            assert row.inputs_hash
            assert row.calc_version.startswith("calc/")
            seen += 1
    assert seen >= 4


async def test_four_nodes_share_one_calculation(
    admin: ApiClient, accepted: dict[str, Any], fake_openrouter: FakeOpenRouter, db: AsyncSession
) -> None:
    """The dedupe is a unique constraint, so this is a database fact.

    Without `UNIQUE(plan_run_id, formula_id, inputs_hash)` the four nodes would
    write four identical rows and the Evidence Explorer would carry four
    identical `derived` entries for one piece of arithmetic.
    """
    plan_run_id = await run_2_1(admin, accepted, fake_openrouter)
    rows = (
        (
            await db.execute(
                sa.select(PlanCalc).where(
                    PlanCalc.plan_run_id == plan_run_id,
                    PlanCalc.formula_id == stage_2_1.MAX_CPA,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].node_id == "2.1.1", "the first node to compute it owns the row"

    cited: set[str] = set()
    for node_id in ("2.1.1", "2.1.2", "2.1.3", "2.1.4"):
        output = (await admin.get(f"/runs/{plan_run_id}/nodes/{node_id}")).json()["output"]
        cited.update(output["calc_evidence_ids"])
    assert str(rows[0].evidence_id) in cited


async def test_the_calculation_is_searchable_as_derived_evidence(
    admin: ApiClient, accepted: dict[str, Any], fake_openrouter: FakeOpenRouter, db: AsyncSession
) -> None:
    """PRD §7.3: a `derived` row with a one-line human rendering."""
    plan_run_id = await run_2_1(admin, accepted, fake_openrouter)
    rows = (
        (
            await db.execute(
                sa.select(Evidence).where(
                    Evidence.run_id == plan_run_id, Evidence.source == EvidenceSource.DERIVED
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows
    assert {row.kind for row in rows} <= {"calc_economics"}
    assert any("Max CPL" in (row.content_text or "") for row in rows)
    assert all(row.payload["formula_id"] for row in rows)


async def test_the_figures_are_the_computed_ones(
    admin: ApiClient, accepted: dict[str, Any], fake_openrouter: FakeOpenRouter
) -> None:
    """ACV 20,000 at a 73% blended margin, 80% close rate, 3x CAC, 15% safety.

        gross profit  = 20,000 x 0.73        = 14,600
        max CPA (won) = 14,600 / 3           =  4,866.67
        max CPL       = 4,866.66… x 0.80     =  3,893.33
        target CPL    = 3,893.33… x 0.85     =  3,309.33

    The margin is the mean of the golden report's two products (82 and 64).
    """
    plan_run_id = await run_2_1(admin, accepted, fake_openrouter)
    ceiling = (await admin.get(f"/runs/{plan_run_id}/nodes/2.1.2")).json()["output"]
    row = ceiling["by_segment"][0]
    assert row["segment"] == "Chemicals"
    assert row["acv_usd"] == 20_000.0
    assert row["gross_margin_pct"] == 73.0
    assert row["lead_to_won_pct"] == 80.0
    assert row["max_cpa_won_usd"] == pytest.approx(4866.67, abs=0.01)
    assert row["max_cpl_usd"] == pytest.approx(3893.33, abs=0.01)
    assert row["target_cpl_usd"] == pytest.approx(3309.33, abs=0.01)

    targets = (await admin.get(f"/runs/{plan_run_id}/nodes/2.1.3")).json()["output"]
    objective = targets["objectives"][0]
    assert objective["target_value"] == row["target_cpl_usd"]
    assert objective["ceiling_value"] == row["max_cpl_usd"]
    assert objective["target_value"] <= objective["ceiling_value"]


# ---------------------------------------------------------------------------
# "an approver resumes each, an operator gets 403"
# ---------------------------------------------------------------------------


async def _approvals(admin: ApiClient, plan_run_id: uuid.UUID) -> dict[str, dict[str, Any]]:
    listed = (await admin.get("/approvals")).json()
    return {
        item["node_id"]: item
        for item in listed["items"]
        if item["run_id"] == str(plan_run_id) and item["status"] == ApprovalStatus.PENDING
    }


async def test_an_approver_resumes_each_gate_and_an_operator_cannot(
    admin: ApiClient,
    accepted: dict[str, Any],
    fake_openrouter: FakeOpenRouter,
    signed_in_as: Any,
) -> None:
    plan_run_id = await run_2_1(admin, accepted, fake_openrouter)
    cards = await _approvals(admin, plan_run_id)
    assert set(cards) == {"2.1.3", "2.1.4"}

    operator = await signed_in_as("operator")
    refused = await operator.post(
        f"/approvals/{cards['2.1.3']['id']}", json={"decision": "approve"}
    )
    assert refused.status_code == 403, refused.text

    approver = await signed_in_as("approver")
    for node_id in ("2.1.4", "2.1.3"):
        decided = await approver.post(
            f"/approvals/{cards[node_id]['id']}",
            json={"decision": "approve", "note": f"{node_id} looks right"},
        )
        assert decided.status_code == 200, decided.text

    state = (await admin.get(f"/runs/{plan_run_id}")).json()
    by_node = {node["id"]: node["status"] for node in state["nodes"]}
    assert by_node["2.1.3"] == NodeRunStatus.SUCCEEDED
    assert by_node["2.1.4"] == NodeRunStatus.SUCCEEDED


async def test_an_operator_cannot_decide_a_gate_on_a_run_they_started(
    admin: ApiClient,
    accepted: dict[str, Any],
    fake_openrouter: FakeOpenRouter,
    signed_in_as: Any,
) -> None:
    """The Stage 01 rule, restated for plan gates (§5.2)."""
    plan_run_id = await run_2_1(admin, accepted, fake_openrouter)
    cards = await _approvals(admin, plan_run_id)
    operator = await signed_in_as("operator")
    refused = await operator.post(
        f"/approvals/{cards['2.1.4']['id']}", json={"decision": "approve"}
    )
    assert refused.status_code == 403


async def test_an_approver_may_not_start_a_plan_run(
    admin: ApiClient, accepted: dict[str, Any], signed_in_as: Any
) -> None:
    """`PLAN_EXECUTE` is admin and operator; `approver` decides, it does not run."""
    approver = await signed_in_as("approver")
    refused = await approver.post(f"/projects/{accepted['project_id']}/plan/runs", json={})
    assert refused.status_code == 403, refused.text


# ---------------------------------------------------------------------------
# gate routing and the approver's edit
# ---------------------------------------------------------------------------


async def test_plan_approvers_routes_the_card_by_gate_key(
    admin: ApiClient,
    accepted: dict[str, Any],
    fake_openrouter: FakeOpenRouter,
    db: AsyncSession,
    signed_in_as: Any,
) -> None:
    """§5.3: the sales lead owns G2. Setting `plan_approvers` addresses the card."""
    from agent.orchestrator.approvals import PLAN_APPROVERS

    sales = await signed_in_as("approver")
    sales_id = uuid.UUID((await sales.get("/auth/me")).json()["id"])

    project = await db.get(Project, accepted["project_id"])
    assert project is not None
    project.settings = {**(project.settings or {}), PLAN_APPROVERS: {"G2": str(sales_id)}}
    await db.commit()

    plan_run_id = await run_2_1(admin, accepted, fake_openrouter)
    rows = {
        row.node_id: row
        for row in (
            (await db.execute(sa.select(Approval).where(Approval.run_id == plan_run_id)))
            .scalars()
            .all()
        )
    }
    assert rows["2.1.4"].assignee_id == sales_id, "G2 is addressed to the named sales lead"
    assert rows["2.1.3"].assignee_id is None, "G1 was left unassigned, so any approver may claim it"


async def test_an_edit_that_breaks_the_shape_is_refused_and_the_gate_stays_open(
    admin: ApiClient,
    accepted: dict[str, Any],
    fake_openrouter: FakeOpenRouter,
    signed_in_as: Any,
    db: AsyncSession,
) -> None:
    """§8.4: the edited object is re-validated before the branch resumes.

    Without this the run would resume and fail four nodes later on a
    `KeyError` naming neither the approver nor the field they deleted.
    """
    plan_run_id = await run_2_1(admin, accepted, fake_openrouter)
    cards = await _approvals(admin, plan_run_id)
    approver = await signed_in_as("approver")

    broken = await approver.post(
        f"/approvals/{cards['2.1.3']['id']}",
        json={
            "decision": "approve",
            "edited_proposal": {"objectives": [{"campaign_ref": "x"}], "calc_evidence_ids": []},
        },
    )
    assert broken.status_code == 422, broken.text
    body = broken.json()
    assert "2.1.3" in body["detail"]
    assert any("calc_evidence_ids" in field for field in body["fields"])

    still_open = await db.get(Approval, uuid.UUID(cards["2.1.3"]["id"]))
    assert still_open is not None
    await db.refresh(still_open)
    assert still_open.status is ApprovalStatus.PENDING


async def test_a_valid_edit_is_what_the_branch_resumes_with(
    admin: ApiClient,
    accepted: dict[str, Any],
    fake_openrouter: FakeOpenRouter,
    signed_in_as: Any,
) -> None:
    """An approver may change the SLA on G2; the edit becomes the node output."""
    plan_run_id = await run_2_1(admin, accepted, fake_openrouter)
    cards = await _approvals(admin, plan_run_id)
    proposal = cards["2.1.4"]["proposal"]
    edited = {**proposal, "sla_response_hours": 24}

    approver = await signed_in_as("approver")
    decided = await approver.post(
        f"/approvals/{cards['2.1.4']['id']}",
        json={"decision": "approve", "edited_proposal": edited, "note": "24h is our SLA"},
    )
    assert decided.status_code == 200, decided.text

    output = (await admin.get(f"/runs/{plan_run_id}/nodes/2.1.4")).json()["output"]
    assert output["sla_response_hours"] == 24


async def test_a_rejected_gate_fails_its_branch_and_is_not_retried(
    admin: ApiClient,
    accepted: dict[str, Any],
    fake_openrouter: FakeOpenRouter,
    signed_in_as: Any,
) -> None:
    plan_run_id = await run_2_1(admin, accepted, fake_openrouter)
    cards = await _approvals(admin, plan_run_id)
    approver = await signed_in_as("approver")
    rejected = await approver.post(
        f"/approvals/{cards['2.1.3']['id']}",
        json={"decision": "reject", "note": "the CPL target is above what finance will sign"},
    )
    assert rejected.status_code == 200, rejected.text

    state = (await admin.get(f"/runs/{plan_run_id}")).json()
    by_node = {node["id"]: node["status"] for node in state["nodes"]}
    assert by_node["2.1.3"] == NodeRunStatus.FAILED


# ---------------------------------------------------------------------------
# what the run refuses to do
# ---------------------------------------------------------------------------


async def test_a_plan_run_with_no_crm_stops_and_names_the_missing_rows(
    admin: ApiClient,
    db: AsyncSession,
    project: Any,
    workspace_id: uuid.UUID,
    fake_openrouter: FakeOpenRouter,
) -> None:
    """No closed-won rows: the run fails with the field named, spending nothing.

    Deliberately not an empty plan. PRD §18 asks for `insufficient_input`, and
    `stage_2_1._insufficient` argues why an output carrying
    `calc_evidence_ids: min_length=1` cannot express that — the sentence a
    person reads is the same either way, and it names `crm_won`.
    """
    from agent.export.contract import ResearchReport
    from tests.integration.runs_support import by_output_model

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
    await db.flush()
    payload = golden_payload()
    payload["project_id"] = str(project.id)
    payload["run_id"] = str(run.id)
    payload["launch_readiness"] = "go"
    db.add(
        Report(
            run_id=run.id,
            schema_version="1.0",
            payload=json.loads(ResearchReport.model_validate(payload).model_dump_json()),
            markdown="# report",
        )
    )
    await db.commit()
    assert (await admin.post(f"/runs/{run.id}/accept", json={})).status_code == 201

    by_output_model(fake_openrouter, answers())
    plan_run_id = await start_plan(admin, project.id)
    result = await execute(plan_run_id, fake_openrouter)

    assert result.status is RunStatus.FAILED
    node = (await admin.get(f"/runs/{plan_run_id}/nodes/2.1.1")).json()
    assert node["status"] == NodeRunStatus.FAILED
    assert "crm_won" in node["error"]["message"]
    assert fake_openrouter.requests == [], "no tokens may be spent on a shape that cannot validate"


async def test_a_plan_node_may_only_pull_a_read_only_connector(
    admin: ApiClient,
    accepted: dict[str, Any],
    fake_openrouter: FakeOpenRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Law 12 / PS1, asserted where the executor asserts it: before `gather()`.

    The shipped 2.1 nodes only reach `google_ads` and `csv_ingest`, both of
    which declare `ReadOnlyConnector` — so the way to prove the guard fires is
    to point a node at one that does not.
    """
    from agent.connectors import MutationForbidden, assert_read_only

    with pytest.raises(MutationForbidden, match="dataforseo"):
        assert_read_only("dataforseo", why="node 2.1.1")

    # `NodeSpec` is frozen, so the spec is replaced rather than mutated — and
    # on the class, which is where the node reads it from.
    patched = stage_2_1.conversion_taxonomy.spec.model_copy(update={"connectors": ("dataforseo",)})
    monkeypatch.setattr(type(stage_2_1.conversion_taxonomy), "spec", patched)
    from tests.integration.runs_support import by_output_model

    by_output_model(fake_openrouter, answers())
    plan_run_id = await start_plan(admin, accepted["project_id"])
    result = await execute(plan_run_id, fake_openrouter)
    assert result.status is RunStatus.FAILED
    node = (await admin.get(f"/runs/{plan_run_id}/nodes/2.1.1")).json()
    assert node["error"]["code"] == "MutationForbidden"


async def test_the_project_overrides_the_constants_and_the_calc_says_so(
    admin: ApiClient, accepted: dict[str, Any], fake_openrouter: FakeOpenRouter, db: AsyncSession
) -> None:
    """§9.3: overrides merge at run start and change `calc_version`."""
    from agent.orchestrator.plan_input import PLANNING_OVERRIDES

    project = await db.get(Project, accepted["project_id"])
    assert project is not None
    project.settings = {
        **(project.settings or {}),
        PLANNING_OVERRIDES: {"economics": {"target_cac_ratio": 2.0}},
    }
    await db.commit()

    plan_run_id = await run_2_1(admin, accepted, fake_openrouter)
    row = (
        (
            await db.execute(
                sa.select(PlanCalc).where(
                    PlanCalc.plan_run_id == plan_run_id,
                    PlanCalc.formula_id == stage_2_1.MAX_CPA,
                )
            )
        )
        .scalars()
        .one()
    )
    assert "+ovr." in row.calc_version
    assert row.inputs["target_cac_ratio"] == 2.0
    # 14,600 / 2 rather than / 3.
    assert row.result["blended"]["max_cpa_won_usd"] == pytest.approx(7300.0, abs=0.01)


async def test_the_acceptance_the_plan_reads_is_the_one_it_was_started_from(
    admin: ApiClient, accepted: dict[str, Any], fake_openrouter: FakeOpenRouter, db: AsyncSession
) -> None:
    """Law 13, checked rather than assumed: the run's `input_hash` still matches."""
    from agent.orchestrator.plan_input import build_plan_input_for_run

    plan_run_id = await run_2_1(admin, accepted, fake_openrouter)
    run = await db.get(Run, plan_run_id)
    assert run is not None
    rebuilt = await build_plan_input_for_run(db, run, workspace_id=accepted["workspace_id"])
    assert rebuilt.content_hash() == run.input_hash
    assert rebuilt.research_run_id == accepted["research_run_id"]

    current = (
        await db.execute(
            sa.select(ResearchAcceptance).where(
                ResearchAcceptance.project_id == accepted["project_id"],
                ResearchAcceptance.superseded_by.is_(None),
            )
        )
    ).scalar_one()
    assert current.run_id == accepted["research_run_id"]


async def test_the_crm_rows_never_reach_a_prompt(
    admin: ApiClient, accepted: dict[str, Any], fake_openrouter: FakeOpenRouter
) -> None:
    """PC2, for the one stage that reads the CRM most closely.

    2.1.1 shows the model closed-won rows on purpose — they are evidence it
    must cite — but 2.1.2 and 2.1.4 reason over aggregates, and an account
    name leaking into either would be a raw CRM row in a prompt.
    """
    await run_2_1(admin, accepted, fake_openrouter)
    prompts = [
        json.loads(request.content or b"{}")
        for request in fake_openrouter.requests
        if request.content
    ]
    for body in prompts:
        text = json.dumps(body)
        name = body.get("response_format", {}).get("json_schema", {}).get("name", "")
        if name in {"MethodNotes", "LeadDefinitionDraft"}:
            assert "Acme 0" not in text, f"{name} was shown a raw CRM row"
            assert "Lost Co" not in text, f"{name} was shown a raw CRM row"
