"""The Media Library's preview proxy (Stage 04 PRD §15.5 item 2).

The grid shows a small WebP instead of the file. It must keep the file's shape
— one factor on both axes, so a tile drawn from it is still aspect-true — and
must never be larger than the file it stands for.
"""

from __future__ import annotations

import io
from fractions import Fraction

import numpy as np
import pytest
from PIL import Image

from agent.creative.previews import preview_key
from agent.postprod.image import PREVIEW_LONG_SIDE, preview_webp


def _encoded(size: tuple[int, int], mode: str = "RGB", fmt: str = "JPEG") -> bytes:
    rng = np.random.default_rng(size[0] * 7 + size[1])
    channels = 4 if mode == "RGBA" else 3
    pixels = rng.integers(0, 255, (size[1], size[0], channels), dtype=np.uint8)
    if mode == "RGBA":
        pixels[..., 3] = 0
        pixels[: size[1] // 2, :, 3] = 255
    buffer = io.BytesIO()
    image = Image.fromarray(pixels, mode=mode)
    if fmt == "JPEG":
        exif = Image.Exif()
        exif[0x010F] = "camera maker"  # Make
        image.save(buffer, format=fmt, exif=exif)
    else:
        image.save(buffer, format=fmt)
    return buffer.getvalue()


@pytest.mark.parametrize(
    "size", [(1200, 628), (1080, 1920), (1200, 1200), (3840, 2160), (628, 1200), (1999, 7)]
)
def test_the_proxy_keeps_the_shape_with_one_factor_and_a_640_long_side(
    size: tuple[int, int],
) -> None:
    made = preview_webp(_encoded(size))

    with Image.open(io.BytesIO(made.content)) as opened:
        assert opened.format == "WEBP"
        assert opened.size == (made.width, made.height)
    assert max(made.width, made.height) == PREVIEW_LONG_SIDE
    assert made.scale == Fraction(PREVIEW_LONG_SIDE, max(size))
    # Rounded to whole pixels, each side is within half a pixel of the one factor.
    assert abs(made.width - size[0] * made.scale) <= Fraction(1, 2)
    assert abs(made.height - size[1] * made.scale) <= Fraction(1, 2)
    assert made.transform["sx"] == made.transform["sy"] == float(made.scale)


def test_a_small_file_is_not_enlarged() -> None:
    made = preview_webp(_encoded((320, 180)))

    assert (made.width, made.height, made.scale) == (320, 180, Fraction(1))


def test_transparency_survives_and_metadata_does_not() -> None:
    logo = preview_webp(_encoded((800, 400), mode="RGBA", fmt="PNG"))
    photo = preview_webp(_encoded((900, 600)))

    with Image.open(io.BytesIO(logo.content)) as opened:
        assert opened.mode == "RGBA"
        alpha = np.asarray(opened.getchannel("A"))
    assert alpha[: logo.height // 4].min() > 200 and alpha[-logo.height // 4 :].max() < 50
    with Image.open(io.BytesIO(photo.content)) as opened:
        assert opened.mode == "RGB"
        assert not opened.getexif()
        assert "xmp" not in opened.info and "exif" not in opened.info


def test_the_proxy_is_stored_beside_the_file_it_stands_for() -> None:
    assert (
        preview_key("creative/r/renditions/a/1_91x1.jpg")
        == "creative/r/renditions/a/1_91x1_preview.webp"
    )
    assert preview_key("creative/r/logos/l/c-pmax-logo.png") == (
        "creative/r/logos/l/c-pmax-logo_preview.webp"
    )
    assert preview_key("master") == "master_preview.webp"
