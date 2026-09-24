"""4.2.2 `claim_bound_descriptions` — the parts that decide (PRD §11 4.2.2, law 34).

The node's database and model work is proved in
`tests/integration/test_s4p6_descriptions_variant_b.py` and, as the input 4.2.3
consumes, in `tests/integration/test_s4p5_headlines_combinations.py`. What is
proved here:

* the schema the model answers in cannot carry a description without a claim,
  or a claim the pin does not license — the model cannot cite what code did
  not offer;
* the words a description says state its claim are found in it, or it is
  dropped;
* what becomes of a written description, in the law's order: an unlicensed
  claim span withholds it whole (never an asset), then a lint failure keeps it
  draft, then a missing quote drops it;
* selection: written order, never two near-duplicates, the rest reserves.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from agent.nodes.base import NodeContractError
from agent.nodes.creative.n4_2_2_claim_bound_descriptions import (
    claim_span,
    draft_model,
    licensed_or_fail,
    pick,
    screen,
)
from tests.creative.helpers import LICENSED as HELPER_LICENSED
from tests.creative.helpers import NOW, ruleset

LICENSED = uuid.UUID(int=201)
OTHER = uuid.UUID(int=205)


def _description(**overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "text": "SDS updates within 24 hours for every sheet on file.",
        "claim_ids": [str(LICENSED)],
        "claim_text": "SDS updates within 24 hours",
    }
    item.update(overrides)
    return item


def _draft(descriptions: int = 2, paths: int = 2, **overrides: object) -> dict[str, object]:
    return {"descriptions": [_description(**overrides)] * descriptions, "paths": ["sds"] * paths}


# ---------------------------------------------------------------------------
# the schema the model answers in
# ---------------------------------------------------------------------------


def test_the_draft_takes_exactly_the_pool_and_two_paths() -> None:
    model = draft_model((LICENSED,), pool_size=2)
    assert len(model.model_validate(_draft()).descriptions) == 2  # type: ignore[attr-defined]
    with pytest.raises(ValidationError):
        model.model_validate(_draft(descriptions=1))
    with pytest.raises(ValidationError):
        model.model_validate(_draft(descriptions=3))
    with pytest.raises(ValidationError):
        model.model_validate(_draft(paths=1))
    with pytest.raises(ValidationError):
        model.model_validate(_draft(paths=3))


def test_a_description_without_a_claim_fails_the_draft_schema() -> None:
    with pytest.raises(ValidationError):
        draft_model((LICENSED,), pool_size=2).model_validate(_draft(claim_ids=[]))


def test_the_draft_offers_only_the_claims_the_pin_licenses() -> None:
    model = draft_model((LICENSED, OTHER), pool_size=2)
    assert model.model_validate(_draft(claim_ids=[str(OTHER), str(LICENSED)]))
    with pytest.raises(ValidationError):
        model.model_validate(_draft(claim_ids=[str(uuid.UUID(int=999))]))


def test_an_empty_path_or_quote_fails_the_draft_schema() -> None:
    model = draft_model((LICENSED,), pool_size=2)
    with pytest.raises(ValidationError):
        model.model_validate({"descriptions": [_description()] * 2, "paths": ["sds", ""]})
    with pytest.raises(ValidationError):
        model.model_validate(_draft(claim_text=""))


def test_with_no_licensed_claim_there_is_no_schema_to_answer() -> None:
    with pytest.raises(ValueError, match="licensed"):
        draft_model((), pool_size=2)


# ---------------------------------------------------------------------------
# claim_span — the quoted words, found in the text
# ---------------------------------------------------------------------------


def test_the_quote_is_located_in_the_text() -> None:
    text = "Every sheet current: SDS updates within 24 hours."
    assert claim_span(text, "SDS updates within 24 hours") == (21, 48)
    assert text[21:48] == "SDS updates within 24 hours"


def test_the_quote_is_located_whatever_its_case_and_outer_spaces() -> None:
    text = "SDS updates within 24 hours, every sheet."
    assert claim_span(text, "  sds UPDATES within 24 hours ") == (0, 27)


def test_a_quote_the_text_does_not_contain_has_no_span() -> None:
    assert claim_span("One library for every site.", "SDS updates within 24 hours") is None
    assert claim_span("One library for every site.", "   ") is None


def test_the_quote_is_literal_text_not_a_pattern() -> None:
    assert claim_span("Plans from 20 a month.", "20.a") is None
    assert claim_span("Save 20 (a month).", "20 (a") == (5, 10)


# ---------------------------------------------------------------------------
# screen — what becomes of one written description
# ---------------------------------------------------------------------------


def test_an_unlicensed_claim_span_withholds_the_copy_before_anything_else() -> None:
    assert screen("fail", unlicensed=("#1",), quoted=False) == "exception"
    assert screen("fail", unlicensed=("#1",), quoted=True) == "exception"


def test_a_lint_failure_stays_draft() -> None:
    assert screen("fail", unlicensed=(), quoted=True) == "failed_lint"
    assert screen("fail", unlicensed=(), quoted=False) == "failed_lint"


def test_passing_copy_that_does_not_quote_its_claim_is_dropped() -> None:
    assert screen("pass", unlicensed=(), quoted=False) == "dropped"
    assert screen("pass_with_warnings", unlicensed=(), quoted=False) == "dropped"


def test_passing_quoted_copy_is_eligible() -> None:
    assert screen("pass", unlicensed=(), quoted=True) == "eligible"
    assert screen("pass_with_warnings", unlicensed=(), quoted=True) == "eligible"


# ---------------------------------------------------------------------------
# pick — written order, no near-duplicates, the rest in reserve
# ---------------------------------------------------------------------------


def test_the_first_eligible_in_written_order_are_selected() -> None:
    written = [("d1", "One library for every site"), ("d2", "Audit-ready records, no paperwork")]
    written += [("d3", "Every sheet current, always"), ("d4", "Set up in one afternoon")]
    written += [("d5", "Works with the files you have")]
    assert pick(written, limit=4, near_duplicate=0.8) == (["d1", "d2", "d3", "d4"], ["d5"])


def test_a_near_duplicate_of_a_selection_waits_in_reserve() -> None:
    written = [
        ("d1", "Find any SDS in seconds"),
        ("d2", "Find any SDS in a second"),
        ("d3", "One library for every site"),
    ]
    assert pick(written, limit=2, near_duplicate=0.8) == (["d1", "d3"], ["d2"])


def test_fewer_eligible_than_the_limit_are_all_selected() -> None:
    assert pick([("d1", "One library for every site")], limit=4, near_duplicate=0.8) == (
        ["d1"],
        [],
    )


# ---------------------------------------------------------------------------
# a pin that licenses nothing cannot be written for
# ---------------------------------------------------------------------------


def test_a_pin_that_licenses_no_claim_fails_the_node() -> None:
    pin = ruleset()
    unlicensed = tuple(claim for claim in pin.claims_index if claim.claim_id != HELPER_LICENSED)
    with pytest.raises(NodeContractError, match="licenses no claim"):
        licensed_or_fail(pin.model_copy(update={"claims_index": unlicensed}), NOW)
    assert [claim.claim_id for claim in licensed_or_fail(pin, NOW)] == [HELPER_LICENSED]
