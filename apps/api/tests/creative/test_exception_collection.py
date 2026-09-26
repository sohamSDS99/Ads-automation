"""4.6.2's exception collection, the pure half (Stage 04 PRD §11 4.6.2, Law 34).

Sightings of the same claim anywhere in the run are one exception, counted;
its licence is scoped to exactly the markets and languages it was seen in;
its type is the detector family Stage 03's own detectors assign; and the set
is ranked by occurrences and capped at `max_exceptions_per_run`, with what did
not fit reported, never dropped silently.
"""

from __future__ import annotations

import uuid

import pytest

from agent.creative import exceptions
from agent.creative.exceptions import Raise, Sighting
from tests.creative.helpers import claims_ruleset

A1 = uuid.UUID("00000000-0000-4000-8000-000000000001")
A2 = uuid.UUID("00000000-0000-4000-8000-000000000002")
R1 = uuid.UUID("00000000-0000-4000-8000-000000000003")


def test_a_claim_is_typed_by_the_detector_family_that_finds_it() -> None:
    ruleset = claims_ruleset()
    assert exceptions.claim_family("The #1 SDS platform", ruleset, language="en") == "superlative"
    assert exceptions.claim_family("40% less paperwork", ruleset, language="en") == "quantified"
    assert exceptions.claim_family("Guaranteed results", ruleset, language="en") == "guarantee"


def test_a_clause_no_detector_matches_has_no_family() -> None:
    with pytest.raises(ValueError, match="no claim detector"):
        exceptions.claim_family("SDS updates within a day", claims_ruleset(), language="en")


def test_sightings_of_one_claim_are_one_exception_scoped_to_where_it_was_seen() -> None:
    found = exceptions.claims(
        [
            Sighting(
                clause="The #1 SDS platform", markets=("US",), languages=("en",), occurrences=2
            ),
            Sighting(
                clause="the  #1 SDS platform", markets=("GB",), languages=("en",), asset_id=A1
            ),
            Sighting(
                clause="The #1 SDS platform",
                markets=("US",),
                languages=("en",),
                asset_id=A2,
                fallback=(R1,),
            ),
            Sighting(clause="Guaranteed results", markets=("US",), languages=("en",)),
        ],
        claims_ruleset(),
    )

    first, second = found
    assert first.kind == "new_claim" and first.subject == "The #1 SDS platform"
    assert first.occurrences == 4
    assert first.asset_ids == (A1, A2)
    assert first.fallback_asset_ids == (R1,)
    assert first.proposed == {
        "claim_type": "superlative",
        "surface_forms": ["The #1 SDS platform"],
        "substantiation": None,
        "market_scope": ["GB", "US"],
        "languages": ["en"],
    }
    assert (second.subject, second.occurrences) == ("Guaranteed results", 1)
    assert second.proposed["claim_type"] == "guarantee"


def _raise(kind: str, subject: str, occurrences: int) -> Raise:
    return Raise(kind=kind, subject=subject, occurrences=occurrences, proposed={})  # type: ignore[arg-type]


def test_exceptions_are_ranked_by_occurrences_and_capped() -> None:
    found = [
        _raise("image_right", "recognisable person", 2),
        _raise("new_claim", "The #1 SDS platform", 5),
        _raise("disclaimer", "Made with AI", 2),
        _raise("new_claim", "Guaranteed results", 2),
        _raise("new_claim", "Rated best", 1),
    ]

    kept, over = exceptions.rank(found, cap=3)

    assert [r.subject for r in kept] == [
        "The #1 SDS platform",
        "Guaranteed results",  # ties: new_claim, disclaimer, image_right
        "Made with AI",
    ]
    assert [r.subject for r in over] == ["recognisable person", "Rated best"]


def test_the_ranking_does_not_depend_on_the_order_things_were_found_in() -> None:
    found = [_raise("new_claim", f"claim {n}", n % 3) for n in range(1, 9)]
    assert exceptions.rank(found, cap=5) == exceptions.rank(list(reversed(found)), cap=5)
