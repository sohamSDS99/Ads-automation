"""`creative/exceptions.py` — an unlicensed claim-shaped span becomes an exception candidate.

Stage 04 PRD §11 4.2.2 and law 34. Stage 04 does not detect claims: Stage 03's
`claim.licence.v1` rule does, through the linter, and this module only reads
what that rule found. So every test lints through the real `PinnedLinter`
against the real rule and the **shipped** detectors (`claims_ruleset`), never
a finding built by hand that happens to look like one.
"""

from __future__ import annotations

import uuid

from agent.creative import exceptions
from agent.creative.lint_adapter import PinnedLinter
from agent.guardrails.matchers.lexicon import banned_term
from agent.schemas.guardrails import (
    ClaimLicenceMatcher,
    ClaimRef,
    DetectorSpec,
    LintResult,
    LintTarget,
)
from agent.schemas.search_ads import ExceptionCandidate
from tests.creative.helpers import LEGAL, NOW, claims_ruleset

BEST = uuid.UUID(int=204)


def _lint(text: str, *, language: str = "en", **pin: object) -> tuple[LintResult, PinnedLinter]:
    linter = PinnedLinter(ruleset=claims_ruleset(**pin), offer_records=())  # type: ignore[arg-type]
    result = linter.lint_candidate(
        LintTarget(
            ref="d1",
            surface="rsa_description",
            campaign_type="search",
            market="*",
            language=language,
            text=text,
            generated_by_ai=True,
        ),
        now=NOW,
    )
    return result, linter


def _spans(text: str, **kwargs: object) -> tuple[str, ...]:
    result, linter = _lint(text, **kwargs)  # type: ignore[arg-type]
    return exceptions.unlicensed_spans(result, text=text, ruleset=linter.ruleset)


# ---------------------------------------------------------------------------
# unlicensed_spans — what the pin's claim-licence rule found, and only that
# ---------------------------------------------------------------------------


def test_an_unlicensed_claim_shaped_span_is_found() -> None:
    text = "The #1 SDS software for every site."
    result, _ = _lint(text)
    assert result.verdict == "fail"
    assert _spans(text) == ("#1",)


def test_an_expired_claim_licenses_nothing() -> None:
    # `rated best by users` is registered, approved and expired at the pin.
    assert _spans("Rated best by users.") == ("best",)


def test_a_licensed_claim_is_not_an_exception() -> None:
    best = ClaimRef(claim_id=BEST, normalized_text="the best sds software", status="approved")
    text = "The best SDS software."
    result, _ = _lint(text, claims=(best,))
    assert result.verdict == "pass", result.findings
    assert _spans(text, claims=(best,)) == ()


def test_copy_with_no_claim_shaped_language_has_no_span() -> None:
    assert _spans("SDS updates within 24 hours, on every sheet.") == ()


def test_another_rules_span_is_not_a_claim_span() -> None:
    banned = banned_term(("guaranteed compliance",), authority=LEGAL, message="Never promise it.")
    text = "Guaranteed compliance for every sheet."
    result, _ = _lint(text, rules=(banned,))
    assert {finding.rule_id for finding in result.findings if finding.span} == {
        banned.rule_id,
        "claim.licence.v1",
    }
    assert _spans(text, rules=(banned,)) == ("Guaranteed",)


def test_an_unchecked_language_has_no_span_and_still_fails() -> None:
    # No German detectors ship: the rule reports blocking-and-indeterminate
    # (law 31). That is a coverage gap, not a claim anybody can sign for.
    text = "Die beste SDS-Software."
    result, _ = _lint(text, language="de")
    assert result.verdict == "fail"
    assert all(finding.span is None for finding in result.findings)
    assert _spans(text, language="de") == ()


def test_every_claim_licence_rule_counts_and_spans_come_in_text_order() -> None:
    # Two claim rules under two ids. The linter orders findings by rule id, so
    # `claim.a.v1` (the quantified family) reports "40%" before
    # `claim.licence.v1` reports "Guaranteed" — the text says "Guaranteed" first.
    pin = claims_ruleset()
    (licence,) = pin.rules
    first = licence.model_copy(
        update={
            "rule_id": "claim.a.v1",
            "matcher": ClaimLicenceMatcher(
                detector_ids=("claim.quantified.en.v1",),
                match_threshold=licence.matcher.match_threshold,  # type: ignore[union-attr]
            ),
        }
    )
    second = licence.model_copy(
        update={
            "matcher": ClaimLicenceMatcher(
                detector_ids=("claim.guarantee.en.v1",),
                match_threshold=licence.matcher.match_threshold,  # type: ignore[union-attr]
            )
        }
    )
    pin = pin.model_copy(update={"rules": (first, second)})
    text = "Guaranteed: 40% less paperwork."
    result = PinnedLinter(ruleset=pin, offer_records=()).lint_candidate(
        LintTarget(
            ref="d1",
            surface="rsa_description",
            campaign_type="search",
            market="*",
            language="en",
            text=text,
        ),
        now=NOW,
    )
    assert [finding.rule_id for finding in result.findings] == ["claim.a.v1", "claim.licence.v1"]
    assert exceptions.unlicensed_spans(result, text=text, ruleset=pin) == ("Guaranteed", "40%")


def test_a_detector_that_matches_the_spaces_around_a_word_yields_the_word() -> None:
    # Detectors are data (`content_constants.yaml`), so a pattern may well
    # match the whitespace around its trigger. The span to sign for is the words.
    only = DetectorSpec(detector_id="claim.only.en.v1", family="superlative", pattern=r"\s+only\s+")
    pin = claims_ruleset()
    (licence,) = pin.rules
    pin = pin.model_copy(
        update={
            "detectors": (only,),
            "rules": (
                licence.model_copy(
                    update={
                        "matcher": ClaimLicenceMatcher(
                            detector_ids=(only.detector_id,), match_threshold=0.88
                        )
                    }
                ),
            ),
        }
    )
    text = "The only SDS tool."
    result = PinnedLinter(ruleset=pin, offer_records=()).lint_candidate(
        LintTarget(
            ref="d1",
            surface="rsa_description",
            campaign_type="search",
            market="*",
            language="en",
            text=text,
        ),
        now=NOW,
    )
    (finding,) = result.findings
    assert finding.span is not None and text[finding.span[0] : finding.span[1]] == " only "
    assert exceptions.unlicensed_spans(result, text=text, ruleset=pin) == ("only",)


# ---------------------------------------------------------------------------
# candidates — one per distinct span, with its occurrences
# ---------------------------------------------------------------------------


def test_each_distinct_span_is_one_candidate_counting_its_occurrences() -> None:
    found = exceptions.candidates(["#1", "Guaranteed", "#1", "guaranteed", "Number  one"])
    assert found == [
        ExceptionCandidate(span="#1", occurrences=2),
        ExceptionCandidate(span="Guaranteed", occurrences=2),
        ExceptionCandidate(span="Number  one", occurrences=1),
    ]


def test_case_and_spacing_are_folded_and_the_first_spelling_is_kept() -> None:
    assert exceptions.candidates(["number  one", "Number one", "NUMBER ONE"]) == [
        ExceptionCandidate(span="number  one", occurrences=3)
    ]


def test_no_spans_no_candidates() -> None:
    assert exceptions.candidates([]) == []
