"""4.3.3 `lead_form_asset` — a stub until its build phase (Stage 04 PRD §11, §21.3).

The lead form and its field-count trade-off. Registered in S4-P4 so the
creative DAG carries §11's exact edges; it gathers nothing, calls no model,
submits nothing and writes no asset. The phase that builds it replaces this
module.
"""

from __future__ import annotations

from agent.llm.router import TaskClass
from agent.nodes.creative._stub import stub

LEAD_FORM_ASSET = stub(
    "4.3.3",
    "lead_form_asset",
    depends_on=("4.5.2",),
    task_class=TaskClass.COPYWRITE,
    lint_required=True,
)
