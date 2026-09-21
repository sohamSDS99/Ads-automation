"""`structure.volume_check_v1` and `structure.grouping_v1` (PRD §9.2).

Volume thresholds come from the shipped constants: tCPA 30 conversions/30d,
tROAS 50, absolute minimum 15, min 3 ad groups, min 5 / max 20 keywords per ad
group. Every forecast below is `monthly_budget / forecast_cpa`, which is what
makes the verdicts checkable without a spreadsheet.
"""

from __future__ import annotations

import pytest

from agent.calc import structure
from agent.calc.registry import CalcError
from tests.calc_support import CONSTANTS, frame

CAMPAIGNS = [
    # 12,000 / 200 = 60 conversions. No revenue values, so tCPA's 30 is the bar.
    {
        "campaign_ref": "nonbrand-US",
        "monthly_budget_usd": 12_000,
        "forecast_cpa_usd": 200,
        "avg_cpc_usd": 6.0,
        "ad_group_count": 5,
        "keyword_count": 40,
        "has_revenue_values": False,
    },
    # 2,000 / 80 = 25. Revenue values, so tROAS's 50 is the bar: marginal.
    {
        "campaign_ref": "brand-US",
        "monthly_budget_usd": 2_000,
        "forecast_cpa_usd": 80,
        "avg_cpc_usd": 3.0,
        "ad_group_count": 4,
        "keyword_count": 25,
        "has_revenue_values": True,
    },
    # 1,000 / 250 = 4, and only two ad groups: structurally thin.
    {
        "campaign_ref": "pmax-DE",
        "monthly_budget_usd": 1_000,
        "forecast_cpa_usd": 250,
        "avg_cpc_usd": 4.0,
        "ad_group_count": 2,
        "keyword_count": 6,
        "has_revenue_values": False,
    },
    # 1,500 / 400 = 3.75, structure fine: a budget problem, not a layout one.
    {
        "campaign_ref": "exp-UK",
        "monthly_budget_usd": 1_500,
        "forecast_cpa_usd": 400,
        "avg_cpc_usd": 5.0,
        "ad_group_count": 4,
        "keyword_count": 30,
        "has_revenue_values": False,
    },
]


def check(rows: list[dict] | None = None, **kwargs: object) -> dict:
    return structure.volume_check_v1(
        frame(rows if rows is not None else CAMPAIGNS),
        constants=CONSTANTS,
        **kwargs,  # type: ignore[arg-type]
    ).result


def row(result: dict, ref: str) -> dict:
    return next(item for item in result["campaigns"] if item["campaign_ref"] == ref)


def test_forecast_conversions_are_budget_over_cpa() -> None:
    result = check()
    assert row(result, "nonbrand-US")["forecast_conv_30d"] == 60
    assert row(result, "brand-US")["forecast_conv_30d"] == 25
    assert row(result, "pmax-DE")["forecast_conv_30d"] == 4
    assert row(result, "exp-UK")["forecast_conv_30d"] == 3.75


def test_clearing_the_tcpa_threshold_needs_no_remedy() -> None:
    clear = row(check(), "nonbrand-US")
    assert clear["threshold"] == 30
    assert clear["verdict"] == "clears"
    assert clear["bid_strategy_recommended"] == "tcpa"
    assert clear["remedy"] is None


def test_revenue_values_raise_the_bar_to_the_troas_threshold() -> None:
    """25 conversions clears tCPA but not tROAS, and the campaign wants tROAS."""
    brand = row(check(), "brand-US")
    assert brand["bid_strategy_desired"] == "troas"
    assert brand["threshold"] == 50
    assert brand["verdict"] == "marginal"
    assert brand["remedy"] == "switch_strategy"
    assert brand["bid_strategy_recommended"] == "max_conv"
    assert brand["needed_budget_usd"] == 4_000  # 50 x $80


def test_structure_is_fixed_before_budget_is_blamed() -> None:
    """Two ad groups is a merge, whatever the forecast says about money."""
    thin = row(check(), "pmax-DE")
    assert thin["verdict"] == "below"
    assert thin["remedy"] == "merge"


def test_too_few_keywords_for_the_ad_groups_is_a_broadening_problem() -> None:
    rows = [{**CAMPAIGNS[2], "ad_group_count": 4, "keyword_count": 8}]
    assert row(check(rows), "pmax-DE")["remedy"] == "broaden"


def test_an_adequate_structure_that_is_simply_underfunded_asks_for_budget() -> None:
    underfunded = row(check(), "exp-UK")
    assert underfunded["verdict"] == "below"
    assert underfunded["remedy"] == "raise_budget"
    assert underfunded["needed_budget_usd"] == 12_000  # 30 x $400
    assert underfunded["budget_shortfall_usd"] == 10_500


def test_a_campaign_that_would_eat_the_whole_envelope_is_deferred_instead() -> None:
    assert row(check(envelope_usd=10_000), "exp-UK")["remedy"] == "defer_to_wave_2"
    assert row(check(envelope_usd=50_000), "exp-UK")["remedy"] == "raise_budget"


def test_no_envelope_means_no_deferral_claim() -> None:
    assert row(check(), "exp-UK")["remedy"] == "raise_budget"


@pytest.mark.parametrize(
    ("conversions", "verdict", "strategy"),
    [
        (14.9, "below", "max_clicks"),
        (15.0, "marginal", "max_conv"),
        (29.9, "marginal", "max_conv"),
        (30.0, "clears", "tcpa"),
        (60.0, "clears", "tcpa"),
    ],
)
def test_the_verdict_boundaries_are_where_the_constants_put_them(
    conversions: float, verdict: str, strategy: str
) -> None:
    rows = [
        {
            "campaign_ref": "x",
            "monthly_budget_usd": conversions * 100,
            "forecast_cpa_usd": 100,
            "ad_group_count": 4,
            "keyword_count": 40,
        }
    ]
    result = row(check(rows), "x")
    assert result["verdict"] == verdict
    assert result["bid_strategy_recommended"] == strategy


def test_troas_is_only_recommended_where_there_are_values_to_optimise() -> None:
    rows = [
        {
            "campaign_ref": "x",
            "monthly_budget_usd": 6_000,
            "forecast_cpa_usd": 100,  # 60 conversions, over the tROAS bar
            "has_revenue_values": has_values,
            "ad_group_count": 4,
            "keyword_count": 40,
        }
        for has_values in (True, False)
    ]
    result = check(rows)
    assert result["campaigns"][0]["bid_strategy_recommended"] == "troas"
    assert result["campaigns"][1]["bid_strategy_recommended"] == "tcpa"


def test_a_spreadsheet_spelling_of_no_is_read_as_no() -> None:
    """`bool("false")` is True, which would silently promote every campaign."""
    rows = [{**CAMPAIGNS[0], "has_revenue_values": "false"}]
    assert row(check(rows), "nonbrand-US")["bid_strategy_desired"] == "tcpa"
    rows = [{**CAMPAIGNS[0], "has_revenue_values": "true"}]
    assert row(check(rows), "nonbrand-US")["bid_strategy_desired"] == "troas"


def test_the_click_budget_ratio_is_daily_clicks_the_budget_can_buy() -> None:
    assert row(check(), "nonbrand-US")["budget_to_cpc_ratio"] == 66.6667  # 12,000/30/6
    assert row(check(), "nonbrand-US")["forecast_clicks_30d"] == 2_000  # 12,000/6


def test_no_cpc_means_no_click_figures_rather_than_zeroes() -> None:
    rows = [{k: v for k, v in CAMPAIGNS[0].items() if k != "avg_cpc_usd"}]
    result = row(check(rows), "nonbrand-US")
    assert result["forecast_clicks_30d"] is None
    assert result["budget_to_cpc_ratio"] is None


def test_the_account_verdict_is_the_worst_campaign_not_the_average() -> None:
    marginal_and_thin = [
        {
            "campaign_ref": "x",
            "monthly_budget_usd": 2_000,
            "forecast_cpa_usd": 100,  # 20 conversions: marginal, not below
            "ad_group_count": 2,  # under the 3 ad-group minimum
            "keyword_count": 40,
        }
    ]
    assert check()["structure_verdict"] == "too_thin"
    assert check(CAMPAIGNS[:1])["structure_verdict"] == "sound"
    assert check(marginal_and_thin)["structure_verdict"] == "needs_merge"


def test_a_campaign_that_clears_is_not_told_to_merge_however_few_ad_groups_it_has() -> None:
    """You do not merge a working campaign.

    `min_ad_groups_per_campaign` is a structure-quality constant, and a campaign
    forecast at 60 conversions a month has all the optimisation signal it needs
    whatever its layout. Its ad-group count is reported so node 2.4.3 can act on
    it; inventing a `merge` remedy here would be advice to break the one
    campaign that works.
    """
    result = row(check([{**CAMPAIGNS[0], "ad_group_count": 1}]), "nonbrand-US")
    assert result["verdict"] == "clears"
    assert result["remedy"] is None
    assert result["ad_group_count"] == 1


def test_an_unusable_campaign_is_excluded_with_its_reason() -> None:
    rows = [{**CAMPAIGNS[0], "forecast_cpa_usd": 0}, CAMPAIGNS[1]]
    calc = structure.volume_check_v1(frame(rows), constants=CONSTANTS)
    assert [item["campaign_ref"] for item in calc.result["campaigns"]] == ["brand-US"]
    assert "positive monthly_budget_usd" in calc.excluded[0]["reason"]


def test_every_campaign_being_unusable_raises() -> None:
    rows = [{**item, "monthly_budget_usd": 0} for item in CAMPAIGNS]
    with pytest.raises(CalcError, match="no campaign could be volume-checked"):
        structure.volume_check_v1(frame(rows), constants=CONSTANTS)


def test_thresholds_out_of_order_are_refused_before_any_verdict() -> None:
    with pytest.raises(CalcError, match="learning thresholds must satisfy"):
        structure.volume_check_v1(
            frame(CAMPAIGNS),
            constants=CONSTANTS.merged({"learning.absolute_min_conv_30d": 100}),
        )


# --- grouping ---------------------------------------------------------------

KEYWORDS = [
    {
        "term": "ehs software",
        "search_volume": 900,
        "forecast_cpc_usd": 7,
        "intent_label": "commercial",
        "landing_url": "/ehs-software",
    },
    {
        "term": "EHS Software",
        "search_volume": 400,
        "forecast_cpc_usd": 7,
        "intent_label": "commercial",
        "landing_url": "/ehs-software",
    },
    {
        "term": "best ehs software",
        "search_volume": 400,
        "forecast_cpc_usd": 8,
        "intent_label": "commercial",
        "landing_url": "/ehs-software",
    },
    {
        "term": "ehs management software",
        "search_volume": 300,
        "forecast_cpc_usd": 6,
        "intent_label": "commercial",
        "landing_url": "/ehs-software",
    },
    {
        "term": "sds management",
        "search_volume": 600,
        "forecast_cpc_usd": 5,
        "intent_label": "commercial",
        "landing_url": "/sds",
    },
    {
        "term": "safety data sheet software",
        "search_volume": 500,
        "forecast_cpc_usd": 5,
        "intent_label": "commercial",
        "landing_url": "/sds",
    },
    {
        "term": "what is an sds",
        "search_volume": 2_000,
        "forecast_cpc_usd": 1,
        "intent_label": "informational",
        "landing_url": "/blog/sds",
    },
    {
        "term": "chemical inventory",
        "search_volume": 700,
        "forecast_cpc_usd": 4,
        "intent_label": "commercial",
    },
]


def group(rows: list[dict] | None = None) -> dict:
    return structure.grouping_v1(
        frame(rows if rows is not None else KEYWORDS), constants=CONSTANTS
    ).result


def find(result: dict, key: str) -> dict:
    return next(item for item in result["ad_groups"] if item["key"] == key)


def test_the_page_map_and_the_intent_label_are_what_form_an_ad_group() -> None:
    result = group()
    assert {item["key"] for item in result["ad_groups"]} == {
        "commercial|/ehs-software",
        "commercial|/sds",
        "informational|/blog/sds",
    }


def test_no_keyword_lands_in_two_ad_groups() -> None:
    """PRD §11 critique assertion 4, true by construction."""
    result = group()
    terms = [keyword["term"] for item in result["ad_groups"] for keyword in item["keywords"]]
    assert len(terms) == len(set(terms))
    assert result["assigned_keyword_count"] == len(terms)


def test_a_duplicate_term_is_collapsed_keeping_the_larger_measured_volume() -> None:
    result = group()
    ehs = find(result, "commercial|/ehs-software")
    exact = next(k for k in ehs["keywords"] if k["term"] == "ehs software")
    assert exact["search_volume"] == 900
    assert result["duplicates_collapsed"] == [
        {"term": "ehs software", "dropped_search_volume": 400}
    ]


def test_case_and_whitespace_do_not_make_two_keywords() -> None:
    rows = [
        {"term": "  EHS   Software ", "intent_label": "c", "landing_url": "/a", "search_volume": 5},
        {"term": "ehs software", "intent_label": "c", "landing_url": "/a", "search_volume": 5},
    ]
    assert group(rows)["assigned_keyword_count"] == 1


def test_a_keyword_with_no_landing_page_is_an_orphan_not_a_guess() -> None:
    result = group()
    assert result["orphans"] == [
        {"term": "chemical inventory", "reason": "no landing_url in the page map"}
    ]
    assert result["orphan_count"] == 1
    assert result["orphan_pct"] == 14.29  # 1 of 7 deduped terms


def test_a_keyword_with_no_intent_label_is_also_an_orphan() -> None:
    rows = [
        {"term": "x", "landing_url": "/a"},
        {"term": "y", "landing_url": "/a", "intent_label": "c"},
    ]
    result = group(rows)
    assert result["orphans"] == [{"term": "x", "reason": "no intent_label"}]


def test_coherence_is_the_mean_pairwise_jaccard_of_the_token_sets() -> None:
    # {ehs, software} vs {ehs, software} = 1; both vs {ehs, management, software} = 2/3
    assert find(group(), "commercial|/ehs-software")["coherence"] == 0.7778


def test_an_incoherent_group_scores_zero_rather_than_being_hidden() -> None:
    # {sds, management} shares no token with {safety, data, sheet, software}
    assert find(group(), "commercial|/sds")["coherence"] == 0


def test_a_single_keyword_group_is_trivially_coherent() -> None:
    """Scoring it 0 would make `thin` and `incoherent` the same signal."""
    assert find(group(), "informational|/blog/sds")["coherence"] == 1


def test_a_group_under_the_minimum_is_flagged_thin_and_not_merged_away() -> None:
    result = group()
    assert set(result["thin_ad_groups"]) == {item["key"] for item in result["ad_groups"]}
    assert all(item["thin"] for item in result["ad_groups"])


def test_an_oversized_group_is_split_on_its_most_discriminating_token() -> None:
    rows = [
        {
            "term": f"widget {'blue' if index % 2 else 'red'} {index}",
            "search_volume": 100 - index,
            "intent_label": "commercial",
            "landing_url": "/widgets",
        }
        for index in range(30)
    ]
    result = group(rows)
    assert len(result["ad_groups"]) > 1
    assert all(item["keyword_count"] <= 20 for item in result["ad_groups"])
    assert result["assigned_keyword_count"] == 30
    # The split is on a shared token, so each group says one thing.
    for item in result["ad_groups"]:
        colours = {"blue" if "blue" in keyword["term"] else "red" for keyword in item["keywords"]}
        assert len(colours) == 1, item["key"]


def test_a_split_group_carries_its_index_so_naming_can_tell_them_apart() -> None:
    rows = [
        {
            "term": f"widget {'blue' if index % 2 else 'red'} {index}",
            "search_volume": 100 - index,
            "intent_label": "commercial",
            "landing_url": "/widgets",
        }
        for index in range(30)
    ]
    result = group(rows)
    assert all(item["split_index"] is not None for item in result["ad_groups"])
    assert all(item["split_of"] == len(result["ad_groups"]) for item in result["ad_groups"])


def test_a_group_no_token_divides_is_chunked_by_volume_rather_than_left_oversized() -> None:
    rows = [
        {
            "term": f"unique{index}",
            "search_volume": 100 - index,
            "intent_label": "commercial",
            "landing_url": "/x",
        }
        for index in range(25)
    ]
    result = group(rows)
    assert [item["keyword_count"] for item in result["ad_groups"]] == [20, 5]
    assert result["assigned_keyword_count"] == 25
    # Volume-descending, so the valuable terms share a group.
    assert find(result, "commercial|/x|1")["keywords"][0]["term"] == "unique0"


def test_grouping_is_deterministic_whatever_order_the_rows_arrive_in() -> None:
    forwards = group()
    backwards = group(list(reversed(KEYWORDS)))
    assert [item["key"] for item in forwards["ad_groups"]] == [
        item["key"] for item in backwards["ad_groups"]
    ]
    assert forwards["mean_coherence"] == backwards["mean_coherence"]


def test_the_theme_is_a_working_label_not_a_name() -> None:
    """Naming is node 2.4.1's job; it owns the pattern and the validator regex."""
    ehs = find(group(), "commercial|/ehs-software")
    assert ehs["theme"] == "ehs software management"
    assert "name" not in ehs


def test_a_blank_term_is_excluded_with_its_reason() -> None:
    rows = [{"term": "   ", "intent_label": "c", "landing_url": "/a"}, KEYWORDS[0]]
    calc = structure.grouping_v1(frame(rows), constants=CONSTANTS)
    assert calc.excluded[0]["reason"] == "term is blank"
    assert calc.result["assigned_keyword_count"] == 1


def test_no_keyword_being_assignable_raises() -> None:
    rows = [{"term": "x"}, {"term": "y"}]
    with pytest.raises(CalcError, match="no ad group can be formed"):
        structure.grouping_v1(frame(rows), constants=CONSTANTS)


def test_min_above_max_keywords_is_refused_before_any_grouping() -> None:
    with pytest.raises(CalcError, match="structure constants must satisfy"):
        structure.grouping_v1(
            frame(KEYWORDS),
            constants=CONSTANTS.merged({"structure.min_keywords_per_ad_group": 50}),
        )


def test_the_group_ordering_is_by_search_volume() -> None:
    assert [item["search_volume"] for item in group()["ad_groups"]] == [2_000, 1_600, 1_100]


# ---------------------------------------------------------------------------
# structure.overlap_v1
# ---------------------------------------------------------------------------

#: Three campaigns and what each one targets. Worked out by hand:
#:
#:     search-us       {ehs software, ehs platform, safety software,
#:                      chemical management}                            4 terms
#:     pmax-us         {ehs software, ehs platform, sds management}     3 terms
#:     remarketing-us  {forklift training}                              1 term
#:
#:     pmax-us / search-us      shared 2, combined 5  -> 40.00%
#:                              2/3 of pmax-us        -> 66.67%
#:                              2/4 of search-us      -> 50.00%
#:     every other pair         shared 0              ->  0.00%
MEMBERS = [
    {"campaign_ref": "search-us", "member": "ehs software"},
    {"campaign_ref": "search-us", "member": "ehs platform"},
    {"campaign_ref": "search-us", "member": "safety software"},
    {"campaign_ref": "search-us", "member": "chemical management"},
    {"campaign_ref": "pmax-us", "member": "ehs software"},
    {"campaign_ref": "pmax-us", "member": "ehs platform"},
    {"campaign_ref": "pmax-us", "member": "sds management"},
    {"campaign_ref": "remarketing-us", "member": "forklift training"},
]


def overlap(rows: list[dict[str, object]] | None = None) -> dict:
    calc = structure.overlap_v1(frame(rows if rows is not None else MEMBERS), constants=CONSTANTS)
    return calc.result


def test_overlap_is_the_share_of_the_combined_targeting_the_two_campaigns_share() -> None:
    top = overlap()["pairs"][0]
    assert (top["campaign_a"], top["campaign_b"]) == ("pmax-us", "search-us")
    assert top["overlap_pct"] == 40.0
    assert top["shared_count"] == 2


def test_the_directional_shares_say_which_campaign_is_the_one_being_eaten() -> None:
    """40% symmetric hides that two thirds of PMax is inside Search but only half the reverse."""
    top = overlap()["pairs"][0]
    assert top["a_shared_pct"] == 66.67
    assert top["b_shared_pct"] == 50.0


def test_campaigns_that_share_nothing_are_reported_as_zero_not_omitted() -> None:
    pairs = {
        (row["campaign_a"], row["campaign_b"]): row["overlap_pct"] for row in overlap()["pairs"]
    }
    assert pairs[("pmax-us", "remarketing-us")] == 0.0
    assert pairs[("remarketing-us", "search-us")] == 0.0
    assert len(pairs) == 3


def test_only_pairs_at_or_above_the_reporting_threshold_are_flagged() -> None:
    result = overlap()
    assert result["reportable"] == [["pmax-us", "search-us"]]
    assert result["max_overlap_pct"] == 40.0


def test_the_shared_terms_are_named_so_a_resolution_can_be_argued() -> None:
    assert overlap()["pairs"][0]["shared"] == ["ehs platform", "ehs software"]


def test_a_single_campaign_has_no_pair_and_that_is_not_an_error() -> None:
    result = overlap([{"campaign_ref": "only", "member": "ehs software"}])
    assert result["pairs"] == []
    assert result["max_overlap_pct"] == 0.0
    assert result["campaign_count"] == 1


def test_a_member_repeated_within_a_campaign_is_counted_once() -> None:
    rows = [*MEMBERS, {"campaign_ref": "pmax-us", "member": "ehs software"}]
    assert overlap(rows)["pairs"][0]["overlap_pct"] == 40.0


def test_members_are_matched_case_and_whitespace_insensitively() -> None:
    """`EHS  Software` and `ehs software` are one term, or every overlap reads as zero."""
    rows = [*MEMBERS[:4], {"campaign_ref": "pmax-us", "member": "  EHS   Software "}]
    assert overlap(rows)["pairs"][0]["shared"] == ["ehs software"]


def test_a_blank_member_is_excluded_with_its_reason() -> None:
    calc = structure.overlap_v1(
        frame([{"campaign_ref": "a", "member": "  "}, *MEMBERS]), constants=CONSTANTS
    )
    assert calc.excluded[0]["reason"] == "member is blank"


def test_no_campaign_carrying_a_member_raises() -> None:
    with pytest.raises(CalcError, match="no campaign carried a targetable member"):
        structure.overlap_v1(frame([{"campaign_ref": "a", "member": ""}]), constants=CONSTANTS)
