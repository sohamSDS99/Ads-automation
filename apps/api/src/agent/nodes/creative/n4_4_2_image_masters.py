"""4.4.2 `image_masters` — a stub until its build phase (Stage 04 PRD §11, §21.3).

Master candidates per concept, linted before VISION ranks them. Registered in
S4-P4 so the creative DAG carries §11's exact edges; it gathers nothing, calls
no model, submits nothing and writes no asset. The phase that builds it
replaces this module.
"""

from __future__ import annotations

from agent.llm.router import TaskClass
from agent.nodes.creative._stub import stub

IMAGE_MASTERS = stub(
    "4.4.2",
    "image_masters",
    depends_on=("4.4.1",),
    task_class=TaskClass.VISION,
    media=("image",),
    lint_required=True,
)
