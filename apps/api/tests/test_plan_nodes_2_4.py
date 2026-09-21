"""Stage 2.4 — how the account is organised (PRD §11).

The four things PRD §21 asks S2-P4 to prove are each asserted here by name:
a full campaign -> ad group -> keyword tree, every generated name passing
`validator_regex`, brand terms only in the brand campaign, and no keyword in
two ad groups.

The fixture, worked out by hand. Gate G4 put four campaigns in the slate:

    campaign_ref  channel   market   approved usd    daily (/30.4)
    brand         search    US        6,000          197.37
    nonbrand      search    US        7,000          230.26
    nonbrand      search    DE        4,000          131.58
    remarketing   display   US        3,000           98.68

`sds manager` is the brand term, so it may appear only in `brand`, and must be
a negative in every other campaign.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.export.contract import DemandMap, KeywordPageMapping, PricedKeyword
from agent.nodes import gather
from agent.nodes.plan import stage_2_4
from agent.planning import naming
from tests import plan_support as support

ENVELOPE = 20_000.0

ALLOCATION = [
    {
        "campaign_ref": "brand",
        "market": "US",
        "funnel_stage": "bottom",
        "usd": 6_000.0,
        "target_cpa_usd": 200.0,
        "forecast_cpa_usd": 80.0,
        "est_conv": 30.0,
        "avg_cpc_usd": 3.0,
    },
    {
        "campaign_ref": "nonbrand",
        "market": "US",
        "funnel_stage": "mid",
        "usd": 7_000.0,
        "target_cpa_usd": 250.0,
        "forecast_cpa_usd": 250.0,
        "est_conv": 28.0,
        "avg_cpc_usd": 8.0,
    },
    {
        "campaign_ref": "nonbrand",
        "market": "DE",
        "funnel_stage": "mid",
        "usd": 4_000.0,
        "target_cpa_usd": 220.0,
        "forecast_cpa_usd": 220.0,
        "est_conv": 16.0,
        "avg_cpc_usd": 4.0,
    },
    {
        "campaign_ref": "remarketing",
        "market": "US",
        "funnel_stage": "bottom",
        "usd": 3_000.0,
        "target_cpa_usd": 120.0,
        "forecast_cpa_usd": 120.0,
        "est_conv": 25.0,
        "avg_cpc_usd": 2.0,
    },
]

OUT_2_2_4 = {
    "envelope": {"monthly_cap_usd": ENVELOPE, "quarterly_cap_usd": 60_000.0, "currency": "USD"},
    "allocation": ALLOCATION,
}

OUT_2_2_2 = {
    "campaigns": [
        {
            "campaign_ref": "brand",
            "verdict": "clears",
            "has_revenue_values": False,
            "bid_strategy_recommended": "tcpa",
            "forecast_conv_30d": 75.0,
            "threshold": 30.0,
        },
        {
            "campaign_ref": "nonbrand",
            "verdict": "clears",
            "has_revenue_values": False,
            "bid_strategy_recommended": "tcpa",
            "forecast_conv_30d": 44.0,
            "threshold": 30.0,
        },
        {
            "campaign_ref": "remarketing",
            "verdict": "marginal",
            "has_revenue_values": False,
            "bid_strategy_recommended": "max_conv",
            "forecast_conv_30d": 25.0,
            "threshold": 30.0,
            "remedy": "switch_strategy",
        },
    ],
    "assignments": [],
    "structure_verdict": "sound",
}

OUT_2_3_1 = {
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

OUT_2_3_3 = {
    "brand_terms": [{"term": "sds manager", "variant_type": "exact_brand"}],
    "brand_campaign": {
        "campaign_ref": "brand",
        "match_types": ["exact", "phrase"],
        "budget_pct": 30.0,
        "target": 200.0,
    },
    "negatives_for_nonbrand": ["sds manager"],
    "reporting_rule": "never blended",
    "competitor_bidding_policy": "no",
}

PATTERNS = {
    "campaign": "{market} | {channel} | {brand_split}",
    "ad_group": "{market} | {channel} | {theme}",
}
TOKENS = [
    {"token": "market", "allowed_values": ["US", "DE"], "source": "project markets"},
    {"token": "channel", "allowed_values": ["Search", "Display"], "source": "the G4 slate"},
    {"token": "brand_split", "allowed_values": ["Brand", "NonBrand"], "source": "node 2.3.3"},
]

OUT_2_4_1 = {
    "patterns": PATTERNS,
    "tokens": TOKENS,
    "validator_regex": naming.compile_validator(
        PATTERNS, {row["token"]: row["allowed_values"] for row in TOKENS}
    ),
    "examples": ["US | Search | Brand"],
    "collisions": [],
    "collision_check": "checked",
}

KEYWORDS = [
    PricedKeyword(
        term="sds manager",
        market="US",
        intent="navigational",
        volume=2_000,
        cpc_low=2.0,
        cpc_high=4.0,
        best_url="/sds",
        match_type="exact",
    ),
    PricedKeyword(
        term="sds software",
        market="US",
        intent="commercial_investigation",
        volume=1_000,
        cpc_low=8.0,
        cpc_high=12.0,
        best_url="/sds",
    ),
    PricedKeyword(
        term="safety data sheet management",
        market="US",
        intent="commercial_investigation",
        volume=800,
        cpc_low=6.0,
        cpc_high=10.0,
        best_url="/sds",
    ),
    PricedKeyword(
        term="what is an sds",
        market="US",
        intent="informational",
        volume=500,
        cpc_low=1.0,
        cpc_high=1.0,
        best_url="/blog/sds",
    ),
    PricedKeyword(
        term="gefahrstoffmanagement",
        market="DE",
        intent="commercial_investigation",
        volume=600,
        cpc_low=3.0,
        cpc_high=5.0,
        best_url="/de/gefahrstoffe",
    ),
]

DEMAND_MAP = DemandMap(
    total_keywords=5,
    mapping=[KeywordPageMapping(term_cluster="sds", best_url="/sds", verdict="good_fit")],
)

NAMING_DRAFT = {
    "patterns": PATTERNS,
    "tokens": TOKENS,
    "examples": ["US | Search | Brand"],
    "notes": "Market first so the account sorts by market.",
}

STRUCTURE_DRAFT = {
    "ad_groups": [
        {"key": k, "theme": t, "primary_message": m}
        for k, t, m in [
            ("navigational|/sds", "Sds Manager", "The SDS system teams already know."),
            ("commercial_investigation|/sds", "Sds Software", "Manage every safety data sheet."),
            ("informational|/blog/sds", "What Is An Sds", "Start with the basics."),
            (
                "commercial_investigation|/de/gefahrstoffe",
                "Gefahrstoffe",
                "Gefahrstoffe sicher verwalten.",
            ),
        ]
    ],
    "account_negatives": ["jobs", "free"],
    "notes": "Themes follow the landing page.",
}

VERDICT_DRAFT = {"notes": "The display campaign is marginal by design.", "risks": []}


@pytest.fixture(autouse=True)
def collected(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Replace `gather.collect` with prepared evidence.

    Mutable so a test can say what the live Google Ads account holds; the
    default is an account that answered nothing, which is PRD §18's "not
    connected at all" and the case most likely to be got wrong.
    """
    holder = {"found": gather.Gathered()}

    async def collect(ctx: Any, *needs: gather.Need) -> gather.Gathered:
        return holder["found"]

    monkeypatch.setattr(gather, "collect", collect)
    return holder


def harness(node_id: str, answers: dict[str, Any], **outputs: Any) -> Any:
    permitted = {
        "2.4.1": (),
        "2.4.2": (stage_2_4.GROUPING, stage_2_4.SHARE),
        "2.4.3": (stage_2_4.VOLUME_CHECK,),
    }[node_id]
    base = {
        "2.2.2": OUT_2_2_2,
        "2.2.4": OUT_2_2_4,
        "2.3.1": OUT_2_3_1,
        "2.3.3": OUT_2_3_3,
        "2.4.1": OUT_2_4_1,
    }
    return support.harness(
        node_id,
        answers=answers,
        gathered=gather.Gathered(),
        source=support.plan_input(_report(KEYWORDS)),
        permitted=permitted,
        outputs={**base, **outputs},
    )


def _report(keywords: list[PricedKeyword]) -> dict[str, Any]:
    """The keywords ride in on the report: `plan_input` fills `priced_keyword_list`
    from it, so passing them as an override collides with its own argument."""
    return support.research_report(
        demand_map=DEMAND_MAP.model_dump(mode="json"),
        priced_keyword_list=[item.model_dump(mode="json") for item in keywords],
    )


async def run(node: Any, harnessed: Any) -> Any:
    evidence = await node.gather(harnessed.ctx)
    return await node.reason(harnessed.ctx, evidence)


# ---------------------------------------------------------------------------
# 2.4.1 naming_convention
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_validator_regex_is_compiled_from_the_patterns_not_written_by_the_model() -> None:
    harnessed = harness("2.4.1", {"NamingDraft": NAMING_DRAFT})
    out = await run(stage_2_4.NamingConventionNode(), harnessed)
    assert out.validator_regex == naming.compile_validator(
        PATTERNS, {row["token"]: row["allowed_values"] for row in TOKENS}
    )


@pytest.mark.asyncio
async def test_every_example_the_node_publishes_passes_its_own_regex() -> None:
    import re

    harnessed = harness("2.4.1", {"NamingDraft": NAMING_DRAFT})
    out = await run(stage_2_4.NamingConventionNode(), harnessed)
    pattern = re.compile(out.validator_regex)
    assert out.examples
    assert all(pattern.match(example) for example in out.examples)


@pytest.mark.asyncio
async def test_a_name_already_in_the_live_account_is_reported_with_a_resolution(
    collected: Any,
) -> None:
    collected["found"] = gather.Gathered(
        evidence=[support.evidence("campaign_perf", {"campaign": "US | Search | Brand"})]
    )
    harnessed = harness("2.4.1", {"NamingDraft": NAMING_DRAFT})
    out = await run(stage_2_4.NamingConventionNode(), harnessed)
    assert out.collision_check == "checked"
    assert out.collisions[0].existing_name == "US | Search | Brand"
    assert out.collisions[0].resolution == "US | Search | Brand v2"


@pytest.mark.asyncio
async def test_no_google_ads_account_means_no_collision_report_not_an_empty_one() -> None:
    """PRD §18: never assume an empty account."""
    harnessed = harness("2.4.1", {"NamingDraft": NAMING_DRAFT})
    out = await run(stage_2_4.NamingConventionNode(), harnessed)
    assert out.collision_check == "skipped"
    assert out.collisions == []
    assert "account_snapshot_unavailable" in out.open_gaps


@pytest.mark.asyncio
async def test_a_pattern_the_tokens_cannot_satisfy_fails_the_node_rather_than_shipping() -> None:
    draft = {**NAMING_DRAFT, "patterns": {"campaign": "{market} | {mystery}"}}
    harnessed = harness("2.4.1", {"NamingDraft": draft})
    with pytest.raises(naming.NamingError, match="unknown token"):
        await run(stage_2_4.NamingConventionNode(), harnessed)


# ---------------------------------------------------------------------------
# 2.4.2 account_structure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_structure_is_a_campaign_ad_group_keyword_tree() -> None:
    """PRD §21 exit criterion 1."""
    harnessed = harness("2.4.2", {"StructureDraft": STRUCTURE_DRAFT})
    out = await run(stage_2_4.AccountStructureNode(), harnessed)
    assert [campaign.campaign_ref for campaign in out.campaigns] == [
        "brand",
        "nonbrand",
        "nonbrand",
        "remarketing",
    ]
    tree = {
        c.name: {g.name: [k.term for k in g.keywords] for g in c.ad_groups} for c in out.campaigns
    }
    assert tree["US | Search | Brand"] == {"US | Search | Sds Manager": ["sds manager"]}
    assert sorted(tree["US | Search | NonBrand"]) == [
        "US | Search | Sds Software",
        "US | Search | What Is An Sds",
    ]


@pytest.mark.asyncio
async def test_every_generated_name_passes_the_validator_regex() -> None:
    """PRD §21 exit criterion 2, and §12 invariant 5."""
    import re

    harnessed = harness("2.4.2", {"StructureDraft": STRUCTURE_DRAFT})
    out = await run(stage_2_4.AccountStructureNode(), harnessed)
    pattern = re.compile(OUT_2_4_1["validator_regex"])
    names = [c.name for c in out.campaigns] + [g.name for c in out.campaigns for g in c.ad_groups]
    assert names
    assert [name for name in names if not pattern.match(name)] == []
    assert out.invalid_names == []


@pytest.mark.asyncio
async def test_a_brand_term_appears_only_in_the_brand_campaign() -> None:
    """PRD §21 exit criterion 3, and 2.6.2 check 5."""
    harnessed = harness("2.4.2", {"StructureDraft": STRUCTURE_DRAFT})
    out = await run(stage_2_4.AccountStructureNode(), harnessed)
    carrying = {
        c.campaign_ref
        for c in out.campaigns
        for g in c.ad_groups
        for k in g.keywords
        if k.term == "sds manager"
    }
    assert carrying == {"brand"}
    others = [c for c in out.campaigns if c.campaign_ref != "brand"]
    assert others
    assert all("sds manager" in c.negatives for c in others)


@pytest.mark.asyncio
async def test_no_keyword_appears_in_two_ad_groups() -> None:
    """PRD §21 exit criterion 4."""
    harnessed = harness("2.4.2", {"StructureDraft": STRUCTURE_DRAFT})
    out = await run(stage_2_4.AccountStructureNode(), harnessed)
    seen = [k.term for c in out.campaigns for g in c.ad_groups for k in g.keywords]
    assert len(seen) == len(set(seen))
    assert out.duplicate_terms == []


@pytest.mark.asyncio
async def test_the_daily_budget_is_computed_from_the_approved_monthly_split() -> None:
    """6,000 / 30.4 = 197.37. Not the model's number, and not divided by 30."""
    harnessed = harness("2.4.2", {"StructureDraft": STRUCTURE_DRAFT})
    out = await run(stage_2_4.AccountStructureNode(), harnessed)
    brand = next(c for c in out.campaigns if c.campaign_ref == "brand")
    assert (brand.monthly_budget_usd, brand.daily_budget_usd) == (6_000.0, 197.37)
    assert brand.target == 200.0
    assert out.calc_evidence_ids


@pytest.mark.asyncio
async def test_a_campaign_on_an_automated_channel_carries_no_keywords() -> None:
    """Display has no keyword targeting; inventing some would be a plan nobody can build."""
    harnessed = harness("2.4.2", {"StructureDraft": STRUCTURE_DRAFT})
    out = await run(stage_2_4.AccountStructureNode(), harnessed)
    display = next(c for c in out.campaigns if c.campaign_ref == "remarketing")
    assert display.ad_groups == []
    assert display.type == "display"


@pytest.mark.asyncio
async def test_a_keyword_with_no_landing_page_is_an_orphan_not_a_silent_loss() -> None:
    keywords = [
        *KEYWORDS,
        PricedKeyword(
            term="sds jobs",
            market="US",
            intent="informational",
            volume=100,
            cpc_low=1.0,
            cpc_high=1.0,
        ),
    ]
    harnessed = support.harness(
        "2.4.2",
        answers={"StructureDraft": STRUCTURE_DRAFT},
        gathered=gather.Gathered(),
        source=support.plan_input(_report(keywords)),
        permitted=(stage_2_4.GROUPING, stage_2_4.SHARE),
        outputs={
            "2.2.2": OUT_2_2_2,
            "2.2.4": OUT_2_2_4,
            "2.3.1": OUT_2_3_1,
            "2.3.3": OUT_2_3_3,
            "2.4.1": OUT_2_4_1,
        },
    )
    out = await run(stage_2_4.AccountStructureNode(), harnessed)
    assert any("sds jobs" in term for term in out.orphan_terms)


@pytest.mark.asyncio
async def test_the_bid_strategy_comes_from_the_learning_check_not_the_model() -> None:
    harnessed = harness("2.4.2", {"StructureDraft": STRUCTURE_DRAFT})
    out = await run(stage_2_4.AccountStructureNode(), harnessed)
    assert {c.campaign_ref: c.bid_strategy for c in out.campaigns} == {
        "brand": "tcpa",
        "nonbrand": "tcpa",
        "remarketing": "max_conv",
    }


# ---------------------------------------------------------------------------
# 2.4.3 structure_volume_check
# ---------------------------------------------------------------------------


def _built() -> dict[str, Any]:
    return {
        "campaigns": [
            {
                "campaign_ref": "brand",
                "name": "US | Search | Brand",
                "type": "search",
                "market": "US",
                "ad_groups": [{"name": "a", "keywords": [{"term": "t"}] * 6}],
            },
            {
                "campaign_ref": "nonbrand",
                "name": "US | Search | NonBrand",
                "type": "search",
                "market": "US",
                "ad_groups": [
                    {"name": "b", "keywords": [{"term": "u"}] * 8},
                    {"name": "c", "keywords": [{"term": "v"}] * 8},
                    {"name": "d", "keywords": [{"term": "w"}] * 8},
                ],
            },
            {
                "campaign_ref": "remarketing",
                "name": "US | Display | NonBrand",
                "type": "display",
                "market": "US",
                "ad_groups": [],
            },
        ],
    }


@pytest.mark.asyncio
async def test_a_campaign_that_clears_its_threshold_ships() -> None:
    harnessed = harness("2.4.3", {"VerdictDraft": VERDICT_DRAFT}, **{"2.4.2": _built()})
    out = await run(stage_2_4.StructureVolumeCheckNode(), harnessed)
    assert next(r for r in out.campaigns if r.ref == "nonbrand").action == "ship"


@pytest.mark.asyncio
async def test_a_campaign_with_too_few_ad_groups_is_merged_not_shipped() -> None:
    """`brand` has one ad group against a minimum of three: it is not a campaign yet."""
    harnessed = harness("2.4.3", {"VerdictDraft": VERDICT_DRAFT}, **{"2.4.2": _built()})
    out = await run(stage_2_4.StructureVolumeCheckNode(), harnessed)
    row = next(r for r in out.campaigns if r.ref == "brand")
    assert (row.remedy, row.action) == ("merge", "merge_into")


@pytest.mark.asyncio
async def test_the_action_vocabulary_never_leaves_a_campaign_without_one() -> None:
    harnessed = harness("2.4.3", {"VerdictDraft": VERDICT_DRAFT}, **{"2.4.2": _built()})
    out = await run(stage_2_4.StructureVolumeCheckNode(), harnessed)
    assert out.campaigns
    assert all(row.action in {"ship", "merge_into", "split", "defer"} for row in out.campaigns)


@pytest.mark.asyncio
async def test_a_campaign_spanning_two_markets_must_be_split() -> None:
    """Location targeting, budget and bid strategy are campaign-level in Google Ads,
    so one campaign cannot serve two markets however well it forecasts."""
    built = _built()
    built["campaigns"][1]["ad_groups"][0]["market"] = "DE"
    harnessed = harness("2.4.3", {"VerdictDraft": VERDICT_DRAFT}, **{"2.4.2": built})
    out = await run(stage_2_4.StructureVolumeCheckNode(), harnessed)
    assert next(r for r in out.campaigns if r.ref == "nonbrand").action == "split"


@pytest.mark.asyncio
async def test_the_counts_are_of_the_tree_that_was_built() -> None:
    harnessed = harness("2.4.3", {"VerdictDraft": VERDICT_DRAFT}, **{"2.4.2": _built()})
    out = await run(stage_2_4.StructureVolumeCheckNode(), harnessed)
    row = next(r for r in out.campaigns if r.ref == "nonbrand")
    assert (row.ad_group_count, row.keyword_count) == (3, 24)
    assert out.calc_evidence_ids
