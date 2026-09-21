"""`planning/experiments.py` — the candidates a plan's own decisions raise.

The rule this suite exists to pin is that **the backlog is derived, not
brainstormed**: the same four node outputs must always produce the same
candidate list, and a campaign with no unresolved tension must produce none.
A generator that "helpfully" proposed a landing-page test for every campaign
would pass a smoke test and fail here.
"""

from __future__ import annotations

from typing import Any

from agent.planning import experiments

STRUCTURE: dict[str, Any] = {
    "campaigns": [
        {
            "campaign_ref": "us-search-brand",
            "name": "US | Search | Brand",
            "market": "US",
            "monthly_budget_usd": 4_000.0,
            "bid_strategy": "manual_cpc",
            "locations": ["United States"],
            "ad_groups": [{"name": "brand core", "landing_url": "https://example.com/"}],
        },
        {
            "campaign_ref": "us-search-nonbrand",
            "name": "US | Search | Non-brand",
            "market": "US",
            "monthly_budget_usd": 9_000.0,
            "bid_strategy": "max_conv",
            "locations": ["United States", "Canada"],
            "ad_groups": [{"name": "sds software", "landing_url": "https://example.com/sds"}],
        },
    ]
}

ALLOCATION: dict[str, Any] = {
    "experiment_reserve_usd": 1_300.0,
    "allocation": [
        {
            "campaign_ref": "us-search-brand",
            "market": "US",
            "usd": 4_000.0,
            "est_conv": 120.0,
            "est_clicks": 3_000.0,
            "avg_cpc_usd": 1.33,
        },
        {
            "campaign_ref": "us-search-nonbrand",
            "market": "US",
            "usd": 9_000.0,
            "est_conv": 90.0,
            "est_clicks": 6_000.0,
            "avg_cpc_usd": 1.50,
            "cap_applied": True,
        },
    ],
}

CAPACITY: dict[str, Any] = {
    "campaigns": [
        {"campaign_ref": "us-search-brand", "verdict": "clears", "forecast_clicks_30d": 3_000.0},
        {
            "campaign_ref": "us-search-nonbrand",
            "verdict": "marginal",
            "remedy": "switch_strategy",
            "bid_strategy_recommended": "tcpa",
            "forecast_clicks_30d": 6_000.0,
        },
    ]
}

SLATE: dict[str, Any] = {
    "slate": [
        {"campaign_type": "search", "campaign_refs": ["us-search-brand"], "launch_wave": 1},
        {"campaign_type": "search", "campaign_refs": ["us-search-nonbrand"], "launch_wave": 1},
        {"campaign_type": "display_remarketing", "campaign_refs": ["us-rmkt"], "launch_wave": 2},
    ]
}

BOUNDARIES: dict[str, Any] = {"broad_match": {"allowed_campaigns": ["us-search-nonbrand"]}}


def build(**overrides: Any) -> experiments.Backlog:
    kwargs: dict[str, Any] = {
        "structure": STRUCTURE,
        "allocation": ALLOCATION,
        "capacity": CAPACITY,
        "slate": SLATE,
        "boundaries": BOUNDARIES,
        "consent_markets": ["US"],
    }
    kwargs.update(overrides)
    return experiments.build(**kwargs)


def variables(backlog: experiments.Backlog, ref: str) -> set[str]:
    return {c.variable for c in backlog.candidates if c.campaign_ref == ref}


def test_a_marginal_learning_verdict_raises_a_bid_strategy_test() -> None:
    assert "bid_strategy" in variables(build(), "us-search-nonbrand")


def test_a_clearing_campaign_raises_no_bid_strategy_test() -> None:
    assert "bid_strategy" not in variables(build(), "us-search-brand")


def test_a_capped_allocation_line_raises_a_budget_test() -> None:
    assert "budget" in variables(build(), "us-search-nonbrand")
    assert "budget" not in variables(build(), "us-search-brand")


def test_broad_match_permission_raises_a_match_type_test() -> None:
    assert "match_type" in variables(build(), "us-search-nonbrand")
    assert "match_type" not in variables(build(), "us-search-brand")


def test_multiple_locations_raise_a_geo_test() -> None:
    assert "geo" in variables(build(), "us-search-nonbrand")
    assert "geo" not in variables(build(), "us-search-brand")


def test_clearing_the_learning_threshold_raises_an_ad_schedule_test() -> None:
    assert "ad_schedule" in variables(build(), "us-search-brand")


def test_remarketing_plus_a_lawful_basis_raises_an_audience_test() -> None:
    assert "audience" in variables(build(), "us-search-brand")


def test_a_blocked_market_raises_no_audience_test() -> None:
    # PC1: the consent gate is authoritative. With no market carrying a lawful
    # basis, the audience rule must not fire however attractive remarketing is.
    assert "audience" not in variables(build(consent_markets=[]), "us-search-brand")


def test_no_remarketing_in_the_slate_raises_no_audience_test() -> None:
    search_only = {"slate": [row for row in SLATE["slate"] if row["campaign_type"] == "search"]}
    assert "audience" not in variables(build(slate=search_only), "us-search-brand")


def test_a_campaign_with_no_tension_raises_nothing() -> None:
    quiet_structure = {
        "campaigns": [
            {
                "campaign_ref": "quiet",
                "name": "Quiet",
                "market": "US",
                "monthly_budget_usd": 1_000.0,
                "locations": ["United States"],
                "ad_groups": [],
            }
        ]
    }
    backlog = build(
        structure=quiet_structure,
        allocation={"allocation": [{"campaign_ref": "quiet", "est_conv": 5, "est_clicks": 500}]},
        capacity={"campaigns": [{"campaign_ref": "quiet", "verdict": "below"}]},
        slate={"slate": []},
        boundaries={},
        consent_markets=[],
    )
    assert backlog.candidates == []
    assert backlog.unsizable == []


def test_the_landing_page_rule_is_capped_at_three_campaigns() -> None:
    many = {
        "campaigns": [
            {
                "campaign_ref": f"c{index}",
                "name": f"C{index}",
                "market": "US",
                "monthly_budget_usd": float(1_000 * index),
                "locations": ["US"],
                "ad_groups": [{"landing_url": f"https://example.com/{index}"}],
            }
            for index in range(1, 7)
        ]
    }
    allocation = {
        "allocation": [
            {"campaign_ref": f"c{index}", "est_conv": 10, "est_clicks": 1_000, "avg_cpc_usd": 1.0}
            for index in range(1, 7)
        ]
    }
    backlog = experiments.build(
        structure=many,
        allocation=allocation,
        capacity={"campaigns": []},
        slate={"slate": []},
        boundaries={},
    )
    page_tests = [c for c in backlog.candidates if c.variable == "landing_page"]
    assert len(page_tests) == experiments.LANDING_PAGE_TESTS
    # The three biggest budgets, not the first three in the list.
    assert {c.campaign_ref for c in page_tests} == {"c6", "c5", "c4"}


def test_the_baseline_is_conversions_over_clicks_as_a_percentage() -> None:
    backlog = build()
    candidate = next(c for c in backlog.candidates if c.campaign_ref == "us-search-brand")
    assert candidate.baseline_cvr_pct == 4.0  # 120 / 3,000
    assert candidate.clicks_per_day == 100.0  # 3,000 / 30


def test_a_campaign_with_no_forecast_becomes_not_yet_rather_than_a_guess() -> None:
    blind = {"allocation": [{"campaign_ref": "us-search-nonbrand", "usd": 9_000.0}]}
    backlog = build(allocation=blind, capacity={"campaigns": CAPACITY["campaigns"][:1]})
    assert not any(c.campaign_ref == "us-search-nonbrand" for c in backlog.candidates)
    assert any("no conversion or click forecast" in row["blocked_by"] for row in backlog.unsizable)


def test_more_conversions_than_clicks_is_refused_rather_than_capped_at_100() -> None:
    impossible = {
        "allocation": [
            {"campaign_ref": "us-search-brand", "est_conv": 5_000.0, "est_clicks": 100.0}
        ]
    }
    backlog = build(allocation=impossible, capacity={"campaigns": []})
    assert backlog.candidates == []


def test_the_earliest_wave_comes_from_the_slate_agreed_at_g4() -> None:
    backlog = build()
    assert all(c.launch_wave == 1 for c in backlog.candidates)


def test_the_sizing_frame_carries_exactly_what_the_power_formula_needs() -> None:
    frame = build().sizing_frame(default_mde_pct=20.0)
    assert set(frame.columns) == {
        "id",
        "baseline_cvr_pct",
        "mde_pct",
        "arms",
        "clicks_per_day",
    }
    assert (frame["mde_pct"] == 20.0).all()
    assert (frame["arms"] == 2).all()


def test_candidate_ids_are_stable_and_unique() -> None:
    first = [c.id for c in build().candidates]
    second = [c.id for c in build().candidates]
    assert first == second
    assert len(set(first)) == len(first)


def test_every_candidate_names_the_upstream_finding_that_raised_it() -> None:
    for candidate in build().candidates:
        assert candidate.basis.strip()
        assert candidate.primary_metric in {"cpl", "cpa", "roas", "conv_volume", "cvr"}


def test_a_duplicated_allocation_line_keeps_the_first() -> None:
    doubled = {
        "allocation": [
            {"campaign_ref": "us-search-brand", "est_conv": 120.0, "est_clicks": 3_000.0},
            {"campaign_ref": "us-search-brand", "est_conv": 1.0, "est_clicks": 10.0},
        ]
    }
    backlog = build(allocation=doubled, capacity={"campaigns": CAPACITY["campaigns"][:1]})
    candidate = next(c for c in backlog.candidates if c.campaign_ref == "us-search-brand")
    assert candidate.baseline_cvr_pct == 4.0
