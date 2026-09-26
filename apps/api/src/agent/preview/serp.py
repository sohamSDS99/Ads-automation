"""Playwright render of ad previews, mobile and desktop (Stage 04 PRD §10.2, D12).

Each preview is one ad combination filled into a versioned HTML template —
`templates/serp_mobile.html` or `serp_desktop.html` — and rendered in Chromium
at `landing.viewport_mobile` / `landing.viewport_desktop`. What comes back, per
text element, is what §10.2 asks for: `scrollWidth` against `clientWidth`,
measured in the page. A box with a character budget is that many characters
wide in the template, so an element that overflows its box is longer than its
slot; an element the device layout pushes past its block's line clamp is
*clipped*. Both are pixel facts and **advisory** (D12): a blocking verdict
comes only from the `RuleSet` specs, which node 4.6.4 checks separately.

**Versioned.** Each template declares `serp-template-version` in a `<meta>`;
the caller passes the version the run's constants pin, and a template that
says anything else is refused before a browser starts — so the version stamped
on a preview is the version of the markup that drew it.

**Nothing leaves the page.** The HTML is set in place (`page.set_content`),
every value in it is escaped by the template engine, and a route handler
aborts any request the page might still make; the count is recorded.

**Concurrency 1.** One Chromium, one page at a time, sharing
`landing.one_at_a_time()` — a landing render and a preview render never run
together in one process.

Import-safe without a browser: Playwright is imported inside the render.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

import structlog
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from pydantic import BaseModel, ConfigDict, Field

from agent.connectors.browser import LAUNCH_ARGS, USER_AGENT, BrowserUnavailable
from agent.preview.landing import DEVICES, MOBILE_USER_AGENT, Viewport, one_at_a_time
from agent.schemas.landing import Device

if TYPE_CHECKING:
    from playwright.async_api import Browser, Route

log = structlog.get_logger(__name__)

TEMPLATE_DIR: Final = Path(__file__).parent / "templates"
TEMPLATES: Final[Mapping[Device, str]] = {
    "mobile": "serp_mobile.html",
    "desktop": "serp_desktop.html",
}
_VERSION = re.compile(r'<meta name="serp-template-version" content="([^"]+)">')
#: A preview is a few kilobytes of local HTML: anything slower is a hung page.
RENDER_TIMEOUT_MS: Final = 15_000

ElementRole = Literal["headline", "description", "path"]

_MEASURE = """
() => {
  const out = [];
  for (const el of document.querySelectorAll('[data-el]')) {
    const box = el.getBoundingClientRect();
    const clip = el.parentElement ? el.parentElement.closest('[data-clip]') : null;
    let clipped = false;
    if (clip) {
      const c = clip.getBoundingClientRect();
      clipped = box.bottom > c.bottom + 0.5 || box.right > c.right + 0.5;
    }
    out.push({key: el.dataset.el, scroll_width: el.scrollWidth,
              client_width: el.clientWidth, clipped});
  }
  return out;
}
"""


class SerpTemplateError(RuntimeError):
    """A template is missing, unversioned, or not the version the run pins."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PreviewElement(_Model):
    """One text element of the ad: `headline_1`…`headline_3`, `description_1`, `path_2`."""

    key: str = Field(min_length=1)
    asset_id: uuid.UUID | None = None
    role: ElementRole
    text: str
    #: The spec's character limit — the width of the element's box. None when
    #: the pin has no spec for it: then the box is as wide as its text.
    max_chars: int | None = Field(default=None, ge=1)


class AdPreview(_Model):
    """One combination to render: the caller's `ref`, a display URL and its elements."""

    ref: str = Field(min_length=1)
    display_url: str
    elements: list[PreviewElement]
    language: str = "en"


class ElementMetrics(_Model):
    key: str
    asset_id: uuid.UUID | None = None
    scroll_width: int
    client_width: int
    #: `scroll_width - client_width` when the text is wider than its box, else 0.
    overflow_px: int = Field(ge=0)
    #: The device layout hides some of it: past the block's line clamp or edge.
    clipped: bool

    @property
    def truncated(self) -> bool:
        return self.overflow_px > 0 or self.clipped


class PreviewRender(_Model):
    device: Device
    template_version: str
    rendered: bool
    error: str | None = None
    elements: list[ElementMetrics] = Field(default_factory=list)
    #: Requests the page tried to make. Every one was aborted.
    requests: int = 0
    screenshot: bytes | None = None

    def dom(self) -> dict[str, Any]:
        """§11 4.6.4 `dom{truncated[], overflow_px[]}`, with every element's raw metrics."""
        return {
            "truncated": [m.key for m in self.elements if m.truncated],
            "overflow_px": [
                {
                    "element": m.key,
                    "asset_id": str(m.asset_id) if m.asset_id is not None else None,
                    "px": m.overflow_px,
                }
                for m in self.elements
                if m.overflow_px > 0
            ],
            "elements": [m.model_dump(mode="json") for m in self.elements],
            "requests": self.requests,
            **({"error": self.error} if self.error else {}),
        }


class AdRender(_Model):
    ref: str
    mobile: PreviewRender
    desktop: PreviewRender


def template_version(device: Device) -> str:
    """The `serp-template-version` the device's template declares."""
    return _declared(TEMPLATES[device])


@cache
def _declared(name: str) -> str:
    try:
        markup = (TEMPLATE_DIR / name).read_text(encoding="utf-8")
    except OSError as exc:
        raise SerpTemplateError(f"preview template {name} cannot be read: {exc}") from exc
    found = _VERSION.search(markup)
    if found is None:
        raise SerpTemplateError(f"preview template {name} declares no serp-template-version")
    return found.group(1)


@cache
def _environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
        undefined=StrictUndefined,
    )


def html(ad: AdPreview, device: Device) -> str:
    """The template filled with one combination. Every value is escaped."""

    def elements(role: ElementRole) -> list[dict[str, Any]]:
        return [
            {"key": e.key, "text": e.text, "budget": e.max_chars}
            for e in ad.elements
            if e.role == role
        ]

    return (
        _environment()
        .get_template(TEMPLATES[device])
        .render(
            lang=ad.language,
            display_url=ad.display_url,
            headlines=elements("headline"),
            descriptions=elements("description"),
            paths=elements("path"),
        )
    )


def check_version(version: str) -> None:
    """Refuse to render with templates other than the version the run pins."""
    for device in DEVICES:
        declared = template_version(device)
        if declared != version:
            raise SerpTemplateError(
                f"{TEMPLATES[device]} is serp template version {declared}, but the run's "
                f"constants pin {version}: bump both together"
            )


async def render_previews(
    ads: Sequence[AdPreview], *, viewports: Mapping[Device, Viewport], version: str
) -> list[AdRender]:
    """Render every combination at both viewports, in order, one page at a time.

    A page that fails is a `PreviewRender(rendered=False)`, never an exception;
    a missing browser raises `BrowserUnavailable` and a template that is not
    `version` raises `SerpTemplateError`, both before anything is drawn.
    """
    missing = [device for device in DEVICES if device not in viewports]
    if missing:
        raise ValueError(f"no viewport for {', '.join(missing)}")
    check_version(version)
    if not ads:
        return []
    async with one_at_a_time():
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
                renders: list[AdRender] = []
                for ad in ads:
                    mobile = await _render(browser, ad, "mobile", viewports["mobile"], version)
                    desktop = await _render(browser, ad, "desktop", viewports["desktop"], version)
                    renders.append(AdRender(ref=ad.ref, mobile=mobile, desktop=desktop))
                return renders
            finally:
                await browser.close()


async def _render(
    browser: Browser, ad: AdPreview, device: Device, viewport: Viewport, version: str
) -> PreviewRender:
    from playwright.async_api import Error as PlaywrightError

    mobile = device == "mobile"
    requests = 0

    async def refuse(route: Route) -> None:
        nonlocal requests
        requests += 1
        try:
            await route.abort("blockedbyclient")
        except PlaywrightError:
            return

    context = await browser.new_context(
        viewport={"width": viewport.width, "height": viewport.height},
        user_agent=MOBILE_USER_AGENT if mobile else USER_AGENT,
        is_mobile=mobile,
        has_touch=mobile,
        locale="en-US",
        service_workers="block",
        accept_downloads=False,
        java_script_enabled=True,
    )
    asset_ids = {e.key: e.asset_id for e in ad.elements}
    try:
        await context.route("**/*", refuse)
        page = await context.new_page()
        page.set_default_timeout(RENDER_TIMEOUT_MS)
        try:
            await page.set_content(html(ad, device), wait_until="load")
            facts = await page.evaluate(_MEASURE)
            screenshot = await page.screenshot(full_page=True, type="png")
        except PlaywrightError as exc:
            log.warning("serp_preview.failed", ref=ad.ref, device=device, error=str(exc))
            return PreviewRender(
                device=device,
                template_version=version,
                rendered=False,
                error=_first_line(exc),
                requests=requests,
            )
        elements = [
            ElementMetrics(
                key=item["key"],
                asset_id=asset_ids.get(item["key"]),
                scroll_width=int(item["scroll_width"]),
                client_width=int(item["client_width"]),
                overflow_px=max(0, int(item["scroll_width"]) - int(item["client_width"])),
                clipped=bool(item["clipped"]),
            )
            for item in facts
        ]
        return PreviewRender(
            device=device,
            template_version=version,
            rendered=True,
            elements=elements,
            requests=requests,
            screenshot=screenshot,
        )
    finally:
        await context.close()


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__
