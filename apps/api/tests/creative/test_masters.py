"""4.4.2 — `creative/masters.py`: distinct candidates under Law 37, the master ratio,
the retry's strengthened prompt, VISION's schema, and images reaching a vision model."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from pydantic import ValidationError

from agent.creative.concepts import STRENGTHENED_NEGATIVES
from agent.creative.masters import (
    candidate_requests,
    derive_seed,
    master_ratio,
    ranking_model,
    request_prompt,
)
from agent.llm.gateway import InlineImage, _render_prompt
from agent.media.types import CapabilityRecord, Descriptor, PriceLine
from agent.schemas.creative_media import Concept

RUN = uuid.UUID("00000000-0000-4000-8000-0000000000aa")


def _model(**params: Descriptor) -> CapabilityRecord:
    return CapabilityRecord(
        modality="image",
        model_id="vendor/model",
        params={"aspect_ratio": Descriptor(kind="enum", values=["1:1", "16:9", "auto"]), **params},
        pricing=[PriceLine(billable="output_image", unit="image", usd=Decimal("0.01"))],
    )


def _requests(
    capability: CapabilityRecord, count: int = 2, attempt: int = 1
) -> list[dict[str, int]]:
    return candidate_requests(capability, count, run_id=RUN, concept_id="c:1", attempt=attempt)


def test_a_model_taking_n_is_asked_once_for_every_candidate() -> None:
    assert _requests(_model(n=Descriptor(kind="range", min=1, max=4))) == [{"n": 2}]


def test_a_model_taking_a_seed_is_asked_once_per_candidate_with_distinct_seeds() -> None:
    seed = Descriptor(kind="range", min=0, max=1000)
    requests = _requests(_model(seed=seed))
    assert len(requests) == 2
    assert requests[0]["seed"] != requests[1]["seed"]
    assert all(0 <= r["seed"] <= 1000 for r in requests)


def test_seeds_are_stable_across_a_resume_and_differ_on_the_retry() -> None:
    capability = _model(seed=Descriptor(kind="range", min=0, max=2**31 - 1))
    assert _requests(capability) == _requests(capability)
    assert _requests(capability, attempt=1) != _requests(capability, attempt=2)


def test_n_too_small_falls_back_to_seeds() -> None:
    capability = _model(
        n=Descriptor(kind="range", min=1, max=1), seed=Descriptor(kind="range", min=0, max=99)
    )
    assert [set(r) for r in _requests(capability)] == [{"seed"}, {"seed"}]


def test_a_model_taking_neither_makes_one_distinct_candidate() -> None:
    # Identical requests are one GenerationJob under Law 37 — asking twice
    # would return the same job, so it is asked once.
    assert _requests(_model()) == [{}]


def test_derive_seed_stays_inside_the_descriptor() -> None:
    descriptor = Descriptor(kind="range", min=10, max=12)
    seeds = {
        derive_seed(descriptor, run_id=RUN, concept_id="c", attempt=1, index=i) for i in range(50)
    }
    assert seeds <= {10, 11, 12}


def test_the_master_ratio_is_the_first_required_ratio_the_model_paints() -> None:
    capability = _model()
    assert master_ratio(["1.91:1", "1:1"], capability) == "1:1"
    assert master_ratio(["4:5"], capability) is None


def _concept() -> Concept:
    return Concept(
        id="c-sds:c1",
        campaign_ref="c-sds",
        name="Bench",
        angle="angle",
        angle_text="Compliance without the binder",
        rationale="Order reads as control.",
        subject="a bench",
        setting="a lab",
        composition_by_ratio={"1:1": "bench centred."},
        product_depiction="none",
        prompt="A tidy bench. Avoid: no text, no logos, no watermark.",
        negative_constraints=["no text, no logos, no watermark"],
        surfaces=["search_image"],
    )


def test_the_request_prompt_keeps_every_negative_and_adds_the_ratios_composition() -> None:
    prompt = request_prompt(_concept(), "1:1", strengthened=False)
    assert prompt.startswith(_concept().prompt)
    assert "Composition at 1:1: bench centred." in prompt
    assert STRENGTHENED_NEGATIVES[0] not in prompt


def test_the_retry_adds_the_strengthened_negatives() -> None:
    prompt = request_prompt(_concept(), None, strengthened=True)
    assert all(item in prompt for item in STRENGTHENED_NEGATIVES)
    assert "no text, no logos, no watermark" in prompt


def test_vision_may_only_name_candidates_that_were_shown() -> None:
    schema = ranking_model(["c1", "c2"])
    schema.model_validate(
        {"best": "c2", "why": "Cleaner.", "notes": [{"candidate": "c1", "note": "ok", "flags": []}]}
    )
    with pytest.raises(ValidationError):
        schema.model_validate({"best": "c3", "why": "?", "notes": []})
    with pytest.raises(ValidationError):
        schema.model_validate(
            {
                "best": "c1",
                "why": "?",
                "notes": [{"candidate": "c1", "note": "x", "flags": ["nsfw"]}],
            }
        )


def test_images_reach_the_model_but_never_the_stored_prompt() -> None:
    from agent.llm.gateway import LLMGateway, Strategy

    gateway = LLMGateway.__new__(LLMGateway)
    messages = gateway._messages(
        model="google/gemini-2.5-flash",
        system="rank",
        user="CANDIDATES: c1",
        strategy=Strategy.STRICT_SCHEMA,
        schema={},
        images=[InlineImage(media_type="image/png", data=b"CANDIDATE-BYTES")],
    )
    content = messages[1]["content"]
    assert content[0] == {"type": "text", "text": "CANDIDATES: c1"}
    assert content[1]["image_url"]["url"] == "data:image/png;base64,Q0FORElEQVRFLUJZVEVT"
    assert "Q0FORElEQVRFLUJZVEVT" not in _render_prompt(messages)


def test_a_text_only_call_is_unchanged() -> None:
    from agent.llm.gateway import LLMGateway, Strategy

    gateway = LLMGateway.__new__(LLMGateway)
    messages = gateway._messages(
        model="google/gemini-2.5-flash",
        system="s",
        user="u",
        strategy=Strategy.STRICT_SCHEMA,
        schema={},
    )
    assert messages[1] == {"role": "user", "content": "u"}
