"""The project request models, where a typo is cheapest to catch."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent.api.schemas_projects import (
    SETTINGS_MODELS,
    CreateProjectRequest,
    ModelRouting,
    UpdateProjectRequest,
)


@pytest.mark.parametrize(
    ("typed", "stored"),
    [
        ("sdsmanager.com", "sdsmanager.com"),
        ("https://sdsmanager.com/en/", "sdsmanager.com"),
        ("http://www.sdsmanager.com", "sdsmanager.com"),
        ("  SDSManager.com  ", "sdsmanager.com"),
        ("sdsmanager.com/", "sdsmanager.com"),
    ],
)
def test_a_pasted_url_is_stored_as_a_hostname(typed: str, stored: str) -> None:
    """Two projects for one brand would break every comparison done by domain."""
    assert CreateProjectRequest(name="SDS", domain=typed).domain == stored


@pytest.mark.parametrize("typed", ["not a domain", "localhost", ""])
def test_something_that_is_not_a_domain_is_refused(typed: str) -> None:
    with pytest.raises(ValidationError):
        CreateProjectRequest(name="SDS", domain=typed)


def test_model_routing_round_trips_through_settings() -> None:
    routing = ModelRouting(
        extract="google/gemini-2.5-flash", synthesize="anthropic/claude-opus-4.6"
    )
    settings = {SETTINGS_MODELS: routing.as_settings()}

    assert settings[SETTINGS_MODELS] == {
        "extract": "google/gemini-2.5-flash",
        "synthesize": "anthropic/claude-opus-4.6",
    }
    assert ModelRouting.from_settings(settings) == routing


def test_a_model_id_without_a_vendor_is_refused() -> None:
    """The router raises on this at run time, by which point money is committed."""
    with pytest.raises(ValidationError, match="OpenRouter model id"):
        ModelRouting(extract="gemini-2.5-flash")


def test_reading_back_settings_tolerates_a_task_class_this_build_dropped() -> None:
    routing = ModelRouting.from_settings({SETTINGS_MODELS: {"summarise": "vendor/model"}})
    assert routing == ModelRouting()


def test_an_unknown_gate_is_refused_by_name() -> None:
    with pytest.raises(ValidationError, match="not an approval gate"):
        UpdateProjectRequest(approvals={"invented_gate": {"assignee_id": None, "sla_hours": None}})


def test_a_known_gate_is_accepted() -> None:
    body = UpdateProjectRequest(approvals={"1.1.5": {"sla_hours": 24}})
    assert body.approvals is not None
    assert body.approvals["1.1.5"].sla_hours == 24


def test_an_unknown_field_is_refused_rather_than_silently_ignored() -> None:
    """A client sending `context` instead of `product_context` should hear about it."""
    with pytest.raises(ValidationError):
        UpdateProjectRequest(context={"summary": "…"})  # type: ignore[call-arg]
