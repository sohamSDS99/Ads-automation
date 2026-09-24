"""4.2.2 `claim_bound_descriptions` — a stub until its build phase (Stage 04 PRD §11, §21.3).

Per ad group: descriptions each bound to >=1 claim licensed at the pin.
Registered in S4-P4 so the creative DAG carries §11's exact edges; it gathers
nothing, calls no model, submits nothing and writes no asset. The phase that
builds it replaces this module.
"""

from __future__ import annotations

from agent.llm.router import TaskClass
from agent.nodes.creative._stub import stub

CLAIM_BOUND_DESCRIPTIONS = stub(
    "4.2.2",
    "claim_bound_descriptions",
    depends_on=("4.1.1",),
    task_class=TaskClass.COPYWRITE,
    lint_required=True,
)
