"""Image post-production (Stage 04 PRD §9.4 "Images"; Laws 38, 39).

A model paints a master; everything after that is code, and deterministic:

1. **Renditions** follow the ratio plan — `relaid` > `native` > `crop` — and a
   crop's window is `media.crop_window_v1`'s, which may turn it into a gap.
2. **Scaling is uniform only.** A `Geometry` is one scale factor and a box of
   the source with exactly the frame's shape, in rational arithmetic, so
   `sx == sy` is a fact that can be asserted — and is, at construction — and
   is persisted as one number in both keys (the DB CHECK compares their text).
3. **Logos** — Stage 03's registered ones only, on `logo.permitted_surfaces`
   only, never on `search_image`. The variant is the one whose measured
   luminance reaches ≥ 3:1 against the background it would sit on; it keeps
   `clear_space_ratio × logo height` from every edge, is at least
   `min_width_px` wide, and is fitted by padding, never stretched.
4. **Encode** as JPEG at the highest quality, searched down to
   `jpeg_quality_floor`, whose STAMPED file fits `max_bytes` — else a gap.
5. **Stamp** — EXIF and GPS stripped, XMP `Iptc4xmpExt:DigitalSourceType`
   written by `exiftool` and read back; and any visible disclosure label a
   pinned Stage 03 rule requires for this surface and market, drawn at its
   placement.

No storage or database here: bytes and pixels in, bytes out — `exiftool` is
the one process this module runs. The node stores and records.
"""

from __future__ import annotations

import io
import json
import math
import subprocess
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Literal

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from agent.media.capability import Coverage, parse_ratio, ratio_coverage, supported_ratios
from agent.media.types import CapabilityRecord
from agent.schemas.guardrails import DisclosureRule


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


# ---------------------------------------------------------------------------
# 3. logos
# ---------------------------------------------------------------------------

#: §9.4 item 3: "≥ 3:1 contrast" — WCAG 2.2's floor for a graphical object.
MIN_LOGO_CONTRAST = 3.0
#: Never on a Search image (Law 38; Google disapproves any added logo there),
#: whatever `logo.permitted_surfaces` says.
NO_LOGO_SURFACE = "search_image"

Corner = Literal["bottom_right", "bottom_left", "top_right", "top_left"]
#: The tie-break when corners are equally salient: where a logo conventionally sits.
CORNERS: tuple[Corner, ...] = ("bottom_right", "bottom_left", "top_right", "top_left")


@dataclass(frozen=True, slots=True)
class LogoArt:
    """One registered logo, trimmed to its visible pixels, with their luminance."""

    asset_id: uuid.UUID
    label: str
    image: Image.Image
    luminance: float


@dataclass(frozen=True, slots=True)
class LogoPlacement:
    logo: LogoArt
    corner: Corner
    #: `(x0, y0, x1, y1)` in frame px — the logo itself, clear space outside it.
    box: tuple[int, int, int, int]
    clear_space_px: int
    background_luminance: float
    contrast: float

    @property
    def label(self) -> str:
        return self.logo.label

    def record(self) -> dict[str, Any]:
        return {
            "asset_id": str(self.logo.asset_id),
            "label": self.logo.label,
            "corner": self.corner,
            "box": list(self.box),
            "clear_space_px": self.clear_space_px,
            "contrast": round(self.contrast, 2),
        }


@dataclass(frozen=True, slots=True)
class LogoOutcome:
    placement: LogoPlacement | None
    #: Why there is no logo — recorded, never silent.
    reason: str | None = None


def relative_luminance(rgb: tuple[int, int, int]) -> float:
    """WCAG 2.2 relative luminance of one sRGB colour."""
    return float(_luminance(np.asarray([rgb], dtype=np.float64)).mean())


def _luminance(pixels: np.ndarray) -> np.ndarray:
    channel = pixels[..., :3] / 255.0
    linear = np.where(channel <= 0.04045, channel / 12.92, ((channel + 0.055) / 1.055) ** 2.4)
    weights: np.ndarray = linear @ np.asarray([0.2126, 0.7152, 0.0722])
    return weights


def contrast_ratio(a: float, b: float) -> float:
    high, low = max(a, b), min(a, b)
    return (high + 0.05) / (low + 0.05)


def load_logo(content: bytes, *, asset_id: uuid.UUID, label: str) -> LogoArt:
    """Decode a registered logo and trim it to its visible pixels — clear space
    is measured from the mark, not from the margin the file happened to carry."""
    with Image.open(io.BytesIO(content)) as opened:
        image = opened.convert("RGBA")
    visible = image.getchannel("A").point(lambda a: 255 if a > 0 else 0).getbbox()
    if visible is None:
        raise ValueError(f"registered logo {asset_id} has no visible pixels")
    image = image.crop(visible)
    pixels = np.asarray(image, dtype=np.float64)
    alpha = pixels[..., 3] / 255.0
    luminance = float((_luminance(pixels) * alpha).sum() / alpha.sum())
    return LogoArt(asset_id=asset_id, label=label, image=image, luminance=luminance)


def place_logo(
    frame: Image.Image,
    logos: Sequence[LogoArt],
    *,
    surface: str,
    permitted_surfaces: Sequence[str],
    clear_space_ratio: float | None,
    min_width_px: int | None,
    width_ratio: float,
    saliency: np.ndarray | None,
    corners: Sequence[Corner] = CORNERS,
) -> LogoOutcome:
    """Where the logo goes and which variant, or why none does.

    Corners (`corners`, all four unless a caller reserves some — a video keeps
    the bottom for its captions) are ranked by the saliency their footprint
    would cover (least first — the logo never sits on the subject), then in
    `CORNERS` order. In
    the first corner where any variant reaches 3:1 against the measured
    background, the variant with the highest contrast is placed.
    """
    if surface == NO_LOGO_SURFACE:
        return LogoOutcome(None, "never on search_image (Law 38)")
    if surface not in permitted_surfaces:
        return LogoOutcome(None, f"{surface} is not in logo.permitted_surfaces")
    if not logos:
        return LogoOutcome(None, "no registered logo could be read")
    if clear_space_ratio is None:
        return LogoOutcome(
            None, "the brand rules state no clear space, so a logo's clear space cannot be kept"
        )
    width, height = frame.size
    logo_w = max(math.ceil(width_ratio * width), min_width_px or 0)
    sized = []
    for logo in logos:
        logo_h = math.ceil(logo_w * logo.image.height / logo.image.width)
        clear = math.ceil(clear_space_ratio * logo_h)
        if logo_w + 2 * clear <= width and logo_h + 2 * clear <= height:
            sized.append((logo, logo_h, clear))
    if not sized:
        return LogoOutcome(
            None,
            f"a {width}x{height} frame cannot hold a {logo_w} px wide logo and its clear space",
        )
    pixels = np.asarray(flatten(frame), dtype=np.float64)
    luminance = _luminance(pixels)
    footprint_h = max(logo_h + 2 * clear for _, logo_h, clear in sized)
    footprint_w = max(logo_w + 2 * clear for _, _, clear in sized)

    def corner_mass(corner: Corner) -> float:
        if saliency is None:
            return 0.0
        x0, y0 = _corner_origin(corner, width, height, footprint_w, footprint_h, 0)
        return float(saliency[y0 : y0 + footprint_h, x0 : x0 + footprint_w].sum())

    best = 0.0
    for corner in sorted(corners, key=lambda c: (corner_mass(c), CORNERS.index(c))):
        candidates = []
        for logo, logo_h, clear in sized:
            x0, y0 = _corner_origin(corner, width, height, logo_w, logo_h, clear)
            zone = luminance[
                max(0, y0 - clear) : y0 + logo_h + clear, max(0, x0 - clear) : x0 + logo_w + clear
            ]
            background = float(zone.mean())
            ratio = contrast_ratio(logo.luminance, background)
            best = max(best, ratio)
            candidates.append((ratio, logo, (x0, y0, x0 + logo_w, y0 + logo_h), clear, background))
        ratio, logo, box, clear, background = max(candidates, key=lambda item: item[0])
        if ratio >= MIN_LOGO_CONTRAST:
            return LogoOutcome(LogoPlacement(logo, corner, box, clear, background, ratio))
    return LogoOutcome(
        None,
        f"no registered logo reaches 3:1 contrast in any "
        f"{'corner' if tuple(corners) == CORNERS else 'permitted corner'} (best {best:.2f}:1)",
    )


def _corner_origin(
    corner: Corner, width: int, height: int, box_w: int, box_h: int, clear: int
) -> tuple[int, int]:
    right, bottom = width - clear - box_w, height - clear - box_h
    return {
        "bottom_right": (right, bottom),
        "bottom_left": (clear, bottom),
        "top_right": (right, clear),
        "top_left": (clear, clear),
    }[corner]


def padding_scale(logo_w: int, logo_h: int, width: int, height: int) -> Fraction:
    """The one factor that fits a `logo_w × logo_h` logo inside `width × height`."""
    return min(Fraction(width, logo_w), Fraction(height, logo_h))


def logo_canvas(
    logo_w: int, logo_h: int, ratio: str, min_px: str | None, *, tolerance: float
) -> tuple[int, int]:
    """The smallest canvas of `ratio` (within `tolerance`), at least `min_px`,
    that holds the logo at its own size — the frame a logo slot is padded to."""
    wanted = parse_ratio(ratio)
    min_w, min_h = parse_px(min_px)
    height = max(logo_h, min_h, math.ceil(logo_w / wanted), math.ceil(min_w / wanted))
    while True:
        width = max(logo_w, min_w, round(height * wanted))
        if abs(width / height / wanted - 1) <= tolerance:
            return width, height
        height += 1


def fit_by_padding(logo: Image.Image, width: int, height: int) -> Image.Image:
    """`logo` scaled by ONE factor to fit `width × height`, centred on a
    transparent canvas of exactly that size — padded, never stretched. The
    source box has the target's exact shape, so both axes scale identically."""
    scale = padding_scale(logo.width, logo.height, width, height)
    fit_w = max(1, math.floor(logo.width * scale))
    fit_h = max(1, math.floor(logo.height * scale))
    scaled = logo.convert("RGBA").resize(
        (fit_w, fit_h),
        Image.Resampling.LANCZOS,
        box=(0.0, 0.0, float(fit_w / scale), float(fit_h / scale)),
    )
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    canvas.alpha_composite(scaled, dest=((width - fit_w) // 2, (height - fit_h) // 2))
    return canvas


def composite_logo(frame: Image.Image, placement: LogoPlacement) -> Image.Image:
    x0, y0, x1, y1 = placement.box
    fitted = fit_by_padding(placement.logo.image, x1 - x0, y1 - y0)
    canvas = flatten(frame).convert("RGBA")
    canvas.alpha_composite(fitted, dest=(x0, y0))
    return canvas.convert("RGB")


# ---------------------------------------------------------------------------
# 5. visible disclosure labels
# ---------------------------------------------------------------------------

#: `fonts-inter` in the worker image (PRD §22): the face captions use.
LABEL_FONTS = (
    "/usr/share/fonts/opentype/inter/Inter-SemiBold.otf",
    "/usr/share/fonts/opentype/inter/Inter-Medium.otf",
)
#: The box behind the label, as §9.4's captions: 60% opacity, so contrast holds on any frame.
LABEL_BOX_ALPHA = 153


class LabelError(RuntimeError):
    """A required label could not be drawn — the rendition cannot ship without it."""


def required_labels(
    rules: Sequence[DisclosureRule], *, surface: str, market: str
) -> list[DisclosureRule]:
    """The pinned rules that name this image surface, for this market (a rule
    with no markets applies in every market). A rule that names no image
    surface is a copy rule — the linter enforces it on text."""
    return [
        rule
        for rule in rules
        if surface in rule.surfaces and (not rule.markets or market in rule.markets)
    ]


def apply_labels(
    frame: Image.Image, rules: Sequence[DisclosureRule], *, height_pct: float
) -> tuple[Image.Image, list[dict[str, Any]]]:
    """Each rule's `required_text`, drawn white on a 60%-opacity box, its text
    `height_pct` of the frame tall: `prefix` top-left, `suffix` bottom-right,
    `anywhere` bottom-left. Raises `LabelError` when it cannot be drawn whole."""
    if not rules:
        return frame, []
    width, height = frame.size
    size = max(1, round(height_pct * height))
    font = _label_font(size)
    canvas = flatten(frame).convert("RGBA")
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    drawn: list[dict[str, Any]] = []
    stacked: dict[str, int] = {}
    pad = max(1, size // 3)
    for rule in rules:
        bounds = draw.textbbox((0, 0), rule.required_text, font=font)
        left, top = math.floor(bounds[0]), math.floor(bounds[1])
        right, bottom = math.ceil(bounds[2]), math.ceil(bounds[3])
        box_w = right - left + 2 * pad
        box_h = max(size, bottom - top) + 2 * pad
        if box_w > width - 2 * pad:
            raise LabelError(f"label {rule.required_text!r} is wider than a {width} px frame")
        offset = stacked.get(rule.placement, 0)
        x0 = pad if rule.placement in ("prefix", "anywhere") else width - pad - box_w
        y0 = pad + offset if rule.placement == "prefix" else height - pad - box_h - offset
        if y0 < 0 or y0 + box_h > height:
            raise LabelError(f"labels for placement {rule.placement!r} do not fit the frame")
        stacked[rule.placement] = offset + box_h + pad
        draw.rectangle((x0, y0, x0 + box_w - 1, y0 + box_h - 1), fill=(0, 0, 0, LABEL_BOX_ALPHA))
        draw.text(
            (x0 + pad - left, y0 + (box_h - (bottom - top)) // 2 - top),
            rule.required_text,
            font=font,
            fill=(255, 255, 255, 255),
        )
        drawn.append(
            {
                "disclosure_id": rule.disclosure_id,
                "text": rule.required_text,
                "placement": rule.placement,
                "box": [x0, y0, x0 + box_w, y0 + box_h],
            }
        )
    canvas.alpha_composite(overlay)
    return canvas.convert("RGB"), drawn


def _label_font(size: int) -> ImageFont.FreeTypeFont:
    for path in LABEL_FONTS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    raise LabelError(f"no label face is installed (looked for {', '.join(LABEL_FONTS)})")


# ---------------------------------------------------------------------------
# 4. encode to fit
# ---------------------------------------------------------------------------

#: The top of the search. Pillow: "values above 95 should be avoided".
JPEG_QUALITY_CEILING = 95


@dataclass(frozen=True, slots=True)
class Encoded:
    content: bytes
    quality: int

    @property
    def encoder_args(self) -> dict[str, Any]:
        return {"format": "JPEG", "quality": self.quality, "optimize": True}


def encode_jpeg(
    frame: Image.Image, *, max_bytes: int | None, quality_floor: int, overhead: int = 0
) -> Encoded | None:
    """The highest quality in [floor, 95] whose file plus `overhead` (the
    stamp) fits `max_bytes`, by binary search; None when even the floor does
    not fit — a gap, never an over-size file or a quality below the floor."""
    rgb = flatten(frame)
    cache: dict[int, bytes] = {}

    def at(quality: int) -> bytes:
        if quality not in cache:
            buffer = io.BytesIO()
            rgb.save(buffer, format="JPEG", quality=quality, optimize=True)
            cache[quality] = buffer.getvalue()
        return cache[quality]

    if max_bytes is None or len(at(JPEG_QUALITY_CEILING)) + overhead <= max_bytes:
        return Encoded(at(JPEG_QUALITY_CEILING), JPEG_QUALITY_CEILING)
    if len(at(quality_floor)) + overhead > max_bytes:
        return None
    low, high = quality_floor, JPEG_QUALITY_CEILING - 1  # `low` always fits
    while low < high:
        middle = (low + high + 1) // 2
        if len(at(middle)) + overhead <= max_bytes:
            low = middle
        else:
            high = middle - 1
    return Encoded(at(low), low)


# ---------------------------------------------------------------------------
# 5. the disclosure stamp
# ---------------------------------------------------------------------------

#: IPTC NewsCodes Digital Source Type (§13 "AI disclosure").
_IPTC_DST = "http://cv.iptc.org/newscodes/digitalsourcetype/"
TRAINED = _IPTC_DST + "trainedAlgorithmicMedia"
COMPOSITE = _IPTC_DST + "compositeWithTrainedAlgorithmicMedia"
_EXIFTOOL_TIMEOUT_S = 60


class StampError(RuntimeError):
    """The disclosure could not be written, or did not read back — unstamped
    media never ships (§13 blocking #8)."""


def digital_source_type(*, composited: bool) -> str:
    return COMPOSITE if composited else TRAINED


def stamp(content: bytes, *, composited: bool) -> tuple[bytes, dict[str, Any]]:
    """Strip every tag (EXIF and GPS included), write the XMP DigitalSourceType
    — `compositeWithTrainedAlgorithmicMedia` when code composited a logo onto
    the model's pixels — then re-read the written bytes and refuse anything
    that does not say exactly that, or still carries EXIF or GPS."""
    uri = digital_source_type(composited=composited)
    written = _exiftool(
        ["-all=", f"-XMP-iptcExt:DigitalSourceType#={uri}", "-o", "-", "-"], content
    )
    back = read_stamp(written)
    if back.get("DigitalSourceType") != uri:
        raise StampError(
            f"the stamp did not read back: wrote {uri!r}, read {back.get('DigitalSourceType')!r}"
        )
    leaked = sorted(key for key in back if key not in ("DigitalSourceType", "SourceFile"))
    if leaked:
        raise StampError(f"EXIF/GPS survived the strip: {', '.join(leaked)}")
    return written, {"xmp_digital_source_type": uri}


def read_stamp(content: bytes) -> dict[str, Any]:
    """What a file says about itself: its DigitalSourceType and every EXIF and
    GPS tag it still carries, raw values (`-n`), as exiftool reads them."""
    out = _exiftool(
        ["-j", "-n", "-XMP-iptcExt:DigitalSourceType", "-EXIF:all", "-GPS:all", "-"], content
    )
    try:
        (record,) = json.loads(out)
    except (ValueError, TypeError) as exc:
        raise StampError(f"exiftool returned no readable record: {exc}") from exc
    return dict(record)


def encode_and_stamp(
    frame: Image.Image, *, max_bytes: int | None, quality_floor: int, composited: bool
) -> tuple[bytes, dict[str, Any], dict[str, Any]] | None:
    """Encode, stamp, and keep the STAMPED file under `max_bytes`.

    The stamp's size does not depend on the pixels, so it is measured once on
    the ceiling-quality encode and reserved while the quality is searched. The
    result is checked after stamping all the same; None is a gap.
    """
    first = encode_jpeg(frame, max_bytes=None, quality_floor=quality_floor)
    assert first is not None  # no cap: the ceiling is always returned
    stamped, disclosure = stamp(first.content, composited=composited)
    if max_bytes is None or len(stamped) <= max_bytes:
        return stamped, first.encoder_args, disclosure
    overhead = len(stamped) - len(first.content)
    fitted = encode_jpeg(frame, max_bytes=max_bytes, quality_floor=quality_floor, overhead=overhead)
    if fitted is None:
        return None
    stamped, disclosure = stamp(fitted.content, composited=composited)
    if len(stamped) > max_bytes:
        return None
    return stamped, fitted.encoder_args, disclosure


def _exiftool(arguments: list[str], content: bytes) -> bytes:
    try:
        done = subprocess.run(  # noqa: S603 — a fixed binary, arguments built here
            ["exiftool", *arguments],  # noqa: S607 — installed in the worker image
            input=content,
            capture_output=True,
            timeout=_EXIFTOOL_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StampError(f"exiftool could not run: {exc}") from exc
    if done.returncode != 0 or not done.stdout:
        detail = done.stderr.decode("utf-8", "replace").strip() or f"exit {done.returncode}"
        raise StampError(f"exiftool failed: {detail}")
    return done.stdout
