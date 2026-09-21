"""Stage 2.5 — what the two nodes do with what they were given.

Same property as the Stage 2.1 suite, and the scripted answers are built the
same way: **they contain no figure at all.** A node that got a tolerance, a lag
or a retention window from the model could not have, because there was none in
the answer to get. Every number asserted below is traced back by value to the
`PlanCalc` result the node cited.

On top of that, two controls this stage owns and 2.1 does not:

* **§13 / PC1.** A market gate 1.5.3 refused must not be reachable in 2.5.2's
  consent block through any input. The draft model has no field for it, and
  the tests here assert the output equals the computed scope rather than
  anything the model said.
* **§13, EU consent signals.** An EU market in scope with no named mechanism
  must raise a *blocking* prerequisite out of 2.5.1, because a modelled
  conversion cannot be reconciled to a CRM row.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.nodes import gather
from agent.nodes.base import collect_calc_evidence_ids, derived_ids
from agent.nodes.plan import stage_2_5
from agent.orchestrator.plan_calc import CalcNotPermitted
from agent.planning import crm, tracking
from tests import plan_support as support


@pytest.fixture
def stub_collect(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Replace `gather.collect` with a prepared `Gathered`.

    What these tests are about is what a node does with what it got;
    `tests/integration/test_plan_stage_2_5.py` proves the read itself.
    """

    def install(found: gather.Gathered) -> None:
        async def collect(ctx: Any, *needs: gather.Need) -> gather.Gathered:
            return found

        monkeypatch.setattr(gather, "collect", collect)

    return install


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

MARKETS_US = [{"country": "US", "language": "en", "currency": "USD"}]
MARKETS_EU = [
    {"country": "US", "language": "en", "currency": "USD"},
    {"country": "DE", "language": "de", "currency": "EUR"},
]

#: One usable list with a basis, one refusal. DE is refused on the second and
#: permitted on neither, which is what PC1 is about.
CONSENT_LISTS = [
    {
        "name": "Customers",
        "usable": True,
        "consent_basis": "contract, art. 6(1)(b)",
        "markets_allowed": ["US"],
        "evidence_ids": [],
    },
    {
        "name": "Prospects",
        "usable": False,
        "blocker": "no lawful basis recorded for DE",
        "markets_allowed": ["DE"],
        "evidence_ids": [],
    },
]


def report(**readiness: Any) -> dict[str, Any]:
    return support.research_report(
        business_context={
            "products": [{"name": "SDS Manager", "gross_margin_pct": 80, "evidence_ids": []}]
        },
        readiness=readiness or {},
    )


def source(*, markets: list[dict[str, Any]] | None = None, **readiness: Any) -> Any:
    return support.plan_input(report(**readiness), markets=markets or MARKETS_US)


def action_rows(
    name: str,
    *,
    days: list[tuple[str, float]],
    category: str = "SUBMIT_LEAD_FORM",
    action_type: str = "WEBPAGE",
) -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "conversion_action_id": name.lower(),
            "status": "ENABLED",
            "category": category,
            "action_type": action_type,
            "counting_type": "ONE_PER_CLICK",
            "primary_for_goal": True,
            "date": date,
            "conversions": conversions,
        }
        for date, conversions in days
    ]


#: 40 conversions, last recorded well past the 30-day staleness threshold, so
#: the whole 40 is unreconciled: a measured 100% gap, capped at 40%.
STALE_ACTION = action_rows("Demo request", days=[("2020-01-01", 40.0)])
#: The same action, recently converting: nothing unreconciled, so the floor.
FRESH_ACTION = action_rows("Demo request", days=[("2026-09-20", 40.0)])

TAXONOMY_OUTPUT: dict[str, Any] = {
    "actions": [{"name": "Demo request", "primary": True, "include_in_conversions": True}],
    "status": "ok",
}
LEAD_OUTPUT: dict[str, Any] = {
    "qualified_lead": {"required_signals": ["job title"], "threshold": 3},
    "status": "ok",
}


def gathered_2_5_1(rows: list[dict[str, Any]]) -> gather.Gathered:
    return gather.Gathered(
        evidence=[support.evidence(tracking.CONVERSION_ACTION, row) for row in rows]
    )


def gathered_2_5_2(
    *, actions: list[dict[str, Any]], pages: list[dict[str, Any]] | None = None
) -> gather.Gathered:
    rows = [support.evidence(tracking.CONVERSION_ACTION, row) for row in actions]
    rows.extend(support.evidence(tracking.PAGE, page) for page in pages or [])
    rows.append(support.evidence(crm.CRM_WON, {"account_name": "Acme", "created_at": "2026-08-22"}))
    rows.append(
        support.evidence(crm.CRM_LOST, {"account_name": "Lost", "created_at": "2026-09-01"})
    )
    return gather.Gathered(evidence=rows)


# The model is asked for names, owners and a selection. No figure appears.
MEASUREMENT_ANSWER: dict[str, Any] = {
    "primary_source": "crm",
    "rationale": "The CRM is where a deal becomes real.",
    "metric_definitions": [
        {
            "metric": "conversions",
            "formula": "counted conversion actions",
            "source_field": "metrics.conversions",
            "owner": "growth",
            "refresh": "daily",
        }
    ],
    "reconciliation_owners": [
        {"metric": "cost", "cadence": "monthly", "owner": "finance"},
        {"metric": "conversions", "cadence": "weekly", "owner": "growth"},
        {"metric": "qualified_leads", "cadence": "weekly", "owner": "sales ops"},
    ],
    "known_discrepancies": [
        {
            "metric": "conversions",
            "systems": ["google_ads", "ga4"],
            "cause": "GA4 sessionises differently",
            "tolerated": True,
        }
    ],
    "dashboard_spec": {"fields": ["cost", "conversions"], "grain": "campaign", "cadence": "weekly"},
    "consent_signal_mechanism": None,
}

OFFLINE_ANSWER: dict[str, Any] = {
    "method": "manual_csv",
    "cadence": "monthly",
    "method_rationale": "Nobody owns an API integration yet.",
    "gclid_capture": {
        "point": "landing page query string",
        "form_field": "gclid",
        "storage_object": "Opportunity.gclid__c",
    },
    "stage_map": [
        {
            "crm_stage": "Closed Won",
            "ads_conversion_action": "Closed won",
            "value_field": "Amount",
        }
    ],
    "prerequisites": [{"task": "Agree the CRM field name", "owner": "revops", "blocking": False}],
    "notes": "",
}


async def run_2_5_1(
    stub_collect: Any,
    *,
    rows: list[dict[str, Any]] | None = None,
    answer: dict[str, Any] | None = None,
    markets: list[dict[str, Any]] | None = None,
    **readiness: Any,
) -> tuple[Any, Any, Any]:
    found = gathered_2_5_1(rows if rows is not None else STALE_ACTION)
    stub_collect(found)
    harness = support.harness(
        "2.5.1",
        answers={"MeasurementDraft": answer or MEASUREMENT_ANSWER},
        gathered=found,
        source=source(markets=markets, **readiness),
        permitted=stage_2_5.MeasurementSourceOfTruthNode.spec.calc,
        outputs={"2.1.1": TAXONOMY_OUTPUT},
    )
    node = stage_2_5.measurement_source_of_truth
    evidence = await node.gather(harness.ctx)
    output = await node.reason(harness.ctx, evidence)
    return output, evidence, harness


async def run_2_5_2(
    stub_collect: Any,
    *,
    actions: list[dict[str, Any]] | None = None,
    pages: list[dict[str, Any]] | None = None,
    answer: dict[str, Any] | None = None,
    markets: list[dict[str, Any]] | None = None,
    **readiness: Any,
) -> tuple[Any, Any, Any]:
    found = gathered_2_5_2(actions=actions if actions is not None else FRESH_ACTION, pages=pages)
    stub_collect(found)
    harness = support.harness(
        "2.5.2",
        answers={"OfflinePlanDraft": answer or OFFLINE_ANSWER},
        gathered=found,
        source=source(markets=markets, **readiness),
        permitted=stage_2_5.OfflineConversionPlanNode.spec.calc,
        outputs={"2.1.1": TAXONOMY_OUTPUT, "2.1.4": LEAD_OUTPUT},
    )
    node = stage_2_5.offline_conversion_plan
    evidence = await node.gather(harness.ctx)
    output = await node.reason(harness.ctx, evidence)
    return output, evidence, harness


def computed(harness: Any, formula_id: str) -> dict[str, Any]:
    """The formula result the node actually persisted."""
    row = next(item for key, item in harness.writer.rows.items() if key[0] == formula_id)
    return dict(row.payload["result"])


# ---------------------------------------------------------------------------
# 2.5.1 — measurement_source_of_truth
# ---------------------------------------------------------------------------


async def test_the_dag_edges_are_the_ones_prd_11_declares() -> None:
    assert stage_2_5.MeasurementSourceOfTruthNode.spec.depends_on == ("2.1.1",)
    assert stage_2_5.OfflineConversionPlanNode.spec.depends_on == ("2.1.1", "2.1.4")
    # Neither is a gate: §11 puts G1..G4 on 2.1.3, 2.1.4, 2.2.4 and 2.3.1.
    assert stage_2_5.MeasurementSourceOfTruthNode.spec.gate is False
    assert stage_2_5.OfflineConversionPlanNode.spec.gate is False


async def test_every_tolerance_comes_from_the_calculation(stub_collect: Any) -> None:
    output, _, harness = await run_2_5_1(stub_collect)
    result = computed(harness, "measurement.reconciliation_v1")
    by_metric = {row["metric"]: row for row in result["by_metric"]}
    assert output.reconciliation
    for rule in output.reconciliation:
        assert rule.tolerance_pct == by_metric[rule.metric]["tolerance_pct"]
        assert rule.binding_driver == by_metric[rule.metric]["binding_driver"]


async def test_a_stale_action_widens_the_tolerance_to_the_cap(stub_collect: Any) -> None:
    output, _, _ = await run_2_5_1(stub_collect, rows=STALE_ACTION)
    conversions = next(row for row in output.reconciliation if row.metric == "conversions")
    assert conversions.observed_gap_pct == 100.0
    assert conversions.tolerance_pct == 40.0
    assert conversions.binding_driver == "unreconciled_volume"


async def test_a_healthy_account_reconciles_at_the_floor(stub_collect: Any) -> None:
    output, _, _ = await run_2_5_1(stub_collect, rows=FRESH_ACTION)
    conversions = next(row for row in output.reconciliation if row.metric == "conversions")
    assert conversions.observed_gap_pct == 0.0
    assert conversions.tolerance_pct == 5.0
    assert conversions.binding_driver == "floor"


async def test_the_model_annotates_the_metric_set_it_cannot_choose(stub_collect: Any) -> None:
    output, _, harness = await run_2_5_1(stub_collect)
    computed_metrics = [
        row["metric"] for row in computed(harness, "measurement.reconciliation_v1")["by_metric"]
    ]
    assert [rule.metric for rule in output.reconciliation] == computed_metrics
    owners = {rule.metric: rule.owner for rule in output.reconciliation}
    assert owners["cost"] == "finance"
    assert owners["conversions"] == "growth"


async def test_an_unannotated_metric_keeps_its_tolerance_and_names_the_gap(
    stub_collect: Any,
) -> None:
    answer = {**MEASUREMENT_ANSWER, "reconciliation_owners": []}
    output, _, _ = await run_2_5_1(stub_collect, answer=answer)
    assert all(rule.owner == "unassigned" for rule in output.reconciliation)
    assert all(rule.cadence is None for rule in output.reconciliation)
    assert any("no owner or cadence" in gap for gap in output.open_gaps)


async def test_an_owner_for_a_metric_nobody_reconciles_is_named(stub_collect: Any) -> None:
    answer = {
        **MEASUREMENT_ANSWER,
        "reconciliation_owners": [{"metric": "impressions", "cadence": "daily", "owner": "growth"}],
    }
    output, _, _ = await run_2_5_1(stub_collect, answer=answer)
    assert "impressions" not in {rule.metric for rule in output.reconciliation}
    assert any("impressions" in gap for gap in output.open_gaps)


async def test_observed_discrepancies_are_not_tolerated_and_are_marked_observed(
    stub_collect: Any,
) -> None:
    output, _, _ = await run_2_5_1(stub_collect, rows=STALE_ACTION)
    observed = [item for item in output.known_discrepancies if item.observed]
    assert observed, "a stale action should surface as an observed divergence"
    assert all(item.tolerated is False for item in observed)
    # The model's own contribution survives alongside, with its own judgement.
    proposed = [item for item in output.known_discrepancies if not item.observed]
    assert [item.cause for item in proposed] == ["GA4 sessionises differently"]
    assert proposed[0].tolerated is True


async def test_an_eu_market_without_a_named_signal_blocks(stub_collect: Any) -> None:
    output, _, _ = await run_2_5_1(stub_collect, markets=MARKETS_EU)
    assert output.consent_signal is not None
    assert output.consent_signal.required is True
    assert output.consent_signal.markets == ["DE"]
    assert output.consent_signal.mechanism is None
    blocking = [item for item in output.prerequisites if item.blocking]
    assert len(blocking) == 1
    assert "consent-signal mechanism" in blocking[0].task
    assert "DE" in blocking[0].task


async def test_an_eu_market_with_a_named_signal_does_not_block(stub_collect: Any) -> None:
    answer = {**MEASUREMENT_ANSWER, "consent_signal_mechanism": "Consent Mode v2 via Cookiebot"}
    output, _, _ = await run_2_5_1(stub_collect, answer=answer, markets=MARKETS_EU)
    assert output.consent_signal is not None
    assert output.consent_signal.mechanism == "Consent Mode v2 via Cookiebot"
    assert output.prerequisites == []


async def test_a_blank_mechanism_is_not_a_mechanism(stub_collect: Any) -> None:
    answer = {**MEASUREMENT_ANSWER, "consent_signal_mechanism": "   "}
    output, _, _ = await run_2_5_1(stub_collect, answer=answer, markets=MARKETS_EU)
    assert output.consent_signal is not None
    assert output.consent_signal.mechanism is None
    assert [item.blocking for item in output.prerequisites] == [True]


async def test_no_eu_market_asks_for_no_signal(stub_collect: Any) -> None:
    output, _, _ = await run_2_5_1(stub_collect, markets=MARKETS_US)
    assert output.consent_signal is not None
    assert output.consent_signal.required is False
    assert output.prerequisites == []


async def test_the_modelled_allowance_only_applies_in_eu_markets(stub_collect: Any) -> None:
    """8% would be the measured gap; in the EU the 20% allowance is wider."""
    rows = [
        *action_rows("Demo request", days=[("2026-09-20", 46.0)]),
        *action_rows("Old thing", days=[("2020-01-01", 4.0)], category="PAGE_VIEW"),
    ]
    inside, _, _ = await run_2_5_1(stub_collect, rows=rows, markets=MARKETS_EU)
    outside, _, _ = await run_2_5_1(stub_collect, rows=rows, markets=MARKETS_US)
    eu_rule = next(row for row in inside.reconciliation if row.metric == "conversions")
    us_rule = next(row for row in outside.reconciliation if row.metric == "conversions")
    assert eu_rule.observed_gap_pct == us_rule.observed_gap_pct == 8.0
    assert eu_rule.tolerance_pct == 20.0
    assert eu_rule.binding_driver == "modelled_conversions"
    assert us_rule.tolerance_pct == 8.0


async def test_the_citation_resolves_to_a_row_the_node_produced(stub_collect: Any) -> None:
    output, evidence, _ = await run_2_5_1(stub_collect)
    payload = output.model_dump(mode="json")
    assert collect_calc_evidence_ids(payload) <= derived_ids(evidence)
    assert len(output.calc_evidence_ids) == 1


async def test_the_node_may_not_call_a_formula_it_did_not_declare(stub_collect: Any) -> None:
    found = gathered_2_5_1(FRESH_ACTION)
    stub_collect(found)
    harness = support.harness(
        "2.5.1",
        answers={"MeasurementDraft": MEASUREMENT_ANSWER},
        gathered=found,
        source=source(),
        permitted=("economics.max_cpa_v1",),
        outputs={"2.1.1": TAXONOMY_OUTPUT},
    )
    with pytest.raises(CalcNotPermitted, match="measurement.reconciliation_v1"):
        await stage_2_5.measurement_source_of_truth.gather(harness.ctx)


async def test_an_account_with_no_conversion_actions_still_plans(stub_collect: Any) -> None:
    output, _, _ = await run_2_5_1(stub_collect, rows=[])
    assert output.reconciliation
    assert all(rule.tolerance_pct == 5.0 for rule in output.reconciliation)
    assert any("conversion_action" in gap for gap in output.open_gaps)


# ---------------------------------------------------------------------------
# 2.5.2 — offline_conversion_plan
# ---------------------------------------------------------------------------


async def test_every_day_count_comes_from_the_calculation(stub_collect: Any) -> None:
    output, _, harness = await run_2_5_2(stub_collect)
    result = computed(harness, "measurement.upload_window_v1")
    chosen = next(
        row
        for row in result["options"]
        if row["method"] == "manual_csv" and row["cadence"] == "monthly"
    )
    assert output.upload is not None
    assert output.upload.lag_days == chosen["lag_days"] == 32.0
    assert output.upload.backfill_days == chosen["backfill_days"]
    assert output.upload.headroom_days == chosen["headroom_days"]
    assert output.upload.click_upload_window_days == 90.0
    assert output.gclid_capture is not None
    assert output.gclid_capture.retention_days == chosen["retention_days"]


async def test_the_crm_history_bounds_the_backfill(stub_collect: Any) -> None:
    """The oldest CRM row is 2026-08-22; Google would allow 90 days."""
    output, _, _ = await run_2_5_2(stub_collect)
    assert output.upload is not None
    assert output.upload.backfill_days < 90.0
    assert output.upload.history_limits_backfill is True


async def test_an_unselectable_path_falls_back_to_the_honest_floor(stub_collect: Any) -> None:
    """`manual_csv` + `daily` is a pair the Literals allow and the table does not.

    The structured-output schema already stops a made-up cadence, so the only
    way this branch is reachable is a *valid* pair `UPLOAD_PATHS` withholds —
    which is the case it exists for: nobody exports a CSV every morning.
    """
    answer = {**OFFLINE_ANSWER, "method": "manual_csv", "cadence": "daily"}
    output, _, _ = await run_2_5_2(stub_collect, answer=answer)
    assert output.upload is not None
    assert (output.upload.method, output.upload.cadence) == ("manual_csv", "monthly")
    assert any("not one of the computed upload options" in gap for gap in output.open_gaps)


async def test_a_selected_path_is_honoured(stub_collect: Any) -> None:
    answer = {**OFFLINE_ANSWER, "method": "ads_api", "cadence": "daily"}
    output, _, _ = await run_2_5_2(stub_collect, answer=answer, lists=[CONSENT_LISTS[0]])
    assert output.upload is not None
    assert (output.upload.method, output.upload.cadence) == ("ads_api", "daily")
    assert output.upload.lag_days == 1.0
    assert output.open_gaps == []


async def test_consent_is_computed_and_a_refused_market_never_appears(
    stub_collect: Any,
) -> None:
    output, _, _ = await run_2_5_2(stub_collect, markets=MARKETS_EU, lists=CONSENT_LISTS)
    assert output.consent is not None
    assert output.consent.markets_allowed == ["US"]
    assert output.consent.markets_blocked == ["DE"]
    assert output.consent.basis == ["Customers: contract, art. 6(1)(b)"]
    # PC1: the refusal reaches the plan as a blocking prerequisite too.
    assert any(
        item.blocking and "no lawful basis recorded for DE" in item.task
        for item in output.prerequisites
    )


async def test_the_model_is_given_no_field_that_could_widen_consent() -> None:
    """The structural half of PC1: the draft cannot express an allowed market."""
    fields = set(stage_2_5.OfflinePlanDraft.model_fields)
    assert not fields & {"consent", "markets_allowed", "markets_blocked"}


async def test_no_lawful_basis_anywhere_blocks_the_whole_plan(stub_collect: Any) -> None:
    output, _, _ = await run_2_5_2(stub_collect, markets=MARKETS_EU)
    assert output.consent is not None
    assert output.consent.markets_allowed == []
    assert any("Record a lawful basis" in item.task for item in output.prerequisites)
    assert any("gate 1.5.3 never mentioned" in gap for gap in output.open_gaps)


async def test_a_missing_click_id_is_the_first_blocking_prerequisite(
    stub_collect: Any,
) -> None:
    output, _, _ = await run_2_5_2(stub_collect, pages=[{"form_fields": ["email"]}])
    assert output.gclid_capture is not None
    assert output.gclid_capture.present_today is False
    assert output.gclid_capture.caveat is not None
    assert output.prerequisites[0].blocking is True
    assert "Capture the Google click id" in output.prerequisites[0].task
    assert "Opportunity.gclid__c" in output.prerequisites[0].task


async def test_an_account_already_uploading_raises_no_click_id_prerequisite(
    stub_collect: Any,
) -> None:
    actions = [
        *FRESH_ACTION,
        *action_rows("Closed won", days=[("2026-09-19", 3.0)], action_type="UPLOAD_CLICKS"),
    ]
    output, _, _ = await run_2_5_2(
        stub_collect,
        actions=actions,
        lists=[CONSENT_LISTS[0]],
    )
    assert output.gclid_capture is not None
    assert output.gclid_capture.present_today is True
    assert not any("Capture the Google click id" in item.task for item in output.prerequisites)


async def test_the_models_own_prerequisites_survive(stub_collect: Any) -> None:
    output, _, _ = await run_2_5_2(stub_collect, lists=[CONSENT_LISTS[0]])
    assert any(item.task == "Agree the CRM field name" for item in output.prerequisites)
    assert not any(
        item.task == "Agree the CRM field name" and item.blocking for item in output.prerequisites
    )


async def test_the_stage_map_is_the_models_to_write(stub_collect: Any) -> None:
    output, _, _ = await run_2_5_2(stub_collect)
    assert [item.crm_stage for item in output.stage_map] == ["Closed Won"]
    assert output.stage_map[0].value_field == "Amount"


async def test_the_citation_resolves_to_a_row_2_5_2_produced(stub_collect: Any) -> None:
    output, evidence, _ = await run_2_5_2(stub_collect)
    payload = output.model_dump(mode="json")
    assert collect_calc_evidence_ids(payload) <= derived_ids(evidence)
    assert len(output.calc_evidence_ids) == 1


async def test_no_crm_date_bounds_the_backfill_by_the_window_alone(
    stub_collect: Any,
) -> None:
    found = gather.Gathered(
        evidence=[
            *(support.evidence(tracking.CONVERSION_ACTION, row) for row in FRESH_ACTION),
            support.evidence(crm.CRM_WON, {"account_name": "Acme"}),
        ]
    )
    stub_collect(found)
    harness = support.harness(
        "2.5.2",
        answers={"OfflinePlanDraft": OFFLINE_ANSWER},
        gathered=found,
        source=source(),
        permitted=stage_2_5.OfflineConversionPlanNode.spec.calc,
        outputs={"2.1.1": TAXONOMY_OUTPUT, "2.1.4": LEAD_OUTPUT},
    )
    node = stage_2_5.offline_conversion_plan
    output = await node.reason(harness.ctx, await node.gather(harness.ctx))
    assert output.upload is not None
    assert output.upload.backfill_days == 90.0
    assert output.upload.history_limits_backfill is False
    assert any("created_at" in gap for gap in output.open_gaps)


# ---------------------------------------------------------------------------
# the prompts
# ---------------------------------------------------------------------------


async def test_the_prompt_shows_the_tolerances_and_forbids_restating_them(
    stub_collect: Any,
) -> None:
    _, _, harness = await run_2_5_1(stub_collect)
    prompt = harness.llm.user_prompt("MeasurementDraft")
    assert "annotate, never restate" in prompt
    assert "tolerance_pct" in prompt


async def test_the_prompt_tells_the_model_the_consent_scope_is_final(
    stub_collect: Any,
) -> None:
    _, _, harness = await run_2_5_2(stub_collect, markets=MARKETS_EU, lists=CONSENT_LISTS)
    prompt = harness.llm.user_prompt("OfflinePlanDraft")
    assert "you may not widen this" in prompt
    assert "Plan nothing for a market listed as blocked" in prompt


# ---------------------------------------------------------------------------
# 2.5.3 — experiment_backlog
# ---------------------------------------------------------------------------

#: Two campaigns with round forecasts, so every figure below is checkable:
#: brand     960 conv / 12,000 clicks = 8.0% baseline, 400 clicks a day
#: nonbrand  600 conv / 12,000 clicks = 5.0% baseline, 400 clicks a day
#:
#: The volumes are deliberately high. At B2B conversion volumes almost nothing
#: reaches significance inside two quarters — which is a true and useful thing
#: for the node to report, and is pinned by its own test below — but a fixture
#: where every test is refused on the horizon can never exercise the funding
#: path. 5.0% is `tests/test_calc_power.py`'s anchor case: 8,158 visitors per
#: arm, so the costs below are checkable against it.
STRUCTURE_OUTPUT: dict[str, Any] = {
    "campaigns": [
        {
            "campaign_ref": "us-brand",
            "name": "US | Search | Brand",
            "market": "US",
            "monthly_budget_usd": 4_000.0,
            "bid_strategy": "manual_cpc",
            "locations": ["United States"],
            "ad_groups": [{"name": "brand core", "landing_url": "https://example.com/"}],
        },
        {
            "campaign_ref": "us-nonbrand",
            "name": "US | Search | Non-brand",
            "market": "US",
            "monthly_budget_usd": 9_000.0,
            "bid_strategy": "max_conv",
            "locations": ["United States", "Canada"],
            "ad_groups": [{"name": "sds software", "landing_url": "https://example.com/sds"}],
        },
    ]
}

ALLOCATION_OUTPUT: dict[str, Any] = {
    "experiment_reserve_usd": 20_000.0,
    "allocation": [
        {
            "campaign_ref": "us-brand",
            "market": "US",
            "usd": 4_000.0,
            "est_conv": 960.0,
            "est_clicks": 12_000.0,
            "avg_cpc_usd": 0.50,
        },
        {
            "campaign_ref": "us-nonbrand",
            "market": "US",
            "usd": 9_000.0,
            "est_conv": 600.0,
            "est_clicks": 12_000.0,
            "avg_cpc_usd": 0.75,
            "cap_applied": True,
        },
    ],
}

CAPACITY_OUTPUT: dict[str, Any] = {
    "campaigns": [
        {"campaign_ref": "us-brand", "verdict": "clears", "forecast_clicks_30d": 12_000.0},
        {
            "campaign_ref": "us-nonbrand",
            "verdict": "marginal",
            "remedy": "switch_strategy",
            "bid_strategy_recommended": "tcpa",
            "forecast_clicks_30d": 12_000.0,
        },
    ]
}

SLATE_OUTPUT: dict[str, Any] = {
    "slate": [
        {"campaign_type": "search", "campaign_refs": ["us-brand"], "launch_wave": 1},
        {"campaign_type": "search", "campaign_refs": ["us-nonbrand"], "launch_wave": 1},
    ]
}

BOUNDARIES_OUTPUT: dict[str, Any] = {
    "broad_match": {"allowed_campaigns": ["us-nonbrand"], "guardrails": []}
}

#: No figure anywhere. A sample size, a score or a reserve in the output can
#: only have come from `calc/`, because there was none here to copy.
BACKLOG_ANSWER: dict[str, Any] = {
    "ratings": [
        {
            "id": "us-nonbrand:bid_strategy",
            "hypothesis": "If we move to tCPA then CPA falls, because the campaign is marginal.",
            "impact_1_5": 5,
            "confidence_1_5": 4,
            "effort_1_5": 1,
        },
        {
            "id": "us-nonbrand:budget",
            "hypothesis": "If we lift the cap then volume rises, because demand is unabsorbed.",
            "impact_1_5": 3,
            "confidence_1_5": 3,
            "effort_1_5": 3,
        },
        {
            "id": "us-nonbrand:match_type",
            "hypothesis": "If we add broad match then reach grows, because exact caps it.",
            "impact_1_5": 3,
            "confidence_1_5": 2,
            "effort_1_5": 2,
        },
        {
            "id": "us-nonbrand:landing_page",
            "hypothesis": "If we rebuild the page then CVR rises, because it buries the proof.",
            "impact_1_5": 4,
            "confidence_1_5": 3,
            "effort_1_5": 5,
        },
        {
            "id": "us-nonbrand:geo",
            "hypothesis": "If we split Canada out then CPA falls, because it is cheaper.",
            "impact_1_5": 2,
            "confidence_1_5": 3,
            "effort_1_5": 2,
        },
        {
            "id": "us-brand:landing_page",
            "hypothesis": "If we shorten the brand page then CVR rises, because intent is high.",
            "impact_1_5": 2,
            "confidence_1_5": 4,
            "effort_1_5": 3,
        },
        {
            "id": "us-brand:ad_schedule",
            "hypothesis": "If we bid up business hours then CPA falls, because sales answer then.",
            "impact_1_5": 2,
            "confidence_1_5": 2,
            "effort_1_5": 1,
        },
    ],
    "notes": "Assumes the forecast holds through wave 1.",
}


async def run_2_5_3(
    *,
    answer: dict[str, Any] | None = None,
    structure: dict[str, Any] | None = None,
    allocation: dict[str, Any] | None = None,
    capacity: dict[str, Any] | None = None,
    slate: dict[str, Any] | None = None,
    boundaries: dict[str, Any] | None = None,
    markets: list[dict[str, Any]] | None = None,
    **readiness: Any,
) -> tuple[Any, Any, Any]:
    harness = support.harness(
        "2.5.3",
        answers={"ExperimentDraft": answer or BACKLOG_ANSWER},
        gathered=gather.Gathered(),
        source=source(markets=markets, **readiness),
        permitted=stage_2_5.ExperimentBacklogNode.spec.calc,
        outputs={
            "2.4.2": structure if structure is not None else STRUCTURE_OUTPUT,
            "2.2.4": allocation if allocation is not None else ALLOCATION_OUTPUT,
            "2.2.2": capacity if capacity is not None else CAPACITY_OUTPUT,
            "2.3.1": slate if slate is not None else SLATE_OUTPUT,
            "2.3.2": boundaries if boundaries is not None else BOUNDARIES_OUTPUT,
        },
    )
    node = stage_2_5.experiment_backlog
    evidence = await node.gather(harness.ctx)
    output = await node.reason(harness.ctx, evidence)
    return output, evidence, harness


def backlog_item(output: Any, identifier: str) -> Any:
    """One ranked test by id. Not named `test_` — pytest would collect it."""
    return next(item for item in output.tests if item.id == identifier)


async def test_2_5_3_declares_the_edges_prd_11_gives_it_plus_two_it_argues_for() -> None:
    spec = stage_2_5.ExperimentBacklogNode.spec
    # §11: 2.5.3←{2.4.2, 2.2.4, 2.3.1}. 2.2.2 and 2.3.2 are the two the module
    # docstring argues for; 2.5.2 is deliberately absent so G2 cannot hold it.
    assert set(spec.depends_on) == {"2.4.2", "2.2.4", "2.3.1", "2.2.2", "2.3.2"}
    assert "2.5.2" not in spec.depends_on
    assert spec.gate is False
    assert spec.connectors == ()


async def test_the_backlog_is_ranked_by_ice_not_by_the_order_it_was_given() -> None:
    output, _, _ = await run_2_5_3()
    ranks = [item.rank for item in output.tests]
    assert ranks == sorted(ranks)
    # 5 x 4 / 1 = 20, the highest score in the answer.
    assert output.tests[0].id == "us-nonbrand:bid_strategy"
    assert output.tests[0].ice_score == 20.0
    assert output.tests[0].rank == 1


async def test_every_size_comes_from_the_power_formula_not_the_model() -> None:
    output, _, harness = await run_2_5_3()
    sized = computed(harness, "power.sample_size_v1")
    by_id = {row["id"]: row for row in sized["tests"]}
    for item in output.tests:
        assert item.required_conv_per_arm == by_id[item.id]["required_conv_per_arm"]
        assert item.required_visitors_per_arm == by_id[item.id]["required_visitors_per_arm"]
        assert item.est_days_to_significance == by_id[item.id]["est_days_to_significance"]
    assert output.alpha == sized["alpha"]
    assert output.power == sized["power"]


async def test_the_baseline_is_the_campaigns_own_forecast() -> None:
    output, _, _ = await run_2_5_3()
    assert backlog_item(output, "us-brand:landing_page").baseline == 8.0
    assert backlog_item(output, "us-nonbrand:budget").baseline == 5.0


async def test_the_reserve_funds_down_the_ranking_and_stops() -> None:
    output, _, harness = await run_2_5_3()
    ranked = computed(harness, "experiments.ice_rank_v1")
    assert output.reserve_pool_usd == 20_000.0
    assert output.reserve_committed_usd == ranked["reserve_committed_usd"]
    # The highest-ICE test is funded first; 5 x 4 / 1 = 20 and it costs
    # 8,158 x 2 arms x $0.75 = $12,237.
    top = output.tests[0]
    assert top.id == "us-nonbrand:bid_strategy"
    assert top.funded is True
    assert top.reserve_usd == 12_237.00
    # The reserve is a budget, not a ration: what it funds is exactly what it
    # could afford at that rank, and the committed total is their sum.
    funded = [item for item in output.tests if item.funded]
    assert output.reserve_committed_usd == pytest.approx(sum(item.reserve_usd for item in funded))
    assert output.reserve_committed_usd <= output.reserve_pool_usd
    assert 0 < len(funded) < len(output.tests), "this fixture exercises both sides of the line"


async def test_an_unfunded_test_reserves_nothing_and_appears_in_not_yet() -> None:
    output, _, _ = await run_2_5_3()
    unfunded = [item for item in output.tests if not item.funded]
    assert unfunded, "this fixture is meant to exhaust the reserve"
    for item in unfunded:
        assert item.reserve_usd == 0.0
    blocked = {row.test for row in output.not_yet}
    assert blocked, "an unfunded test must say why"


async def test_the_sum_of_the_reserves_never_exceeds_the_pool() -> None:
    output, _, _ = await run_2_5_3()
    assert sum(item.reserve_usd for item in output.tests) <= output.reserve_pool_usd + 0.01


async def test_every_number_resolves_to_a_derived_row_the_node_produced() -> None:
    output, evidence, harness = await run_2_5_3()
    cited = collect_calc_evidence_ids(output.model_dump(mode="json"))
    assert len(cited) == 2, "the sizing row and the ranking row, and nothing else"
    # The sizing row comes back from `gather()`. The ranking row cannot: what
    # to rank depends on what the model rated, so it is produced in `reason()`
    # and folded into the citable set by the executor from
    # `ctx.plan.calc.evidence`. Both are `derived` rows this node produced.
    assert derived_ids(evidence) <= cited
    assert cited == {item.evidence.id for item in harness.ctx.plan.calc.made}


async def test_an_unrated_candidate_is_carried_neutral_rather_than_dropped() -> None:
    thin = {"ratings": BACKLOG_ANSWER["ratings"][:1], "notes": ""}
    output, _, _ = await run_2_5_3(answer=thin)
    unrated = backlog_item(output, "us-brand:ad_schedule")
    assert unrated.impact_1_5 == stage_2_5.NEUTRAL_RATING
    assert unrated.confidence_1_5 == stage_2_5.NEUTRAL_RATING
    assert unrated.effort_1_5 == stage_2_5.NEUTRAL_RATING
    assert unrated.hypothesis.startswith("Untested")
    assert any("unrated" in gap for gap in output.open_gaps)


async def test_a_rating_for_a_candidate_that_does_not_exist_is_ignored_and_named() -> None:
    invented = {
        "ratings": [
            *BACKLOG_ANSWER["ratings"],
            {
                "id": "ghost:landing_page",
                "hypothesis": "If we test a campaign that does not exist, nothing happens.",
                "impact_1_5": 5,
                "confidence_1_5": 5,
                "effort_1_5": 1,
            },
        ],
        "notes": "",
    }
    output, _, _ = await run_2_5_3(answer=invented)
    assert not any(item.id == "ghost:landing_page" for item in output.tests)
    assert any("never raised" in gap for gap in output.open_gaps)


async def test_no_experiment_reserve_ranks_everything_and_funds_none() -> None:
    poor = dict(ALLOCATION_OUTPUT, experiment_reserve_usd=0.0)
    output, _, _ = await run_2_5_3(allocation=poor)
    assert output.tests, "a zero reserve still produces a ranked backlog"
    assert not any(item.funded for item in output.tests)
    assert any("no experiment reserve" in gap for gap in output.open_gaps)


async def test_a_plan_with_no_forecast_stops_rather_than_emitting_an_uncited_backlog() -> None:
    blind = {"experiment_reserve_usd": 1_000.0, "allocation": []}
    with pytest.raises(stage_2_5.InsufficientBacklog, match="could not size a single test"):
        await run_2_5_3(allocation=blind, capacity={"campaigns": []})


async def test_the_model_is_shown_the_candidates_and_never_a_score() -> None:
    _, _, harness = await run_2_5_3()
    prompt = harness.llm.user_prompt("ExperimentDraft")
    assert "us-nonbrand:bid_strategy" in prompt
    assert "why_this_is_in_question" in prompt
    for forbidden in ("ice_score", "reserve_usd", "rank"):
        assert forbidden not in prompt


async def test_the_model_never_calls_a_formula_outside_its_allow_list() -> None:
    harness = support.harness(
        "2.5.3",
        answers={"ExperimentDraft": BACKLOG_ANSWER},
        gathered=gather.Gathered(),
        source=source(),
        permitted=("economics.max_cpa_v1",),
        outputs={
            "2.4.2": STRUCTURE_OUTPUT,
            "2.2.4": ALLOCATION_OUTPUT,
            "2.2.2": CAPACITY_OUTPUT,
            "2.3.1": SLATE_OUTPUT,
            "2.3.2": BOUNDARIES_OUTPUT,
        },
    )
    with pytest.raises(CalcNotPermitted, match="power.sample_size_v1"):
        await stage_2_5.experiment_backlog.gather(harness.ctx)


async def test_the_consent_gate_bounds_the_audience_test_not_the_model() -> None:
    # PC1. DE is refused by gate 1.5.3 in `CONSENT_LISTS`, so even with a
    # remarketing channel on the slate no audience test may name it.
    remarketing = {
        "slate": [
            *SLATE_OUTPUT["slate"],
            {
                "campaign_type": "display_remarketing",
                "campaign_refs": ["de-rmkt"],
                "launch_wave": 2,
            },
        ]
    }
    output, _, _ = await run_2_5_3(slate=remarketing, markets=MARKETS_EU, lists=CONSENT_LISTS)
    audience = [item for item in output.tests if item.variable == "audience"]
    assert all(item.market != "DE" for item in audience)


async def test_a_low_traffic_campaign_is_told_it_cannot_test_rather_than_flattered() -> None:
    """The finding that makes this node worth having.

    At ordinary B2B volumes — 120 conversions on 3,000 clicks a month — a 20%
    relative lift needs 10,316 visitors per arm, which at 100 clicks a day is
    207 days. Past the 180-day horizon, so nothing is funded and every test
    says why. A backlog that promised a readout in three weeks here would be
    worse than no backlog, because somebody would plan the quarter around it.
    """
    thin_allocation = {
        "experiment_reserve_usd": 50_000.0,
        "allocation": [
            {
                "campaign_ref": "us-brand",
                "market": "US",
                "usd": 4_000.0,
                "est_conv": 120.0,
                "est_clicks": 3_000.0,
                "avg_cpc_usd": 1.0,
            }
        ],
    }
    output, _, _ = await run_2_5_3(
        allocation=thin_allocation,
        structure={"campaigns": STRUCTURE_OUTPUT["campaigns"][:1]},
        capacity={"campaigns": CAPACITY_OUTPUT["campaigns"][:1]},
    )
    assert output.tests, "the backlog still ranks them — it just cannot fund them"
    assert not any(item.funded for item in output.tests)
    assert all(item.reserve_usd == 0.0 for item in output.tests)
    assert output.reserve_committed_usd == 0.0
    # And the reason is the horizon, not the money: the reserve is $50,000.
    assert any("180-day horizon" in row.blocked_by for row in output.not_yet)
    assert all(item.est_days_to_significance > 180 for item in output.tests)
