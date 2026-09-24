"""The shapes the media gateway speaks (PRD §9.1, §23.1 items 2–5).

Pure declarations — pydantic and nothing else — because `calc/` prices
requests from these records and `calc/` may not import HTTP, Redis or the ORM.

`CapabilityRecord` is what `catalogue.py` normalises OpenRouter's three
catalogue reads into, what `MediaModelChoice.capability` snapshots at
selection time, and what `capability.validate()` checks every request against
before spend. It is the catalogue *as the user chose from it*: a later
catalogue change never silently alters a running job (PRD §4.3 rule 3).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Modality = Literal["image", "video"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ---------------------------------------------------------------------------
# the capability record
# ---------------------------------------------------------------------------


class Descriptor(_Frozen):
    """One `supported_parameters` entry, as OpenRouter types it.

    `enum` ⇒ the value must be one of `values`; `range` ⇒ an integer in
    `[min, max]`; `boolean` ⇒ the field may be sent at all. A parameter with no
    descriptor is unsupported — never "try it".
    """

    kind: Literal["enum", "range", "boolean"]
    values: list[str] | None = None
    min: int | None = None
    max: int | None = None


class VideoCaps(_Frozen):
    """`/videos/models`' `supported_*` lists. Null in the catalogue ⇒ empty here,
    and empty means unsupported."""

    durations: list[int] = Field(default_factory=list)
    resolutions: list[str] = Field(default_factory=list)
    aspect_ratios: list[str] = Field(default_factory=list)
    sizes: list[str] = Field(default_factory=list)
    #: `supported_frame_images`: which of first_frame / last_frame it accepts.
    frame_images: list[str] = Field(default_factory=list)


class PriceLine(_Frozen):
    """One billable line, in US dollars.

    Images carry OpenRouter's endpoint `pricing[]` verbatim (`billable`,
    `unit` ∈ image | megapixel | token | request, `variant`). Video carries one
    line per `pricing_skus` key, parsed by `catalogue.py`: `unit='second'` for
    a per-second SKU with `variant` = its resolution, `audio` and `mode` when
    the key names them, and `sku` = the key it came from. A key the parser does
    not recognise is kept with `unit='unknown'` — dropped, it would read as
    free.
    """

    billable: str
    unit: str
    usd: Decimal
    variant: str | None = None
    audio: bool | None = None
    mode: Literal["text_to_video", "image_to_video"] | None = None
    sku: str | None = None


class CapabilityRecord(_Frozen):
    """What one model (on one pinned provider, for images) accepts and costs."""

    modality: Modality
    model_id: str = Field(min_length=1)
    #: Images: the endpoint's provider tag, when the choice pins one. None ⇒
    #: OpenRouter routes within the model. Video has no provider routing field
    #: on its request, so this is always None there.
    provider_tag: str | None = None
    params: dict[str, Descriptor] = Field(default_factory=dict)
    #: Video only.
    video: VideoCaps | None = None
    pricing: list[PriceLine] = Field(default_factory=list)
    input_modalities: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# requests
# ---------------------------------------------------------------------------


class ReferenceImage(_Frozen):
    """An image sent to a provider — as a base64 data URL on the wire, and by
    sha256 everywhere else (Law 44, PRD §9.1 item 7). `data` is excluded from
    every dump, so the redacted form cannot carry it by accident."""

    sha256: str = Field(min_length=64, max_length=64)
    media_type: str
    data: bytes = Field(exclude=True, repr=False)


class FrameImage(_Frozen):
    frame_type: Literal["first_frame", "last_frame"]
    image: ReferenceImage


class ProviderPreferences(_Frozen):
    """Images only. `only=[tag]` + `allow_fallbacks=False` when a tag is pinned;
    fallback *within* the chosen model otherwise (PRD §9.1 item 6)."""

    only: list[str] | None = None
    order: list[str] | None = None
    allow_fallbacks: bool | None = None


class ImageRequest(_Frozen):
    """`POST /api/v1/images`. Only the fields the capability record allows are
    ever set; `validate()` refuses the rest before a byte leaves."""

    model: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    aspect_ratio: str | None = None
    resolution: str | None = None
    size: str | None = None
    quality: str | None = None
    output_format: str | None = None
    background: str | None = None
    output_compression: int | None = None
    n: int | None = None
    seed: int | None = None
    input_references: list[ReferenceImage] | None = None
    provider: ProviderPreferences | None = None


class VideoRequest(_Frozen):
    """`POST /api/v1/videos`. No `callback_url` in v1 (PRD §9.1 item 4)."""

    model: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    duration: int | None = None
    resolution: str | None = None
    aspect_ratio: str | None = None
    size: str | None = None
    frame_images: list[FrameImage] | None = None
    input_references: list[ReferenceImage] | None = None
    generate_audio: bool | None = None
    seed: int | None = None


MediaRequest = ImageRequest | VideoRequest


def redacted(request: MediaRequest) -> dict[str, Any]:
    """The request as `GenerationJob.request` stores it: canonical, unset
    fields dropped, every image by sha256 and media type — never base64."""
    return request.model_dump(mode="json", exclude_none=True)
