"""The Bright Data SERP connector, replayed from one real result page.

`tests/fixtures/serp_page.json` is an unedited capture — favicon data URIs, ad
click-tracking redirects and all — because the two things most likely to break
this connector are exactly the things a hand-written fixture would tidy away.

The agreement tests matter more than the parsing ones. A `serp_snapshot` this
connector writes is read by `creatives.competitor_rows`, which was written
against DataForSEO's payload; a `serp_ad` is read by `creatives.creative_rows`,
which was written against the Transparency Center's. Neither of those readers
knows a second writer now exists, so the contract is asserted here rather than
discovered in a run.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent.config import Settings
from agent.connectors import build_connector
from agent.connectors.base import (
    ConnectorAuthError,
    ConnectorContext,
    ConnectorDegraded,
    ConnectorError,
)
from agent.connectors.serp import RETRY_AFTER_S, SerpConnector
from agent.db.models import EvidenceSource
from agent.nodes import creatives

TEST_KEY = "dW5pdC10ZXN0LWtleS0zMi1ieXRlcy1leGFjdGx5ISE="
CREDENTIALS = {"api_key": "brd-test-api-key"}
PAGE: dict[str, Any] = json.loads(
    (Path(__file__).parent / "fixtures" / "serp_page.json").read_text()
)


def settings(**overrides: Any) -> Settings:
    overrides.setdefault("serp_zone", "serp_api1")
    return Settings(app_encryption_key=TEST_KEY, **overrides)


def target(request: httpx.Request) -> httpx.URL:
    """The Google URL inside the request, which is now a field and not the path.

    Every request goes to `api.brightdata.com/request`; the keyword, `gl` and
    `hl` ride in the JSON body. A test that used to read `request.url.params`
    reads this instead.
    """
    return httpx.URL(json.loads(request.content)["url"])


def connector(
    handler: Any = None, *, credentials: dict[str, str] | None = None, **setting_overrides: Any
) -> SerpConnector:
    """A connector whose transport is a `MockTransport`, or none at all."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler)) if handler else None
    return SerpConnector(
        ConnectorContext(
            credentials={**CREDENTIALS, **(credentials or {})},
            settings=settings(**setting_overrides),
            client=client,
        )
    )


def always(page: dict[str, Any]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=page)

    return handler


def by_kind(drafts: list[Any], kind: str) -> list[Any]:
    return [draft for draft in drafts if draft.kind == kind]


def ids(count: int) -> list[uuid.UUID]:
    return [uuid.uuid4() for _ in range(count)]


# ---------------------------------------------------------------------------
# what comes off a page
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_result_page_becomes_four_kinds_of_evidence() -> None:
    drafts = await connector(always(PAGE)).fetch({"serp_keywords": ["sds management software"]})

    kinds = {draft.kind for draft in drafts}
    assert kinds == {"serp_snapshot", "serp_ad", "serp_question", "serp_related"}
    # One snapshot, one PAA row and one related row per page; one draft per ad.
    assert len(by_kind(drafts, "serp_snapshot")) == 1
    assert len(by_kind(drafts, "serp_ad")) == len(PAGE["bottom_ads"])
    assert all(draft.source is EvidenceSource.SERP for draft in drafts)


@pytest.mark.asyncio
async def test_the_registry_builds_it_and_it_declares_the_serp_source() -> None:
    built = build_connector("serp", ConnectorContext(settings=settings()))
    assert built.name == "serp"
    assert built.source is EvidenceSource.SERP


@pytest.mark.asyncio
async def test_an_organic_result_keeps_its_hostname_not_its_display_name() -> None:
    """The proxy's `source` is what Google prints — "SDS Manager", not a domain.

    Storing that would make every competitor comparison miss, because the reader
    matches on hostnames.
    """
    drafts = await connector(always(PAGE)).fetch({"serp_keywords": ["sds management software"]})
    results = by_kind(drafts, "serp_snapshot")[0].payload["results"]

    domains = [row["domain"] for row in results]
    assert "sdsmanager.com" in domains
    assert all(row["domain"] and "/" not in row["domain"] for row in results)
    assert all(not row["domain"].startswith("www.") for row in results)


@pytest.mark.asyncio
async def test_the_snapshot_carries_the_advertisers_the_organic_list_cannot() -> None:
    """An advertiser who bought the page but does not rank on it is the finding."""
    drafts = await connector(always(PAGE)).fetch({"serp_keywords": ["sds management software"]})
    snapshot = by_kind(drafts, "serp_snapshot")[0].payload

    assert snapshot["ad_domains"] == ["ideagen.com", "ul.com"]
    assert not set(snapshot["ad_domains"]) & {row["domain"] for row in snapshot["results"]}


@pytest.mark.asyncio
async def test_the_base64_favicon_never_reaches_the_evidence_store() -> None:
    """Nine data URIs per page, embedded and full-text indexed if they got through."""
    assert "icon" in PAGE["organic"][0], "the fixture must still carry what is being stripped"

    drafts = await connector(always(PAGE)).fetch({"serp_keywords": ["sds management software"]})

    for draft in drafts:
        rendered = json.dumps(draft.payload) + draft.text()
        assert "data:image" not in rendered
        assert "icon" not in draft.payload


@pytest.mark.asyncio
async def test_a_click_tracking_redirect_is_not_stored_so_dedupe_still_works() -> None:
    """`referral_link` carries a fresh click id every fetch.

    Keeping it would give the same unchanged ad a different content hash on
    every run, and the evidence table would grow a duplicate row a day.
    """
    first = await connector(always(PAGE)).fetch({"serp_keywords": ["sds management software"]})
    second = await connector(always(PAGE)).fetch({"serp_keywords": ["sds management software"]})

    ad = by_kind(first, "serp_ad")[0]
    assert "referral_link" not in ad.payload
    assert "aclk" not in json.dumps(ad.payload)
    assert [draft.hash() for draft in first] == [draft.hash() for draft in second]


@pytest.mark.asyncio
async def test_the_questions_and_related_searches_come_through_as_phrases() -> None:
    drafts = await connector(always(PAGE)).fetch({"serp_keywords": ["sds management software"]})

    questions = by_kind(drafts, "serp_question")[0].payload["questions"]
    related = by_kind(drafts, "serp_related")[0].payload["related"]
    assert "What is the best software for managing safety data sheets?" in questions
    assert "Safety data sheet software free" in related
    assert all(isinstance(item, str) and item for item in questions + related)


# ---------------------------------------------------------------------------
# agreement with the readers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_snapshot_is_shaped_like_the_keyword_vendors() -> None:
    """`creatives.competitor_rows` reads both writers' rows through one path."""
    drafts = await connector(always(PAGE)).fetch({"serp_keywords": ["sds management software"]})
    payload = by_kind(drafts, "serp_snapshot")[0].payload

    assert {"keyword", "total_results", "results"} <= set(payload)
    assert {"rank", "domain", "title", "description", "url"} == set(payload["results"][0])


@pytest.mark.asyncio
async def test_a_snapshot_scores_competitors_through_the_existing_reader() -> None:
    drafts = await connector(always(PAGE)).fetch({"serp_keywords": ["sds management software"]})
    payloads = [draft.payload for draft in by_kind(drafts, "serp_snapshot")]

    rows, _, checked = creatives.competitor_rows(
        domain_rows=[],
        domain_ids=[],
        serp_rows=payloads,
        serp_ids=ids(len(payloads)),
        our_domain="sdsmanager.com",
        our_terms={"sds management software"},
    )

    assert checked == 1
    scored = {row.domain for row in rows}
    assert "enhesa.com" in scored
    assert "sdsmanager.com" not in scored, "our own domain is not a competitor"
    assert all("serp" in row.overlap_basis for row in rows)


@pytest.mark.asyncio
async def test_an_ad_reads_as_a_creative_row_alongside_the_archive() -> None:
    """Node 1.3.2 hands `serp_ad` payloads to the Transparency Center's reader."""
    drafts = await connector(always(PAGE)).fetch({"serp_keywords": ["sds management software"]})
    payloads = [draft.payload for draft in by_kind(drafts, "serp_ad")]

    rows, dropped = creatives.creative_rows(payloads, ids(len(payloads)), max_ads=300)

    assert dropped == 0
    assert len(rows) == len(payloads)
    assert {row.advertiser for row in rows} == {"ideagen.com", "ul.com"}
    assert all(row.creative_text for row in rows)
    assert all(row.landing_url and row.landing_url.startswith("http") for row in rows)
    # We know it was live when we looked, and nothing before that.
    assert all(row.first_shown is None and row.last_shown for row in rows)


# ---------------------------------------------------------------------------
# the account, and what goes wrong with it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_key_is_the_whole_account_and_it_travels_as_a_bearer() -> None:
    """One value in, one `Authorization` header out — and the zone beside it."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=PAGE)

    await connector(handler).search("sds management software")
    assert seen["url"] == "https://api.brightdata.com/request"
    assert seen["auth"] == "Bearer brd-test-api-key"
    assert seen["body"]["zone"] == "serp_api1"
    assert seen["body"]["format"] == "raw"
    # The parse the whole connector depends on is a property of the target URL,
    # not of the transport, so it has to survive the move off the proxy.
    assert httpx.URL(seen["body"]["url"]).params["brd_json"] == "1"


def test_a_missing_key_is_named_before_any_request_is_spent() -> None:
    with pytest.raises(ConnectorAuthError, match="api_key"):
        SerpConnector(ConnectorContext(credentials={}, settings=settings()))._headers()


@pytest.mark.asyncio
async def test_a_rejected_account_is_an_auth_error_and_is_not_retried() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(401, json={"error": "invalid auth token"})

    with pytest.raises(ConnectorAuthError, match="401"):
        await connector(handler).search("sds management software")
    assert attempts == 1, "a wrong key is wrong all three times"


@pytest.mark.asyncio
async def test_a_cooldown_waits_the_fifteen_seconds_the_vendor_asked_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default backoff is about a second, which just buys another refusal."""
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, text="This query recently failed…after a minimum of 15 s")
        return httpx.Response(200, json=PAGE)

    page = await connector(handler).search("sds management software")

    assert page["general"]["search_engine"] == "google"
    assert slept and slept[0] >= RETRY_AFTER_S


@pytest.mark.asyncio
async def test_an_html_error_page_is_an_error_not_an_empty_serp() -> None:
    """The dangerous failure: a 200 carrying HTML reads as "nobody advertises here"."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text="<html>proxy error</html>", headers={"content-type": "text/html"}
        )

    with pytest.raises(ConnectorError, match="text/html"):
        await connector(handler, connector_max_retries=1).search("sds management software")


@pytest.mark.asyncio
async def test_one_keyword_failing_keeps_the_pages_that_came_back() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if target(request).params.get("q") == "broken term":
            return httpx.Response(500, text="upstream is down")
        return httpx.Response(200, json=PAGE)

    with pytest.raises(ConnectorDegraded) as raised:
        await connector(handler, connector_max_retries=1).fetch(
            {"serp_keywords": ["sds management software", "broken term"]}
        )

    assert "broken term" in raised.value.reason
    assert by_kind(raised.value.drafts, "serp_snapshot"), "the page that worked is still evidence"


@pytest.mark.asyncio
async def test_every_keyword_failing_is_a_plain_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream is down")

    with pytest.raises(ConnectorError):
        await connector(handler, connector_max_retries=1).fetch({"serp_keywords": ["a", "b"]})


# ---------------------------------------------------------------------------
# what is asked for
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_keyword_list_is_capped_because_the_vendor_bills_per_page() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(target(request).params["q"])
        return httpx.Response(200, json=PAGE)

    await connector(handler, serp_max_keywords=3).fetch(
        {"serp_keywords": [f"term {index}" for index in range(10)]}
    )
    assert len(seen) == 3


@pytest.mark.asyncio
async def test_the_same_keyword_twice_is_one_page() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(target(request).params["q"])
        return httpx.Response(200, json=PAGE)

    await connector(handler).fetch({"serp_keywords": ["sds software", " sds software ", ""]})
    assert seen == ["sds software"]


@pytest.mark.asyncio
async def test_the_market_reaches_the_request_as_gl_and_hl() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(target(request).params))
        return httpx.Response(200, json=PAGE)

    await connector(handler).fetch(
        {"serp_keywords": ["sds software"], "country": "no", "language": "nb"}
    )
    assert seen["gl"] == "no"
    assert seen["hl"] == "nb"
    assert seen["brd_json"] == "1"
    assert "num" not in seen, "Google stopped honouring it and it costs us empty pages"


@pytest.mark.asyncio
async def test_no_keywords_is_a_programming_error_not_an_empty_fetch() -> None:
    with pytest.raises(ConnectorError, match="serp_keywords"):
        await connector(always(PAGE)).fetch({"domain": "sdsmanager.com"})


@pytest.mark.asyncio
async def test_test_connection_counts_what_it_found() -> None:
    status = await connector(always(PAGE)).test_connection()
    assert status.ok
    assert status.meta["probe_organic"] == len(PAGE["organic"])
    assert status.meta["probe_ads"] == len(PAGE["bottom_ads"])
    assert status.meta["country_code"] == "US"


@pytest.mark.asyncio
async def test_a_page_with_nothing_on_it_is_reported_as_broken_not_as_connected() -> None:
    status = await connector(always({"general": {}})).test_connection()
    assert not status.ok
    assert "no results" in status.detail


# ---------------------------------------------------------------------------
# the zone, which is discovered rather than asked for
# ---------------------------------------------------------------------------


def unpinned(handler: Any) -> SerpConnector:
    """A connector with no `SERP_ZONE` set, so `_zone` has to go and ask."""
    return SerpConnector(
        ConnectorContext(
            credentials=CREDENTIALS,
            settings=Settings(app_encryption_key=TEST_KEY, serp_zone=""),
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
    )


@pytest.mark.asyncio
async def test_the_zone_is_read_off_the_account_rather_than_typed() -> None:
    """The second value the API needs is one the first value can fetch."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "get_active_zones" in str(request.url):
            assert request.headers.get("authorization") == "Bearer brd-test-api-key"
            return httpx.Response(
                200,
                json=[
                    {"name": "unblocker1", "type": "unblocker"},
                    {"name": "our_serp_zone", "type": "serp"},
                ],
            )
        return httpx.Response(200, json=PAGE)

    connector_ = unpinned(handler)
    await connector_.search("sds management software")
    assert connector_._zone_name == "our_serp_zone"
    assert any("get_active_zones" in call for call in calls)


@pytest.mark.asyncio
async def test_the_zone_is_asked_for_once_no_matter_how_many_keywords() -> None:
    """25 keywords must not be 25 account lookups."""
    listings = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal listings
        if "get_active_zones" in str(request.url):
            listings += 1
            return httpx.Response(200, json=[{"name": "our_serp_zone", "type": "serp"}])
        return httpx.Response(200, json=PAGE)

    await unpinned(handler).fetch({"serp_keywords": ["one", "two", "three", "four"]})
    assert listings == 1


@pytest.mark.asyncio
async def test_an_account_that_will_not_list_its_zones_still_searches() -> None:
    """Discovery is a convenience, not a dependency: a refusal falls back."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "get_active_zones" in str(request.url):
            return httpx.Response(403, text="forbidden")
        return httpx.Response(200, json=PAGE)

    connector_ = unpinned(handler)
    page = await connector_.search("sds management software")
    assert connector_._zone_name == "serp_api1", "the documented default, not a crash"
    assert page["organic"], "and the search still happened"


@pytest.mark.asyncio
async def test_a_refusal_names_the_zone_because_the_zone_was_the_guess() -> None:
    """A 400 against a guessed zone must say which zone was guessed."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "get_active_zones" in str(request.url):
            return httpx.Response(200, json=[])
        return httpx.Response(400, json={"error": "zone not found"})

    with pytest.raises(ConnectorError, match="serp_api1"):
        await unpinned(handler).search("sds management software")
