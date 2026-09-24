"""4.6.3 `legal_exception_clearance` — a stub until its build phase (Stage 04 PRD §11, §21.3).

H3 -> the named legal owner only; not_required when there are no exceptions.
Registered in S4-P4 so the creative DAG carries §11's exact edges; it gathers
nothing, calls no model, submits nothing and writes no asset. The phase that
builds it replaces this module.
"""

from __future__ import annotations

from agent.nodes.creative._stub import stub_task

LEGAL_EXCEPTION_CLEARANCE = stub_task(
    "4.6.3", "legal_exception_clearance", depends_on=("4.6.2",), human_task_key="H3"
)
