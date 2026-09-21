"""Stage 2.2 — what the nodes do with the forecast they were given.

The property under test throughout is law 14: **no figure in a plan node's
output was produced by the model.** The scripted answers here contain no
figures at all — the model is asked for a cluster-to-campaign assignment, a
scenario name, a threshold *basis* and some prose — so a node that got a number
from the model could not have, there being none to get.

The fixtures are round numbers on purpose. Every expectation below is
hand-checkable from the block at the top, and where a figure is asserted it is
asserted by value against the calculation the node cited, not against a
recomputation of the same arithmetic in the test.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from agent.nodes import gather
from agent.nodes.base import collect_calc_evidence_ids, derived_ids
from agent.nodes.plan import stage_2_2
from agent.orchestrator.plan_calc import CalcNotPermitted
from agent.planning import demand
from tests import plan_support as support


@pytest.fixture
def stub_collect(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Replace `gather.collect` with prepared evidence, per call.

    A list rather than one value: `_demand` collects twice — the benchmarks,
    then the forecast it can only ask for once it knows the clusters — and the
    two answers are different. A single stub would hide the second call
    entirely, which is the call the degradation test is about.
    """

    def install(*answers: gather.Gathered) -> list[tuple[Any, ...]]:
        calls: list[tuple[Any, ...]] = []
        queue = list(answers)

        async def collect(ctx: Any, *needs: gather.Need) -> gather.Gathered:
            calls.append(needs)
            return queue.pop(0) if queue else gather.Gathered()

        monkeypatch.setattr(gather, "collect", collect)
        return calls

    return install


# ---------------------------------------------------------------------------
# fixtures — every figure below is checkable by hand
# ---------------------------------------------------------------------------

#: Two clusters, three cluster-markets, one flat month. Scaled to a plan a
#: person would recognise: the per-campaign floor is $1,000/month, and a
#: fixture under it collapses all three scenarios onto the floor and hides
#: every difference between them.
#:
#:   SDS management / US : 600,000 searches, CPC mid $10
#:   SDS management / GB : 150,000 searches, CPC mid $8
#:   SDS education  / US : 900,000 searches, CPC mid $1.50
#:
#: Account history: 400,000 impressions, 16,000 clicks, 480 conversions
#:   => CTR 4%, CVR 3%.
#: Impression share held: 35% of 200,000 impressions => headroom 65%.
#:
#: Derived arithmetic at a 45% impression-share target:
#:   SDS management/US impressions = 600,000 x 0.45        = 270,000
#:                     clicks      = 270,000 x 0.04        =  10,800
#:                     cost        = 10,800 x 10           = 108,000
#:                     conversions = 10,800 x 0.03         =     324
#:                     CPA         = 108,000 / 324         =  333.33
#:   SDS management/GB                                cost =  21,600, conv  81
#:   SDS education/US                                 cost =  24,300, conv 486
#:   forecast total, one month                        cost = 153,900
KEYWORDS = [
    {
        "term": "sds software",
        "market": "US",
        "volume": 400_000,
        "cpc_low": 8.0,
        "cpc_high": 12.0,
        "best_url": "https://sdsmanager.test/sds",
        "funnel_stage": "bofu",
    },
    {
        "term": "sds management system",
        "market": "US",
        "volume": 200_000,
        "cpc_low": 8.0,
        "cpc_high": 12.0,
        "best_url": "https://sdsmanager.test/sds",
        "funnel_stage": "bofu",
    },
    {
        "term": "sds software uk",
        "market": "GB",
        "volume": 150_000,
        "cpc_low": 6.0,
        "cpc_high": 10.0,
        "best_url": "https://sdsmanager.test/sds",
        "funnel_stage": "bofu",
    },
    {
        "term": "what is an sds",
        "market": "US",
        "volume": 900_000,
        "cpc_low": 1.0,
        "cpc_high": 2.0,
        "best_url": "https://sdsmanager.test/blog",
        "funnel_stage": "tofu",
    },
]

#: The same shape at a thousandth of the volume, for the two tests that are
#: about a plan the floor *does* dominate. Kept separate rather than reused
#: with a scale factor, so every figure in either fixture stays hand-checkable.
TINY_KEYWORDS = [{**row, "volume": row["volume"] // 1000} for row in KEYWORDS]

MAPPING = [
    {
        "term_cluster": "SDS management",
        "best_url": "https://sdsmanager.test/sds",
        "verdict": "good_fit",
        "evidence_ids": [],
    },
    {
        "term_cluster": "SDS education",
        "best_url": "https://sdsmanager.test/blog",
        "verdict": "good_fit",
        "evidence_ids": [],
    },
]

CAMPAIGN_PERF = {
    "channel": "SEARCH",
    "impressions": 400_000,
    "clicks": 16_000,
    "conversions": 480,
    "cost": 96_000.0,
}
IMPRESSION_SHARE = {"impression_share": 0.35, "impressions": 200_000}

EXPECTED_CTR_PCT = 4.0
EXPECTED_CVR_PCT = 3.0
EXPECTED_HEADROOM_PCT = 65.0

#: What 2.1.3 agreed, as its output looks on the wire.
OBJECTIVES = {
    "objectives": [
        {
            "campaign_ref": "brand-us",
            "objective": "lead_gen",
            "primary_kpi": "cpa",
            "target_value": 400.0,
            "ceiling_value": 500.0,
        },
        {
            "campaign_ref": "education-us",
            "objective": "lead_gen",
            "primary_kpi": "cpl",
            "target_value": 120.0,
            "ceiling_value": 150.0,
        },
        {
            "campaign_ref": "brand-gb",
            "objective": "lead_gen",
            "primary_kpi": "cpa",
            "target_value": 450.0,
            "ceiling_value": 520.0,
        },
    ],
    "north_star": {"metric": "cpa", "target": 400.0, "period": "monthly"},
}

TAXONOMY = {
    "actions": [
        {"name": "Demo request", "primary": True, "value_model": "fixed", "rank": 1},
        {"name": "Newsletter", "primary": False, "value_model": "none", "rank": 2},
    ]
}

CEILINGS = {
    "blended": {
        "segment": "blended",
        "acv_usd": 20_000.0,
        "max_cpa_won_usd": 5_333.33,
        "target_cpa_won_usd": 4_533.33,
        "max_cpl_usd": 4_266.67,
        "target_cpl_usd": 3_626.67,
    }
}

ASSIGNMENTS = [
    {
        "cluster": "SDS management",
        "market": "US",
        "campaign_ref": "brand-us",
        "rationale": "bottom-of-funnel product demand",
    },
    {
        "cluster": "SDS management",
        "market": "GB",
        "campaign_ref": "brand-gb",
        "rationale": "the same demand in the UK",
    },
    {
        "cluster": "SDS education",
        "market": "US",
        "campaign_ref": "education-us",
        "rationale": "research intent, not purchase intent",
    },
]


def report(keywords: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    rows = keywords if keywords is not None else KEYWORDS
    return support.research_report(
        priced_keyword_list=rows,
        demand_map={"total_keywords": len(rows), "mapping": MAPPING},
    )


def found(
    *,
    perf: list[dict[str, Any]] | None = None,
    share: list[dict[str, Any]] | None = None,
) -> gather.Gathered:
    rows = [
        support.evidence(demand.CAMPAIGN_PERF, row)
        for row in (perf if perf is not None else [CAMPAIGN_PERF])
    ]
    rows.extend(
        support.evidence(demand.KEYWORD_IMPRESSION_SHARE, row)
        for row in (share if share is not None else [IMPRESSION_SHARE])
    )
    return gather.Gathered(evidence=rows)


def forecast_found(
    rows: list[dict[str, Any]] | None = None, *, degraded: str | None = None
) -> gather.Gathered:
    got = gather.Gathered(
        evidence=[support.evidence(demand.KEYWORD_FORECAST, row) for row in rows or []]
    )
    if degraded:
        got.degraded[demand.KEYWORD_FORECAST] = degraded
    return got


def harness(
    node_id: str,
    *,
    answers: dict[str, Any],
    outputs: dict[str, Any],
    keywords: list[dict[str, Any]] | None = None,
    **kwargs: Any,
):
    permitted = {
        "2.2.1": (stage_2_2.TRAFFIC,),
        "2.2.2": (stage_2_2.TRAFFIC, stage_2_2.VOLUME_CHECK),
        "2.2.3": (stage_2_2.TRAFFIC, stage_2_2.ENVELOPE, stage_2_2.SPLIT),
        "2.2.4": (stage_2_2.TRAFFIC, stage_2_2.SPLIT),
        "2.2.5": (stage_2_2.VOLUME_CHECK,),
    }[node_id]
    return support.harness(
        node_id,
        answers=answers,
        gathered=gather.Gathered(),
        source=support.plan_input(report(keywords), **kwargs),
        permitted=permitted,
        outputs=outputs,
    )


NOTES = {
    "method_notes": "Built from the account's own rates.",
    "caveats": ["A forecast is not a promise."],
}
CAPACITY_DRAFT = {"assignments": ASSIGNMENTS, "notes": "Three clusters, three campaigns."}
NARRATIVES = {
    "narratives": [
        {"name": name, "case_for": f"for {name}", "case_against": f"against {name}"}
        for name in ("cautious", "expected", "aggressive")
    ],
    "notes": "All three share the same CPA under linear scaling.",
}
CHOICE = {
    "chosen_scenario": "expected",
    "rationale": "The forecast CPA is inside the target.",
    "what_would_change_it": "A measured CPC above the research's range.",
}


async def run(node: Any, harnessed: Any) -> Any:
    evidence = await node.gather(harnessed.ctx)
    return await node.reason(harnessed.ctx, evidence), evidence


# ---------------------------------------------------------------------------
# 2.2.1 — demand_forecast
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_2_2_1_forecasts_from_the_accounts_own_rates(stub_collect: Any) -> None:
    stub_collect(found(), forecast_found())
    out, _ = await run(
        stage_2_2.demand_forecast,
        harness("2.2.1", answers={"ForecastNotes": NOTES}, outputs={"2.1.1": TAXONOMY}),
    )

    assert out.method == "derived_arithmetic"
    assert out.status == "ok"
    # The rates are measured, not the planning defaults.
    assert {row.ctr_pct for row in out.forecast} == {EXPECTED_CTR_PCT}
    assert {row.cvr_pct for row in out.forecast} == {EXPECTED_CVR_PCT}
    assert out.impression_share_headroom_pct == EXPECTED_HEADROOM_PCT

    by_cluster = {(row.cluster, row.market): row for row in out.forecast}
    brand_us = by_cluster[("SDS management", "US")]
    assert brand_us.impressions == 270_000  # 600,000 x 45%
    assert brand_us.clicks == 10_800.0  # 270,000 x 4%
    assert brand_us.cost_usd == 108_000.0  # 10,800 x $10
    assert brand_us.conversions == 324.0  # 10,800 x 3%


@pytest.mark.asyncio
async def test_2_2_1_asks_the_model_for_prose_and_nothing_else(stub_collect: Any) -> None:
    stub_collect(found(), forecast_found())
    harnessed = harness("2.2.1", answers={"ForecastNotes": NOTES}, outputs={"2.1.1": TAXONOMY})
    out, _ = await run(stage_2_2.demand_forecast, harnessed)

    assert out.method_notes == NOTES["method_notes"]
    assert set(stage_2_2.ForecastNotes.model_fields) == {"method_notes", "caveats"}
    prompt = harnessed.llm.user_prompt("ForecastNotes")
    assert "do not restate or adjust" in prompt


@pytest.mark.asyncio
async def test_2_2_1_cites_only_derived_rows_it_returned(stub_collect: Any) -> None:
    stub_collect(found(), forecast_found())
    out, evidence = await run(
        stage_2_2.demand_forecast,
        harness("2.2.1", answers={"ForecastNotes": NOTES}, outputs={"2.1.1": TAXONOMY}),
    )

    cited = collect_calc_evidence_ids(out.model_dump(mode="python"))
    assert cited
    assert cited <= derived_ids(evidence)


@pytest.mark.asyncio
async def test_2_2_1_falls_back_to_planning_defaults_and_says_so(stub_collect: Any) -> None:
    """PRD §18: "Google Ads account not connected at all — 2.2.1 runs on Stage
    01 data only". It runs, and the gap names the substitution."""
    stub_collect(found(perf=[], share=[]), forecast_found())
    out, _ = await run(
        stage_2_2.demand_forecast,
        harness("2.2.1", answers={"ForecastNotes": NOTES}, outputs={"2.1.1": TAXONOMY}),
    )

    assert out.status == "ok"
    assert any("campaign_perf" in gap for gap in out.open_gaps)
    assert any("planning default" in gap for gap in out.open_gaps)
    assert out.impression_share_headroom_pct is None


@pytest.mark.asyncio
async def test_2_2_1_uses_googles_forecast_when_it_answers(stub_collect: Any) -> None:
    stub_collect(
        found(),
        forecast_found(
            [
                {
                    "cluster": "SDS management",
                    "market": "US",
                    "impressions": 10_000.0,
                    "clicks": 500.0,
                    "cost": 4_000.0,
                }
            ]
        ),
    )
    out, _ = await run(
        stage_2_2.demand_forecast,
        harness("2.2.1", answers={"ForecastNotes": NOTES}, outputs={"2.1.1": TAXONOMY}),
    )

    assert out.method == "google_forecast"
    by_cluster = {(row.cluster, row.market): row for row in out.forecast}
    google = by_cluster[("SDS management", "US")]
    # Google's own impressions, CTR and CPC — not the account benchmark.
    assert google.impressions == 10_000
    assert google.ctr_pct == 5.0  # 500 / 10,000
    assert google.avg_cpc_usd == 8.0  # 4,000 / 500
    # A cluster Google did not answer for crosses to the same column by hand,
    # at the impression-share target, rather than being dropped.
    derived = by_cluster[("SDS education", "US")]
    assert derived.impressions == 405_000  # 900,000 x 45%
    assert derived.ctr_pct == EXPECTED_CTR_PCT


@pytest.mark.asyncio
async def test_2_2_1_degrades_when_the_forecast_service_refuses(stub_collect: Any) -> None:
    """The S2-P3 exit criterion: "forecast-service failure degrades without
    stopping the run"."""
    stub_collect(found(), forecast_found(degraded="CLOUD_PROJECT_NOT_APPROVED"))
    out, _ = await run(
        stage_2_2.demand_forecast,
        harness("2.2.1", answers={"ForecastNotes": NOTES}, outputs={"2.1.1": TAXONOMY}),
    )

    assert out.status == "ok"
    assert out.method == "derived_arithmetic"
    assert out.degraded_sources == [stage_2_2.FORECAST_SOURCE]
    assert out.forecast


@pytest.mark.asyncio
async def test_2_2_1_stops_when_there_is_no_demand_to_forecast(stub_collect: Any) -> None:
    stub_collect(found(), forecast_found())
    harnessed = support.harness(
        "2.2.1",
        answers={"ForecastNotes": NOTES},
        gathered=gather.Gathered(),
        source=support.plan_input(support.research_report()),
        permitted=(stage_2_2.TRAFFIC,),
        outputs={"2.1.1": TAXONOMY},
    )
    with pytest.raises(stage_2_2.InsufficientDemand) as caught:
        await run(stage_2_2.demand_forecast, harnessed)

    assert "priced_keyword_list" in str(caught.value)


@pytest.mark.asyncio
async def test_2_2_1_asks_google_for_the_clusters_it_found(stub_collect: Any) -> None:
    """The groups are discovered before the pull, which is why the frame is
    built twice. The second gather must carry them."""
    calls = stub_collect(found(), forecast_found())
    await run(
        stage_2_2.demand_forecast,
        harness("2.2.1", answers={"ForecastNotes": NOTES}, outputs={"2.1.1": TAXONOMY}),
    )

    assert len(calls) == 2
    groups = calls[1][0].params["groups"]
    assert {(item["cluster"], item["market"]) for item in groups} == {
        ("SDS management", "US"),
        ("SDS management", "GB"),
        ("SDS education", "US"),
    }
    brand_us = next(
        item for item in groups if item["market"] == "US" and "management" in item["cluster"]
    )
    assert sorted(brand_us["keywords"]) == ["sds management system", "sds software"]
    assert brand_us["max_cpc_usd"] == 10.0


# ---------------------------------------------------------------------------
# 2.2.2 — learning_capacity_check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_2_2_2_checks_the_forecast_against_the_learning_thresholds(
    stub_collect: Any,
) -> None:
    stub_collect(found(), forecast_found())
    out, _ = await run(
        stage_2_2.learning_capacity_check,
        harness(
            "2.2.2",
            answers={"CapacityDraft": CAPACITY_DRAFT},
            outputs={"2.1.1": TAXONOMY, "2.1.3": OBJECTIVES},
        ),
    )

    by_ref = {row.campaign_ref: row for row in out.campaigns}
    assert set(by_ref) == {"brand-us", "education-us", "brand-gb"}
    # brand-us: $108,000 cost / 324 conversions => $333.33 CPA, 324 conv/30d.
    assert by_ref["brand-us"].forecast_cpa_usd == 333.33
    assert by_ref["brand-us"].forecast_conv_30d == 324.0
    # Comfortably over `learning.troas_min_conv_30d` (50), and the taxonomy's
    # primary action carries a value, so the account can run tROAS.
    assert by_ref["brand-us"].verdict == "clears"
    assert by_ref["brand-us"].bid_strategy_recommended == "troas"
    assert out.structure_verdict == "sound"
    assert by_ref["brand-us"].clusters == ["SDS management (US)"]


@pytest.mark.asyncio
async def test_2_2_2_keeps_the_models_assignment(stub_collect: Any) -> None:
    stub_collect(found(), forecast_found())
    out, _ = await run(
        stage_2_2.learning_capacity_check,
        harness(
            "2.2.2",
            answers={"CapacityDraft": CAPACITY_DRAFT},
            outputs={"2.1.1": TAXONOMY, "2.1.3": OBJECTIVES},
        ),
    )

    assert [item.campaign_ref for item in out.assignments] == [
        "brand-us",
        "brand-gb",
        "education-us",
    ]
    assert out.notes == CAPACITY_DRAFT["notes"]
    assert out.unassigned_clusters == []


@pytest.mark.asyncio
async def test_2_2_2_refuses_a_campaign_gate_g1_never_agreed(stub_collect: Any) -> None:
    """Budget against a campaign with no target and no gate decision behind it
    is budget nobody approved. The cluster becomes visibly unassigned instead."""
    stub_collect(found(), forecast_found())
    invented = [
        {**ASSIGNMENTS[0], "campaign_ref": "a-campaign-nobody-agreed"},
        *ASSIGNMENTS[1:],
    ]
    out, _ = await run(
        stage_2_2.learning_capacity_check,
        harness(
            "2.2.2",
            answers={"CapacityDraft": {**CAPACITY_DRAFT, "assignments": invented}},
            outputs={"2.1.1": TAXONOMY, "2.1.3": OBJECTIVES},
        ),
    )

    refs = {row.campaign_ref for row in out.campaigns}
    assert "a-campaign-nobody-agreed" not in refs
    assert demand.UNASSIGNED in refs
    assert out.unassigned_clusters == ["SDS management (US)"]


@pytest.mark.asyncio
async def test_2_2_2_reads_the_taxonomy_for_whether_troas_is_possible(
    stub_collect: Any,
) -> None:
    """An account whose primary conversion carries no value cannot run tROAS
    whatever its volume, and the taxonomy is the only place that is knowable."""
    stub_collect(found(), forecast_found())
    valueless = {
        "actions": [{"name": "Demo request", "primary": True, "value_model": "none", "rank": 1}]
    }
    out, _ = await run(
        stage_2_2.learning_capacity_check,
        harness(
            "2.2.2",
            answers={"CapacityDraft": CAPACITY_DRAFT},
            outputs={"2.1.1": valueless, "2.1.3": OBJECTIVES},
        ),
    )

    # `troas_min_conv_30d` is 50 and `tcpa_min_conv_30d` is 30; a valueless
    # account is judged against the tCPA threshold.
    assert {row.threshold for row in out.campaigns} == {30.0}


# ---------------------------------------------------------------------------
# 2.2.3 — budget_scenarios
# ---------------------------------------------------------------------------


def scenario_outputs() -> dict[str, Any]:
    return {
        "2.1.1": TAXONOMY,
        "2.1.2": CEILINGS,
        "2.1.3": OBJECTIVES,
        "2.2.2": {"assignments": ASSIGNMENTS, "campaigns": []},
    }


@pytest.mark.asyncio
async def test_2_2_3_produces_three_scenarios_each_already_split(stub_collect: Any) -> None:
    """The S2-P3 exit criterion: "three scenarios generate from real demand
    data"."""
    stub_collect(found(), forecast_found())
    out, _ = await run(
        stage_2_2.budget_scenarios,
        harness("2.2.3", answers={"ScenariosDraft": NARRATIVES}, outputs=scenario_outputs()),
    )

    assert [item.name for item in out.scenarios] == ["cautious", "expected", "aggressive"]
    assert all(item.allocation for item in out.scenarios)
    assert out.recommended in {"cautious", "expected", "aggressive"}
    for scenario in out.scenarios:
        assert {line.campaign_ref for line in scenario.allocation} <= {
            "brand-us",
            "education-us",
            "brand-gb",
        }


@pytest.mark.asyncio
async def test_2_2_3_allocation_lines_are_campaign_market_funnel(stub_collect: Any) -> None:
    stub_collect(found(), forecast_found())
    out, _ = await run(
        stage_2_2.budget_scenarios,
        harness("2.2.3", answers={"ScenariosDraft": NARRATIVES}, outputs=scenario_outputs()),
    )

    expected = next(item for item in out.scenarios if item.name == "expected")
    units = {(line.campaign_ref, line.market, line.funnel_stage) for line in expected.allocation}
    assert units == {
        ("brand-us", "US", "bofu"),
        ("brand-gb", "GB", "bofu"),
        ("education-us", "US", "tofu"),
    }


@pytest.mark.asyncio
async def test_2_2_3_carries_the_models_case_without_taking_a_figure(
    stub_collect: Any,
) -> None:
    stub_collect(found(), forecast_found())
    harnessed = harness("2.2.3", answers={"ScenariosDraft": NARRATIVES}, outputs=scenario_outputs())
    out, _ = await run(stage_2_2.budget_scenarios, harnessed)

    assert {item.case_for for item in out.scenarios} == {
        "for cautious",
        "for expected",
        "for aggressive",
    }
    assert set(stage_2_2.ScenarioNarrative.model_fields) == {"name", "case_for", "case_against"}


@pytest.mark.asyncio
async def test_2_2_3_names_an_envelope_the_demand_cannot_absorb(stub_collect: Any) -> None:
    """PRD §18's "envelope too small for the slate", and its mirror.

    `TINY_KEYWORDS` forecasts far under the $1,000-per-campaign floor, so every
    scenario is raised to the floor and then cannot spend it — which is a
    finding, not a budget to celebrate.
    """
    stub_collect(found(), forecast_found())
    out, _ = await run(
        stage_2_2.budget_scenarios,
        harness(
            "2.2.3",
            answers={"ScenariosDraft": NARRATIVES},
            outputs=scenario_outputs(),
            keywords=TINY_KEYWORDS,
        ),
    )

    assert out.status == "infeasible"
    assert out.infeasible_reason is not None
    assert "cannot absorb" in out.infeasible_reason
    assert out.minimum_viable_envelope_usd == 3_000.0  # 3 campaigns x $1,000


@pytest.mark.asyncio
async def test_2_2_3_cites_every_calculation_it_made(stub_collect: Any) -> None:
    stub_collect(found(), forecast_found())
    out, evidence = await run(
        stage_2_2.budget_scenarios,
        harness("2.2.3", answers={"ScenariosDraft": NARRATIVES}, outputs=scenario_outputs()),
    )

    cited = collect_calc_evidence_ids(out.model_dump(mode="python"))
    assert cited <= derived_ids(evidence)
    # One traffic forecast, one envelope, and one split per *distinct* envelope.
    # Distinct, not three: `DerivedWriter` dedupes on
    # `(plan_run_id, formula_id, inputs_hash)`, so two scenarios that round to
    # the same envelope — which is what the per-campaign floor does to a small
    # plan — are legitimately one calculation. `TINY_KEYWORDS` proves that half.
    envelopes = {scenario.monthly_total_usd for scenario in out.scenarios}
    assert len(envelopes) == 3
    assert len(cited) == 5


# ---------------------------------------------------------------------------
# 2.2.4 ⛳ G3 — budget_allocation
# ---------------------------------------------------------------------------


async def scenarios_for_gate(stub_collect: Any) -> dict[str, Any]:
    """Run 2.2.3 for real and hand its output to 2.2.4, as the executor would."""
    stub_collect(found(), forecast_found(), found(), forecast_found())
    out, _ = await run(
        stage_2_2.budget_scenarios,
        harness("2.2.3", answers={"ScenariosDraft": NARRATIVES}, outputs=scenario_outputs()),
    )
    return out.model_dump(mode="json")


@pytest.mark.asyncio
async def test_2_2_4_is_gate_g3_and_routes_to_an_approver() -> None:
    spec = stage_2_2.budget_allocation.spec
    assert spec.gate is True
    assert spec.gate_key == "G3"
    assert spec.required_role.value == "approver"


@pytest.mark.asyncio
async def test_2_2_4_envelope_equals_what_the_allocation_commits(stub_collect: Any) -> None:
    """PRD §12 invariant 4. A measured absorption cap can leave a scenario's
    headline unspendable, and an envelope that disagrees with its own lines is
    a plan that gets queried instead of approved."""
    proposed = await scenarios_for_gate(stub_collect)
    out, _ = await run(
        stage_2_2.budget_allocation,
        harness(
            "2.2.4",
            answers={"ScenarioChoice": CHOICE},
            outputs={
                "2.1.1": TAXONOMY,
                "2.1.2": CEILINGS,
                "2.1.3": OBJECTIVES,
                "2.2.2": {"assignments": ASSIGNMENTS, "campaigns": []},
                "2.2.3": proposed,
            },
        ),
    )

    committed = round(sum(line.usd for line in out.allocation), 2)
    assert out.envelope.monthly_cap_usd == pytest.approx(committed, abs=0.01)
    assert out.envelope.scenario_total_usd >= out.envelope.monthly_cap_usd
    if out.envelope.unallocated_usd > 0:
        assert out.envelope.unallocated_reason


@pytest.mark.asyncio
async def test_2_2_4_takes_the_scenario_the_model_chose(stub_collect: Any) -> None:
    proposed = await scenarios_for_gate(stub_collect)
    out, _ = await run(
        stage_2_2.budget_allocation,
        harness(
            "2.2.4",
            answers={"ScenarioChoice": {**CHOICE, "chosen_scenario": "cautious"}},
            outputs={
                "2.1.1": TAXONOMY,
                "2.1.2": CEILINGS,
                "2.1.3": OBJECTIVES,
                "2.2.2": {"assignments": ASSIGNMENTS, "campaigns": []},
                "2.2.3": proposed,
            },
        ),
    )

    assert out.chosen_scenario == "cautious"
    assert out.rationale == CHOICE["rationale"]


@pytest.mark.asyncio
async def test_2_2_4_warns_about_a_campaign_this_envelope_cannot_teach(
    stub_collect: Any,
) -> None:
    """§18: "approver's edit pushes a campaign below its learning threshold —
    inline amber warning naming the threshold and the shortfall"."""
    proposed = await scenarios_for_gate(stub_collect)
    capacity = [
        {
            "campaign_ref": "brand-us",
            "forecast_conv_30d": 3.24,
            "threshold": 30.0,
            "verdict": "below",
            "remedy": "raise_budget",
        },
        {
            "campaign_ref": "education-us",
            "forecast_conv_30d": 40.0,
            "threshold": 30.0,
            "verdict": "clears",
            "remedy": None,
        },
    ]
    out, _ = await run(
        stage_2_2.budget_allocation,
        harness(
            "2.2.4",
            answers={"ScenarioChoice": CHOICE},
            outputs={
                "2.1.1": TAXONOMY,
                "2.1.2": CEILINGS,
                "2.1.3": OBJECTIVES,
                "2.2.2": {"assignments": ASSIGNMENTS, "campaigns": capacity},
                "2.2.3": proposed,
            },
        ),
    )

    warned = {item.campaign_ref for item in out.learning_warnings}
    assert warned == {"brand-us"}
    assert out.learning_warnings[0].threshold == 30.0
    assert out.learning_warnings[0].forecast_conv_30d == 3.24


@pytest.mark.asyncio
async def test_2_2_4_carries_the_degraded_source_to_the_gate_card(
    stub_collect: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR1: "a degraded connector never stops the run; `degraded_sources` is
    carried into the plan and displayed on the budget gate"."""
    stub_collect(found(), forecast_found(), found(), forecast_found(degraded="not approved"))
    scenarios = await run(
        stage_2_2.budget_scenarios,
        harness("2.2.3", answers={"ScenariosDraft": NARRATIVES}, outputs=scenario_outputs()),
    )
    out, _ = await run(
        stage_2_2.budget_allocation,
        harness(
            "2.2.4",
            answers={"ScenarioChoice": CHOICE},
            outputs={
                "2.1.1": TAXONOMY,
                "2.1.2": CEILINGS,
                "2.1.3": OBJECTIVES,
                "2.2.2": {"assignments": ASSIGNMENTS, "campaigns": []},
                "2.2.3": scenarios[0].model_dump(mode="json"),
            },
        ),
    )

    assert out.degraded_sources == [stage_2_2.FORECAST_SOURCE]


# ---------------------------------------------------------------------------
# 2.2.5 — reallocation_rules
# ---------------------------------------------------------------------------

APPROVED = {
    "envelope": {"monthly_cap_usd": 6000.0, "quarterly_cap_usd": 18000.0, "currency": "USD"},
    "allocation": [
        {
            "campaign_ref": "brand-us",
            "market": "US",
            "funnel_stage": "bofu",
            "usd": 4000.0,
            "forecast_cpa_usd": 100.0,
            "avg_cpc_usd": 10.0,
        },
        {
            "campaign_ref": "education-us",
            "market": "US",
            "funnel_stage": "tofu",
            "usd": 2000.0,
            "forecast_cpa_usd": 50.0,
            "avg_cpc_usd": 1.5,
        },
    ],
    "learning_warnings": [],
}

RULES_DRAFT = {
    "rules": [
        {
            "id": "R1",
            "trigger_metric": "cpa",
            "comparison": "above",
            "threshold_basis": "forecast_cpa",
            "from_campaign": "brand-us",
            "to_campaign": "education-us",
            "shift_size": "standard",
            "requires_human": False,
            "rationale": "Stop paying over the forecast for the same lead.",
        }
    ],
    "review_cadence": "monthly",
    "notes": "One rule while the account is small.",
}


@pytest.mark.asyncio
async def test_2_2_5_fills_every_figure_from_a_calculation_or_a_constant() -> None:
    harnessed = harness(
        "2.2.5",
        answers={"RulesDraft": RULES_DRAFT},
        outputs={
            "2.1.3": OBJECTIVES,
            "2.2.2": {"campaigns": [{"campaign_ref": "brand-us", "has_revenue_values": True}]},
            "2.2.4": APPROVED,
        },
    )
    out, _ = await run(stage_2_2.reallocation_rules, harnessed)

    assert len(out.rules) == 1
    rule = out.rules[0]
    # brand-us: $4,000 at a $100 CPA => 40 conversions per 30 days.
    assert rule.threshold == 100.0
    assert rule.threshold_basis == "forecast_cpa"
    assert rule.lookback_days == 14.0
    assert rule.cooldown_days == 14.0
    assert rule.max_shift_pct == 20.0
    assert out.review_cadence == "monthly"
    assert set(stage_2_2.RuleDraft.model_fields) == {
        "id",
        "trigger_metric",
        "comparison",
        "threshold_basis",
        "from_campaign",
        "to_campaign",
        "shift_size",
        "requires_human",
        "rationale",
    }


@pytest.mark.asyncio
async def test_2_2_5_bounds_a_shift_by_what_the_donor_can_spare() -> None:
    """brand-us clears tROAS (40 conv over the 50 threshold? no — 40 is under
    50 but over tCPA's 30, so it is `marginal`), and a donor that is not
    clearing gets a human in the loop whether the model asked for one or not.
    """
    harnessed = harness(
        "2.2.5",
        answers={"RulesDraft": RULES_DRAFT},
        outputs={
            "2.1.3": OBJECTIVES,
            "2.2.2": {"campaigns": [{"campaign_ref": "brand-us", "has_revenue_values": True}]},
            "2.2.4": APPROVED,
        },
    )
    out, _ = await run(stage_2_2.reallocation_rules, harnessed)

    rule = out.rules[0]
    assert rule.requires_human is True
    # 40 conversions against a 50-conversion tROAS threshold: the campaign needs
    # every dollar it has, so there is nothing to give.
    assert rule.max_shift_usd == 0.0


@pytest.mark.asyncio
async def test_2_2_5_drops_a_rule_naming_a_campaign_with_no_budget() -> None:
    draft = {
        **RULES_DRAFT,
        "rules": [
            {**RULES_DRAFT["rules"][0], "id": "R2", "to_campaign": "a-campaign-with-no-money"}
        ],
    }
    harnessed = harness(
        "2.2.5",
        answers={"RulesDraft": draft},
        outputs={
            "2.1.3": OBJECTIVES,
            "2.2.2": {"campaigns": []},
            "2.2.4": APPROVED,
        },
    )
    out, _ = await run(stage_2_2.reallocation_rules, harnessed)

    assert out.rules == []
    assert out.rejected[0]["id"] == "R2"
    assert "no approved budget" in out.rejected[0]["reason"]


@pytest.mark.asyncio
async def test_2_2_5_gives_a_donor_with_headroom_something_to_move() -> None:
    generous = {
        **APPROVED,
        "allocation": [
            {**APPROVED["allocation"][0], "usd": 20_000.0},
            APPROVED["allocation"][1],
        ],
    }
    harnessed = harness(
        "2.2.5",
        answers={"RulesDraft": RULES_DRAFT},
        outputs={
            "2.1.3": OBJECTIVES,
            "2.2.2": {"campaigns": [{"campaign_ref": "brand-us", "has_revenue_values": False}]},
            "2.2.4": generous,
        },
    )
    out, _ = await run(stage_2_2.reallocation_rules, harnessed)

    rule = out.rules[0]
    # $20,000 at a $100 CPA is 200 conversions against a 30-conversion tCPA
    # threshold, so the policy bound binds first: 20% of $20,000.
    assert rule.max_shift_usd == 4_000.0
    assert rule.requires_human is False


# ---------------------------------------------------------------------------
# the laws, across the stage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_node_cannot_call_a_formula_it_did_not_declare(stub_collect: Any) -> None:
    stub_collect(found(), forecast_found())
    harnessed = support.harness(
        "2.2.1",
        answers={"ForecastNotes": NOTES},
        gathered=gather.Gathered(),
        source=support.plan_input(report()),
        permitted=(),  # declares nothing
        outputs={"2.1.1": TAXONOMY},
    )
    with pytest.raises(CalcNotPermitted) as caught:
        await stage_2_2.demand_forecast.gather(harnessed.ctx)

    assert stage_2_2.TRAFFIC in str(caught.value)


def test_every_stage_2_2_node_declares_the_formulas_it_calls() -> None:
    """A census, deliberately, not a derivation.

    `NodeSpec.calc` is an allow-list a reviewer reads: an entry nothing calls is
    permission nobody asked for, and a node quietly gaining one is a new number
    in the plan with no argument behind it. Both should fail here and be argued
    in the pull request, which a test that derived the list from the source
    could not make happen.
    """
    declared = {
        node.spec.id: set(node.spec.calc)
        for node in (
            stage_2_2.demand_forecast,
            stage_2_2.learning_capacity_check,
            stage_2_2.budget_scenarios,
            stage_2_2.budget_allocation,
            stage_2_2.reallocation_rules,
        )
    }
    assert declared == {
        # Every node that reads the forecast re-runs it and cites the deduped
        # row, which is why TRAFFIC appears four times and writes once.
        "2.2.1": {stage_2_2.TRAFFIC},
        "2.2.2": {stage_2_2.TRAFFIC, stage_2_2.VOLUME_CHECK},
        "2.2.3": {stage_2_2.TRAFFIC, stage_2_2.ENVELOPE, stage_2_2.SPLIT},
        "2.2.4": {stage_2_2.TRAFFIC, stage_2_2.SPLIT},
        # Against the approved allocation, not the forecast — a different row.
        "2.2.5": {stage_2_2.VOLUME_CHECK},
    }


def test_the_stage_declares_exactly_one_gate() -> None:
    """Stage 02 law 16 is four gates across the whole plan DAG. 2.2 owns one."""
    gates = [
        node.spec
        for node in (
            stage_2_2.demand_forecast,
            stage_2_2.learning_capacity_check,
            stage_2_2.budget_scenarios,
            stage_2_2.budget_allocation,
            stage_2_2.reallocation_rules,
        )
        if node.spec.gate
    ]
    assert [item.gate_key for item in gates] == ["G3"]


@pytest.mark.asyncio
async def test_all_five_nodes_share_one_forecast_within_a_run(stub_collect: Any) -> None:
    """`DerivedWriter` dedupes on `(plan_run_id, formula_id, inputs_hash)`, so a
    reader following a number from the budget gate back to its calculation lands
    on the row 2.2.1 published — not a fourth copy of it."""
    writer = support.StubWriter()
    ids: list[uuid.UUID] = []
    for node_id, node, answers, outputs in (
        ("2.2.1", stage_2_2.demand_forecast, {"ForecastNotes": NOTES}, {"2.1.1": TAXONOMY}),
        (
            "2.2.2",
            stage_2_2.learning_capacity_check,
            {"CapacityDraft": CAPACITY_DRAFT},
            {"2.1.1": TAXONOMY, "2.1.3": OBJECTIVES},
        ),
    ):
        stub_collect(found(), forecast_found())
        harnessed = harness(node_id, answers=answers, outputs=outputs)
        harnessed.ctx.plan.calc.writer = writer
        harnessed.ctx.plan.calc.session = writer
        out, _ = await run(node, harnessed)
        ids.append(out.calc_evidence_ids[0])

    assert ids[0] == ids[1]
    assert sum(1 for call in writer.calls if call.endswith(stage_2_2.TRAFFIC)) == 2
