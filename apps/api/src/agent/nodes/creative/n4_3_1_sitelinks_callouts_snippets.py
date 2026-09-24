"""4.3.1 `sitelinks_callouts_snippets` — a stub until its build phase (Stage 04 PRD §11, §21.3).

Sitelinks (on-domain, 2xx, unique), callouts and structured snippets.
Registered in S4-P4 so the creative DAG carries §11's exact edges; it gathers
nothing, calls no model, submits nothing and writes no asset. The phase that
builds it replaces this module.
"""

from __future__ import annotations

from agent.llm.router import TaskClass
from agent.nodes.creative._stub import stub

SITELINKS_CALLOUTS_SNIPPETS = stub(
    "4.3.1",
    "sitelinks_callouts_snippets",
    depends_on=("4.5.1",),
    task_class=TaskClass.COPYWRITE,
    lint_required=True,
)
