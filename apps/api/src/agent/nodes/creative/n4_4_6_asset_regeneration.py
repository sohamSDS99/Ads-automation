"""4.4.6 `asset_regeneration` — a stub until its build phase (Stage 04 PRD §11, §21.3).

Regenerates only the items G8 sent back. Registered in S4-P4 so the creative
DAG carries §11's exact edges; it gathers nothing, calls no model, submits
nothing and writes no asset. The phase that builds it replaces this module.
"""

from __future__ import annotations

from agent.llm.router import TaskClass
from agent.nodes.creative._stub import stub

ASSET_REGENERATION = stub(
    "4.4.6",
    "asset_regeneration",
    depends_on=("4.4.5",),
    task_class=TaskClass.VISION,
    media=("image", "video"),
    lint_required=True,
)
