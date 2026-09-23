"""Stage 3.4 — technical requirements. Nodes 3.4.1, 3.4.2 and 3.4.3.

**None of these three calls a model, and that is the design rather than an
optimisation.** Every field they emit is a projection of `content_constants.
asset_specs` and `image_policy` across the campaign types in scope. Law 25
already forbids putting Google's character limits and image thresholds in a
prompt; a node that asked a model for them would either be handed them back
(pointless, and billed) or be told something plausible that Google never said
(worse than pointless, and unattributable). §11 gives each node a `task_class`
and each declares one, because the registry requires it — but the completion is
never made, and `test_stage_3_4_makes_no_llm_call` fails if one ever is.

The scoping rule underneath all three is §11's one sentence: **unbound ⇒ every
campaign type; bound ⇒ the slate's types only.** A missing plan binding widens
what the rulebook covers and is recorded as `scope='unscoped'`. It never
narrows anything silently, and it is never an error (law 21).
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

import sqlalchemy as sa
import structlog
from pydantic import BaseModel, Field

from agent.db.models import Evidence, RunStage
from agent.guardrails.matchers.image import (
    logo_area,
    logo_match,
    logo_present,
    text_coverage,
)
from agent.guidelines.constants import ContentConstants, get_content_constants
from agent.imaging import LogoTemplateData, template_from_bytes
from agent.llm.router import TaskClass
from agent.nodes.base import NodeContractError, NodeSpec, RunContext
from agent.policy.sources import get_policy_sources
from agent.schemas.guardrails import Authority, LogoTemplate, Rule, RuleScope
from agent.storage import get_storage

log = structlog.get_logger(__name__)

Scope = Literal["scoped", "unscoped"]

#: The policy family the image rules answer to. Google's editorial policy is
#: what disapproves an image for what is printed on it; the exact page is
#: configuration (§9.7), read from `policy_sources.yaml` rather than written
#: here, because law 25 forbids a policy URL in a function body.
IMAGE_POLICY_AREA = "editorial"

#: Asset types whose spec describes a picture rather than a string. Only these
#: get image rules — a headline has no text-coverage ratio.
IMAGE_ASSET_HINTS = ("image", "logo", "video", "thumbnail")


# ---------------------------------------------------------------------------
# shared scoping
# ---------------------------------------------------------------------------


def _guideline(ctx: RunContext) -> Any:
    """The `GuidelineInput` for this run, however the executor carried it."""
    return getattr(ctx, "guideline", None) or ctx.scratch.get("guideline_input")


def _slate_campaign_types(ctx: RunContext) -> tuple[str, ...] | None:
    """The campaign types a bound plan actually plans to run, or `None`.

    `None` and `()` mean genuinely different things here and the difference is
    load-bearing: `None` is "no plan was bound, so cover everything", and `()`
    would be "a plan was bound and it plans no campaigns". `GuidelineInput`
    keeps every optional binding as `None` rather than an empty collection for
    exactly this reason, so the distinction survives the trip.
    """
    source = _guideline(ctx)
    slate = getattr(source, "channel_slate", None) if source is not None else None
    if slate is None:
        return None
    types = [entry.campaign_type for entry in slate.slate if entry.campaign_type]
    # Sorted and de-duplicated: a slate may name `search` in three markets, and
    # the spec sheet has one `search` section however many rows produced it.
    return tuple(sorted(set(types)))


def _markets(ctx: RunContext) -> tuple[str, ...]:
    source = _guideline(ctx)
    markets = getattr(source, "markets", None) if source is not None else None
    if not markets:
        return ()
    return tuple(sorted({getattr(m, "code", None) or str(m) for m in markets}))


# ---------------------------------------------------------------------------
# 3.4.1 asset_spec_sheet
# ---------------------------------------------------------------------------


class AssetSpecRow(BaseModel):
    """One asset type's shape, with the provenance of every number (§11).

    `constants_key`, `source` and `reviewed_at` travel together all the way to
    a finding. That is what stops an `unverified` character limit from being
    enforced as though Google had published it — the writer who is blocked can
    see that the number is a placeholder and that nobody has checked it yet
    (§9.6, open question Q7).
    """

    campaign_type: str = Field(min_length=1)
    asset_type: str = Field(min_length=1)
    min_count: int | None = None
    max_count: int | None = None
    max_chars: int | None = None
    dimensions: list[str] = Field(default_factory=list)
    aspect_ratios: list[str] = Field(default_factory=list)
    max_bytes: int | None = None
    file_types: list[str] = Field(default_factory=list)
    notes: str = ""
    constants_key: str = Field(min_length=1)
    source: str = Field(min_length=1)
    reviewed_at: str = Field(min_length=1)

    @property
    def is_image(self) -> bool:
        """Whether this row describes a picture, by its own shape not its name.

        A row carrying an aspect ratio or a byte ceiling is an image spec
        whatever it is called; a row carrying a character limit is not. Reading
        the shape rather than matching on the name means a future
        `image_portrait` needs no change here, and a `long_headline` can never
        acquire a text-coverage rule by being spelled unluckily.
        """
        if self.max_chars is not None:
            return False
        if self.aspect_ratios or self.dimensions or self.max_bytes is not None:
            return True
        return any(hint in self.asset_type for hint in IMAGE_ASSET_HINTS)


class AssetSpecSheetOutput(BaseModel):
    """3.4.1 — what Google will accept, for everything in scope."""

    specs: list[AssetSpecRow] = Field(default_factory=list)
    scope: Scope = "unscoped"
    #: The campaign types covered. On an unbound run this is every type the
    #: constants know; on a plan-bound run it is the slate's.
    campaign_types: list[str] = Field(default_factory=list)
    #: Campaign types the slate named that the constants have no specs for.
    #: Named rather than dropped: a plan that intends to run `demand_gen` and a
    #: spec sheet that silently omits it is how a campaign reaches launch with
    #: nobody having stated what its assets must look like.
    unspecified_campaign_types: list[str] = Field(default_factory=list)
    constants_version: str = Field(min_length=1)
    markets: list[str] = Field(default_factory=list)


def _rows(constants: ContentConstants, campaign_types: tuple[str, ...]) -> list[AssetSpecRow]:
    rows: list[AssetSpecRow] = []
    for campaign_type in campaign_types:
        for asset_type, spec in sorted(constants.asset_specs.get(campaign_type, {}).items()):
            rows.append(
                AssetSpecRow(
                    campaign_type=campaign_type,
                    asset_type=asset_type,
                    min_count=spec.min_count,
                    max_count=spec.max_count,
                    max_chars=spec.max_chars,
                    dimensions=[spec.min_px] if spec.min_px else [],
                    aspect_ratios=[spec.ratio] if spec.ratio else [],
                    max_bytes=spec.max_bytes,
                    # Left empty rather than guessed. `content_constants.yaml`
                    # carries no `file_types` today, and inventing "PNG, JPG"
                    # here would put an unattributable constraint in a rulebook
                    # — the exact thing law 25 exists to prevent.
                    file_types=[],
                    constants_key=f"asset_specs.{campaign_type}.{asset_type}",
                    source=spec.source,
                    reviewed_at=spec.reviewed_at.isoformat(),
                )
            )
    return rows


class AssetSpecSheetNode:
    """3.4.1 — the spec sheet, projected from constants and never from a model."""

    spec = NodeSpec(
        id="3.4.1",
        name="asset_spec_sheet",
        stage="3.4",
        run_stage=RunStage.GUIDELINE,
        # §11's DAG edge. 3.4.1 reads no field of 3.3.1's output — its inputs
        # are the constants, the slate and the markets — but it is ordered
        # after the policy surface so that a spec sheet is never produced for a
        # campaign type the policy map has ruled out for this advertiser.
        depends_on=("3.3.1",),
        task_class=TaskClass.CLASSIFY,
        input_model=BaseModel,
        output_model=AssetSpecSheetOutput,
        connectors=(),
        optional_inputs=("channel_slate", "markets"),
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        """Nothing. The constants are configuration, not evidence."""
        return []

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        constants = get_content_constants()
        known = tuple(sorted(constants.asset_specs))
        slate = _slate_campaign_types(ctx)

        covered: tuple[str, ...]
        missing: tuple[str, ...]
        scope: Scope
        if slate is None:
            covered, scope, missing = known, "unscoped", ()
        else:
            covered = tuple(t for t in slate if t in constants.asset_specs)
            missing = tuple(t for t in slate if t not in constants.asset_specs)
            scope = "scoped"

        rows = _rows(constants, covered)
        if not rows:
            # Every path that leads here is a real problem worth stopping for:
            # a constants file with no specs at all, or a bound slate whose
            # every campaign type is unknown. Emitting an empty sheet would let
            # 3.4.2 declare a launch minimum of nothing.
            raise NodeContractError(
                "3.4.1 produced no asset specs. "
                + (
                    f"The bound plan's campaign types are {list(slate)} and "
                    f"`content_constants.asset_specs` covers {list(known)}."
                    if slate is not None
                    else "`content_constants.asset_specs` is empty."
                )
            )
        return AssetSpecSheetOutput(
            specs=rows,
            scope=scope,
            campaign_types=list(covered),
            unspecified_campaign_types=list(missing),
            constants_version=constants.version,
            markets=list(_markets(ctx)),
        )


asset_spec_sheet = AssetSpecSheetNode()


# ---------------------------------------------------------------------------
# 3.4.2 launch_minimum_set
# ---------------------------------------------------------------------------


class RequiredAsset(BaseModel):
    asset_type: str = Field(min_length=1)
    count: int = Field(ge=1)
    constants_key: str = Field(min_length=1)
    source: str = Field(min_length=1)


class CampaignMinimum(BaseModel):
    campaign_type: str = Field(min_length=1)
    required_assets: list[RequiredAsset] = Field(default_factory=list)
    optional_but_recommended: list[str] = Field(default_factory=list)
    blocking_for_launch: bool = True


class LaunchMinimumOutput(BaseModel):
    """3.4.2 — the shortest honest answer to "what must exist before this runs"."""

    minimums: list[CampaignMinimum] = Field(default_factory=list)
    readiness_checklist: list[str] = Field(default_factory=list)
    scope: Scope = "unscoped"
    #: Campaign types whose specs name no minimum count at all. They are listed
    #: with `blocking_for_launch=False` and named here, because "we do not know
    #: what this campaign needs" must not render as "this campaign needs
    #: nothing" on a readiness checklist somebody signs off against.
    no_stated_minimum: list[str] = Field(default_factory=list)


class LaunchMinimumNode:
    """3.4.2 — derived from 3.4.1, by arithmetic rather than by judgement."""

    spec = NodeSpec(
        id="3.4.2",
        name="launch_minimum_set",
        stage="3.4",
        run_stage=RunStage.GUIDELINE,
        depends_on=("3.4.1",),
        task_class=TaskClass.CLASSIFY,
        input_model=BaseModel,
        output_model=LaunchMinimumOutput,
        connectors=(),
        optional_inputs=("channel_slate", "account_structure"),
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        return []

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        sheet = ctx.output_of("3.4.1")
        rows = [AssetSpecRow.model_validate(row) for row in sheet.get("specs", [])]
        if not rows:
            raise NodeContractError("3.4.2 was handed an empty spec sheet by 3.4.1")

        by_campaign: dict[str, list[AssetSpecRow]] = {}
        for row in rows:
            by_campaign.setdefault(row.campaign_type, []).append(row)

        minimums: list[CampaignMinimum] = []
        silent: list[str] = []
        for campaign_type in sorted(by_campaign):
            required: list[RequiredAsset] = []
            recommended: list[str] = []
            for row in by_campaign[campaign_type]:
                if row.min_count and row.min_count > 0:
                    required.append(
                        RequiredAsset(
                            asset_type=row.asset_type,
                            count=row.min_count,
                            constants_key=row.constants_key,
                            source=row.source,
                        )
                    )
                else:
                    # A spec with no minimum is not a spec for nothing: the
                    # asset type is permitted and un-mandated, which is exactly
                    # "optional but recommended".
                    recommended.append(row.asset_type)
            if not required:
                silent.append(campaign_type)
            minimums.append(
                CampaignMinimum(
                    campaign_type=campaign_type,
                    required_assets=required,
                    optional_but_recommended=sorted(recommended),
                    blocking_for_launch=bool(required),
                )
            )

        return LaunchMinimumOutput(
            minimums=minimums,
            readiness_checklist=_checklist(minimums, silent),
            scope=sheet.get("scope", "unscoped"),
            no_stated_minimum=silent,
        )


def _checklist(minimums: list[CampaignMinimum], silent: list[str]) -> list[str]:
    """One line per thing a person has to be able to tick before launch."""
    lines: list[str] = []
    for entry in minimums:
        for asset in entry.required_assets:
            lines.append(
                f"{entry.campaign_type}: at least {asset.count} "
                f"{asset.asset_type.replace('_', ' ')}"
                + ("" if asset.source != "unverified" else " (limit not yet verified)")
            )
    for campaign_type in silent:
        lines.append(
            f"{campaign_type}: no minimum asset count is on record — confirm with the "
            f"platform before launch rather than treating this as 'nothing required'"
        )
    return lines


launch_minimum_set = LaunchMinimumNode()


# ---------------------------------------------------------------------------
# 3.4.3 image_precheck_rules
# ---------------------------------------------------------------------------


class SkippedTemplate(BaseModel):
    """A registered logo that could not become a template, and why."""

    asset_id: uuid.UUID
    reason: str = Field(min_length=1)


class ImagePrecheckOutput(BaseModel):
    """3.4.3 — the rules the linter applies, and the templates it matches with."""

    rules: list[Rule] = Field(default_factory=list)
    logo_templates: list[LogoTemplate] = Field(default_factory=list)
    skipped_templates: list[SkippedTemplate] = Field(default_factory=list)
    scope: Scope = "unscoped"
    #: Named so the rulebook can say on its face that the logo half of the
    #: image check is not running, rather than looking complete.
    logo_matching_available: bool = False


class ImagePrecheckNode:
    """3.4.3 — compiles the image rules, before anything is uploaded."""

    spec = NodeSpec(
        id="3.4.3",
        name="image_precheck_rules",
        stage="3.4",
        run_stage=RunStage.GUIDELINE,
        depends_on=("3.4.1", "3.1.3"),
        task_class=TaskClass.CLASSIFY,
        input_model=BaseModel,
        output_model=ImagePrecheckOutput,
        connectors=(),
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        """The brand-book asset rows 3.1.3 registered as logos.

        Loaded by id from 3.1.3's output rather than re-pulled from the
        connector: the logos this rulebook matches against must be the same
        ones the brand owner approved at G5, not whatever a second parse of the
        document happens to find.
        """
        visual = ctx.output_of("3.1.3")
        raw = (visual.get("logo") or {}).get("assets") or []
        ids: list[uuid.UUID] = []
        for value in raw:
            try:
                ids.append(uuid.UUID(str(value)))
            except ValueError:
                log.warning("3.4.3.logo_asset_not_an_id", value=str(value))
        if not ids:
            return []
        rows = await ctx.db.execute(sa.select(Evidence).where(Evidence.id.in_(ids)))
        return list(rows.scalars())

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        constants = get_content_constants()
        sheet = ctx.output_of("3.4.1")
        rows = [AssetSpecRow.model_validate(row) for row in sheet.get("specs", [])]
        scope: Scope = sheet.get("scope", "unscoped")

        image_rows = [row for row in rows if row.is_image]
        campaign_types = tuple(sorted({row.campaign_type for row in image_rows}))
        asset_types = tuple(sorted({row.asset_type for row in image_rows}))
        templates, skipped = await _templates(ctx, ev, constants)

        policy = _image_policy_reference()
        rules = _image_rules(
            constants,
            policy_reference=policy,
            campaign_types=campaign_types,
            asset_types=asset_types,
            has_templates=bool(templates),
        )
        return ImagePrecheckOutput(
            rules=rules,
            logo_templates=[
                LogoTemplate(
                    asset_id=t.asset_id,
                    label=t.label,
                    phash=t.phash,
                    min_score=t.min_score,
                    descriptors_b64=t.descriptors_b64,
                    keypoint_count=t.keypoint_count,
                )
                for t in templates
            ],
            skipped_templates=skipped,
            scope=scope,
            logo_matching_available=bool(templates),
        )


def _image_policy_reference() -> str:
    """The editorial policy URL, from `policy_sources.yaml` (§9.7, law 25)."""
    sources = get_policy_sources().by_area(IMAGE_POLICY_AREA)
    if not sources:
        raise NodeContractError(
            f"3.4.3 found no {IMAGE_POLICY_AREA!r} policy source. An image rule with no "
            f"authority to point at is a rule a blocked writer cannot appeal, which "
            f"§9.1 rule 5 calls a bug."
        )
    return sources[0].url


def _image_rules(
    constants: ContentConstants,
    *,
    policy_reference: str,
    campaign_types: tuple[str, ...],
    asset_types: tuple[str, ...],
    has_templates: bool,
) -> list[Rule]:
    """§11's three metrics, each with the constant that set its threshold.

    Every rule's `authority` is `internal` with the `content_constants.yaml`
    key as its reference, and that is deliberate even for the coverage rule.
    Google publishes no text-coverage percentage — the threshold is ours, a
    screening proxy — so claiming `google_policy` would put words in Google's
    mouth on the face of a finding. The Google page is named in the message,
    where it belongs: as the reason the metric is worth screening, not as the
    source of the number.
    """
    image_policy = constants.image_policy
    scope = RuleScope(campaign_types=campaign_types, asset_types=asset_types)

    def authority(key: str) -> Authority:
        constant = getattr(image_policy, key)
        return Authority(
            source="internal",
            reference=f"content_constants.image_policy.{key}",
            reviewed_at=constant.reviewed_at,
        )

    coverage_max = image_policy.search_image_text_coverage_max.value
    rules = [
        text_coverage(
            authority=authority("search_image_text_coverage_max"),
            maximum=coverage_max,
            scope=scope,
            message=(
                f"Too much of this image is text. Our screening threshold is "
                f"{coverage_max:.0%} of the image area. Google publishes no percentage: "
                f"for Search image assets it disallows added text, logos and graphic "
                f"overlays outright, while Performance Max permits overlays — see "
                f"{policy_reference}."
            ),
        )
    ]
    if has_templates:
        # Only emitted when there is something to match against. A logo rule
        # with an empty template list can never be satisfied: every image would
        # come back `indeterminate` forever, and a rulebook full of permanent
        # `indeterminate` teaches people to ignore the whole category.
        rules.append(
            logo_match(
                authority=authority("logo_match_score_min"),
                minimum=image_policy.logo_match_score_min.value,
                scope=scope,
            )
        )
        rules.append(
            logo_area(
                authority=authority("logo_area_ratio_max"),
                maximum=image_policy.logo_area_ratio_max.value,
                minimum=image_policy.logo_area_ratio_min.value,
                scope=scope,
            )
        )
        rules.append(logo_present(authority=authority("logo_match_score_min"), scope=scope))
    return rules


async def _templates(
    ctx: RunContext, ev: list[Evidence], constants: ContentConstants
) -> tuple[tuple[LogoTemplateData, ...], list[SkippedTemplate]]:
    """Turn the registered logo assets into matchable templates.

    **Today this returns nothing on every real project, and that is a finding
    rather than a bug in this node.** A `brand_book_asset` row records the page
    and pixel size of an image found inside an uploaded document, plus the
    `asset_path` of the document it came from — but the upload route stores the
    extracted *text* and never the bytes, so `asset_path` is not populated and
    there is nothing on the Volume to read. The path below is written for the
    day S3-P7 gives the uploader somewhere to put them; until then every asset
    is recorded in `skipped_templates` with the reason, the logo rules are not
    emitted at all, and the rulebook says `logo_matching_available: false` on
    its face instead of quietly checking nothing.
    """
    if not ev:
        return (), []

    store = get_storage()
    minimum = constants.image_policy.logo_match_score_min.value
    width = constants.image_policy.ocr_working_width_px.as_int()
    templates: list[LogoTemplateData] = []
    skipped: list[SkippedTemplate] = []

    for row in sorted(ev, key=lambda item: str(item.id)):
        payload = row.payload if isinstance(row.payload, dict) else {}
        path = payload.get("asset_path")
        label = str(payload.get("name") or f"logo {payload.get('page', '?')}")
        if not path:
            skipped.append(
                SkippedTemplate(
                    asset_id=row.id,
                    reason="the asset has no stored file — the brand book's bytes are not kept",
                )
            )
            continue
        try:
            content = store.get(str(path))
        except Exception as exc:  # noqa: BLE001 - a missing object is a skip, not a failed run
            skipped.append(
                SkippedTemplate(asset_id=row.id, reason=f"the stored file could not be read: {exc}")
            )
            continue
        try:
            templates.append(
                template_from_bytes(
                    content,
                    asset_id=row.id,
                    label=label,
                    min_score=minimum,
                    working_width=width,
                )
            )
        except Exception as exc:  # noqa: BLE001 - an undecodable asset is a skip
            skipped.append(
                SkippedTemplate(asset_id=row.id, reason=f"the file is not a readable image: {exc}")
            )

    templates.sort(key=lambda item: str(item.asset_id))
    return tuple(templates), skipped


image_precheck_rules = ImagePrecheckNode()
