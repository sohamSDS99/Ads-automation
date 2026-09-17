"""Write the ten golden fixtures. Run once; the JSON is what ships.

Built through the real output models rather than typed as JSON by hand, so a
fixture cannot be born invalid — the schema assertion in `test_eval.py` then
does what it is for, which is catching drift *later*, when a prompt changes and
a node starts emitting something its own contract no longer accepts.

    uv run python tests/eval/build_fixtures.py
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parent / "fixtures"

#: Deterministic ids. A fixture regenerated tomorrow should be byte-identical to
#: one regenerated today, or every rebuild is a diff nobody can review.
NAMESPACE = uuid.UUID("6f1f0c9a-3c3e-4a44-bd2a-2c3f2f2b6c11")


def ev(*names: str) -> list[str]:
    return [str(uuid.uuid5(NAMESPACE, name)) for name in names]


def case(node_id: str, why: str, gathered: list[str], output: dict[str, Any]) -> dict[str, Any]:
    return {"node_id": node_id, "why": why, "gathered": gathered, "output": output}


CRM = ev("crm-1", "crm-2", "crm-3")
ADS = ev("ads-1", "ads-2", "ads-3")
TERMS = ev("terms-1", "terms-2")
SERP = ev("serp-1", "serp-2")
CREATIVE = ev("creative-1", "creative-2")
KEYWORDS = ev("kw-1", "kw-2")
PAGES = ev("page-1", "page-2")


CASES: list[dict[str, Any]] = [
    case(
        "1.1.1",
        "Unit economics are arithmetic, not prose: every figure has to trace to a CRM row.",
        CRM,
        {
            "products": [
                {
                    "name": "SDS Manager — Team",
                    "price_model": "Per seat, billed annually",
                    "delivery_cost_notes": "Hosting and support, ~18% of ACV.",
                    "evidence_ids": CRM[:1],
                },
                {
                    "name": "SDS Manager — Enterprise",
                    "price_model": "Site licence with an onboarding fee",
                    "delivery_cost_notes": "Implementation is 40 hours in the first year.",
                    "evidence_ids": CRM[1:2],
                },
            ],
            "assumptions": {
                "gross_margin_pct": 78.0,
                "expected_lifetime_months": 34.0,
                "target_ltv_cac_ratio": 3.0,
                "basis": "Closed-won cohort, trailing 24 months.",
            },
            "economics": {
                "acv": 8400.0,
                "median_deal_value": 6200.0,
                "deals": 214,
                "revenue": 1_797_600.0,
                "gross_margin_pct": 78.0,
                "lifetime_months": 34.0,
                "ltv_estimate": 18_564.0,
                "target_cac": 6188.0,
                "payback_months": 9.5,
            },
            "ltv_estimate": 18_564.0,
            "target_cac": 6188.0,
            "payback_months": 9.5,
            "coverage": [],
        },
    ),
    case(
        "1.1.2",
        "Segments are the thing most likely to be invented; each one must cite the deals behind it.",
        CRM,
        {
            "segments": [
                {
                    "label": "Mid-market chemical distributors, DACH",
                    "firmographics": "50-250 employees, 3-8 warehouses, REACH-regulated.",
                    "triggers": ["An audit finding", "A new warehouse opening"],
                    "jobs_to_be_done": ["Prove compliance to an auditor in under a day"],
                    "industry": "Chemical distribution",
                    "country": "DE",
                    "size_band": "50-250",
                    "deals": 61,
                    "revenue": 548_000.0,
                    "share_of_revenue_pct": 30.5,
                    "avg_deal_value": 8983.6,
                    "evidence_ids": CRM[:2],
                },
                {
                    "label": "Manufacturing EHS teams, Nordics",
                    "firmographics": "250-1000 employees, single-site, ISO 45001.",
                    "triggers": ["An ISO recertification"],
                    "jobs_to_be_done": [
                        "Keep safety data sheets current without a full-time owner"
                    ],
                    "industry": "Manufacturing",
                    "country": "DK",
                    "size_band": "250-1000",
                    "deals": 38,
                    "revenue": 402_000.0,
                    "share_of_revenue_pct": 22.4,
                    "avg_deal_value": 10_578.9,
                    "evidence_ids": CRM[2:3],
                },
            ],
            "segments_omitted": 3,
            "coverage": [],
        },
    ),
    case(
        "1.2.1",
        "A winner and a loser that cite the same account history, with the verdict stated.",
        ADS,
        {
            "winners": [
                {
                    "campaign": "Brand — Exact — EU",
                    "verdict": "winner",
                    "why": "Lowest CPA in the account and the only one under target.",
                    "period": "2025-09-01..2026-08-31",
                    "cost": 41_200.0,
                    "conversions": 128.0,
                    "conversion_value": 812_000.0,
                    "cpa": 321.9,
                    "roas": 19.7,
                    "metric_delta": -18.4,
                    "evidence_ids": ADS[:1],
                }
            ],
            "losers": [
                {
                    "campaign": "Generic — Broad — Worldwide",
                    "verdict": "loser",
                    "why": "Broad match pulled unrelated chemistry traffic; no conversions in six months.",
                    "period": "2025-09-01..2026-08-31",
                    "cost": 58_900.0,
                    "conversions": 3.0,
                    "conversion_value": 12_400.0,
                    "cpa": 19_633.3,
                    "roas": 0.2,
                    "metric_delta": 240.0,
                    "evidence_ids": ADS[1:3],
                }
            ],
            "neutral": [],
            "structural_findings": [
                "Brand and generic share one budget, so generic starves brand at month end.",
            ],
            "coverage": [],
        },
    ),
    case(
        "1.2.2",
        "The P&L is computed in pandas (law 3); the model only labels. Both sides cite the same rows.",
        TERMS,
        {
            "profitable_terms": [
                {
                    "term": "sds management software",
                    "cost": 6120.0,
                    "conversions": 24.0,
                    "conversion_value": 198_400.0,
                    "cpa": 255.0,
                    "roas": 32.4,
                    "clicks": 1420,
                    "impressions": 38_900,
                    "campaigns": ["Generic — Phrase — EU"],
                    "evidence_ids": TERMS[:1],
                }
            ],
            "wasteful_terms": [
                {
                    "term": "free sds sheets download",
                    "cost": 9840.0,
                    "conversions": 0.0,
                    "clicks": 3120,
                    "impressions": 142_000,
                    "recommended_action": "negative_phrase",
                    "reason": "Search intent is a free document, not a system to manage them.",
                    "campaigns": ["Generic — Broad — Worldwide"],
                    "evidence_ids": TERMS[1:2],
                }
            ],
            "clusters": [
                {
                    "theme": "Free document seekers",
                    "verdict": "wasteful",
                    "terms": ["free sds sheets download", "sds pdf free"],
                    "note": "Consistently zero-conversion across every market.",
                }
            ],
            "totals": {
                "terms": 2,
                "cost": 15_960.0,
                "conversions": 24.0,
                "conversion_value": 198_400.0,
                "wasted_spend": 9840.0,
                "waste_pct": 61.7,
                "profitable_terms": 1,
                "wasteful_terms": 1,
                "blended_cpa": 665.0,
                "blended_roas": 12.4,
            },
            "truncated": {"profitable_terms": 0, "wasteful_terms": 0},
            "coverage": [],
        },
    ),
    case(
        "1.3.1",
        "Overlap is a relative rank, not a percentage — the case that broke P5b's renderer.",
        SERP,
        {
            "competitors": [
                {
                    "domain": "chemwatch.net",
                    "name": "Chemwatch",
                    "positioning": "Enterprise chemical compliance suite.",
                    "threat": "direct",
                    "overlap_score": 84.0,
                    "overlap_basis": ["Appears on 31 of 40 checked terms"],
                    "paid_keyword_overlap": 31,
                    "paid_keyword_count": 412,
                    "est_paid_traffic_cost": 41_000.0,
                    "avg_position": 2.1,
                    "serp_hits": 31,
                    "serp_terms": ["sds management software", "chemical inventory software"],
                    "evidence_ids": SERP[:1],
                },
                {
                    "domain": "verisk3e.com",
                    "name": "Verisk 3E",
                    "positioning": "Regulatory data and content licensing.",
                    "threat": "adjacent",
                    "overlap_score": 41.0,
                    "overlap_basis": ["Bids on regulatory terms, not on management terms"],
                    "paid_keyword_overlap": 12,
                    "paid_keyword_count": 288,
                    "est_paid_traffic_cost": 18_500.0,
                    "avg_position": 3.4,
                    "serp_hits": 12,
                    "serp_terms": ["reach compliance software"],
                    "evidence_ids": SERP[1:2],
                },
            ],
            "serp_terms_checked": 40,
            "competitors_omitted": 6,
            "coverage": [],
        },
    ),
    case(
        "1.3.2",
        "Creatives are observed, never described from memory — and a degraded pull still ships.",
        CREATIVE,
        {
            "ads": [
                {
                    "advertiser": "Chemwatch",
                    "headline": "SDS Management, Audited",
                    "description": "Keep every safety data sheet current and prove it in one click.",
                    "offer": "Free compliance audit",
                    "angle": "Audit readiness",
                    "proof_type": "certification",
                    "cta": "Book a demo",
                    "landing_url": "https://chemwatch.net/sds-management",
                    "first_seen": "2026-05-02",
                    "last_seen": "2026-09-12",
                    "screenshot_path": "creatives/run-x/chemwatch-1a2b3c4d.png",
                    "format": "text",
                    "theme": "Audit readiness",
                    "evidence_ids": CREATIVE[:1],
                }
            ],
            "message_clusters": [
                {
                    "theme": "Audit readiness",
                    "frequency": 14,
                    "share_pct": 46.7,
                    "advertisers": ["Chemwatch", "Verisk 3E"],
                }
            ],
            "stats": {"advertisers": 2, "ads_seen": 30},
            "ads_omitted": 0,
            "unread_ads": 4,
            "coverage": ["creative: partial — transparency: 1 of 3 advertisers failed"],
        },
    ),
    case(
        "1.4.1",
        "Every term traces to the source that produced it; a term with no citation is invented.",
        KEYWORDS,
        {
            "keywords": [
                {
                    "term": "sds management software",
                    "source": ["dataforseo", "google_ads"],
                    "market": ["DE", "DK"],
                    "evidence_ids": KEYWORDS[:1],
                },
                {
                    "term": "chemical inventory system",
                    "source": ["dataforseo"],
                    "market": ["DE"],
                    "evidence_ids": KEYWORDS[1:2],
                },
            ],
            "total_terms": 2,
            "source_counts": {"dataforseo": 2, "google_ads": 1},
            "terms_omitted": 0,
            "coverage": [],
        },
    ),
    case(
        "1.4.4",
        "A blocklist is a spending decision, so each entry names the evidence class it came from.",
        TERMS,
        {
            "negatives": [
                {
                    "term": "free",
                    "match_type": "broad",
                    "reason": "Every free-intent term in the account converted zero times.",
                    "source": "wasteful_terms",
                },
                {
                    "term": "jobs",
                    "match_type": "phrase",
                    "reason": "Careers traffic, consistently marked irrelevant at classification.",
                    "source": "intent_irrelevant",
                },
                {
                    "term": "too expensive",
                    "match_type": "exact",
                    "reason": "Named as the lost reason on 18 closed-lost deals.",
                    "source": "lost_reasons",
                },
            ],
            "withheld": ["sds"],
            "counts_by_source": {
                "wasteful_terms": 1,
                "intent_irrelevant": 1,
                "lost_reasons": 1,
            },
            "coverage": [],
        },
    ),
    case(
        "1.5.1",
        "A page audit is a measurement. Severity has to follow the numbers, not the prose.",
        PAGES,
        {
            "pages": [
                {
                    "url": "https://sdsmanager.com/pricing",
                    "lcp_ms": 4800.0,
                    "cls": 0.02,
                    "mobile_ok": True,
                    "form_fields_count": 9,
                    "trust_signals": ["ISO 45001 badge", "Customer logos"],
                    "issues": ["LCP over 4s on mobile", "Nine form fields before a first reply"],
                    "severity": "major",
                    "status": 200,
                    "mapped_clusters": ["Pricing intent"],
                    "message_match": "partial",
                    "evidence_ids": PAGES[:1],
                },
                {
                    "url": "https://sdsmanager.com/de/produkt",
                    "lcp_ms": 2100.0,
                    "cls": 0.01,
                    "mobile_ok": True,
                    "form_fields_count": 3,
                    "trust_signals": ["Customer logos"],
                    "issues": [],
                    "severity": "none",
                    "status": 200,
                    "mapped_clusters": ["Product intent"],
                    "message_match": "good",
                    "evidence_ids": PAGES[1:2],
                },
            ],
            "unreachable": [],
            "blocking": 0,
            "coverage": [],
        },
    ),
    case(
        "1.5.4",
        "Sizing is the most tempting place to invent a number; the whole node cites its inputs.",
        ADS + KEYWORDS,
        {
            "scenarios": [
                {
                    "budget_usd_month": 10_000.0,
                    "est_clicks": 1180.0,
                    "est_conv": 21.2,
                    "est_cpa": 471.7,
                    "est_revenue": 131_440.0,
                    "assumptions": [
                        "Click-through and conversion rates from the trailing 12 months",
                        "Blended CPC from the priced keyword list",
                    ],
                    "confidence_interval": "14-28 conversions",
                    "demand_capped": False,
                    "risk": "Assumes the generic campaign is restructured first.",
                },
                {
                    "budget_usd_month": 25_000.0,
                    "est_clicks": 2400.0,
                    "est_conv": 38.0,
                    "est_cpa": 657.9,
                    "est_revenue": 235_600.0,
                    "assumptions": ["Demand caps out before budget does in DK"],
                    "confidence_interval": "24-47 conversions",
                    "demand_capped": True,
                    "risk": "Above the point where incremental spend buys broader, worse traffic.",
                },
            ],
            "baseline": {"cpc": 8.47, "cvr": 0.018, "aov": 6200.0},
            "blockers": ["Conversion tracking has not fired in 41 days"],
            "evidence_ids": ADS + KEYWORDS,
            "coverage": [],
        },
    ),
]


def main() -> int:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for item in CASES:
        path = FIXTURES / f"{item['node_id'].replace('.', '_')}.json"
        path.write_text(json.dumps(item, indent=2, sort_keys=False) + "\n")
        print(f"wrote {path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
