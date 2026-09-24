"""4.6.1 `spec_conformance` — a stub until its build phase (Stage 04 PRD §11, §21.3).

Every count, size, ratio, byte size, format, duration and codec. Registered in
S4-P4 so the creative DAG carries §11's exact edges; it gathers nothing, calls
no model, submits nothing and writes no asset. The phase that builds it
replaces this module.
"""

from __future__ import annotations

from agent.llm.router import TaskClass
from agent.nodes.creative._stub import stub

SPEC_CONFORMANCE = stub(
    "4.6.1",
    "spec_conformance",
    depends_on=("4.2.4", "4.2.5", "4.3.1", "4.3.2", "4.3.3", "4.4.7"),
    task_class=TaskClass.CLASSIFY,
)
