"""4.3.2 `offer_assets` — a stub until its build phase (Stage 04 PRD §11, §21.3).

Promotion and price assets whose every number and date is an OfferBinding.
Registered in S4-P4 so the creative DAG carries §11's exact edges; it gathers
nothing, calls no model, submits nothing and writes no asset. The phase that
builds it replaces this module.
"""

from __future__ import annotations

from agent.llm.router import TaskClass
from agent.nodes.creative._stub import stub

OFFER_ASSETS = stub(
    "4.3.2",
    "offer_assets",
    depends_on=("4.1.1",),
    task_class=TaskClass.COPYWRITE,
    lint_required=True,
)
