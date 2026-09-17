"""Working out the two setup fields, and refusing to when nothing says so.

The property worth defending here is the refusal. A detector that always
answers is worse than no detector: a market with a guessed currency prices
every CPC in the report wrongly, and a site URL invented from a domain sends
the crawler somewhere nobody chose. So most of what follows checks that when
the evidence runs out, the answer is "nothing to read" and not a plausible
value.
"""

from __future__ import annotations

from collections import Counter

import httpx
import pytest

from agent.autofill import (
    MAX_MARKETS,
    Finding,
    _hreflang_countries,
    compose_markets,
    detect_site_url,
    market_for,
    normalise_country,
)
from agent.config import Settings

TEST_KEY = "dW5pdC10ZXN0LWtleS0zMi1ieXRlcy1leGFjdGx5ISE="

PAGE = """
<html lang="en">
  <head>
    <link rel="alternate" hreflang="x-default" href="https://example.com/" />
    <link rel="alternate" hreflang="en-US" href="https://example.com/us/" />
    <link rel="alternate" hreflang="de-DE" href="https://example.com/de/" />
    <link rel="alternate" hreflang="nb" href="https://example.com/no/" />
    <link rel="alternate" hreflang="en-us" href="https://example.com/us/duplicate" />
    <link rel="alternate" hreflang="not a locale" href="https://example.com/x/" />
  </head>
</html>
"""


def settings() -> Settings:
    return Settings(app_encryption_key=TEST_KEY)


def client_returning(handler: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


# --- reading a country -------------------------------------------------------


def test_a_crm_export_says_united_states_not_us() -> None:
    assert normalise_country("United States") == "US"
    assert normalise_country("  united kingdom ") == "GB"
    assert normalise_country("de") == "DE"
    assert normalise_country("Norway") == "NO"


def test_a_country_nothing_recognises_is_not_guessed_at() -> None:
    assert normalise_country("Middle East") is None
    assert normalise_country("EMEA") is None
    assert normalise_country("") is None
    assert normalise_country(None) is None


def test_a_market_without_a_known_currency_is_refused() -> None:
    """`Market` requires a currency and every CPC in the report inherits it, so
    an absent market beats a guessed one."""
    assert market_for("NO") == {"country": "NO", "language": "nb", "currency": "NOK"}
    assert market_for("us", language="en") == {"country": "US", "language": "en", "currency": "USD"}
    assert market_for("ZZ") is None


# --- reading a site ----------------------------------------------------------


def test_language_links_are_read_in_the_order_the_site_publishes_them() -> None:
    assert _hreflang_countries(PAGE) == [("US", "en"), ("DE", "de")]


def test_a_locale_with_its_own_page_outranks_one_sharing_a_regional_page() -> None:
    """Measured against sdsmanager.com, which publishes 64 language links: 26
    of them point at one shared `/eu/` page, and document order would have
    proposed six of those over the five countries with pages of their own."""
    page = """
    <html><head>
      <link rel="alternate" hreflang="en-AT" href="https://example.com/eu/" />
      <link rel="alternate" hreflang="en-BE" href="https://example.com/eu/" />
      <link rel="alternate" hreflang="en-GB" href="https://example.com/uk/" />
      <link rel="alternate" hreflang="en-US" href="https://example.com/us/" />
    </head></html>
    """
    assert [country for country, _ in _hreflang_countries(page)] == ["GB", "US", "AT", "BE"]


def test_a_language_link_with_no_country_is_not_a_market() -> None:
    """`hreflang="nb"` says which language, not which country — and a market is
    a country. Guessing Norway from Norwegian is how NO and SE become one."""
    countries = [country for country, _ in _hreflang_countries(PAGE)]
    assert "NO" not in countries
    assert "x-default" not in countries


# --- merging the two ---------------------------------------------------------


def test_the_crm_leads_and_the_website_follows() -> None:
    """One is where revenue came from; the other is where someone published a
    translation. They are not the same claim."""
    markets, unknown, dropped = compose_markets(
        Counter({"DE": 40, "US": 9}), [("US", "en"), ("FR", "fr")]
    )

    assert [market["country"] for market in markets] == ["DE", "US", "FR"]
    assert not unknown and not dropped
    # The CRM's own country keeps its default language rather than the one the
    # website happened to list it under.
    assert markets[1] == {"country": "US", "language": "en", "currency": "USD"}


def test_a_site_with_forty_locales_is_a_translation_list_not_a_target_list() -> None:
    published = [(country, "en") for country in ("US", "GB", "DE", "FR", "ES", "IT", "NL", "SE")]
    markets, _, dropped = compose_markets(Counter(), published)

    assert len(markets) == MAX_MARKETS
    assert dropped == len(published) - MAX_MARKETS, "what was left out has to be countable"


def test_an_unrecognised_country_is_named_rather_than_dropped_silently() -> None:
    markets, unknown, _ = compose_markets(Counter({"ZZ": 5, "US": 1}), [])

    assert [market["country"] for market in markets] == ["US"]
    assert unknown == ["ZZ"]


def test_nothing_in_means_nothing_out() -> None:
    assert compose_markets(Counter(), []) == ([], [], 0)


# --- the site URL ------------------------------------------------------------


async def test_the_site_url_keeps_where_the_redirects_land() -> None:
    """A domain that redirects to a country path is telling you where its
    content lives; crawling the bare host would find a language picker."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in ("", "/"):
            return httpx.Response(301, headers={"Location": "https://www.example.com/us/"})
        return httpx.Response(200, html="<html></html>")

    async with client_returning(handler) as client:
        finding = await detect_site_url("example.com", settings=settings(), client=client)

    assert isinstance(finding, Finding)
    assert finding.found
    assert finding.value == "https://www.example.com/us"
    assert "redirects to" in finding.source


async def test_a_domain_that_does_not_answer_is_reported_not_assumed() -> None:
    """The tempting wrong answer is `https://<domain>`, which looks right and
    sends the crawler at an address nobody confirmed exists."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no such host", request=request)

    async with client_returning(handler) as client:
        finding = await detect_site_url("nope.invalid", settings=settings(), client=client)

    assert not finding.found
    assert finding.value == ""
    assert "did not answer" in finding.source


@pytest.mark.parametrize("status", [404, 500])
async def test_a_site_that_answers_with_an_error_is_not_a_site(status: int) -> None:
    async with client_returning(lambda request: httpx.Response(status)) as client:
        finding = await detect_site_url("example.com", settings=settings(), client=client)

    assert not finding.found
