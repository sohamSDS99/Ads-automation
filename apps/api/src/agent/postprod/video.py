"""Video post-production (Stage 04 PRD §9.4 "Video" items 3 and 5; Laws 38, 39).

A model paints clips; everything after that is code, and deterministic. One
ffmpeg run assembles a finished video from the clips of one ratio:

1. **Concat, uniformly.** Every clip is cropped to a box of exactly the
   frame's shape, scaled by ONE rational factor and cropped to the frame —
   `setsar=1`, never stretched: `sx == sy` is exact, and asserted
   (`UniformScale`). Each clip is held to its planned length (padded with its
   last frame when a provider returned it short, trimmed when long), so the
   script's clock is the video's clock. 30 fps (`target_fps`).
2. **Logo** — a registered logo, fitted by padding, overlaid with
   `enable='between(t,0.5,4.5)'`, its clear space kept, in the top corner
   whose measured background gives it ≥ 3:1 (the bottom is the captions').
3. **Captions** burned by libass from a generated `.ass`: Inter Semi Bold, a
   line `caption_height_pct` of the frame tall, on a 60%-opacity box so the
   contrast holds on any frame, above the surface's safe-zone bottom margin.
   A shot's on-screen text is burned the same way, at the top. libass silently falls
   back to another face for a name it cannot match, so the face it chose is
   read back from its log, and a fallback fails the assembly.
4. **End card** — `end_card_ms` of a Pillow-rendered card: the brand colour
   token, the logo, the CTA.
5. **Audio** — `loudnorm=I=-16:TP=-1.5` over the clips' audio when any clip
   has some, in two passes (measure, then one linear gain — `Loudness` says
   why); otherwise a silent AAC track, so every file has one.
6. **Encode** — `libx264 -profile:v high -pix_fmt yuv420p -crf 18 -movflags
   +faststart`, input metadata dropped, the disclosure in the MP4 `comment`.

A non-zero exit keeps ffmpeg's stderr tail and is retried ONCE with
conservative arguments (one thread, a faster preset, bicubic scaling, a deep
muxing queue) — never with a different codec, profile, pixel format or
quality. Then `stamp()` writes XMP `DigitalSourceType` and reads it back, and
`preview_proxy()` / `poster()` make the 480p proxy and poster frame the UI
loads (§15.5 item 3).

No storage or database here: files in, files out. ffmpeg and exiftool are the
processes this module runs; `postprod/verify.py` judges what they wrote.
"""

from __future__ import annotations

import io
import json
import math
import re
import subprocess  # noqa: S404 — ffmpeg and exiftool, with argv built here
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Literal

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from agent.media.capability import parse_ratio
from agent.postprod import image as still
from agent.postprod.probe import VideoFacts
from agent.schemas.creative_video import VideoScript

FFMPEG = "ffmpeg"
EXIFTOOL = "exiftool"
#: One assembly never runs longer than this (a 30 s 1080p master takes ~1 min).
FFMPEG_TIMEOUT_S = 900
#: The share of ffmpeg's stderr kept when it fails (§18: "stderr tail stored").
STDERR_TAIL_CHARS = 4000

#: §9.4 video 3: the logo is shown from 0.5 s to 4.5 s.
LOGO_WINDOW_S = (0.5, 4.5)
#: The bottom corners belong to the captions.
LOGO_CORNERS: tuple[still.Corner, ...] = ("top_right", "top_left")
#: §9.4 video 3: `loudnorm=I=-16:TP=-1.5`; I is `video.loudness_lufs`.
TRUE_PEAK_DBTP = -1.5
AUDIO_RATE = 48_000
AUDIO_BITRATE = "128k"

#: fontconfig's family name for `fonts-inter`'s SemiBold face (fc-query:
#: "Inter,Inter Semi Bold"). "Inter SemiBold" matches nothing, and libass then
#: renders in DejaVu Sans without a word of warning.
CAPTION_FONT = "Inter Semi Bold"
CAPTION_FONT_FILE = "Inter-SemiBold"
FONTS_DIR = "/usr/share/fonts/opentype/inter"
#: §9.4 video 3: the caption box at 60% opacity. ASS alpha is transparency.
CAPTION_BOX_OPACITY = 0.6
CAPTION_TEXT_RGB = (255, 255, 255)
#: WCAG 2.2 AA for text, used for the end card's CTA.
MIN_TEXT_CONTRAST = 4.5

#: x264 and yuv420p need even dimensions.
_EVEN = 2
#: How far `uniform_scale` may trim a source edge to find an exact factor.
_PRE_CROP_SLACK_PX = 16


class AssemblyError(RuntimeError):
    """The assembly cannot even be planned — a gap, never a bad video."""


class FfmpegFailed(RuntimeError):
    def __init__(self, exit_code: int | None, stderr_tail: str, what: str) -> None:
        super().__init__(f"{what}: ffmpeg exited {exit_code}: {stderr_tail[-400:]}")
        self.exit_code = exit_code
        self.stderr_tail = stderr_tail


class StampError(RuntimeError):
    """The disclosure could not be written or did not read back (§13 blocking #8)."""


# ---------------------------------------------------------------------------
# 1. geometry — relay out, never stretch (Law 39)
# ---------------------------------------------------------------------------


def even_size(
    src_w: int, src_h: int, ratio: str, min_px: str | None, *, tolerance: float
) -> tuple[int, int]:
    """The frame of `ratio` a video is made at: the largest even-sided frame
    the source holds at its own resolution, unless that is below `min_px`,
    when it is the smallest even-sided frame of `ratio` that meets it — the
    rule `postprod.image.target_size` applies to stills, on even sides."""
    wanted = parse_ratio(ratio)
    min_w, min_h = still.parse_px(min_px)

    def fits(width: int, height: int) -> bool:
        return (
            width > 0
            and height > 0
            and width % _EVEN == 0
            and height % _EVEN == 0
            and abs(width / height / wanted - 1) <= tolerance
        )

    def widths(height: int) -> list[int]:
        exact = height * wanted
        low = math.floor(exact / _EVEN) * _EVEN
        return sorted({low, low + _EVEN}, key=lambda w: (abs(w - exact), w))

    for height in range(src_h - src_h % _EVEN, 0, -_EVEN):
        for width in widths(height):
            if width <= src_w and fits(width, height):
                if width >= min_w and height >= min_h:
                    return width, height
                break
        else:
            continue
        break
    height = max(min_h, math.ceil(min_w / wanted))
    height += height % _EVEN
    while True:
        for width in widths(height):
            if width >= min_w and fits(width, height):
                return width, height
        height += _EVEN


@dataclass(frozen=True, slots=True)
class UniformScale:
    """`src` → a `pre` box (whole source px) → scaled by ONE factor to `scaled`
    (whole px on both axes) → an `out` frame cropped from that at `offset`."""

    src: tuple[int, int]
    pre: tuple[int, int, int, int]
    scaled: tuple[int, int]
    offset: tuple[int, int]
    out: tuple[int, int]
    scale: Fraction

    def __post_init__(self) -> None:
        _, _, pre_w, pre_h = self.pre
        sx, sy = Fraction(self.scaled[0], pre_w), Fraction(self.scaled[1], pre_h)
        if not (sx == sy == self.scale):
            raise still.StretchError(
                f"{pre_w}x{pre_h} → {self.scaled[0]}x{self.scaled[1]} scales x by {float(sx):.6f} "
                f"and y by {float(sy):.6f}: relay out, never stretch (Law 39)"
            )
        if self.out[0] > self.scaled[0] or self.out[1] > self.scaled[1]:
            raise still.StretchError(f"{self.out} is not inside the scaled {self.scaled}")

    @property
    def crop_box(self) -> tuple[float, float, float, float]:
        """The source box the frame shows, in source px (fractional)."""
        x0 = self.pre[0] + Fraction(self.offset[0]) / self.scale
        y0 = self.pre[1] + Fraction(self.offset[1]) / self.scale
        return (
            float(x0),
            float(y0),
            float(x0 + Fraction(self.out[0]) / self.scale),
            float(y0 + Fraction(self.out[1]) / self.scale),
        )

    def filters(self, *, flags: str) -> str:
        x, y, w, h = self.pre
        return (
            f"crop={w}:{h}:{x}:{y},scale={self.scaled[0]}:{self.scaled[1]}:flags={flags},"
            f"crop={self.out[0]}:{self.out[1]}:{self.offset[0]}:{self.offset[1]},setsar=1"
        )

    def apply(self, frame: Image.Image) -> Image.Image:
        """The same geometry in Pillow, for measuring a frame before assembly."""
        x, y, w, h = self.pre
        scaled = (
            still.flatten(frame)
            .crop((x, y, x + w, y + h))
            .resize(self.scaled, Image.Resampling.LANCZOS)
        )
        ox, oy = self.offset
        return scaled.crop((ox, oy, ox + self.out[0], oy + self.out[1]))

    def transform(self) -> dict[str, Any]:
        factor = float(self.scale)
        return {"crop_box": list(self.crop_box), "sx": factor, "sy": factor}


def uniform_scale(src_w: int, src_h: int, out_w: int, out_h: int) -> UniformScale:
    """The exact uniform geometry of an `out_w × out_h` frame from a clip.

    ffmpeg scales to whole pixels, so an arbitrary factor rounds each axis
    differently — a stretch of a fraction of a percent, but a stretch. Here the
    factor is `n / gcd(w, h)` of a (slightly trimmed) source box, which lands
    both axes on whole pixels; the trim and the final crop are centred and
    chosen to waste the fewest source pixels.
    """
    if min(src_w, src_h, out_w, out_h) <= 0:
        raise AssemblyError(f"cannot frame {out_w}x{out_h} from {src_w}x{src_h}")
    best: tuple[float, UniformScale] | None = None
    for trim_w in range(min(_PRE_CROP_SLACK_PX, src_w - 1) + 1):
        for trim_h in range(min(_PRE_CROP_SLACK_PX, src_h - 1) + 1):
            pre_w, pre_h = src_w - trim_w, src_h - trim_h
            g = math.gcd(pre_w, pre_h)
            step_w, step_h = pre_w // g, pre_h // g
            n = max(math.ceil(out_w / step_w), math.ceil(out_h / step_h))
            scaled = (step_w * n, step_h * n)
            scale = Fraction(n, g)
            # Source px that never reach the frame: the trim, plus what the
            # final crop throws away, measured back in source px.
            waste = (src_w * src_h) - float(out_w * out_h / scale**2)
            if best is not None and waste >= best[0]:
                continue
            placed = UniformScale(
                src=(src_w, src_h),
                pre=(trim_w // 2, trim_h // 2, pre_w, pre_h),
                scaled=scaled,
                offset=((scaled[0] - out_w) // 2, (scaled[1] - out_h) // 2),
                out=(out_w, out_h),
                scale=scale,
            )
            best = (waste, placed)
    assert best is not None  # noqa: S101 — the loops run at least once
    return best[1]


# ---------------------------------------------------------------------------
# 2. the logo — placed on what the frames actually show
# ---------------------------------------------------------------------------


def place_video_logo(
    frames: Sequence[Image.Image],
    logos: Sequence[still.LogoArt],
    *,
    surface: str,
    permitted_surfaces: Sequence[str],
    clear_space_ratio: float | None,
    min_width_px: int | None,
    width_ratio: float,
) -> still.LogoOutcome:
    """`postprod.image.place_logo` on the mean of the frames the logo will sit
    on (sampled across its window), in a top corner. A logo that clears 3:1
    against the average background is placed; `verify.py` then proves it is
    seen in the frames."""
    if not frames:
        return still.LogoOutcome(None, "no frame to place the logo on")
    stack = np.stack([np.asarray(still.flatten(f), dtype=np.float64) for f in frames])
    mean = Image.fromarray(np.clip(stack.mean(axis=0).round(), 0, 255).astype(np.uint8), "RGB")
    return still.place_logo(
        mean,
        logos,
        surface=surface,
        permitted_surfaces=permitted_surfaces,
        clear_space_ratio=clear_space_ratio,
        min_width_px=min_width_px,
        width_ratio=width_ratio,
        saliency=None,
        corners=LOGO_CORNERS,
    )


def logo_png(placement: still.LogoPlacement) -> bytes:
    """The placed logo, fitted by padding to its box, as the overlay input."""
    x0, y0, x1, y1 = placement.box
    fitted = still.fit_by_padding(placement.logo.image, x1 - x0, y1 - y0)
    buffer = io.BytesIO()
    fitted.save(buffer, format="PNG")
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# 3. captions and on-screen text — one generated .ass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TextLayout:
    """Where libass draws, in frame px (PlayRes = the frame)."""

    width: int
    height: int
    #: ASS `Fontsize` — libass renders it as the line height.
    font_px: int
    #: Padding of the 60% box around the text (`Outline` under BorderStyle 3).
    box_pad_px: int
    bottom_px: int
    edge_px: int
    #: Horizontal room the top text leaves for the logo's corner.
    top_side_px: int

    def record(self) -> dict[str, Any]:
        return {"font": CAPTION_FONT, **asdict(self)}


def text_layout(
    width: int,
    height: int,
    *,
    caption_height_pct: float,
    safe_bottom_pct: float,
    safe_edge_pct: float,
    logo: still.LogoPlacement | None,
) -> TextLayout:
    font_px = max(1, round(caption_height_pct * height))
    edge = round(safe_edge_pct * min(width, height))
    top_side = edge
    if logo is not None:
        x0, _, x1, _ = logo.box
        top_side = max(edge, (x1 - x0) + 2 * logo.clear_space_px + edge)
    return TextLayout(
        width=width,
        height=height,
        font_px=font_px,
        box_pad_px=max(1, round(font_px / 4)),
        bottom_px=round(safe_bottom_pct * height),
        edge_px=edge,
        top_side_px=top_side,
    )


def ass_text(text: str) -> str:
    """Text libass draws literally: no override blocks, no line breaks of its
    own. `\\{`/`\\}` are libass's literal braces; a backslash before anything
    else is drawn as itself (measured on libass 0.15.2)."""
    flat = " ".join(text.split())
    return flat.replace("{", "\\{").replace("}", "\\}")


def ass_time(seconds: float) -> str:
    """`H:MM:SS.cc`, rounded to the centisecond ASS counts in."""
    centis = max(0, round(seconds * 100))
    hours, rest = divmod(centis, 360_000)
    minutes, rest = divmod(rest, 6_000)
    secs, cs = divmod(rest, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"


def _ass_colour(rgb: tuple[int, int, int], opacity: float) -> str:
    alpha = round((1 - opacity) * 255)
    r, g, b = rgb
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"


def ass_document(script: VideoScript, layout: TextLayout) -> str:
    """The subtitle file: `Caption` (bottom centre, above the safe zone) and
    `OnScreen` (top centre, clear of the logo's corner), white on the
    60%-opacity box."""
    white = _ass_colour(CAPTION_TEXT_RGB, 1.0)
    box = _ass_colour((0, 0, 0), CAPTION_BOX_OPACITY)
    fmt = (
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"
    )

    def style(name: str, align: int, left: int, right: int, vertical: int) -> str:
        return (
            f"Style: {name},{CAPTION_FONT},{layout.font_px},{white},{white},{box},{box},"
            f"0,0,0,0,100,100,0,0,3,{layout.box_pad_px},0,{align},{left},{right},{vertical},1"
        )

    edge, pad = layout.edge_px, layout.box_pad_px
    styles = [
        style("Caption", 2, edge, edge, layout.bottom_px),
        style("OnScreen", 8, layout.top_side_px, layout.top_side_px, edge + pad),
    ]
    events = [
        f"Dialogue: 0,{ass_time(c.t0)},{ass_time(c.t1)},Caption,,0,0,0,,{ass_text(c.text)}"
        for c in script.captions
    ]
    events.extend(
        f"Dialogue: 1,{ass_time(b.t0)},{ass_time(b.t1)},OnScreen,,0,0,0,,"
        f"{ass_text(b.on_screen_text)}"
        for b in script.beats
        if b.on_screen_text
    )
    return "\n".join(
        [
            "[Script Info]",
            "ScriptType: v4.00+",
            f"PlayResX: {layout.width}",
            f"PlayResY: {layout.height}",
            "WrapStyle: 0",
            "ScaledBorderAndShadow: yes",
            "YCbCr Matrix: None",
            "",
            "[V4+ Styles]",
            fmt,
            *styles,
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
            *events,
            "",
        ]
    )


_FONTSELECT = re.compile(r"fontselect: \((?P<asked>[^,]+), \d+, \d+\) -> (?P<got>[^,]+),")


def fonts_selected(stderr: str) -> list[tuple[str, str]]:
    """libass's `fontselect: (asked, weight, italic) -> file, index, psname` lines."""
    return [(m["asked"].strip(), m["got"].strip()) for m in _FONTSELECT.finditer(stderr)]


# ---------------------------------------------------------------------------
# 4. the end card — Pillow
# ---------------------------------------------------------------------------

_HEX = re.compile(r"#?([0-9a-fA-F]{6}|[0-9a-fA-F]{3})")
#: With no colour token the card is near-black: a neutral, recorded as such.
NEUTRAL_RGB = (17, 17, 17)


def parse_hex(value: str) -> tuple[int, int, int] | None:
    match = _HEX.fullmatch(value.strip())
    if match is None:
        return None
    digits = match.group(1)
    if len(digits) == 3:
        digits = "".join(ch * 2 for ch in digits)
    return int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16)


def brand_colour(tokens: Sequence[Any]) -> dict[str, Any] | None:
    """The brand colour token: the first whose role says `primary`, else the
    first with a readable hex (Stage 03's `ColourToken{name, hex, role}`)."""
    readable = [
        token
        for token in tokens
        if isinstance(token, dict) and parse_hex(str(token.get("hex") or "")) is not None
    ]
    primary = [t for t in readable if "primary" in str(t.get("role") or "").casefold()]
    if not (primary or readable):
        return None
    chosen = (primary or readable)[0]
    return {
        "name": str(chosen.get("name") or ""),
        "hex": str(chosen["hex"]),
        "role": str(chosen.get("role") or ""),
    }


def _card_font(size: int) -> ImageFont.FreeTypeFont:
    path = f"{FONTS_DIR}/{CAPTION_FONT_FILE}.otf"
    try:
        return ImageFont.truetype(path, size)
    except OSError as exc:
        raise AssemblyError(f"the end card's face is not installed: {path}") from exc


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: Any, width: int) -> list[str] | None:
    lines: list[str] = []
    for word in text.split():
        candidate = f"{lines[-1]} {word}" if lines else word
        if lines and draw.textlength(candidate, font=font) <= width:
            lines[-1] = candidate
        elif draw.textlength(word, font=font) <= width:
            lines.append(word)
        else:
            return None
    return lines


@dataclass(frozen=True, slots=True)
class EndCard:
    png: bytes
    record: dict[str, Any]


def end_card(
    width: int,
    height: int,
    *,
    cta: str,
    colour: dict[str, Any] | None,
    logos: Sequence[still.LogoArt],
    clear_space_ratio: float | None,
    min_width_px: int | None,
    width_ratio: float,
    font_px: int,
    edge_px: int,
) -> EndCard:
    """The brand colour token, the registered logo variant that reaches 3:1
    on it (with its clear space), and the CTA in the text colour — white or
    near-black — that contrasts most, at ≥ 4.5:1 or the card is refused."""
    rgb = parse_hex(colour["hex"]) if colour is not None else None
    background = rgb or NEUTRAL_RGB
    bg_lum = still.relative_luminance(background)
    card = Image.new("RGB", (width, height), background)
    record: dict[str, Any] = {
        "colour_token": colour,
        "background": "#{:02x}{:02x}{:02x}".format(*background),
        "logo": None,
        "logo_note": None,
    }
    if colour is None:
        record["colour_note"] = "the brand rules carry no colour token; a neutral card"
    inks = {"white": (255, 255, 255), "near_black": NEUTRAL_RGB}
    ink_name, ink = max(
        inks.items(),
        key=lambda item: still.contrast_ratio(still.relative_luminance(item[1]), bg_lum),
    )
    ink_contrast = still.contrast_ratio(still.relative_luminance(ink), bg_lum)
    if ink_contrast < MIN_TEXT_CONTRAST:
        raise AssemblyError(
            f"no text colour reaches {MIN_TEXT_CONTRAST}:1 on {record['background']} "
            f"(best {ink_contrast:.2f}:1)"
        )
    draw = ImageDraw.Draw(card)
    room = width - 2 * edge_px
    size = max(font_px, round(font_px * 1.6))
    while True:
        font = _card_font(size)
        lines = _wrap(draw, cta, font, room)
        line_h = math.ceil(size * 1.25)
        if lines is not None and len(lines) * line_h <= height // 3:
            break
        if size <= font_px:
            raise AssemblyError(f"the CTA {cta!r} does not fit a {width}x{height} end card")
        size -= 1
    text_h = len(lines) * line_h
    logo_box: tuple[int, int, int, int] | None = None
    chosen = _card_logo(
        logos, bg_lum, width, height - text_h, clear_space_ratio, min_width_px, width_ratio
    )
    if isinstance(chosen, str):
        record["logo_note"] = chosen
        top = (height - text_h) // 2
    else:
        logo, logo_w, logo_h, clear, contrast = chosen
        block = logo_h + 2 * clear + text_h
        y0 = (height - block) // 2 + clear
        x0 = (width - logo_w) // 2
        logo_box = (x0, y0, x0 + logo_w, y0 + logo_h)
        fitted = still.fit_by_padding(logo.image, logo_w, logo_h)
        canvas = card.convert("RGBA")
        canvas.alpha_composite(fitted, dest=(x0, y0))
        card = canvas.convert("RGB")
        draw = ImageDraw.Draw(card)
        top = y0 + logo_h + clear
        record["logo"] = {
            "asset_id": str(logo.asset_id),
            "label": logo.label,
            "box": list(logo_box),
            "clear_space_px": clear,
            "contrast": round(contrast, 2),
        }
    for index, line in enumerate(lines):
        line_w = draw.textlength(line, font=font)
        draw.text(((width - line_w) / 2, top + index * line_h), line, font=font, fill=ink)
    record.update(
        {"cta": cta, "cta_px": size, "ink": ink_name, "ink_contrast": round(ink_contrast, 2)}
    )
    buffer = io.BytesIO()
    card.save(buffer, format="PNG")
    return EndCard(png=buffer.getvalue(), record=record)


def _card_logo(
    logos: Sequence[still.LogoArt],
    bg_lum: float,
    width: int,
    room_h: int,
    clear_space_ratio: float | None,
    min_width_px: int | None,
    width_ratio: float,
) -> tuple[still.LogoArt, int, int, int, float] | str:
    if not logos:
        return "no registered logo could be read"
    if clear_space_ratio is None:
        return "the brand rules state no clear space, so a logo's clear space cannot be kept"
    logo_w = max(math.ceil(width_ratio * width), min_width_px or 0)
    ranked = sorted(logos, key=lambda logo: -still.contrast_ratio(logo.luminance, bg_lum))
    best = ranked[0]
    contrast = still.contrast_ratio(best.luminance, bg_lum)
    if contrast < still.MIN_LOGO_CONTRAST:
        return f"no registered logo reaches 3:1 on the brand colour (best {contrast:.2f}:1)"
    logo_h = math.ceil(logo_w * best.image.height / best.image.width)
    clear = math.ceil(clear_space_ratio * logo_h)
    if logo_w + 2 * clear > width or logo_h + 2 * clear > room_h:
        return f"a {width} px wide card cannot hold a {logo_w} px logo, its clear space and the CTA"
    return best, logo_w, logo_h, clear, contrast


# ---------------------------------------------------------------------------
# 5-6. the ffmpeg run
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Segment:
    """One clip on the video's clock."""

    path: Path
    facts: VideoFacts
    #: Its planned length — what the script was timed on.
    duration_s: float
    geometry: UniformScale


@dataclass(frozen=True, slots=True)
class Assembly:
    """Everything one ffmpeg run needs, all of it decided before it starts."""

    segments: tuple[Segment, ...]
    width: int
    height: int
    fps: int
    end_card_png: Path
    end_card_s: float
    ass_path: Path
    logo_png: Path | None
    logo_xy: tuple[int, int] | None
    loudness_lufs: float
    comment: str

    @property
    def content_s(self) -> float:
        return sum(segment.duration_s for segment in self.segments)

    @property
    def total_s(self) -> float:
        return self.content_s + self.end_card_s

    @property
    def has_source_audio(self) -> bool:
        return any(segment.facts.has_audio for segment in self.segments)


Profile = Literal["standard", "conservative"]
_ENCODER: dict[Profile, dict[str, str]] = {
    "standard": {"preset": "medium", "scale_flags": "lanczos", "threads": "0"},
    # §18: "one retry with conservative encoder args" — one thread, a cheaper
    # preset, a cheaper scaler, a deep muxing queue. Never a different codec,
    # profile, pixel format, quality or frame rate: those are the spec.
    "conservative": {"preset": "veryfast", "scale_flags": "bicubic", "threads": "1"},
}


def _audio_graph(plan: Assembly, loudness: Loudness | None) -> list[str]:
    """The clips' audio on the video's clock (silence where a clip has none,
    and under the end card) → `loudnorm`; or one silent track for the whole
    file when no clip has audio (§9.4 video 3). With `loudness` None the chain
    ends in loudnorm's measuring pass instead of its linear apply."""
    silence = f"anullsrc=r={AUDIO_RATE}:cl=stereo"
    if not plan.has_source_audio:
        return [f"{silence},atrim=duration={plan.total_s:g}[aout]"]
    parts: list[str] = []
    n = len(plan.segments)
    for i, segment in enumerate(plan.segments):
        d = f"{segment.duration_s:g}"
        source = (
            f"[{i}:a]aresample={AUDIO_RATE},aformat=sample_fmts=fltp:channel_layouts=stereo,apad"
            if segment.facts.has_audio
            else silence
        )
        parts.append(f"{source},atrim=duration={d},asetpts=PTS-STARTPTS[a{i}]")
    parts.append(f"{silence},atrim=duration={plan.end_card_s:g}[aend]")
    target = f"loudnorm=I={plan.loudness_lufs:g}:TP={TRUE_PEAK_DBTP:g}"
    if loudness is None:
        tail_filter = f"{target}:print_format=json"
    elif loudness.silent:
        tail_filter = "anull"
    else:
        tail_filter = f"{target}:{loudness.apply()},aresample={AUDIO_RATE}"
    parts.append(
        "".join(f"[a{i}]" for i in range(n)) + f"[aend]concat=n={n + 1}:v=0:a=1,{tail_filter}[aout]"
    )
    return parts


@dataclass(frozen=True, slots=True)
class Loudness:
    """loudnorm's measuring pass over the finished audio (EBU R128 two-pass).

    One pass is not enough: in its single-pass (dynamic) mode loudnorm sets
    its gain from a 3 s window, and behind a silent first shot it never lifts
    the voice at all — measured on the fixtures, -52 LUFS in, -50.9 out. The
    second pass applies one linear gain from these numbers."""

    input_i: float
    input_tp: float
    input_lra: float
    input_thresh: float
    target_offset: float

    @property
    def silent(self) -> bool:
        """Nothing above the gate: there is no loudness to normalise."""
        return not math.isfinite(self.input_i)

    def apply(self) -> str:
        return (
            f"measured_I={self.input_i:g}:measured_TP={self.input_tp:g}:"
            f"measured_LRA={self.input_lra:g}:measured_thresh={self.input_thresh:g}:"
            f"offset={self.target_offset:g}:linear=true"
        )

    def record(self) -> dict[str, Any]:
        return {k: (v if math.isfinite(v) else None) for k, v in asdict(self).items()}


_LOUDNORM_JSON = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.S)


def parse_loudness(stderr: str) -> Loudness:
    found = _LOUDNORM_JSON.findall(stderr)
    if not found:
        raise AssemblyError("loudnorm's measuring pass printed no measurement")
    raw = json.loads(found[-1])

    def number(key: str) -> float:
        return float(str(raw[key]).strip())  # "-inf" parses; an absent key raises

    return Loudness(
        input_i=number("input_i"),
        input_tp=number("input_tp"),
        input_lra=number("input_lra"),
        input_thresh=number("input_thresh"),
        target_offset=number("target_offset"),
    )


def loudness_args(plan: Assembly, profile: Profile) -> list[str]:
    """The measuring pass: the clips' audio chain only, to nowhere."""
    threads = _ENCODER[profile]["threads"]
    inputs: list[str] = []
    for segment in plan.segments:
        inputs += ["-i", str(segment.path)]
    return [
        FFMPEG,
        "-hide_banner",
        "-nostdin",
        "-nostats",
        "-v",
        "info",
        "-y",
        "-filter_complex_threads",
        threads,
        *inputs,
        "-filter_complex",
        ";".join(_audio_graph(plan, None)),
        "-map",
        "[aout]",
        "-f",
        "null",
        "-",
    ]


def _filtergraph(plan: Assembly, profile: Profile, loudness: Loudness | None) -> str:
    flags = _ENCODER[profile]["scale_flags"]
    n = len(plan.segments)
    parts: list[str] = []
    for i, segment in enumerate(plan.segments):
        d = f"{segment.duration_s:g}"
        parts.append(
            f"[{i}:v]{segment.geometry.filters(flags=flags)},fps={plan.fps},"
            f"tpad=stop_mode=clone:stop_duration={d},trim=duration={d},"
            f"setpts=PTS-STARTPTS,format=yuv420p[v{i}]"
        )
    card = n
    parts.append(
        # Rendered at exactly the frame's size: nothing scales it, so a card of
        # another size fails the concat loudly instead of being stretched.
        f"[{card}:v]setsar=1,fps={plan.fps},"
        f"trim=duration={plan.end_card_s:g},setpts=PTS-STARTPTS,format=yuv420p[vend]"
    )
    parts.append("".join(f"[v{i}]" for i in range(n)) + f"[vend]concat=n={n + 1}:v=1:a=0[base]")
    video = "base"
    if plan.logo_png is not None and plan.logo_xy is not None:
        start, end = LOGO_WINDOW_S
        x, y = plan.logo_xy
        parts.append(
            f"[{video}][{n + 1}:v]overlay=x={x}:y={y}:enable='between(t,{start:g},{end:g})'"
            ":format=auto[logo]"
        )
        video = "logo"
    parts.append(
        f"[{video}]ass=filename={_escape(str(plan.ass_path))}:fontsdir={_escape(FONTS_DIR)},"
        "format=yuv420p[vout]"
    )
    parts.extend(_audio_graph(plan, loudness))
    return ";".join(parts)


def _escape(value: str) -> str:
    """A filtergraph option value: `\\`, `:` and `'` are special."""
    return value.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def ffmpeg_args(
    plan: Assembly, output: Path, profile: Profile, loudness: Loudness | None = None
) -> list[str]:
    encoder = _ENCODER[profile]
    inputs: list[str] = []
    for segment in plan.segments:
        inputs += ["-i", str(segment.path)]
    inputs += [
        "-loop",
        "1",
        "-framerate",
        str(plan.fps),
        "-t",
        f"{plan.end_card_s:g}",
        "-i",
        str(plan.end_card_png),
    ]
    if plan.logo_png is not None:
        inputs += ["-i", str(plan.logo_png)]
    threads = encoder["threads"]
    return [
        FFMPEG,
        "-hide_banner",
        "-nostdin",
        "-nostats",
        "-v",
        "info",
        "-y",
        "-filter_threads",
        threads,
        "-filter_complex_threads",
        threads,
        *inputs,
        "-filter_complex",
        _filtergraph(plan, profile, loudness),
        "-map",
        "[vout]",
        "-map",
        "[aout]",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-c:v",
        "libx264",
        "-profile:v",
        "high",
        "-pix_fmt",
        "yuv420p",
        "-crf",
        "18",
        "-preset",
        encoder["preset"],
        "-threads",
        threads,
        "-r",
        str(plan.fps),
        "-vsync",
        "cfr",
        "-c:a",
        "aac",
        "-b:a",
        AUDIO_BITRATE,
        "-ar",
        str(AUDIO_RATE),
        "-ac",
        "2",
        "-t",
        f"{plan.total_s:g}",
        "-max_muxing_queue_size",
        "4096" if profile == "conservative" else "1024",
        "-metadata",
        f"comment={plan.comment}",
        "-movflags",
        "+faststart",
        str(output),
    ]


def encoder_record(profile: Profile) -> dict[str, Any]:
    return {
        "codec": "libx264",
        "profile": "high",
        "pix_fmt": "yuv420p",
        "crf": 18,
        "movflags": "+faststart",
        "audio": {"codec": "aac", "bitrate": AUDIO_BITRATE, "rate": AUDIO_RATE},
        "args": profile,
        **{k: v for k, v in _ENCODER[profile].items()},
    }


def tail(stderr: bytes | str) -> str:
    text = stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else stderr
    return text[-STDERR_TAIL_CHARS:]


def run_ffmpeg(args: list[str], *, what: str, timeout: float = FFMPEG_TIMEOUT_S) -> str:
    """Run ffmpeg; its stderr on success, `FfmpegFailed` with the tail otherwise."""
    try:
        done = subprocess.run(  # noqa: S603 — argv built here, no shell
            args, capture_output=True, timeout=timeout, check=False
        )
    except FileNotFoundError as exc:
        raise FfmpegFailed(None, f"{args[0]} is not installed: {exc}", what) from exc
    except subprocess.TimeoutExpired as exc:
        raise FfmpegFailed(
            None, tail(exc.stderr or b"") + f"\n[timed out after {timeout:g} s]", what
        ) from exc
    stderr = done.stderr.decode("utf-8", "replace")
    if done.returncode != 0:
        raise FfmpegFailed(done.returncode, tail(stderr), what)
    return stderr


@dataclass
class Attempt:
    args: Profile
    exit_code: int | None
    stderr_tail: str

    def record(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Assembled:
    output: Path
    profile: Profile
    fonts: list[tuple[str, str]]
    loudness: Loudness | None
    #: The failed attempts before the one that succeeded (§18).
    failures: list[Attempt] = field(default_factory=list)


class AssemblyFailed(RuntimeError):
    """Both attempts exited non-zero — a gap, with both stderr tails."""

    def __init__(self, attempts: list[Attempt]) -> None:
        last = attempts[-1]
        super().__init__(f"ffmpeg failed {len(attempts)} times; last exit {last.exit_code}")
        self.attempts = attempts


def assemble(
    plan: Assembly,
    output: Path,
    *,
    runner: Any = run_ffmpeg,
) -> Assembled:
    """One run with the standard arguments; on a non-zero exit, its stderr
    tail is kept and ONE retry with conservative arguments follows (§18).
    A caption face other than Inter Semi Bold fails the run it came from."""
    failures: list[Attempt] = []
    profiles: tuple[Profile, ...] = ("standard", "conservative")
    for profile in profiles:
        try:
            loudness = None
            if plan.has_source_audio:
                measured = runner(loudness_args(plan, profile), what=f"loudness ({profile})")
                loudness = parse_loudness(measured)
            stderr = runner(
                ffmpeg_args(plan, output, profile, loudness), what=f"assembly ({profile})"
            )
        except FfmpegFailed as exc:
            failures.append(Attempt(profile, exc.exit_code, exc.stderr_tail))
            continue
        fonts = fonts_selected(stderr)
        wrong = [(asked, got) for asked, got in fonts if got != CAPTION_FONT_FILE]
        if wrong:
            raise AssemblyError(
                "libass drew captions in a fallback face: "
                + "; ".join(f"{asked!r} -> {got!r}" for asked, got in wrong)
            )
        return Assembled(
            output=output, profile=profile, fonts=fonts, loudness=loudness, failures=failures
        )
    raise AssemblyFailed(failures)


# ---------------------------------------------------------------------------
# frames, the proxy and the poster
# ---------------------------------------------------------------------------


def frame_at(path: Path, seconds: float) -> Image.Image:
    """The decoded frame on screen at `seconds` (accurate seek)."""
    stdout = subprocess.run(  # noqa: S603 — argv built here, no shell
        [
            FFMPEG,
            "-hide_banner",
            "-nostdin",
            "-v",
            "error",
            "-ss",
            f"{seconds:.3f}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "-",
        ],
        capture_output=True,
        timeout=120,
        check=False,
    )
    if stdout.returncode != 0 or not stdout.stdout:
        raise FfmpegFailed(stdout.returncode, tail(stdout.stderr), f"frame at {seconds:.3f} s")
    with Image.open(io.BytesIO(stdout.stdout)) as opened:
        return opened.convert("RGB")


#: §15.5 item 3: the grid plays a 480p proxy; the short side is 480 px.
PROXY_SHORT_SIDE = 480


def proxy_size(width: int, height: int) -> tuple[int, int]:
    """480p: the short side 480 (or the master's, if smaller), even sides, the
    master's ratio within one pixel."""
    short = min(PROXY_SHORT_SIDE, min(width, height) - min(width, height) % _EVEN)
    if width <= height:
        long_side = round(height * short / width / _EVEN) * _EVEN
        return short, long_side
    long_side = round(width * short / height / _EVEN) * _EVEN
    return long_side, short


def preview_proxy(master: Path, output: Path, *, width: int, height: int) -> UniformScale:
    """The 480p proxy the UI plays (`preload="metadata"`): the same uniform
    geometry rule as the master, H.264 high / yuv420p / +faststart, AAC."""
    out_w, out_h = proxy_size(width, height)
    placed = uniform_scale(width, height, out_w, out_h)
    run_ffmpeg(
        [
            FFMPEG,
            "-hide_banner",
            "-nostdin",
            "-nostats",
            "-v",
            "error",
            "-y",
            "-i",
            str(master),
            "-vf",
            placed.filters(flags="bicubic") + ",format=yuv420p",
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-map_metadata",
            "0",
            "-c:v",
            "libx264",
            "-profile:v",
            "high",
            "-pix_fmt",
            "yuv420p",
            "-crf",
            "28",
            "-preset",
            "veryfast",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            "-movflags",
            "+faststart",
            str(output),
        ],
        what="preview proxy",
    )
    return placed


def poster_jpeg(frame: Image.Image) -> bytes:
    buffer = io.BytesIO()
    still.flatten(frame).save(buffer, format="JPEG", quality=90, optimize=True)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# the stamp — MP4 comment (written by ffmpeg) + XMP DigitalSourceType
# ---------------------------------------------------------------------------


def comment(*, composited: bool, model_id: str) -> str:
    """The MP4 `comment`: what made the file, in words and as the IPTC term."""
    uri = still.digital_source_type(composited=composited)
    return f"AI-generated video ({model_id}); IPTC DigitalSourceType {uri}"


def stamp(path: Path, *, composited: bool) -> dict[str, Any]:
    """Write XMP `Iptc4xmpExt:DigitalSourceType` into the MP4 in place, then read
    the file back: the term must be exactly the one written, and the comment
    ffmpeg wrote must still be there."""
    uri = still.digital_source_type(composited=composited)
    _exiftool(["-overwrite_original", f"-XMP-iptcExt:DigitalSourceType={uri}", str(path)])
    back = read_stamp(path)
    if back.get("DigitalSourceType") != uri:
        raise StampError(
            f"the stamp did not read back: wrote {uri!r}, read {back.get('DigitalSourceType')!r}"
        )
    if not back.get("Comment"):
        raise StampError("the MP4 comment did not survive the XMP write")
    leaked = sorted(key for key in back if key.startswith("GPS"))
    if leaked:
        raise StampError(f"GPS survived into the master: {', '.join(leaked)}")
    return {"xmp_digital_source_type": uri, "mp4_comment": back["Comment"]}


def read_stamp(path: Path) -> dict[str, Any]:
    out = _exiftool(
        ["-j", "-XMP-iptcExt:DigitalSourceType", "-QuickTime:Comment", "-GPS:all", str(path)]
    )
    try:
        (record,) = json.loads(out)
    except (ValueError, TypeError) as exc:
        raise StampError(f"exiftool returned no readable record: {exc}") from exc
    return dict(record)


def _exiftool(arguments: list[str]) -> bytes:
    try:
        done = subprocess.run(  # noqa: S603 — a fixed binary, arguments built here
            [EXIFTOOL, *arguments], capture_output=True, timeout=120, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StampError(f"exiftool could not run: {exc}") from exc
    if done.returncode != 0:
        detail = done.stderr.decode("utf-8", "replace").strip() or f"exit {done.returncode}"
        raise StampError(f"exiftool failed: {detail}")
    return done.stdout


# ---------------------------------------------------------------------------
# planning one master
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Knobs:
    """The constants and brand rules one assembly runs on."""

    fps: int
    caption_height_pct: float
    safe_bottom_pct: float
    safe_edge_pct: float
    end_card_ms: int
    loudness_lufs: float
    ratio_tolerance: float
    logo_width_ratio: float
    permitted_surfaces: tuple[str, ...]
    clear_space_ratio: float | None
    min_width_px: int | None


@dataclass(frozen=True, slots=True)
class ClipFile:
    path: Path
    facts: VideoFacts
    #: The shot plan's length for this clip.
    duration_s: float


@dataclass(frozen=True, slots=True)
class Prepared:
    assembly: Assembly
    layout: TextLayout
    logo: still.LogoOutcome
    end_card: dict[str, Any]

    @property
    def geometry(self) -> UniformScale:
        """The geometry `MediaArtifact.transform` records: the largest scale
        any clip takes (one number in both keys; each clip's own is listed)."""
        return max((s.geometry for s in self.assembly.segments), key=lambda g: g.scale)

    def transform(self, *, encoder: dict[str, Any], node_id: str) -> dict[str, Any]:
        placement = self.logo.placement
        return {
            **self.geometry.transform(),
            "clips": [
                {"path": s.path.name, "duration_s": s.duration_s, **s.geometry.transform()}
                for s in self.assembly.segments
            ],
            "fps": self.assembly.fps,
            "logo": placement.record() if placement is not None else None,
            "logo_window_s": list(LOGO_WINDOW_S) if placement is not None else None,
            "logo_note": self.logo.reason,
            "captions": self.layout.record(),
            "end_card": {**self.end_card, "ms": round(self.assembly.end_card_s * 1000)},
            "audio": (
                {"loudnorm": f"I={self.assembly.loudness_lufs:g}:TP={TRUE_PEAK_DBTP:g}"}
                if self.assembly.has_source_audio
                else {"silent_aac": True}
            ),
            "encoder_args": encoder,
            "node_id": node_id,
        }


def logo_sample_times(content_s: float) -> list[float]:
    """Where the logo will sit: 0.5, 1.5, …, 4.5 s, inside the footage."""
    start, end = LOGO_WINDOW_S
    last = min(end, content_s - 0.05)
    times: list[float] = []
    t = start
    while t <= last + 1e-9:
        times.append(round(t, 3))
        t += 1.0
    return times


def prepare(
    clips: Sequence[ClipFile],
    *,
    ratio: str,
    min_px: str | None,
    script: VideoScript,
    logos: Sequence[still.LogoArt],
    colour: dict[str, Any] | None,
    surface: str,
    knobs: Knobs,
    workdir: Path,
    model_id: str,
) -> Prepared:
    """Everything the run needs, decided up front and written to `workdir`:
    the frame, each clip's uniform geometry, the logo's corner and variant
    (measured on the footage it will sit on), the `.ass`, the end card."""
    if not clips:
        raise AssemblyError("no clip to assemble")
    width, height = even_size(
        min(c.facts.width for c in clips),
        min(c.facts.height for c in clips),
        ratio,
        min_px,
        tolerance=knobs.ratio_tolerance,
    )
    segments = tuple(
        Segment(
            path=clip.path,
            facts=clip.facts,
            duration_s=clip.duration_s,
            geometry=uniform_scale(clip.facts.width, clip.facts.height, width, height),
        )
        for clip in clips
    )
    content_s = sum(s.duration_s for s in segments)
    samples: list[Image.Image] = []
    for t in logo_sample_times(content_s):
        start = 0.0
        for segment in segments:
            if t < start + segment.duration_s:
                local = min(t - start, segment.facts.duration_ms / 1000 - 0.05)
                samples.append(segment.geometry.apply(frame_at(segment.path, max(0.0, local))))
                break
            start += segment.duration_s
    outcome = place_video_logo(
        samples,
        logos,
        surface=surface,
        permitted_surfaces=knobs.permitted_surfaces,
        clear_space_ratio=knobs.clear_space_ratio,
        min_width_px=knobs.min_width_px,
        width_ratio=knobs.logo_width_ratio,
    )
    layout = text_layout(
        width,
        height,
        caption_height_pct=knobs.caption_height_pct,
        safe_bottom_pct=knobs.safe_bottom_pct,
        safe_edge_pct=knobs.safe_edge_pct,
        logo=outcome.placement,
    )
    end_s = knobs.end_card_ms / 1000
    card = end_card(
        width,
        height,
        cta=script.cta,
        colour=colour,
        logos=logos,
        clear_space_ratio=knobs.clear_space_ratio,
        min_width_px=knobs.min_width_px,
        width_ratio=knobs.logo_width_ratio,
        font_px=layout.font_px,
        edge_px=layout.edge_px,
    )
    workdir.mkdir(parents=True, exist_ok=True)
    card_path = workdir / "end_card.png"
    card_path.write_bytes(card.png)
    ass_path = workdir / "captions.ass"
    ass_path.write_text(ass_document(script, layout), encoding="utf-8")
    logo_path: Path | None = None
    logo_xy: tuple[int, int] | None = None
    if outcome.placement is not None:
        logo_path = workdir / "logo.png"
        logo_path.write_bytes(logo_png(outcome.placement))
        logo_xy = outcome.placement.box[0], outcome.placement.box[1]
    assembly = Assembly(
        segments=segments,
        width=width,
        height=height,
        fps=knobs.fps,
        end_card_png=card_path,
        end_card_s=end_s,
        ass_path=ass_path,
        logo_png=logo_path,
        logo_xy=logo_xy,
        loudness_lufs=knobs.loudness_lufs,
        # Captions, the end card and any logo are code's marks on a model's
        # footage: always a composite (IPTC compositeWithTrainedAlgorithmicMedia).
        comment=comment(composited=True, model_id=model_id),
    )
    return Prepared(assembly=assembly, layout=layout, logo=outcome, end_card=card.record)
