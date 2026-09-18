"""The one render context every template sees.

The markdown and the print HTML are the same document in two skins, so they get
the same context object rather than each computing its own. That is what makes
PRD §12's acceptance — "both contain every section present in the JSON" —
checkable by a test instead of by reading two templates side by side.

Anything derived lives here, not in a template. A template that sums a column is
a template that can disagree with the one next to it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from agent.export.citations import CitationIndex
from agent.export.contract import Competitor, CompetitorAd, PricedKeyword, ResearchReport
from agent.export.templating import (
    EMPTY,
    fmt_bool,
    fmt_date,
    fmt_days,
    fmt_list,
    fmt_money,
    fmt_months,
    fmt_number,
    fmt_pct,
    fmt_ratio_pct,
    fmt_text,
)

#: How many priced keywords a prose document shows before deferring to the CSV.
#: A run targets ≥2,000 (PRD §10, 1.4.1); a 2,000-row table in a PDF is not a
#: report, it is a phone book. The document says out loud that it is truncated —
#: a table silently cut to 50 rows reads as the complete list.
KEYWORD_PREVIEW_LIMIT = 50

#: Same reasoning for the creative corpus: §12 sizes a PDF for 200 ads.
AD_PREVIEW_LIMIT = 40

#: The eight sections, in order, that every format must contain. PRD §12's
#: acceptance is that the PDF and the DOCX "contain every section present in the
#: JSON"; this is that list, and `tests/test_report_parity.py` checks all three
#: renderings against it. The markdown numbers them ("## 1. Executive summary"),
#: so the check is a substring match, not equality.
SECTION_TITLES = (
    "Executive summary",
    "Business context",
    "What we already ran",
    "The competition",
    "Demand",
    "Are we ready",
    "What to do next",
    "Evidence index",
)

READINESS_LABELS = {
    "go": "Go",
    "go_with_fixes": "Go, with fixes",
    "no_go": "No go",
}


#: Buying order for the keyword preview. The two money intents share a tier on
#: purpose — somebody comparing SDS tools is as much a buyer as somebody ready
#: to click, and usually a higher-volume one — so within tier 0 raw volume
#: decides. `irrelevant` sorts last rather than being dropped, because the
#: preview is a view of the CSV and the CSV is the complete artefact.
_INTENT_TIER: dict[str, int] = {
    "transactional": 0,
    "commercial_investigation": 0,
    "navigational": 2,
    "informational": 3,
    "irrelevant": 5,
}


#: How much of a rival 1.3.1 judged each domain to be. Overlap is the wrong
#: order for this table on its own: it measures the share of the keyword surface
#: two domains have in common, and on terms like "safety data sheet" the biggest
#: sharers are encyclopaedias and regulators. They led the competitor table at
#: overlap 1.00 while the four real rivals sat below the fold.
_THREAT_TIER: dict[str, int] = {
    "direct": 0,
    "adjacent": 1,
    "aggregator": 2,
    "irrelevant": 4,
}


def _ad_presence(ads: Sequence[CompetitorAd]) -> list[dict[str, Any]]:
    """Ads per advertiser, split by format, busiest first."""
    rows: dict[str, dict[str, Any]] = {}
    for ad in ads:
        key = ad.advertiser or "unattributed"
        row = rows.setdefault(
            key, {"advertiser": key, "total": 0, "image": 0, "video": 0, "text": 0}
        )
        row["total"] += 1
        bucket = ad.format if ad.format in {"image", "video"} else "text"
        row[bucket] += 1
    return sorted(rows.values(), key=lambda row: (-row["total"], row["advertiser"]))


def _competitor_sort_key(competitor: Competitor) -> tuple[int, float, str]:
    """Most dangerous first, then most overlapping, then domain for stability."""
    tier = _THREAT_TIER.get(competitor.threat or "", 3)
    return (tier, -(competitor.overlap_score or 0.0), competitor.domain)


def _keyword_sort_key(keyword: PricedKeyword) -> tuple[int, int, float, str]:
    """Most worth buying first, then highest volume, then term for stability.

    Volume alone was the old key, and it handed the top of every preview to
    whatever the keyword vendor returned largest. A site scrape returns
    fragments — two-letter strings carrying six-figure volumes — which the
    classifier correctly marks `irrelevant` and which then filled the first
    screen of the report's headline table. Intent tier comes first so the rows a
    reader sees are the rows they could act on.

    Volume is optional; a keyword with no volume sorts last within its tier
    rather than first, which is where `None` would land under a naive
    descending sort.
    """
    tier = _INTENT_TIER.get(keyword.intent or "", 4)
    volume = keyword.volume if keyword.volume is not None else -1
    return (tier, -volume, -(keyword.cpc_high or 0.0), keyword.term)


def build_context(
    report: ResearchReport,
    *,
    project_name: str | None = None,
    keyword_limit: int = KEYWORD_PREVIEW_LIMIT,
    ad_limit: int = AD_PREVIEW_LIMIT,
) -> dict[str, Any]:
    """Everything a template may read, and nothing it has to compute."""
    citations = CitationIndex.for_report(report)

    competitors = sorted(report.competitive_landscape.competitors, key=_competitor_sort_key)

    keywords_total = len(report.priced_keyword_list)
    keywords = sorted(report.priced_keyword_list, key=_keyword_sort_key)[:keyword_limit]

    ads_total = len(report.competitive_landscape.ads)
    ads: list[CompetitorAd] = report.competitive_landscape.ads[:ad_limit]
    # Whether the corpus has anything to *read*. The Transparency Center grid
    # renders each creative as an image and keeps the wording on the
    # per-creative page, so a scrape can come back with 300 real ads and not one
    # headline. Printing the ad table then produces 40 rows of em-dashes, which
    # claims a creative corpus and shows nothing. `ad_presence` is what survives:
    # who is advertising, how much, and in what form.
    ads_have_text = any(
        ad.headline or ad.description or ad.offer or ad.angle or ad.cta
        for ad in report.competitive_landscape.ads
    )
    ad_presence = _ad_presence(report.competitive_landscape.ads)

    wasted_spend = sum(term.cost or 0.0 for term in report.account_learnings.wasteful_terms)

    return {
        "report": report,
        "project_name": project_name,
        "cite": citations,
        "readiness_label": READINESS_LABELS.get(report.launch_readiness, report.launch_readiness),
        # Section shortcuts. The templates are long enough without
        # `report.competitive_landscape.spend_estimates` on every line.
        "business": report.business_context,
        "learnings": report.account_learnings,
        "competition": report.competitive_landscape,
        "demand": report.demand_map,
        "readiness": report.readiness,
        # Derived once, here.
        "competitors": competitors,
        "ads_have_text": ads_have_text,
        "ad_presence": ad_presence,
        "wasted_spend": wasted_spend,
        "keywords": keywords,
        "keywords_total": keywords_total,
        "keywords_truncated": keywords_total > len(keywords),
        "ads": ads,
        "ads_total": ads_total,
        "ads_truncated": ads_total > len(ads),
    }


# ---------------------------------------------------------------------------
# Print rows
# ---------------------------------------------------------------------------
#
# The print template renders tables from lists of pre-formatted strings rather
# than from model objects. Two reasons: a Jinja macro that has to know how to
# format a column is a macro that will format it differently from the markdown
# template, and every number in a table has exactly one correct rendering, which
# is decided here next to the others.


def _print_rows(report: ResearchReport, context: dict[str, Any]) -> dict[str, Any]:
    """Every table in `report.html.j2`, as rows of rendered strings."""
    business = report.business_context
    learnings = report.account_learnings
    competition = report.competitive_landscape
    demand = report.demand_map
    readiness = report.readiness
    citations: CitationIndex = context["cite"]
    competitors = sorted(competition.competitors, key=_competitor_sort_key)

    return {
        "product_rows": [
            [
                product.name,
                fmt_text(product.price_model),
                fmt_money(product.acv),
                fmt_pct(product.gross_margin_pct),
                fmt_text(product.delivery_cost_notes),
            ]
            for product in business.products
        ],
        "exclusion_rows": [
            [
                exclusion.persona,
                exclusion.disqualifier,
                fmt_text(exclusion.observable_signal),
                fmt_list(exclusion.suggested_negative_terms),
            ]
            for exclusion in business.exclusions
        ],
        "market_rows": [
            [
                market.country,
                fmt_text(market.language),
                fmt_text(market.currency),
                fmt_months(market.demand_months),
                fmt_months(market.dead_months),
            ]
            for market in business.markets
        ],
        "regulated_rows": [
            [term.term, term.rule]
            for term in (business.compliance.regulated_terms if business.compliance else [])
        ],
        "performance_rows": [
            ["Won", finding.campaign, fmt_text(finding.metric_delta), fmt_text(finding.period)]
            for finding in learnings.winners
        ]
        + [
            ["Lost", finding.campaign, fmt_text(finding.metric_delta), fmt_text(finding.period)]
            for finding in learnings.losers
        ],
        "profitable_rows": [
            [
                term.term,
                fmt_money(term.cost),
                fmt_number(term.conv, 1),
                fmt_money(term.cpa),
                fmt_number(term.roas, 1),
            ]
            for term in learnings.profitable_terms
        ],
        "wasteful_rows": [
            [
                term.term,
                fmt_money(term.cost),
                fmt_number(term.conv, 1),
                fmt_text(term.recommended_action),
            ]
            for term in learnings.wasteful_terms
        ],
        "presence_rows": [
            [
                row["advertiser"],
                fmt_number(row["total"]),
                fmt_number(row["image"]),
                fmt_number(row["video"]),
                fmt_number(row["text"]),
            ]
            for row in _ad_presence(competition.ads)
        ],
        "competitor_rows": [
            [
                competitor.domain,
                fmt_text(competitor.name),
                fmt_text(competitor.threat),
                fmt_ratio_pct(competitor.overlap_score),
                fmt_list(competitor.overlap_basis),
            ]
            for competitor in competitors
        ],
        "cluster_rows": [
            [cluster.theme, fmt_number(cluster.frequency), fmt_list(cluster.advertisers)]
            for cluster in competition.message_clusters
        ],
        "ad_rows": [
            [
                ad.advertiser,
                fmt_text(ad.headline),
                fmt_text(ad.angle),
                fmt_text(ad.offer),
                fmt_text(ad.cta),
                f"{fmt_text(ad.first_seen)} → {fmt_text(ad.last_seen)}",
            ]
            for ad in context["ads"]
        ],
        "spend_rows": [
            [
                estimate.competitor,
                estimate.est_monthly_spend_range,
                estimate.method,
                fmt_text(estimate.confidence),
                fmt_months(estimate.peak_months),
            ]
            for estimate in competition.spend_estimates
        ],
        "keyword_rows": [
            [
                keyword.term,
                fmt_text(keyword.market),
                fmt_text(keyword.intent),
                fmt_number(keyword.volume),
                fmt_money(keyword.cpc_low),
                fmt_money(keyword.cpc_high),
                fmt_ratio_pct(keyword.competition),
                fmt_text(keyword.best_url),
                fmt_text(keyword.verdict),
            ]
            for keyword in context["keywords"]
        ],
        "mapping_rows": [
            [
                row.term_cluster,
                fmt_text(row.best_url),
                fmt_ratio_pct(row.relevance_score),
                row.verdict,
            ]
            for row in demand.mapping
        ],
        "negative_rows": [
            [
                negative.term,
                negative.match_type,
                fmt_text(negative.source),
                fmt_text(negative.reason),
            ]
            for negative in demand.negatives
        ],
        "page_rows": [
            [
                page.url,
                f"{fmt_number(page.lcp_ms)} ms" if page.lcp_ms is not None else EMPTY,
                fmt_number(page.cls, 2),
                fmt_bool(page.mobile_ok),
                fmt_number(page.form_fields_count),
                fmt_text(page.severity),
                fmt_list(page.issues),
            ]
            for page in readiness.pages
        ],
        "conversion_rows": [
            [
                action.name,
                fmt_text(action.status),
                fmt_date(action.last_conversion_at),
                fmt_days(action.staleness_days),
            ]
            for action in readiness.conversion_actions
        ],
        "audience_rows": [
            [
                audience.name,
                fmt_number(audience.size),
                fmt_text(audience.consent_basis),
                fmt_list(audience.markets_allowed),
                fmt_bool(audience.usable),
                fmt_text(audience.blocker),
            ]
            for audience in readiness.lists
        ],
        "scenario_rows": [
            [
                fmt_money(scenario.budget_usd_month, "USD", 0),
                fmt_number(scenario.est_clicks),
                fmt_number(scenario.est_conv, 1),
                fmt_money(scenario.est_cpa),
                fmt_money(scenario.est_revenue, "", 0),
                fmt_text(scenario.confidence_interval),
            ]
            for scenario in readiness.scenarios
        ],
        "evidence_rows": [
            [citation.marker, str(citation.evidence_id)] for citation in citations.citations
        ],
    }


def build_print_context(
    report: ResearchReport,
    *,
    project_name: str | None = None,
    charts: Sequence[Any] = (),
    screenshots: Sequence[Any] = (),
    screenshot_total: int = 0,
    keyword_limit: int = KEYWORD_PREVIEW_LIMIT,
    ad_limit: int = AD_PREVIEW_LIMIT,
) -> dict[str, Any]:
    """`build_context` plus everything only the printed document needs."""
    context = build_context(
        report,
        project_name=project_name,
        keyword_limit=keyword_limit,
        ad_limit=ad_limit,
    )
    context.update(_print_rows(report, context))
    context["charts"] = list(charts)
    context["screenshots"] = list(screenshots)
    context["screenshot_total"] = screenshot_total
    return context
