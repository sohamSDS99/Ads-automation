"""How much traffic a test needs before its result means anything (PRD §9.2).

Node 2.5.3 (`experiment_backlog`) ranks tests by ICE and states, per test, the
conversions per arm and the days to significance. Those two figures come from
here and **are never estimated by the model** — a backlog that promises a
readable result in three weeks when the real answer is seven months is worse
than no backlog, because somebody will plan around it.

Standard two-proportion z-test, two-sided:

    n = ( z(1-a/2)*sqrt(2*p*(1-p)) + z(power)*sqrt(p1(1-p1)+p2(1-p2)) )^2 / (p2-p1)^2

`alpha`, `power` and `min_mde_pct` come from `planning_constants.yaml`. The
normal quantile comes from `statistics.NormalDist`, in the standard library —
there is no scipy in this image and adding one for `inv_cdf` would be a
dependency for a function that ships with Python.
"""

from __future__ import annotations

import math
from statistics import NormalDist
from typing import Any

import pandas as pd

from agent.calc import rows
from agent.calc.registry import CalcDraft, CalcError, formula, money, pct, ratio
from agent.planning.constants import PlanningConstants

TEST_COLUMNS = ("id", "baseline_cvr_pct")

#: Arms in a test unless the row says otherwise: control plus one variant.
DEFAULT_ARMS = 2

#: A relative lift this large stops being a test and starts being a different
#: product. Refused rather than clamped, because the sample size it implies is
#: so small that the answer would look encouraging and mean nothing.
MAX_MDE_PCT = 500.0


@formula("power.sample_size_v1", kind="calc_power")
def sample_size_v1(
    tests: pd.DataFrame,
    *,
    constants: PlanningConstants,
) -> CalcDraft:
    """Conversions per arm and days to significance, per planned test."""
    alpha = constants.get("test.alpha").value
    power = constants.get("test.power").value
    min_mde = constants.get("test.min_mde_pct").value
    if not 0 < alpha < 1:
        raise CalcError(f"test.alpha must be in (0, 1), got {alpha}")
    if not 0 < power < 1:
        raise CalcError(f"test.power must be in (0, 1), got {power}")
    if min_mde <= 0:
        raise CalcError(f"test.min_mde_pct must be positive, got {min_mde}")

    normal = NormalDist()
    z_alpha = normal.inv_cdf(1 - alpha / 2)
    z_power = normal.inv_cdf(power)

    records = rows.records(tests, TEST_COLUMNS, what="tests")
    sized: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []

    for record in records:
        test_id = rows.text(record, "id", default="(unnamed)")
        try:
            baseline_pct = rows.number(record, "baseline_cvr_pct")
            mde_pct = rows.number(record, "mde_pct", default=min_mde)
            arms = rows.number(record, "arms", default=float(DEFAULT_ARMS))
            clicks_per_day = rows.number(record, "clicks_per_day", default=-1.0)
        except CalcError as exc:
            excluded.append({"id": test_id, "reason": str(exc)})
            continue

        if not 0 < baseline_pct < 100:
            excluded.append(
                {"id": test_id, "reason": f"baseline_cvr_pct {baseline_pct} outside (0, 100)"}
            )
            continue
        if not 0 < mde_pct <= MAX_MDE_PCT:
            excluded.append(
                {"id": test_id, "reason": f"mde_pct {mde_pct} outside (0, {MAX_MDE_PCT:g}]"}
            )
            continue
        if arms < 2 or not float(arms).is_integer():
            excluded.append(
                {"id": test_id, "reason": f"arms must be a whole number >= 2, got {arms}"}
            )
            continue

        p1 = baseline_pct / 100
        p2 = p1 * (1 + mde_pct / 100)
        if p2 >= 1:
            excluded.append(
                {
                    "id": test_id,
                    "reason": (
                        f"a {mde_pct:g}% lift on a {baseline_pct:g}% baseline implies a "
                        f"{p2 * 100:.1f}% conversion rate, which is not a testable hypothesis"
                    ),
                }
            )
            continue

        pooled = (p1 + p2) / 2
        numerator = (
            z_alpha * math.sqrt(2 * pooled * (1 - pooled))
            + z_power * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))
        ) ** 2
        visitors_per_arm = numerator / (p2 - p1) ** 2
        # Ceiling, not rounding: a fractional visitor short of the requirement is
        # still short of it, and a test stopped one observation early is exactly
        # the mistake the power calculation exists to prevent.
        visitors = math.ceil(visitors_per_arm)
        conversions = math.ceil(visitors_per_arm * p1)
        total_visitors = visitors * int(arms)

        days: int | None = None
        if clicks_per_day > 0:
            days = math.ceil(total_visitors / clicks_per_day)

        sized.append(
            {
                "id": test_id,
                "baseline_cvr_pct": pct(baseline_pct),
                "mde_pct": pct(mde_pct),
                "target_cvr_pct": pct(p2 * 100),
                "absolute_lift_pp": pct((p2 - p1) * 100),
                "arms": int(arms),
                "alpha": ratio(alpha),
                "power": ratio(power),
                "required_visitors_per_arm": visitors,
                "required_conv_per_arm": conversions,
                "required_visitors_total": total_visitors,
                "clicks_per_day": money(clicks_per_day) if clicks_per_day > 0 else None,
                "est_days_to_significance": days,
                # A test nobody will wait for should not be ranked as if they
                # would. The node decides what to do about it; the arithmetic
                # only says how long.
                "est_weeks_to_significance": ratio(days / 7) if days is not None else None,
            }
        )

    if not sized:
        raise CalcError(
            "no test could be sized: "
            + "; ".join(f"{row['id']}: {row['reason']}" for row in excluded)
        )

    longest = max((row["est_days_to_significance"] or 0) for row in sized)
    return CalcDraft(
        inputs={"tests": records, "alpha": alpha, "power": power, "min_mde_pct": min_mde},
        result={
            "tests": sized,
            "test_count": len(sized),
            "z_alpha": ratio(z_alpha),
            "z_power": ratio(z_power),
            "longest_days_to_significance": longest or None,
        },
        summary=(
            f"{len(sized)} test(s) sized at alpha {alpha:g} / power {power:g}; "
            f"{sized[0]['required_conv_per_arm']:,} conversions per arm for "
            f"{sized[0]['id']}" + (f"; longest run {longest} day(s)" if longest else "")
        ),
        constants_version=constants.version,
        excluded=excluded,
    )
