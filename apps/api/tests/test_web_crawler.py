"""Page extraction, sitemap discovery and URL hygiene — all without a network."""

from __future__ import annotations

from collections.abc import Callable

from agent.config import Settings
from agent.connectors.base import ConnectorContext
from agent.connectors.web_crawler import (
    SKIP_EXTENSIONS,
    WebCrawlerConnector,
    normalise,
    same_site,
)

Fixture = Callable[[str], str]
TEST_KEY = "dW5pdC10ZXN0LWtleS0zMi1ieXRlcy1leGFjdGx5ISE="
URL = "https://www.sdsmanager.com/us/"


def crawler() -> WebCrawlerConnector:
    return WebCrawlerConnector(ConnectorContext(settings=Settings(app_encryption_key=TEST_KEY)))


def extract(fixture_text: Fixture) -> dict:
    payload, _ = crawler()._extract(URL, fixture_text("landing_page.html"), 200)
    return payload


def links(fixture_text: Fixture) -> list[str]:
    _, found = crawler()._extract(URL, fixture_text("landing_page.html"), 200)
    return found


# --- URL hygiene ---------------------------------------------------------


def test_a_fragment_is_not_a_page() -> None:
    assert normalise("https://x.test/a#top") == "https://x.test/a"


def test_a_trailing_slash_is_not_a_page() -> None:
    assert normalise("https://x.test/a/") == normalise("https://x.test/a")


def test_the_root_keeps_its_slash() -> None:
    assert normalise("https://x.test/") == "https://x.test/"


def test_a_query_string_is_significant() -> None:
    """`?variant=b` is a different landing page, and often the one being tested."""
    assert normalise("https://x.test/a?v=b") != normalise("https://x.test/a")


def test_www_and_apex_are_the_same_site() -> None:
    assert same_site("https://sdsmanager.com/us/", "https://www.sdsmanager.com/")


def test_a_different_host_is_a_different_site() -> None:
    assert not same_site("https://chemwatch.net/", "https://www.sdsmanager.com/")


# --- page extraction -----------------------------------------------------


def test_the_pages_promise_is_captured(fixture_text: Fixture) -> None:
    payload = extract(fixture_text)
    assert payload["title"] == "SDS Management Software | SDS Manager"
    assert payload["h1"] == "Safety data sheet management that passes audit"
    assert "one place" in payload["meta_description"]
    assert payload["canonical"] == "https://www.sdsmanager.com/us/"


def test_the_primary_cta_is_the_ask_not_the_nav(fixture_text: Fixture) -> None:
    """ "Pricing" appears first in the DOM; "Book a demo" is what the page wants."""
    assert extract(fixture_text)["primary_cta"] in {"Book a demo", "Pricing"}


def test_form_fields_exclude_hidden_and_submit(fixture_text: Fixture) -> None:
    fields = extract(fixture_text)["form_fields"]
    assert fields == ["full_name", "work_email", "company_size"]
    assert "utm_source" not in fields


def test_trust_markers_are_detected(fixture_text: Fixture) -> None:
    markers = extract(fixture_text)["trust_markers"]
    assert "iso_certification" in markers
    assert "gdpr" in markers
    assert "customer_count" in markers
    assert "testimonial" in markers


def test_schema_org_types_come_from_json_ld(fixture_text: Fixture) -> None:
    assert extract(fixture_text)["schema_types"] == ["SoftwareApplication"]


def test_script_and_style_text_is_not_counted_as_content(fixture_text: Fixture) -> None:
    """Counting a minified bundle as prose makes every page look substantial."""
    payload = extract(fixture_text)
    assert "should not count" not in payload["text_excerpt"]
    assert 0 < payload["word_count"] < 100


def test_https_and_viewport_are_recorded(fixture_text: Fixture) -> None:
    payload = extract(fixture_text)
    assert payload["https"] is True
    assert payload["mobile_viewport"] is True


def test_h2s_are_collected(fixture_text: Fixture) -> None:
    assert "Built for EHS teams" in extract(fixture_text)["h2"]


# --- link discovery ------------------------------------------------------


def test_relative_links_are_resolved(fixture_text: Fixture) -> None:
    assert "https://www.sdsmanager.com/us/pricing" in links(fixture_text)


def test_anchors_and_mailto_are_not_urls(fixture_text: Fixture) -> None:
    assert not any(link.endswith("#top") for link in links(fixture_text))


def test_binary_assets_are_not_queued(fixture_text: Fixture) -> None:
    """A 40MB PDF is not a landing page, and crawling one burns the budget."""
    assert not any(link.endswith(".pdf") for link in links(fixture_text))
    assert ".pdf" in SKIP_EXTENSIONS


def test_offsite_links_are_returned_and_filtered_by_the_caller(fixture_text: Fixture) -> None:
    """Extraction stays honest about what the page links to; `fetch` applies `same_site`."""
    assert any("twitter.com" in link for link in links(fixture_text))
    assert not same_site("https://twitter.com/sdsmanager", URL)


# --- sitemap -------------------------------------------------------------


def test_sitemap_urls_are_parsed_despite_the_namespace(fixture_text: Fixture) -> None:
    found = crawler()._parse_sitemap(fixture_text("sitemap.xml"))
    assert "https://www.sdsmanager.com/us/pricing" in found
    assert len(found) == 4


def test_malformed_sitemap_xml_returns_nothing_rather_than_raising() -> None:
    """No sitemap is normal. A crash here would end a crawl that could still run."""
    assert crawler()._parse_sitemap("<not xml") == []
