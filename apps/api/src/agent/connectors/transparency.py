"""Google Ads Transparency Center — competitor and own live ads (PRD §9.2).

The scrape is deliberately split in two. `parse_ad_cards` is a pure function
over HTML: no browser, no network, fully testable against a saved page. The
browser half only navigates, scrolls and hands that function a DOM snapshot.
That split is what makes a selector regression reproducible — a captured page
from the day it broke replays forever.

Failure policy follows §9.2 exactly. A selector that stops matching does not
end the run: the fallback DOM-text heuristic tries to recover the card, the
failing HTML is written to `debug/` for repair, and `ConnectorDegraded` carries
whatever *was* recovered upward so the report can flag reduced coverage.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import structlog
from selectolax.parser import HTMLParser, Node

from agent.connectors import selectors
from agent.connectors.base import (
    BaseConnector,
    ConnectorDegraded,
    ConnectorError,
    ConnectorStatus,
    EvidenceDraft,
    build_client,
)
from agent.connectors.browser import BrowserUnavailable, browser_page, polite_delay
from agent.db.models import EvidenceSource
from agent.storage.backend import StorageBackend, get_storage

log = structlog.get_logger(__name__)

#: "Jan 3, 2025 – Feb 9, 2025", with either dash, and an open-ended right side.
DATE_RANGE = re.compile(
    r"([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})\s*[–—-]\s*([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4}|Present)",
)
CREATIVE_ID = re.compile(r"/creative/(CR[0-9A-Za-z]+)")
FORMATS = ("text", "image", "video")


def _first(node: Node | HTMLParser, candidates: list[str]) -> Node | None:
    """Try each selector in order. The fallback chain §9.2 asks for."""
    for selector in candidates:
        try:
            found = node.css_first(selector)
        except (ValueError, SyntaxError):
            # Playwright-only pseudo-selectors (`:has-text`) are not CSS.
            continue
        if found is not None:
            return found
    return None


def _parse_date(value: str) -> str | None:
    if value.lower() == "present":
        return None
    try:
        return datetime.strptime(value, "%b %d, %Y").replace(tzinfo=UTC).date().isoformat()
    except ValueError:
        return None


def parse_ad_cards(
    html: str, *, advertiser: str, region: str | None = None
) -> list[dict[str, Any]]:
    """Every ad on one rendered grid page. Pure: give it HTML, get back payloads.

    Falls back to text heuristics per card rather than per page — one card with
    an unrecognised layout should cost that card's fields, not the whole scrape.
    """
    tree = HTMLParser(html)

    # The selector that matches the *most* nodes wins, not the first that
    # matches any. First-match is one stray node away from disaster: a single
    # hidden `creative-preview` template left in the DOM after a redesign would
    # return one card and silently drop the fifty real ones under whatever
    # selector replaced it. Ties go to the earlier, more specific candidate.
    cards: list[Node] = []
    for selector in selectors.AD_CARD:
        try:
            found = tree.css(selector)
        except (ValueError, SyntaxError):
            # Playwright-only pseudo-selectors are not valid CSS here.
            continue
        if len(found) > len(cards):
            cards = found

    parsed: list[dict[str, Any]] = []
    for card in cards:
        text_node = _first(card, selectors.AD_CREATIVE_TEXT)
        creative_text = text_node.text(strip=True) if text_node else card.text(strip=True)

        link = _first(card, selectors.AD_DESTINATION_LINK)
        destination = link.attributes.get("href") if link else None

        image = _first(card, selectors.AD_IMAGE)
        image_url = image.attributes.get("src") if image else None
        video = _first(card, selectors.AD_VIDEO)

        ad_id = card.attributes.get("data-creative-id")
        if not ad_id:
            # The id is in the permalink when it is not an attribute.
            match = CREATIVE_ID.search(card.html or "")
            ad_id = match.group(1) if match else None

        badge = _first(card, selectors.AD_FORMAT_BADGE)
        format_label = (badge.text(strip=True).lower() if badge else "") or None
        if format_label not in FORMATS:
            # Heuristic fallback: an <img> means image, an <iframe> means video.
            if video is not None:
                format_label = "video"
            elif image_url:
                format_label = "image"
            else:
                format_label = "text"

        first_shown = last_shown = None
        date_node = _first(card, selectors.AD_DATE_RANGE)
        haystack = date_node.text(strip=True) if date_node else card.text(strip=True)
        match = DATE_RANGE.search(haystack or "")
        if match:
            first_shown = _parse_date(match.group(1))
            last_shown = _parse_date(match.group(2))

        parsed.append(
            {
                "advertiser": advertiser,
                "ad_id": ad_id,
                "format": format_label,
                "first_shown": first_shown,
                "last_shown": last_shown,
                "creative_text": (creative_text or "")[:2000],
                "image_url": image_url,
                "destination_url": destination,
                "regions": [region] if region else [],
                # Filled in by `_scrape_advertiser` once the grid is captured.
                # Present here so a card parsed from a saved page has the same
                # keys as one scraped live.
                "screenshot_path": None,
            }
        )
    return parsed


class TransparencyConnector(BaseConnector):
    """Competitor creative corpus, screenshots, and the landing pages behind the ads."""

    name = "transparency"
    source = EvidenceSource.TRANSPARENCY

    def __init__(self, context: Any = None, storage: StorageBackend | None = None) -> None:
        super().__init__(context)
        self._storage = storage

    @property
    def storage(self) -> StorageBackend:
        if self._storage is None:
            self._storage = get_storage(self.settings)
        return self._storage

    async def fetch(self, params: dict[str, Any]) -> list[EvidenceDraft]:
        """`params`: `{advertisers: [...], region?, format?, max_ads?, landing_pages?}`."""
        advertisers = [str(name).strip() for name in (params.get("advertisers") or []) if name]
        if not advertisers:
            raise ConnectorError("transparency.fetch needs at least one advertiser")
        region = params.get("region")
        max_ads = int(params.get("max_ads") or self.settings.transparency_max_ads)

        drafts: list[EvidenceDraft] = []
        failures: list[str] = []
        try:
            async with browser_page(self.settings) as page:
                for advertiser in advertisers:
                    try:
                        cards = await self._scrape_advertiser(page, advertiser, region, max_ads)
                    except ConnectorDegraded as exc:
                        failures.append(f"{advertiser}: {exc.reason}")
                        cards = [draft.payload for draft in exc.drafts]
                    except Exception as exc:  # noqa: BLE001 — one advertiser, not the batch
                        log.warning(
                            "transparency.advertiser_failed", name=advertiser, error=str(exc)
                        )
                        failures.append(f"{advertiser}: {exc}")
                        continue
                    drafts.extend(
                        self.draft(
                            "competitor_creative",
                            card,
                            source_url=self._permalink(card),
                        )
                        for card in cards
                    )
        except BrowserUnavailable as exc:
            raise ConnectorError(str(exc)) from exc

        if params.get("landing_pages"):
            destinations = {
                draft.payload.get("destination_url")
                for draft in drafts
                if draft.payload.get("destination_url")
            }
            try:
                drafts.extend(await self._landing_pages(sorted(filter(None, destinations))[:25]))
            except Exception as exc:  # noqa: BLE001 — the creatives still stand on their own
                failures.append(f"landing_pages: {exc}")

        if failures:
            raise ConnectorDegraded("; ".join(failures), drafts)
        return drafts

    def _permalink(self, card: dict[str, Any]) -> str | None:
        ad_id = card.get("ad_id")
        if not ad_id:
            return None
        return f"{self.settings.transparency_base_url}/advertiser/creative/{ad_id}"

    async def _scrape_advertiser(
        self, page: Any, advertiser: str, region: str | None, max_ads: int
    ) -> list[dict[str, Any]]:
        """Search, paginate, screenshot. One page at a time, 2–5s between actions."""
        url = f"{self.settings.transparency_base_url}/?region={region or 'anywhere'}"
        await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        await polite_delay(self.settings)

        box = await self._find(page, selectors.ADVERTISER_SEARCH_INPUT)
        if box is None:
            await self._save_debug(await page.content(), advertiser, "no-search-box")
            raise ConnectorDegraded(f"search box not found for {advertiser}")
        await box.fill(advertiser)
        await polite_delay(self.settings)

        suggestion = await self._find(page, selectors.ADVERTISER_SUGGESTION)
        if suggestion is not None:
            await suggestion.click()
        else:
            await box.press("Enter")
        await polite_delay(self.settings)

        collected: list[dict[str, Any]] = []
        seen: set[str] = set()
        for _ in range(20):  # bounded: 20 scrolls × one grid page each
            html = await page.content()
            for card in parse_ad_cards(html, advertiser=advertiser, region=region):
                key = card.get("ad_id") or card.get("creative_text", "")[:120]
                if key and key not in seen:
                    seen.add(key)
                    collected.append(card)
            if len(collected) >= max_ads:
                break
            more = await self._find(page, selectors.LOAD_MORE)
            if more is None:
                await page.mouse.wheel(0, 4000)
            else:
                await more.click()
            await polite_delay(self.settings)

        if not collected:
            empty = await self._find(page, selectors.EMPTY_STATE)
            if empty is None:
                # No ads *and* no empty-state marker means the selectors moved.
                await self._save_debug(await page.content(), advertiser, "no-cards")
                raise ConnectorDegraded(f"no ad cards and no empty-state marker for {advertiser}")
            return []

        collected = collected[:max_ads]
        # Stamped onto every card rather than discarded. Until now `_screenshot`
        # wrote a PNG nobody could find again: the key never left this method,
        # so PRD §10's `screenshot_path` on node 1.3.2 had no way to be filled.
        # One full-page capture of the scrolled grid covers the cards collected
        # from it — it is the grid the ad was seen in, not a crop of the ad, and
        # `screenshot_path` is documented that way downstream.
        key = await self._screenshot(page, advertiser)
        for card in collected:
            card["screenshot_path"] = key
        return collected

    @staticmethod
    async def _find(page: Any, candidates: list[str]) -> Any | None:
        for selector in candidates:
            locator = page.locator(selector).first
            try:
                if await locator.count() and await locator.is_visible():
                    return locator
            except Exception:  # noqa: BLE001, S112 — an invalid selector is just a miss
                continue
        return None

    async def _screenshot(self, page: Any, advertiser: str) -> str | None:
        """Write the grid through `StorageBackend`, never straight to disk (PRD Law 10)."""
        run_id = self.context.run_id or "adhoc"
        slug = re.sub(r"[^a-z0-9]+", "-", advertiser.lower()).strip("-") or "advertiser"
        key = f"creatives/{run_id}/{slug}-{uuid.uuid4().hex[:8]}.png"
        try:
            image = await page.screenshot(full_page=True)
        except Exception as exc:  # noqa: BLE001 — a screenshot is evidence, not the point
            log.warning("transparency.screenshot_failed", advertiser=advertiser, error=str(exc))
            return None
        self.storage.put(key, image, content_type="image/png")
        return key

    async def _save_debug(self, html: str, advertiser: str, reason: str) -> None:
        """Keep the page that broke the selectors. §9.2: 'save failure HTML for repair'."""
        slug = re.sub(r"[^a-z0-9]+", "-", f"{advertiser}-{reason}".lower()).strip("-")
        key = f"debug/transparency/{slug}-{uuid.uuid4().hex[:8]}.html"
        try:
            self.storage.put(key, html.encode("utf-8"), content_type="text/html")
            log.warning("transparency.selector_miss", advertiser=advertiser, reason=reason, key=key)
        except Exception as exc:  # noqa: BLE001 — diagnostics must never mask the failure
            log.warning("transparency.debug_save_failed", error=str(exc))

    async def _landing_pages(self, urls: list[str]) -> list[EvidenceDraft]:
        """Fetch each destination and extract it with the site crawler's parser.

        Reuses `WebCrawlerConnector._extract` rather than writing a second
        extractor, so a competitor's page and our own are described by the same
        fields and stage 1.3 can compare them directly.
        """
        from agent.connectors.web_crawler import WebCrawlerConnector

        extractor = WebCrawlerConnector(self.context)
        drafts: list[EvidenceDraft] = []
        client = build_client(self.settings)
        try:
            for url in urls:
                try:
                    response = await client.get(url)
                    if response.status_code >= 400 or "html" not in response.headers.get(
                        "content-type", ""
                    ):
                        continue
                    payload, _ = extractor._extract(url, response.text, response.status_code)  # noqa: SLF001
                except Exception as exc:  # noqa: BLE001 — one dead landing page is normal
                    log.info("transparency.landing_page_failed", url=url, error=str(exc))
                    continue
                payload["competitor_domain"] = urlparse(url).netloc
                drafts.append(self.draft("competitor_landing_page", payload, source_url=url))
        finally:
            await client.aclose()
        return drafts

    async def test_connection(self) -> ConnectorStatus:
        """Open the Transparency Center and confirm the search box is still there."""
        try:
            async with browser_page(self.settings) as page:
                await page.goto(
                    self.settings.transparency_base_url,
                    wait_until="domcontentloaded",
                    timeout=45_000,
                )
                box = await self._find(page, selectors.ADVERTISER_SEARCH_INPUT)
        except BrowserUnavailable as exc:
            return ConnectorStatus(ok=False, detail=str(exc))
        except Exception as exc:  # noqa: BLE001 — a test must report, never raise
            return ConnectorStatus(
                ok=False, detail=f"could not reach the Transparency Center: {exc}"
            )
        if box is None:
            return ConnectorStatus(ok=False, detail="reachable, but the search box selector missed")
        return ConnectorStatus(ok=True, detail="Transparency Center reachable, selectors matching")
