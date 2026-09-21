"""Splitting an envelope across campaign x market x funnel stage (PRD §9.2).

`split_v1` proposes the split. `whatif_v1` re-forecasts an approver's edited one
for gate G3, which is the only gate that hands a number back to the engine.

The solver is deterministic and explainable by design, because every figure it
produces has to survive a budget owner asking "why does Germany get 18%". It is
weight-and-normalise, with three corrections applied in a fixed order:

1. **Efficiency** — a unit forecast to beat its target CPA earns more budget,
   proportionally. `efficiency = targetCPA / forecastCPA`.
2. **Floors** — anything under `budget.min_monthly_per_campaign_usd` is raised
   to the floor and the rest renormalised, because a campaign under the floor
   never leaves the learning period and spends its budget learning nothing.
   Raising one unit can push another under the floor, so this repeats until it
   settles; if the floors cannot all be met the lowest-weight units are deferred
   to a later wave rather than every campaign being underfunded together.
3. **Caps** — a unit with a measured ceiling on what it can absorb is pinned
   there and its surplus redistributed.

The output sums to the envelope **exactly**, not within a tolerance: the residual
cent from rounding 160 rows is handed to the largest unpinned unit. PRD §12
invariant 4 allows +/-0.5%, but a media plan whose rows do not add up to its own
total is a plan that gets queried instead of approved.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from agent.calc import rows
from agent.calc.registry import CalcDraft, CalcError, formula, money, pct, ratio
from agent.planning.constants import PlanningConstants

#: Identity of one allocation unit.
UNIT_COLUMNS = ("campaign_ref", "market", "funnel_stage")

SPLIT_COLUMNS = (*UNIT_COLUMNS, "forecast_cpa_usd", "target_cpa_usd")
WHATIF_COLUMNS = (*UNIT_COLUMNS, "edited_usd", "forecast_cpa_usd")

#: A numerical bound on a ratio, not a planning threshold — which is why it
#: lives here and not in `planning_constants.yaml`. A unit forecast at 1/50th of
#: its target CPA is far more likely to be a bad CPC estimate than a 50x
#: opportunity, and without a bound that one row would take the whole envelope.
#: Worth revisiting with real forecast data in S2-P3.
MAX_EFFICIENCY = 3.0

#: PRD §12 invariant 4. The default an edited allocation is judged against.
DEFAULT_TOLERANCE_PCT = 0.5

#: Residual below which the sum is treated as exact (half a cent).
CENT = 0.005


def _unit_key(record: rows.Row) -> str:
    return "/".join(rows.text(record, column, default="-") for column in UNIT_COLUMNS)


@formula("allocation.split_v1", kind="calc_allocation")
def split_v1(
    units: pd.DataFrame,
    *,
    constants: PlanningConstants,
    envelope_usd: float,
) -> CalcDraft:
    """Propose a monthly split of `envelope_usd` across the units given."""
    if envelope_usd <= 0:
        raise CalcError(f"envelope_usd must be positive, got {envelope_usd}")
    floor = constants.get("budget.min_monthly_per_campaign_usd").value
    if floor < 0:
        raise CalcError(f"budget.min_monthly_per_campaign_usd must not be negative, got {floor}")

    records = rows.records(units, SPLIT_COLUMNS, what="units")
    candidates: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []

    for record in records:
        key = _unit_key(record)
        try:
            forecast_cpa = rows.number(record, "forecast_cpa_usd")
            target_cpa = rows.number(record, "target_cpa_usd")
            weight = rows.number(record, "strategic_weight", default=1.0)
            cap = rows.number(record, "max_spend_usd", default=-1.0)
        except CalcError as exc:
            excluded.append({"unit": key, "reason": str(exc)})
            continue
        if forecast_cpa <= 0 or target_cpa <= 0 or weight <= 0:
            excluded.append(
                {
                    "unit": key,
                    "reason": (
                        f"needs a positive forecast_cpa_usd, target_cpa_usd and "
                        f"strategic_weight; got {forecast_cpa}, {target_cpa}, {weight}"
                    ),
                }
            )
            continue
        efficiency = min(target_cpa / forecast_cpa, MAX_EFFICIENCY)
        candidates.append(
            {
                "key": key,
                "record": record,
                "forecast_cpa": forecast_cpa,
                "target_cpa": target_cpa,
                "strategic_weight": weight,
                "efficiency": efficiency,
                "weight": weight * efficiency,
                "cap": cap if cap >= 0 else None,
            }
        )

    if not candidates:
        raise CalcError(
            "no unit could be allocated to: "
            + "; ".join(f"{row['unit']}: {row['reason']}" for row in excluded)
        )

    solved = _solve(candidates, envelope_usd=envelope_usd, floor=floor)
    allocation = [
        _render(unit, usd=usd, envelope=envelope_usd, floor=floor, pin=solved.pins.get(unit["key"]))
        for unit, usd in solved.awarded
    ]
    allocated = money(rows.total(row["usd"] for row in allocation))

    return CalcDraft(
        inputs={
            "units": records,
            "envelope_usd": envelope_usd,
            "min_monthly_per_campaign_usd": floor,
            "max_efficiency": MAX_EFFICIENCY,
        },
        result={
            "allocation": allocation,
            "deferred": solved.deferred,
            "envelope_usd": money(envelope_usd),
            "allocated_usd": allocated,
            "unallocated_usd": money(envelope_usd - allocated),
            "unit_count": len(allocation),
            "totals": {
                "est_clicks": money(rows.total(row["est_clicks"] or 0.0 for row in allocation)),
                "est_conv": money(rows.total(row["est_conv"] for row in allocation)),
            },
        },
        summary=(
            f"${allocated:,.2f} of a ${envelope_usd:,.2f} envelope split across "
            f"{len(allocation)} unit(s)"
            + (f", {len(solved.deferred)} deferred" if solved.deferred else "")
        ),
        constants_version=constants.version,
        excluded=excluded,
    )


class _Solution:
    """The awarded split, which units were pinned and why, and what was deferred."""

    def __init__(self) -> None:
        self.awarded: list[tuple[dict[str, Any], float]] = []
        self.pins: dict[str, str] = {}
        self.deferred: list[dict[str, Any]] = []


def _solve(candidates: list[dict[str, Any]], *, envelope_usd: float, floor: float) -> _Solution:
    """Weight, normalise, then pin floors and caps until nothing moves.

    Deferrals accumulate here, across retries, rather than in module state: two
    plan runs solving concurrently in one worker process would otherwise read
    each other's deferred units, and a formula that depends on a global is not
    the pure function the `calc/` contract promises.
    """
    pool = list(candidates)
    deferred: list[dict[str, Any]] = []
    while True:
        outcome = _attempt(pool, envelope_usd=envelope_usd, floor=floor)
        if outcome is not None:
            outcome.deferred = deferred
            return outcome
        # The floors of `pool` cost more than the envelope. Defer the weakest
        # unit to a later wave and try again with the rest, rather than funding
        # everything below the threshold at which any of it works.
        weakest = min(pool, key=lambda unit: (unit["weight"], unit["key"]))
        pool = [unit for unit in pool if unit["key"] != weakest["key"]]
        deferred.append(
            {
                "unit": weakest["key"],
                "reason": (
                    f"the floors of the remaining units leave less than ${floor:,.2f} for it"
                ),
                "strategic_weight": ratio(weakest["strategic_weight"]),
            }
        )
        if not pool:
            raise CalcError(
                f"an envelope of ${envelope_usd:,.2f} cannot fund a single campaign at the "
                f"${floor:,.2f} monthly floor"
            )


def _attempt(pool: list[dict[str, Any]], *, envelope_usd: float, floor: float) -> _Solution | None:
    """One fixed-point pass. None when `pool`'s floors exceed the envelope."""
    if floor * len(pool) > envelope_usd + CENT:
        return None

    pinned: dict[str, float] = {}
    reasons: dict[str, str] = {}
    for _ in range(len(pool) + 1):
        remaining = envelope_usd - rows.total(pinned.values())
        free = [unit for unit in pool if unit["key"] not in pinned]
        if not free:
            break
        weight_total = rows.total(unit["weight"] for unit in free)
        shares = {unit["key"]: remaining * unit["weight"] / weight_total for unit in free}
        changed = False
        for unit in free:
            share = shares[unit["key"]]
            cap = unit["cap"]
            if cap is not None and share > cap:
                pinned[unit["key"]] = cap
                reasons[unit["key"]] = "cap"
                changed = True
            elif share < floor:
                pinned[unit["key"]] = floor
                reasons[unit["key"]] = "floor"
                changed = True
        if not changed:
            break
        if rows.total(pinned.values()) > envelope_usd + CENT:
            return None

    solution = _Solution()
    solution.pins = reasons

    remaining = envelope_usd - rows.total(pinned.values())
    free = [unit for unit in pool if unit["key"] not in pinned]
    weight_total = rows.total(unit["weight"] for unit in free)
    awarded: list[tuple[dict[str, Any], float]] = []
    for unit in pool:
        if unit["key"] in pinned:
            awarded.append((unit, money(pinned[unit["key"]])))
        else:
            awarded.append((unit, money(remaining * unit["weight"] / weight_total)))

    _absorb_surplus(awarded, envelope_usd=envelope_usd)
    _settle(awarded, envelope_usd=envelope_usd)
    solution.awarded = awarded
    return solution


def _absorb_surplus(awarded: list[tuple[dict[str, Any], float]], *, envelope_usd: float) -> None:
    """Spread anything the fixed point left over across the units that can take it.

    The fixed point pins a unit that fell under the floor at exactly the floor
    and then stops reconsidering it. That is right while the envelope is the
    binding constraint and wrong once the *caps* are: a unit can end up held at
    its $1,000 minimum while the envelope still has $3,000 nobody is allowed to
    spend, purely because the pass that pinned it ran before the passes that
    capped everything above it.

    A floor is a minimum, so a floored unit may absorb more; a cap is a ceiling,
    so nothing goes past it. Whatever is still unspent once every unit is at its
    cap is genuinely unspendable and is reported as `unallocated_usd`.
    """
    for _ in range(len(awarded) + 1):
        surplus = envelope_usd - rows.total(usd for _, usd in awarded)
        if surplus < CENT:
            return
        room = {
            index: (unit["cap"] - usd if unit["cap"] is not None else surplus)
            for index, (unit, usd) in enumerate(awarded)
            if unit["cap"] is None or usd + CENT < unit["cap"]
        }
        if not room:
            return
        weight_total = rows.total(awarded[index][0]["weight"] for index in room)
        moved = False
        for index, available in room.items():
            unit, usd = awarded[index]
            share = min(surplus * unit["weight"] / weight_total, available)
            if share > CENT:
                awarded[index] = (unit, money(usd + share))
                moved = True
        if not moved:
            return


def _settle(awarded: list[tuple[dict[str, Any], float]], *, envelope_usd: float) -> None:
    """Hand the rounding residual to one unit so the column sums exactly.

    It goes to the largest unit that can still take it **without breaking its
    cap** — which is not the same as "is not pinned at a cap". A unit whose
    proportional share landed just under its ceiling was never pinned, and
    handing it the stray cent would put it a fraction over the one figure a
    reader might actually check. A floor is a minimum, so exceeding one is fine.

    When no unit can take the residual the envelope simply does not divide, and
    the difference is reported as `unallocated_usd` rather than hidden inside a
    row that now contradicts its own cap.
    """
    residual = envelope_usd - rows.total(usd for _, usd in awarded)
    if abs(residual) < CENT:
        return
    eligible = [
        index
        for index, (unit, usd) in enumerate(awarded)
        if usd + residual >= 0 and (unit["cap"] is None or usd + residual <= unit["cap"])
    ]
    if not eligible:
        return
    target = max(eligible, key=lambda index: (awarded[index][1], awarded[index][0]["key"]))
    unit, usd = awarded[target]
    awarded[target] = (unit, money(usd + residual))


def _render(
    unit: dict[str, Any],
    *,
    usd: float,
    envelope: float,
    floor: float,
    pin: str | None,
) -> dict[str, Any]:
    record = unit["record"]
    cpc = rows.number(record, "avg_cpc_usd", default=-1.0)
    return {
        "campaign_ref": rows.text(record, "campaign_ref", default="-"),
        "market": rows.text(record, "market", default="-"),
        "funnel_stage": rows.text(record, "funnel_stage", default="-"),
        "pct": pct(usd / envelope * 100),
        "usd": money(usd),
        "strategic_weight": ratio(unit["strategic_weight"]),
        "efficiency": ratio(unit["efficiency"]),
        "forecast_cpa_usd": money(unit["forecast_cpa"]),
        "target_cpa_usd": money(unit["target_cpa"]),
        "avg_cpc_usd": money(cpc) if cpc > 0 else None,
        "est_conv": money(usd / unit["forecast_cpa"]),
        "est_clicks": money(usd / cpc) if cpc > 0 else None,
        # Carried onto the line, not left behind in `inputs`. Gate G3 lets a
        # budget owner raise a unit, and `allocation.whatif_v1` can only report
        # the raise as unspendable if the cap travels with the line the editor
        # is editing. Without it the what-if silently forecasts conversions
        # against impressions that are not for sale.
        "max_spend_usd": money(unit["cap"]) if unit["cap"] is not None else None,
        "floor_applied": pin == "floor",
        "cap_applied": pin == "cap",
        "below_floor": usd + CENT < floor,
    }


@formula("allocation.whatif_v1", kind="calc_allocation")
def whatif_v1(
    units: pd.DataFrame,
    *,
    constants: PlanningConstants,
    envelope_usd: float,
    tolerance_pct: float = DEFAULT_TOLERANCE_PCT,
) -> CalcDraft:
    """Re-forecast an approver's edited split and report what it broke.

    Gate G3 lets a budget owner move money. This answers the two questions that
    follow: what does the edit buy, and is the result still a plan we can sign.

    A unit edited above what it can absorb is re-forecast on the **capped**
    amount, with the difference reported as `wasted_usd`. Forecasting the
    requested figure instead would have the plan promise conversions against
    impressions that are not for sale, and gate G3 is the last place anyone
    looks before the money is committed.
    """
    if envelope_usd <= 0:
        raise CalcError(f"envelope_usd must be positive, got {envelope_usd}")
    if tolerance_pct < 0:
        raise CalcError(f"tolerance_pct must not be negative, got {tolerance_pct}")
    floor = constants.get("budget.min_monthly_per_campaign_usd").value

    records = rows.records(units, WHATIF_COLUMNS, what="units")
    allocation: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    capped: list[dict[str, Any]] = []
    below_floor: list[dict[str, Any]] = []

    for record in records:
        key = _unit_key(record)
        try:
            edited = rows.number(record, "edited_usd")
            forecast_cpa = rows.number(record, "forecast_cpa_usd")
            baseline = rows.number(record, "baseline_usd", default=0.0)
            cap = rows.number(record, "max_spend_usd", default=-1.0)
            cpc = rows.number(record, "avg_cpc_usd", default=-1.0)
        except CalcError as exc:
            excluded.append({"unit": key, "reason": str(exc)})
            continue
        if edited < 0 or forecast_cpa <= 0:
            excluded.append(
                {
                    "unit": key,
                    "reason": (
                        f"needs a non-negative edited_usd and a positive forecast_cpa_usd; "
                        f"got {edited}, {forecast_cpa}"
                    ),
                }
            )
            continue

        effective = min(edited, cap) if cap >= 0 else edited
        wasted = edited - effective
        if wasted > CENT:
            capped.append(
                {"unit": key, "requested_usd": money(edited), "absorbable_usd": money(effective)}
            )
        # Zero is not under the floor. An approver who types 0 is switching a
        # campaign off, which is an allocation decision; the floor exists to
        # catch a campaign funded too thinly to ever leave the learning period,
        # and one that is not running cannot have that problem.
        underfunded = edited > CENT and edited + CENT < floor
        if underfunded:
            below_floor.append({"unit": key, "usd": money(edited), "floor_usd": money(floor)})

        allocation.append(
            {
                "campaign_ref": rows.text(record, "campaign_ref", default="-"),
                "market": rows.text(record, "market", default="-"),
                "funnel_stage": rows.text(record, "funnel_stage", default="-"),
                "requested_usd": money(edited),
                "usd": money(effective),
                "wasted_usd": money(wasted),
                "pct": pct(edited / envelope_usd * 100),
                "baseline_usd": money(baseline),
                "delta_usd": money(edited - baseline),
                "delta_pct": pct((edited / baseline - 1) * 100) if baseline > 0 else None,
                "forecast_cpa_usd": money(forecast_cpa),
                "est_conv": money(effective / forecast_cpa),
                "est_clicks": money(effective / cpc) if cpc > 0 else None,
                "below_floor": underfunded,
                "cap_applied": wasted > CENT,
            }
        )

    if not allocation:
        raise CalcError(
            "no edited unit could be re-forecast: "
            + "; ".join(f"{row['unit']}: {row['reason']}" for row in excluded)
        )

    requested = money(rows.total(row["requested_usd"] for row in allocation))
    effective_total = money(rows.total(row["usd"] for row in allocation))
    baseline_total = money(rows.total(row["baseline_usd"] for row in allocation))
    est_conv = money(rows.total(row["est_conv"] for row in allocation))
    delta_usd = money(requested - envelope_usd)
    delta_pct = pct(delta_usd / envelope_usd * 100)
    breach = abs(delta_pct) > tolerance_pct

    return CalcDraft(
        inputs={
            "units": records,
            "envelope_usd": envelope_usd,
            "tolerance_pct": tolerance_pct,
            "min_monthly_per_campaign_usd": floor,
        },
        result={
            "allocation": allocation,
            "requested_usd": requested,
            "effective_usd": effective_total,
            "wasted_usd": money(requested - effective_total),
            "envelope_usd": money(envelope_usd),
            "delta_usd": delta_usd,
            "delta_pct": delta_pct,
            "envelope_breach": breach,
            "tolerance_pct": pct(tolerance_pct),
            "baseline_usd": baseline_total,
            "est_conv": est_conv,
            "est_cpa_usd": money(effective_total / est_conv) if est_conv > 0 else None,
            "capped": capped,
            "below_floor": below_floor,
            "unit_count": len(allocation),
        },
        summary=(
            f"Edited split totals ${requested:,.2f} against a ${envelope_usd:,.2f} envelope "
            f"({delta_pct:+.2f}%{', BREACH' if breach else ''}); "
            f"{est_conv:,.1f} conversions at "
            + (
                f"${money(effective_total / est_conv):,.2f} CPA"
                if est_conv > 0
                else "no forecast CPA"
            )
        ),
        constants_version=constants.version,
        excluded=excluded,
    )


# ---------------------------------------------------------------------------
# share_v1
# ---------------------------------------------------------------------------

#: One line of an already-approved split, plus the label to total it under.
SHARE_COLUMNS = ("group", "usd")


@formula("allocation.share_v1", kind="calc_allocation")
def share_v1(
    lines: pd.DataFrame,
    *,
    constants: PlanningConstants,
    envelope_usd: float,
) -> CalcDraft:
    """Total an approved split under whatever label stages 2.3 and 2.4 read it by.

    NOT IN THE PRD's §9.2 table, for the same reason `measurement.*` is not:
    stages 2.3 and 2.4 publish figures — a channel's share of the budget, a
    brand campaign's percentage, a campaign's daily budget — and law 14 says a
    figure in a plan comes from a registered formula and nowhere else. Summing
    the approved lines inside a node would be exactly the arithmetic
    `check_calc_isolation.py` exists to fail.

    The grouping is the caller's: 2.3.1 groups by campaign type and market,
    2.3.3 by brand versus non-brand, 2.4.2 by campaign. One formula rather than
    three because the arithmetic is identical and only the label differs, and a
    per-caller copy is three places for the weighting to drift.

    Two things this does not do. It does not re-derive anything: `usd` is what
    the budget owner approved at G3, and re-solving it here would let stage 2.3
    quietly move money a human signed for. And the percentages are of the
    **envelope**, never of the lines present — a caller that groups a subset
    gets shares that sum to less than 100, which is the honest answer to "what
    share of the budget is this".
    """
    if envelope_usd <= 0:
        raise CalcError(f"envelope_usd must be positive, got {envelope_usd}")
    days = constants.get("budget.days_per_month").value
    if days <= 0:
        raise CalcError(f"budget.days_per_month must be positive, got {days}")

    records = rows.records(lines, SHARE_COLUMNS, what="allocation lines")
    buckets: dict[str, dict[str, list[Any]]] = {}
    excluded: list[dict[str, Any]] = []

    for record in records:
        label = rows.text(record, "group").strip()
        if not label:
            excluded.append({"group": rows.text(record, "group"), "reason": "group is blank"})
            continue
        try:
            usd = rows.number(record, "usd")
            target_cpa = rows.number(record, "target_cpa_usd", default=0.0)
            est_conv = rows.number(record, "est_conv", default=0.0)
        except CalcError as exc:
            excluded.append({"group": label, "reason": str(exc)})
            continue
        if usd < 0:
            excluded.append({"group": label, "reason": f"usd must not be negative, got {usd}"})
            continue

        bucket = buckets.setdefault(
            label, {"usd": [], "weighted": [], "weight": [], "conv": [], "members": []}
        )
        bucket["usd"].append(usd)
        bucket["conv"].append(est_conv)
        member = rows.text(record, "campaign_ref")
        if member and member not in bucket["members"]:
            bucket["members"].append(member)
        # Only a line that actually carries a target contributes to the weighted
        # mean. Treating a missing target as zero would drag a group's ceiling
        # down in proportion to how much of it we failed to measure.
        if target_cpa > 0:
            bucket["weighted"].append(usd * target_cpa)
            bucket["weight"].append(usd)

    if not buckets:
        raise CalcError(
            "no allocation line could be grouped: "
            + "; ".join(f"{row['group']}: {row['reason']}" for row in excluded)
        )

    groups: list[dict[str, Any]] = []
    for label, bucket in buckets.items():
        usd_total = rows.total(bucket["usd"])
        weight = rows.total(bucket["weight"])
        groups.append(
            {
                "group": label,
                "usd": money(usd_total),
                "pct": pct(usd_total / envelope_usd * 100),
                "daily_usd": money(usd_total / days),
                "target_cpa_usd": (
                    money(rows.total(bucket["weighted"]) / weight) if weight > 0 else None
                ),
                "est_conv": money(rows.total(bucket["conv"])),
                "line_count": len(bucket["usd"]),
                "campaign_refs": sorted(bucket["members"]),
            }
        )

    groups.sort(key=lambda row: (-float(row["usd"]), str(row["group"])))
    total = rows.total(float(row["usd"]) for row in groups)

    return CalcDraft(
        inputs={"lines": records, "envelope_usd": envelope_usd, "days_per_month": days},
        result={
            "groups": groups,
            "envelope_usd": money(envelope_usd),
            "total_usd": money(total),
            "unallocated_usd": money(max(0.0, envelope_usd - total)),
            "allocated_pct": pct(total / envelope_usd * 100),
            "group_count": len(groups),
        },
        summary=(
            f"{len(groups)} group(s) over a ${money(envelope_usd):,.2f} envelope: "
            + ", ".join(f"{row['group']} {row['pct']:g}%" for row in groups[:4])
            + ("…" if len(groups) > 4 else "")
        ),
        constants_version=constants.version,
        excluded=excluded,
    )
