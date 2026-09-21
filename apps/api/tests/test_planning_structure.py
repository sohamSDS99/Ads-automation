"""`planning/structure.py` — the frames stages 2.3 and 2.4 compute over.

Same division of labour as `planning/demand.py`: this module reshapes what
Stage 01 and the approved budget already established into the columns a formula
declares, and every figure the plan publishes is produced by `agent/calc/` from
one of these frames. Nothing here decides anything.
"""

from __future__ import annotations

import pytest

from agent.export.contract import DemandMap, KeywordPageMapping, PricedKeyword
from agent.planning import structure

KEYWORDS = [
    PricedKeyword(
        term="ehs software",
        market="US",
        intent="commercial_investigation",
        volume=2_000,
        cpc_low=5.0,
        cpc_high=9.0,
        best_url="/ehs-software",
        match_type="phrase",
    ),
    PricedKeyword(
        term="safety management system",
        market="US",
        intent="commercial_investigation",
        volume=800,
        cpc_low=4.0,
        cpc_high=6.0,
        best_url="/ehs-software",
        match_type="exact",
    ),
    PricedKeyword(
        term="gefahrstoffmanagement",
        market="DE",
        intent="commercial_investigation",
        volume=500,
        cpc_low=3.0,
        cpc_high=5.0,
        best_url="/de/gefahrstoffe",
    ),
    # No best_url. Kept, not dropped: `grouping_v1` reports it as an orphan, and
    # a keyword silently removed here is one nobody ever finds out about.
    PricedKeyword(
        term="what is an sds",
        market="US",
        intent="informational",
        volume=300,
        cpc_low=1.0,
        cpc_high=1.0,
    ),
]

DEMAND_MAP = DemandMap(
    total_keywords=4,
    mapping=[
        KeywordPageMapping(term_cluster="ehs", best_url="/ehs-software", verdict="good_fit"),
        KeywordPageMapping(term_cluster="gefahrstoffe", best_url="/de/gefahrstoffe", verdict="gap"),
    ],
)


# ---------------------------------------------------------------------------
# keyword_frame
# ---------------------------------------------------------------------------


def test_the_keyword_frame_carries_the_columns_grouping_declares() -> None:
    frame = structure.keyword_frame(KEYWORDS, demand_map=DEMAND_MAP)
    assert set(structure.GROUPING_INPUT).issubset(frame.columns)


def test_the_forecast_cpc_is_the_midpoint_of_the_researched_range() -> None:
    """5.0 and 9.0 -> 7.0. Taking the low end would under-forecast every click."""
    frame = structure.keyword_frame(KEYWORDS, demand_map=DEMAND_MAP)
    row = frame[frame["term"] == "ehs software"].iloc[0]
    assert row["forecast_cpc_usd"] == 7.0


def test_a_keyword_with_only_one_end_of_the_range_uses_it() -> None:
    keywords = [
        PricedKeyword(
            term="x",
            market="US",
            intent="commercial_investigation",
            volume=10,
            cpc_high=4.0,
            best_url="/x",
        )
    ]
    frame = structure.keyword_frame(keywords, demand_map=DEMAND_MAP)
    assert frame.iloc[0]["forecast_cpc_usd"] == 4.0


def test_the_market_filter_keeps_only_that_market() -> None:
    frame = structure.keyword_frame(KEYWORDS, demand_map=DEMAND_MAP, market="DE")
    assert list(frame["term"]) == ["gefahrstoffmanagement"]


def test_a_keyword_with_no_landing_url_survives_to_be_reported_as_an_orphan() -> None:
    frame = structure.keyword_frame(KEYWORDS, demand_map=DEMAND_MAP, market="US")
    assert "what is an sds" in list(frame["term"])
    assert frame[frame["term"] == "what is an sds"].iloc[0]["landing_url"] == ""


def test_the_match_type_defaults_to_phrase_when_research_did_not_say() -> None:
    frame = structure.keyword_frame(KEYWORDS, demand_map=DEMAND_MAP, market="DE")
    assert frame.iloc[0]["match_type"] == "phrase"


def test_a_page_stage_one_called_a_gap_is_not_used_as_a_landing_url() -> None:
    """1.4.5 said no suitable page exists. Sending traffic there is the bounce."""
    frame = structure.keyword_frame(KEYWORDS, demand_map=DEMAND_MAP, market="DE")
    assert frame.iloc[0]["landing_url"] == ""
    assert frame.iloc[0]["landing_url_note"] == "1.4.5 mapped /de/gefahrstoffe as a content gap"


def test_a_page_stage_one_called_a_good_fit_is_used() -> None:
    frame = structure.keyword_frame(KEYWORDS, demand_map=DEMAND_MAP, market="US")
    row = frame[frame["term"] == "ehs software"].iloc[0]
    assert row["landing_url"] == "/ehs-software"
    assert row["landing_url_note"] == ""


def test_no_keyword_at_all_raises_rather_than_building_an_empty_structure() -> None:
    with pytest.raises(structure.StructureInputError, match="carries no priced keyword"):
        structure.keyword_frame([], demand_map=DEMAND_MAP)


def test_a_market_nothing_was_researched_for_raises_naming_the_market() -> None:
    with pytest.raises(structure.StructureInputError, match="market FR"):
        structure.keyword_frame(KEYWORDS, demand_map=DEMAND_MAP, market="FR")


# ---------------------------------------------------------------------------
# share_frame
# ---------------------------------------------------------------------------

ALLOCATION = [
    {
        "campaign_ref": "brand",
        "market": "US",
        "funnel_stage": "bottom",
        "usd": 6_000,
        "target_cpa_usd": 200,
        "est_conv": 30,
    },
    {
        "campaign_ref": "nonbrand",
        "market": "US",
        "funnel_stage": "mid",
        "usd": 3_000,
        "target_cpa_usd": 250,
        "est_conv": 12,
    },
    {
        "campaign_ref": "nonbrand",
        "market": "DE",
        "funnel_stage": "mid",
        "usd": 4_000,
        "target_cpa_usd": 220,
        "est_conv": 16,
    },
]


def test_share_frame_labels_each_line_with_the_group_the_caller_asked_for() -> None:
    frame = structure.share_frame(ALLOCATION, label=lambda line: str(line["campaign_ref"]))
    assert list(frame["group"]) == ["brand", "nonbrand", "nonbrand"]
    assert list(frame["usd"]) == [6_000, 3_000, 4_000]


def test_share_frame_can_group_by_more_than_one_field() -> None:
    frame = structure.share_frame(
        ALLOCATION, label=lambda line: f"{line['campaign_ref']}|{line['market']}"
    )
    assert list(frame["group"]) == ["brand|US", "nonbrand|US", "nonbrand|DE"]


def test_a_line_the_labeller_cannot_place_is_kept_and_left_blank_for_the_formula() -> None:
    """`share_v1` excludes a blank group with its reason; dropping it here hides it."""
    frame = structure.share_frame(
        ALLOCATION, label=lambda line: "" if line["market"] == "DE" else "x"
    )
    assert list(frame["group"]) == ["x", "x", ""]


def test_share_frame_of_nothing_raises() -> None:
    with pytest.raises(structure.StructureInputError, match="no approved allocation"):
        structure.share_frame([], label=lambda line: "x")


# ---------------------------------------------------------------------------
# member_frame
# ---------------------------------------------------------------------------


def test_member_frame_flattens_a_campaign_to_one_row_per_member() -> None:
    frame = structure.member_frame({"search-us": ["a", "b"], "pmax-us": ["b"]})
    assert list(zip(frame["campaign_ref"], frame["member"], strict=True)) == [
        ("pmax-us", "b"),
        ("search-us", "a"),
        ("search-us", "b"),
    ]


def test_a_campaign_targeting_nothing_still_appears_so_it_reads_as_zero_overlap() -> None:
    frame = structure.member_frame({"search-us": ["a"], "empty": []})
    assert "empty" in list(frame["campaign_ref"])


# ---------------------------------------------------------------------------
# built_campaign_frame
# ---------------------------------------------------------------------------

BUILT = [
    {
        "name": "US | Search | Brand | EHS",
        "campaign_ref": "brand",
        "ad_groups": [{"keywords": [1, 2, 3]}, {"keywords": [4, 5]}],
    },
    {
        "name": "US | Search | NonBrand | Safety",
        "campaign_ref": "nonbrand",
        "ad_groups": [{"keywords": [1]}],
    },
]

CAPACITY = [{"campaign_ref": "brand", "has_revenue_values": True}]


def test_the_built_frame_counts_the_tree_that_was_actually_built() -> None:
    frame = structure.built_campaign_frame(BUILT, allocation=ALLOCATION, capacity=CAPACITY)
    row = frame[frame["campaign_ref"] == "brand"].iloc[0]
    assert row["ad_group_count"] == 2
    assert row["keyword_count"] == 5


def test_the_built_frame_carries_the_approved_money_not_the_forecast() -> None:
    """2.2.2 checked the forecast; 2.4.3 checks what a human signed."""
    frame = structure.built_campaign_frame(BUILT, allocation=ALLOCATION, capacity=CAPACITY)
    assert frame[frame["campaign_ref"] == "brand"].iloc[0]["monthly_budget_usd"] == 6_000
    assert frame[frame["campaign_ref"] == "nonbrand"].iloc[0]["monthly_budget_usd"] == 7_000


def test_the_revenue_flag_is_carried_across_so_the_two_checks_cannot_disagree() -> None:
    frame = structure.built_campaign_frame(BUILT, allocation=ALLOCATION, capacity=CAPACITY)
    assert bool(frame[frame["campaign_ref"] == "brand"].iloc[0]["has_revenue_values"]) is True
    assert bool(frame[frame["campaign_ref"] == "nonbrand"].iloc[0]["has_revenue_values"]) is False


def test_a_built_campaign_with_no_approved_money_raises_rather_than_shipping_unfunded() -> None:
    built = [{"name": "X", "campaign_ref": "ghost", "ad_groups": []}]
    with pytest.raises(structure.StructureInputError, match="ghost"):
        structure.built_campaign_frame(built, allocation=ALLOCATION, capacity=CAPACITY)
