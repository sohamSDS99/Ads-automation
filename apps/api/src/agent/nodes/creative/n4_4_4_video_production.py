"""4.4.4 `video_production` — script, shot plan and clips, up to downloaded clips
(Stage 04 PRD §11 4.4.4, §9.4 video 1–2, §8.4, §18).

`not_required` when video is off, or when no campaign in the slate has a video
surface under the pinned spec sheet. Otherwise, per campaign that has one:

1. **gather** — the length the spec's duration window allows
   (`video_duration`), cut by `media.shot_plan_v1` into clips of the model's
   `supported_durations` only, and recorded as a `derived` row. No duration in
   the spec is `spec_missing`, never a guess (§9.5, Q6).
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

A clip that failed, was cancelled or expired, or whose submit state is unknown
is a gap: nothing here POSTs a video twice, and `unknown_submit_state` is a
human's decision (§18). A clip that **timed out** is not a gap — its OpenRouter
job is still alive — so the node fails naming it: Check again resumes polling
in the worker, and a retry of this node then finds the clip finished.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
import structlog
from pydantic import BaseModel

from agent.calc.derived import DerivedWriter
from agent.calc.media import shot_plan_v1, video_duration, video_job_price
from agent.creative import video
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
from agent.orchestrator.creative_input import ratios_for
from agent.orchestrator.creative_run import PASSING
from agent.postprod.image import supported_label
from agent.postprod.probe import ProbeError, probe_video
from agent.schemas.creative_brief import CreativeBrief
from agent.schemas.creative_input import CreativeInput, MediaModelChoice
from agent.schemas.creative_media import CampaignConcepts, Concept, CreativeConcepts
from agent.schemas.creative_video import (
    CampaignVideo,
    DurationWindowOut,
    PlannedClip,
    ScriptLint,
    ShotPlanOut,
    VideoClip,
    VideoGap,
    VideoProduction,
    VideoScript,
)
from agent.schemas.guardrails import LintResult, LintTarget

log = structlog.get_logger(__name__)

NODE_ID = "4.4.4"
CONCEPTS_NODE = "4.4.1"
SCRIPT_SURFACE = "youtube_script"
#: §7.1's Stage 03 delta names the surface a video frame is linted as.
VIDEO_SURFACE = "video_frame"

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
                    min_s=window.min_s, max_s=window.max_s, supported=supported
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
                        f"{' s' if window.max_s else ''} can be cut from "
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
        )


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
