"""Law 36 in code: validate before spend (PRD §9.1 item 2, §23.1 item 3). PURE.

`validate()` checks every field a request sets against the capability record
the user chose from — `enum` ⇒ value ∈ values, `range` ⇒ min ≤ v ≤ max,
`boolean` ⇒ the key is present, video ⇒ value ∈ `supported_*` — and returns
every failure, not the first. **An absent key means unsupported, never "try
it"**: a request that sets a field the record does not list is refused here,
so OpenRouter never sees it and nobody pays to learn it would have failed.

`ratio_coverage()` answers, before spend, what each ratio the spec sheet
requires will cost in quality (PRD §9.2, §9.4 item 1, where `relaid` >
`native` > `crop`):

* `relaid` — the model supports the ratio and takes the master as input
  (image-to-image, or a first frame for video), so the rendition is re-laid
  out from the master and keeps the concept;
* `native` — the model supports the ratio but cannot take the master, so the
  rendition is painted fresh at that ratio;
* `crop` — the model does not support the ratio, but a supported one covers
  it: cropping from it keeps at least `min_retained` of the frame;
* `gap` — neither; recorded, never faked (Law 39).

Saliency cannot be measured before a pixel exists, so the crop test here is on
frame area — the plan-time bound. `media.crop_window_v1` (S4-P10) re-decides
every crop on real saliency and may still turn one into a gap.

No I/O, no clock, no randomness: `calc/` imports this module.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from agent.media.types import (
    CapabilityRecord,
    Descriptor,
    ImageRequest,
    MediaRequest,
    ProviderPreferences,
    VideoCaps,
    VideoRequest,
)

Coverage = Literal["native", "relaid", "crop", "gap"]

#: `aspect_ratio: auto` lets the provider choose. It is a mode, not a ratio,
#: and can never satisfy a spec sheet's requirement.
AUTO = "auto"

#: Image fields validated against a scalar descriptor. `input_references` and
#: `provider` have their own rules below.
_IMAGE_SCALARS = (
    "aspect_ratio",
    "resolution",
    "size",
    "quality",
    "output_format",
    "background",
    "output_compression",
    "n",
    "seed",
)
#: Video fields validated against a `supported_*` list, and the list each reads.
_VIDEO_LISTS = ("duration", "resolution", "aspect_ratio", "size")
#: Video fields the catalogue publishes as booleans (`generate_audio`, `seed`).
_VIDEO_FLAGS = ("generate_audio", "seed")


class FieldError(BaseModel):
    """One refused field: what was asked, and what the model accepts.

    `supported` is the list for an enum or a `supported_*` field, `{min, max}`
    for a range, and `[]` when the model accepts the field in no form at all.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    field: str
    value: Any
    supported: list[Any] | dict[str, int]
    reason: Literal["unsupported_field", "not_in_values", "out_of_range"]


def validate(request: MediaRequest, capability: CapabilityRecord) -> list[FieldError]:
    """Every field of `request` that `capability` does not allow, by field name."""
    modality = "image" if isinstance(request, ImageRequest) else "video"
    if modality != capability.modality:
        return [
            FieldError(
                field="modality",
                value=modality,
                supported=[capability.modality],
                reason="not_in_values",
            )
        ]
    errors: list[FieldError] = []
    if request.model != capability.model_id:
        errors.append(
            FieldError(
                field="model",
                value=request.model,
                supported=[capability.model_id],
                reason="not_in_values",
            )
        )
    if isinstance(request, ImageRequest):
        errors.extend(_image_errors(request, capability))
    else:
        errors.extend(_video_errors(request, capability))
    return sorted(errors, key=lambda error: error.field)


def ratio_coverage(
    required_ratios: list[str],
    capability: CapabilityRecord,
    *,
    tolerance: float,
    min_retained: float,
) -> dict[str, Coverage]:
    """`{ratio: native | relaid | crop | gap}` for every required ratio.

    `tolerance` is `media.ratio_tolerance` (relative), `min_retained` is
    `media.crop_min_saliency_retained`, both from the constants.
    """
    supported = [parse_ratio(value) for value in supported_ratios(capability)]
    takes_master = accepts_master(capability)
    plan: dict[str, Coverage] = {}
    for label in required_ratios:
        wanted = parse_ratio(label)
        if any(abs(have / wanted - 1) <= tolerance for have in supported):
            plan[label] = "relaid" if takes_master else "native"
        elif best_crop_retention(wanted, supported) >= min_retained:
            plan[label] = "crop"
        else:
            plan[label] = "gap"
    return plan


def supported_ratios(capability: CapabilityRecord) -> list[str]:
    """The ratios the model paints, `auto` excluded."""
    if capability.modality == "video":
        values = (capability.video or VideoCaps()).aspect_ratios
    else:
        descriptor = capability.params.get("aspect_ratio")
        values = (descriptor.values or []) if descriptor and descriptor.kind == "enum" else []
    return [value for value in values if value != AUTO]


def accepts_master(capability: CapabilityRecord) -> bool:
    """Can the model take the master as input? Image-to-image for images (an
    `image` input modality and room for at least one reference), a first frame
    for video."""
    if capability.modality == "video":
        return "first_frame" in (capability.video or VideoCaps()).frame_images
    references = capability.params.get("input_references")
    return (
        "image" in capability.input_modalities
        and references is not None
        and references.kind == "range"
        and (references.max or 0) >= 1
    )


def best_crop_retention(wanted: float, supported: list[float]) -> float:
    """The largest share of a supported frame a crop to `wanted` keeps."""
    return max((min(have, wanted) / max(have, wanted) for have in supported), default=0.0)


def parse_ratio(label: str) -> float:
    """`"16:9"` → 1.777…, `"1.91:1"` → 1.91. Anything else is refused loudly."""
    width, sep, height = label.partition(":")
    try:
        value = float(width) / float(height)
    except (ValueError, ZeroDivisionError):
        value = 0.0
    if not sep or value <= 0:
        raise ValueError(f"{label!r} is not an aspect ratio (expected W:H, e.g. 16:9)")
    return value


def capability_hash(capability: CapabilityRecord) -> str:
    """sha256 over the record's canonical JSON — the same canonical form as
    `schemas.creative_input.canonical_hash`, restated here because that module
    imports the export contracts and this one must stay importable by `calc/`.
    """
    rendered = json.dumps(
        capability.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# per modality
# ---------------------------------------------------------------------------


def _image_errors(request: ImageRequest, capability: CapabilityRecord) -> list[FieldError]:
    errors: list[FieldError] = []
    for field in _IMAGE_SCALARS:
        value = getattr(request, field)
        if value is not None:
            error = _against(field, value, capability.params.get(field))
            if error is not None:
                errors.append(error)

    references = len(request.input_references or [])
    descriptor = capability.params.get("input_references")
    if descriptor is None:
        if references:
            errors.append(_unsupported("input_references", references))
    else:
        error = _against("input_references", references, descriptor)
        if error is None and references and "image" not in capability.input_modalities:
            error = _unsupported("input_references", references)
        if error is not None:
            errors.append(error)

    if request.provider is not None:
        error = _provider_error(request.provider, capability)
        if error is not None:
            errors.append(error)
    return errors


def _provider_error(
    provider: ProviderPreferences, capability: CapabilityRecord
) -> FieldError | None:
    """A pinned choice stays pinned (`allow_fallbacks=false`); an unpinned one
    cannot be pinned by the request. Fallback *within* the model is otherwise
    OpenRouter's to make (PRD §9.1 item 6)."""
    asked = provider.model_dump(exclude_none=True)
    tag = capability.provider_tag
    if tag is None:
        return _unsupported("provider", asked) if provider.only else None
    if (provider.only is not None and provider.only != [tag]) or provider.allow_fallbacks:
        return FieldError(field="provider", value=asked, supported=[tag], reason="not_in_values")
    return None


def _video_errors(request: VideoRequest, capability: CapabilityRecord) -> list[FieldError]:
    caps = capability.video or VideoCaps()
    lists: dict[str, list[Any]] = {
        "duration": list(caps.durations),
        "resolution": list(caps.resolutions),
        "aspect_ratio": list(caps.aspect_ratios),
        "size": list(caps.sizes),
    }
    errors: list[FieldError] = []
    for field in _VIDEO_LISTS:
        value = getattr(request, field)
        if value is None:
            continue
        allowed = lists[field]
        if not allowed:
            errors.append(_unsupported(field, value))
        elif value not in allowed:
            errors.append(
                FieldError(field=field, value=value, supported=allowed, reason="not_in_values")
            )

    for field in _VIDEO_FLAGS:
        value = getattr(request, field)
        if value is not None:
            error = _against(field, value, capability.params.get(field))
            if error is not None:
                errors.append(error)

    if request.frame_images:
        if not caps.frame_images or "image" not in capability.input_modalities:
            errors.append(_unsupported("frame_images", len(request.frame_images)))
        else:
            wrong = next(
                (
                    f.frame_type
                    for f in request.frame_images
                    if f.frame_type not in caps.frame_images
                ),
                None,
            )
            if wrong is not None:
                errors.append(
                    FieldError(
                        field="frame_images",
                        value=wrong,
                        supported=list(caps.frame_images),
                        reason="not_in_values",
                    )
                )

    # `/videos/models` publishes no capability for reference-to-video, and an
    # absent key is unsupported.
    if request.input_references:
        errors.append(_unsupported("input_references", len(request.input_references)))
    return errors


def _against(field: str, value: Any, descriptor: Descriptor | None) -> FieldError | None:
    if descriptor is None:
        return _unsupported(field, value)
    if descriptor.kind == "boolean":
        return None
    if descriptor.kind == "enum":
        allowed = list(descriptor.values or [])
        if value in allowed:
            return None
        return FieldError(field=field, value=value, supported=allowed, reason="not_in_values")
    low, high = descriptor.min, descriptor.max
    bounds = {key: bound for key, bound in (("min", low), ("max", high)) if bound is not None}
    in_range = (
        isinstance(value, int)
        and not isinstance(value, bool)
        and (low is None or value >= low)
        and (high is None or value <= high)
    )
    if in_range:
        return None
    return FieldError(field=field, value=value, supported=bounds, reason="out_of_range")


def _unsupported(field: str, value: Any) -> FieldError:
    return FieldError(field=field, value=value, supported=[], reason="unsupported_field")
