"""Law 36: every request field is validated against the pinned capability record.

`capability.py` is pure, so every case here builds its record by hand. The
records mirror recorded catalogue entries (tests/fixtures/openrouter/) so the
table exercises the shapes OpenRouter actually publishes, but nothing here
parses a fixture — that is `catalogue.py`'s job and its own test file's.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from agent.media.capability import (
    FieldError,
    capability_hash,
    ratio_coverage,
    validate,
)
from agent.media.types import (
    CapabilityRecord,
    Descriptor,
    FrameImage,
    ImageRequest,
    PriceLine,
    ProviderPreferences,
    ReferenceImage,
    VideoCaps,
    VideoRequest,
)
from agent.schemas.creative_input import canonical_hash

TOLERANCE = 0.005
MIN_RETAINED = 0.85

FLUX_RATIOS = ["1:1", "4:3", "3:4", "3:2", "2:3", "16:9", "9:16", "21:9", "auto"]


def flux() -> CapabilityRecord:
    """black-forest-labs/flux.2-klein-4b's endpoint record."""
    return CapabilityRecord(
        modality="image",
        model_id="black-forest-labs/flux.2-klein-4b",
        provider_tag="black-forest-labs",
        params={
            "aspect_ratio": Descriptor(kind="enum", values=FLUX_RATIOS),
            "output_format": Descriptor(kind="enum", values=["png", "jpeg"]),
            "n": Descriptor(kind="range", min=1, max=1),
            "input_references": Descriptor(kind="range", min=0, max=4),
            "seed": Descriptor(kind="boolean"),
        },
        pricing=[PriceLine(billable="output_image", unit="megapixel", usd=Decimal("0.014"))],
        input_modalities=["text", "image"],
    )


def text_only_image() -> CapabilityRecord:
    """inclusionai/ming-image-0.1-design: no image input, references pinned to 0."""
    return CapabilityRecord(
        modality="image",
        model_id="inclusionai/ming-image-0.1-design",
        provider_tag="novita",
        params={
            "output_format": Descriptor(kind="enum", values=["png", "jpeg", "webp"]),
            "n": Descriptor(kind="range", min=1, max=1),
            "input_references": Descriptor(kind="range", min=0, max=0),
        },
        pricing=[],
        input_modalities=["text"],
    )


def reference_required() -> CapabilityRecord:
    """recraft/recraft-v4-styles: at least one reference or the provider refuses."""
    return CapabilityRecord(
        modality="image",
        model_id="recraft/recraft-v4-styles",
        provider_tag="recraft",
        params={
            "aspect_ratio": Descriptor(kind="enum", values=["1:1", "16:9", "auto"]),
            "input_references": Descriptor(kind="range", min=1, max=10),
        },
        pricing=[],
        input_modalities=["text", "image"],
    )


def veo_lite() -> CapabilityRecord:
    """google/veo-3.1-lite's /videos/models entry."""
    return CapabilityRecord(
        modality="video",
        model_id="google/veo-3.1-lite",
        params={
            "generate_audio": Descriptor(kind="boolean"),
            "seed": Descriptor(kind="boolean"),
        },
        video=VideoCaps(
            durations=[4, 6, 8],
            resolutions=["720p", "1080p"],
            aspect_ratios=["16:9", "9:16"],
            sizes=["1280x720", "720x1280", "1920x1080", "1080x1920"],
            frame_images=["first_frame", "last_frame"],
        ),
        pricing=[],
        input_modalities=["text", "image"],
    )


def grok_video() -> CapabilityRecord:
    """x-ai/grok-imagine-video: `generate_audio` and `seed` are null — unknown."""
    return CapabilityRecord(
        modality="video",
        model_id="x-ai/grok-imagine-video",
        params={},
        video=VideoCaps(
            durations=list(range(1, 16)),
            resolutions=["480p", "720p"],
            aspect_ratios=["16:9", "9:16", "1:1", "4:3", "3:4", "3:2", "2:3"],
            sizes=[],
            frame_images=["first_frame"],
        ),
        pricing=[],
        input_modalities=["text", "image"],
    )


def edit_only_video() -> CapabilityRecord:
    """black-forest-labs/flux-video-edit: every `supported_*` is null."""
    return CapabilityRecord(
        modality="video",
        model_id="black-forest-labs/flux-video-edit",
        params={},
        video=VideoCaps(),
        pricing=[],
        input_modalities=["text", "video"],
    )


def image(**fields: object) -> ImageRequest:
    return ImageRequest.model_validate(
        {"model": "black-forest-labs/flux.2-klein-4b", "prompt": "a mug", **fields}
    )


def video(**fields: object) -> VideoRequest:
    return VideoRequest.model_validate(
        {"model": "google/veo-3.1-lite", "prompt": "a mug", **fields}
    )


def ref(n: int = 0) -> ReferenceImage:
    return ReferenceImage(sha256=f"{n:064x}", media_type="image/png", data=b"\x89PNG")


# ---------------------------------------------------------------------------
# images
# ---------------------------------------------------------------------------


def test_a_request_with_only_model_and_prompt_is_valid() -> None:
    assert validate(image(), flux()) == []


def test_an_enum_value_in_the_list_passes() -> None:
    assert validate(image(aspect_ratio="16:9", output_format="jpeg"), flux()) == []


def test_an_enum_value_outside_the_list_names_the_field_and_every_supported_value() -> None:
    errors = validate(image(aspect_ratio="7:3"), flux())

    assert errors == [
        FieldError(
            field="aspect_ratio",
            value="7:3",
            supported=FLUX_RATIOS,
            reason="not_in_values",
        )
    ]


@pytest.mark.parametrize("count", [0, 4])
def test_a_range_accepts_both_of_its_bounds(count: int) -> None:
    refs = [ref(i) for i in range(count)] or None
    assert validate(image(input_references=refs), flux()) == []


def test_a_range_rejects_one_past_its_maximum() -> None:
    errors = validate(image(input_references=[ref(i) for i in range(5)]), flux())

    assert errors == [
        FieldError(
            field="input_references",
            value=5,
            supported={"min": 0, "max": 4},
            reason="out_of_range",
        )
    ]


@pytest.mark.parametrize("n", [0, 2])
def test_a_range_rejects_values_either_side_of_a_fixed_count(n: int) -> None:
    errors = validate(image(n=n), flux())

    assert [(e.field, e.value, e.supported, e.reason) for e in errors] == [
        ("n", n, {"min": 1, "max": 1}, "out_of_range")
    ]


def test_a_boolean_descriptor_means_the_field_may_be_sent() -> None:
    assert validate(image(seed=42), flux()) == []


def test_a_boolean_field_the_record_does_not_list_is_unsupported() -> None:
    record = text_only_image().model_copy(update={"model_id": image().model})

    errors = validate(image(seed=42), record)

    assert [(e.field, e.reason, e.supported) for e in errors] == [("seed", "unsupported_field", [])]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("quality", "high"),
        ("background", "transparent"),
        ("size", "1024x1024"),
        ("resolution", "2K"),
        ("output_compression", 80),
    ],
)
def test_an_absent_key_means_unsupported_never_try_it(field: str, value: object) -> None:
    errors = validate(image(**{field: value}), flux())

    assert errors == [
        FieldError(field=field, value=value, supported=[], reason="unsupported_field")
    ]


def test_every_failing_field_is_reported_not_just_the_first() -> None:
    errors = validate(image(aspect_ratio="7:3", quality="high", n=3), flux())

    assert [e.field for e in errors] == ["aspect_ratio", "n", "quality"]


def test_a_request_for_a_different_model_is_refused() -> None:
    errors = validate(image().model_copy(update={"model": "openai/gpt-image-1"}), flux())

    assert errors == [
        FieldError(
            field="model",
            value="openai/gpt-image-1",
            supported=["black-forest-labs/flux.2-klein-4b"],
            reason="not_in_values",
        )
    ]


def test_a_model_that_needs_a_reference_refuses_a_request_without_one() -> None:
    request = image().model_copy(update={"model": "recraft/recraft-v4-styles"})

    errors = validate(request, reference_required())

    assert errors == [
        FieldError(
            field="input_references",
            value=0,
            supported={"min": 1, "max": 10},
            reason="out_of_range",
        )
    ]


def test_references_to_a_model_without_image_input_are_refused() -> None:
    request = image(input_references=[ref()]).model_copy(
        update={"model": text_only_image().model_id}
    )

    errors = validate(request, text_only_image())

    assert [(e.field, e.reason) for e in errors] == [("input_references", "out_of_range")]


def test_a_pinned_provider_cannot_be_swapped_or_allowed_to_fall_back() -> None:
    swapped = image(provider=ProviderPreferences(only=["someone-else"]))
    loose = image(provider=ProviderPreferences(only=["black-forest-labs"], allow_fallbacks=True))
    pinned = image(provider=ProviderPreferences(only=["black-forest-labs"], allow_fallbacks=False))

    assert [(e.field, e.supported) for e in validate(swapped, flux())] == [
        ("provider", ["black-forest-labs"])
    ]
    assert [e.field for e in validate(loose, flux())] == ["provider"]
    assert validate(pinned, flux()) == []


def test_a_request_cannot_pin_a_provider_the_choice_did_not_pin() -> None:
    record = flux().model_copy(update={"provider_tag": None})
    request = image(provider=ProviderPreferences(only=["black-forest-labs"]))

    assert [(e.field, e.reason) for e in validate(request, record)] == [
        ("provider", "unsupported_field")
    ]


def test_an_image_request_against_a_video_record_is_refused() -> None:
    errors = validate(image().model_copy(update={"model": "google/veo-3.1-lite"}), veo_lite())

    assert [(e.field, e.reason) for e in errors] == [("modality", "not_in_values")]


# ---------------------------------------------------------------------------
# video
# ---------------------------------------------------------------------------


def test_a_video_request_inside_every_supported_list_is_valid() -> None:
    request = video(
        duration=4,
        resolution="720p",
        aspect_ratio="16:9",
        generate_audio=False,
        seed=7,
        frame_images=[FrameImage(frame_type="first_frame", image=ref())],
    )

    assert validate(request, veo_lite()) == []


def test_a_duration_outside_supported_durations_names_them() -> None:
    errors = validate(video(duration=5), veo_lite())

    assert errors == [
        FieldError(field="duration", value=5, supported=[4, 6, 8], reason="not_in_values")
    ]


def test_a_resolution_outside_supported_resolutions_names_them() -> None:
    errors = validate(video(resolution="4K"), veo_lite())

    assert errors == [
        FieldError(
            field="resolution", value="4K", supported=["720p", "1080p"], reason="not_in_values"
        )
    ]


def test_an_unsupported_video_aspect_ratio_names_the_supported_ratios() -> None:
    errors = validate(video(aspect_ratio="1:1"), veo_lite())

    assert errors == [
        FieldError(
            field="aspect_ratio", value="1:1", supported=["16:9", "9:16"], reason="not_in_values"
        )
    ]


def test_a_size_outside_supported_sizes_names_them() -> None:
    errors = validate(video(size="640x480"), veo_lite())

    assert [(e.field, e.supported) for e in errors] == [
        ("size", ["1280x720", "720x1280", "1920x1080", "1080x1920"])
    ]


def test_a_null_catalogue_flag_is_absent_so_audio_is_unsupported() -> None:
    request = video(generate_audio=False).model_copy(update={"model": grok_video().model_id})

    errors = validate(request, grok_video())

    assert errors == [
        FieldError(field="generate_audio", value=False, supported=[], reason="unsupported_field")
    ]


def test_a_frame_type_the_model_does_not_take_is_refused() -> None:
    request = video(frame_images=[FrameImage(frame_type="last_frame", image=ref())]).model_copy(
        update={"model": grok_video().model_id}
    )

    errors = validate(request, grok_video())

    assert errors == [
        FieldError(
            field="frame_images",
            value="last_frame",
            supported=["first_frame"],
            reason="not_in_values",
        )
    ]


def test_a_model_with_no_supported_durations_refuses_any_duration() -> None:
    request = video(duration=5).model_copy(update={"model": edit_only_video().model_id})

    errors = validate(request, edit_only_video())

    assert errors == [
        FieldError(field="duration", value=5, supported=[], reason="unsupported_field")
    ]


def test_video_input_references_have_no_catalogue_key_so_are_unsupported() -> None:
    errors = validate(video(input_references=[ref()]), veo_lite())

    assert errors == [
        FieldError(field="input_references", value=1, supported=[], reason="unsupported_field")
    ]


# ---------------------------------------------------------------------------
# ratio coverage
# ---------------------------------------------------------------------------


def coverage(required: list[str], record: CapabilityRecord) -> dict[str, str]:
    return ratio_coverage(required, record, tolerance=TOLERANCE, min_retained=MIN_RETAINED)


def test_a_supported_ratio_is_relaid_when_the_model_takes_image_input() -> None:
    assert coverage(["1:1", "9:16"], flux()) == {"1:1": "relaid", "9:16": "relaid"}


def test_a_supported_ratio_is_native_when_the_model_cannot_take_the_master() -> None:
    record = flux().model_copy(update={"input_modalities": ["text"]})

    assert coverage(["1:1", "16:9"], record) == {"1:1": "native", "16:9": "native"}


def test_a_ratio_within_tolerance_of_a_supported_one_counts_as_supported() -> None:
    record = flux().model_copy(update={"input_modalities": ["text"]})

    # 16:9 = 1.7778; 1.78:1 is 0.12 % away, inside the 0.5 % tolerance.
    assert coverage(["1.78:1"], record) == {"1.78:1": "native"}


def test_an_unsupported_ratio_a_supported_one_covers_is_a_crop() -> None:
    # 1.91:1 from 16:9 keeps 1.7778 / 1.91 = 93 % of the frame.
    assert coverage(["1.91:1", "4:5"], flux()) == {"1.91:1": "crop", "4:5": "crop"}


def test_an_unsupported_ratio_no_supported_one_covers_is_a_gap() -> None:
    # veo-lite has 16:9 and 9:16 only; 1:1 from either keeps 56 % of the frame.
    assert coverage(["1:1", "16:9"], veo_lite()) == {"1:1": "gap", "16:9": "relaid"}


def test_auto_is_not_a_ratio_and_a_model_without_aspect_ratio_covers_nothing() -> None:
    record = text_only_image()

    assert coverage(["1:1", "16:9"], record) == {"1:1": "gap", "16:9": "gap"}


def test_video_is_relaid_only_when_the_model_takes_a_first_frame() -> None:
    no_frames = veo_lite().model_copy(
        update={"video": veo_lite().video.model_copy(update={"frame_images": ["last_frame"]})}  # type: ignore[union-attr]
    )

    assert coverage(["9:16"], veo_lite()) == {"9:16": "relaid"}
    assert coverage(["9:16"], no_frames) == {"9:16": "native"}


def test_an_unparseable_required_ratio_is_refused_loudly() -> None:
    with pytest.raises(ValueError, match="wide"):
        coverage(["wide"], flux())


# ---------------------------------------------------------------------------
# the hash
# ---------------------------------------------------------------------------


def test_capability_hash_is_the_creative_input_canonical_hash_of_the_record() -> None:
    record = flux()

    assert capability_hash(record) == canonical_hash(record.model_dump(mode="json"))


def test_capability_hash_moves_when_a_supported_value_moves() -> None:
    narrower = flux().model_copy(
        update={"params": {**flux().params, "n": Descriptor(kind="range", min=1, max=2)}}
    )

    assert capability_hash(narrower) != capability_hash(flux())
