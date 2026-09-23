"""Assembling the object a guideline run executes against (PRD §4.5).

Assembly only. No network, no LLM, no connector — the same discipline as
`plan_input.py`, for the same reason.

What is **not** the same is what happens when something is missing. Stage 02's
builder raises `PlanInputError`, the route turns it into a 422, and that is
correct there: a plan without the research it was built from is not a plan.
Stage 03 has no such dependency. Every binding here resolves independently and
a binding that does not resolve is dropped, recorded in `unbound_inputs`, and
the run continues with a wider scope (law 21).

So there is no `GuidelineInputError`. That absence is the design, not an
omission — the module has no failure mode to report because a missing upstream
is not a failure.

Resolution is workspace- *and* project-scoped throughout. A binding is a
pointer supplied by a caller, so treating it as one that has already been
authorized is how another project's research ends up in this project's
rulebook.
"""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from agent.config import get_settings
from agent.db.models import (
    CampaignPlan,
    CampaignPlanStatus,
    ContentGuideline,
    GuidelineStatus,
    Project,
    Report,
    ResearchAcceptance,
    Run,
    RunStage,
    SignOffMatrix,
)
from agent.export.contract import ComplianceGuardrails
from agent.guidelines.constants import get_content_constants
from agent.schemas.guideline_input import GuidelineBindings, GuidelineInput

log = structlog.get_logger(__name__)

#: Where a project stores its overrides of `content_constants.yaml`
#: (PRD §7.1). Read by S3-P1's `constants.py`; named here so the two phases
#: cannot disagree about the key.
CONTENT_OVERRIDES = "content_overrides"


async def build_guideline_input(
    db: AsyncSession,
    project_id: uuid.UUID,
    requested: GuidelineBindings,
    *,
    workspace_id: uuid.UUID,
) -> tuple[GuidelineInput, str]:
    """The `GuidelineInput` for one run, and the hash `Run.input_hash` stores.

    `requested` is what the caller asked to bind. What comes back on
    `GuidelineInput.bindings` is what actually resolved, which is why `mode` is
    derived from the second and never from the first.
    """
    settings = get_settings()
    project = await db.get(Project, project_id)
    if project is None:  # pragma: no cover — the route checks first
        raise LookupError(f"no project {project_id}")

    unbound: list[str] = []
    resolved = GuidelineBindings()
    guardrails: ComplianceGuardrails | None = None
    differentiation: dict[str, Any] | None = None
    competitor_creative: list[Any] | None = None
    channel_slate: dict[str, Any] | None = None
    account_structure: dict[str, Any] | None = None
    measurement_consent: dict[str, Any] | None = None

    # -- the research binding ----------------------------------------------
    research_fields: dict[str, object] = {}
    if requested.research_run_id is None:
        unbound.append("research")
    else:
        acceptance, report = await _research(
            db,
            workspace_id=workspace_id,
            project_id=project_id,
            run_id=requested.research_run_id,
        )
        if acceptance is None or report is None:
            unbound.append("research: no accepted research run with that id on this project")
        elif report.schema_version not in settings.guideline_supported_research_schemas:
            # Dropped, not raised. See the module docstring.
            unbound.append(
                f"research: report schema {report.schema_version} is not one of "
                f"{', '.join(sorted(settings.guideline_supported_research_schemas))}"
            )
        else:
            research_fields = {
                "research_run_id": acceptance.run_id,
                "acceptance_id": acceptance.id,
                "research_schema_version": report.schema_version,
            }
            guardrails, differentiation, competitor_creative = _from_report(report)

    # -- the plan binding, resolved independently of the research one -------
    plan_fields: dict[str, object] = {}
    if requested.plan_id is None:
        unbound.append("plan")
    else:
        plan = await _frozen_plan(
            db, workspace_id=workspace_id, project_id=project_id, plan_id=requested.plan_id
        )
        if plan is None:
            unbound.append("plan: no frozen plan with that id on this project")
        elif plan.schema_version not in settings.guideline_supported_plan_schemas:
            unbound.append(
                f"plan: schema {plan.schema_version} is not one of "
                f"{', '.join(sorted(settings.guideline_supported_plan_schemas))}"
            )
        else:
            plan_fields = {
                "plan_id": plan.id,
                "plan_version": plan.version,
                "plan_schema_version": plan.schema_version,
            }
            channel_slate, account_structure, measurement_consent = _from_plan(plan)

    resolved = GuidelineBindings(**research_fields, **plan_fields)

    matrix = await _current_matrix(db, workspace_id=workspace_id, project_id=project_id)
    prior = await _published(db, workspace_id=workspace_id, project_id=project_id)

    built = GuidelineInput(
        project_id=project_id,
        guideline_run_id=uuid.UUID(int=0),  # replaced by the caller once the row exists
        bindings=resolved,
        product_context=project.product_context or {},
        markets=project.markets or [],
        signoff_matrix=matrix,
        prior_guideline_id=prior,
        compliance_guardrails=guardrails,
        differentiation_claim=differentiation,
        competitor_creative=competitor_creative,
        channel_slate=channel_slate,
        account_structure=account_structure,
        measurement_consent=measurement_consent,
        unbound_inputs=unbound,
        # The LOADED constants, not `settings.content_constants_version`.
        # S3-P0 added that Settings field as a placeholder and S3-P1 then made
        # `content_constants.yaml` the real source, leaving two copies of one
        # string. They agreed until this phase edited the YAML — at which point
        # every run would have recorded `2026.09.1` while the `RuleSet` compiled
        # from the same file recorded `2026.09.2`, and the pair that is supposed
        # to make a verdict re-derivable would have disagreed about which
        # constants produced it. `test_constants_version_has_one_source` fails
        # if the two ever drift again.
        constants_version=get_content_constants().version,
    )
    if unbound:
        log.info(
            "guideline_input.unbound",
            project_id=str(project_id),
            mode=built.mode.value,
            unbound=unbound,
        )
    return built, built.content_hash()


# ---------------------------------------------------------------------------
# resolution — every one of these returns None rather than raising
# ---------------------------------------------------------------------------


async def _research(
    db: AsyncSession, *, workspace_id: uuid.UUID, project_id: uuid.UUID, run_id: uuid.UUID
) -> tuple[ResearchAcceptance | None, Report | None]:
    """The current acceptance of `run_id`, if it belongs to this project."""
    acceptance = (
        await db.execute(
            sa.select(ResearchAcceptance)
            .join(Run, Run.id == ResearchAcceptance.run_id)
            .where(
                ResearchAcceptance.run_id == run_id,
                ResearchAcceptance.workspace_id == workspace_id,
                ResearchAcceptance.project_id == project_id,
                ResearchAcceptance.superseded_by.is_(None),
                Run.stage == RunStage.RESEARCH,
            )
        )
    ).scalar_one_or_none()
    if acceptance is None:
        return None, None
    return acceptance, await db.get(Report, acceptance.report_id)


async def _frozen_plan(
    db: AsyncSession, *, workspace_id: uuid.UUID, project_id: uuid.UUID, plan_id: uuid.UUID
) -> CampaignPlan | None:
    """A *frozen* plan on this project. A draft is still moving (§4.2)."""
    return (
        await db.execute(
            sa.select(CampaignPlan).where(
                CampaignPlan.id == plan_id,
                CampaignPlan.workspace_id == workspace_id,
                CampaignPlan.project_id == project_id,
                CampaignPlan.status == CampaignPlanStatus.FROZEN,
            )
        )
    ).scalar_one_or_none()


async def _current_matrix(
    db: AsyncSession, *, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> uuid.UUID | None:
    return (
        await db.execute(
            sa.select(SignOffMatrix.id).where(
                SignOffMatrix.workspace_id == workspace_id,
                SignOffMatrix.project_id == project_id,
                SignOffMatrix.superseded_at.is_(None),
            )
        )
    ).scalar_one_or_none()


async def _published(
    db: AsyncSession, *, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> uuid.UUID | None:
    return (
        await db.execute(
            sa.select(ContentGuideline.id)
            .where(
                ContentGuideline.workspace_id == workspace_id,
                ContentGuideline.project_id == project_id,
                ContentGuideline.status == GuidelineStatus.PUBLISHED,
            )
            .order_by(ContentGuideline.version_major.desc(), ContentGuideline.version_minor.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# slicing the two upstream payloads
# ---------------------------------------------------------------------------


def _from_report(
    report: Report,
) -> tuple[ComplianceGuardrails | None, dict[str, Any] | None, list[Any] | None]:
    """1.1.5, 1.3.4 and 1.3.2 out of an accepted research payload.

    **The paths are not the names PRD §4.5 uses.** The PRD calls these
    `compliance_guardrails`, `differentiation_claim` and `competitor_creative`;
    the shipped Stage 01 contract nests them as `business_context.compliance`,
    `competitive_landscape.recommended_claim` and `competitive_landscape.ads`.
    The PRD's names are kept on `GuidelineInput` because that is the contract
    Stage 03's own nodes read — this function is the one place the two
    vocabularies meet, so a Stage 01 rename breaks here and nowhere else.

    Read defensively throughout: the payload is an immutable JSON blob written
    by an earlier stage, and a section that is absent or malformed narrows
    scope rather than failing the run.
    """
    payload = report.payload or {}
    business = payload.get("business_context")
    landscape = payload.get("competitive_landscape")
    business = business if isinstance(business, dict) else {}
    landscape = landscape if isinstance(landscape, dict) else {}

    guardrails = None
    raw = business.get("compliance")
    if isinstance(raw, dict):
        try:
            guardrails = ComplianceGuardrails.model_validate(raw)
        except ValueError:  # pragma: no cover — a malformed section is scope, not a crash
            log.warning("guideline_input.guardrails_unreadable", report_id=str(report.id))

    claim = landscape.get("recommended_claim")
    differentiation: dict[str, Any] | None = None
    if isinstance(claim, dict):
        # `substantiation_required[]` rides along: §4.2 says the claim register
        # inherits it, and it lives beside the claim rather than inside it.
        required = landscape.get("substantiation_required")
        differentiation = {
            **claim,
            "substantiation_required": required if isinstance(required, list) else [],
        }

    ads = landscape.get("ads")
    return guardrails, differentiation, ads if isinstance(ads, list) else None


def _from_plan(
    plan: CampaignPlan,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    """2.3.1, 2.4.2 and 2.5.2's consent outcome out of a frozen plan."""
    payload = plan.payload or {}
    slate = payload.get("channel_slate")
    structure = payload.get("account_structure")
    measurement = payload.get("measurement_plan")
    consent = None
    if isinstance(measurement, dict):
        consent = {
            key: measurement[key]
            for key in (
                "consent_signal",
                "consent_markets_allowed",
                "consent_markets_blocked",
                "consent_basis",
            )
            if key in measurement
        } or None
    return (
        slate if isinstance(slate, dict) else None,
        structure if isinstance(structure, dict) else None,
        consent,
    )
