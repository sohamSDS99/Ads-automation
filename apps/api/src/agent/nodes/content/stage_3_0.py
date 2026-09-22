"""A two-node smoke DAG for the guideline pipeline (PRD §23 deliverable 7).

**These two nodes are scaffolding and S3-P2 deletes them.** They exist so that
`stage='guideline'` is executable end to end before a single real node is
written: `all_dags()` builds and validates one DAG per `RunStage` at worker
boot, and a `RunStage` member with no registered node is a boot-time
`DagError` rather than a quiet gap.

They are numbered `3.0.x` on purpose. Every id the PRD actually uses is
`3.1`–`3.6`, so nothing here squats on a node S3-P2 has to write, and the
deletion is a whole file rather than a hunt through one.

Two properties that are not incidental:

* **No model call.** A cold-start smoke test must not cost anything or need a
  live key, and `test_cold_start_no_upstream_dependency` runs on a database
  whose only rows are a workspace, a user and a project.
* **No gate and no person-task.** G5, G6, H1 and H2 arrive with the nodes that
  own them. A placeholder that halted for approval would make the one thing
  this phase has to prove — that a guideline run reaches a terminal node on a
  bare project — impossible to prove.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from agent.db.models import Evidence, RunStage
from agent.llm.router import TaskClass
from agent.nodes.base import NodeSpec, RunContext


class GuidelinePlaceholderOutput(BaseModel):
    """Enough shape to persist a `NodeRun` and render in the console."""

    node: str
    #: Which optional bindings resolved, carried through so the smoke run
    #: proves the mode survives the trip from the route to the worker.
    mode: str
    unbound_inputs: list[str] = Field(default_factory=list)
    note: str = "Placeholder node — replaced by the real Stage 03 DAG in S3-P2."


def _from_run(ctx: RunContext) -> tuple[str, list[str]]:
    """The run's bindings, read off the row rather than rebuilt.

    S3-P2 replaces this with `build_guideline_input_for_run`, which reassembles
    the whole object and checks it against `Run.input_hash`. There is nothing
    to reassemble yet.
    """
    bindings = getattr(ctx.run, "bindings", None) or {}
    research = bindings.get("research_run_id") is not None
    plan = bindings.get("plan_id") is not None
    mode = {
        (True, True): "fully_linked",
        (True, False): "research_linked",
        (False, True): "plan_linked",
        (False, False): "standalone",
    }[(research, plan)]
    unbound = [name for name, present in (("research", research), ("plan", plan)) if not present]
    return mode, unbound


class GuidelineEntry:
    """3.0.1 — proves the pipeline starts."""

    spec = NodeSpec(
        id="3.0.1",
        name="guideline_entry",
        stage="3.0",
        run_stage=RunStage.GUIDELINE,
        task_class=TaskClass.EXTRACT,
        input_model=BaseModel,
        output_model=GuidelinePlaceholderOutput,
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        return []

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        mode, unbound = _from_run(ctx)
        return GuidelinePlaceholderOutput(node=self.spec.id, mode=mode, unbound_inputs=unbound)


class GuidelineTerminal:
    """3.0.2 — proves a second wave runs and the DAG reaches a terminal node."""

    spec = NodeSpec(
        id="3.0.2",
        name="guideline_terminal",
        stage="3.0",
        run_stage=RunStage.GUIDELINE,
        depends_on=("3.0.1",),
        task_class=TaskClass.EXTRACT,
        input_model=BaseModel,
        output_model=GuidelinePlaceholderOutput,
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        return []

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        mode, unbound = _from_run(ctx)
        return GuidelinePlaceholderOutput(node=self.spec.id, mode=mode, unbound_inputs=unbound)


#: Module-level instances. The registry discovers instances, not classes — a
#: declared-but-never-instantiated node is a node that silently never runs, so
#: `discover()` refuses it outright.
guideline_entry = GuidelineEntry()
guideline_terminal = GuidelineTerminal()
