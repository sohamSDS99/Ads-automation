"""Task class → model, and the settings that override it."""

from __future__ import annotations

import pytest

from agent.llm.router import (
    SEED_MODELS,
    TEXT_TASK_CLASSES,
    ModelRouter,
    ModelRoutingError,
    TaskClass,
)


def test_every_task_class_has_a_seed_and_a_fallback() -> None:
    # IMAGE_GEN / VIDEO_GEN have no seed by law (Stage 04 law 36) and the
    # router refuses them; `tests/creative/test_task_classes.py` asserts that.
    for task_class in TEXT_TASK_CLASSES:
        chain = ModelRouter().chain(task_class)
        assert len(chain) >= 2, f"{task_class} has nothing to fall back to"
        assert chain[0] == SEED_MODELS[task_class][0]


def test_fallbacks_cross_vendors() -> None:
    """A same-vendor fallback does not survive the outage it exists for."""
    for task_class in TEXT_TASK_CLASSES:
        chain = ModelRouter().chain(task_class)
        vendors = {model.split("/", 1)[0] for model in chain}
        assert len(vendors) > 1, f"{task_class} never leaves {vendors}"


def test_extract_and_classify_are_pinned_deterministic() -> None:
    """PRD §15 NF7 measures determinism against exactly these two classes."""
    for task_class in (TaskClass.EXTRACT, TaskClass.CLASSIFY):
        choice = ModelRouter().choose(task_class)
        assert choice.temperature == 0.0
        assert choice.top_p == 1.0


def test_project_settings_override_the_primary_and_keep_the_fallbacks() -> None:
    router = ModelRouter.resolve(project_settings={"models": {"extract": "vendor/chosen"}})
    chain = router.chain(TaskClass.EXTRACT)
    assert chain[0] == "vendor/chosen"
    assert chain[1:] == SEED_MODELS[TaskClass.EXTRACT]


def test_project_settings_beat_workspace_settings() -> None:
    router = ModelRouter.resolve(
        workspace_settings={"models": {"synthesize": "vendor/workspace"}},
        project_settings={"models": {"synthesize": "vendor/project"}},
    )
    assert router.chain(TaskClass.SYNTHESIZE)[0] == "vendor/project"


def test_workspace_settings_apply_where_the_project_is_silent() -> None:
    router = ModelRouter.resolve(
        workspace_settings={"models": {"critique": "vendor/workspace"}},
        project_settings={"models": {"extract": "vendor/project"}},
    )
    assert router.chain(TaskClass.CRITIQUE)[0] == "vendor/workspace"
    assert router.chain(TaskClass.EXTRACT)[0] == "vendor/project"


def test_an_override_equal_to_the_seed_does_not_duplicate_the_chain() -> None:
    seed = SEED_MODELS[TaskClass.EXTRACT][0]
    chain = ModelRouter.resolve(project_settings={"models": {"extract": seed}}).chain(
        TaskClass.EXTRACT
    )
    assert chain == SEED_MODELS[TaskClass.EXTRACT]


@pytest.mark.parametrize(
    "settings",
    [
        {"models": {"extract": "gemini-flash"}},
        {"models": {"extract": ""}},
        {"models": {"extract": "/leading-slash"}},
        {"models": {"nonsense": "vendor/model"}},
        {"models": {"extract": 42}},
        {"models": "vendor/model"},
    ],
)
def test_a_malformed_override_is_refused_rather_than_ignored(settings: dict[str, object]) -> None:
    """Falling through to the seed would spend real money on a model nobody picked."""
    with pytest.raises(ModelRoutingError):
        ModelRouter.resolve(project_settings=settings)


def test_no_settings_means_seeds() -> None:
    router = ModelRouter.resolve(workspace_settings=None, project_settings={})
    assert router.overrides == {}
    assert router.chain(TaskClass.EXTRACT) == SEED_MODELS[TaskClass.EXTRACT]
