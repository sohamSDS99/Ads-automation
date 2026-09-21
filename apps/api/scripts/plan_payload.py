"""A §12 `CampaignPlan` payload, for reading against before 2.6.1 writes real ones.

S2-P6c builds the read surface of Stage 02 — Plan Viewer, structure tree, freeze
dialog, compare. The thing it reads is `CampaignPlan.payload`, which node 2.6.1
fills, and 2.6.1 ships in S2-P5b. A read surface with nothing to read cannot be
tested, and "it renders once the writer lands" is the claim every
written-never-read defect in this project was hiding behind.

So this module is the payload, built from the §12 contract and the node output
models of 2.1–2.5 that 2.6.1 will assemble it from. **One definition**, imported
by the unit tests, the integration tests and the browser check, because a
fixture defined twice is a fixture that disagrees with itself about how many
keywords a plan has.

It is deliberately parameterised by size. §15.4 rule 1 sets a 16 ms frame budget
at 40 campaigns / 400 ad groups / 4,000 keywords, and a tree that is only ever
tested at four campaigns is a tree whose virtualisation was never exercised.

What it is NOT: a substitute for the real thing. When S2-P5b commits
`plan_contract.py`, the field list moves there and this becomes a builder for
*that* model. Until then, every field below is traceable to a line of §12 or to
a node output model, and the frontend degrades per section rather than requiring
any of them.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any


#: §12 `Number`. A figure the reader can trace back to a `PlanCalc` row.
def number(
    value: float,
    unit: str,
    calc_evidence_id: uuid.UUID | str | None = None,
    confidence: str = "high",
    label: str | None = None,
) -> dict[str, Any]:
    return {
        "value": value,
        "unit": unit,
        "calc_evidence_id": str(calc_evidence_id) if calc_evidence_id else None,
        "confidence": confidence,
        "label": label,
    }


MARKETS = ("US", "DE", "UK", "FR")
FUNNEL = ("capture", "create")
CHANNELS = ("search", "performance_max", "demand_gen", "display_remarketing", "youtube")
MATCH_TYPES = ("exact", "phrase", "broad")

#: The convention 2.4.1 emits and 2.4.2's names are held to.
VALIDATOR_REGEX = r"^[a-z]+-[a-z]{2}-(capture|create)-[a-z0-9]+$"


def campaign_plan_payload(
    *,
    project_id: uuid.UUID | str,
    plan_run_id: uuid.UUID | str,
    research_run_id: uuid.UUID | str,
    report_id: uuid.UUID | str,
    acceptance_id: uuid.UUID | str,
    accepted_by: uuid.UUID | str,
    decider_id: uuid.UUID | str,
    calc_evidence_id: uuid.UUID | str | None = None,
    version: int = 0,
    plan_status: str = "ready_to_freeze",
    campaigns: int = 4,
    ad_groups_per_campaign: int = 3,
    keywords_per_ad_group: int = 8,
    envelope_usd: float = 48_000.0,
    months: int = 12,
    degraded_sources: tuple[str, ...] = (),
    invalid_names: tuple[str, ...] = (),
    duplicate_terms: tuple[str, ...] = (),
    #: False omits `invalid_names` and `duplicate_terms` entirely — the state
    #: that means node 2.4.2 never checked, as distinct from checking and
    #: finding nothing. The reader must not render the two the same way.
    declare_findings: bool = True,
    seed_now: datetime | None = None,
) -> dict[str, Any]:
    """The whole §12 object, sized to taste.

    `seed_now` is threaded rather than read from the clock so two payloads built
    for a diff differ only where they were asked to differ. A fixture that
    stamps `utcnow()` reports `generated_at` as changed on every comparison and
    teaches the reader to ignore the scalar section.
    """
    now = seed_now or datetime(2026, 9, 21, 9, 30, tzinfo=UTC)
    tree = _structure(
        campaigns=campaigns,
        ad_groups_per_campaign=ad_groups_per_campaign,
        keywords_per_ad_group=keywords_per_ad_group,
        envelope_usd=envelope_usd,
        invalid_names=invalid_names,
        duplicate_terms=duplicate_terms,
        declare_findings=declare_findings,
    )
    allocation = _allocation(tree["campaigns"], envelope_usd)

    return {
        "schema_version": "1.0",
        "project_id": str(project_id),
        "plan_run_id": str(plan_run_id),
        "version": version,
        "generated_at": now.isoformat(),
        "plan_status": plan_status,
        "source": {
            "research_run_id": str(research_run_id),
            "report_id": str(report_id),
            "acceptance_id": str(acceptance_id),
            "accepted_by": str(accepted_by),
            "accepted_at": (now - timedelta(days=3)).isoformat(),
            "degraded_sources": list(degraded_sources),
        },
        "executive_summary": (
            "Four campaigns across two markets carry a $48,000 monthly envelope at a blended "
            "target CPA of $312, against a ceiling of $410. Capture demand is funded first "
            "because it converts at 3.1% against create demand's 0.8%, and the create half of "
            "the slate launches in wave two once the measurement prerequisites close. The plan "
            "leaves 8% of the envelope in an experiment reserve so the three tests in the "
            "backlog can run without reopening the budget gate."
        ),
        "objectives": _objectives(tree["campaigns"], calc_evidence_id),
        "media_plan": _media_plan(
            allocation=allocation,
            envelope_usd=envelope_usd,
            months=months,
            now=now,
            calc_evidence_id=calc_evidence_id,
        ),
        "channel_slate": _channel_slate(tree["campaigns"]),
        "account_structure": tree,
        "measurement_plan": _measurement_plan(decider_id),
        "experiment_backlog": _backlog(calc_evidence_id),
        "decisions": _decisions(decider_id, now),
        "open_dependencies": [
            {
                "statement": "Offline conversion import needs a CRM service account in DE.",
                "owner": "Revenue operations",
                "blocking": False,
                "due_at": (now + timedelta(days=21)).isoformat(),
                "evidence_ids": [str(report_id)],
            }
        ],
        "assumptions": [
            {
                "statement": "Average deal size holds at $4,100 across both markets.",
                "evidence_ids": [str(report_id)],
            },
            {
                "statement": "Google's 30-conversion learning threshold applies per campaign.",
                "evidence_ids": [str(report_id)],
            },
        ],
        "risks": [
            {
                "statement": "DE keyword coverage rests on one priced term with no landing page.",
                "evidence_ids": [str(report_id)],
                "severity": "medium",
            }
        ],
        "critique_issues": [
            {
                "severity": "warning",
                "section": "channel_slate",
                "finding": "Wave 2 has no named owner for its landing pages.",
                "fix": "Assign an owner before wave 1 exits.",
                "check": "reader",
            },
            {
                "severity": "note",
                "section": "measurement_plan",
                "finding": "Two measurement prerequisites are still open.",
                "fix": "Close them before the first offline upload.",
                "check": "10_launch_blockers",
            },
        ],
        "constants_version": "2026.09.4",
        "cost_usd": 4.37,
    }


def _objectives(
    campaigns: list[dict[str, Any]], calc_evidence_id: uuid.UUID | str | None
) -> dict[str, Any]:
    """Target per campaign with 2.1.2's ceiling alongside it (§15.3 D).

    The ceiling travels with the target rather than in a note further down,
    because a target above its ceiling is the single thing this section exists
    to make visible.
    """
    return {
        "blended_target_cpl": number(312.0, "usd", calc_evidence_id),
        "blended_max_cpa_won": number(410.0, "usd", calc_evidence_id),
        "blended_max_cpl": number(365.0, "usd", calc_evidence_id),
        "north_star_target": number(154.0, "count", calc_evidence_id),
        "lead_definition": {
            "qualified_as": "Demo requested with a named account and >50 employees",
            "scoring": [
                {"signal": "employee_count", "weight": 0.4},
                {"signal": "pricing_page_visit", "weight": 0.35},
                {"signal": "industry_match", "weight": 0.25},
            ],
        },
        "kpis": [
            {
                "name": "Qualified leads per month",
                "target": number(154.0, "count", calc_evidence_id),
            },
            {"name": "Blended CPA", "target": number(312.0, "usd", calc_evidence_id)},
            {"name": "Pipeline per month", "target": number(631_400.0, "usd", calc_evidence_id)},
        ],
        "campaign_targets": [
            {
                "campaign_ref": campaign["campaign_ref"],
                "market": campaign["market"],
                "metric": "cost_per_acquisition",
                "target": number(round(240.0 + index * 37.5, 2), "usd", calc_evidence_id),
                "ceiling": number(410.0, "usd", calc_evidence_id),
                # Deliberately over its ceiling on one campaign: the screen has
                # to be able to show that state, and a fixture in which nothing
                # is ever wrong tests only the happy path.
                "over_ceiling": index == 2,
                "rationale": (
                    "Capture demand converts at 3.1%, so the target sits below the blended figure."
                    if campaign["campaign_ref"].endswith("capture")
                    else "Create demand carries a longer path, so the target is set above blended."
                ),
            }
            for index, campaign in enumerate(campaigns)
        ],
    }


def _allocation(campaigns: list[dict[str, Any]], envelope_usd: float) -> list[dict[str, Any]]:
    """The split, summing to the envelope within §12 invariant 4's ±0.5%.

    The last row absorbs the rounding remainder rather than every row carrying
    an even share: an allocation that does not add up is a `422` from the budget
    gate, and a fixture that cannot be approved is not a fixture of an
    approvable plan.
    """
    if not campaigns:
        return []
    share = envelope_usd / len(campaigns)
    rows: list[dict[str, Any]] = []
    for index, campaign in enumerate(campaigns):
        usd = (
            round(share, 2)
            if index < len(campaigns) - 1
            else round(envelope_usd - round(share, 2) * (len(campaigns) - 1), 2)
        )
        rows.append(
            {
                "campaign_ref": campaign["campaign_ref"],
                "market": campaign["market"],
                "funnel_stage": FUNNEL[index % len(FUNNEL)],
                "usd": usd,
                "pct": round(usd / envelope_usd * 100, 2),
                "forecast_cpa_usd": round(268.0 + index * 19.0, 2),
                "target_cpa_usd": round(240.0 + index * 37.5, 2),
                "efficiency": round((240.0 + index * 37.5) / (268.0 + index * 19.0), 3),
                "est_conv": round(usd / (268.0 + index * 19.0), 1),
                "est_clicks": int(usd / 4.2),
                "avg_cpc_usd": 4.2,
                "min_spend_usd": 3_000.0,
                "max_spend_usd": None,
                "floor_applied": index == 3,
                "cap_applied": False,
                "below_floor": False,
            }
        )
    return rows


def _media_plan(
    *,
    allocation: list[dict[str, Any]],
    envelope_usd: float,
    months: int,
    now: datetime,
    calc_evidence_id: uuid.UUID | str | None,
) -> dict[str, Any]:
    monthly = [
        {
            "month": f"{(now.year + (now.month - 1 + index) // 12)}-"
            f"{(now.month - 1 + index) % 12 + 1:02d}",
            "cost_usd": round(envelope_usd * (0.72 + 0.03 * min(index, 9)), 2),
            "clicks": int(envelope_usd * (0.72 + 0.03 * min(index, 9)) / 4.2),
            "conversions": round(envelope_usd * (0.72 + 0.03 * min(index, 9)) / 290.0, 1),
            "cpa_usd": round(290.0 - index * 1.4, 2),
        }
        for index in range(months)
    ]
    return {
        "envelope": {
            "monthly_cap": number(envelope_usd, "usd", calc_evidence_id, label="Monthly envelope"),
            "quarterly_cap": number(
                envelope_usd * 3, "usd", calc_evidence_id, label="Quarterly envelope"
            ),
            "currency": "USD",
            "scenario_total_usd": envelope_usd,
            "unallocated_usd": 0.0,
        },
        "selected_scenario": "expected",
        "recommended_scenario": "expected",
        "recommendation_reason": (
            "Expected clears every campaign's learning threshold inside one month; cautious "
            "leaves two campaigns short."
        ),
        "scenarios": [
            {
                "name": name,
                "monthly_total_usd": round(envelope_usd * factor, 2),
                "quarterly_total_usd": round(envelope_usd * factor * 3, 2),
                "allocated_usd": round(envelope_usd * factor, 2),
                "unallocated_usd": 0.0,
                "working_budget_usd": round(envelope_usd * factor * 0.92, 2),
                "experiment_reserve_usd": round(envelope_usd * factor * 0.08, 2),
                "experiment_reserve_pct": 8.0,
                "est_clicks": int(envelope_usd * factor / 4.2),
                "est_conv": round(envelope_usd * factor / 290.0, 1),
                "est_cpa": 290.0,
                "est_pipeline_usd": round(envelope_usd * factor / 290.0 * 4_100.0, 2),
                "allocation": allocation,
                "floor_applied": name == "cautious",
                "headroom_capped": name == "aggressive",
                "case_for": f"{name.title()} funds the slate at {int(factor * 100)}% of expected.",
                "case_against": "Leaves the create half of the slate unfunded until wave two."
                if name == "cautious"
                else "Runs ahead of what the measurement plan can attribute.",
            }
            for name, factor in (("cautious", 0.7), ("expected", 1.0), ("aggressive", 1.35))
        ],
        "allocation": allocation,
        "monthly_totals": monthly,
        "confidence_band": {"basis": "cpc_range", "low_pct": -12.0, "high_pct": 18.0},
        "experiment_reserve": number(
            round(envelope_usd * 0.08, 2), "usd", calc_evidence_id, label="Experiment reserve"
        ),
        "reallocation_rules": [
            "Move up to 15% of a campaign's monthly budget to another in the same market when "
            "its 14-day CPA exceeds target by 25%.",
            "Never reallocate out of a campaign inside its first 30 conversions.",
            "The experiment reserve is not reallocated to working budget without a new G3.",
        ],
    }


def _channel_slate(campaigns: list[dict[str, Any]]) -> dict[str, Any]:
    entries = [
        {
            "campaign_ref": campaign["campaign_ref"],
            "market": campaign["market"],
            "channel": CHANNELS[index % len(CHANNELS)],
            "wave": index % 3 + 1,
            "entry_criteria": (
                "Conversion tracking verified in this market and a landing page live."
            ),
            "exit_criteria": "30 conversions in 30 days, or CPA above ceiling for 14 days.",
            "rationale": "Search first: the demand is already expressed and the intent is priced.",
        }
        for index, campaign in enumerate(campaigns)
    ]
    return {
        "entries": entries,
        "waves": [
            {
                "wave": wave,
                "label": f"Wave {wave}",
                "starts_week": (wave - 1) * 4 + 1,
                "ends_week": wave * 4,
                "campaign_refs": [
                    entry["campaign_ref"] for entry in entries if entry["wave"] == wave
                ],
                "gate": "Wave 2 does not start until wave 1 clears its learning threshold.",
            }
            for wave in (1, 2, 3)
        ],
    }


def _measurement_plan(decider_id: uuid.UUID | str) -> dict[str, Any]:
    return {
        "source_of_truth": "crm",
        "rationale": (
            "The CRM settles an argument because a qualified lead is a CRM state, not an ad "
            "click. Google Ads may differ by up to 12% on conversion count."
        ),
        "tolerance_pct": 12.0,
        "metric_definitions": [
            {
                "metric": "qualified_lead",
                "definition": "Demo requested, named account, >50 employees, not a competitor.",
                "system": "crm",
                "owner": "Revenue operations",
            },
            {
                "metric": "cost_per_acquisition",
                "definition": "Ad spend / qualified leads, attributed on the click date.",
                "system": "google_ads",
                "owner": "Paid media",
            },
            {
                "metric": "pipeline",
                "definition": "Qualified leads x average deal size at the stage they reached.",
                "system": "crm",
                "owner": "Revenue operations",
            },
        ],
        "offline_conversion_plan": {
            "gclid_capture": {
                "storage_object": "crm.lead.gclid",
                "retention_days": 90,
                "consent_basis": "legitimate_interest",
            },
            "upload": {
                "mechanism": "Google Ads offline conversion import",
                "frequency": "daily",
                "stages": [
                    {"crm_stage": "demo_requested", "conversion_action": "Qualified lead"},
                    {"crm_stage": "opportunity", "conversion_action": "Opportunity"},
                    {"crm_stage": "won", "conversion_action": "Closed won"},
                ],
            },
            "consent": {"markets_allowed": ["US", "UK"], "markets_blocked": ["DE", "FR"]},
        },
        "prerequisites": [
            {
                "what": "Enhanced conversions enabled on the demo form",
                "owner": "Web engineering",
                "blocking": True,
                "status": "open",
                "owner_id": str(decider_id),
            },
            {
                "what": "GCLID column added to the CRM lead object",
                "owner": "Revenue operations",
                "blocking": True,
                "status": "open",
                "owner_id": str(decider_id),
            },
            {
                "what": "EU consent signal confirmed for DE and FR",
                "owner": "Legal",
                "blocking": False,
                "status": "in_progress",
                "owner_id": str(decider_id),
            },
        ],
        "known_discrepancies": [
            {
                "between": "google_ads vs crm",
                "expected_pct": 12.0,
                "reason": "Click-date attribution against CRM created-date.",
            }
        ],
        "dashboards": [
            {"name": "Paid acquisition weekly", "system": "looker", "audience": "Marketing"},
        ],
    }


def _backlog(calc_evidence_id: uuid.UUID | str | None) -> list[dict[str, Any]]:
    """The ranked test backlog (§15.3 D, *Test backlog*).

    `required_conv_per_arm` and `est_days_to_significance` are the two figures
    the table exists to show: a test that cannot finish inside the quarter is one
    nobody should schedule, and that is only visible when both are on the row.
    One entry below cannot, on purpose.
    """
    tests = [
        ("Broad match on capture terms", 9, 7, 8, 384, 41),
        ("Landing page: pricing above the fold", 8, 6, 9, 512, 63),
        ("Value-based bidding on closed-won", 9, 4, 3, 1_180, 147),
    ]
    return [
        {
            "name": name,
            "hypothesis": f"{name} lifts qualified-lead rate without raising CPA past target.",
            "metric": "qualified_lead_rate",
            "arms": 2,
            "ice": {
                "impact": impact,
                "confidence": confidence,
                "ease": ease,
                "score": round(impact * confidence * ease / 100, 2),
            },
            "required_conv_per_arm": number(float(conv), "count", calc_evidence_id),
            "est_days_to_significance": number(float(days), "days", calc_evidence_id),
            "fits_in_quarter": days <= 90,
            "reserve_usd": 1_280.0,
        }
        for name, impact, confidence, ease, conv, days in tests
    ]


def _decisions(decider_id: uuid.UUID | str, now: datetime) -> list[dict[str, Any]]:
    """Exactly four, one per gate, each approved — §12 invariant 3."""
    gates = (
        ("G1", "2.1.3", "Campaign targets", False),
        ("G2", "2.1.4", "Lead definition", False),
        ("G3", "2.2.5", "Budget allocation", True),
        ("G4", "2.3.3", "Channel slate", False),
    )
    return [
        {
            "gate_key": gate,
            "node_id": node,
            "title": title,
            "status": "approved",
            "decider": str(decider_id),
            "decided_at": (now - timedelta(hours=6 - index)).isoformat(),
            "note": "Approved with the DE floor raised to $3,000." if edited else "Approved.",
            "edited": edited,
        }
        for index, (gate, node, title, edited) in enumerate(gates)
    ]


def _structure(
    *,
    campaigns: int,
    ad_groups_per_campaign: int,
    keywords_per_ad_group: int,
    envelope_usd: float,
    invalid_names: tuple[str, ...],
    duplicate_terms: tuple[str, ...] = (),
    declare_findings: bool = True,
) -> dict[str, Any]:
    """The campaign -> ad group -> keyword tree, as 2.4.2 emits it.

    One campaign per `(campaign_ref, market)`, which is what makes the ref alone
    a non-identity — the reason the structure endpoint's cursor and the diff's
    keys are both `ref@market`.
    """
    rows: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    per_campaign_budget = round(envelope_usd / max(campaigns, 1), 2)

    for index in range(campaigns):
        market = MARKETS[index % len(MARKETS)]
        stage = FUNNEL[index % len(FUNNEL)]
        theme = f"theme{index // len(MARKETS) + 1}"
        ref = f"{theme}-{stage}"
        name = f"{theme}-{market.lower()}-{stage}-{index:02d}"
        groups = [
            {
                "name": f"{name}-ag{group:02d}",
                "theme": f"{theme} {group}",
                "landing_url": f"https://example.com/{market.lower()}/{theme}-{group}",
                "primary_message": (
                    "Compliance reporting your auditors already accept."
                    if group % 2 == 0
                    else "Ship the SDS binder in a day, not a quarter."
                ),
                "market": market,
                "coherence": round(0.68 + (group % 5) * 0.06, 2),
                "negatives": ["free", "template"] if group == 0 else [],
                "keywords": [
                    {
                        "term": f"{theme} {stage} term {group}-{word}",
                        "match_type": MATCH_TYPES[word % len(MATCH_TYPES)],
                        "forecast_cpc_usd": round(2.4 + (word % 7) * 0.45, 2),
                        "search_volume": 120 + (word % 11) * 90,
                    }
                    for word in range(keywords_per_ad_group)
                ],
            }
            for group in range(ad_groups_per_campaign)
        ]
        rows.append(
            {
                "campaign_ref": ref,
                "name": name,
                "type": "search" if index % 3 else "performance_max",
                "market": market,
                "language": {"US": "en", "UK": "en", "DE": "de", "FR": "fr"}[market],
                "monthly_budget_usd": per_campaign_budget,
                "daily_budget_usd": round(per_campaign_budget / 30.4, 2),
                "bid_strategy": "maximize_conversions" if index % 2 else "target_cpa",
                "target": round(240.0 + index * 37.5, 2) if index % 2 else None,
                "locations": [market],
                "negatives": ["competitor brand"] if index == 0 else [],
                "ad_groups": groups,
            }
        )
        conversions = round(per_campaign_budget / (268.0 + index * 19.0), 1)
        # One campaign below its threshold, on purpose: the badge has a state
        # for it and §15.3 D asks for it per campaign.
        checks.append(
            {
                "ref": ref,
                "name": name,
                "ad_group_count": len(groups),
                "keyword_count": len(groups) * keywords_per_ad_group,
                "forecast_clicks_30d": round(per_campaign_budget / 4.2, 1),
                "forecast_conv_30d": conversions,
                "budget_to_cpc_ratio": round(per_campaign_budget / 4.2 / 30.4, 2),
                "threshold": 30.0,
                "verdict": "clears" if conversions >= 30 else "below_threshold",
                "remedy": None if conversions >= 30 else "Merge into the sibling capture campaign.",
                "action": "launch" if conversions >= 30 else "merge",
                "reason": (
                    f"{conversions} forecast conversions in 30 days against a threshold of 30."
                ),
            }
        )

    return {
        "campaigns": rows,
        "volume_check": checks,
        "structure_verdict": "launch_ready"
        if all(check["verdict"] == "clears" for check in checks)
        else "launch_with_remedies",
        "naming_convention": {
            "validator_regex": VALIDATOR_REGEX,
            "collision_check": "checked",
        },
        "invalid_names": list(invalid_names) if declare_findings else None,
        "duplicate_terms": list(duplicate_terms) if declare_findings else None,
        "account_negatives": ["jobs", "salary", "free download", "crack"],
        "orphan_terms": ["sds authoring for schools"],
        "notes": "Brand terms appear only in the brand campaign; no term appears twice.",
    }
