"""The three approval gates of the research DAG (PRD §10).

A gate is a node the run stops at until a human decides. The *mechanics* — the
halt, the `Approval` row, the resume — are P3's; what lives here is only the
list of gates and who each one belongs to, because the setup wizard has to
offer a default assignee per gate (PRD §13.4 step 4) before any gate node
exists in the registry.

Keeping the list here rather than in the wizard means the browser never decides
what a gate is: it renders what this module says, and P3 registers nodes whose
ids are checked against it.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent.db.models import ApprovalRequiredRole


@dataclass(frozen=True, slots=True)
class GateSpec:
    """One approval gate, as the setup wizard needs to describe it."""

    node_id: str
    stage: str
    name: str
    #: Who the run asks. Rendered so an admin assigning the gate knows which of
    #: their people the question is actually for.
    audience: str
    description: str
    required_role: ApprovalRequiredRole


#: PRD §10, stages 1.1.5, 1.3.4 and 1.5.3. Ordered as the run reaches them.
GATES: tuple[GateSpec, ...] = (
    GateSpec(
        node_id="compliance_guardrails",
        stage="1.1.5",
        name="Compliance guardrails",
        audience="Legal",
        description=(
            "The claims we may not make, the disclaimers we must carry, and the "
            "regulated terms that need wording sign-off."
        ),
        required_role=ApprovalRequiredRole.APPROVER,
    ),
    GateSpec(
        node_id="differentiation_claim",
        stage="1.3.4",
        name="Differentiation claim",
        audience="Marketing lead",
        description=(
            "The one thing we will say that competitors do not, and the proof we hold for it."
        ),
        required_role=ApprovalRequiredRole.APPROVER,
    ),
    GateSpec(
        node_id="audience_consent_check",
        stage="1.5.3",
        name="Audience consent",
        audience="Data officer",
        description=(
            "Which audience lists may be used in which markets, and on what consent basis."
        ),
        required_role=ApprovalRequiredRole.APPROVER,
    ),
)

GATE_IDS: frozenset[str] = frozenset(gate.node_id for gate in GATES)


def gate(node_id: str) -> GateSpec | None:
    """The gate with this node id, or None if it is not a gate."""
    return next((item for item in GATES if item.node_id == node_id), None)
