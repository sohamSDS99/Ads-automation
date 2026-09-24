"""4.4.1 — what code decides about a concept (`creative/concepts.py`, PRD §11, §13).

The model's share is words; `product_depiction`, surfaces and the negative
constraints are code's. These tests pin that split: a draft that tries to carry
a depiction does not validate, and every prompt carries every code-owned
negative — "no text, no logos, no watermark" verbatim on a search surface.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from agent.creative import concepts
from agent.creative.concepts import (
    ALWAYS_NEGATIVES,
    SEARCH_NEGATIVES,
    Angle,
    assemble,
    assemble_prompt,
    brief_angles,
    draft_model,
    negatives_for,
    surfaces_for,
)
from tests.creative.test_brief import _brief

ANGLES = [Angle("angle", "Compliance without the binder"), Angle("g1:primary", "SDS in seconds")]
RATIOS = ["1.91:1", "1:1"]


def _concept(**overrides: Any) -> dict[str, Any]:
    concept: dict[str, Any] = {
        "name": "Clean bench",
        "angle": "angle",
        "rationale": "An uncluttered lab bench reads as control.",
        "subject": "a tidy laboratory bench",
        "setting": "a bright lab at morning",
        "scene_prompt": "A tidy laboratory bench in soft morning light",
        "extra_negatives": ["no clutter"],
        "composition": {"ratio_0": "bench across the lower third", "ratio_1": "bench centred"},
    }
    concept.update(overrides)
    return concept


def _draft(count: int = 2, **overrides: Any) -> Any:
    schema = draft_model(ANGLES, RATIOS, [], count)
    return schema.model_validate({"concepts": [_concept(**overrides) for _ in range(count)]})


# ---------------------------------------------------------------------------
# surfaces
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("campaign_type", "expected"),
    [
        ("search", ["search_image"]),
        ("performance_max", ["pmax_image"]),
        ("display", ["display_image"]),
        ("demand_gen", ["demand_gen_image"]),
        ("video", []),
        ("shopping", []),
    ],
)
def test_the_campaign_type_names_the_image_surface(campaign_type: str, expected: list[str]) -> None:
    assert surfaces_for(campaign_type, images_in_scope=True, image_ratios=["1:1"]) == expected


def test_no_surface_without_images_in_scope_or_an_image_ratio() -> None:
    assert surfaces_for("search", images_in_scope=False, image_ratios=["1:1"]) == []
    assert surfaces_for("search", images_in_scope=True, image_ratios=[]) == []


# ---------------------------------------------------------------------------
# negatives — code's, always
# ---------------------------------------------------------------------------


def test_a_search_surface_carries_no_text_no_logos_no_watermark_verbatim() -> None:
    negatives = negatives_for("none", ["search_image"], forbidden_subjects=[])
    assert negatives[0] == "no text, no logos, no watermark"
    assert SEARCH_NEGATIVES == "no text, no logos, no watermark"


def test_only_a_search_surface_carries_it() -> None:
    assert SEARCH_NEGATIVES not in negatives_for("none", ["pmax_image"], forbidden_subjects=[])


def test_every_concept_forbids_people_marks_competitors_and_invented_product() -> None:
    negatives = negatives_for("reference_guided", ["pmax_image"], forbidden_subjects=[])
    assert list(ALWAYS_NEGATIVES) == negatives


@pytest.mark.parametrize("depiction", ["composited_real", "none"])
def test_a_depiction_other_than_reference_guided_forbids_the_product(depiction: str) -> None:
    negatives = negatives_for(depiction, [], forbidden_subjects=[])  # type: ignore[arg-type]
    assert any(item.startswith("do not depict the product") for item in negatives)


def test_reference_guided_does_not_forbid_the_product() -> None:
    negatives = negatives_for("reference_guided", [], forbidden_subjects=[])
    assert not any("do not depict the product" in item for item in negatives)


def test_the_briefs_forbidden_subjects_and_the_models_extras_follow_deduplicated() -> None:
    negatives = negatives_for(
        "none",
        ["search_image"],
        forbidden_subjects=["hazard symbols", " "],
        extra=["No Text, No Logos, No Watermark", "no clutter"],
    )
    assert "no hazard symbols" in negatives
    assert negatives[-1] == "no clutter"
    assert sum(item.casefold() == SEARCH_NEGATIVES for item in negatives) == 1


def test_the_prompt_carries_the_scene_and_every_negative() -> None:
    negatives = negatives_for("none", ["search_image"], forbidden_subjects=["gloves"])
    prompt = assemble_prompt("A tidy bench.", negatives)
    assert prompt.startswith("A tidy bench. Avoid: ")
    for item in negatives:
        assert item in prompt


# ---------------------------------------------------------------------------
# the model's schema
# ---------------------------------------------------------------------------


def test_a_draft_cannot_carry_a_product_depiction() -> None:
    with pytest.raises(ValidationError):
        _draft(product_depiction="reference_guided")


def test_a_draft_cannot_choose_an_angle_the_brief_does_not_have() -> None:
    with pytest.raises(ValidationError):
        _draft(angle="a-new-angle")


def test_a_draft_must_compose_every_ratio() -> None:
    with pytest.raises(ValidationError):
        _draft(composition={"ratio_0": "bench across the lower third"})


@pytest.mark.parametrize("given", [1, 3])
def test_a_draft_has_exactly_concepts_per_campaign(given: int) -> None:
    schema = draft_model(ANGLES, RATIOS, [], 2)
    with pytest.raises(ValidationError):
        schema.model_validate({"concepts": [_concept() for _ in range(given)]})


def test_palette_tokens_are_an_enum_when_the_brief_has_them() -> None:
    schema = draft_model(ANGLES, RATIOS, ["brand-blue"], 1)
    schema.model_validate({"concepts": [_concept(palette_tokens=["brand-blue"])]})
    with pytest.raises(ValidationError):
        schema.model_validate({"concepts": [_concept(palette_tokens=["hot-pink"])]})


# ---------------------------------------------------------------------------
# assembly — the model's words bound to code's decisions
# ---------------------------------------------------------------------------


def test_assembly_binds_the_resolved_depiction_surfaces_and_negatives() -> None:
    built = assemble(
        _draft(),
        campaign_ref="c-sds",
        angles=ANGLES,
        ratios=RATIOS,
        surfaces=["search_image"],
        depiction="composited_real",
        forbidden_subjects=["hazard symbols"],
    )
    assert [c.id for c in built] == ["c-sds:c1", "c-sds:c2"]
    for concept in built:
        assert concept.product_depiction == "composited_real"
        assert concept.surfaces == ["search_image"]
        assert concept.angle_text == "Compliance without the binder"
        assert concept.composition_by_ratio == {
            "1.91:1": "bench across the lower third",
            "1:1": "bench centred",
        }
        assert "no text, no logos, no watermark" in concept.prompt
        assert "no hazard symbols" in concept.negative_constraints
        assert all(item in concept.prompt for item in concept.negative_constraints)


def test_brief_angles_are_the_briefs_angle_and_the_campaigns_ad_groups() -> None:
    brief = _brief()
    campaign = brief.ad_groups[0].campaign_ref
    angles = brief_angles(brief, campaign)
    assert angles[0] == Angle("angle", brief.angle.text)
    mine = [g for g in brief.ad_groups if g.campaign_ref == campaign]
    assert len(angles) == 1 + 2 * len(mine)
    assert brief_angles(brief, "no-such-campaign") == [Angle("angle", brief.angle.text)]


def test_image_surfaces_cover_every_image_bearing_campaign_type() -> None:
    assert set(concepts.IMAGE_SURFACES) == {"search", "performance_max", "display", "demand_gen"}
