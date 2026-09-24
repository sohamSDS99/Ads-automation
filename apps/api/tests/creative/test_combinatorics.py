"""`creative/combinatorics.py` — pairs, deterministic flags, one repair round, pins.

Google serves an RSA as any headlines beside any descriptions, so 4.2.3 checks
every pair it could serve (PRD §11 4.2.3). The flags here are code, blocking,
and cheap; the labels come from CLASSIFY. These tests pin `pair_flags_v1` —
the definition S4-P5 gives the six flags §11 names — and the repair and pin
rules the node runs on top of them.
"""

from __future__ import annotations

import pytest

from agent.creative import metrics
from agent.creative.combinatorics import (
    FLAGS,
    PAIR_FLAGS_V1,
    Asset,
    FlagRules,
    Pin,
    cta_verbs,
    enumerate_pairs,
    flags,
    pins_v1,
    repair_v1,
)

RULES = FlagRules(near_duplicate=0.80, cta_verbs=frozenset({"book", "start"}))


def headline(
    ref: str,
    text: str,
    category: str = "benefit",
    *,
    keyword_ref: str | None = None,
    claim_ids: tuple[str, ...] = (),
) -> Asset:
    return Asset(
        ref=ref,
        role="headline",
        text=text,
        category=category,
        keyword_ref=keyword_ref,
        claim_ids=claim_ids,
        claim_span=(0, len(text)) if claim_ids else None,
    )


def description(
    ref: str,
    text: str,
    *,
    claim_ids: tuple[str, ...] = ("c1",),
    span: tuple[int, int] | None = None,
) -> Asset:
    return Asset(
        ref=ref,
        role="description",
        text=text,
        claim_ids=claim_ids,
        claim_span=span if span is not None else (0, len(text)),
    )


# ---------------------------------------------------------------------------
# enumeration
# ---------------------------------------------------------------------------


def test_fifteen_headlines_and_four_descriptions_make_171_pairs() -> None:
    hs = [headline(f"h{i}", f"headline {i}") for i in range(15)]
    ds = [description(f"d{i}", f"description {i}") for i in range(4)]
    pairs = enumerate_pairs(hs, ds)

    kinds = [kind for _a, _b, kind in pairs]
    assert (kinds.count("HH"), kinds.count("HD"), kinds.count("DD")) == (105, 60, 6)
    assert kinds == sorted(kinds, key=("HH", "HD", "DD").index), "HH, then HD, then DD"
    keys = {frozenset((a.ref, b.ref)) for a, b, _kind in pairs}
    assert len(keys) == 171, "every unordered pair exactly once"
    assert all(a.ref != b.ref for a, b, _kind in pairs)
    assert all(a.role == "headline" and b.role == "description" for a, b, k in pairs if k == "HD")


def test_the_flag_vocabulary_and_its_version() -> None:
    assert PAIR_FLAGS_V1 == "combinatorics.pair_flags_v1"
    assert FLAGS == (
        "duplicate",
        "near_duplicate",
        "offer_conflict",
        "cta_collision",
        "claim_conflict",
        "keyword_stuffing",
    )


# ---------------------------------------------------------------------------
# duplicate and near_duplicate
# ---------------------------------------------------------------------------


def test_text_equal_after_normalising_is_a_duplicate_not_a_near_duplicate() -> None:
    assert flags(
        headline("a", "No IT Team Needed"), headline("b", "no it team needed!"), "HH", RULES
    ) == ("duplicate",)


def test_a_near_duplicate_is_flagged_at_the_threshold() -> None:
    a, b = headline("a", "Find Any SDS In Seconds"), headline("b", "Find Any SDS In A Second")
    assert flags(a, b, "HH", RULES) == ("near_duplicate",)
    assert flags(a, headline("c", "Audit-Ready Chemical Records"), "HH", RULES) == ()


# ---------------------------------------------------------------------------
# offer_conflict — two different offers in one impression
# ---------------------------------------------------------------------------


def test_two_different_discounts_are_an_offer_conflict() -> None:
    offer = headline("h", "Save 20% On Annual Plans", "offer")
    claim = "SDS updates within 24 hours."
    d = description("d", f"{claim} Now 30% off every annual plan.", span=(0, len(claim)))
    assert flags(offer, d, "HD", RULES) == ("offer_conflict",)


def test_the_same_discount_twice_is_not_a_conflict() -> None:
    claim = "SDS updates within 24 hours."
    d = description("d", f"{claim} Now 20% off every annual plan.", span=(0, len(claim)))
    assert flags(headline("h", "Save 20% On Annual Plans", "offer"), d, "HD", RULES) == ()


def test_two_different_prices_are_an_offer_conflict() -> None:
    a, b = (
        headline("a", "Plans From $49 A Month", "offer"),
        headline("b", "Now Only $59 Monthly", "offer"),
    )
    assert "offer_conflict" in flags(a, b, "HH", RULES)
    assert "offer_conflict" not in flags(a, headline("c", "Plans From $49", "offer"), "HH", RULES)


def test_a_percentage_inside_a_licensed_claim_is_not_an_offer() -> None:
    """ "Cut admin time by 40%" states the claim; it offers nothing."""
    proof = headline("p", "Cut Admin Time By 40%", "proof", claim_ids=("c9",))
    offer = headline("o", "Save 20% On Annual Plans", "offer")
    assert "offer_conflict" not in flags(proof, offer, "HH", RULES)


# ---------------------------------------------------------------------------
# claim_conflict — two licensed claims that disagree on a number
# ---------------------------------------------------------------------------


def test_two_claims_that_disagree_on_a_duration_conflict() -> None:
    a = headline("a", "SDS Updates Within 24 Hours", "proof", claim_ids=("c1",))
    b = description("d", "Every sheet refreshed within 48 hours of a change.", claim_ids=("c2",))
    assert flags(a, b, "HD", RULES) == ("claim_conflict",)


def test_the_same_claim_twice_or_a_claim_beside_no_claim_does_not_conflict() -> None:
    a = headline("a", "SDS Updates Within 24 Hours", "proof", claim_ids=("c1",))
    same = description("d", "Sheets refreshed within 48 hours of a change.", claim_ids=("c1",))
    unclaimed = headline("u", "Set Up In 2 Hours")
    assert "claim_conflict" not in flags(a, same, "HD", RULES)
    assert "claim_conflict" not in flags(a, unclaimed, "HH", RULES)


def test_claims_that_count_different_things_do_not_conflict() -> None:
    a = headline("a", "Trusted By 500 EHS Teams", "proof", claim_ids=("c1",))
    b = headline("b", "Over 900 Sites Covered", "proof", claim_ids=("c2",))
    c = headline("c", "Trusted By 900 EHS Teams", "proof", claim_ids=("c3",))
    assert "claim_conflict" not in flags(a, b, "HH", RULES)
    assert "claim_conflict" in flags(a, c, "HH", RULES)


# ---------------------------------------------------------------------------
# cta_collision — two different asks in one impression
# ---------------------------------------------------------------------------


def test_two_ctas_asking_for_different_actions_collide() -> None:
    book = headline("a", "Book Your Demo Today", "cta")
    start = headline("b", "Start Your Free Trial", "cta")
    assert flags(book, start, "HH", RULES) == ("cta_collision",)
    assert flags(book, headline("c", "Book A Walkthrough Now", "cta"), "HH", RULES) == ()


def test_a_description_ending_on_a_different_ask_collides_with_a_cta_headline() -> None:
    start = headline("h", "Start Your Free Trial", "cta")
    asks = description("d", "Keep every SDS current across sites. Book a demo today.")
    quiet = description("q", "Keep every SDS current across sites and teams.")
    same = description("s", "Keep every SDS current. Start your free trial now.")
    assert flags(start, asks, "HD", RULES) == ("cta_collision",)
    assert flags(start, quiet, "HD", RULES) == ()
    assert flags(start, same, "HD", RULES) == ()


def test_cta_verbs_are_the_leading_words_of_the_pools_cta_headlines() -> None:
    pool = [
        headline("a", "Book Your Demo Today", "cta"),
        headline("b", "Start Your Free Trial", "cta"),
        headline("c", "Find Any SDS In Seconds", "benefit"),
    ]
    assert cta_verbs(pool) == frozenset({"book", "start"})


# ---------------------------------------------------------------------------
# keyword_stuffing — a keyword headline's keyword again beside it
# ---------------------------------------------------------------------------


def test_a_keyword_headlines_keyword_repeated_in_another_headline_is_stuffing() -> None:
    carrier = headline(
        "k", "SDS Management Software", "keyword", keyword_ref="sds management software"
    )
    again = headline("b", "Why Teams Choose SDS Management Software")
    other = headline("c", "SDS Software For Teams")
    assert flags(carrier, again, "HH", RULES) == ("keyword_stuffing",)
    assert flags(again, carrier, "HH", RULES) == ("keyword_stuffing",)
    assert flags(carrier, other, "HH", RULES) == ()


def test_keyword_stuffing_is_judged_on_headline_pairs_only() -> None:
    carrier = headline(
        "k", "SDS Management Software", "keyword", keyword_ref="sds management software"
    )
    d = description("d", "SDS management software that keeps every sheet current.")
    assert "keyword_stuffing" not in flags(carrier, d, "HD", RULES)


def test_flags_come_back_in_vocabulary_order() -> None:
    a = headline("a", "Book Now: Save 20%", "cta")
    b = headline("b", "Start Now: Save 30%", "cta")
    result = flags(a, b, "HH", RULES)
    assert list(result) == sorted(result, key=FLAGS.index)
    assert set(result) >= {"offer_conflict", "cta_collision"}


# ---------------------------------------------------------------------------
# repair — exactly one round, from reserves
# ---------------------------------------------------------------------------

QUOTAS = {"offer": 1, "benefit": 1, "cta": 1}


def _ad() -> tuple[list[Asset], list[Asset]]:
    claim = "SDS updates within 24 hours."
    hs = [
        headline("h-offer", "Save 20% On Annual Plans", "offer"),
        headline("h-benefit", "Find Any SDS In Seconds", "benefit"),
        headline("h-cta", "Book Your Demo Today", "cta"),
    ]
    ds = [
        description("d-offer", f"{claim} Now 30% off every annual plan.", span=(0, len(claim))),
        description("d-plain", "Keep every SDS current across every site you run."),
    ]
    return hs, ds


def _bad(hs: list[Asset], ds: list[Asset]) -> dict[tuple[str, str], tuple[str, ...]]:
    return {
        (a.ref, b.ref): found
        for a, b, kind in enumerate_pairs(hs, ds)
        if (found := flags(a, b, kind, RULES))
    }


def test_a_planted_offer_conflict_is_swapped_from_reserve() -> None:
    hs, ds = _ad()
    bad = _bad(hs, ds)
    assert bad == {("h-offer", "d-offer"): ("offer_conflict",)}
    reserve = [
        headline("r-near", "Save 20% On Annual Plan", "offer"),  # still 20% vs 30%
        headline("r-offer", "Plans For Every Team Size", "offer"),
        headline("r-benefit", "Audit-Ready Chemical Records", "benefit"),
    ]
    repaired = repair_v1(hs, ds, reserve, [], bad, quotas=QUOTAS, rules=RULES)

    assert [(s.out, s.into) for s in repaired.swaps] == [("h-offer", "r-offer")]
    assert "offer_conflict" in repaired.swaps[0].why and "d-offer" in repaired.swaps[0].why
    assert [a.ref for a in repaired.headlines] == ["r-offer", "h-benefit", "h-cta"]
    assert repaired.unresolved == ()
    assert _bad(list(repaired.headlines), list(repaired.descriptions)) == {}


def test_a_swap_keeps_the_category_while_its_quota_has_no_slack() -> None:
    hs, ds = _ad()
    reserve = [headline("r-benefit", "Audit-Ready Chemical Records", "benefit")]
    repaired = repair_v1(hs, ds, reserve, [], _bad(hs, ds), quotas=QUOTAS, rules=RULES)
    # No offer reserve: the headline cannot go without breaking the offer quota,
    # so the description goes — and there is no description reserve either.
    assert repaired.swaps == ()
    assert repaired.unresolved == (("h-offer", "d-offer"),)


def test_a_category_with_slack_may_be_replaced_by_another_category() -> None:
    hs, ds = _ad()
    reserve = [headline("r-benefit", "Audit-Ready Chemical Records", "benefit")]
    repaired = repair_v1(hs, ds, reserve, [], _bad(hs, ds), quotas={"offer": 0}, rules=RULES)
    assert [(s.out, s.into) for s in repaired.swaps] == [("h-offer", "r-benefit")]


def test_a_description_is_swapped_when_no_headline_can_be() -> None:
    hs, ds = _ad()
    d_reserve = [description("r-desc", "Every sheet current in one searchable library.")]
    repaired = repair_v1(hs, ds, [], d_reserve, _bad(hs, ds), quotas=QUOTAS, rules=RULES)
    assert [(s.out, s.into) for s in repaired.swaps] == [("d-offer", "r-desc")]
    assert repaired.unresolved == ()


def test_a_labelled_pair_is_repaired_like_a_flagged_one() -> None:
    hs, ds = _ad()
    bad = {("h-benefit", "d-plain"): ("redundant",)}
    reserve = [headline("r-benefit", "Audit-Ready Chemical Records", "benefit")]
    repaired = repair_v1(hs, ds, reserve, [], bad, quotas=QUOTAS, rules=RULES)
    assert [(s.out, s.into, s.why) for s in repaired.swaps] == [
        ("h-benefit", "r-benefit", "redundant with d-plain")
    ]


def test_a_reserve_that_would_bring_a_flag_of_its_own_is_passed_over() -> None:
    """`r-deal` is the more diverse offer reserve, and it conflicts with d-offer."""
    hs, ds = _ad()
    deal = headline("r-deal", "Quarterly Deal: 25% Off", "offer")
    plain = headline("r-offer", "Book Plans For Your Team", "offer")
    staying = [h.text for h in hs[1:]]

    def closest(asset: Asset) -> float:
        return min(1 - metrics.similarity(asset.text, text) for text in staying)

    assert closest(deal) > closest(plain), "the flagged reserve must be the diverse pick"
    assert "offer_conflict" in flags(deal, ds[0], "HD", RULES)

    repaired = repair_v1(hs, ds, [deal, plain], [], _bad(hs, ds), quotas=QUOTAS, rules=RULES)
    assert [s.into for s in repaired.swaps] == ["r-offer"]


def test_one_round_never_swaps_back_in_what_it_swapped_out() -> None:
    """`h4` leaves first; when `h2` leaves, `h4` is the most diverse reserve left."""
    h1, h2, h3 = (
        headline("h1", "alpha one"),
        headline("h2", "bravo two"),
        headline("h3", "charlie three"),
    )
    h4 = headline("h4", "zulu yankee xray")
    r1 = headline("r1", "quantum ledger vortex")
    r2 = headline("r2", "alpha two three")
    bad = {("h1", "h2"): ("redundant",), ("h3", "h4"): ("redundant",)}

    repaired = repair_v1([h1, h2, h3, h4], [], [r1, h4, r2], [], bad, quotas={}, rules=RULES)

    assert [(s.out, s.into) for s in repaired.swaps] == [("h4", "r1"), ("h2", "r2")]
    assert repaired.unresolved == ()


def test_the_asset_in_the_most_bad_pairs_goes_first() -> None:
    offer = headline("h-offer", "Save 20% On Annual Plans", "offer")
    hs = [
        offer,
        headline("h2", "Save 30% On Monthly Plans", "benefit"),
        headline("h3", "Book A Demo", "cta"),
    ]
    ds = [description("d1", "Now 40% off for new teams.", span=(0, 0))]
    bad = _bad(hs, ds)
    assert set(bad) == {("h-offer", "h2"), ("h-offer", "d1"), ("h2", "d1")}
    reserve = [
        headline("r1", "Plans For Every Team Size", "offer"),
        headline("r2", "Audit-Ready Chemical Records", "benefit"),
    ]
    repaired = repair_v1(hs, ds, reserve, [], bad, quotas={"offer": 1}, rules=RULES)
    # Every asset is in two bad pairs; headlines go before descriptions, and the
    # later headline (the less diverse pick) before the earlier.
    assert [s.out for s in repaired.swaps][:1] == ["h2"]


# ---------------------------------------------------------------------------
# pins — only for order_dependent pairs
# ---------------------------------------------------------------------------


def test_an_order_dependent_pair_is_pinned_in_the_order_it_reads() -> None:
    a, b = headline("a", "Need SDS Software?"), headline("b", "Try It Free For A Week")
    d = description("d", "Then keep every sheet current.")
    assert pins_v1([(a, b, "HH")]) == (
        Pin(asset_ref="a", position="H1", why="order_dependent with b (HH)"),
        Pin(asset_ref="b", position="H2", why="order_dependent with a (HH)"),
    )
    assert [(p.asset_ref, p.position) for p in pins_v1([(a, d, "HD")])] == [
        ("a", "H1"),
        ("d", "D1"),
    ]


def test_a_pair_whose_pins_contradict_earlier_ones_is_not_pinned() -> None:
    a, b, c = headline("a", "one"), headline("b", "two"), headline("c", "three")
    pins = pins_v1([(a, b, "HH"), (b, c, "HH"), (c, headline("e", "four"), "HH")])
    assert [(p.asset_ref, p.position) for p in pins] == [
        ("a", "H1"),
        ("b", "H2"),
        ("c", "H1"),
        ("e", "H2"),
    ]


@pytest.mark.parametrize("kind", ["HH", "HD", "DD"])
def test_no_asset_is_pinned_twice(kind: str) -> None:
    a = headline("a", "one") if kind != "DD" else description("a", "one")
    b = headline("b", "two") if kind == "HH" else description("b", "two")
    pins = pins_v1([(a, b, kind), (a, b, kind)])  # type: ignore[list-item]
    assert len(pins) == len({p.asset_ref for p in pins}) == 2
