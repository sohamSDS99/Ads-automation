"""S4-P24 — PRD §17 CC1's copy track (G7 → 4.3.3 ≤ 12 min), the part of it the
DAG's shape decides.

CC1 is NOT MET for the copy track. `test_s4p24_cost_and_time::
test_cc1_copy_track_does_not_wait_for_video_latency` is its strict xfail. The
cause is scheduling, not dependency. `RunExecutor` runs the DAG wave by wave,
and 4.3.3's wave comes after waves holding 4.4.1, 4.4.2, 4.4.4 (video), 4.4.3
and 4.4.5 (G8, a human stop). So the copy track waits for video generation and
for the G8 decision, although nothing on it needs either.

Two tests pin that shape:
- **Nothing on the copy track depends on media.** This holds today, and a
  change that made it false would turn a scheduling defect into a design one.
- **A ratchet on what 4.3.3 waits for.** The set of 4.4.x nodes scheduled
  before it, and its wave, may not grow past what S4-P24 measured. The
  strict xfail fails the day the copy track stops waiting. This fails the day
  it waits for more.
"""

from __future__ import annotations

from agent.db.models import RunStage
from agent.orchestrator.dag import Dag
from agent.orchestrator.registry import get_registry

#: What S4-P24 measured: 4.3.3 in wave 5, behind these media nodes.
MEASURED_WAVE = 5
MEASURED_MEDIA_BEFORE = frozenset({"4.4.1", "4.4.2", "4.4.3", "4.4.4", "4.4.5"})


def _dag() -> Dag:
    return Dag.from_registry(get_registry().for_stage(RunStage.CREATIVE))


def _needs(dag: Dag, node_id: str) -> set[str]:
    seen: set[str] = set()
    stack = [node_id]
    while stack:
        for parent in dag.depends_on(stack.pop()):
            if parent not in seen:
                seen.add(parent)
                stack.append(parent)
    return seen


def test_nothing_on_the_copy_track_depends_on_a_media_node() -> None:
    dag = _dag()
    assert {n for n in _needs(dag, "4.3.3") if n.startswith("4.4.")} == set()


def test_ratchet_the_copy_track_waits_for_no_more_media_than_s4_p24_measured() -> None:
    waves = [set(wave) for wave in _dag().waves()]
    at = next(i for i, wave in enumerate(waves) if "4.3.3" in wave)
    before = {n for wave in waves[:at] for n in wave if n.startswith("4.4.")}
    assert at <= MEASURED_WAVE, f"4.3.3 moved to wave {at}"
    assert before <= MEASURED_MEDIA_BEFORE, (
        f"4.3.3 now also waits for {before - MEASURED_MEDIA_BEFORE}"
    )
