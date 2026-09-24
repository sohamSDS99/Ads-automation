"""Combination coherence — pairs, deterministic flags, one repair round, pins (PRD §11 4.2.3).

Google assembles a responsive search ad from any of its headlines beside any of
its descriptions, so every pair it could serve is checked: headline × headline
(HH), headline × description (HD) and description × description (DD). Fifteen
headlines and four descriptions are 105 + 60 + 6 = 171 pairs.

**`combinatorics.pair_flags_v1`** — §11 names six flags and defines none. This
is the definition S4-P5 gives them (a ruling is owed, docs/stage-04-questions.md).
Each is a property of two assets' text *and* their structured fields, so none
needs a word list:

* `duplicate` — equal after `metrics.normalize`.
* `near_duplicate` — not equal, `copy.trigram_v1` ≥ `copy.near_duplicate_trigram`.
* `offer_conflict` — both carry offer terms of one kind (a percentage, or an
  amount in one currency) with different values. An offer term is a
  percentage or an amount *outside* the asset's licensed claim span: "cut admin
  time by 40%" inside a claim states the claim, it offers nothing.
* `claim_conflict` — both carry licensed claims, not the same ones, and a
  quantity of one kind inside their claim spans differs: "within 24 hours" and
  "within 48 hours"; "500 teams" and "900 teams". A count's kind is the word it
  counts, so "500 teams" and "900 sites" do not conflict.
* `cta_collision` — both ask for an action and share no ask. An asset asks with
  the leading word of any of its clauses that is a CTA verb; the CTA verbs are
  the leading words of the pool's `cta` headlines (the model's own labels); a
  `cta` headline always asks with its first word.
* `keyword_stuffing` — HH only: a headline written to carry a keyword
  (`keyword_ref`) beside another headline that contains that keyword again.

A flag is blocking. **`repair_v1`** is the one repair round: it swaps assets
out of bad pairs (flagged, or labelled `redundant` / `contradictory` by
CLASSIFY) for reserves, each swap chosen so it brings no flag of its own and
keeps every headline quota; it never swaps an asset back in, and what it cannot
repair it reports as unresolved rather than looping. **`pins_v1`** pins only
`order_dependent` pairs, in the order they read — never to force a message.

Pure: no I/O, no clock, no randomness, no ORM (`scripts/check_creative_purity.py`).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from itertools import combinations
from typing import Final, Literal

from agent.creative import metrics

PAIR_FLAGS_V1: Final = "combinatorics.pair_flags_v1"

Kind = Literal["HH", "HD", "DD"]
Role = Literal["headline", "description"]
Position = Literal["H1", "H2", "H3", "D1", "D2"]

FLAGS: Final = (
    "duplicate",
    "near_duplicate",
    "offer_conflict",
    "cta_collision",
    "claim_conflict",
    "keyword_stuffing",
)


@dataclass(frozen=True, slots=True)
class Asset:
    """One headline or description as the pair checks see it.

    `text` is what a reader sees — for dynamic keyword insertion, the default
    text. `claim_span` is the `[start, end)` of `text` that states the licensed
    claims; with `claim_ids` and no span, the whole text states them.
    """

    ref: str
    role: Role
    text: str
    category: str | None = None
    keyword_ref: str | None = None
    claim_ids: tuple[str, ...] = ()
    claim_span: tuple[int, int] | None = None


@dataclass(frozen=True, slots=True)
class FlagRules:
    """The parameters `pair_flags_v1` is evaluated with."""

    near_duplicate: float
    cta_verbs: frozenset[str]


# ---------------------------------------------------------------------------
# enumeration
# ---------------------------------------------------------------------------


def enumerate_pairs(
    headlines: Sequence[Asset], descriptions: Sequence[Asset]
) -> list[tuple[Asset, Asset, Kind]]:
    """HH, then HD, then DD — each unordered pair once, in the order given."""
    pairs: list[tuple[Asset, Asset, Kind]] = [(a, b, "HH") for a, b in combinations(headlines, 2)]
    pairs.extend((h, d, "HD") for h in headlines for d in descriptions)
    pairs.extend((a, b, "DD") for a, b in combinations(descriptions, 2))
    return pairs


# ---------------------------------------------------------------------------
# pair_flags_v1
# ---------------------------------------------------------------------------


def flags(a: Asset, b: Asset, kind: Kind, rules: FlagRules) -> tuple[str, ...]:
    """Every `pair_flags_v1` flag the pair raises, in `FLAGS` order."""
    raised: set[str] = set()
    if metrics.normalize(a.text) == metrics.normalize(b.text):
        raised.add("duplicate")
    elif metrics.similarity(a.text, b.text) >= rules.near_duplicate:
        raised.add("near_duplicate")
    if _disagree(_offer_terms(a), _offer_terms(b)):
        raised.add("offer_conflict")
    left, right = _asks(a, rules.cta_verbs), _asks(b, rules.cta_verbs)
    if left and right and left.isdisjoint(right):
        raised.add("cta_collision")
    if (
        a.claim_ids
        and b.claim_ids
        and set(a.claim_ids) != set(b.claim_ids)
        and _disagree(_claim_quantities(a), _claim_quantities(b))
    ):
        raised.add("claim_conflict")
    if kind == "HH" and (_carries_again(a, b) or _carries_again(b, a)):
        raised.add("keyword_stuffing")
    return tuple(flag for flag in FLAGS if flag in raised)


def cta_verbs(pool: Iterable[Asset]) -> frozenset[str]:
    """The leading words of the pool's `cta` headlines."""
    verbs: set[str] = set()
    for asset in pool:
        if asset.role == "headline" and asset.category == "cta":
            tokens = metrics.normalize(asset.text).split()
            if tokens:
                verbs.add(tokens[0])
    return frozenset(verbs)


_NUMBER = r"\d+(?:[.,]\d+)*"
_CURRENCY = "$€£¥₹"
_PERCENT = re.compile(rf"({_NUMBER})\s*(?:%|\bper\s?cent\b)", re.IGNORECASE)
_MONEY = re.compile(rf"([{_CURRENCY}])\s*({_NUMBER})|({_NUMBER})\s*([{_CURRENCY}])")
_DURATION = re.compile(
    rf"({_NUMBER})\s*-?\s*(hours?|hrs?|minutes?|mins?|days?|weeks?|months?|years?)\b",
    re.IGNORECASE,
)
_COUNT = re.compile(rf"({_NUMBER})\+?\s+([^\W\d_]+)")
_DURATION_UNITS = {"hr": "hour", "min": "minute"}
_CLAUSE = re.compile(r"[.!?;:]+")


@dataclass(frozen=True, slots=True)
class _Quantity:
    kind: str
    value: str
    start: int
    end: int


def _number(raw: str) -> str:
    """One spelling per value: "1,000" and "1000" agree, "49,99" is 49.99."""
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", raw):
        raw = raw.replace(",", "")
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?", raw):
        raw = raw.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d+,\d{1,2}", raw):
        raw = raw.replace(",", ".")
    else:
        raw = raw.replace(",", "")
    return format(Decimal(raw).normalize(), "f")


def _quantities(text: str) -> list[_Quantity]:
    """Percentages, amounts, durations, then counts — a later kind never
    re-reads the digits an earlier one took ("24 hours" is not a count)."""
    found: list[_Quantity] = []

    def taken(start: int, end: int) -> bool:
        return any(start < q.end and q.start < end for q in found)

    for match in _PERCENT.finditer(text):
        found.append(_Quantity("percent", _number(match[1]), match.start(), match.end()))
    for match in _MONEY.finditer(text):
        if not taken(match.start(), match.end()):
            symbol, amount = (match[1], match[2]) if match[1] else (match[4], match[3])
            found.append(_Quantity(f"money:{symbol}", _number(amount), *match.span()))
    for match in _DURATION.finditer(text):
        if not taken(match.start(), match.end()):
            unit = match[2].casefold().rstrip("s")
            unit = _DURATION_UNITS.get(unit, unit)
            found.append(_Quantity(f"duration:{unit}", _number(match[1]), *match.span()))
    for match in _COUNT.finditer(text):
        if not taken(match.start(), match.end()):
            word = match[2].casefold()
            unit = word[:-1] if word.endswith("s") and len(word) > 3 else word
            found.append(_Quantity(f"count:{unit}", _number(match[1]), *match.span()))
    return found


def _span(asset: Asset) -> tuple[int, int] | None:
    if asset.claim_span is not None:
        return asset.claim_span
    return (0, len(asset.text)) if asset.claim_ids else None


def _inside(quantity: _Quantity, span: tuple[int, int] | None) -> bool:
    return span is not None and span[0] <= quantity.start and quantity.end <= span[1]


def _offer_terms(asset: Asset) -> dict[str, frozenset[str]]:
    span = _span(asset) if asset.claim_ids else None
    terms: dict[str, set[str]] = {}
    for quantity in _quantities(asset.text):
        is_offer = quantity.kind == "percent" or quantity.kind.startswith("money:")
        if is_offer and not _inside(quantity, span):
            terms.setdefault(quantity.kind, set()).add(quantity.value)
    return {kind: frozenset(values) for kind, values in terms.items()}


def _claim_quantities(asset: Asset) -> dict[str, frozenset[str]]:
    span = _span(asset)
    found: dict[str, set[str]] = {}
    for quantity in _quantities(asset.text):
        if _inside(quantity, span):
            found.setdefault(quantity.kind, set()).add(quantity.value)
    return {kind: frozenset(values) for kind, values in found.items()}


def _disagree(left: Mapping[str, frozenset[str]], right: Mapping[str, frozenset[str]]) -> bool:
    return any(left[kind] != right[kind] for kind in left.keys() & right.keys())


def _asks(asset: Asset, verbs: frozenset[str]) -> frozenset[str]:
    """The CTA verbs an asset asks with: clause-leading words that are CTA verbs."""
    leading: set[str] = set()
    for clause in _CLAUSE.split(asset.text):
        tokens = metrics.normalize(clause).split()
        if tokens:
            leading.add(tokens[0])
    asks = leading & verbs
    if asset.role == "headline" and asset.category == "cta":
        tokens = metrics.normalize(asset.text).split()
        if tokens:
            asks.add(tokens[0])
    return frozenset(asks)


def contains_phrase(text: str, phrase: str) -> bool:
    """Whether `phrase` occurs in `text` as whole words, after `metrics.normalize`."""
    tokens = metrics.normalize(text).split()
    needle = metrics.normalize(phrase).split()
    width = len(needle)
    return bool(needle) and any(
        tokens[index : index + width] == needle for index in range(len(tokens) - width + 1)
    )


def _carries_again(carrier: Asset, other: Asset) -> bool:
    return carrier.keyword_ref is not None and contains_phrase(other.text, carrier.keyword_ref)


# ---------------------------------------------------------------------------
# repair_v1 — exactly one round, from reserves
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Swap:
    out: str
    into: str
    #: "offer_conflict with d-2; redundant with h-7" — every bad pair it ended.
    why: str


@dataclass(frozen=True, slots=True)
class Repair:
    headlines: tuple[Asset, ...]
    descriptions: tuple[Asset, ...]
    swaps: tuple[Swap, ...]
    #: Bad pairs no swap could end, in the order they were given.
    unresolved: tuple[tuple[str, str], ...]


def repair_v1(
    headlines: Sequence[Asset],
    descriptions: Sequence[Asset],
    reserve_headlines: Sequence[Asset],
    reserve_descriptions: Sequence[Asset],
    bad: Mapping[tuple[str, str], Sequence[str]],
    *,
    quotas: Mapping[str, int],
    rules: FlagRules,
) -> Repair:
    """One repair round over `bad` (pair key → the flags and labels that made it bad).

    Repeatedly takes the asset in the most bad pairs — headlines before
    descriptions (a headline has more reserves; a description carries a
    licensed claim), a later pick before an earlier one, then ref — and swaps
    it for the best reserve that brings no flag against what stays, keeps its
    category while that category's quota has no slack, and has not been used.
    An asset no reserve can replace is passed over for the next; when nothing
    in a bad pair can be replaced, the round ends. Each swap removes the bad
    pairs of the asset it took out; the pairs a new asset forms are not
    re-judged here (the node labels them once, and repairs nothing further).
    """
    current = {"headline": list(headlines), "description": list(descriptions)}
    reserves = {"headline": list(reserve_headlines), "description": list(reserve_descriptions)}
    open_bad = {key: tuple(reasons) for key, reasons in bad.items()}
    retired: set[str] = set()
    swaps: list[Swap] = []

    while open_bad:
        counts: dict[str, int] = {}
        for key in open_bad:
            for ref in key:
                counts[ref] = counts.get(ref, 0) + 1
        placed = {asset.ref: asset for group in current.values() for asset in group}
        position = {
            asset.ref: index for group in current.values() for index, asset in enumerate(group)
        }
        outs = sorted(
            (ref for ref in counts if ref in placed),
            key=lambda ref: (
                -counts[ref],
                0 if placed[ref].role == "headline" else 1,
                -position[ref],
                ref,
            ),
        )
        for ref in outs:
            out = placed[ref]
            into = _replacement(out, current, reserves[out.role], retired, quotas, rules)
            if into is None:
                continue
            group = current[out.role]
            group[group.index(out)] = into
            retired.update({out.ref, into.ref})
            ended = [(key, reasons) for key, reasons in open_bad.items() if out.ref in key]
            why = "; ".join(
                f"{reason} with {key[1] if key[0] == out.ref else key[0]}"
                for key, reasons in ended
                for reason in reasons
            )
            swaps.append(Swap(out=out.ref, into=into.ref, why=why))
            open_bad = {key: reasons for key, reasons in open_bad.items() if out.ref not in key}
            break
        else:
            break

    return Repair(
        headlines=tuple(current["headline"]),
        descriptions=tuple(current["description"]),
        swaps=tuple(swaps),
        unresolved=tuple(key for key in bad if key in open_bad),
    )


def _replacement(
    out: Asset,
    current: Mapping[str, list[Asset]],
    reserves: Sequence[Asset],
    retired: set[str],
    quotas: Mapping[str, int],
    rules: FlagRules,
) -> Asset | None:
    staying = [asset for group in current.values() for asset in group if asset.ref != out.ref]
    in_use = {asset.ref for asset in staying}
    peers = [asset for asset in staying if asset.role == out.role]
    must_keep_category = False
    if out.role == "headline" and out.category is not None:
        held = sum(1 for asset in current["headline"] if asset.category == out.category)
        must_keep_category = held <= quotas.get(out.category, 0)

    ranked: list[tuple[tuple[bool, Fraction, Fraction, int], Asset]] = []
    for index, candidate in enumerate(reserves):
        if candidate.ref in retired or candidate.ref in in_use or candidate.ref == out.ref:
            continue
        if must_keep_category and candidate.category != out.category:
            continue
        if any(flags(*_ordered(candidate, other), rules) for other in staying):
            continue
        distances = [1 - metrics.similarity_ratio(candidate.text, peer.text) for peer in peers]
        closest = min(distances) if distances else Fraction(1)
        total = sum(distances, Fraction(0))
        ranked.append(((candidate.category == out.category, closest, total, -index), candidate))
    if not ranked:
        return None
    return max(ranked, key=lambda entry: entry[0])[1]


def _ordered(left: Asset, right: Asset) -> tuple[Asset, Asset, Kind]:
    if left.role == right.role:
        return left, right, "HH" if left.role == "headline" else "DD"
    if left.role == "headline":
        return left, right, "HD"
    return right, left, "HD"


# ---------------------------------------------------------------------------
# pins_v1 — only order_dependent pairs
# ---------------------------------------------------------------------------

_PIN_POSITIONS: Final[Mapping[str, tuple[Position, Position]]] = {
    "HH": ("H1", "H2"),
    "HD": ("H1", "D1"),
    "DD": ("D1", "D2"),
}


@dataclass(frozen=True, slots=True)
class Pin:
    asset_ref: str
    position: Position
    why: str


def pins_v1(order_dependent: Sequence[tuple[Asset, Asset, Kind]]) -> tuple[Pin, ...]:
    """Pin each pair as it reads — `a` before `b` — unless an earlier pin contradicts it.

    An asset is pinned at most once. A pair one of whose assets is already
    pinned elsewhere is left unpinned: moving the earlier pin would break the
    pair that asked for it.
    """
    pinned: dict[str, Position] = {}
    pins: list[Pin] = []
    for a, b, kind in order_dependent:
        first, second = _PIN_POSITIONS[kind]
        if pinned.get(a.ref, first) != first or pinned.get(b.ref, second) != second:
            continue
        for asset, place, partner in ((a, first, b), (b, second, a)):
            if asset.ref not in pinned:
                pinned[asset.ref] = place
                pins.append(
                    Pin(
                        asset_ref=asset.ref,
                        position=place,
                        why=f"order_dependent with {partner.ref} ({kind})",
                    )
                )
    return tuple(pins)
