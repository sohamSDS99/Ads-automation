"""A gate that does not always have a question to ask (Stage 03 PRD §11, 3.5.1).

Every gate before Stage 03 was unconditional: reaching the node meant asking the
human. 3.5.1 is the first that can legitimately have nothing to ask — when a
current `SignOffMatrix` already names the three owners, the answer is on file and
halting the run would be asking somebody to re-approve their own unchanged
decision.

The executor's `if spec.gate:` is what makes that impossible, so the declaration
has to grow. What these tests pin down is that the new flag cannot be *quietly*
wrong in either direction: a node that claims a conditional gate and cannot
decide it, and a node that can decide it but never declared the flag, are both
refused at import time rather than discovered at 3am.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from agent.db.models import ApprovalRequiredRole, RunStage
from agent.llm.router import TaskClass
from agent.nodes.base import LLMNode, NodeSpec
from agent.orchestrator.registry import NodeRegistry, RegistryError


class Out(BaseModel):
    value: str = "x"


def _spec(**kwargs: object) -> NodeSpec:
    return NodeSpec(
        id="9.9.1",
        name="conditional",
        stage="9.9",
        run_stage=RunStage.GUIDELINE,
        task_class=TaskClass.CLASSIFY,
        input_model=BaseModel,
        output_model=Out,
        **kwargs,  # type: ignore[arg-type]
    )


class TestDeclaration:
    def test_a_conditional_gate_that_cannot_decide_itself_is_refused(self) -> None:
        """The flag would be a lie: the gate would open every time."""

        class Node(LLMNode):
            spec = _spec(
                gate=True,
                gate_conditional=True,
                gate_key="G6",
                required_role=ApprovalRequiredRole.APPROVER,
            )

        with pytest.raises(RegistryError, match="gate_required"):
            NodeRegistry.of([Node()])

    def test_a_node_that_can_decide_a_gate_it_did_not_declare_is_refused(self) -> None:
        """The other direction. The method would never be consulted — silently."""

        class Node(LLMNode):
            spec = _spec(
                gate=True,
                gate_key="G6",
                required_role=ApprovalRequiredRole.APPROVER,
            )

            def gate_required(self, ctx: object, output: object) -> bool:
                return False

        with pytest.raises(RegistryError, match="gate_conditional"):
            NodeRegistry.of([Node()])

    def test_a_conditional_flag_on_a_node_that_is_not_a_gate_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="gate_conditional"):
            _spec(gate_conditional=True)

    def test_an_unconditional_gate_needs_no_predicate(self) -> None:
        class Node(LLMNode):
            spec = _spec(
                gate=True,
                gate_key="G6",
                required_role=ApprovalRequiredRole.APPROVER,
            )

        assert len(NodeRegistry.of([Node()])) == 1


class TestExecutorDecision:
    """`gate_wanted` is the executor's `if spec.gate:` after this phase.

    Extracted to a function so the decision can be tested without a database,
    a Redis, a worker and a model. The integration suite proves it is wired in;
    these prove it decides correctly.
    """

    def test_a_plain_node_never_wants_a_gate(self) -> None:
        from agent.orchestrator.executor import gate_wanted

        class Node(LLMNode):
            spec = _spec()

        assert gate_wanted(Node(), None, Out()) is False

    def test_an_unconditional_gate_always_wants_one(self) -> None:
        from agent.orchestrator.executor import gate_wanted

        class Node(LLMNode):
            spec = _spec(gate=True, gate_key="G5", required_role=ApprovalRequiredRole.APPROVER)

        assert gate_wanted(Node(), None, Out()) is True

    def test_a_conditional_gate_defers_to_the_node(self) -> None:
        from agent.orchestrator.executor import gate_wanted

        class Node(LLMNode):
            spec = _spec(
                gate=True,
                gate_conditional=True,
                gate_key="G6",
                required_role=ApprovalRequiredRole.APPROVER,
            )

            def gate_required(self, ctx: object, output: BaseModel) -> bool:
                return output.value == "ask"

        assert gate_wanted(Node(), None, Out(value="ask")) is True
        assert gate_wanted(Node(), None, Out(value="on file")) is False

    def test_the_node_is_handed_its_own_output_model_not_a_dict(self) -> None:
        """A node answers from what it just produced, with its own types."""
        from agent.orchestrator.executor import gate_wanted

        seen: list[object] = []

        class Node(LLMNode):
            spec = _spec(
                gate=True,
                gate_conditional=True,
                gate_key="G6",
                required_role=ApprovalRequiredRole.APPROVER,
            )

            def gate_required(self, ctx: object, output: BaseModel) -> bool:
                seen.append(output)
                return True

        output = Out(value="x")
        gate_wanted(Node(), None, output)

        assert seen == [output]
