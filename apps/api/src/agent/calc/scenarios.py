"""Cautious, expected and aggressive monthly envelopes (PRD §9.2).

Node 2.2.3 (`budget_scenarios`) offers three numbers to the budget gate. Their
construction is the interesting part:

**Expected is the forecast, per month.** Not a target anyone picked — the
monthly mean of what `forecast.traffic_v1` says the demand costs.

**Cautious and aggressive are steps off expected**, and the step sizes are
constants with a source (`budget.cautious_step_pct`, `budget.aggressive_step_pct`)
rather than numbers in this file. Global law 15.

**Aggressive is capped by measured impression-share headroom.** You cannot spend
into impressions that do not exist. If the account already holds 85% of
available impressions, a 40% budget increase buys 15% more traffic and 25% more
CPC, and a plan that promises otherwise is a plan that misses. The cap is the
one place diminishing returns enter the arithmetic, and it is why scaling
clicks linearly elsewhere is defensible rather than naive.

**Every scenario respects the per-campaign monthly floor.** A campaign under
`budget.min_monthly_per_campaign_usd` never leaves the learning period, so a
cautious scenario that funds six campaigns at $300 is not cautious, it is six
campaigns that do not work.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from agent.calc import rows
from agent.calc.registry import CalcDraft, CalcError, formula, money, pct, ratio
from agent.planning.constants import PlanningConstants

SCENARIO_NAMES = ("cautious", "expected", "aggressive")

#: What `forecast.traffic_v1` emits per row and this formula reads back.
FORECAST_COLUMNS = ("month", "clicks", "conversions", "cost_usd")


@formula("scenarios.envelope_v1", kind="calc_scenario")
def envelope_v1(
    forecast: pd.DataFrame,
    *,
    constants: PlanningConstants,
    campaign_count: int,
    headroom_pct: float | None = None,
    target_cpa_usd: float | None = None,
    acv_usd: float | None = None,
) -> CalcDraft:
    """Three monthly totals, each with the traffic and efficiency it buys."""
    if campaign_count < 1:
        raise CalcError(f"campaign_count must be at least 1, got {campaign_count}")

    cautious_step = constants.get("budget.cautious_step_pct").value
    aggressive_step = constants.get("budget.aggressive_step_pct").value
    reserve = constants.get("budget.experiment_reserve_pct").value
    floor_per_campaign = constants.get("budget.min_monthly_per_campaign_usd").value
    if not 0 <= cautious_step < 100:
        raise CalcError(f"budget.cautious_step_pct must be in [0, 100), got {cautious_step}")
    if aggressive_step < 0:
        raise CalcError(f"budget.aggressive_step_pct must not be negative, got {aggressive_step}")
    if not 0 <= reserve < 100:
        raise CalcError(f"budget.experiment_reserve_pct must be in [0, 100), got {reserve}")

    records = rows.records(forecast, FORECAST_COLUMNS, what="forecast")
    months = {rows.text(record, "month", default="-") for record in records}
    month_count = len(months)
    total_cost = rows.total(rows.number(record, "cost_usd") for record in records)
    total_clicks = rows.total(rows.number(record, "clicks") for record in records)
    total_conv = rows.total(rows.number(record, "conversions") for record in records)
    if total_cost <= 0:
        raise CalcError("the forecast costs nothing — there is no envelope to set")

    expected = total_cost / month_count
    monthly_clicks = total_clicks / month_count
    monthly_conv = total_conv / month_count
    floor_total = floor_per_campaign * campaign_count

    raw = {
        "cautious": expected * (1 - cautious_step / 100),
        "expected": expected,
        "aggressive": expected * (1 + aggressive_step / 100),
    }
    capped = False
    if headroom_pct is not None:
        if headroom_pct < 0:
            raise CalcError(f"headroom_pct must not be negative, got {headroom_pct}")
        ceiling = expected * (1 + headroom_pct / 100)
        if raw["aggressive"] > ceiling:
            raw["aggressive"] = ceiling
            capped = True

    scenarios: list[dict[str, Any]] = []
    for name in SCENARIO_NAMES:
        monthly_total = raw[name]
        floored = monthly_total < floor_total
        if floored:
            monthly_total = floor_total
        scale = monthly_total / expected
        est_clicks = monthly_clicks * scale
        est_conv = monthly_conv * scale
        working = monthly_total * (1 - reserve / 100)

        assumptions = [
            f"clicks and conversions scale linearly with spend at a constant "
            f"${money(total_cost / total_clicks) if total_clicks > 0 else 0:,.2f} CPC",
            f"expected is the monthly mean of a {month_count}-month forecast",
        ]
        risks: list[str] = []
        if floored:
            assumptions.append(
                f"raised to the ${floor_total:,.2f} floor for {campaign_count} campaign(s) "
                f"at ${floor_per_campaign:,.2f} each"
            )
        if name == "aggressive" and capped:
            risks.append(
                f"capped at {headroom_pct:g}% impression-share headroom — the extra "
                f"{aggressive_step:g}% of budget has nowhere to go"
            )
        if name == "aggressive" and not capped and headroom_pct is None:
            risks.append("no impression-share headroom was measured, so this is uncapped")

        scenario = {
            "name": name,
            "monthly_total_usd": money(monthly_total),
            "quarterly_total_usd": money(monthly_total * 3),
            "working_budget_usd": money(working),
            "experiment_reserve_usd": money(monthly_total - working),
            "experiment_reserve_pct": pct(reserve),
            "scale_vs_expected": ratio(scale),
            "est_clicks": money(est_clicks),
            "est_conv": money(est_conv),
            "est_cpa": money(monthly_total / est_conv) if est_conv > 0 else None,
            "floor_applied": floored,
            "headroom_capped": name == "aggressive" and capped,
            "assumptions": assumptions,
            "risks": risks,
        }
        if acv_usd is not None:
            if acv_usd <= 0:
                raise CalcError(f"acv_usd must be positive when given, got {acv_usd}")
            scenario["est_pipeline_usd"] = money(est_conv * acv_usd)
        scenarios.append(scenario)

    forecast_cpa = total_cost / total_conv if total_conv > 0 else None
    recommended, reason = _recommend(forecast_cpa, target_cpa_usd)

    return CalcDraft(
        inputs={
            "forecast": records,
            "campaign_count": campaign_count,
            "headroom_pct": headroom_pct,
            "target_cpa_usd": target_cpa_usd,
            "acv_usd": acv_usd,
            "cautious_step_pct": cautious_step,
            "aggressive_step_pct": aggressive_step,
            "experiment_reserve_pct": reserve,
            "min_monthly_per_campaign_usd": floor_per_campaign,
        },
        result={
            "scenarios": scenarios,
            "recommended": recommended,
            "recommendation_reason": reason,
            "month_count": month_count,
            "forecast_monthly_usd": money(expected),
            "forecast_cpa_usd": money(forecast_cpa) if forecast_cpa is not None else None,
            "floor_total_usd": money(floor_total),
        },
        summary=(
            f"Monthly envelope: cautious ${scenarios[0]['monthly_total_usd']:,.2f}, "
            f"expected ${scenarios[1]['monthly_total_usd']:,.2f}, "
            f"aggressive ${scenarios[2]['monthly_total_usd']:,.2f}"
            f" — recommending {recommended} ({reason})"
        ),
        constants_version=constants.version,
    )


def _recommend(forecast_cpa: float | None, target_cpa: float | None) -> tuple[str, str]:
    """Which scenario to put in front of the budget owner, and why.

    `aggressive` is never recommended automatically. Under linear scaling every
    scenario shares the same CPA, so no arithmetic here can tell aggressive from
    expected on efficiency — the difference is appetite for risk, and that is
    exactly what gate G3 exists to collect from a person. Recommending it from a
    formula would be dressing up a judgement call as a calculation.
    """
    if target_cpa is not None and forecast_cpa is not None and forecast_cpa > target_cpa:
        return (
            "cautious",
            f"forecast CPA ${forecast_cpa:,.2f} is above the ${target_cpa:,.2f} target, "
            f"so buy less until efficiency improves",
        )
    if target_cpa is None:
        return "expected", "no target CPA was supplied to test the forecast against"
    return (
        "expected",
        f"forecast CPA ${forecast_cpa or 0:,.2f} is within the ${target_cpa:,.2f} target",
    )
