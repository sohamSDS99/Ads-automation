"""`creative/metrics.py` — copy similarity and distinctness, pure and versioned.

Every expectation here is hand-computed: the metric is a definition, and a
definition that drifted would silently re-grade every stored near-duplicate
and every variant B, so the numbers are pinned to the id that names them.
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from agent.creative import metrics


def test_the_metric_ids_are_versioned_and_fixed() -> None:
    assert metrics.TRIGRAM_V1 == "copy.trigram_v1"
    assert metrics.DISTINCTNESS_V1 == "copy.distinctness_v1"


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("SDS Software", "sds software"),
        ("  SDS—Software!! ", "sds software"),
        ("ＳＤＳ　Ｓｏｆｔｗａｒｅ", "sds software"),  # full-width folds under NFKC
        ("Keep_every SDS", "keep every sds"),
        ("Save 20% now", "save 20 now"),
        ("", ""),
        ("!!!", ""),
    ],
)
def test_normalize_folds_case_width_and_punctuation(raw: str, normalized: str) -> None:
    assert metrics.normalize(raw) == normalized


def test_trigrams_are_taken_over_the_padded_normalized_text() -> None:
    assert metrics.trigrams("abc") == frozenset({" ab", "abc", "bc "})
    assert metrics.trigrams("A") == frozenset({" a "})
    assert metrics.trigrams("") == frozenset()


def test_similarity_is_the_dice_coefficient_of_the_trigram_sets() -> None:
    # {" ab","abc","bc "} vs {" ab","abd","bd "}: one shared of six → 2·1/6.
    assert metrics.similarity_ratio("abc", "abd") == Fraction(1, 3)
    assert metrics.similarity("abc", "abd") == pytest.approx(1 / 3)


def test_texts_equal_after_normalizing_are_identical() -> None:
    assert metrics.similarity("SDS Software", "sds software!") == 1.0


def test_similarity_is_symmetric_and_bounded() -> None:
    a, b = "Free SDS management trial", "Book an SDS software demo"
    assert metrics.similarity(a, b) == metrics.similarity(b, a)
    assert 0.0 < metrics.similarity(a, b) < 1.0
    assert metrics.similarity("abc", "xyz") == 0.0


def test_empty_text_is_like_only_empty_text() -> None:
    assert metrics.similarity("", "") == 1.0
    assert metrics.similarity("", "abc") == 0.0
    assert metrics.similarity("!!!", "") == 1.0


def test_a_plural_is_a_near_duplicate_and_a_new_idea_is_not() -> None:
    assert metrics.similarity("Free SDS Management Trial", "Free SDS Management Trials") >= 0.8
    assert metrics.similarity("SDS Software For Teams", "Keep Every Safety Sheet Current") < 0.8


def test_distinctness_of_two_texts_is_one_minus_their_similarity() -> None:
    a, b = "Free SDS management trial", "Book an SDS software demo"
    assert metrics.distinctness([a], [b]) == pytest.approx(1 - metrics.similarity(a, b))
    assert metrics.distinctness([a], [a]) == 0.0


def test_distinctness_of_two_ads_averages_each_assets_closest_match_both_ways() -> None:
    """1 − ½(mean over A of its best match in B + mean over B of its best in A)."""
    a = ["abc", "xyz"]
    b = ["abd"]
    # A→B: abc↔abd = 1/3, xyz↔abd = 0 → mean 1/6.  B→A: abd's best is abc = 1/3.
    expected = 1 - Fraction(1, 2) * (Fraction(1, 6) + Fraction(1, 3))
    assert metrics.distinctness_ratio(a, b) == expected
    assert metrics.distinctness(a, b) == pytest.approx(float(expected))


def test_distinctness_ignores_order_and_is_symmetric() -> None:
    a = ["Keep every SDS current", "Free SDS management trial", "Book a demo"]
    b = ["SDS software for teams", "Start your free trial"]
    assert metrics.distinctness(a, b) == metrics.distinctness(list(reversed(a)), b)
    assert metrics.distinctness(a, b) == metrics.distinctness(b, a)


def test_a_paraphrased_ad_is_not_distinct_and_a_different_ad_is() -> None:
    a = ["Keep Every SDS Current", "Free SDS Management Trial", "Book An SDS Demo Today"]
    paraphrase = ["Keep Every SDS Current Now", "Free SDS Management Trials", "Book SDS Demo Today"]
    different = [
        "Chemical Inventory In One Place",
        "Audit-Ready In Minutes",
        "Talk To A Specialist",
    ]
    assert metrics.distinctness(a, paraphrase) < 0.65 <= metrics.distinctness(a, different)


def test_distinctness_of_empty_sides() -> None:
    assert metrics.distinctness([], []) == 0.0
    assert metrics.distinctness(["abc"], []) == 1.0
