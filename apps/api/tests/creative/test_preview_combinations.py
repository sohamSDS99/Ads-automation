"""`creative/preview_combinations.py` — which RSA combinations 4.6.4 renders (PRD §11 4.6.4).

"The three highest-likelihood combinations plus the longest-string combination
of every RSA." Google fills H1–H3 and D1–D2 from the ad's assets; an asset
pinned to a position serves only there, and a position with a pin takes only
its pinned assets. With no performance history every valid assignment is
equally likely (`likelihood_v1`), so the three are the most likely ones in the
slate's own order, each led by a different asset where the slate allows it.
"""

from __future__ import annotations

from fractions import Fraction

from agent.creative.preview_combinations import Piece, plan


def _heads(*texts: str, pins: dict[int, str] | None = None) -> list[Piece]:
    pins = pins or {}
    return [Piece(ref=f"h{i}", text=text, pin=pins.get(i)) for i, text in enumerate(texts)]


def _descs(*texts: str, pins: dict[int, str] | None = None) -> list[Piece]:
    pins = pins or {}
    return [Piece(ref=f"d{i}", text=text, pin=pins.get(i)) for i, text in enumerate(texts)]


def test_three_likely_combinations_are_distinct_and_each_leads_with_a_different_headline() -> None:
    combos = plan(_heads("a", "b", "c", "d", "e"), _descs("x", "y", "z"))

    likely = [c for c in combos if any(r.startswith("likely_") for r in c.roles)]
    assert [c.roles[0] for c in likely] == ["likely_1", "likely_2", "likely_3"]
    assert likely[0].headlines == ("h0", "h1", "h2")
    assert likely[0].descriptions == ("d0", "d1")
    assert len({c.headlines + c.descriptions for c in likely}) == 3
    assert [c.headlines[0] for c in likely] == ["h0", "h1", "h2"]


def test_a_pinned_headline_holds_its_position_and_serves_nowhere_else() -> None:
    combos = plan(_heads("a", "b", "c", "d", pins={2: "H1"}), _descs("x", "y"))

    for combo in combos:
        assert combo.headlines[0] == "h2"
        assert "h2" not in combo.headlines[1:]


def test_a_position_pinned_by_two_assets_rotates_between_them_only() -> None:
    combos = plan(_heads("a", "b", "c", "d", pins={0: "H2", 3: "H2"}), _descs("x", "y"))

    assert {c.headlines[1] for c in combos} <= {"h0", "h3"}
    assert all(c.headlines[0] not in {"h0", "h3"} for c in combos)


def test_the_longest_combination_takes_the_longest_asset_each_position_admits() -> None:
    heads = _heads("short", "a much longer headline", "mid length", "pinned", "pinned longer one",
                   pins={3: "H1", 4: "H1"})  # fmt: skip
    descs = _descs("tiny", "the longest description here", "medium description")

    (longest,) = [c for c in plan(heads, descs) if "longest" in c.roles]

    # H1 admits only its pinned assets; H2/H3 the longest unpinned ones.
    assert longest.headlines == ("h4", "h1", "h2")
    assert longest.descriptions == ("d1", "d2")


def test_a_longest_combination_equal_to_a_likely_one_is_rendered_once_with_both_roles() -> None:
    combos = plan(_heads("a", "b", "c"), _descs("x", "y"))

    # Every asset is one character long: the longest is the slate's order.
    assert combos[0].roles == ("likely_1", "longest")
    assert [c for c in combos if "longest" in c.roles] == [combos[0]]
    assert len(combos) == len({c.headlines + c.descriptions for c in combos})


def test_too_few_assets_leave_the_trailing_positions_empty() -> None:
    combos = plan(_heads("a", "b"), _descs("x"))

    # h0 then h1, and h1 then h0: two different ads Google could serve.
    assert [c.headlines for c in combos] == [("h0", "h1", None), ("h1", "h0", None)]
    assert all(c.descriptions == ("d0", None) for c in combos)
    assert combos[0].roles == ("likely_1", "longest")
    assert combos[0].likelihood == Fraction(1, 2)


def test_one_headline_and_one_description_is_one_combination() -> None:
    (only,) = plan(_heads("a"), _descs("x"))

    assert only.headlines == ("h0", None, None) and only.descriptions == ("d0", None)
    assert only.roles == ("likely_1", "longest")


def test_likelihood_is_uniform_over_every_valid_assignment() -> None:
    # H1..H3 from 4 unpinned headlines: 4*3*2 = 24; D1..D2 from 2: 2*1 = 2.
    combos = plan(_heads("a", "b", "c", "d"), _descs("x", "y"))
    assert {c.likelihood for c in combos} == {Fraction(1, 48)}

    # H1 pinned by two: 2 choices; H2..H3 from 3 unpinned: 3*2 = 6; D: 2.
    pinned = plan(_heads("a", "b", "c", "d", "e", pins={0: "H1", 1: "H1"}), _descs("x", "y"))
    assert {c.likelihood for c in pinned} == {Fraction(1, 24)}


def test_the_plan_is_deterministic() -> None:
    heads = _heads("a", "b", "c", "d", "e", "f", pins={5: "H3"})
    descs = _descs("x", "y", "z", pins={2: "D1"})
    assert plan(heads, descs) == plan(list(heads), list(descs))


def test_no_headlines_means_nothing_to_render() -> None:
    assert plan([], _descs("x", "y")) == ()
