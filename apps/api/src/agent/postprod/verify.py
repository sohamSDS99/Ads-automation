"""Verify a finished video (Stage 04 PRD §9.4 video 4). A failure is blocking.

What the file IS, read back from the file — never what the filtergraph asked
for:

1. **ffprobe facts vs the spec**: H.264 High, `yuv420p`, the planned frame,
   30 fps, an AAC track, the planned length (within one frame) inside the
   spec's duration window, and `+faststart` — the `moov` box before `mdat`,
   read from the file's own top-level boxes.
2. **Brand inside 5 s**: frames sampled at 1 fps over [0, 5 s], each through
   Stage 03's own logo metrics (`imaging.precheck.measure`) where the logo was
   placed, against the pinned ruleset's registered logos re-registered at the
   size the video shows them (`shown_templates` says why). A frame shows the
   brand when a registered logo scores at or above its pinned `min_score`;
   `brand_first_at_ms` is the first such sample, and it must be ≤
   `brand_within_ms`.
3. **Captions readable**: the frame at each caption's midpoint, OCR'd over the
   band the captions are burned in (two fixed readings, the better one kept),
   compared with the caption text as words; the best-aligned run of read words
   must reach `caption_ocr_min_similarity`. Extra text in the band (a label, a stray
   word read off the footage) does not count against it; missing or garbled
   caption words do.

No storage, no database: a file in, a `Verification` out.
"""

from __future__ import annotations

import io
import struct
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from agent.imaging.precheck import LogoTemplateData, measure, template_from_bytes
from agent.postprod.image import LogoArt, fit_by_padding
from agent.postprod.probe import ProbeError, VideoFacts, probe_video
from agent.postprod.video import frame_at
from agent.schemas.creative_video import ScriptCaption, words

#: §9.4 video 4: "sample frames at 1 fps over [0, 5 s]".
SAMPLE_WINDOW_S = 5
#: Tesseract, as Stage 03 runs it for images — a block of text, English.
OCR_PSM = 6
OCR_LANG = "eng"
#: What is kept of the text read off a frame: footage can read as pages of noise.
MAX_READ_CHARS = 400


@dataclass(frozen=True, slots=True)
class Expected:
    width: int
    height: int
    fps: int
    duration_ms: int
    min_duration_s: int | None
    max_duration_s: int | None
    #: The spec's file-size cap, when it gives one.
    max_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class LogoFrame:
    t_ms: int
    detected: bool
    #: The best registered logo's match score in this frame (0 when none).
    score: float
    asset_id: str | None
    status: str

    def record(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CaptionFrame:
    index: int
    t_ms: int
    expected: str
    read: str
    similarity: float
    passed: bool

    def record(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Verification:
    facts: VideoFacts | None
    boxes: list[str]
    faststart: bool
    logo_frames: list[LogoFrame] = field(default_factory=list)
    caption_frames: list[CaptionFrame] = field(default_factory=list)
    brand_first_at_ms: int | None = None
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures

    @property
    def caption_ocr_min_similarity(self) -> float | None:
        """The lowest caption's similarity — the number the threshold judges."""
        if not self.caption_frames:
            return None
        return min(frame.similarity for frame in self.caption_frames)

    def record(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "failures": list(self.failures),
            "faststart": self.faststart,
            "boxes": list(self.boxes),
            "brand_first_at_ms": self.brand_first_at_ms,
            "caption_ocr_min_similarity": self.caption_ocr_min_similarity,
            "logo_frames": [frame.record() for frame in self.logo_frames],
            "caption_frames": [frame.record() for frame in self.caption_frames],
        }


# ---------------------------------------------------------------------------
# the container
# ---------------------------------------------------------------------------


def top_level_boxes(content: bytes) -> list[str]:
    """The ISO-BMFF top-level box types in file order (`ftyp`, `moov`, …)."""
    boxes: list[str] = []
    offset = 0
    while offset + 8 <= len(content):
        size, kind = struct.unpack(">I4s", content[offset : offset + 8])
        header = 8
        if size == 1:
            if offset + 16 > len(content):
                break
            (size,) = struct.unpack(">Q", content[offset + 8 : offset + 16])
            header = 16
        elif size == 0:  # the box runs to the end of the file
            size = len(content) - offset
        if size < header:
            break
        boxes.append(kind.decode("latin-1"))
        offset += size
    return boxes


def is_faststart(boxes: Sequence[str]) -> bool:
    """`+faststart`: the index (`moov`) is before the media (`mdat`), so a
    player can start before it has the whole file."""
    return "moov" in boxes and "mdat" in boxes and boxes.index("moov") < boxes.index("mdat")


# ---------------------------------------------------------------------------
# captions
# ---------------------------------------------------------------------------


def similarity(expected: str, read: str) -> float:
    """How well `read` contains `expected`, as words (case, punctuation and
    spacing are not what a viewer reads): the best SequenceMatcher ratio of the
    expected words against any run of read words of about the same length."""
    want = words(expected).split()
    got = words(read).split()
    if not want:
        return 1.0
    if not got:
        return 0.0
    target = " ".join(want)
    best = 0.0
    for length in range(max(1, len(want) - 2), len(want) + 3):
        for start in range(max(1, len(got) - length + 1)):
            window = " ".join(got[start : start + length])
            best = max(best, SequenceMatcher(None, target, window, autojunk=False).ratio())
    return best


def read_text(frame: Image.Image, box: tuple[int, int, int, int] | None = None) -> str:
    """Tesseract over `box` of the frame (all of it by default), inverted —
    burned text is white on a dark box, and tesseract reads dark on light."""
    grey = frame.convert("L")
    return _tesseract(ImageOps.invert(grey.crop(box) if box is not None else grey))


#: Burned captions are pure white; the 60% box keeps what is behind them at
#: ≤ 40% of its brightness. Binarising here drops smooth footage from the read.
CAPTION_WHITE_MIN = 225


def caption_readings(frame: Image.Image, band: tuple[int, int] | None = None) -> list[str]:
    """The caption band (`band` = rows `y0..y1` where libass draws captions;
    the bottom half when unknown), read two fixed ways: inverted grey, and
    binarised on the captions' white. Busy footage beside the box defeats one
    or the other; the caption's own words survive in at least one."""
    width, height = frame.size
    y0, y1 = band if band is not None else (height // 2, height)
    grey = frame.convert("L").crop((0, y0, width, y1))
    return [
        _tesseract(ImageOps.invert(grey)),
        _tesseract(grey.point(lambda v: 0 if v >= CAPTION_WHITE_MIN else 255)),
    ]


def _tesseract(image: Image.Image) -> str:
    import pytesseract

    text: str = pytesseract.image_to_string(image, lang=OCR_LANG, config=f"--psm {OCR_PSM}")
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# the logo
# ---------------------------------------------------------------------------


#: ORB needs the mark this wide to find enough keypoints; a smaller shown
#: logo is measured with its template and its crop upscaled by one factor.
MEASURE_MIN_LOGO_PX = 256


@dataclass(frozen=True, slots=True)
class LogoWindow:
    """Where post-production placed the logo (the logo's own box and its
    clear space, frame px)."""

    box: tuple[int, int, int, int]
    clear_space_px: int

    @property
    def size(self) -> tuple[int, int]:
        x0, y0, x1, y1 = self.box
        return x1 - x0, y1 - y0

    @property
    def factor(self) -> float:
        return max(1.0, MEASURE_MIN_LOGO_PX / self.size[0])

    def crop(self, frame: Image.Image) -> Image.Image:
        x0, y0, x1, y1 = self.box
        pad = self.clear_space_px
        width, height = frame.size
        return frame.crop(
            (max(0, x0 - pad), max(0, y0 - pad), min(width, x1 + pad), min(height, y1 + pad))
        )


def shown_templates(
    logos: Sequence[LogoArt], pinned: Sequence[LogoTemplateData], window: LogoWindow
) -> tuple[LogoTemplateData, ...]:
    """Each registered logo re-registered at the size the video shows it.

    The pinned templates are Stage 03's, built at `ocr_working_width_px`
    (1280): ORB matches within about one octave of scale, so a 256 px mark
    against a 1280 px template scores as noise whether or not it is there
    (measured: ~0.15 either way, against a 0.62 floor). The SAME registered
    asset, fitted to the logo box exactly as it was composited, goes through
    Stage 03's own `template_from_bytes` — and keeps its pinned `min_score`.
    A logo the pinned ruleset does not register is never a brand match."""
    floors = {template.asset_id: template for template in pinned}
    width, height = window.size
    working = round(width * window.factor)
    out: list[LogoTemplateData] = []
    for logo in logos:
        registered = floors.get(logo.asset_id)
        if registered is None:
            continue
        buffer = io.BytesIO()
        fit_by_padding(logo.image, width, height).save(buffer, format="PNG")
        out.append(
            template_from_bytes(
                buffer.getvalue(),
                asset_id=logo.asset_id,
                label=registered.label,
                min_score=registered.min_score,
                working_width=working,
            )
        )
    return tuple(out)


def logo_frame(
    frame: Image.Image, templates: Sequence[LogoTemplateData], t_ms: int, window: LogoWindow
) -> LogoFrame:
    """Stage 03's logo metrics (`imaging.precheck.measure`) on the logo's box
    in one frame, at the templates' scale: detected when a registered logo
    scores at or above its template's `min_score`."""
    crop = window.crop(frame)
    buffer = io.BytesIO()
    crop.save(buffer, format="PNG")
    measured = measure(
        buffer.getvalue(),
        templates=tuple(templates),
        working_width=max(1, round(crop.width * window.factor)),
    )
    floors = {template.asset_id: template.min_score for template in templates}
    passing = [m for m in measured.logo_matches if m.score >= floors.get(m.asset_id, 1.0)]
    best = max(measured.logo_matches, key=lambda m: m.score, default=None)
    chosen = max(passing, key=lambda m: m.score, default=best)
    return LogoFrame(
        t_ms=t_ms,
        detected=bool(passing),
        score=float(chosen.score) if chosen is not None else 0.0,
        asset_id=str(chosen.asset_id) if chosen is not None else None,
        status=measured.status,
    )


# ---------------------------------------------------------------------------
# the verification
# ---------------------------------------------------------------------------

FrameLoader = Callable[[Path, float], Image.Image]


def verify(
    path: Path,
    *,
    expected: Expected,
    captions: Sequence[ScriptCaption],
    templates: Sequence[LogoTemplateData],
    logos: Sequence[LogoArt],
    logo: LogoWindow | None,
    brand_within_ms: int,
    min_similarity: float,
    caption_band: tuple[int, int] | None = None,
    frame_loader: FrameLoader = frame_at,
) -> Verification:
    content = path.read_bytes()
    boxes = top_level_boxes(content)
    result = Verification(facts=None, boxes=boxes, faststart=is_faststart(boxes))
    fail = result.failures.append
    try:
        facts = probe_video(content)
    except ProbeError as exc:
        fail(f"not a whole video: {exc}")
        return result
    result.facts = facts
    _facts_vs_spec(facts, expected, result.faststart, fail)

    frame_ms = 1000 / expected.fps
    last_s = max(0.0, (facts.duration_ms - frame_ms) / 1000)
    if not templates:
        fail("the pinned ruleset registers no logo, so the brand cannot be seen in 0–5 s")
    elif logo is None:
        fail("no logo was placed on the footage, so the brand is not seen in 0–5 s")
    elif not (shown := shown_templates(logos, templates, logo)):
        fail("the placed logo is not one the pinned ruleset registers")
    else:
        for second in range(SAMPLE_WINDOW_S + 1):
            if second > last_s:
                break
            frame = frame_loader(path, float(second))
            result.logo_frames.append(logo_frame(frame, shown, second * 1000, logo))
        seen = [frame.t_ms for frame in result.logo_frames if frame.detected]
        result.brand_first_at_ms = seen[0] if seen else None
        if result.brand_first_at_ms is None:
            fail(f"no registered logo is recognised in any 1 fps sample over 0–{SAMPLE_WINDOW_S} s")
        elif result.brand_first_at_ms > brand_within_ms:
            fail(
                f"the brand is first seen at {result.brand_first_at_ms} ms, after "
                f"{brand_within_ms} ms"
            )

    for index, caption in enumerate(captions):
        middle = min((caption.t0 + caption.t1) / 2, last_s)
        readings = caption_readings(frame_loader(path, middle), caption_band)
        read = max(readings, key=lambda text: similarity(caption.text, text))
        score = round(similarity(caption.text, read), 4)
        result.caption_frames.append(
            CaptionFrame(
                index=index,
                t_ms=round(middle * 1000),
                expected=caption.text,
                read=read[:MAX_READ_CHARS],
                similarity=score,
                passed=score >= min_similarity,
            )
        )
        if score < min_similarity:
            fail(
                f"caption {index + 1} reads {read!r} at {middle:.2f} s — similarity {score:.2f} "
                f"to {caption.text!r}, under {min_similarity:.2f}"
            )
    return result


def _facts_vs_spec(
    facts: VideoFacts, expected: Expected, faststart: bool, fail: Callable[[str], None]
) -> None:
    if facts.codec != "h264" or (facts.profile or "").lower() != "high":
        fail(f"video is {facts.codec} {facts.profile}, not H.264 High")
    if facts.pix_fmt != "yuv420p":
        fail(f"pixel format is {facts.pix_fmt}, not yuv420p")
    if (facts.width, facts.height) != (expected.width, expected.height):
        fail(f"frame is {facts.width}x{facts.height}, not {expected.width}x{expected.height}")
    if facts.fps is None or abs(facts.fps - expected.fps) > 1e-6:
        fail(f"frame rate is {facts.fps}, not {expected.fps} fps")
    if not facts.has_audio or facts.audio_codec != "aac":
        fail(f"audio is {facts.audio_codec or 'absent'}, not an AAC track")
    frame_ms = 1000 / expected.fps
    if abs(facts.duration_ms - expected.duration_ms) > frame_ms:
        fail(f"length is {facts.duration_ms} ms, not the planned {expected.duration_ms} ms")
    if expected.min_duration_s is not None and facts.duration_ms < expected.min_duration_s * 1000:
        fail(f"length {facts.duration_ms} ms is under the spec's {expected.min_duration_s} s")
    if expected.max_duration_s is not None and facts.duration_ms > expected.max_duration_s * 1000:
        fail(f"length {facts.duration_ms} ms is over the spec's {expected.max_duration_s} s")
    if expected.max_bytes is not None and facts.bytes > expected.max_bytes:
        fail(f"file is {facts.bytes} bytes, over the spec's {expected.max_bytes}")
    if not faststart:
        fail("moov is not before mdat: the file was not written with +faststart")
