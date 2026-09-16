"""The one render context every template sees.

The markdown and the print HTML are the same document in two skins, so they get
the same context object rather than each computing its own. That is what makes
PRD §12's acceptance — "both contain every section present in the JSON" —
checkable by a test instead of by reading two templates side by side.

Anything derived lives here, not in a template. A template that sums a column is
a template that can disagree with the one next to it.
"""

from __future__ import annotations

from typing import Any

from agent.export.citations import CitationIndex
from agent.export.contract import CompetitorAd, PricedKeyword, ResearchReport

#: How many priced keywords a prose document shows before deferring to the CSV.
#: A run targets ≥2,000 (PRD §10, 1.4.1); a 2,000-row table in a PDF is not a
#: report, it is a phone book. The document says out loud that it is truncated —
#: a table silently cut to 50 rows reads as the complete list.
KEYWORD_PREVIEW_LIMIT = 50

#: Same reasoning for the creative corpus: §12 sizes a PDF for 200 ads.
AD_PREVIEW_LIMIT = 40

READINESS_LABELS = {
    "go": "Go",
    "go_with_fixes": "Go, with fixes",
    "no_go": "No go",
}


def _keyword_sort_key(keyword: PricedKeyword) -> tuple[int, float, str]:
    """Highest volume first, then term, so the preview is stable across renders.

    Volume is optional; a keyword with no volume sorts last rather than first,
    which is where `None` would land under a naive descending sort.
    """
    volume = keyword.volume if keyword.volume is not None else -1
    return (-volume, -(keyword.cpc_high or 0.0), keyword.term)


def build_context(
    report: ResearchReport,
    *,
    project_name: str | None = None,
    keyword_limit: int = KEYWORD_PREVIEW_LIMIT,
    ad_limit: int = AD_PREVIEW_LIMIT,
) -> dict[str, Any]:
    """Everything a template may read, and nothing it has to compute."""
    citations = CitationIndex.for_report(report)

    keywords_total = len(report.priced_keyword_list)
    keywords = sorted(report.priced_keyword_list, key=_keyword_sort_key)[:keyword_limit]

    ads_total = len(report.competitive_landscape.ads)
    ads: list[CompetitorAd] = report.competitive_landscape.ads[:ad_limit]

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
        "wasted_spend": wasted_spend,
        "keywords": keywords,
        "keywords_total": keywords_total,
        "keywords_truncated": keywords_total > len(keywords),
        "ads": ads,
        "ads_total": ads_total,
        "ads_truncated": ads_total > len(ads),
    }
