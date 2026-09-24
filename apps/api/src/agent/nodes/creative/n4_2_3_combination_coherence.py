"""4.2.3 `combination_coherence` — a stub until its build phase (Stage 04 PRD §11, §21.3).

HH/HD/DD pairs: deterministic flags block, CLASSIFY labels drive one repair
round. Registered in S4-P4 so the creative DAG carries §11's exact edges; it
gathers nothing, calls no model, submits nothing and writes no asset. The phase
that builds it replaces this module.
"""

from __future__ import annotations

from agent.llm.router import TaskClass
from agent.nodes.creative._stub import stub

COMBINATION_COHERENCE = stub(
    "4.2.3",
    "combination_coherence",
    depends_on=("4.2.1", "4.2.2"),
    task_class=TaskClass.CLASSIFY,
    lint_required=True,
)
