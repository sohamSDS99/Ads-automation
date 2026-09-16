"""Every CSS selector the Transparency Center scrape depends on, in one file.

PRD §9.2: "selectors in a single `selectors.py` map; on selector miss, fall back
to DOM-text heuristics and raise `ConnectorDegraded`."

The point of the indirection is repair speed. Google ships obfuscated,
generated class names and changes them without notice, so when the scrape goes
quiet the fix should be editing a string in this file and nothing else. Each
entry is a *list* of candidates tried in order, so a selector can be updated
without deleting the one that used to work — a rollback is a reorder.
"""

from __future__ import annotations

from typing import Final

#: The search box on the advertiser search page.
ADVERTISER_SEARCH_INPUT: Final[list[str]] = [
    "input[aria-label*='Search' i]",
    "input[placeholder*='advertiser' i]",
    "input[type='text']",
]

#: Rows in the advertiser autocomplete dropdown.
ADVERTISER_SUGGESTION: Final[list[str]] = [
    "[role='option']",
    "material-select-dropdown-item",
    "li[role='listitem']",
]

#: One card in the ad results grid.
AD_CARD: Final[list[str]] = [
    "creative-preview",
    "[data-creative-id]",
    "div[role='listitem']",
    "a[href*='/advertiser/'][href*='/creative/']",
]

#: Fields inside a card.
AD_CREATIVE_TEXT: Final[list[str]] = ["div.creative-text", "[aria-label*='ad text' i]", "span"]
AD_FORMAT_BADGE: Final[list[str]] = ["div.format-label", "[data-format]", "span.format"]
#: Image creatives only. An `iframe` is a video embed, and conflating the two
#: made every video ad report `format="image"`.
AD_IMAGE: Final[list[str]] = ["img[src]"]
AD_VIDEO: Final[list[str]] = ["iframe[src]", "video[src]"]
AD_DESTINATION_LINK: Final[list[str]] = ["a[href^='http']:not([href*='google.com'])"]

#: Date range shown on the card, e.g. "Jan 3, 2025 – Feb 9, 2025".
AD_DATE_RANGE: Final[list[str]] = ["div.date-range", "[aria-label*='shown' i]", "span.dates"]

#: The button that loads the next page of the grid.
LOAD_MORE: Final[list[str]] = [
    "button[aria-label*='more' i]",
    "material-button:has-text('Show more')",
    "button:has-text('Load more')",
]

#: Marks "there were no ads for this advertiser" rather than "the scrape broke".
EMPTY_STATE: Final[list[str]] = [
    "[aria-label*='no results' i]",
    "div.empty-state",
    "text=No ads to show",
]

#: Every selector group, so a health check can report which ones matched.
ALL: Final[dict[str, list[str]]] = {
    "advertiser_search_input": ADVERTISER_SEARCH_INPUT,
    "advertiser_suggestion": ADVERTISER_SUGGESTION,
    "ad_card": AD_CARD,
    "ad_creative_text": AD_CREATIVE_TEXT,
    "ad_format_badge": AD_FORMAT_BADGE,
    "ad_image": AD_IMAGE,
    "ad_video": AD_VIDEO,
    "ad_destination_link": AD_DESTINATION_LINK,
    "ad_date_range": AD_DATE_RANGE,
    "load_more": LOAD_MORE,
    "empty_state": EMPTY_STATE,
}
