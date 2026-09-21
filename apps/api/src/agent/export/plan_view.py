"""The one render context every plan template sees (§12, §14).

The Stage 02 twin of `export/view.py`, and here for the same reason: the plan
markdown and the plan print-HTML are one document in two skins, so they get one
context rather than each computing its own. §14's acceptance — every section
present in the JSON is present in the PDF — is checkable by a test instead of
by reading two templates side by side.

Anything derived lives here. A template that sums a column is a template that
can disagree with the one next to it, and §12's whole point is that the six
formats cannot.

**The watermark is part of the context, not part of the renderer.** §14 is
explicit that a draft export must be watermarked on every page and that a
frozen one carries `version`, `frozen_by` and `frozen_at`. Deciding that in
one place means the markdown says it too — a `.md` handed round Slack is
exactly as capable of being mistaken for a signed-off plan as a PDF is.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from agent.export.plan_contract import CampaignPlan, Experiment, PlannedCampaign
from agent.export.templating import (
    EMPTY,
    fmt_bool,
    fmt_date,
    fmt_datetime,
    fmt_days,
    fmt_list,
    fmt_money,
    fmt_number,
    fmt_pct,
    fmt_text,
)

#: What a draft of any format carries, on every page. §14: "a draft PDF must
#: not be able to circulate as a signed-off plan."
DRAFT_WATERMARK = "DRAFT — NOT APPROVED"

#: How many campaigns a prose document renders in full. A 40-campaign plan
#: with 4,000 keywords is not a document, it is the Editor CSV — which is the
#: export that exists for it. The document says out loud that it is truncated.
CAMPAIGN_PREVIEW_LIMIT = 25

#: Keywords shown per ad group before the document defers to the CSV.
KEYWORD_PREVIEW_LIMIT = 12

#: Forecast rows in the prose document. The XLSX carries the whole grid.
FORECAST_PREVIEW_LIMIT = 40

#: The nine sections, in order, that every format must contain. §14's
#: acceptance is that every section present in the JSON is present in the PDF;
#: this is that list, and `tests/test_plan_parity.py` checks all three
#: renderings against it. The markdown numbers them, so it is a substring
#: match rather than equality.
SECTION_TITLES = (
    "Executive summary",
    "Objectives",
    "Media plan",
    "Channel slate",
    "Account structure",
    "Measurement plan",
    "Test backlog",
    "Decisions",
    "Open dependencies",
)

STATUS_LABELS = {
    "draft": "Draft",
    "blocked": "Blocked",
    "ready_to_freeze": "Ready to freeze",
    "frozen": "Frozen",
}

GATE_LABELS = {
    "G1": "Campaign targets",
    "G2": "Lead definition",
    "G3": "Budget allocation",
    "G4": "Channel slate",
}


def build_context(
    plan: CampaignPlan,
    *,
    project_name: str | None = None,
    frozen_by_name: str = "",
) -> dict[str, Any]:
    """Everything a plan template reads, computed once."""
    counts = plan.account_structure.counts()
    campaigns = plan.account_structure.campaigns
    forecast = plan.media_plan.forecast

    return {
        "plan": plan,
        "project_name": project_name or EMPTY,
        "status_label": STATUS_LABELS.get(plan.plan_status, plan.plan_status),
        "gate_labels": GATE_LABELS,
        # -- §14's draft vs frozen ------------------------------------------
        "is_draft": not plan.is_frozen,
        "watermark": None if plan.is_frozen else DRAFT_WATERMARK,
        "version_label": f"v{plan.version}" if plan.version > 0 else "unversioned draft",
        "frozen_by_name": frozen_by_name,
        # -- counts the document states about itself -------------------------
        "counts": counts,
        "campaigns": campaigns[:CAMPAIGN_PREVIEW_LIMIT],
        "campaigns_omitted": max(0, len(campaigns) - CAMPAIGN_PREVIEW_LIMIT),
        "keyword_preview_limit": KEYWORD_PREVIEW_LIMIT,
        "forecast": forecast[:FORECAST_PREVIEW_LIMIT],
        "forecast_omitted": max(0, len(forecast) - FORECAST_PREVIEW_LIMIT),
        "funded_tests": [test for test in plan.experiment_backlog if test.funded],
        "unfunded_tests": [test for test in plan.experiment_backlog if not test.funded],
        "blocking_dependencies": plan.blocking_dependencies,
        "other_dependencies": [item for item in plan.open_dependencies if not item.blocking],
        "blocking_issues": [
            issue for issue in plan.critique_issues if issue.get("severity") == "blocking"
        ],
        "warning_issues": [
            issue for issue in plan.critique_issues if issue.get("severity") == "warning"
        ],
        "numbers": plan.numbers(),
        "section_titles": SECTION_TITLES,
        # -- helpers a template may call -------------------------------------
        "keywords_of": keywords_of,
        "ad_group_rows": ad_group_rows,
        "test_rows": test_rows,
    }


def build_print_context(
    plan: CampaignPlan,
    *,
    project_name: str | None = None,
    frozen_by_name: str = "",
    charts: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The markdown context plus what only the printed document has."""
    context = build_context(plan, project_name=project_name, frozen_by_name=frozen_by_name)
    context["charts"] = charts or {}
    return context


# ---------------------------------------------------------------------------
# projections a template would otherwise have to compute
# ---------------------------------------------------------------------------


def keywords_of(campaign: PlannedCampaign) -> int:
    return sum(len(group.keywords) for group in campaign.ad_groups)


def ad_group_rows(campaign: PlannedCampaign) -> list[dict[str, Any]]:
    """One row per ad group, with its keyword preview already cut."""
    rows: list[dict[str, Any]] = []
    for group in campaign.ad_groups:
        shown = group.keywords[:KEYWORD_PREVIEW_LIMIT]
        rows.append(
            {
                "name": group.name,
                "theme": group.theme,
                "landing_url": group.landing_url,
                "primary_message": group.primary_message,
                "keyword_count": len(group.keywords),
                "keywords": shown,
                "omitted": max(0, len(group.keywords) - len(shown)),
                "negatives": group.negatives,
            }
        )
    return rows


def test_rows(tests: Sequence[Experiment]) -> list[dict[str, Any]]:
    """The backlog as the document shows it, in rank order."""
    return [
        {
            "rank": test.rank,
            "id": test.id,
            "hypothesis": test.hypothesis,
            "campaign": test.campaign_name or test.campaign_ref,
            "variable": (test.variable or "").replace("_", " "),
            "metric": test.primary_metric,
            "baseline": fmt_pct(test.baseline, 2),
            "conv_per_arm": fmt_number(test.required_conv_per_arm),
            "days": fmt_days(test.est_days_to_significance),
            "ice": fmt_number(test.ice_score, 2),
            "reserve": fmt_money(test.reserve_usd, "USD"),
            "funded": fmt_bool(test.funded),
            "wave": fmt_text(test.earliest_wave),
        }
        for test in sorted(tests, key=lambda row: (row.rank or 10_000, row.id))
    ]


__all__ = [
    "CAMPAIGN_PREVIEW_LIMIT",
    "DRAFT_WATERMARK",
    "FORECAST_PREVIEW_LIMIT",
    "KEYWORD_PREVIEW_LIMIT",
    "SECTION_TITLES",
    "ad_group_rows",
    "build_context",
    "build_print_context",
    "keywords_of",
    "test_rows",
    # re-exported so a template's filters and this module's helpers cannot
    # drift into formatting the same figure two ways
    "fmt_date",
    "fmt_datetime",
    "fmt_list",
    "fmt_money",
    "fmt_number",
    "fmt_pct",
]
