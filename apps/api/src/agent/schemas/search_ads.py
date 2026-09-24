"""Search-ad contracts — nodes 4.2.1, 4.2.2 and 4.2.3 (Stage 04 PRD §11 4.2, §12.2).

* `HeadlineSpreadOutput` — 4.2.1: per ad group, the pool the model wrote
  (`copy.headline_pool_size` candidates, each linted at creation), the ≤ 15
  that `select.headlines_v1` chose, the reserves and the quota report.
* `ClaimBoundDescriptionsOutput` — 4.2.2's output, defined here so 4.2.3 reads
  it through the real schema before 4.2.2 exists (S4-P6 builds the node; S4-P5
  feeds fixtures through this). **`claim_ids ⊆ licensed(pin)` is a
  validator** — it needs the pin's licensed ids in the validation context, and
  refuses to validate without them, so the check cannot be skipped by omission.
* `CombinationCoherenceOutput` — 4.2.3: per ad, every pair it could serve, the
  swaps of its one repair round, and pins only for `order_dependent` pairs.

Dynamic keyword insertion is part of the contract because the PRD makes it one:
"DKI `{KeyWord:default}` validated on the **default text's** length". A
headline's `default_text` is what Google shows when no keyword fits, and it is
what the linter and every pair check measure.
"""

from __future__ import annotations

import re
from itertools import combinations
from typing import Final, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

SEARCH_ADS_SCHEMA_VERSION: Literal["1.0"] = "1.0"

Category = Literal["keyword", "benefit", "offer", "proof", "objection", "cta"]
LintVerdict = Literal["pass", "pass_with_warnings", "fail"]
Outcome = Literal["selected", "reserve", "dropped", "failed_lint"]
Variant = Literal["A", "B"]
PairKind = Literal["HH", "HD", "DD"]
PairFlag = Literal[
    "duplicate",
    "near_duplicate",
    "offer_conflict",
    "cta_collision",
    "claim_conflict",
    "keyword_stuffing",
]
PairLabel = Literal["reads_well", "redundant", "contradictory", "order_dependent"]
PinPosition = Literal["H1", "H2", "H3", "D1", "D2"]

PASSING: Final = ("pass", "pass_with_warnings")
#: The labels that make a pair bad enough to repair (§11 4.2.3).
BAD_LABELS: Final = ("redundant", "contradictory")


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------
# dynamic keyword insertion
# ---------------------------------------------------------------------------

#: The capitalisations Google documents for keyword insertion, and only those
#: (support.google.com/google-ads/answer/2454041, read 2026-09-25). Anything
#: else in braces is not an insertion Google will perform.
DKI_TOKENS: Final = ("keyword", "Keyword", "KeyWord", "KEYWord", "KeyWORD")

_BRACED = re.compile(r"\{([^{}]*)\}")


class DkiError(ValueError):
    """A headline's braces are not one well-formed keyword insertion."""


def _insertion(text: str) -> tuple[re.Match[str], str] | None:
    tokens = list(_BRACED.finditer(text))
    if text.count("{") != len(tokens) or text.count("}") != len(tokens):
        raise DkiError(f"unbalanced braces in {text!r}")
    if not tokens:
        return None
    for token in tokens:
        name, colon, _default = token[1].partition(":")
        if not colon or name not in DKI_TOKENS:
            raise DkiError(
                f"{{{token[1]}}} is not a keyword insertion Google documents; "
                f"write {{KeyWord:default text}} ({', '.join(DKI_TOKENS)})"
            )
    if len(tokens) > 1:
        raise DkiError(f"a headline takes one keyword insertion; this one has {len(tokens)}")
    default = tokens[0][1].partition(":")[2]
    if not default.strip():
        raise DkiError("the keyword insertion has an empty default text")
    return tokens[0], default


def dki_default(text: str) -> str | None:
    """The default text of the headline's keyword insertion, or None without one."""
    found = _insertion(text)
    return None if found is None else found[1]


def render_default(text: str) -> str:
    """The headline as shown when no keyword fits: the insertion replaced by its default."""
    found = _insertion(text)
    if found is None:
        return text
    token, default = found
    return text[: token.start()] + default + text[token.end() :]


# ---------------------------------------------------------------------------
# 4.2.1 headline_spread
# ---------------------------------------------------------------------------


class LintRef(_Frozen):
    """The asset's `LintResult`, by verdict. The full result is on the asset row."""

    verdict: LintVerdict
    ruleset_version: str = Field(min_length=1)
    rule_ids: list[str] = Field(default_factory=list)


class HeadlineCandidate(_Frozen):
    asset_id: UUID
    #: As written — with the `{KeyWord:default}` token, if any.
    text: str = Field(min_length=1)
    #: What the linter and the pair checks measured: the text with the
    #: insertion rendered as its default (the text itself when there is none,
    #: or when its braces are malformed and it was dropped for it).
    default_text: str
    category: Category
    keyword_ref: str | None = None
    claim_ids: list[UUID] = Field(default_factory=list)
    dki: bool
    lint: LintRef
    outcome: Outcome
    #: Why a candidate was dropped, or which headline a reserve duplicates.
    reason: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> HeadlineCandidate:
        try:
            rendered: str | None = render_default(self.text)
            has_insertion = dki_default(self.text) is not None
        except DkiError:
            rendered, has_insertion = None, False
        expected = self.text if rendered is None else rendered
        if self.default_text != expected:
            raise ValueError(
                f"default_text must be the text rendered with its default ({expected!r}), "
                f"not {self.default_text!r}"
            )
        shippable = self.outcome in ("selected", "reserve")
        if shippable and rendered is None:
            raise ValueError("a selected or reserve candidate cannot carry malformed braces")
        if shippable and self.dki != has_insertion:
            raise ValueError(
                f"dki is {self.dki} but the text {'has' if has_insertion else 'has no'} "
                f"keyword insertion"
            )
        if shippable and self.lint.verdict not in PASSING:
            raise ValueError("a selected or reserve candidate must have passed lint")
        if self.outcome == "failed_lint" and self.lint.verdict in PASSING:
            raise ValueError("a candidate that passed lint is not failed_lint")
        if self.outcome == "dropped" and not self.reason:
            raise ValueError("a dropped candidate says why")
        return self


class QuotaLine(_Frozen):
    category: str
    required: int = Field(ge=0)
    selected: int = Field(ge=0)
    available: int = Field(ge=0)


class QuotaReport(_Frozen):
    lines: list[QuotaLine]
    limit: int = Field(ge=1)
    selected: int = Field(ge=0)
    met: bool


class HeadlineGroup(_Frozen):
    campaign_ref: str = Field(min_length=1)
    ad_group_ref: str = Field(min_length=1)
    campaign_type: str = Field(min_length=1)
    market: str = Field(min_length=1)
    language: str = Field(min_length=1)
    variant: Variant = "A"
    candidates: list[HeadlineCandidate] = Field(min_length=1)
    #: In selection order.
    selected: list[UUID]
    #: Best replacement first.
    reserve: list[UUID]
    quota_report: QuotaReport
    selection: Literal["select.headlines_v1"] = "select.headlines_v1"
    similarity_metric: Literal["copy.trigram_v1"] = "copy.trigram_v1"
    near_duplicate_trigram: float = Field(gt=0.0, le=1.0)

    @model_validator(mode="after")
    def _outcomes_match_lists(self) -> HeadlineGroup:
        ids = [candidate.asset_id for candidate in self.candidates]
        if len(set(ids)) != len(ids):
            raise ValueError("a candidate appears twice")
        for outcome, listed in (("selected", self.selected), ("reserve", self.reserve)):
            marked = {c.asset_id for c in self.candidates if c.outcome == outcome}
            if set(listed) != marked or len(set(listed)) != len(listed):
                raise ValueError(f"`{outcome}` must list exactly the {outcome} candidates, once")
        if len(self.selected) != self.quota_report.selected:
            raise ValueError("the quota report counts a different selection")
        if len(self.selected) > self.quota_report.limit:
            raise ValueError("more headlines selected than the spec allows")
        return self


class HeadlineSpreadOutput(_Frozen):
    schema_version: Literal["1.0"] = SEARCH_ADS_SCHEMA_VERSION
    #: One per Search ad group in scope; empty when the slate has none.
    ad_groups: list[HeadlineGroup] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 4.2.2 claim_bound_descriptions — the output schema only (S4-P6 builds the node)
# ---------------------------------------------------------------------------


class DescriptionItem(_Frozen):
    asset_id: UUID
    text: str = Field(min_length=1)
    #: Law 34: at least one, every one licensed at the pin.
    claim_ids: list[UUID] = Field(min_length=1)
    #: `[start, end)` of `text` that states the claims.
    claim_span: tuple[int, int]
    lint: LintRef

    @field_validator("claim_ids")
    @classmethod
    def _licensed(cls, value: list[UUID], info: ValidationInfo) -> list[UUID]:
        licensed = (info.context or {}).get("licensed_claim_ids")
        if licensed is None:
            raise ValueError(
                "validate with context={'licensed_claim_ids': ...}: claim_ids ⊆ licensed(pin) "
                "is a validator, and there is no pin to check against"
            )
        unlicensed = [str(claim) for claim in value if claim not in licensed]
        if unlicensed:
            raise ValueError(f"claim(s) {', '.join(unlicensed)} are not licensed at the pin")
        return value

    @model_validator(mode="after")
    def _span_and_lint(self) -> DescriptionItem:
        start, end = self.claim_span
        if not 0 <= start < end <= len(self.text):
            raise ValueError(f"claim_span {self.claim_span} is not inside the text")
        if self.lint.verdict not in PASSING:
            raise ValueError("a description or reserve must have passed lint")
        return self


class ExceptionCandidate(_Frozen):
    """An unlicensed claim-shaped span, never shipped (§11 4.2.2, law 34)."""

    span: str = Field(min_length=1)
    occurrences: int = Field(ge=1)


class DescriptionGroup(_Frozen):
    campaign_ref: str = Field(min_length=1)
    ad_group_ref: str = Field(min_length=1)
    variant: Variant = "A"
    descriptions: list[DescriptionItem] = Field(min_length=1, max_length=4)
    paths: tuple[str | None, str | None] = (None, None)
    reserve: list[DescriptionItem] = Field(default_factory=list)
    exception_candidates: list[ExceptionCandidate] = Field(default_factory=list)


class ClaimBoundDescriptionsOutput(_Frozen):
    schema_version: Literal["1.0"] = SEARCH_ADS_SCHEMA_VERSION
    ad_groups: list[DescriptionGroup] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 4.2.3 combination_coherence
# ---------------------------------------------------------------------------


class Pair(_Frozen):
    a: UUID
    b: UUID
    kind: PairKind
    #: `combinatorics.pair_flags_v1`. Blocking.
    flags: list[PairFlag] = Field(default_factory=list)
    label: PairLabel


class Swap(_Frozen):
    out: UUID
    in_from_reserve: UUID
    why: str = Field(min_length=1)


class Pin(_Frozen):
    asset_id: UUID
    position: PinPosition
    why: str = Field(min_length=1)


class PairReport(_Frozen):
    """One ad's combinations after its one repair round (§12.2 `pair_report`)."""

    campaign_ref: str = Field(min_length=1)
    ad_group_ref: str = Field(min_length=1)
    variant: Variant = "A"
    #: The final combination: what the ad now carries.
    headlines: list[UUID] = Field(min_length=1)
    descriptions: list[UUID] = Field(default_factory=list)
    pairs: list[Pair]
    swaps: list[Swap] = Field(default_factory=list)
    pins: list[Pin] = Field(default_factory=list)
    repair_rounds: int = Field(ge=0, le=1)
    #: Pairs still flagged or labelled redundant/contradictory — reported,
    #: never repaired a second time.
    unresolved: list[tuple[UUID, UUID]] = Field(default_factory=list)
    flags_version: Literal["combinatorics.pair_flags_v1"] = "combinatorics.pair_flags_v1"

    @model_validator(mode="after")
    def _consistent(self) -> PairReport:
        heads, descs = set(self.headlines), set(self.descriptions)
        if len(heads) != len(self.headlines) or len(descs) != len(self.descriptions):
            raise ValueError("an asset appears twice in the ad")
        members = heads | descs
        for pair in self.pairs:
            if pair.a not in members or pair.b not in members:
                raise ValueError(f"pair {pair.a}/{pair.b} names an asset not in the ad")
            expected = {
                "HH": pair.a in heads and pair.b in heads,
                "HD": pair.a in heads and pair.b in descs,
                "DD": pair.a in descs and pair.b in descs,
            }[pair.kind]
            if not expected:
                raise ValueError(f"pair {pair.a}/{pair.b} is not a {pair.kind} pair")
        covered = [frozenset((pair.a, pair.b)) for pair in self.pairs]
        every = {frozenset(p) for p in combinations([*self.headlines, *self.descriptions], 2)}
        if len(covered) != len(set(covered)) or set(covered) != every:
            raise ValueError("pairs must cover every pair the ad can serve, exactly once")
        pinnable = {
            asset
            for pair in self.pairs
            if pair.label == "order_dependent" and not pair.flags
            for asset in (pair.a, pair.b)
        }
        for pin in self.pins:
            if pin.asset_id not in pinnable:
                raise ValueError(
                    f"asset {pin.asset_id} is pinned but is in no unflagged order_dependent "
                    f"pair; pins never force a message"
                )
        for swap in self.swaps:
            if swap.in_from_reserve not in members:
                raise ValueError(f"in_from_reserve {swap.in_from_reserve} is not in the ad")
            if swap.out in members:
                raise ValueError(f"swapped-out {swap.out} is still in the ad")
        bad = [(p.a, p.b) for p in self.pairs if p.flags or p.label in BAD_LABELS]
        if list(self.unresolved) != bad:
            raise ValueError(
                "unresolved must list exactly the pairs still flagged or labelled "
                "redundant/contradictory, in pair order"
            )
        return self


class CombinationCoherenceOutput(_Frozen):
    schema_version: Literal["1.0"] = SEARCH_ADS_SCHEMA_VERSION
    ads: list[PairReport] = Field(default_factory=list)
