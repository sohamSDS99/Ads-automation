"""What a full run is expected to consume, per task class.

PRD §13.4 step 3 asks the model picker for a "live estimated cost per full run"
that "recalculates from historical token counts". Historical counts are the
right source and they are not always there — a fresh installation has run
nothing — so this module answers from measurement when it can and from a
declared assumption when it cannot, and says which it did.

The browser multiplies these token counts by the price of the model it is
hovering over. That keeps the estimate live without a request per keystroke,
and keeps one definition of "how big is a run" on the server.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import NodeRun, NodeRunStatus, Run, RunMode, RunStatus
from agent.llm.router import TaskClass
from agent.orchestrator.registry import get_registry

#: How many completed full runs are enough to stop guessing. One run is a
#: sample of one; it would make the estimate lurch after every research cycle.
MEASURED_RUNS_REQUIRED = 3

#: The fallback, from PRD §10's node table: 21 nodes, of which the extract and
#: classify classes carry the volume (evidence in, structure out) and the two
#: writing classes carry the report. Tokens are per full run, summed across
#: every call in that class.
#:
#: These are assumptions, labelled as such wherever they are shown. They exist
#: so a new workspace sees a number with the right order of magnitude instead of
#: "$0.00", which reads as free.
ASSUMED: dict[TaskClass, tuple[int, int, int]] = {
    # task class: (calls, prompt tokens, completion tokens)
    TaskClass.EXTRACT: (9, 900_000, 90_000),
    TaskClass.CLASSIFY: (6, 400_000, 60_000),
    TaskClass.SYNTHESIZE: (4, 180_000, 60_000),
    TaskClass.CRITIQUE: (2, 90_000, 20_000),
}


@dataclass(frozen=True, slots=True)
class ClassUsage:
    """Expected consumption of one task class over one full run."""

    task_class: TaskClass
    calls: int
    token_in: int
    token_out: int
    #: "measured" once enough full runs have finished, "assumed" until then.
    source: str


async def usage_baseline(db: AsyncSession, workspace_id: uuid.UUID) -> list[ClassUsage]:
    """Per-class token counts for one full run.

    Measured from completed full runs when there are enough of them, averaged
    per run so the number means "the next run", not "every run so far".
    """
    measured = await _measured(db, workspace_id)
    return [measured.get(task_class) or _assumed(task_class) for task_class in TaskClass]


async def _measured(db: AsyncSession, workspace_id: uuid.UUID) -> dict[TaskClass, ClassUsage]:
    completed = (
        (
            await db.execute(
                sa.select(Run.id).where(
                    Run.workspace_id == workspace_id,
                    Run.mode == RunMode.FULL,
                    Run.status == RunStatus.SUCCEEDED,
                )
            )
        )
        .scalars()
        .all()
    )
    if len(completed) < MEASURED_RUNS_REQUIRED:
        return {}

    run_count = len(completed)
    rows = (
        await db.execute(
            sa.select(
                NodeRun.node_id,
                sa.func.count(),
                sa.func.coalesce(sa.func.sum(NodeRun.token_in), 0),
                sa.func.coalesce(sa.func.sum(NodeRun.token_out), 0),
            )
            .where(
                NodeRun.run_id.in_(completed),
                NodeRun.status == NodeRunStatus.SUCCEEDED,
            )
            .group_by(NodeRun.node_id)
        )
    ).all()

    # `node_run` records what a node cost, not what class of thinking it was —
    # that belongs to the node's spec. Folding through the registry means a node
    # that has since been retired drops out of the baseline instead of being
    # attributed to the wrong class.
    registry = get_registry()
    totals: dict[TaskClass, list[int]] = {}
    for node_id, calls, token_in, token_out in rows:
        if node_id not in registry:
            continue
        bucket = totals.setdefault(registry.spec(node_id).task_class, [0, 0, 0])
        bucket[0] += int(calls)
        bucket[1] += int(token_in)
        bucket[2] += int(token_out)

    return {
        task_class: ClassUsage(
            task_class=task_class,
            calls=round(calls / run_count),
            token_in=round(token_in / run_count),
            token_out=round(token_out / run_count),
            source="measured",
        )
        for task_class, (calls, token_in, token_out) in totals.items()
    }


def _assumed(task_class: TaskClass) -> ClassUsage:
    calls, token_in, token_out = ASSUMED[task_class]
    return ClassUsage(
        task_class=task_class,
        calls=calls,
        token_in=token_in,
        token_out=token_out,
        source="assumed",
    )
