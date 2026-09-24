"""The token baseline the model picker prices a routing choice against."""

from __future__ import annotations

from agent.llm.estimate import ASSUMED, MEASURED_RUNS_REQUIRED, _assumed
from agent.llm.router import TaskClass


def test_every_task_class_has_an_assumption() -> None:
    """A class with no baseline would price to $0.00, which reads as free.

    The estimate prices a *research* run, so the classes that matter are the
    ones research nodes declare — derived from the registry, not listed, so a
    research node that starts using a new class fails here.
    """
    from agent.db.models import RunStage
    from agent.orchestrator.registry import get_registry

    research = {spec.task_class for spec in get_registry().for_stage(RunStage.RESEARCH).specs()}
    assert research <= set(ASSUMED)
    assert set(ASSUMED) <= set(TaskClass)


def test_an_assumed_baseline_says_it_is_assumed() -> None:
    usage = _assumed(TaskClass.SYNTHESIZE)
    assert usage.source == "assumed"
    assert usage.token_in > 0
    assert usage.token_out > 0


def test_one_finished_run_is_not_enough_to_stop_guessing() -> None:
    """A sample of one would make the estimate lurch after every research cycle."""
    assert MEASURED_RUNS_REQUIRED > 1
