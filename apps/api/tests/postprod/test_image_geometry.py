"""`postprod/image.py` — which derivation a ratio gets, and a geometry that can
only ever scale uniformly (Stage 04 PRD §9.4 items 1–2, Law 39)."""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from PIL import Image, ImageDraw

from agent.media.capability import parse_ratio
from agent.media.types import CapabilityRecord, Descriptor, PriceLine
from agent.postprod.image import (
    StretchError,
    choose_derivation,
    geometry,
    render,
    supported_label,
    target_size,
)

TOLERANCE = 0.005


def _capability(*, ratios: list[str], image_input: bool) -> CapabilityRecord:
    return CapabilityRecord(
        modality="image",
        model_id="vendor/painter",
        params={
            "aspect_ratio": Descriptor(kind="enum", values=[*ratios, "auto"]),
            "input_references": Descriptor(kind="range", min=0, max=4),
        },
        pricing=[PriceLine(billable="output_image", unit="image", usd=Decimal("0.01"))],
        input_modalities=["text", "image"] if image_input else ["text"],
    )


# -- derivation: relaid > native > crop, else gap ------------------------------


@pytest.mark.parametrize(
    ("ratio", "image_input", "master_ratio", "want"),
    [
        ("16:9", True, "1:1", "relaid"),  # painted at the ratio FROM the master
        ("16:9", False, "1:1", "native"),  # painted at the ratio, fresh
        ("1:1", True, "1:1", "native"),  # the master itself was painted at it
        ("1.91:1", True, "1:1", "crop"),  # unsupported; 16:9 covers 93% of it
        ("9:16", True, "1:1", "gap"),  # unsupported; nothing covers 85% of it
    ],
)
def test_each_ratio_gets_the_best_derivation_the_model_allows(
    ratio: str, image_input: bool, master_ratio: str, want: str
) -> None:
    capability = _capability(ratios=["1:1", "16:9"], image_input=image_input)
    got = choose_derivation(ratio, master_ratio, capability, tolerance=TOLERANCE, min_retained=0.85)
    assert got == want


def test_a_request_names_the_ratio_in_the_models_own_words() -> None:
    capability = _capability(ratios=["1:1", "1.9:1"], image_input=True)
    # 1.91 / 1.9 − 1 = 0.53% > 0.5%: not the same ratio.
    assert supported_label("1.91:1", capability, tolerance=TOLERANCE) is None
    assert supported_label("1.91:1", capability, tolerance=0.006) == "1.9:1"
    assert supported_label("1:1", capability, tolerance=TOLERANCE) == "1:1"


# -- the frame size -------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "ratio", "min_px", "want"),
    [
        ((1600, 900), "1:1", "300x300", (900, 900)),  # a crop at source resolution
        ((1824, 1024), "1.91:1", "600x314", (1824, 955)),  # width-limited, 1824/1.91
        ((1024, 1024), "16:9", "600x338", (1024, 576)),
        ((200, 100), "1:1", "600x600", (600, 600)),  # too small: scaled UP, uniformly
        ((1024, 1024), "1:1", None, (1024, 1024)),  # native at the ratio: untouched
    ],
)
def test_the_frame_is_the_source_at_the_ratio_and_never_below_min_px(
    source: tuple[int, int], ratio: str, min_px: str | None, want: tuple[int, int]
) -> None:
    assert target_size(*source, ratio, min_px, tolerance=TOLERANCE) == want


def test_a_non_uniform_geometry_cannot_be_built() -> None:
    # 100×50 out of a 100×40 box would stretch y by 1.25 against x's 1.0.
    with pytest.raises(StretchError):
        geometry(100, 40, 100, 50, box=(0, 0, 100, 40))


# -- Law 39, for every input ----------------------------------------------------

_RATIOS = st.one_of(
    st.sampled_from(["1:1", "1.91:1", "4:5", "16:9", "9:16", "4:1", "3:2"]),
    st.tuples(st.integers(1, 40), st.integers(1, 40)).map(lambda wh: f"{wh[0]}:{wh[1]}"),
)


@settings(max_examples=1000, deadline=None, derandomize=True)
@given(
    src_w=st.integers(8, 6000),
    src_h=st.integers(8, 6000),
    ratio=_RATIOS,
    min_w=st.integers(1, 1600),
    min_h=st.integers(1, 1600),
    fx=st.fractions(0, 1),
    fy=st.fractions(0, 1),
)
def test_scaling_is_uniform_for_every_input(
    src_w: int,
    src_h: int,
    ratio: str,
    min_w: int,
    min_h: int,
    fx: Fraction,
    fy: Fraction,
) -> None:
    out_w, out_h = target_size(src_w, src_h, ratio, f"{min_w}x{min_h}", tolerance=TOLERANCE)
    placed = geometry(src_w, src_h, out_w, out_h, origin=(fx * src_w, fy * src_h))

    # sx == sy exactly — rational arithmetic, no float can hide a stretch.
    assert placed.sx == placed.sy == placed.scale
    # What is persisted is ONE number in both keys (the DB CHECK compares text).
    record = placed.transform()
    assert record["sx"] == record["sy"] == float(placed.scale)
    x0, y0, x1, y1 = placed.box
    assert 0 <= x0 < x1 <= src_w and 0 <= y0 < y1 <= src_h
    assert out_w >= min_w and out_h >= min_h
    assert abs(Fraction(out_w, out_h) / Fraction(parse_ratio(ratio)) - 1) <= TOLERANCE


# -- the pixels -------------------------------------------------------------------


def _disc(size: tuple[int, int], radius: int) -> Image.Image:
    image = Image.new("RGB", size, (240, 240, 240))
    cx, cy = size[0] // 2, size[1] // 2
    ImageDraw.Draw(image).ellipse(
        (cx - radius, cy - radius, cx + radius, cy + radius), fill=(10, 10, 10)
    )
    return image


def _disc_bbox(image: Image.Image) -> tuple[int, int]:
    dark = np.asarray(image.convert("L")) < 128
    ys, xs = np.nonzero(dark)
    return int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1)


@pytest.mark.parametrize(
    ("source", "radius", "ratio", "min_px", "size"),
    [
        ((1600, 900), 200, "1:1", "300x300", (900, 900)),  # crop, scale 1
        ((200, 100), 30, "1:1", "600x600", (600, 600)),  # scale 6, both axes
        ((1600, 900), 200, "1.91:1", "600x314", (1600, 838)),
    ],
)
def test_a_circle_stays_a_circle(
    source: tuple[int, int], radius: int, ratio: str, min_px: str, size: tuple[int, int]
) -> None:
    out_w, out_h = target_size(*source, ratio, min_px, tolerance=TOLERANCE)
    placed = geometry(*source, out_w, out_h)
    frame = render(_disc(source, radius), placed)
    assert frame.size == size
    width, height = _disc_bbox(frame)
    assert abs(width - height) <= 2  # a stretch would make it an ellipse
    # PIL draws a radius-r disc 2r+1 px across; it scales by exactly `scale`.
    assert width == pytest.approx((2 * radius + 1) * float(placed.scale), abs=3)


def test_a_scale_one_crop_copies_pixels_exactly() -> None:
    source = _disc((1600, 900), 200)
    placed = geometry(1600, 900, 900, 900, origin=(350, 0))
    assert placed.scale == 1
    frame = render(source, placed)
    assert np.array_equal(np.asarray(frame), np.asarray(source.crop((350, 0, 1250, 900))))
