"""Building the object that crosses into Stage 04 (PRD §4.3).

Assembly only: no network, no LLM, no connector. Every fact here was settled
by an artifact a person already signed off — an accepted report, a frozen plan,
a published rulebook — and re-deriving any of it would break law 32 in the
first function it could be broken in.

The resolvers below (`frozen_plan`, `published_pin`, `current_signoff`, …) are
also what `api/routes_creative.py` builds eligibility from. One definition of
"the frozen plan" and "the governing pin" serves both the Start button and the
run it starts, so the two cannot disagree about which plan a run writes into.

What this module *decides* is version support. Stage 04 is gated, so a skew is
the Stage 02 kind (§4.3 rule 5): a `422` at trigger time naming both
versions, before the run row exists — never a dropped input, because Stage 04
cannot do without either artifact.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
import structlog
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from agent.config import get_settings
from agent.db.models import (
    CampaignPlan,
    CampaignPlanStatus,
    ContentGuideline,
    Evidence,
    GuidelineStatus,
    HumanTask,
    HumanTaskStatus,
    MediaReference,
    Project,
    Report,
    RuleSet,
    SignOffMatrix,
)
from agent.export.contract import ResearchReport
from agent.export.plan_contract import CampaignPlan as PlanContract
from agent.export.plan_contract import Dependency
from agent.guidelines import versions
from agent.guidelines.projection import project as project_context
from agent.schemas.creative_input import (
    AudienceSlice,
    CreativeContextRef,
    CreativeInput,
    CreativeScope,
    DifferentiationClaim,
    MediaModelChoice,
    MediaReferenceRef,
    PlanRef,
    ResearchRef,
    RuleSetRef,
    SignOffMatrixRef,
)
from agent.schemas.guardrails import OfferRecord

log = structlog.get_logger(__name__)

#: The Evidence kind `csv_ingest` offer rows are stored under — the same one
#: Stage 03's 3.2.4 reads (`nodes/content/stage_3_2._offer_records`).
OFFER_RECORD_KIND = "offer_record"

#: An H2 still waiting on its named person. `not_required`, `completed` and
#: `expired` are terminal and inherit nothing.
OPEN_TASK_STATUSES: frozenset[HumanTaskStatus] = frozenset(
    {HumanTaskStatus.PENDING, HumanTaskStatus.IN_PROGRESS, HumanTaskStatus.BLOCKED}
)


class CreativeInputError(RuntimeError):
    """The upstream artifacts cannot be turned into a `CreativeInput`.

    `code` names the failure for the route; `detail` is written for the person
    reading the 422 and says what to do about it.
    """

    def __init__(self, code: str, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.extra = extra


@dataclass(frozen=True, slots=True)
class PublishedPin:
    """The published guideline and the ruleset that governs it right now."""

    guideline: ContentGuideline
    ruleset: RuleSet

    @property
    def schema_version(self) -> str:
        """Read raw from the compiled JSON, not parsed: checking the version is
        what has to happen *before* anything trusts the rest of the shape."""
        return str((self.ruleset.compiled or {}).get("schema_version", ""))


# ---------------------------------------------------------------------------
# resolvers — shared with eligibility
# ---------------------------------------------------------------------------


async def frozen_plan(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> CampaignPlan | None:
    """CR-E1: the frozen, non-superseded plan. Newest version wins."""
    return (
        await db.execute(
            sa.select(CampaignPlan)
            .where(
                CampaignPlan.workspace_id == workspace_id,
                CampaignPlan.project_id == project_id,
                CampaignPlan.status == CampaignPlanStatus.FROZEN,
                CampaignPlan.source_superseded.is_(False),
            )
            .order_by(CampaignPlan.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def latest_plan(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> CampaignPlan | None:
    """Whatever plan exists, for naming its state when CR-E1 fails."""
    return (
        await db.execute(
            sa.select(CampaignPlan)
            .where(
                CampaignPlan.workspace_id == workspace_id,
                CampaignPlan.project_id == project_id,
            )
            .order_by(CampaignPlan.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def published_pin(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> PublishedPin | None:
    """CR-E2: the latest published guideline and its governing ruleset.

    Resolved exactly as `GET /guidelines/published/ruleset` resolves it, so
    "the route returns 200" and "this returns a pin" are the same statement.
    """
    guideline = (
        await db.execute(
            sa.select(ContentGuideline)
            .where(
                ContentGuideline.workspace_id == workspace_id,
                ContentGuideline.project_id == project_id,
                ContentGuideline.status == GuidelineStatus.PUBLISHED,
            )
            .order_by(ContentGuideline.version_major.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if guideline is None:
        return None
    ruleset = await versions.current_ruleset(db, guideline)
    if ruleset is None:
        return None
    return PublishedPin(guideline=guideline, ruleset=ruleset)


async def current_signoff(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> SignOffMatrix | None:
    """CR-E5. The three owner columns are NOT NULL, so a row names all three."""
    return (
        await db.execute(
            sa.select(SignOffMatrix).where(
                SignOffMatrix.workspace_id == workspace_id,
                SignOffMatrix.project_id == project_id,
                SignOffMatrix.superseded_at.is_(None),
            )
        )
    ).scalar_one_or_none()


async def open_h2_tasks(
    db: AsyncSession, workspace_id: uuid.UUID, guideline: ContentGuideline
) -> list[HumanTask]:
    """The published guideline's H2s still waiting on their named person."""
    return list(
        (
            await db.execute(
                sa.select(HumanTask)
                .where(
                    HumanTask.workspace_id == workspace_id,
                    HumanTask.guideline_run_id == guideline.guideline_run_id,
                    HumanTask.task_key == "H2",
                    HumanTask.status.in_(OPEN_TASK_STATUSES),
                )
                .order_by(HumanTask.created_at)
            )
        )
        .scalars()
        .all()
    )


@dataclass(frozen=True, slots=True)
class OfferSnapshot:
    records: list[OfferRecord]
    #: The freshest `observed_at` (else the evidence row's `fetched_at`), or
    #: None when there are no usable rows. CR-E13 reads this.
    freshest: datetime | None


async def offer_snapshot(db: AsyncSession, project_id: uuid.UUID) -> OfferSnapshot:
    """Every usable `offer_record` evidence row for the project.

    A malformed row is skipped *loudly*, as 3.2.4 does: a silently dropped offer
    is a price nothing later checks.
    """
    rows = (
        (
            await db.execute(
                sa.select(Evidence)
                .where(Evidence.project_id == project_id, Evidence.kind == OFFER_RECORD_KIND)
                .order_by(Evidence.fetched_at, Evidence.id)
            )
        )
        .scalars()
        .all()
    )
    records: list[OfferRecord] = []
    freshest: datetime | None = None
    for row in rows:
        try:
            record = OfferRecord.model_validate(row.payload or {})
        except ValidationError as exc:
            log.warning("offer_record.unusable", evidence_id=str(row.id), errors=exc.error_count())
            continue
        records.append(record)
        seen = record.observed_at or row.fetched_at
        if freshest is None or seen > freshest:
            freshest = seen
    return OfferSnapshot(records=records, freshest=freshest)


def schema_skew(plan: CampaignPlan, pin: PublishedPin) -> str | None:
    """CR-E3: a message naming both versions, or None when both are supported."""
    settings = get_settings()
    plan_ok = plan.schema_version in settings.creative_supported_plan_schemas
    ruleset_ok = pin.schema_version in settings.creative_supported_ruleset_schemas
    if plan_ok and ruleset_ok:
        return None
    return (
        f"The frozen plan is schema {plan.schema_version or '?'} and the published ruleset "
        f"is schema {pin.schema_version or '?'}; creative reads plan "
        f"{', '.join(sorted(settings.creative_supported_plan_schemas))} and ruleset "
        f"{', '.join(sorted(settings.creative_supported_ruleset_schemas))}. Re-freeze or "
        "re-publish in a supported version."
    )


def campaign_refs(plan: PlanContract) -> list[str]:
    """Every campaign the plan defines, by the ref `CreativeScope` speaks in."""
    return [c.campaign_ref or c.name for c in plan.account_structure.campaigns]


# ---------------------------------------------------------------------------
# the builder
# ---------------------------------------------------------------------------


def reject_media(scope: CreativeScope, media_models: list[MediaModelChoice]) -> None:
    """S4-P0 accepts `media_models=[]` and a text-only scope, and nothing else.

    There is no media gateway yet, so a requested model — or a scope that
    would need one — is refused rather than accepted and silently ignored.
    S4-P1 replaces this with allowlist, catalogue and capability validation
    (law 36).
    """
    if media_models or scope.images or scope.video:
        raise CreativeInputError(
            "media_not_configured",
            "Image and video generation are not available yet: no media model can be "
            "selected in this release. Start a text-only run (images and video off, no "
            "media models).",
        )



async def build_creative_input(
    db: AsyncSession,
    project_id: uuid.UUID,
    scope: CreativeScope,
    media_models: list[MediaModelChoice],
    *,
    workspace_id: uuid.UUID,
    creative_run_id: uuid.UUID,
) -> tuple[CreativeInput, str]:
    """The `CreativeInput` a run will start from, and its sha256. Raises
    `CreativeInputError`.

    `creative_run_id` is minted by the caller before the run row exists: the
    input names its run and is hashed into that run's `input_hash`, so the id
    has to be known before either is written.
    """
    reject_media(scope, media_models)

    project = (
        await db.execute(
            sa.select(Project).where(Project.id == project_id, Project.workspace_id == workspace_id)
        )
    ).scalar_one_or_none()
    if project is None:
        raise CreativeInputError("project_missing", f"No project {project_id}.")

    plan_row = await frozen_plan(db, workspace_id, project_id)
    if plan_row is None:
        raise CreativeInputError(
            "no_frozen_plan", "This project has no frozen campaign plan to write creative into."
        )
    pin = await published_pin(db, workspace_id, project_id)
    if pin is None:
        raise CreativeInputError(
            "no_published_ruleset",
            "This project has no published content guidelines, so there are no rules to "
            "check creative against.",
        )
    skew = schema_skew(plan_row, pin)
    if skew is not None:
        raise CreativeInputError(
            "schema_unsupported",
            skew,
            plan_schema_version=plan_row.schema_version,
            ruleset_schema_version=pin.schema_version,
        )

    plan = PlanContract.model_validate(plan_row.payload)
    report = (
        await db.execute(sa.select(Report).where(Report.id == plan.source.report_id))
    ).scalar_one_or_none()
    if report is None:
        raise CreativeInputError(
            "report_missing",
            "The research report behind the frozen plan no longer exists. Re-run research, "
            "re-plan and freeze again.",
        )
    research = ResearchReport.model_validate(report.payload)

    known = campaign_refs(plan)
    wanted = list(scope.campaign_refs) or known
    unknown = sorted(set(wanted) - set(known))
    if unknown:
        raise CreativeInputError(
            "scope_unknown_campaign",
            f"The frozen plan (v{plan_row.version}) has no campaign {', '.join(unknown)}. "
            f"It defines: {', '.join(known) or 'none'}.",
        )

    signoff = await current_signoff(db, workspace_id, project_id)
    if signoff is None:
        raise CreativeInputError(
            "no_signoff_matrix",
            "No current sign-off matrix names brand, legal and performance owners, so the "
            "brief, media and legal reviews have nobody to route to.",
        )

    context = project_context(pin.guideline, pin.ruleset)
    offers = await offer_snapshot(db, project_id)
    references = (
        (
            await db.execute(
                sa.select(MediaReference)
                .where(
                    MediaReference.workspace_id == workspace_id,
                    MediaReference.project_id == project_id,
                    MediaReference.retired_at.is_(None),
                )
                .order_by(MediaReference.created_at, MediaReference.id)
            )
        )
        .scalars()
        .all()
    )
    h2 = await open_h2_tasks(db, workspace_id, pin.guideline)

    built = CreativeInput(
        project_id=project_id,
        creative_run_id=creative_run_id,
        plan_ref=PlanRef(
            plan_id=plan_row.id,
            version=plan_row.version,
            schema_version=plan_row.schema_version,
            plan_run_id=plan_row.plan_run_id,
        ),
        research_ref=ResearchRef(
            research_run_id=plan.source.research_run_id,
            report_id=plan.source.report_id,
            acceptance_id=plan.source.acceptance_id,
        ),
        ruleset_ref=RuleSetRef(
            guideline_id=pin.guideline.id,
            ruleset_version=pin.ruleset.ruleset_version,
            hash=pin.ruleset.hash,
        ),
        context_ref=CreativeContextRef(hash=context.hash),
        scope=scope.model_copy(update={"campaign_refs": wanted}),
        media_models=[],
        audience=AudienceSlice(
            best_customers=research.business_context.segments,
            not_wanted=research.business_context.exclusions,
        ),
        differentiation=DifferentiationClaim(
            recommended_claim=research.competitive_landscape.recommended_claim,
            whitespace=research.competitive_landscape.whitespace,
            substantiation_required=research.competitive_landscape.substantiation_required,
        ),
        competitor_messages=research.competitive_landscape.message_clusters,
        keyword_page_map=research.demand_map.mapping,
        landing_audit=research.readiness.pages,
        objectives=plan.objectives,
        lead_definition=plan.objectives.qualified_lead,
        channel_slate=plan.channel_slate,
        account_structure=plan.account_structure,
        naming=plan.account_structure.naming_convention,
        creative_context=context,
        offer_records=offers.records,
        offer_snapshot_at=datetime.now(UTC),
        references=[
            MediaReferenceRef(
                reference_id=ref.id,
                kind=ref.kind.value,
                origin=ref.origin.value,
                sha256=ref.sha256,
                media_type=ref.media_type,
                width=ref.width,
                height=ref.height,
                product_ref=ref.product_ref,
                rights_statement=ref.rights_statement,
                attested_by=ref.attested_by,
                attested_at=ref.attested_at,
            )
            for ref in references
        ],
        signoff_matrix=SignOffMatrixRef(
            matrix_id=signoff.id,
            version=signoff.version,
            brand_owner_id=signoff.brand_owner_id,
            legal_owner_id=signoff.legal_owner_id,
            performance_owner_id=signoff.performance_owner_id,
        ),
        inherited_dependencies=[
            *plan.open_dependencies,
            *(
                Dependency(
                    task=task.title,
                    owner=str(task.assignee_id),
                    blocking=True,
                    source=f"guideline:H2:{task.id}",
                )
                for task in h2
            ),
        ],
        constants_version=get_settings().creative_constants_version,
    )
    input_hash = built.content_hash()
    log.info(
        "creative_input.built",
        project_id=str(project_id),
        creative_run_id=str(creative_run_id),
        plan_version=plan_row.version,
        ruleset_version=pin.ruleset.ruleset_version,
        input_hash=input_hash,
    )
    return built, input_hash
