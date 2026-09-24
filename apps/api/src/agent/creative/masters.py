"""4.4.2 `image_masters` — the parts of mastering an image code owns (PRD §11, Laws 33, 37).

* **Distinct candidates under Law 37.** A `GenerationJob` is keyed by its request,
  so `candidates_per_concept` identical requests would be one job, not several.
  A model that takes `n` is asked once for all of them; one that takes `seed` is
  asked once per candidate with a seed derived from (run, concept, attempt,
  index) — stable across a resume, so a crashed node re-finds the same jobs; a
  model that takes neither can make exactly one distinct candidate, and does.
* **Lint before ranking.** Every candidate is measured (the Stage 03 precheck:
  OCR text coverage, logo match) and linted through the run's pinned linter;
  `guardrails.verdicts.image_verdict` decides, so an unmeasured blocking check
  never reads as a pass (law 31). Failing candidates never reach VISION.
* **VISION is advisory.** It ranks the survivors and flags what a reviewer
  should look at; if it cannot answer, the first survivor is the master and
  the reason says so. Nothing blocks on it.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

from agent.creative.concepts import STRENGTHENED_NEGATIVES
from agent.imaging.precheck import LogoTemplateData
from agent.media.capability import supported_ratios
from agent.media.types import CapabilityRecord, Descriptor
from agent.schemas.creative_media import Concept
from agent.schemas.guardrails import RuleSet
from agent.schemas.imaging import ImageMeasurement

#: What VISION may flag on a candidate (§13): each is for a reviewer's eyes.
VISION_FLAGS: tuple[str, ...] = (
    "product_like_object",
    "recognisable_person",
    "third_party_logo",
    "competitor_product",
    "visible_text",
    "watermark",
    "off_brief",
)

VISION_SYSTEM = """You review candidate advertising images generated for one visual concept.
Rules, all of them hard:
- Candidates are named c1, c2, … in the order the images are attached.
- Pick the candidate that most clearly carries the concept's subject, setting and angle
  as a clean photograph. Explain the pick in one or two plain sentences.
- For every candidate, write one short note, and flag only what you can see:
  product_like_object, recognisable_person, third_party_logo, competitor_product,
  visible_text, watermark, off_brief.
- Your ranking is advice to a human reviewer. Do not invent defects to seem careful.
"""


def candidate_requests(
    capability: CapabilityRecord,
    count: int,
    *,
    run_id: uuid.UUID,
    concept_id: str,
    attempt: int,
) -> list[dict[str, Any]]:
    """The request fields that make `count` candidates distinct jobs (Law 37)."""
    n = capability.params.get("n")
    if n is not None and n.kind == "range" and (n.min or 1) <= count <= (n.max or 1):
        return [{"n": count}] if count > 1 else [{}]
    seed = capability.params.get("seed")
    if count > 1 and seed is not None and seed.kind == "range":
        return [
            {
                "seed": derive_seed(
                    seed, run_id=run_id, concept_id=concept_id, attempt=attempt, index=i
                )
            }
            for i in range(count)
        ]
    return [{}]


def derive_seed(
    descriptor: Descriptor, *, run_id: uuid.UUID, concept_id: str, attempt: int, index: int
) -> int:
    """A seed inside the model's range, the same on every resume of this run."""
    low = descriptor.min or 0
    high = descriptor.max if descriptor.max is not None else 2**31 - 1
    digest = hashlib.sha256(f"{run_id}|{concept_id}|{attempt}|{index}".encode()).digest()
    return low + int.from_bytes(digest[:8], "big") % (high - low + 1)


def master_ratio(image_ratios: Sequence[str], capability: CapabilityRecord) -> str | None:
    """The first ratio the campaign requires that the model paints natively.

    None when it paints none of them: the request then names no ratio, and
    4.4.3 relays or crops from whatever the model returns (§11, Law 39).
    """
    supported = supported_ratios(capability)
    return next((ratio for ratio in image_ratios if ratio in supported), None)


def request_prompt(concept: Concept, ratio: str | None, *, strengthened: bool) -> str:
    """The concept's prompt — every code-owned negative included — plus the
    composition note for the ratio asked for, and on the one retry the
    strengthened negatives."""
    parts = [concept.prompt]
    note = concept.composition_by_ratio.get(ratio) if ratio else None
    if note:
        parts.append(f"Composition at {ratio}: {note.strip().rstrip('.')}.")
    if strengthened:
        parts.append(f"Also avoid: {'; '.join(STRENGTHENED_NEGATIVES)}.")
    return " ".join(parts)


def logo_templates(ruleset: RuleSet) -> tuple[LogoTemplateData, ...]:
    """The pinned ruleset's registered logos, in the precheck's shape."""
    return tuple(
        LogoTemplateData(
            asset_id=template.asset_id,
            label=template.label,
            phash=template.phash,
            descriptors_b64=template.descriptors_b64,
            keypoint_count=template.keypoint_count,
            min_score=template.min_score,
        )
        for template in ruleset.logo_templates
    )


def measure_candidate(content: bytes, templates: tuple[LogoTemplateData, ...]) -> ImageMeasurement:
    """The Stage 03 precheck measurement of one candidate. Runs in the worker,
    the only image carrying tesseract; never raises for a missing detector —
    every metric is then absent and every image rule `indeterminate`."""
    from agent.imaging import precheck

    return precheck.measure(content, templates=templates)


class _Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")


def ranking_model(keys: Sequence[str]) -> type[BaseModel]:
    """VISION's answer: the best candidate, why, and a note per candidate."""
    key = Literal[tuple(keys)]  # type: ignore[valid-type]
    flag = Literal[VISION_FLAGS]  # type: ignore[valid-type]
    note = create_model(
        "CandidateNote",
        __base__=_Draft,
        candidate=(key, ...),
        note=(str, Field(min_length=1)),
        flags=(list[flag], ...),
    )
    return create_model(
        "CandidateRanking",
        __base__=_Draft,
        best=(key, ...),
        why=(str, Field(min_length=1)),
        notes=(list[note], ...),
    )
