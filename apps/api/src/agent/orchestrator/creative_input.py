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
from decimal import Decimal
from typing import Any, Literal

import sqlalchemy as sa
import structlog
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from agent.calc.media import TEXT_ESTIMATE_USD
from agent.config import get_settings
from agent.creative.constants import creative_constants_for
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
    Workspace,
)
from agent.export.contract import ResearchReport
from agent.export.plan_contract import CampaignPlan as PlanContract
from agent.export.plan_contract import Dependency
from agent.guidelines import versions
from agent.guidelines.projection import project as project_context
from agent.media.budget import BudgetCaps
from agent.media.capability import capability_hash, validate
from agent.media.catalogue import CatalogueUnavailable, MediaCatalogue
from agent.media.types import CapabilityRecord, ImageRequest, MediaRequest, VideoRequest
from agent.schemas.creative_input import (
    AudienceSlice,
    CreativeContextRef,
    CreativeInput,
    CreativeScope,
    DifferentiationClaim,
    MediaModelChoice,
    MediaModelSelection,
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


MEDIA_ALLOWLIST_SETTING = "media_allowlist"
MEDIA_DEFAULTS_SETTING = "media_defaults"
MEDIA_MODELS_SETTING = "media_models"
_MODALITIES: tuple[Literal["image", "video"], ...] = ("image", "video")
#: The request fields a default may set. Model, prompt, references and frames
#: belong to a job, never to a default.
DEFAULT_FIELDS: dict[str, frozenset[str]] = {
    "image": frozenset(
        {
            "aspect_ratio",
            "resolution",
            "size",
            "quality",
            "output_format",
            "background",
            "output_compression",
            "n",
            "seed",
        }
    ),
    "video": frozenset(
        {"duration", "resolution", "aspect_ratio", "size", "generate_audio", "seed"}
    ),
}


def enabled_modalities(scope: CreativeScope) -> list[Literal["image", "video"]]:
    return [m for m in _MODALITIES if (scope.images if m == "image" else scope.video)]


def default_selections(project: Project) -> list[MediaModelSelection]:
    """The project's default model per modality (`settings.media_models`)."""
    media = (project.settings or {}).get(MEDIA_MODELS_SETTING) or {}
    selections: list[MediaModelSelection] = []
    if not isinstance(media, dict):
        return selections
    for modality in _MODALITIES:
        entry = media.get(modality)
        if isinstance(entry, dict) and entry.get("model_id"):
            selections.append(
                MediaModelSelection(
                    modality=modality,
                    model_id=str(entry["model_id"]),
                    provider_tag=entry.get("provider_tag"),
                    defaults=dict(entry.get("defaults") or {}),
                )
            )
    return selections


def allowlist(workspace: Workspace | None, modality: str) -> list[dict[str, Any]]:
    raw = ((workspace.settings or {}) if workspace else {}).get(MEDIA_ALLOWLIST_SETTING) or {}
    entries = raw.get(modality) if isinstance(raw, dict) else None
    return [e for e in entries or [] if isinstance(e, dict)]


async def resolve_media_models(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    scope: CreativeScope,
    selections: list[MediaModelSelection],
    *,
    catalogue: MediaCatalogue,
) -> list[MediaModelChoice]:
    """CR-E8 and Law 36: one selection per enabled modality, each on the
    admin allowlist, present in the live catalogue, with defaults the pinned
    capability record accepts. Returns the choices with their capability
    snapshotted; raises `CreativeInputError` naming the modality otherwise.
    """
    wanted = enabled_modalities(scope)
    seen: set[str] = set()
    for selection in selections:
        if selection.modality not in wanted or selection.modality in seen:
            raise CreativeInputError(
                "media_model_out_of_scope",
                f"A {selection.modality} model was selected, but this run's scope "
                + (
                    "already has one."
                    if selection.modality in seen
                    else f"has {selection.modality} off."
                ),
                modality=selection.modality,
            )
        seen.add(selection.modality)

    workspace = await db.get(Workspace, workspace_id)
    by_modality = {s.modality: s for s in selections}
    choices: list[MediaModelChoice] = []
    for modality in wanted:
        chosen = by_modality.get(modality)
        if chosen is None:
            raise CreativeInputError(
                "media_model_unselected",
                f"{modality.capitalize()} is on for this run but no {modality} model is "
                "selected. Choose one from the allowlist, or turn it off.",
                modality=modality,
            )
        choices.append(await resolve_choice(workspace, chosen, catalogue=catalogue))
    return choices


async def resolve_choice(
    workspace: Workspace | None,
    chosen: MediaModelSelection,
    *,
    catalogue: MediaCatalogue,
) -> MediaModelChoice:
    """One selection, judged as the Start dialog judges it (CR-E8, Law 36): on
    the workspace's allowlist for its modality, present in the live catalogue,
    with defaults the snapshotted capability record accepts.

    Shared by run creation and by a media regeneration's `model_override`
    (Stage 04 PRD §9.2 "Regenerate"), so an override is refused for exactly
    the reasons a start would be. Raises `CreativeInputError` otherwise.
    """
    modality = chosen.modality
    entry = next(
        (
            e
            for e in allowlist(workspace, modality)
            if e.get("enabled", True)
            and e.get("model_id") == chosen.model_id
            and (chosen.provider_tag is None or e.get("provider_tag") == chosen.provider_tag)
        ),
        None,
    )
    if entry is None:
        raise CreativeInputError(
            "media_model_not_allowlisted",
            f"The {modality} model {chosen.model_id} is not on this workspace's "
            f"{modality} allowlist. An admin adds it under Settings → Models → Media "
            "generation, or choose one that is.",
            modality=modality,
            model_id=chosen.model_id,
        )
    provider_tag = entry.get("provider_tag")
    try:
        record = await catalogue.record_for(modality, chosen.model_id, provider_tag)
    except CatalogueUnavailable as unavailable:
        raise CreativeInputError(
            "media_model_unavailable",
            str(unavailable),
            modality=modality,
            model_id=chosen.model_id,
        ) from unavailable
    if record is None:
        raise CreativeInputError(
            "media_model_unavailable",
            f"The {modality} model {chosen.model_id}"
            + (f" on {provider_tag}" if provider_tag else "")
            + " is not in OpenRouter's live catalogue any more. Choose another "
            "allowlisted model; nothing is swapped automatically.",
            modality=modality,
            model_id=chosen.model_id,
        )
    inherited = _applicable(modality, workspace_defaults(workspace, modality), record)
    defaults = _validated_defaults(modality, {**inherited, **chosen.defaults}, record)
    return MediaModelChoice(
        modality=modality,
        model_id=chosen.model_id,
        provider_tag=provider_tag,
        capability=record.model_dump(mode="json"),
        capability_hash=capability_hash(record),
        defaults=defaults,
    )


def validated_defaults(
    modality: str, defaults: dict[str, Any], record: CapabilityRecord
) -> dict[str, Any]:
    """`_validated_defaults`, for a caller outside run creation: a
    regeneration's `params_override` on the run's own pinned model."""
    return _validated_defaults(modality, defaults, record)


def workspace_defaults(workspace: Workspace | None, modality: str) -> dict[str, Any]:
    raw = ((workspace.settings or {}) if workspace else {}).get(MEDIA_DEFAULTS_SETTING) or {}
    entry = raw.get(modality) if isinstance(raw, dict) else None
    return dict(entry) if isinstance(entry, dict) else {}


def _applicable(
    modality: str, defaults: dict[str, Any], record: CapabilityRecord
) -> dict[str, Any]:
    """The workspace defaults this model takes. A workspace default is written
    for every model at once, so one the chosen model does not support does not
    apply to it; a project's own defaults are validated strictly instead."""
    kept: dict[str, Any] = {}
    for field, value in defaults.items():
        if field not in DEFAULT_FIELDS[modality]:
            continue
        try:
            probe: MediaRequest = (
                ImageRequest(model=record.model_id, prompt="(defaults)", **{field: value})
                if modality == "image"
                else VideoRequest(model=record.model_id, prompt="(defaults)", **{field: value})
            )
        except ValidationError:
            continue
        if not validate(probe, record):
            kept[field] = value
    return kept


def _validated_defaults(
    modality: str, defaults: dict[str, Any], record: CapabilityRecord
) -> dict[str, Any]:
    unknown = sorted(set(defaults) - DEFAULT_FIELDS[modality])
    if unknown:
        raise CreativeInputError(
            "capability_unsupported",
            f"{unknown[0]!r} is not a {modality} default; defaults may set "
            f"{', '.join(sorted(DEFAULT_FIELDS[modality]))}.",
            field=unknown[0],
            supported=[],
            modality=modality,
        )
    probe: MediaRequest
    try:
        if modality == "image":
            probe = ImageRequest(model=record.model_id, prompt="(defaults)", **defaults)
        else:
            probe = VideoRequest(model=record.model_id, prompt="(defaults)", **defaults)
    except ValidationError as exc:
        first = exc.errors()[0]
        field = str(first["loc"][0]) if first.get("loc") else "defaults"
        raise CreativeInputError(
            "capability_unsupported",
            f"{field}: {first['msg']}",
            field=field,
            supported=[],
            modality=modality,
        ) from exc
    errors = validate(probe, record)
    if errors:
        first_error = errors[0]
        raise CreativeInputError(
            "capability_unsupported",
            f"{record.model_id} does not support {first_error.field}={first_error.value!r}"
            + (f"; it accepts {first_error.supported}." if first_error.supported else "."),
            field=first_error.field,
            supported=first_error.supported,
            modality=modality,
            errors=[e.model_dump(mode="json") for e in errors],
        )
    return dict(defaults)


def required_ratios(pin: PublishedPin, campaign_type: str) -> tuple[list[str], list[str]]:
    """`(image ratios, video ratios)` the pinned spec sheet requires for one
    campaign type. A logo is fitted by padding (Law 38), never generated."""
    return ratios_for(_pinned_specs(pin), campaign_type)


def ratios_for(specs: dict[str, Any], campaign_type: str) -> tuple[list[str], list[str]]:
    """`required_ratios` over a spec sheet already in hand — the pinned
    `RuleSet.asset_specs.specs` a creative node holds (4.1.1's media plan)."""
    image: list[str] = []
    video: list[str] = []
    for asset_type, spec in (specs.get(campaign_type) or {}).items():
        ratio = (spec or {}).get("ratio") if isinstance(spec, dict) else None
        if not ratio or "logo" in asset_type:
            continue
        bucket = video if "video" in asset_type else image
        if ratio not in bucket:
            bucket.append(str(ratio))
    return image, video


def _pinned_specs(pin: PublishedPin) -> dict[str, Any]:
    return dict(((pin.ruleset.compiled or {}).get("asset_specs") or {}).get("specs") or {})


def estimate_inputs(
    plan: PlanContract,
    pin: PublishedPin,
    scope: CreativeScope,
    choices: list[MediaModelChoice],
    caps: BudgetCaps,
) -> dict[str, Any]:
    """The arguments `media.cost_estimate_v1` prices a scope from — gathered
    here, computed in `calc/` (law 14)."""
    return estimate_inputs_for(
        plan.account_structure.campaigns, _pinned_specs(pin), scope, choices, caps
    )


def estimate_inputs_for(
    campaigns_in_plan: list[Any],
    specs: dict[str, Any],
    scope: CreativeScope,
    choices: list[MediaModelChoice],
    caps: BudgetCaps,
) -> dict[str, Any]:
    """`estimate_inputs` from the plan's campaigns and a spec sheet in hand."""
    every = [c.campaign_ref or c.name for c in campaigns_in_plan]
    wanted = set(scope.campaign_refs) or set(every)
    campaigns = []
    for campaign in campaigns_in_plan:
        ref = campaign.campaign_ref or campaign.name
        if ref not in wanted:
            continue
        image_ratios, video_ratios = ratios_for(specs, campaign.type)
        campaigns.append(
            {"campaign_ref": ref, "image_ratios": image_ratios, "video_ratios": video_ratios}
        )
    by_modality = {c.modality: c for c in choices}

    def model(modality: str) -> dict[str, Any] | None:
        choice = by_modality.get(modality)  # type: ignore[call-overload]
        if choice is None:
            return None
        return {"capability": choice.capability, "params": dict(choice.defaults)}

    return {
        "campaigns": campaigns,
        "scope": {
            "images": scope.images,
            "video": scope.video,
            "concepts_per_campaign": scope.concepts_per_campaign,
        },
        "image": model("image"),
        "video": model("video"),
        "text_usd": TEXT_ESTIMATE_USD,
        "caps": {
            "max_creative_cost_usd": str(caps.max_creative_cost_usd),
            "max_media_cost_usd": str(caps.max_media_cost_usd),
        },
    }


def ratio_inputs(
    plan: PlanContract, pin: PublishedPin, scope: CreativeScope, choices: list[MediaModelChoice]
) -> dict[str, Any]:
    """The arguments of `media.ratio_plan_v1`: every ratio any campaign in
    scope requires, against each chosen model."""
    return ratio_inputs_for(plan.account_structure.campaigns, _pinned_specs(pin), scope, choices)


def ratio_inputs_for(
    campaigns_in_plan: list[Any],
    specs: dict[str, Any],
    scope: CreativeScope,
    choices: list[MediaModelChoice],
) -> dict[str, Any]:
    inputs = estimate_inputs_for(
        campaigns_in_plan,
        specs,
        scope,
        choices,
        BudgetCaps(max_creative_cost_usd=Decimal(0), max_media_cost_usd=Decimal(0)),
    )
    image: list[str] = []
    video: list[str] = []
    for campaign in inputs["campaigns"]:
        image += [r for r in campaign["image_ratios"] if r not in image]
        video += [r for r in campaign["video_ratios"] if r not in video]
    by_modality = {c.modality: c.capability for c in choices}
    return {
        "image_ratios": image,
        "video_ratios": video,
        "image": by_modality.get("image"),
        "video": by_modality.get("video"),
    }


def check_choices_cover(scope: CreativeScope, media_models: list[MediaModelChoice]) -> None:
    """A `CreativeInput` carries exactly one choice per enabled modality."""
    wanted = enabled_modalities(scope)
    have = [choice.modality for choice in media_models]
    if sorted(have) != sorted(wanted):
        missing = sorted(set(wanted) - set(have))
        if missing:
            raise CreativeInputError(
                "media_model_unselected",
                f"{missing[0].capitalize()} is on for this run but no {missing[0]} model is "
                "selected.",
                modality=missing[0],
            )
        raise CreativeInputError(
            "media_model_out_of_scope",
            "A media model was chosen for a modality this run has off, or twice.",
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
    check_choices_cover(scope, media_models)

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
        media_models=list(media_models),
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
        # `creative_constants.yaml` with this project's overrides merged in —
        # the version the run's cache key and every package cite (PRD §8.3).
        constants_version=creative_constants_for(project).version,
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
