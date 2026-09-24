"""What "Check again" can act on (Stage 04 PRD §16, §18; law 37; S4-P18).

The Jobs tab offers the control only where `MediaJobs.check()` would change
something, and the route refuses the rest with a 409 — one predicate for both.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from agent.db.models import GenerationJob, GenerationModality, GenerationStatus
from agent.media.jobs import checkable
from agent.schemas.creative_input import MediaModelChoice

MODEL = "black-forest-labs/flux.2-pro"
HASH = "c" * 64


def _choice(**overrides: Any) -> MediaModelChoice:
    fields: dict[str, Any] = {
        "modality": "image",
        "model_id": MODEL,
        "capability": {},
        "capability_hash": HASH,
    }
    return MediaModelChoice.model_validate({**fields, **overrides})


def _job(status: GenerationStatus, modality: GenerationModality, **overrides: Any) -> GenerationJob:
    fields: dict[str, Any] = {
        "id": uuid.uuid4(),
        "status": status,
        "modality": modality,
        "model_id": MODEL,
        "capability_hash": HASH,
        "request": {"model": MODEL, "prompt": "A stack of safety data sheets"},
        "openrouter_job_id": None,
    }
    return GenerationJob(**{**fields, **overrides})


def test_a_timed_out_video_with_a_known_id_is_re_polled() -> None:
    row = _job(GenerationStatus.TIMED_OUT, GenerationModality.VIDEO, openrouter_job_id="vid-1")
    assert checkable(row, None)


def test_a_timed_out_job_with_no_provider_id_has_nothing_to_poll() -> None:
    assert not checkable(_job(GenerationStatus.TIMED_OUT, GenerationModality.VIDEO), None)


def test_an_image_in_unknown_submit_state_is_re_submitted_with_its_own_choice() -> None:
    row = _job(GenerationStatus.UNKNOWN_SUBMIT_STATE, GenerationModality.IMAGE)
    assert checkable(row, _choice())


@pytest.mark.parametrize(
    "choice",
    [None, _choice(capability_hash="d" * 64), _choice(model_id="openai/gpt-image-1")],
    ids=["no-choice", "other-capability", "other-model"],
)
def test_an_image_is_never_re_submitted_against_a_different_capability(
    choice: MediaModelChoice | None,
) -> None:
    row = _job(GenerationStatus.UNKNOWN_SUBMIT_STATE, GenerationModality.IMAGE)
    assert not checkable(row, choice)


def test_an_image_with_references_waits_for_their_bytes() -> None:
    row = _job(
        GenerationStatus.UNKNOWN_SUBMIT_STATE,
        GenerationModality.IMAGE,
        request={
            "model": MODEL,
            "prompt": "A stack of safety data sheets",
            "input_references": [{"sha256": "e" * 64, "media_type": "image/png"}],
        },
    )
    assert not checkable(row, _choice())


def test_a_video_in_unknown_submit_state_is_never_re_posted() -> None:
    row = _job(GenerationStatus.UNKNOWN_SUBMIT_STATE, GenerationModality.VIDEO)
    assert not checkable(row, _choice(modality="video"))


@pytest.mark.parametrize(
    "status",
    [
        GenerationStatus.QUEUED,
        GenerationStatus.SUBMITTED,
        GenerationStatus.IN_PROGRESS,
        GenerationStatus.COMPLETED,
        GenerationStatus.FAILED,
        GenerationStatus.BLOCKED_BY_BUDGET,
    ],
)
def test_every_other_state_is_not_checkable(status: GenerationStatus) -> None:
    assert not checkable(_job(status, GenerationModality.VIDEO, openrouter_job_id="vid-1"), None)
