"""Stage 04's four task classes (PRD §7.1, §9.6; law 36).

The CI assertion §7.1 asks for is `test_complete_structured_refuses_image_gen`:
IMAGE_GEN and VIDEO_GEN are routed only through `media/`, and a text gateway
that accepted one would be a second, unvalidated door to a media model — no
capability check, no budget reservation, no G7 spend gate.
"""

from __future__ import annotations

import httpx
import pytest
from pydantic import BaseModel

from agent.llm.gateway import LLMGateway, MediaTaskClassError, RateLimiter
from agent.llm.ledger import ModelCatalogue
from agent.llm.router import (
    MEDIA_TASK_CLASSES,
    ModelChoice,
    ModelRouter,
    ModelRoutingError,
    TaskClass,
    validate_overrides,
)
from tests.openrouter_fake import FakeOpenRouter

BASE_URL = "https://openrouter.test/api/v1"


class _Out(BaseModel):
    text: str


def test_the_four_stage_04_classes_exist() -> None:
    assert TaskClass("copywrite") is TaskClass.COPYWRITE
    assert TaskClass("vision") is TaskClass.VISION
    assert TaskClass("image_gen") is TaskClass.IMAGE_GEN
    assert TaskClass("video_gen") is TaskClass.VIDEO_GEN
    assert frozenset({TaskClass.IMAGE_GEN, TaskClass.VIDEO_GEN}) == MEDIA_TASK_CLASSES


def test_copywrite_seeds_opus_at_the_constants_temperature() -> None:
    choice = ModelRouter().choose(TaskClass.COPYWRITE)
    assert choice.primary == "anthropic/claude-opus-4.6"
    # `copy.temperature_copywrite` in creative_constants.yaml, never a literal.
    assert choice.temperature == 0.7


def test_a_project_override_of_the_constant_moves_the_temperature() -> None:
    router = ModelRouter.resolve(
        project_settings={"creative_overrides": {"copy.temperature_copywrite": 0.2}}
    )
    assert router.choose(TaskClass.COPYWRITE).temperature == 0.2
    # Nothing else moves with it.
    assert router.choose(TaskClass.SYNTHESIZE).temperature == 0.3


def test_vision_seeds_gemini_flash() -> None:
    assert ModelRouter().choose(TaskClass.VISION).primary == "google/gemini-2.5-flash"


@pytest.mark.parametrize("task_class", sorted(MEDIA_TASK_CLASSES))
def test_media_classes_have_no_seed_and_cannot_be_routed(task_class: TaskClass) -> None:
    with pytest.raises(ModelRoutingError, match="user-selected"):
        ModelRouter().choose(task_class)


@pytest.mark.parametrize("key", ["image_gen", "video_gen"])
def test_a_media_class_cannot_be_given_a_text_override(key: str) -> None:
    with pytest.raises(ModelRoutingError, match="media/"):
        validate_overrides({key: "google/gemini-2.5-flash-image"})


def test_a_vision_override_is_refused_until_modalities_can_be_checked() -> None:
    """§9.6: the router rejects a VISION model lacking image input. Nothing
    here can see `input_modalities` yet, so the override fails closed."""
    with pytest.raises(ModelRoutingError, match="image input"):
        validate_overrides({"vision": "openai/gpt-5.2"})
    with pytest.raises(ModelRoutingError, match="image input"):
        ModelRouter.resolve(workspace_settings={"models": {"vision": "openai/gpt-5.2"}})


def test_text_overrides_for_the_new_classes_are_accepted() -> None:
    assert validate_overrides({"copywrite": "openai/gpt-5.2"}) == {
        TaskClass.COPYWRITE: "openai/gpt-5.2"
    }


@pytest.mark.parametrize("task_class", sorted(MEDIA_TASK_CLASSES))
async def test_complete_structured_refuses_image_gen(task_class: TaskClass) -> None:
    fake = FakeOpenRouter()
    client = fake.client()
    gateway = LLMGateway(
        client=client,
        api_key="sk-or-test",
        catalogue=ModelCatalogue(client, base_url=BASE_URL, api_key="sk-or-test"),
        base_url=BASE_URL,
        limiter=RateLimiter(rate=1000, concurrency=8),
    )
    # Built by hand: the router already refuses, and the gateway must not
    # depend on the router having been asked.
    choice = ModelChoice(
        task_class=task_class, chain=("google/gemini-2.5-flash-image",), temperature=0, top_p=1
    )
    try:
        with pytest.raises(MediaTaskClassError, match="media/"):
            await gateway.complete_structured(
                output_model=_Out, system="s", user="u", choice=choice
            )
    finally:
        await client.aclose()
    # Refused before a single byte left the process.
    assert fake.requests == []


def test_the_run_estimate_only_prices_text_classes() -> None:
    from agent.llm.estimate import ASSUMED

    assert not MEDIA_TASK_CLASSES & set(ASSUMED)


def test_httpx_is_the_only_transport_type_used() -> None:
    # A guard that the fake above is the transport under test, not a real one.
    assert isinstance(FakeOpenRouter().client(), httpx.AsyncClient)
