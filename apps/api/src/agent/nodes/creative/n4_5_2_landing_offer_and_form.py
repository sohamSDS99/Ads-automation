"""4.5.2 `landing_offer_and_form` — a stub until its build phase (Stage 04 PRD §11, §21.3).

Offer above the fold, the minimal form and a landing-page patch. Registered in
S4-P4 so the creative DAG carries §11's exact edges; it gathers nothing, calls
no model, submits nothing and writes no asset. The phase that builds it
replaces this module.
"""

from __future__ import annotations

from agent.llm.router import TaskClass
from agent.nodes.creative._stub import stub

LANDING_OFFER_AND_FORM = stub(
    "4.5.2", "landing_offer_and_form", depends_on=("4.5.1",), task_class=TaskClass.CLASSIFY
)
