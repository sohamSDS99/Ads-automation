"""The Transparency Center scrape, exercised without a browser.

`parse_ad_cards` is pure, so a saved page *is* the cassette — and a page saved
on the day the selectors broke replays that break forever. These fixtures cover
the three states the connector has to tell apart: a normal grid, an advertiser
with genuinely no ads, and selectors that have moved.
"""

from __future__ import annotations

from collections.abc import Callable

from agent.connectors import selectors
from agent.connectors.transparency import DATE_RANGE, parse_ad_cards

Fixture = Callable[[str], str]


def cards(fixture_text: Fixture, name: str = "transparency_grid.html") -> list[dict]:
    return parse_ad_cards(fixture_text(name), advertiser="Chemwatch", region="US")


def test_every_card_on_the_page_is_found(fixture_text: Fixture) -> None:
    assert len(cards(fixture_text)) == 3


def test_the_creative_id_is_read_from_the_attribute(fixture_text: Fixture) -> None:
    first = cards(fixture_text)[0]
    assert first["ad_id"] == "CR01234567890123456789"


def test_creative_text_and_destination_are_captured(fixture_text: Fixture) -> None:
    first = cards(fixture_text)[0]
    assert "Compliance without the binders" in first["creative_text"]
    assert first["destination_url"].startswith("https://chemwatch.net/sds/")


def test_a_closed_date_range_parses_to_iso(fixture_text: Fixture) -> None:
    first = cards(fixture_text)[0]
    assert first["first_shown"] == "2025-01-03"
    assert first["last_shown"] == "2025-02-09"


def test_a_still_running_ad_has_no_last_shown(fixture_text: Fixture) -> None:
    """ "Present" is not a date. Storing today's date would invent an end that has not happened."""
    running = cards(fixture_text)[1]
    assert running["first_shown"] == "2025-03-11"
    assert running["last_shown"] is None


def test_the_image_url_is_captured(fixture_text: Fixture) -> None:
    assert cards(fixture_text)[1]["image_url"].endswith("abc123.png")


def test_format_falls_back_to_a_dom_heuristic(fixture_text: Fixture) -> None:
    """Third card has no badge and no id — §9.2's "fall back to DOM-text heuristics"."""
    third = cards(fixture_text)[2]
    assert third["format"] == "video"  # it carries an iframe
    assert third["ad_id"] is None
    assert "GHS labelling" in third["creative_text"]
    # The date is in the card's free text, not in a `.date-range` div.
    assert third["first_shown"] == "2025-02-02"


def test_the_declared_formats_are_recognised(fixture_text: Fixture) -> None:
    found = [card["format"] for card in cards(fixture_text)]
    assert found[0] == "text"
    assert found[1] == "image"


def test_the_region_is_stamped_on_every_card(fixture_text: Fixture) -> None:
    assert all(card["regions"] == ["US"] for card in cards(fixture_text))


def test_no_region_yields_an_empty_list_not_a_null_entry(fixture_text: Fixture) -> None:
    parsed = parse_ad_cards(fixture_text("transparency_grid.html"), advertiser="X")
    assert all(card["regions"] == [] for card in parsed)


def test_the_advertiser_is_stamped_on_every_card(fixture_text: Fixture) -> None:
    assert all(card["advertiser"] == "Chemwatch" for card in cards(fixture_text))


def test_an_empty_state_page_yields_no_cards(fixture_text: Fixture) -> None:
    """Distinct from a selector miss: this advertiser genuinely runs no ads."""
    assert parse_ad_cards(fixture_text("transparency_empty.html"), advertiser="Nobody") == []


def test_a_page_whose_selectors_moved_yields_no_cards(fixture_text: Fixture) -> None:
    """The connector turns this into ConnectorDegraded plus a saved debug page."""
    assert parse_ad_cards(fixture_text("transparency_broken.html"), advertiser="Chemwatch") == []


def test_google_own_links_are_not_treated_as_destinations(fixture_text: Fixture) -> None:
    """ "Why this ad?" points at support.google.com and is not the advertiser's landing page."""
    third = cards(fixture_text)[2]
    assert "google.com" not in (third["destination_url"] or "")


def test_one_stray_node_cannot_hide_the_real_cards(fixture_text: Fixture) -> None:
    """A leftover hidden template under the old selector, four real cards under the new.

    Picking the first selector that matches *anything* would return one card and
    report success. The most-matches rule is what makes that a non-event.
    """
    parsed = parse_ad_cards(fixture_text("transparency_stray_node.html"), advertiser="X")
    assert len(parsed) == 4


def test_the_date_range_regex_accepts_both_dash_characters() -> None:
    assert DATE_RANGE.search("Jan 3, 2025 - Feb 9, 2025")
    assert DATE_RANGE.search("Jan 3, 2025 – Feb 9, 2025")
    assert DATE_RANGE.search("Jan 3, 2025 — Present")


def test_every_selector_group_is_a_non_empty_fallback_chain() -> None:
    """A group that became empty would fail open and silently match nothing."""
    assert selectors.ALL
    for name, chain in selectors.ALL.items():
        assert chain, f"{name} has no selectors"
        assert all(isinstance(item, str) and item for item in chain)


def test_parsing_is_stable_across_calls(fixture_text: Fixture) -> None:
    """Card payloads feed content hashes; instability would defeat dedupe."""
    assert cards(fixture_text) == cards(fixture_text)
