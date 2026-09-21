"""The ceiling every target in a campaign plan is derived from (PRD §9.2).

    maxCPL = (ACV x grossMargin / targetCacRatio) x closeRate(lead->won)

Read left to right that is: the gross profit one customer produces, divided by
how many times over that profit must cover its own acquisition cost, is the most
we can pay for a *customer*; multiplied by the share of leads that become
customers, it is the most we can pay for a *lead*.

Node 2.1.2 (`unit_economics_ceiling`) emits these figures and the model writes
only `method_notes` around them. Nothing downstream may set a target above the
ceiling — critique assertion 3 in PRD §11 fails blocking if it does.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from agent.calc import rows
from agent.calc.registry import (
    CalcDraft,
    CalcError,
    formula,
    money,
    pct,
    ratio,
)
from agent.planning.constants import PlanningConstants

#: Columns `max_cpa_v1` cannot work without. `deals` is optional and decides
#: whether the blend is deal-weighted or flat.
CEILING_COLUMNS = ("segment", "acv_usd", "gross_margin_pct", "lead_to_won_pct")

#: Columns `payback_v1` cannot work without.
PAYBACK_COLUMNS = ("segment", "acv_usd", "gross_margin_pct", "contract_term_months")


def _unusable(row: rows.Row) -> str | None:
    """Why this segment cannot produce a ceiling, or None if it can.

    Excluded rather than raised, one row at a time: a CRM export with one
    zero-revenue segment should still produce a plan for the other four, and the
    reason travels with the result so the exclusion is visible rather than
    quietly absorbed into a smaller average.
    """
    try:
        acv = rows.number(row, "acv_usd")
        margin = rows.number(row, "gross_margin_pct")
        close = rows.number(row, "lead_to_won_pct")
    except CalcError as exc:
        return str(exc)
    if acv <= 0:
        return f"acv_usd is {acv}, so there is no revenue to pay out of"
    if not 0 < margin <= 100:
        return f"gross_margin_pct is {margin}, outside (0, 100]"
    if not 0 < close <= 100:
        return f"lead_to_won_pct is {close}, outside (0, 100]"
    return None


@formula("economics.max_cpa_v1", kind="calc_economics")
def max_cpa_v1(segments: pd.DataFrame, *, constants: PlanningConstants) -> CalcDraft:
    """Max payable per closed-won customer and per lead, per segment and blended."""
    cac_ratio = constants.get("economics.target_cac_ratio").value
    safety = constants.get("economics.safety_margin_pct").value
    if cac_ratio <= 0:
        raise CalcError(f"economics.target_cac_ratio must be positive, got {cac_ratio}")
    if not 0 <= safety < 100:
        raise CalcError(f"economics.safety_margin_pct must be in [0, 100), got {safety}")
    keep = 1 - safety / 100

    records = rows.records(segments, CEILING_COLUMNS, what="segments")
    by_segment: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    weights: list[float] = []

    for record in records:
        name = rows.text(record, "segment", default="(unnamed)")
        reason = _unusable(record)
        if reason is not None:
            excluded.append({"segment": name, "reason": reason})
            continue

        acv = rows.number(record, "acv_usd")
        margin = rows.number(record, "gross_margin_pct")
        close = rows.number(record, "lead_to_won_pct")
        deals = max(rows.number(record, "deals", default=1.0), 0.0)

        gross_profit = acv * margin / 100
        max_cpa_won = gross_profit / cac_ratio
        max_cpl = max_cpa_won * close / 100
        target_cpa_won = max_cpa_won * keep
        target_cpl = max_cpl * keep

        by_segment.append(
            {
                "segment": name,
                "deals": int(deals) if deals.is_integer() else deals,
                "acv_usd": money(acv),
                "gross_margin_pct": pct(margin),
                "lead_to_won_pct": pct(close),
                "gross_profit_usd": money(gross_profit),
                "max_cpa_won_usd": money(max_cpa_won),
                "max_cpl_usd": money(max_cpl),
                "target_cpa_won_usd": money(target_cpa_won),
                "target_cpl_usd": money(target_cpl),
                # ROAS is measured against revenue, not gross profit, because
                # that is what Google Ads reports as conversion value.
                "target_roas": ratio(acv / target_cpa_won),
            }
        )
        weights.append(deals)

    if not by_segment:
        raise CalcError(
            "no segment produced a usable ceiling: "
            + "; ".join(f"{row['segment']}: {row['reason']}" for row in excluded)
        )

    blended = _blend(
        by_segment,
        weights,
        cac_ratio=cac_ratio,
        keep=keep,
        basis="deals" if "deals" in segments.columns else "equal",
    )
    return CalcDraft(
        inputs={
            "segments": [
                {key: record.get(key) for key in (*CEILING_COLUMNS, "deals")} for record in records
            ],
            "target_cac_ratio": cac_ratio,
            "safety_margin_pct": safety,
        },
        result={
            "by_segment": by_segment,
            "blended": blended,
            "segment_count": len(by_segment),
        },
        summary=(
            f"Max CPL ${blended['max_cpl_usd']:,.2f} and max CPA per won customer "
            f"${blended['max_cpa_won_usd']:,.2f} blended over {len(by_segment)} segment(s) "
            f"at a {cac_ratio:g}x CAC ratio; target CPL ${blended['target_cpl_usd']:,.2f} "
            f"after a {safety:g}% safety margin"
        ),
        constants_version=constants.version,
        excluded=excluded,
    )


def _blend(
    by_segment: list[dict[str, Any]],
    weights: list[float],
    *,
    cac_ratio: float,
    keep: float,
    basis: str,
) -> dict[str, Any]:
    """One blended ceiling for the account.

    Derived from blended *inputs*, not by averaging the per-segment ceilings.
    Averaging outputs would answer "what is the mean of five ceilings", which is
    not a question anyone asks; blending the inputs answers "what is the ceiling
    for the book of business as a whole", which is what a single account-level
    target has to respect.

    Weighted by deals when the CRM supplied a count, flat otherwise — and which
    of the two happened is recorded, because a flat blend over segments of very
    different size is a materially different number and a reader should be able
    to tell.
    """
    supplied = rows.total(weights)
    weight_total = supplied
    if weight_total <= 0:
        # Every deal count was zero or absent. Fall back to a flat blend and say
        # so, rather than dividing by nothing.
        weights = [1.0] * len(by_segment)
        weight_total = float(len(by_segment))

    acv = rows.total(
        row["acv_usd"] * weight for row, weight in zip(by_segment, weights, strict=True)
    )
    acv /= weight_total
    profit = rows.total(
        row["gross_profit_usd"] * weight for row, weight in zip(by_segment, weights, strict=True)
    )
    profit /= weight_total
    close = rows.total(
        row["lead_to_won_pct"] * weight for row, weight in zip(by_segment, weights, strict=True)
    )
    close /= weight_total

    # Margin is recovered from the blended profit and revenue rather than
    # averaged directly: a mean of percentages over segments with different
    # revenue is not the margin of the combined revenue.
    margin = profit / acv * 100 if acv > 0 else 0.0
    max_cpa_won = profit / cac_ratio
    max_cpl = max_cpa_won * close / 100
    target_cpa_won = max_cpa_won * keep

    return {
        "weight_basis": basis if supplied > 0 else "equal",
        "acv_usd": money(acv),
        "gross_margin_pct": pct(margin),
        "lead_to_won_pct": pct(close),
        "gross_profit_usd": money(profit),
        "max_cpa_won_usd": money(max_cpa_won),
        "max_cpl_usd": money(max_cpl),
        "target_cpa_won_usd": money(target_cpa_won),
        "target_cpl_usd": money(max_cpl * keep),
        "target_roas": ratio(acv / target_cpa_won) if target_cpa_won > 0 else 0.0,
    }


@formula("economics.payback_v1", kind="calc_economics")
def payback_v1(
    segments: pd.DataFrame,
    *,
    constants: PlanningConstants,
    cac_column: str = "max_cpa_won_usd",
) -> CalcDraft:
    """CAC payback in months and LTV:CAC, per segment and blended.

    `cac_column` defaults to the ceiling from `max_cpa_v1`, which answers "if we
    paid the most we are allowed to pay, how long until that customer has paid
    us back". Pass a column of actual or target CPAs to answer it for a real
    plan instead.

    `lifetime_months` defaults to one contract term. That is the conservative
    reading and the one a plan should be defended with: a renewal that has not
    happened yet is not revenue, and an LTV built on assumed renewals is how a
    CAC ceiling ends up 3x too high.
    """
    records = rows.records(segments, (*PAYBACK_COLUMNS, cac_column), what="segments")
    by_segment: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    weights: list[float] = []

    for record in records:
        name = rows.text(record, "segment", default="(unnamed)")
        try:
            acv = rows.number(record, "acv_usd")
            margin = rows.number(record, "gross_margin_pct")
            term = rows.number(record, "contract_term_months")
            cac = rows.number(record, cac_column)
            lifetime = rows.number(record, "lifetime_months", default=term)
        except CalcError as exc:
            excluded.append({"segment": name, "reason": str(exc)})
            continue

        if acv <= 0 or not 0 < margin <= 100 or term <= 0 or cac <= 0 or lifetime <= 0:
            excluded.append(
                {
                    "segment": name,
                    "reason": (
                        f"needs positive acv_usd, contract_term_months, {cac_column} and "
                        f"lifetime_months and a margin in (0, 100]; got "
                        f"acv={acv}, margin={margin}, term={term}, cac={cac}, "
                        f"lifetime={lifetime}"
                    ),
                }
            )
            continue

        monthly_profit = acv * margin / 100 / term
        ltv = monthly_profit * lifetime
        deals = max(rows.number(record, "deals", default=1.0), 0.0)

        by_segment.append(
            {
                "segment": name,
                "cac_usd": money(cac),
                "monthly_gross_profit_usd": money(monthly_profit),
                "contract_term_months": ratio(term),
                "lifetime_months": ratio(lifetime),
                "payback_months": ratio(cac / monthly_profit),
                "ltv_usd": money(ltv),
                "ltv_to_cac": ratio(ltv / cac),
            }
        )
        weights.append(deals)

    if not by_segment:
        raise CalcError(
            "no segment produced a usable payback: "
            + "; ".join(f"{row['segment']}: {row['reason']}" for row in excluded)
        )

    weight_total = rows.total(weights) or float(len(by_segment))
    if rows.total(weights) <= 0:
        weights = [1.0] * len(by_segment)

    def weighted(key: str) -> float:
        return (
            rows.total(row[key] * weight for row, weight in zip(by_segment, weights, strict=True))
            / weight_total
        )

    blended_cac = weighted("cac_usd")
    blended_profit = weighted("monthly_gross_profit_usd")
    blended_ltv = weighted("ltv_usd")
    blended = {
        "cac_usd": money(blended_cac),
        "monthly_gross_profit_usd": money(blended_profit),
        "payback_months": ratio(blended_cac / blended_profit) if blended_profit > 0 else 0.0,
        "ltv_usd": money(blended_ltv),
        "ltv_to_cac": ratio(blended_ltv / blended_cac) if blended_cac > 0 else 0.0,
    }

    return CalcDraft(
        inputs={
            "segments": [
                {
                    key: record.get(key)
                    for key in (*PAYBACK_COLUMNS, cac_column, "lifetime_months", "deals")
                }
                for record in records
            ],
            "cac_column": cac_column,
        },
        result={"by_segment": by_segment, "blended": blended},
        summary=(
            f"CAC payback {blended['payback_months']:g} months at ${blended['cac_usd']:,.2f} "
            f"CAC, LTV ${blended['ltv_usd']:,.2f}, LTV:CAC {blended['ltv_to_cac']:g}:1 "
            f"over {len(by_segment)} segment(s)"
        ),
        constants_version=constants.version,
        excluded=excluded,
    )
