"""The search-term P&L (node 1.2.2), computed in pandas.

PRD §18 law 3: "All arithmetic (CPA, ROAS, P&L, sizing) happens in pandas/
Python. The model writes labels and prose only." This module is the enforcement
of that for stage 1.2 — every number node 1.2.2 emits is produced here, and the
node merges the model's labels onto these rows rather than letting the model
restate them. A model that miscopies a cost figure cannot corrupt the P&L,
because the cost figure never passes through it.

The shape of the answer is PRD §10 1.2.2: `profitable_terms[]{term, cost, conv,
cpa, roas}` and `wasteful_terms[]{term, cost, conv=0, recommended_action}`. The
one thing worth stating plainly is what "wasteful" means here — **zero
conversions and non-zero cost**, over the whole window, not a bad ratio. A term
with a poor CPA is a bidding decision; a term with 40 clicks and no conversion
at all is money that bought nothing, and that is the number stage 1.2 exists to
surface.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
import structlog

from agent.nodes.frames import numeric, with_ids

log = structlog.get_logger(__name__)

#: Columns the Google Ads connector writes for `search_term_pnl` evidence
#: (`connectors/google_ads.py::_search_term`). Anything absent is filled with
#: zero rather than dropped, so a partial pull still produces a P&L.
NUMERIC_COLUMNS = ("cost", "conversions", "conversion_value", "impressions", "clicks")

#: How many terms of each list reach the output. Both lists are ordered by money
#: — value earned, then money wasted — so the cut is always "the rest are
#: smaller than these", and `Totals` still counts every row.
TOP_N = 100

#: Evidence ids carried per term. A term aggregated from 24 monthly rows does
#: not need 24 citations to be checkable, and an unbounded list would dominate
#: the output document.
MAX_IDS_PER_TERM = 12


@dataclass(frozen=True, slots=True)
class TermRow:
    """One search term, aggregated over every row that mentions it."""

    term: str
    cost: float
    conversions: float
    conversion_value: float
    impressions: int
    clicks: int
    cpa: float | None
    roas: float | None
    campaigns: tuple[str, ...]
    evidence_ids: tuple[uuid.UUID, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "term": self.term,
            "cost": self.cost,
            "conversions": self.conversions,
            "conversion_value": self.conversion_value,
            "impressions": self.impressions,
            "clicks": self.clicks,
            "cpa": self.cpa,
            "roas": self.roas,
            "campaigns": list(self.campaigns),
            "evidence_ids": [str(item) for item in self.evidence_ids],
        }


@dataclass(frozen=True, slots=True)
class Totals:
    """The whole window, so the truncated lists cannot be mistaken for the total."""

    terms: int
    cost: float
    conversions: float
    conversion_value: float
    wasted_spend: float
    waste_pct: float
    profitable_terms: int
    wasteful_terms: int
    blended_cpa: float | None
    blended_roas: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "terms": self.terms,
            "cost": self.cost,
            "conversions": self.conversions,
            "conversion_value": self.conversion_value,
            "wasted_spend": self.wasted_spend,
            "waste_pct": self.waste_pct,
            "profitable_terms": self.profitable_terms,
            "wasteful_terms": self.wasteful_terms,
            "blended_cpa": self.blended_cpa,
            "blended_roas": self.blended_roas,
        }


@dataclass(slots=True)
class SearchTermPnl:
    """The computed P&L. Nothing in here was written by a model."""

    profitable: list[TermRow] = field(default_factory=list)
    wasteful: list[TermRow] = field(default_factory=list)
    totals: Totals = field(
        default_factory=lambda: Totals(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0, 0, None, None)
    )
    #: Terms omitted from each list by `TOP_N`. Reported, never silent.
    truncated: dict[str, int] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return self.totals.terms == 0


def compute(rows: list[dict[str, Any]], evidence_ids: list[uuid.UUID]) -> SearchTermPnl:
    """Aggregate raw `search_term_pnl` evidence payloads into a P&L.

    `rows[i]` and `evidence_ids[i]` are parallel — `frames.with_ids` enforces
    that — so every aggregated term can name the evidence rows that produced it
    and the node's citations survive the executor's subset check.
    """
    if not rows:
        return SearchTermPnl()

    frame = _frame(rows, evidence_ids)
    if frame.empty:
        return SearchTermPnl()

    grouped = frame.groupby("term", sort=False, dropna=False).agg(
        cost=("cost", "sum"),
        conversions=("conversions", "sum"),
        conversion_value=("conversion_value", "sum"),
        impressions=("impressions", "sum"),
        clicks=("clicks", "sum"),
        campaigns=("campaign", lambda values: sorted({str(v) for v in values if v})),
        evidence_ids=("evidence_id", list),
    )

    # Divide only where the denominator is non-zero. Pandas would return inf and
    # a RuntimeWarning otherwise, and `inf` serialises to invalid JSON — which
    # fails at the API boundary, a long way from the division that caused it.
    grouped["cpa"] = _safe_divide(grouped["cost"], grouped["conversions"])
    grouped["roas"] = _safe_divide(grouped["conversion_value"], grouped["cost"])

    profitable_mask = grouped["conversions"] > 0
    wasteful_mask = (grouped["conversions"] <= 0) & (grouped["cost"] > 0)

    profitable_all = grouped[profitable_mask].sort_values(
        ["conversion_value", "conversions"], ascending=False
    )
    wasteful_all = grouped[wasteful_mask].sort_values("cost", ascending=False)

    result = SearchTermPnl(
        profitable=[_row(term, data) for term, data in profitable_all.head(TOP_N).iterrows()],
        wasteful=[_row(term, data) for term, data in wasteful_all.head(TOP_N).iterrows()],
        totals=_totals(grouped, profitable=int(profitable_mask.sum())),
    )
    for name, full, kept in (
        ("profitable_terms", len(profitable_all), len(result.profitable)),
        ("wasteful_terms", len(wasteful_all), len(result.wasteful)),
    ):
        if full > kept:
            result.truncated[name] = full - kept

    if result.truncated:
        # PRD §16 has no entry for "the list was quietly cut short", because a
        # silent cap reads as complete coverage. Say it in the log and in the
        # output document both.
        log.info("pnl.truncated", **result.truncated, top_n=TOP_N)
    return result


def _frame(rows: list[dict[str, Any]], evidence_ids: list[uuid.UUID]) -> pd.DataFrame:
    """Raw payloads to a typed frame, one row per (term, month, campaign) record."""
    frame = with_ids(rows, evidence_ids)
    if "search_term" not in frame.columns:
        return pd.DataFrame()

    frame["term"] = frame["search_term"].astype("string").str.strip().str.lower()
    frame = frame[frame["term"].notna() & (frame["term"] != "")]
    if frame.empty:
        return frame

    if "campaign" not in frame.columns:
        frame["campaign"] = None
    for column in NUMERIC_COLUMNS:
        if column not in frame.columns:
            frame[column] = 0.0
        frame[column] = numeric(frame, column).fillna(0.0)
    return frame


def _safe_divide(numerator: pd.Series[float], denominator: pd.Series[float]) -> pd.Series:
    return (numerator / denominator.where(denominator > 0)).round(4)


def _row(term: Any, data: pd.Series[Any]) -> TermRow:
    ids = [item for item in data["evidence_ids"] if isinstance(item, uuid.UUID)]
    return TermRow(
        term=str(term),
        cost=_money(data["cost"]),
        conversions=round(float(data["conversions"]), 2),
        conversion_value=_money(data["conversion_value"]),
        impressions=int(data["impressions"]),
        clicks=int(data["clicks"]),
        cpa=_money(data["cpa"]) if pd.notna(data["cpa"]) else None,
        roas=round(float(data["roas"]), 3) if pd.notna(data["roas"]) else None,
        campaigns=tuple(data["campaigns"])[:5],
        evidence_ids=tuple(dict.fromkeys(ids))[:MAX_IDS_PER_TERM],
    )


def _totals(grouped: pd.DataFrame, *, profitable: int) -> Totals:
    cost = float(grouped["cost"].sum())
    conversions = float(grouped["conversions"].sum())
    value = float(grouped["conversion_value"].sum())
    wasted = float(grouped.loc[grouped["conversions"] <= 0, "cost"].sum())
    return Totals(
        terms=int(len(grouped)),
        cost=_money(cost),
        conversions=round(conversions, 2),
        conversion_value=_money(value),
        wasted_spend=_money(wasted),
        # Of money spent, not of terms: "38% of spend bought nothing" is the
        # sentence a marketer acts on; "38% of terms" is not.
        waste_pct=round(100.0 * wasted / cost, 2) if cost > 0 else 0.0,
        profitable_terms=profitable,
        wasteful_terms=int(((grouped["conversions"] <= 0) & (grouped["cost"] > 0)).sum()),
        blended_cpa=_money(cost / conversions) if conversions > 0 else None,
        blended_roas=round(value / cost, 3) if cost > 0 else None,
    )


def _money(value: Any) -> float:
    return round(float(value), 2)
