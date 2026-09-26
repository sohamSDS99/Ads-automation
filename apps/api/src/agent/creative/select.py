"""`select.headlines_v1` — quota-constrained maximum-diversity selection (PRD §11 4.2.1).

**The model writes candidates; code selects** (design principle 4). 4.2.1 asks
for a pool of `copy.headline_pool_size` headlines, lints each at creation, and
hands the ones that passed to this function, which picks at most `limit` of
them (`asset_specs.search.headline.max_count`, 15):

1. **Quotas first.** While any category of `copy.headline_quotas` is short,
   pick among the candidates of the short categories.
2. **Then fill.** Pick among everything left until `limit` is reached.
3. **Every pick is max-min diversity:** the candidate whose *closest* selected
   headline is farthest away (`copy.distinctness_v1`), then the one farthest
   from the selected set in total, then canonical order. The first pick, with
   nothing selected, is the candidate farthest from the rest of the pool.
4. **Never a near-duplicate.** A pick excludes every candidate at or above
   `near_duplicate` (`copy.trigram_v1`) from it, so no two selected headlines
   are near-duplicates. An exact duplicate is similarity 1.0 and always
   excluded.
5. **Feasibility before diversity.** A quota pick that would exclude so many of
   a short category that its quota could no longer be met is deferred while a
   pick exists that keeps every quota reachable. Greedy diversity alone takes
   the most distinctive headline first even when it is the near-twin of the
   only other CTA.

Everything not selected is a **reserve**, best replacement first (the same
score against the final selection); the excluded near-duplicates come last,
each naming the headline it is too close to. 4.2.3 repairs from reserves.

Deterministic by construction, which is what "byte-identical across two
processes" asks for: candidates are ranked in a canonical order (normalised
text, text, category, ref) before anything else happens, so the input order
never matters; scores are exact fractions, so a tie is a real tie and is broken
by that order; and no set is ever iterated to produce an ordering, so the
per-process hash seed cannot leak in.

Pure: no I/O, no clock, no randomness, no ORM (`scripts/check_creative_purity.py`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Final

from agent.creative import metrics

HEADLINES_V1: Final = "select.headlines_v1"

#: §11 4.2.1's categories, in the order a quota report lists them.
CATEGORIES: Final = ("keyword", "benefit", "offer", "proof", "objection", "cta")


class SelectionError(ValueError):
    """The arguments cannot describe a selection (quotas above the limit, a repeated ref)."""


@dataclass(frozen=True, slots=True)
class Candidate:
    """One linted, valid headline. `text` is what a reader sees — for dynamic
    keyword insertion, the default text."""

    ref: str
    text: str
    category: str


@dataclass(frozen=True, slots=True)
class QuotaLine:
    category: str
    required: int
    selected: int
    #: Candidates of this category that reached selection (linted and valid).
    available: int


@dataclass(frozen=True, slots=True)
class QuotaReport:
    lines: tuple[QuotaLine, ...]
    limit: int
    selected: int
    met: bool


@dataclass(frozen=True, slots=True)
class NearDuplicate:
    """A candidate excluded because it is too close to a selected one."""

    ref: str
    of: str
    similarity: float


@dataclass(frozen=True, slots=True)
class Selection:
    selected: tuple[str, ...]
    reserve: tuple[str, ...]
    quota_report: QuotaReport
    near_duplicates: tuple[NearDuplicate, ...]
    method: str = HEADLINES_V1


def headlines_v1(
    pool: Sequence[Candidate],
    *,
    quotas: Mapping[str, int],
    limit: int,
    near_duplicate: float,
) -> Selection:
    """Pick at most `limit` headlines from `pool`. Raises `SelectionError`."""
    _check(pool, quotas, limit, near_duplicate)
    order = sorted(
        pool, key=lambda item: (metrics.normalize(item.text), item.text, item.category, item.ref)
    )
    state = _State(order, near_duplicate)
    need = {category: quotas.get(category, 0) for category in _categories(quotas, order)}

    while any(need.values()):
        eligible = [item for item in state.unpicked() if need.get(item.category, 0) > 0]
        if not eligible:
            break
        feasible = [item for item in eligible if state.keeps_quotas(item, need)]
        chosen = state.best(feasible or eligible)
        state.choose(chosen)
        need[chosen.category] -= 1

    while len(state.selected) < limit:
        remaining = state.unpicked()
        if not remaining:
            break
        state.choose(state.best(remaining))

    return Selection(
        selected=tuple(item.ref for item in state.selected),
        reserve=state.reserve(),
        quota_report=quota_report(quotas, order, state.selected, limit),
        near_duplicates=state.near_duplicates(),
    )


def _check(
    pool: Sequence[Candidate], quotas: Mapping[str, int], limit: int, near_duplicate: float
) -> None:
    if limit < 1:
        raise SelectionError(f"limit must be at least 1, got {limit}")
    if not 0.0 < near_duplicate <= 1.0:
        raise SelectionError(f"near_duplicate must be in (0, 1], got {near_duplicate}")
    negative = sorted(category for category, count in quotas.items() if count < 0)
    if negative:
        raise SelectionError(f"quotas cannot be negative: {', '.join(negative)}")
    total = sum(quotas.values())
    if total > limit:
        raise SelectionError(
            f"quotas sum to {total}, more than the {limit} headlines an ad may carry"
        )
    seen: set[str] = set()
    for item in pool:
        if item.ref in seen:
            raise SelectionError(f"ref {item.ref!r} appears twice in the pool")
        seen.add(item.ref)


def _categories(quotas: Mapping[str, int], order: Sequence[Candidate]) -> list[str]:
    """§11's categories in their order, then any other quota key, alphabetically."""
    extra = sorted(set(quotas) - set(CATEGORIES))
    return [*(c for c in CATEGORIES if c in quotas), *extra]


class _State:
    """The selection in progress. Every ordering it produces walks `order`."""

    def __init__(self, order: list[Candidate], near_duplicate: float) -> None:
        self.order = order
        self.rank = {item.ref: index for index, item in enumerate(order)}
        self.threshold = near_duplicate
        self.selected: list[Candidate] = []
        self.chosen: set[str] = set()
        #: ref -> (the selected ref it is too close to, their similarity).
        self.excluded: dict[str, tuple[str, Fraction]] = {}
        self._similarity: dict[tuple[str, str], Fraction] = {}

    def similarity(self, left: Candidate, right: Candidate) -> Fraction:
        key = (left.ref, right.ref) if left.ref <= right.ref else (right.ref, left.ref)
        if key not in self._similarity:
            self._similarity[key] = metrics.similarity_ratio(left.text, right.text)
        return self._similarity[key]

    def distance(self, left: Candidate, right: Candidate) -> Fraction:
        return 1 - self.similarity(left, right)

    def too_close(self, left: Candidate, right: Candidate) -> bool:
        return float(self.similarity(left, right)) >= self.threshold

    def unpicked(self) -> list[Candidate]:
        return [
            item
            for item in self.order
            if item.ref not in self.chosen and item.ref not in self.excluded
        ]

    def score(self, item: Candidate, against: Sequence[Candidate]) -> tuple[Fraction, Fraction]:
        """(closest distance, total distance) to `against`."""
        if not against:
            return Fraction(1), Fraction(0)
        distances = [self.distance(item, other) for other in against]
        return min(distances), sum(distances, Fraction(0))

    def best(self, candidates: Sequence[Candidate]) -> Candidate:
        if self.selected:
            scored = [(self.score(item, self.selected), item) for item in candidates]
        else:
            # Nothing selected yet: the candidate farthest from the rest of the
            # open pool opens the selection.
            rest = self.unpicked()
            scored = [
                (self.score(item, [other for other in rest if other.ref != item.ref]), item)
                for item in candidates
            ]
            scored = [((Fraction(1), total), item) for (_closest, total), item in scored]
        return max(scored, key=lambda pair: (pair[0], -self.rank[pair[1].ref]))[1]

    def keeps_quotas(self, item: Candidate, need: Mapping[str, int]) -> bool:
        """Would every short category still have enough open candidates after `item`?"""
        remaining = dict(need)
        if remaining.get(item.category, 0) > 0:
            remaining[item.category] -= 1
        left: dict[str, int] = {}
        for other in self.unpicked():
            if other.ref == item.ref or self.too_close(other, item):
                continue
            left[other.category] = left.get(other.category, 0) + 1
        return all(left.get(category, 0) >= count for category, count in remaining.items())

    def choose(self, item: Candidate) -> None:
        self.selected.append(item)
        self.chosen.add(item.ref)
        for other in self.unpicked():
            if self.too_close(other, item):
                self.excluded[other.ref] = (item.ref, self.similarity(other, item))

    def reserve(self) -> tuple[str, ...]:
        rest = [item for item in self.order if item.ref not in self.chosen]
        return tuple(
            item.ref
            for item in sorted(
                rest,
                key=lambda item: (
                    item.ref in self.excluded,
                    tuple(-part for part in self.score(item, self.selected)),
                    self.rank[item.ref],
                ),
            )
        )

    def near_duplicates(self) -> tuple[NearDuplicate, ...]:
        return tuple(
            NearDuplicate(ref=ref, of=of, similarity=float(similarity))
            for ref, (of, similarity) in sorted(
                self.excluded.items(), key=lambda entry: self.rank[entry[0]]
            )
        )


def quota_report(
    quotas: Mapping[str, int],
    order: Sequence[Candidate],
    selected: Sequence[Candidate],
    limit: int,
) -> QuotaReport:
    lines = tuple(
        QuotaLine(
            category=category,
            required=quotas[category],
            selected=sum(1 for item in selected if item.category == category),
            available=sum(1 for item in order if item.category == category),
        )
        for category in _categories(quotas, order)
    )
    return QuotaReport(
        lines=lines,
        limit=limit,
        selected=len(selected),
        met=all(line.selected >= line.required for line in lines),
    )
