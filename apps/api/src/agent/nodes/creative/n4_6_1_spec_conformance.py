"""4.6.1 `spec_conformance` — every count, size, ratio, byte size, format, duration and codec.

Stage 04 PRD §11 4.6.1: `checks[]{asset_id, constraint, expected, measured,
source ∈ {lint, pillow, ffprobe}, verdict}` over every non-dropped asset,
against the pin's `asset_specs`.

What is measured, and by what (the checks themselves are `creative/conformance`):

* **text** — every line the asset's node linted, counted by the linter's own
  counter against its surface's spec (source `lint`). A multi-line asset is
  one check per line: a sitelink's link text and both description lines, a
  snippet's values, a lead form's headline, description and questions, a
  price item's header and description (which 4.3.2 keeps in its output, not on
  the row), a video script's every spoken or shown line;
* **images and logos** — every rendition file, decoded by Pillow, against the
  spec its ratio was made for (source `pillow`);
* **video** — every encoded file, probed by ffprobe, against its ratio's spec
  and the encoder's promises (source `ffprobe`).

Nothing is skipped silently: an asset with no spec to answer to, or a file that
is missing or will not decode, is an `unchecked[]` entry naming why. No model
call, no spend, no asset written.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field

from agent.creative import conformance, edits, video
from agent.db.models import (
    CreativeAsset,
    CreativeAssetKind,
    CreativeAssetStatus,
    Evidence,
    MediaArtifact,
    MediaArtifactRole,
    RunStage,
)
from agent.export.plan_contract import PlannedCampaign
from agent.llm.router import TaskClass
from agent.nodes.base import NodeSpec, RunContext
from agent.postprod.probe import ProbeError, probe_image, probe_video
from agent.schemas.creative_input import CreativeInput
from agent.schemas.creative_qa import ConformanceCheck, SpecConformance, Unchecked
from agent.schemas.creative_video import VideoScript
from agent.schemas.guardrails import AssetSpec
from agent.storage.backend import StorageBackend, StorageError, get_storage

NODE_ID = "4.6.1"
OFFERS_NODE = "4.3.2"

#: Assets measured as files, and the spec family their ratio is looked up in.
MEDIA_FAMILIES: Mapping[CreativeAssetKind, str] = {
    CreativeAssetKind.IMAGE: "image",
    CreativeAssetKind.LOGO: "logo",
    CreativeAssetKind.VIDEO: "video",
}


class _PriceItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    asset_id: uuid.UUID
    description: str | None = None


class _PriceAsset(BaseModel):
    model_config = ConfigDict(extra="ignore")
    items: list[_PriceItem] = Field(default_factory=list)


class _Offers(BaseModel):
    """Only what 4.6.1 reads of 4.3.2's output: each price item's description."""

    model_config = ConfigDict(extra="ignore")
    prices: list[_PriceAsset] = Field(default_factory=list)


class SpecConformanceNode:
    spec = NodeSpec(
        id=NODE_ID,
        name="spec_conformance",
        stage="4.6",
        run_stage=RunStage.CREATIVE,
        depends_on=("4.2.4", "4.2.5", "4.3.1", "4.3.2", "4.3.3", "4.4.7"),
        task_class=TaskClass.CLASSIFY,
        input_model=CreativeInput,
        output_model=SpecConformance,
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        return []

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        creative = ctx.require_creative()
        ruleset = creative.linter.ruleset
        tolerance = float(creative.constants.media.ratio_tolerance.value)
        fps = int(creative.constants.video.target_fps.value)
        campaigns = _campaigns(creative.input)
        descriptions = price_descriptions(ctx.outputs.get(OFFERS_NODE) or {})
        assets = await _assets(ctx)
        files = await _renditions(ctx, [a.id for a in assets if a.kind in MEDIA_FAMILIES])
        storage = ctx.media.storage if ctx.media is not None else get_storage()

        checks: list[ConformanceCheck] = []
        unchecked: list[Unchecked] = []
        for asset in assets:
            campaign = campaigns.get(asset.campaign_ref)
            specs: dict[str, AssetSpec] = (
                ruleset.asset_specs.for_campaign(campaign.type.strip().lower())
                if campaign is not None
                else {}
            )
            family = MEDIA_FAMILIES.get(asset.kind)
            if family is None:
                spec = conformance.text_spec(specs, asset.surface)
                if spec is None:
                    unchecked.append(
                        _missing(asset, None, f"no spec for surface {asset.surface!r}")
                    )
                    continue
                checks.extend(
                    conformance.text_checks(asset.id, linted_lines(asset, descriptions), spec)
                )
                continue
            for artifact in files.get(asset.id, []):
                found, problem = await _measured(storage, artifact, family)
                if problem is not None:
                    unchecked.append(problem.model_copy(update={"asset_id": asset.id}))
                    continue
                matched = conformance.media_spec(
                    specs, artifact.aspect_ratio, kind=family, tolerance=tolerance
                )
                if matched is None:
                    unchecked.append(
                        _missing(
                            asset,
                            artifact.id,
                            f"no {family} spec with ratio {artifact.aspect_ratio} for this "
                            "campaign type",
                        )
                    )
                if family == "video":
                    checks.extend(
                        conformance.video_checks(
                            asset.id,
                            artifact.id,
                            found,
                            matched[1] if matched is not None else None,
                            declared_media_type=artifact.media_type,
                            tolerance=tolerance,
                            fps=fps,
                        )
                    )
                elif matched is not None:
                    checks.extend(
                        conformance.image_checks(
                            asset.id,
                            artifact.id,
                            found,
                            matched[1],
                            declared_media_type=artifact.media_type,
                            tolerance=tolerance,
                        )
                    )
        return SpecConformance(
            ruleset_version=creative.linter.pin,
            checks=checks,
            unchecked=unchecked,
            failed=sum(1 for check in checks if check.verdict == "fail"),
        )


def _campaigns(creative_input: CreativeInput) -> dict[str, PlannedCampaign]:
    return {
        (campaign.campaign_ref or campaign.name): campaign
        for campaign in creative_input.account_structure.campaigns
    }


def price_descriptions(output: Mapping[str, Any]) -> dict[uuid.UUID, str]:
    offers = _Offers.model_validate(output)
    return {
        item.asset_id: item.description
        for price in offers.prices
        for item in price.items
        if item.description
    }


def linted_lines(asset: CreativeAsset, descriptions: Mapping[uuid.UUID, str]) -> list[str]:
    """Every line the asset's node linted, as it linted it."""
    fields = asset.fields or {}
    text = asset.text or ""
    match asset.kind:
        case CreativeAssetKind.SITELINK:
            lines = [text, fields.get("line1"), fields.get("line2")]
        case CreativeAssetKind.STRUCTURED_SNIPPET:
            lines = list(fields.get("values") or [])
        case CreativeAssetKind.LEAD_FORM:
            questions = fields.get("questions") or []
            lines = [
                text,
                fields.get("description"),
                *(q.get("text") for q in questions if isinstance(q, Mapping)),
            ]
        case CreativeAssetKind.PRICE:
            lines = [text, descriptions.get(asset.id)]
        case CreativeAssetKind.VIDEO_SCRIPT:
            script = fields.get("script")
            lines = (
                [line for _, line in video.script_texts(VideoScript.model_validate(script))]
                if script
                else [text]
            )
        case _:
            lines = [edits.linted_text(asset.surface, text)]
    return [str(line) for line in lines if isinstance(line, str) and line]


async def _assets(ctx: RunContext) -> list[CreativeAsset]:
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


async def _renditions(
    ctx: RunContext, asset_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[MediaArtifact]]:
    if not asset_ids:
        return {}
    rows = (
        (
            await ctx.db.execute(
                sa.select(MediaArtifact)
                .where(
                    MediaArtifact.asset_id.in_(asset_ids),
                    MediaArtifact.role == MediaArtifactRole.RENDITION,
                )
                .order_by(MediaArtifact.created_at, MediaArtifact.id)
            )
        )
        .scalars()
        .all()
    )
    found: dict[uuid.UUID, list[MediaArtifact]] = {}
    for row in rows:
        found.setdefault(row.asset_id, []).append(row)
    return found


async def _measured(
    storage: StorageBackend, artifact: MediaArtifact, family: str
) -> tuple[Any, Unchecked | None]:
    """The file's facts, decoded here rather than trusted from its row."""
    try:
        content = await asyncio.to_thread(storage.get, artifact.storage_path)
    except StorageError as exc:
        return None, Unchecked(
            asset_id=artifact.asset_id,
            media_id=artifact.id,
            reason="file_missing",
            detail=f"{artifact.storage_path}: {exc}",
        )
    try:
        if family == "video":
            return await asyncio.to_thread(probe_video, content), None
        return await asyncio.to_thread(probe_image, content), None
    except ProbeError as exc:
        return None, Unchecked(
            asset_id=artifact.asset_id,
            media_id=artifact.id,
            reason="file_unreadable",
            detail=f"{artifact.storage_path}: {exc}",
        )


def _missing(asset: CreativeAsset, media_id: uuid.UUID | None, detail: str) -> Unchecked:
    return Unchecked(asset_id=asset.id, media_id=media_id, reason="spec_missing", detail=detail)


SPEC_CONFORMANCE = SpecConformanceNode()
