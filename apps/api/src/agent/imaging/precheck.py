"""Measure one image. No thresholds, no verdicts, no opinions.

PRD §9.5's five steps, in order: normalise to RGB and downscale to a fixed
working width so the metric does not depend on what camera took the picture;
OCR at a fixed PSM and language set from constants; union the text bounding
boxes and divide by the image area; match every registered logo template by ORB
features and by perceptual hash; and hand the numbers back for somebody else to
judge.

**Why this file may be slow and `matchers/image.py` may not.** This runs once
per image, in `worker`, at concurrency 1, with a 3 s p95 budget (§17 CF5). The
rule that reads its output runs on every lint of every target forever. Putting
the native binary on the second path would make a blocking verdict depend on
whether a particular container happened to have `tesseract` — which is the
failure §18 turns into `indeterminate` rather than a silent pass.

**Determinism, measured rather than assumed.** Tesseract publishes no
determinism guarantee, so it was tested: the same image through five calls in
one process and four subprocesses with different `PYTHONHASHSEED` values gave
byte-identical coverage, text and boxes. OpenCV's ORB was identical with
threading on and off, but its intermediates are floats and float reductions are
not guaranteed identical across architectures, so two guards are in place —
`cv2.setNumThreads(0)`, and rounding every metric to `METRIC_DP` before it can
reach a threshold. The architectural guard is stronger than either: the numbers
are persisted in a `derived` Evidence row and the verdict is evaluated over the
*stored* number, so re-linting never re-measures.
"""

from __future__ import annotations

import base64
import hashlib
import io
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

import structlog

from agent.schemas.imaging import METRIC_DP, ImageMeasurement, LogoMatch

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass

log = structlog.get_logger(__name__)

__all__ = [
    "DEFAULT_PSM",
    "LogoTemplateData",
    "OcrUnavailable",
    "detector_version",
    "measure",
    "phash",
    "template_from_bytes",
]

#: Sparse text, no assumed page order. An ad is a poster, not a document: the
#: default PSM 3 assumes columns and paragraphs and finds far less on creative.
#: Fixed here rather than tuned per image — a segmentation mode chosen per
#: input is a verdict that depends on a heuristic nobody reviewed.
DEFAULT_PSM = 11

#: Keypoints kept per logo template. Bounded so a `RuleSet` carrying a dozen
#: logos stays a reasonable size, and fixed so two compiles of the same asset
#: produce the same descriptors.
TEMPLATE_KEYPOINTS = 300

#: Keypoints detected in the image being checked. Larger than the template
#: budget because the logo may be a small part of a busy picture.
IMAGE_KEYPOINTS = 2000

#: Hamming distance under which two ORB descriptors are "the same feature".
#: 64 of 256 bits. Above this the match is noise at any score.
ORB_MAX_DISTANCE = 64

#: Perceptual-hash distance under which two images are near-duplicates. Only
#: consulted when ORB finds nothing — a logo that fills the frame has few
#: corners to match and would otherwise score zero.
PHASH_NEAR_DUPLICATE = 10

PRECHECK_VERSION = "1"


class OcrUnavailable(RuntimeError):
    """`tesseract` is not installed, or refused to run.

    Raised rather than returned so no caller can mistake it for a measurement.
    `measure` catches it and returns `status='detector_unavailable'`, which
    §18 requires to read as `indeterminate` and never as `pass`.
    """


@dataclass(frozen=True, slots=True)
class LogoTemplateData:
    """One registered logo, prepared for matching.

    `descriptors` is the ORB descriptor matrix as raw bytes, 32 per keypoint,
    carried in the `RuleSet` rather than left on the Volume: a `RuleSet` that
    referenced a file path would stop being self-determining, and Stage 04 is
    handed a ruleset and nothing else.
    """

    asset_id: UUID
    label: str
    phash: str
    descriptors_b64: str
    keypoint_count: int
    min_score: float

    def descriptors(self) -> Any:
        import numpy as np

        raw = base64.b64decode(self.descriptors_b64)
        if not raw or self.keypoint_count <= 0:
            return None
        return np.frombuffer(raw, dtype=np.uint8).reshape(self.keypoint_count, -1)


def _cv2() -> Any:
    """OpenCV, with threading pinned off before anything uses it.

    Thread count changes reduction order, and a reduction order change is how a
    score moves in the last decimal place. Measured identical either way on the
    fixtures here; pinned anyway because the cost is zero and the failure would
    be a blocking verdict that flickers.
    """
    import cv2

    cv2.setNumThreads(0)
    return cv2


def detector_version() -> str:
    """`tesseract/4.1.1+opencv/5.0.0+precheck/1`, or what is actually installed.

    Stamped into every measurement. A metric whose engine version is unknown
    cannot be compared with one taken a year later, and §7.3's whole purpose is
    that an image verdict stays inspectable.
    """
    try:
        import pytesseract

        tess = str(pytesseract.get_tesseract_version()).split()[0]
    except Exception:  # noqa: BLE001 - an unreadable version is not a failed check
        tess = "unavailable"
    try:
        import cv2

        opencv = cv2.__version__
    except Exception:  # noqa: BLE001 - same
        opencv = "unavailable"
    return f"tesseract/{tess}+opencv/{opencv}+precheck/{PRECHECK_VERSION}"


DETECTOR_VERSION = detector_version


def _load(content: bytes, *, working_width: int) -> tuple[Any, Any, int, int, str]:
    """Decode, note the submitted size, and downscale to the working width.

    Returns the working RGB image, its greyscale array, the *submitted*
    dimensions and the media type. The submitted dimensions are kept because an
    asset spec's `min_px` is about the file somebody will upload, while the
    coverage ratio is about a normalised view of it.
    """
    from PIL import Image

    with Image.open(io.BytesIO(content)) as opened:
        media_type = f"image/{(opened.format or 'unknown').lower()}"
        image = opened.convert("RGB")
        submitted_w, submitted_h = image.size
        if submitted_w != working_width:
            height = max(1, round(submitted_h * working_width / submitted_w))
            image = image.resize((working_width, height), Image.Resampling.LANCZOS)
        return image, image.convert("L"), submitted_w, submitted_h, media_type


def _ocr(image: Any, *, lang: str, psm: int) -> tuple[str, int, float]:
    """Recognised text, word count, and the share of the image it covers.

    Coverage is the area of the *union* of the word boxes, not their sum: two
    overlapping boxes describe one patch of text, and summing them would report
    more text than the image can physically hold.
    """
    import numpy as np
    import pytesseract
    from pytesseract import TesseractNotFoundError

    width, height = image.size
    try:
        data = pytesseract.image_to_data(
            image, lang=lang, config=f"--psm {psm}", output_type=pytesseract.Output.DICT
        )
    except TesseractNotFoundError as exc:
        raise OcrUnavailable(f"tesseract is not installed in this image: {exc}") from exc
    except pytesseract.TesseractError as exc:
        raise OcrUnavailable(f"tesseract refused to run: {exc}") from exc

    mask = np.zeros((height, width), dtype=bool)
    words: list[str] = []
    for index, text in enumerate(data["text"]):
        if not text.strip():
            continue
        # -1 is tesseract's "this is a block, not a word". Counting it would
        # cover the whole page with a single box on any image it emits one for.
        if float(data["conf"][index]) < 0:
            continue
        left = max(0, int(data["left"][index]))
        top = max(0, int(data["top"][index]))
        box_w = max(0, int(data["width"][index]))
        box_h = max(0, int(data["height"][index]))
        if box_w == 0 or box_h == 0:
            continue
        mask[top : top + box_h, left : left + box_w] = True
        words.append(text.strip())

    covered = float(mask.sum()) / float(width * height)
    return " ".join(words), len(words), covered


def phash(image: Any, *, size: int = 32, keep: int = 8) -> str:
    """64-bit perceptual hash, as a hex string. DCT-II by matrix multiply.

    `ImageHash` would do this and pulls `scipy` and `PyWavelets` behind it for
    one transform that is two matrix products against a constant basis. The
    basis is built from `numpy.cos` on integers, which is bit-identical
    wherever IEEE-754 doubles are.
    """
    import numpy as np
    from PIL import Image

    reduced = image.convert("L").resize((size, size), Image.Resampling.LANCZOS)
    values = np.asarray(reduced, dtype=np.float64)
    k = np.arange(size)
    basis = np.cos(np.pi * (2 * k[None, :] + 1) * k[:, None] / (2 * size))
    basis[0] /= np.sqrt(2)
    transformed = basis @ values @ basis.T
    low = transformed[:keep, :keep].flatten()
    # The DC term is the average brightness and swamps the median, so it is
    # excluded from the threshold but still hashed against it.
    median = float(np.median(low[1:]))
    bits = "".join("1" if value > median else "0" for value in low)
    return f"{int(bits, 2):016x}"


def _phash_distance(left: str, right: str) -> int:
    try:
        return bin(int(left, 16) ^ int(right, 16)).count("1")
    except ValueError:
        return 64


def template_from_bytes(
    content: bytes, *, asset_id: UUID, label: str, min_score: float, working_width: int
) -> LogoTemplateData:
    """Prepare one logo asset for matching. Called by node 3.4.3, never at lint time."""
    cv2 = _cv2()
    import numpy as np

    image, grey, _, _, _ = _load(content, working_width=working_width)
    array = np.asarray(grey, dtype=np.uint8)
    orb = cv2.ORB_create(nfeatures=TEMPLATE_KEYPOINTS)
    keypoints, descriptors = orb.detectAndCompute(array, None)
    if descriptors is None or len(keypoints) == 0:
        # A flat wordmark can genuinely have no corners. Recorded with zero
        # keypoints rather than dropped: the perceptual hash still matches it,
        # and a template that silently vanished would read as "no logo rule"
        # rather than "this logo can only be matched whole".
        return LogoTemplateData(
            asset_id=asset_id,
            label=label,
            phash=phash(image),
            descriptors_b64="",
            keypoint_count=0,
            min_score=min_score,
        )
    return LogoTemplateData(
        asset_id=asset_id,
        label=label,
        phash=phash(image),
        descriptors_b64=base64.b64encode(descriptors.tobytes()).decode("ascii"),
        keypoint_count=int(descriptors.shape[0]),
        min_score=min_score,
    )


def _match_logos(
    grey: Any, *, templates: tuple[LogoTemplateData, ...], image_phash: str, area: int
) -> tuple[tuple[LogoMatch, ...], float | None, float | None]:
    """Best score per template, plus the area the best match occupies."""
    if not templates:
        return (), None, None

    cv2 = _cv2()
    import numpy as np

    array = np.asarray(grey, dtype=np.uint8)
    orb = cv2.ORB_create(nfeatures=IMAGE_KEYPOINTS)
    image_kp, image_desc = orb.detectAndCompute(array, None)
    matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)

    found: list[LogoMatch] = []
    for template in templates:
        distance = _phash_distance(image_phash, template.phash)
        descriptors = template.descriptors()
        score = 0.0
        bbox: tuple[int, int, int, int] | None = None
        method: str = "orb"

        if descriptors is not None and image_desc is not None and len(image_desc):
            raw = matcher.match(descriptors, image_desc)
            # Sorted on the tuple, not on distance alone: two matches at the
            # same distance would otherwise come back in whatever order the
            # matcher happened to build them, and the bbox would follow suit.
            ordered = sorted(raw, key=lambda item: (item.distance, item.queryIdx, item.trainIdx))
            good = [item for item in ordered if item.distance <= ORB_MAX_DISTANCE]
            if good:
                score = len(good) / float(template.keypoint_count)
                xs = [image_kp[item.trainIdx].pt[0] for item in good]
                ys = [image_kp[item.trainIdx].pt[1] for item in good]
                left, top = int(min(xs)), int(min(ys))
                bbox = (left, top, max(1, int(max(xs)) - left), max(1, int(max(ys)) - top))

        if score == 0.0 and distance <= PHASH_NEAR_DUPLICATE:
            # The whole image *is* the logo. ORB has nothing to localise, so
            # the hash carries the match and the bbox is honestly absent.
            score = 1.0 - (distance / 64.0)
            method = "phash"

        if score <= 0.0:
            continue
        found.append(
            LogoMatch(
                asset_id=template.asset_id,
                label=template.label,
                score=round(min(1.0, score), METRIC_DP),
                bbox=bbox,
                phash_distance=distance,
                method="phash" if method == "phash" else "orb",
            )
        )

    # Deterministic order: strongest first, then by asset id so two equal
    # scores never swap places between runs.
    found.sort(key=lambda item: (-item.score, str(item.asset_id)))
    best = found[0] if found else None
    best_score = best.score if best is not None else 0.0
    if best is not None and best.bbox is not None and area > 0:
        _, _, box_w, box_h = best.bbox
        area_ratio = round(min(1.0, (box_w * box_h) / float(area)), METRIC_DP)
    else:
        area_ratio = 0.0
    return tuple(found), best_score, area_ratio


def measure(
    content: bytes,
    *,
    templates: tuple[LogoTemplateData, ...] = (),
    working_width: int = 1280,
    lang: str = "eng",
    psm: int = DEFAULT_PSM,
) -> ImageMeasurement:
    """PRD §9.5 steps 1–4 for one image. Never raises for a missing detector."""
    started = time.perf_counter()
    image_hash = hashlib.sha256(content).hexdigest()
    version = detector_version()

    try:
        image, grey, submitted_w, submitted_h, media_type = _load(
            content, working_width=working_width
        )
    except Exception as exc:  # noqa: BLE001 - an undecodable upload is a 422, not a 500
        raise ValueError(f"this file could not be read as an image: {exc}") from exc

    working_w, working_h = image.size
    common: dict[str, Any] = {
        "image_hash": image_hash,
        "width_px": submitted_w,
        "height_px": submitted_h,
        "byte_size": len(content),
        "media_type": media_type,
        "detector_version": version,
        "working_width_px": working_w,
    }

    try:
        text, word_count, coverage = _ocr(image, lang=lang, psm=psm)
    except OcrUnavailable as exc:
        # Law 31. Every metric stays `None`, so every image rule reports
        # `indeterminate` and none of them reports a pass.
        log.warning("imaging.detector_unavailable", image_hash=image_hash, error=str(exc))
        return ImageMeasurement(
            status="detector_unavailable",
            reason="detector_unavailable",
            measured_ms=int((time.perf_counter() - started) * 1000),
            **common,
        )

    matches, best_score, area_ratio = _match_logos(
        grey, templates=templates, image_phash=phash(image), area=working_w * working_h
    )
    return ImageMeasurement(
        status="measured",
        ocr_text=text,
        ocr_word_count=word_count,
        text_coverage_ratio=round(coverage, METRIC_DP),
        logo_match_score=best_score,
        logo_area_ratio=area_ratio,
        # `None` when there was nothing to look for: "no logo was registered"
        # and "we looked and found none" are different facts, and only the
        # second one should ever fail a `logo_present` rule.
        logo_present=None if not templates else (1.0 if matches else 0.0),
        logo_matches=matches,
        measured_ms=int((time.perf_counter() - started) * 1000),
        **common,
    )
