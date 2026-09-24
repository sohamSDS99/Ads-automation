"""4.4.1 `creative_concepts` — everything about a visual concept code decides (PRD §11, §13).

The model is asked for words: a name, the angle it builds on (from an enum of
the approved brief's angles), a rationale, the subject, the setting, one
composition note per required ratio and a scene. Code owns the rest:

* `product_depiction`, resolved from capability × references × Law 44 by
  `media/references.py` — the model is never asked (Law 38);
* `surfaces`, from the campaign type and the pinned spec sheet;
* the negative constraints §13 and §11 require — no recognisable people, no
  third-party marks, no competitor products, no invented packaging or UI; the
  product itself when the depiction is not `reference_guided`; the brief's
  forbidden subjects; and on a search surface, verbatim, "no text, no logos, no
  watermark". The image API has no negative-prompt field, so they are written
  into the prompt, where a model cannot drop them.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

from agent.schemas.creative_brief import CreativeBrief
from agent.schemas.creative_media import Concept, Depiction, ImageSurface

#: Campaign type (Stage 02) → the image surface its images are linted as
#: (Stage 03 delta, PRD §7.1). `video` and `shopping` carry no image asset.
IMAGE_SURFACES: dict[str, ImageSurface] = {
    "search": "search_image",
    "performance_max": "pmax_image",
    "display": "display_image",
    "demand_gen": "demand_gen_image",
}

#: §11 4.4.1, verbatim: search-surface prompts carry these negatives.
SEARCH_NEGATIVES = "no text, no logos, no watermark"

#: §13 "People, marks, competitors" and "Product fidelity", on every concept.
ALWAYS_NEGATIVES: tuple[str, ...] = (
    "no recognisable real people or celebrities",
    "no third-party logos or brand marks",
    "no competitor products",
    "no invented packaging, labels, user-interface screenshots or certificates",
)

#: What the depiction forbids. `composited_real`: the real photo is composited
#: later by `postprod/`, so the model paints the scene and leaves room for it.
DEPICTION_NEGATIVES: dict[Depiction, tuple[str, ...]] = {
    "reference_guided": (),
    "composited_real": (
        "do not depict the product; leave clear space where the real product photo will be placed",
    ),
    "none": ("do not depict the product",),
}

#: Added on 4.4.2's one retry, when every candidate failed image lint (§11 4.4.2).
STRENGTHENED_NEGATIVES: tuple[str, ...] = (
    "absolutely no text, letters, numbers, captions or typography anywhere in the image",
    "absolutely no logos, badges, stamps, watermarks or graphic overlays",
    "a single clean photographic scene with generous empty space",
)


@dataclass(frozen=True, slots=True)
class Angle:
    key: str
    text: str


def brief_angles(brief: CreativeBrief, campaign_ref: str) -> list[Angle]:
    """The angles a concept for `campaign_ref` may build on: the brief's angle,
    then each of the campaign's ad groups' primary message and angle B."""
    angles = [Angle("angle", brief.angle.text)]
    for group in brief.ad_groups:
        if group.campaign_ref != campaign_ref:
            continue
        angles.append(Angle(f"{group.ad_group_ref}:primary", group.primary_message.text))
        angles.append(Angle(f"{group.ad_group_ref}:angle_b", group.angle_b.text))
    return angles


def surfaces_for(
    campaign_type: str, *, images_in_scope: bool, image_ratios: Sequence[str]
) -> list[ImageSurface]:
    surface = IMAGE_SURFACES.get(campaign_type)
    if not images_in_scope or not image_ratios or surface is None:
        return []
    return [surface]


def negatives_for(
    depiction: Depiction,
    surfaces: Iterable[ImageSurface],
    *,
    forbidden_subjects: Iterable[str],
    extra: Iterable[str] = (),
) -> list[str]:
    """Every negative constraint, code's first, deduplicated, order kept."""
    wanted: list[str] = []
    if "search_image" in set(surfaces):
        wanted.append(SEARCH_NEGATIVES)
    wanted.extend(ALWAYS_NEGATIVES)
    wanted.extend(DEPICTION_NEGATIVES[depiction])
    wanted.extend(f"no {subject.strip()}" for subject in forbidden_subjects if subject.strip())
    wanted.extend(item.strip() for item in extra if item.strip())
    seen: set[str] = set()
    ordered: list[str] = []
    for item in wanted:
        key = item.casefold()
        if key not in seen:
            seen.add(key)
            ordered.append(item)
    return ordered


def assemble_prompt(scene: str, negatives: Sequence[str]) -> str:
    """The scene, then every negative, in the prompt itself."""
    body = scene.strip().rstrip(".")
    return f"{body}. Avoid: {'; '.join(negatives)}." if negatives else f"{body}."


# ---------------------------------------------------------------------------
# the model's share: an enum-constrained schema
# ---------------------------------------------------------------------------


class _Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")


def draft_model(
    angles: Sequence[Angle], ratios: Sequence[str], palette: Sequence[str], count: int
) -> type[BaseModel]:
    """Exactly `count` concepts; every choice the model has is an enum, and the
    composition has one required field per ratio so none can be skipped."""
    angle_keys = Literal[tuple(angle.key for angle in angles)]  # type: ignore[valid-type]
    fields: dict[str, Any] = {
        "name": (str, Field(min_length=1, max_length=60)),
        "angle": (angle_keys, ...),
        "rationale": (str, Field(min_length=1)),
        "subject": (str, Field(min_length=1)),
        "setting": (str, Field(min_length=1)),
        "scene_prompt": (str, Field(min_length=1)),
        "extra_negatives": (list[str], ...),
    }
    if ratios:
        per_ratio: dict[str, Any] = {
            ratio_field(index): (str, Field(min_length=1, description=f"framing at {ratio}"))
            for index, ratio in enumerate(ratios)
        }
        composition = create_model("CompositionDraft", __base__=_Draft, **per_ratio)
        fields["composition"] = (composition, ...)
    if palette:
        tokens = Literal[tuple(palette)]  # type: ignore[valid-type]
        fields["palette_tokens"] = (list[tokens], ...)  # type: ignore[valid-type]
    concept = create_model("ConceptDraft", __base__=_Draft, **fields)
    return create_model(
        "ConceptsDraft",
        __base__=_Draft,
        concepts=(list[concept], Field(min_length=count, max_length=count)),
    )


def ratio_field(index: int) -> str:
    return f"ratio_{index}"


def assemble(
    draft: BaseModel,
    *,
    campaign_ref: str,
    angles: Sequence[Angle],
    ratios: Sequence[str],
    surfaces: Sequence[ImageSurface],
    depiction: Depiction,
    forbidden_subjects: Sequence[str],
) -> list[Concept]:
    """The model's words bound to what code decided. Deterministic ids."""
    by_key = {angle.key: angle for angle in angles}
    concepts: list[Concept] = []
    for index, item in enumerate(getattr(draft, "concepts")):  # noqa: B009 — a dynamic model
        composition = getattr(item, "composition", None)
        negatives = negatives_for(
            depiction,
            surfaces,
            forbidden_subjects=forbidden_subjects,
            extra=item.extra_negatives,
        )
        concepts.append(
            Concept(
                id=f"{campaign_ref}:c{index + 1}",
                campaign_ref=campaign_ref,
                name=item.name,
                angle=item.angle,
                angle_text=by_key[item.angle].text,
                rationale=item.rationale,
                subject=item.subject,
                setting=item.setting,
                composition_by_ratio={
                    ratio: getattr(composition, ratio_field(i)) for i, ratio in enumerate(ratios)
                }
                if composition is not None
                else {},
                palette_tokens=list(getattr(item, "palette_tokens", []) or []),
                product_depiction=depiction,
                prompt=assemble_prompt(item.scene_prompt, negatives),
                negative_constraints=negatives,
                surfaces=list(surfaces),
            )
        )
    return concepts
