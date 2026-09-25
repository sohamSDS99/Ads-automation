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

**A candidate is the clause, not the trigger** (S4-P14). A detector matches
`#1`; the claim is what the clause around it says, and Stage 03's licence pass
compares a registered claim against exactly that clause
(`matchers.claims.sentence_around`). Collecting bare triggers made H3 unusable
both ways: a claim registered as `the #1 SDS platform` scores below the
threshold against `the #1 SDS platform for teams`, and one registered as `#1`
licenses every `#1` anybody ever writes. Two triggers in one clause are one
claim; the same trigger in two clauses is two.

The caller decides what happens to the copy — a candidate carrying any span
found here is withheld whole and never written as an asset, draft or not.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from agent.guardrails.matchers.claims import compiled_detectors, sentence_around
from agent.guardrails.normalize import normalize
from agent.schemas.guardrails import ClaimLicenceMatcher, LintResult, RuleSet
from agent.schemas.search_ads import ExceptionCandidate

ExceptionKind = Literal["new_claim", "disclaimer", "image_right"]

#: Tie order when two exceptions occur equally often: a claim a legal owner
#: licenses outranks a package-scoped disclaimer, which outranks an image right.
KIND_ORDER: Mapping[str, int] = {"new_claim": 0, "disclaimer": 1, "image_right": 2}


def unlicensed_spans(result: LintResult, *, text: str, ruleset: RuleSet) -> tuple[str, ...]:
    """The unlicensed claims the pin's claim-licence rules found in `text` —
    each the clause its trigger sits in, once per clause, in text order.

    `result` must be the lint of `text` itself — the offsets are its.
    """
    claim_rules = {
        rule.rule_id for rule in ruleset.rules if isinstance(rule.matcher, ClaimLicenceMatcher)
    }
    spans: list[tuple[int, int]] = sorted(
        finding.span
        for finding in result.findings
        if finding.rule_id in claim_rules and finding.span is not None
    )
    found = (sentence_around(text, start, end) for start, end in spans)
    return tuple(dict.fromkeys(clause for clause in found if clause))


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


# ---------------------------------------------------------------------------
# 4.6.2 — from candidates to the exceptions H3 is asked about
# ---------------------------------------------------------------------------


def fold(text: str) -> str:
    """The key two spellings of one subject share: case and runs of spaces folded."""
    return " ".join(text.split()).casefold()


@dataclass(frozen=True, slots=True)
class Sighting:
    """One place an unlicensed claim was found: a withheld candidate (no asset)
    or an asset that carries it, with the assets that stand in if it is refused."""

    clause: str
    #: Where the copy was aimed. A withheld offer candidate is collected across
    #: campaigns, so a sighting may stand for several.
    markets: tuple[str, ...]
    languages: tuple[str, ...]
    occurrences: int = 1
    asset_id: uuid.UUID | None = None
    fallback: tuple[uuid.UUID, ...] = ()


@dataclass(frozen=True, slots=True)
class Raise:
    """One exception 4.6.2 would raise — a `creative_exception` row, unwritten."""

    kind: ExceptionKind
    subject: str
    occurrences: int
    proposed: Mapping[str, Any]
    asset_ids: tuple[uuid.UUID, ...] = ()
    fallback_asset_ids: tuple[uuid.UUID, ...] = ()
    evidence_ids: tuple[uuid.UUID, ...] = field(default_factory=tuple)


def claim_family(clause: str, ruleset: RuleSet, *, language: str) -> str:
    """The claim type of a clause: the family of the first of the pin's own
    detectors that matches it, in the order the licence pass runs them.

    Not a second matcher (Law 33): the patterns are Stage 03's, compiled by
    Stage 03's function; this only asks which of them the clause trips.
    """
    wanted = tuple(
        dict.fromkeys(
            detector_id
            for rule in ruleset.rules
            if isinstance(rule.matcher, ClaimLicenceMatcher)
            for detector_id in rule.matcher.detector_ids
        )
    )
    folded = normalize(clause, locale=language).text
    for detector, pattern in compiled_detectors(ruleset.detectors, wanted, language):
        if pattern.search(folded):
            return str(detector.family)
    raise ValueError(f"no claim detector of the pin matches {clause!r}")


def claims(sightings: Iterable[Sighting], ruleset: RuleSet) -> list[Raise]:
    """Every sighting of one claim as one `new_claim`, counted, in first-seen order.

    The licence it asks for is scoped to exactly the markets and languages the
    claim was seen in — the narrowest thing that makes the copy it came from
    pass, never "unrestricted" by default.
    """
    grouped: dict[str, list[Sighting]] = {}
    for sighting in sightings:
        grouped.setdefault(fold(sighting.clause), []).append(sighting)
    raised: list[Raise] = []
    for group in grouped.values():
        first = group[0]
        subject = " ".join(first.clause.split())
        raised.append(
            Raise(
                kind="new_claim",
                subject=subject,
                occurrences=sum(s.occurrences for s in group),
                asset_ids=_unique(s.asset_id for s in group if s.asset_id is not None),
                fallback_asset_ids=_unique(f for s in group for f in s.fallback),
                proposed={
                    "claim_type": claim_family(
                        subject, ruleset, language=(first.languages or ("en",))[0]
                    ),
                    "surface_forms": [subject],
                    "substantiation": None,
                    "market_scope": sorted({m for s in group for m in s.markets}),
                    "languages": sorted({lang for s in group for lang in s.languages}),
                },
            )
        )
    return raised


def rank(raised: Sequence[Raise], *, cap: int) -> tuple[list[Raise], list[Raise]]:
    """Most occurrences first, then by kind, then by subject; the first `cap` and the rest."""
    ordered = sorted(
        raised, key=lambda r: (-r.occurrences, KIND_ORDER[r.kind], fold(r.subject), r.subject)
    )
    return ordered[:cap], ordered[cap:]


def _unique(values: Iterable[uuid.UUID]) -> tuple[uuid.UUID, ...]:
    return tuple(dict.fromkeys(values))
