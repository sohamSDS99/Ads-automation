"""4.4.1 `creative_concepts` — a stub until its build phase (Stage 04 PRD §11, §21.3).

Visual concepts per campaign; product_depiction resolved in code. Registered in
S4-P4 so the creative DAG carries §11's exact edges; it gathers nothing, calls
no model, submits nothing and writes no asset. The phase that builds it
replaces this module.
"""

from __future__ import annotations

from agent.llm.router import TaskClass
from agent.nodes.creative._stub import stub

CREATIVE_CONCEPTS = stub(
    "4.4.1", "creative_concepts", depends_on=("4.1.1",), task_class=TaskClass.SYNTHESIZE
)
