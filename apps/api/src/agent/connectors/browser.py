"""The one place a real browser is driven.

Two connectors need Chromium — `transparency` (PRD §9.2) and the optional
vitals pass in `web_crawler` (§9.4) — and both want the same launch flags,
the same politeness delays and the same screenshot path. Keeping that in one
module means a selector repair or a stealth tweak happens once.

On `playwright-stealth`: §9.2 names it, and it is not a dependency here. What it
does that matters for a public, unauthenticated ad-library page is a handful of
init scripts that unset the automation tells; those are inlined below, which is
a dozen lines against a package that patches broadly and breaks on Playwright
upgrades. If a selector-level block ever shows up, that is the moment to
reconsider — not before.

This module is import-safe without a browser installed. Playwright is imported
inside the functions, so the API process, which never opens one, pays nothing.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import structlog

from agent.config import Settings, get_settings

log = structlog.get_logger(__name__)

#: Flags that remove the obvious "this is automation" signals.
LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",  # /dev/shm is tiny in a container; Chromium crashes without this
    "--no-sandbox",
    "--disable-gpu",
]

#: Runs before any page script. `navigator.webdriver` is the tell that matters.
STEALTH_INIT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || {runtime: {}};
"""

VIEWPORT: dict[str, int] = {"width": 1440, "height": 900}
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

#: Collected in-page. LCP and CLS come from PerformanceObserver; TBT is
#: approximated from long tasks, which is what Lighthouse does for a field run.
VITALS_SCRIPT = """
() => new Promise((resolve) => {
  const out = {lcp: null, cls: 0, tbt: 0};
  try {
    new PerformanceObserver((list) => {
      const entries = list.getEntries();
      out.lcp = entries[entries.length - 1].startTime;
    }).observe({type: 'largest-contentful-paint', buffered: true});
    new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) {
        if (!entry.hadRecentInput) out.cls += entry.value;
      }
    }).observe({type: 'layout-shift', buffered: true});
    new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) {
        out.tbt += Math.max(0, entry.duration - 50);
      }
    }).observe({type: 'longtask', buffered: true});
  } catch (e) { out.error = String(e); }
  setTimeout(() => resolve(out), 2500);
})
"""


class BrowserUnavailable(RuntimeError):
    """Playwright or its Chromium build is not installed in this image."""


async def polite_delay(settings: Settings | None = None) -> None:
    """Wait a randomised 2–5s (PRD §9.2). Randomised so the cadence is not a fingerprint."""
    settings = settings or get_settings()
    delay = random.uniform(  # noqa: S311 — crawl politeness, not a secret
        settings.transparency_min_delay_s, settings.transparency_max_delay_s
    )
    await asyncio.sleep(delay)


@asynccontextmanager
async def browser_page(settings: Settings | None = None) -> AsyncIterator[Any]:
    """One Chromium page, configured and torn down.

    One page at a time by design — §9.2 says one concurrent page, and the
    politeness budget is the point rather than a limitation to engineer around.
    """
    settings = settings or get_settings()
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise BrowserUnavailable("playwright is not installed") from exc

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=True, args=LAUNCH_ARGS)
        except Exception as exc:  # noqa: BLE001 — surfaces as "run `playwright install`"
            raise BrowserUnavailable(f"could not launch Chromium: {exc}") from exc
        context = await browser.new_context(
            viewport={"width": VIEWPORT["width"], "height": VIEWPORT["height"]},
            user_agent=USER_AGENT,
            locale="en-US",
        )
        await context.add_init_script(STEALTH_INIT)
        page = await context.new_page()
        try:
            yield page
        finally:
            await context.close()
            await browser.close()


async def measure_vitals(
    urls: list[str], settings: Settings | None = None
) -> dict[str, dict[str, Any]]:
    """Core Web Vitals per URL. A page that fails to load is omitted, not faked."""
    settings = settings or get_settings()
    measured: dict[str, dict[str, Any]] = {}
    if not urls:
        return measured
    async with browser_page(settings) as page:
        for url in urls:
            try:
                await page.goto(url, wait_until="networkidle", timeout=30_000)
                result = await page.evaluate(VITALS_SCRIPT)
                measured[url] = {
                    "lcp_ms": round(result.get("lcp") or 0, 1),
                    "cls": round(result.get("cls") or 0, 4),
                    "tbt_ms": round(result.get("tbt") or 0, 1),
                }
            except Exception as exc:  # noqa: BLE001 — one bad page must not end the pass
                log.warning("browser.vitals_failed", url=url, error=str(exc))
    return measured
