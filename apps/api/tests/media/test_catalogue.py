"""`catalogue.py`: OpenRouter's three catalogue reads, normalised and cached.

Normalisation is checked against the recorded bodies; caching against a clock
the test owns. respx replays the recordings and refuses anything unmocked.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal

import httpx
import pytest
import respx

from agent.media.capability import capability_hash, validate
from agent.media.catalogue import (
    CatalogueUnavailable,
    MediaCatalogue,
    catalogue_hash,
    intersect_endpoints,
    normalise_image_endpoints,
    normalise_image_models,
    normalise_video_models,
    parse_video_skus,
)
from agent.media.types import CapabilityRecord, Descriptor, PriceLine, VideoRequest
from tests.media.fake_redis import Clock, FakeRedis
from tests.media.openrouter_mock import (
    BASE,
    FLUX,
    GEMINI,
    QWEN,
    VEO,
    body,
    endpoints_fixture,
    mock_catalogue,
)

# ---------------------------------------------------------------------------
# normalisation
# ---------------------------------------------------------------------------


def test_every_recorded_image_model_normalises_to_a_record() -> None:
    records = normalise_image_models(body("images_models.json"))

    assert len(records) == len(body("images_models.json")["data"]) == 55
    flux = next(r for r in records if r.model_id == FLUX)
    assert flux.modality == "image"
    assert flux.provider_tag is None
    assert flux.input_modalities == ["text", "image"]
    assert flux.params["seed"] == Descriptor(kind="boolean")
    assert flux.params["n"] == Descriptor(kind="range", min=1, max=1)
    assert flux.params["output_format"] == Descriptor(kind="enum", values=["png", "jpeg"])
    # The model-level listing carries no pricing; the endpoint records do.
    assert flux.pricing == []


def test_an_endpoint_record_carries_its_provider_tag_and_pricing_lines() -> None:
    records = normalise_image_endpoints(
        body(endpoints_fixture(GEMINI)), input_modalities=["image", "text"]
    )

    assert [r.provider_tag for r in records] == [
        "google-ai-studio/priority",
        "google-ai-studio/flex",
        "google-ai-studio",
        "google-vertex/global",
    ]
    priority = records[0]
    assert priority.pricing == [
        PriceLine(billable="input_image", unit="token", usd=Decimal("5.4E-7")),
        PriceLine(billable="output_image", unit="token", usd=Decimal("0.000054")),
    ]
    assert priority.input_modalities == ["image", "text"]


def test_a_variant_priced_endpoint_keeps_each_variant_line() -> None:
    (record,) = normalise_image_endpoints(body(endpoints_fixture(QWEN)), input_modalities=[])

    assert [(p.billable, p.variant, p.usd) for p in record.pricing] == [
        ("input_image", None, Decimal("0.003")),
        ("output_image", "1k", Decimal("0.04")),
        ("output_image", "2k", Decimal("0.075")),
    ]


def test_video_models_normalise_their_supported_lists_and_flags() -> None:
    records = {r.model_id: r for r in normalise_video_models(body("videos_models.json"))}

    assert len(records) == 29
    veo = records[VEO]
    assert veo.video is not None
    assert veo.video.durations == [4, 6, 8]  # the catalogue lists 8, 4, 6
    assert veo.video.resolutions == ["720p", "1080p"]
    assert veo.video.aspect_ratios == ["16:9", "9:16"]
    assert veo.video.frame_images == ["first_frame", "last_frame"]
    assert set(veo.params) == {"generate_audio", "seed"}
    assert veo.input_modalities == ["text", "image"]

    # null flags are unknown, and unknown is absent: nothing may be sent.
    grok = records["x-ai/grok-imagine-video"]
    assert grok.params == {}
    # every supported_* null: an edit model this product cannot drive.
    edit = records["black-forest-labs/flux-video-edit"]
    assert (
        edit.video is not None and edit.video.durations == [] and edit.input_modalities == ["text"]
    )


def test_a_video_record_from_the_catalogue_refuses_what_the_catalogue_lacks() -> None:
    veo = next(r for r in normalise_video_models(body("videos_models.json")) if r.model_id == VEO)

    errors = validate(VideoRequest(model=VEO, prompt="x", aspect_ratio="1:1", duration=5), veo)

    assert [(e.field, e.supported) for e in errors] == [
        ("aspect_ratio", ["16:9", "9:16"]),
        ("duration", [4, 6, 8]),
    ]


@pytest.mark.parametrize(
    ("sku", "value", "expected"),
    [
        (
            "duration_seconds",
            "0.08",
            PriceLine(
                billable="output_video", unit="second", usd=Decimal("0.08"), sku="duration_seconds"
            ),
        ),
        (
            "duration_seconds_480p",
            "0.05",
            PriceLine(
                billable="output_video",
                unit="second",
                usd=Decimal("0.05"),
                variant="480p",
                sku="duration_seconds_480p",
            ),
        ),
        (
            "duration_seconds_without_audio_720p",
            "0.03",
            PriceLine(
                billable="output_video",
                unit="second",
                usd=Decimal("0.03"),
                variant="720p",
                audio=False,
                sku="duration_seconds_without_audio_720p",
            ),
        ),
        (
            "duration_seconds_with_audio_4k",
            "0.30",
            PriceLine(
                billable="output_video",
                unit="second",
                usd=Decimal("0.30"),
                variant="4k",
                audio=True,
                sku="duration_seconds_with_audio_4k",
            ),
        ),
        (
            "text_to_video_duration_seconds_1080p",
            "0.112",
            PriceLine(
                billable="output_video",
                unit="second",
                usd=Decimal("0.112"),
                variant="1080p",
                mode="text_to_video",
                sku="text_to_video_duration_seconds_1080p",
            ),
        ),
        (
            "cents_per_second_output_720p",
            "17",
            PriceLine(
                billable="output_video",
                unit="second",
                usd=Decimal("0.17"),
                variant="720p",
                sku="cents_per_second_output_720p",
            ),
        ),
        (
            "cents_per_video_output_second_480p",
            "5",
            PriceLine(
                billable="output_video",
                unit="second",
                usd=Decimal("0.05"),
                variant="480p",
                sku="cents_per_video_output_second_480p",
            ),
        ),
        (
            "cents_per_second_video_continuation_720p",
            "41",
            PriceLine(
                billable="continuation",
                unit="second",
                usd=Decimal("0.41"),
                variant="720p",
                sku="cents_per_second_video_continuation_720p",
            ),
        ),
        (
            "minimum_cents_per_generation",
            "56",
            PriceLine(
                billable="minimum",
                unit="generation",
                usd=Decimal("0.56"),
                sku="minimum_cents_per_generation",
            ),
        ),
        (
            "cents_per_image_input",
            "0.2",
            PriceLine(
                billable="input_image",
                unit="image",
                usd=Decimal("0.002"),
                sku="cents_per_image_input",
            ),
        ),
        (
            "reference_images",
            "0.04",
            PriceLine(
                billable="input_reference",
                unit="image",
                usd=Decimal("0.04"),
                sku="reference_images",
            ),
        ),
        (
            "video_tokens_1080p_with_video_input",
            "0.0000047",
            PriceLine(
                billable="output_video",
                unit="video_token",
                usd=Decimal("0.0000047"),
                variant="1080p_with_video_input",
                sku="video_tokens_1080p_with_video_input",
            ),
        ),
        (
            "cents_per_megapixel_second_precise",
            "7.5",
            PriceLine(
                billable="output_video",
                unit="megapixel_second",
                usd=Decimal("0.075"),
                variant="precise",
                sku="cents_per_megapixel_second_precise",
            ),
        ),
        (
            "per_banana_output",
            "1",
            PriceLine(
                billable="unknown", unit="unknown", usd=Decimal("1"), sku="per_banana_output"
            ),
        ),
    ],
)
def test_a_pricing_sku_parses_into_its_unit_variant_and_dollars(
    sku: str, value: str, expected: PriceLine
) -> None:
    assert parse_video_skus({sku: value}) == [expected]


def test_every_recorded_video_sku_is_recognised() -> None:
    unknown = [
        (model["id"], line.sku)
        for model in body("videos_models.json")["data"]
        for line in parse_video_skus(model["pricing_skus"] or {})
        if line.unit == "unknown"
    ]

    assert unknown == []


def test_an_unknown_descriptor_type_is_absent_not_guessed() -> None:
    payload = {
        "data": [
            {
                "id": "acme/x",
                "architecture": {"input_modalities": ["text"]},
                "supported_parameters": {"style": {"type": "freeform"}},
            }
        ]
    }

    (record,) = normalise_image_models(payload)

    assert record.params == {}


# ---------------------------------------------------------------------------
# an unpinned choice: every endpoint must accept it
# ---------------------------------------------------------------------------


def _endpoint(tag: str, params: dict[str, Descriptor], usd: str) -> CapabilityRecord:
    return CapabilityRecord(
        modality="image",
        model_id="acme/two-endpoints",
        provider_tag=tag,
        params=params,
        pricing=[PriceLine(billable="output_image", unit="image", usd=Decimal(usd))],
        input_modalities=["text", "image"],
    )


def test_an_unpinned_record_is_what_every_endpoint_accepts_at_the_highest_price() -> None:
    a = _endpoint(
        "a",
        {
            "aspect_ratio": Descriptor(kind="enum", values=["1:1", "16:9", "9:16"]),
            "n": Descriptor(kind="range", min=1, max=4),
            "seed": Descriptor(kind="boolean"),
            "quality": Descriptor(kind="enum", values=["low"]),
        },
        "0.04",
    )
    b = _endpoint(
        "b",
        {
            "aspect_ratio": Descriptor(kind="enum", values=["16:9", "1:1"]),
            "n": Descriptor(kind="range", min=2, max=10),
            "quality": Descriptor(kind="enum", values=["high"]),
        },
        "0.05",
    )

    merged = intersect_endpoints([a, b])

    assert merged.provider_tag is None
    assert merged.params == {
        "aspect_ratio": Descriptor(kind="enum", values=["1:1", "16:9"]),
        "n": Descriptor(kind="range", min=2, max=4),
    }
    assert merged.pricing == [PriceLine(billable="output_image", unit="image", usd=Decimal("0.05"))]


# ---------------------------------------------------------------------------
# the cache
# ---------------------------------------------------------------------------


@pytest.fixture
def router() -> Iterator[respx.Router]:
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        yield mock


@pytest.fixture
def clock() -> Clock:
    return Clock()


async def _catalogue(clock: Clock, redis: FakeRedis) -> tuple[MediaCatalogue, httpx.AsyncClient]:
    client = httpx.AsyncClient()
    return MediaCatalogue(client=client, redis=redis, base_url=BASE, clock=clock), client


async def test_a_read_is_cached_for_ten_minutes_under_its_catalogue_hash(
    router: respx.Router, clock: Clock
) -> None:
    routes = mock_catalogue(router)
    redis = FakeRedis(clock)
    catalogue, client = await _catalogue(clock, redis)

    first = await catalogue.fetch_video_models()
    clock.advance(minutes=9, seconds=59)
    again = await catalogue.fetch_video_models()
    clock.advance(seconds=2)
    refreshed = await catalogue.fetch_video_models()
    await client.aclose()

    assert routes["videos_models"].call_count == 2
    assert first.catalogue_hash == again.catalogue_hash == catalogue_hash(list(first.records))
    assert first.warning is None and again.warning is None and refreshed.warning is None
    assert again.fetched_at == first.fetched_at


async def test_a_failed_fetch_serves_the_last_good_snapshot_with_a_warning(
    router: respx.Router, clock: Clock
) -> None:
    routes = mock_catalogue(router)
    redis = FakeRedis(clock)
    catalogue, client = await _catalogue(clock, redis)
    good = await catalogue.fetch_image_models()

    routes["images_models"].mock(return_value=httpx.Response(503))
    clock.advance(hours=23, minutes=59)
    served = await catalogue.fetch_image_models()
    await client.aclose()

    assert served.catalogue_hash == good.catalogue_hash
    assert served.fetched_at == good.fetched_at
    assert served.warning is not None and "503" in served.warning and "23 h" in served.warning


async def test_a_last_good_snapshot_older_than_a_day_is_refused(
    router: respx.Router, clock: Clock
) -> None:
    routes = mock_catalogue(router)
    catalogue, client = await _catalogue(clock, FakeRedis(clock))
    await catalogue.fetch_image_models()

    routes["images_models"].mock(side_effect=httpx.ConnectError("down"))
    clock.advance(hours=24, seconds=1)
    with pytest.raises(CatalogueUnavailable, match="older than 24 h"):
        await catalogue.fetch_image_models()
    await client.aclose()


async def test_no_snapshot_at_all_is_refused(router: respx.Router, clock: Clock) -> None:
    router.get(f"{BASE}/images/models").mock(return_value=httpx.Response(200, text="<html>"))
    catalogue, client = await _catalogue(clock, FakeRedis(clock))

    with pytest.raises(CatalogueUnavailable, match="no last-good snapshot"):
        await catalogue.fetch_image_models()
    await client.aclose()


async def test_an_unknown_model_has_no_endpoints_and_that_answer_is_not_a_failure(
    router: respx.Router, clock: Clock
) -> None:
    mock_catalogue(router)
    catalogue, client = await _catalogue(clock, FakeRedis(clock))

    snapshot = await catalogue.fetch_image_endpoints("acme/no-such-model")
    await client.aclose()

    assert snapshot.records == () and snapshot.warning is None


async def test_record_for_resolves_pinned_unpinned_video_and_missing(
    router: respx.Router, clock: Clock
) -> None:
    mock_catalogue(router)
    catalogue, client = await _catalogue(clock, FakeRedis(clock))

    pinned = await catalogue.record_for("image", FLUX, "black-forest-labs")
    unpinned = await catalogue.record_for("image", GEMINI, None)
    wrong_tag = await catalogue.record_for("image", FLUX, "nobody")
    video = await catalogue.record_for("video", VEO, None)
    missing = await catalogue.record_for("image", "acme/no-such-model", None)
    await client.aclose()

    assert pinned is not None and pinned.provider_tag == "black-forest-labs"
    assert pinned.pricing[0].unit == "megapixel"
    assert unpinned is not None and unpinned.provider_tag is None
    # Four gemini endpoints; the unpinned price is the dearest of them.
    assert [p.usd for p in unpinned.pricing if p.billable == "output_image"] == [
        Decimal("0.000054")
    ]
    assert wrong_tag is None
    assert video is not None and video.model_id == VEO
    assert missing is None
    assert capability_hash(pinned) != capability_hash(unpinned)  # type: ignore[arg-type]
