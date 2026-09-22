"""The claim licence mechanism (S3-P1 deliverable 5, PRD §9.3, law 24).

`test_claim_default_deny` is the phase's named acceptance test and the reason
this file is longer than the others. The property it protects is the one the
whole stage rests on: claim-shaped language is denied unless somebody signed
for it, and "somebody signed for it" means an approved, unexpired, in-scope
`ClaimRecord` — not a claim that reads true, not a claim that used to be
approved, not a claim approved for another market.

The detectors used here are the **shipped** ones from
`content_constants.yaml`, not invented fixtures. A detector family that stopped
matching real copy would otherwise pass this suite and fail in production.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import UUID

import pytest

from agent.guardrails.matchers.claims import (
    claim_licence,
    evaluate_claims,
    prepare_claims,
    sentence_around,
)
from agent.guardrails.normalize import normalized_text
from agent.guidelines.constants import load_content_constants
from agent.schemas.guardrails import ClaimRef
from tests.guardrails.helpers import LEGAL, NOW, context, target

DETECTORS = load_content_constants().detectors()
EN_DETECTORS = tuple(d.detector_id for d in DETECTORS if d.locale == "en")
THRESHOLD = load_content_constants().value("claims.match_threshold")
CLAIM_ID = UUID("00000000-0000-0000-0000-00000000c1a1")

RULE = claim_licence(EN_DETECTORS, authority=LEGAL, match_threshold=THRESHOLD)


def lint(text: str, claims: tuple[ClaimRef, ...] = (), *, now: datetime = NOW, **kw: object):
    item = target(text, **kw)  # type: ignore[arg-type]
    ctx = context([item], claims=claims, detectors=DETECTORS, now=now)
    return evaluate_claims(RULE, prepare_claims(RULE.matcher), item, ctx)


def approved_claim(
    text: str = "the best sds software",
    *,
    status: str = "approved",
    expires_at: datetime | None = datetime(2027, 1, 1, tzinfo=UTC),
    markets: tuple[str, ...] = (),
    languages: tuple[str, ...] = (),
    surface_forms: tuple[str, ...] = (),
) -> ClaimRef:
    return ClaimRef.model_validate(
        {
            "claim_id": CLAIM_ID,
            "normalized_text": text,
            "surface_forms": surface_forms,
            "status": status,
            "market_scope": markets,
            "languages": languages,
            "expires_at": expires_at,
        }
    )


# --- the acceptance test ----------------------------------------------------


def test_claim_default_deny() -> None:
    """The named S3-P1 acceptance test, in the three states the brief asks for."""
    copy = "The best SDS software"

    unlicensed = lint(copy)
    assert len(unlicensed) == 1
    assert unlicensed[0].severity == "blocking"
    assert "superlative" in unlicensed[0].message

    licensed = lint(copy, (approved_claim(),))
    assert licensed == []

    expired = lint(copy, (approved_claim(expires_at=datetime(2026, 1, 1, tzinfo=UTC)),))
    assert len(expired) == 1
    assert expired[0].severity == "blocking"


# --- every way a licence can fail to apply ----------------------------------


@pytest.mark.parametrize(
    "claim",
    [
        pytest.param(approved_claim(status="draft"), id="unsigned-draft"),
        pytest.param(approved_claim(status="rejected"), id="rejected"),
        pytest.param(approved_claim(status="expired"), id="marked-expired"),
        pytest.param(approved_claim(expires_at=NOW), id="expires-exactly-now"),
        pytest.param(approved_claim(markets=("FR",)), id="wrong-market"),
        pytest.param(approved_claim(languages=("de",)), id="wrong-language"),
        pytest.param(approved_claim("the fastest sds import"), id="different-claim"),
    ],
)
def test_a_claim_that_does_not_apply_licenses_nothing(claim: ClaimRef) -> None:
    """Unlicensed, unsigned, rejected and expired behave identically: blocking.
    The difference between them is a process state; the consequence is not."""
    assert lint("The best SDS software", (claim,)) != []


@pytest.mark.parametrize(
    "claim",
    [
        pytest.param(approved_claim(markets=("DE",)), id="right-market"),
        pytest.param(approved_claim(markets=("de",)), id="market-case-folded"),
        pytest.param(approved_claim(languages=("en",)), id="right-language"),
        pytest.param(approved_claim(expires_at=None), id="never-expires"),
        pytest.param(
            approved_claim("something else", surface_forms=("the best sds software",)),
            id="licensed-by-a-surface-form",
        ),
    ],
)
def test_a_claim_in_scope_licenses_the_span(claim: ClaimRef) -> None:
    assert lint("The best SDS software", (claim,)) == []


def test_a_claim_licenses_a_near_miss_above_the_threshold_only() -> None:
    """Trigram similarity is what lets a registered claim cover the wording a
    writer actually used. It must not stretch to a different claim."""
    claim = approved_claim("the best sds software")
    assert lint("The best SDS software.", (claim,)) == []
    assert lint("The best price in Europe", (claim,)) != []


# --- the detector families --------------------------------------------------


#: One line of real copy per shipped detector. This corpus is the guard against
#: a whole class of authoring bug: a `\b` in front of a non-word character can
#: never match, so an alternative like `#1` silently drops out of its family and
#: the pattern still compiles, still matches its other alternatives, and still
#: looks right. Every detector must prove it matches something.
CORPUS: dict[str, str] = {
    "claim.superlative.en.v1": "The best SDS software",
    "claim.comparative.en.v1": "Faster than any alternative",
    "claim.quantified.en.v1": "Cut admin time by 40%",
    "claim.guarantee.en.v1": "Guaranteed compliance",
    "claim.certification.en.v1": "ISO 9001 certified",
    "claim.endorsement.en.v1": "Trusted by 500 companies",
    "claim.superlative.de.v1": "Die beste SDS-Software",
    "claim.guarantee.de.v1": "Garantierte Konformität",
}


def test_the_corpus_covers_every_shipped_detector() -> None:
    """A detector added without a sample would never be proved to match."""
    assert set(CORPUS) == {detector.detector_id for detector in DETECTORS}


@pytest.mark.parametrize(("detector_id", "copy"), sorted(CORPUS.items()))
def test_every_shipped_detector_matches_real_copy(detector_id: str, copy: str) -> None:
    detector = next(d for d in DETECTORS if d.detector_id == detector_id)
    assert re.search(detector.pattern, normalized_text(copy, locale=detector.locale)), copy


@pytest.mark.parametrize(
    ("copy", "family"),
    [
        ("The best SDS software", "superlative"),
        ("We are #1 for compliance", "superlative"),
        ("We are # 1 in Europe", "superlative"),
        ("Faster than any alternative", "comparative"),
        ("Cut admin time by 40%", "quantified"),
        ("Up to 30 hours saved", "quantified"),
        ("Guaranteed compliance", "guarantee"),
        ("Risk-free for 30 days", "guarantee"),
        ("ISO 9001 certified", "certification"),
        ("Trusted by 500 companies", "endorsement"),
    ],
)
def test_each_shipped_detector_family_finds_real_copy(copy: str, family: str) -> None:
    findings = lint(copy)
    assert findings, copy
    assert any(family in item.message for item in findings), [i.message for i in findings]


def test_copy_that_claims_nothing_is_silent() -> None:
    assert lint("Manage your safety data sheets in one place") == []
    assert lint("Upload a document to get started") == []


def test_a_span_points_at_the_claim_the_writer_typed() -> None:
    copy = "The BEST SDS software"
    assert copy[slice(*lint(copy)[0].span)] == "BEST"


def test_one_span_matched_by_two_families_is_one_finding() -> None:
    """`the only` is superlative and, followed by a comparison, comparative.
    Telling a writer twice about one phrase is how a linter gets switched off."""
    findings = lint("The only SDS tool")
    assert len({item.span for item in findings}) == len(findings)


def test_every_finding_names_its_detector_and_its_authority() -> None:
    item = lint("The best SDS software")[0]
    assert "claim.superlative.en.v1" in item.message
    assert item.authority_ref == "sig-7f3a"
    assert item.fix_hint


# --- law 31: no detectors is not a pass -------------------------------------


def test_a_language_with_no_detectors_is_indeterminate_never_pass() -> None:
    """We hold no French patterns, so French copy was not checked. Reporting
    that as a pass would be the linter certifying something it never read."""
    findings = lint("Le meilleur logiciel SDS", language="fr")
    assert len(findings) == 1
    assert findings[0].indeterminate is True
    assert findings[0].severity == "blocking"
    assert "fr" in findings[0].message


def test_german_copy_uses_the_german_detectors() -> None:
    german = claim_licence(
        tuple(d.detector_id for d in DETECTORS if d.locale == "de"),
        authority=LEGAL,
        match_threshold=THRESHOLD,
    )
    item = target("Die beste SDS-Software", language="de")
    ctx = context([item], detectors=DETECTORS)
    findings = evaluate_claims(german, prepare_claims(german.matcher), item, ctx)
    assert findings and not findings[0].indeterminate


# --- the sentence window ----------------------------------------------------


def test_the_sentence_window_is_the_clause_around_the_span() -> None:
    text = "we ship fast. the best sds software. call us"
    assert sentence_around(text, 19, 23) == "the best sds software"
    assert sentence_around("no punctuation here", 3, 5) == "no punctuation here"


def test_a_claim_is_licensed_by_its_sentence_not_just_the_trigger_word() -> None:
    """The detector matches `best`; the registered claim is a whole phrase.
    Comparing only the trigger would deny everything ever registered."""
    claim = approved_claim("the best sds software")
    assert lint("We ship fast. The best SDS software. Call us.", (claim,)) == []


# --- shape ------------------------------------------------------------------


def test_the_rule_is_always_blocking_and_carries_its_threshold() -> None:
    assert RULE.severity == "blocking"
    assert RULE.matcher.match_threshold == pytest.approx(THRESHOLD)


def test_a_target_with_no_text_is_not_a_finding() -> None:
    item = target(None, ref="img", image_ref="s3://a.png")
    ctx = context([item], detectors=DETECTORS)
    assert evaluate_claims(RULE, prepare_claims(RULE.matcher), item, ctx) == []
