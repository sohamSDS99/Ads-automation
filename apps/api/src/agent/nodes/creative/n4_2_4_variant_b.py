"""4.2.4 `variant_b` — a stub until its build phase (Stage 04 PRD §11, §21.3).

A second RSA per ad group from a different brief angle, distinct from A.
Registered in S4-P4 so the creative DAG carries §11's exact edges; it gathers
nothing, calls no model, submits nothing and writes no asset. The phase that
builds it replaces this module.
"""

from __future__ import annotations

from agent.llm.router import TaskClass
from agent.nodes.creative._stub import stub

VARIANT_B = stub(
    "4.2.4", "variant_b", depends_on=("4.2.3",), task_class=TaskClass.COPYWRITE, lint_required=True
)
