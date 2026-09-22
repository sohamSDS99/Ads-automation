"""Shape rules (S3-P1 deliverable 5, `matchers/assets.py`).

Two properties here are easy to get wrong and expensive to get wrong:

* lengths are measured on the **submitted** text, not the folded text. Folding
  removes zero-width characters and expands `ß`, so a headline that measured 30
  folded can be 31 on the way to Google and get rejected after passing here;
* a count rule is evaluated once over the set, not once per target, and it is
  scoped by the rule's own scope — otherwise a German rule counts the French
  headlines too.
"""

from __future__ import annotations

import pytest

from agent.guardrails.matchers.assets import (
    SET_REF,
    allowed_value,
    asset_count,
    asset_length,
    evaluate_count,
    evaluate_enum,
    evaluate_length,
    measure,
    prepare_count,
    prepare_enum,
    prepare_length,
    sentence_length,
)
from agent.guardrails.registry import GuardrailsError
from agent.schemas.guardrails import CountMatcher, EnumAllowMatcher, LengthMatcher, RuleScope
from tests.guardrails.helpers import BRAND, GOOGLE, context, target


def lint_length(rule, text: str, **kw):
    item = target(text, **kw)
    return evaluate_length(rule, prepare_length(rule.matcher), item, context([item]))


def lint_count(rule, items):
    return evaluate_count(rule, prepare_count(rule.matcher), None, context(items))


# --- measuring --------------------------------------------------------------


def test_the_three_units_measure_what_they_say() -> None:
    assert measure("Safety data sheets", "chars") == 18
    assert measure("Safety data sheets", "words") == 3
    assert measure("Safety data sheets", "graphemes") == 18


def test_a_combining_mark_is_part_of_its_letter_not_a_character_of_its_own() -> None:
    # Spelled with escapes on purpose: an editor would silently store the
    # precomposed form and the test would stop testing anything.
    decomposed = "u\u0065\u0301"  # u, e, combining acute
    assert measure(decomposed, "graphemes") == 2
    assert measure(decomposed, "chars") == 3


def test_length_is_measured_on_the_submitted_text_not_the_folded_one() -> None:
    """`ß` folds to `ss`. Google counts what arrives, so this must be 6."""
    rule = asset_length(authority=GOOGLE, message="Too long.", maximum=6)
    assert lint_length(rule, "straße") == []
    assert lint_length(rule, "strasses") != []


# --- length rules -----------------------------------------------------------


def test_a_headline_over_the_limit_is_blocking_and_states_both_numbers() -> None:
    rule = asset_length(authority=GOOGLE, message="Headlines are 30 characters.", maximum=30)
    findings = lint_length(rule, "A headline that is comfortably over the limit")
    assert len(findings) == 1
    assert findings[0].severity == "blocking"
    assert "45" in findings[0].message and "30" in findings[0].message


def test_a_headline_at_the_limit_passes() -> None:
    rule = asset_length(authority=GOOGLE, message="m", maximum=30)
    assert lint_length(rule, "x" * 30) == []
    assert lint_length(rule, "x" * 31) != []


def test_a_minimum_is_enforced_too() -> None:
    rule = asset_length(authority=GOOGLE, message="m", minimum=5)
    assert lint_length(rule, "hi") != []
    assert lint_length(rule, "hello") == []


def test_a_length_rule_that_bounds_nothing_is_refused_at_preparation() -> None:
    with pytest.raises(GuardrailsError, match="enforces nothing"):
        prepare_length(LengthMatcher())


def test_an_inverted_length_rule_is_refused() -> None:
    with pytest.raises(GuardrailsError, match="above max"):
        prepare_length(LengthMatcher(min=30, max=10))


def test_a_voice_rule_counts_words_and_is_advisory_by_default() -> None:
    rule = sentence_length(authority=BRAND, message="Keep headlines short.", maximum=5)
    findings = lint_length(rule, "one two three four five six seven")
    assert findings[0].severity == "advisory"
    assert "7 words" in findings[0].message


# --- count rules ------------------------------------------------------------


def test_too_few_headlines_is_one_finding_about_the_set() -> None:
    rule = asset_count("rsa_headline", authority=GOOGLE, message="Search needs 3.", minimum=3)
    items = [target("a", ref="h1"), target("b", ref="h2")]
    findings = lint_count(rule, items)
    assert len(findings) == 1
    assert findings[0].target_ref == SET_REF
    assert "There are 2" in findings[0].message


def test_enough_headlines_is_silence() -> None:
    rule = asset_count("rsa_headline", authority=GOOGLE, message="m", minimum=3)
    items = [target("a", ref=f"h{i}") for i in range(3)]
    assert lint_count(rule, items) == []


def test_too_many_is_caught_as_well() -> None:
    rule = asset_count("rsa_headline", authority=GOOGLE, message="m", maximum=2)
    items = [target("a", ref=f"h{i}") for i in range(3)]
    assert lint_count(rule, items) != []


def test_a_submission_holding_none_of_the_entity_still_fails_a_minimum() -> None:
    """Zero is the count a minimum most often catches; skipping the empty case
    would make an empty ad group pass."""
    rule = asset_count("rsa_headline", authority=GOOGLE, message="m", minimum=3)
    assert lint_count(rule, [target("a", ref="d1", surface="rsa_description")]) != []


def test_a_count_rule_counts_only_what_its_own_scope_selects() -> None:
    """A German rule must not be satisfied by French headlines."""
    rule = asset_count(
        "rsa_headline",
        authority=GOOGLE,
        message="m",
        minimum=3,
        scope=RuleScope(markets=("DE",)),
    )
    german = [target("a", ref=f"de{i}", market="DE") for i in range(2)]
    french = [target("a", ref=f"fr{i}", market="FR") for i in range(5)]
    assert lint_count(rule, german + french) != []


def test_a_count_rule_bounding_nothing_is_refused() -> None:
    with pytest.raises(GuardrailsError, match="enforces nothing"):
        prepare_count(CountMatcher(entity="rsa_headline"))


# --- enum rules -------------------------------------------------------------


def test_a_disallowed_value_is_a_finding_that_lists_what_was_allowed() -> None:
    rule = allowed_value("market", ("DE", "AT"), authority=GOOGLE, message="Not launched there.")
    item = target("x", market="FR")
    findings = evaluate_enum(rule, prepare_enum(rule.matcher), item, context([item]))
    assert findings and "DE, AT" in findings[0].message


def test_an_allowed_value_is_silent_and_case_is_folded() -> None:
    rule = allowed_value("market", ("de",), authority=GOOGLE, message="m")
    item = target("x", market="DE")
    assert evaluate_enum(rule, prepare_enum(rule.matcher), item, context([item])) == []


def test_only_scalar_fields_can_be_allow_listed() -> None:
    """`text` as an enum would be a lexicon rule wearing the wrong matcher, and
    it would silently never match."""
    with pytest.raises(GuardrailsError, match="not an allow-listable field"):
        prepare_enum(EnumAllowMatcher(field="text", allowed=("a",)))


# --- shared -----------------------------------------------------------------


def test_a_target_with_no_text_is_not_a_length_finding() -> None:
    rule = asset_length(authority=GOOGLE, message="m", maximum=5)
    item = target(None, ref="img", image_ref="s3://a.png")
    assert evaluate_length(rule, prepare_length(rule.matcher), item, context([item])) == []
