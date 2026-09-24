"""Two placeholder nodes so `stage='creative'` runs end to end (S4-P0 §23 item 8).

They do nothing but prove the path: a creative run is launched, the executor
picks the creative DAG, both nodes checkpoint, and the terminal event reaches
the client over the existing SSE channel. No gather, no model call, no spend.

Real creative nodes start in S4-P4, which deletes this module and the two
re-exports in `nodes/creative/__init__.py`. The leading underscore keeps
`registry.discover()` from walking this file on its own; the package re-export
is the single place they are registered, so removing them is one edit.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from agent.db.models import Evidence, RunStage
from agent.llm.router import TaskClass
from agent.nodes.base import NodeSpec, RunContext


class DummyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DummyOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    placeholder: bool = True


class _DummyNode:
    spec: NodeSpec

    def __init__(self, spec: NodeSpec) -> None:
        self.spec = spec

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        return []

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        return DummyOutput(node_id=self.spec.id)


def _spec(node_id: str, name: str, depends_on: tuple[str, ...]) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        name=name,
        stage="4.0",
        run_stage=RunStage.CREATIVE,
        depends_on=depends_on,
        # Declared because NodeSpec requires one; neither node calls a model.
        task_class=TaskClass.CLASSIFY,
        input_model=DummyInput,
        output_model=DummyOutput,
    )


CREATIVE_DUMMY_START = _DummyNode(_spec("4.0.1", "creative_dummy_start", ()))
CREATIVE_DUMMY_END = _DummyNode(_spec("4.0.2", "creative_dummy_end", ("4.0.1",)))
