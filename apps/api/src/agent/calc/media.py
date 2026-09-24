"""What a creative run's media will cost, and which ratios a model can make
(PRD §9.2–9.3, §23.1 item 8). Two formulas:

* `media.cost_estimate_v1` — counts the jobs a scope implies from its ratio
  plan and prices each from the pinned catalogue record: image `pricing[]`
  lines (`unit ∈ image | megapixel | token`, resolution `variant` tiers; a
  token price goes through `image_tokens_per_megapixel`, confidence `low`) and
  video per-second SKUs × planned duration. It also walks the degrade ladder
  to the smallest reduction that fits both caps, which is CR-E9's answer.
* `media.ratio_plan_v1` — per required ratio: native | relaid | crop | gap,
  and for a crop, which supported ratio it comes from and how much it keeps.
* `media.crop_window_v1` — on a real frame, the crop window of a ratio that
  keeps the most OpenCV spectral-residual saliency, and whether what it keeps
  clears `crop_min_saliency_retained` (§9.4 item 1): below it the rendition is
  a recorded gap, never a bad crop (Law 39). It re-decides on pixels what the
  ratio plan could only bound by frame area.

Pure, like every formula here: records, numbers and bytes in, a `CalcDraft` out.

Four readings the catalogue does not settle, each labelled where it is used:

* A resolution tier is priced at its area: `1K` = 1024², `2K` = 2048², …
  ("concrete pixel dimensions are derived per-provider"), and an image with no
  tier at `1K`. Confidence `medium`. The one recorded megapixel image,
  flux.2-klein-4b at 16:9 with no tier, came back 1824×1024 and billed $0.015
  against the $0.0147 this prices it at.
* An image with no line for its tier is priced at its dearest output line —
  an upper bound — confidence `low`.
* A video with no planned duration is priced at its longest supported one —
  again the upper bound — confidence `low`.
* Text is PRD §17 CC2's declared ceiling ("Text ≤ $6 at default routing"),
  confidence `low`, until creative runs exist to measure.

A model that cannot be priced this way (a video billed in `video_tokens`, an
image with no output line) is a `CalcError`: a budget cannot be reserved for a
number nobody can compute (Law 43).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

import cv2
import numpy as np

from agent.calc.registry import CalcDraft, CalcError, formula
from agent.media.capability import (
    best_crop_retention,
    parse_ratio,
    ratio_coverage,
    supported_ratios,
)
from agent.media.constants import MediaConstants
from agent.media.types import CapabilityRecord, PriceLine

Confidence = Literal["high", "medium", "low"]
_RANK: dict[str, int] = {"high": 2, "medium": 1, "low": 0}

#: PRD §17 CC2: "Text ≤ $6 at default routing". The declared ceiling, used as
#: the text estimate until creative runs exist to measure (confidence `low`).
TEXT_ESTIMATE_USD = Decimal("6.00")
#: A resolution tier's long edge, squared for its area.
TIER_EDGE_PX: dict[str, int] = {"512": 512, "1k": 1024, "2k": 2048, "4k": 4096}
DEFAULT_TIER = "1k"
MEGAPIXEL = Decimal(1_000_000)
MONEY_PLACES = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class JobPrice:
    usd: Decimal
    confidence: Confidence
    #: The price line this came from, for the evidence row.
    basis: str


# ---------------------------------------------------------------------------
# one job
# ---------------------------------------------------------------------------


def image_job_price(
    capability: CapabilityRecord, params: dict[str, Any], constants: MediaConstants
) -> JobPrice:
    """One image request: `n` images at `params`' resolution and quality."""
    outputs = [line for line in capability.pricing if line.billable == "output_image"]
    if not outputs:
        raise CalcError(f"{capability.model_id} has no output_image price to estimate from")
    count = Decimal(int(params.get("n") or 1))
    line, matched = _image_line(outputs, params)
    confidence: Confidence = "high" if matched else "low"

    if line.unit == "image":
        usd = line.usd * count
    elif line.unit in ("megapixel", "token"):
        megapixels = _megapixels(params)
        if line.unit == "megapixel":
            usd = line.usd * megapixels * count
            confidence = _lowest(confidence, "medium")
        else:
            tokens = Decimal(constants.image_tokens_per_megapixel) * megapixels
            usd = line.usd * tokens * count
            confidence = "low"
    else:
        raise CalcError(
            f"{capability.model_id} prices its output per {line.unit!r}, which this estimate "
            "cannot compute"
        )
    return JobPrice(usd=usd, confidence=confidence, basis=_basis(line))


def video_job_price(
    capability: CapabilityRecord, params: dict[str, Any], constants: MediaConstants
) -> JobPrice:
    """One video request: a per-second SKU × duration, the minimum charge as a
    floor, plus any per-frame-image input price."""
    del constants  # no constant enters the video price; kept for a uniform signature
    caps = capability.video
    duration = params.get("duration")
    confidence: Confidence = "high"
    if duration is None:
        durations = caps.durations if caps else []
        if not durations:
            raise CalcError(f"{capability.model_id} publishes no durations to plan a video with")
        duration = max(durations)
        confidence = "low"

    resolution = params.get("resolution")
    audio = params.get("generate_audio")
    if audio is None:
        # OpenRouter: "defaults to the endpoint's generate_audio capability flag".
        audio = "generate_audio" in capability.params
    frames = int(params.get("frame_images") or 0)
    mode = "image_to_video" if frames else "text_to_video"

    per_second = [
        line
        for line in capability.pricing
        if line.billable == "output_video" and line.unit == "second"
    ]
    compatible = [
        line
        for line in per_second
        if (line.variant is None or (resolution is not None and line.variant == resolution.lower()))
        and (line.audio is None or line.audio == audio)
        and (line.mode is None or line.mode == mode)
    ]
    if not compatible:
        raise CalcError(
            f"{capability.model_id} has no per-second price for "
            f"{resolution or 'its default resolution'}"
            f"{' with' if audio else ' without'} audio — it cannot be estimated from its "
            f"pricing_skus ({', '.join(line.sku or line.unit for line in capability.pricing)})"
        )
    line = max(compatible, key=lambda item: (_specificity(item), item.usd))
    if not _pins_everything(line, per_second, resolution):
        confidence = _lowest(confidence, "medium")

    usd = line.usd * Decimal(int(duration))
    minimum = next(
        (item.usd for item in capability.pricing if item.billable == "minimum"), Decimal(0)
    )
    usd = max(usd, minimum)
    per_frame = next(
        (
            item.usd
            for item in capability.pricing
            if item.billable == "input_image" and item.unit == "image"
        ),
        Decimal(0),
    )
    usd += per_frame * frames
    return JobPrice(usd=usd, confidence=confidence, basis=_basis(line))


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------


@formula("media.cost_estimate_v1", kind="calc_media_cost")
def cost_estimate_v1(
    *,
    campaigns: list[dict[str, Any]],
    scope: dict[str, Any],
    image: dict[str, Any] | None,
    video: dict[str, Any] | None,
    text_usd: Decimal,
    caps: dict[str, Any],
    constants: MediaConstants,
) -> CalcDraft:
    """Pre-flight estimate of a creative run: text, images, video, and the
    smallest degrade-ladder reduction that fits both caps."""
    image_record = CapabilityRecord.model_validate(image["capability"]) if image else None
    video_record = CapabilityRecord.model_validate(video["capability"]) if video else None
    image_params = dict(image.get("params") or {}) if image else {}
    video_params = dict(video.get("params") or {}) if video else {}
    text = Decimal(str(text_usd))
    cap_creative = Decimal(str(caps["max_creative_cost_usd"]))
    cap_media = Decimal(str(caps["max_media_cost_usd"]))

    def priced(steps: tuple[str, ...]) -> dict[str, Any]:
        counts = _job_counts(campaigns, scope, image_record, video_record, constants, steps)
        image_usd = video_usd = Decimal(0)
        confidence: Confidence = "low"  # the text half is always an assumption
        bases: list[str] = []
        if counts["image"]:
            assert image_record is not None  # noqa: S101 — counted only with a record
            price = image_job_price(image_record, image_params, constants)
            image_usd = price.usd * counts["image"]
            confidence = _lowest(confidence, price.confidence)
            bases.append(price.basis)
        if counts["video"]:
            assert video_record is not None  # noqa: S101
            price = video_job_price(video_record, video_params, constants)
            video_usd = price.usd * counts["video"]
            confidence = _lowest(confidence, price.confidence)
            bases.append(price.basis)
        media = image_usd + video_usd
        return {
            "jobs": counts,
            "image_usd": image_usd,
            "video_usd": video_usd,
            "media_usd": media,
            "total_usd": text + media,
            "confidence": confidence,
            "bases": bases,
            "fits": media <= cap_media and text + media <= cap_creative,
        }

    full = priced(())
    reduction: dict[str, Any] | None = None
    if not full["fits"]:
        applied: list[str] = []
        attempt = full
        for step in constants.degrade_ladder:
            candidate = priced((*applied, step))
            if candidate["jobs"] != attempt["jobs"]:
                applied.append(step)
                attempt = candidate
            if attempt["fits"]:
                break
        reduction = {
            "steps": applied,
            "fits": attempt["fits"],
            "jobs": attempt["jobs"],
            "image_usd": _money(attempt["image_usd"]),
            "video_usd": _money(attempt["video_usd"]),
            "media_usd": _money(attempt["media_usd"]),
            "total_usd": _money(attempt["total_usd"]),
        }

    result = {
        "text_usd": _money(text),
        "image_usd": _money(full["image_usd"]),
        "video_usd": _money(full["video_usd"]),
        "media_usd": _money(full["media_usd"]),
        "total_usd": _money(full["total_usd"]),
        "confidence": full["confidence"],
        "jobs": full["jobs"],
        "priced_at": full["bases"],
        "caps": {
            "max_creative_cost_usd": _money(cap_creative),
            "max_media_cost_usd": _money(cap_media),
        },
        "fits": full["fits"],
        "reduction": reduction,
    }
    return CalcDraft(
        inputs={
            "campaigns": campaigns,
            "scope": scope,
            "image": image,
            "video": video,
            "text_usd": str(text),
            "caps": caps,
        },
        result=result,
        summary=(
            f"Estimated ${result['total_usd']:.2f}: text ${result['text_usd']:.2f}, images "
            f"${result['image_usd']:.2f} ({full['jobs']['image']} jobs), video "
            f"${result['video_usd']:.2f} ({full['jobs']['video']} jobs); confidence "
            f"{full['confidence']}."
        ),
        constants_version=constants.version,
    )


#: The degrade-ladder rungs a person can take in the Start dialog (PRD §15.4
#: B), as the `CreativeScope` change each one is. `candidates` and
#: `video_square` have no scope field — they are the run's own degradations,
#: walked when a reservation would breach a cap (§9.3) — so a request cannot
#: carry them and the dialog cannot offer them.
SCOPE_RUNGS: dict[str, dict[str, Any]] = {
    "third_concept": {"concepts_per_campaign": 2},
    "video": {"video": False},
}


def scope_reduction(
    *,
    campaigns: list[dict[str, Any]],
    scope: dict[str, Any],
    image: dict[str, Any] | None,
    video: dict[str, Any] | None,
    text_usd: Decimal,
    caps: dict[str, Any],
    constants: MediaConstants,
) -> dict[str, Any] | None:
    """The smallest change to the *scope* that fits both caps: the Start
    dialog's one-click reduction (PRD §15.4 B).

    `cost_estimate_v1`'s `reduction` walks the whole ladder, and its first
    rung — one candidate per concept — is not something a start request can
    say. This walks only `SCOPE_RUNGS`, in ladder order and cumulatively,
    re-pricing each step with `cost_estimate_v1` itself, and stops at the
    first that fits. `None` when the scope already fits; `fits: False` when
    no rung the scope can express is enough.
    """

    def price(candidate: dict[str, Any]) -> dict[str, Any]:
        return cost_estimate_v1(
            campaigns=campaigns,
            scope=candidate,
            image=image,
            video=video,
            text_usd=text_usd,
            caps=caps,
            constants=constants,
        ).result

    attempt = price(scope)
    if attempt["fits"]:
        return None
    current = dict(scope)
    steps: list[str] = []
    for rung in constants.degrade_ladder:
        change = SCOPE_RUNGS.get(rung)
        if change is None or all(current.get(key) == value for key, value in change.items()):
            continue
        current = {**current, **change}
        steps.append(rung)
        attempt = price(current)
        if attempt["fits"]:
            break
    return {
        "scope": current,
        "steps": steps,
        "fits": attempt["fits"],
        "jobs": attempt["jobs"],
        **{
            key: attempt[key]
            for key in ("text_usd", "image_usd", "video_usd", "media_usd", "total_usd")
        },
    }


@formula("media.ratio_plan_v1", kind="calc_ratio_plan")
def ratio_plan_v1(
    *,
    image_ratios: list[str],
    video_ratios: list[str],
    image: dict[str, Any] | None,
    video: dict[str, Any] | None,
    constants: MediaConstants,
) -> CalcDraft:
    """Per required ratio: native, relaid, crop (from which ratio, keeping how
    much of the frame) or gap — the consequence of a model choice, before spend."""
    result: dict[str, Any] = {}
    for modality, ratios, record in (
        ("image", image_ratios, image),
        ("video", video_ratios, video),
    ):
        if record is None:
            continue
        capability = CapabilityRecord.model_validate(record)
        plan = ratio_coverage(
            ratios,
            capability,
            tolerance=constants.ratio_tolerance,
            min_retained=constants.crop_min_saliency_retained,
        )
        labels = supported_ratios(capability)
        detail: dict[str, Any] = {}
        for ratio, coverage in plan.items():
            entry: dict[str, Any] = {"plan": coverage}
            if coverage in ("crop", "gap") and labels:
                wanted = parse_ratio(ratio)
                source = max(
                    labels, key=lambda label: best_crop_retention(wanted, [parse_ratio(label)])
                )
                entry["from"] = source
                entry["retained"] = round(best_crop_retention(wanted, [parse_ratio(source)]), 4)
            detail[ratio] = entry
        result[modality] = detail
    return CalcDraft(
        inputs={
            "image_ratios": image_ratios,
            "video_ratios": video_ratios,
            "image": image,
            "video": video,
        },
        result=result,
        summary="; ".join(
            f"{modality} {ratio} {entry['plan']}"
            for modality, entries in result.items()
            for ratio, entry in entries.items()
        )
        or "No media ratios required.",
        constants_version=constants.version,
    )


#: The frame spectral residual is computed on (Hou & Zhang, CVPR 2007: 64 px),
#: as OpenCV's `saliency.StaticSaliencySpectralResidual` computes it.
SALIENCY_WORKING_PX = 64

CropDecision = Literal["crop", "gap"]


@formula("media.crop_window_v1", kind="calc_crop_window")
def crop_window_v1(*, image: bytes, ratio: str, constants: MediaConstants) -> CalcDraft:
    """The crop window of `ratio` that keeps the most saliency in `image`, and
    whether what it keeps clears `crop_min_saliency_retained` (§9.4 item 1).

    The window is the largest one of that ratio the frame holds — any smaller
    window of the ratio sits inside one of these, so it cannot keep more — slid
    along the free axis to the position holding the most saliency mass. The
    image is recorded by sha256: the pixels decide, and the hash is what tells
    a re-run that they changed.
    """
    pixels = np.frombuffer(image, dtype=np.uint8)
    gray = cv2.imdecode(pixels, cv2.IMREAD_GRAYSCALE | cv2.IMREAD_IGNORE_ORIENTATION)
    if gray is None:
        raise CalcError("crop_window_v1: the image could not be decoded")
    height, width = (int(side) for side in gray.shape[:2])
    try:
        wanted = parse_ratio(ratio)
    except ValueError as exc:
        raise CalcError(f"crop_window_v1: {exc}") from exc
    if width / height >= wanted:
        window_w, window_h = height * wanted, float(height)
    else:
        window_w, window_h = float(width), width / wanted
    x0, y0, retained = best_window(spectral_residual(gray), window_w, window_h)
    minimum = constants.crop_min_saliency_retained
    decision = crop_decision(retained, minimum)
    return CalcDraft(
        inputs={
            "image_sha256": hashlib.sha256(image).hexdigest(),
            "width": width,
            "height": height,
            "ratio": ratio,
            "min_retained": minimum,
        },
        result={
            "box": [x0, y0, x0 + window_w, y0 + window_h],
            "retained": retained,
            "decision": decision,
            "saliency": "spectral_residual",
        },
        summary=f"{ratio} window: keeps {retained:.0%} of the saliency (floor "
        f"{minimum:.0%}) — {decision}",
        constants_version=constants.version,
    )


def crop_decision(retained: float, minimum: float) -> CropDecision:
    """Below the floor is a gap (§9.4: "if retained saliency < 0.85"); the floor crops."""
    return "crop" if retained >= minimum else "gap"


def spectral_residual(gray: np.ndarray) -> np.ndarray:
    """OpenCV spectral-residual saliency of a grayscale frame, at the frame's
    size, scaled so its peak is 1.

    The steps of OpenCV contrib's `StaticSaliencySpectralResidual`, in OpenCV's
    own primitives: the base `opencv-python-headless` wheel this repo ships
    has no `cv2.saliency` module. Resize to 64×64, DFT, subtract the 3×3 mean
    of the log amplitude from itself (the spectral residual), invert with the
    original phase, blur (5×5, σ 8), square, normalise, resize back.

    A frame with no structure at all has no residual to speak of — the method
    would turn its all-zero spectrum into a spike at the origin — so it has no
    saliency, and `best_window` then keeps saliency in proportion to area.
    """
    height, width = gray.shape[:2]
    if float(gray.std()) == 0.0:
        return np.zeros((height, width), dtype=np.float32)
    side = SALIENCY_WORKING_PX
    small = cv2.resize(gray, (side, side), interpolation=cv2.INTER_LINEAR_EXACT)
    planes = cv2.merge([small.astype(np.float32), np.zeros((side, side), dtype=np.float32)])
    real, imaginary = cv2.split(cv2.dft(planes))
    magnitude, angle = cv2.cartToPolar(real, imaginary)
    log_amplitude = np.log(magnitude + np.float32(1e-12))
    residual = log_amplitude - cv2.blur(log_amplitude, (3, 3))
    real, imaginary = cv2.polarToCart(np.exp(residual), angle)
    real, imaginary = cv2.split(cv2.idft(cv2.merge([real, imaginary])))
    magnitude, _ = cv2.cartToPolar(real, imaginary)
    magnitude = cv2.GaussianBlur(magnitude, (5, 5), 8)
    magnitude = magnitude * magnitude
    peak = float(magnitude.max())
    if peak > 0.0:
        magnitude = magnitude / peak
    return cv2.resize(magnitude, (width, height), interpolation=cv2.INTER_LINEAR_EXACT)


def best_window(
    saliency: np.ndarray, window_w: float, window_h: float
) -> tuple[float, float, float]:
    """`(x0, y0, retained)` for the `window_w × window_h` window holding the
    most saliency mass. Mass is summed over whole pixels (an integral image,
    every position at once); ties go to the position nearest the centre, then
    the top-left-most, so the answer never depends on scan order. With no
    saliency anywhere, a window keeps its share of the area.
    """
    height, width = saliency.shape[:2]
    w_px = min(width, max(1, round(window_w)))
    h_px = min(height, max(1, round(window_h)))
    integral = cv2.integral(saliency.astype(np.float64))
    mass = (
        integral[h_px:, w_px:]
        - integral[: height - h_px + 1, w_px:]
        - integral[h_px:, : width - w_px + 1]
        + integral[: height - h_px + 1, : width - w_px + 1]
    )
    total = float(integral[-1, -1])
    if total <= 0.0:
        area = (window_w * window_h) / (width * height)
        return (width - window_w) / 2, (height - window_h) / 2, min(1.0, area)
    best = float(mass.max())
    ys, xs = np.nonzero(mass >= best - 1e-9 * best)
    centre_x, centre_y = (width - w_px) / 2, (height - h_px) / 2
    y, x = min(
        zip(ys.tolist(), xs.tolist(), strict=True),
        key=lambda yx: ((yx[1] - centre_x) ** 2 + (yx[0] - centre_y) ** 2, yx[0], yx[1]),
    )
    x0 = min(float(x), max(0.0, width - window_w))
    y0 = min(float(y), max(0.0, height - window_h))
    return x0, y0, min(1.0, best / total)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _job_counts(
    campaigns: list[dict[str, Any]],
    scope: dict[str, Any],
    image: CapabilityRecord | None,
    video: CapabilityRecord | None,
    constants: MediaConstants,
    steps: tuple[str, ...],
) -> dict[str, int]:
    """How many generation jobs the scope implies, after `steps` of the
    degrade ladder. Per campaign and concept: `candidates_per_concept` masters
    at the first required ratio the model can make, plus one job for each
    other required ratio that is `native` or `relaid` (a crop costs nothing, a
    gap is not made). Per campaign: one video per makeable video ratio."""
    concepts = int(scope.get("concepts_per_campaign") or 2)
    if "third_concept" in steps:
        concepts = min(concepts, 2)
    candidates = 1 if "candidates" in steps else constants.candidates_per_concept
    images = videos = 0
    for campaign in campaigns:
        if scope.get("images") and image is not None:
            makeable = _makeable(campaign.get("image_ratios") or [], image, constants)
            if makeable:
                images += concepts * (candidates + len(makeable) - 1)
        if scope.get("video") and video is not None and "video" not in steps:
            ratios = [
                ratio
                for ratio in campaign.get("video_ratios") or []
                if not ("video_square" in steps and parse_ratio(ratio) == 1.0)
            ]
            videos += len(_makeable(ratios, video, constants))
    return {"image": images, "video": videos}


def _makeable(
    ratios: list[str], capability: CapabilityRecord, constants: MediaConstants
) -> list[str]:
    plan = ratio_coverage(
        ratios,
        capability,
        tolerance=constants.ratio_tolerance,
        min_retained=constants.crop_min_saliency_retained,
    )
    return [ratio for ratio in ratios if plan[ratio] in ("native", "relaid")]


def _image_line(outputs: list[PriceLine], params: dict[str, Any]) -> tuple[PriceLine, bool]:
    """The output line for this request's tier and quality, and whether it was
    an exact match (else the dearest line — an upper bound)."""
    by_variant = {(line.variant or "").lower(): line for line in outputs}
    tier = str(params.get("resolution") or "").lower()
    quality = str(params.get("quality") or "").lower()
    for wanted in (f"{quality}_{tier}", tier, quality):
        if wanted and wanted not in ("_",) and wanted in by_variant:
            return by_variant[wanted], True
    if "" in by_variant and not tier:
        return by_variant[""], True
    if "" in by_variant and not any(line.variant for line in outputs):
        return by_variant[""], True
    return max(outputs, key=lambda line: line.usd), False


def _megapixels(params: dict[str, Any]) -> Decimal:
    size = str(params.get("size") or "")
    width, sep, height = size.partition("x")
    if sep and width.isdigit() and height.isdigit():
        return Decimal(int(width) * int(height)) / MEGAPIXEL
    tier = str(params.get("resolution") or size or DEFAULT_TIER).lower()
    edge = TIER_EDGE_PX.get(tier)
    if edge is None:
        raise CalcError(f"resolution {tier!r} is not a tier this estimate can size")
    return Decimal(edge * edge) / MEGAPIXEL


def _specificity(line: PriceLine) -> int:
    return sum(value is not None for value in (line.variant, line.audio, line.mode))


def _pins_everything(line: PriceLine, family: list[PriceLine], resolution: str | None) -> bool:
    """Whether the chosen line names every attribute its SKU family varies on."""
    varies_audio = any(item.audio is not None for item in family)
    varies_mode = any(item.mode is not None for item in family)
    return (
        (resolution is None or line.variant is not None)
        and (not varies_audio or line.audio is not None)
        and (not varies_mode or line.mode is not None)
    )


def _lowest(a: Confidence, b: Confidence) -> Confidence:
    return a if _RANK[a] <= _RANK[b] else b


def _basis(line: PriceLine) -> str:
    return line.sku or f"{line.billable}/{line.unit}" + (f"/{line.variant}" if line.variant else "")


def _money(value: Decimal) -> float:
    return float(value.quantize(MONEY_PLACES, rounding=ROUND_HALF_UP))
