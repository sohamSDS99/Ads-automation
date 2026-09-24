"""The Stage 04 crossing contracts (PRD §4.3): closed, frozen and hashable."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from agent.export.guideline_contract import VisualIdentity, VoiceProfile
from agent.schemas.creative_input import (
    CompetitorRules,
    CreativeContext,
    CreativeScope,
    LexiconGuidance,
    PersonalizationRules,
    canonical_hash,
)


def _context(**overrides: object) -> CreativeContext:
    fields: dict[str, object] = {
        "guideline_id": uuid.UUID(int=1),
        "guideline_version": "1.0",
        "ruleset_version": "v1.0",
        "voice": VoiceProfile(voice_words=["plain"]),
        "lexicon_guidance": LexiconGuidance(),
        "visual_identity": VisualIdentity(),
        "competitor_rules": CompetitorRules(allowed=False),
        "personalization_rules": PersonalizationRules(),
        "hash": "pending",
    }
    fields.update(overrides)
    return CreativeContext.model_validate(fields)


def test_canonical_hash_ignores_key_order() -> None:
    assert canonical_hash({"a": 1, "b": {"y": 2, "x": 1}}) == canonical_hash(
        {"b": {"x": 1, "y": 2}, "a": 1}
    )


def test_the_context_hash_excludes_the_hash_field() -> None:
    one = _context(hash="x")
    two = _context(hash="y")
    assert one.computed_hash() == two.computed_hash()
    assert one.computed_hash() != _context(ruleset_version="v1.1").computed_hash()


def test_free_form_upstream_sections_ride_along_verbatim() -> None:
    assert _context().competitor_rules.model_dump() == {"allowed": False}


def test_the_contracts_are_closed_and_frozen() -> None:
    with pytest.raises(ValidationError):
        CreativeScope(images=False, video=False, concepts_per_campaign=2, extra=1)  # type: ignore[call-arg]
    scope = CreativeScope(images=False, video=False, concepts_per_campaign=3)
    with pytest.raises(ValidationError):
        scope.images = True  # type: ignore[misc]


def test_concepts_per_campaign_is_two_or_three() -> None:
    with pytest.raises(ValidationError):
        CreativeScope(images=False, video=False, concepts_per_campaign=4)  # type: ignore[arg-type]
