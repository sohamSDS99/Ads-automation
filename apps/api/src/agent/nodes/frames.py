"""Every number stages 1.1 and 1.2 report, computed in pandas.

PRD §18 law 3 again, this time for the CRM and campaign rollups: share of
revenue, average contract value, LTV, payback, seasonal demand months and
period-over-period deltas are all arithmetic, so none of them are asked of a
model. The nodes call in here, hand the model the resulting table, and ask it
only for the things a table cannot hold — a segment's name, what triggers a
purchase, why a campaign won.

Each rollup carries the `evidence_id`s of the rows it aggregated, which is what
lets a node cite grounded facts about a group of 400 CRM rows without putting
400 rows in a prompt.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import pandas as pd

#: Headcount bands. Coarse on purpose — an ICP argument is won at "SMB versus
#: enterprise", not at 250 versus 280 employees.
SIZE_BANDS: tuple[tuple[str, float, float], ...] = (
    ("1-49", 0, 50),
    ("50-249", 50, 250),
    ("250-999", 250, 1000),
    ("1000+", 1000, float("inf")),
)

#: Evidence ids kept per aggregated group. Enough to check the claim by hand,
#: bounded so one segment cannot fill the output document.
MAX_IDS_PER_GROUP = 15

#: Segments reaching a prompt, ordered by revenue. The `Rollup.totals` still
#: counts everything, and anything dropped is reported in `truncated`.
MAX_SEGMENTS = 25


@dataclass(frozen=True, slots=True)
class Segment:
    """One firmographic slice of closed-won revenue."""

    key: str
    industry: str
    country: str
    size_band: str
    deals: int
    revenue: float
    share_of_revenue_pct: float
    avg_deal_value: float
    evidence_ids: tuple[uuid.UUID, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "industry": self.industry,
            "country": self.country,
            "size_band": self.size_band,
            "deals": self.deals,
            "revenue": self.revenue,
            "share_of_revenue_pct": self.share_of_revenue_pct,
            "avg_deal_value": self.avg_deal_value,
        }


@dataclass(frozen=True, slots=True)
class Economics:
    """The unit economics of one product line, derived from CRM plus assumptions.

    The assumptions (`gross_margin_pct`, `lifetime_months`, `ltv_cac_ratio`) are
    the model's contribution; every figure below is arithmetic over them and the
    measured ACV.
    """

    acv: float
    median_deal_value: float
    deals: int
    revenue: float
    gross_margin_pct: float
    lifetime_months: float
    ltv_estimate: float
    target_cac: float
    payback_months: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "acv": self.acv,
            "median_deal_value": self.median_deal_value,
            "deals": self.deals,
            "revenue": self.revenue,
            "gross_margin_pct": self.gross_margin_pct,
            "lifetime_months": self.lifetime_months,
            "ltv_estimate": self.ltv_estimate,
            "target_cac": self.target_cac,
            "payback_months": self.payback_months,
        }


@dataclass(frozen=True, slots=True)
class MarketDemand:
    """When a market actually buys, measured from closed-won dates."""

    country: str
    deals: int
    monthly_deals: tuple[int, ...]
    demand_months: tuple[int, ...]
    dead_months: tuple[int, ...]
    evidence_ids: tuple[uuid.UUID, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "country": self.country,
            "deals": self.deals,
            "monthly_deals": list(self.monthly_deals),
            "demand_months": list(self.demand_months),
            "dead_months": list(self.dead_months),
        }


@dataclass(frozen=True, slots=True)
class CampaignRow:
    """One campaign over the window, split into two halves to give a delta."""

    campaign: str
    cost: float
    conversions: float
    conversion_value: float
    cpa: float | None
    roas: float | None
    first_half_cpa: float | None
    second_half_cpa: float | None
    cpa_delta_pct: float | None
    period: str
    evidence_ids: tuple[uuid.UUID, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "campaign": self.campaign,
            "cost": self.cost,
            "conversions": self.conversions,
            "conversion_value": self.conversion_value,
            "cpa": self.cpa,
            "roas": self.roas,
            "first_half_cpa": self.first_half_cpa,
            "second_half_cpa": self.second_half_cpa,
            "cpa_delta_pct": self.cpa_delta_pct,
            "period": self.period,
        }


# ---------------------------------------------------------------------------
# CRM
# ---------------------------------------------------------------------------


def crm_frame(payloads: list[dict[str, Any]], evidence_ids: list[uuid.UUID]) -> pd.DataFrame:
    """CRM evidence payloads as a typed frame. Empty in, empty out."""
    if not payloads:
        return pd.DataFrame()
    frame = with_ids(payloads, evidence_ids)
    for column in ("account_name", "industry", "country", "close_reason", "source"):
        frame[column] = frame[column].astype("string").fillna("") if column in frame.columns else ""
    frame["deal_value"] = (
        numeric(frame, "deal_value").fillna(0.0) if "deal_value" in frame.columns else 0.0
    )
    frame["employee_count"] = (
        numeric(frame, "employee_count") if "employee_count" in frame.columns else pd.NA
    )
    frame["size_band"] = frame["employee_count"].map(size_band)
    frame["month"] = _month_of(frame, "created_at")
    return frame


def size_band(headcount: Any) -> str:
    """The band a headcount falls in, or `unknown` when the CSV did not carry one."""
    if headcount is None or pd.isna(headcount):
        return "unknown"
    value = float(headcount)
    for label, low, high in SIZE_BANDS:
        if low <= value < high:
            return label
    return "unknown"  # pragma: no cover — the bands are exhaustive for value >= 0


def crm_segments(frame: pd.DataFrame) -> tuple[list[Segment], int]:
    """Closed-won revenue sliced by industry x country x size. Returns (segments, dropped)."""
    if frame.empty:
        return [], 0
    revenue_total = float(frame["deal_value"].sum())
    grouped = frame.groupby(["industry", "country", "size_band"], dropna=False).agg(
        deals=("account_name", "size"),
        revenue=("deal_value", "sum"),
        evidence_ids=("evidence_id", list),
    )
    # `reset_index` before reading rows: the group key is a three-part index,
    # and unpacking it out of `iterrows()` gives an untyped tuple. As plain
    # columns each part keeps its name at the point of use.
    grouped = grouped.sort_values(["revenue", "deals"], ascending=False).reset_index()

    segments: list[Segment] = []
    for record in grouped.head(MAX_SEGMENTS).to_dict("records"):
        industry = str(record["industry"] or "unknown")
        country = str(record["country"] or "unknown")
        band = str(record["size_band"] or "unknown")
        revenue = round(float(record["revenue"]), 2)
        deals = int(record["deals"])
        segments.append(
            Segment(
                key=f"{industry}|{country}|{band}",
                industry=industry,
                country=country,
                size_band=band,
                deals=deals,
                revenue=revenue,
                # Share of *revenue*, not of deals: a segment that is 5% of
                # logos and 40% of money is the one paid search should chase.
                share_of_revenue_pct=(
                    round(100.0 * revenue / revenue_total, 2) if revenue_total > 0 else 0.0
                ),
                avg_deal_value=round(revenue / deals, 2) if deals else 0.0,
                evidence_ids=_ids(list(record["evidence_ids"])),
            )
        )
    return segments, max(0, len(grouped) - len(segments))


def crm_economics(
    frame: pd.DataFrame,
    *,
    gross_margin_pct: float,
    lifetime_months: float,
    ltv_cac_ratio: float,
) -> Economics:
    """ACV from the CRM, then LTV, target CAC and payback from the assumptions.

    `target_cac = ltv / ratio` is the standard SaaS rule and the reason the
    ratio is an input rather than a constant: a 3:1 target and a 5:1 target are
    different businesses, and that call belongs to whoever reads the report.
    """
    values = frame["deal_value"] if not frame.empty else pd.Series(dtype="float64")
    paying = values[values > 0]
    acv = round(float(paying.mean()), 2) if len(paying) else 0.0
    margin = max(0.0, min(100.0, gross_margin_pct)) / 100.0
    months = max(1.0, lifetime_months)
    ratio = max(1.0, ltv_cac_ratio)

    ltv = round(acv * (months / 12.0) * margin, 2)
    target_cac = round(ltv / ratio, 2)
    monthly_gross_profit = (acv / 12.0) * margin
    return Economics(
        acv=acv,
        median_deal_value=round(float(paying.median()), 2) if len(paying) else 0.0,
        deals=int(len(paying)),
        revenue=round(float(paying.sum()), 2) if len(paying) else 0.0,
        gross_margin_pct=round(margin * 100.0, 2),
        lifetime_months=round(months, 1),
        ltv_estimate=ltv,
        target_cac=target_cac,
        payback_months=(
            round(target_cac / monthly_gross_profit, 1) if monthly_gross_profit > 0 else None
        ),
    )


def crm_demand_by_country(frame: pd.DataFrame) -> list[MarketDemand]:
    """Monthly closed-won counts per country, and which months are live or dead.

    A month is *demand* when it is at or above the country's own mean and *dead*
    when it is at or below half of it. Relative to the country, never across
    countries: a market with 12 deals a year and one with 1,200 both have a
    shape, and it is the shape that tells you when to spend.
    """
    if frame.empty or "month" not in frame.columns:
        return []
    dated = frame[frame["month"].notna()]
    if dated.empty:
        return []

    markets: list[MarketDemand] = []
    for country, rows in dated.groupby("country", dropna=False):
        counts = [int((rows["month"] == month).sum()) for month in range(1, 13)]
        mean = sum(counts) / 12.0
        markets.append(
            MarketDemand(
                country=str(country or "unknown"),
                deals=int(len(rows)),
                monthly_deals=tuple(counts),
                demand_months=tuple(
                    month for month, count in enumerate(counts, start=1) if count >= mean > 0
                ),
                dead_months=tuple(
                    month for month, count in enumerate(counts, start=1) if count <= mean / 2
                ),
                evidence_ids=_ids(list(rows["evidence_id"])),
            )
        )
    return sorted(markets, key=lambda item: item.deals, reverse=True)


def lost_reasons(frame: pd.DataFrame) -> list[dict[str, Any]]:
    """Closed-lost reasons by frequency, with the rows behind each one."""
    if frame.empty or "close_reason" not in frame.columns:
        return []
    labelled = frame[frame["close_reason"].astype("string").str.strip() != ""]
    if labelled.empty:
        return []
    grouped = labelled.groupby("close_reason", dropna=False).agg(
        deals=("account_name", "size"),
        lost_value=("deal_value", "sum"),
        industries=("industry", lambda values: sorted({str(v) for v in values if v})[:5]),
        evidence_ids=("evidence_id", list),
    )
    grouped = grouped.sort_values("deals", ascending=False)
    return [
        {
            "reason": str(reason),
            "deals": int(data["deals"]),
            "lost_value": round(float(data["lost_value"]), 2),
            "industries": list(data["industries"]),
            "evidence_ids": [str(item) for item in _ids(data["evidence_ids"])],
        }
        for reason, data in grouped.head(MAX_SEGMENTS).iterrows()
    ]


# ---------------------------------------------------------------------------
# Google Ads
# ---------------------------------------------------------------------------


def campaign_rollup(
    payloads: list[dict[str, Any]], evidence_ids: list[uuid.UUID]
) -> list[CampaignRow]:
    """Per-campaign totals plus a first-half/second-half CPA delta.

    The window is split at its own midpoint rather than at a fixed date, so a
    12-month pull and a 24-month pull both produce a comparable "is this getting
    better or worse" number — which is exactly what PRD §10 1.2.1 asks for as
    `metric_delta`.
    """
    if not payloads:
        return []
    frame = with_ids(payloads, evidence_ids)
    if "campaign" not in frame.columns:
        return []
    frame["campaign"] = frame["campaign"].astype("string").fillna("")
    frame = frame[frame["campaign"] != ""]
    if frame.empty:
        return []
    for column in ("cost", "conversions", "conversion_value"):
        frame[column] = numeric(frame, column).fillna(0.0) if column in frame.columns else 0.0
    frame["month"] = frame["month"].astype("string") if "month" in frame.columns else ""

    months = sorted({value for value in frame["month"].dropna().tolist() if value})
    midpoint = months[len(months) // 2] if len(months) > 1 else None
    period = f"{months[0]}..{months[-1]}" if months else "unknown"

    rows: list[CampaignRow] = []
    for campaign, data in frame.groupby("campaign", dropna=False):
        cost = float(data["cost"].sum())
        conversions = float(data["conversions"].sum())
        value = float(data["conversion_value"].sum())
        first = _half_cpa(data, midpoint, second=False)
        second = _half_cpa(data, midpoint, second=True)
        rows.append(
            CampaignRow(
                campaign=str(campaign),
                cost=round(cost, 2),
                conversions=round(conversions, 2),
                conversion_value=round(value, 2),
                cpa=round(cost / conversions, 2) if conversions > 0 else None,
                roas=round(value / cost, 3) if cost > 0 else None,
                first_half_cpa=first,
                second_half_cpa=second,
                cpa_delta_pct=(
                    round(100.0 * (second - first) / first, 2)
                    if first and second and first > 0
                    else None
                ),
                period=period,
                evidence_ids=_ids(list(data["evidence_id"])),
            )
        )
    return sorted(rows, key=lambda item: item.cost, reverse=True)[:MAX_SEGMENTS]


def _half_cpa(data: pd.DataFrame, midpoint: str | None, *, second: bool) -> float | None:
    if midpoint is None:
        return None
    half = data[data["month"] >= midpoint] if second else data[data["month"] < midpoint]
    conversions = float(half["conversions"].sum())
    if conversions <= 0:
        return None
    return round(float(half["cost"].sum()) / conversions, 2)


# ---------------------------------------------------------------------------
# shared
# ---------------------------------------------------------------------------


def with_ids(payloads: list[dict[str, Any]], evidence_ids: list[uuid.UUID]) -> pd.DataFrame:
    """Payloads as a frame, each row carrying the id of the evidence it came from.

    The id is merged into the record rather than assigned as a column
    afterwards, which keeps the two lists paired by construction: a mismatch
    becomes an error here rather than a silently misattributed citation three
    functions later.
    """
    if len(payloads) != len(evidence_ids):
        raise ValueError(
            "payloads and evidence_ids differ in length "
            f"({len(payloads)} vs {len(evidence_ids)}); they must be parallel"
        )
    return pd.DataFrame(
        [
            {**payload, "evidence_id": evidence_id}
            for payload, evidence_id in zip(payloads, evidence_ids, strict=True)
        ]
    )


def numeric(frame: pd.DataFrame, column: str) -> pd.Series[Any]:
    """One column as numbers, with anything unparseable as NaN rather than a raise."""
    return pd.to_numeric(frame[column], errors="coerce")


def _ids(values: list[Any]) -> tuple[uuid.UUID, ...]:
    unique = dict.fromkeys(item for item in values if isinstance(item, uuid.UUID))
    return tuple(unique)[:MAX_IDS_PER_GROUP]


def _month_of(frame: pd.DataFrame, column: str) -> pd.Series[Any]:
    """Calendar month (1-12) of a CRM date column, NA where it cannot be read."""
    if column not in frame.columns:
        return pd.Series(pd.NA, index=frame.index, dtype="Int64")
    parsed = pd.to_datetime(frame[column], errors="coerce", format="mixed", utc=True)
    return parsed.dt.month.astype("Int64")
