"""What the two systems may disagree by, and how late a conversion may arrive.

Nodes 2.5.1 (`measurement_source_of_truth`) and 2.5.2
(`offline_conversion_plan`) each carry figures — a reconciliation tolerance, an
upload lag, a backfill depth — and global law 14 says a figure in a plan
resolves to a registered formula or it does not appear. **Neither node's
numbers are in the PRD's §9.2 table**, which stops at the nine formulas the
budget and structure branches need; §11 nonetheless mandates
`reconciliation[].tolerance_pct`, `upload.lag_days` and `upload.backfill_days`
as output fields. The two halves of the spec cannot both hold with nine
formulas, so this module is the tenth and eleventh, and the rest of this
docstring is the argument for what they compute rather than what they could
have computed.

**A tolerance is the widest divergence we already know about, not a sum of
allowances.** It would have been easy to write `floor + modelled + attribution
+ counting` and produce an impressive-looking 34%. That number would be
arithmetic over four guesses. `reconciliation_v1` instead takes the *maximum*
of the terms it can name — the measured share of recorded volume that comes
from actions nobody can reconcile, the modelled-conversion allowance where
consent limits tagging, and the floor below which two systems are simply
agreeing — and reports which term bound it. One of those terms is measured
from the account; the other two are constants with a `source`. Nothing is
compounded, so no term can quietly inflate another.

**An upload lag is a consequence of the cadence, not an estimate of the sales
cycle.** The obvious computation — days from lead to close, out of the CRM — is
not available: the canonical export (Stage 01 §9.5) carries exactly one date
column, `created_at`, whose own alias list includes "close date". There is no
pair of timestamps to subtract, and subtracting a column from itself to produce
a confident-looking lag is the failure law 14 exists to prevent. What *is*
knowable is when we have committed to upload: a monthly export means a deal
closed the day after the last one waits a month, plus whatever the human
turnaround costs. `upload_window_v1` computes that for every candidate cadence
and checks it against Google's click-upload window, so the model selects a
cadence from a table rather than asserting a lag.

**Retention falls out of the same table.** PRD §13 requires the plan to name a
GCLID retention window. The shortest honest one is the longest a click id must
survive to still be uploadable — `max(lag, backfill)`, capped at the window
after which Google will not accept it anyway. Computing it rather than picking
it is what keeps data minimisation from becoming a number somebody typed.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from agent.calc import rows
from agent.calc.registry import CalcDraft, CalcError, formula, pct
from agent.planning.constants import PlanningConstants

METRIC_COLUMNS = ("metric",)
OPTION_COLUMNS = ("method", "cadence", "cadence_days")

#: Why a metric's tolerance ended up where it did. Returned per row so a
#: reader of the plan can see whether 31% is something we measured or something
#: we allowed for, which is the difference between a finding and a policy.
DRIVER_FLOOR = "floor"
DRIVER_UNRECONCILED = "unreconciled_volume"
DRIVER_MODELLED = "modelled_conversions"


@formula("measurement.reconciliation_v1", kind="calc_measurement")
def reconciliation_v1(
    metrics: pd.DataFrame,
    *,
    constants: PlanningConstants,
) -> CalcDraft:
    """How far apart two systems may be on each metric before somebody looks."""
    floor = constants.value("measurement.tolerance_floor_pct")
    cap = constants.value("measurement.tolerance_cap_pct")
    modelled_allowance = constants.value("measurement.modelled_conversion_pct")
    if not 0 <= floor <= cap:
        raise CalcError(
            f"measurement.tolerance_floor_pct ({floor}) must be between 0 and "
            f"measurement.tolerance_cap_pct ({cap})"
        )

    records = rows.records(metrics, METRIC_COLUMNS, what="reconciliation metrics")
    computed: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    seen: set[str] = set()

    for record in records:
        metric = rows.text(record, "metric")
        if not metric:
            excluded.append({"metric": "(blank)", "reason": "no metric name"})
            continue
        if metric in seen:
            # Two rows for one metric would give the plan two tolerances for
            # the same number and no rule for which wins.
            excluded.append({"metric": metric, "reason": "duplicate metric row"})
            continue
        seen.add(metric)

        try:
            recorded = rows.number(record, "recorded_conversions", default=0.0)
            unreconciled = rows.number(record, "unreconciled_conversions", default=0.0)
        except CalcError as exc:
            excluded.append({"metric": metric, "reason": str(exc)})
            continue
        if recorded < 0 or unreconciled < 0:
            excluded.append({"metric": metric, "reason": "negative conversion volume"})
            continue
        if unreconciled > recorded:
            # The caller has counted something twice. Clamping would hide the
            # assembly bug behind a 100% tolerance that looks deliberate.
            excluded.append(
                {
                    "metric": metric,
                    "reason": (
                        f"unreconciled_conversions {unreconciled} exceeds "
                        f"recorded_conversions {recorded}"
                    ),
                }
            )
            continue

        # Zero recorded volume is not a 0% gap and it is not a 100% one: it is
        # an account with nothing to reconcile yet, which the floor covers.
        observed_gap = pct(unreconciled / recorded * 100) if recorded > 0 else 0.0
        modelled = rows.flag(record, "modelled")
        candidates = {DRIVER_FLOOR: floor, DRIVER_UNRECONCILED: observed_gap}
        if modelled:
            candidates[DRIVER_MODELLED] = modelled_allowance

        driver = max(candidates, key=lambda key: (candidates[key], key == DRIVER_FLOOR))
        widest = candidates[driver]
        capped = widest > cap
        computed.append(
            {
                "metric": metric,
                "systems": rows.text(record, "systems", default="google_ads vs crm"),
                "tolerance_pct": pct(min(widest, cap)),
                "binding_driver": driver,
                "observed_gap_pct": observed_gap,
                "recorded_conversions": recorded,
                "unreconciled_conversions": unreconciled,
                "modelled": modelled,
                "capped": capped,
            }
        )

    if not computed:
        refused = "; ".join(f"{row['metric']} ({row['reason']})" for row in excluded)
        raise CalcError(f"no metric row survived: {refused}")

    widest_row = max(computed, key=lambda row: row["tolerance_pct"])
    return CalcDraft(
        inputs={
            "metrics": records,
            "tolerance_floor_pct": floor,
            "tolerance_cap_pct": cap,
            "modelled_conversion_pct": modelled_allowance,
        },
        result={
            "by_metric": computed,
            "floor_pct": pct(floor),
            "cap_pct": pct(cap),
            "modelled_allowance_pct": pct(modelled_allowance),
            "widest_tolerance_pct": widest_row["tolerance_pct"],
            "widest_metric": widest_row["metric"],
            "metrics": len(computed),
        },
        summary=(
            f"Reconciliation tolerance across {len(computed)} metric(s): floor {pct(floor)}%, "
            f"widest {widest_row['tolerance_pct']}% on {widest_row['metric']} "
            f"({widest_row['binding_driver']})"
        ),
        constants_version=constants.version,
        excluded=excluded,
    )


@formula("measurement.upload_window_v1", kind="calc_measurement")
def upload_window_v1(
    options: pd.DataFrame,
    *,
    observed_history_days: float | None = None,
    constants: PlanningConstants,
) -> CalcDraft:
    """Lag, backfill depth and retention for each offline-upload cadence."""
    window = constants.value("measurement.click_upload_window_days")
    if window <= 0:
        raise CalcError(f"measurement.click_upload_window_days must be positive, got {window}")

    history: float | None = None
    if observed_history_days is not None:
        history = float(observed_history_days)
        if history < 0:
            raise CalcError(f"observed_history_days must not be negative, got {history}")

    # Backfill is bounded by Google's window and by how much history exists.
    # No history observed means the bound is the window alone — stated rather
    # than assumed, because "we have 90 days of CRM" is a claim nobody made.
    backfill = min(window, history) if history is not None else window

    records = rows.records(options, OPTION_COLUMNS, what="upload options")
    computed: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []

    for record in records:
        method = rows.text(record, "method")
        cadence = rows.text(record, "cadence")
        label = f"{method}/{cadence}" if method and cadence else "(unnamed)"
        try:
            cadence_days = rows.number(record, "cadence_days")
            preparation_days = rows.number(record, "preparation_days", default=0.0)
        except CalcError as exc:
            excluded.append({"option": label, "reason": str(exc)})
            continue
        if cadence_days <= 0 or preparation_days < 0:
            excluded.append(
                {"option": label, "reason": f"cadence_days {cadence_days} must be positive"}
            )
            continue

        # Worst case, not average: a conversion that lands the day after an
        # export waits the whole period, and then the human turnaround.
        lag = cadence_days + preparation_days
        headroom = window - lag
        computed.append(
            {
                "method": method,
                "cadence": cadence,
                "lag_days": pct(lag),
                "backfill_days": pct(backfill),
                "headroom_days": pct(headroom),
                "retention_days": pct(min(window, max(lag, backfill))),
                "fits": headroom > 0,
            }
        )

    if not computed:
        raise CalcError(
            "no upload option survived: "
            + "; ".join(f"{row['option']} ({row['reason']})" for row in excluded)
        )

    fitting = [row for row in computed if row["fits"]]
    return CalcDraft(
        inputs={
            "options": records,
            "observed_history_days": history,
            "click_upload_window_days": window,
        },
        result={
            "options": computed,
            "click_upload_window_days": pct(window),
            "observed_history_days": None if history is None else pct(history),
            "backfill_days": pct(backfill),
            "history_limits_backfill": history is not None and history < window,
            "fitting_options": [f"{row['method']}/{row['cadence']}" for row in fitting],
            "options_count": len(computed),
        },
        summary=(
            f"Offline upload: Google accepts a click conversion for {pct(window)} days; "
            f"{len(fitting)} of {len(computed)} cadence(s) fit; "
            f"backfill bounded at {pct(backfill)} days"
        ),
        constants_version=constants.version,
        excluded=excluded,
    )
