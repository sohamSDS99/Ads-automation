"""`planning/naming.py` — the pattern language behind node 2.4.1.

A naming convention is only worth having if something enforces it, so 2.4.1
emits a `validator_regex` and PRD §12 invariant 5 requires every generated name
to pass it. That regex is compiled here from the patterns and the token
vocabulary, never written by the model: a model-authored regex is a rule nobody
can predict the failures of.

The house pattern used throughout:

    campaign   {market} | {channel} | {brand_split} | {theme}
    ad_group   {market} | {channel} | {theme} | {intent}

with `market`, `channel` and `brand_split` closed vocabularies and `theme` and
`intent` free text.
"""

from __future__ import annotations

import re

import pytest

from agent.planning import naming

PATTERNS = {
    "campaign": "{market} | {channel} | {brand_split} | {theme}",
    "ad_group": "{market} | {channel} | {theme} | {intent}",
}

TOKENS = {
    "market": ["US", "DE"],
    "channel": ["Search", "PMax", "Display"],
    "brand_split": ["Brand", "NonBrand"],
}


def validator() -> re.Pattern[str]:
    return re.compile(naming.compile_validator(PATTERNS, TOKENS))


# ---------------------------------------------------------------------------
# compiling the validator
# ---------------------------------------------------------------------------


def test_a_name_built_from_the_pattern_passes() -> None:
    assert validator().match("US | Search | NonBrand | EHS Software")
    assert validator().match("DE | PMax | Brand | Gefahrstoffe")


def test_a_token_outside_its_allowed_values_fails() -> None:
    """`FR` is a market nobody agreed to, and that is the whole point of the check."""
    assert not validator().match("FR | Search | NonBrand | EHS Software")
    assert not validator().match("US | Shopping | NonBrand | EHS Software")


def test_a_name_missing_a_segment_fails() -> None:
    assert not validator().match("US | Search | EHS Software")


def test_a_name_with_a_trailing_segment_fails() -> None:
    assert not validator().match("US | Search | NonBrand | EHS Software | extra | more")


def test_the_ad_group_pattern_is_accepted_by_the_same_regex() -> None:
    """§12 invariant 5 says *every* generated name passes it, not every campaign name."""
    assert validator().match("US | Search | EHS Software | commercial")


def test_the_separator_is_escaped_not_read_as_alternation() -> None:
    """A raw `|` in the pattern would compile to `or` and match almost anything."""
    assert not validator().match("US")
    assert not validator().match("Search")


def test_a_free_token_may_not_swallow_the_separator() -> None:
    """If `{theme}` matched `|`, a three-segment name would pass as a four-segment one."""
    assert not validator().match("US | Search | NonBrand")


def test_a_pattern_naming_an_unknown_token_is_refused() -> None:
    with pytest.raises(naming.NamingError, match="unknown token"):
        naming.compile_validator({"campaign": "{market} | {nonsense}"}, TOKENS)


def test_a_pattern_with_no_token_at_all_is_refused() -> None:
    with pytest.raises(naming.NamingError, match="no token"):
        naming.compile_validator({"campaign": "Everything"}, TOKENS)


def test_no_pattern_at_all_is_refused() -> None:
    with pytest.raises(naming.NamingError, match="at least one pattern"):
        naming.compile_validator({}, TOKENS)


def test_a_regex_metacharacter_in_an_allowed_value_is_escaped() -> None:
    """`C++` would otherwise compile to a repetition operator and raise."""
    pattern = re.compile(naming.compile_validator({"campaign": "{lang}"}, {"lang": ["C++", "F#"]}))
    assert pattern.match("C++")
    assert not pattern.match("CCC")


# ---------------------------------------------------------------------------
# rendering a name
# ---------------------------------------------------------------------------


def test_render_substitutes_every_token() -> None:
    assert (
        naming.render(
            PATTERNS["campaign"],
            {"market": "US", "channel": "Search", "brand_split": "Brand", "theme": "EHS"},
        )
        == "US | Search | Brand | EHS"
    )


def test_render_refuses_a_missing_value_rather_than_leaving_a_brace() -> None:
    with pytest.raises(naming.NamingError, match="no value for"):
        naming.render(PATTERNS["campaign"], {"market": "US"})


def test_render_collapses_whitespace_in_a_value() -> None:
    """A theme arriving as `  EHS   Software ` must not produce a name its own regex rejects."""
    name = naming.render(
        PATTERNS["campaign"],
        {"market": "US", "channel": "Search", "brand_split": "Brand", "theme": "  EHS   Software "},
    )
    assert name == "US | Search | Brand | EHS Software"
    assert validator().match(name)


def test_render_refuses_a_value_carrying_the_separator() -> None:
    """`EHS | Software` as a theme silently invents a segment."""
    with pytest.raises(naming.NamingError, match="separator"):
        naming.render(
            PATTERNS["campaign"],
            {"market": "US", "channel": "Search", "brand_split": "Brand", "theme": "EHS | Soft"},
        )


# ---------------------------------------------------------------------------
# collisions with the live account
# ---------------------------------------------------------------------------

EXISTING = ["US | Search | Brand | EHS", "Old Legacy Campaign", "us | search | brand | ehs 2"]


def test_an_identical_name_is_an_exact_collision() -> None:
    found = naming.collisions(["US | Search | Brand | EHS"], EXISTING)
    assert found[0]["existing_name"] == "US | Search | Brand | EHS"
    assert found[0]["conflict_type"] == "exact"


def test_a_collision_is_found_regardless_of_case_and_spacing() -> None:
    """Google Ads rejects a duplicate campaign name case-insensitively."""
    found = naming.collisions(["us  |  search | brand | ehs"], EXISTING)
    assert found[0]["conflict_type"] == "exact"


def test_a_name_differing_only_in_punctuation_is_a_near_collision() -> None:
    found = naming.collisions(["US-Search-Brand-EHS"], EXISTING)
    assert found[0]["conflict_type"] == "near"


def test_a_name_nobody_is_using_collides_with_nothing() -> None:
    assert naming.collisions(["US | Search | NonBrand | Chemicals"], EXISTING) == []


def test_every_collision_carries_a_resolution_that_is_a_usable_name() -> None:
    found = naming.collisions(["US | Search | Brand | EHS"], EXISTING)
    assert found[0]["resolution"] == "US | Search | Brand | EHS v2"
    assert not naming.collisions([found[0]["resolution"]], EXISTING)


def test_a_resolution_steps_past_a_suffix_that_is_also_taken() -> None:
    found = naming.collisions(
        ["US | Search | Brand | EHS 2"], [*EXISTING, "US | Search | Brand | EHS 2 v2"]
    )
    assert found[0]["resolution"] == "US | Search | Brand | EHS 2 v3"


def test_two_proposed_names_colliding_with_each_other_are_reported() -> None:
    """The live account is not the only source of a duplicate; the plan can collide with itself."""
    found = naming.collisions(["US | Search | Brand | X", "us | search | brand | x"], [])
    assert len(found) == 1
    assert found[0]["conflict_type"] == "duplicate_in_plan"
