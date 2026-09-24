"""Submit once, resume always (PRD §8.4, §18; Law 37).

Video generation takes 30 s to several minutes, and it is billed per job. The
wait is therefore **re-entrant**: a `GenerationJob` row, keyed by a UNIQUE
`idempotency_key`, is committed *before* any network call, so a resumed
worker finds it and re-polls instead of paying for the video a second time.

    submit_or_resume(asset_id, round, request) -> GenerationJob
      key = sha256(asset_id | round | canonical_json(request) | model_id | capability_hash)
      row = SELECT … WHERE idempotency_key = key FOR UPDATE
      terminal                    -> return it
      openrouter_job_id is set    -> return it (poll it; never re-POST)
      status 'submitting'         -> unknown_submit_state; return it (§18)
      none                        -> INSERT 'queued', committed before any network call
      assert_g7_approved(run); budget.reserve(estimate) or blocked_by_budget
      semaphore(modality); UPDATE 'submitting'; COMMIT
      image: POST, decode, store -> completed, cost = usage.cost
      video: POST -> 202 {id} -> UPDATE openrouter_job_id, 'submitted'

A row caught in `submitting` cannot tell "the process died before the POST"
from "it died after the POST, before the id was saved", so it is surfaced as
`unknown_submit_state` and never guessed at. For an image a second POST is
safe (a failed generation is unbilled), so `check()` sends it under the same
key; for a video it is a human's decision and never automatic.

There is no cross-model fallback anywhere in this module: every request goes
to the model the user chose, and a model that is gone fails the job as
`model_unavailable`.

`checkpoint(name)` is called at the five points the kill-injection suite
crashes at (`queued`, `submitting`, `after_post`, `submitted`, `in_progress`)
and one more (`submitting_committed`). In production it does nothing.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent.db.models import (
    Approval,
    ApprovalStatus,
    CreativeBrief,
    GenerationJob,
    GenerationModality,
    GenerationStatus,
    Project,
    Run,
    Workspace,
)
from agent.llm.ledger import RunLedger
from agent.media import references
from agent.media.budget import MediaBudget, resolve_media_caps, text_spend
from agent.media.capability import CapabilityUnsupported, validate
from agent.media.constants import MediaConstants
from agent.media.http import JobNotFound, ProviderRejected, ProviderUnavailable
from agent.media.images import ImageClient
from agent.media.semaphore import MediaSemaphore
from agent.media.types import (
    CapabilityRecord,
    ImageRequest,
    MediaRequest,
    VideoRequest,
    redacted,
)
from agent.media.videos import Poll, VideoClient
from agent.schemas.creative_input import MediaModelChoice
from agent.storage.backend import StorageBackend

log = structlog.get_logger(__name__)

#: The brief gate (Stage 04 PRD §5.3). `Approval.gate_key` of node 4.1.1.
G7 = "G7"

#: `submit_or_resume` returns these unchanged.
TERMINAL = frozenset(
    {
        GenerationStatus.COMPLETED,
        GenerationStatus.FAILED,
        GenerationStatus.CANCELLED,
        GenerationStatus.EXPIRED,
        GenerationStatus.BLOCKED_BY_BUDGET,
        GenerationStatus.UNKNOWN_SUBMIT_STATE,
    }
)
#: Nothing left to poll.
FINISHED = frozenset(
    {
        GenerationStatus.COMPLETED,
        GenerationStatus.FAILED,
        GenerationStatus.CANCELLED,
        GenerationStatus.EXPIRED,
    }
)
#: A slot is a lease. An image slot covers the whole synchronous generation;
#: a video slot covers the submit POST only — the render happens on
#: OpenRouter's side and is waited on by polling, not by holding a slot.
IMAGE_LEASE_SECONDS = 300
VIDEO_LEASE_SECONDS = 120
SEMAPHORE_WAIT_SECONDS = 600
#: A poll interval grows by this factor from `video_poll_initial_s` to
#: `video_poll_max_s`, and each wait carries up to this much extra jitter.
POLL_GROWTH = 1.5
POLL_JITTER = 0.2

_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/svg+xml": "svg",
    "video/mp4": "mp4",
}

Checkpoint = Callable[[str], None]
Progress = Callable[[GenerationJob, Poll | None], Awaitable[None]]


class MediaNotDeclared(RuntimeError):
    """A node submitted a modality its `NodeSpec.media` does not list (§8.1 item 2)."""

    def __init__(self, node_id: str, modality: str, declared: tuple[str, ...]) -> None:
        super().__init__(
            f"Node {node_id} may submit {list(declared) or 'no media'}, not {modality}."
        )
        self.node_id = node_id
        self.modality = modality


class BriefNotApproved(RuntimeError):
    """No media is spent before G7 (Law 40): the run's brief is unapproved, or
    was edited after approval (`approved_hash != brief_hash`)."""

    def __init__(self, run_id: uuid.UUID, detail: str) -> None:
        super().__init__(detail)
        self.run_id = run_id


def idempotency_key(
    asset_id: uuid.UUID | None,
    round: int,
    request: MediaRequest,
    model_id: str,
    capability_hash: str,
) -> str:
    """PRD §8.4. The request is hashed in its redacted, canonical form — the
    form it is stored in — so references count by sha256, never by bytes."""
    material = "|".join(
        [
            str(asset_id) if asset_id is not None else "none",
            str(round),
            json.dumps(
                redacted(request), sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ),
            model_id,
            capability_hash,
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _noop(_name: str) -> None:
    return None


def checkable(row: GenerationJob, choice: MediaModelChoice | None) -> bool:
    """Whether "Check again" (§16 `POST /generation-jobs/{id}/check`) can do
    anything for this job: the two cases `MediaJobs.check()` acts on.

    A timed-out video re-polls its known `openrouter_job_id`. An image in
    `unknown_submit_state` is POSTed again under the same key, which needs the
    run's own `choice` for that model; its references, stored by hash, are
    re-read and judged again by `media/references.py` (Law 44) when it is
    checked, and one that may no longer go leaves the job as it is. A video in
    `unknown_submit_state` is never re-POSTed (Law 37), so it is not
    checkable — a control that cannot change anything is not offered.
    """
    if row.status == GenerationStatus.TIMED_OUT:
        return row.openrouter_job_id is not None
    if (
        row.status == GenerationStatus.UNKNOWN_SUBMIT_STATE
        and row.modality == GenerationModality.IMAGE
    ):
        return (
            choice is not None
            and choice.model_id == row.model_id
            and choice.capability_hash == row.capability_hash
        )
    return False


class MediaJobs:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        redis: Any,
        images: ImageClient,
        videos: VideoClient,
        storage: StorageBackend,
        constants: MediaConstants,
        clock: Callable[[], datetime] = _utcnow,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        checkpoint: Checkpoint | None = None,
        progress: Progress | None = None,
        defaults: Any = None,
    ) -> None:
        self._sessions = sessionmaker
        self._images = images
        self._videos = videos
        self._storage = storage
        self._constants = constants
        self._clock = clock
        self._sleep = sleep
        self._checkpoint = checkpoint or _noop
        self._progress = progress
        self._defaults = defaults
        self._budget = MediaBudget(redis)
        self._semaphores = {
            "image": MediaSemaphore(
                redis, "image", limit=constants.semaphore_image, lease_seconds=IMAGE_LEASE_SECONDS
            ),
            "video": MediaSemaphore(
                redis, "video", limit=constants.semaphore_video, lease_seconds=VIDEO_LEASE_SECONDS
            ),
        }

    @property
    def storage(self) -> StorageBackend:
        """Where finished media lands — `creative/{run}/media/{asset}/{job}-{i}.{ext}`."""
        return self._storage

    # -- submit ------------------------------------------------------------

    async def submit_or_resume(
        self,
        *,
        run_id: uuid.UUID,
        node_id: str,
        asset_id: uuid.UUID | None,
        round: int,
        request: MediaRequest,
        choice: MediaModelChoice,
        estimate_usd: Decimal,
        ledger: RunLedger | None = None,
    ) -> GenerationJob:
        _assert_declared(node_id, choice.modality)
        capability = _capability(choice)
        # Law 36, before a row exists and before a cent is reserved.
        errors = validate(request, capability)
        if errors:
            raise CapabilityUnsupported(errors)
        key = idempotency_key(asset_id, round, request, choice.model_id, choice.capability_hash)

        async with self._session() as session:
            run = await session.get(Run, run_id)
            if run is None:
                raise LookupError(f"No run {run_id}.")
            await session.execute(
                pg_insert(GenerationJob)
                .values(
                    id=uuid.uuid4(),
                    workspace_id=run.workspace_id,
                    project_id=run.project_id,
                    creative_run_id=run_id,
                    node_id=node_id,
                    asset_id=asset_id,
                    round=round,
                    modality=GenerationModality(choice.modality),
                    model_id=choice.model_id,
                    provider_tag=choice.provider_tag,
                    capability_hash=choice.capability_hash,
                    request=redacted(request),
                    idempotency_key=key,
                    status=GenerationStatus.QUEUED,
                    estimate_usd=estimate_usd,
                )
                .on_conflict_do_nothing(index_elements=["idempotency_key"])
            )
            row = await self._locked(session, key=key)
            if row.status in TERMINAL or row.openrouter_job_id is not None:
                await session.commit()
                return row
            if row.status == GenerationStatus.SUBMITTING:
                row.status = GenerationStatus.UNKNOWN_SUBMIT_STATE
                row.error = {
                    "code": "unknown_submit_state",
                    "message": "The worker stopped while this job was being submitted; whether "
                    "OpenRouter received it is unknown.",
                }
                await session.commit()
                log.warning("media.unknown_submit_state", job_id=str(row.id), modality=row.modality)
                return row
            await session.commit()
        self._checkpoint("queued")
        return await self._submit(row.id, request, capability, ledger)

    async def _submit(
        self,
        job_id: uuid.UUID,
        request: MediaRequest,
        capability: CapabilityRecord,
        ledger: RunLedger | None,
    ) -> GenerationJob:
        row = await self._load(job_id)
        await self.assert_g7_approved(row.creative_run_id)
        caps, text_spent, media_floor = await self._budget_inputs(row.creative_run_id)
        reserved = await self._budget.reserve(
            row.creative_run_id,
            row.modality.value,
            row.estimate_usd,
            job_id=row.id,
            caps=caps,
            text_spent_usd=text_spent,
            media_spent_floor_usd=media_floor,
        )
        if not reserved:
            return await self._finish(
                job_id,
                GenerationStatus.BLOCKED_BY_BUDGET,
                error={
                    "code": "estimate_exceeds_cap",
                    "estimate": str(row.estimate_usd),
                    "max_media_cost_usd": str(caps.max_media_cost_usd),
                    "max_creative_cost_usd": str(caps.max_creative_cost_usd),
                },
            )

        semaphore = self._semaphores[row.modality.value]
        async with semaphore.hold(timeout_seconds=SEMAPHORE_WAIT_SECONDS):
            async with self._session() as session:
                row = await self._locked(session, job_id=job_id)
                if row.status != GenerationStatus.QUEUED:
                    # Another worker got here first; it owns the submit.
                    await session.commit()
                    return row
                row.status = GenerationStatus.SUBMITTING
                row.attempts += 1
                await session.flush()
                self._checkpoint("submitting")
                await session.commit()
            self._checkpoint("submitting_committed")
            if isinstance(request, ImageRequest):
                return await self._generate_image(row, request, capability, ledger)
            return await self._submit_video(row, request, capability)

    async def _generate_image(
        self,
        row: GenerationJob,
        request: ImageRequest,
        capability: CapabilityRecord,
        ledger: RunLedger | None,
    ) -> GenerationJob:
        try:
            result = await self._images.generate(request, capability=capability, ledger=ledger)
        except ProviderRejected as refused:
            await self._budget.release(row.id)
            return await self._finish(row.id, GenerationStatus.FAILED, error=_rejected(refused))
        except ProviderUnavailable as failure:
            # Unbilled on failure (§8.4): nothing to reconcile.
            await self._budget.release(row.id)
            return await self._finish(
                row.id,
                GenerationStatus.FAILED,
                error={"code": "provider_unavailable", "message": str(failure)},
            )
        for index, image in enumerate(result.images):
            await asyncio.to_thread(
                self._storage.put,
                self._key(row, index, image.media_type),
                image.data,
                content_type=image.media_type,
            )
        cost = result.cost_usd if result.cost_usd is not None else row.estimate_usd
        done = await self._finish(
            row.id,
            GenerationStatus.COMPLETED,
            cost_usd=cost,
            completed_at=self._clock(),
        )
        await self._budget.reconcile(row.id, cost)
        return done

    async def _submit_video(
        self, row: GenerationJob, request: VideoRequest, capability: CapabilityRecord
    ) -> GenerationJob:
        try:
            submitted = await self._videos.submit(request, capability=capability)
        except ProviderRejected as refused:
            # A 4xx created no job.
            await self._budget.release(row.id)
            return await self._finish(row.id, GenerationStatus.FAILED, error=_rejected(refused))
        except ProviderUnavailable as failure:
            # A job may exist and may be billed: keep the reservation, and
            # leave the decision to a human (§18). Never re-POSTed.
            return await self._finish(
                row.id,
                GenerationStatus.UNKNOWN_SUBMIT_STATE,
                error={"code": "unknown_submit_state", "message": str(failure)},
            )
        self._checkpoint("after_post")
        now = self._clock()
        async with self._session() as session:
            row = await self._locked(session, job_id=row.id)
            row.openrouter_job_id = submitted.id
            row.status = GenerationStatus.SUBMITTED
            row.submitted_at = now
            row.next_poll_at = now + timedelta(seconds=self._constants.video_poll_initial_s)
            await session.commit()
        self._checkpoint("submitted")
        return row

    # -- wait --------------------------------------------------------------

    async def await_video(
        self, job_id: uuid.UUID, *, since: datetime | None = None
    ) -> GenerationJob:
        """Poll until the job ends or `video_job_timeout_s` passes since
        `since` (default: when it was submitted). Re-entrant: a resumed worker
        calls it again on the same row and carries on polling."""
        row = await self._load(job_id)
        if row.status in FINISHED or row.openrouter_job_id is None:
            return row
        openrouter_id = row.openrouter_job_id
        start = since or row.submitted_at or self._clock()
        deadline = start + timedelta(seconds=self._constants.video_job_timeout_s)
        interval = self._constants.video_poll_initial_s
        while True:
            if self._clock() >= deadline:
                # The OpenRouter job is left alive; `check()` resumes polling.
                return await self._finish(job_id, GenerationStatus.TIMED_OUT)
            try:
                poll: Poll | None = await self._videos.poll(openrouter_id)
            except JobNotFound as missing:
                await self._budget.release(job_id)
                return await self._finish(job_id, GenerationStatus.FAILED, error=_rejected(missing))
            except ProviderUnavailable as failure:
                log.info("media.video_poll_unavailable", job_id=str(job_id), reason=str(failure))
                poll = None
            async with self._session() as session:
                row = await self._locked(session, job_id=job_id)
                row.polls += 1
                row.next_poll_at = self._clock() + timedelta(seconds=interval)
                if poll is not None and poll.status == "in_progress":
                    row.status = GenerationStatus.IN_PROGRESS
                await session.commit()
            if poll is not None and poll.status == "in_progress":
                self._checkpoint("in_progress")
            if poll is not None and poll.terminal:
                return await self._conclude(row, poll)
            if self._progress is not None:
                await self._progress(row, poll)
            await self._sleep(interval * (1 + random.random() * POLL_JITTER))  # noqa: S311
            interval = min(interval * POLL_GROWTH, self._constants.video_poll_max_s)

    async def _conclude(self, row: GenerationJob, poll: Poll) -> GenerationJob:
        if poll.status != "completed":
            done = await self._finish(
                row.id,
                GenerationStatus(poll.status),
                error={"code": poll.status, "message": poll.error},
                cost_usd=poll.cost_usd,
            )
            if poll.cost_usd is not None:
                await self._budget.reconcile(row.id, poll.cost_usd)
            else:
                await self._budget.release(row.id)
            return done
        if poll.outputs == 0:
            await self._budget.release(row.id)
            return await self._finish(
                row.id,
                GenerationStatus.FAILED,
                error={"code": "no_outputs", "message": "The job completed with no video."},
            )
        assert row.openrouter_job_id is not None  # noqa: S101 — checked by the caller
        for index in range(poll.outputs):
            await self._videos.download(
                row.openrouter_job_id,
                index,
                storage=self._storage,
                key=self._key(row, index, "video/mp4"),
            )
        cost = poll.cost_usd if poll.cost_usd is not None else row.estimate_usd
        done = await self._finish(
            row.id, GenerationStatus.COMPLETED, cost_usd=cost, completed_at=self._clock()
        )
        await self._budget.reconcile(row.id, cost)
        return done

    # -- check again -------------------------------------------------------

    async def check(
        self, job_id: uuid.UUID, *, choice: MediaModelChoice | None = None
    ) -> GenerationJob:
        """ "Check again" (§18) on a `timed_out` or `unknown_submit_state` job.

        A timed-out video resumes polling with a fresh window. An image in
        `unknown_submit_state` is POSTed again under the same key — safe,
        because a failed generation is unbilled — using the run's `choice`.
        A video in `unknown_submit_state` is returned as it is: re-POSTing
        one may double-bill, and that is never automatic.
        """
        row = await self._load(job_id)
        if row.status == GenerationStatus.TIMED_OUT and row.openrouter_job_id is not None:
            async with self._session() as session:
                locked = await self._locked(session, job_id=job_id)
                locked.status = GenerationStatus.SUBMITTED
                await session.commit()
            return await self.await_video(job_id, since=self._clock())
        if row.status in (GenerationStatus.SUBMITTED, GenerationStatus.IN_PROGRESS):
            return await self.await_video(job_id)
        if (
            row.status == GenerationStatus.UNKNOWN_SUBMIT_STATE
            and row.modality == GenerationModality.IMAGE
            and choice is not None
        ):
            if choice.capability_hash != row.capability_hash or choice.model_id != row.model_id:
                raise ValueError(
                    f"Job {job_id} was made with {row.model_id} at capability "
                    f"{row.capability_hash[:12]}; the choice given is a different one."
                )
            capability = _capability(choice)
            request = await self._stored_image_request(row, capability)
            if request is None:
                return row
            async with self._session() as session:
                locked = await self._locked(session, job_id=job_id)
                locked.status = GenerationStatus.QUEUED
                locked.error = None
                await session.commit()
            return await self._submit(job_id, request, capability, None)
        return row

    async def _stored_image_request(
        self, row: GenerationJob, capability: CapabilityRecord
    ) -> ImageRequest | None:
        """The image request `row` recorded, with its references' bytes re-read.

        The row holds references by sha256 only (Law 44, §9.1 item 7), so their
        bytes come back through `media/references.py`, which judges every one
        again against live rows: a reference the project no longer allows, that
        was retired, or whose bytes changed is not re-sent, and the job stays in
        `unknown_submit_state` for a human. Same references, same order — so the
        same idempotency key.
        """
        stored = dict(row.request)
        hashes = [str(ref["sha256"]) for ref in stored.pop("input_references", None) or []]
        if not hashes:
            return ImageRequest.model_validate(stored)
        async with self._session() as session:
            project = await session.get(Project, row.project_id)
            try:
                images = await references.reload_by_sha256(
                    session,
                    self._storage,
                    run_id=row.creative_run_id,
                    project_id=row.project_id,
                    allowed=references.references_allowed(project.settings if project else None),
                    capability=capability,
                    sha256s=hashes,
                    max_bytes=self._constants.reference_max_bytes,
                )
            except references.ReferenceRefused as refused:
                log.warning(
                    "media.check_reference_refused", job_id=str(row.id), reason=refused.reason
                )
                return None
        return ImageRequest.model_validate({**stored, "input_references": images})

    # -- G7 ------------------------------------------------------------------

    async def assert_g7_approved(self, run_id: uuid.UUID) -> None:
        """The spend gate (§8.3): G7 `approved`, and `approved_hash == brief_hash`.

        Both halves, because each alone is forgeable by a bug: a hash with no
        decided approval behind it is a brief nobody signed, and an approval
        whose hash no longer matches is a brief edited after it was signed.
        """
        async with self._session() as session:
            brief = await session.scalar(
                sa.select(CreativeBrief).where(CreativeBrief.creative_run_id == run_id)
            )
            approval = (
                await session.get(Approval, brief.approval_id)
                if brief is not None and brief.approval_id is not None
                else None
            )
        if brief is None:
            raise BriefNotApproved(run_id, f"Run {run_id} has no brief yet; G7 comes first.")
        if (
            approval is None
            or approval.run_id != run_id
            or approval.gate_key != G7
            or approval.status is not ApprovalStatus.APPROVED
            or brief.approved_hash is None
        ):
            raise BriefNotApproved(run_id, f"The brief for run {run_id} is not approved (G7).")
        if brief.approved_hash != brief.brief_hash:
            raise BriefNotApproved(
                run_id, f"The brief for run {run_id} changed after G7 approved it."
            )

    # -- internals -----------------------------------------------------------

    async def _budget_inputs(self, run_id: uuid.UUID) -> tuple[Any, Decimal, Decimal]:
        async with self._session() as session:
            run = await session.get(Run, run_id)
            if run is None:
                raise LookupError(f"No run {run_id}.")
            project = await session.get(Project, run.project_id)
            workspace = await session.get(Workspace, run.workspace_id)
            media = await session.scalar(
                sa.select(sa.func.coalesce(sa.func.sum(GenerationJob.cost_usd), 0)).where(
                    GenerationJob.creative_run_id == run_id,
                    GenerationJob.cost_usd.is_not(None),
                )
            )
        media_spent = Decimal(media or 0)
        caps = resolve_media_caps(
            project_settings=project.settings if project else None,
            workspace_settings=workspace.settings if workspace else None,
            defaults=self._defaults or _settings(),
        )
        return caps, text_spend(run.cost_usd, media_spent), media_spent

    async def _finish(
        self, job_id: uuid.UUID, status: GenerationStatus, **fields: Any
    ) -> GenerationJob:
        async with self._session() as session:
            row = await self._locked(session, job_id=job_id)
            row.status = status
            for name, value in fields.items():
                setattr(row, name, value)
            await session.commit()
        log.info(
            "media.job_status",
            job_id=str(job_id),
            status=status.value,
            modality=row.modality.value,
            model=row.model_id,
        )
        return row

    async def _load(self, job_id: uuid.UUID) -> GenerationJob:
        async with self._session() as session:
            row = await session.get(GenerationJob, job_id)
        if row is None:
            raise LookupError(f"No generation job {job_id}.")
        return row

    @staticmethod
    async def _locked(
        session: AsyncSession, *, key: str | None = None, job_id: uuid.UUID | None = None
    ) -> GenerationJob:
        query = sa.select(GenerationJob).with_for_update()
        if key is not None:
            query = query.where(GenerationJob.idempotency_key == key)
        else:
            query = query.where(GenerationJob.id == job_id)
        row = await session.scalar(query.execution_options(populate_existing=True))
        if row is None:
            raise LookupError(f"No generation job {key or job_id}.")
        return row

    def _key(self, row: GenerationJob, index: int, media_type: str | None) -> str:
        """`creative/{run}/media/{asset}/{job}-{i}.{ext}` (PRD §7.4). A job
        with no asset yet files under `unassigned`."""
        asset = str(row.asset_id) if row.asset_id is not None else "unassigned"
        extension = _EXTENSIONS.get(media_type or "", "bin")
        return f"creative/{row.creative_run_id}/media/{asset}/{row.id}-{index}.{extension}"

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[AsyncSession]:
        async with self._sessions() as session:
            yield session


def _assert_declared(node_id: str, modality: str) -> None:
    """A submit outside the node's `NodeSpec.media` raises (§8.1 item 2).

    Read from the registry rather than passed in, so a caller cannot widen its
    own permission; an unregistered node declares nothing and may submit
    nothing.
    """
    from agent.orchestrator.registry import RegistryError, get_registry

    try:
        declared = get_registry().spec(node_id).media
    except RegistryError:
        declared = ()
    if modality not in declared:
        raise MediaNotDeclared(node_id, modality, tuple(declared))


def _capability(choice: MediaModelChoice) -> CapabilityRecord:
    return CapabilityRecord.model_validate(choice.capability)


def _rejected(refused: ProviderRejected) -> dict[str, Any]:
    return {"code": refused.code, "status": refused.status, "body": refused.body}


def _settings() -> Any:
    from agent.config import get_settings

    return get_settings()
