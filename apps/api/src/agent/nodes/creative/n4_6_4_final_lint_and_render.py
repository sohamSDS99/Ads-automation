"""4.6.4 `final_lint_and_render` — everything re-linted at the final pin, and the ad previews.

Stage 04 PRD §11 4.6.4: "Re-lint everything against the final pin; swap or
drop assets tied to rejected exceptions; `previews[]{ad_ref, device,
combination, screenshot, dom{truncated[], overflow_px[]}, spec_diff{missing[],
extra[], mismatched[]}, visual_diff_vs_previous?}` for the three
highest-likelihood combinations plus the longest-string combination of every
RSA."

**The final pin** is the run's last `Run.pins` entry — the Stage 03 MINOR a
cleared H3 claim repinned it to, or the start pin when H3 cleared no claim.
The run's linter is reloaded if it is not already at that pin.

**Rejected exceptions first.** Every exception the legal owner rejected, or an
operator withdrew, drops the assets it ties up and swaps each carried one for
its precomputed fallback (`clearance.swap_to_fallbacks` — the swap the H3
routes make, so a decision whose swap is already on record changes nothing).
The outcome is read back from the rows, whoever made the swap.

**The re-lint** covers every asset not dropped, through the run's pinned
linter (`lint_adapter`, law 33) exactly as its node linted it: a text asset
line by line (4.6.1's `linted_lines` — a sitelink's link text and both lines, a
headline on its keyword-insertion default); an image or a logo file by file,
re-measured by Stage 03's precheck against the final pin's logo templates; a
video by the script it says (its captions are that script). The result is
stored on the asset beside the pin it was reached at. The asset's status is
not changed: this node checks, and a failure is reported in `failed` for 4.7
to act on — which is also why a failing element still appears in its preview.

**The previews** are rendered by `preview/serp.py` from versioned templates,
for the combinations `creative/preview_combinations.py` picks, on both
devices. Each is a `RenderPreview` row. Pixels are **advisory** (D12): overflow
or clipping makes a preview a `warning`; only the `RuleSet` specs
(`conformance.spec_diff`) make it `blocking`. A missing browser makes the
previews `unavailable`, never the run fail. `visual_diff_vs_previous` is not
computed (it is optional in §11; docs/stage-04-questions.md § S4-P15).

No model call, no spend. A retry rewrites the previews.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import sqlalchemy as sa
from pydantic import BaseModel

from agent.connectors.browser import BrowserUnavailable
from agent.creative import clearance, conformance, edits, lint_adapter, masters
from agent.creative.lint_adapter import PinnedLinter
from agent.creative.preview_combinations import Combination, Piece, plan
from agent.db.models import (
    CreativeAsset,
    CreativeAssetKind,
    CreativeAssetStatus,
    CreativeException,
    CreativeExceptionStatus,
    Evidence,
    MediaArtifact,
    MediaArtifactRole,
    PreviewDevice,
    PreviewVerdict,
    RenderPreview,
    RunStage,
)
from agent.export.plan_contract import PlannedCampaign
from agent.guardrails.verdicts import image_verdict
from agent.llm.router import TaskClass
from agent.nodes.base import NodeContractError, NodeSpec, RunContext
from agent.nodes.creative.n4_2_1_headline_spread import SURFACE as HEADLINE_SURFACE
from agent.nodes.creative.n4_2_2_claim_bound_descriptions import PATH_SURFACE
from agent.nodes.creative.n4_2_2_claim_bound_descriptions import SURFACE as DESCRIPTION_SURFACE
from agent.nodes.creative.n4_2_4_variant_b import ad_ref
from agent.nodes.creative.n4_6_1_spec_conformance import (
    OFFERS_NODE,
    linted_lines,
    price_descriptions,
)
from agent.nodes.creative.n4_6_2_editorial_lint_and_exceptions import locale
from agent.preview import landing, serp
from agent.schemas.creative_input import CreativeInput
from agent.schemas.creative_qa import (
    ExceptionOutcome,
    FinalLint,
    FinalLintAndRender,
    PreviewCombination,
    PreviewItem,
    SpecDiff,
)
from agent.schemas.guardrails import SURFACE_ASSET_TYPES, AssetSpec, LintResult, LintTarget
from agent.storage.backend import StorageBackend, StorageError, get_storage

NODE_ID = "4.6.4"
CLEARANCE_NODE = "4.6.3"
REFUSED: Mapping[CreativeExceptionStatus, str] = {
    CreativeExceptionStatus.REJECTED: "rejected",
    CreativeExceptionStatus.WITHDRAWN: "withdrawn",
}
IMAGE_KINDS = frozenset({CreativeAssetKind.IMAGE, CreativeAssetKind.LOGO})
RSA_SURFACES = {HEADLINE_SURFACE: "headline", DESCRIPTION_SURFACE: "description",
                PATH_SURFACE: "path"}  # fmt: skip
#: Google shows two display-URL paths.
MAX_PATHS = 2


@dataclass(frozen=True)
class _Planned:
    """One combination of one RSA, ready to render."""

    ad_ref: str
    combination: Combination
    paths: tuple[uuid.UUID, ...]
    preview: serp.AdPreview
    spec_diff: SpecDiff


class FinalLintAndRenderNode:
    spec = NodeSpec(
        id=NODE_ID,
        name="final_lint_and_render",
        stage="4.6",
        run_stage=RunStage.CREATIVE,
        depends_on=(CLEARANCE_NODE,),
        task_class=TaskClass.CLASSIFY,
        input_model=CreativeInput,
        output_model=FinalLintAndRender,
        lint_required=True,
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        return []

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        creative = ctx.require_creative()
        linter = await _final_linter(ctx, creative.linter)
        outcomes = await _refused(ctx)
        assets = await _live_assets(ctx)
        storage = ctx.media.storage if ctx.media is not None else get_storage()
        campaigns = {
            (campaign.campaign_ref or campaign.name): campaign
            for campaign in creative.input.account_structure.campaigns
        }
        relinter = _Relinter(
            linter=linter,
            campaigns=campaigns,
            descriptions=price_descriptions(ctx.outputs.get(OFFERS_NODE) or {}),
            files=await _files(ctx, [a.id for a in assets if a.kind in IMAGE_KINDS]),
            by_id={asset.id: asset for asset in assets},
            storage=storage,
            now=datetime.now(UTC),
        )
        relinted = [await relinter.relint(asset) for asset in assets]
        await ctx.db.flush()

        version = creative.constants.preview.serp_template_version.value
        viewports = {
            "mobile": landing.parse_viewport(creative.constants.landing.viewport_mobile.value),
            "desktop": landing.parse_viewport(creative.constants.landing.viewport_desktop.value),
        }
        planned = _plan(assets, campaigns, linter)
        previews = await _render(ctx, storage, planned, viewports=viewports, version=version)
        return FinalLintAndRender(
            ruleset_version=linter.pin,
            relinted=relinted,
            failed=sum(1 for item in relinted if item.verdict in ("fail", "indeterminate")),
            exception_outcomes=outcomes,
            template_version=version,
            previews=previews,
        )


# ---------------------------------------------------------------------------
# the final pin, and what a "no" leaves behind
# ---------------------------------------------------------------------------


async def _final_linter(ctx: RunContext, linter: PinnedLinter) -> PinnedLinter:
    await ctx.db.refresh(ctx.run, attribute_names=["pins"])
    final = lint_adapter.current_pin(ctx.run)
    if linter.pin == final:
        return linter
    return await lint_adapter.load(
        ctx.db, workspace_id=ctx.run.workspace_id, pin=final, offer_records=linter.offer_records
    )


async def _refused(ctx: RunContext) -> list[ExceptionOutcome]:
    """Swap every rejected or withdrawn exception's assets, then read back what it left."""
    rows = list(
        (
            await ctx.db.execute(
                sa.select(CreativeException)
                .where(
                    CreativeException.creative_run_id == ctx.run.id,
                    CreativeException.status.in_(list(REFUSED)),
                )
                .order_by(CreativeException.created_at, CreativeException.id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return []
    for row in rows:
        by_user = row.decided_by or ctx.run.triggered_by
        if by_user is None:  # pragma: no cover — both routes stamp the decider
            raise NodeContractError(
                f"exception {row.id} is {row.status.value} but names no decider"
            )
        try:
            await clearance.swap_to_fallbacks(ctx.db, ctx.run.id, [row], by_user=by_user)
        except clearance.ClearanceError as exc:
            raise NodeContractError(exc.detail) from exc
    assets = {
        asset.id: asset
        for asset in (
            await ctx.db.execute(
                sa.select(CreativeAsset)
                .where(CreativeAsset.creative_run_id == ctx.run.id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    }
    outcomes: list[ExceptionOutcome] = []
    for row in rows:
        tied = list(row.asset_ids or ())
        parents = {str(asset_id) for asset_id in tied}
        outcomes.append(
            ExceptionOutcome(
                exception_id=row.id,
                kind=row.kind.value,
                decision=REFUSED[row.status],
                dropped=[
                    a
                    for a in tied
                    if a in assets and assets[a].status is CreativeAssetStatus.DROPPED
                ],  # fmt: skip
                swapped_in=[
                    asset.id
                    for asset in sorted(assets.values(), key=lambda a: (a.created_at, a.id))
                    if asset.status is not CreativeAssetStatus.DROPPED
                    and (asset.lineage or {}).get("origin") == "reserve_swap"
                    and (asset.lineage or {}).get("parent_id") in parents
                ],
            )
        )
    return outcomes


async def _live_assets(ctx: RunContext) -> list[CreativeAsset]:
    return list(
        (
            await ctx.db.execute(
                sa.select(CreativeAsset)
                .where(
                    CreativeAsset.creative_run_id == ctx.run.id,
                    CreativeAsset.status != CreativeAssetStatus.DROPPED,
                )
                .order_by(CreativeAsset.node_id, CreativeAsset.created_at, CreativeAsset.id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )


async def _files(
    ctx: RunContext, asset_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[MediaArtifact]]:
    """Each image's rendition files — or, with none, its master."""
    if not asset_ids:
        return {}
    rows = (
        (
            await ctx.db.execute(
                sa.select(MediaArtifact)
                .where(
                    MediaArtifact.asset_id.in_(asset_ids),
                    MediaArtifact.role.in_([MediaArtifactRole.RENDITION, MediaArtifactRole.MASTER]),
                )
                .order_by(MediaArtifact.created_at, MediaArtifact.id)
            )
        )
        .scalars()
        .all()
    )
    found: dict[uuid.UUID, list[MediaArtifact]] = {}
    for role in (MediaArtifactRole.RENDITION, MediaArtifactRole.MASTER):
        for row in rows:
            if row.role is role and (
                row.asset_id not in found or found[row.asset_id][0].role is role
            ):
                found.setdefault(row.asset_id, []).append(row)
    return found


# ---------------------------------------------------------------------------
# the re-lint
# ---------------------------------------------------------------------------


@dataclass
class _Relinter:
    linter: PinnedLinter
    campaigns: Mapping[str, PlannedCampaign]
    descriptions: Mapping[uuid.UUID, str]
    files: Mapping[uuid.UUID, list[MediaArtifact]]
    by_id: Mapping[uuid.UUID, CreativeAsset]
    storage: StorageBackend
    now: datetime

    async def relint(self, asset: CreativeAsset) -> FinalLint:
        campaign = self.campaigns.get(asset.campaign_ref)
        campaign_type = (campaign.type if campaign is not None else "").strip().lower()
        if not campaign_type:
            return self._unlinted(asset, "no_campaign")
        market, language = locale(self.campaigns, asset.campaign_ref, asset.ad_group_ref)

        def target(ref: str, surface: str, **values: Any) -> LintTarget:
            return LintTarget(
                ref=ref,
                surface=surface,
                campaign_type=campaign_type,
                market=market,
                language=language,
                generated_by_ai=asset.generated_by_ai,
                **values,
            )

        if asset.kind in IMAGE_KINDS:
            targets, problem = await self._image_targets(asset, target)
            if problem is not None:
                return self._unlinted(asset, problem)
            result = self.linter.lint_candidates(targets, now=self.now)
            return self._record(asset, result, len(targets), image_verdict(result)[0])
        source = asset
        if asset.kind is CreativeAssetKind.VIDEO:
            script_id = (asset.fields or {}).get("script_asset_id")
            script = self.by_id.get(uuid.UUID(script_id)) if isinstance(script_id, str) else None
            if script is None:
                return self._unlinted(asset, "no_script")
            source = script
        lines = linted_lines(source, self.descriptions)
        if not lines:
            return self._unlinted(asset, "no_text")
        targets = [
            target(f"{asset.id}:{index}", source.surface, text=line)
            for index, line in enumerate(lines)
        ]
        result = self.linter.lint_candidates(targets, now=self.now)
        return self._record(asset, result, len(targets), result.verdict)

    async def _image_targets(
        self, asset: CreativeAsset, target: Any
    ) -> tuple[list[LintTarget], str | None]:
        files = self.files.get(asset.id, [])
        if not files:
            return [], "file_missing"
        templates = masters.logo_templates(self.linter.ruleset)
        targets: list[LintTarget] = []
        for artifact in files:
            try:
                content = await asyncio.to_thread(self.storage.get, artifact.storage_path)
            except StorageError:
                return [], "file_missing"
            try:
                measurement = await asyncio.to_thread(masters.measure_candidate, content, templates)
            except ValueError:
                return [], "file_unreadable"
            targets.append(
                target(
                    str(artifact.id),
                    asset.surface,
                    image_ref=measurement.image_hash,
                    image_metrics=measurement.metrics(),
                )
            )
        return targets, None

    def _record(
        self, asset: CreativeAsset, result: LintResult, count: int, verdict: str
    ) -> FinalLint:
        if asset.frozen_at is None:
            asset.lint = result.model_dump(mode="json")
            asset.ruleset_version = result.ruleset_version
        return FinalLint(
            asset_id=asset.id,
            kind=asset.kind.value,
            surface=asset.surface,
            status=asset.status.value,
            verdict=verdict,
            targets=count,
            lint=result,
        )

    @staticmethod
    def _unlinted(asset: CreativeAsset, reason: str) -> FinalLint:
        return FinalLint(
            asset_id=asset.id,
            kind=asset.kind.value,
            surface=asset.surface,
            status=asset.status.value,
            verdict="unlinted",
            targets=0,
            reason=reason,
        )


# ---------------------------------------------------------------------------
# the previews
# ---------------------------------------------------------------------------


def _plan(
    assets: Sequence[CreativeAsset],
    campaigns: Mapping[str, PlannedCampaign],
    linter: PinnedLinter,
) -> list[_Planned]:
    """Every RSA's combinations, as elements with their budgets and spec diff."""
    groups: dict[tuple[str, str | None, str], dict[str, list[CreativeAsset]]] = {}
    for asset in assets:
        role = RSA_SURFACES.get(asset.surface)
        if role is None or asset.status not in clearance.CARRIED:
            continue
        variant = asset.variant.value if asset.variant is not None else "A"
        key = (asset.campaign_ref, asset.ad_group_ref, variant)
        groups.setdefault(key, {"headline": [], "description": [], "path": []})[role].append(asset)
    planned: list[_Planned] = []
    for (campaign_ref, ad_group_ref, variant), pool in groups.items():
        if not pool["headline"]:
            continue
        campaign = campaigns.get(campaign_ref)
        specs = (
            linter.ruleset.asset_specs.for_campaign(campaign.type.strip().lower())
            if campaign is not None
            else {}
        )
        _, language = locale(campaigns, campaign_ref, ad_group_ref)
        reference = ad_ref(campaign_ref, ad_group_ref or "", variant)  # type: ignore[arg-type]
        by_ref = {str(a.id): a for role in pool.values() for a in role}
        counts = Counter(SURFACE_ASSET_TYPES[a.surface] for a in by_ref.values())
        paths = tuple(a.id for a in pool["path"][:MAX_PATHS])
        combinations = plan(
            [Piece(str(a.id), edits.linted_text(a.surface, a.text or ""), a.pin_position)
             for a in pool["headline"]],
            [Piece(str(a.id), a.text or "", a.pin_position) for a in pool["description"]],
        )  # fmt: skip
        for index, combination in enumerate(combinations):
            chosen = [
                *(("headline", i, ref) for i, ref in enumerate(combination.headlines, 1) if ref),
                *(("description", i, ref) for i, ref in enumerate(combination.descriptions, 1)
                  if ref),
                *(("path", i, str(a)) for i, a in enumerate(paths, 1)),
            ]  # fmt: skip
            elements: list[serp.PreviewElement] = []
            checked: list[conformance.Element] = []
            for role, position, ref in chosen:
                asset = by_ref[ref]
                text = edits.linted_text(asset.surface, asset.text or "")
                spec: AssetSpec | None = conformance.text_spec(specs, asset.surface)
                element = f"{role}_{position}"
                elements.append(
                    serp.PreviewElement(
                        key=element,
                        asset_id=asset.id,
                        role=role,
                        text=text,
                        max_chars=spec.max_chars if spec is not None else None,
                    )
                )
                checked.append(conformance.Element(element, asset.id, asset.surface, text))
            planned.append(
                _Planned(
                    ad_ref=reference,
                    combination=combination,
                    paths=paths,
                    preview=serp.AdPreview(
                        ref=f"{reference}#{index}",
                        display_url=_display_url(campaign, ad_group_ref),
                        elements=elements,
                        language=language,
                    ),
                    spec_diff=conformance.spec_diff(checked, counts=counts, specs=specs),
                )
            )
    return planned


def _display_url(campaign: PlannedCampaign | None, ad_group_ref: str | None) -> str:
    if campaign is None:
        return ""
    group = next((g for g in campaign.ad_groups if g.name == ad_group_ref), None)
    url = group.landing_url if group is not None else ""
    return (urlsplit(url).hostname or "") if url else ""


async def _render(
    ctx: RunContext,
    storage: StorageBackend,
    planned: Sequence[_Planned],
    *,
    viewports: Mapping[Any, landing.Viewport],
    version: str,
) -> list[PreviewItem]:
    await ctx.db.execute(
        sa.delete(RenderPreview).where(RenderPreview.creative_run_id == ctx.run.id)
    )
    if not planned:
        return []
    ads = [item.preview for item in planned]
    try:
        renders = await serp.render_previews(ads, viewports=viewports, version=version)
    except BrowserUnavailable as exc:
        renders = [
            serp.AdRender(
                ref=ad.ref,
                mobile=_unavailable("mobile", version, str(exc)),
                desktop=_unavailable("desktop", version, str(exc)),
            )
            for ad in ads
        ]
    await ctx.progress(f"rendered {len(planned)} ad previews on 2 devices")
    items: list[PreviewItem] = []
    for item, render in zip(planned, renders, strict=True):
        combination = PreviewCombination(
            roles=list(item.combination.roles),
            headlines=[uuid.UUID(ref) if ref else None for ref in item.combination.headlines],
            descriptions=[uuid.UUID(ref) if ref else None for ref in item.combination.descriptions],
            paths=list(item.paths),
            likelihood=str(item.combination.likelihood),
        )
        for shot in (render.mobile, render.desktop):
            key = None
            if shot.rendered and shot.screenshot is not None:
                key = _key(ctx.run.id, item.ad_ref, shot.device, item.combination.roles[0])
                await asyncio.to_thread(storage.put, key, shot.screenshot, content_type="image/png")
            verdict = _verdict(item.spec_diff, shot)
            row = RenderPreview(
                id=uuid.uuid4(),
                creative_run_id=ctx.run.id,
                ad_ref=item.ad_ref,
                device=PreviewDevice(shot.device),
                combination=combination.model_dump(mode="json"),
                storage_path=key,
                dom_metrics=shot.dom(),
                spec_diff=item.spec_diff.model_dump(mode="json"),
                visual_diff=None,
                template_version=shot.template_version,
                verdict=PreviewVerdict(verdict),
            )
            ctx.db.add(row)
            items.append(
                PreviewItem(
                    preview_id=row.id,
                    ad_ref=item.ad_ref,
                    device=shot.device,
                    combination=combination,
                    screenshot=key,
                    dom=shot.dom(),
                    spec_diff=item.spec_diff,
                    template_version=shot.template_version,
                    verdict=verdict,
                )
            )
    await ctx.db.flush()
    return items


def _verdict(diff: SpecDiff, shot: serp.PreviewRender) -> str:
    """D12: the spec decides `blocking`; pixels can make a preview a warning, no more."""
    if diff.blocking:
        return "blocking"
    if not shot.rendered:
        return "unavailable"
    if shot.dom()["truncated"] or diff.unchecked:
        return "warning"
    return "pass"


def _unavailable(device: Any, version: str, error: str) -> serp.PreviewRender:
    return serp.PreviewRender(device=device, template_version=version, rendered=False, error=error)


def _key(run_id: uuid.UUID, reference: str, device: str, role: str) -> str:
    """§7.4 `creative/{run_id}/renders/{ad_ref}_{device}.png`, one per combination."""
    slug = re.sub(r"[^a-z0-9]+", "-", reference.lower()).strip("-")[:60]
    digest = hashlib.sha256(reference.encode()).hexdigest()[:8]
    return f"creative/{run_id}/renders/{slug}-{digest}_{device}_{role}.png"


FINAL_LINT_AND_RENDER = FinalLintAndRenderNode()
