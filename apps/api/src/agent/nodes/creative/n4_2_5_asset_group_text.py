"""4.2.5 `asset_group_text` — a stub until its build phase (Stage 04 PRD §11, §21.3).

PMax / Demand Gen / Display asset-group text, all linted. Registered in S4-P4
so the creative DAG carries §11's exact edges; it gathers nothing, calls no
model, submits nothing and writes no asset. The phase that builds it replaces
this module.
"""

from __future__ import annotations

from agent.llm.router import TaskClass
from agent.nodes.creative._stub import stub

ASSET_GROUP_TEXT = stub(
    "4.2.5",
    "asset_group_text",
    depends_on=("4.1.1",),
    task_class=TaskClass.COPYWRITE,
    lint_required=True,
)
