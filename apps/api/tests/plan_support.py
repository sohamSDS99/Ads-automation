"""A plan node, run in a unit test, without a database or a model provider.

The four Stage 2.1 nodes each split into "read the CRM, compute a ceiling" and
"ask for labels, merge the figures in". The second half is where every defect
this suite is looking for lives — a target above its ceiling, a rank the model
chose being thrown away, a figure the node recomputed instead of copying — and
none of it needs Postgres or OpenRouter to exercise.

So: `gather.collect` is stubbed to return prepared evidence, the LLM is a
scripted dict, and `PlanCalcRunner` is the **real one** with a stub writer.
That last choice matters. Faking the runner would leave `NodeSpec.calc` — the
allow-list that makes law 14 enforceable — untested in exactly the place it is
used.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from agent.calc.registry import CalcResult
from agent.db.models import Evidence, EvidenceSource, Project, Run, RunStage
from agent.llm.router import TaskClass
from agent.nodes import gather
from agent.nodes.base import PlanContext, RunContext
from agent.orchestrator.plan_calc import PlanCalcRunner
from agent.planning.constants import PlanningConstants, load_planning_constants
from agent.schemas.plan_input import PlanInput

CONSTANTS: PlanningConstants = load_planning_constants()

WORKSPACE_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
PROJECT_ID = uuid.UUID("00000000-0000-4000-8000-000000000002")
PLAN_RUN_ID = uuid.UUID("00000000-0000-4000-8000-000000000003")
RESEARCH_RUN_ID = uuid.UUID("00000000-0000-4000-8000-000000000004")


# ---------------------------------------------------------------------------
# the fakes
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class StubWriter:
    """`DerivedWriter` without a database.

    Mints one Evidence id per distinct `(formula_id, inputs_hash)`, which is
    the dedupe key the real writer's unique constraint enforces — so a test
    that calls the same formula twice sees one citation here too, exactly as
    it would against Postgres.
    """

    rows: dict[tuple[str, str], Evidence] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    async def record(self, result: CalcResult, *, node_id: str) -> uuid.UUID:
        self.calls.append(f"{node_id}:{result.formula_id}")
        key = (result.formula_id, result.inputs_hash)
        existing = self.rows.get(key)
        if existing is not None:
            return existing.id
        row = Evidence(
            id=uuid.uuid4(),
            project_id=PROJECT_ID,
            run_id=PLAN_RUN_ID,
            source=EvidenceSource.DERIVED,
            kind=result.kind,
            payload=result.payload(),
            content_text=result.summary,
            hash=result.inputs_hash,
        )
        self.rows[key] = row
        return row.id

    async def get(self, model: type[Any], entity_id: uuid.UUID) -> Evidence | None:
        """Stands in for `AsyncSession.get`, which is all the runner needs."""
        return next((row for row in self.rows.values() if row.id == entity_id), None)


@dataclass(slots=True)
class Usage:
    prompt_tokens: int = 10
    completion_tokens: int = 20


@dataclass(slots=True)
class Completion:
    value: BaseModel
    model: str = "test/model"
    usage: Usage = field(default_factory=Usage)
    cost_usd: Decimal = Decimal("0.001")
    repairs: int = 0
    latency_ms: int = 1
    prompt: str = ""


@dataclass(slots=True)
class ScriptedLLM:
    """Answers each completion from a table keyed by output-model name.

    The same dispatch the integration suite's `by_output_model` uses, so a
    node's scripted answer reads the same in both places.
    """

    answers: dict[str, Any]
    prompts: list[tuple[str, str]] = field(default_factory=list)

    async def complete_structured(
        self, *, output_model: type[BaseModel], system: str, user: str, choice: Any, **_: Any
    ) -> Completion:
        name = output_model.__name__
        if name not in self.answers:
            raise AssertionError(f"no scripted answer for {name}")
        self.prompts.append((name, user))
        return Completion(value=output_model.model_validate(self.answers[name]), prompt=user)

    def user_prompt(self, output_model: str) -> str:
        return next(user for name, user in self.prompts if name == output_model)


class StubRouter:
    def choose(self, task_class: TaskClass) -> str:
        return "test/model"

    def chain(self, task_class: TaskClass) -> tuple[str, ...]:
        return ("test/model",)


@dataclass(slots=True)
class StubLedger:
    spent: Decimal = Decimal(0)

    def record(self, *, usage: Any, cost: Decimal) -> None:
        self.spent += cost


# ---------------------------------------------------------------------------
# the context
# ---------------------------------------------------------------------------


def evidence(kind: str, payload: dict[str, Any]) -> Evidence:
    """One Stage 01 evidence row, unsaved."""
    return Evidence(
        id=uuid.uuid4(),
        project_id=PROJECT_ID,
        source=EvidenceSource.CSV,
        kind=kind,
        payload=payload,
        hash=uuid.uuid4().hex,
    )


def research_report(**sections: Any) -> dict[str, Any]:
    """The smallest research report the contract accepts, plus the sections given.

    Built from `report_support.minimal_report` rather than declared again here:
    a second spelling of "the smallest valid report" would drift from the one
    the export suite asserts against, and this is the object Stage 02 reads.
    """
    from tests.report_support import minimal_report

    return minimal_report(
        executive_summary="Ready to plan.", launch_readiness="go", **sections
    ).model_dump(mode="json")


def plan_input(report: dict[str, Any], **overrides: Any) -> PlanInput:
    """A `PlanInput` around one research report payload."""
    from datetime import UTC, datetime

    from agent.export.contract import ResearchReport

    research = ResearchReport.model_validate(report)
    return PlanInput(
        project_id=PROJECT_ID,
        research_run_id=RESEARCH_RUN_ID,
        research_report_id=uuid.uuid4(),
        research_schema_version="1.0",
        accepted_by=uuid.uuid4(),
        accepted_at=datetime(2026, 9, 1, tzinfo=UTC),
        launch_readiness=research.launch_readiness,
        launch_blockers=research.launch_blockers,
        business_context=research.business_context,
        account_learnings=research.account_learnings,
        competitive_landscape=research.competitive_landscape,
        demand_map=research.demand_map,
        readiness=research.readiness,
        priced_keyword_list=research.priced_keyword_list,
        degraded_sources=research.degraded_sources,
        **overrides,
    )


@dataclass(slots=True)
class Harness:
    """Everything a test needs to drive one node and inspect what it did."""

    ctx: RunContext
    llm: ScriptedLLM
    writer: StubWriter
    gathered: gather.Gathered


def harness(
    node_id: str,
    *,
    answers: dict[str, Any],
    gathered: gather.Gathered,
    source: PlanInput,
    permitted: tuple[str, ...],
    outputs: dict[str, dict[str, Any]] | None = None,
    constants: PlanningConstants = CONSTANTS,
) -> Harness:
    """A `RunContext` for one plan node, with a real calc runner behind it."""
    writer = StubWriter()
    llm = ScriptedLLM(answers=answers)
    runner = PlanCalcRunner(
        node_id=node_id,
        permitted=permitted,
        writer=writer,  # type: ignore[arg-type]
        constants=constants,
        session=writer,  # type: ignore[arg-type]
    )
    ctx = RunContext(
        run=Run(id=PLAN_RUN_ID, workspace_id=WORKSPACE_ID, stage=RunStage.PLAN),
        project=Project(
            id=PROJECT_ID,
            workspace_id=WORKSPACE_ID,
            name="SDS Manager",
            domain="sdsmanager.com",
            product_context={},
            markets=[],
            settings={},
        ),
        db=None,  # type: ignore[arg-type]
        llm=llm,  # type: ignore[arg-type]
        router=StubRouter(),  # type: ignore[arg-type]
        ledger=StubLedger(),  # type: ignore[arg-type]
        outputs=outputs or {},
        node_id=node_id,
        plan=PlanContext(input=source, calc=runner, constants=constants),
    )
    return Harness(ctx=ctx, llm=llm, writer=writer, gathered=gathered)
