"""Folding, and the offset map back out of it (S3-P1 deliverable 4).

Two properties are load-bearing and get most of the attention here:

* the same word typed six different ways folds to one string, so one rule
  recognises all six;
* a span found in the folded string maps back to the characters the writer
  actually typed. A finding that underlines the wrong word is worse than one
  with no span at all — the writer looks at it, sees nothing wrong, and stops
  believing the linter.
"""

from __future__ import annotations

import pytest

from agent.guardrails.normalize import (
    HOMOGLYPHS,
    normalize,
    normalized_text,
    trigram_similarity,
)

#: Every one of these is "best" to a reader and a different byte sequence to
#: `re`. They are the real ways copy arrives — a bad paste, a designer's emoji,
#: a CJK keyboard — not hypothetical attacks.
DISGUISES = [
    ("plain", "best"),
    ("upper", "BEST"),
    ("mixed", "BeSt"),
    ("cyrillic-e", "bеst"),
    ("cyrillic-o-word", "bеst"),
    ("zero-width-joiner", "b‍e‍st"),
    ("zero-width-space", "b​e​st"),
    ("soft-hyphen", "b­es­t"),
    ("byte-order-mark", "﻿best"),
    ("fullwidth", "ｂｅｓｔ"),
    ("word-joiner", "be⁠st"),
]


@pytest.mark.parametrize(("label", "raw"), DISGUISES, ids=[label for label, _ in DISGUISES])
def test_every_disguise_of_a_word_folds_to_the_same_string(label: str, raw: str) -> None:
    assert normalized_text(raw) == "best", label


def test_fullwidth_digits_and_percent_fold() -> None:
    """A quantified-claim detector has to see `100%` in `１００％`."""
    assert normalized_text("１００％") == "100%"


def test_cyrillic_lookalikes_fold_to_latin() -> None:
    # `с`, `о`, `р`, `а`, `е`, `х` are the six that actually get pasted.
    assert normalized_text("сомрае") == "compae"


def test_smart_punctuation_folds() -> None:
    assert normalized_text("risk‑free") == "risk-free"
    assert normalized_text("world’s") == "world's"
    assert normalized_text("“best”") == '"best"'
    assert normalized_text("a — b") == "a - b"


def test_whitespace_collapses_and_ends_are_trimmed() -> None:
    assert normalized_text("  the   best \n\t product  ") == "the best product"
    assert normalized_text(" best ") == "best"
    assert normalized_text("   ") == ""
    assert normalized_text("") == ""


def test_combining_marks_compose_rather_than_splitting_the_word() -> None:
    """A decomposed umlaut must fold to the same string as a precomposed one,
    or a German pattern written with `ü` silently never matches it."""
    decomposed = "unübertroffen"
    precomposed = "unübertroffen"
    assert normalized_text(decomposed) == normalized_text(precomposed)


def test_eszett_folds_to_ss_and_the_offsets_survive_the_expansion() -> None:
    folded = normalize("straße")
    assert folded.text == "strasse"
    # One source character produced two folded ones; both point back at it.
    assert folded.source_span(4, 6) == (4, 5)
    assert folded.source[slice(*folded.source_span(4, 6))] == "ß"


# --- the offset map ---------------------------------------------------------


def test_a_span_maps_back_to_what_the_writer_typed() -> None:
    folded = normalize("The BEST product")
    start = folded.text.index("best")
    assert folded.source_span(start, start + 4) == (4, 8)
    assert folded.source[slice(*folded.source_span(start, start + 4))] == "BEST"


def test_a_span_maps_back_through_removed_zero_width_characters() -> None:
    """The characters were deleted from the folded text; the highlight still
    has to cover them, or it underlines half a word."""
    folded = normalize("b‍e​st deal")
    start = folded.text.index("best")
    assert folded.source[slice(*folded.source_span(start, start + 4))] == "b‍e​st"


def test_a_span_maps_back_through_collapsed_whitespace() -> None:
    folded = normalize("the    best   deal")
    start = folded.text.index("deal")
    assert folded.source[slice(*folded.source_span(start, start + 4))] == "deal"


def test_spans_are_parallel_to_the_folded_text() -> None:
    for _, raw in DISGUISES:
        folded = normalize(raw)
        assert len(folded.spans) == len(folded.text), raw


def test_an_out_of_range_span_is_clamped_not_raised() -> None:
    """A matcher that computed a span one past the end has a bug. Taking a
    whole lint run down over a highlight would be a worse one."""
    folded = normalize("best")
    assert folded.source_span(0, 99) == (0, 4)
    assert folded.source_span(-5, 2) == (0, 2)
    assert normalize("").source_span(0, 1) == (0, 0)


def test_the_source_is_kept_verbatim() -> None:
    raw = "  The ‍BEST  "
    assert normalize(raw).source == raw


# --- locale -----------------------------------------------------------------


def test_turkish_keeps_its_two_letter_i_apart() -> None:
    """`I` and `İ` are different letters in Turkish, and `str.casefold` merges
    them. Everything else in this module is locale-independent."""
    assert normalized_text("ISO", locale="tr") == "ıso"
    assert normalized_text("İSO", locale="tr") == "iso"
    assert normalized_text("ISO", locale="en") == "iso"


def test_a_locale_with_a_region_is_accepted() -> None:
    assert normalized_text("ISO", locale="tr-TR") == "ıso"
    assert normalized_text("ISO", locale="de_DE") == "iso"


def test_the_locale_is_recorded_on_the_result() -> None:
    assert normalize("best", locale="de").locale == "de"


# --- determinism ------------------------------------------------------------


def test_folding_is_idempotent() -> None:
    """Folding an already-folded string must change nothing, or a rule
    evaluated twice could disagree with itself."""
    for _, raw in DISGUISES:
        once = normalized_text(raw)
        assert normalized_text(once) == once, raw


def test_the_homoglyph_table_only_maps_onto_ascii() -> None:
    """A homoglyph that mapped onto another non-ASCII character would need a
    second pass to settle, and the table would stop being obviously finite."""
    for source, replacement in HOMOGLYPHS.items():
        assert replacement.isascii(), source
        assert source not in HOMOGLYPHS.values()


# --- trigram similarity -----------------------------------------------------


def test_identical_strings_score_one() -> None:
    assert trigram_similarity("safety data sheet", "safety data sheet") == 1.0


def test_a_near_miss_scores_high_and_an_unrelated_string_scores_low() -> None:
    assert trigram_similarity("the best sds software", "the best sds software tool") > 0.85
    assert trigram_similarity("the best sds software", "chemical inventory") < 0.1


def test_strings_shorter_than_a_trigram_fall_back_to_equality() -> None:
    """Padding them would invent shared trigrams out of the padding."""
    assert trigram_similarity("ab", "cd") == 0.0
    assert trigram_similarity("ab", "ab") == 1.0


def test_similarity_is_symmetric() -> None:
    left, right = "iso 9001 certified", "iso 9001 accredited"
    assert trigram_similarity(left, right) == trigram_similarity(right, left)
