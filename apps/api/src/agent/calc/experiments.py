"""Ranking a test backlog, and paying for it (Stage 02 PRD §11, node 2.5.3).

`power.sample_size_v1` says how much traffic a test needs. This module says
which tests are worth running, in what order, and which of them the experiment
reserve can actually afford.

**Why this is not in PRD §9.2.** That table stops at nine formulas and none of
them serves 2.5.3's output beyond the sample size. Yet §11 mandates
`ice_score`, `rank` and `reserve_usd` as fields on every test, and global law
14 forbids the model producing a number. Something has to compute them, and the
choice is between a tenth registered formula and three figures with no
`PlanCalc` row behind them. `calc/measurement.py` made the same argument in
S2-P5a and this follows it.

**`reserve_usd` is a cost, not a share.** The tempting implementation splits the
experiment reserve across the backlog pro rata, which produces a tidy number
that means nothing: a test funded at 40% of what its traffic costs does not
return a 40%-significant answer, it returns nothing. So the cost of a test is
what its own power calculation demands —

    cost = required_visitors_total x avg_cpc_usd

— and the reserve funds tests in rank order until it runs out. What it cannot
reach is not underfunded; it is `not_yet`, which is the honest word for it and
the one §11 already uses.

The `kind` is `calc_power` rather than a tenth Evidence kind. §7.3's kinds
partition *what wrote a derived row*, and everything here was written for the
same node the power calculation serves.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from agent.calc import rows
from agent.calc.registry import CalcDraft, CalcError, formula, money, ratio
from agent.planning.constants import PlanningConstants

CANDIDATE_COLUMNS = ("id", "impact_1_5", "confidence_1_5", "effort_1_5")

#: ICE ratings are a 1-5 scale by definition. A rating outside it is clamped
#: rather than refused: the score is ordinal, the model supplied a judgement
#: that is merely badly calibrated, and dropping the test would lose the
#: hypothesis along with the bad number. Every clamp is reported.
MIN_RATING = 1.0
MAX_RATING = 5.0

#: A test nobody will wait for is not a test. Anything past this is ranked but
#: never funded, and says why in `not_yet`.
MAX_PATIENCE_DAYS = 180.0


@formula("experiments.ice_rank_v1", kind="calc_power")
def ice_rank_v1(
    candidates: pd.DataFrame,
    *,
    constants: PlanningConstants,
    reserve_pool_usd: float = 0.0,
) -> CalcDraft:
    """ICE score, rank and the reserve each planned test is actually funded."""
    reserve_pct = constants.get("budget.experiment_reserve_pct").value
    if reserve_pool_usd < 0:
        raise CalcError(f"reserve_pool_usd must not be negative, got {reserve_pool_usd}")

    records = rows.records(candidates, CANDIDATE_COLUMNS, what="test candidates")
    scored: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    clamped: list[str] = []

    for record in records:
        test_id = rows.text(record, "id", default="(unnamed)")
        try:
            impact = rows.number(record, "impact_1_5")
            confidence = rows.number(record, "confidence_1_5")
            effort = rows.number(record, "effort_1_5")
        except CalcError as exc:
            excluded.append({"id": test_id, "reason": str(exc)})
            continue

        impact, impact_clamped = _clamp(impact)
        confidence, confidence_clamped = _clamp(confidence)
        effort, effort_clamped = _clamp(effort)
        if impact_clamped or confidence_clamped or effort_clamped:
            clamped.append(test_id)

        visitors = rows.number(record, "required_visitors_total", default=0.0)
        cpc = rows.number(record, "avg_cpc_usd", default=0.0)
        days = rows.number(record, "est_days_to_significance", default=-1.0)

        cost = visitors * cpc
        scored.append(
            {
                "id": test_id,
                "impact_1_5": ratio(impact),
                "confidence_1_5": ratio(confidence),
                "effort_1_5": ratio(effort),
                # The ICE convention: impact x confidence, divided by what it
                # costs to find out. Effort cannot be zero — `_clamp` floors it
                # at 1 — so this division is total.
                "ice_score": ratio(impact * confidence / effort),
                "required_visitors_total": visitors if visitors > 0 else None,
                "avg_cpc_usd": money(cpc) if cpc > 0 else None,
                "cost_usd": money(cost) if cost > 0 else None,
                "est_days_to_significance": int(days) if days > 0 else None,
                "launch_wave": rows.text(record, "launch_wave", default=""),
            }
        )

    if not scored:
        raise CalcError(
            "no test candidate could be scored: "
            + "; ".join(f"{row['id']}: {row['reason']}" for row in excluded)
        )

    # Descending ICE, then cheapest first, then by id. The last term is not
    # decoration: PT3 wants byte-identical output from identical inputs, and
    # two tests tied on both score and cost would otherwise rank in whatever
    # order the frame happened to arrive in.
    scored.sort(key=lambda row: (-row["ice_score"], row["cost_usd"] or 0.0, row["id"]))

    spent = 0.0
    funded_count = 0
    for position, row in enumerate(scored, start=1):
        row["rank"] = position
        cost = row["cost_usd"]
        days = row["est_days_to_significance"]

        if cost is None:
            row["funded"] = False
            row["reserve_usd"] = 0.0
            row["blocked_by"] = (
                "no traffic forecast for this campaign, so the cost of running it is unknown"
            )
            continue
        if days is None:
            row["funded"] = False
            row["reserve_usd"] = 0.0
            row["blocked_by"] = "no click forecast, so there is no date this test would read out"
            continue
        if days > MAX_PATIENCE_DAYS:
            row["funded"] = False
            row["reserve_usd"] = 0.0
            row["blocked_by"] = (
                f"{days} days to significance, past the {MAX_PATIENCE_DAYS:g}-day horizon — "
                "the campaign needs more traffic before this is testable"
            )
            continue

        remaining = reserve_pool_usd - spent
        if cost > remaining:
            row["funded"] = False
            row["reserve_usd"] = 0.0
            row["blocked_by"] = (
                f"costs ${cost:,.2f} and the experiment reserve has ${max(remaining, 0.0):,.2f} "
                "left at this rank"
            )
            continue

        spent = spent + cost
        funded_count = funded_count + 1
        row["funded"] = True
        row["reserve_usd"] = cost
        row["blocked_by"] = None

    return CalcDraft(
        inputs={
            "candidates": records,
            "reserve_pool_usd": reserve_pool_usd,
            "experiment_reserve_pct": reserve_pct,
            "max_patience_days": MAX_PATIENCE_DAYS,
        },
        result={
            "tests": scored,
            "test_count": len(scored),
            "funded_count": funded_count,
            "reserve_pool_usd": money(reserve_pool_usd),
            "reserve_committed_usd": money(spent),
            "reserve_unspent_usd": money(reserve_pool_usd - spent),
            "clamped_ratings": clamped,
        },
        summary=(
            f"{len(scored)} test(s) ranked; {funded_count} funded from a "
            f"${reserve_pool_usd:,.2f} reserve, committing ${spent:,.2f}. "
            f"Top: {scored[0]['id']} at ICE {scored[0]['ice_score']:g}"
        ),
        constants_version=constants.version,
        excluded=excluded,
    )


def _clamp(value: float) -> tuple[float, bool]:
    """A 1-5 rating, and whether it had to be moved to get there."""
    if value < MIN_RATING:
        return MIN_RATING, True
    if value > MAX_RATING:
        return MAX_RATING, True
    return value, False
