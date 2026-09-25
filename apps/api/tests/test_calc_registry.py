"""The `@formula` contract (PRD §9.1).

The decorator is not decoration. It is the thing that makes a `CalcResult`
un-forgeable: a formula body cannot claim an id it was not registered under and
cannot forget to stamp the constants version it ran against. These tests are
what hold that.
"""

from __future__ import annotations

import math

import pytest

from agent.calc import FORMULAS
from agent.calc.registry import (
    CALC_KINDS,
    FORMULA_ID,
    CalcDraft,
    CalcError,
    FormulaRegistrationError,
    calc_version,
    canonical,
    formula,
    inputs_hash,
    money,
    pct,
    ratio,
    require_positive,
)

#: Every formula PRD §9.2 names. Not a count — the ids, so a rename is caught.
PRD_FORMULAS = frozenset(
    {
        "economics.max_cpa_v1",
        "economics.payback_v1",
        "forecast.traffic_v1",
        "scenarios.envelope_v1",
        "allocation.split_v1",
        "allocation.whatif_v1",
        "structure.volume_check_v1",
        "structure.grouping_v1",
        "power.sample_size_v1",
    }
)

#: Formulas §9.2 does not name, each with the §11 output field that forced it.
#: Listed rather than folded into the set above so the deviation stays visible:
#: §9.2's table serves the budget and structure branches and stops there, while
#: §11 still mandates a tolerance, a lag and a backfill depth on stage 2.5.
#: `agent/calc/measurement.py` argues the trade in full.
BEYOND_THE_PRD = {
    "measurement.reconciliation_v1": "2.5.1 reconciliation[].tolerance_pct",
    "measurement.upload_window_v1": "2.5.2 upload.lag_days / upload.backfill_days",
    "allocation.share_v1": (
        "2.3.1 slate[].est_share_of_budget_pct, 2.3.3 brand_campaign.budget_pct, "
        "2.4.2 campaigns[].daily_budget_usd"
    ),
    "structure.overlap_v1": "2.3.2 overlap[].overlap_pct",
    "experiments.ice_rank_v1": (
        "2.5.3 tests[].ice_score, tests[].rank, tests[].reserve_usd — the only three "
        "figures on the backlog that `power.sample_size_v1` does not produce"
    ),
}

#: The Stage 04 PRD names its own (§9.2 ratio coverage, §9.3 the estimate,
#: §23.1 item 8; §9.4 the saliency crop window and video 2 the shot plan).
STAGE04_FORMULAS = frozenset(
    {
        "media.cost_estimate_v1",
        "media.ratio_plan_v1",
        "media.crop_window_v1",
        "media.shot_plan_v1",
    }
)

EXPECTED_FORMULAS = PRD_FORMULAS | set(BEYOND_THE_PRD) | STAGE04_FORMULAS


def test_every_formula_in_the_prd_is_registered() -> None:
    assert set(FORMULAS) >= PRD_FORMULAS


def test_nothing_is_registered_that_no_prd_field_asked_for() -> None:
    """A new formula is a deliberate act, not something that appears."""
    assert set(FORMULAS) == EXPECTED_FORMULAS


def test_every_registered_formula_declares_a_known_evidence_kind() -> None:
    for spec in FORMULAS.values():
        assert spec.kind in CALC_KINDS, f"{spec.formula_id} writes an unknown evidence kind"


def test_every_registered_formula_has_a_one_line_description() -> None:
    for spec in FORMULAS.values():
        assert spec.doc, f"{spec.formula_id} has no docstring"


def test_the_decorator_stamps_identity_the_body_never_sets() -> None:
    @formula("testing.stamp_v1", kind="calc_economics")
    def stamped() -> CalcDraft:
        """A formula that knows nothing about its own id."""
        return CalcDraft(
            inputs={"a": 1}, result={"b": 2}, summary="two", constants_version="2026.09.1"
        )

    result = stamped()
    assert result.formula_id == "testing.stamp_v1"
    assert result.kind == "calc_economics"
    assert result.calc_version == "calc/1.0+constants/2026.09.1"
    assert result.inputs_hash == inputs_hash({"a": 1})
    assert result.payload() == {
        "formula_id": "testing.stamp_v1",
        "calc_version": "calc/1.0+constants/2026.09.1",
        "inputs_hash": result.inputs_hash,
        "inputs": {"a": 1},
        "result": {"b": 2},
    }
    FORMULAS.pop("testing.stamp_v1")


def test_a_formula_reading_no_constants_says_so() -> None:
    @formula("testing.noconstants_v1", kind="calc_power")
    def bare() -> CalcDraft:
        """No constants at all."""
        return CalcDraft(inputs={}, result={}, summary="")

    assert bare().calc_version == "calc/1.0+constants/none"
    FORMULAS.pop("testing.noconstants_v1")


def test_a_malformed_id_is_refused() -> None:
    for bad in ("nonamespace", "Economics.MaxCpa_v1", "economics.max_cpa", "economics.v1", ""):
        with pytest.raises(FormulaRegistrationError, match="is not a formula id"):
            formula(bad, kind="calc_economics")
        assert not FORMULA_ID.match(bad)


def test_an_unknown_evidence_kind_is_refused() -> None:
    with pytest.raises(FormulaRegistrationError, match="is not one of"):
        formula("testing.kind_v1", kind="calc_nonsense")


def test_two_formulas_cannot_share_an_id() -> None:
    @formula("testing.dupe_v1", kind="calc_economics")
    def first() -> CalcDraft:
        """First."""
        return CalcDraft(inputs={}, result={}, summary="")

    with pytest.raises(FormulaRegistrationError, match="already registered by"):

        @formula("testing.dupe_v1", kind="calc_economics")
        def second() -> CalcDraft:
            """Second."""
            return CalcDraft(inputs={}, result={}, summary="")

    assert FORMULAS["testing.dupe_v1"].origin.endswith("first")
    FORMULAS.pop("testing.dupe_v1")


def test_re_registering_the_same_function_is_not_a_collision() -> None:
    """A module reload must not look like two formulas fighting over one id."""

    def build() -> None:
        @formula("testing.reload_v1", kind="calc_economics")
        def same_name() -> CalcDraft:
            """Reloaded."""
            return CalcDraft(inputs={}, result={}, summary="")

    build()
    build()
    assert "testing.reload_v1" in FORMULAS
    FORMULAS.pop("testing.reload_v1")


# --- the hash ---------------------------------------------------------------


def test_key_order_does_not_change_the_hash() -> None:
    assert inputs_hash({"a": 1, "b": 2}) == inputs_hash({"b": 2, "a": 1})


def test_an_integral_float_hashes_as_an_int() -> None:
    """JSON round-trips turn 1 into 1.0; that is not a different input."""
    assert inputs_hash({"a": 1}) == inputs_hash({"a": 1.0})


def test_a_different_value_changes_the_hash() -> None:
    assert inputs_hash({"a": 1}) != inputs_hash({"a": 2})


def test_a_missing_key_changes_the_hash() -> None:
    assert inputs_hash({"a": 1}) != inputs_hash({"a": 1, "b": None})


def test_list_order_does_change_the_hash() -> None:
    """Rows are ordered data. Two orderings are two inputs."""
    assert inputs_hash([1, 2]) != inputs_hash([2, 1])


def test_nested_structures_are_canonicalised_all_the_way_down() -> None:
    assert canonical({"b": [{"z": 1.0, "a": 2}], "a": 3}) == {"a": 3, "b": [{"a": 2, "z": 1}]}


def test_a_non_finite_value_cannot_be_persisted() -> None:
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(CalcError, match="not a finite number"):
            canonical({"value": bad})


def test_unhashable_types_are_stringified_rather_than_dropped() -> None:
    import uuid
    from datetime import date

    identifier = uuid.uuid4()
    rendered = canonical({"when": date(2026, 9, 21), "id": identifier})
    assert rendered == {"when": "2026-09-21", "id": str(identifier)}


def test_booleans_survive_canonicalisation() -> None:
    """`True` is not `1` here: a flag and a count are different inputs."""
    assert canonical({"flag": True}) is not None
    assert canonical({"flag": True})["flag"] is True
    assert inputs_hash({"flag": True}) != inputs_hash({"flag": 1})


# --- rounding and guards ----------------------------------------------------


def test_rounding_happens_at_the_boundary_to_fixed_places() -> None:
    assert money(1.005001) == 1.01
    assert money(1234.5678) == 1234.57
    assert pct(33.333333) == 33.33
    assert ratio(1 / 3) == 0.3333


def test_rounding_refuses_a_non_finite_number() -> None:
    with pytest.raises(CalcError, match="not a finite number"):
        money(math.inf)
    with pytest.raises(CalcError, match="not a finite number"):
        pct(math.nan)


def test_require_positive_names_the_input_it_refused() -> None:
    assert require_positive("acv", 5) == 5.0
    for bad in (0, -1, math.nan, math.inf):
        with pytest.raises(CalcError, match="acv must be a positive finite number"):
            require_positive("acv", bad)


def test_calc_version_carries_both_halves() -> None:
    assert calc_version("2026.09.1") == "calc/1.0+constants/2026.09.1"
    assert calc_version(None).endswith("constants/none")
