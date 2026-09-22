"""One spelling of a string, so one rule can recognise all of its disguises.

A banned term is banned however it was typed. Copy arrives from Word with curly
quotes, from Figma with non-breaking spaces, from a designer's emoji with a
zero-width joiner wedged into it, and from a bad paste with a Cyrillic `а`
sitting inside a Latin word. Every one of those is the *same word* to a reader
and a different byte sequence to `re`. Normalising first is what stops the
rulebook from being defeated by a keyboard.

The hard requirement is not the folding — it is that **a span found in the
folded text maps back to the caller's original offsets**. A finding whose span
highlights the wrong characters is worse than one with no span at all: the
writer looks at the underlined word, sees nothing wrong with it, and stops
trusting the linter. So every output character records the source range it came
from, and `source_span()` is the only supported way to read that back.

Folding is done chunk by chunk, where a chunk is a base character plus any
combining marks that follow it. Per-character NFKC would leave a decomposed
`u` + umlaut as two characters and a German pattern written with `ü` would
silently never match it; whole-string NFKC would compose correctly but leave no
honest way to recover offsets. Chunking gets both.

Pure by construction: no clock, no I/O, no model. Same input, same output, in
any process, on any day.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Final

#: Cross-script lookalikes, mapped onto the Latin letter they imitate. NFKC
#: already folds fullwidth forms, ligatures, superscripts and the ellipsis, so
#: this table only carries what NFKC deliberately leaves alone: characters that
#: are genuinely different letters and merely *look* the same.
#:
#: Applied after case folding, so one lower-case entry covers both cases.
HOMOGLYPHS: Final[dict[str, str]] = {
    # Cyrillic
    "а": "a",
    "б": "b",
    "в": "b",
    "г": "r",
    "е": "e",
    "ѕ": "s",
    "і": "i",
    "ј": "j",
    "к": "k",
    "м": "m",
    "н": "h",
    "о": "o",
    "р": "p",
    "с": "c",
    "т": "t",
    "у": "y",
    "х": "x",
    "ԁ": "d",
    "һ": "h",
    "ӏ": "l",
    "ё": "e",
    # Greek
    "α": "a",
    "β": "b",
    "ε": "e",
    "ζ": "z",
    "η": "n",
    "ι": "i",
    "κ": "k",
    "ν": "v",
    "ο": "o",
    "ρ": "p",
    "τ": "t",
    "υ": "u",
    "χ": "x",
    "ϲ": "c",
    # Quotes and dashes — NFKC keeps these distinct, and a writer who typed
    # `risk‑free` with a non-breaking hyphen means `risk-free`.
    "‘": "'",
    "’": "'",
    "‚": "'",
    "‛": "'",
    "“": '"',
    "”": '"',
    "„": '"',
    "‟": '"',
    "′": "'",
    "″": '"',
    "´": "'",
    "`": "'",
    "‐": "-",
    "‑": "-",
    "‒": "-",
    "–": "-",
    "—": "-",
    "―": "-",
    "−": "-",
}

#: Locales whose case folding genuinely differs from everybody else's: a dotted
#: capital `İ` lowercases to `i`, and a dotless `I` to `ı`. Python's `casefold`
#: is locale-independent and gets both wrong for Turkish and Azeri. Every other
#: transform in this module is the same in every language.
DOTTED_I_LOCALES: Final[frozenset[str]] = frozenset({"tr", "az"})


@dataclass(frozen=True, slots=True)
class Normalized:
    """Folded text, plus the map back to where each character came from."""

    #: The folded string. This is what patterns and term sets run against.
    text: str
    #: The original string, untouched. Findings quote from this.
    source: str
    #: `spans[i]` is the `(start, end)` range of `source` that produced
    #: `text[i]`. Parallel to `text`, so `len(spans) == len(text)`.
    spans: tuple[tuple[int, int], ...]
    locale: str

    def source_span(self, start: int, end: int) -> tuple[int, int]:
        """Translate a half-open span of `text` into one of `source`.

        Clamped rather than raising: a matcher that computed a span one past
        the end has a bug, but turning that into an exception would take a
        whole lint run down over a highlight. The span is the one part of a
        finding that can be wrong without the verdict being wrong.
        """
        if not self.spans:
            return (0, 0)
        start = max(0, min(start, len(self.spans) - 1))
        end = max(start + 1, min(end, len(self.spans)))
        return (self.spans[start][0], self.spans[end - 1][1])


def normalize(text: str, *, locale: str = "en") -> Normalized:
    """Fold `text` into its one comparable spelling, keeping the offset map.

    NFKC, case folding, zero-width removal, homoglyph mapping and whitespace
    collapse — in that order, chunk by chunk. Leading and trailing whitespace
    is dropped; internal runs collapse to a single space.
    """
    language = locale.split("-")[0].split("_")[0].casefold()
    dotted_i = language in DOTTED_I_LOCALES

    out: list[str] = []
    spans: list[tuple[int, int]] = []
    pending_space_from: int | None = None
    index = 0
    length = len(text)

    while index < length:
        start = index
        index += 1
        # A base character carries its combining marks with it, so NFKC can
        # compose them. `Mn`/`Mc`/`Me` is the full set of combining categories.
        while index < length and unicodedata.category(text[index]) in ("Mn", "Mc", "Me"):
            index += 1
        chunk = text[start:index]

        # Zero-width and other format characters are removed outright: they are
        # invisible to a reader, so they must be invisible to a rule.
        if all(unicodedata.category(character) == "Cf" for character in chunk):
            continue

        folded = unicodedata.normalize("NFKC", chunk)
        folded = _casefold(folded, dotted_i=dotted_i)
        folded = "".join(HOMOGLYPHS.get(character, character) for character in folded)

        if not folded:
            continue

        if folded.isspace():
            # Held, not emitted: a run collapses to one space, and a run at the
            # end of the string collapses to nothing at all.
            if pending_space_from is None:
                pending_space_from = start
            continue

        if pending_space_from is not None and out:
            out.append(" ")
            spans.append((pending_space_from, pending_space_from + 1))
        pending_space_from = None

        for character in folded:
            out.append(character)
            spans.append((start, index))

    return Normalized(text="".join(out), source=text, spans=tuple(spans), locale=locale)


def _casefold(value: str, *, dotted_i: bool) -> str:
    """`str.casefold`, with the one locale that disagrees with it handled.

    Turkish and Azeri keep the dot as a letter distinction: `İ` is the capital
    of `i`, and `I` is the capital of dotless `ı`. Python folds `İ` to `i̇`
    (an `i` with a combining dot) and `I` to `i`, which merges two letters that
    those languages keep apart. Folding them explicitly first is the whole fix.
    """
    if dotted_i:
        value = value.replace("İ", "i").replace("I", "ı")
    return value.casefold()


def normalized_text(text: str, *, locale: str = "en") -> str:
    """`normalize(...).text`, for callers that do not need the offset map."""
    return normalize(text, locale=locale).text


def trigram_similarity(left: str, right: str) -> float:
    """Dice coefficient over character trigrams, on already-folded strings.

    Used by the claim licence pass to decide whether a candidate span is one of
    a registered claim's surface forms (PRD §9.3). Dice rather than Jaccard
    because it is the measure Postgres' `pg_trgm` uses, and the threshold in
    `content_constants.yaml` is a number a person will eventually tune by
    comparing against what the database reports.

    Strings shorter than a trigram fall back to equality: padding them would
    invent shared trigrams out of the padding itself and score `ab` against
    `cd` far above zero.
    """
    if left == right:
        return 1.0
    if len(left) < 3 or len(right) < 3:
        return 0.0
    left_grams = {left[i : i + 3] for i in range(len(left) - 2)}
    right_grams = {right[i : i + 3] for i in range(len(right) - 2)}
    overlap = len(left_grams & right_grams)
    if not overlap:
        return 0.0
    return 2.0 * overlap / (len(left_grams) + len(right_grams))
