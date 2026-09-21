"""Two placeholder plan nodes, so `stage='plan'` is a thing that runs.

S2-P0 builds the handshake: accept research, check eligibility, take the plan
lock, write `Run(stage='plan')`, enqueue it. The last of those is only
verifiable end to end if the worker can actually execute the run it picks up
— otherwise the phase ships a launch path proven against nothing, which is the
exact shape of "written, never read".

So: two nodes, in a two-node chain, carrying real Stage 02 facts and calling
no model.

* **Neither spends anything.** `reason()` returns a model directly, as
  `1.5.2` and `1.4.2` already do for measurement. Stage 02 law 14 says the
  LLM never does arithmetic; these do not do arithmetic *or* prose.
* **They echo the `PlanInput` the run was started from.** A placeholder that
  returned a constant would prove the executor runs and nothing else. These
  read `Run.input_hash` and the project's markets, so a green run also proves
  the handshake delivered its object to the pipeline intact.
* **Neither is a gate.** Gates G1–G4 are S2-P2 and S2-P3. A placeholder gate
  would put an undecidable approval in somebody's inbox.

Deleted wholesale by S2-P2.
"""

from __future__ import annotations

import structlog
from pydantic import BaseModel, Field

from agent.db.models import Evidence, RunStage
from agent.llm.router import TaskClass
from agent.nodes.base import NodeSpec, RunContext

log = structlog.get_logger(__name__)


class HandshakeInput(BaseModel):
    """Nothing. The node reads the run, not an upstream output."""


class SourceEcho(BaseModel):
    """What the handshake delivered, as the pipeline received it."""

    research_run_id: str | None = Field(
        default=None, description="The accepted research run this plan consumes"
    )
    input_hash: str | None = Field(default=None, description="sha256 of the PlanInput at launch")
    markets: list[str] = Field(default_factory=list, description="Market countries on the project")


class ReadyEcho(BaseModel):
    """That the second wave saw the first one's output."""

    source_confirmed: bool
    note: str


class _PlanPlaceholder:
    """Shared shape: gather nothing, call nothing, return a model."""

    spec: NodeSpec

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        return []


class HandshakeSourceNode(_PlanPlaceholder):
    """2.0.1 — repeat back what the run was started from."""

    spec = NodeSpec(
        id="2.0.1",
        name="handshake_source",
        stage="2.0",
        run_stage=RunStage.PLAN,
        task_class=TaskClass.EXTRACT,
        input_model=HandshakeInput,
        output_model=SourceEcho,
    )

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        return SourceEcho(
            research_run_id=str(ctx.run.source_run_id) if ctx.run.source_run_id else None,
            input_hash=ctx.run.input_hash,
            markets=[
                str(market.get("country", "")) for market in (ctx.project.markets or []) if market
            ],
        )


class HandshakeReadyNode(_PlanPlaceholder):
    """2.0.2 — prove the second wave ran after the first."""

    spec = NodeSpec(
        id="2.0.2",
        name="handshake_ready",
        stage="2.0",
        run_stage=RunStage.PLAN,
        depends_on=("2.0.1",),
        task_class=TaskClass.EXTRACT,
        input_model=HandshakeInput,
        output_model=ReadyEcho,
    )

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        source = ctx.output_of("2.0.1")
        return ReadyEcho(
            source_confirmed=bool(source.get("input_hash")),
            note=(
                "Placeholder node. The campaign planning DAG is built in S2-P2; "
                "this run proves the handshake reaches the executor."
            ),
        )


handshake_source = HandshakeSourceNode()
handshake_ready = HandshakeReadyNode()
