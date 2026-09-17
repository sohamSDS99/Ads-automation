"""The approval gates, as the setup wizard needs to describe them.

*Which* gates exist is not decided here. A gate is a node that declares
`gate=True`, so the registry already knows the list, their ids, their stages and
who each one needs — and P3's `orchestrator/approvals.py` opens them from the
same specs. Keeping a second list beside it would mean a gate could be added to
the DAG and silently never appear in the wizard.

What lives here is the part a `NodeSpec` has no field for: who in the business
is actually being asked, and what the question is. A gate with no copy yet still
appears, named after its node — a missing sentence is a worse failure than a
missing screen.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent.db.models import ApprovalRequiredRole
from agent.orchestrator.approvals import GATE_ASSIGNEES, GATE_SLA_HOURS
from agent.orchestrator.registry import get_registry

#: Where the wizard writes the default approver and the optional SLA per gate.
#: Re-exported from the *reader* rather than spelled again here — P6 shipped a
#: wizard that wrote `approvals` while P3's machinery read `gate_assignees`, and
#: the only symptom was runs halting on nobody. Two names for one JSONB key is
#: how that happens; an alias cannot drift.
SETTINGS_ASSIGNEES = GATE_ASSIGNEES
SETTINGS_SLA = GATE_SLA_HOURS


@dataclass(frozen=True, slots=True)
class GateSpec:
    """One approval gate, merged from its node spec and its copy."""

    node_id: str
    stage: str
    name: str
    #: Who in the business answers this. Rendered so an admin assigning the gate
    #: knows which of their people the question is for.
    audience: str
    description: str
    required_role: ApprovalRequiredRole


#: Per gate: the audience, and the question in one sentence. Keyed by node id.
_COPY: dict[str, tuple[str, str, str]] = {
    # node id: (title, audience, description)
    "1.1.5": (
        "Compliance guardrails",
        "Legal",
        "The claims we may not make, the disclaimers we must carry, and the "
        "regulated terms that need wording sign-off.",
    ),
    "1.3.4": (
        "Differentiation claim",
        "Marketing lead",
        "The one thing we will say that competitors do not, and the proof we hold for it.",
    ),
    "1.5.3": (
        "Audience consent",
        "Data officer",
        "Which audience lists may be used in which markets, and on what consent basis.",
    ),
}


def gates() -> tuple[GateSpec, ...]:
    """Every gate in the registered DAG, in the order a run reaches them.

    Read at call time, not at import: the registry discovers nodes by walking
    `agent.nodes`, and a stage that has not shipped yet simply has no gate here
    rather than a placeholder nothing can assign.
    """
    found = []
    for spec in get_registry().specs():
        if not spec.gate:
            continue
        title, audience, description = _COPY.get(
            spec.id,
            # Fall back to the node's own name rather than hiding the gate. A
            # gate with no copy reads as unfinished, which it is.
            (spec.name.replace("_", " ").capitalize(), "An approver", ""),
        )
        found.append(
            GateSpec(
                node_id=spec.id,
                stage=spec.stage,
                name=title,
                audience=audience,
                description=description,
                # `NodeSpec` guarantees this is set whenever `gate` is.
                required_role=spec.required_role or ApprovalRequiredRole.APPROVER,
            )
        )
    return tuple(sorted(found, key=lambda gate: gate.node_id))


def gate_ids() -> frozenset[str]:
    return frozenset(gate.node_id for gate in gates())
