"""4.6.4 `final_lint_and_render` — a stub until its build phase (Stage 04 PRD §11, §21.3).

Re-lint against the final pin and render previews. Registered in S4-P4 so the
creative DAG carries §11's exact edges; it gathers nothing, calls no model,
submits nothing and writes no asset. The phase that builds it replaces this
module.
"""

from __future__ import annotations

from agent.llm.router import TaskClass
from agent.nodes.creative._stub import stub

FINAL_LINT_AND_RENDER = stub(
    "4.6.4",
    "final_lint_and_render",
    depends_on=("4.6.3",),
    task_class=TaskClass.CLASSIFY,
    lint_required=True,
)
