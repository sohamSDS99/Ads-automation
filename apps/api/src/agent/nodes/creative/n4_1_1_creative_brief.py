"""4.1.1 `creative_brief` ⛳G7 — placeholder until the node lands (S4-P4 item 6)."""

from __future__ import annotations

from agent.db.models import ApprovalRequiredRole
from agent.llm.router import TaskClass
from agent.nodes.creative._stub import StubNode, _spec

CREATIVE_BRIEF = StubNode(
    _spec("4.1.1", "creative_brief", depends_on=(), task_class=TaskClass.SYNTHESIZE).model_copy(
        update={"gate": True, "gate_key": "G7", "required_role": ApprovalRequiredRole.APPROVER}
    )
)
