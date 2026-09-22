"""Default deny on claim-shaped language. The mechanism Stage 03 exists for.

Stage 03 PRD §9.3 and global law 24. Also the one most likely to be built
wrong, so it is worth being explicit about what it does and does not attempt.

**It does not decide whether a claim is true.** That is not available
deterministically, and a rule that guessed would be a blocking verdict resting
on an opinion. What *is* available deterministically is whether a sentence is
making a claim at all — `best`, `#1`, `guaranteed`, `ISO 9001`, `40% faster` —
and that is what the detector pass finds.

Two passes, both deterministic:

1. **Detect.** The regex families from `content_constants.yaml`, compiled into
   the ruleset, produce candidate claim spans.
2. **Licence.** Each candidate is checked against the ruleset's `claims_index`
   for a `ClaimRecord` that licenses it: approved, unexpired at the `now` the
   caller passed in, in scope for the target's market and language, and
   matching one of the claim's registered surface forms.

**An unlicensed candidate is blocking.** Unlicensed, unsigned, rejected and
expired all behave identically, because the difference between them is a
process state and the consequence is the same: nobody has signed for this.
Nothing is ever licensed because it reads true.

The one judgement call in here is what a surface form is compared *against*.
A detector matches the trigger — `best` — not the claim; a registered claim
reads `the best SDS software`. Comparing a bare trigger against a full claim by
trigram would score near zero and deny everything, so the licence pass tries
the matched span *and* the sentence containing it, and takes the better score.
That is why `the best SDS software` licenses a headline making exactly that
claim and does not license `the best price in Europe`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final

from agent.guardrails.matchers.lexicon import REGEX_FLAGS, language_of
from agent.guardrails.normalize import Normalized, normalize, trigram_similarity
from agent.guardrails.registry import (
    EVERYWHERE,
    GuardrailsError,
    LintContext,
    RuleBody,
    finding,
    matcher_kind,
    rule,
)
from agent.schemas.guardrails import (
    Authority,
    ClaimLicenceMatcher,
    ClaimRef,
    DetectorSpec,
    LintFinding,
    LintTarget,
    Matcher,
    Rule,
    RuleScope,
)

#: Sentence ends, for the window the licence pass compares against. Deliberately
#: crude: ad copy is short and rarely punctuated, and a full sentence tokenizer
#: would be a dependency and a source of disagreement between versions.
SENTENCE_END: Final[re.Pattern[str]] = re.compile(r"[.!?;\n]")


@dataclass(frozen=True, slots=True)
class PreparedClaims:
    """The detectors this rule names, compiled once."""

    detector_ids: tuple[str, ...]
    threshold: float


def prepare_claims(matcher: Matcher) -> PreparedClaims:
    if not isinstance(matcher, ClaimLicenceMatcher):  # pragma: no cover - registry guarantees it
        raise GuardrailsError(f"expected a ClaimLicenceMatcher, got {type(matcher).__name__}")
    return PreparedClaims(detector_ids=matcher.detector_ids, threshold=matcher.match_threshold)


def compiled_detectors(
    detectors: tuple[DetectorSpec, ...], wanted: tuple[str, ...], language: str
) -> list[tuple[DetectorSpec, re.Pattern[str]]]:
    """The named detectors that serve this target's language, compiled.

    Filtering by locale is not an optimisation: an English superlative pattern
    run over German copy produces nothing, and "produced nothing" would read
    as "made no claims".
    """
    selected = []
    for detector in detectors:
        if detector.detector_id not in wanted:
            continue
        if language_of(detector.locale) != language:
            continue
        flags = 0
        for flag in detector.flags:
            flags |= REGEX_FLAGS[flag]
        try:
            selected.append((detector, re.compile(detector.pattern, flags)))
        except re.error as exc:
            raise GuardrailsError(
                f"detector {detector.detector_id} has an invalid pattern: {exc}"
            ) from exc
    return selected


def sentence_around(text: str, start: int, end: int) -> str:
    """The clause a span sits in, for the licence comparison."""
    left = 0
    for match in SENTENCE_END.finditer(text, 0, start):
        left = match.end()
    right_match = SENTENCE_END.search(text, end)
    right = right_match.start() if right_match else len(text)
    return text[left:right].strip()


def licences(
    claim: ClaimRef,
    *,
    span_text: str,
    sentence: str,
    threshold: float,
    target: LintTarget,
    now: Any,
) -> bool:
    """Does this registered claim license this candidate span?

    Every condition is a reason to deny, and they are checked in the order a
    person would ask them. `status` first because a rejected claim is the one
    somebody most wants to sneak past.
    """
    if claim.status != "approved":
        return False
    if claim.expires_at is not None and claim.expires_at <= now:
        return False
    if claim.market_scope and target.market.casefold() not in {
        market.casefold() for market in claim.market_scope
    }:
        return False
    if claim.languages and language_of(target.language) not in {
        language_of(language) for language in claim.languages
    }:
        return False

    forms = [claim.normalized_text, *claim.surface_forms]
    for form in forms:
        folded = normalize(form, locale=target.language).text
        if not folded:
            continue
        if folded in (span_text, sentence):
            return True
        if (
            max(trigram_similarity(folded, span_text), trigram_similarity(folded, sentence))
            >= threshold
        ):
            return True
    return False


@matcher_kind("claim_licence", prepare=prepare_claims)
def evaluate_claims(
    rule_: Rule, prepared: Any, target: LintTarget | None, ctx: LintContext
) -> list[LintFinding]:
    """Detect claim-shaped language, then demand a licence for each candidate."""
    if target is None or target.text is None:
        return []

    folded: Normalized = ctx.normalized[target.ref]
    language = language_of(target.language)
    detectors = compiled_detectors(ctx.ruleset.detectors, prepared.detector_ids, language)

    if not detectors:
        # Law 31, applied to this detector: we hold no patterns for this
        # language, so we did not check. That is not a pass. It is reported as
        # blocking-and-indeterminate so a human sees a coverage gap rather than
        # a clean bill of health.
        return [
            finding(
                rule_,
                target.ref,
                message=(
                    f"No claim detectors for language {target.language!r}, so claim-shaped "
                    f"language in this copy was not checked."
                ),
                indeterminate=True,
            )
        ]

    findings: list[LintFinding] = []
    seen: set[tuple[int, int]] = set()
    for detector, pattern in detectors:
        for match in pattern.finditer(folded.text):
            if (match.start(), match.end()) in seen:
                # Two families can match the same words — `the only` is both a
                # superlative and, with a comparison after it, a comparative.
                # One span is one candidate, or the writer gets told twice.
                continue
            span_text = match.group().strip()
            sentence = sentence_around(folded.text, match.start(), match.end())
            licensed = next(
                (
                    claim
                    for claim in ctx.ruleset.claims_index
                    if licences(
                        claim,
                        span_text=span_text,
                        sentence=sentence,
                        threshold=prepared.threshold,
                        target=target,
                        now=ctx.now,
                    )
                ),
                None,
            )
            seen.add((match.start(), match.end()))
            if licensed is not None:
                continue
            findings.append(
                finding(
                    rule_,
                    target.ref,
                    message=(
                        f"{rule_.message} {span_text!r} is {detector.family} claim-shaped "
                        f"language ({detector.detector_id}) with no approved, unexpired claim "
                        f"covering it."
                    ),
                    span=folded.source_span(match.start(), match.end()),
                )
            )
    return findings


@rule("claim.licence.v1", category="claim", matcher_kind="claim_licence", severity="blocking")
def claim_licence(
    detector_ids: tuple[str, ...],
    *,
    authority: Authority,
    match_threshold: float,
    message: str = "Unlicensed claim.",
    scope: RuleScope = EVERYWHERE,
    fix_hint: str | None = (
        "Register the claim and have the legal owner sign it, or reword the copy so it "
        "makes no claim."
    ),
) -> RuleBody:
    """Claim-shaped language needs a signature behind it.

    Always blocking, by law 24. There is no severity at which "we asserted
    something nobody signed for" is a note to the writer.
    """
    return RuleBody(
        matcher=ClaimLicenceMatcher(detector_ids=detector_ids, match_threshold=match_threshold),
        message=message,
        authority=authority,
        scope=scope,
        fix_hint=fix_hint,
    )
