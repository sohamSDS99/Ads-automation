"""Live Google result pages, read through the Bright Data SERP proxy (PRD §9.3).

PRD §9.3 asks two things of the demand side: what a phrase is worth, and who
already owns the page it lands on. `dataforseo.py` answers the first well and
the second thinly — its `/serp/google/organic/live/advanced` result is filtered
down to `type == "organic"`, so the *ads* on the page, which are the whole point
of a paid-ads research agent, never reach the evidence store at all.

This connector answers the second question properly. It asks Google itself,
through a proxy that returns the page already parsed, and keeps four things off
each result page:

- `serp_snapshot` — the organic ranking, in exactly the payload shape
  `dataforseo._serp_draft` writes, so `creatives.competitor_rows` scores a
  Bright Data SERP and a DataForSEO one through the same code path.
- `serp_ad` — one row per live search ad: the advertiser, their copy and where
  it points. The Transparency Center archive (`transparency.py`) says what an
  advertiser has *ever* run; this says what is on the page for our money terms
  today, which is a different fact and a better one for a bid decision.
- `serp_question` and `serp_related` — People Also Ask and the related searches.
  Demand phrasing straight from the engine, and node 1.4.1 seeds on both.

**Why a proxy and not an API.** The proxy terminates TLS with its own CA, so
`serp_verify_tls` is false by default and the reason is written out in
`config.py`. What travels through the tunnel is a public search query; the
account secret goes to the proxy in a `Proxy-Authorization` header and never
enters it.

**Why only this connector uses it.** A SERP zone is scoped to search engines.
Pointed at a competitor's homepage it answers `400 This target URL isn't
supported with SERP API, use the Web Unlocker product for targeting this URL` —
so `web_crawler` and `transparency` cannot borrow this account, and routing
them through a proxy is a second zone, not a config flag.

**Why the vendor is not hidden behind a Protocol.** `KeywordProvider` in
`dataforseo.py` exists because nodes ask a *vendor* for volume and CPC, and
PRD §9.3 wants that vendor swappable. Nothing in here is vendor-shaped: the
evidence is a Google result page, and a second implementation would differ only
in which proxy fetched it. The seam that matters is already `EvidenceDraft`.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

import httpx
import structlog

from agent.connectors.base import (
    BaseConnector,
    ConnectorAuthError,
    ConnectorDegraded,
    ConnectorError,
    ConnectorRateLimited,
    ConnectorStatus,
    EvidenceDraft,
    build_client,
    with_retries,
)
from agent.db.models import EvidenceSource

log = structlog.get_logger(__name__)

#: The keys Bright Data returns ad blocks under. Google moves ads above and
#: below the organic results and sometimes both; the position is kept on the
#: payload because "they outbid us for the top slot" is a different finding from
#: "they were at the bottom of page one".
AD_BLOCKS: dict[str, str] = {"top_ads": "top", "bottom_ads": "bottom", "ads": "unknown"}

#: Dropped from every organic result before it is stored. `icon` is a base64
#: favicon — a few KB of data URI per result, carrying no fact, embedded and
#: full-text indexed along with everything else if it were left in.
NOISE_KEYS = frozenset({"icon", "snippet_highlighted_words", "image", "thumbnail"})

#: What the proxy asks for after it has refused a query, in seconds.
RETRY_AFTER_S = 15.0


class SerpConnector(BaseConnector):
    """One Google result page per keyword, parsed into four kinds of evidence."""

    name = "serp"
    source = EvidenceSource.SERP

    CREDENTIAL_FIELDS = ("username", "password")

    # --- transport ---------------------------------------------------------

    def _proxy_url(self) -> str:
        """`http://user:pass@host:port`, with the credential's own host if it set one.

        Both halves are percent-encoded: a zone password is generated, not
        chosen, and one containing an `@` would otherwise silently retarget the
        request at a host that does not exist.
        """
        username, password = self.context.require(*self.CREDENTIAL_FIELDS)
        host = self.context.credentials.get("host") or self.settings.serp_proxy_host
        port = self.context.credentials.get("port") or self.settings.serp_proxy_port
        return f"http://{quote(username, safe='')}:{quote(password, safe='')}@{host}:{port}"

    def _client(self) -> tuple[httpx.AsyncClient, bool]:
        """The borrowed client if a caller supplied one, otherwise our own.

        A borrowed client comes from a test, and it already points wherever that
        test wants — a `MockTransport`, usually — so the proxy is configured
        only on the client this connector builds for itself. Nothing in the
        product hands one over: `gather._pull` and `routes_credentials._run_test`
        both leave `context.client` unset precisely so that the transport under
        test is the transport a run uses.
        """
        if self.context.client is not None:
            return self.context.client, False
        return (
            build_client(
                self.settings,
                proxy=self._proxy_url(),
                verify=self.settings.serp_verify_tls,
            ),
            True,
        )

    def _search_url(self, keyword: str, *, country: str, language: str) -> str:
        """The page a person would open, plus the flag that returns it parsed.

        No `num`. Google stopped honouring it in 2025 — measured here, the same
        query returned nine organic results with `num=20` and nine without — and
        a request carrying it came back empty often enough to be worth not
        sending. `serp_max_results` is applied to the parsed list instead, where
        it is a cap we control rather than a hint the engine may ignore.
        """
        query = urlencode({"q": keyword, "gl": country, "hl": language, "brd_json": 1})
        return f"{self.settings.serp_search_url}?{query}"

    async def search(
        self, keyword: str, *, country: str | None = None, language: str | None = None
    ) -> dict[str, Any]:
        """One result page, as Bright Data parsed it."""
        url = self._search_url(
            keyword,
            country=country or self.settings.serp_country,
            language=language or self.settings.serp_language,
        )
        client, owned = self._client()

        async def call() -> dict[str, Any]:
            response = await client.get(url)
            if response.status_code in (401, 403, 407):
                raise ConnectorAuthError(
                    f"the SERP proxy rejected the account ({response.status_code}) — "
                    "check the zone username and password"
                )
            if response.status_code == 429:
                # Bright Data's own words on this status: "This query recently
                # failed and cannot be attempted at this time. Please try again
                # later, after a minimum of 15 seconds." The default backoff is
                # about a second, which spends two more retries to be told the
                # same thing, so the vendor's number is used instead of ours.
                raise ConnectorRateLimited(
                    "the SERP proxy put this query in cooldown", retry_after_s=RETRY_AFTER_S
                )
            response.raise_for_status()
            try:
                body = response.json()
            except ValueError as exc:
                # A proxy error arrives as an HTML page with a 200 on it. Saying
                # "no results" here would reach a node as "nobody advertises on
                # this term", which is the one answer that must never be faked.
                raise ConnectorError(
                    f"the SERP proxy returned {response.headers.get('content-type', 'no')} "
                    f"content, not JSON ({exc})"
                ) from exc
            if not isinstance(body, dict):
                raise ConnectorError("the SERP proxy returned JSON that is not a result page")
            return body

        try:
            return await with_retries(
                call, attempts=self.settings.connector_max_retries, label="serp"
            )
        finally:
            if owned:
                await client.aclose()

    # --- connector ---------------------------------------------------------

    async def fetch(self, params: dict[str, Any]) -> list[EvidenceDraft]:
        """A result page per keyword, `serp_concurrency` of them at a time.

        `params`: `{serp_keywords, country?, language?}`. `country` and
        `language` are ISO codes — a project's `markets[0]` carries exactly
        those, so the caller passes them straight through.

        One keyword failing does not fail the rest: the pages that came back are
        evidence, and `ConnectorDegraded` carries them up with the reason
        attached (PRD §9.2).
        """
        self.context.require(*self.CREDENTIAL_FIELDS)
        keywords = _clean(params.get("serp_keywords") or params.get("keywords"))
        if not keywords:
            raise ConnectorError("serp.fetch needs `serp_keywords`")
        keywords = keywords[: self.settings.serp_max_keywords]
        country = str(params.get("country") or self.settings.serp_country).lower()
        language = str(params.get("language") or self.settings.serp_language).lower()

        client, owned = self._client()
        # One connection pool for the whole fetch, shared by every sub-call, so
        # 25 keywords do not open 25 proxy sessions.
        self.context.client = client
        semaphore = asyncio.Semaphore(max(1, self.settings.serp_concurrency))

        async def one(keyword: str) -> tuple[str, dict[str, Any] | str]:
            async with semaphore:
                try:
                    return keyword, await self.search(keyword, country=country, language=language)
                except ConnectorError as exc:
                    return keyword, str(exc)

        try:
            pages = await asyncio.gather(*(one(keyword) for keyword in keywords))
        finally:
            if owned:
                self.context.client = None
                await client.aclose()

        drafts: list[EvidenceDraft] = []
        failures: list[str] = []
        for keyword, page in pages:
            if isinstance(page, str):
                failures.append(f"serp[{keyword}]: {page}")
                continue
            drafts.extend(self._drafts(keyword, page, country=country, language=language))

        if failures and not drafts:
            raise ConnectorError("; ".join(failures))
        if failures:
            raise ConnectorDegraded("; ".join(failures), drafts)
        return drafts

    def _drafts(
        self, keyword: str, page: dict[str, Any], *, country: str, language: str
    ) -> list[EvidenceDraft]:
        """Everything worth keeping off one result page."""
        url = _page_url(page) or self._search_url(keyword, country=country, language=language)
        seen_on = _seen_on(page)
        drafts = [self._snapshot(keyword, page, url=url, country=country, language=language)]
        drafts.extend(self._ads(keyword, page, url=url, seen_on=seen_on))

        questions = [
            text
            for text in (
                str(item.get("question") or "").strip()
                for item in _rows(page.get("people_also_ask"))
            )
            if text
        ]
        if questions:
            drafts.append(
                self.draft(
                    "serp_question",
                    {"keyword": keyword, "questions": questions, "market": country},
                    source_url=url,
                )
            )

        related = [
            text
            for text in (
                str(item.get("text") or item.get("query") or "").strip()
                for item in _rows(page.get("related"))
            )
            if text
        ]
        if related:
            drafts.append(
                self.draft(
                    "serp_related",
                    {"keyword": keyword, "related": related, "market": country},
                    source_url=url,
                )
            )
        return drafts

    def _snapshot(
        self,
        keyword: str,
        page: dict[str, Any],
        *,
        url: str,
        country: str,
        language: str,
    ) -> EvidenceDraft:
        """The organic ranking, in `dataforseo._serp_draft`'s payload shape.

        The shape is the contract: `creatives.competitor_rows` reads `keyword`
        and `results[].domain`, and it must not have to know which vendor filled
        them in. `ad_domains` is the one addition — an advertiser who bought the
        page but does not rank on it is invisible in the organic list, and is
        exactly who a competitor set is looking for.
        """
        results = [
            {
                "rank": item.get("rank"),
                "domain": _hostname(item.get("link")),
                "title": _text(item.get("title")),
                "description": _text(item.get("description")),
                "url": item.get("link"),
            }
            for item in _rows(page.get("organic"))
            if item.get("link")
        ][: self.settings.serp_max_results]

        general = _mapping(page.get("general"))
        ad_domains = sorted({_hostname(ad.get("link")) for ad in _ad_rows(page)} - {""})
        return self.draft(
            "serp_snapshot",
            {
                "keyword": keyword,
                "total_results": general.get("results_cnt"),
                "results": results,
                "ad_domains": ad_domains,
                "market": country,
                "language": language,
            },
            source_url=url,
        )

    def _ads(
        self, keyword: str, page: dict[str, Any], *, url: str, seen_on: str | None
    ) -> list[EvidenceDraft]:
        """One draft per live search ad.

        The payload deliberately answers to `creatives.creative_rows` — the same
        `advertiser` / `creative_text` / `destination_url` / `last_shown` keys
        the Transparency Center scrape writes — so node 1.3.2 reads an ad seen
        on the SERP and an ad seen in the archive as one corpus.

        `referral_link` is dropped on purpose. It is a `google.com/aclk?...`
        redirect carrying a click id that changes on every fetch, so keeping it
        would defeat evidence dedupe: the same unchanged ad would hash
        differently every run and pile up a new row each time.
        """
        drafts: list[EvidenceDraft] = []
        for block, position in AD_BLOCKS.items():
            for item in _rows(page.get(block)):
                link = item.get("link")
                headline = _text(item.get("title"))
                description = _text(item.get("description"))
                if not (headline or description):
                    continue
                advertiser = _hostname(link) or _hostname(item.get("display_link")) or "unknown"
                drafts.append(
                    self.draft(
                        "serp_ad",
                        {
                            "advertiser": advertiser,
                            "keyword": keyword,
                            "headline": headline,
                            "description": description,
                            "creative_text": "\n".join(
                                part for part in (headline, description) if part
                            ),
                            "destination_url": link,
                            "display_url": item.get("display_link"),
                            "position": position,
                            "rank": item.get("rank"),
                            "format": "text",
                            # We know it was live when we looked and nothing
                            # more, so only the seen date is claimed.
                            "first_shown": None,
                            "last_shown": seen_on,
                            "capture": "serp",
                        },
                        source_url=url,
                    )
                )
        return drafts

    async def test_connection(self) -> ConnectorStatus:
        """One cheap search. Proves the zone, the password and the parse at once."""
        try:
            page = await self.search("safety data sheet software")
        except ConnectorError as exc:
            return ConnectorStatus(ok=False, detail=str(exc))
        organic = len(_rows(page.get("organic")))
        ads = len(_ad_rows(page))
        general = _mapping(page.get("general"))
        if not organic and not ads:
            return ConnectorStatus(
                ok=False,
                detail=(
                    "the proxy answered but the page had no results — "
                    "the parse or the zone is wrong"
                ),
            )
        return ConnectorStatus(
            ok=True,
            detail=f"connected, {organic} organic result(s) and {ads} ad(s) on the probe page",
            meta={
                "probe_organic": organic,
                "probe_ads": ads,
                "search_engine": general.get("search_engine"),
                "country_code": general.get("country_code"),
            },
        )


# ---------------------------------------------------------------------------
# payload helpers
# ---------------------------------------------------------------------------


def _mapping(value: Any) -> dict[str, Any]:
    """A dict, whatever the upstream key actually held."""
    return value if isinstance(value, dict) else {}


def _rows(value: Any) -> list[dict[str, Any]]:
    """A list of dicts, whatever the upstream key actually held."""
    if not isinstance(value, list):
        return []
    return [
        {key: item for key, item in row.items() if key not in NOISE_KEYS}
        for row in value
        if isinstance(row, dict)
    ]


def _ad_rows(page: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for block in AD_BLOCKS for row in _rows(page.get(block))]


def _clean(value: Any) -> list[str]:
    """Deduped, stripped keywords in the order they were given."""
    if not isinstance(value, list | tuple | set):
        return []
    seen: dict[str, None] = {}
    for item in value:
        text = str(item or "").strip()
        if text:
            seen.setdefault(text, None)
    return list(seen)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _hostname(value: Any) -> str:
    """`https://WWW.Acme.com/pricing` -> `acme.com`, matching `creatives._hostname`."""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if "//" not in text:
        text = "//" + text
    host = urlsplit(text).hostname or ""
    if host.startswith("www."):
        host = host[4:]
    return host.strip(".")


def _page_url(page: dict[str, Any]) -> str | None:
    """The search URL a person could open, with the JSON flag taken back off."""
    original = _mapping(page.get("input")).get("original_url")
    if not original:
        return None
    return str(original).replace("&brd_json=1", "").replace("?brd_json=1&", "?")


def _seen_on(page: dict[str, Any]) -> str | None:
    """The date the page was fetched, from the engine's own timestamp if it gave one."""
    general = _mapping(page.get("general"))
    stamp = str(general.get("timestamp") or "").strip()
    if stamp:
        try:
            return dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            pass
    return dt.datetime.now(dt.UTC).date().isoformat()


__all__ = ["SerpConnector"]
