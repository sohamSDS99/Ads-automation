"""`media.crop_window_v1` — where a crop goes, and whether it may happen at all
(Stage 04 PRD §9.4 item 1, Law 39).

The saliency is OpenCV spectral residual; the window is the largest one of the
target ratio, slid to keep the most saliency mass. Retained saliency below
`crop_min_saliency_retained` (0.85) is a `gap` — never a bad crop.
"""

from __future__ import annotations

import io
from dataclasses import replace

import numpy as np
import pytest
from PIL import Image

from agent.calc.media import best_window, crop_decision, crop_window_v1, spectral_residual
from agent.calc.registry import FORMULAS
from agent.media.constants import media_constants

CONSTANTS = media_constants()


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _textured(size: tuple[int, int], level: int = 200, seed: int = 0) -> np.ndarray:
    """A photograph-like field: a flat tone with sensor-like noise. A perfectly
    flat synthetic field is not a photograph — its spectrum is exact zeros,
    which spectral residual turns into echoes rather than saliency."""
    width, height = size
    noise = np.random.default_rng(seed).normal(0, 6, (height, width))
    return np.clip(level + noise, 0, 255).astype(np.uint8)


def _frame(gray: np.ndarray) -> bytes:
    return _png(Image.fromarray(gray, mode="L"))


def test_it_is_a_registered_stage04_formula() -> None:
    spec = FORMULAS["media.crop_window_v1"]
    assert spec.kind == "calc_crop_window"


def test_the_window_slides_to_the_subject_and_keeps_it() -> None:
    # 16:9 frame → 1.91:1: the largest window is full width and 837.7 px tall,
    # so it can slide 62 px vertically. The subject sits at the top edge.
    gray = _textured((1600, 900), level=120)
    gray[20:120, 700:900] = 10
    result = crop_window_v1(image=_frame(gray), ratio="1.91:1", constants=CONSTANTS)
    x0, y0, x1, y1 = result.result["box"]
    assert result.result["decision"] == "crop"
    assert result.result["retained"] >= 0.85
    assert (x0, x1) == (0, 1600)
    assert y1 - y0 == pytest.approx(1600 / 1.91)
    assert y0 == 0  # slid up to keep the subject, not centred


def test_a_sixty_percent_crop_is_a_gap_never_a_bad_crop() -> None:
    # 2.5:1 frame → 1.5:1: the window holds 60% of the frame, and with the
    # detail spread evenly it keeps ~60% of the saliency — below 0.85, so it is
    # a recorded gap.
    result = crop_window_v1(
        image=_frame(_textured((1000, 400))), ratio="1.5:1", constants=CONSTANTS
    )
    assert result.result["decision"] == "gap"
    assert 0.55 <= result.result["retained"] <= 0.65


def test_the_threshold_comes_from_the_constants() -> None:
    content = _frame(_textured((1000, 400)))
    lenient = replace(CONSTANTS, crop_min_saliency_retained=0.5)
    assert crop_window_v1(image=content, ratio="1.5:1", constants=lenient).result["decision"] == (
        "crop"
    )


def test_the_best_window_keeps_the_most_mass() -> None:
    # A 10x20 map with 3 units at column 2 and 2 units at column 17: the best
    # 10x10 window holds column 2 and keeps exactly 3/5 of the mass.
    saliency = np.zeros((10, 20), dtype=np.float64)
    saliency[5, 2] = 3.0
    saliency[5, 17] = 2.0
    x0, y0, retained = best_window(saliency, 10.0, 10.0)
    # x0 ∈ {0, 1, 2} all hold column 2 (mass 3); the tie goes to the one
    # nearest the centre (x0 = 5), so 2.
    assert (x0, y0) == (2.0, 0.0)
    assert retained == pytest.approx(0.6)


def test_retained_saliency_below_the_floor_is_a_gap_and_the_floor_itself_crops() -> None:
    assert crop_decision(0.6, 0.85) == "gap"
    assert crop_decision(0.8499, 0.85) == "gap"
    assert crop_decision(0.85, 0.85) == "crop"


def test_a_frame_with_no_saliency_keeps_its_area_share() -> None:
    # Nothing stands out anywhere, so saliency is spread like area: a 1:1 crop
    # of a 16:9 frame keeps 9/16 of it — a gap, not a "free" crop.
    content = _png(Image.new("RGB", (1600, 900), (120, 130, 140)))
    result = crop_window_v1(image=content, ratio="1:1", constants=CONSTANTS)
    assert result.result["retained"] == pytest.approx(0.5625)
    assert result.result["decision"] == "gap"


def test_the_saliency_map_is_the_frame_size_and_peaks_on_the_subject() -> None:
    gray = _textured((800, 400), level=236)
    gray[180:220, 580:620] = 16
    saliency = spectral_residual(gray)
    assert saliency.shape == (400, 800)
    peak_y, peak_x = np.unravel_index(int(np.argmax(saliency)), saliency.shape)
    assert 560 <= peak_x <= 640 and 160 <= peak_y <= 240


def test_identical_inputs_give_identical_results() -> None:
    gray = _textured((1200, 800))
    gray[250:350, 250:350] = 10
    content = _frame(gray)
    first = crop_window_v1(image=content, ratio="4:5", constants=CONSTANTS)
    second = crop_window_v1(image=content, ratio="4:5", constants=CONSTANTS)
    assert first.inputs_hash == second.inputs_hash
    assert first.result == second.result
    assert "image_sha256" in first.inputs and "image" not in first.inputs


def test_an_undecodable_image_is_a_calc_error() -> None:
    from agent.calc.registry import CalcError

    with pytest.raises(CalcError):
        crop_window_v1(image=b"not an image", ratio="1:1", constants=CONSTANTS)
