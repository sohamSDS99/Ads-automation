"""`keywords.py` — the arithmetic and the guards behind stage 1.4."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from agent.nodes import keywords


def ids(count: int) -> list[uuid.UUID]:
    return [uuid.uuid4() for _ in range(count)]


# --- 1.4.1 -----------------------------------------------------------------


def test_the_same_phrase_spelled_three_ways_is_one_term() -> None:
    rows, _ = keywords.assemble(
        [
            keywords.Seed("  SDS Software ", "our_search_terms"),
            keywords.Seed("sds  software", "keyword_vendor_site"),
            keywords.Seed("sds software.", "competitor_ads"),
        ]
    )
    assert [row.term for row in rows] == ["sds software"]
    assert rows[0].sources == ("competitor_ads", "keyword_vendor_site", "our_search_terms")


def test_terms_many_sources_agree_on_survive_the_cap_first() -> None:
    rows, dropped = keywords.assemble(
        [
            keywords.Seed("everyone says this", "a"),
            keywords.Seed("everyone says this", "b"),
            keywords.Seed("only a vendor guessed", "a"),
        ],
        cap=1,
    )
    assert [row.term for row in rows] == ["everyone says this"]
    assert dropped == 1


def test_source_counts_are_per_term_not_per_seed() -> None:
    rows, _ = keywords.assemble(
        [
            keywords.Seed("alpha", "vendor"),
            keywords.Seed("alpha", "vendor"),
            keywords.Seed("beta", "vendor"),
            keywords.Seed("beta", "ads"),
        ]
    )
    assert keywords.source_counts(rows) == {"vendor": 2, "ads": 1}


def test_phrases_come_out_of_ad_copy_as_windows_not_whole_sentences() -> None:
    found = keywords.phrases("The best safety data sheet software for chemists")
    assert "safety data" in found
    assert "safety data sheet" in found
    assert all(len(phrase.split()) <= 4 for phrase in found)


# --- clustering ------------------------------------------------------------


def test_clustering_is_stable_across_runs() -> None:
    terms = ["sds software", "sds library", "sds management", "ghs labels", "ghs poster"]
    first = keywords.cluster_terms(terms, min_size=2)
    second = keywords.cluster_terms(list(reversed(terms)), min_size=2)
    assert [(item.key, item.terms) for item in first] == [(item.key, item.terms) for item in second]


def test_a_shared_phrase_beats_the_word_every_term_happens_to_contain() -> None:
    """The failure this exists to stop: one `sds` blob holding opposite intents."""
    terms = [
        "sds software pricing",
        "sds software demo",
        "sds software reviews",
        "free sds template download",
        "free sds template word",
        "free sds template excel",
    ]
    clusters = {item.key: item.terms for item in keywords.cluster_terms(terms, min_size=3)}
    assert set(clusters) == {"sds software", "free sds"}
    assert len(clusters["sds software"]) == 3
    assert len(clusters["free sds"]) == 3


def test_a_single_token_is_used_when_no_phrase_is_shared_widely_enough() -> None:
    terms = ["sds", "cheap sds", "sds for labs"]
    clusters = keywords.cluster_terms(terms, min_size=3)
    assert [item.key for item in clusters] == ["sds"]


def test_terms_too_scattered_to_group_land_in_one_named_bucket() -> None:
    clusters = keywords.cluster_terms(["alpha thing", "beta item", "gamma widget"], min_size=3)
    assert [item.key for item in clusters] == ["unclustered"]
    assert len(clusters[0].terms) == 3


# --- 1.4.3 -----------------------------------------------------------------


def metric(keyword: str, **extra: Any) -> dict[str, Any]:
    return {"keyword": keyword, "search_volume": 100, **extra}


def test_a_term_the_vendor_never_returned_is_unpriced_not_zero() -> None:
    """Zero volume and unknown volume are different findings."""
    rows, unpriced = keywords.join_demand(
        ["known term", "unknown term"], [metric("known term")], ids(1)
    )
    assert [row.term for row in rows] == ["known term"]
    assert unpriced == ["unknown term"]


def test_the_row_carrying_monthly_history_wins_over_a_bare_volume() -> None:
    rows, _ = keywords.join_demand(
        ["sds software"],
        [
            metric("sds software", search_volume=900),
            metric(
                "sds software",
                search_volume=100,
                monthly_searches=[{"year": 2025, "month": 1, "search_volume": 100}],
            ),
        ],
        ids(2),
    )
    assert rows[0].months_observed == 1
    assert rows[0].volume == 100


def test_seasonality_is_a_ratio_to_the_mean_month() -> None:
    monthly = [{"year": 2025, "month": 1, "search_volume": 200}] + [
        {"year": 2025, "month": month, "search_volume": 100} for month in range(2, 13)
    ]
    index = keywords.seasonality(monthly)
    assert len(index) == 12
    assert index[0] > 100 > index[1]
    assert index[1] == index[11]


def test_no_monthly_history_says_nothing_rather_than_saying_flat() -> None:
    assert keywords.seasonality([]) == ()


def test_year_on_year_needs_two_years_before_it_will_answer() -> None:
    one_year = [{"year": 2025, "month": month, "search_volume": 100} for month in range(1, 13)]
    assert keywords.trend_yoy(one_year) is None

    two_years = [{"year": 2024, "month": month, "search_volume": 100} for month in range(1, 13)] + [
        {"year": 2025, "month": month, "search_volume": 150} for month in range(1, 13)
    ]
    assert keywords.trend_yoy(two_years) == 50.0


def test_competition_arrives_as_a_label_or_a_float_and_leaves_as_a_label() -> None:
    rows, _ = keywords.join_demand(
        ["a", "b"], [metric("a", competition="high"), metric("b", competition=0.1)], ids(2)
    )
    by_term = {row.term: row.competition for row in rows}
    assert by_term == {"a": "HIGH", "b": "LOW"}


# --- 1.4.4 -----------------------------------------------------------------


def test_a_term_that_converts_is_never_blocked_however_many_sources_want_it() -> None:
    """The expensive failure this module exists to prevent."""
    rows, withheld = keywords.blocklist(
        lost_reason_terms=[("free sds template", "Students, not buyers")],
        irrelevant=[("free sds template", "Classified irrelevant")],
        protect=["Free SDS Template"],
    )
    assert rows == []
    assert withheld == ["free sds template"]


def test_only_the_negative_actions_become_negatives() -> None:
    rows, _ = keywords.blocklist(
        wasteful=[
            {"term": "sds jobs", "recommended_action": "negative_exact", "reason": "Job seekers"},
            {"term": "sds pricing", "recommended_action": "lower_bid", "reason": "Expensive"},
            {"term": "sds guide", "recommended_action": "fix_landing_page"},
        ]
    )
    assert [(row.term, row.match_type) for row in rows] == [("sds jobs", "exact")]


def test_the_source_that_watched_the_money_keeps_the_attribution() -> None:
    rows, _ = keywords.blocklist(
        wasteful=[
            {"term": "free sds", "recommended_action": "negative_phrase", "reason": "No revenue"}
        ],
        lost_reason_terms=[("free sds", "Students")],
        irrelevant=[("free sds", "Not a buyer")],
    )
    assert len(rows) == 1
    assert rows[0].source == "wasteful_terms"
    assert rows[0].match_type == "phrase"


# --- 1.4.5 -----------------------------------------------------------------


PAGES: list[dict[str, Any]] = [
    {
        "url": "https://sdsmanager.com/software",
        "title": "SDS software for chemical manufacturers",
        "h1": "SDS software",
        "h2": ["Why SDS software"],
        "meta_description": "Manage safety data sheets.",
        "text_excerpt": "software for managing sheets",
    },
    {
        "url": "https://sdsmanager.com/about",
        "title": "About us",
        "h1": "Our story",
        "h2": [],
        "meta_description": "Who we are.",
        "text_excerpt": "a company",
    },
]


def test_the_page_that_is_about_the_cluster_wins_and_the_score_is_explainable() -> None:
    clusters = [keywords.Cluster(key="software", terms=("sds software", "sds software pricing"))]
    match = keywords.map_clusters_to_pages(clusters, PAGES, ids(2))[0]

    assert match.best_url == "https://sdsmanager.com/software"
    assert match.verdict == "good_fit"
    assert "software" in match.matched_tokens
    # /about shares no token with the cluster, so it is not offered as a
    # second-best page. A runner-up that scored zero is not a runner-up.
    assert match.runner_up_url is None


def test_a_page_that_partly_overlaps_is_offered_as_the_runner_up() -> None:
    pages = [
        *PAGES,
        {
            "url": "https://sdsmanager.com/plans",
            "title": "Plans",
            "h1": "Pricing plans",
            "h2": [],
            "meta_description": "What it costs.",
            "text_excerpt": "monthly and annual",
        },
    ]
    clusters = [keywords.Cluster(key="software", terms=("sds software", "sds software pricing"))]
    match = keywords.map_clusters_to_pages(clusters, pages, ids(3))[0]
    assert match.best_url == "https://sdsmanager.com/software"
    assert match.runner_up_url == "https://sdsmanager.com/plans"


def test_a_cluster_no_page_speaks_to_is_a_gap_with_no_url() -> None:
    clusters = [keywords.Cluster(key="respirator", terms=("respirator fit testing",))]
    match = keywords.map_clusters_to_pages(clusters, PAGES, ids(2))[0]
    assert match.verdict == "gap"
    assert match.best_url is None
    assert match.evidence_ids == ()
    assert "respirator" in match.missing_tokens


def test_the_verdict_is_a_threshold_on_the_score_and_only_that() -> None:
    assert keywords.verdict_for(keywords.GOOD_FIT) == "good_fit"
    assert keywords.verdict_for(keywords.GOOD_FIT - 0.001) == "weak_fit"
    assert keywords.verdict_for(keywords.WEAK_FIT - 0.001) == "gap"


def test_pages_and_ids_must_be_parallel() -> None:
    with pytest.raises(ValueError, match="parallel"):
        keywords.map_clusters_to_pages([], PAGES, ids(1))
