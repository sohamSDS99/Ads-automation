"""`images.py`: POST /api/v1/images with only validated fields (PRD §9.1 item 3).

A failed generation is a 502 and is unbilled, so it is retried (three times,
with backoff); any 4xx is the provider's answer and is raised with its body
verbatim. Every case replays a recorded response.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from decimal import Decimal

import httpx
import pytest
import respx

from agent.llm.ledger import RunLedger, Usage
from agent.media.capability import CapabilityUnsupported
from agent.media.http import MediaApi, ProviderRejected, ProviderUnavailable
from agent.media.images import ImageClient, wire_image
from agent.media.types import (
    CapabilityRecord,
    Descriptor,
    ImageRequest,
    PriceLine,
    ProviderPreferences,
    ReferenceImage,
)
from tests.media.openrouter_mock import BASE, FLUX, body, response

KEY = "sk-or-v1-canary-images-0123456789"


def flux(provider_tag: str | None = "black-forest-labs") -> CapabilityRecord:
    return CapabilityRecord(
        modality="image",
        model_id=FLUX,
        provider_tag=provider_tag,
        params={
            "aspect_ratio": Descriptor(kind="enum", values=["1:1", "16:9", "9:16", "7:3"]),
            "output_format": Descriptor(kind="enum", values=["png", "jpeg"]),
            "n": Descriptor(kind="range", min=1, max=1),
            "input_references": Descriptor(kind="range", min=0, max=4),
            "seed": Descriptor(kind="boolean"),
        },
        pricing=[PriceLine(billable="output_image", unit="megapixel", usd=Decimal("0.014"))],
        input_modalities=["text", "image"],
    )


def request(**fields: object) -> ImageRequest:
    return ImageRequest.model_validate(
        {
            "model": FLUX,
            "prompt": "a plain ceramic mug on a wooden desk by a window, soft morning light",
            "aspect_ratio": "16:9",
            "n": 1,
            "output_format": "jpeg",
            **fields,
        }
    )


class CountingLedger(RunLedger):
    """A real ledger that also counts entries, so "one entry" is observable."""

    entries: int = 0

    def record(self, *, usage: Usage, cost: Decimal) -> None:
        self.entries += 1
        super().record(usage=usage, cost=cost)


@pytest.fixture
def router() -> Iterator[respx.Router]:
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        yield mock


@pytest.fixture
async def images() -> ImageClient:
    sleeps: list[float] = []

    async def no_wait(seconds: float) -> None:
        sleeps.append(seconds)

    client = ImageClient(
        MediaApi(client=httpx.AsyncClient(), api_key=KEY, base_url=BASE, referer="https://app"),
        sleep=no_wait,
    )
    client.sleeps = sleeps  # type: ignore[attr-defined]
    return client


async def test_a_generation_decodes_every_image_and_prices_it_from_usage(
    router: respx.Router, images: ImageClient
) -> None:
    route = router.post(f"{BASE}/images").mock(return_value=response("image_generate.json"))
    ledger = CountingLedger(cap_usd=Decimal(50))

    result = await images.generate(request(), capability=flux(), ledger=ledger)

    recorded = body("image_generate.json")
    assert len(result.images) == 1
    assert result.images[0].data == base64.b64decode(recorded["data"][0]["b64_json"])
    assert result.images[0].media_type == "image/jpeg"
    assert result.cost_usd == Decimal("0.015")
    assert (ledger.entries, ledger.spent_usd) == (1, Decimal("0.015"))
    assert ledger.usage == Usage(prompt_tokens=17, completion_tokens=7291)
    assert route.call_count == 1


async def test_only_validated_fields_go_on_the_wire_with_the_gateway_headers(
    router: respx.Router, images: ImageClient
) -> None:
    route = router.post(f"{BASE}/images").mock(return_value=response("image_generate.json"))

    await images.generate(request(seed=7), capability=flux())

    sent = route.calls.last.request
    assert json.loads(sent.content) == {
        "model": FLUX,
        "prompt": "a plain ceramic mug on a wooden desk by a window, soft morning light",
        "aspect_ratio": "16:9",
        "n": 1,
        "output_format": "jpeg",
        "seed": 7,
        # A pinned provider is pinned: no fallback to another endpoint.
        "provider": {"only": ["black-forest-labs"], "allow_fallbacks": False},
    }
    assert sent.headers["authorization"] == f"Bearer {KEY}"
    assert sent.headers["x-title"] == "Paid Ads Research Agent"
    assert sent.headers["http-referer"] == "https://app"


async def test_an_unpinned_choice_sends_no_provider_preferences(
    router: respx.Router, images: ImageClient
) -> None:
    route = router.post(f"{BASE}/images").mock(return_value=response("image_generate.json"))

    await images.generate(request(), capability=flux(provider_tag=None))

    assert "provider" not in json.loads(route.calls.last.request.content)


async def test_502_502_200_is_one_ledger_entry_and_one_image(
    router: respx.Router, images: ImageClient
) -> None:
    route = router.post(f"{BASE}/images").mock(
        side_effect=[httpx.Response(502), httpx.Response(502), response("image_generate.json")]
    )
    ledger = CountingLedger(cap_usd=Decimal(50))

    result = await images.generate(request(), capability=flux(), ledger=ledger)

    assert route.call_count == 3
    assert len(result.images) == 1
    assert (ledger.entries, ledger.spent_usd) == (1, Decimal("0.015"))
    assert result.attempts == 3
    assert len(images.sleeps) == 2  # type: ignore[attr-defined]
    assert images.sleeps[0] < images.sleeps[1]  # type: ignore[attr-defined]


async def test_a_502_is_retried_three_times_and_then_given_up_unbilled(
    router: respx.Router, images: ImageClient
) -> None:
    route = router.post(f"{BASE}/images").mock(return_value=httpx.Response(502))
    ledger = CountingLedger(cap_usd=Decimal(50))

    with pytest.raises(ProviderUnavailable, match="502"):
        await images.generate(request(), capability=flux(), ledger=ledger)

    assert route.call_count == 4
    assert ledger.entries == 0


@pytest.mark.parametrize("status", [500, 503, 504])
async def test_another_5xx_is_not_retried_because_only_502_is_known_unbilled(
    router: respx.Router, images: ImageClient, status: int
) -> None:
    route = router.post(f"{BASE}/images").mock(return_value=httpx.Response(status))

    with pytest.raises(ProviderUnavailable, match=str(status)):
        await images.generate(request(), capability=flux())

    assert route.call_count == 1


async def test_a_transport_error_is_raised_not_retried(
    router: respx.Router, images: ImageClient
) -> None:
    route = router.post(f"{BASE}/images").mock(side_effect=httpx.ReadTimeout("slow"))

    with pytest.raises(ProviderUnavailable, match="ReadTimeout"):
        await images.generate(request(), capability=flux())

    assert route.call_count == 1


async def test_a_4xx_raises_provider_rejected_with_the_body_verbatim(
    router: respx.Router, images: ImageClient
) -> None:
    # The capability record here (wrongly) allows 7:3, so the request passes our
    # check and reaches OpenRouter — which is how a catalogue that drifted
    # between selection and submit looks. OpenRouter's own 400 comes back.
    route = router.post(f"{BASE}/images").mock(
        return_value=response("image_unsupported_aspect_ratio.json")
    )

    with pytest.raises(ProviderRejected) as refused:
        await images.generate(request(aspect_ratio="7:3"), capability=flux())

    assert refused.value.status == 400
    assert refused.value.body == body("image_unsupported_aspect_ratio.json")
    assert refused.value.code == "provider_rejected"
    assert route.call_count == 1


async def test_a_model_the_provider_does_not_have_is_model_unavailable_after_one_request(
    router: respx.Router, images: ImageClient
) -> None:
    route = router.post(f"{BASE}/images").mock(return_value=response("image_unknown_model.json"))

    with pytest.raises(ProviderRejected) as refused:
        await images.generate(request(), capability=flux())

    assert refused.value.code == "model_unavailable"
    assert route.call_count == 1
    assert json.loads(route.calls.last.request.content)["model"] == FLUX


async def test_an_unsupported_field_is_our_422_and_nothing_reaches_the_mock(
    router: respx.Router, images: ImageClient
) -> None:
    route = router.post(f"{BASE}/images").mock(return_value=response("image_generate.json"))

    with pytest.raises(CapabilityUnsupported) as refused:
        await images.generate(request(aspect_ratio="4:5", quality="high"), capability=flux())

    assert [(e.field, e.supported) for e in refused.value.errors] == [
        ("aspect_ratio", ["1:1", "16:9", "9:16", "7:3"]),
        ("quality", []),
    ]
    assert route.call_count == 0
    assert len(router.calls) == 0


def test_references_go_as_base64_data_urls_on_the_wire_only() -> None:
    reference = ReferenceImage(sha256="ab" * 32, media_type="image/png", data=b"\x89PNG-bytes")

    wire = wire_image(request(input_references=[reference]))

    assert wire["input_references"] == [
        {
            "type": "image_url",
            "image_url": {
                "url": "data:image/png;base64," + base64.b64encode(b"\x89PNG-bytes").decode()
            },
        }
    ]


def test_provider_preferences_on_the_wire_drop_unset_keys() -> None:
    wire = wire_image(request(provider=ProviderPreferences(order=["a", "b"])))

    assert wire["provider"] == {"order": ["a", "b"]}
