"""Image post-production (Stage 04 PRD §9.4 "Images"; Laws 38, 39).

A model paints a master; everything after that is code, and deterministic:

1. **Renditions** follow the ratio plan — `relaid` > `native` > `crop` — and a
   crop's window is `media.crop_window_v1`'s, which may turn it into a gap.
2. **Scaling is uniform only.** A `Geometry` is one scale factor and a box of
   the source with exactly the frame's shape, in rational arithmetic, so
   `sx == sy` is a fact that can be asserted — and is, at construction — and
   is persisted as one number in both keys (the DB CHECK compares their text).

No I/O here: bytes and pixels in, pixels out. The node stores and records.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from PIL import Image

from agent.media.capability import Coverage, parse_ratio, ratio_coverage, supported_ratios
from agent.media.types import CapabilityRecord


class StretchError(ValueError):
    """A geometry whose axes would scale by different factors (Law 39)."""


# ---------------------------------------------------------------------------
# 1. which derivation a ratio gets
# ---------------------------------------------------------------------------


def same_ratio(a: str, b: str, *, tolerance: float) -> bool:
    return abs(parse_ratio(a) / parse_ratio(b) - 1) <= tolerance


def choose_derivation(
    ratio: str,
    master_ratio: str | None,
    capability: CapabilityRecord,
    *,
    tolerance: float,
    min_retained: float,
) -> Coverage:
    """`native` when the master itself was painted at `ratio`; otherwise the
    ratio plan's answer for this model — `relaid` if it paints the ratio and
    takes the master as input, `native` if it paints it but cannot, `crop` if a
    ratio it paints covers this one, else `gap` (§9.4 item 1, PRD §9.2)."""
    if master_ratio is not None and same_ratio(ratio, master_ratio, tolerance=tolerance):
        return "native"
    return ratio_coverage([ratio], capability, tolerance=tolerance, min_retained=min_retained)[
        ratio
    ]


def supported_label(ratio: str, capability: CapabilityRecord, *, tolerance: float) -> str | None:
    """The model's own label for `ratio` — the value a request must carry, since
    `validate()` checks it against the enum — or None if it paints no such ratio."""
    wanted = parse_ratio(ratio)
    close = [
        (abs(parse_ratio(label) / wanted - 1), label)
        for label in supported_ratios(capability)
        if abs(parse_ratio(label) / wanted - 1) <= tolerance
    ]
    return min(close)[1] if close else None


# ---------------------------------------------------------------------------
# 2. uniform scaling
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Geometry:
    """An `out_w × out_h` frame drawn from `box` (source px) at ONE `scale`."""

    out_w: int
    out_h: int
    scale: Fraction
    box: tuple[Fraction, Fraction, Fraction, Fraction]

    def __post_init__(self) -> None:
        if not (self.sx == self.sy == self.scale):
            raise StretchError(
                f"{self.out_w}x{self.out_h} from a {float(self.box[2] - self.box[0]):.3f}x"
                f"{float(self.box[3] - self.box[1]):.3f} box scales x by {float(self.sx):.6f} "
                f"and y by {float(self.sy):.6f}: relay out, never stretch (Law 39)"
            )

    @property
    def sx(self) -> Fraction:
        return Fraction(self.out_w) / (self.box[2] - self.box[0])

    @property
    def sy(self) -> Fraction:
        return Fraction(self.out_h) / (self.box[3] - self.box[1])

    def transform(self) -> dict[str, Any]:
        """`MediaArtifact.transform`'s geometry: one computed number in both keys."""
        factor = float(self.scale)
        return {"crop_box": [float(v) for v in self.box], "sx": factor, "sy": factor}


def parse_px(min_px: str | None) -> tuple[int, int]:
    """`"600x314"` → (600, 314); no minimum → (1, 1)."""
    if not min_px:
        return 1, 1
    width, sep, height = min_px.lower().partition("x")
    try:
        parsed = int(width), int(height)
    except ValueError:
        parsed = 0, 0
    if not sep or min(parsed) < 1:
        raise ValueError(f"{min_px!r} is not a pixel size (expected WxH, e.g. 600x314)")
    return parsed


def target_size(
    src_w: int, src_h: int, ratio: str, min_px: str | None, *, tolerance: float
) -> tuple[int, int]:
    """The rendition's pixel size: the largest frame of `ratio` the source holds
    at its own resolution — so a crop copies pixels rather than resampling them
    — unless that is below `min_px`, when it is the smallest frame of `ratio`
    that meets it (reached by scaling up, uniformly)."""
    wanted = parse_ratio(ratio)
    min_w, min_h = parse_px(min_px)
    native = _largest_within(src_w, src_h, wanted, tolerance)
    if native is not None and native[0] >= min_w and native[1] >= min_h:
        return native
    height = max(min_h, math.ceil(min_w / wanted))
    while True:
        width = max(min_w, round(height * wanted))
        if abs(width / height / wanted - 1) <= tolerance:
            return width, height
        height += 1


def _largest_within(
    src_w: int, src_h: int, wanted: float, tolerance: float
) -> tuple[int, int] | None:
    """Integer `(w, h)` within `tolerance` of `wanted`, inside the source,
    largest first; None when the source is too small to hold any."""
    if src_w / src_h >= wanted:  # height-limited
        for height in range(src_h, 0, -1):
            width = round(height * wanted)
            if 1 <= width <= src_w and abs(width / height / wanted - 1) <= tolerance:
                return width, height
        return None
    for width in range(src_w, 0, -1):  # width-limited
        height = round(width / wanted)
        if 1 <= height <= src_h and abs(width / height / wanted - 1) <= tolerance:
            return width, height
    return None


def geometry(
    src_w: int,
    src_h: int,
    out_w: int,
    out_h: int,
    *,
    origin: tuple[float | Fraction, float | Fraction] | None = None,
    box: tuple[float | Fraction, float | Fraction, float | Fraction, float | Fraction]
    | None = None,
) -> Geometry:
    """The uniform geometry of an `out_w × out_h` frame from a `src_w × src_h`
    source: the largest box of the frame's exact shape, at `origin` (clamped
    inside the source; centred when None). An explicit `box` is checked, not
    trusted: a box that would scale the axes differently raises `StretchError`.
    """
    if box is not None:
        x0, y0, x1, y1 = (Fraction(v) for v in box)
        if not (0 <= x0 < x1 <= src_w and 0 <= y0 < y1 <= src_h):
            raise ValueError(f"box {box} is not inside the {src_w}x{src_h} source")
        return Geometry(out_w, out_h, Fraction(out_w) / (x1 - x0), (x0, y0, x1, y1))
    scale = max(Fraction(out_w, src_w), Fraction(out_h, src_h))
    box_w, box_h = out_w / scale, out_h / scale
    free_x, free_y = src_w - box_w, src_h - box_h
    if origin is None:
        left, top = free_x / 2, free_y / 2
    else:
        left = min(max(Fraction(origin[0]), Fraction(0)), free_x)
        top = min(max(Fraction(origin[1]), Fraction(0)), free_y)
    return Geometry(out_w, out_h, scale, (left, top, left + box_w, top + box_h))


def render(source: Image.Image, placed: Geometry) -> Image.Image:
    """The frame's pixels. At scale 1 on whole pixels a crop copies them
    exactly; otherwise one Lanczos resample of the box — Pillow receives the
    size and the box, so it too scales both axes by `placed.scale`."""
    rgb = flatten(source)
    x0, y0, x1, y1 = placed.box
    if placed.scale == 1 and all(v.denominator == 1 for v in placed.box):
        return rgb.crop((int(x0), int(y0), int(x1), int(y1)))
    return rgb.resize(
        (placed.out_w, placed.out_h),
        Image.Resampling.LANCZOS,
        box=(float(x0), float(y0), float(x1), float(y1)),
    )


def flatten(image: Image.Image) -> Image.Image:
    """RGB, with any transparency composited onto white — an ad image has none."""
    if image.mode == "RGB":
        return image
    if "A" in image.getbands() or "transparency" in image.info:
        rgba = image.convert("RGBA")
        white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(white, rgba).convert("RGB")
    return image.convert("RGB")
