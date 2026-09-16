"""Our own site and its landing pages (PRD §9.4).

`httpx` + `selectolax`, breadth-first from `sitemap.xml`, capped at 500 URLs and
depth 3. The caps are in `Settings`, not here, because a crawl that quietly runs
away is the failure mode that costs a run its time budget.

What it extracts is chosen by what stages 1.1 and 1.5 ask of it: the page's
promise (title, H1, meta description), its ask (primary CTA, form fields), its
proof (schema.org blocks, trust markers), and enough structure to tell a landing
page from a blog post.

Core Web Vitals need a real browser and live in `browser.py`; this connector
calls into it only when `params["vitals"]` is set, so the common crawl stays
pure HTTP and finishes in seconds.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse
from xml.etree import ElementTree

import httpx
import structlog
from selectolax.parser import HTMLParser

from agent.connectors.base import (
    BaseConnector,
    ConnectorDegraded,
    ConnectorError,
    ConnectorStatus,
    EvidenceDraft,
    build_client,
)
from agent.db.models import EvidenceSource

log = structlog.get_logger(__name__)

#: Regexes for the credibility signals §9.4 calls "visible trust markers".
TRUST_PATTERNS: dict[str, re.Pattern[str]] = {
    "iso_certification": re.compile(r"\bISO[\s/-]?\d{4,5}\b", re.I),
    "soc2": re.compile(r"\bSOC\s?2\b", re.I),
    "gdpr": re.compile(r"\bGDPR\b", re.I),
    "customer_count": re.compile(r"\b[\d,]{3,}\+?\s+(customers|companies|users|clients)\b", re.I),
    "testimonial": re.compile(r"\b(testimonial|case study|success story)\b", re.I),
    "certification": re.compile(r"\b(certified|accredited|compliance)\b", re.I),
}

#: Words that mark a button as the page's real ask rather than nav furniture.
CTA_HINTS = (
    "demo",
    "trial",
    "quote",
    "contact",
    "get started",
    "sign up",
    "book",
    "buy",
    "pricing",
    "download",
)

SKIP_EXTENSIONS = (
    ".pdf",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".svg",
    ".webp",
    ".zip",
    ".mp4",
    ".mp3",
    ".css",
    ".js",
    ".ico",
    ".woff",
    ".woff2",
)


def normalise(url: str) -> str:
    """Drop the fragment and any trailing slash so one page is one URL."""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme, parsed.netloc, path, "", parsed.query, ""))


def same_site(url: str, root: str) -> bool:
    return urlparse(url).netloc.lower().removeprefix("www.") == (
        urlparse(root).netloc.lower().removeprefix("www.")
    )


class WebCrawlerConnector(BaseConnector):
    """Breadth-first crawl of one site, one evidence row per page."""

    name = "web_crawler"
    source = EvidenceSource.WEB

    async def fetch(self, params: dict[str, Any]) -> list[EvidenceDraft]:
        """`params`: `{url | domain, max_urls?, max_depth?, vitals?}`."""
        root = self._root(params)
        max_urls = int(params.get("max_urls") or self.settings.crawl_max_urls)
        max_depth = int(params.get("max_depth") or self.settings.crawl_max_depth)

        owned = self.context.client is None
        client = self.context.client or build_client(self.settings)
        drafts: list[EvidenceDraft] = []
        failures: list[str] = []
        try:
            seeds = await self._sitemap_urls(client, root)
            if seeds:
                log.info("web_crawler.sitemap", root=root, urls=len(seeds))
            else:
                # No sitemap is normal, not an error. Fall back to link-following.
                log.info("web_crawler.no_sitemap", root=root)
                seeds = [root]

            seen: set[str] = set()
            queue: list[tuple[str, int]] = [(normalise(url), 0) for url in seeds[:max_urls]]
            semaphore = asyncio.Semaphore(self.settings.crawl_concurrency)

            while queue and len(seen) < max_urls:
                batch = queue[: self.settings.crawl_concurrency]
                queue = queue[self.settings.crawl_concurrency :]
                batch = [(url, depth) for url, depth in batch if url not in seen]
                for url, _ in batch:
                    seen.add(url)

                results = await asyncio.gather(
                    *(self._page(client, url, semaphore) for url, _ in batch),
                    return_exceptions=True,
                )
                for (url, depth), result in zip(batch, results, strict=True):
                    if isinstance(result, BaseException):
                        failures.append(f"{url}: {result}")
                        continue
                    payload, links = result
                    drafts.append(self.draft("page", payload, source_url=url))
                    if depth < max_depth:
                        for link in links:
                            candidate = normalise(link)
                            if (
                                candidate not in seen
                                and same_site(candidate, root)
                                and len(seen) + len(queue) < max_urls
                            ):
                                queue.append((candidate, depth + 1))
        finally:
            if owned:
                await client.aclose()

        if params.get("vitals"):
            try:
                drafts.extend(await self._vitals([d.source_url or "" for d in drafts][:20]))
            except Exception as exc:  # noqa: BLE001 — a browser failure must not lose the crawl
                failures.append(f"vitals: {exc}")

        if failures:
            raise ConnectorDegraded(
                f"{len(failures)} of {len(failures) + len(drafts)} fetches failed: "
                + "; ".join(failures[:5]),
                drafts,
            )
        return drafts

    def _root(self, params: dict[str, Any]) -> str:
        raw = str(params.get("url") or params.get("domain") or "").strip()
        if not raw:
            raise ConnectorError("web_crawler.fetch needs a `url` or `domain`")
        if not raw.startswith(("http://", "https://")):
            raw = "https://" + raw
        return normalise(raw)

    async def _sitemap_urls(self, client: httpx.AsyncClient, root: str) -> list[str]:
        """Read `sitemap.xml`, following one level of sitemap index."""
        found: list[str] = []
        try:
            response = await client.get(urljoin(root + "/", "/sitemap.xml"))
            if response.status_code >= 400:
                return []
            found = self._parse_sitemap(response.text)
            indexes = [url for url in found if url.endswith(".xml")]
            if indexes:
                pages = [url for url in found if not url.endswith(".xml")]
                for index in indexes[:10]:
                    child = await client.get(index)
                    if child.status_code < 400:
                        pages.extend(
                            url
                            for url in self._parse_sitemap(child.text)
                            if not url.endswith(".xml")
                        )
                found = pages
        except (httpx.HTTPError, ElementTree.ParseError) as exc:
            log.info("web_crawler.sitemap_unreadable", root=root, error=str(exc))
            return []
        return [url for url in found if not url.lower().endswith(SKIP_EXTENSIONS)]

    @staticmethod
    def _parse_sitemap(xml: str) -> list[str]:
        try:
            tree = ElementTree.fromstring(xml)  # noqa: S314 — our own site's sitemap
        except ElementTree.ParseError:
            return []
        # Sitemaps are namespaced; match on the local name to avoid hardcoding it.
        return [
            element.text.strip()
            for element in tree.iter()
            if element.tag.rsplit("}", 1)[-1] == "loc" and element.text
        ]

    async def _page(
        self, client: httpx.AsyncClient, url: str, semaphore: asyncio.Semaphore
    ) -> tuple[dict[str, Any], list[str]]:
        async with semaphore:
            await asyncio.sleep(self.settings.crawl_delay_s)
            response = await client.get(url)
            response.raise_for_status()
            if "html" not in response.headers.get("content-type", ""):
                raise ConnectorError(f"not HTML: {response.headers.get('content-type')}")
            return self._extract(url, response.text, response.status_code)

    def _extract(self, url: str, html: str, status: int) -> tuple[dict[str, Any], list[str]]:
        tree = HTMLParser(html)
        # Read JSON-LD before stripping scripts — schema.org blocks live inside
        # a <script type="application/ld+json">, so the order here is the whole
        # difference between finding structured data and reporting none.
        schema_types = self._schema_types(tree)
        for node in tree.css("script, style, noscript"):
            node.decompose()
        body_text = tree.body.text(separator=" ", strip=True) if tree.body else ""

        payload = {
            "url": url,
            "status": status,
            "title": self._text(tree, "title"),
            "h1": self._text(tree, "h1"),
            "h2": [node.text(strip=True) for node in tree.css("h2")][:15],
            "meta_description": self._attr(tree, 'meta[name="description"]', "content"),
            "canonical": self._attr(tree, 'link[rel="canonical"]', "href"),
            "primary_cta": self._cta(tree),
            "form_fields": self._form_fields(tree),
            "word_count": len(body_text.split()),
            "https": url.startswith("https://"),
            "mobile_viewport": bool(tree.css_first('meta[name="viewport"]')),
            "schema_types": schema_types,
            "trust_markers": sorted(
                name for name, pattern in TRUST_PATTERNS.items() if pattern.search(body_text)
            ),
            # Capped: this is the retrieval surface, not an archive of the page.
            "text_excerpt": body_text[:2000],
        }
        skip_prefixes = ("#", "mailto:", "tel:", "javascript:")
        hrefs = [(node.attributes.get("href") or "").strip() for node in tree.css("a[href]")]
        links = [
            urljoin(url, href) for href in hrefs if href and not href.startswith(skip_prefixes)
        ]
        return payload, [link for link in links if not link.lower().endswith(SKIP_EXTENSIONS)]

    @staticmethod
    def _text(tree: HTMLParser, selector: str) -> str | None:
        node = tree.css_first(selector)
        return node.text(strip=True) if node else None

    @staticmethod
    def _attr(tree: HTMLParser, selector: str, attribute: str) -> str | None:
        node = tree.css_first(selector)
        return node.attributes.get(attribute) if node else None

    @staticmethod
    def _cta(tree: HTMLParser) -> str | None:
        """The first button or link whose text sounds like the page's ask."""
        for node in tree.css("a, button"):
            label = node.text(strip=True)
            if label and 2 < len(label) < 60:
                lowered = label.lower()
                if any(hint in lowered for hint in CTA_HINTS):
                    return label
        return None

    @staticmethod
    def _form_fields(tree: HTMLParser) -> list[str]:
        fields: list[str] = []
        for node in tree.css("form input, form select, form textarea"):
            attributes = node.attributes
            if (attributes.get("type") or "").lower() in {"hidden", "submit", "button"}:
                continue
            name = attributes.get("name") or attributes.get("id") or attributes.get("placeholder")
            if name:
                fields.append(name)
        return fields[:25]

    @staticmethod
    def _schema_types(tree: HTMLParser) -> list[str]:
        """schema.org `@type` values, from JSON-LD and from microdata."""
        import json

        types: set[str] = set()
        for node in tree.css('script[type="application/ld+json"]'):
            try:
                data = json.loads(node.text())
            except (ValueError, TypeError):
                continue
            for entry in data if isinstance(data, list) else [data]:
                if isinstance(entry, dict) and entry.get("@type"):
                    value = entry["@type"]
                    types.update(value if isinstance(value, list) else [value])
        for node in tree.css("[itemtype]"):
            itemtype = node.attributes.get("itemtype") or ""
            if "schema.org" in itemtype:
                types.add(itemtype.rsplit("/", 1)[-1])
        return sorted(str(value) for value in types)

    async def _vitals(self, urls: list[str]) -> list[EvidenceDraft]:
        """Core Web Vitals, measured in a real browser."""
        from agent.connectors.browser import measure_vitals

        drafts: list[EvidenceDraft] = []
        for url, measured in (await measure_vitals([u for u in urls if u], self.settings)).items():
            drafts.append(self.draft("page_vitals", {"url": url, **measured}, source_url=url))
        return drafts

    async def test_connection(self) -> ConnectorStatus:
        return ConnectorStatus(ok=True, detail="no credentials required")
