"""Search volume to spend: the chain every media plan figure hangs off (PRD §9.2).

    impressions = searchVolume x seasonalityIndex x impressionShareTarget
    clicks      = impressions x CTR
    cost        = clicks x CPC
    conversions = clicks x CVR
    CPA         = cost / conversions

Node 2.2.1 (`demand_forecast`) reports it per cluster, per market, per month.
Two things about this module are deliberate and worth knowing before reading it:

**Totals are sums of the rounded rows, not rounded sums.** A media plan is read
as a table, and a table whose total does not equal the sum of its visible rows
is a defect a reader will find in ten seconds. Rates at the total level are
re-derived from the rounded totals for the same reason.

**The confidence band comes from the CPC range, not from a feeling.** Where the
source supplied `cpc_low_usd`/`cpc_high_usd` the band is what those costs work
out to; where it did not, the band is zero and says so, rather than inventing a
plus-or-minus that would then be quoted back as if it had been measured.
"""

from __future__ import annotations

from typing import Any, Literal

import pandas as pd

from agent.calc import rows
from agent.calc.registry import CalcDraft, CalcError, formula, money, pct, ratio
from agent.planning.constants import PlanningConstants

Method = Literal["google_forecast", "derived_arithmetic"]

#: The dimensions every forecast row carries, plus the rates the chain needs.
FORECAST_COLUMNS = ("cluster", "market", "month", "ctr_pct", "avg_cpc_usd", "cvr_pct")

#: `google_forecast` means Google's Keyword Planner returned impressions for a
#: bid; `derived_arithmetic` means we turned search volume into impressions
#: ourselves with the impression-share target. Which one produced a figure
#: changes how much weight it deserves, so it is recorded rather than implied.
METHODS: tuple[Method, ...] = ("google_forecast", "derived_arithmetic")


@formula("forecast.traffic_v1", kind="calc_forecast")
def traffic_v1(
    demand: pd.DataFrame,
    *,
    constants: PlanningConstants,
    method: Method = "derived_arithmetic",
) -> CalcDraft:
    """Impressions, clicks, conversions, cost and CPA per cluster per market per month."""
    if method not in METHODS:
        raise CalcError(f"method must be one of {METHODS}, got {method!r}")

    share_target = constants.get("forecast.impression_share_target_pct").value
    if not 0 < share_target <= 100:
        raise CalcError(
            f"forecast.impression_share_target_pct must be in (0, 100], got {share_target}"
        )

    volume_column = "impressions" if method == "google_forecast" else "search_volume"
    records = rows.records(demand, (*FORECAST_COLUMNS, volume_column), what="demand")

    forecast: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    band_low_cost = 0.0
    band_high_cost = 0.0
    has_range = False
    share_weighted = 0.0
    share_weight = 0.0

    for record in records:
        label = (
            f"{rows.text(record, 'cluster', default='(unnamed)')}"
            f"/{rows.text(record, 'market', default='-')}"
            f"/{rows.text(record, 'month', default='-')}"
        )
        try:
            volume = rows.number(record, volume_column)
            ctr = rows.number(record, "ctr_pct")
            cpc = rows.number(record, "avg_cpc_usd")
            cvr = rows.number(record, "cvr_pct")
            seasonality = rows.number(record, "seasonality_index", default=1.0)
            row_share = rows.number(record, "impression_share_target_pct", default=share_target)
        except CalcError as exc:
            excluded.append({"row": label, "reason": str(exc)})
            continue

        if volume < 0 or ctr < 0 or cpc < 0 or cvr < 0 or seasonality < 0:
            excluded.append({"row": label, "reason": "a negative rate or volume cannot forecast"})
            continue
        if not 0 < row_share <= 100:
            excluded.append(
                {"row": label, "reason": f"impression_share_target_pct {row_share} outside (0, 100]"}
            )
            continue
        if ctr > 100 or cvr > 100:
            excluded.append({"row": label, "reason": "ctr_pct or cvr_pct above 100"})
            continue

        # A Google forecast already answers "impressions we would win", so the
        # share target must not be applied a second time.
        impressions = (
            volume * seasonality
            if method == "google_forecast"
            else volume * seasonality * row_share / 100
        )
        clicks = impressions * ctr / 100
        cost = clicks * cpc
        conversions = clicks * cvr / 100

        clicks_r = money(clicks)
        cost_r = money(cost)
        conversions_r = money(conversions)

        forecast.append(
            {
                "cluster": rows.text(record, "cluster", default="(unnamed)"),
                "market": rows.text(record, "market", default="-"),
                "month": rows.text(record, "month", default="-"),
                "impressions": int(round(impressions)),
                "ctr_pct": pct(ctr),
                "clicks": clicks_r,
                "avg_cpc_usd": money(cpc),
                "cvr_pct": pct(cvr),
                "conversions": conversions_r,
                "cost_usd": cost_r,
                "cpa_usd": money(cost_r / conversions_r) if conversions_r > 0 else None,
                "seasonality_index": ratio(seasonality),
            }
        )

        cpc_low = rows.number(record, "cpc_low_usd", default=cpc)
        cpc_high = rows.number(record, "cpc_high_usd", default=cpc)
        if cpc_low != cpc or cpc_high != cpc:
            has_range = True
        band_low_cost += clicks * max(cpc_low, 0.0)
        band_high_cost += clicks * max(cpc_high, 0.0)

        current_share = rows.number(record, "current_impression_share_pct", default=-1.0)
        if current_share >= 0:
            share_weighted += max(0.0, 100 - min(current_share, 100)) * impressions
            share_weight += impressions

    if not forecast:
        raise CalcError(
            "no demand row produced a usable forecast: "
            + "; ".join(f"{row['row']}: {row['reason']}" for row in excluded)
        )

    totals = _totals(forecast)
    monthly = _monthly(forecast)
    expected_cost = totals["cost_usd"]

    band = {
        "basis": "cpc_range" if has_range else "point_estimate",
        "low_pct": pct((band_low_cost / expected_cost - 1) * 100) if expected_cost > 0 else 0.0,
        "high_pct": pct((band_high_cost / expected_cost - 1) * 100) if expected_cost > 0 else 0.0,
    }
    headroom = pct(share_weighted / share_weight) if share_weight > 0 else None

    return CalcDraft(
        inputs={
            "demand": records,
            "method": method,
            "impression_share_target_pct": share_target,
        },
        result={
            "forecast": forecast,
            "monthly_totals": monthly,
            "totals": totals,
            "method": method,
            "confidence_band": band,
            "impression_share_headroom_pct": headroom,
            "month_count": len(monthly),
        },
        summary=(
            f"{method}: {totals['clicks']:,.0f} clicks and {totals['conversions']:,.1f} "
            f"conversions for ${totals['cost_usd']:,.2f} over {len(monthly)} month(s) "
            f"across {len(forecast)} cluster-market-month row(s)"
            + (f" at ${totals['cpa_usd']:,.2f} CPA" if totals["cpa_usd"] else "")
        ),
        constants_version=constants.version,
        excluded=excluded,
    )


def _totals(forecast: list[dict[str, Any]]) -> dict[str, Any]:
    """Sums of the rounded rows, with the rates re-derived from those sums."""
    impressions = sum(int(row["impressions"]) for row in forecast)
    clicks = money(rows.total(row["clicks"] for row in forecast))
    conversions = money(rows.total(row["conversions"] for row in forecast))
    cost = money(rows.total(row["cost_usd"] for row in forecast))
    return {
        "impressions": impressions,
        "clicks": clicks,
        "conversions": conversions,
        "cost_usd": cost,
        "ctr_pct": pct(clicks / impressions * 100) if impressions > 0 else 0.0,
        "avg_cpc_usd": money(cost / clicks) if clicks > 0 else 0.0,
        "cvr_pct": pct(conversions / clicks * 100) if clicks > 0 else 0.0,
        "cpa_usd": money(cost / conversions) if conversions > 0 else None,
    }


def _monthly(forecast: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per month, in the order the months first appear in the input.

    Insertion order rather than sorted: `month` is a label the caller chose and
    may be `2027-01` or `Jan` or `wave 2`. Sorting a label we do not own would
    reorder a plan's own months, so the source's order is the answer.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in forecast:
        grouped.setdefault(str(row["month"]), []).append(row)
    return [{"month": month, **_totals(group)} for month, group in grouped.items()]
