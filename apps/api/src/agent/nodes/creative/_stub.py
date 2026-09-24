"""The shape a creative node has before its build phase (Stage 04 PRD §21.3).

S4-P4 registers all 24 nodes with §11's exact edges so the creative DAG is the
PRD's graph from the first phase that runs it. Each node's own phase replaces
its module (`n4_2_1_headline_spread.py` and so on) with the real thing; until
then the node does nothing: no gather, no model call, no media submit, no
asset, no spend. Its output says so, so nothing downstream can mistake a stub
for a result.

Gates and the person-task are conditional stubs. A stub produced no asset and
no exception, so G8, G8b and H3 have nothing to ask and are `not_required` —
the same answer the real nodes give when there is nothing (§8.5, §11 4.6.3).
The leading underscore keeps `registry.discover()` from walking this module.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from agent.db.models import ApprovalRequiredRole, Evidence, RunStage
from agent.llm.router import TaskClass
from agent.nodes.base import NodeSpec, RunContext


class StubInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StubOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    #: Always true. A downstream node reading `stub: true` knows it has nothing.
    stub: Literal[True] = True


class StubNode:
    spec: NodeSpec

    def __init__(self, spec: NodeSpec) -> None:
        self.spec = spec

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        return []

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        return StubOutput(node_id=self.spec.id)


class StubGate(StubNode):
    """G8 / G8b before 4.4.5 / 4.4.7 exist: no AI asset, nothing to review."""

    def gate_required(self, ctx: RunContext, output: BaseModel) -> bool:
        return False


class StubTask(StubNode):
    """H3 before 4.6.3 exists: no exception, no task (§11 4.6.3)."""

    def task_required(self, ctx: RunContext, output: BaseModel) -> bool:
        return False


def _spec(
    node_id: str,
    name: str,
    *,
    depends_on: tuple[str, ...],
    task_class: TaskClass,
    media: tuple[Literal["image", "video"], ...] = (),
    lint_required: bool = False,
    gate_key: str | None = None,
    human_task_key: str | None = None,
) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        name=name,
        stage=node_id.rsplit(".", 1)[0],
        run_stage=RunStage.CREATIVE,
        depends_on=depends_on,
        task_class=task_class,
        input_model=StubInput,
        output_model=StubOutput,
        media=media,
        lint_required=lint_required,
        gate=gate_key is not None,
        gate_conditional=gate_key is not None,
        gate_key=gate_key,
        required_role=ApprovalRequiredRole.APPROVER if gate_key else None,
        human_task_key=human_task_key,
        human_task_conditional=human_task_key is not None,
    )


def stub(
    node_id: str,
    name: str,
    *,
    depends_on: tuple[str, ...],
    task_class: TaskClass,
    media: tuple[Literal["image", "video"], ...] = (),
    lint_required: bool = False,
) -> StubNode:
    return StubNode(
        _spec(
            node_id,
            name,
            depends_on=depends_on,
            task_class=task_class,
            media=media,
            lint_required=lint_required,
        )
    )


def stub_gate(node_id: str, name: str, *, depends_on: tuple[str, ...], gate_key: str) -> StubGate:
    return StubGate(
        _spec(node_id, name, depends_on=depends_on, task_class=TaskClass.VISION, gate_key=gate_key)
    )


def stub_task(
    node_id: str, name: str, *, depends_on: tuple[str, ...], human_task_key: str
) -> StubTask:
    return StubTask(
        _spec(
            node_id,
            name,
            depends_on=depends_on,
            task_class=TaskClass.CLASSIFY,
            human_task_key=human_task_key,
        )
    )
