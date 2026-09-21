"""The Google Ads Editor import bundle (Stage 02 PRD §14).

A ZIP of four CSVs — `campaigns.csv`, `ad_groups.csv`, `keywords.csv`,
`negatives.csv` — carrying the exact column headers Editor's import expects.
Ads and asset groups are deliberately absent: Stage 03 owns them, and a bundle
that shipped empty ad rows would import empty ads.

§14's acceptance is binary and worth restating, because it is what every
decision below serves: **the bundle imports with zero errors, and the
imported campaign, ad-group and keyword counts equal the counts in
`CampaignPlan.account_structure`.** So:

* **Everything imports paused.** A bundle that arrives enabled is one careless
  import away from spending a month's budget on a plan nobody has frozen.
  `manifest.txt` says so in the first line, and the CSVs say so in a column.
* **A draft bundle is watermarked too.** §14 requires the watermark on every
  page of a document; a CSV has no pages, so it travels in `manifest.txt` and
  in the ZIP's own filename. A spreadsheet is the *easiest* of the six formats
  to mistake for an approved plan, because it looks like a working file.
* **Every file is CRLF and UTF-8 with a BOM.** RFC 4180 for the first, and the
  second because the first thing that happens to these is that somebody opens
  them in Excel — which reads a BOM-less UTF-8 file as Latin-1 and turns every
  German keyword into mojibake.
* **Nothing is invented.** A campaign with no daily budget exports an empty
  Budget cell rather than a zero. Editor rejects a bad value loudly; it
  accepts a zero silently and runs the campaign at zero.
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections.abc import Iterable, Sequence
from typing import Any

from agent.export.plan_contract import CampaignPlan, PlannedCampaign

#: Editor's own column names, in its own spelling.
CAMPAIGN_COLUMNS = (
    "Campaign",
    "Campaign Type",
    "Status",
    "Budget",
    "Budget Type",
    "Bid Strategy Type",
    "Target CPA",
    "Target ROAS",
    "Languages",
    "Location",
)

AD_GROUP_COLUMNS = (
    "Campaign",
    "Ad Group",
    "Status",
    "Ad Group Type",
    "Max CPC",
)

KEYWORD_COLUMNS = (
    "Campaign",
    "Ad Group",
    "Keyword",
    "Criterion Type",
    "Max CPC",
    "Final URL",
    "Status",
)

NEGATIVE_COLUMNS = (
    "Campaign",
    "Ad Group",
    "Keyword",
    "Criterion Type",
)

#: Nothing in this bundle is enabled on import. See the module docstring.
STATUS = "Paused"

#: Editor spells a campaign-level negative differently from an ad-group one,
#: and an account-level negative is not importable through this bundle at all —
#: it is a shared negative list, which Editor manages separately. Those are
#: written into `manifest.txt` as a manual step rather than silently dropped.
CAMPAIGN_NEGATIVE = "Campaign Negative {match}"
AD_GROUP_NEGATIVE = "Negative {match}"

#: How a plain negative string is matched when the plan does not say. Phrase,
#: not broad: a broad negative excludes more than it was meant to, and the
#: failure is invisible — traffic that never arrives.
DEFAULT_NEGATIVE_MATCH = "Phrase"

#: Editor's campaign-type spellings, from the plan's own vocabulary.
CAMPAIGN_TYPES = {
    "search": "Search",
    "performance_max": "Performance Max",
    "demand_gen": "Demand Gen",
    "display": "Display",
    "display_remarketing": "Display",
    "video": "Video",
    "video_remarketing": "Video",
    "shopping": "Shopping",
}

#: Editor's bid-strategy spellings.
BID_STRATEGIES = {
    "manual_cpc": "Manual CPC",
    "max_clicks": "Maximize clicks",
    "max_conv": "Maximize conversions",
    "max_conv_value": "Maximize conversion value",
    "tcpa": "Target CPA",
    "troas": "Target ROAS",
}

FILES = ("campaigns.csv", "ad_groups.csv", "keywords.csv", "negatives.csv")


def render_editor_csv(plan: CampaignPlan, *, project_name: str | None = None) -> bytes:
    """The whole bundle as one ZIP. Deterministic: same plan, same bytes."""
    buffer = io.BytesIO()
    # `ZIP_DEFLATED` with a fixed date so §14's "exporting a frozen plan twice
    # produces byte-identical output" holds for this format too. `zipfile`
    # would otherwise stamp each entry with the wall clock.
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in (
            ("campaigns.csv", campaign_rows(plan)),
            ("ad_groups.csv", ad_group_rows(plan)),
            ("keywords.csv", keyword_rows(plan)),
            ("negatives.csv", negative_rows(plan)),
        ):
            columns = {
                "campaigns.csv": CAMPAIGN_COLUMNS,
                "ad_groups.csv": AD_GROUP_COLUMNS,
                "keywords.csv": KEYWORD_COLUMNS,
                "negatives.csv": NEGATIVE_COLUMNS,
            }[name]
            archive.writestr(_entry(name), _csv(columns, payload))
        archive.writestr(_entry("manifest.txt"), manifest(plan, project_name=project_name))
    return buffer.getvalue()


def _entry(name: str) -> zipfile.ZipInfo:
    """A ZIP entry with a fixed timestamp, so two exports compare equal.

    1980-01-01 is the earliest date the ZIP format can represent, which makes
    it the obvious "no date here" value — and unlike today's date it cannot be
    mistaken for when the plan was made.
    """
    info = zipfile.ZipInfo(filename=name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    # 0o644, in the high bits where ZIP keeps the unix mode.
    info.external_attr = 0o644 << 16
    return info


# ---------------------------------------------------------------------------
# the four sheets
# ---------------------------------------------------------------------------


def campaign_rows(plan: CampaignPlan) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for campaign in plan.account_structure.campaigns:
        strategy = BID_STRATEGIES.get(campaign.bid_strategy.strip().lower(), "")
        rows.append(
            {
                "Campaign": campaign.name,
                "Campaign Type": CAMPAIGN_TYPES.get(campaign.type.strip().lower(), ""),
                "Status": STATUS,
                "Budget": _number(campaign.daily_budget_usd),
                "Budget Type": "Daily" if campaign.daily_budget_usd else "",
                "Bid Strategy Type": strategy,
                # Editor reads exactly one of these, and which one depends on
                # the strategy. Writing the target into both columns is how a
                # tCPA campaign imports with a ROAS goal nobody asked for.
                "Target CPA": _number(campaign.target) if strategy == "Target CPA" else "",
                "Target ROAS": _number(campaign.target) if strategy == "Target ROAS" else "",
                "Languages": campaign.language or "",
                "Location": "; ".join(campaign.locations),
            }
        )
    return rows


def ad_group_rows(plan: CampaignPlan) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for campaign in plan.account_structure.campaigns:
        for group in campaign.ad_groups:
            rows.append(
                {
                    "Campaign": campaign.name,
                    "Ad Group": group.name,
                    "Status": STATUS,
                    "Ad Group Type": "Standard",
                    # Left blank on a smart-bidding campaign: a Max CPC on a
                    # tCPA ad group is ignored by Google and misleading to a
                    # reader, who will think it is the bid.
                    "Max CPC": _max_cpc(campaign, group.keywords),
                }
            )
    return rows


def keyword_rows(plan: CampaignPlan) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for campaign in plan.account_structure.campaigns:
        for group in campaign.ad_groups:
            for keyword in group.keywords:
                rows.append(
                    {
                        "Campaign": campaign.name,
                        "Ad Group": group.name,
                        "Keyword": keyword.term,
                        "Criterion Type": _match(keyword.match_type),
                        "Max CPC": _number(keyword.forecast_cpc_usd),
                        "Final URL": group.landing_url,
                        "Status": STATUS,
                    }
                )
    return rows


def negative_rows(plan: CampaignPlan) -> list[dict[str, Any]]:
    """Campaign- and ad-group-level negatives. Account-level ones go in the manifest."""
    rows: list[dict[str, Any]] = []
    for campaign in plan.account_structure.campaigns:
        for term in campaign.negatives:
            rows.append(
                {
                    "Campaign": campaign.name,
                    "Ad Group": "",
                    "Keyword": term,
                    "Criterion Type": CAMPAIGN_NEGATIVE.format(match=DEFAULT_NEGATIVE_MATCH),
                }
            )
        for group in campaign.ad_groups:
            for term in group.negatives:
                rows.append(
                    {
                        "Campaign": campaign.name,
                        "Ad Group": group.name,
                        "Keyword": term,
                        "Criterion Type": AD_GROUP_NEGATIVE.format(match=DEFAULT_NEGATIVE_MATCH),
                    }
                )
    return rows


def manifest(plan: CampaignPlan, *, project_name: str | None = None) -> str:
    """What a person needs to know before importing, in the first thing they open."""
    counts = plan.account_structure.counts()
    header = (
        f"{'DRAFT — NOT APPROVED. Do not import.' if not plan.is_frozen else 'Frozen plan'}\n"
        f"{'=' * 60}\n"
    )
    lines = [
        header,
        f"Campaign plan for {project_name or 'this project'}",
        f"Status:       {plan.plan_status}"
        + (f" (v{plan.version})" if plan.version > 0 else " (unversioned)"),
        f"Plan run:     {plan.plan_run_id}",
        f"Generated:    {plan.generated_at.isoformat()}",
        "",
        "CONTENTS",
        f"  campaigns.csv   {counts['campaigns']} campaigns",
        f"  ad_groups.csv   {counts['ad_groups']} ad groups",
        f"  keywords.csv    {counts['keywords']} keywords",
        f"  negatives.csv   {len(negative_rows(plan))} negatives",
        "",
        "EVERYTHING IMPORTS PAUSED.",
        "  Review the budgets and the bid strategies in Editor before you post.",
        "",
        "ADS AND ASSET GROUPS ARE NOT IN THIS BUNDLE.",
        "  They belong to the next stage. Importing this creates the structure to",
        "  write them into, and nothing that can serve.",
    ]
    if plan.account_structure.account_negatives:
        lines += [
            "",
            "ACCOUNT-LEVEL NEGATIVES — ADD THESE BY HAND",
            "  Editor manages account negatives as a shared list, which this bundle",
            "  cannot create. Add them to a negative keyword list and apply it:",
            *(f"    - {term}" for term in plan.account_structure.account_negatives),
        ]
    if not plan.is_frozen:
        lines += [
            "",
            "THIS PLAN IS NOT FROZEN.",
            "  The figures above can still change. If you import it and the plan is",
            "  then revised, the account and the plan will disagree and nothing will",
            "  tell you.",
        ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _csv(columns: Sequence[str], rows: Iterable[dict[str, Any]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer, fieldnames=list(columns), lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL
    )
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


def _match(match_type: str) -> str:
    """Editor spells match types with an initial capital."""
    return match_type.strip().capitalize() or "Phrase"


def _number(value: float | int | None, places: int = 2) -> str:
    """Plain decimals, no thousands separators — this is a file for a machine.

    None becomes an empty cell, never a zero. Editor treats a zero budget as a
    zero budget.
    """
    if value is None:
        return ""
    return f"{float(value):.{places}f}"


def _max_cpc(campaign: PlannedCampaign, keywords: Sequence[Any]) -> str:
    """The ad group's default bid, on a manual campaign only.

    Smart bidding ignores it, and a number Google ignores is a number a person
    will nonetheless read as the bid. The value is the highest forecast CPC in
    the group — a ceiling to start from, not a recommendation, and the Paused
    status is what keeps it from spending either way.
    """
    if campaign.bid_strategy.strip().lower() not in ("manual_cpc", "max_clicks"):
        return ""
    bids = [item.forecast_cpc_usd for item in keywords if item.forecast_cpc_usd]
    return _number(max(bids)) if bids else ""
