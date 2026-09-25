"""Copy metrics — pure, with versioned metric ids (Stage 04 PRD §6.1, §9.5, §11 4.2, 4.5).

Three metrics, each named by the id a stored number cites:

* `copy.trigram_v1` — the Dice coefficient of two texts' character-trigram
  sets. `copy.near_duplicate_trigram` (0.80) is a threshold on it: selection
  never picks two headlines at or above it, and 4.2.3 flags a pair that is.
* `copy.distinctness_v1` — how far apart two *sets* of copy are: one minus the
  mean, taken both ways, of each asset's similarity to its closest match on the
  other side. For two single texts it is `1 − copy.trigram_v1`.
  `copy.variant_min_distance` (0.65, "1 − similarity") is a threshold on it.
* `match.token_trigram_v1` — whether a landing page's H1 echoes an ad group's
  headlines (4.5.1). Per headline, each of its distinct words is matched to
  its closest word in the H1 by `copy.trigram_v1`'s trigram Dice, and the
  headline scores the mean: the share of the ad's message the page repeats.
  The page scores its best-echoed headline. `landing.message_match_min`
  (0.55) is a threshold on it.

**A version pins a definition.** A number recorded under `copy.trigram_v1`
must mean the same thing a year later, so this module owns its normalisation
and its trigrams rather than borrowing `guardrails.normalize.trigram_similarity`
— an edit made there for claim matching would otherwise re-grade every stored
near-duplicate without changing the id. A change to either definition is a new
id (`_v2`), never an edit to this one.

Pure: no I/O, no clock, no randomness, no ORM (`scripts/check_creative_purity.py`).
Arithmetic is exact (`Fraction`) wherever a result is summed or ranked, so a
selection built on these numbers is identical in every process and in every
input order; the float a report shows is the exact ratio, rounded once.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from fractions import Fraction
from typing import Final

TRIGRAM_V1: Final = "copy.trigram_v1"
DISTINCTNESS_V1: Final = "copy.distinctness_v1"
MESSAGE_MATCH_V1: Final = "match.token_trigram_v1"

#: A run of letters or digits in any script. The underscore is a word
#: character to `\w` and a separator to a reader, so it is excluded.
_WORD = re.compile(r"[^\W_]+")


def normalize(text: str) -> str:
    """NFKC, casefolded, every run of anything but letters and digits → one space.

    "SDS—Software!!" and "sds software" are the same headline to a reader, so
    they are the same text to the metric.
    """
    folded = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(_WORD.findall(folded))


def trigrams(text: str) -> frozenset[str]:
    """Character trigrams of the normalised text, padded by one space each side.

    The padding makes word boundaries count: "sds" at the start of a headline
    shares " sd" with another headline that starts with it.
    """
    normalized = normalize(text)
    if not normalized:
        return frozenset()
    padded = f" {normalized} "
    return frozenset(padded[index : index + 3] for index in range(len(padded) - 2))


def _dice(left: frozenset[str], right: frozenset[str]) -> Fraction:
    if not left and not right:
        return Fraction(1)
    if not left or not right:
        return Fraction(0)
    return Fraction(2 * len(left & right), len(left) + len(right))


def similarity_ratio(a: str, b: str) -> Fraction:
    """`copy.trigram_v1`, exactly."""
    return _dice(trigrams(a), trigrams(b))


def similarity(a: str, b: str) -> float:
    """`copy.trigram_v1` as a float: 1.0 for identical text, 0.0 for nothing shared."""
    return float(similarity_ratio(a, b))


def distinctness_ratio(a: Sequence[str], b: Sequence[str]) -> Fraction:
    """`copy.distinctness_v1`, exactly.

    `1 − ½(mean_{x∈a} max_{y∈b} sim(x, y) + mean_{y∈b} max_{x∈a} sim(x, y))`.
    Taken both ways so the result is symmetric; a best match rather than the
    union of every trigram so that two ads sharing the words every ad uses are
    not scored alike. Two empty sides are identical (0); one empty side is
    wholly distinct (1).
    """
    if not a and not b:
        return Fraction(0)
    if not a or not b:
        return Fraction(1)
    left = [trigrams(text) for text in a]
    right = [trigrams(text) for text in b]
    forward = sum((max(_dice(x, y) for y in right) for x in left), Fraction(0)) / len(left)
    backward = sum((max(_dice(y, x) for x in left) for y in right), Fraction(0)) / len(right)
    return 1 - (forward + backward) / 2


def distinctness(a: Sequence[str], b: Sequence[str]) -> float:
    """`copy.distinctness_v1` as a float."""
    return float(distinctness_ratio(a, b))


def tokens(text: str) -> tuple[str, ...]:
    """The distinct words of the normalised text, in the order they first appear."""
    return tuple(dict.fromkeys(normalize(text).split()))


def echo_ratio(ad_text: str, page_text: str | None) -> Fraction:
    """How much of `ad_text` the page repeats, exactly: one headline's part of
    `match.token_trigram_v1`.

    `mean_{t ∈ tokens(ad)} max_{u ∈ tokens(page)} dice(trigrams(t), trigrams(u))`.

    Directional on purpose. The question is whether the page says what the ad
    said, so the page may say more — an H1 is usually longer than a 30-character
    headline — without losing anything, and it is the ad's words that are
    counted. Matched word by word rather than over the whole string so that
    word order does not matter and an inflection is a near match rather than a
    miss ("manage" against "management" is 5/8). No page headline, or no word
    in it, echoes nothing.
    """
    ad = tokens(ad_text)
    page = [trigrams(token) for token in tokens(page_text or "")]
    if not ad or not page:
        return Fraction(0)
    total = sum((max(_dice(trigrams(token), other) for other in page) for token in ad), Fraction(0))
    return total / len(ad)


def message_match_ratio(headlines: Sequence[str], h1: str | None) -> Fraction:
    """`match.token_trigram_v1`, exactly: the page's H1 against an ad group's
    headlines, scored by the headline it echoes best.

    Google serves any of an ad's headlines, and a page cannot repeat fifteen
    deliberately different ones, so the page is asked to echo one of them —
    the maximum. No headline is no match.
    """
    return max((echo_ratio(headline, h1) for headline in headlines), default=Fraction(0))


def message_match(headlines: Sequence[str], h1: str | None) -> float:
    """`match.token_trigram_v1` as a float."""
    return float(message_match_ratio(headlines, h1))
