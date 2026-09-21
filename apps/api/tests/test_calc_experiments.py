"""`experiments.ice_rank_v1` (Stage 02 PRD §11, node 2.5.3).

Every expected value below is checkable with a calculator:

    ICE = impact x confidence / effort
    cost = required_visitors_total x avg_cpc_usd

The anchor case is three tests against a $5,000 reserve, sized so that the
funding line falls *between* the second and the third — the interesting
property is not that a ranking exists but that the reserve runs out partway
down it and the tests below the line say so.
"""

from __future__ import annotations

import pytest

from agent.calc import experiments
from agent.calc.registry import CalcError
from tests.calc_support import CONSTANTS, frame

#: ICE: A = 5x4/2 = 10.0, B = 4x4/4 = 4.0, C = 3x3/3 = 3.0
#: cost: A = 10,000 x 0.20 = $2,000, B = 20,000 x 0.15 = $3,000,
#:       C = 8,000 x 0.50 = $4,000
CANDIDATES = [
    {
        "id": "A",
        "impact_1_5": 5,
        "confidence_1_5": 4,
        "effort_1_5": 2,
        "required_visitors_total": 10_000,
        "avg_cpc_usd": 0.20,
        "est_days_to_significance": 30,
        "launch_wave": 1,
    },
    {
        "id": "B",
        "impact_1_5": 4,
        "confidence_1_5": 4,
        "effort_1_5": 4,
        "required_visitors_total": 20_000,
        "avg_cpc_usd": 0.15,
        "est_days_to_significance": 45,
        "launch_wave": 1,
    },
    {
        "id": "C",
        "impact_1_5": 3,
        "confidence_1_5": 3,
        "effort_1_5": 3,
        "required_visitors_total": 8_000,
        "avg_cpc_usd": 0.50,
        "est_days_to_significance": 60,
        "launch_wave": 2,
    },
]


def rank(rows: list[dict] | None = None, *, pool: float = 5_000.0) -> dict:
    return experiments.ice_rank_v1(
        frame(rows if rows is not None else CANDIDATES),
        constants=CONSTANTS,
        reserve_pool_usd=pool,
    ).result


def row_for(result: dict, identifier: str) -> dict:
    """One scored row by id. Not named `test_` — pytest would collect it."""
    return next(row for row in result["tests"] if row["id"] == identifier)


def test_ice_is_impact_times_confidence_over_effort() -> None:
    result = rank()
    assert row_for(result, "A")["ice_score"] == 10.0
    assert row_for(result, "B")["ice_score"] == 4.0
    assert row_for(result, "C")["ice_score"] == 3.0


def test_rank_is_descending_ice() -> None:
    result = rank()
    assert [row["id"] for row in result["tests"]] == ["A", "B", "C"]
    assert [row["rank"] for row in result["tests"]] == [1, 2, 3]


def test_cost_is_the_traffic_the_power_calculation_demands() -> None:
    result = rank()
    assert row_for(result, "A")["cost_usd"] == 2_000.00
    assert row_for(result, "B")["cost_usd"] == 3_000.00
    assert row_for(result, "C")["cost_usd"] == 4_000.00


def test_the_reserve_funds_down_the_ranking_until_it_runs_out() -> None:
    result = rank(pool=5_000.0)
    # A ($2,000) + B ($3,000) exactly exhausts the pool; C cannot be funded.
    assert row_for(result, "A")["funded"] is True
    assert row_for(result, "B")["funded"] is True
    assert row_for(result, "C")["funded"] is False
    assert result["funded_count"] == 2
    assert result["reserve_committed_usd"] == 5_000.00
    assert result["reserve_unspent_usd"] == 0.0


def test_an_unfunded_test_says_what_stopped_it_and_reserves_nothing() -> None:
    unfunded = row_for(rank(pool=5_000.0), "C")
    assert unfunded["reserve_usd"] == 0.0
    assert "$4,000.00" in unfunded["blocked_by"]
    assert "$0.00 left" in unfunded["blocked_by"]


def test_a_bigger_reserve_funds_the_whole_backlog() -> None:
    result = rank(pool=20_000.0)
    assert all(row["funded"] for row in result["tests"])
    assert result["reserve_committed_usd"] == 9_000.00
    assert result["reserve_unspent_usd"] == 11_000.00


def test_an_empty_reserve_ranks_everything_and_funds_nothing() -> None:
    result = rank(pool=0.0)
    assert [row["rank"] for row in result["tests"]] == [1, 2, 3]
    assert not any(row["funded"] for row in result["tests"])
    assert result["reserve_committed_usd"] == 0.0


def test_a_test_past_the_patience_horizon_is_never_funded() -> None:
    slow = [dict(CANDIDATES[0], id="SLOW", est_days_to_significance=400)]
    row = row_for(rank(slow, pool=1_000_000.0), "SLOW")
    assert row["funded"] is False
    assert "180-day horizon" in row["blocked_by"]


def test_a_test_with_no_forecast_cannot_be_costed_and_says_so() -> None:
    blind = [dict(CANDIDATES[0], id="BLIND", avg_cpc_usd=0, required_visitors_total=0)]
    row = row_for(rank(blind), "BLIND")
    assert row["cost_usd"] is None
    assert row["funded"] is False
    assert "cost of running it is unknown" in row["blocked_by"]


def test_a_test_with_no_click_forecast_has_no_read_out_date() -> None:
    undated = [dict(CANDIDATES[0], id="UNDATED", est_days_to_significance=0)]
    row = row_for(rank(undated), "UNDATED")
    assert row["funded"] is False
    assert "no date this test would read out" in row["blocked_by"]


def test_ratings_outside_one_to_five_are_clamped_and_reported() -> None:
    wild = [dict(CANDIDATES[0], id="WILD", impact_1_5=9, effort_1_5=0)]
    result = rank(wild)
    row = row_for(result, "WILD")
    # 9 clamps to 5, 0 clamps to 1: 5 x 4 / 1 = 20.
    assert row["impact_1_5"] == 5.0
    assert row["effort_1_5"] == 1.0
    assert row["ice_score"] == 20.0
    assert result["clamped_ratings"] == ["WILD"]


def test_effort_can_never_divide_by_zero() -> None:
    zero_effort = [dict(CANDIDATES[0], id="Z", effort_1_5=0)]
    assert row_for(rank(zero_effort), "Z")["ice_score"] == 20.0


def test_a_tie_on_score_and_cost_breaks_on_id_so_the_order_is_stable() -> None:
    twins = [
        dict(CANDIDATES[0], id="zebra"),
        dict(CANDIDATES[0], id="alpha"),
    ]
    assert [row["id"] for row in rank(twins)["tests"]] == ["alpha", "zebra"]


def test_a_row_missing_a_rating_is_excluded_by_name_not_dropped_silently() -> None:
    partial = [CANDIDATES[0], {"id": "NORATING", "impact_1_5": 3, "confidence_1_5": 3}]
    result = experiments.ice_rank_v1(frame(partial), constants=CONSTANTS, reserve_pool_usd=5_000.0)
    assert [row["id"] for row in result.result["tests"]] == ["A"]
    assert result.excluded[0]["id"] == "NORATING"
    assert "effort_1_5" in result.excluded[0]["reason"]


def test_no_scorable_candidate_is_an_error_naming_every_reason() -> None:
    with pytest.raises(CalcError, match="NORATING"):
        rank([{"id": "NORATING", "impact_1_5": 3, "confidence_1_5": 3, "effort_1_5": None}])


def test_a_missing_rating_column_fails_before_the_loop_and_names_the_column() -> None:
    # Distinct from the case above: no row carries `effort_1_5` *at all*, which
    # `rows.records` refuses up front. The message should name the column, not
    # list every row as an exclusion.
    with pytest.raises(CalcError, match="missing required column"):
        rank([{"id": "NORATING", "impact_1_5": 3, "confidence_1_5": 3}])


def test_a_negative_reserve_is_refused_rather_than_floored() -> None:
    with pytest.raises(CalcError, match="must not be negative"):
        rank(pool=-1.0)


def test_the_constants_version_is_stamped_on_the_result() -> None:
    result = experiments.ice_rank_v1(frame(CANDIDATES), constants=CONSTANTS, reserve_pool_usd=1.0)
    assert CONSTANTS.version in result.calc_version
    assert result.formula_id == "experiments.ice_rank_v1"
    assert result.kind == "calc_power"


def test_identical_inputs_hash_identically() -> None:
    first = experiments.ice_rank_v1(
        frame(CANDIDATES), constants=CONSTANTS, reserve_pool_usd=5_000.0
    )
    second = experiments.ice_rank_v1(
        frame(CANDIDATES), constants=CONSTANTS, reserve_pool_usd=5_000.0
    )
    assert first.inputs_hash == second.inputs_hash
    assert first.result == second.result


def test_the_summary_names_the_pool_and_the_top_test() -> None:
    result = experiments.ice_rank_v1(
        frame(CANDIDATES), constants=CONSTANTS, reserve_pool_usd=5_000.0
    )
    assert "2 funded" in result.summary
    assert "$5,000.00 reserve" in result.summary
    assert "Top: A" in result.summary
