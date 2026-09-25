"""Playwright render + DOM extraction for Stage 4.5 (Stage 04 PRD §10.2, §13, law 41).

Each distinct `landing_url` is loaded at `landing.viewport_mobile` and
`landing.viewport_desktop`, waits for `networkidle` (20 s), and yields the
facts 4.5.1 and 4.5.2 judge: the `h1`, every visible text node with its box,
the fold line (`window.innerHeight`), the form controls (`input|select|
textarea`: name, label, type, required), the final URL after redirects and the
HTTP status. Boxes are measured from the DOM, independently of any overlay; a
`position: fixed` element covering more than 30% of the viewport is recorded
as `obscured_by_overlay`.

**GETs only, and proved rather than promised (law 41, §13).** Every request of
every render passes one route handler. A non-GET never reaches the network: a
fetch, XHR, beacon or subresource is aborted, and a navigation — a script
submitting a form — is answered locally with `204 No Content`, which a
browser treats as "stay on this page", so the document being audited is not
replaced by an error page. Each blocked request is recorded, so the render
says what the page tried. Nothing here clicks, types, scrolls to trigger or
submits anything: consent banners are measured, never dismissed.

**No cookie outlives its render context.** Each (URL, device) render gets a
fresh browser context — no `storage_state` in, none saved out — and service
workers are blocked so no request can bypass the route handler.

**Concurrency 1.** One Chromium, one page at a time, and one render pass at a
time per process: a second caller waits for the first to finish.

This module is import-safe without a browser installed. Playwright is imported
inside the render, so the API process, which never opens one, pays nothing.
"""

from __future__ import annotations

import asyncio
import re
import time
import weakref
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Final, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field

from agent.connectors.browser import LAUNCH_ARGS, USER_AGENT, BrowserUnavailable
from agent.db.models import EvidenceSource
from agent.evidence.normalize import MAX_CONTENT_TEXT, EvidenceDraft
from agent.schemas.landing import Box, Device

if TYPE_CHECKING:
    from playwright.async_api import Browser, Route

log = structlog.get_logger(__name__)

DEVICES: Final[tuple[Device, ...]] = ("mobile", "desktop")

#: §10.2: "waits for `networkidle` (timeout 20 s)".
NETWORKIDLE_TIMEOUT_MS: Final = 20_000
#: §10.2: "a fixed overlay covering > 30% of the viewport".
OVERLAY_OBSCURES: Final = 0.30
#: A bound on what one render can put into an Evidence payload. A page with
#: more visible text nodes than this is recorded as truncated, never silently.
MAX_TEXT_NODES: Final = 5_000
MAX_OVERLAYS: Final = 20

#: A phone. The desktop render uses `connectors.browser.USER_AGENT`; a site that
#: picks its markup by user agent must get its mobile markup at 390 wide.
MOBILE_USER_AGENT: Final = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"
)

#: Controls a visitor does not fill in. Recorded, never part of a form's fields.
NOT_FILLABLE: Final = frozenset({"hidden", "submit", "button", "reset", "image"})

_VIEWPORT = re.compile(r"^\s*(\d+)\s*x\s*(\d+)\s*$")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Viewport(_Model):
    width: int = Field(gt=0)
    height: int = Field(gt=0)


def parse_viewport(value: str) -> Viewport:
    """`"390x844"` → `Viewport(390, 844)`: the constants' spelling of a viewport."""
    match = _VIEWPORT.match(value)
    if match is None:
        raise ValueError(f"viewport {value!r} is not WIDTHxHEIGHT")
    return Viewport(width=int(match[1]), height=int(match[2]))


class TextNode(_Model):
    text: str
    box: Box


class FormControl(_Model):
    #: Index into `document.forms`; None for a control outside any form.
    form: int | None
    tag: Literal["input", "select", "textarea"]
    name: str | None
    id: str | None
    label: str | None
    #: `input.type`, `select-one` / `select-multiple`, or `textarea`.
    type: str
    required: bool
    visible: bool

    @property
    def fillable(self) -> bool:
        """A field a visitor fills in — not hidden, not a button."""
        return self.type not in NOT_FILLABLE


class Overlay(_Model):
    tag: str
    id: str | None
    #: Share of the viewport this fixed element covers, 0–1.
    fraction: float
    box: Box


class BlockedRequest(_Model):
    method: str
    url: str
    resource_type: str
    navigation: bool


class DeviceRender(_Model):
    device: Device
    viewport: Viewport
    user_agent: str
    #: The page answered with an HTTP response. False: DNS, refused, TLS,
    #: timeout — no final URL, no status, no facts.
    reached: bool
    error: str | None = None
    final_url: str | None = None
    http_status: int | None = None
    #: `networkidle` arrived within the timeout. A page that never idles (a
    #: long-poll, a chat widget) is still measured, and says so here.
    settled: bool = False
    fold_px: int | None = None
    h1: str | None = None
    text_nodes: list[TextNode] = Field(default_factory=list)
    text_nodes_truncated: bool = False
    controls: list[FormControl] = Field(default_factory=list)
    overlays: list[Overlay] = Field(default_factory=list)
    obscured_by_overlay: bool = False
    #: Every non-GET the page attempted; none left the browser.
    blocked: list[BlockedRequest] = Field(default_factory=list)
    #: GETs the page issued, counted.
    get_requests: int = 0
    #: Full-page PNG. Never serialised: the node writes it through
    #: `StorageBackend` and records the key.
    screenshot: bytes | None = Field(default=None, exclude=True, repr=False)

    @property
    def ok(self) -> bool:
        """Reached, measured, and answered 2xx — a page there is something to audit on."""
        return (
            self.reached
            and self.error is None
            and self.http_status is not None
            and 200 <= self.http_status < 300
        )


class LandingRender(_Model):
    url: str
    mobile: DeviceRender
    desktop: DeviceRender

    def device(self, device: Device) -> DeviceRender:
        return self.mobile if device == "mobile" else self.desktop


# ---------------------------------------------------------------------------
# Evidence (§10.2: "DOM facts become Evidence(source='web', kind='landing_dom')")
# ---------------------------------------------------------------------------

RENDER_KIND: Final = "landing_render"
DOM_KIND: Final = "landing_dom"
#: What `landing_render` carries: how the page answered. `landing_dom` carries
#: what was on it. Together they rebuild the `DeviceRender`, bar the pixels.
_RENDER_FIELDS: Final = frozenset(
    {
        "device", "viewport", "user_agent", "reached", "error", "final_url", "http_status",
        "settled", "fold_px", "overlays", "obscured_by_overlay", "blocked", "get_requests",
    }
)  # fmt: skip
_DOM_FIELDS: Final = frozenset({"device", "h1", "text_nodes", "text_nodes_truncated", "controls"})


def evidence_drafts(
    render: LandingRender, screenshots: Mapping[Device, str | None]
) -> list[EvidenceDraft]:
    """Two drafts per device: `landing_render` (with the screenshot's storage
    key) and `landing_dom`."""
    drafts: list[EvidenceDraft] = []
    for device in DEVICES:
        rendered = render.device(device)
        facts = rendered.model_dump(mode="json")
        status = (
            f"HTTP {rendered.http_status} at {rendered.final_url}"
            if rendered.reached
            else f"no answer ({rendered.error})"
        )
        drafts.append(
            EvidenceDraft(
                source=EvidenceSource.WEB,
                kind=RENDER_KIND,
                source_url=render.url,
                payload={
                    "url": render.url,
                    "screenshot": screenshots.get(device),
                    **{key: facts[key] for key in sorted(_RENDER_FIELDS)},
                },
                content_text=f"Landing page {render.url} on {device}: {status}.",
            )
        )
        text = " ".join(node.text for node in rendered.text_nodes)
        drafts.append(
            EvidenceDraft(
                source=EvidenceSource.WEB,
                kind=DOM_KIND,
                source_url=render.url,
                payload={"url": render.url, **{key: facts[key] for key in sorted(_DOM_FIELDS)}},
                content_text=f"{rendered.h1 or ''}\n{text}".strip()[:MAX_CONTENT_TEXT] or None,
            )
        )
    return drafts


def from_evidence(rows: Iterable[tuple[str, Mapping[str, Any]]]) -> dict[str, LandingRender]:
    """`(kind, payload)` rows back into renders, by URL. A URL missing either
    kind for either device is not rebuilt: half a render is not a render."""
    parts: dict[tuple[str, str], dict[str, Any]] = {}
    for kind, payload in rows:
        if kind not in (RENDER_KIND, DOM_KIND):
            continue
        fields = _RENDER_FIELDS if kind == RENDER_KIND else _DOM_FIELDS
        key = (str(payload["url"]), str(payload["device"]))
        parts.setdefault(key, {"kinds": set()})["kinds"].add(kind)
        parts[key].update({name: payload[name] for name in fields if name in payload})
    renders: dict[str, LandingRender] = {}
    for url in dict.fromkeys(url for url, _ in parts):
        devices = {device: parts.get((url, device)) for device in DEVICES}
        if not all(item and item["kinds"] == {RENDER_KIND, DOM_KIND} for item in devices.values()):
            continue
        built = {
            device: DeviceRender.model_validate(
                {name: value for name, value in item.items() if name != "kinds"}
            )
            for device, item in devices.items()
            if item is not None
        }
        renders[url] = LandingRender(url=url, mobile=built["mobile"], desktop=built["desktop"])
    return renders


#: One evaluate call: every fact is read from the same DOM state.
_EXTRACT = """
({maxTextNodes, maxOverlays}) => {
  const norm = (s) => (s || "").replace(/\\s+/g, " ").trim();
  const visible = (el) =>
    !!el && el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true});
  const box = (r) => ({x: r.left + window.scrollX, y: r.top + window.scrollY,
                       width: r.width, height: r.height});
  const SKIP = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE"]);

  let h1 = null;
  for (const el of document.querySelectorAll("h1")) {
    const text = visible(el) ? norm(el.innerText) : "";
    if (text) { h1 = text; break; }
  }

  const textNodes = [];
  let truncated = false;
  const root = document.body || document.documentElement;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const range = document.createRange();
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const parent = node.parentElement;
    if (!parent || SKIP.has(parent.tagName) || !visible(parent)) continue;
    const text = norm(node.nodeValue);
    if (!text) continue;
    range.selectNodeContents(node);
    const rect = range.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) continue;
    if (textNodes.length >= maxTextNodes) { truncated = true; break; }
    textNodes.push({text, box: box(rect)});
  }

  const forms = Array.from(document.forms);
  const labelOf = (el) => {
    if (el.labels && el.labels.length) {
      const text = norm(el.labels[0].innerText);
      if (text) return text;
    }
    const aria = norm(el.getAttribute("aria-label"));
    if (aria) return aria;
    const ids = (el.getAttribute("aria-labelledby") || "").split(/\\s+/).filter(Boolean);
    const by = norm(ids.map((id) => (document.getElementById(id) || {}).innerText || "").join(" "));
    if (by) return by;
    return norm(el.getAttribute("placeholder")) || null;
  };
  const controls = Array.from(document.querySelectorAll("input, select, textarea")).map((el) => {
    const rect = el.getBoundingClientRect();
    const tag = el.tagName.toLowerCase();
    const type = tag === "input" ? (el.type || "text").toLowerCase()
      : tag === "select" ? (el.multiple ? "select-multiple" : "select-one") : "textarea";
    return {
      form: el.form ? forms.indexOf(el.form) : null,
      tag, type,
      name: el.getAttribute("name") || null,
      id: el.id || null,
      label: labelOf(el),
      required: !!el.required || el.getAttribute("aria-required") === "true",
      visible: visible(el) && rect.width > 0 && rect.height > 0,
    };
  });

  const vw = window.innerWidth, vh = window.innerHeight, area = vw * vh;
  const overlays = [];
  for (const el of document.querySelectorAll("body *")) {
    if (getComputedStyle(el).position !== "fixed" || !visible(el)) continue;
    const r = el.getBoundingClientRect();
    const w = Math.max(0, Math.min(r.right, vw) - Math.max(r.left, 0));
    const h = Math.max(0, Math.min(r.bottom, vh) - Math.max(r.top, 0));
    if (w * h <= 0) continue;
    overlays.push({tag: el.tagName.toLowerCase(), id: el.id || null,
                   fraction: area ? (w * h) / area : 0, box: box(r)});
  }
  overlays.sort((a, b) => b.fraction - a.fraction);

  return {h1, textNodes, truncated, controls, overlays: overlays.slice(0, maxOverlays),
          fold: vh};
}
"""

_LOCKS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
    weakref.WeakKeyDictionary()
)


@asynccontextmanager
async def _one_at_a_time() -> AsyncIterator[None]:
    """Concurrency 1 per process (§13). One lock per event loop, so a lock is
    never awaited from a loop it was not created on."""
    loop = asyncio.get_running_loop()
    lock = _LOCKS.get(loop)
    if lock is None:
        lock = _LOCKS[loop] = asyncio.Lock()
    async with lock:
        yield


async def render_pages(
    urls: Sequence[str],
    *,
    viewports: Mapping[Device, Viewport],
    timeout_ms: int = NETWORKIDLE_TIMEOUT_MS,
) -> list[LandingRender]:
    """Render every URL at both viewports, in order, one page at a time.

    A page that fails is a `DeviceRender(reached=False)`, never an exception;
    only a missing browser raises (`BrowserUnavailable`), because that is the
    worker's fault, not the page's.
    """
    missing = [device for device in DEVICES if device not in viewports]
    if missing:
        raise ValueError(f"no viewport for {', '.join(missing)}")
    async with _one_at_a_time():
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise BrowserUnavailable("playwright is not installed") from exc
        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(headless=True, args=LAUNCH_ARGS)
            except Exception as exc:  # noqa: BLE001 — surfaces as "run `playwright install`"
                raise BrowserUnavailable(f"could not launch Chromium: {exc}") from exc
            try:
                renders: list[LandingRender] = []
                for url in urls:
                    mobile = await _render(browser, url, "mobile", viewports["mobile"], timeout_ms)
                    desktop = await _render(
                        browser, url, "desktop", viewports["desktop"], timeout_ms
                    )
                    renders.append(LandingRender(url=url, mobile=mobile, desktop=desktop))
                return renders
            finally:
                await browser.close()


async def _render(
    browser: Browser, url: str, device: Device, viewport: Viewport, timeout_ms: int
) -> DeviceRender:
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    mobile = device == "mobile"
    user_agent = MOBILE_USER_AGENT if mobile else USER_AGENT
    blocked: list[BlockedRequest] = []
    gets = 0

    async def guard(route: Route) -> None:
        nonlocal gets
        request = route.request
        try:
            if request.method == "GET":
                gets += 1
                await route.continue_()
                return
            navigation = request.is_navigation_request()
            blocked.append(
                BlockedRequest(
                    method=request.method,
                    url=request.url,
                    resource_type=request.resource_type,
                    navigation=navigation,
                )
            )
            if navigation:
                # 204 to a navigation is "do not navigate": the audited
                # document stays, instead of an error page replacing it.
                await route.fulfill(status=204, body="")
            else:
                await route.abort("blockedbyclient")
        except PlaywrightError:
            # The page closed under the request. It never left the browser.
            return

    base: dict[str, Any] = {"device": device, "viewport": viewport, "user_agent": user_agent}
    context = await browser.new_context(
        viewport={"width": viewport.width, "height": viewport.height},
        user_agent=user_agent,
        is_mobile=mobile,
        has_touch=mobile,
        locale="en-US",
        service_workers="block",
        accept_downloads=False,
    )
    try:
        await context.route("**/*", guard)
        page = await context.new_page()
        page.set_default_timeout(timeout_ms)
        deadline = time.monotonic() + timeout_ms / 1000
        try:
            response = await page.goto(url, wait_until="load", timeout=timeout_ms)
        except PlaywrightError as exc:
            log.info("landing.unreached", url=url, device=device, error=str(exc))
            return DeviceRender(**base, reached=False, error=_first_line(exc), blocked=blocked)
        settled = True
        try:
            remaining = max(1, int((deadline - time.monotonic()) * 1000))
            await page.wait_for_load_state("networkidle", timeout=remaining)
        except PlaywrightTimeoutError:
            settled = False
        status = response.status if response is not None else None
        try:
            facts = await page.evaluate(
                _EXTRACT, {"maxTextNodes": MAX_TEXT_NODES, "maxOverlays": MAX_OVERLAYS}
            )
            screenshot = await page.screenshot(full_page=True, type="png")
        except PlaywrightError as exc:
            log.warning("landing.extract_failed", url=url, device=device, error=str(exc))
            return DeviceRender(
                **base,
                reached=True,
                error=_first_line(exc),
                final_url=page.url,
                http_status=status,
                settled=settled,
                blocked=blocked,
                get_requests=gets,
            )
        overlays = [Overlay.model_validate(item) for item in facts["overlays"]]
        return DeviceRender(
            **base,
            reached=True,
            final_url=page.url,
            http_status=status,
            settled=settled,
            fold_px=int(facts["fold"]),
            h1=facts["h1"],
            text_nodes=[TextNode.model_validate(item) for item in facts["textNodes"]],
            text_nodes_truncated=bool(facts["truncated"]),
            controls=[FormControl.model_validate(item) for item in facts["controls"]],
            overlays=overlays,
            obscured_by_overlay=any(item.fraction > OVERLAY_OBSCURES for item in overlays),
            blocked=blocked,
            get_requests=gets,
            screenshot=screenshot,
        )
    finally:
        await context.close()


def _first_line(exc: BaseException) -> str:
    """Playwright errors carry a call log after the first line; the reason is the first."""
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__
