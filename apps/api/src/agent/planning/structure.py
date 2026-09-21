"""The frames stages 2.3 and 2.4 compute over (Stage 02 PRD §11).

Same division of labour as `planning/demand.py`, for the same reason. A plan
node may not do arithmetic (law 14, enforced by `check_calc_isolation.py`), and
`agent/calc/` may not touch the ORM or the report contract (it is pure, enforced
by the same script). Something has to stand between them and turn `PlanInput`
and an approved allocation into the columns a formula declares. That is this
module, and it decides nothing: every figure the plan publishes is produced by
`agent/calc/` from one of these frames.

Four frames, one per formula stages 2.3 and 2.4 call:

* `keyword_frame` -> `structure.grouping_v1` (2.4.2)
* `share_frame` -> `allocation.share_v1` (2.3.1, 2.3.3, 2.4.2)
* `member_frame` -> `structure.overlap_v1` (2.3.2)
* `built_campaign_frame` -> `structure.volume_check_v1` (2.4.3)

**Nothing unusable is dropped silently.** A keyword with no landing page stays
in the frame so `grouping_v1` can report it as an orphan; an allocation line the
caller could not label stays in so `share_v1` can exclude it with a reason. A
row removed here is a row that never appears in any report, and the thing most
worth knowing about a plan is what it could not place.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

import pandas as pd

from agent.export.contract import DemandMap, PricedKeyword
from agent.planning import demand

#: The columns `structure.grouping_v1` declares.
GROUPING_INPUT = (
    "term",
    "search_volume",
    "forecast_cpc_usd",
    "intent_label",
    "landing_url",
    "match_type",
)

#: The columns `allocation.share_v1` declares, plus the identity it echoes.
SHARE_INPUT = ("group", "usd", "target_cpa_usd", "est_conv", "campaign_ref")


class StructureInputError(ValueError):
    """The accepted research or the approved budget cannot build a structure.

    Raised rather than returning an empty frame: a formula handed nothing
    raises anyway, and it raises naming a column instead of naming the reason
    the plan has nothing to work with.
    """


def keyword_frame(
    priced_keywords: Sequence[PricedKeyword],
    *,
    demand_map: DemandMap,
    market: str | None = None,
) -> pd.DataFrame:
    """Stage 01's priced keywords, in the columns `grouping_v1` declares.

    `landing_url` prefers the keyword's own `best_url` and falls back to node
    1.4.5's cluster mapping, which is the same precedence `demand.py` uses —
    two different answers to "which page does this term belong to" would put
    the same keyword in two ad groups, and §18 makes that a blocking critique.
    """
    if not priced_keywords:
        raise StructureInputError(
            "priced_keyword_list — the accepted research carries no priced keyword, so "
            "there is nothing to build an account structure from"
        )

    wanted = (market or "").strip()
    selected = [
        keyword
        for keyword in priced_keywords
        if not wanted or _market_of(keyword).casefold() == wanted.casefold()
    ]
    if not selected:
        raise StructureInputError(
            f"market {wanted} — none of the {len(priced_keywords)} priced keyword(s) was "
            f"researched for it, so no campaign can be built for that market"
        )

    gaps = _content_gaps(demand_map)
    records: list[dict[str, Any]] = []
    for keyword in selected:
        url = (keyword.best_url or "").strip()
        note = ""
        if url and _norm_url(url) in gaps:
            note = f"1.4.5 mapped {url} as a content gap"
            url = ""
        records.append(
            {
                "term": keyword.term,
                "search_volume": int(keyword.volume or 0),
                "forecast_cpc_usd": demand._cpc_mid(keyword),
                "intent_label": str(keyword.intent or ""),
                "landing_url": url,
                "landing_url_note": note,
                "match_type": keyword.match_type,
                "market": _market_of(keyword),
                "funnel_stage": keyword.funnel_stage or "",
            }
        )
    return pd.DataFrame.from_records(records)


def share_frame(
    allocation: Sequence[Mapping[str, Any]],
    *,
    label: Callable[[Mapping[str, Any]], str],
) -> pd.DataFrame:
    """The approved split, each line tagged with the group to total it under.

    The labeller is the caller's because the grouping is: 2.3.1 totals by
    campaign type and market, 2.3.3 by brand versus non-brand, 2.4.2 by
    campaign. A label the caller cannot produce comes back blank rather than
    missing, so `share_v1` reports the line as excluded instead of the money
    quietly vanishing from the percentages.
    """
    if not allocation:
        raise StructureInputError(
            "no approved allocation — gate G3 has not produced a split, so there is no "
            "budget to apportion"
        )
    records = [
        {
            "group": str(label(line) or "").strip(),
            "usd": demand._float(line.get("usd")),
            "target_cpa_usd": demand._float(line.get("target_cpa_usd")),
            "est_conv": demand._float(line.get("est_conv")),
            "campaign_ref": str(line.get("campaign_ref") or ""),
            "market": str(line.get("market") or ""),
        }
        for line in allocation
    ]
    return pd.DataFrame.from_records(records)


def member_frame(targeting: Mapping[str, Sequence[str]]) -> pd.DataFrame:
    """`campaign -> what it targets`, flattened for `overlap_v1`.

    A campaign targeting nothing keeps a row with a blank member. `overlap_v1`
    excludes the blank and the campaign drops out of the pair list — which is
    the honest reading of "we could not tell what this campaign targets", and
    distinguishable from a campaign that targets things nobody else does.
    """
    records = [
        {"campaign_ref": ref, "member": member}
        for ref in sorted(targeting)
        for member in (sorted({str(item) for item in targeting[ref]}) or [""])
    ]
    return pd.DataFrame.from_records(records)


def built_campaign_frame(
    campaigns: Sequence[Mapping[str, Any]],
    *,
    allocation: Sequence[Mapping[str, Any]],
    capacity: Sequence[Mapping[str, Any]] = (),
) -> pd.DataFrame:
    """The structure 2.4.2 actually built, against the money G3 actually approved.

    Node 2.2.2 asked `volume_check_v1` whether a *forecast* budget could clear
    the learning threshold, over campaigns that were still a proposal. This asks
    the same question of the built tree: real ad-group and keyword counts, and
    the approved split rather than the forecast one. It is the check that can
    tell 2.4.3 a campaign is too thin, which the earlier one structurally could
    not — it had no ad groups to count.
    """
    approved = demand.approved_campaign_frame(allocation, capacity=capacity)
    funded = {str(row["campaign_ref"]): row for _, row in approved.iterrows()}

    records: list[dict[str, Any]] = []
    for campaign in campaigns:
        ref = str(campaign.get("campaign_ref") or campaign.get("name") or "")
        row = funded.get(ref)
        if row is None:
            raise StructureInputError(
                f"{ref} was built into the structure but carries no approved budget line — "
                f"a campaign nobody funded must not reach the plan"
            )
        ad_groups = list(campaign.get("ad_groups") or [])
        records.append(
            {
                "campaign_ref": ref,
                "name": str(campaign.get("name") or ref),
                "monthly_budget_usd": row["monthly_budget_usd"],
                "forecast_cpa_usd": row["forecast_cpa_usd"],
                "avg_cpc_usd": row["avg_cpc_usd"],
                "has_revenue_values": bool(row["has_revenue_values"]),
                "ad_group_count": len(ad_groups),
                "keyword_count": sum(len(list(group.get("keywords") or [])) for group in ad_groups),
            }
        )
    return pd.DataFrame.from_records(records)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _market_of(keyword: PricedKeyword) -> str:
    return (keyword.market or "").strip() or demand.UNSPECIFIED_MARKET


def _content_gaps(demand_map: DemandMap) -> frozenset[str]:
    """The URLs node 1.4.5 judged a `gap` — no page good enough exists yet.

    A keyword pointed at one of these is a click that lands on nothing it asked
    for. Stage 01 already made that judgement and 2.4.2 must not quietly
    overrule it, so the URL is dropped and the keyword becomes an orphan that
    `grouping_v1` reports by name.
    """
    return frozenset(
        _norm_url(mapping.best_url or "")
        for mapping in demand_map.mapping
        if mapping.verdict == "gap" and mapping.best_url
    )


def _norm_url(url: str) -> str:
    return url.strip().rstrip("/").casefold()
