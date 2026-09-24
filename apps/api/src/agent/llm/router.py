"""Per-task-class model routing (PRD §8.2).

A node declares what *kind* of thinking it needs — `EXTRACT`, `CLASSIFY`,
`SYNTHESIZE`, `CRITIQUE` — and never a model id. The mapping from class to
model is runtime configuration: the seeds below are what a fresh installation
starts with, and Settings overrides them per workspace or per project without a
deploy (PRD §8: "Model IDs are **not hardcoded** … Defaults above are seed
values only").
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import structlog

log = structlog.get_logger(__name__)


class TaskClass(StrEnum):
    """What a node asks of a model. The only thing a node declares."""

    EXTRACT = "extract"
    """High volume, long context, no judgement: pull structure out of evidence."""

    CLASSIFY = "classify"
    """Label something against a fixed rubric. Fast, cheap, deterministic."""

    SYNTHESIZE = "synthesize"
    """Long-form reasoning and prose. The report depends on this being good."""

    CRITIQUE = "critique"
    """Adversarial review of another model's output — deliberately cross-family."""

    COPYWRITE = "copywrite"
    """Stage 04: headline and description pools, variant B, extras, video scripts."""

    VISION = "vision"
    """Stage 04, advisory only: ranking masters, review notes, rights flags."""

    IMAGE_GEN = "image_gen"
    """Stage 04. **Routed only through `media/`** — user-selected, no seed (law 36)."""

    VIDEO_GEN = "video_gen"
    """Stage 04. **Routed only through `media/`** — user-selected, no seed (law 36)."""


#: The two classes a text gateway must never serve (PRD §7.1). They have no
#: seed model: the user picks one from the admin allowlist ∩ the live catalogue,
#: and every request is capability-validated and budget-reserved in `media/`
#: before a cent is spent. `router.choose()` refuses them, `validate_overrides`
#: refuses them, and `LLMGateway.complete_structured` refuses them again.
MEDIA_TASK_CLASSES: frozenset[TaskClass] = frozenset({TaskClass.IMAGE_GEN, TaskClass.VIDEO_GEN})


#: Seed routing. Each entry is `(primary, *fallbacks)`; the gateway walks the
#: tuple left to right when a model errors past its retries (PRD §16, "auto-
#: fallback to the next model in the task class's fallback list").
#:
#: Fallbacks cross vendors on purpose: the failure being defended against is
#: usually one provider degrading, and a same-vendor sibling degrades with it.
SEED_MODELS: dict[TaskClass, tuple[str, ...]] = {
    TaskClass.EXTRACT: ("google/gemini-2.5-flash", "anthropic/claude-haiku-4.5"),
    TaskClass.CLASSIFY: ("anthropic/claude-haiku-4.5", "google/gemini-2.5-flash"),
    TaskClass.SYNTHESIZE: ("anthropic/claude-opus-4.6", "openai/gpt-5.2"),
    TaskClass.CRITIQUE: ("openai/gpt-5.2", "anthropic/claude-opus-4.6"),
    # Stage 04 PRD §9.6. The seed is the PRD's; the fallback crosses vendor for
    # the reason above, and is SYNTHESIZE's own fallback.
    TaskClass.COPYWRITE: ("anthropic/claude-opus-4.6", "openai/gpt-5.2"),
    # Both accept image input; a vision fallback that could not see the image
    # would be worse than none. Advisory only — nothing blocks on VISION.
    TaskClass.VISION: ("google/gemini-2.5-flash", "anthropic/claude-haiku-4.5"),
}

#: Sampling per class as `(temperature, top_p)`.
#:
#: PRD §15 NF7 pins `EXTRACT` and `CLASSIFY` to `temperature=0, top_p=1` — the
#: determinism requirement is measured against them ("identical input hash +
#: cache reuse ⇒ identical output"). The two writing classes are left a little
#: room; nothing downstream asserts byte-equality on their prose.
SAMPLING: dict[TaskClass, tuple[float, float]] = {
    TaskClass.EXTRACT: (0.0, 1.0),
    TaskClass.CLASSIFY: (0.0, 1.0),
    TaskClass.SYNTHESIZE: (0.3, 1.0),
    TaskClass.CRITIQUE: (0.3, 1.0),
    # A ranking is a judgement asked twice; it should give the same answer.
    TaskClass.VISION: (0.0, 1.0),
    # COPYWRITE is deliberately absent: its temperature is
    # `copy.temperature_copywrite` in creative_constants.yaml (PRD §9.6), with
    # the project's `creative_overrides` applied, and `ModelRouter` reads it.
}

#: Where an override lives inside `Project.settings` / `Workspace.settings`.
SETTINGS_KEY = "models"


class ModelRoutingError(ValueError):
    """Settings name a model this router cannot use."""


@dataclass(frozen=True, slots=True)
class ModelChoice:
    """The resolved answer for one task class."""

    task_class: TaskClass
    chain: tuple[str, ...]
    temperature: float
    top_p: float

    @property
    def primary(self) -> str:
        return self.chain[0]


def _validate(model: str, *, source: str) -> str:
    """Reject a malformed override loudly rather than quietly routing elsewhere.

    A typo in `settings.models` would otherwise fall through to the seed model
    and spend real money on a model nobody chose, which is exactly the kind of
    substitution that should never be silent.
    """
    candidate = model.strip()
    if not candidate or "/" not in candidate or candidate.startswith("/"):
        raise ModelRoutingError(
            f"{source} names {model!r}, which is not an OpenRouter model id "
            "(expected the form 'vendor/model')."
        )
    return candidate


def _overrides_from(settings: Mapping[str, Any] | None, *, source: str) -> dict[TaskClass, str]:
    """Read `settings["models"]` into `{TaskClass: model_id}`."""
    if not settings:
        return {}
    raw = settings.get(SETTINGS_KEY)
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ModelRoutingError(f"{source}.{SETTINGS_KEY} must be an object of task class → model.")

    resolved: dict[TaskClass, str] = {}
    for key, value in raw.items():
        try:
            task_class = TaskClass(str(key).lower())
        except ValueError as exc:
            raise ModelRoutingError(
                f"{source}.{SETTINGS_KEY} names task class {key!r}; "
                f"valid classes are {', '.join(sorted(TEXT_TASK_CLASSES))}."
            ) from exc
        if task_class in MEDIA_TASK_CLASSES:
            raise ModelRoutingError(
                f"{source}.{SETTINGS_KEY}.{key}: {task_class.value} is routed only through "
                "media/ — choose the model on the Media generation screen, where it is "
                "checked against the live catalogue and the allowlist."
            )
        if task_class is TaskClass.VISION:
            # PRD §9.6: the router rejects a VISION model whose
            # `input_modalities` lacks `image`. Nothing here can see the
            # catalogue yet, so an override is refused rather than trusted —
            # fail closed until the first VISION caller (4.4.2) wires the check.
            raise ModelRoutingError(
                f"{source}.{SETTINGS_KEY}.{key}: a vision override cannot be checked for image "
                "input yet, so it is not accepted. The seed model is used."
            )
        if not isinstance(value, str):
            raise ModelRoutingError(f"{source}.{SETTINGS_KEY}.{key} must be a model id string.")
        resolved[task_class] = _validate(value, source=f"{source}.{SETTINGS_KEY}.{key}")
    return resolved


def validate_overrides(
    models: Mapping[str, str], *, source: str = "settings"
) -> dict[TaskClass, str]:
    """Check a `{task class: model id}` mapping before it is stored.

    The router already refuses a bad override at run time; this is the same
    check moved forward to the moment someone typed it, where the message can
    still reach them.
    """
    return _overrides_from({SETTINGS_KEY: dict(models)}, source=source)


class ModelRouter:
    """Task class → model chain, with project settings winning over workspace settings."""

    def __init__(
        self,
        overrides: Mapping[TaskClass, str] | None = None,
        *,
        copywrite_temperature: float | None = None,
    ) -> None:
        self._overrides = dict(overrides or {})
        if copywrite_temperature is None:
            copywrite_temperature = _copywrite_temperature(None)
        self._copywrite_temperature = copywrite_temperature

    @classmethod
    def resolve(
        cls,
        *,
        workspace_settings: Mapping[str, Any] | None = None,
        project_settings: Mapping[str, Any] | None = None,
    ) -> ModelRouter:
        """Build the router for one run. Project settings are the narrower scope, so they win."""
        merged = _overrides_from(workspace_settings, source="workspace.settings")
        merged.update(_overrides_from(project_settings, source="project.settings"))
        return cls(merged, copywrite_temperature=_copywrite_temperature(project_settings))

    def choose(self, task_class: TaskClass) -> ModelChoice:
        if task_class in MEDIA_TASK_CLASSES:
            raise ModelRoutingError(
                f"{task_class.value} has no routable model: it is user-selected and served "
                "only through media/ (law 36)."
            )
        seeds = SEED_MODELS[task_class]
        override = self._overrides.get(task_class)
        # An override replaces the primary but keeps the seeds behind it: the
        # point of a fallback list is that the run survives one model being
        # down, and a chosen model is not less likely to be down than a seed.
        chain: Sequence[str] = seeds if override is None else (override, *seeds)
        deduped = tuple(dict.fromkeys(chain))
        if task_class is TaskClass.COPYWRITE:
            temperature, top_p = self._copywrite_temperature, 1.0
        else:
            temperature, top_p = SAMPLING[task_class]
        return ModelChoice(
            task_class=task_class, chain=deduped, temperature=temperature, top_p=top_p
        )

    def chain(self, task_class: TaskClass) -> tuple[str, ...]:
        return self.choose(task_class).chain

    @property
    def overrides(self) -> dict[TaskClass, str]:
        return dict(self._overrides)


#: Every class a text model may be chosen for.
TEXT_TASK_CLASSES: frozenset[TaskClass] = frozenset(TaskClass) - MEDIA_TASK_CLASSES


def _copywrite_temperature(project_settings: Mapping[str, Any] | None) -> float:
    """`copy.temperature_copywrite`, with this project's overrides applied."""
    from agent.creative.constants import OVERRIDES_KEY, get_creative_constants

    overrides = (project_settings or {}).get(OVERRIDES_KEY)
    constants = get_creative_constants().merged(
        overrides if isinstance(overrides, Mapping) else None
    )
    return constants.copy_.temperature_copywrite.value
