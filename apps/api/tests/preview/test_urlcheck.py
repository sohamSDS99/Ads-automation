"""`preview/urlcheck.py` — the sitelink URL checker (S4-P8, PRD §11 4.3.1).

A sitelink's URL is **on-domain, 2xx after redirects, and unique per
campaign**. Proved against an `httpx.MockTransport` that records every request,
so "off-domain is never fetched" and "GETs only" (law 41) are observations,
not assumptions.
"""

from __future__ import annotations

import httpx
import pytest

from agent.preview import urlcheck
from agent.preview.urlcheck import UrlCheck

DOMAIN = "example.com"


class _Site:
    """A scripted web: `url -> (status, location)`; anything else refuses to connect."""

    def __init__(self, pages: dict[str, tuple[int, str | None]]) -> None:
        self.pages = pages
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        found = self.pages.get(str(request.url))
        if found is None:
            raise httpx.ConnectError("connection refused", request=request)
        status, location = found
        headers = {"location": location} if location else {}
        return httpx.Response(status, headers=headers, request=request)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


async def _check(site: _Site, url: str) -> UrlCheck:
    async with site.client() as client:
        return await urlcheck.check(url, domain=DOMAIN, client=client)


# ---------------------------------------------------------------------------
# on-domain
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.com/pricing", True),
        ("https://www.example.com/pricing", True),
        ("https://shop.example.com/", True),
        ("http://EXAMPLE.com:443/a", True),
        ("https://example.com.evil.net/", False),
        ("https://notexample.com/", False),
        ("https://evil.net/?r=example.com", False),
        ("ftp://example.com/file", False),
        ("mailto:sales@example.com", False),
        ("/relative/path", False),
    ],
)
def test_on_domain(url: str, expected: bool) -> None:
    assert urlcheck.on_domain(url, DOMAIN) is expected


@pytest.mark.parametrize("domain", ["example.com", "www.example.com", "https://www.Example.com/"])
def test_the_project_domain_is_normalised_however_it_was_typed(domain: str) -> None:
    assert urlcheck.on_domain("https://example.com/a", domain)
    assert urlcheck.on_domain("https://blog.example.com/a", domain)
    assert not urlcheck.on_domain("https://example.org/a", domain)


# ---------------------------------------------------------------------------
# 2xx after redirects
# ---------------------------------------------------------------------------


async def test_a_2xx_page_is_ok() -> None:
    site = _Site({"https://example.com/pricing": (200, None)})
    result = await _check(site, "https://example.com/pricing")
    assert result == UrlCheck(
        url="https://example.com/pricing",
        status="ok",
        final_url="https://example.com/pricing",
        http_status=200,
    )


async def test_redirects_are_followed_to_the_final_url() -> None:
    site = _Site(
        {
            "https://example.com/old": (301, "/new"),
            "https://example.com/new": (302, "https://www.example.com/final"),
            "https://www.example.com/final": (200, None),
        }
    )
    result = await _check(site, "https://example.com/old")
    assert result.status == "ok"
    assert result.final_url == "https://www.example.com/final"
    assert result.http_status == 200
    assert len(site.requests) == 3


async def test_a_redirect_off_domain_fails_and_is_never_followed() -> None:
    site = _Site(
        {
            "https://example.com/go": (302, "https://tracker.evil.net/x"),
            "https://tracker.evil.net/x": (200, None),
        }
    )
    result = await _check(site, "https://example.com/go")
    assert result.status == "off_domain"
    assert result.final_url == "https://tracker.evil.net/x"
    assert [str(r.url) for r in site.requests] == ["https://example.com/go"]


async def test_an_off_domain_url_is_never_requested() -> None:
    site = _Site({"https://partner.net/": (200, None)})
    result = await _check(site, "https://partner.net/")
    assert result.status == "off_domain"
    assert site.requests == []


@pytest.mark.parametrize("status", [404, 410, 500, 503])
async def test_a_non_2xx_final_status_fails(status: int) -> None:
    site = _Site({"https://example.com/gone": (status, None)})
    result = await _check(site, "https://example.com/gone")
    assert result.status == "http_error"
    assert result.http_status == status


async def test_a_redirect_without_a_location_fails() -> None:
    site = _Site({"https://example.com/broken": (302, None)})
    result = await _check(site, "https://example.com/broken")
    assert result.status == "http_error"
    assert result.http_status == 302


async def test_a_connection_failure_is_unreachable() -> None:
    site = _Site({})
    result = await _check(site, "https://example.com/down")
    assert result.status == "unreachable"
    assert result.http_status is None
    assert result.final_url is None


async def test_a_redirect_loop_stops() -> None:
    site = _Site(
        {
            "https://example.com/a": (302, "/b"),
            "https://example.com/b": (302, "/a"),
        }
    )
    result = await _check(site, "https://example.com/a")
    assert result.status == "too_many_redirects"
    assert len(site.requests) == urlcheck.MAX_REDIRECTS + 1


async def test_only_gets_are_issued() -> None:
    site = _Site(
        {
            "https://example.com/old": (308, "/new"),
            "https://example.com/new": (200, None),
        }
    )
    await _check(site, "https://example.com/old")
    assert {request.method for request in site.requests} == {"GET"}


async def test_check_all_checks_each_distinct_url_once() -> None:
    site = _Site({"https://example.com/a": (200, None), "https://example.com/b": (404, None)})
    async with site.client() as client:
        results = await urlcheck.check_all(
            ["https://example.com/a", "https://example.com/b", "https://example.com/a"],
            domain=DOMAIN,
            client=client,
        )
    assert set(results) == {"https://example.com/a", "https://example.com/b"}
    assert results["https://example.com/a"].status == "ok"
    assert results["https://example.com/b"].status == "http_error"
    assert len(site.requests) == 2


# ---------------------------------------------------------------------------
# unique per campaign
# ---------------------------------------------------------------------------


def _ok(url: str, final: str) -> UrlCheck:
    return UrlCheck(url=url, status="ok", final_url=final, http_status=200)


def test_two_urls_landing_on_one_page_are_duplicates() -> None:
    checks = [
        _ok("https://example.com/pricing", "https://example.com/pricing"),
        _ok("https://example.com/plans", "https://EXAMPLE.com/pricing/#top"),
        _ok("https://example.com/demo", "https://example.com/demo"),
    ]
    marked = urlcheck.unique(checks)
    assert [item.status for item in marked] == ["ok", "duplicate", "ok"]
    assert marked[1].final_url == "https://EXAMPLE.com/pricing/#top"


def test_a_failed_check_does_not_claim_its_page() -> None:
    checks = [
        UrlCheck(url="https://example.com/x", status="http_error", final_url=None, http_status=500),
        _ok("https://example.com/x", "https://example.com/x"),
    ]
    assert [item.status for item in urlcheck.unique(checks)] == ["http_error", "ok"]


@pytest.mark.parametrize(
    ("left", "right", "same"),
    [
        ("https://example.com/a", "https://example.com/a/", True),
        ("https://Example.com/a#x", "https://example.com/a", True),
        ("https://example.com/a?b=1", "https://example.com/a?b=2", False),
        ("https://example.com/a", "http://example.com/a", True),
        ("https://www.example.com/a", "https://example.com/a", True),
        ("https://example.com/A", "https://example.com/a", False),
    ],
)
def test_canonical(left: str, right: str, same: bool) -> None:
    assert (urlcheck.canonical(left) == urlcheck.canonical(right)) is same
