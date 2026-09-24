"""Logos and labels are placed by code (Stage 04 PRD §9.4 items 3 and 5, Law 38).

A registered logo goes only on a `logo.permitted_surfaces` surface and never
on `search_image`; its variant is the one reaching ≥ 3:1 against the measured
background; it keeps `clear_space_ratio × logo height` from every edge and is
at least `min_width_px` wide; and it is fitted by padding, never stretched.
"""

from __future__ import annotations

import io
import uuid

import numpy as np
import pytest
from PIL import Image

from agent.postprod.image import (
    LogoArt,
    apply_labels,
    composite_logo,
    fit_by_padding,
    load_logo,
    place_logo,
    relative_luminance,
    required_labels,
)
from agent.schemas.guardrails import DisclosureRule

PERMITTED = ("pmax_image", "display_image", "demand_gen_image", "video")


def _logo(ink: tuple[int, int, int], size: tuple[int, int] = (400, 100)) -> LogoArt:
    """A wordmark-shaped logo: solid ink on a transparent field with a margin."""
    image = Image.new("RGBA", (size[0] + 40, size[1] + 40), (0, 0, 0, 0))
    image.paste(Image.new("RGBA", size, (*ink, 255)), (20, 20))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return load_logo(buffer.getvalue(), asset_id=uuid.uuid4(), label=f"logo {ink}")


def _frame(colour: tuple[int, int, int], size: tuple[int, int] = (1200, 628)) -> Image.Image:
    return Image.new("RGB", size, colour)


def _place(frame: Image.Image, logos: list[LogoArt], **overrides: object):  # type: ignore[no-untyped-def]
    options: dict[str, object] = {
        "surface": "pmax_image",
        "permitted_surfaces": PERMITTED,
        "clear_space_ratio": 0.5,
        "min_width_px": 120,
        "width_ratio": 0.2,
        "saliency": None,
    }
    options.update(overrides)
    return place_logo(frame, logos, **options)  # type: ignore[arg-type]


def test_a_logo_is_trimmed_to_its_visible_pixels() -> None:
    logo = _logo((10, 10, 10))
    assert logo.image.size == (400, 100)
    assert logo.luminance == pytest.approx(relative_luminance((10, 10, 10)))


def test_never_on_search_image_even_if_the_constants_listed_it() -> None:
    outcome = _place(
        _frame((20, 20, 20)),
        [_logo((245, 245, 245))],
        surface="search_image",
        permitted_surfaces=(*PERMITTED, "search_image"),
    )
    assert outcome.placement is None
    assert "search_image" in (outcome.reason or "")


def test_only_on_a_permitted_surface() -> None:
    outcome = _place(
        _frame((20, 20, 20)), [_logo((245, 245, 245))], permitted_surfaces=("display_image",)
    )
    assert outcome.placement is None
    assert "logo.permitted_surfaces" in (outcome.reason or "")


@pytest.mark.parametrize(
    ("background", "want"),
    [((20, 20, 20), (245, 245, 245)), ((235, 235, 235), (10, 10, 10))],
)
def test_the_variant_is_the_one_that_contrasts_with_the_background(
    background: tuple[int, int, int], want: tuple[int, int, int]
) -> None:
    dark, light = _logo((10, 10, 10)), _logo((245, 245, 245))
    outcome = _place(_frame(background), [dark, light])
    assert outcome.placement is not None
    assert outcome.placement.label == f"logo {want}"
    assert outcome.placement.contrast >= 3.0


def test_no_variant_reaching_three_to_one_means_no_logo() -> None:
    # L(100) = 0.127, L(150) = 0.305 against L(128) = 0.216: 1.52:1 and 1.34:1.
    outcome = _place(_frame((128, 128, 128)), [_logo((100, 100, 100)), _logo((150, 150, 150))])
    assert outcome.placement is None
    assert "3:1" in (outcome.reason or "")


def test_clear_space_and_min_width_decide_the_box() -> None:
    # width = max(0.2 × 1200, 300) = 300; height = 300 × 100/400 = 75;
    # clear space = ceil(0.5 × 75) = 38 from each edge — bottom-right first.
    frame = _frame((235, 235, 235))
    outcome = _place(frame, [_logo((10, 10, 10))], min_width_px=300)
    assert outcome.placement is not None
    assert outcome.placement.box == (862, 515, 1162, 590)
    assert outcome.placement.clear_space_px == 38

    placed = np.asarray(composite_logo(frame, outcome.placement))
    assert (placed[515:590, 862:1162] < 40).all()  # the logo, edge to edge
    assert (placed[590:, :] == 235).all() and (placed[:, 1162:] == 235).all()  # clear space


def test_a_frame_too_small_for_the_logo_and_its_clear_space_gets_none() -> None:
    outcome = _place(_frame((235, 235, 235), (200, 100)), [_logo((10, 10, 10))], min_width_px=300)
    assert outcome.placement is None


def test_without_a_stated_clear_space_there_is_nothing_to_respect_so_no_logo() -> None:
    outcome = _place(_frame((235, 235, 235)), [_logo((10, 10, 10))], clear_space_ratio=None)
    assert outcome.placement is None
    assert "clear space" in (outcome.reason or "")


def test_the_least_salient_corner_wins() -> None:
    saliency = np.zeros((628, 1200), dtype=np.float32)
    saliency[400:, 800:] = 1.0  # the subject fills the bottom-right
    outcome = _place(_frame((235, 235, 235)), [_logo((10, 10, 10))], saliency=saliency)
    assert outcome.placement is not None
    assert outcome.placement.corner == "bottom_left"


def test_a_logo_is_fitted_by_padding_never_stretched() -> None:
    fitted = fit_by_padding(_logo((10, 10, 10)).image, 256, 256)
    assert fitted.size == (256, 256)
    alpha = np.asarray(fitted.getchannel("A"))
    ys, xs = np.nonzero(alpha > 128)
    assert (int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1)) == (256, 64)
    assert alpha[:90, :].max() == 0 and alpha[166:, :].max() == 0  # padding, top and bottom


# -- the visible disclosure label (§9.4 item 5) ----------------------------------


def _rule(**overrides: object) -> DisclosureRule:
    fields: dict[str, object] = {
        "disclosure_id": "ai-label-de",
        "surfaces": ("pmax_image",),
        "markets": ("DE",),
        "required_text": "KI-generiert",
        "placement": "suffix",
    }
    fields.update(overrides)
    return DisclosureRule.model_validate(fields)


def test_only_a_rule_naming_this_surface_and_market_requires_a_label() -> None:
    rules = [
        _rule(),
        _rule(disclosure_id="other-surface", surfaces=("display_image",)),
        _rule(disclosure_id="other-market", markets=("FR",)),
        _rule(disclosure_id="every-market", markets=()),
        _rule(disclosure_id="text-only", surfaces=("rsa_headline",)),
    ]
    got = required_labels(rules, surface="pmax_image", market="DE")
    assert [rule.disclosure_id for rule in got] == ["ai-label-de", "every-market"]


def test_a_required_label_is_drawn_at_its_placement() -> None:
    frame = _frame((235, 235, 235))
    labelled, drawn = apply_labels(frame, [_rule()], height_pct=0.055)
    pixels = np.asarray(labelled).astype(int)
    changed = np.nonzero((pixels != 235).any(axis=2))
    assert drawn == [
        {
            "disclosure_id": "ai-label-de",
            "text": "KI-generiert",
            "placement": "suffix",
            "box": drawn[0]["box"],
        }  # fmt: skip
    ]
    x0, y0, x1, y1 = drawn[0]["box"]
    assert changed[0].min() >= y0 and changed[1].min() >= x0  # suffix → bottom-right
    assert x1 <= 1200 and y1 <= 628 and x0 > 600 and y0 > 314
    assert y1 - y0 >= round(0.055 * 628)
