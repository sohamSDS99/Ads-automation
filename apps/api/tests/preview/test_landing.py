"""`preview/landing.py` against the three committed fixture pages (S4-P7, PRD §10.2).

A real Chromium renders each page at both viewports from
`creative_constants.yaml`, served by a local server that records every request
it receives. Two witnesses for law 41's "GETs only": the renderer's route
interception (what it blocked) and the server's log (what arrived).
"""

from __future__ import annotations

import asyncio

import pytest

from agent.creative.constants import load_creative_constants
from agent.preview import landing
from agent.preview.landing import DeviceRender, LandingRender, Viewport
from tests.landing_support import (
    MISMATCHED_H1,
    NINE_FIELD_FORM,
    OFFER_BELOW_FOLD,
    FixtureServer,
    fixture_server,
)

PNG = b"\x89PNG\r\n\x1a\n"


def _viewports() -> dict[landing.Device, Viewport]:
    constants = load_creative_constants().landing
    return {
        "mobile": landing.parse_viewport(constants.viewport_mobile.value),
        "desktop": landing.parse_viewport(constants.viewport_desktop.value),
    }


async def _render(server: FixtureServer, *names: str) -> list[LandingRender]:
    return await landing.render_pages([server.url(name) for name in names], viewports=_viewports())


def _node(render: DeviceRender, text: str) -> landing.TextNode:
    (found,) = [node for node in render.text_nodes if node.text == text]
    return found


def test_viewports_come_from_the_constants() -> None:
    assert _viewports() == {
        "mobile": Viewport(width=390, height=844),
        "desktop": Viewport(width=1280, height=800),
    }
    with pytest.raises(ValueError, match="WIDTHxHEIGHT"):
        landing.parse_viewport("390 by 844")


async def test_a_page_is_rendered_at_both_viewports_with_its_facts() -> None:
    with fixture_server() as server:
        (page,) = await _render(server, MISMATCHED_H1)
    assert page.url == server.url(MISMATCHED_H1)
    for device, fold in (("mobile", 844), ("desktop", 800)):
        render = page.device(device)
        assert render.device == device
        assert render.reached and render.error is None
        assert render.http_status == 200
        assert render.final_url == server.url(MISMATCHED_H1)
        assert render.settled
        assert render.fold_px == fold
        assert render.viewport.height == fold
        assert render.h1 == "Welcome to Acme Industrial Group"
        intro = _node(render, "Serving manufacturers across three continents since 1987.")
        assert 0 < intro.box.y < fold and intro.box.width > 0 and intro.box.height > 0
        # Script and style text is not visible text.
        assert not any("document.cookie" in node.text for node in render.text_nodes)
        assert render.screenshot is not None and render.screenshot.startswith(PNG)
        assert render.obscured_by_overlay is False
        assert render.blocked == []
    # The mobile render is a phone, not a narrow desktop.
    assert page.mobile.user_agent != page.desktop.user_agent


async def test_no_cookie_outlives_its_render_context() -> None:
    with fixture_server() as server:
        first = await _render(server, MISMATCHED_H1, MISMATCHED_H1)
        again = await _render(server, MISMATCHED_H1)
    for page in (*first, *again):
        for render in (page.mobile, page.desktop):
            # The page sets `seen=1` on every load and says "Welcome back" when
            # it finds it: six loads, six first visits.
            assert _node(render, "First visit")
            assert not [node for node in render.text_nodes if node.text == "Welcome back"]


async def test_the_offer_text_node_is_below_the_fold_on_mobile_and_above_it_on_desktop() -> None:
    with fixture_server() as server:
        (page,) = await _render(server, OFFER_BELOW_FOLD)
    mobile = _node(page.mobile, "Get 20% off the first year")
    desktop = _node(page.desktop, "Get 20% off the first year")
    assert mobile.box.y >= page.mobile.fold_px
    assert desktop.box.y < page.desktop.fold_px


async def test_form_controls_are_extracted_with_labels_types_and_required() -> None:
    with fixture_server() as server:
        (page,) = await _render(server, NINE_FIELD_FORM)
    render = page.desktop
    lead = [control for control in render.controls if control.form == 0]
    visible = [control for control in lead if control.visible and control.fillable]
    assert [control.name for control in visible] == [
        "first_name",
        "last_name",
        "email",
        "phone",
        "company",
        "job_title",
        "company_size",
        "country",
        "consent",
    ]
    by_name = {control.name: control for control in lead}
    assert by_name["email"].type == "email" and by_name["email"].required
    assert by_name["email"].label == "Work email"
    assert by_name["phone"].type == "tel" and not by_name["phone"].required
    assert by_name["company_size"].tag == "select" and by_name["company_size"].required
    assert by_name["consent"].type == "checkbox" and by_name["consent"].required
    assert by_name["consent"].label is not None
    assert by_name["consent"].label.startswith("I agree to the privacy policy")
    # Not fields a visitor fills in: a hidden input, and a CSS-hidden honeypot.
    assert by_name["utm_source"].type == "hidden" and not by_name["utm_source"].fillable
    assert by_name["website"].visible is False
    # The footer newsletter is its own form.
    (newsletter,) = [control for control in render.controls if control.name == "nl_email"]
    assert newsletter.form == 1


async def test_an_overlay_over_30_percent_of_the_viewport_is_recorded_per_device() -> None:
    with fixture_server() as server:
        (page,) = await _render(server, NINE_FIELD_FORM)
    assert page.mobile.obscured_by_overlay is True
    assert page.mobile.overlays[0].fraction == pytest.approx(0.40, abs=0.01)
    # 200px of an 800px screen is 25%: recorded, not obscuring.
    assert page.desktop.obscured_by_overlay is False
    assert page.desktop.overlays[0].fraction == pytest.approx(0.25, abs=0.01)
    # Boxes are measured independently of the overlay: the form under the
    # banner still has its fields.
    assert len([c for c in page.mobile.controls if c.form == 0 and c.fillable and c.visible]) == 9


async def test_the_renderer_issues_nothing_but_get() -> None:
    """Law 41, proved by route interception and by the server's own log.

    The page tries a fetch POST, an XHR PUT, a beacon and a JS submit of its
    lead form on load, and carries a consent banner whose Accept would POST.
    """
    with fixture_server() as server:
        (page,) = await _render(server, NINE_FIELD_FORM)
        methods = server.methods()
        paths = {hit.path for hit in server.hits}
    for render in (page.mobile, page.desktop):
        blocked = {
            (request.method, request.url.removeprefix(server.base_url))
            for request in render.blocked
        }
        assert {
            ("POST", "/collect"),
            ("PUT", "/profile"),
            ("POST", "/beacon"),
            ("POST", "/lead"),
        } <= blocked
        assert all(request.method != "GET" for request in render.blocked)
    assert methods == {"GET"}
    assert not {"/collect", "/profile", "/beacon", "/lead", "/consent"} & paths


async def test_the_final_url_after_redirects_and_the_status_are_recorded() -> None:
    with fixture_server() as server:
        redirected, missing = await landing.render_pages(
            [f"{server.base_url}/redirect/{OFFER_BELOW_FOLD}", server.url("gone.html")],
            viewports=_viewports(),
        )
    for render in (redirected.mobile, redirected.desktop):
        assert render.final_url == server.url(OFFER_BELOW_FOLD)
        assert render.http_status == 200
    for render in (missing.mobile, missing.desktop):
        assert render.reached and render.http_status == 404


async def test_a_page_that_never_answers_is_unreached_not_an_exception() -> None:
    with fixture_server() as server:
        closed = server.base_url
    (page,) = await landing.render_pages([f"{closed}/{MISMATCHED_H1}"], viewports=_viewports())
    for render in (page.mobile, page.desktop):
        assert not render.reached
        assert render.error
        assert render.http_status is None and render.final_url is None
        assert render.h1 is None and render.text_nodes == [] and render.screenshot is None


async def test_renders_run_one_at_a_time() -> None:
    """Concurrency 1 (§13): two callers in one process never interleave pages."""
    with fixture_server() as server:
        await asyncio.gather(
            _render(server, MISMATCHED_H1, OFFER_BELOW_FOLD),
            _render(server, NINE_FIELD_FORM, MISMATCHED_H1),
        )
        hits = server.pages()
    order = [hit.path.lstrip("/") for hit in hits]
    # Each caller's four document loads (two pages x two devices) are
    # contiguous: the second caller started only after the first finished.
    first, second = order[:4], order[4:]
    assert sorted({*first}) in (
        sorted({MISMATCHED_H1, OFFER_BELOW_FOLD}),
        sorted({NINE_FIELD_FORM, MISMATCHED_H1}),
    )
    assert len(order) == 8 and set(first) != set(second)
    assert all(a.finished <= b.started for a, b in zip(hits, hits[1:], strict=False))


async def test_the_render_round_trips_through_its_two_evidence_kinds() -> None:
    """4.5.2 judges the stored `landing_dom` / `landing_render` rows, never a re-render."""
    with fixture_server() as server:
        (page,) = await _render(server, NINE_FIELD_FORM)
    drafts = landing.evidence_drafts(page, {"mobile": "k/mobile.png", "desktop": None})
    assert [(d.source, d.kind) for d in drafts] == [
        ("web", "landing_render"),
        ("web", "landing_dom"),
    ] * 2
    assert drafts[0].payload["screenshot"] == "k/mobile.png"
    assert "screenshot" not in drafts[1].payload
    rebuilt = landing.from_evidence([(d.kind, d.payload) for d in drafts])
    assert rebuilt[page.url] == page.model_copy(
        update={
            "mobile": page.mobile.model_copy(update={"screenshot": None}),
            "desktop": page.desktop.model_copy(update={"screenshot": None}),
        }
    )
    # Half a render is not a render.
    assert landing.from_evidence([(d.kind, d.payload) for d in drafts[:3]]) == {}
