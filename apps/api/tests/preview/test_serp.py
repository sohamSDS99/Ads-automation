"""`preview/serp.py` — ad previews from versioned templates, overflow per element (PRD §10.2, D12).

A real Chromium renders each preview at both viewports. Each text element sits
in a box its spec's character budget wide, so "does not fit" is measured the
way §10.2 says — `scrollWidth > clientWidth`, element by element — and a
31-character headline overflows a 30-character slot on every device. What the
device layout hides (a third headline past the mobile clamp) is recorded too,
as clipping: advisory, like every pixel fact here.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from agent.creative.constants import load_creative_constants
from agent.preview import landing, serp
from agent.preview.serp import AdPreview, PreviewElement

PNG = b"\x89PNG\r\n\x1a\n"
HEAD = uuid.UUID("00000000-0000-4000-8000-0000000000a1")


def _viewports() -> dict[landing.Device, landing.Viewport]:
    constants = load_creative_constants().landing
    return {
        "mobile": landing.parse_viewport(constants.viewport_mobile.value),
        "desktop": landing.parse_viewport(constants.viewport_desktop.value),
    }


def _version() -> str:
    return load_creative_constants().preview.serp_template_version.value


def _ad(*headlines: str, descriptions: tuple[str, ...] = ("Manage every safety data sheet.",),
        paths: tuple[str, ...] = ("sds", "software")) -> AdPreview:  # fmt: skip
    elements = [
        PreviewElement(key=f"headline_{i}", asset_id=HEAD, role="headline", text=t, max_chars=30)
        for i, t in enumerate(headlines, start=1)
    ]
    elements += [
        PreviewElement(key=f"description_{i}", role="description", text=t, max_chars=90)
        for i, t in enumerate(descriptions, start=1)
    ]
    elements += [
        PreviewElement(key=f"path_{i}", role="path", text=t, max_chars=15)
        for i, t in enumerate(paths, start=1)
    ]
    return AdPreview(ref="ad-1", display_url="www.example.com", elements=elements)


async def _render(ad: AdPreview) -> serp.AdRender:
    (render,) = await serp.render_previews([ad], viewports=_viewports(), version=_version())
    return render


def test_each_template_carries_the_version_the_constants_pin() -> None:
    assert serp.template_version("mobile") == _version()
    assert serp.template_version("desktop") == _version()


async def test_a_template_other_than_the_pinned_version_is_refused() -> None:
    with pytest.raises(serp.SerpTemplateError, match="1999.01"):
        await serp.render_previews([_ad("SDS software")], viewports=_viewports(), version="1999.01")


async def test_a_31_character_headline_overflows_its_element_and_a_30_character_one_does_not() -> (
    None
):
    over, fits = "x" * 31, "WWWWWWWWWWiiiiiiiiiimmmmmmmmmm"
    assert len(fits) == 30
    render = await _render(_ad(over, fits))

    for device in (render.mobile, render.desktop):
        assert device.rendered, device.error
        assert device.template_version == _version()
        by_key = {m.key: m for m in device.elements}
        assert by_key["headline_1"].scroll_width > by_key["headline_1"].client_width
        assert by_key["headline_1"].overflow_px > 0
        assert by_key["headline_2"].overflow_px == 0
        dom = device.dom()
        assert "headline_1" in dom["truncated"] and "headline_2" not in dom["truncated"]
        assert [(o["element"], o["asset_id"]) for o in dom["overflow_px"]] == [
            ("headline_1", str(HEAD))
        ]


async def test_every_element_is_measured() -> None:
    render = await _render(_ad("SDS software", "One place for SDS"))

    keys = {m.key for m in render.desktop.elements}
    assert keys == {
        "headline_1", "headline_2", "description_1", "path_1", "path_2", "display_url",
    }  # fmt: skip
    assert all(m.client_width > 0 for m in render.desktop.elements)


async def test_a_headline_the_mobile_layout_cannot_show_is_clipped_not_overflowing() -> None:
    thirty = ("A" * 30, "B" * 30, "C" * 30)
    render = await _render(_ad(*thirty))

    third = {m.key: m for m in render.mobile.elements}["headline_3"]
    assert third.overflow_px == 0 and third.clipped
    assert "headline_3" in render.mobile.dom()["truncated"]


async def test_text_is_escaped_and_nothing_leaves_the_page() -> None:
    hostile = '<img src="http://example.invalid/x.png">'
    render = await _render(_ad("SDS software", descriptions=(hostile,)))

    for device in (render.mobile, render.desktop):
        assert device.rendered, device.error
        assert device.requests == 0
        assert device.screenshot is not None and device.screenshot.startswith(PNG)


async def test_a_render_waits_for_the_landing_renderer_one_at_a_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Playwright concurrency stays at 1 across both renderers (§13): while a
    landing render holds the lock, no preview page is even opened."""
    started = asyncio.Event()
    real = serp._render

    async def watched(*args: object, **kwargs: object) -> serp.PreviewRender:
        started.set()
        return await real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(serp, "_render", watched)
    async with landing.one_at_a_time():
        task = asyncio.create_task(_render(_ad("SDS software")))
        try:
            await asyncio.wait_for(started.wait(), timeout=8)
            opened_while_locked = True
        except TimeoutError:
            opened_while_locked = False
    render = await task
    assert not opened_while_locked
    assert started.is_set() and render.mobile.rendered
