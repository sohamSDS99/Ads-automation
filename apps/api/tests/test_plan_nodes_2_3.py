"""Stage 2.3 — choosing the campaign types (PRD §11).

Same property under test as stages 2.1 and 2.2: **no figure in a plan node's
output was produced by the model.** Every scripted answer below contains labels,
names and prose and no numbers at all, so a node that published a figure the
model chose could not have — there was none to take.

The fixtures, worked out by hand. Gate G3 approved a $20,000 monthly envelope
across four allocation lines:

    campaign_ref  market  usd      target CPA
    brand         US      6,000    200
    nonbrand      US      7,000    250
    nonbrand      DE      4,000    220
    remarketing   US      3,000    120
                          20,000

The slate the scripted model proposes folds those into three channel entries,
so `est_share_of_budget_pct` is checkable without a spreadsheet:

    search | US        brand + nonbrand    13,000   65.00%
    search | DE        nonbrand             4,000   20.00%
    display | US       remarketing          3,000   15.00%
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.nodes import gather
from agent.nodes.plan import stage_2_3
from tests import plan_support as support

ENVELOPE = 20_000.0

ALLOCATION = [
    {
        "campaign_ref": "brand",
        "market": "US",
        "funnel_stage": "bottom",
        "usd": 6_000.0,
        "pct": 30.0,
        "target_cpa_usd": 200.0,
        "forecast_cpa_usd": 80.0,
        "est_conv": 30.0,
        "efficiency": 2.5,
        "avg_cpc_usd": 3.0,
    },
    {
        "campaign_ref": "nonbrand",
        "market": "US",
        "funnel_stage": "mid",
        "usd": 7_000.0,
        "pct": 35.0,
        "target_cpa_usd": 250.0,
        "forecast_cpa_usd": 250.0,
        "est_conv": 28.0,
        "efficiency": 1.0,
        "avg_cpc_usd": 6.0,
    },
    {
        "campaign_ref": "nonbrand",
        "market": "DE",
        "funnel_stage": "mid",
        "usd": 4_000.0,
        "pct": 20.0,
        "target_cpa_usd": 220.0,
        "forecast_cpa_usd": 220.0,
        "est_conv": 16.0,
        "efficiency": 1.0,
        "avg_cpc_usd": 4.0,
    },
    {
        "campaign_ref": "remarketing",
        "market": "US",
        "funnel_stage": "bottom",
        "usd": 3_000.0,
        "pct": 15.0,
        "target_cpa_usd": 120.0,
        "forecast_cpa_usd": 120.0,
        "est_conv": 25.0,
        "efficiency": 1.0,
        "avg_cpc_usd": 2.0,
    },
]

OUT_2_1_3 = {
    "objectives": [
        {
            "campaign_ref": "brand",
            "objective": "brand_defense",
            "primary_kpi": "cpa",
            "target_value": 200.0,
            "ceiling_value": 250.0,
            "basis": "computed",
            "confidence": "high",
        },
        {
            "campaign_ref": "nonbrand",
            "objective": "lead_gen",
            "primary_kpi": "cpl",
            "target_value": 250.0,
            "ceiling_value": 300.0,
            "basis": "computed",
            "confidence": "high",
        },
        {
            "campaign_ref": "remarketing",
            "objective": "remarketing",
            "primary_kpi": "cpa",
            "target_value": 120.0,
            "ceiling_value": 150.0,
            "basis": "computed",
            "confidence": "medium",
        },
    ],
    "north_star": {"metric": "cpl", "target": 250.0, "period": "monthly"},
}

OUT_2_2_4 = {
    "chosen_scenario": "expected",
    "envelope": {
        "monthly_cap_usd": ENVELOPE,
        "quarterly_cap_usd": 60_000.0,
        "currency": "USD",
        "scenario_total_usd": ENVELOPE,
        "unallocated_usd": 0.0,
    },
    "allocation": ALLOCATION,
    "experiment_reserve_pct": 10.0,
    "experiment_reserve_usd": 2_000.0,
    "degraded_sources": [],
}

OUT_2_2_2 = {
    "campaigns": [
        {
            "campaign_ref": "brand",
            "verdict": "clears",
            "forecast_conv_30d": 75.0,
            "threshold": 30.0,
            "bid_strategy_recommended": "tcpa",
            "monthly_budget_usd": 6_000.0,
            "forecast_cpa_usd": 80.0,
        },
        {
            "campaign_ref": "nonbrand",
            "verdict": "clears",
            "forecast_conv_30d": 44.0,
            "threshold": 30.0,
            "bid_strategy_recommended": "tcpa",
            "monthly_budget_usd": 11_000.0,
            "forecast_cpa_usd": 250.0,
        },
        {
            "campaign_ref": "remarketing",
            "verdict": "marginal",
            "forecast_conv_30d": 25.0,
            "threshold": 30.0,
            "bid_strategy_recommended": "max_conv",
            "monthly_budget_usd": 3_000.0,
            "forecast_cpa_usd": 120.0,
            "remedy": "switch_strategy",
        },
    ],
    "assignments": [
        {
            "cluster": "sds management",
            "market": "US",
            "campaign_ref": "nonbrand",
            "rationale": "core intent",
        },
        {
            "cluster": "sds education",
            "market": "US",
            "campaign_ref": "nonbrand",
            "rationale": "top of funnel",
        },
        {"cluster": "brand", "market": "US", "campaign_ref": "brand", "rationale": "our own name"},
        {
            "cluster": "chemical inventory",
            "market": "US",
            "campaign_ref": "remarketing",
            "rationale": "re-engage the people who priced inventory",
        },
        {
            "cluster": "sds management",
            "market": "DE",
            "campaign_ref": "nonbrand",
            "rationale": "same intent, German",
        },
    ],
    "structure_verdict": "sound",
}

SLATE_DRAFT = {
    "slate": [
        {
            "campaign_type": "search",
            "market": "US",
            "campaign_refs": ["brand", "nonbrand"],
            "launch_wave": 1,
            "rationale": "Existing demand, measurable intent.",
            "entry_criteria": ["Conversion tracking verified"],
            "exit_criteria": ["CPL above the ceiling for two months"],
            "prerequisites": ["Offline conversion import live"],
        },
        {
            "campaign_type": "search",
            "market": "DE",
            "campaign_refs": ["nonbrand"],
            "launch_wave": 2,
            "rationale": "Same intent, translated pages ready.",
            "entry_criteria": ["German landing pages live"],
            "exit_criteria": [],
            "prerequisites": ["German ad copy reviewed"],
        },
        {
            "campaign_type": "display",
            "market": "US",
            "campaign_refs": ["remarketing"],
            "launch_wave": 2,
            "rationale": "Re-engage visitors who did not convert.",
            "entry_criteria": ["Audience list above 1,000"],
            "exit_criteria": [],
            "prerequisites": [],
        },
    ],
    "rejected": [
        {"campaign_type": "shopping", "why_not": "No product feed; the offer is a subscription."},
    ],
    "notes": "Search first, everything else behind a wave.",
}

BRAND_DRAFT = {
    "brand_terms": [
        {"term": "sds manager", "variant_type": "exact_brand"},
        {"term": "sdsmanager", "variant_type": "exact_brand"},
        {"term": "sds manager software", "variant_type": "brand_plus_category"},
        {"term": "sds manger", "variant_type": "misspelling"},
    ],
    "brand_campaign_ref": "brand",
    "match_types": ["exact", "phrase"],
    "negatives_for_nonbrand": ["sds manager", "sdsmanager"],
    "reporting_rule": "Brand and non-brand are never reported as one blended CPL.",
    "competitor_bidding_policy": "We do not bid on competitor brand terms.",
    "notes": "Brand is defended, not grown.",
}

AUTOMATION_DRAFT = {
    "pmax_allowed": True,
    "included_themes": ["chemical safety software"],
    "excluded_urls": ["/careers", "/support"],
    "account_negatives": ["jobs", "free", "pdf download"],
    "broad_match_campaigns": ["nonbrand"],
    "broad_match_guardrails": ["Only with tCPA and a shared negative list"],
    "resolutions": [
        {
            "campaign_a": "display|US",
            "campaign_b": "search|US",
            "resolution": "Search keeps the query; display is audience-only.",
        }
    ],
    "notes": "PMax stays off until brand exclusions are confirmed.",
}


def harness(node_id: str, answers: dict[str, Any], **outputs: Any) -> Any:
    permitted = {
        "2.3.1": (stage_2_3.SHARE,),
        "2.3.3": (stage_2_3.SHARE,),
        "2.3.2": (stage_2_3.OVERLAP,),
    }[node_id]
    base = {"2.1.3": OUT_2_1_3, "2.2.4": OUT_2_2_4, "2.2.2": OUT_2_2_2}
    return support.harness(
        node_id,
        answers=answers,
        gathered=gather.Gathered(),
        source=support.plan_input(support.research_report()),
        permitted=permitted,
        outputs={**base, **outputs},
    )


async def run(node: Any, harnessed: Any) -> Any:
    evidence = await node.gather(harnessed.ctx)
    return await node.reason(harnessed.ctx, evidence)


# ---------------------------------------------------------------------------
# 2.3.1 channel_slate — gate G4
# ---------------------------------------------------------------------------


def test_the_slate_node_carries_gate_g4_for_an_approver() -> None:
    spec = stage_2_3.ChannelSlateNode.spec
    assert (spec.gate, spec.gate_key) == (True, "G4")
    assert spec.required_role.value == "approver"
    assert spec.depends_on == ("2.2.4", "2.1.3")


@pytest.mark.asyncio
async def test_the_slate_shares_are_computed_from_the_approved_split() -> None:
    harnessed = harness("2.3.1", {"SlateDraft": SLATE_DRAFT})
    out = await run(stage_2_3.ChannelSlateNode(), harnessed)
    assert [(row.campaign_type, row.market, row.est_share_of_budget_pct) for row in out.slate] == [
        ("search", "US", 65.0),
        ("search", "DE", 20.0),
        ("display", "US", 15.0),
    ]


@pytest.mark.asyncio
async def test_every_share_is_cited_to_a_calculation_this_node_made() -> None:
    harnessed = harness("2.3.1", {"SlateDraft": SLATE_DRAFT})
    out = await run(stage_2_3.ChannelSlateNode(), harnessed)
    assert out.calc_evidence_ids
    assert set(out.calc_evidence_ids) <= {row.id for row in harnessed.writer.rows.values()}


@pytest.mark.asyncio
async def test_the_shares_sum_to_the_whole_envelope_when_every_campaign_is_placed() -> None:
    harnessed = harness("2.3.1", {"SlateDraft": SLATE_DRAFT})
    out = await run(stage_2_3.ChannelSlateNode(), harnessed)
    assert sum(row.est_share_of_budget_pct for row in out.slate) == 100.0
    assert out.unplaced_campaigns == []


@pytest.mark.asyncio
async def test_a_campaign_the_slate_forgot_is_named_rather_than_funded_silently() -> None:
    """Money the approved split gave a campaign that no channel entry claims."""
    draft = {**SLATE_DRAFT, "slate": SLATE_DRAFT["slate"][:2]}
    harnessed = harness("2.3.1", {"SlateDraft": draft})
    out = await run(stage_2_3.ChannelSlateNode(), harnessed)
    assert out.unplaced_campaigns == ["remarketing (US)"]
    assert sum(row.est_share_of_budget_pct for row in out.slate) == 85.0


@pytest.mark.asyncio
async def test_a_slate_entry_naming_a_campaign_nobody_agreed_to_is_dropped() -> None:
    """2.1.3 fixed the campaign list at G1; 2.3.1 chooses channels, not campaigns."""
    draft = {
        **SLATE_DRAFT,
        "slate": [
            *SLATE_DRAFT["slate"],
            {
                "campaign_type": "video",
                "market": "US",
                "campaign_refs": ["invented"],
                "launch_wave": 3,
                "rationale": "n/a",
                "entry_criteria": [],
                "exit_criteria": [],
                "prerequisites": [],
            },
        ],
    }
    harnessed = harness("2.3.1", {"SlateDraft": draft})
    out = await run(stage_2_3.ChannelSlateNode(), harnessed)
    assert [row.campaign_type for row in out.slate] == ["search", "search", "display"]
    assert "invented" in out.notes or any("invented" in item for item in out.dropped)


@pytest.mark.asyncio
async def test_the_rejected_channels_survive_with_their_reason() -> None:
    harnessed = harness("2.3.1", {"SlateDraft": SLATE_DRAFT})
    out = await run(stage_2_3.ChannelSlateNode(), harnessed)
    assert out.rejected[0].campaign_type == "shopping"
    assert "product feed" in out.rejected[0].why_not


@pytest.mark.asyncio
async def test_the_launch_waves_are_kept_in_order() -> None:
    harnessed = harness("2.3.1", {"SlateDraft": SLATE_DRAFT})
    out = await run(stage_2_3.ChannelSlateNode(), harnessed)
    assert [row.launch_wave for row in out.slate] == [1, 2, 2]


# ---------------------------------------------------------------------------
# 2.3.3 brand_isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_brand_campaign_budget_share_is_computed_not_chosen() -> None:
    """Brand is the 6,000 line of a 20,000 envelope: 30%."""
    harnessed = harness("2.3.3", {"BrandDraft": BRAND_DRAFT}, **{"2.3.1": _slate_output()})
    out = await run(stage_2_3.BrandIsolationNode(), harnessed)
    assert out.brand_campaign.budget_pct == 30.0
    assert out.brand_campaign.target == 200.0
    assert out.calc_evidence_ids


@pytest.mark.asyncio
async def test_every_brand_term_becomes_a_negative_for_the_non_brand_campaigns() -> None:
    """PRD §11 2.6.2 check 5: brand terms appear only in the brand campaign."""
    harnessed = harness("2.3.3", {"BrandDraft": BRAND_DRAFT}, **{"2.3.1": _slate_output()})
    out = await run(stage_2_3.BrandIsolationNode(), harnessed)
    assert set(out.negatives_for_nonbrand) >= {term.term for term in out.brand_terms}


@pytest.mark.asyncio
async def test_the_match_types_are_carried_through() -> None:
    harnessed = harness("2.3.3", {"BrandDraft": BRAND_DRAFT}, **{"2.3.1": _slate_output()})
    out = await run(stage_2_3.BrandIsolationNode(), harnessed)
    assert out.brand_campaign.match_types == ["exact", "phrase"]
    assert out.competitor_bidding_policy.startswith("We do not bid")


# ---------------------------------------------------------------------------
# 2.3.2 automation_boundaries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_keyword_channel_covers_what_it_was_assigned_an_automated_one_covers_the_market() -> (
    None
):
    """The whole reason 2.3.2 exists, and the arithmetic is checkable by hand.

    `search|US` targets keywords, so it covers exactly the clusters 2.2.2 gave
    its campaigns: {sds management, sds education, brand} in US. `display|US`
    targets an audience, so Google decides what it serves and it can reach every
    cluster in the market — all four, including `chemical inventory`, which
    nobody pointed it at.

        shared   3      combined  4   ->  75.00%
        3 of display's 4                 75.00%
        3 of search's 3                 100.00%   <- Search is wholly inside it
    """
    harnessed = harness(
        "2.3.2",
        {"AutomationDraft": AUTOMATION_DRAFT},
        **{"2.3.1": _slate_output(), "2.3.3": _brand_output()},
    )
    out = await run(stage_2_3.AutomationBoundariesNode(), harnessed)
    pair = next(
        row
        for row in out.overlap
        if {row.campaign_a, row.campaign_b} == {"display|US", "search|US"}
    )
    assert (pair.campaign_a, pair.campaign_b) == ("display|US", "search|US")
    assert pair.overlap_pct == 75.0
    assert (pair.a_shared_pct, pair.b_shared_pct) == (75.0, 100.0)
    assert out.calc_evidence_ids


@pytest.mark.asyncio
async def test_the_same_cluster_in_two_markets_is_not_an_overlap() -> None:
    """`sds management` runs in US and DE. They do not compete for one impression."""
    harnessed = harness(
        "2.3.2",
        {"AutomationDraft": AUTOMATION_DRAFT},
        **{"2.3.1": _slate_output(), "2.3.3": _brand_output()},
    )
    out = await run(stage_2_3.AutomationBoundariesNode(), harnessed)
    pair = next(
        row for row in out.overlap if {row.campaign_a, row.campaign_b} == {"search|DE", "search|US"}
    )
    assert pair.overlap_pct == 0.0


@pytest.mark.asyncio
async def test_pmax_may_be_allowed_but_never_without_brand_exclusions() -> None:
    """Q9's default: allowed, with brand exclusions mandatory. Not the model's call."""
    harnessed = harness(
        "2.3.2",
        {"AutomationDraft": {**AUTOMATION_DRAFT, "pmax_allowed": True}},
        **{"2.3.1": _slate_output(), "2.3.3": _brand_output()},
    )
    out = await run(stage_2_3.AutomationBoundariesNode(), harnessed)
    assert out.pmax.brand_exclusion_required is True
    assert set(out.pmax.account_negatives) >= {"sds manager", "sdsmanager"}


@pytest.mark.asyncio
async def test_a_model_that_says_brand_exclusions_are_optional_is_overruled() -> None:
    draft = {**AUTOMATION_DRAFT, "pmax_allowed": True, "brand_exclusion_required": False}
    harnessed = harness(
        "2.3.2",
        {"AutomationDraft": draft},
        **{"2.3.1": _slate_output(), "2.3.3": _brand_output()},
    )
    out = await run(stage_2_3.AutomationBoundariesNode(), harnessed)
    assert out.pmax.brand_exclusion_required is True


@pytest.mark.asyncio
async def test_broad_match_is_only_allowed_on_campaigns_that_are_in_the_slate() -> None:
    draft = {**AUTOMATION_DRAFT, "broad_match_campaigns": ["nonbrand", "ghost"]}
    harnessed = harness(
        "2.3.2",
        {"AutomationDraft": draft},
        **{"2.3.1": _slate_output(), "2.3.3": _brand_output()},
    )
    out = await run(stage_2_3.AutomationBoundariesNode(), harnessed)
    assert out.broad_match.allowed_campaigns == ["nonbrand"]


# ---------------------------------------------------------------------------
# outputs of the earlier nodes, as the later ones read them
# ---------------------------------------------------------------------------


def _slate_output() -> dict[str, Any]:
    return {
        "slate": [
            {
                "campaign_type": "search",
                "market": "US",
                "campaign_refs": ["brand", "nonbrand"],
                "launch_wave": 1,
                "rationale": "r",
                "entry_criteria": [],
                "exit_criteria": [],
                "prerequisites": [],
                "est_share_of_budget_pct": 65.0,
                "est_monthly_usd": 13_000.0,
            },
            {
                "campaign_type": "search",
                "market": "DE",
                "campaign_refs": ["nonbrand"],
                "launch_wave": 2,
                "rationale": "r",
                "entry_criteria": [],
                "exit_criteria": [],
                "prerequisites": [],
                "est_share_of_budget_pct": 20.0,
                "est_monthly_usd": 4_000.0,
            },
            {
                "campaign_type": "display",
                "market": "US",
                "campaign_refs": ["remarketing"],
                "launch_wave": 2,
                "rationale": "r",
                "entry_criteria": [],
                "exit_criteria": [],
                "prerequisites": [],
                "est_share_of_budget_pct": 15.0,
                "est_monthly_usd": 3_000.0,
            },
        ],
        "rejected": [],
        "brand_campaign_ref": "brand",
    }


def _brand_output() -> dict[str, Any]:
    return {
        "brand_terms": [
            {"term": "sds manager", "variant_type": "exact_brand"},
            {"term": "sdsmanager", "variant_type": "exact_brand"},
        ],
        "brand_campaign": {
            "campaign_ref": "brand",
            "match_types": ["exact", "phrase"],
            "budget_pct": 30.0,
            "target": 200.0,
        },
        "negatives_for_nonbrand": ["sds manager", "sdsmanager"],
        "reporting_rule": "never blended",
        "competitor_bidding_policy": "no",
    }


# ---------------------------------------------------------------------------
# placing money the slate did not enumerate market-by-market
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_funded_market_the_slate_did_not_name_still_gets_its_channel() -> None:
    """The slate assigns channels to campaigns, not to markets.

    A campaign funded in a market no entry enumerated still needs a channel,
    and when that campaign appears in exactly one entry there is only one
    channel it could be. Leaving it unplaced would report a slate that covers
    less of the budget than it really does.
    """
    draft = {
        **SLATE_DRAFT,
        "slate": [
            {**SLATE_DRAFT["slate"][0], "campaign_refs": ["brand", "nonbrand", "remarketing"]},
        ],
    }
    harnessed = harness("2.3.1", {"SlateDraft": draft})
    out = await run(stage_2_3.ChannelSlateNode(), harnessed)
    assert out.unplaced_campaigns == []
    assert out.slate[0].est_share_of_budget_pct == 100.0


@pytest.mark.asyncio
async def test_a_campaign_in_two_entries_is_placed_by_market_not_by_guesswork() -> None:
    """`nonbrand` runs as Search in both US and DE, so the market decides."""
    harnessed = harness("2.3.1", {"SlateDraft": SLATE_DRAFT})
    out = await run(stage_2_3.ChannelSlateNode(), harnessed)
    by_key = {(row.campaign_type, row.market): row.est_share_of_budget_pct for row in out.slate}
    assert by_key[("search", "US")] == 65.0
    assert by_key[("search", "DE")] == 20.0


@pytest.mark.asyncio
async def test_a_slate_that_places_none_of_the_approved_budget_fails_by_name() -> None:
    """Not a `CalcError` from three frames down: the reason a person can act on."""
    draft = {
        **SLATE_DRAFT,
        "slate": [{**SLATE_DRAFT["slate"][0], "campaign_refs": ["invented"]}],
        "rejected": [],
    }
    harnessed = harness("2.3.1", {"SlateDraft": draft})
    with pytest.raises(stage_2_3.SlateUnusable, match="none of the campaigns agreed at gate G1"):
        await run(stage_2_3.ChannelSlateNode(), harnessed)
