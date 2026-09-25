"""Bodies for the Media Library's reads (Stage 04 PRD §15.4 G, §16 "Media")."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent.api.schemas_runs import SpendMeter

MediaVariant = Literal["preview", "poster", "master"]


class RegenerationEstimateRequest(BaseModel):
    """What a regeneration would be asked for, less the note: the same
    `model_override` and `params_override` `POST /creative-assets/{id}/regenerate`
    takes (§16), so the price shown is the price of the request submitted."""

    model_config = ConfigDict(extra="forbid")

    model_override: str | None = Field(
        default=None,
        min_length=1,
        description="An allowlisted model of the asset's modality; None keeps the run's.",
    )
    provider_tag: str | None = Field(
        default=None, description="The allowlist entry's provider pin, when it has one."
    )
    params_override: dict[str, Any] = Field(
        default_factory=dict,
        description="Parameters the chosen model supports, over its validated defaults.",
    )


class RegenerationEstimate(BaseModel):
    """The consequence in numbers before the spend (§15.2 rule 8)."""

    asset_id: uuid.UUID
    modality: Literal["image", "video"]
    model_id: str
    provider_tag: str | None
    #: What each request would send, less the prompt — validated against the
    #: chosen model's live capability record (Law 36).
    params: list[dict[str, Any]]
    requests: int = Field(description="One image; one request per clip of a video.")
    estimate_usd: Decimal
    confidence: Literal["high", "medium", "low"]
    media: SpendMeter = Field(description="The run's media cap: spent and reserved now.")
    remaining_usd: Decimal = Field(description="Media cap less spent and reserved, now.")
    remaining_after_usd: Decimal = Field(
        description="What would remain once this regeneration is reserved (never below 0)."
    )
    fits: bool = Field(description="The estimate is within what remains of the media cap.")
