"""4.2.1 `headline_spread` — the code half of "the model writes candidates; code selects".

The node's database and model work is proved in
`tests/integration/test_s4p5_headlines_combinations.py`. What is proved here is
the part that decides, before a candidate may reach selection, whether it is
what it says it is: a keyword headline carries its keyword, a proof headline
stands on a licensed claim, and a DKI headline's braces are one insertion
Google will perform and agree with its `dki` flag.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from agent.nodes.creative.n4_2_1_headline_spread import (
    category_asks,
    draft_model,
    invalid_reason,
    measured_text,
)

LICENSED = uuid.UUID(int=201)
KEYWORDS = ("sds software", "sds management software")


def _reason(
    text: str,
    category: str = "benefit",
    *,
    keyword_ref: str | None = None,
    claim_ids: tuple[uuid.UUID, ...] = (),
    dki: bool = False,
) -> str | None:
    return invalid_reason(
        text,
        category,
        keyword_ref=keyword_ref,
        claim_ids=claim_ids,
        dki=dki,
        keywords=KEYWORDS,
        licensed=frozenset({LICENSED}),
    )


# ---------------------------------------------------------------------------
# DKI is measured on the default text
# ---------------------------------------------------------------------------


def test_a_dki_headline_is_measured_on_its_default_text() -> None:
    raw = "{KeyWord:Safety Data Sheet Software}"
    assert len(raw) > 30
    assert measured_text(raw) == "Safety Data Sheet Software"
    assert len(measured_text(raw)) <= 30


def test_malformed_braces_are_measured_as_written() -> None:
    assert measured_text("{KEYWORD:SDS} Tools") == "{KEYWORD:SDS} Tools"


def test_a_well_formed_dki_headline_is_valid() -> None:
    assert _reason("{KeyWord:SDS Software} Online", dki=True) is None


def test_a_dki_flag_that_disagrees_with_the_text_is_invalid() -> None:
    assert "dki" in (_reason("{KeyWord:SDS Software} Online", dki=False) or "")
    assert "dki" in (_reason("SDS Software Online", dki=True) or "")


def test_malformed_braces_are_invalid() -> None:
    reason = _reason("{KEYWORD:SDS Software} Online", dki=True)
    assert reason is not None and "keyword insertion" in reason


# ---------------------------------------------------------------------------
# categories say what the headline is
# ---------------------------------------------------------------------------


def test_a_keyword_headline_carries_the_keyword_it_names() -> None:
    assert _reason("SDS Software For Teams", "keyword", keyword_ref="sds software") is None
    assert "keyword" in (
        _reason("SDS Tools For Teams", "keyword", keyword_ref="sds software") or ""
    )
    assert "names no keyword" in (_reason("SDS Software For Teams", "keyword") or "")


def test_a_dki_keyword_headline_carries_its_keyword_in_the_default() -> None:
    assert (
        _reason("Buy {KeyWord:SDS Software}", "keyword", keyword_ref="sds software", dki=True)
        is None
    )


def test_a_keyword_that_is_not_the_ad_groups_is_invalid() -> None:
    reason = _reason("Chemical Software Online", "keyword", keyword_ref="chemical software")
    assert reason is not None and "not one of this ad group's keywords" in reason


def test_a_proof_headline_stands_on_a_licensed_claim() -> None:
    assert _reason("SDS Updates Within 24 Hours", "proof", claim_ids=(LICENSED,)) is None
    assert "licensed claim" in (_reason("SDS Updates Within 24 Hours", "proof") or "")


def test_a_claim_the_pin_does_not_license_is_invalid() -> None:
    reason = _reason("SDS Updates Within 24 Hours", "proof", claim_ids=(uuid.UUID(int=999),))
    assert reason is not None and "not licensed" in reason


# ---------------------------------------------------------------------------
# the schema the model answers in
# ---------------------------------------------------------------------------


def _item(**overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "text": "SDS Software For Teams",
        "category": "keyword",
        "keyword_ref": "sds software",
        "claim_ids": [],
        "dki": False,
    }
    item.update(overrides)
    return item


def test_the_draft_schema_takes_exactly_the_pool_size() -> None:
    model = draft_model(KEYWORDS, (LICENSED,), pool_size=3)
    assert len(model.model_validate({"candidates": [_item()] * 3}).candidates) == 3  # type: ignore[attr-defined]
    with pytest.raises(ValidationError):
        model.model_validate({"candidates": [_item()] * 2})


def test_the_draft_schema_offers_only_the_ad_groups_keywords_and_licensed_claims() -> None:
    model = draft_model(KEYWORDS, (LICENSED,), pool_size=1)
    with pytest.raises(ValidationError):
        model.model_validate({"candidates": [_item(keyword_ref="chemical software")]})
    with pytest.raises(ValidationError):
        model.model_validate({"candidates": [_item(claim_ids=[str(uuid.UUID(int=999))])]})
    assert model.model_validate({"candidates": [_item(claim_ids=[str(LICENSED)])]})


def test_with_no_keywords_or_claims_there_is_nothing_to_pick() -> None:
    model = draft_model((), (), pool_size=1)
    assert model.model_validate({"candidates": [_item(keyword_ref=None)]})
    with pytest.raises(ValidationError):
        model.model_validate({"candidates": [_item()]})
    with pytest.raises(ValidationError):
        model.model_validate({"candidates": [_item(keyword_ref=None, claim_ids=[str(LICENSED)])]})


def test_the_prompt_asks_for_a_margin_over_every_quota_that_fits_the_pool() -> None:
    quotas = {"keyword": 3, "benefit": 3, "offer": 2, "proof": 2, "objection": 2, "cta": 2}
    assert category_asks(quotas, pool_size=25) == {c: n + 1 for c, n in quotas.items()}
    assert category_asks(quotas, pool_size=16) == quotas
