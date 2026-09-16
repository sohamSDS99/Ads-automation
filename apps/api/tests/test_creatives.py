"""`creatives.py` — the arithmetic behind stage 1.3.

These are the numbers a model never sees before they are final, so the tests
are about the values themselves rather than about anything being wired up.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from agent.nodes import creatives


def ids(count: int) -> list[uuid.UUID]:
    return [uuid.uuid4() for _ in range(count)]


def creative(
    advertiser: str = "Acme",
    *,
    ad_id: str | None = "CR1",
    text: str = "Manage your SDS library",
    first: str | None = "2025-01-04",
    last: str | None = "2025-03-20",
    screenshot: str | None = "creatives/run/acme.png",
) -> dict[str, Any]:
    return {
        "advertiser": advertiser,
        "ad_id": ad_id,
        "format": "text",
        "first_shown": first,
        "last_shown": last,
        "creative_text": text,
        "destination_url": "https://acme.com/sds",
        "screenshot_path": screenshot,
    }


# --- 1.3.1 -----------------------------------------------------------------


def test_our_own_domain_is_never_a_competitor() -> None:
    rows, _, _ = creatives.competitor_rows(
        domain_rows=[
            {"competitor_domain": "https://www.sdsmanager.com/", "intersections": 90},
            {"competitor_domain": "rival.com", "intersections": 30},
        ],
        domain_ids=ids(2),
        serp_rows=[],
        serp_ids=[],
        our_domain="sdsmanager.com",
    )
    assert [row.domain for row in rows] == ["rival.com"]


def test_overlap_is_relative_to_the_strongest_competitor_in_the_set() -> None:
    rows, _, _ = creatives.competitor_rows(
        domain_rows=[
            {"competitor_domain": "big.com", "intersections": 100},
            {"competitor_domain": "small.com", "intersections": 25},
        ],
        domain_ids=ids(2),
        serp_rows=[],
        serp_ids=[],
        our_domain="us.com",
    )
    by_domain = {row.domain: row for row in rows}
    # Only the keyword signal exists, so its weight renormalises to 1.0 rather
    # than capping the strongest competitor at 60 out of 100.
    assert by_domain["big.com"].overlap_score == 100.0
    assert by_domain["small.com"].overlap_score == 25.0
    assert by_domain["big.com"].overlap_basis == ("paid_keywords",)


def test_a_serp_only_project_still_scores_out_of_one_hundred() -> None:
    rows, _, checked = creatives.competitor_rows(
        domain_rows=[],
        domain_ids=[],
        serp_rows=[
            {"keyword": "sds software", "results": [{"domain": "rival.com"}]},
            {"keyword": "sds library", "results": [{"domain": "rival.com"}]},
        ],
        serp_ids=ids(2),
        our_domain="us.com",
        our_terms={"sds software", "sds library"},
    )
    assert checked == 2
    assert rows[0].overlap_score == 100.0
    assert rows[0].overlap_basis == ("serp",)


def test_a_serp_for_a_term_we_do_not_bid_on_is_not_counted_either_way() -> None:
    """It is not a hit, and it is not in the denominator that hits are read against."""
    rows, _, checked = creatives.competitor_rows(
        domain_rows=[],
        domain_ids=[],
        serp_rows=[
            {"keyword": "sds software", "results": [{"domain": "rival.com"}]},
            {"keyword": "unrelated hobby", "results": [{"domain": "noise.com"}]},
        ],
        serp_ids=ids(2),
        our_domain="us.com",
        our_terms={"sds software"},
    )
    assert checked == 1
    assert [row.domain for row in rows] == ["rival.com"]


def test_the_same_domain_twice_on_one_serp_is_one_hit() -> None:
    rows, _, _ = creatives.competitor_rows(
        domain_rows=[],
        domain_ids=[],
        serp_rows=[
            {
                "keyword": "sds software",
                "results": [{"domain": "rival.com"}, {"domain": "www.rival.com"}],
            }
        ],
        serp_ids=ids(1),
        our_domain="us.com",
        our_terms={"sds software"},
    )
    assert rows[0].serp_hits == 1


def test_parallel_inputs_are_enforced_rather_than_misattributed() -> None:
    with pytest.raises(ValueError, match="parallel"):
        creatives.competitor_rows(
            domain_rows=[{"competitor_domain": "a.com"}],
            domain_ids=ids(2),
            serp_rows=[],
            serp_ids=[],
            our_domain="us.com",
        )


# --- 1.3.2 -----------------------------------------------------------------


def test_the_same_ad_reached_through_two_searches_is_one_ad() -> None:
    rows, dropped = creatives.creative_rows(
        [creative(ad_id="CR7"), creative(ad_id="CR7")], ids(2), max_ads=50
    )
    assert len(rows) == 1
    assert dropped == 0


def test_an_ad_with_no_id_is_deduped_on_its_text_and_advertiser() -> None:
    rows, _ = creatives.creative_rows(
        [
            creative(ad_id=None, text="Same words"),
            creative(ad_id=None, text="Same words"),
            creative("Other", ad_id=None, text="Same words"),
        ],
        ids(3),
        max_ads=50,
    )
    assert len(rows) == 2


def test_the_cap_reports_what_it_dropped() -> None:
    rows, dropped = creatives.creative_rows(
        [creative(ad_id=f"CR{index}") for index in range(10)], ids(10), max_ads=4
    )
    assert (len(rows), dropped) == (4, 6)


def test_the_screenshot_key_survives_into_the_row() -> None:
    rows, _ = creatives.creative_rows([creative()], ids(1), max_ads=5)
    assert rows[0].screenshot_path == "creatives/run/acme.png"


def test_corpus_stats_counts_what_a_model_must_not_be_asked_to_count() -> None:
    rows, _ = creatives.creative_rows(
        [
            creative("Acme", ad_id="CR1"),
            creative("Acme", ad_id="CR2"),
            creative("Rival", ad_id="CR3", screenshot=None),
        ],
        ids(3),
        max_ads=50,
    )
    stats = creatives.corpus_stats(rows)
    assert stats["ads"] == 3
    assert stats["advertisers"] == 2
    assert stats["ads_per_advertiser"] == {"Acme": 2, "Rival": 1}
    assert stats["with_screenshot"] == 2
    assert stats["first_seen"] == "2025-01-04"


def test_a_theme_for_an_ad_that_is_not_in_the_corpus_is_dropped() -> None:
    rows, _ = creatives.creative_rows([creative(ad_id="CR1")], ids(1), max_ads=5)
    clusters = creatives.cluster_frequency(
        {"CR1": "compliance fear", "CR-does-not-exist": "invented"}, rows
    )
    assert [item["theme"] for item in clusters] == ["compliance fear"]
    assert clusters[0]["frequency"] == 1
    assert clusters[0]["share_pct"] == 100.0


# --- 1.3.3 -----------------------------------------------------------------


def test_an_ad_running_across_the_new_year_is_live_in_every_month_it_spans() -> None:
    rows, _ = creatives.creative_rows(
        [creative(first="2024-11-10", last="2025-02-08")], ids(1), max_ads=5
    )
    counts = creatives.months_live(rows)
    # November, December, January, February.
    assert [index + 1 for index, value in enumerate(counts) if value] == [1, 2, 11, 12]


def test_peak_months_are_the_months_at_or_near_the_busiest() -> None:
    rows, _ = creatives.creative_rows(
        [
            creative(ad_id="CR1", first="2025-03-01", last="2025-03-31"),
            creative(ad_id="CR2", first="2025-03-01", last="2025-03-31"),
            creative(ad_id="CR3", first="2025-08-01", last="2025-08-31"),
        ],
        ids(3),
        max_ads=5,
    )
    assert creatives.peak_months(rows) == (3,)


def test_a_vendor_traffic_cost_produces_a_banded_estimate_and_says_so() -> None:
    competitors, _, _ = creatives.competitor_rows(
        domain_rows=[
            {
                "competitor_domain": "rival.com",
                "intersections": 10,
                "paid_estimated_traffic_cost": 10_000,
            }
        ],
        domain_ids=ids(1),
        serp_rows=[],
        serp_ids=[],
        our_domain="us.com",
    )
    estimate = creatives.spend_estimates(competitors, [])[0]
    assert estimate.method == "paid_traffic_cost"
    assert estimate.confidence == "medium"
    assert (estimate.est_monthly_spend_low, estimate.est_monthly_spend_high) == (6000.0, 16000.0)
    assert estimate.basis["vendor_monthly_paid_traffic_cost"] == 10_000.0


def test_with_no_spend_signal_the_method_degrades_and_confidence_goes_low() -> None:
    rows, _ = creatives.creative_rows(
        [creative("Rival", ad_id=f"CR{index}") for index in range(3)], ids(3), max_ads=10
    )
    estimate = creatives.spend_estimates([], rows)[0]
    assert estimate.method == "creative_volume"
    assert estimate.confidence == "low"
    assert estimate.creatives == 3
    assert estimate.basis["live_creatives"] == 3


def test_with_no_signal_at_all_the_estimate_refuses_to_be_a_number() -> None:
    competitors, _, _ = creatives.competitor_rows(
        domain_rows=[{"competitor_domain": "quiet.com", "intersections": 5}],
        domain_ids=ids(1),
        serp_rows=[],
        serp_ids=[],
        our_domain="us.com",
    )
    estimate = creatives.spend_estimates(competitors, [])[0]
    assert estimate.method == "insufficient_evidence"
    assert estimate.est_monthly_spend_low is None
    assert estimate.est_monthly_spend_high is None


def test_every_estimate_carries_the_method_that_produced_it() -> None:
    """PRD §10 1.3.3: "must state method; never present as fact"."""
    competitors, _, _ = creatives.competitor_rows(
        domain_rows=[
            {"competitor_domain": "a.com", "intersections": 5, "paid_estimated_traffic_cost": 900},
            {"competitor_domain": "b.com", "intersections": 5},
        ],
        domain_ids=ids(2),
        serp_rows=[],
        serp_ids=[],
        our_domain="us.com",
    )
    rows, _ = creatives.creative_rows([creative("b.com", ad_id="CR9")], ids(1), max_ads=5)
    for estimate in creatives.spend_estimates(competitors, rows):
        assert estimate.method in {"paid_traffic_cost", "creative_volume", "insufficient_evidence"}
        assert estimate.basis, "an estimate with no stated basis is a fact claim"


def test_a_scraped_name_and_a_vendor_domain_are_one_company_not_two() -> None:
    """Without the alias, Chemwatch gets a spend range with no ads under it."""
    competitors, _, _ = creatives.competitor_rows(
        domain_rows=[
            {
                "competitor_domain": "chemwatch.net",
                "intersections": 40,
                "paid_estimated_traffic_cost": 5_000,
            }
        ],
        domain_ids=ids(1),
        serp_rows=[],
        serp_ids=[],
        our_domain="us.com",
    )
    rows, _ = creatives.creative_rows(
        [creative("Chemwatch", ad_id=f"CR{index}") for index in range(4)], ids(4), max_ads=10
    )

    unaliased = creatives.spend_estimates(competitors, rows)
    assert len(unaliased) == 2

    aliased = creatives.spend_estimates(competitors, rows, aliases={"Chemwatch": "chemwatch.net"})
    assert len(aliased) == 1
    assert aliased[0].competitor == "chemwatch.net"
    assert aliased[0].method == "paid_traffic_cost"
    assert aliased[0].creatives == 4
