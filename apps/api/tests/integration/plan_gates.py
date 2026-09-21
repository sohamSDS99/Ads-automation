"""Everything more than one plan integration suite needs to reach a gate.

Extracted from `test_plan_stage_2_2.py` when S2-P4 added a second suite that
has to walk the same road: the accepted research report, the seeded CRM and
account history, the scripted answers and the four gate handovers are ~20
seconds of setup and one description of what a plan run looks like. A second
copy would be a second thing to keep in step with the golden report.

The two *fixtures* that go with these helpers — `accepted` and
`fake_openrouter` — live in `conftest.py` rather than here. A fixture imported
into a test module collides with the parameter that requests it, so the only
place shared fixtures can live without every suite carrying a `noqa` is the
conftest pytest already reads.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from agent.db.models import NodeRunStatus, RunStatus
from agent.planning import demand
from tests.integration.plan_answers import every_plan_answer
from tests.integration.runs_support import execute
from tests.openrouter_fake import FakeOpenRouter

if TYPE_CHECKING:
    # Runtime-free: `conftest` imports this module for its fixtures, and a
    # real import here would close the circle.
    from tests.integration.conftest import ApiClient

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
    """Every plan node's scripted answer, from the shared table.

    `every_plan_answer()` rather than a table of this suite's own: a plan run
    is the whole DAG, so scripting only 2.1 and 2.2 would fail on the first
    node of any stage shipped after this file was written — which is exactly
    what happened the first time S2-P5a and this phase met. The 2.2 entries
    this suite needs to *differ* from the shared ones are layered on top by
    `assign_every_cluster`.
    """
    script = dict(every_plan_answer())
    # Two campaigns, not the shared table's one. This suite needs at least two
    # allocation lines to mean anything: a budget owner moving money *between*
    # lines is the edit gate G3 exists to collect, and a single-line split
    # cannot express it.
    script["CampaignTargetsDraft"] = {
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
    }
    return script


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


def assign_every_cluster(script: dict[str, Any], clusters: list[dict[str, str]]) -> None:
    """Point every cluster at the campaign for its market.

    Built from what 2.2.1 actually found rather than hard-coded: the golden
    report's clusters come from its own keyword-to-page map, and a fixed list
    here would silently stop covering them the day that map changes.
    """
    script["RulesDraft"] = {
        **script["RulesDraft"],
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
    }
    # This suite renames the campaigns, so the shared slate — which names the
    # ref `STAGE_2_1` produces — would place none of them and node 2.3.1 would
    # (correctly) refuse the run. Derived here from the same two refs above.
    script["SlateDraft"] = {
        **script["SlateDraft"],
        "slate": [
            {
                "campaign_type": "search",
                "market": market,
                "campaign_refs": [f"nonbrand-{market.lower()}-lead-gen"],
                "launch_wave": 1,
                "rationale": "the campaign for this market",
                "entry_criteria": [],
                "exit_criteria": [],
                "prerequisites": [],
            }
            for market in sorted({row["market"] for row in clusters})
        ],
    }
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
