"""The H3 set hash (Stage 04 PRD §8.6 step 1, §16 contract rule 4).

`register_hash` is the exception set as the legal owner read it — what the
client echoes and the 409 compares. `decided_hash` carries their decisions and
is what `exceptions/clear` is idempotent on. The split is S3-P8's: folding the
decision into the echoed hash made any set with a rejection unsignable.

Every field the signer reads is hashed, so nothing can change what a clearance
covers between the read and the submit without the submit being refused.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from agent.creative.clearance import ExceptionView, decided_hash, register_hash

A = uuid.UUID("00000000-0000-4000-8000-00000000000a")
B = uuid.UUID("00000000-0000-4000-8000-00000000000b")
ASSET_1 = uuid.UUID("00000000-0000-4000-8000-000000000101")
ASSET_2 = uuid.UUID("00000000-0000-4000-8000-000000000102")
RESERVE = uuid.UUID("00000000-0000-4000-8000-000000000201")
EVIDENCE = uuid.UUID("00000000-0000-4000-8000-000000000301")


def _claim(**overrides: Any) -> ExceptionView:
    base: dict[str, Any] = {
        "exception_id": A,
        "kind": "new_claim",
        "subject": "the #1 SDS platform",
        "asset_ids": (ASSET_1, ASSET_2),
        "occurrences": 3,
        "evidence_ids": (EVIDENCE,),
        "proposed": {
            "claim_type": "superlative",
            "surface_forms": ["the #1 SDS platform"],
            "substantiation": None,
            "market_scope": ["US"],
            "languages": ["en"],
        },
        "fallback_asset_ids": (RESERVE,),
    }
    return ExceptionView(**{**base, **overrides})


def _image_right() -> ExceptionView:
    return ExceptionView(
        exception_id=B,
        kind="image_right",
        subject="recognisable person",
        asset_ids=(ASSET_2,),
        occurrences=1,
        evidence_ids=(),
        proposed={"basis": "VISION flag", "flag": "recognisable person"},
        fallback_asset_ids=(),
    )


def test_the_register_hash_does_not_depend_on_the_order_the_set_is_listed_in() -> None:
    assert register_hash([_claim(), _image_right()]) == register_hash([_image_right(), _claim()])


def test_the_order_of_ids_inside_an_exception_is_not_material() -> None:
    swapped = _claim(asset_ids=(ASSET_2, ASSET_1))
    assert register_hash([_claim()]) == register_hash([swapped])


def test_key_order_inside_proposed_is_not_material() -> None:
    reordered = _claim(proposed=dict(reversed(list(_claim().proposed.items()))))
    assert register_hash([_claim()]) == register_hash([reordered])


@pytest.mark.parametrize(
    "change",
    [
        {"kind": "disclaimer"},
        {"subject": "the #2 SDS platform"},
        {"asset_ids": (ASSET_1,)},
        {"occurrences": 4},
        {"evidence_ids": ()},
        {"fallback_asset_ids": ()},
        {"proposed": {**_claim().proposed, "surface_forms": ["the #1 SDS platform", "#1"]}},
        {"proposed": {**_claim().proposed, "market_scope": []}},
        {"proposed": {**_claim().proposed, "languages": ["en", "de"]}},
        {"proposed": {**_claim().proposed, "claim_type": "comparative"}},
    ],
    ids=lambda change: next(iter(change)),
)
def test_every_field_the_signer_reads_moves_the_register_hash(change: dict[str, Any]) -> None:
    """A change to what the clearance would cover must make a stale submit a 409."""
    assert register_hash([_claim()]) != register_hash([_claim(**change)])


def test_withdrawing_one_exception_moves_the_register_hash() -> None:
    assert register_hash([_claim(), _image_right()]) != register_hash([_claim()])


def test_the_register_hash_carries_no_decision_and_the_decided_hash_does() -> None:
    views = [_claim(), _image_right()]
    cleared = decided_hash(views, {A: "cleared", B: "cleared"})
    rejected = decided_hash(views, {A: "cleared", B: "rejected"})
    assert cleared != rejected
    assert register_hash(views) not in (cleared, rejected)
    assert decided_hash(list(reversed(views)), {B: "rejected", A: "cleared"}) == rejected


def test_a_decided_hash_needs_exactly_one_decision_per_exception() -> None:
    views = [_claim(), _image_right()]
    with pytest.raises(ValueError, match="no decision"):
        decided_hash(views, {A: "cleared"})
    with pytest.raises(ValueError, match="not in the set"):
        decided_hash(views, {A: "cleared", B: "cleared", uuid.uuid4(): "rejected"})


def test_an_empty_set_has_no_hash() -> None:
    with pytest.raises(ValueError, match="empty"):
        register_hash([])


def test_a_set_listing_one_exception_twice_has_no_hash() -> None:
    with pytest.raises(ValueError, match="twice"):
        register_hash([_claim(), _claim()])
