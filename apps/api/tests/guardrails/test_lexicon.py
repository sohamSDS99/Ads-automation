"""Vocabulary rules (S3-P1 deliverable 5, `matchers/lexicon.py`).

The expectations here are hand-checked rather than generated. `lemma` catching
`cheapest` for a ban on `cheap` and `stem` *not* catching it is a real property
of those two libraries, and writing it down is how a library upgrade that
changes it shows up as a failing test rather than as a verdict quietly moving.
"""

from __future__ import annotations

import pytest

from agent.guardrails.matchers import lexicon
from agent.guardrails.matchers.lexicon import (
    banned_term,
    disapproval_construction,
    lemma,
    prepare_pattern,
    prepare_terms,
    required_term,
    restricted_phrase,
    review_trigger,
    stem,
)
from agent.guardrails.registry import GuardrailsError
from agent.schemas.guardrails import RegexMatcher, Rule, RuleScope, TermSetMatcher
from tests.guardrails.helpers import BRAND, GOOGLE, context, target


def run(rule: Rule, text: str, **target_kwargs: object) -> list:
    item = target(text, **target_kwargs)  # type: ignore[arg-type]
    ctx = context([item])
    kind = lexicon.evaluate_terms if rule.matcher.kind == "term_set" else lexicon.evaluate_pattern
    prepare = prepare_terms if rule.matcher.kind == "term_set" else prepare_pattern
    return kind(rule, prepare(rule.matcher), item, ctx)


# --- the two transforms are genuinely different -----------------------------


def test_lemma_and_stem_disagree_and_that_is_why_both_exist() -> None:
    assert lemma("cheapest", "en") == "cheap"
    assert stem("cheapest", "en") == "cheapest"
    assert stem("running", "en") == "run"
    assert lemma("ran", "en") == "run"


def test_an_unsupported_locale_raises_rather_than_silently_matching_nothing() -> None:
    with pytest.raises(GuardrailsError, match="no stemmer for locale"):
        stem("word", "klingon")


def test_an_unsupported_stem_locale_fails_when_the_rule_is_prepared() -> None:
    """At compile time, not at lint time: a rule nobody can evaluate must not
    reach a RuleSet."""
    with pytest.raises(GuardrailsError, match="no stemmer"):
        prepare_terms(TermSetMatcher(terms=("cheap",), match="stem", locale="klingon"))


# --- banned terms -----------------------------------------------------------


def test_a_banned_term_is_found_and_its_span_points_at_the_word() -> None:
    rule = banned_term(("cheap",), authority=BRAND, message="`cheap` is off-brand.")
    findings = run(rule, "The cheap option")
    assert len(findings) == 1
    assert findings[0].severity == "blocking"
    assert findings[0].authority_ref == "brand_book#p3"
    assert "The cheap option"[slice(*findings[0].span)] == "cheap"
    assert "'cheap'" in findings[0].message


def test_lemma_mode_catches_the_inflection_a_substring_test_would_miss() -> None:
    rule = banned_term(("cheap",), authority=BRAND, message="off-brand")
    assert run(rule, "the cheapest deal")
    assert run(rule, "cheaper than ever")


def test_matching_is_by_token_window_not_substring() -> None:
    """The rule that stops `ass` banning `class`, and `cheap` banning
    `cheapskate-free`."""
    rule = banned_term(("ass",), authority=BRAND, message="off-brand", match="exact")
    assert run(rule, "a class of products") == []
    assert run(rule, "ass") != []


def test_a_multi_word_term_matches_consecutive_tokens_only() -> None:
    rule = banned_term(("best price",), authority=BRAND, message="off-brand", match="exact")
    assert run(rule, "our best price today")
    assert run(rule, "best value and a fair price") == []


def test_a_banned_term_is_found_through_a_homoglyph_paste() -> None:
    """Cyrillic `е`. The normalizer already folded it; this proves the rule
    sees the folded text and not the raw bytes."""
    rule = banned_term(("cheap",), authority=BRAND, message="off-brand")
    assert run(rule, "a chеap option")


def test_every_occurrence_is_reported_not_just_the_first() -> None:
    rule = banned_term(("cheap",), authority=BRAND, message="off-brand")
    findings = run(rule, "cheap and cheap again")
    assert len(findings) == 2
    assert findings[0].span != findings[1].span


def test_a_clean_target_produces_nothing() -> None:
    rule = banned_term(("cheap",), authority=BRAND, message="off-brand")
    assert run(rule, "Affordable safety data sheets") == []


def test_severity_is_the_brand_s_call_for_a_lexicon_rule() -> None:
    soft = banned_term(("cheap",), authority=BRAND, message="m", severity="advisory")
    assert run(soft, "cheap")[0].severity == "advisory"


def test_a_term_that_normalises_to_nothing_is_refused() -> None:
    with pytest.raises(GuardrailsError, match="normalises to nothing"):
        prepare_terms(TermSetMatcher(terms=("   ",)))


# --- required terms ---------------------------------------------------------


def test_a_required_term_missing_is_a_finding_and_present_is_silence() -> None:
    rule = required_term(
        ("safety data sheet",), authority=BRAND, message="Name the product in full."
    )
    assert run(rule, "Manage your sheets") != []
    assert run(rule, "Manage your Safety Data Sheets") == []


def test_any_one_of_several_required_terms_satisfies_the_rule() -> None:
    rule = required_term(("sds", "safety data sheet"), authority=BRAND, message="m")
    assert run(rule, "Our SDS library") == []
    assert run(rule, "Our library") != []


def test_a_missing_required_term_has_no_span_because_nothing_is_wrong_anywhere() -> None:
    rule = required_term(("sds",), authority=BRAND, message="m")
    assert run(rule, "Our library")[0].span is None


# --- regex rules ------------------------------------------------------------


def test_a_review_trigger_warns_and_routes_rather_than_blocking() -> None:
    rule = review_trigger(r"\bcompetitor\b", authority=BRAND, message="Legal reviews this.")
    findings = run(rule, "Better than any competitor")
    assert len(findings) == 1
    assert findings[0].severity == "warning"


def test_a_restricted_phrase_and_a_learned_construction_are_always_blocking() -> None:
    policy = restricted_phrase(r"\bguaranteed cure\b", authority=GOOGLE, message="m")
    learned = disapproval_construction(r"\bmiracle\b", authority=GOOGLE, message="m")
    assert run(policy, "a guaranteed cure")[0].severity == "blocking"
    assert run(learned, "a miracle product")[0].severity == "blocking"


def test_a_pattern_span_maps_back_to_the_original_text() -> None:
    rule = review_trigger(r"\bcompetitor\b", authority=BRAND, message="m")
    text = "Beats every COMPETITOR today"
    assert text[slice(*run(rule, text)[0].span)] == "COMPETITOR"


def test_an_invalid_pattern_is_refused_when_the_rule_is_prepared() -> None:
    with pytest.raises(GuardrailsError, match="not a valid pattern"):
        prepare_pattern(RegexMatcher(pattern="(unclosed"))


def test_regex_flags_are_carried_through() -> None:
    prepared = prepare_pattern(RegexMatcher(pattern="a.b", flags=("s",)))
    assert prepared.pattern.search("a\nb")


# --- shared behaviour -------------------------------------------------------


def test_a_target_with_no_text_is_not_a_finding() -> None:
    """An image target reaching a lexicon rule must be silence, not a crash."""
    rule = banned_term(("cheap",), authority=BRAND, message="m")
    item = target(None, ref="img", image_ref="s3://logo.png")
    ctx = context([item])
    assert lexicon.evaluate_terms(rule, prepare_terms(rule.matcher), item, ctx) == []


def test_the_language_library_versions_are_pinned_into_the_compiler_version() -> None:
    """A simplemma upgrade can change what `lemma` resolves to, which changes
    verdicts. It has to move the ruleset hash."""
    assert "snowball/" in lexicon.LANGUAGE_LIB_VERSION
    assert "simplemma/" in lexicon.LANGUAGE_LIB_VERSION


def test_scope_is_carried_onto_the_rule() -> None:
    rule = banned_term(
        ("cheap",), authority=BRAND, message="m", scope=RuleScope(surfaces=("rsa_headline",))
    )
    assert rule.scope.surfaces == ("rsa_headline",)
