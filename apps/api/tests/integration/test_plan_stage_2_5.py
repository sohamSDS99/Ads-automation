"""S2-P5a end to end: stage 2.5 inside a real plan run.

Four things the unit suite structurally cannot prove, and this file does.

1. **The measurement branch does not wait for a gate.** PRD §11 puts 2.5.1 on
   2.1.1 and 2.5.2 on 2.1.1 and 2.1.4, and §5.3 says the 2.5 branch runs in
   parallel with the critical path so a slow approver never holds it up. That
   is a claim about the executor's wavefront, not about a node.
2. **The citation check is made against `derived` rows in the run's own
   session.** A node that computed correctly and forgot to return the row from
   `gather()` passes every unit test and fails here.
3. **`PlanCalc` is really written**, with the formula id, the inputs hash and
   the constants version a reader needs to re-derive a tolerance.
4. **PRD §13 prompt hygiene.** 2.5.2 reads the CRM to date the backfill. No
   account name from those rows may reach a prompt, and the only honest way to
   check that is to look at what actually went over the wire.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import (
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
from agent.nodes.plan import stage_2_5
from agent.planning.constants import load_planning_constants
from tests.integration.conftest import ApiClient
from tests.integration.plan_answers import every_plan_answer
from tests.integration.runs_support import by_output_model, execute, seed_crm
from tests.openrouter_fake import FakeOpenRouter
from tests.report_support import golden_payload

pytestmark = pytest.mark.anyio

#: How far back the seeded CRM reaches. Dated **relative to now** on purpose:
#: `tracking.history_days` measures the age of the oldest row against the
#: clock, so a literal date would make `backfill_days` grow by one every day
#: and this suite would go red on a Tuesday for no reason anybody could see.
HISTORY_DAYS = 45.0


def _days_ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).date().isoformat()


#: The account names here are the canary for §13: they are in the CRM rows
#: 2.5.2 gathers, and they must not appear in anything sent to a model.
WON = (
    {
        "account_name": "Brightwater Chemicals",
        "industry": "Chemicals",
        "deal_value": 22_000,
        "created_at": _days_ago(int(HISTORY_DAYS)),
    },
    {
        "account_name": "Keld Industrial",
        "industry": "Chemicals",
        "deal_value": 18_000,
        "created_at": _days_ago(20),
    },
)
LOST = (
    {
        "account_name": "Pellmore Coatings",
        "industry": "Chemicals",
        "close_reason": "price",
        "created_at": _days_ago(10),
    },
)

CRM_NAMES = tuple(row["account_name"] for row in (*WON, *LOST))

STAGE_2_5_NODES = ("2.5.1", "2.5.2")


@pytest.fixture
def fake_openrouter() -> FakeOpenRouter:
    return FakeOpenRouter()


@pytest_asyncio.fixture
async def accepted(
    admin: ApiClient, db: AsyncSession, project: Any, workspace_id: uuid.UUID
) -> dict[str, Any]:
    """A finished research run, accepted, with the CRM rows 2.5.2 dates from.

    The golden report's 1.5.3 output is what makes this suite worth running:
    two usable audience lists carrying a lawful basis for GB and DE, and one
    refusal with a blocker. 2.5.2's consent block is computed from exactly
    those rows, so the assertions below are against research, not a fixture
    invented for the test.
    """
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
    report = Report(
        run_id=run.id,
        schema_version="1.0",
        payload=json.loads(ResearchReport.model_validate(payload).model_dump_json()),
        markdown="# report",
    )
    db.add(report)
    await db.commit()

    await seed_crm(project.id, won=WON, lost=LOST)
    response = await admin.post(f"/runs/{run.id}/accept", json={})
    assert response.status_code == 201, response.text
    return {"project_id": project.id, "research_run_id": run.id, "user_id": user_id}


async def run_plan(admin: ApiClient, accepted: dict[str, Any], fake: FakeOpenRouter) -> uuid.UUID:
    """Start the whole plan DAG and execute it until it parks on the gates."""
    by_output_model(fake, every_plan_answer())
    response = await admin.post(f"/projects/{accepted['project_id']}/plan/runs")
    assert response.status_code == 202, response.text
    plan_run_id = uuid.UUID(response.json()["run_id"])
    result = await execute(plan_run_id, fake)
    assert result.error is None, result.error
    return plan_run_id


async def run_past_g2(
    admin: ApiClient, accepted: dict[str, Any], fake: FakeOpenRouter, signed_in_as: Any
) -> uuid.UUID:
    """Run, approve G1 and G2, and drive the resumed run — which is 2.5.2.

    2.5.2 sits under gate G2 (§11: `2.5.2<-{2.1.1,2.1.4}`), so there is no
    state of the world in which it has run and G2 has not been decided. See
    `test_2_5_1_clears_the_halt_and_2_5_2_does_not`.
    """
    plan_run_id = await run_plan(admin, accepted, fake)
    listed = (await admin.get("/approvals")).json()["items"]
    cards = {
        item["node_id"]: item["id"]
        for item in listed
        if item["run_id"] == str(plan_run_id) and item["status"] == "pending"
    }
    assert set(cards) == {"2.1.3", "2.1.4"}, cards

    approver = await signed_in_as("approver")
    for node_id in ("2.1.4", "2.1.3"):
        decided = await approver.post(f"/approvals/{cards[node_id]}", json={"decision": "approve"})
        assert decided.status_code == 200, decided.text

    result = await execute(plan_run_id, fake)
    assert result.error is None, result.error
    return plan_run_id


async def output_of(admin: ApiClient, plan_run_id: uuid.UUID, node_id: str) -> dict[str, Any]:
    response = await admin.get(f"/runs/{plan_run_id}/nodes/{node_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["output"] is not None, f"{node_id} produced no output"
    return dict(body["output"])


# ---------------------------------------------------------------------------
# the branch runs, and it does not wait
# ---------------------------------------------------------------------------


async def test_2_5_1_clears_the_halt_and_2_5_2_does_not(
    admin: ApiClient, accepted: dict[str, Any], fake_openrouter: FakeOpenRouter
) -> None:
    """**A measured contradiction in the PRD, resolved in favour of §11.**

    §8.4 says "G2 (`lead_definition`) is off the critical path, so 2.5.1 and
    2.5.2 keep executing while sales deliberates". §11's edge list says
    `2.5.2<-{2.1.1,2.1.4}`, and a gate halts the branch *below* it. Both
    cannot hold: 2.5.2 is below G2.

    §11 wins, and not only because it is the formal spec. 2.5.2 maps CRM
    stages onto conversion actions, and which stage counts as a qualified
    lead is exactly what G2 decides — uploading a "qualified lead" conversion
    before sales has agreed what one is would be building on sand. So the
    edge is right and §8.4's sentence is wrong about 2.5.2; it is right about
    2.5.1, which this test also pins.
    """
    plan_run_id = await run_plan(admin, accepted, fake_openrouter)

    state = (await admin.get(f"/runs/{plan_run_id}")).json()
    by_node = {node["id"]: node["status"] for node in state["nodes"]}
    assert state["status"] == RunStatus.AWAITING_APPROVAL
    assert by_node["2.1.3"] == NodeRunStatus.AWAITING_APPROVAL
    assert by_node["2.1.4"] == NodeRunStatus.AWAITING_APPROVAL
    # 2.5.1 hangs off 2.1.1 alone, so it is finished before anyone decides.
    assert by_node["2.5.1"] == NodeRunStatus.SUCCEEDED
    assert by_node.get("2.5.2") != NodeRunStatus.SUCCEEDED


async def test_2_5_2_runs_once_g2_is_decided(
    admin: ApiClient,
    accepted: dict[str, Any],
    fake_openrouter: FakeOpenRouter,
    signed_in_as: Any,
) -> None:
    plan_run_id = await run_past_g2(admin, accepted, fake_openrouter, signed_in_as)
    state = (await admin.get(f"/runs/{plan_run_id}")).json()
    by_node = {node["id"]: node["status"] for node in state["nodes"]}
    assert by_node["2.5.1"] == NodeRunStatus.SUCCEEDED
    assert by_node["2.5.2"] == NodeRunStatus.SUCCEEDED
    # The run is parked on the budget gate, not finished: G3 arrived with
    # S2-P3 and a plan cannot end while a gate is open. That the 2.5 branch
    # ran *anyway* is the claim this test makes — §5.3 puts it off the
    # critical path, so it must not wait for the budget owner either.
    assert state["status"] == RunStatus.AWAITING_APPROVAL
    assert by_node["2.2.4"] == NodeRunStatus.AWAITING_APPROVAL


async def test_every_2_5_number_resolves_to_a_plan_calc_row(
    admin: ApiClient,
    accepted: dict[str, Any],
    fake_openrouter: FakeOpenRouter,
    db: AsyncSession,
    signed_in_as: Any,
) -> None:
    """PT1, walked from the output back to the row rather than asserted about."""
    plan_run_id = await run_past_g2(admin, accepted, fake_openrouter, signed_in_as)
    calcs = {
        row.evidence_id: row
        for row in (
            await db.execute(sa.select(PlanCalc).where(PlanCalc.plan_run_id == plan_run_id))
        )
        .scalars()
        .all()
    }
    expected = {
        "2.5.1": stage_2_5.RECONCILIATION,
        "2.5.2": stage_2_5.UPLOAD_WINDOW,
    }
    for node_id, formula_id in expected.items():
        output = await output_of(admin, plan_run_id, node_id)
        cited = output["calc_evidence_ids"]
        assert cited, f"{node_id} carries numbers and cited no calculation"
        for evidence_id in cited:
            row = calcs[uuid.UUID(evidence_id)]
            assert row.formula_id == formula_id
            assert row.node_id == node_id
            assert row.inputs_hash
            assert row.calc_version.startswith("calc/")
            # Against the file, not a literal: the property is that the row
            # stamps the constants it was built with, and a hard-coded version
            # breaks on every bump — including the merge bump that two parallel
            # phases both landing on 2026.09.2 made necessary.
            assert row.calc_version.endswith(load_planning_constants().version)


async def test_the_calculations_are_searchable_as_derived_evidence(
    admin: ApiClient,
    accepted: dict[str, Any],
    fake_openrouter: FakeOpenRouter,
    db: AsyncSession,
    signed_in_as: Any,
) -> None:
    """PRD §7.3: a `derived` row whose `content_text` a person can read."""
    plan_run_id = await run_past_g2(admin, accepted, fake_openrouter, signed_in_as)
    rows = (
        (
            await db.execute(
                sa.select(Evidence).where(
                    Evidence.run_id == plan_run_id,
                    Evidence.source == EvidenceSource.DERIVED,
                    Evidence.kind == "calc_measurement",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
    summaries = sorted(row.content_text or "" for row in rows)
    assert any("Reconciliation tolerance" in text for text in summaries)
    assert any("Offline upload" in text for text in summaries)


async def test_the_node_returned_the_derived_row_it_cited(
    admin: ApiClient, accepted: dict[str, Any], fake_openrouter: FakeOpenRouter, signed_in_as: Any
) -> None:
    """The executor checks the citation against what `gather()` handed back."""
    plan_run_id = await run_past_g2(admin, accepted, fake_openrouter, signed_in_as)
    for node_id in STAGE_2_5_NODES:
        body = (await admin.get(f"/runs/{plan_run_id}/nodes/{node_id}")).json()
        cited = set(body["output"]["calc_evidence_ids"])
        assert cited, node_id
        assert cited <= set(body["evidence_ids"]), (
            f"{node_id} cited a calculation it did not return from gather()"
        )


# ---------------------------------------------------------------------------
# what the nodes actually said
# ---------------------------------------------------------------------------


async def test_the_tolerances_fall_back_to_the_floor_with_no_ad_account(
    admin: ApiClient, accepted: dict[str, Any], fake_openrouter: FakeOpenRouter
) -> None:
    """No Google Ads connection means no measured divergence — and it says so."""
    plan_run_id = await run_plan(admin, accepted, fake_openrouter)
    output = await output_of(admin, plan_run_id, "2.5.1")

    assert output["primary_source"] == "crm"
    metrics = {row["metric"]: row for row in output["reconciliation"]}
    assert set(metrics) == {"cost", "conversions", "qualified_leads"}
    assert all(row["tolerance_pct"] == 5.0 for row in metrics.values())
    assert all(row["binding_driver"] == "floor" for row in metrics.values())
    assert any("conversion_action" in gap for gap in output["open_gaps"])
    # closed_won is a discrepancy, not a tolerance: nothing flows back yet.
    causes = [row["cause"] for row in output["known_discrepancies"] if row["observed"]]
    assert any("no offline-conversion action exists" in cause for cause in causes)


async def test_the_failing_synthetic_probe_reaches_the_measurement_plan(
    admin: ApiClient, accepted: dict[str, Any], fake_openrouter: FakeOpenRouter
) -> None:
    """The golden report's 1.5.2 probe failed, and 2.5.1 must not lose that."""
    plan_run_id = await run_plan(admin, accepted, fake_openrouter)
    output = await output_of(admin, plan_run_id, "2.5.1")
    causes = [row["cause"] for row in output["known_discrepancies"]]
    assert any("synthetic conversion probe returned fail" in cause for cause in causes)


async def test_consent_comes_from_the_1_5_3_gate_and_not_from_the_model(
    admin: ApiClient, accepted: dict[str, Any], fake_openrouter: FakeOpenRouter, signed_in_as: Any
) -> None:
    """PRD §13 / PC1, against the golden report's real audience lists."""
    plan_run_id = await run_past_g2(admin, accepted, fake_openrouter, signed_in_as)
    output = await output_of(admin, plan_run_id, "2.5.2")

    consent = output["consent"]
    # Two usable lists name GB and DE and carry a basis; the refused list
    # names no market, so it blocks none — and its blocker still travels.
    assert consent["markets_allowed"] == ["DE", "GB"]
    assert consent["markets_blocked"] == []
    assert sorted(consent["basis"]) == [
        "Customer list (all): contract",
        "UK prospects 2025: legitimate_interest",
    ]
    # The project runs in US, which no list mentions: unstated, not allowed.
    assert consent["markets_unstated"] == ["US"]
    assert any("gate 1.5.3 never mentioned US" in gap for gap in output["open_gaps"])
    assert any(
        item["blocking"] and "GDPR Art.6" in item["task"] for item in output["prerequisites"]
    )


async def test_the_click_id_prerequisite_leads_and_blocks(
    admin: ApiClient, accepted: dict[str, Any], fake_openrouter: FakeOpenRouter, signed_in_as: Any
) -> None:
    """PRD §21 Q3: until the CRM captures a click id, nothing else happens."""
    plan_run_id = await run_past_g2(admin, accepted, fake_openrouter, signed_in_as)
    output = await output_of(admin, plan_run_id, "2.5.2")

    capture = output["gclid_capture"]
    assert capture["present_today"] is False
    assert capture["caveat"] is not None
    assert output["prerequisites"][0]["blocking"] is True
    assert "Capture the Google click id" in output["prerequisites"][0]["task"]
    # §13 wants a retention window named. It is the longest a click id has to
    # survive to still be uploadable — the deeper of the backfill and the wait
    # before the next upload — and not a number anyone typed.
    upload = output["upload"]
    assert capture["retention_days"] == HISTORY_DAYS
    assert capture["retention_days"] == max(upload["lag_days"], upload["backfill_days"])


async def test_the_backfill_is_bounded_by_the_crm_history(
    admin: ApiClient, accepted: dict[str, Any], fake_openrouter: FakeOpenRouter, signed_in_as: Any
) -> None:
    """The oldest seeded row is 45 days old, so Google's 90 is not on offer."""
    plan_run_id = await run_past_g2(admin, accepted, fake_openrouter, signed_in_as)
    upload = (await output_of(admin, plan_run_id, "2.5.2"))["upload"]
    assert upload["method"] == "manual_csv"
    assert upload["cadence"] == "monthly"
    assert upload["lag_days"] == 32.0  # a 30-day period, then a 2-day turnaround
    assert upload["click_upload_window_days"] == 90.0
    assert upload["headroom_days"] == 58.0
    assert upload["history_limits_backfill"] is True
    assert upload["backfill_days"] == HISTORY_DAYS


# ---------------------------------------------------------------------------
# PRD §13 — prompt hygiene
# ---------------------------------------------------------------------------


async def test_no_crm_account_name_reaches_a_2_5_prompt(
    admin: ApiClient,
    accepted: dict[str, Any],
    fake_openrouter: FakeOpenRouter,
    signed_in_as: Any,
) -> None:
    """§13: plan nodes read aggregates. 2.5.2 dates the CRM; it never quotes it.

    Asserted against the prompt the node actually sent, read back off its
    `NodeRun` — not against the node's source, which is where a future
    `evidence_block(found, crm.CRM_WON)` would slip through unnoticed.

    Scoped to 2.5.1 and 2.5.2 deliberately. **Node 2.1.1 puts raw `crm_won`
    rows into its prompt** — `account_name` and all, via
    `evidence_block(found, crm.CRM_WON)` — which §13's "no individual record,
    name, email or company identifier enters a prompt" forbids. Measured, not
    assumed: a probe over every node's recorded prompt on one run found the
    two seeded account names in 2.1.1's and in nothing else's. That is
    S2-P2's defect, not this phase's, and widening this test to the whole run
    would only mean deleting it again.
    """
    plan_run_id = await run_past_g2(admin, accepted, fake_openrouter, signed_in_as)

    for node_id in STAGE_2_5_NODES:
        body = (await admin.get(f"/runs/{plan_run_id}/nodes/{node_id}")).json()
        prompt = body["prompt"] or ""
        assert prompt, f"{node_id} recorded no prompt, so this proves nothing"
        for name in CRM_NAMES:
            assert name not in prompt, f"{node_id} put the CRM account {name!r} in a prompt"


# ---------------------------------------------------------------------------
# PRD §13 — EU consent signals
# ---------------------------------------------------------------------------


async def test_an_eu_market_in_scope_makes_the_missing_signal_blocking(
    admin: ApiClient,
    accepted: dict[str, Any],
    fake_openrouter: FakeOpenRouter,
    db: AsyncSession,
    project: Any,
) -> None:
    """§13, last row: name the mechanism or carry a blocking dependency."""
    project.markets = [
        {"country": "US", "language": "en", "currency": "USD"},
        {"country": "DE", "language": "de", "currency": "EUR"},
    ]
    db.add(project)
    await db.commit()

    plan_run_id = await run_plan(admin, accepted, fake_openrouter)
    output = await output_of(admin, plan_run_id, "2.5.1")

    assert output["consent_signal"]["required"] is True
    assert output["consent_signal"]["markets"] == ["DE"]
    assert output["consent_signal"]["mechanism"] is None
    blocking = [item for item in output["prerequisites"] if item["blocking"]]
    assert len(blocking) == 1
    assert "consent-signal mechanism" in blocking[0]["task"]

    # And the modelled allowance is now the widest term on the modelled metrics.
    metrics = {row["metric"]: row for row in output["reconciliation"]}
    assert metrics["conversions"]["tolerance_pct"] == 20.0
    assert metrics["conversions"]["binding_driver"] == "modelled_conversions"
    assert metrics["cost"]["tolerance_pct"] == 5.0
