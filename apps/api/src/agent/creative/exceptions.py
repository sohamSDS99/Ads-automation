"""Exception candidates — unlicensed claim-shaped spans, collected and never shipped.

Stage 04 PRD §11 4.2.2 and law 34: "An unlicensed claim-shaped span never
ships — it becomes a CreativeException". This module is the collection half:
it turns what the linter found into `exception_candidates[]{span, occurrences}`.

**It detects nothing.** Whether a span is claim-shaped, and whether any
registered claim licenses it, is Stage 03's `claim.licence.v1` rule, run by the
linter through `lint_adapter` (law 33: "never reimplement a matcher"). What is
read here is that rule's findings — identified by the pin's own rules, not by a
hard-coded rule id, so a claim rule compiled under another id still counts.

A finding without a span is not a candidate. The only one the rule emits is
"no claim detectors for this language": nobody can sign for a span nobody
found, and the copy still fails lint as blocking-and-indeterminate (law 31).

The caller decides what happens to the copy — a candidate carrying any span
found here is withheld whole and never written as an asset, draft or not.
"""

from __future__ import annotations

from collections.abc import Iterable

from agent.schemas.guardrails import ClaimLicenceMatcher, LintResult, RuleSet
from agent.schemas.search_ads import ExceptionCandidate


def unlicensed_spans(result: LintResult, *, text: str, ruleset: RuleSet) -> tuple[str, ...]:
    """The unlicensed claim-shaped spans the pin's claim-licence rules found in `text`.

    `result` must be the lint of `text` itself — the offsets are its. Text order.
    """
    claim_rules = {
        rule.rule_id for rule in ruleset.rules if isinstance(rule.matcher, ClaimLicenceMatcher)
    }
    spans: list[tuple[int, int]] = sorted(
        finding.span
        for finding in result.findings
        if finding.rule_id in claim_rules and finding.span is not None
    )
    found = (text[start:end].strip() for start, end in spans)
    return tuple(span for span in found if span)


def candidates(spans: Iterable[str]) -> list[ExceptionCandidate]:
    """One candidate per distinct span, in first-seen order, counting its occurrences.

    Case and runs of whitespace are folded — "#1" written twice is one
    exception to sign for, not two — and the first spelling seen is the one shown.
    """
    shown: dict[str, str] = {}
    counts: dict[str, int] = {}
    for span in spans:
        key = " ".join(span.split()).casefold()
        shown.setdefault(key, span)
        counts[key] = counts.get(key, 0) + 1
    return [ExceptionCandidate(span=shown[key], occurrences=count) for key, count in counts.items()]
