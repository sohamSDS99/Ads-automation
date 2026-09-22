"""The object that crosses into Stage 03 (PRD §4.5).

Stage 02's `PlanInput` carries one required upstream. This one carries six
optional ones, and every test here is about the *absence* of them being a
first-class state rather than a degraded one (law 21).
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from agent.schemas.guideline_input import (
    GuidelineBindings,
    GuidelineInput,
    GuidelineMode,
)

PROJECT = uuid.UUID("11111111-1111-1111-1111-111111111111")
RUN = uuid.UUID("22222222-2222-2222-2222-222222222222")
RESEARCH = uuid.UUID("33333333-3333-3333-3333-333333333333")
PLAN = uuid.UUID("44444444-4444-4444-4444-444444444444")


def _input(bindings: GuidelineBindings, **kwargs: object) -> GuidelineInput:
    fields: dict[str, object] = {
        "project_id": PROJECT,
        "guideline_run_id": RUN,
        "bindings": bindings,
        "constants_version": "2026.09.1",
    }
    fields.update(kwargs)
    return GuidelineInput(**fields)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# mode is derived, never asserted by the caller
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bindings", "expected"),
    [
        (GuidelineBindings(), GuidelineMode.STANDALONE),
        (GuidelineBindings(research_run_id=RESEARCH), GuidelineMode.RESEARCH_LINKED),
        (GuidelineBindings(plan_id=PLAN), GuidelineMode.PLAN_LINKED),
        (
            GuidelineBindings(research_run_id=RESEARCH, plan_id=PLAN),
            GuidelineMode.FULLY_LINKED,
        ),
    ],
)
def test_mode_follows_the_bindings(bindings: GuidelineBindings, expected: GuidelineMode) -> None:
    assert bindings.mode is expected
    assert _input(bindings).mode is expected


def test_a_claimed_mode_that_contradicts_the_bindings_is_rejected() -> None:
    """PRD §4.5 rule 3. A client that says `fully_linked` with no plan is wrong,
    and being wrong here would mis-describe what the rulebook was built from."""
    with pytest.raises(ValidationError, match="mode"):
        _input(GuidelineBindings(), mode=GuidelineMode.FULLY_LINKED)


def test_plan_linked_without_research_is_legal() -> None:
    """Bindings are additive and independent (§4.2). A frozen plan already
    carries its own research pointer; Stage 03 does not need the acceptance."""
    assert _input(GuidelineBindings(plan_id=PLAN)).mode is GuidelineMode.PLAN_LINKED


# ---------------------------------------------------------------------------
# the standalone path is first-class
# ---------------------------------------------------------------------------


def test_a_standalone_input_needs_no_optional_field() -> None:
    """The headline of the whole stage: this must construct."""
    built = _input(GuidelineBindings())
    assert built.compliance_guardrails is None
    assert built.differentiation_claim is None
    assert built.competitor_creative is None
    assert built.channel_slate is None
    assert built.account_structure is None
    assert built.measurement_consent is None
    assert built.mode is GuidelineMode.STANDALONE


def test_every_binding_derived_field_defaults_to_none() -> None:
    """A node must be able to branch on `is None`, so no optional input may
    default to an empty collection that reads as "we looked and found none"."""
    built = _input(GuidelineBindings())
    optional = {
        name
        for name, field in GuidelineInput.model_fields.items()
        if name
        in {
            "compliance_guardrails",
            "differentiation_claim",
            "competitor_creative",
            "channel_slate",
            "account_structure",
            "measurement_consent",
            "signoff_matrix",
            "prior_guideline_id",
        }
    }
    assert len(optional) == 8
    for name in optional:
        assert getattr(built, name) is None, name


def test_unbound_inputs_is_recorded() -> None:
    built = _input(GuidelineBindings(), unbound_inputs=["research", "plan"])
    assert built.unbound_inputs == ["research", "plan"]


# ---------------------------------------------------------------------------
# the hash
# ---------------------------------------------------------------------------


def test_the_hash_is_stable_across_instances() -> None:
    assert _input(GuidelineBindings()).content_hash() == _input(GuidelineBindings()).content_hash()


def test_the_hash_moves_with_the_constants_version() -> None:
    """A constants bump must re-run the deterministic nodes rather than reuse a
    cached output shaped by the old thresholds (PRD §8.3)."""
    a = _input(GuidelineBindings())
    b = _input(GuidelineBindings(), constants_version="2026.10.1")
    assert a.content_hash() != b.content_hash()


def test_the_hash_moves_with_the_bindings() -> None:
    assert (
        _input(GuidelineBindings()).content_hash()
        != _input(GuidelineBindings(research_run_id=RESEARCH)).content_hash()
    )


def test_the_input_is_frozen() -> None:
    """Assembled once at run start and passed read-only to every node."""
    built = _input(GuidelineBindings())
    with pytest.raises(ValidationError):
        built.mode = GuidelineMode.FULLY_LINKED  # type: ignore[misc]
