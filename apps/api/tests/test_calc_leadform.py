"""`leadform.field_tradeoff_v1` — lead-form length against historical junk leads (Stage 04 §11 4.3.3).

The worked example: 100 historical leads (30 won, 70 lost), 40 of them lost
to the sales-agreed disqualifier "student" — a 40% junk rate — through a
7-field landing form that asked one of the two required signals. Asking the
second screens the junk that signal accounts for; every question beyond the
history's costs `retention` of the completions, every one removed gains it.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.calc.leadform import field_tradeoff_v1
from agent.calc.registry import FORMULAS, CalcError

VERSION = "2026.09.2"


def _calc(**overrides: Any) -> Any:
    inputs: dict[str, Any] = {
        "won": 30,
        "lost_reasons": {"Student project": 40, "Price": 30},
        "disqualifiers": ["student"],
        "signals": ["job title", "company size"],
        "history_fields_n": 7,
        "history_signals_n": 1,
        "retention_per_field": 0.9,
        "constants_version": VERSION,
    }
    inputs.update(overrides)
    return field_tradeoff_v1(**inputs)


def test_it_is_a_registered_formula_whose_evidence_kind_is_calc_leadform() -> None:
    spec = FORMULAS["leadform.field_tradeoff_v1"]
    assert spec.kind == "calc_leadform"
    result = _calc()
    assert result.formula_id == "leadform.field_tradeoff_v1"
    assert result.kind == "calc_leadform"
    assert result.calc_version.endswith(VERSION)


def test_the_worked_example() -> None:
    result = _calc().result
    assert (result["leads"], result["junk"], result["junk_rate"]) == (100, 40, 0.4)
    assert result["junk_reasons"] == ["Student project"]
    assert result["options"] == [
        # contact + one signal: 100 × 0.9^(2−7) = 169.35 leads, 40% junk.
        {
            "fields_n": 2,
            "signals_asked": 1,
            "expected_leads": 169.35,
            "junk_rate": 0.4,
            "expected_qualified": 101.61,
        },
        # contact + both: 100 × 0.9^(3−7) = 152.42 leads, the junk screened.
        {
            "fields_n": 3,
            "signals_asked": 2,
            "expected_leads": 152.42,
            "junk_rate": 0.0,
            "expected_qualified": 152.42,
        },
    ]
    assert result["chosen"] == {
        "fields_n": 3,
        "signals_asked": 2,
        "expected_leads": 152.42,
        "expected_qualified": 152.42,
    }
    assert result["attribution"] == "per_unasked_signal"


def test_it_never_recommends_asking_less_than_the_history_observed() -> None:
    # The history asked both signals: dropping one was never observed.
    result = _calc(history_signals_n=2).result
    assert [option["signals_asked"] for option in result["options"]] == [2]
    assert result["attribution"] == "none"
    assert result["chosen"]["fields_n"] == 3


def test_with_no_junk_the_shortest_form_wins() -> None:
    result = _calc(disqualifiers=["competitor"]).result
    assert result["junk"] == 0
    assert result["chosen"]["fields_n"] == 2


def test_a_disqualifier_matches_whole_words_only() -> None:
    result = _calc(lost_reasons={"Students": 40, "Price": 30}).result
    assert result["junk"] == 0
    result = _calc(lost_reasons={"No budget - STUDENT": 40, "Price": 30}).result
    assert result["junk"] == 40


def test_equal_outcomes_prefer_the_shorter_form() -> None:
    result = _calc(retention_per_field=1.0, disqualifiers=[]).result
    assert result["chosen"]["fields_n"] == 2


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"won": 0, "lost_reasons": {}}, "no CRM history"),
        ({"retention_per_field": 0.0}, "retention"),
        ({"retention_per_field": 1.2}, "retention"),
        ({"history_signals_n": 3}, "history_signals_n"),
        ({"history_fields_n": 0}, "history_fields_n"),
        ({"won": -1}, "won"),
    ],
)
def test_inputs_it_cannot_compute_honestly_from_are_refused(
    overrides: dict[str, Any], match: str
) -> None:
    with pytest.raises(CalcError, match=match):
        _calc(**overrides)


def test_the_same_inputs_are_the_same_calculation() -> None:
    first = _calc()
    second = _calc(lost_reasons={"Price": 30, "Student project": 40})
    assert first.inputs_hash == second.inputs_hash
    assert first.result == second.result
    assert "3 fields" in first.summary


def test_a_multi_word_disqualifier_needs_every_word() -> None:
    result = _calc(
        disqualifiers=["no budget"], lost_reasons={"Budget approved later": 40, "Price": 30}
    ).result
    assert result["junk"] == 0
    result = _calc(disqualifiers=["no budget"], lost_reasons={"No budget this year": 40}).result
    assert result["junk"] == 40
