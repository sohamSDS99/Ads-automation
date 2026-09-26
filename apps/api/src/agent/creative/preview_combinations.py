"""Which combinations of an RSA 4.6.4 renders (Stage 04 PRD §11 4.6.4).

"The three highest-likelihood combinations plus the longest-string combination
of every RSA." Google fills three headline positions (H1–H3) and two
description positions (D1–D2) from the ad's assets. An asset pinned to a
position serves only there, and a position that has a pin takes only its
pinned assets — so a combination is one *valid assignment* of assets to
positions.

**`likelihood_v1`** — §11 names the likelihood and defines none; this is the
definition S4-P15 gives it (a ruling is owed, docs/stage-04-questions.md). A
creative run happens before launch, so there is no serving history to weight
one asset over another, and Google rotates evenly among the assets a position
admits: every valid assignment is equally likely, `1 / (valid headline
assignments × valid description assignments)`. The three "highest" are
therefore a tie, broken deterministically by the slate's own order — and
rotated, so the k-th combination leads with the k-th asset a position admits
and the three previews show as much of the slate as three ads can.

**Longest-string** — each pinned position takes its longest pinned asset and
the free positions take the longest unpinned ones, in position order. The
positions' candidate sets are disjoint (a pinned asset serves nowhere else),
so this maximises the combination's total length. Lengths are the linter's own
count (`guardrails.matchers.assets.measure`), never a second definition.

When the longest combination is one of the three, it is one preview with both
roles, not two identical renders. Pure: no I/O, no clock, no randomness.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Final

from agent.guardrails.matchers.assets import measure

HEADLINE_POSITIONS: Final = ("H1", "H2", "H3")
DESCRIPTION_POSITIONS: Final = ("D1", "D2")
LIKELY: Final = 3
LONGEST: Final = "longest"
#: How many rotations are tried for three distinct combinations. A slate with
#: fewer distinct valid assignments than three runs out long before this.
MAX_ROTATIONS: Final = 64


@dataclass(frozen=True, slots=True)
class Piece:
    """One carried asset of the ad: its id, the text Google shows, its pin."""

    ref: str
    text: str
    pin: str | None = None


@dataclass(frozen=True, slots=True)
class Combination:
    roles: tuple[str, ...]
    #: One asset ref per position (H1, H2, H3); None where the ad has too few.
    headlines: tuple[str | None, ...]
    descriptions: tuple[str | None, ...]
    likelihood: Fraction


def plan(headlines: Sequence[Piece], descriptions: Sequence[Piece]) -> tuple[Combination, ...]:
    """The likely combinations (`likely_1..3`), then the longest — deduplicated."""
    if not headlines:
        return ()
    likelihood = Fraction(
        1,
        _assignments(headlines, HEADLINE_POSITIONS)
        * _assignments(descriptions, DESCRIPTION_POSITIONS),
    )
    seen: list[tuple[tuple[str | None, ...], tuple[str | None, ...]]] = []
    rotations = min(MAX_ROTATIONS, max(1, len(headlines)) * max(1, len(descriptions)))
    for k in range(rotations):
        key = (
            _rotated(headlines, HEADLINE_POSITIONS, k),
            _rotated(descriptions, DESCRIPTION_POSITIONS, k),
        )
        if key not in seen:
            seen.append(key)
        if len(seen) == LIKELY:
            break
    roles: list[list[str]] = [[f"likely_{index + 1}"] for index in range(len(seen))]
    longest = (
        _longest(headlines, HEADLINE_POSITIONS),
        _longest(descriptions, DESCRIPTION_POSITIONS),
    )
    if longest in seen:
        roles[seen.index(longest)].append(LONGEST)
    else:
        seen.append(longest)
        roles.append([LONGEST])
    return tuple(
        Combination(
            roles=tuple(role),
            headlines=heads,
            descriptions=descs,
            likelihood=likelihood,
        )
        for role, (heads, descs) in zip(roles, seen, strict=True)
    )


def _candidates(pieces: Sequence[Piece], position: str, positions: Sequence[str]) -> list[Piece]:
    """What a position admits: its pinned assets, or every unpinned one."""
    pinned = [piece for piece in pieces if piece.pin == position]
    return pinned or [piece for piece in pieces if piece.pin not in positions]


def _rotated(pieces: Sequence[Piece], positions: Sequence[str], k: int) -> tuple[str | None, ...]:
    used: set[str] = set()
    out: list[str | None] = []
    for position in positions:
        admitted = _candidates(pieces, position, positions)
        chosen = None
        for offset in range(len(admitted)):
            piece = admitted[(k + offset) % len(admitted)]
            if piece.ref not in used:
                chosen = piece.ref
                break
        if chosen is not None:
            used.add(chosen)
        out.append(chosen)
    return tuple(out)


def _longest(pieces: Sequence[Piece], positions: Sequence[str]) -> tuple[str | None, ...]:
    order = {piece.ref: index for index, piece in enumerate(pieces)}
    used: set[str] = set()
    out: list[str | None] = []
    for position in positions:
        admitted = sorted(
            (p for p in _candidates(pieces, position, positions) if p.ref not in used),
            key=lambda p: (-measure(p.text, "chars"), order[p.ref]),
        )
        chosen = admitted[0].ref if admitted else None
        if chosen is not None:
            used.add(chosen)
        out.append(chosen)
    return tuple(out)


def _assignments(pieces: Sequence[Piece], positions: Sequence[str]) -> int:
    """How many valid assignments of `pieces` to `positions` there are."""
    count = 1
    free = [position for position in positions if not any(p.pin == position for p in pieces)]
    for position in positions:
        if position not in free:
            count *= sum(1 for piece in pieces if piece.pin == position)
    unpinned = sum(1 for piece in pieces if piece.pin not in positions)
    filled = min(unpinned, len(free))
    return count * math.perm(unpinned, filled)
