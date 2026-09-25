"""4.4.3 `image_renditions` — every required ratio, from the master (PRD §9.4, §11).

For every concept 4.4.2 mastered, and every image ratio its campaign's pinned
spec sheet requires:

1. **The derivation is the best the model allows** — `relaid` > `native` >
   `crop` (`postprod.choose_derivation`); the master's own ratio is the master
   itself. A `relaid` or `native` ratio is painted by one new job through
   `MediaJobs.submit_or_resume` — submit once, budget first (Laws 37, 43) — and
   a relay carries the master as its input image. A job that does not
   complete falls back to a crop, the next preference; never to a second paid
   job the estimate did not count.
2. **A crop is cut from the painted frame that covers the ratio best** (the
   master or a relay) at the window `media.crop_window_v1` chooses; keeping
   less than `crop_min_saliency_retained` of the saliency makes it a recorded
   gap. A ratio nothing covers is a gap from the start (Law 39).
3. **Post-production is code** (`postprod/image.py`, Law 38): uniform scaling
   with `sx == sy` asserted and persisted; a registered logo only on
   `logo.permitted_surfaces`, never on `search_image`; any visible label a
   pinned disclosure rule requires; JPEG fitted to `max_bytes` no lower than
   quality 80; the XMP DigitalSourceType written and read back.
4. **Linted at creation** against the run's current pin (Law 33). A rendition
   that does not pass is never emitted: its ratio is a gap that says why.

The spec sheet's logo slots are filled with the registered logos fitted by
padding (`logos[]`) — assets of their own, never AI-generated.

Rows are written last, and this node's earlier rows cleared first: the
executor commits a failed attempt's writes.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fractions import Fraction
from typing import Any

import numpy as np
import sqlalchemy as sa
import structlog
from PIL import Image, ImageOps
from pydantic import BaseModel

from agent.calc.media import crop_window_v1, image_job_price, spectral_residual
from agent.creative import masters
from agent.creative.concepts import IMAGE_SURFACES
from agent.creative.previews import store_preview
from agent.db.models import (
    CreativeAsset,
    CreativeAssetKind,
    CreativeAssetStatus,
    Evidence,
    GenerationStatus,
    MediaArtifact,
    MediaArtifactDerivation,
    MediaArtifactRole,
    RunStage,
)
from agent.guardrails.verdicts import image_verdict
from agent.llm.router import TaskClass
from agent.media.capability import CapabilityUnsupported, best_crop_retention, parse_ratio
from agent.media.jobs import MediaJobs
from agent.media.types import CapabilityRecord, ImageRequest, ReferenceImage
from agent.nodes.base import NodeContractError, NodeSpec, RunContext
from agent.orchestrator.creative_run import PASSING
from agent.postprod import image as postprod
from agent.postprod.probe import ProbeError, probe_image
from agent.schemas.creative_input import CreativeInput, MediaModelChoice
from agent.schemas.creative_media import (
    CampaignConcepts,
    CandidateLint,
    Concept,
    ConceptMasters,
    CreativeConcepts,
    FittedLogo,
    ImageMasters,
    ImageRenditions,
    Rendition,
    RenditionDerivation,
    RenditionGap,
    Scale,
)
from agent.schemas.guardrails import AssetSpec, LintResult, LintTarget

log = structlog.get_logger(__name__)

NODE_ID = "4.4.3"
MASTERS_NODE = "4.4.2"
CONCEPTS_NODE = "4.4.1"

#: What a relay asks of the model, beyond the concept's own prompt: the master
#: re-laid out at the new ratio, not a new picture.
RELAY_INSTRUCTION = (
    "Re-lay out the attached image at {ratio}: keep its subject, setting, palette and "
    "light, and extend or recompose the scene rather than distorting it."
)
_DERIVATIONS = {
    "native": MediaArtifactDerivation.NATIVE,
    "relaid": MediaArtifactDerivation.RELAID,
    "crop": MediaArtifactDerivation.CROP,
}


class ImageRenditionsNode:
    spec = NodeSpec(
        id=NODE_ID,
        name="image_renditions",
        stage="4.4",
        run_stage=RunStage.CREATIVE,
        depends_on=(MASTERS_NODE,),
        task_class=TaskClass.CLASSIFY,
        input_model=CreativeInput,
        output_model=ImageRenditions,
        media=("image",),
        lint_required=True,
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        return []

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        creative = ctx.require_creative()
        inp = creative.input
        if not inp.scope.images:
            return ImageRenditions()
        choice = next((c for c in inp.media_models if c.modality == "image"), None)
        if choice is None:
            raise NodeContractError("images are in scope but the input carries no image model")
        mastered = ImageMasters.model_validate(ctx.output_of(MASTERS_NODE))
        # 4.4.1 is 4.4.2's parent, so it has succeeded whenever 4.4.2 has.
        concepts = CreativeConcepts.model_validate(ctx.output_of(CONCEPTS_NODE))
        rendering = await _Rendering.start(ctx, choice)
        await rendering.clear(mastered)
        return await rendering.run(concepts, mastered)


@dataclass(frozen=True)
class _Frame:
    """A painted image a rendition can be cut from."""

    content: bytes
    width: int
    height: int
    media_type: str
    #: The artifact it is, when it is one (the master); a job's raw file is not.
    media_id: uuid.UUID | None
    job_id: uuid.UUID | None
    #: What a rendition of it records as `derived_from`.
    derived_from: uuid.UUID | None


@dataclass(frozen=True)
class _Slot:
    """One ratio a campaign type's spec sheet requires, over every asset type
    that asks for it: the strictest `min_px` and the smallest `max_bytes`."""

    ratio: str
    min_px: str | None
    max_bytes: int | None


@dataclass
class _Made:
    """A rendition, built and linted in memory; written after everything else."""

    concept: Concept
    master: ConceptMasters
    surface: str
    slot: _Slot
    derivation: RenditionDerivation
    frame: _Frame
    content: bytes
    probe: dict[str, Any]
    transform: dict[str, Any]
    disclosure: dict[str, Any]
    scale: float
    retained: float
    logo_note: str | None
    composited: bool
    lint: CandidateLint


@dataclass
class _Fitted:
    campaign_ref: str
    surface: str
    asset_type: str
    ratio: str
    logo: postprod.LogoArt
    content: bytes
    probe: dict[str, Any]
    transform: dict[str, Any]
    scale: float
    max_bytes: int | None
    result: LintResult
    lint: CandidateLint


@dataclass
class _Rendering:
    ctx: RunContext
    choice: MediaModelChoice
    capability: CapabilityRecord
    media: MediaJobs
    logos: list[postprod.LogoArt]
    logo_unreadable: list[str]
    clear_space_ratio: float | None
    min_width_px: int | None
    templates: tuple[Any, ...]
    made: list[_Made] = field(default_factory=list)
    fitted: list[_Fitted] = field(default_factory=list)
    gaps: list[RenditionGap] = field(default_factory=list)

    @classmethod
    async def start(cls, ctx: RunContext, choice: MediaModelChoice) -> _Rendering:
        creative = ctx.require_creative()
        media = ctx.require_media()
        logos, unreadable = await registered_logos(ctx, media)
        rules = creative.input.creative_context.visual_identity.logo or {}
        return cls(
            ctx=ctx,
            choice=choice,
            capability=CapabilityRecord.model_validate(choice.capability),
            media=media,
            logos=logos,
            logo_unreadable=unreadable,
            clear_space_ratio=positive_float(rules.get("clear_space_ratio")),
            min_width_px=positive_int(rules.get("min_width_px")),
            templates=masters.logo_templates(creative.linter.ruleset),
        )

    # -- constants, read once ------------------------------------------------

    @property
    def _constants(self) -> Any:
        return self.ctx.require_creative().constants

    @property
    def _media_constants(self) -> Any:
        return self._constants.media_constants()

    # -- clear, then run -----------------------------------------------------

    async def clear(self, mastered: ImageMasters) -> None:
        """Remove what an earlier attempt of this node wrote: its rendition
        files on the concepts' assets, and its logo assets (their files go with
        them). The jobs stay — submit once — and are resumed, not re-sent."""
        run = self.ctx.run
        asset_ids = [item.asset_id for item in mastered.concepts]
        if asset_ids:
            await self.ctx.db.execute(
                sa.delete(MediaArtifact).where(
                    MediaArtifact.asset_id.in_(asset_ids),
                    MediaArtifact.role == MediaArtifactRole.RENDITION,
                    MediaArtifact.storage_path.like(f"creative/{run.id}/renditions/%"),
                )
            )
        await self.ctx.db.execute(
            sa.delete(CreativeAsset).where(
                CreativeAsset.creative_run_id == run.id, CreativeAsset.node_id == NODE_ID
            )
        )
        await self.ctx.db.flush()

    async def run(self, concepts: CreativeConcepts, mastered: ImageMasters) -> ImageRenditions:
        by_id = {
            concept.id: (campaign, concept)
            for campaign in concepts.campaigns
            for concept in campaign.concepts
        }
        for item in mastered.concepts:
            if item.concept_id not in by_id:
                raise NodeContractError(f"4.4.2 mastered concept {item.concept_id}, 4.4.1 has none")
            campaign, concept = by_id[item.concept_id]
            if not concept.surfaces:
                continue
            await self._concept(campaign, concept, item)
        for campaign in concepts.campaigns:
            await self._logo_slots(campaign)
        renditions = await self._write_renditions()
        logos = await self._write_logos()
        return ImageRenditions(renditions=renditions, logos=logos, gaps=self.gaps)

    # -- one concept ---------------------------------------------------------

    async def _concept(
        self, campaign: CampaignConcepts, concept: Concept, item: ConceptMasters
    ) -> None:
        surface = concept.surfaces[0]
        slots = self._slots(campaign.campaign_type, logos=False)
        if item.master is None:
            why = "no master" + (f": {item.gap.reason} — {item.gap.detail}" if item.gap else "")
            for slot in slots:
                self._gap(campaign.campaign_ref, concept.id, surface, slot.ratio, why)
            return
        master = await self._master_frame(item)
        media = self._media_constants
        plan = {
            slot.ratio: postprod.choose_derivation(
                slot.ratio,
                item.aspect_ratio,
                self.capability,
                tolerance=media.ratio_tolerance,
                min_retained=media.crop_min_saliency_retained,
            )
            for slot in slots
        }
        own = {
            slot.ratio
            for slot in slots
            if item.aspect_ratio is not None
            and postprod.same_ratio(slot.ratio, item.aspect_ratio, tolerance=media.ratio_tolerance)
        }
        # The master's own ratio, then the painted ones (a crop may be cut from
        # them), then the crops, then the gaps.
        rank = {"native": 1, "relaid": 1, "crop": 2, "gap": 3}
        ordered = sorted(slots, key=lambda s: 0 if s.ratio in own else rank[plan[s.ratio]])
        frames = [master]
        market, language = self._market(concept.campaign_ref)
        for slot in ordered:
            derivation = plan[slot.ratio]
            if derivation == "gap":
                self._gap(
                    campaign.campaign_ref, concept.id, surface, slot.ratio,
                    f"no ratio {self.choice.model_id} paints covers {slot.ratio}, so it can be "
                    "neither relaid, painted nor cropped (the ratio plan)",
                )  # fmt: skip
                continue
            reasons: list[str] = []
            attempts: list[tuple[RenditionDerivation, _Frame | None]] = []
            if slot.ratio in own:
                attempts.append(("native", master))
            elif derivation in ("relaid", "native"):
                painted, why = await self._paint(concept, item, slot, derivation, master)
                if painted is not None:
                    frames.append(painted)
                    attempts.append((derivation, painted))
                else:
                    reasons.append(f"{derivation}: {why}")
            attempts.append(("crop", None))
            for kind, frame in attempts:
                source = frame or _best_source(frames, slot.ratio)
                built = await self._build(
                    kind, source, slot, concept, item, surface,
                    campaign.campaign_type, market, language,
                )  # fmt: skip
                if isinstance(built, _Made):
                    self.made.append(built)
                    break
                reasons.append(f"{kind}: {built}")
            else:
                self._gap(
                    campaign.campaign_ref, concept.id, surface, slot.ratio, "; ".join(reasons)
                )

    async def _master_frame(self, item: ConceptMasters) -> _Frame:
        assert item.master is not None
        row = await self.ctx.db.get(MediaArtifact, item.master.media_id)
        if row is None:
            raise NodeContractError(f"master {item.master.media_id} of {item.concept_id} is gone")
        content = await asyncio.to_thread(self.media.storage.get, row.storage_path)
        return _Frame(
            content=content,
            width=row.width,
            height=row.height,
            media_type=row.media_type,
            media_id=row.id,
            job_id=row.job_id,
            derived_from=row.id,
        )

    async def _paint(
        self,
        concept: Concept,
        item: ConceptMasters,
        slot: _Slot,
        derivation: str,
        master: _Frame,
    ) -> tuple[_Frame | None, str]:
        """One job at `slot.ratio`: a relay from the master, or a fresh painting."""
        media = self._media_constants
        label = postprod.supported_label(
            slot.ratio, self.capability, tolerance=media.ratio_tolerance
        )
        if label is None:  # pragma: no cover — the plan said the model paints it
            return None, f"{self.choice.model_id} names no ratio for {slot.ratio}"
        prompt = masters.request_prompt(concept, slot.ratio, strengthened=False)
        references: list[ReferenceImage] | None = None
        if derivation == "relaid":
            prompt = f"{prompt} {RELAY_INSTRUCTION.format(ratio=label)}"
            references = [
                ReferenceImage(
                    sha256=hashlib.sha256(master.content).hexdigest(),
                    media_type=master.media_type,
                    data=master.content,
                )
            ]
        defaults = {
            k: v for k, v in self.choice.defaults.items() if k in masters.REQUEST_DEFAULT_FIELDS
        }
        request = ImageRequest(
            **{**defaults, "aspect_ratio": label},
            model=self.choice.model_id,
            prompt=prompt,
            input_references=references,
        )
        params = request.model_dump(
            mode="json", exclude_none=True, exclude={"model", "prompt", "input_references"}
        )
        try:
            job = await self.media.submit_or_resume(
                run_id=self.ctx.run.id,
                node_id=NODE_ID,
                asset_id=item.asset_id,
                round=1,
                request=request,
                choice=self.choice,
                estimate_usd=image_job_price(self.capability, params, media).usd,
                ledger=self.ctx.ledger,
            )
        except CapabilityUnsupported as exc:
            return None, f"the model refused the request before spend: {exc}"
        if job.status is GenerationStatus.BLOCKED_BY_BUDGET:
            return None, "the media budget refused the job before it was sent"
        if job.status is not GenerationStatus.COMPLETED:
            return None, f"job {job.id} ended {job.status.value}"
        content = await self._job_file(job.creative_run_id, item.asset_id, job.id)
        if content is None:
            return None, f"job {job.id} returned no image that could be decoded"
        facts = probe_image(content)
        return (
            _Frame(
                content=content,
                width=facts.width,
                height=facts.height,
                media_type=facts.media_type,
                media_id=None,
                job_id=job.id,
                derived_from=master.media_id if derivation == "relaid" else None,
            ),
            "",
        )

    async def _job_file(
        self, run_id: uuid.UUID, asset_id: uuid.UUID, job_id: uuid.UUID
    ) -> bytes | None:
        """The job's first decodable file, `creative/{run}/media/{asset}/{job}-{i}.{ext}`."""
        storage = self.media.storage
        # No trailing slash: the local backend resolves a prefix as a path.
        folder = f"creative/{run_id}/media/{asset_id}"
        stem = f"{job_id}-"
        keys = sorted(
            (
                info.key
                for info in await asyncio.to_thread(lambda: list(storage.iter_objects(folder)))
                if info.key.rsplit("/", 1)[-1].startswith(stem)
            ),
            key=lambda key: int(key.rsplit("-", 1)[-1].split(".", 1)[0]),
        )
        for key in keys:
            content = await asyncio.to_thread(storage.get, key)
            try:
                probe_image(content)
            except ProbeError:
                log.warning("image_renditions.undecodable", job_id=str(job_id), key=key)
                continue
            return content
        return None

    # -- one rendition -------------------------------------------------------

    async def _build(
        self,
        derivation: RenditionDerivation,
        frame: _Frame,
        slot: _Slot,
        concept: Concept,
        item: ConceptMasters,
        surface: str,
        campaign_type: str,
        market: str,
        language: str,
    ) -> _Made | str:
        """The rendition, or why this derivation cannot give one."""
        creative = self.ctx.require_creative()
        constants = self._constants
        media = self._media_constants
        rules = postprod.required_labels(
            creative.linter.ruleset.disclosure_requirements, surface=surface, market=market
        )
        try:
            pixels = await asyncio.to_thread(
                _postprocess,
                frame.content,
                slot=slot,
                media=media,
                surface=surface,
                logos=self.logos,
                permitted=tuple(constants.logo.permitted_surfaces.value),
                clear_space_ratio=self.clear_space_ratio,
                min_width_px=self.min_width_px,
                width_ratio=constants.logo.width_ratio.value,
                labels=rules,
                label_height_pct=constants.video.caption_height_pct.value,
                quality_floor=constants.media.jpeg_quality_floor.value,
            )
        except (postprod.LabelError, postprod.StampError, ProbeError) as exc:
            return str(exc)
        if isinstance(pixels, str):
            return pixels
        ref = f"{item.asset_id}:{slot.ratio}"
        result, lint = await self._lint(
            pixels.content, ref, surface, campaign_type, market, language, generated=True
        )
        if lint.verdict not in PASSING:
            return f"image lint {lint.verdict} ({', '.join(lint.rule_ids) or 'no finding'})"
        note = pixels.logo_note
        if note and self.logo_unreadable and "no registered logo" in note:
            note = f"{note}: {'; '.join(self.logo_unreadable)}"
        return _Made(
            concept=concept,
            master=item,
            surface=surface,
            slot=slot,
            derivation=derivation,
            frame=frame,
            content=pixels.content,
            probe=pixels.probe,
            transform=pixels.transform,
            disclosure=pixels.disclosure,
            scale=pixels.scale,
            retained=pixels.retained,
            logo_note=note,
            composited=pixels.composited,
            lint=lint,
        )

    async def _lint(
        self,
        content: bytes,
        ref: str,
        surface: str,
        campaign_type: str,
        market: str,
        language: str,
        *,
        generated: bool,
    ) -> tuple[LintResult, CandidateLint]:
        """Measure (Stage 03's precheck), then lint alone at creation against the
        current pin — `lint_candidate`: a lone file is never counted as an ad."""
        creative = self.ctx.require_creative()
        measurement = await asyncio.to_thread(masters.measure_candidate, content, self.templates)
        target = LintTarget(
            ref=ref,
            surface=surface,
            campaign_type=campaign_type,
            market=market,
            language=language,
            image_ref=measurement.image_hash,
            image_metrics=measurement.metrics(),
            generated_by_ai=generated,
        )
        result = creative.linter.lint_candidate(target, now=datetime.now(UTC))
        verdict, unchecked = image_verdict(result)
        return result, CandidateLint(
            verdict=verdict,
            unchecked=unchecked,
            ruleset_version=result.ruleset_version,
            rule_ids=sorted({finding.rule_id for finding in result.findings}),
        )

    # -- logo slots ----------------------------------------------------------

    async def _logo_slots(self, campaign: CampaignConcepts) -> None:
        """Each registered logo fitted by padding to each logo slot the spec
        sheet names for this campaign type (Law 38: fitted, never generated)."""
        surface = IMAGE_SURFACES.get(campaign.campaign_type)
        slots = self._slots(campaign.campaign_type, logos=True)
        if surface is None or not slots:
            return
        if not self.logos:
            why = "no registered logo could be read" + (
                f": {'; '.join(self.logo_unreadable)}" if self.logo_unreadable else ""
            )
            for asset_type, slot in slots:
                self._gap(campaign.campaign_ref, None, surface, slot.ratio, f"{asset_type}: {why}")
            return
        market, language = self._market(campaign.campaign_ref)
        tolerance = self._media_constants.ratio_tolerance
        for logo in self.logos:
            for asset_type, slot in slots:
                width, height = postprod.logo_canvas(
                    logo.image.width, logo.image.height, slot.ratio, slot.min_px,
                    tolerance=tolerance,
                )  # fmt: skip
                fitted = postprod.fit_by_padding(logo.image, width, height)
                buffer = io.BytesIO()
                fitted.save(buffer, format="PNG", optimize=True)
                content = buffer.getvalue()
                label = f"{asset_type} ({logo.label})"
                if slot.max_bytes is not None and len(content) > slot.max_bytes:
                    self._gap(
                        campaign.campaign_ref, None, surface, slot.ratio,
                        f"{label}: {len(content)} bytes exceeds {slot.max_bytes}",
                    )  # fmt: skip
                    continue
                ref = f"logo:{campaign.campaign_ref}:{asset_type}:{logo.asset_id}"
                result, lint = await self._lint(
                    content, ref, surface, campaign.campaign_type, market, language,
                    generated=False,
                )  # fmt: skip
                if lint.verdict not in PASSING:
                    self._gap(
                        campaign.campaign_ref, None, surface, slot.ratio,
                        f"{label}: image lint {lint.verdict} ({', '.join(lint.rule_ids)})",
                    )  # fmt: skip
                    continue
                scale = postprod.padding_scale(logo.image.width, logo.image.height, width, height)
                self.fitted.append(
                    _Fitted(
                        campaign_ref=campaign.campaign_ref,
                        surface=surface,
                        asset_type=asset_type,
                        ratio=slot.ratio,
                        logo=logo,
                        content=content,
                        probe=probe_image(content).as_json(),
                        transform={
                            "sx": float(scale),
                            "sy": float(scale),
                            "fit": "padding",
                            "node_id": NODE_ID,
                        },  # fmt: skip
                        scale=float(scale),
                        max_bytes=slot.max_bytes,
                        result=result,
                        lint=lint,
                    )
                )

    # -- writing, last -------------------------------------------------------

    async def _write_renditions(self) -> list[Rendition]:
        run = self.ctx.run
        storage = self.media.storage
        out: list[Rendition] = []
        for made in self.made:
            slug = made.slot.ratio.replace(":", "x").replace(".", "_")
            key = f"creative/{run.id}/renditions/{made.master.asset_id}/{slug}.jpg"
            await asyncio.to_thread(storage.put, key, made.content, content_type="image/jpeg")
            row = MediaArtifact(
                workspace_id=run.workspace_id,
                asset_id=made.master.asset_id,
                job_id=made.frame.job_id,
                role=MediaArtifactRole.RENDITION,
                storage_path=key,
                media_type="image/jpeg",
                width=made.probe["width"],
                height=made.probe["height"],
                bytes=len(made.content),
                sha256=made.probe["sha256"],
                aspect_ratio=made.slot.ratio,
                derivation=_DERIVATIONS[made.derivation],
                derived_from=made.frame.derived_from,
                transform=made.transform,
                probe=made.probe,
                disclosure=made.disclosure,
            )
            self.ctx.db.add(row)
            await self.ctx.db.flush()
            # §15.5 item 2: the grid's tile is this proxy, never the file.
            await store_preview(self.ctx.db, storage, row, made.content)
            out.append(
                Rendition(
                    concept_id=made.concept.id,
                    campaign_ref=made.concept.campaign_ref,
                    asset_id=made.master.asset_id,
                    media_id=row.id,
                    job_id=made.frame.job_id,
                    surface=made.surface,
                    ratio=made.slot.ratio,
                    px=f"{row.width}x{row.height}",
                    derivation=made.derivation,
                    scale=Scale(sx=made.scale, sy=made.scale),
                    retained_saliency=made.retained,
                    logo_composited=made.composited,
                    logo_note=made.logo_note,
                    bytes=row.bytes,
                    max_bytes=made.slot.max_bytes,
                    lint=made.lint,
                    disclosure=made.disclosure,
                )
            )
        return out

    async def _write_logos(self) -> list[FittedLogo]:
        run = self.ctx.run
        storage = self.media.storage
        creative = self.ctx.require_creative()
        out: list[FittedLogo] = []
        for fitted in self.fitted:
            sha = hashlib.sha256(fitted.content).hexdigest()
            asset = CreativeAsset(
                workspace_id=run.workspace_id,
                project_id=run.project_id,
                creative_run_id=run.id,
                node_id=NODE_ID,
                campaign_ref=fitted.campaign_ref,
                kind=CreativeAssetKind.LOGO,
                surface=fitted.surface,
                generated_by_ai=False,
                status=CreativeAssetStatus.LINTED,
                lint=fitted.result.model_dump(mode="json"),
                ruleset_version=creative.linter.pin,
                fields={
                    "registered_logo_id": str(fitted.logo.asset_id),
                    "asset_type": fitted.asset_type,
                },
                lineage={
                    "origin": "reused",
                    "node_id": NODE_ID,
                    "registered_logo_id": str(fitted.logo.asset_id),
                },
                content_hash=sha,
            )
            self.ctx.db.add(asset)
            await self.ctx.db.flush()
            key = (
                f"creative/{run.id}/logos/{fitted.logo.asset_id}/"
                f"{fitted.campaign_ref}-{fitted.asset_type}.png"
            )
            await asyncio.to_thread(storage.put, key, fitted.content, content_type="image/png")
            row = MediaArtifact(
                workspace_id=run.workspace_id,
                asset_id=asset.id,
                role=MediaArtifactRole.RENDITION,
                storage_path=key,
                media_type="image/png",
                width=fitted.probe["width"],
                height=fitted.probe["height"],
                bytes=len(fitted.content),
                sha256=sha,
                aspect_ratio=fitted.ratio,
                derivation=MediaArtifactDerivation.COMPOSITED,
                transform=fitted.transform,
                probe=fitted.probe,
                # Nobody generated a registered logo: there is nothing to disclose.
                disclosure=None,
            )
            self.ctx.db.add(row)
            await self.ctx.db.flush()
            await store_preview(self.ctx.db, storage, row, fitted.content)
            out.append(
                FittedLogo(
                    campaign_ref=fitted.campaign_ref,
                    asset_type=fitted.asset_type,
                    ratio=fitted.ratio,
                    px=f"{row.width}x{row.height}",
                    registered_logo_id=fitted.logo.asset_id,
                    asset_id=asset.id,
                    media_id=row.id,
                    scale=Scale(sx=fitted.scale, sy=fitted.scale),
                    surface=fitted.surface,
                    max_bytes=fitted.max_bytes,
                    bytes=row.bytes,
                    lint=fitted.lint,
                )
            )
        return out

    # -- small reads ---------------------------------------------------------

    def _slots(self, campaign_type: str, *, logos: bool) -> Any:
        """The image slots (`logos=False`: one `_Slot` per ratio) or the logo
        slots (`logos=True`: `(asset_type, _Slot)` pairs) of a campaign type."""
        specs = self.ctx.require_creative().linter.ruleset.asset_specs.for_campaign(campaign_type)
        rows = [
            (asset_type, spec)
            for asset_type, spec in specs.items()
            if spec.ratio and "video" not in asset_type and ("logo" in asset_type) == logos
        ]
        if logos:
            return [(asset_type, _slot([spec])) for asset_type, spec in rows]
        by_ratio: dict[str, list[AssetSpec]] = {}
        for _, spec in rows:
            by_ratio.setdefault(str(spec.ratio), []).append(spec)
        return [_slot(group) for group in by_ratio.values()]

    def _market(self, campaign_ref: str) -> tuple[str, str]:
        planned = {
            (c.campaign_ref or c.name): c
            for c in self.ctx.require_creative().input.account_structure.campaigns
        }
        plan = planned.get(campaign_ref)
        return (getattr(plan, "market", "") or "*"), (getattr(plan, "language", None) or "en")

    def _gap(
        self, campaign_ref: str, concept_id: str | None, surface: str, ratio: str, why: str
    ) -> None:
        self.gaps.append(
            RenditionGap(
                campaign_ref=campaign_ref,
                concept_id=concept_id,
                surface=surface,
                ratio=ratio,
                why=why,
            )
        )


# ---------------------------------------------------------------------------
# the pixel work, off the event loop
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Pixels:
    content: bytes
    probe: dict[str, Any]
    transform: dict[str, Any]
    disclosure: dict[str, Any]
    scale: float
    retained: float
    composited: bool
    logo_note: str | None


def _postprocess(
    content: bytes,
    *,
    slot: _Slot,
    media: Any,
    surface: str,
    logos: list[postprod.LogoArt],
    permitted: tuple[str, ...],
    clear_space_ratio: float | None,
    min_width_px: int | None,
    width_ratio: float,
    labels: list[Any],
    label_height_pct: float,
    quality_floor: int,
) -> _Pixels | str:
    """Window → uniform geometry → pixels → logo → labels → encode → stamp →
    probe. A string is why this frame gives no rendition at this ratio."""
    source, calc_bytes = _upright(content)
    out_w, out_h = postprod.target_size(
        source.width, source.height, slot.ratio, slot.min_px, tolerance=media.ratio_tolerance
    )
    window = crop_window_v1(image=calc_bytes, ratio=f"{out_w}:{out_h}", constants=media)
    retained = float(window.result["retained"])
    if window.result["decision"] == "gap":
        return (
            f"the best {slot.ratio} window keeps {retained:.0%} of the saliency, under the "
            f"{media.crop_min_saliency_retained:.0%} floor"
        )
    x0, y0 = window.result["box"][0], window.result["box"][1]
    placed = postprod.geometry(
        source.width, source.height, out_w, out_h, origin=(Fraction(x0), Fraction(y0))
    )
    frame = postprod.render(source, placed)
    outcome = postprod.place_logo(
        frame,
        logos,
        surface=surface,
        permitted_surfaces=permitted,
        clear_space_ratio=clear_space_ratio,
        min_width_px=min_width_px,
        width_ratio=width_ratio,
        saliency=spectral_residual(np.asarray(frame.convert("L"))),
    )
    if outcome.placement is not None:
        frame = postprod.composite_logo(frame, outcome.placement)
    frame, drawn = postprod.apply_labels(frame, labels, height_pct=label_height_pct)
    done = postprod.encode_and_stamp(
        frame,
        max_bytes=slot.max_bytes,
        quality_floor=quality_floor,
        composited=outcome.placement is not None,
    )
    if done is None:
        return (
            f"does not fit {slot.max_bytes} bytes at JPEG quality {quality_floor} or above, "
            "stamp included"
        )
    stamped, encoder_args, disclosure = done
    facts = probe_image(stamped)
    if (facts.width, facts.height) != (out_w, out_h) or facts.exif or facts.gps:
        return f"the written file is not the rendition planned: {facts.as_json()}"
    transform = {
        **placed.transform(),
        "retained_saliency": retained,
        "crop_window": {
            "formula_id": window.formula_id,
            "calc_version": window.calc_version,
            "inputs_hash": window.inputs_hash,
        },
        "logo": outcome.placement.record() if outcome.placement is not None else None,
        "logo_note": outcome.reason,
        "labels": drawn,
        "encoder_args": encoder_args,
        "node_id": NODE_ID,
    }
    return _Pixels(
        content=stamped,
        probe=facts.as_json(),
        transform=transform,
        disclosure={**disclosure, "visible_labels": drawn},
        scale=transform["sx"],
        retained=retained,
        composited=outcome.placement is not None,
        logo_note=outcome.reason,
    )


def _upright(content: bytes) -> tuple[Image.Image, bytes]:
    """The frame the way it is meant to be seen, and the bytes the crop window
    is computed on. Only when an EXIF orientation has to be applied are they
    re-encoded (losslessly), so the window and the pixels share one frame."""
    with Image.open(io.BytesIO(content)) as opened:
        orientation = opened.getexif().get(0x0112, 1)
        opened.load()
        upright = ImageOps.exif_transpose(opened) if orientation != 1 else opened.copy()
    if orientation == 1:
        return upright, content
    buffer = io.BytesIO()
    upright.save(buffer, format="PNG")
    return upright, buffer.getvalue()


def _best_source(frames: list[_Frame], ratio: str) -> _Frame:
    """The painted frame a crop to `ratio` keeps most of; the master on a tie."""
    wanted = parse_ratio(ratio)
    return max(
        frames,
        key=lambda frame: (
            best_crop_retention(wanted, [frame.width / frame.height]),
            -frames.index(frame),
        ),
    )


def _slot(specs: list[AssetSpec]) -> _Slot:
    sizes = [postprod.parse_px(spec.min_px) for spec in specs if spec.min_px]
    caps = [spec.max_bytes for spec in specs if spec.max_bytes is not None]
    return _Slot(
        ratio=str(specs[0].ratio),
        min_px=f"{max(w for w, _ in sizes)}x{max(h for _, h in sizes)}" if sizes else None,
        max_bytes=min(caps) if caps else None,
    )


async def registered_logos(
    ctx: RunContext, media: MediaJobs
) -> tuple[list[postprod.LogoArt], list[str]]:
    """The pinned ruleset's registered logos, read from the files Stage 03
    registered them from (`brand_book_asset.asset_path`). Composited locally:
    a logo file never goes to a media provider (Law 44)."""
    templates = ctx.require_creative().linter.ruleset.logo_templates
    if not templates:
        return [], []
    rows = {
        row.id: row
        for row in (
            await ctx.db.execute(
                sa.select(Evidence).where(
                    Evidence.project_id == ctx.project.id,
                    Evidence.id.in_([template.asset_id for template in templates]),
                )
            )
        ).scalars()
    }
    logos: list[postprod.LogoArt] = []
    unreadable: list[str] = []
    for template in templates:
        row = rows.get(template.asset_id)
        path = (row.payload or {}).get("asset_path") if row is not None else None
        if not path:
            unreadable.append(f"{template.label}: no stored file")
            continue
        try:
            content = await asyncio.to_thread(media.storage.get, str(path))
            logos.append(
                postprod.load_logo(content, asset_id=template.asset_id, label=template.label)
            )
        except Exception as exc:  # noqa: BLE001 — an unreadable logo is recorded, not fatal
            unreadable.append(f"{template.label}: {exc}")
    return logos, unreadable


def positive_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return None
    return float(value)


def positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return None
    return int(value)


IMAGE_RENDITIONS = ImageRenditionsNode()
