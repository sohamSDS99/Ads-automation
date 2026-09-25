"""What a regeneration costs before it is asked for (Stage 04 PRD §15.2 rule 8,
§15.4 G) — `calc.media`'s prices for the requests the asset's node made.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from agent.calc.media import image_job_price, video_job_price
from agent.media.capability import capability_hash
from agent.media.constants import media_constants
from agent.media.regeneration_price import PlannedClip, regeneration_price
from agent.media.types import CapabilityRecord, Descriptor, PriceLine, VideoCaps
from agent.orchestrator.creative_input import CreativeInputError
from agent.schemas.creative_input import MediaModelChoice

VIDEO = CapabilityRecord(
    modality="video",
    model_id="acme/clip-1",
    params={"generate_audio": Descriptor(kind="boolean")},
    video=VideoCaps(durations=[4, 6, 8], resolutions=["720p"], aspect_ratios=["16:9", "9:16"]),
    pricing=[
        PriceLine(
            billable="output_video", unit="second", usd=Decimal("0.10"), variant="720p", audio=False
        ),  # fmt: skip
        PriceLine(
            billable="output_video", unit="second", usd=Decimal("0.25"), variant="720p", audio=True
        ),  # fmt: skip
    ],
    input_modalities=["text"],
)
IMAGE = CapabilityRecord(
    modality="image",
    model_id="acme/still-1",
    params={
        "aspect_ratio": Descriptor(kind="enum", values=["1:1", "16:9"]),
        "quality": Descriptor(kind="enum", values=["low", "high"]),
    },
    pricing=[
        PriceLine(billable="output_image", unit="image", usd=Decimal("0.02"), variant="low"),
        PriceLine(billable="output_image", unit="image", usd=Decimal("0.08"), variant="high"),
    ],
    input_modalities=["text"],
)


def _choice(record: CapabilityRecord, defaults: dict[str, object]) -> MediaModelChoice:
    return MediaModelChoice(
        modality=record.modality,
        model_id=record.model_id,
        capability=record.model_dump(mode="json"),
        capability_hash=capability_hash(record),
        defaults=defaults,
    )


def test_an_image_is_one_request_at_the_masters_ratio_with_the_nodes_fields() -> None:
    choice = _choice(IMAGE, {"quality": "high", "seed": 7})

    price = regeneration_price(choice, constants=media_constants(), aspect_ratio="16:9")

    # `seed` is not one of 4.4.2's request fields, so it is neither sent nor priced.
    assert price.params == [{"quality": "high", "aspect_ratio": "16:9"}]
    assert price.requests == 1
    assert price.usd == image_job_price(IMAGE, price.params[0], media_constants()).usd


def test_a_video_is_every_planned_clip_at_its_duration_and_ratio() -> None:
    choice = _choice(VIDEO, {"resolution": "720p", "generate_audio": False})
    clips = [PlannedClip("16:9", 4), PlannedClip("16:9", 6), PlannedClip("9:16", 4)]

    price = regeneration_price(choice, constants=media_constants(), clips=clips)

    assert price.requests == 3
    assert [(p["aspect_ratio"], p["duration"]) for p in price.params] == [
        ("16:9", 4), ("16:9", 6), ("9:16", 4),
    ]  # fmt: skip
    assert price.usd == sum(
        (video_job_price(VIDEO, p, media_constants()).usd for p in price.params), Decimal(0)
    )
    assert price.usd == Decimal("0.10") * 14


def test_audio_follows_the_constants_when_the_run_did_not_choose() -> None:
    silent = regeneration_price(
        _choice(VIDEO, {"resolution": "720p"}),
        constants=media_constants(),
        clips=[PlannedClip("16:9", 4)],
    )

    assert silent.params[0]["generate_audio"] is media_constants().generate_audio_default


def test_a_duration_the_model_cannot_make_is_refused_naming_what_it_makes() -> None:
    choice = _choice(VIDEO, {"resolution": "720p"})

    with pytest.raises(CreativeInputError) as refused:
        regeneration_price(choice, constants=media_constants(), clips=[PlannedClip("16:9", 5)])

    assert refused.value.code == "capability_unsupported"
    assert refused.value.extra == {"modality": "video", "field": "duration", "supported": [4, 6, 8]}


def test_a_video_with_no_stored_shot_plan_cannot_be_priced() -> None:
    with pytest.raises(CreativeInputError) as refused:
        regeneration_price(_choice(VIDEO, {}), constants=media_constants(), clips=None)

    assert refused.value.code == "no_shot_plan"
