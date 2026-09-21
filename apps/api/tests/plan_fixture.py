"""One realistic `CampaignPlan`, shared by every plan-export and critique suite.

Built by hand rather than by running the DAG, and the figures are round so an
assertion can be checked with a calculator: a $40,000 envelope split 62.5/37.5
across two markets, one campaign carrying one ad group carrying two keywords.

**Every `Number` cites the same calc id.** These tests are about rendering and
about the ten assertions, not about provenance — `tests/test_plan_nodes_2_6.py`
is where a figure is traced back to the `PlanCalc` row that produced it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from agent.export.contract import Claim
from agent.export.plan_contract import (
    AccountStructure,
    AllocationLine,
    AutomationBoundaries,
    BrandIsolation,
    CampaignObjective,
    CampaignPlan,
    ChannelSlate,
    ConversionAction,
    Dependency,
    Envelope,
    Experiment,
    ForecastRow,
    GateDecision,
    MeasurementPlan,
    MediaPlan,
    NamingConvention,
    Number,
    Objectives,
    PlannedAdGroup,
    PlannedCampaign,
    PlannedKeyword,
    PlanSource,
    QualifiedLead,
    ReallocationRule,
    Scenario,
    SegmentCeiling,
    SlateEntry,
)

CALC_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
EVIDENCE_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
PROJECT_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
PLAN_RUN_ID = uuid.UUID("44444444-4444-4444-8444-444444444444")
RESEARCH_RUN_ID = uuid.UUID("55555555-5555-4555-8555-555555555555")
GENERATED_AT = datetime(2026, 9, 22, 9, 30, tzinfo=UTC)

#: Anything matching the naming convention below. Written as a real pattern so
#: assertion 9 has something to actually fail against.
VALIDATOR = r"[A-Z]{2} \| [A-Za-z ]+ \| [A-Za-z\-]+"


def number(value: str, unit: str = "usd", label: str = "") -> Number:
    return Number(
        value=Decimal(value),
        unit=unit,  # type: ignore[arg-type]
        calc_evidence_id=CALC_ID,
        confidence="high",
        label=label,
    )


def gate(key: str, node_id: str, name: str, *, status: str = "approved") -> GateDecision:
    return GateDecision(
        gate_key=key,  # type: ignore[arg-type]
        node_id=node_id,
        name=name,
        status=status,  # type: ignore[arg-type]
        decided_by=uuid.UUID("66666666-6666-4666-8666-666666666666"),
        decided_by_name="Soham Sarker",
        decided_at=GENERATED_AT,
        note="Approved.",
    )


def campaign(
    *,
    name: str = "US | Search | Non-brand",
    ref: str = "us-nonbrand",
    market: str = "US",
    monthly: float = 25_000.0,
    keywords: list[PlannedKeyword] | None = None,
    landing_url: str = "https://example.com/sds",
    negatives: list[str] | None = None,
) -> PlannedCampaign:
    return PlannedCampaign(
        name=name,
        campaign_ref=ref,
        type="search",
        market=market,
        language="en",
        monthly_budget_usd=monthly,
        daily_budget_usd=round(monthly / 30.4, 2),
        bid_strategy="tcpa",
        target=900.0,
        locations=["United States"],
        negatives=negatives if negatives is not None else ["acme"],
        ad_groups=[
            PlannedAdGroup(
                name="sds software",
                theme="SDS software",
                landing_url=landing_url,
                primary_message="Compliance without the binder",
                market=market,
                coherence=0.82,
                keywords=keywords
                if keywords is not None
                else [
                    PlannedKeyword(
                        term="sds software",
                        match_type="phrase",
                        forecast_cpc_usd=5.20,
                        search_volume=2_400,
                    ),
                    PlannedKeyword(
                        term="safety data sheet software",
                        match_type="exact",
                        forecast_cpc_usd=6.40,
                        search_volume=880,
                    ),
                ],
                negatives=["free"],
            )
        ],
    )


def plan(**overrides: Any) -> CampaignPlan:
    """A sound plan: four gates approved, allocation sums, nothing blocking."""
    base: dict[str, Any] = {
        "project_id": PROJECT_ID,
        "plan_run_id": PLAN_RUN_ID,
        "version": 0,
        "generated_at": GENERATED_AT,
        "source": PlanSource(
            research_run_id=RESEARCH_RUN_ID,
            report_id=uuid.uuid4(),
            acceptance_id=uuid.uuid4(),
            accepted_by=uuid.uuid4(),
            accepted_at=GENERATED_AT,
            research_schema_version="1.0",
            launch_readiness="go_with_fixes",
            degraded_sources=["dataforseo"],
        ),
        "executive_summary": "A first paid-search plan for SDS Manager in the US and Germany.",
        "plan_status": "ready_to_freeze",
        "objectives": Objectives(
            north_star_metric="cpl",
            north_star_target=number("900", "usd", "north star target"),
            north_star_period="monthly",
            conversion_actions=[
                ConversionAction(
                    name="Demo request",
                    category="SUBMIT_LEAD_FORM",
                    counting="one_per_click",
                    value_model="fixed",
                    assigned_value_usd=120.0,
                    lead_to_won_rate_pct=12.0,
                    rank=1,
                    primary=True,
                )
            ],
            unit_economics=[
                SegmentCeiling(
                    segment="enterprise",
                    acv_usd=60_000,
                    gross_margin_pct=80,
                    lead_to_won_pct=12,
                    max_cpa_won_usd=16_000,
                    max_cpl_usd=1_920,
                    target_cpl_usd=1_632,
                    target_roas=3.0,
                    payback_months=3.0,
                )
            ],
            blended_max_cpl=number("1920", "usd", "blended max CPL"),
            blended_target_cpl=number("1632", "usd", "blended target CPL"),
            method_notes="Computed from CRM won deals over 18 months.",
            campaign_objectives=[
                CampaignObjective(
                    campaign_ref="us-nonbrand",
                    objective="lead_gen",
                    primary_kpi="cpl",
                    target_value=900.0,
                    ceiling_value=1_920.0,
                    basis="2.1.2 enterprise ceiling",
                    confidence="high",
                    evidence_ids=[EVIDENCE_ID],
                ),
                CampaignObjective(
                    campaign_ref="de-nonbrand",
                    objective="lead_gen",
                    primary_kpi="cpl",
                    target_value=950.0,
                    ceiling_value=1_920.0,
                    basis="2.1.2 enterprise ceiling",
                    confidence="medium",
                    evidence_ids=[EVIDENCE_ID],
                ),
            ],
            qualified_lead=QualifiedLead(
                required_signals=["company size > 50"],
                disqualifiers=["student"],
                threshold=3,
            ),
            expected_mql_to_sql_pct=34.0,
            sla_response_hours=4,
            calc_evidence_ids=[CALC_ID],
        ),
        "media_plan": MediaPlan(
            chosen_scenario="expected",
            rationale="Funds both markets above the per-campaign floor.",
            what_would_change_it="A CPC 20% above forecast in Germany.",
            envelope=Envelope(
                monthly_cap=number("40000", "usd", "monthly envelope"),
                quarterly_cap=number("120000", "usd", "quarterly envelope"),
                currency="USD",
            ),
            experiment_reserve=number("4000", "usd", "experiment reserve"),
            allocation=[
                AllocationLine(
                    campaign_ref="us-nonbrand",
                    market="US",
                    funnel_stage="mid",
                    usd=25_000.0,
                    pct=62.5,
                    forecast_cpa_usd=820.0,
                    target_cpa_usd=900.0,
                    est_clicks=4_800.0,
                    est_conv=30.5,
                ),
                AllocationLine(
                    campaign_ref="de-nonbrand",
                    market="DE",
                    funnel_stage="mid",
                    usd=15_000.0,
                    pct=37.5,
                    forecast_cpa_usd=910.0,
                    target_cpa_usd=950.0,
                    est_clicks=2_900.0,
                    est_conv=16.5,
                ),
            ],
            scenarios=[
                Scenario(
                    name="expected",
                    monthly_total_usd=40_000.0,
                    quarterly_total_usd=120_000.0,
                    est_clicks=7_700.0,
                    est_conv=47.0,
                    est_cpa=851.0,
                    est_pipeline_usd=2_820_000.0,
                    allocation=[
                        AllocationLine(
                            campaign_ref="us-nonbrand",
                            market="US",
                            funnel_stage="mid",
                            usd=25_000.0,
                            pct=62.5,
                            forecast_cpa_usd=820.0,
                            est_conv=30.5,
                        )
                    ],
                ),
                Scenario(name="cautious", monthly_total_usd=24_000.0, est_conv=28.0),
            ],
            forecast=[
                ForecastRow(
                    cluster="core",
                    market="US",
                    month="Jan",
                    impressions=120_000,
                    ctr_pct=4.0,
                    clicks=4_800,
                    avg_cpc_usd=5.2,
                    cvr_pct=0.63,
                    conversions=30.5,
                    cost_usd=25_000,
                    cpa_usd=820.0,
                ),
                ForecastRow(
                    cluster="core",
                    market="DE",
                    month="Feb",
                    impressions=72_000,
                    ctr_pct=4.0,
                    clicks=2_900,
                    avg_cpc_usd=5.17,
                    cvr_pct=0.57,
                    conversions=16.5,
                    cost_usd=15_000,
                    cpa_usd=910.0,
                ),
            ],
            forecast_method="google_forecast",
            impression_share_headroom_pct=41.0,
            learning_warnings=[
                {
                    "campaign_ref": "de-nonbrand",
                    "verdict": "marginal",
                    "remedy": "switch_strategy",
                    "forecast_conv_30d": 16.5,
                    "threshold": 30,
                }
            ],
            reallocation_rules=[
                ReallocationRule(
                    id="R1",
                    trigger_metric="cpa",
                    comparison="gt",
                    threshold=950.0,
                    lookback_days=14,
                    from_campaign="de-nonbrand",
                    to_campaign="us-nonbrand",
                    max_shift_pct=20.0,
                    cooldown_days=14.0,
                    requires_human=True,
                    rationale="Germany is the unproven market.",
                )
            ],
            review_cadence="fortnightly",
            degraded_sources=["dataforseo"],
            calc_evidence_ids=[CALC_ID],
        ),
        "channel_slate": ChannelSlate(
            slate=[
                SlateEntry(
                    campaign_type="search",
                    market="US",
                    campaign_refs=["us-nonbrand"],
                    launch_wave=1,
                    rationale="Proven demand and a page that converts.",
                    entry_criteria=["tracking verified"],
                    prerequisites=["Publish the comparison page"],
                    est_share_of_budget_pct=62.5,
                ),
                SlateEntry(
                    campaign_type="search",
                    market="DE",
                    campaign_refs=["de-nonbrand"],
                    launch_wave=2,
                    rationale="Unproven; wave 2 after the US reads out.",
                    est_share_of_budget_pct=37.5,
                ),
            ],
            rejected=[{"campaign_type": "performance_max", "why_not": "No conversion history."}],
            brand_isolation=BrandIsolation(
                brand_terms=[{"term": "sds manager", "variant_type": "exact"}],
                brand_campaign_ref="us-brand",
                match_types=["exact", "phrase"],
                budget_pct=8.0,
                negatives_for_nonbrand=["sds manager"],
                reporting_rule="Brand is reported separately from non-brand.",
                competitor_bidding_policy="No competitor bidding in year one.",
            ),
            automation_boundaries=AutomationBoundaries(
                pmax={"allowed": False, "brand_exclusion_required": True},
                broad_match={"allowed_campaigns": ["us-nonbrand"], "guardrails": ["tCPA only"]},
            ),
            calc_evidence_ids=[CALC_ID],
        ),
        "account_structure": AccountStructure(
            naming_convention=NamingConvention(
                patterns={"campaign": "{market} | {channel} | {brand_split}"},
                validator_regex=VALIDATOR,
                examples=["US | Search | Non-brand"],
                collision_check="checked",
            ),
            campaigns=[campaign(negatives=["acme", "sds manager"])],
            account_negatives=["free", "jobs"],
            orphan_terms=["msds jobs"],
            volume_check=[{"ref": "us-nonbrand", "verdict": "clears", "action": "ship"}],
            structure_verdict="sound",
            calc_evidence_ids=[CALC_ID],
        ),
        "measurement_plan": MeasurementPlan(
            primary_source="google_ads",
            rationale="Ads is where bids are set, so Ads settles an argument.",
            metric_definitions=[
                {
                    "metric": "CPL",
                    "formula": "cost / leads",
                    "source_field": "conversions",
                    "owner": "growth",
                    "refresh": "daily",
                }
            ],
            reconciliation=[
                {
                    "metric": "CPL",
                    "systems": "ads vs crm",
                    "tolerance_pct": 12.0,
                    "cadence": "weekly",
                    "owner": "growth",
                }
            ],
            upload={
                "method": "manual_csv",
                "cadence": "monthly",
                "lag_days": 30,
                "backfill_days": 60,
            },
            gclid_capture={"present_today": False, "basis": "No form stores a click id."},
            consent_markets_allowed=["US"],
            consent_markets_blocked=["FR"],
            consent_basis=["contract, art. 6(1)(b)"],
            calc_evidence_ids=[CALC_ID],
        ),
        "experiment_backlog": [
            Experiment(
                id="us-nonbrand:bid_strategy",
                hypothesis="If we move to tCPA then CPA falls, because the campaign is marginal.",
                campaign_ref="us-nonbrand",
                campaign_name="US | Search | Non-brand",
                market="US",
                variable="bid_strategy",
                primary_metric="cpa",
                baseline=5.0,
                mde_pct=20.0,
                required_conv_per_arm=408,
                est_days_to_significance=41,
                impact_1_5=5,
                confidence_1_5=4,
                effort_1_5=1,
                ice_score=20.0,
                rank=1,
                earliest_wave=1,
                reserve_usd=4_000.0,
                funded=True,
            ),
            Experiment(
                id="us-nonbrand:landing_page",
                hypothesis="If we rebuild the page then CVR rises, because it buries the proof.",
                campaign_ref="us-nonbrand",
                campaign_name="US | Search | Non-brand",
                market="US",
                variable="landing_page",
                primary_metric="cvr",
                baseline=5.0,
                required_conv_per_arm=408,
                est_days_to_significance=62,
                ice_score=2.4,
                rank=2,
                reserve_usd=0.0,
                funded=False,
            ),
        ],
        "decisions": [
            gate("G1", "2.1.3", "campaign_targets"),
            gate("G2", "2.1.4", "lead_definition"),
            gate("G3", "2.2.4", "budget_allocation"),
            gate("G4", "2.3.1", "channel_slate"),
        ],
        "open_dependencies": [
            Dependency(
                task="Capture the Google click id on every form.",
                owner="unassigned",
                blocking=True,
                source="2.5.2",
            ),
            Dependency(
                task="Publish the comparison page",
                owner="content",
                blocking=False,
                source="2.3.1",
            ),
        ],
        "assumptions": [
            Claim(
                statement="German CPCs behave like the US within 20%.",
                evidence_ids=[EVIDENCE_ID],
                confidence="medium",
            )
        ],
        "risks": [
            Claim(
                statement="A competitor could start bidding on our brand.",
                evidence_ids=[EVIDENCE_ID],
                confidence="low",
            )
        ],
        "constants_version": "2026.09.4",
        "cost_usd": 4.12,
        "critique_issues": [],
    }
    base.update(overrides)
    return CampaignPlan.model_validate(base)


def frozen_plan(**overrides: Any) -> CampaignPlan:
    """The same plan, sealed. §14's frozen exports carry version and no watermark."""
    return plan(plan_status="frozen", version=3, **overrides)
