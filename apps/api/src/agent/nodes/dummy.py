"""Two trivial nodes, and the only DAG that exists until P3.

PRD §17 gives P1 "2 trivial dummy nodes" so the executor, the gateway and the
run API can be proved end to end before a single real node is written. They are
deliberately minimal: one depends on the other, both round-trip the model
through the same structured-output path a real node uses, and neither touches a
connector.

**P3 deletes this module** and the ids move to the 1.x namespace of PRD §10.
Nothing outside the tests should grow a dependency on `0.1` or `0.2`.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, Field

from agent.db.models import Evidence
from agent.llm.router import TaskClass
from agent.nodes.base import LLMNode, NodeSpec, RunContext


class ProjectBrief(BaseModel):
    """What node 0.1 is asked for."""

    summary: str = Field(description="One sentence describing what this business sells.")
    keywords: list[str] = Field(default_factory=list, description="Up to five topical keywords.")


class BriefCritique(BaseModel):
    """What node 0.2 is asked for, given 0.1's answer."""

    verdict: str = Field(description="'useful' or 'thin'.")
    missing: list[str] = Field(default_factory=list, description="What a real brief would add.")


class ProjectBriefNode(LLMNode):
    """0.1 — describe the project from its own settings. No dependencies."""

    spec = NodeSpec(
        id="0.1",
        name="project_brief",
        stage="0",
        task_class=TaskClass.EXTRACT,
        input_model=BaseModel,
        output_model=ProjectBrief,
    )

    def system_prompt(self, ctx: RunContext) -> str:
        return (
            "You summarise a company for a paid-search research brief. "
            "Answer only from the context given."
        )

    def user_prompt(self, ctx: RunContext, ev: Sequence[Evidence]) -> str:
        return (
            f"Company domain: {ctx.project.domain}\n"
            f"Project name: {ctx.project.name}\n"
            f"Product context: {ctx.project.product_context}"
        )


class BriefCritiqueNode(LLMNode):
    """0.2 — read 0.1's output and say what is missing. Depends on 0.1."""

    spec = NodeSpec(
        id="0.2",
        name="brief_critique",
        stage="0",
        depends_on=("0.1",),
        task_class=TaskClass.CRITIQUE,
        input_model=ProjectBrief,
        output_model=BriefCritique,
    )

    def system_prompt(self, ctx: RunContext) -> str:
        return "You review research briefs for gaps. Be terse."

    def user_prompt(self, ctx: RunContext, ev: Sequence[Evidence]) -> str:
        brief = ctx.output_of("0.1")
        return f"Brief under review:\n{brief}"


project_brief = ProjectBriefNode()
brief_critique = BriefCritiqueNode()
