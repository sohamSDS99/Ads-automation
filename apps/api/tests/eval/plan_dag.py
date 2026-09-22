"""What the planning DAG produces for one `PlanInput` — deterministically.

PQ3 asks for "five golden `PlanInput` fixtures produce plans that pass every
§11 critique assertion on every CI run". Two of those words do the work.
*`PlanInput`* means the fixture is the **input** to Stage 02, not a plan
somebody hand-wrote to pass; *every CI run* means no model may be called. This
module is what stands between the two: the deterministic half of the DAG,
written so that everything the critique looks at is **derived from the input**
rather than declared next to it.

That derivation is the whole value. A fixture whose campaigns, keywords, brand
terms, consent scope and blockers are typed in by hand is a test of typing. So:

* campaigns come from `source.markets`;
* keywords come from `source.priced_keyword_list`, each assigned to exactly one
  ad group (assertion 4 cannot pass by accident);
* brand terms come from `source.business_context.brand_terms`, and every
  non-brand campaign negatives them (assertion 5);
* the consent scope is computed by the **real** `tracking.consent_scope` from
  `source.readiness`, so a fixture that blocks a market blocks it everywhere
  (assertion 8, invariant PC1);
* `source.launch_blockers` become `open_dependencies` (assertion 10);
* the envelope and its split are computed so they sum exactly (assertion 1).

**What is *not* derived is the prose, and that is the honest boundary.** A
model writes rationales, names ad-group themes and rates a hypothesis; none of
that is what the ten assertions check. Everything a node would have got from a
model is a constant here and is marked as such.

**The outputs are validated against the registry's declared models before they
are used.** `assemble()` reads dictionaries, so a fixture could quietly feed it
a shape no node could ever produce and the critique would happily pass on the
result. Round-tripping through `spec(node_id).output_model` makes that
impossible: a node whose contract changes breaks these fixtures, which is
exactly the alarm PQ3 is asking for.

**Every id is fixed.** `uuid4()` here would make two builds of one fixture
differ, which breaks PT3 determinism and the byte-identical export rule the
same way it broke `plan_fixture.py` once already.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from agent.export.contract import Claim
from agent.export.plan_contract import CampaignPlan, PlanSource
from agent.orchestrator.registry import get_registry
from agent.planning import naming, tracking
from agent.planning.plan_synthesis import CalcIndex, CalcRef, PlanFacts, assemble
from agent.schemas.plan_input import PlanInput

# ---------------------------------------------------------------------------
# fixed identity
# ---------------------------------------------------------------------------

PLAN_RUN_ID = uuid.UUID("0eaa0000-0000-4000-8000-000000000001")
CALC_ECONOMICS = uuid.UUID("0eaa1111-1111-4111-8111-111111111111")
CALC_SPLIT = uuid.UUID("0eaa2222-2222-4222-8222-222222222222")
CALC_ENVELOPE = uuid.UUID("0eaa3333-3333-4333-8333-333333333333")
EVIDENCE = uuid.UUID("0eaa4444-4444-4444-8444-444444444444")
GENERATED_AT = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
#: Namespace for the acceptance id derived from a fixture's research run.
ACCEPTANCE_NS = uuid.UUID("0eaa5555-5555-4555-8555-555555555555")

#: The three `(node, formula)` pairs `plan_synthesis` resolves figures through.
#: Written out rather than discovered, because a figure that silently stops
#: resolving becomes an *absent* figure, and assertion 7 would then pass on a
#: plan with nothing in it.
CALC_ROWS: tuple[CalcRef, ...] = (
    CalcRef(node_id="2.1.2", formula_id="economics.max_cpa_v1", evidence_id=CALC_ECONOMICS),
    CalcRef(node_id="2.1.3", formula_id="economics.max_cpa_v1", evidence_id=CALC_ECONOMICS),
    CalcRef(node_id="2.2.4", formula_id="allocation.split_v1", evidence_id=CALC_SPLIT),
    CalcRef(node_id="2.2.3", formula_id="scenarios.envelope_v1", evidence_id=CALC_ENVELOPE),
)

#: The naming convention every fixture builds names against. Both patterns go
#: through the real `compile_validator`, so assertion 9 is checking the product's
#: regex and not a copy of it.
PATTERNS: Mapping[str, str] = {
    "campaign": "{market} | {segment} | {channel}",
    "ad_group": "{market} | {segment} | {theme}",
}
TOKENS: Mapping[str, Sequence[str]] = {
    "market": ("US", "GB", "DE", "FR", "NL"),
    "segment": ("Brand", "Nonbrand"),
    "channel": ("Search",),
}

#: Monthly envelope per market, in whole dollars. Round on purpose: assertion 1
#: allows 0.5% drift and a fixture that only passes because of the tolerance is
#: a fixture that stops passing when somebody tightens it.
PER_MARKET_USD = Decimal("12000")
BRAND_SHARE = Decimal("0.25")


@dataclass(frozen=True, slots=True)
class Built:
    """One fixture, all the way through."""

    source: PlanInput
    outputs: dict[str, dict[str, Any]]
    plan: CampaignPlan
    consent: tracking.ConsentScope


@dataclass(slots=True)
class _Shape:
    """The campaign skeleton, derived from the input before anything is written."""

    markets: list[str]
    brand_terms: list[str]
    validator: str
    campaigns: list[dict[str, Any]] = field(default_factory=list)


def _money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _markets(source: PlanInput) -> list[str]:
    """Market codes, uppercased and deduped, in declaration order.

    A `PlanInput` with no markets is legal — Stage 01 can finish without them —
    and produces a plan with no campaigns, which every assertion handles: they
    are all "for each campaign" and an empty account passes them vacuously.
    """
    seen: list[str] = []
    for market in source.markets:
        code = market.country.strip().upper()
        if code and code not in seen:
            seen.append(code)
    return seen


def _brand_terms(source: PlanInput) -> list[str]:
    """Our own name and its variants, from the project's product context.

    In a real run this list is model-authored (2.3.3 asks for "our own name and
    its variants"), which is exactly the kind of thing a deterministic harness
    must not invent. `PlanInput.product_context` is a project-owned dict that
    crosses the handshake verbatim, so putting the brand there makes the terms
    a **function of the fixture** — and a fixture with no brand gets no brand
    campaign and no isolation section, which assertion 5 handles by returning
    early rather than by failing.
    """
    raw = source.product_context.get("brand_terms")
    terms = [str(term).strip() for term in raw or [] if str(term).strip()]
    return list(dict.fromkeys(terms))


def _keywords_for(source: PlanInput, market: str) -> list[dict[str, Any]]:
    """Non-brand keywords for one market, each appearing exactly once.

    `market=None` on a priced keyword means "every market", which is how Stage
    01 records a term it could not localise. Assigning such a term to every
    market would put it in two ad groups and fail assertion 4 — so it goes to
    the first market only, and that is a derivation rule rather than an
    accident of iteration order.
    """
    brand = {term.lower() for term in _brand_terms(source)}
    ordered = _markets(source)
    out: list[dict[str, Any]] = []
    for item in source.priced_keyword_list:
        term = (item.term or "").strip()
        if not term or term.lower() in brand:
            continue
        home = (item.market or "").strip().upper() or (ordered[0] if ordered else market)
        if home != market:
            continue
        out.append(
            {
                "term": term,
                "match_type": item.match_type or "phrase",
                "forecast_cpc_usd": item.cpc_high or item.cpc_low or 2.0,
                "search_volume": item.volume or 0,
            }
        )
    return out


def _shape(source: PlanInput) -> _Shape:
    markets = _markets(source)
    brand_terms = _brand_terms(source)
    validator = naming.compile_validator(PATTERNS, TOKENS)

    shape = _Shape(markets=markets, brand_terms=brand_terms, validator=validator)
    for market in markets:
        # **One brand campaign for the account, not one per market.** §12 gives
        # `brand_isolation` a single `brand_campaign_ref`, and the critique
        # compares every campaign against that one ref — so a second brand
        # campaign is by construction a campaign bidding brand terms *outside*
        # the brand campaign (assertion 5) whose keywords also appear twice
        # (assertion 4). Both fired on the first build of the multi-market
        # fixtures, which is the harness proving it can fail.
        segments = ["Nonbrand"] + (["Brand"] if brand_terms and market == markets[0] else [])
        for segment in segments:
            name = naming.render(
                PATTERNS["campaign"],
                {"market": market, "segment": segment, "channel": "Search"},
            )
            is_brand = segment == "Brand"
            keywords = (
                [
                    {
                        "term": term,
                        "match_type": "exact",
                        # Brand terms are not in the priced list — Stage 01
                        # prices demand, not our own name — so the forecast
                        # fields the contract requires are stated as the
                        # constants they are rather than implied.
                        "forecast_cpc_usd": 1.2,
                        "search_volume": 400,
                    }
                    for term in brand_terms
                ]
                if is_brand
                else _keywords_for(source, market)
            )
            theme = "Brand" if is_brand else "Core"
            group_name = naming.render(
                PATTERNS["ad_group"],
                {"market": market, "segment": segment, "theme": theme},
            )
            shape.campaigns.append(
                {
                    "name": name,
                    "campaign_ref": _ref(market, segment),
                    "market": market,
                    "segment": segment,
                    "is_brand": is_brand,
                    "group_name": group_name,
                    "keywords": keywords,
                }
            )
    return shape


def _ref(market: str, segment: str) -> str:
    return f"{segment.lower()}-{market.lower()}"


def _budgets(shape: _Shape) -> tuple[Decimal, dict[str, Decimal]]:
    """The envelope, and a split that sums to it exactly.

    Computed rather than typed: the last line absorbs the rounding remainder,
    so assertion 1 sees a total equal to the cap and not merely within 0.5% of
    it. A split that relies on the tolerance is a split nobody can tighten.
    """
    envelope = PER_MARKET_USD * len(shape.markets)
    if not shape.campaigns:
        return Decimal(0), {}

    weights: dict[str, Decimal] = {}
    for campaign in shape.campaigns:
        weights[campaign["campaign_ref"]] = BRAND_SHARE if campaign["is_brand"] else Decimal(1)
    total_weight = sum(weights.values())

    split: dict[str, Decimal] = {}
    running = Decimal(0)
    refs = list(weights)
    for ref in refs[:-1]:
        share = (envelope * weights[ref] / total_weight).quantize(Decimal("0.01"))
        split[ref] = share
        running += share
    split[refs[-1]] = (envelope - running).quantize(Decimal("0.01"))
    return envelope, split


# ---------------------------------------------------------------------------
# the node outputs
# ---------------------------------------------------------------------------


def _outputs(source: PlanInput, shape: _Shape, consent: tracking.ConsentScope) -> dict[str, Any]:
    envelope, split = _budgets(shape)
    ids = [str(EVIDENCE)]
    ceiling = 420.0
    target = 380.0  # strictly under the ceiling — assertion 3.
    reserve = _money(envelope * Decimal("0.05"))
    allowed = [market for market in consent.markets_allowed if market in shape.markets]

    allocation = [
        {
            "campaign_ref": campaign["campaign_ref"],
            "market": campaign["market"],
            "funnel_stage": "capture" if campaign["is_brand"] else "demand",
            "usd": _money(split[campaign["campaign_ref"]]),
            "pct": float(
                (split[campaign["campaign_ref"]] / envelope * 100).quantize(Decimal("0.01"))
            )
            if envelope
            else 0.0,
            "forecast_cpa_usd": target,
            "target_cpa_usd": target,
            "efficiency": 1.0,
            "est_conv": 30.0,
            "est_clicks": 1_200.0,
            "below_floor": False,
        }
        for campaign in shape.campaigns
    ]

    structure_campaigns = [
        {
            "name": campaign["name"],
            "campaign_ref": campaign["campaign_ref"],
            "type": "search",
            "market": campaign["market"],
            "language": "en",
            "monthly_budget_usd": _money(split[campaign["campaign_ref"]]),
            "daily_budget_usd": _money(split[campaign["campaign_ref"]] / Decimal("30.4")),
            "bid_strategy": "max_conv" if campaign["is_brand"] else "tcpa",
            "target": target,
            # Assertion 5: every non-brand campaign negatives every brand term.
            "negatives": [] if campaign["is_brand"] else list(shape.brand_terms),
            "ad_groups": [
                {
                    "name": campaign["group_name"],
                    "theme": "Brand" if campaign["is_brand"] else "Core",
                    # Assertion 4: an ad group with no landing URL is blocking.
                    "landing_url": f"https://example.com/{campaign['market'].lower()}",
                    "primary_message": "The compliance answer, in one page.",
                    "market": campaign["market"],
                    "coherence": 0.82,
                    "keywords": campaign["keywords"],
                    "negatives": [],
                }
            ],
        }
        for campaign in shape.campaigns
    ]

    # Assertion 8 and PC1: audience channels only where the consent gate said
    # yes. A blocked market still gets search — consent governs audience lists,
    # not keywords — which is the distinction a hand-written fixture blurs.
    # `demand_gen` rather than "remarketing" because that is what the slate's
    # own enum offers, and `_is_audience_channel` matches on it.
    slate = [
        {
            "campaign_type": "search",
            "market": market,
            "campaign_refs": [
                campaign["campaign_ref"]
                for campaign in shape.campaigns
                if campaign["market"] == market
            ],
            "launch_wave": 1,
            "rationale": "Existing demand, measurable, and the cheapest lesson.",
            "prerequisites": [],
            "est_share_of_budget_pct": round(100 / max(len(shape.markets), 1), 2),
            "est_monthly_usd": _money(PER_MARKET_USD),
        }
        for market in shape.markets
    ] + [
        {
            "campaign_type": "demand_gen",
            "market": market,
            "campaign_refs": [],
            "launch_wave": 2,
            "rationale": "A lawful basis is recorded for this market.",
            "prerequisites": ["Publish the audience-list consent notice."],
            "est_share_of_budget_pct": 0.0,
            "est_monthly_usd": 0.0,
        }
        for market in allowed
    ]

    first = shape.campaigns[0] if shape.campaigns else None
    brand_ref = _ref(shape.markets[0], "Brand") if shape.brand_terms else ""

    return {
        "2.1.1": {
            "actions": [
                {
                    "name": "Qualified lead",
                    "category": "qualified_lead",
                    "counting": "one_per_click",
                    "value_model": "fixed",
                    "value_basis": "target_cpl",
                    "rank": 1,
                    # Assertion 2: something has to be primary.
                    "primary": True,
                    "include_in_conversions": True,
                    "rationale": "Sales works every one of these.",
                }
            ],
            "deprecate": [],
            "status": "ok",
            "calc_evidence_ids": ids,
        },
        "2.1.2": {
            "by_segment": [_ceiling("All", ceiling, target)],
            "blended": _ceiling("Blended", ceiling, target),
            "method_notes": "Gross profit over the CAC ratio, times the observed close rate.",
            "status": "ok",
            "calc_evidence_ids": ids,
        },
        "2.1.3": {
            "objectives": [
                {
                    "campaign_ref": campaign["campaign_ref"],
                    "objective": "lead_gen",
                    "primary_kpi": "cpl",
                    # Assertion 3: target under ceiling, on every campaign.
                    "target_value": target,
                    "ceiling_value": ceiling,
                    "basis": "The unit economics 2.1.2 computed.",
                    "confidence": "medium",
                }
                for campaign in shape.campaigns
            ],
            "north_star": {
                "metric": "cpl",
                "target": target,
                "period": "monthly",
                "rationale": "One number for the account.",
            },
            "status": "ok",
            "calc_evidence_ids": ids,
        },
        "2.1.4": {
            "qualified_lead": {
                "required_signals": ["a compliance obligation"],
                "disqualifiers": ["sole trader"],
                "threshold": 5,
            },
            "expected_mql_to_sql_pct": 34.0,
            "sla_response_hours": 4,
            "routing": [{"segment": "All", "owner": "EMEA desk"}],
            "observed_rejection_reasons": ["price"],
            "status": "ok",
            "calc_evidence_ids": ids,
        },
        "2.2.1": {
            "forecast": [
                {
                    "cluster": "core",
                    "market": market,
                    "month": "2026-10",
                    "impressions": 40_000,
                    "ctr_pct": 3.0,
                    "clicks": 1_200.0,
                    "avg_cpc_usd": 4.2,
                    "cvr_pct": 2.5,
                    "conversions": 30.0,
                    "cost_usd": 5_040.0,
                }
                for market in shape.markets
            ],
            "totals": {
                "impressions": 40_000 * max(len(shape.markets), 1),
                "clicks": 1_200.0 * max(len(shape.markets), 1),
                "conversions": 30.0 * max(len(shape.markets), 1),
                "cost_usd": _money(envelope),
                "ctr_pct": 3.0,
                "avg_cpc_usd": 4.2,
                "cvr_pct": 2.5,
            },
            # §18: no forecast service call, so the fallback path is what ran.
            "method": "derived_arithmetic",
            "confidence_band": {"basis": "cpc_range", "low_pct": -20.0, "high_pct": 20.0},
            "method_notes": "Stage 01 volumes at Stage 01 prices; no forecast service call.",
            "status": "ok",
            "degraded_sources": list(source.degraded_sources),
            "calc_evidence_ids": ids,
        },
        "2.2.2": {
            # Assertion 6: anything that does not clear carries a remedy.
            "campaigns": [
                {
                    "campaign_ref": campaign["campaign_ref"],
                    "monthly_budget_usd": _money(split[campaign["campaign_ref"]]),
                    "forecast_cpa_usd": target,
                    "forecast_conv_30d": 30.0,
                    "threshold": 15.0,
                    "verdict": "clears",
                    "bid_strategy_recommended": "max_conv" if campaign["is_brand"] else "tcpa",
                    "remedy": None,
                }
                for campaign in shape.campaigns
            ],
            "structure_verdict": "sound",
            "notes": "Every campaign clears its threshold at the approved split.",
            "status": "ok",
            "calc_evidence_ids": ids,
        },
        "2.2.3": {
            "scenarios": [
                _scenario(name, envelope * factor, reserve)
                for name, factor in (
                    ("cautious", Decimal("0.7")),
                    ("expected", Decimal(1)),
                    ("aggressive", Decimal("1.4")),
                )
            ],
            "recommended": "expected",
            "recommendation_reason": "The only one that funds every market above its floor.",
            "minimum_viable_envelope_usd": _money(envelope * Decimal("0.7")),
            "forecast_monthly_usd": _money(envelope),
            "notes": "Three scenarios from one demand curve.",
            "status": "ok",
            "calc_evidence_ids": ids,
        },
        "2.2.4": {
            "chosen_scenario": "expected",
            "rationale": "Funds every market above its learning floor and holds a reserve.",
            "what_would_change_it": "A close rate under 6% would make the ceiling unaffordable.",
            "envelope": {
                "monthly_cap_usd": _money(envelope),
                "quarterly_cap_usd": _money(envelope * 3),
                "currency": "USD",
                "scenario_total_usd": _money(envelope),
                "unallocated_usd": 0.0,
            },
            "allocation": allocation,
            "experiment_reserve_pct": 5.0,
            "experiment_reserve_usd": reserve,
            "confidence_band": {"basis": "cpc_range", "low_pct": -20.0, "high_pct": 20.0},
            "learning_warnings": [],
            "degraded_sources": list(source.degraded_sources),
            "status": "ok",
            "calc_evidence_ids": ids,
        },
        "2.2.5": {
            "rules": [
                {
                    "id": "R1",
                    "trigger_metric": "cpl",
                    "comparison": "above",
                    "threshold": ceiling,
                    "threshold_basis": "2.1.2's blended max CPL.",
                    "lookback_days": 14.0,
                    "from_campaign": shape.campaigns[-1]["campaign_ref"] if shape.campaigns else "",
                    "to_campaign": first["campaign_ref"] if first else "",
                    "max_shift_pct": 15.0,
                    "max_shift_usd": _money(envelope * Decimal("0.15")),
                    "cooldown_days": 14.0,
                    "requires_human": True,
                    "rationale": "A fortnight above the ceiling is a trend, not noise.",
                }
            ]
            if shape.campaigns
            else [],
            "review_cadence": "fortnightly",
            "notes": "Nothing moves without a person.",
            "status": "ok",
            "calc_evidence_ids": ids,
        },
        "2.3.1": {
            "slate": slate,
            "rejected": [],
            "brand_campaign_ref": brand_ref,
            "notes": "Search first; audiences only where a basis is recorded.",
            "status": "ok",
            "calc_evidence_ids": ids,
        },
        "2.3.2": {
            "pmax": {
                "allowed": False,
                "brand_exclusion_required": bool(shape.brand_terms),
                "account_negatives": list(shape.brand_terms),
            },
            "broad_match": {"allowed_campaigns": [], "guardrails": ["Not before volume exists."]},
            "overlap": [],
            "notes": "Automation is earned, not assumed.",
            "status": "ok",
            "calc_evidence_ids": ids,
        },
        "2.3.3": {
            "brand_terms": [
                {"term": term, "variant_type": "exact_brand"} for term in shape.brand_terms
            ],
            "brand_campaign": {
                "campaign_ref": brand_ref,
                "match_types": ["exact"],
                "budget_pct": float(BRAND_SHARE * 100),
            },
            "negatives_for_nonbrand": list(shape.brand_terms),
            "reporting_rule": "Brand and non-brand are reported separately, always.",
            "competitor_bidding_policy": "No competitor terms in v1.",
            "notes": "Brand demand is not to be credited to non-brand spend.",
            "status": "ok",
            "calc_evidence_ids": ids,
        }
        if shape.brand_terms
        else None,
        "2.4.1": {
            "patterns": dict(PATTERNS),
            "tokens": [
                {"token": key, "allowed_values": list(value), "source": "project markets"}
                for key, value in TOKENS.items()
            ],
            "validator_regex": shape.validator,
            "examples": [campaign["name"] for campaign in shape.campaigns[:3]],
            # Assertion 9's second half. §18: with no account connected there is
            # nothing to collide with, so the check is `skipped` rather than
            # clean — an empty `collisions` list under `checked` would claim a
            # comparison nobody made.
            "collision_check": "skipped",
            "collisions": [],
            "notes": "One convention, compiled into one regex.",
            "status": "ok",
        },
        "2.4.2": {
            "campaigns": structure_campaigns,
            "account_negatives": [],
            "orphan_terms": [],
            "duplicate_terms": [],
            "invalid_names": [],
            "notes": "One term, one ad group.",
            "status": "ok",
            "calc_evidence_ids": ids,
        },
        "2.4.3": {
            "campaigns": [
                {
                    "ref": campaign["campaign_ref"],
                    "name": campaign["name"],
                    "ad_group_count": 1,
                    "keyword_count": len(campaign["keywords"]),
                    "forecast_conv_30d": 30.0,
                    "threshold": 15.0,
                    "verdict": "clears",
                    "action": "ship",
                    "reason": "Above the volume floor at the approved budget.",
                }
                for campaign in shape.campaigns
            ],
            "structure_verdict": "sound",
            "notes": "Every ad group clears its volume floor.",
            "status": "ok",
            "calc_evidence_ids": ids,
        },
        "2.5.1": {
            "primary_source": "google_ads",
            "rationale": "One system owns the number the budget is judged on.",
            "metric_definitions": [
                {
                    "metric": "cpl",
                    "formula": "cost / qualified_leads",
                    "source_field": "conversions",
                    "owner": "growth",
                    "refresh": "daily",
                }
            ],
            "reconciliation": [],
            "known_discrepancies": [],
            "dashboard_spec": {"grain": "campaign", "cadence": "weekly", "fields": ["cpl"]},
            "consent_signal": _consent_signal(consent, shape),
            "prerequisites": [],
            "status": "ok",
            "calc_evidence_ids": ids,
        },
        "2.5.2": {
            "gclid_capture": {
                "point": "the demo request form",
                "form_field": "gclid",
                # §13: the plan names the storage object and the retention
                # window, or the critique refuses it.
                "storage_object": "crm.lead.gclid",
                "present_today": True,
                "basis": "Observed on the live form by the crawler.",
                "retention_days": 90.0,
            },
            "upload": {
                "method": "ads_api",
                "cadence": "daily",
                "lag_days": 2.0,
                "backfill_days": 30.0,
                "headroom_days": 60.0,
                "fits_click_window": True,
                "click_upload_window_days": 90.0,
                "history_limits_backfill": False,
            },
            "stage_map": [
                {
                    "crm_stage": "qualified",
                    "ads_conversion_action": "Qualified lead",
                    "value_field": "acv_usd",
                }
            ],
            "consent": {
                "markets_allowed": allowed,
                "markets_blocked": sorted(consent.markets_blocked),
                "basis": list(consent.basis),
                "markets_unstated": sorted(consent.markets_unstated),
            },
            "prerequisites": [],
            "notes": "Uploads follow the basis, market by market.",
            "status": "ok",
            "calc_evidence_ids": ids,
        },
        "2.5.3": {
            "tests": [
                {
                    "id": "T1",
                    "hypothesis": "A phrase-match tier beats exact-only on cost per lead.",
                    "campaign_ref": first["campaign_ref"],
                    "campaign_name": first["name"],
                    "market": first["market"],
                    "variable": "match_type",
                    "primary_metric": "cpl",
                    "basis": "2.2.1's click forecast at the approved split.",
                    "baseline": target,
                    "mde_pct": 20.0,
                    "required_conv_per_arm": 120,
                    "required_visitors_per_arm": 4_800,
                    "est_days_to_significance": 62,
                    "impact_1_5": 4,
                    "confidence_1_5": 3,
                    "effort_1_5": 2,
                    "ice_score": 3.4,
                    "rank": 1,
                    "reserve_usd": reserve,
                    "funded": True,
                }
            ]
            if first
            else [],
            "reserve_pool_usd": reserve,
            "alpha": 0.05,
            "power": 0.8,
            "notes": "Ranked by ICE, funded until the reserve runs out.",
            "status": "ok",
            "calc_evidence_ids": ids,
        },
    }


def _ceiling(segment: str, ceiling: float, target: float) -> dict[str, Any]:
    """One 2.1.2 row. Both the per-segment list and `blended` are this shape."""
    return {
        "segment": segment,
        "acv_usd": 9_000.0,
        "gross_margin_pct": 72.0,
        "lead_to_won_pct": 8.0,
        "max_cpa_won_usd": 5_200.0,
        "max_cpl_usd": ceiling,
        "target_cpl_usd": target,
        "target_cpa_won_usd": 4_750.0,
        "target_roas": 3.2,
    }


def _scenario(name: str, total: Decimal, reserve: float) -> dict[str, Any]:
    working = total - Decimal(str(reserve))
    return {
        "name": name,
        "monthly_total_usd": _money(total),
        "quarterly_total_usd": _money(total * 3),
        "allocated_usd": _money(working),
        "unallocated_usd": 0.0,
        "working_budget_usd": _money(working),
        "experiment_reserve_usd": reserve,
        "experiment_reserve_pct": 5.0,
        "est_clicks": float(working / Decimal("4.2")),
        "est_conv": float(working / Decimal("380")),
    }


def _consent_signal(consent: tracking.ConsentScope, shape: _Shape) -> dict[str, Any]:
    """§13: an EU market in scope means the consent-signal mechanism is named."""
    eu = {"DE", "FR", "NL", "IE", "IT", "ES", "PL", "SE", "DK", "FI", "BE", "AT"}
    in_scope = sorted(eu & set(shape.markets))
    if not in_scope:
        return {"required": False, "markets": [], "mechanism": None}
    return {"required": True, "markets": in_scope, "mechanism": "consent_mode_v2"}


# ---------------------------------------------------------------------------
# the build
# ---------------------------------------------------------------------------


def _validated(outputs: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Every output through its node's declared model, and back to a dict.

    This is what stops a fixture drifting into a shape no node could produce.
    `mode="json"` because that is how the executor persists a node output and
    therefore what `assemble` really reads — a `Decimal` that survives in
    memory and becomes a string in JSONB is a class of defect this project has
    already shipped once.
    """
    registry = get_registry()
    built: dict[str, dict[str, Any]] = {}
    for node_id, payload in outputs.items():
        if payload is None:
            # A node that did not run. `assemble` reads an absent node as an
            # empty section and says so — which is the same thing a run halted
            # at a gate produces, and is worth a fixture rather than a special
            # case (see `no_brand`).
            continue
        model = registry.spec(node_id).output_model
        assert model is not None, f"node {node_id} declares no output model"
        built[node_id] = model.model_validate(payload).model_dump(mode="json")
    return built


def build(
    source: PlanInput,
    *,
    version: int = 0,
    mutate: Callable[[dict[str, dict[str, Any]]], None] | None = None,
    cite: bool = True,
) -> Built:
    """One `PlanInput` through the deterministic DAG and the real assembler.

    `mutate` and `cite` exist for the negative controls and for nothing else.
    They run **after** the contract validation on purpose: a negative control
    is simulating a node that produced something wrong, and a mutation forced
    back through the node's own model could only ever produce shapes the model
    already permits — which is not where the interesting failures are.
    """
    shape = _shape(source)
    consent = tracking.consent_scope(source.readiness, _markets(source))
    outputs = _validated(_outputs(source, shape, consent))
    if mutate is not None:
        mutate(outputs)

    facts = PlanFacts(
        project_id=source.project_id,
        plan_run_id=PLAN_RUN_ID,
        source=PlanSource(
            research_run_id=source.research_run_id,
            report_id=source.research_report_id,
            # `PlanInput` carries no `acceptance_id` — it is looked up by
            # `ResearchAcceptance.run_id` at run start, and adding it to the
            # input would change `content_hash` and kill cache reuse. A stable
            # id derived from the run keeps the fixture reproducible without
            # inventing a field the contract does not have.
            acceptance_id=uuid.uuid5(ACCEPTANCE_NS, str(source.research_run_id)),
            accepted_by=source.accepted_by,
            accepted_at=source.accepted_at,
            research_schema_version=source.research_schema_version,
            override_reason=source.override_reason,
            launch_readiness=source.launch_readiness,
            degraded_sources=list(source.degraded_sources),
        ),
        calcs=CalcIndex(CALC_ROWS),
        decisions=[],
        constants_version="test",
        generated_at=GENERATED_AT,
        version=version,
    )

    # Assertion 7: a claim with no citation is blocking, so the two the
    # narrative model would have written carry the fixture's evidence id.
    #
    # `cite=False` has to go round pydantic, and that is the finding rather
    # than a workaround: `Claim.evidence_ids` carries `min_length=1`, so an
    # uncited claim **cannot exist in a validated plan** and the critique's
    # branch for it is defence in depth against a future that relaxes the
    # contract. `model_construct` is how a test reaches a branch whose real
    # guard is one layer up — and PT2's own regression test therefore asserts
    # on `Claim`, not on the critique.
    def claim(statement: str, confidence: str) -> Claim:
        if cite:
            return Claim(
                statement=statement,
                evidence_ids=[EVIDENCE],
                confidence=confidence,  # type: ignore[arg-type]
            )
        return Claim.model_construct(
            statement=statement,
            evidence_ids=[],
            confidence=confidence,  # type: ignore[arg-type]
        )

    plan = assemble(
        facts=facts,
        outputs=outputs,
        summary=(
            f"{len(shape.campaigns)} campaign(s) across {len(shape.markets)} market(s), "
            "funded to the approved envelope."
        ),
        assumptions=[claim("The close rate holds at the level the CRM recorded.", "medium")],
        risks=[claim("A competitor entering the auction would raise the CPC.", "low")],
        launch_blockers=source.launch_blockers,
        plan_status="draft",
    )
    return Built(source=source, outputs=outputs, plan=plan, consent=consent)
