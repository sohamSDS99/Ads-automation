"""Building the object that crosses from Stage 01 to Stage 02 (PRD §4.3).

Assembly only. No network, no LLM, no connector — every fact here was
established by a research run that has already finished and been accepted by a
person, and re-deriving any of it would be Stage 02 law 13 broken in the first
function it could be broken in.

The one thing this module *decides* is version support. An unsupported
`research_schema_version` raises `PlanInputError`, the caller turns that into a
`422`, and PRD §17 PR3 — "fails at trigger time, before any token is spent" —
is satisfied by the fact that this runs before the run row exists.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from agent.config import get_settings
from agent.db.models import Project, Report, ResearchAcceptance, Run, RunStage
from agent.export.contract import ResearchReport
from agent.planning.constants import PlanningConstants, get_planning_constants
from agent.schemas.plan_input import PlanInput

log = structlog.get_logger(__name__)

#: Where a project stores its overrides of `planning_constants.yaml`
#: (Stage 02 PRD §7.1, §9.3). Merged at run start, never at formula-call time:
#: every `PlanCalc` row in one run must claim the same `calc_version`, and a
#: per-call merge is how two nodes in the same run end up disagreeing about
#: which thresholds they used.
PLANNING_OVERRIDES = "planning_overrides"


class PlanInputError(RuntimeError):
    """The accepted research cannot be turned into a `PlanInput`.

    Carries `code` so the route can name the failure rather than paraphrase it,
    and `detail` so the person reading the 422 learns which two versions
    disagree.
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


async def build_plan_input(
    db: AsyncSession, acceptance_id: uuid.UUID, *, workspace_id: uuid.UUID
) -> PlanInput:
    """The `PlanInput` for one acceptance. Raises `PlanInputError`.

    Workspace-scoped like every other read: the acceptance is looked up *with*
    its workspace rather than looked up and then checked, so a caller cannot
    forget the second half.
    """
    acceptance = (
        await db.execute(
            sa.select(ResearchAcceptance).where(
                ResearchAcceptance.id == acceptance_id,
                ResearchAcceptance.workspace_id == workspace_id,
            )
        )
    ).scalar_one_or_none()
    if acceptance is None:
        raise PlanInputError("acceptance_not_found", f"No research acceptance {acceptance_id}.")

    report = await db.get(Report, acceptance.report_id)
    if report is None:  # pragma: no cover — the FK makes this unreachable
        raise PlanInputError(
            "report_missing",
            "The accepted report no longer exists. Re-run research and accept it again.",
        )

    supported = get_settings().plan_supported_research_schemas
    if report.schema_version not in supported:
        raise PlanInputError(
            "research_schema_unsupported",
            f"This report is research schema {report.schema_version}; campaign planning "
            f"reads {', '.join(sorted(supported))}. Re-run research to produce a report "
            f"in a supported version.",
        )

    project = await db.get(Project, acceptance.project_id)
    if project is None:  # pragma: no cover — the FK makes this unreachable
        raise PlanInputError("project_missing", "The project this acceptance belongs to is gone.")

    # `extra="allow"` on the report models means this round-trip keeps fields a
    # later Stage 01 added and this code does not name — which is the point of
    # validating the payload rather than reading keys out of the dict.
    research = ResearchReport.model_validate(report.payload)

    plan_input = PlanInput(
        project_id=acceptance.project_id,
        research_run_id=acceptance.run_id,
        research_report_id=report.id,
        research_schema_version=report.schema_version,
        accepted_by=acceptance.accepted_by,
        accepted_at=acceptance.accepted_at,
        acceptance_note=acceptance.note,
        override_reason=acceptance.override_reason,
        launch_readiness=research.launch_readiness,
        launch_blockers=research.launch_blockers,
        business_context=research.business_context,
        account_learnings=research.account_learnings,
        competitive_landscape=research.competitive_landscape,
        demand_map=research.demand_map,
        readiness=research.readiness,
        priced_keyword_list=research.priced_keyword_list,
        degraded_sources=research.degraded_sources,
        markets=list(project.markets),
        product_context=dict(project.product_context),
    )
    log.info(
        "plan_input.built",
        acceptance_id=str(acceptance_id),
        research_run_id=str(acceptance.run_id),
        input_hash=plan_input.content_hash(),
        degraded_sources=plan_input.degraded_sources,
    )
    return plan_input


async def build_plan_input_for_run(
    db: AsyncSession, run: Run, *, workspace_id: uuid.UUID
) -> PlanInput:
    """The `PlanInput` a plan run executes against, rebuilt from its source.

    The worker does not receive the object the route built — it receives a run
    id — so the object is assembled a second time from the same acceptance. It
    is the same assembly, from the same immutable `Report` payload, so the two
    agree; `Run.input_hash` is how that is *checked* rather than assumed, and a
    drift is logged loudly with both hashes.

    A drift is not fatal. The two mutable inputs are `Project.markets` and
    `Project.product_context`, and killing a plan run because somebody added a
    market between clicking Start and the worker picking the job up would be a
    worse failure than planning on the newer facts and saying so. What must not
    happen is a silent one, and `plan_input.hash_drift` is a line somebody can
    search for.
    """
    if run.stage is not RunStage.PLAN or run.source_run_id is None:
        raise PlanInputError(
            "not_a_plan_run",
            f"Run {run.id} is a {run.stage.value} run; only a plan run has a PlanInput.",
        )

    acceptance = (
        await db.execute(
            sa.select(ResearchAcceptance).where(
                ResearchAcceptance.run_id == run.source_run_id,
                ResearchAcceptance.workspace_id == workspace_id,
            )
        )
    ).scalar_one_or_none()
    if acceptance is None:
        raise PlanInputError(
            "acceptance_missing",
            f"The research acceptance behind plan run {run.id} no longer exists. "
            "Accept the research run again and start a new plan.",
        )

    plan_input = await build_plan_input(db, acceptance.id, workspace_id=workspace_id)
    observed = plan_input.content_hash()
    if run.input_hash and observed != run.input_hash:
        log.warning(
            "plan_input.hash_drift",
            run_id=str(run.id),
            launched_with=run.input_hash,
            executing_with=observed,
            detail="the project changed between launch and execution",
        )
    return plan_input


def constants_for(project: Project) -> PlanningConstants:
    """`planning_constants.yaml` with this project's overrides merged in.

    Raises `ConstantsError` on an override naming a key that does not exist or
    carrying something that is not a number — at run start, where it is one
    project's misconfiguration, rather than mid-run where it is a dead plan.
    """
    overrides = (project.settings or {}).get(PLANNING_OVERRIDES)
    return get_planning_constants().merged(overrides if isinstance(overrides, dict) else None)
