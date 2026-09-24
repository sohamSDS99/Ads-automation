"""4.7.2 `creative_critique` — a stub until its build phase (Stage 04 PRD §11, §21.3).

Blocking code checks plus CRITIQUE warnings. Registered in S4-P4 so the
creative DAG carries §11's exact edges; it gathers nothing, calls no model,
submits nothing and writes no asset. The phase that builds it replaces this
module.
"""

from __future__ import annotations

from agent.llm.router import TaskClass
from agent.nodes.creative._stub import stub

CREATIVE_CRITIQUE = stub(
    "4.7.2", "creative_critique", depends_on=("4.7.1",), task_class=TaskClass.CRITIQUE
)
