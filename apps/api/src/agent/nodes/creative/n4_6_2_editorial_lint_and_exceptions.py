"""4.6.2 `editorial_lint_and_exceptions` — a stub until its build phase (Stage 04 PRD §11, §21.3).

A full LintResult per target and the exceptions the ruleset cannot license.
Registered in S4-P4 so the creative DAG carries §11's exact edges; it gathers
nothing, calls no model, submits nothing and writes no asset. The phase that
builds it replaces this module.
"""

from __future__ import annotations

from agent.llm.router import TaskClass
from agent.nodes.creative._stub import stub

EDITORIAL_LINT_AND_EXCEPTIONS = stub(
    "4.6.2",
    "editorial_lint_and_exceptions",
    depends_on=("4.6.1", "4.5.2"),
    task_class=TaskClass.VISION,
)
