"""The sitelink URL checker (Stage 04 PRD §11 4.3.1, law 41).

A sitelink's URL must be **on the project's domain, answer 2xx after
redirects, and be unique within its campaign**. Three rules, three functions:

* `on_domain` — the host is the project's domain or one of its subdomains.
  `example.com.evil.net` and `notexample.com` are not; neither is a URL that
  merely *mentions* the domain in its query.
* `check` — a GET, following redirects **by hand**, one hop at a time, so that
  every hop is checked on-domain *before* it is requested. `httpx`'s own
  `follow_redirects` would fetch a tracker, a partner or an internal address
  first and only then let us notice where it went. GETs only (law 41): the
  body is never read, nothing is ever submitted.
* `unique` — two sitelinks whose URLs land on the same page after redirects
  are one sitelink; the later one is marked `duplicate`.

This module knows nothing of campaigns or assets: 4.3.1 decides what a
failure means for a sitelink.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Final, Literal
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

#: Hops after the first request. A page further away than this is not where a
#: sitelink should point, and a loop must end.
MAX_REDIRECTS: Final = 5
#: Per request. A sitelink page that takes longer is not one a searcher waits for.
TIMEOUT_S: Final = 10.0
#: Checks in flight at once — a campaign's sitelinks, not a crawl.
CONCURRENCY: Final = 4

UrlStatus = Literal[
    "ok",
    "off_domain",
    "http_error",
    "unreachable",
    "too_many_redirects",
    "duplicate",
]

_REDIRECTS: Final = frozenset({301, 302, 303, 307, 308})
_SCHEMES: Final = frozenset({"http", "https"})


@dataclass(frozen=True, slots=True)
class UrlCheck:
    url: str
    status: UrlStatus
    #: The last URL reached — requested, or refused as off-domain. None when
    #: nothing answered.
    final_url: str | None
    http_status: int | None


def _bare_host(host: str) -> str:
    host = host.strip().lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def _domain(domain: str) -> str:
    """`Project.domain` however it was typed: `example.com`, `www.…`, a whole URL."""
    text = domain.strip()
    host = urlsplit(text).hostname if "//" in text else text.split("/", 1)[0].split(":", 1)[0]
    return _bare_host(host or "")


def on_domain(url: str, domain: str) -> bool:
    """Is `url` an http(s) URL on `domain` or one of its subdomains?"""
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in _SCHEMES or not parts.hostname:
        return False
    host = _bare_host(parts.hostname)
    root = _domain(domain)
    return bool(root) and (host == root or host.endswith("." + root))


def canonical(url: str) -> str:
    """The page a URL lands on: no scheme, `www.` or fragment; no trailing slash; path case kept."""
    parts = urlsplit(url.strip())
    host = _bare_host(parts.hostname or "")
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("", host, path, parts.query, ""))


async def check(url: str, *, domain: str, client: httpx.AsyncClient) -> UrlCheck:
    """GET `url`, following redirects on-domain only, and say where it ended."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        if not on_domain(current, domain):
            return UrlCheck(url=url, status="off_domain", final_url=current, http_status=None)
        try:
            async with client.stream(
                "GET", current, follow_redirects=False, timeout=TIMEOUT_S
            ) as response:
                status = response.status_code
                location = response.headers.get("location")
        except httpx.HTTPError:
            return UrlCheck(url=url, status="unreachable", final_url=None, http_status=None)
        if 200 <= status < 300:
            return UrlCheck(url=url, status="ok", final_url=current, http_status=status)
        if status not in _REDIRECTS or not location:
            return UrlCheck(url=url, status="http_error", final_url=current, http_status=status)
        current = urljoin(current, location)
    return UrlCheck(url=url, status="too_many_redirects", final_url=current, http_status=None)


def new_client() -> httpx.AsyncClient:
    """The client `check_all` opens when it is given none. One seam, for the tests."""
    return httpx.AsyncClient()


async def check_all(
    urls: Iterable[str], *, domain: str, client: httpx.AsyncClient | None = None
) -> dict[str, UrlCheck]:
    """Every distinct URL checked once, `CONCURRENCY` at a time."""
    distinct = list(dict.fromkeys(urls))
    if client is None:
        async with new_client() as owned:
            return await check_all(distinct, domain=domain, client=owned)
    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def one(url: str) -> UrlCheck:
        async with semaphore:
            return await check(url, domain=domain, client=client)

    results = await asyncio.gather(*(one(url) for url in distinct))
    return dict(zip(distinct, results, strict=True))


def unique(checks: Sequence[UrlCheck]) -> list[UrlCheck]:
    """The checks in order, a later `ok` landing on an earlier `ok`'s page marked `duplicate`."""
    seen: set[str] = set()
    marked: list[UrlCheck] = []
    for item in checks:
        if item.status == "ok" and item.final_url is not None:
            page = canonical(item.final_url)
            if page in seen:
                item = replace(item, status="duplicate")
            else:
                seen.add(page)
        marked.append(item)
    return marked
