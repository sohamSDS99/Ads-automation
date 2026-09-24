"""4.4.3 `image_renditions` — a stub until its build phase (Stage 04 PRD §11, §21.3).

Every required ratio as native, relaid or saliency crop (sx == sy). Registered
in S4-P4 so the creative DAG carries §11's exact edges; it gathers nothing,
calls no model, submits nothing and writes no asset. The phase that builds it
replaces this module.
"""

from __future__ import annotations

from agent.llm.router import TaskClass
from agent.nodes.creative._stub import stub

IMAGE_RENDITIONS = stub(
    "4.4.3",
    "image_renditions",
    depends_on=("4.4.2",),
    task_class=TaskClass.CLASSIFY,
    media=("image",),
    lint_required=True,
)
