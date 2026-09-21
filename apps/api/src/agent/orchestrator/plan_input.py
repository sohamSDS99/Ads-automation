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
from agent.db.models import Project, Report, ResearchAcceptance
from agent.export.contract import ResearchReport
from agent.schemas.plan_input import PlanInput

log = structlog.get_logger(__name__)


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
