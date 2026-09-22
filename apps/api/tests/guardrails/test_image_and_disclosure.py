"""Image thresholds and AI disclosure (S3-P1 deliverable 5).

The image rules consume metrics measured elsewhere — S3-P5 builds the OCR pass.
What is provable now is the half that has to be reproducible: a threshold
applied to a number, and what happens when the number is absent.

Law 31 is the point of both files: a blocking check whose input never arrived
reports `indeterminate`, never `pass`. Treating "we did not measure" as "it
measured fine" is how an unreviewed image reaches a live campaign wearing a
green tick.
"""

from __future__ import annotations

import pytest
from tests.guardrails.helpers import GOOGLE, INTERNAL, context, target

from agent.guardrails.matchers.disclosure import (
    ai_generated,
    evaluate_disclosure,
    prepare_disclosure,
)
from agent.guardrails.matchers.image import (
    evaluate_ratio,
    logo_match,
    prepare_ratio,
    text_coverage,
)
from agent.guardrails.registry import GuardrailsError
from agent.schemas.guardrails import DisclosureMatcher, RatioMatcher

COVERAGE = text_coverage(authority=GOOGLE, maximum=0.20)


def lint_image(rule, **target_kwargs):
    item = target(None, ref="img", image_ref="s3://creative.png", **target_kwargs)
    return evaluate_ratio(rule, prepare_ratio(rule.matcher), item, context([item]))


def lint_disclosure(rule, text: str, *, generated: bool = True):
    item = target(text, generated_by_ai=generated)
    return evaluate_disclosure(rule, prepare_disclosure(rule.matcher), item, context([item]))


# --- image ------------------------------------------------------------------


def test_an_image_under_the_coverage_threshold_passes() -> None:
    assert lint_image(COVERAGE, image_metrics={"text_coverage_ratio": 0.12}) == []


def test_an_image_over_the_threshold_is_blocking_and_states_the_ratio() -> None:
    findings = lint_image(COVERAGE, image_metrics={"text_coverage_ratio": 0.34})
    assert len(findings) == 1
    assert findings[0].severity == "blocking"
    assert "0.340" in findings[0].message and "0.200" in findings[0].message


def test_the_threshold_itself_passes() -> None:
    assert lint_image(COVERAGE, image_metrics={"text_coverage_ratio": 0.20}) == []


def test_a_missing_metric_is_indeterminate_never_pass() -> None:
    absent = lint_image(COVERAGE)
    unmeasured = lint_image(COVERAGE, image_metrics={"logo_match_score": 0.9})
    for findings in (absent, unmeasured):
        assert len(findings) == 1
        assert findings[0].indeterminate is True
        assert findings[0].severity == "blocking"
        assert "text_coverage_ratio" in findings[0].message


def test_a_minimum_bound_works_for_logo_matching() -> None:
    rule = logo_match(authority=INTERNAL, minimum=0.62)
    assert lint_image(rule, image_metrics={"logo_match_score": 0.7}) == []
    assert lint_image(rule, image_metrics={"logo_match_score": 0.4}) != []


def test_a_text_target_is_not_an_image_finding() -> None:
    item = target("a headline")
    assert evaluate_ratio(COVERAGE, prepare_ratio(COVERAGE.matcher), item, context([item])) == []


def test_a_ratio_rule_bounding_nothing_is_refused() -> None:
    with pytest.raises(GuardrailsError, match="enforces nothing"):
        prepare_ratio(RatioMatcher(metric="text_coverage_ratio"))


def test_the_same_metrics_always_give_the_same_verdict() -> None:
    metrics = {"text_coverage_ratio": 0.34}
    first = lint_image(COVERAGE, image_metrics=dict(metrics))
    second = lint_image(COVERAGE, image_metrics=dict(metrics))
    assert first == second


# --- disclosure -------------------------------------------------------------


DISCLOSURE = ai_generated(authority=GOOGLE, required_text="AI-generated")


def test_generated_copy_without_its_disclosure_is_blocking() -> None:
    findings = lint_disclosure(DISCLOSURE, "The best way to manage sheets")
    assert len(findings) == 1
    assert findings[0].severity == "blocking"
    assert "ai-generated" in findings[0].message


def test_generated_copy_carrying_the_disclosure_is_silent() -> None:
    assert lint_disclosure(DISCLOSURE, "AI-generated: manage your sheets") == []


def test_copy_a_person_wrote_is_never_nagged() -> None:
    """A disclosure the writer would have to add falsely is worse than none."""
    assert lint_disclosure(DISCLOSURE, "Manage your sheets", generated=False) == []


def test_placement_is_enforced_where_a_policy_asks_for_it() -> None:
    prefixed = ai_generated(authority=GOOGLE, required_text="AI-generated", placement="prefix")
    assert lint_disclosure(prefixed, "AI-generated: manage sheets") == []
    assert lint_disclosure(prefixed, "Manage sheets. AI-generated") != []

    suffixed = ai_generated(authority=GOOGLE, required_text="AI-generated", placement="suffix")
    assert lint_disclosure(suffixed, "Manage sheets. AI-generated") == []
    assert lint_disclosure(suffixed, "AI-generated: manage sheets") != []


def test_the_disclosure_is_matched_on_folded_text_so_casing_never_defeats_it() -> None:
    assert lint_disclosure(DISCLOSURE, "ai-GENERATED — manage sheets") == []


def test_an_empty_required_text_is_refused() -> None:
    with pytest.raises(GuardrailsError, match="enforces nothing"):
        prepare_disclosure(DisclosureMatcher(required_text="  "))


def test_a_finding_says_where_the_disclosure_belongs() -> None:
    prefixed = ai_generated(authority=GOOGLE, required_text="AI-generated", placement="prefix")
    assert "at the start" in lint_disclosure(prefixed, "Manage sheets")[0].message
