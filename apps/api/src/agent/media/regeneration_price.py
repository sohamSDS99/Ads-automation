"""What regenerating one media asset would cost, before anyone asks for it
(PRD §15.2 rule 8, §15.4 G "the cost of this regeneration against the
remaining budget").

The price is `calc.media`'s, for the requests the asset's node made: an image
asset is one image request at the model's validated defaults (4.4.2's
`REQUEST_DEFAULT_FIELDS`, at the master's ratio); a video asset is one request
per clip 4.4.4 made for it, each at its planned duration and ratio with the
fields `creative.video.request_fields` sends — the same way 4.4.4 prices them.
Nothing here reserves or spends; the route that regenerates reserves before it
submits (Law 43).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from agent.calc.media import Confidence, image_job_price, video_job_price
from agent.creative import masters, video
from agent.media.constants import MediaConstants
from agent.media.types import CapabilityRecord
from agent.orchestrator.creative_input import CreativeInputError
from agent.postprod.image import supported_label
from agent.schemas.creative_input import MediaModelChoice

_RANK: dict[str, int] = {"high": 2, "medium": 1, "low": 0}


@dataclass(frozen=True, slots=True)
class PlannedClip:
    ratio: str
    duration_s: int


@dataclass(frozen=True, slots=True)
class RegenerationPrice:
    usd: Decimal
    confidence: Confidence
    #: How many requests the price covers (one image; one per clip).
    requests: int
    #: What each request would send, less the prompt: what the price is of.
    params: list[dict[str, Any]]


def image_params(choice: MediaModelChoice, aspect_ratio: str | None) -> dict[str, Any]:
    """4.4.2's request fields: the validated defaults it sends, at the master's ratio."""
    params = {k: v for k, v in choice.defaults.items() if k in masters.REQUEST_DEFAULT_FIELDS}
    if aspect_ratio:
        params["aspect_ratio"] = aspect_ratio
    return params


def regeneration_price(
    choice: MediaModelChoice,
    *,
    constants: MediaConstants,
    aspect_ratio: str | None = None,
    clips: list[PlannedClip] | None = None,
) -> RegenerationPrice:
    """Price one regeneration of an image (`clips` None) or a video (`clips`
    from its stored shot plan). Raises `CreativeInputError`
    (`capability_unsupported`) when a clip's duration is one the chosen model
    cannot make — the same refusal the submit would meet (Law 36)."""
    capability = CapabilityRecord.model_validate(choice.capability)
    if choice.modality == "image":
        params = image_params(choice, aspect_ratio)
        price = image_job_price(capability, params, constants)
        return RegenerationPrice(
            usd=price.usd, confidence=price.confidence, requests=1, params=[params]
        )

    if not clips:
        raise CreativeInputError(
            "no_shot_plan",
            "This video has no stored shot plan to regenerate from.",
            modality="video",
        )
    supported = sorted(capability.video.durations) if capability.video else []
    fields = video.request_fields(choice.defaults, capability, constants.generate_audio_default)
    total = Decimal(0)
    confidence: Confidence = "high"
    sent: list[dict[str, Any]] = []
    for clip in clips:
        if supported and clip.duration_s not in supported:
            raise CreativeInputError(
                "capability_unsupported",
                f"{choice.model_id} cannot make a {clip.duration_s} s clip, which this video's "
                f"shot plan needs. It makes {', '.join(f'{d} s' for d in supported)}: choose a "
                "model that makes the planned lengths.",
                modality="video",
                field="duration",
                supported=supported,
            )
        label = supported_label(clip.ratio, capability, tolerance=constants.ratio_tolerance)
        params = {**fields, "duration": clip.duration_s, "aspect_ratio": label or clip.ratio}
        price = video_job_price(capability, params, constants)
        total += price.usd
        if _RANK[price.confidence] < _RANK[confidence]:
            confidence = price.confidence
        sent.append(params)
    return RegenerationPrice(usd=total, confidence=confidence, requests=len(clips), params=sent)
