"""4.4.4 `video_production` — script, shot plan, clips, and the finished videos
post-production makes from them (Stage 04 PRD §11 4.4.4, §9.4 video 1–5, §8.4,
§18).

`not_required` when video is off, or when no campaign in the slate has a video
surface under the pinned spec sheet. Otherwise, per campaign that has one:

1. **gather** — the length the spec's duration window allows
   (`video_duration`), cut by `media.shot_plan_v1` into clips of the model's
   `supported_durations` only, and recorded as a `derived` row. No duration in
   the spec is `spec_missing`, never a guess (§9.5, Q6). A spec maximum keeps
   room for the end card post-production appends: the finished file is what
   the spec measures.
2. **The script** — one shot per clip, written by `COPYWRITE`, timed by the
   plan, captioned from its voiceover, validated as working with the sound off
   and linted at creation (Law 33) — then **committed on its own, before any
   clip is paid for**. A resumed node reads that row back instead of asking the
   model again: a new script is new prompts, new idempotency keys, and a second
   bill for every clip (Law 37).
3. **The clips** — for every required ratio the model paints (`native`, or
   `relaid`: 4.4.4 has no master to relay from, so it paints at that ratio),
   one job per shot through `MediaJobs.submit_or_resume`: budget reserved
   first (Law 43), capability-validated (Law 36), never re-POSTed. A ratio the
   model does not paint is cropped in post-production from one it does, or is
   a gap. The budget refusing a clip stops every later submit.
4. **The wait** — every submitted clip is polled at once (10 s → 30 s with
   jitter, `node.progress` on every poll) and downloaded in the worker. A
   finished clip becomes a `MediaArtifact(role=clip)` of what ffprobe reads back.

5. **Post-production** — per required ratio, one at a time, the ratio's clips
   (a crop takes the ratio it is cut from) go through `postprod/video.py`:
   assembled, stamped, then verified by `postprod/verify.py` from the file
   itself. Only a verified file becomes a `rendition` (with its 480p proxy and
   poster); a failed verification is a blocking gap carrying what was read.
   Free Volume space is re-checked before each assembly. ffmpeg exiting
   non-zero keeps its stderr tail and is retried once with conservative
   arguments, then is a gap with both tails (§18). A retried node clears the
   renditions an earlier attempt wrote and makes them again from the clips.

A clip that failed, was cancelled or expired, or whose submit state is unknown
is a gap: nothing here POSTs a video twice, and `unknown_submit_state` is a
human's decision (§18). A clip that **timed out** is not a gap — its OpenRouter
job is still alive — so the node fails naming it: Check again resumes polling
in the worker, and a retry of this node then finds the clip finished.
"""

from __future__ import annotations

import asyncio
import math
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sqlalchemy as sa
import structlog
from pydantic import BaseModel

from agent.calc.derived import DerivedWriter
from agent.calc.media import shot_plan_v1, video_duration, video_job_price
from agent.creative import masters, video
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
from agent.evidence.store import EvidenceStore
from agent.llm.router import TaskClass
from agent.media.capability import best_crop_retention, parse_ratio, ratio_coverage
from agent.media.jobs import MediaJobs
from agent.media.types import CapabilityRecord, VideoRequest
from agent.media.videos import Poll
from agent.nodes.base import NodeContractError, NodeSpec, RunContext
from agent.nodes.creative._text_assets import content_hash, generated_lineage
from agent.nodes.creative.n4_4_1_creative_concepts import approved_brief
from agent.nodes.creative.n4_4_3_image_renditions import (
    positive_float,
    positive_int,
    registered_logos,
)
from agent.orchestrator.creative_input import ratios_for
from agent.orchestrator.creative_run import PASSING
from agent.postprod import image as still
from agent.postprod import verify as checks
from agent.postprod import video as post
from agent.postprod.image import supported_label
from agent.postprod.probe import ProbeError, VideoFacts, probe_image, probe_video
from agent.schemas.creative_brief import CreativeBrief
from agent.schemas.creative_input import CreativeInput, MediaModelChoice
from agent.schemas.creative_media import CampaignConcepts, Concept, CreativeConcepts
from agent.schemas.creative_video import (
    CampaignVideo,
    DurationWindowOut,
    FfmpegAttempt,
    PlannedClip,
    ScriptLint,
    ShotPlanOut,
    VideoClip,
    VideoGap,
    VideoProduction,
    VideoRendition,
    VideoScript,
    VideoVerification,
)
from agent.schemas.guardrails import LintResult, LintTarget

log = structlog.get_logger(__name__)

NODE_ID = "4.4.4"
CONCEPTS_NODE = "4.4.1"
SCRIPT_SURFACE = "youtube_script"
#: §7.1's Stage 03 delta names the surface a video frame is linted as.
VIDEO_SURFACE = "video_frame"
#: `logo.permitted_surfaces` (§9.5) names video as `video`.
LOGO_SURFACE = "video"
#: The media roles post-production writes on the video asset.
_POSTPROD_ROLES = (MediaArtifactRole.RENDITION, MediaArtifactRole.PREVIEW, MediaArtifactRole.POSTER)

#: The coverages a model paints at: `relaid` too, since 4.4.4 has no master to
#: relay from — the model paints that ratio from the prompt.
_PAINTED = frozenset({"native", "relaid"})
#: The statuses `await_video` leaves a clip in that end it without a file.
_ENDED = {
    GenerationStatus.FAILED: "failed",
    GenerationStatus.CANCELLED: "cancelled",
    GenerationStatus.EXPIRED: "expired",
}

SYSTEM = """You write the script of a short video ad, one shot at a time.

Rules:
- Return exactly one entry in `shots` per shot listed, in order. Each shot's
  length is fixed; write for it.
- `visual`: what the camera shows — concrete and filmable. Never text, captions,
  logos or the product itself: captions and the logo are added in editing, and
  the product is not shown.
- `voiceover`: what is said over the shot, short enough to say in its length
  (about two and a half words a second), or null for a silent shot. It is
  burned in word for word as captions, so it must read well.
- `on_screen_text`: a few words shown over the shot, or null. The last shot
  shows the call to action.
- `cta`: the call to action, verb first. It must appear word for word in some
  shot's `on_screen_text` — the ad has to work with the sound off.
- Assert nothing about the product except the proof points listed. Name no
  competitor and no real person. Use the voice words; never use a never term.
"""


class VideoProductionNode:
    spec = NodeSpec(
        id=NODE_ID,
        name="video_production",
        stage="4.4",
        run_stage=RunStage.CREATIVE,
        depends_on=(CONCEPTS_NODE,),
        task_class=TaskClass.COPYWRITE,
        input_model=CreativeInput,
        output_model=VideoProduction,
        media=("video",),
        lint_required=True,
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        """Each campaign's length and shot plan — the numbers — as `derived`
        rows this node computed, so the output can cite them."""
        creative = ctx.require_creative()
        inp = creative.input
        if not inp.scope.video:
            ctx.scratch[NODE_ID] = _Planned(why="Video is off for this run.")
            return []
        choice = _video_choice(inp)
        capability = CapabilityRecord.model_validate(choice.capability)
        supported = list((capability.video.durations if capability.video else []) or [])
        specs = creative.linter.ruleset.asset_specs.model_dump(mode="json").get("specs") or {}
        concepts = CreativeConcepts.model_validate(ctx.output_of(CONCEPTS_NODE))
        end_card_s = math.ceil(creative.constants.video.end_card_ms.value / 1000)
        planned = _Planned()
        writer = DerivedWriter(
            ctx.db,
            store=EvidenceStore(ctx.db, ctx.run.workspace_id),
            project_id=ctx.project.id,
            plan_run_id=ctx.run.id,
        )
        for campaign in concepts.campaigns:
            types = video.video_specs(specs, campaign.campaign_type)
            if not types or not campaign.concepts:
                continue
            planned.surfaced += 1
            try:
                window = video.duration_window(types, campaign_type=campaign.campaign_type)
                duration = video_duration(
                    min_s=window.min_s,
                    max_s=(window.max_s - end_card_s) if window.max_s is not None else None,
                    supported=supported,
                )
            except video.VideoPlanProblem as problem:
                planned.gaps.append(_gap(campaign.campaign_ref, problem.reason, problem.detail))
                continue
            if duration is None:
                planned.gaps.append(
                    _gap(
                        campaign.campaign_ref,
                        "no_plannable_duration",
                        f"No length between {window.min_s or 1} s and "
                        f"{window.max_s or 'no maximum'}"
                        f"{' s' if window.max_s else ''}"
                        f"{f' (less the {end_card_s} s end card)' if window.max_s else ''} "
                        "can be cut from "
                        f"{choice.model_id}'s clips of "
                        f"{', '.join(f'{d} s' for d in sorted(supported)) or 'no duration'}.",
                    )
                )
                continue
            plan = shot_plan_v1(
                duration_s=duration,
                supported_durations=supported,
                constants=creative.constants.media_constants(),
            )
            evidence_id = await writer.record(plan, node_id=NODE_ID)
            planned.campaigns.append(
                _CampaignPlan(
                    concepts=campaign,
                    window=window,
                    duration_s=duration,
                    clips=[PlannedClip.model_validate(c) for c in plan.result["clips"]],
                    evidence_id=evidence_id,
                )
            )
        if not planned.surfaced:
            planned.why = (
                "No campaign in the slate has a video surface under the pinned spec sheet "
                f"(ruleset {creative.linter.pin})."
            )
        ctx.scratch[NODE_ID] = planned
        ids = [item.evidence_id for item in planned.campaigns]
        if not ids:
            return []
        rows = await ctx.db.execute(sa.select(Evidence).where(Evidence.id.in_(ids)))
        return list(rows.scalars().all())

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        planned = ctx.scratch.get(NODE_ID)
        if not isinstance(planned, _Planned):  # pragma: no cover — gather always sets it
            raise NodeContractError("4.4.4 reasoned without its gathered plan")
        if planned.why is not None:
            return VideoProduction(status="not_required", why=planned.why)
        production = await _Production.start(ctx)
        for campaign in planned.campaigns:
            await production.campaign(campaign)
        return await production.finish(planned.gaps)


# ---------------------------------------------------------------------------
# what gather hands reason
# ---------------------------------------------------------------------------


@dataclass
class _CampaignPlan:
    concepts: CampaignConcepts
    window: video.DurationWindow
    duration_s: int
    clips: list[PlannedClip]
    evidence_id: uuid.UUID


@dataclass
class _Planned:
    why: str | None = None
    surfaced: int = 0
    campaigns: list[_CampaignPlan] = field(default_factory=list)
    gaps: list[VideoGap] = field(default_factory=list)


@dataclass
class _Submitted:
    """One clip job, and where its result goes once the wait is over."""

    video: _Video
    clip: PlannedClip
    ratio: str
    label: str
    job: GenerationJob


@dataclass
class _Video:
    """One campaign's video while its clips are in flight."""

    plan: _CampaignPlan
    concept_id: str
    asset: CreativeAsset
    script_asset: CreativeAsset
    script: VideoScript
    script_lint: ScriptLint
    ratio_plan: dict[str, dict[str, str]]
    #: The ratios clips are made at, in the order they are made.
    order: list[str]
    clips: list[VideoClip] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)
    renditions: list[VideoRendition] = field(default_factory=list)


# ---------------------------------------------------------------------------
# the production
# ---------------------------------------------------------------------------


@dataclass
class _Production:
    ctx: RunContext
    choice: MediaModelChoice
    capability: CapabilityRecord
    media: MediaJobs
    brief: CreativeBrief
    gaps: list[VideoGap] = field(default_factory=list)
    submitted: list[_Submitted] = field(default_factory=list)
    videos: list[_Video] = field(default_factory=list)
    blocked: bool = False

    @classmethod
    async def start(cls, ctx: RunContext) -> _Production:
        choice = _video_choice(ctx.require_creative().input)
        return cls(
            ctx=ctx,
            choice=choice,
            capability=CapabilityRecord.model_validate(choice.capability),
            media=ctx.require_media(),
            brief=await approved_brief(ctx),
        )

    # -- one campaign --------------------------------------------------------

    async def campaign(self, plan: _CampaignPlan) -> None:
        """Script, then every clip submitted. When nothing can be made for the
        campaign the reason is a gap, and nothing is asked or spent."""
        ref = plan.concepts.campaign_ref
        if self.blocked:
            self.gaps.append(
                _gap(
                    ref,
                    "blocked_by_budget",
                    "Not attempted: the media cap refused an earlier clip, and it would refuse "
                    "these too.",
                )
            )
            return
        ratio_plan, generate = self._ratio_plan(plan)
        if not generate:
            return
        concept = plan.concepts.concepts[0]
        scripted = await self._script(plan, concept.id)
        if scripted is None:
            return
        script_asset, script, lint = scripted
        asset = await self._video_asset(plan, concept.id, script_asset)
        made = _Video(
            plan=plan,
            concept_id=concept.id,
            asset=asset,
            script_asset=script_asset,
            script=script,
            script_lint=lint,
            ratio_plan=ratio_plan,
            order=generate,
        )
        self.videos.append(made)
        await self._submit(made, concept)

    def _ratio_plan(self, plan: _CampaignPlan) -> tuple[dict[str, dict[str, str]], list[str]]:
        """Which required ratios get clips, which are cropped from one that does,
        and which are gaps — before a script or a clip is paid for."""
        creative = self.ctx.require_creative()
        constants = creative.constants.media_constants()
        specs = creative.linter.ruleset.asset_specs.model_dump(mode="json").get("specs") or {}
        _, ratios = ratios_for(specs, plan.concepts.campaign_type)
        coverage = ratio_coverage(
            ratios,
            self.capability,
            tolerance=constants.ratio_tolerance,
            min_retained=constants.crop_min_saliency_retained,
        )
        generate = video.generation_order([r for r in ratios if coverage[r] in _PAINTED])
        result: dict[str, dict[str, str]] = {r: {"plan": "native"} for r in generate}
        ref = plan.concepts.campaign_ref
        for ratio in ratios:
            if coverage[ratio] == "gap":
                self.gaps.append(
                    _gap(
                        ref,
                        "ratio_unsupported",
                        f"{self.choice.model_id} paints none of {ratio}, and no ratio it paints "
                        f"keeps {constants.crop_min_saliency_retained:.0%} of a {ratio} frame.",
                        ratio=ratio,
                    )
                )
            elif coverage[ratio] == "crop":
                wanted = parse_ratio(ratio)
                source = max(
                    generate,
                    key=lambda have: best_crop_retention(wanted, [parse_ratio(have)]),
                    default=None,
                )
                if (
                    source is None
                    or best_crop_retention(wanted, [parse_ratio(source)])
                    < constants.crop_min_saliency_retained
                ):
                    self.gaps.append(
                        _gap(
                            ref,
                            "no_source_ratio",
                            f"{ratio} is a crop, but no ratio this video is made at keeps "
                            f"{constants.crop_min_saliency_retained:.0%} of it; the estimate G7 "
                            "approved priced no clip to crop it from.",
                            ratio=ratio,
                        )
                    )
                else:
                    result[ratio] = {"plan": "crop", "from": source}
        return result, generate

    async def _script(
        self, plan: _CampaignPlan, concept_id: str
    ) -> tuple[CreativeAsset, VideoScript, ScriptLint] | None:
        """The committed script — read back when a previous attempt wrote it."""
        ref = plan.concepts.campaign_ref
        existing = await _find_asset(self.ctx, ref, CreativeAssetKind.VIDEO_SCRIPT)
        if existing is not None and existing.status is CreativeAssetStatus.LINTED:
            script = VideoScript.model_validate((existing.fields or {})["script"])
            if script.duration_s == plan.duration_s and len(script.beats) == len(plan.clips):
                return existing, script, _script_lint(LintResult.model_validate(existing.lint))
        draft_model = video.script_draft_model(len(plan.clips))
        findings: list[str] = []
        for attempt in (1, 2):
            draft = await self.ctx.complete(
                draft_model,
                system=SYSTEM,
                user=self._script_prompt(plan, concept_id, findings),
            )
            script = video.assemble_script(
                draft, clips=[c.model_dump() for c in plan.clips], duration_s=plan.duration_s
            )
            result = self._lint(script, plan)
            if result.verdict in PASSING:
                row = await self._commit_script(plan, concept_id, script, result, existing)
                return row, script, _script_lint(result)
            findings = [f"{f.target_ref.split(':', 1)[-1]}: {f.message}" for f in result.findings]
            log.info("video_production.script_failed_lint", campaign=ref, attempt=attempt)
        self.gaps.append(
            _gap(
                ref,
                "script_failed_lint",
                "The script failed the pinned rules twice; no clip was paid for. "
                + "; ".join(findings[:10]),
            )
        )
        return None

    def _script_prompt(self, plan: _CampaignPlan, concept_id: str, findings: list[str]) -> str:
        brief = self.brief
        concept = next(c for c in plan.concepts.concepts if c.id == concept_id)
        rules = brief.non_negotiables
        lines = [
            f"CAMPAIGN: {plan.concepts.campaign_ref} ({plan.concepts.campaign_type})",
            f"OBJECTIVE: {brief.objective.text}",
            f"ANGLE: {concept.angle_text}",
            f"AUDIENCE: {'; '.join(line.text for line in brief.audience) or '—'}",
            f"CONCEPT: {concept.name} — {concept.subject}, {concept.setting}",
            "PROOF POINTS (the only claims you may make): "
            + ("; ".join(point.normalized_text for point in brief.proof_points) or "none"),
            f"VOICE WORDS: {', '.join(rules.voice_words) or '—'}",
            f"NEVER TERMS: {', '.join(rules.never_terms) or '—'}",
            f"REQUIRED TERMS: {', '.join(rules.required_terms) or '—'}",
            f"VIDEO: {plan.duration_s} s in {len(plan.clips)} shots —",
            *(
                f"  shot {clip.index + 1}: {clip.t0}–{clip.t1} s ({clip.duration_s} s)"
                for clip in plan.clips
            ),
        ]
        if findings:
            lines.append("THE LAST SCRIPT FAILED THESE RULES — rewrite so none fails:")
            lines.extend(f"  - {item}" for item in findings)
        return "\n".join(lines)

    def _lint(self, script: VideoScript, plan: _CampaignPlan) -> LintResult:
        """Every line a viewer hears or reads, alone, at creation (Law 33)."""
        creative = self.ctx.require_creative()
        market, language = _market(self.ctx, plan.concepts.campaign_ref)
        now = datetime.now(UTC)
        results = [
            creative.linter.lint_candidate(
                LintTarget(
                    ref=f"{plan.concepts.campaign_ref}:{ref}",
                    surface=SCRIPT_SURFACE,
                    campaign_type=plan.concepts.campaign_type,
                    market=market,
                    language=language,
                    text=text,
                    generated_by_ai=True,
                ),
                now=now,
            )
            for ref, text in video.script_texts(script)
        ]
        return video.merge_lint(results)

    async def _commit_script(
        self,
        plan: _CampaignPlan,
        concept_id: str,
        script: VideoScript,
        result: LintResult,
        existing: CreativeAsset | None,
    ) -> CreativeAsset:
        """Committed on its own session, before any clip is submitted: a
        resumed attempt must find exactly this script (Law 37)."""
        creative = self.ctx.require_creative()
        text = video.render_script(script)
        fields = {"script": script.model_dump(mode="json"), "concept_id": concept_id}
        values: dict[str, Any] = {
            "text": text,
            "fields": fields,
            "status": CreativeAssetStatus.LINTED,
            "lint": result.model_dump(mode="json"),
            "ruleset_version": creative.linter.pin,
            "content_hash": content_hash(
                kind=CreativeAssetKind.VIDEO_SCRIPT,
                surface=SCRIPT_SURFACE,
                text=text,
                fields=fields,
                claim_ids=[],
            ),
        }
        async with get_sessionmaker()() as session:
            if existing is not None:
                row = await session.get(CreativeAsset, existing.id)
                assert row is not None  # noqa: S101 — found above
                for name, value in values.items():
                    setattr(row, name, value)
            else:
                row = self._row(plan, CreativeAssetKind.VIDEO_SCRIPT, SCRIPT_SURFACE, **values)
                session.add(row)
            await session.commit()
            asset_id = row.id
        return await self._reload(asset_id)

    async def _video_asset(
        self, plan: _CampaignPlan, concept_id: str, script_asset: CreativeAsset
    ) -> CreativeAsset:
        """The asset every clip job references — committed before the first one."""
        existing = await _find_asset(self.ctx, plan.concepts.campaign_ref, CreativeAssetKind.VIDEO)
        if existing is not None:
            return existing
        row = self._row(
            plan,
            CreativeAssetKind.VIDEO,
            VIDEO_SURFACE,
            text=None,
            fields={
                "concept_id": concept_id,
                "script_asset_id": str(script_asset.id),
                "duration_s": plan.duration_s,
            },
            status=CreativeAssetStatus.DRAFT,
            content_hash=script_asset.content_hash,
        )
        async with get_sessionmaker()() as session:
            session.add(row)
            await session.commit()
            asset_id = row.id
        return await self._reload(asset_id)

    def _row(
        self, plan: _CampaignPlan, kind: CreativeAssetKind, surface: str, **values: Any
    ) -> CreativeAsset:
        run = self.ctx.run
        return CreativeAsset(
            workspace_id=run.workspace_id,
            project_id=run.project_id,
            creative_run_id=run.id,
            node_id=NODE_ID,
            campaign_ref=plan.concepts.campaign_ref,
            kind=kind,
            surface=surface,
            claim_ids=[],
            generated_by_ai=True,
            lineage=generated_lineage(NODE_ID),
            **values,
        )

    async def _reload(self, asset_id: uuid.UUID) -> CreativeAsset:
        asset = await self.ctx.db.get(CreativeAsset, asset_id)
        if asset is None:  # pragma: no cover — committed just above
            raise NodeContractError(f"asset {asset_id} vanished after its commit")
        return asset

    # -- the clips -----------------------------------------------------------

    async def _submit(self, made: _Video, concept: Concept) -> None:
        creative = self.ctx.require_creative()
        constants = creative.constants.media_constants()
        fields = video.request_fields(
            self.choice.defaults, self.capability, constants.generate_audio_default
        )
        count = len(made.plan.clips)
        for ratio in made.order:
            label_ratio = supported_label(
                ratio, self.capability, tolerance=constants.ratio_tolerance
            )
            for clip, beat in zip(made.plan.clips, made.script.beats, strict=True):
                if self.blocked:
                    return
                request = VideoRequest(
                    model=self.choice.model_id,
                    prompt=video.clip_prompt(
                        concept,
                        beat,
                        ratio=ratio,
                        index=clip.index,
                        count=count,
                        forbidden_subjects=self.brief.visual_constraints.forbidden_subjects,
                    ),
                    duration=clip.duration_s,
                    aspect_ratio=label_ratio or ratio,
                    **fields,
                )
                params = request.model_dump(
                    mode="json", exclude_none=True, exclude={"model", "prompt"}
                )
                estimate = video_job_price(self.capability, params, constants).usd
                job = await self.media.submit_or_resume(
                    run_id=self.ctx.run.id,
                    node_id=NODE_ID,
                    asset_id=made.asset.id,
                    round=1,
                    request=request,
                    choice=self.choice,
                    estimate_usd=estimate,
                    ledger=self.ctx.ledger,
                )
                label = (
                    f"Video {made.plan.concepts.campaign_ref} {ratio} clip "
                    f"{clip.index + 1}/{count} ({clip.duration_s} s)"
                )
                if job.status is GenerationStatus.BLOCKED_BY_BUDGET:
                    self.blocked = True
                    made.degraded.append("video_square" if parse_ratio(ratio) == 1.0 else "video")
                    made.clips.append(_clip(clip, ratio, job, "blocked_by_budget"))
                    self.gaps.append(
                        _gap(
                            made.plan.concepts.campaign_ref,
                            "blocked_by_budget",
                            f"The media cap refused {label} (≈ ${estimate:.2f}) before it was "
                            "sent; no later clip was submitted.",
                            ratio=ratio,
                            clip_index=clip.index,
                            job_id=job.id,
                        )
                    )
                    return
                await self.ctx.progress(f"Submitted {label}: {job.status.value}")
                self.submitted.append(
                    _Submitted(video=made, clip=clip, ratio=ratio, label=label, job=job)
                )

    async def _wait(self) -> dict[uuid.UUID, GenerationJob]:
        """Every submitted clip, polled at once. One failure stops the rest —
        they resume on the next attempt, from their rows, without a POST."""
        done: dict[uuid.UUID, GenerationJob] = {}

        async def one(item: _Submitted) -> None:
            done[item.job.id] = await self.media.await_video(
                item.job.id, progress=self._progress(item.label)
            )

        try:
            async with asyncio.TaskGroup() as group:
                for item in self.submitted:
                    if item.job.openrouter_job_id is not None:
                        group.create_task(one(item))
                    else:
                        done[item.job.id] = item.job
        except BaseExceptionGroup as grouped:
            raise grouped.exceptions[0] from None
        return done

    def _progress(self, label: str) -> Callable[[GenerationJob, Poll | None], Awaitable[None]]:
        async def report(row: GenerationJob, poll: Poll | None) -> None:
            state = poll.status if poll is not None else "OpenRouter did not answer"
            await self.ctx.progress(f"{label} — poll {row.polls}: {state}")

        return report

    # -- the end -------------------------------------------------------------

    async def finish(self, planned_gaps: list[VideoGap]) -> VideoProduction:
        """Wait for every clip, record what came back, and name what did not."""
        gaps = [*planned_gaps, *self.gaps]
        done = await self._wait()
        timed_out: list[str] = []
        for item in self.submitted:
            row = done.get(item.job.id, item.job)
            ref = item.video.plan.concepts.campaign_ref
            if row.status is GenerationStatus.COMPLETED:
                media_id = await self._clip_artifact(item, row)
                if media_id is None:
                    item.video.clips.append(_clip(item.clip, item.ratio, row, "undecodable"))
                    gaps.append(
                        _gap(
                            ref,
                            "undecodable_clip",
                            f"{item.label}: job {row.id} completed but its file is not a whole "
                            "video.",
                            ratio=item.ratio,
                            clip_index=item.clip.index,
                            job_id=row.id,
                        )
                    )
                    continue
                item.video.clips.append(_clip(item.clip, item.ratio, row, "completed", media_id))
            elif row.status is GenerationStatus.TIMED_OUT:
                timed_out.append(f"job {row.id} ({item.label})")
            elif row.status is GenerationStatus.UNKNOWN_SUBMIT_STATE:
                item.video.clips.append(_clip(item.clip, item.ratio, row, "unknown_submit_state"))
                gaps.append(
                    _gap(
                        ref,
                        "unknown_submit_state",
                        f"{item.label}: job {row.id} — the worker stopped while it was being "
                        "submitted, so whether OpenRouter has it (and bills it) is unknown. It "
                        "is never re-submitted automatically; a person decides (§18).",
                        ratio=item.ratio,
                        clip_index=item.clip.index,
                        job_id=row.id,
                    )
                )
            elif row.status in _ENDED:
                error = row.error or {}
                item.video.clips.append(_clip(item.clip, item.ratio, row, _ENDED[row.status]))
                gaps.append(
                    _gap(
                        ref,
                        "generation_failed",
                        f"{item.label}: job {row.id} ended {row.status.value}: "
                        f"{error.get('message') or error.get('code') or 'no reason given'}. "
                        "It is not re-submitted; regenerate it to try again.",
                        ratio=item.ratio,
                        clip_index=item.clip.index,
                        job_id=row.id,
                    )
                )
            else:  # pragma: no cover — await_video returns only the states above
                raise NodeContractError(f"{item.label}: job {row.id} is {row.status.value}")
        if timed_out:
            timeout = self.ctx.require_creative().constants.media_constants().video_job_timeout_s
            raise NodeContractError(
                f"{len(timed_out)} clip(s) timed out after {timeout:g} s: "
                + "; ".join(timed_out)
                + ". Each OpenRouter job is still alive — use Check again on it in the Jobs "
                "tab, then retry this node. Nothing is POSTed again."
            )
        if self.videos:
            finishing = await _PostProduction.start(self)
            for made in self.videos:
                gaps.extend(await finishing.video(made))
        return VideoProduction(
            status="produced",
            videos=[self._assembled(made) for made in self.videos],
            gaps=gaps,
        )

    async def _clip_artifact(self, item: _Submitted, row: GenerationJob) -> uuid.UUID | None:
        """The job's file as a `clip` artifact of what ffprobe read back — found
        again, not duplicated, on a resume."""
        storage = self.media.storage
        folder = f"creative/{row.creative_run_id}/media/{item.video.asset.id}"
        stem = f"{row.id}-"
        keys = sorted(
            (
                info.key
                for info in await asyncio.to_thread(lambda: list(storage.iter_objects(folder)))
                if info.key.rsplit("/", 1)[-1].startswith(stem)
            ),
            key=lambda key: int(key.rsplit("-", 1)[-1].split(".", 1)[0]),
        )
        if not keys:
            return None
        key = keys[0]
        existing = await self.ctx.db.scalar(
            sa.select(MediaArtifact).where(
                MediaArtifact.asset_id == item.video.asset.id, MediaArtifact.storage_path == key
            )
        )
        if existing is not None:
            return existing.id
        content = await asyncio.to_thread(storage.get, key)
        try:
            facts = await asyncio.to_thread(probe_video, content)
        except ProbeError as exc:
            log.warning("video_production.undecodable", job_id=str(row.id), error=str(exc))
            return None
        artifact = MediaArtifact(
            workspace_id=item.video.asset.workspace_id,
            asset_id=item.video.asset.id,
            job_id=row.id,
            role=MediaArtifactRole.CLIP,
            storage_path=key,
            media_type=facts.media_type,
            width=facts.width,
            height=facts.height,
            duration_ms=facts.duration_ms,
            bytes=facts.bytes,
            sha256=facts.sha256,
            aspect_ratio=item.ratio,
            derivation=MediaArtifactDerivation.NATIVE,
            probe=facts.as_json(),
        )
        self.ctx.db.add(artifact)
        await self.ctx.db.flush()
        return artifact.id

    def _assembled(self, made: _Video) -> CampaignVideo:
        return CampaignVideo(
            campaign_ref=made.plan.concepts.campaign_ref,
            campaign_type=made.plan.concepts.campaign_type,
            concept_id=made.concept_id,
            asset_id=made.asset.id,
            script_asset_id=made.script_asset.id,
            duration_s=made.plan.duration_s,
            duration_window=DurationWindowOut(
                asset_types=list(made.plan.window.asset_types),
                min_s=made.plan.window.min_s,
                max_s=made.plan.window.max_s,
            ),
            script=made.script,
            script_lint=made.script_lint,
            shot_plan=ShotPlanOut(clips=made.plan.clips, calc_evidence_ids=[made.plan.evidence_id]),
            ratio_plan=made.ratio_plan,
            clips=sorted(made.clips, key=lambda c: (made.order.index(c.ratio), c.index)),
            degraded=made.degraded,
            renditions=made.renditions,
        )


# ---------------------------------------------------------------------------
# post-production (§9.4 video 3–5, §18)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Made:
    """What one ratio's post-production wrote, before anything is stored."""

    prepared: post.Prepared
    assembled: post.Assembled
    facts: VideoFacts
    master: bytes
    stamp: dict[str, Any]
    verification: checks.Verification
    preview: bytes
    preview_facts: VideoFacts
    preview_stamp: dict[str, Any]
    preview_geometry: post.UniformScale
    poster: bytes
    poster_stamp: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _Refused:
    """Why one ratio has no rendition."""

    reason: str
    detail: str
    attempts: list[FfmpegAttempt] = field(default_factory=list)
    verification: VideoVerification | None = None


@dataclass
class _PostProduction:
    production: _Production
    logos: list[still.LogoArt]
    logo_unreadable: list[str]
    templates: tuple[Any, ...]
    knobs: post.Knobs
    colour: dict[str, Any] | None

    @classmethod
    async def start(cls, production: _Production) -> _PostProduction:
        ctx = production.ctx
        creative = ctx.require_creative()
        constants = creative.constants
        identity = creative.input.creative_context.visual_identity
        rules = identity.logo or {}
        logos, unreadable = await registered_logos(ctx, production.media)
        return cls(
            production=production,
            logos=logos,
            logo_unreadable=unreadable,
            templates=masters.logo_templates(creative.linter.ruleset),
            knobs=post.Knobs(
                fps=constants.video.target_fps.value,
                caption_height_pct=constants.video.caption_height_pct.value,
                safe_bottom_pct=constants.video.safe_zone_bottom_pct.value,
                safe_edge_pct=constants.video.safe_zone_edge_pct.value,
                end_card_ms=constants.video.end_card_ms.value,
                loudness_lufs=float(constants.video.loudness_lufs.value),
                ratio_tolerance=constants.media.ratio_tolerance.value,
                logo_width_ratio=constants.logo.width_ratio.value,
                permitted_surfaces=tuple(constants.logo.permitted_surfaces.value),
                clear_space_ratio=positive_float(rules.get("clear_space_ratio")),
                min_width_px=positive_int(rules.get("min_width_px")),
            ),
            colour=post.brand_colour((identity.colour or {}).get("tokens") or []),
        )

    @property
    def ctx(self) -> RunContext:
        return self.production.ctx

    async def video(self, made: _Video) -> list[VideoGap]:
        """Every required ratio of one campaign's video, one at a time."""
        await self._clear(made)
        ref = made.plan.concepts.campaign_ref
        gaps: list[VideoGap] = []
        for ratio, planned in made.ratio_plan.items():
            source = ratio if planned["plan"] == "native" else planned["from"]
            outcome = await self._ratio(made, ratio, source)
            if isinstance(outcome, _Refused):
                gaps.append(
                    _gap(
                        ref,
                        outcome.reason,
                        outcome.detail,
                        ratio=ratio,
                        attempts=outcome.attempts,
                        verification=outcome.verification,
                    )
                )
                log.info(
                    "video_production.rendition_gap",
                    campaign=ref,
                    ratio=ratio,
                    reason=outcome.reason,
                )
                continue
            made.renditions.append(outcome)
        return gaps

    async def _clear(self, made: _Video) -> None:
        """What an earlier attempt of this node post-produced — rows and files
        (flushed here, so a remade row may reuse its id). The clips stay: they
        are paid for, and every rendition is remade from them."""
        rows = (
            await self.ctx.db.execute(
                sa.select(MediaArtifact).where(
                    MediaArtifact.asset_id == made.asset.id,
                    MediaArtifact.role.in_(_POSTPROD_ROLES),
                )
            )
        ).scalars()
        storage = self.production.media.storage
        for row in list(rows):
            await asyncio.to_thread(storage.delete, row.storage_path)
            await self.ctx.db.delete(row)
        await self.ctx.db.flush()

    async def _ratio(self, made: _Video, ratio: str, source: str) -> VideoRendition | _Refused:
        ref = made.plan.concepts.campaign_ref
        wanted = {clip.index for clip in made.plan.clips}
        done = {
            clip.index: clip
            for clip in made.clips
            if clip.ratio == source and clip.status == "completed" and clip.media_id is not None
        }
        missing = sorted(wanted - set(done))
        if missing:
            return _Refused(
                "clips_missing",
                f"{ratio} is made from the {source} clips, and clip(s) "
                f"{', '.join(str(i + 1) for i in missing)} of {len(wanted)} did not complete; "
                "the gaps above say why.",
            )
        rows = {
            row.id: row
            for row in (
                await self.ctx.db.execute(
                    sa.select(MediaArtifact).where(
                        MediaArtifact.id.in_([done[i].media_id for i in sorted(done)])
                    )
                )
            ).scalars()
        }
        ordered = [(made.plan.clips[i], rows[done[i].media_id]) for i in sorted(done)]  # type: ignore[index]
        creative = self.ctx.require_creative()
        specs = creative.linter.ruleset.asset_specs.model_dump(mode="json").get("specs") or {}
        types = [
            spec
            for spec in video.video_specs(specs, made.plan.concepts.campaign_type).values()
            if spec.get("ratio") == ratio
        ]
        min_px = _largest_min_px(types)
        caps = [int(spec["max_bytes"]) for spec in types if spec.get("max_bytes")]
        width, height = post.even_size(
            min(row.width for _, row in ordered),
            min(row.height for _, row in ordered),
            ratio,
            min_px,
            tolerance=self.knobs.ratio_tolerance,
        )
        clip_bytes = sum(row.bytes for _, row in ordered)
        source_px = min(row.width * row.height for _, row in ordered)
        footprint = math.ceil(clip_bytes * max(1.0, width * height / source_px))
        free = await asyncio.to_thread(self.production.media.storage.free_bytes)
        if free is not None and free < 2 * footprint:
            return _Refused(
                "storage_insufficient",
                f"The media volume has {free:,} bytes free; assembling {ratio} needs at least "
                f"{2 * footprint:,} (twice its estimated {footprint:,}-byte footprint). Free "
                "space, then retry this node.",
            )
        await self.ctx.progress(f"Post-production {ref} {ratio}: assembling {width}x{height}")
        expected = checks.Expected(
            width=width,
            height=height,
            fps=self.knobs.fps,
            duration_ms=made.plan.duration_s * 1000 + self.knobs.end_card_ms,
            min_duration_s=made.plan.window.min_s,
            max_duration_s=made.plan.window.max_s,
            max_bytes=min(caps) if caps else None,
        )
        storage = self.production.media.storage
        clips = [
            (clip, await asyncio.to_thread(storage.get, row.storage_path)) for clip, row in ordered
        ]
        result = await asyncio.to_thread(
            _post_produce,
            clips,
            ratio=ratio,
            min_px=min_px,
            script=made.script,
            logos=self.logos,
            templates=self.templates,
            colour=self.colour,
            knobs=self.knobs,
            expected=expected,
            model_id=self.production.choice.model_id,
            brand_within_ms=creative.constants.video.brand_within_ms.value,
            min_similarity=creative.constants.video.caption_ocr_min_similarity.value,
        )
        if isinstance(result, _Refused):
            if result.reason == "verification_failed" and self.logo_unreadable:
                result = _Refused(
                    result.reason,
                    f"{result.detail} (unreadable logos: {'; '.join(self.logo_unreadable)})",
                    result.attempts,
                    result.verification,
                )
            return result
        await self.ctx.progress(
            f"Post-production {ref} {ratio}: verified, brand at "
            f"{result.verification.brand_first_at_ms} ms"
        )
        return await self._store(made, ratio, source, [row for _, row in ordered], result)

    async def _store(
        self,
        made: _Video,
        ratio: str,
        source: str,
        clips: list[MediaArtifact],
        result: _Made,
    ) -> VideoRendition:
        run = self.ctx.run
        storage = self.production.media.storage
        master_id, preview_id, poster_id = (
            _media_id(made.asset.id, ratio, role) for role in _POSTPROD_ROLES
        )
        master_key = f"creative/{run.id}/media/{made.asset.id}/{master_id}.mp4"
        preview_key = f"creative/{run.id}/previews/{master_id}_preview.mp4"
        poster_key = f"creative/{run.id}/previews/{master_id}_poster.jpg"
        await asyncio.to_thread(storage.put, master_key, result.master, content_type="video/mp4")
        await asyncio.to_thread(storage.put, preview_key, result.preview, content_type="video/mp4")
        await asyncio.to_thread(storage.put, poster_key, result.poster, content_type="image/jpeg")
        transform = result.prepared.transform(
            encoder=post.encoder_record(result.assembled.profile), node_id=NODE_ID
        )
        for entry, row in zip(transform["clips"], clips, strict=True):
            entry.pop("path", None)
            entry["media_id"] = str(row.id)
        transform["fonts"] = [list(pair) for pair in result.assembled.fonts]
        transform["loudness"] = (
            result.assembled.loudness.record() if result.assembled.loudness is not None else None
        )
        transform["ffmpeg_failures"] = [a.record() for a in result.assembled.failures]
        facts, derivation = (
            result.facts,
            (MediaArtifactDerivation.NATIVE if source == ratio else MediaArtifactDerivation.CROP),
        )
        common = {
            "workspace_id": run.workspace_id,
            "asset_id": made.asset.id,
            "aspect_ratio": ratio,
        }
        self.ctx.db.add_all(
            [
                MediaArtifact(
                    id=master_id, role=MediaArtifactRole.RENDITION, storage_path=master_key,
                    media_type="video/mp4", width=facts.width, height=facts.height,
                    duration_ms=facts.duration_ms, bytes=facts.bytes, sha256=facts.sha256,
                    derivation=derivation, transform=transform, probe=facts.as_json(),
                    disclosure=result.stamp, **common,
                ),
                MediaArtifact(
                    id=preview_id, role=MediaArtifactRole.PREVIEW, storage_path=preview_key,
                    media_type="video/mp4", width=result.preview_facts.width,
                    height=result.preview_facts.height,
                    duration_ms=result.preview_facts.duration_ms,
                    bytes=result.preview_facts.bytes, sha256=result.preview_facts.sha256,
                    derivation=MediaArtifactDerivation.ENCODED, derived_from=master_id,
                    transform=result.preview_geometry.transform(),
                    probe=result.preview_facts.as_json(), disclosure=result.preview_stamp,
                    **common,
                ),
            ]
        )  # fmt: skip
        await self.ctx.db.flush()  # the poster's FK needs the master row first
        poster = probe_image(result.poster)
        self.ctx.db.add(
            MediaArtifact(
                id=poster_id, role=MediaArtifactRole.POSTER, storage_path=poster_key,
                media_type=poster.media_type, width=poster.width, height=poster.height,
                bytes=poster.bytes, sha256=poster.sha256,
                derivation=MediaArtifactDerivation.ENCODED, derived_from=master_id,
                probe={**poster.as_json(), "t_ms": result.verification.brand_first_at_ms},
                disclosure=result.poster_stamp, **common,
            )
        )  # fmt: skip
        await self.ctx.db.flush()
        verification = _verification_out(result.verification)
        assert verification.brand_first_at_ms is not None  # noqa: S101 — verified above
        return VideoRendition(
            ratio=ratio,
            px=f"{facts.width}x{facts.height}",
            duration_ms=facts.duration_ms,
            derivation="native" if source == ratio else "crop",
            source_ratio=source,
            brand_first_at_ms=verification.brand_first_at_ms,
            captions_burned=bool(made.script.captions),
            caption_ocr_min_similarity=verification.caption_ocr_min_similarity,
            has_audio=result.prepared.assembly.has_source_audio,
            bytes=facts.bytes,
            disclosure=result.stamp,
            media_id=master_id,
            preview_media_id=preview_id,
            poster_media_id=poster_id,
            verification=verification,
            failed_attempts=[FfmpegAttempt(**a.record()) for a in result.assembled.failures],
        )


def _post_produce(
    clips: list[tuple[PlannedClip, bytes]],
    *,
    ratio: str,
    min_px: str | None,
    script: VideoScript,
    logos: list[still.LogoArt],
    templates: tuple[Any, ...],
    colour: dict[str, Any] | None,
    knobs: post.Knobs,
    expected: checks.Expected,
    model_id: str,
    brand_within_ms: int,
    min_similarity: float,
) -> _Made | _Refused:
    """One ratio, start to finish, in a scratch directory: plan → assemble →
    stamp → verify → proxy → poster. Runs in a thread; touches no database."""
    with tempfile.TemporaryDirectory(prefix="s4-video-") as scratch:
        work = Path(scratch)
        files: list[post.ClipFile] = []
        out = work / "master.mp4"
        try:
            for clip, content in clips:
                path = work / f"clip{clip.index}.mp4"
                path.write_bytes(content)
                files.append(post.ClipFile(path, probe_video(content), float(clip.duration_s)))
            prepared = post.prepare(
                files, ratio=ratio, min_px=min_px, script=script, logos=logos, colour=colour,
                surface=LOGO_SURFACE, knobs=knobs, workdir=work / "plan", model_id=model_id,
            )  # fmt: skip
            assembled = post.assemble(prepared.assembly, out)
        except post.AssemblyFailed as exc:
            return _Refused(
                "assembly_failed",
                f"ffmpeg exited non-zero twice (standard, then conservative arguments): exit "
                f"{exc.attempts[-1].exit_code}. The stderr tails are attached.",
                [FfmpegAttempt(**a.record()) for a in exc.attempts],
            )
        except post.FfmpegFailed as exc:
            return _Refused(
                "assembly_failed",
                str(exc),
                [
                    FfmpegAttempt(
                        args="standard", exit_code=exc.exit_code, stderr_tail=exc.stderr_tail
                    )
                ],  # fmt: skip
            )
        except (post.AssemblyError, still.StretchError, ProbeError) as exc:
            return _Refused("assembly_failed", str(exc))
        try:
            stamp = post.stamp(out, composited=True)
        except post.StampError as exc:
            return _Refused("stamp_failed", str(exc))
        placement = prepared.logo.placement
        verification = checks.verify(
            out,
            expected=expected,
            captions=script.captions,
            templates=templates,
            logos=logos,
            logo=(
                checks.LogoWindow(box=placement.box, clear_space_px=placement.clear_space_px)
                if placement is not None
                else None
            ),
            brand_within_ms=brand_within_ms,
            min_similarity=min_similarity,
            caption_band=prepared.layout.caption_band,
        )
        if not verification.passed:
            why = "; ".join(verification.failures)
            if placement is None and prepared.logo.reason:
                why += f" (no logo was placed: {prepared.logo.reason})"
            return _Refused(
                "verification_failed",
                f"The assembled file failed verification, so it does not ship: {why}",
                verification=_verification_out(verification),
            )
        assert verification.facts is not None  # noqa: S101 — passed implies probed
        try:
            preview_path = work / "preview.mp4"
            geometry = post.preview_proxy(
                out, preview_path, width=expected.width, height=expected.height
            )
            preview_stamp = post.stamp(preview_path, composited=True)
            ms = verification.brand_first_at_ms or 0
            poster, poster_stamp = still.stamp(
                post.poster_jpeg(post.frame_at(out, ms / 1000)), composited=True
            )
        except post.FfmpegFailed as exc:
            return _Refused(
                "assembly_failed",
                f"the verified master's proxy or poster could not be made: {exc}",
                [
                    FfmpegAttempt(
                        args="standard", exit_code=exc.exit_code, stderr_tail=exc.stderr_tail
                    )
                ],  # fmt: skip
            )
        except (post.StampError, still.StampError) as exc:
            return _Refused("stamp_failed", str(exc))
        preview = preview_path.read_bytes()
        return _Made(
            prepared=prepared,
            assembled=assembled,
            facts=verification.facts,
            master=out.read_bytes(),
            stamp=stamp,
            verification=verification,
            preview=preview,
            preview_facts=probe_video(preview),
            preview_stamp=preview_stamp,
            preview_geometry=geometry,
            poster=poster,
            poster_stamp=poster_stamp,
        )


def _verification_out(result: checks.Verification) -> VideoVerification:
    return VideoVerification.model_validate(
        {key: value for key, value in result.record().items() if key != "boxes"}
    )


def _media_id(asset_id: uuid.UUID, ratio: str, role: MediaArtifactRole) -> uuid.UUID:
    """One id per (video asset, ratio, role), the same on every attempt: an
    attempt that died after writing a file leaves it at the key the next one
    overwrites (§7.4 keys files by media id), never an orphan beside it."""
    return uuid.uuid5(asset_id, f"{NODE_ID}:{ratio}:{role.value}")


def _largest_min_px(specs: list[dict[str, Any]]) -> str | None:
    sizes = [still.parse_px(str(spec["min_px"])) for spec in specs if spec.get("min_px")]
    if not sizes:
        return None
    return f"{max(w for w, _ in sizes)}x{max(h for _, h in sizes)}"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _video_choice(inp: CreativeInput) -> MediaModelChoice:
    choice = next((c for c in inp.media_models if c.modality == "video"), None)
    if choice is None:
        raise NodeContractError("video is in scope but the input carries no video model")
    return choice


async def _find_asset(
    ctx: RunContext, campaign_ref: str, kind: CreativeAssetKind
) -> CreativeAsset | None:
    row: CreativeAsset | None = await ctx.db.scalar(
        sa.select(CreativeAsset)
        .where(
            CreativeAsset.creative_run_id == ctx.run.id,
            CreativeAsset.node_id == NODE_ID,
            CreativeAsset.campaign_ref == campaign_ref,
            CreativeAsset.kind == kind,
        )
        .execution_options(populate_existing=True)
    )
    return row


def _market(ctx: RunContext, campaign_ref: str) -> tuple[str, str]:
    inp = ctx.require_creative().input
    plan = next(
        (c for c in inp.account_structure.campaigns if (c.campaign_ref or c.name) == campaign_ref),
        None,
    )
    return (getattr(plan, "market", "") or "*"), (getattr(plan, "language", None) or "en")


def _script_lint(result: LintResult) -> ScriptLint:
    return ScriptLint(
        verdict=result.verdict,
        ruleset_version=result.ruleset_version,
        rule_ids=sorted({finding.rule_id for finding in result.findings}),
    )


def _clip(
    clip: PlannedClip,
    ratio: str,
    job: GenerationJob,
    status: str,
    media_id: uuid.UUID | None = None,
) -> VideoClip:
    return VideoClip(
        ratio=ratio,
        index=clip.index,
        t0=clip.t0,
        t1=clip.t1,
        duration_s=clip.duration_s,
        job_id=job.id,
        status=status,
        media_id=media_id,
    )


def _gap(campaign_ref: str, reason: str, detail: str, **where: Any) -> VideoGap:
    return VideoGap(campaign_ref=campaign_ref, reason=reason, detail=detail, **where)


VIDEO_PRODUCTION = VideoProductionNode()
