"""The demand frame, and the roll-ups the budget is built on.

`planning/demand.py` is the Stage 2.2 counterpart of `planning/crm.py`: it turns
Stage 01's keyword list into the frame `forecast.traffic_v1` eats, and rolls a
forecast up into the frames `structure.volume_check_v1` and
`allocation.split_v1` read. Nothing in it produces a figure the plan asserts —
every number here becomes a formula's *input*, recorded verbatim in
`PlanCalc.inputs` and hashed into `inputs_hash`.

Which is exactly why it is worth testing hard. An observation that is wrong here
is a calculation that is right about the wrong thing, and no downstream
assertion would catch it.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.export.contract import DemandMap, KeywordPageMapping, PricedKeyword
from agent.planning import demand
from agent.planning.constants import PlanningConstants, load_planning_constants

CONSTANTS: PlanningConstants = load_planning_constants()

SDS_URL = "https://sdsmanager.test/sds"
BLOG_URL = "https://sdsmanager.test/blog"

MAPPING = DemandMap(
    total_keywords=3,
    mapping=[
        KeywordPageMapping(term_cluster="SDS management", best_url=SDS_URL, verdict="good_fit"),
        KeywordPageMapping(term_cluster="SDS education", best_url=BLOG_URL, verdict="good_fit"),
    ],
)

JANUARY_HEAVY = [2.0, *([1.0] * 11)]


def keyword(**overrides: Any) -> PricedKeyword:
    return PricedKeyword(
        **{
            "term": "sds software",
            "market": "US",
            "volume": 1_000,
            "cpc_low": 8.0,
            "cpc_high": 12.0,
            "best_url": SDS_URL,
            **overrides,
        }
    )


def frame(*keywords: PricedKeyword, **kwargs: Any) -> demand.DemandBasis:
    return demand.demand_frame(
        priced_keywords=list(keywords),
        demand_map=kwargs.pop("demand_map", MAPPING),
        constants=CONSTANTS,
        **kwargs,
    )


def rows(basis: demand.DemandBasis) -> list[dict[str, Any]]:
    return basis.frame.to_dict(orient="records")


# ---------------------------------------------------------------------------
# clustering
# ---------------------------------------------------------------------------


def test_the_cluster_is_the_researchs_own_joined_through_the_url() -> None:
    """Not a second clustering. Two answers to "which cluster is this term in"
    — one on the demand map and one on the media plan — is the most expensive
    duplication available here."""
    basis = frame(
        keyword(term="sds software"),
        keyword(term="what is an sds", best_url=BLOG_URL, volume=500),
    )

    assert basis.clusters == ("SDS education", "SDS management")


def test_an_unmapped_url_falls_back_to_the_funnel_stage_then_the_intent() -> None:
    basis = frame(
        keyword(term="a", best_url="https://elsewhere.test/x", funnel_stage="mofu"),
        keyword(term="b", best_url=None, intent="transactional", volume=10),
        keyword(term="c", best_url=None, volume=10),
    )

    assert set(basis.clusters) == {"mofu", "transactional", demand.UNCLUSTERED}


def test_a_term_is_never_silently_dropped() -> None:
    basis = frame(keyword(term="a", volume=10), keyword(term="b", volume=20))

    assert basis.keywords_used == 2
    assert basis.keywords_dropped == 0


def test_a_zero_volume_term_is_excluded_and_counted() -> None:
    basis = frame(keyword(term="a", volume=1_000), keyword(term="b", volume=0))

    assert basis.keywords_used == 1
    assert basis.keywords_dropped == 1
    assert any("no search volume" in note for note in basis.notes)


# ---------------------------------------------------------------------------
# months
# ---------------------------------------------------------------------------


def test_no_seasonality_anywhere_gives_one_typical_month() -> None:
    basis = frame(keyword())

    assert basis.months == (demand.FLAT_MONTH,)
    assert basis.seasonal is False
    assert len(rows(basis)) == 1


def test_a_seasonality_index_gives_twelve_calendar_months_in_order() -> None:
    """Calendar months, not dates. PT3 wants identical inputs to give
    byte-identical output, and a forecast labelled with the months since today
    is a different plan every morning."""
    basis = frame(keyword(seasonality_index=JANUARY_HEAVY))

    assert basis.months == demand.MONTHS
    assert [row["month"] for row in rows(basis)] == list(demand.MONTHS)
    assert rows(basis)[0]["seasonality_index"] == 2.0
    assert rows(basis)[1]["seasonality_index"] == 1.0


def test_each_month_reads_its_own_multiplier() -> None:
    """The defect this catches is a closure over a loop variable: without the
    binding every month in the year reads December's, which no single-month
    fixture could show."""
    stepped = [float(index + 1) for index in range(12)]
    basis = frame(keyword(seasonality_index=stepped))

    assert [row["seasonality_index"] for row in rows(basis)] == stepped


def test_a_term_with_no_index_contributes_the_neutral_multiplier() -> None:
    """Weighted toward 1 in proportion to how much volume is unmeasured — which
    is the right amount of confidence to lose, not a silent assumption."""
    basis = frame(
        keyword(term="a", volume=1_000, seasonality_index=JANUARY_HEAVY),
        keyword(term="b", volume=1_000),
    )

    # (2.0 x 1,000 + 1.0 x 1,000) / 2,000 = 1.5
    assert rows(basis)[0]["seasonality_index"] == 1.5


# ---------------------------------------------------------------------------
# rates
# ---------------------------------------------------------------------------


def test_ctr_and_cvr_come_from_the_accounts_own_search_history() -> None:
    basis = frame(
        keyword(),
        campaign_perf=[
            {"channel": "SEARCH", "impressions": 100_000, "clicks": 4_000, "conversions": 120}
        ],
    )

    assert basis.benchmarks is not None
    assert basis.benchmarks.measured is True
    assert basis.benchmarks.ctr_pct == 4.0
    assert basis.benchmarks.cvr_pct == 3.0
    assert basis.gaps == []


def test_display_history_does_not_rate_a_search_plan() -> None:
    """Display CTR is an order of magnitude lower and would forecast a tenth of
    the traffic."""
    basis = frame(
        keyword(),
        campaign_perf=[
            {"channel": "DISPLAY", "impressions": 10_000_000, "clicks": 5_000, "conversions": 5}
        ],
    )

    assert basis.benchmarks is not None
    assert basis.benchmarks.measured is False


def test_rates_are_weighted_by_their_own_denominator() -> None:
    """A mean of monthly rates gives a quiet month the same weight as a peak
    one, which is how a forecast comes to disagree with the account it was
    built from."""
    basis = frame(
        keyword(),
        campaign_perf=[
            {"channel": "SEARCH", "impressions": 1_000, "clicks": 100, "conversions": 10},
            {"channel": "SEARCH", "impressions": 99_000, "clicks": 1_980, "conversions": 20},
        ],
    )

    assert basis.benchmarks is not None
    # 2,080 clicks over 100,000 impressions = 2.08%, not the 6.5% an unweighted
    # mean of 10% and 2% would give.
    assert basis.benchmarks.ctr_pct == 2.08


def test_no_account_history_uses_the_planning_defaults_and_names_the_gap() -> None:
    """PRD §18: 2.2.1 still runs. It runs, and it says what it ran on."""
    basis = frame(keyword())

    assert basis.benchmarks is not None
    assert basis.benchmarks.measured is False
    assert basis.benchmarks.ctr_pct == CONSTANTS.get("forecast.default_ctr_pct").value
    assert any("campaign_perf" in gap and "planning default" in gap for gap in basis.gaps)


def test_the_cpc_is_volume_weighted_not_averaged() -> None:
    """One high-volume term at $10 and one long-tail term at $1 is a $9.10
    cluster. An unweighted mean would plan it at $5.50 and be short."""
    basis = frame(
        keyword(term="a", volume=9_000, cpc_low=10.0, cpc_high=10.0),
        keyword(term="b", volume=1_000, cpc_low=1.0, cpc_high=1.0),
    )

    assert rows(basis)[0]["avg_cpc_usd"] == 9.1


def test_the_impression_share_held_becomes_a_percentage_once() -> None:
    """Google reports it as a fraction and the connector passes it through
    unscaled, so the conversion happens at one boundary."""
    basis = frame(
        keyword(),
        impression_share=[
            {"impression_share": 0.4, "impressions": 90_000},
            {"impression_share": 0.1, "impressions": 10_000},
        ],
    )

    # (0.4 x 90,000 + 0.1 x 10,000) / 100,000 = 0.37
    assert basis.current_impression_share_pct == 37.0


# ---------------------------------------------------------------------------
# Google's forecast
# ---------------------------------------------------------------------------


def test_googles_answer_overrides_the_rates_for_the_group_it_answered_for() -> None:
    basis = frame(
        keyword(term="a", volume=1_000),
        keyword(term="b", volume=1_000, best_url=BLOG_URL),
        keyword_forecast=[
            {
                "cluster": "SDS management",
                "market": "US",
                "impressions": 10_000.0,
                "clicks": 500.0,
                "cost": 4_000.0,
            }
        ],
    )

    assert basis.method == "google_forecast"
    by_cluster = {row["cluster"]: row for row in rows(basis)}
    google = by_cluster["SDS management"]
    assert google["forecast_basis"] == "google"
    assert google["impressions"] == 10_000.0
    assert google["ctr_pct"] == 5.0
    assert google["avg_cpc_usd"] == 8.0


def test_an_unanswered_group_crosses_to_impressions_by_hand() -> None:
    """Otherwise one frame mixes "impressions we would win" with "searches that
    happen" in the same column, and undercounts the answered cluster by the
    share factor."""
    basis = frame(
        keyword(term="a", volume=1_000),
        keyword(term="b", volume=1_000, best_url=BLOG_URL),
        keyword_forecast=[
            {"cluster": "SDS management", "market": "US", "impressions": 10_000.0, "clicks": 500.0}
        ],
    )

    by_cluster = {row["cluster"]: row for row in rows(basis)}
    derived = by_cluster["SDS education"]
    assert derived["forecast_basis"] == "derived"
    assert derived["impressions"] == 450.0  # 1,000 x 45%


def test_a_forecast_row_with_no_cluster_is_ignored_not_guessed() -> None:
    basis = frame(
        keyword(),
        keyword_forecast=[{"market": "US", "impressions": 10_000.0, "clicks": 500.0}],
    )

    assert basis.method == "derived_arithmetic"


# ---------------------------------------------------------------------------
# the forecast groups
# ---------------------------------------------------------------------------


def test_each_group_carries_its_terms_and_its_own_bid() -> None:
    basis = frame(
        keyword(term="sds software", volume=1_000, cpc_low=10.0, cpc_high=10.0),
        keyword(term="sds system", volume=1_000, cpc_low=10.0, cpc_high=10.0),
        keyword(term="what is an sds", volume=500, best_url=BLOG_URL, cpc_low=1.0, cpc_high=1.0),
    )

    groups = {(item.cluster, item.market): item for item in basis.groups}
    management = groups[("SDS management", "US")]
    assert sorted(management.keywords) == ["sds software", "sds system"]
    assert management.max_cpc_usd == 10.0
    assert groups[("SDS education", "US")].max_cpc_usd == 1.0


def test_group_keywords_are_sorted_so_the_same_set_hashes_the_same() -> None:
    forwards = frame(keyword(term="b", volume=10), keyword(term="a", volume=10))
    backwards = frame(keyword(term="a", volume=10), keyword(term="b", volume=10))

    assert forwards.groups[0].keywords == backwards.groups[0].keywords
    assert forwards.frame.equals(backwards.frame)


def test_the_funnel_stage_is_the_one_holding_most_of_the_volume() -> None:
    basis = frame(
        keyword(term="a", volume=9_000, funnel_stage="bofu"),
        keyword(term="b", volume=1_000, funnel_stage="tofu"),
    )

    assert basis.groups[0].funnel_stage == "bofu"


def test_as_params_carries_the_language_only_when_there_is_one() -> None:
    basis = frame(keyword())
    group = basis.groups[0]

    assert "language" not in group.as_params()
    assert group.as_params(language="en")["language"] == "en"


# ---------------------------------------------------------------------------
# roll-ups
# ---------------------------------------------------------------------------

FORECAST = [
    {
        "cluster": "SDS management",
        "market": "US",
        "month": "Jan",
        "cost_usd": 6_000.0,
        "conversions": 30.0,
        "clicks": 600.0,
    },
    {
        "cluster": "SDS management",
        "market": "US",
        "month": "Feb",
        "cost_usd": 6_000.0,
        "conversions": 30.0,
        "clicks": 600.0,
    },
    {
        "cluster": "SDS education",
        "market": "US",
        "month": "Jan",
        "cost_usd": 2_000.0,
        "conversions": 40.0,
        "clicks": 1_000.0,
    },
    {
        "cluster": "SDS education",
        "market": "US",
        "month": "Feb",
        "cost_usd": 2_000.0,
        "conversions": 40.0,
        "clicks": 1_000.0,
    },
]

ASSIGNED = {("SDS management", "US"): "brand", ("SDS education", "US"): "education"}


def test_campaign_frame_reports_the_monthly_mean_not_the_total() -> None:
    """There is no approved budget at 2.2.2 — the gate that sets one is two
    nodes away — so the question is what the *forecast* costs per month."""
    rolled = demand.campaign_frame(FORECAST, assignments=ASSIGNED, month_count=2).to_dict(
        orient="records"
    )
    by_ref = {row["campaign_ref"]: row for row in rolled}

    assert by_ref["brand"]["monthly_budget_usd"] == 6_000.0  # 12,000 over two months
    assert by_ref["brand"]["forecast_cpa_usd"] == 200.0  # 12,000 / 60
    assert by_ref["brand"]["avg_cpc_usd"] == 10.0  # 12,000 / 1,200


def test_campaign_frame_omits_the_structure_counts_that_do_not_exist_yet() -> None:
    """The structure is built at 2.4.2. A zero here would make every campaign
    read as needing a merge it has not earned."""
    rolled = demand.campaign_frame(FORECAST, assignments=ASSIGNED, month_count=2)

    assert "ad_group_count" not in rolled.columns
    assert "keyword_count" not in rolled.columns


def test_an_unassigned_cluster_is_funded_visibly_rather_than_vanishing() -> None:
    rolled = demand.campaign_frame(
        FORECAST, assignments={("SDS management", "US"): "brand"}, month_count=2
    ).to_dict(orient="records")

    assert {row["campaign_ref"] for row in rolled} == {"brand", demand.UNASSIGNED}


def test_allocation_units_are_campaign_market_funnel() -> None:
    units = demand.allocation_units(
        FORECAST,
        assignments=ASSIGNED,
        targets={"brand": 300.0, "education": 100.0},
        month_count=2,
        funnel_stages={("SDS management", "US"): "bofu", ("SDS education", "US"): "tofu"},
    ).to_dict(orient="records")

    assert {(row["campaign_ref"], row["market"], row["funnel_stage"]) for row in units} == {
        ("brand", "US", "bofu"),
        ("education", "US", "tofu"),
    }
    assert {row["target_cpa_usd"] for row in units} == {300.0, 100.0}


def test_the_absorption_cap_is_measured_or_absent_never_guessed() -> None:
    with_headroom = demand.allocation_units(
        FORECAST,
        assignments=ASSIGNED,
        targets={"brand": 300.0},
        month_count=2,
        headroom_pct=50.0,
    ).to_dict(orient="records")
    without = demand.allocation_units(
        FORECAST, assignments=ASSIGNED, targets={"brand": 300.0}, month_count=2
    ).to_dict(orient="records")

    brand = next(row for row in with_headroom if row["campaign_ref"] == "brand")
    assert brand["max_spend_usd"] == 9_000.0  # $6,000/month + 50% headroom
    assert "max_spend_usd" not in without[0]


@pytest.mark.parametrize("month_count", [0, -1])
def test_a_roll_up_over_no_months_is_refused(month_count: int) -> None:
    with pytest.raises(ValueError, match="month_count"):
        demand.campaign_frame(FORECAST, assignments=ASSIGNED, month_count=month_count)


# ---------------------------------------------------------------------------
# the approved split, and what a rule may move
# ---------------------------------------------------------------------------

APPROVED = [
    {
        "campaign_ref": "brand",
        "market": "US",
        "funnel_stage": "bofu",
        "usd": 8_000.0,
        "forecast_cpa_usd": 200.0,
        "avg_cpc_usd": 10.0,
    },
    {
        "campaign_ref": "education",
        "market": "US",
        "funnel_stage": "tofu",
        "usd": 2_000.0,
        "forecast_cpa_usd": 50.0,
        "avg_cpc_usd": 2.0,
    },
]


def test_the_approved_frame_reconstructs_conversions_from_the_lines_own_rate() -> None:
    """So an approver's edit — which changes `usd` and nothing else — is
    reflected without the caller recomputing anything."""
    rolled = demand.approved_campaign_frame(APPROVED).to_dict(orient="records")
    by_ref = {row["campaign_ref"]: row for row in rolled}

    assert by_ref["brand"]["monthly_budget_usd"] == 8_000.0
    assert by_ref["brand"]["forecast_cpa_usd"] == 200.0  # 8,000 / 40 conversions
    assert by_ref["brand"]["avg_cpc_usd"] == 10.0


def test_whether_a_campaign_can_run_troas_is_carried_across_not_re_derived() -> None:
    rolled = demand.approved_campaign_frame(
        APPROVED, capacity=[{"campaign_ref": "brand", "has_revenue_values": True}]
    ).to_dict(orient="records")
    by_ref = {row["campaign_ref"]: row for row in rolled}

    assert by_ref["brand"]["has_revenue_values"] is True
    assert by_ref["education"]["has_revenue_values"] is False


def test_a_shift_is_bounded_by_policy_and_by_the_learning_threshold() -> None:
    capacity = demand.shift_capacity(
        [
            # Plenty of surplus: the 20% policy bound binds first.
            {"campaign_ref": "rich", "monthly_budget_usd": 10_000.0, "needed_budget_usd": 1_000.0},
            # Barely clearing: the learning bound binds first.
            {"campaign_ref": "tight", "monthly_budget_usd": 10_000.0, "needed_budget_usd": 9_500.0},
            # Under its threshold: nothing to give.
            {"campaign_ref": "short", "monthly_budget_usd": 1_000.0, "needed_budget_usd": 4_000.0},
        ],
        max_shift_pct=20.0,
    )

    assert capacity["rich"]["standard"] == 2_000.0
    assert capacity["tight"]["standard"] == 500.0
    assert capacity["short"]["standard"] == 0.0
    assert capacity["rich"]["half"] == 1_000.0


def test_the_shift_vocabulary_comes_from_the_constant_not_a_function_body() -> None:
    assert demand.shift_percentages(20.0) == {"standard": 20.0, "half": 10.0, "none": 0.0}


def test_cluster_totals_sum_the_months_away_for_a_prompt() -> None:
    totals = demand.cluster_totals(FORECAST, month_count=2)
    by_cluster = {row["cluster"]: row for row in totals}

    assert len(totals) == 2
    assert by_cluster["SDS management"]["monthly_cost_usd"] == 6_000.0
    assert by_cluster["SDS management"]["forecast_cpa_usd"] == 200.0
