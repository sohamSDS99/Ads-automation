"""4.4.2 `image_masters` — candidates per concept, linted, then ranked (Stage 04 PRD §11).

For every concept 4.4.1 gave an image surface, and only after G7 (`media/jobs.py`
refuses otherwise):

1. The concept's image asset is committed on its own, before any submit — the
   `GenerationJob` rows reference it, and a resumed node must find the same one.
2. The references the request may carry are chosen and loaded through
   `media/references.py`, which judges each one again at this moment (Law 44):
   a third-party reference goes only with a cleared H3 `image_right`.
3. Candidates are requested through `MediaJobs.submit_or_resume` — submit once,
   resume always (Law 37), budget reserved first (Law 43) — distinct by `n` or a
   derived seed (`creative/masters.py`).
4. Every candidate is measured and linted against the run's pin at creation
   (Law 33). **A candidate that fails lint is discarded before ranking**: VISION
   never sees it and it can never be the master.
5. VISION ranks the survivors — advisory. No survivor ⇒ one retry with
   strengthened negatives, then a `gap`.

The chosen candidate becomes the master; the asset leaves `draft` only with the
master's passing `LintResult` against the current pin.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
import structlog
from PIL import Image
from pydantic import BaseModel

from agent.calc.media import image_job_price
from agent.creative import masters
from agent.db.models import (
    CreativeAsset,
    CreativeAssetKind,
    CreativeAssetStatus,
    Evidence,
    GenerationJob,
    GenerationStatus,
    MediaArtifact,
    MediaArtifactDerivation,
    MediaArtifactRole,
    RunStage,
)
from agent.db.session import get_sessionmaker
from agent.guardrails.verdicts import image_verdict
from agent.llm.gateway import InlineImage, LLMTransportError, StructuredOutputError
from agent.llm.router import TaskClass
from agent.media import references
from agent.media.jobs import MediaJobs
from agent.media.types import CapabilityRecord, ImageRequest, ReferenceImage
from agent.nodes.base import NodeContractError, NodeSpec, RunContext
from agent.orchestrator.creative_input import ratios_for
from agent.orchestrator.creative_run import PASSING
from agent.schemas.creative_input import CreativeInput, MediaModelChoice
from agent.schemas.creative_media import (
    Candidate,
    CandidateLint,
    Concept,
    ConceptGap,
    ConceptMasters,
    CreativeConcepts,
    ImageMasters,
    Master,
    VisionAdvisory,
)
from agent.schemas.guardrails import LintResult, LintTarget

log = structlog.get_logger(__name__)

NODE_ID = "4.4.2"
CONCEPTS_NODE = "4.4.1"

#: Request fields a model choice's defaults may set here. The rest — model,
#: prompt, n, seed, references, provider — are this node's or the gateway's.
_DEFAULT_FIELDS = frozenset(
    {"aspect_ratio", "resolution", "size", "quality", "output_format", "background",
     "output_compression"}
)  # fmt: skip
_FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


class ImageMastersNode:
    spec = NodeSpec(
        id=NODE_ID,
        name="image_masters",
        stage="4.4",
        run_stage=RunStage.CREATIVE,
        depends_on=(CONCEPTS_NODE,),
        task_class=TaskClass.VISION,
        input_model=CreativeInput,
        output_model=ImageMasters,
        media=("image",),
        lint_required=True,
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        return []

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        creative = ctx.require_creative()
        inp = creative.input
        if not inp.scope.images:
            return ImageMasters()
        choice = next((c for c in inp.media_models if c.modality == "image"), None)
        if choice is None:
            raise NodeContractError("images are in scope but the input carries no image model")
        concepts = CreativeConcepts.model_validate(ctx.output_of(CONCEPTS_NODE))
        mastering = await _Mastering.start(ctx, choice)
        specs = creative.linter.ruleset.asset_specs.model_dump(mode="json").get("specs") or {}
        planned = {(c.campaign_ref or c.name): c for c in inp.account_structure.campaigns}
        results: list[ConceptMasters] = []
        for campaign in concepts.campaigns:
            image_ratios, _ = ratios_for(specs, campaign.campaign_type)
            plan = planned.get(campaign.campaign_ref)
            for concept in campaign.concepts:
                if not concept.surfaces:
                    continue  # no image surface: 4.4.4's concern, not a master's
                results.append(
                    await mastering.concept(
                        concept,
                        campaign_type=campaign.campaign_type,
                        market=(getattr(plan, "market", "") or "*"),
                        language=(getattr(plan, "language", None) or "en"),
                        image_ratios=image_ratios,
                    )
                )
        return ImageMasters(concepts=results)


@dataclass
class _Judged:
    artifact: MediaArtifact
    content: bytes
    job_id: uuid.UUID
    seed: int | None
    attempt: int
    result: LintResult
    lint: CandidateLint

    @property
    def passes(self) -> bool:
        return self.lint.verdict in PASSING


@dataclass
class _Mastering:
    ctx: RunContext
    choice: MediaModelChoice
    capability: CapabilityRecord
    media: MediaJobs
    facts: list[references.ReferenceFacts]
    cleared: frozenset[uuid.UUID]
    allowed: bool
    count: int
    max_bytes: int
    templates: tuple[Any, ...]

    @classmethod
    async def start(cls, ctx: RunContext, choice: MediaModelChoice) -> _Mastering:
        creative = ctx.require_creative()
        constants = creative.constants.media_constants()
        return cls(
            ctx=ctx,
            choice=choice,
            capability=CapabilityRecord.model_validate(choice.capability),
            media=ctx.require_media(),
            facts=await references.live_facts(
                ctx.db,
                project_id=ctx.project.id,
                reference_ids=[ref.reference_id for ref in creative.input.references],
            ),
            cleared=await references.cleared_image_rights(ctx.db, ctx.run.id),
            allowed=await references.project_allows_references(ctx.db, ctx.project.id),
            count=constants.candidates_per_concept,
            max_bytes=constants.reference_max_bytes,
            templates=masters.logo_templates(creative.linter.ruleset),
        )

    # -- one concept ---------------------------------------------------------

    async def concept(
        self,
        concept: Concept,
        *,
        campaign_type: str,
        market: str,
        language: str,
        image_ratios: list[str],
    ) -> ConceptMasters:
        asset = await self._asset(concept)
        sent = await self._references(concept)
        ratio = masters.master_ratio(image_ratios, self.capability) or self.choice.defaults.get(
            "aspect_ratio"
        )
        judged: list[_Judged] = []
        passing: list[_Judged] = []
        gap: ConceptGap | None = None
        for attempt in (1, 2):
            made, failed = await self._candidates(
                concept, asset, sent, ratio, attempt, campaign_type, market, language
            )
            judged.extend(made)
            if not made:
                gap = failed
                break
            # Law 33: discarded BEFORE ranking — only these ever reach VISION.
            passing = [candidate for candidate in made if candidate.passes]
            if passing:
                break
        else:
            gap = ConceptGap(
                reason="all_candidates_failed_lint",
                detail=f"Every candidate failed image lint, including the retry with "
                f"strengthened negatives ({len(judged)} candidates).",
            )

        master: Master | None = None
        advisory: dict[uuid.UUID, VisionAdvisory] = {}
        if passing:
            chosen, why, advisory = await self._rank(concept, passing)
            await self._keep(asset, judged, chosen)
            master = Master(media_id=chosen.artifact.id, why=why)
        return ConceptMasters(
            concept_id=concept.id,
            campaign_ref=concept.campaign_ref,
            asset_id=asset.id,
            aspect_ratio=ratio,
            reference_sha256s=[ref.sha256 for ref in sent],
            candidates=[
                Candidate(
                    job_id=item.job_id,
                    media_id=item.artifact.id,
                    seed=item.seed,
                    attempt=item.attempt,
                    lint=item.lint,
                    vision_advisory=advisory.get(item.artifact.id),
                )
                for item in judged
            ],
            master=master,
            gap=gap,
        )

    async def _asset(self, concept: Concept) -> CreativeAsset:
        """The concept's image asset, committed on its own before any submit.

        `generation_job.asset_id` references it and the job row is committed in
        the gateway's own transaction (Law 37), so the asset must already be
        visible there; and a resumed node must find this row, not make another.
        """
        run = self.ctx.run
        query = sa.select(CreativeAsset.id).where(
            CreativeAsset.creative_run_id == run.id,
            CreativeAsset.node_id == NODE_ID,
            CreativeAsset.fields["concept_id"].astext == concept.id,
        )
        async with get_sessionmaker()() as session:
            asset_id = await session.scalar(query)
            if asset_id is None:
                row = CreativeAsset(
                    workspace_id=run.workspace_id,
                    project_id=run.project_id,
                    creative_run_id=run.id,
                    node_id=NODE_ID,
                    campaign_ref=concept.campaign_ref,
                    kind=CreativeAssetKind.IMAGE,
                    surface=concept.surfaces[0],
                    generated_by_ai=True,
                    fields={
                        "concept_id": concept.id,
                        "product_depiction": concept.product_depiction,
                    },
                    lineage={"origin": "generated", "node_id": NODE_ID},
                    content_hash=hashlib.sha256(concept.prompt.encode("utf-8")).hexdigest(),
                )
                session.add(row)
                await session.commit()
                asset_id = row.id
        asset = await self.ctx.db.get(CreativeAsset, asset_id)
        if asset is None:  # pragma: no cover — committed just above
            raise NodeContractError(f"image asset for concept {concept.id} vanished")
        return asset

    async def _references(self, concept: Concept) -> list[ReferenceImage]:
        """What this concept's requests carry — chosen, then loaded and judged
        again (Law 44). A third-party reference without a cleared H3
        `image_right` in this run is never among them."""
        chosen = references.select_references(
            self.facts,
            concept.product_depiction,
            allowed=self.allowed,
            capability=self.capability,
            cleared=self.cleared,
            max_bytes=self.max_bytes,
        )
        return await references.load_for_request(
            self.ctx.db,
            self.media.storage,
            run_id=self.ctx.run.id,
            project_id=self.ctx.project.id,
            allowed=await references.project_allows_references(self.ctx.db, self.ctx.project.id),
            capability=self.capability,
            reference_ids=[fact.reference_id for fact in chosen],
            max_bytes=self.max_bytes,
        )

    async def _candidates(
        self,
        concept: Concept,
        asset: CreativeAsset,
        sent: list[ReferenceImage],
        ratio: str | None,
        attempt: int,
        campaign_type: str,
        market: str,
        language: str,
    ) -> tuple[list[_Judged], ConceptGap | None]:
        """One attempt's candidates, each linted. Jobs that did not complete
        contribute nothing; with no candidate at all the attempt is a gap."""
        defaults = {k: v for k, v in self.choice.defaults.items() if k in _DEFAULT_FIELDS}
        prompt = masters.request_prompt(concept, ratio, strengthened=attempt == 2)
        made: list[_Judged] = []
        failures: list[str] = []
        blocked = False
        for fields in masters.candidate_requests(
            self.capability,
            self.count,
            run_id=self.ctx.run.id,
            concept_id=concept.id,
            attempt=attempt,
        ):
            request = ImageRequest(
                **{**defaults, "aspect_ratio": ratio, **fields},
                model=self.choice.model_id,
                prompt=prompt,
                input_references=sent or None,
            )
            params = request.model_dump(
                mode="json", exclude_none=True, exclude={"model", "prompt", "input_references"}
            )
            estimate = image_job_price(
                self.capability, params, self.ctx.require_creative().constants.media_constants()
            ).usd
            job = await self.media.submit_or_resume(
                run_id=self.ctx.run.id,
                node_id=NODE_ID,
                asset_id=asset.id,
                round=1,
                request=request,
                choice=self.choice,
                estimate_usd=estimate,
                ledger=self.ctx.ledger,
            )
            if job.status is GenerationStatus.BLOCKED_BY_BUDGET:
                blocked = True
                break  # the cap will refuse the next request too
            if job.status is not GenerationStatus.COMPLETED:
                failures.append(f"job {job.id} ended {job.status.value}")
                continue
            for artifact, content in await self._stored(job, asset, ratio):
                made.append(
                    await self._judge(
                        artifact,
                        content,
                        job,
                        fields.get("seed"),
                        attempt,
                        concept,
                        campaign_type,
                        market,
                        language,
                    )  # fmt: skip
                )
        if made:
            return made, None
        if blocked:
            return [], ConceptGap(
                reason="blocked_by_budget",
                detail="The media budget refused the request before it was sent; nothing was "
                "spent on this concept.",
            )
        return [], ConceptGap(
            reason="generation_failed",
            detail="; ".join(failures) or "The model returned no image that could be decoded.",
        )

    async def _stored(
        self, job: GenerationJob, asset: CreativeAsset, ratio: str | None
    ) -> list[tuple[MediaArtifact, bytes]]:
        """The job's files, `creative/{run}/media/{asset}/{job}-{i}.{ext}`, as
        candidate artifacts — found again, not duplicated, on a resume."""
        storage = self.media.storage
        # No trailing slash: the local backend resolves a prefix as a path.
        folder = f"creative/{job.creative_run_id}/media/{asset.id}"
        stem = f"{job.id}-"
        keys = sorted(
            (
                info.key
                for info in await asyncio.to_thread(lambda: list(storage.iter_objects(folder)))
                if info.key.rsplit("/", 1)[-1].startswith(stem)
            ),
            key=lambda key: int(key.rsplit("-", 1)[-1].split(".", 1)[0]),
        )
        stored: list[tuple[MediaArtifact, bytes]] = []
        for key in keys:
            content = await asyncio.to_thread(storage.get, key)
            artifact = await self._artifact(job, asset, key, content, ratio)
            if artifact is not None:
                stored.append((artifact, content))
        return stored

    async def _artifact(
        self,
        job: GenerationJob,
        asset: CreativeAsset,
        key: str,
        content: bytes,
        ratio: str | None,
    ) -> MediaArtifact | None:
        existing = await self.ctx.db.scalar(
            sa.select(MediaArtifact).where(
                MediaArtifact.asset_id == asset.id, MediaArtifact.storage_path == key
            )
        )
        if existing is not None:
            return existing
        try:
            with Image.open(io.BytesIO(content)) as image:
                image.load()
                width, height = image.size
                fmt, mode = (image.format or "").upper(), image.mode
        except (OSError, SyntaxError, ValueError, Image.DecompressionBombError):
            log.warning("image_masters.undecodable", job_id=str(job.id), key=key)
            return None
        artifact = MediaArtifact(
            workspace_id=asset.workspace_id,
            asset_id=asset.id,
            job_id=job.id,
            role=MediaArtifactRole.CANDIDATE,
            storage_path=key,
            media_type=_FORMATS.get(fmt, "application/octet-stream"),
            width=width,
            height=height,
            bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            aspect_ratio=ratio or f"{width}:{height}",
            derivation=MediaArtifactDerivation.NATIVE,
            probe={"format": fmt, "mode": mode, "width": width, "height": height},
        )
        self.ctx.db.add(artifact)
        await self.ctx.db.flush()
        return artifact

    async def _judge(
        self,
        artifact: MediaArtifact,
        content: bytes,
        job: GenerationJob,
        seed: int | None,
        attempt: int,
        concept: Concept,
        campaign_type: str,
        market: str,
        language: str,
    ) -> _Judged:
        """Measure, then lint against the run's current pin — at creation."""
        creative = self.ctx.require_creative()
        measurement = await asyncio.to_thread(masters.measure_candidate, content, self.templates)
        target = LintTarget(
            ref=str(artifact.id),
            surface=concept.surfaces[0],
            campaign_type=campaign_type,
            market=market,
            language=language,
            image_ref=measurement.image_hash,
            image_metrics=measurement.metrics(),
            generated_by_ai=True,
        )
        # One candidate, alone, at creation: `lint_candidate` applies every
        # per-target rule and leaves out only the set rules. "At least one
        # image_square" or "3 to 15 headlines" is a property of the assembled
        # ad; asked of a lone image it is "0 of …" and fails every candidate.
        result = creative.linter.lint_candidate(target, now=datetime.now(UTC))
        verdict, unchecked = image_verdict(result)
        return _Judged(
            artifact=artifact,
            content=content,
            job_id=job.id,
            seed=seed,
            attempt=attempt,
            result=result,
            lint=CandidateLint(
                verdict=verdict,
                unchecked=unchecked,
                ruleset_version=result.ruleset_version,
                rule_ids=sorted({finding.rule_id for finding in result.findings}),
            ),
        )

    async def _rank(
        self, concept: Concept, passing: list[_Judged]
    ) -> tuple[_Judged, str, dict[uuid.UUID, VisionAdvisory]]:
        """VISION's pick among the candidates that passed lint — advisory only."""
        if len(passing) == 1:
            return passing[0], "The only candidate that passed image lint.", {}
        keys = [f"c{index + 1}" for index in range(len(passing))]
        try:
            ranked = await self.ctx.complete(
                masters.ranking_model(keys),
                system=masters.VISION_SYSTEM,
                user=_vision_prompt(concept, keys),
                images=[
                    InlineImage(media_type=item.artifact.media_type, data=item.content)
                    for item in passing
                ],
            )
        except (StructuredOutputError, LLMTransportError) as exc:
            log.warning("image_masters.vision_unavailable", concept_id=concept.id, error=str(exc))
            return (
                passing[0],
                f"VISION could not rank the candidates ({type(exc).__name__}); the first "
                "candidate that passed image lint is the master.",
                {},
            )
        by_key = dict(zip(keys, passing, strict=True))
        advisory = {
            by_key[note.candidate].artifact.id: VisionAdvisory(
                note=note.note, flags=list(note.flags)
            )
            for note in getattr(ranked, "notes")  # noqa: B009 — a dynamic model
        }
        best = getattr(ranked, "best")  # noqa: B009
        return by_key[best], f"VISION (advisory): {getattr(ranked, 'why')}", advisory  # noqa: B009

    async def _keep(self, asset: CreativeAsset, judged: list[_Judged], chosen: _Judged) -> None:
        """The master leaves `draft` with its passing LintResult at the current pin."""
        for item in judged:
            item.artifact.role = MediaArtifactRole.CANDIDATE
        chosen.artifact.role = MediaArtifactRole.MASTER
        creative = self.ctx.require_creative()
        asset.status = CreativeAssetStatus.LINTED
        asset.lint = chosen.result.model_dump(mode="json")
        asset.ruleset_version = creative.linter.pin
        asset.content_hash = chosen.artifact.sha256
        asset.fields = {**(asset.fields or {}), "master_media_id": str(chosen.artifact.id)}
        await self.ctx.db.flush()


def _vision_prompt(concept: Concept, keys: list[str]) -> str:
    lines = [
        f"CONCEPT: {concept.name}",
        f"ANGLE: {concept.angle_text}",
        f"SUBJECT: {concept.subject}",
        f"SETTING: {concept.setting}",
        f"PRODUCT DEPICTION: {concept.product_depiction}",
        f"CANDIDATES: {', '.join(keys)} — in the order the images are attached.",
    ]
    return "\n".join(lines)


IMAGE_MASTERS = ImageMastersNode()
