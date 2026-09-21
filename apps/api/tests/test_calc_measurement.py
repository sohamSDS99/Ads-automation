"""`measurement.reconciliation_v1` and `measurement.upload_window_v1`.

Neither formula is in PRD §9.2 — §11 mandates the output fields and the table
of formulas stops short of them, and `agent/calc/measurement.py` argues that
gap out. What these tests pin down is the two decisions that argument rests on:

* a tolerance is the **widest** term we can name, never a sum of allowances,
  so no combination of flags can compound one figure into another;
* an upload lag is a consequence of the cadence, so it is the same number for
  the same cadence whatever anyone believes about the sales cycle.

Every expected value below is arithmetic over the shipped constants — floor 5,
cap 40, modelled allowance 20, Google's window 90, manual turnaround 2 — and is
checkable with a calculator rather than trusted.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.calc import measurement
from agent.calc.registry import CalcError
from tests.calc_support import CONSTANTS, frame

METRICS: list[dict[str, Any]] = [
    # 50 of 200 recorded conversions cannot be tied out: a measured 25% gap.
    {
        "metric": "conversions",
        "systems": "google_ads vs ga4",
        "recorded_conversions": 200,
        "unreconciled_conversions": 50,
        "modelled": False,
    },
    # Nothing recorded is not a 0% agreement and not a 100% disagreement.
    {"metric": "cost", "recorded_conversions": 0, "unreconciled_conversions": 0},
    # Exactly the floor. The floor wins the tie, because a policy band is a
    # better thing to print than a coincidence.
    {"metric": "leads", "recorded_conversions": 200, "unreconciled_conversions": 10},
    # 8% measured, but consent-modelled conversions are the wider term.
    {
        "metric": "eu_leads",
        "recorded_conversions": 200,
        "unreconciled_conversions": 16,
        "modelled": True,
    },
    # 90% measured, capped at 40: past the cap it is a discrepancy, not a band.
    {"metric": "closed_won", "recorded_conversions": 100, "unreconciled_conversions": 90},
]


def reconcile(rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return measurement.reconciliation_v1(
        frame(rows if rows is not None else METRICS), constants=CONSTANTS
    ).result


def metric(result: dict[str, Any], name: str) -> dict[str, Any]:
    return next(row for row in result["by_metric"] if row["metric"] == name)


# ---------------------------------------------------------------------------
# reconciliation
# ---------------------------------------------------------------------------


def test_a_measured_gap_sets_the_tolerance() -> None:
    row = metric(reconcile(), "conversions")
    assert row["observed_gap_pct"] == 25.0  # 50 of 200
    assert row["tolerance_pct"] == 25.0
    assert row["binding_driver"] == measurement.DRIVER_UNRECONCILED
    assert row["capped"] is False


def test_nothing_recorded_falls_back_to_the_floor_rather_than_to_zero() -> None:
    row = metric(reconcile(), "cost")
    assert row["observed_gap_pct"] == 0.0
    assert row["tolerance_pct"] == 5.0
    assert row["binding_driver"] == measurement.DRIVER_FLOOR


def test_the_floor_wins_a_tie() -> None:
    row = metric(reconcile(), "leads")
    assert row["observed_gap_pct"] == 5.0
    assert row["tolerance_pct"] == 5.0
    assert row["binding_driver"] == measurement.DRIVER_FLOOR


def test_terms_are_selected_not_summed() -> None:
    """8% measured plus a 20% allowance is 20%, not 28%."""
    row = metric(reconcile(), "eu_leads")
    assert row["observed_gap_pct"] == 8.0
    assert row["tolerance_pct"] == 20.0
    assert row["binding_driver"] == measurement.DRIVER_MODELLED


def test_the_cap_holds_and_says_it_held() -> None:
    row = metric(reconcile(), "closed_won")
    assert row["observed_gap_pct"] == 90.0
    assert row["tolerance_pct"] == 40.0
    assert row["capped"] is True


def test_the_widest_metric_is_reported() -> None:
    result = reconcile()
    assert result["widest_tolerance_pct"] == 40.0
    assert result["widest_metric"] == "closed_won"
    assert result["metrics"] == 5
    assert result["floor_pct"] == 5.0
    assert result["cap_pct"] == 40.0


def test_more_unreconciled_than_recorded_is_refused_rather_than_clamped() -> None:
    """Clamping would hide an assembly bug behind a tolerance that looks deliberate."""
    result = reconcile(
        [
            {"metric": "ok", "recorded_conversions": 10, "unreconciled_conversions": 1},
            {"metric": "bad", "recorded_conversions": 10, "unreconciled_conversions": 11},
        ]
    )
    assert [row["metric"] for row in result["by_metric"]] == ["ok"]


def test_a_duplicate_metric_row_is_refused() -> None:
    result = reconcile(
        [
            {"metric": "conversions", "recorded_conversions": 10, "unreconciled_conversions": 1},
            {"metric": "conversions", "recorded_conversions": 90, "unreconciled_conversions": 80},
        ]
    )
    assert len(result["by_metric"]) == 1
    assert result["by_metric"][0]["tolerance_pct"] == 10.0


def test_no_usable_row_raises_rather_than_returning_an_empty_plan() -> None:
    with pytest.raises(CalcError, match="no metric row survived"):
        reconcile([{"metric": "", "recorded_conversions": 1}])


def test_a_frame_with_no_metric_column_names_the_column() -> None:
    with pytest.raises(CalcError, match="missing required column"):
        reconcile([])


def test_an_empty_but_shaped_frame_says_there_is_nothing_to_calculate() -> None:
    import pandas as pd

    with pytest.raises(CalcError, match="nothing to calculate"):
        measurement.reconciliation_v1(
            pd.DataFrame(columns=["metric", "recorded_conversions"]), constants=CONSTANTS
        )


def test_the_excluded_rows_keep_their_reason() -> None:
    draft = measurement.reconciliation_v1(
        frame(
            [
                {"metric": "ok", "recorded_conversions": 10, "unreconciled_conversions": 1},
                {"metric": "negative", "recorded_conversions": -1},
            ]
        ),
        constants=CONSTANTS,
    )
    assert draft.excluded[0]["metric"] == "negative"
    assert "negative" in draft.excluded[0]["reason"]


def test_the_constants_version_is_stamped() -> None:
    draft = measurement.reconciliation_v1(frame(METRICS), constants=CONSTANTS)
    assert draft.calc_version.endswith(CONSTANTS.version)


# ---------------------------------------------------------------------------
# the upload window
# ---------------------------------------------------------------------------


def options(history: float | None) -> dict[str, Any]:
    from agent.planning.tracking import upload_options_frame

    return measurement.upload_window_v1(
        upload_options_frame(CONSTANTS), observed_history_days=history, constants=CONSTANTS
    ).result


def option(result: dict[str, Any], method: str, cadence: str) -> dict[str, Any]:
    return next(
        row for row in result["options"] if row["method"] == method and row["cadence"] == cadence
    )


def test_the_lag_is_the_cadence_plus_the_human_turnaround() -> None:
    result = options(45)
    assert option(result, "ads_api", "daily")["lag_days"] == 1.0
    assert option(result, "ads_api", "monthly")["lag_days"] == 30.0
    # 30 days of waiting, then the 2-day manual turnaround.
    assert option(result, "manual_csv", "monthly")["lag_days"] == 32.0


def test_headroom_is_measured_against_googles_window() -> None:
    row = option(options(45), "manual_csv", "monthly")
    assert row["headroom_days"] == 58.0  # 90 - 32
    assert row["fits"] is True


def test_history_bounds_the_backfill() -> None:
    result = options(45)
    assert result["backfill_days"] == 45.0
    assert result["history_limits_backfill"] is True
    assert option(result, "ads_api", "daily")["backfill_days"] == 45.0


def test_the_window_bounds_the_backfill_when_there_is_more_history_than_that() -> None:
    result = options(200)
    assert result["backfill_days"] == 90.0
    assert result["history_limits_backfill"] is False


def test_no_history_means_the_window_alone_bounds_it() -> None:
    result = options(None)
    assert result["observed_history_days"] is None
    assert result["backfill_days"] == 90.0
    assert result["history_limits_backfill"] is False


def test_retention_is_how_long_a_click_id_must_survive_to_be_uploadable() -> None:
    # history 45: the deepest backfill is 45 days and the longest wait 32, so
    # a click id older than 45 days is of no further use.
    assert option(options(45), "manual_csv", "monthly")["retention_days"] == 45.0
    # No history: the bound is the window, and retention runs to it.
    assert option(options(None), "manual_csv", "monthly")["retention_days"] == 90.0


def test_every_shipped_cadence_fits_the_window() -> None:
    result = options(45)
    assert result["options_count"] == 8
    assert len(result["fitting_options"]) == 8


def test_a_cadence_longer_than_the_window_does_not_fit() -> None:
    result = measurement.upload_window_v1(
        frame([{"method": "manual_csv", "cadence": "quarterly", "cadence_days": 120}]),
        constants=CONSTANTS,
    ).result
    row = result["options"][0]
    assert row["headroom_days"] == -30.0
    assert row["fits"] is False
    assert result["fitting_options"] == []


def test_a_zero_cadence_is_excluded_rather_than_dividing_by_itself() -> None:
    draft = measurement.upload_window_v1(
        frame(
            [
                {"method": "ads_api", "cadence": "daily", "cadence_days": 1},
                {"method": "ads_api", "cadence": "never", "cadence_days": 0},
            ]
        ),
        constants=CONSTANTS,
    )
    assert len(draft.result["options"]) == 1
    assert draft.excluded[0]["option"] == "ads_api/never"


def test_negative_history_is_refused() -> None:
    with pytest.raises(CalcError, match="observed_history_days"):
        options(-1)


def test_no_usable_option_raises() -> None:
    with pytest.raises(CalcError, match="no upload option survived"):
        measurement.upload_window_v1(
            frame([{"method": "a", "cadence": "b", "cadence_days": 0}]), constants=CONSTANTS
        )
