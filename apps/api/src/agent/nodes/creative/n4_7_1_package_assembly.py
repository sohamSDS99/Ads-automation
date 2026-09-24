"""4.7.1 `package_assembly` — a stub until its build phase (Stage 04 PRD §11, §21.3).

The CreativePackage — deterministic code, no LLM. Registered in S4-P4 so the
creative DAG carries §11's exact edges; it gathers nothing, calls no model,
submits nothing and writes no asset. The phase that builds it replaces this
module.
"""

from __future__ import annotations

from agent.llm.router import TaskClass
from agent.nodes.creative._stub import stub

PACKAGE_ASSEMBLY = stub(
    "4.7.1",
    "package_assembly",
    depends_on=(
        "4.1.1",
        "4.2.1",
        "4.2.2",
        "4.2.3",
        "4.2.4",
        "4.2.5",
        "4.5.1",
        "4.5.2",
        "4.3.1",
        "4.3.2",
        "4.3.3",
        "4.4.1",
        "4.4.2",
        "4.4.3",
        "4.4.4",
        "4.4.5",
        "4.4.6",
        "4.4.7",
        "4.6.1",
        "4.6.2",
        "4.6.3",
        "4.6.4",
    ),
    task_class=TaskClass.CLASSIFY,
)
