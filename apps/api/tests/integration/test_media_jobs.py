"""Law 37 — SUBMIT ONCE — against real Postgres and Redis (PRD §8.4, §18).

A `GenerationJob` row with a UNIQUE idempotency key is committed before any
network call, and a resumed worker re-polls a known `openrouter_job_id`
instead of POSTing again. The kill-injection test crashes the job layer at
five points (§23.1 item 11) and resumes it in a fresh `MediaJobs`, the way a
restarted worker would, and counts the video POSTs the mock received.

A "crash" is `SimulatedCrash`, a `BaseException`: nothing in the job layer
catches it, an open transaction is rolled back exactly as Postgres rolls back
the transaction of a connection that died, and anything committed stays.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import CreativeBrief, GenerationJob, GenerationStatus, RunStage
from agent.db.session import get_sessionmaker
from agent.llm.ledger import RunLedger, Usage
from agent.media.budget import MediaBudget
from agent.media.capability import capability_hash
from agent.media.constants import media_constants
from agent.media.http import MediaApi
from agent.media.images import ImageClient
from agent.media.jobs import BriefNotApproved, MediaJobs, idempotency_key
from agent.media.types import (
    CapabilityRecord,
    Descriptor,
    ImageRequest,
    PriceLine,
    ReferenceImage,
    VideoCaps,
    VideoRequest,
)
from agent.media.videos import VideoClient
from agent.redis_client import get_redis
from agent.schemas.creative_input import MediaModelChoice
from agent.storage.local import LocalStorage
from tests.integration.creative_support import _run
from tests.media.openrouter_mock import (
    BASE,
    FLUX,
    VEO,
    body,
    mock_video_job,
    response,
    video_bytes,
    video_job_id,
)

KEY = "sk-or-v1-canary-jobs-0123456789abcdef"
CONSTANTS = media_constants()


class SimulatedCrash(BaseException):
    """kill -9 at a checkpoint."""


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def veo() -> CapabilityRecord:
    return CapabilityRecord(
        modality="video",
        model_id=VEO,
        params={"generate_audio": Descriptor(kind="boolean"), "seed": Descriptor(kind="boolean")},
        video=VideoCaps(
            durations=[4, 6, 8],
            resolutions=["720p", "1080p"],
            aspect_ratios=["16:9", "9:16"],
            frame_images=["first_frame", "last_frame"],
        ),
        pricing=[
            PriceLine(
                billable="output_video",
                unit="second",
                usd=Decimal("0.03"),
                variant="720p",
                audio=False,
                sku="duration_seconds_without_audio_720p",
            ),
        ],
        input_modalities=["text", "image"],
    )


def flux() -> CapabilityRecord:
    return CapabilityRecord(
        modality="image",
        model_id=FLUX,
        provider_tag="black-forest-labs",
        params={
            "aspect_ratio": Descriptor(kind="enum", values=["1:1", "16:9"]),
            "output_format": Descriptor(kind="enum", values=["png", "jpeg"]),
            "n": Descriptor(kind="range", min=1, max=1),
            "input_references": Descriptor(kind="range", min=0, max=4),
        },
        pricing=[PriceLine(billable="output_image", unit="megapixel", usd=Decimal("0.014"))],
        input_modalities=["text", "image"],
    )


def choice(record: CapabilityRecord) -> MediaModelChoice:
    return MediaModelChoice(
        modality=record.modality,
        model_id=record.model_id,
        provider_tag=record.provider_tag,
        capability=record.model_dump(mode="json"),
        capability_hash=capability_hash(record),
    )


VIDEO = VideoRequest(
    model=VEO,
    prompt="slow push-in on a ceramic mug on a wooden desk, steam rising, morning light",
    duration=4,
    resolution="720p",
    aspect_ratio="16:9",
    generate_audio=False,
)
IMAGE = ImageRequest(
    model=FLUX, prompt="a plain ceramic mug", aspect_ratio="16:9", n=1, output_format="jpeg"
)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class World:
    """One test's OpenRouter mock, storage, clock and the runs to write into."""

    def __init__(self, tmp_path: Path, run_id: uuid.UUID) -> None:
        self.run_id = run_id
        self.storage = LocalStorage(str(tmp_path))
        self.clock = Clock()
        self.slept: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.clock.now += timedelta(seconds=seconds)

    def jobs(self, *, crash_at: str | None = None) -> MediaJobs:
        def checkpoint(name: str) -> None:
            if name == crash_at:
                raise SimulatedCrash(name)

        api = MediaApi(client=httpx.AsyncClient(), api_key=KEY, base_url=BASE)
        return MediaJobs(
            sessionmaker=get_sessionmaker(),
            redis=get_redis(),
            images=ImageClient(api, sleep=self.sleep),
            videos=VideoClient(api),
            storage=self.storage,
            constants=CONSTANTS,
            clock=self.clock,
            sleep=self.sleep,
            checkpoint=checkpoint,
        )


async def _creative_run(
    db: AsyncSession, ws: uuid.UUID, project_id: uuid.UUID, actor: Any
) -> uuid.UUID:
    research = _run(ws, project_id, actor, RunStage.RESEARCH)
    db.add(research)
    await db.flush()
    plan_run = _run(ws, project_id, actor, RunStage.PLAN, source_run_id=research.id)
    db.add(plan_run)
    await db.flush()
    creative = _run(ws, project_id, actor, RunStage.CREATIVE, source_run_id=plan_run.id)
    db.add(creative)
    await db.flush()
    return creative.id


async def _approve_brief(
    db: AsyncSession,
    ws: uuid.UUID,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    *,
    approved: bool = True,
) -> None:
    db.add(
        CreativeBrief(
            workspace_id=ws,
            project_id=project_id,
            creative_run_id=run_id,
            schema_version="1.0",
            payload={"objective": "fixture brief"},
            brief_hash="b" * 64,
            approved_hash=("b" * 64) if approved else None,
        )
    )
    await db.commit()


@pytest.fixture
def router() -> Iterator[respx.Router]:
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        yield mock


@pytest.fixture
async def world(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    tmp_path: Path,
) -> World:
    run_id = await _creative_run(db, workspace_id, project_id, admin_user.id)
    await _approve_brief(db, workspace_id, project_id, run_id)
    return World(tmp_path, run_id)


def video_posts(router: respx.Router) -> list[dict[str, Any]]:
    return [
        json.loads(call.request.content)
        for call in router.calls
        if call.request.method == "POST" and call.request.url.path.endswith("/videos")
    ]


async def _job(key: str) -> GenerationJob:
    async with get_sessionmaker()() as session:
        row = await session.scalar(
            sa.select(GenerationJob).where(GenerationJob.idempotency_key == key)
        )
        assert row is not None
        return row


async def submit_video(jobs: MediaJobs, world: World, *, estimate: str = "0.12") -> GenerationJob:
    job = await jobs.submit_or_resume(
        run_id=world.run_id,
        node_id="4.4.4",
        asset_id=None,
        round=1,
        request=VIDEO,
        choice=choice(veo()),
        estimate_usd=Decimal(estimate),
    )
    if job.openrouter_job_id is not None:
        job = await jobs.await_video(job.id)
    return job


# ---------------------------------------------------------------------------
# Law 37: the kill-injection suite
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("point", "final"),
    [
        ("queued", GenerationStatus.COMPLETED),
        ("submitting", GenerationStatus.COMPLETED),
        ("after_post", GenerationStatus.UNKNOWN_SUBMIT_STATE),
        ("submitted", GenerationStatus.COMPLETED),
        ("in_progress", GenerationStatus.COMPLETED),
    ],
)
async def test_video_submit_once_under_crash(
    router: respx.Router, world: World, point: str, final: GenerationStatus
) -> None:
    routes = mock_video_job(
        router,
        "video_poll_pending.json",
        "video_poll_in_progress.json",
        "video_poll_completed.json",
    )

    with pytest.raises(SimulatedCrash):
        await submit_video(world.jobs(crash_at=point), world)
    # The worker restarts: a fresh MediaJobs, the same request, no crash.
    resumed = await submit_video(world.jobs(), world)

    assert routes["submit"].call_count == 1, f"{point}: a video was POSTed twice"
    assert resumed.status == final
    key = idempotency_key(None, 1, VIDEO, VEO, capability_hash(veo()))
    assert (await _job(key)).id == resumed.id
    if final is GenerationStatus.COMPLETED:
        assert resumed.openrouter_job_id == video_job_id()
        assert resumed.cost_usd == Decimal("0.12")
        assert routes["content"].call_count == 1
        assert (
            world.storage.get(f"creative/{world.run_id}/media/unassigned/{resumed.id}-0.mp4")
            == video_bytes()
        )
    else:
        # §18: surfaced to a human, never auto-resubmitted; nothing to poll.
        assert resumed.openrouter_job_id is None
        assert routes["poll"].call_count == 0


async def test_a_crash_after_submitting_committed_before_the_post_never_posts(
    router: respx.Router, world: World
) -> None:
    routes = mock_video_job(router, "video_poll_completed.json")

    with pytest.raises(SimulatedCrash):
        await submit_video(world.jobs(crash_at="submitting_committed"), world)
    resumed = await submit_video(world.jobs(), world)

    # The row cannot tell "died before the POST" from "died after it", so it
    # is unknown_submit_state — and a video is never re-POSTed on a guess.
    assert resumed.status == GenerationStatus.UNKNOWN_SUBMIT_STATE
    assert routes["submit"].call_count == 0


async def test_two_workers_submitting_one_job_at_once_post_once(
    router: respx.Router, world: World
) -> None:
    routes = mock_video_job(router, "video_poll_completed.json")

    first, second = await asyncio.gather(
        world.jobs().submit_or_resume(
            run_id=world.run_id,
            node_id="4.4.4",
            asset_id=None,
            round=1,
            request=VIDEO,
            choice=choice(veo()),
            estimate_usd=Decimal("0.12"),
        ),
        world.jobs().submit_or_resume(
            run_id=world.run_id,
            node_id="4.4.4",
            asset_id=None,
            round=1,
            request=VIDEO,
            choice=choice(veo()),
            estimate_usd=Decimal("0.12"),
        ),
    )

    assert routes["submit"].call_count == 1
    assert first.id == second.id


# ---------------------------------------------------------------------------
# images
# ---------------------------------------------------------------------------


class CountingLedger(RunLedger):
    entries: int = 0

    def record(self, *, usage: Usage, cost: Decimal) -> None:
        self.entries += 1
        super().record(usage=usage, cost=cost)


async def test_image_502_502_200_is_one_ledger_entry_and_one_stored_image(
    router: respx.Router, world: World
) -> None:
    route = router.post(f"{BASE}/images").mock(
        side_effect=[httpx.Response(502), httpx.Response(502), response("image_generate.json")]
    )
    ledger = CountingLedger(cap_usd=Decimal(50))

    job = await world.jobs().submit_or_resume(
        run_id=world.run_id,
        node_id="4.4.2",
        asset_id=None,
        round=1,
        request=IMAGE,
        choice=choice(flux()),
        estimate_usd=Decimal("0.03"),
        ledger=ledger,
    )

    assert route.call_count == 3
    assert job.status == GenerationStatus.COMPLETED and job.cost_usd == Decimal("0.015")
    assert ledger.entries == 1
    stored = list(world.storage.iter_objects(f"creative/{world.run_id}"))
    assert [o.key for o in stored] == [f"creative/{world.run_id}/media/unassigned/{job.id}-0.jpg"]
    state = await MediaBudget(get_redis()).state(world.run_id)
    assert (state.spent_usd, state.reserved_usd) == (Decimal("0.015"), Decimal(0))


async def test_an_image_left_in_unknown_submit_state_is_safe_to_post_again_on_check(
    router: respx.Router, world: World
) -> None:
    route = router.post(f"{BASE}/images").mock(return_value=response("image_generate.json"))
    submit = dict(
        run_id=world.run_id,
        node_id="4.4.2",
        asset_id=None,
        round=1,
        request=IMAGE,
        choice=choice(flux()),
        estimate_usd=Decimal("0.03"),
    )

    with pytest.raises(SimulatedCrash):
        await world.jobs(crash_at="submitting_committed").submit_or_resume(**submit)  # type: ignore[arg-type]
    stuck = await world.jobs().submit_or_resume(**submit)  # type: ignore[arg-type]
    assert stuck.status == GenerationStatus.UNKNOWN_SUBMIT_STATE

    # §18: an image re-POST is safe — a failed generation is unbilled — so
    # "Check again" submits it under the same idempotency key.
    checked = await world.jobs().check(stuck.id, choice=choice(flux()))

    assert checked.status == GenerationStatus.COMPLETED
    assert checked.idempotency_key == stuck.idempotency_key
    assert route.call_count == 1


# ---------------------------------------------------------------------------
# timeout, terminal failures, G7, budget, no fallback
# ---------------------------------------------------------------------------


async def test_a_timed_out_video_completes_and_downloads_on_check_again(
    router: respx.Router, world: World
) -> None:
    routes = mock_video_job(router, "video_poll_pending.json")

    timed_out = await submit_video(world.jobs(), world)

    assert timed_out.status == GenerationStatus.TIMED_OUT
    assert sum(world.slept) >= CONSTANTS.video_job_timeout_s
    assert world.slept[0] >= CONSTANTS.video_poll_initial_s
    assert max(world.slept) <= CONSTANTS.video_poll_max_s * 1.2
    # The OpenRouter job is left alive: nothing but polls went to it.
    assert {c.request.method for c in router.calls if "/videos/" in c.request.url.path} == {"GET"}

    routes["poll"].mock(return_value=response("video_poll_completed.json"))
    checked = await world.jobs().check(timed_out.id)

    assert checked.status == GenerationStatus.COMPLETED
    assert routes["content"].call_count == 1
    assert routes["submit"].call_count == 1


@pytest.mark.parametrize(
    ("state", "message"),
    [
        ("failed", "Content policy violation"),
        ("cancelled", "Job was cancelled"),
        ("expired", "Job exceeded maximum time to live"),
    ],
)
async def test_a_terminal_video_failure_keeps_the_error_verbatim_and_releases_the_budget(
    router: respx.Router, world: World, state: str, message: str
) -> None:
    routes = mock_video_job(router, f"video_poll_{state}.json")

    job = await submit_video(world.jobs(), world)
    again = await submit_video(world.jobs(), world)

    assert job.status == GenerationStatus(state)
    assert job.error == {"code": state, "message": message}
    assert again.status == job.status and routes["submit"].call_count == 1
    assert (await MediaBudget(get_redis()).state(world.run_id)).reserved_usd == Decimal(0)


@pytest.mark.parametrize("approved", [None, False])
async def test_a_submit_before_g7_approval_raises_and_issues_no_http(
    router: respx.Router,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    tmp_path: Path,
    approved: bool | None,
) -> None:
    run_id = await _creative_run(db, workspace_id, project_id, admin_user.id)
    if approved is not None:
        await _approve_brief(db, workspace_id, project_id, run_id, approved=approved)
    else:
        await db.commit()
    world = World(tmp_path, run_id)
    mock_video_job(router, "video_poll_completed.json")
    router.post(f"{BASE}/images").mock(return_value=response("image_generate.json"))

    with pytest.raises(BriefNotApproved):
        await submit_video(world.jobs(), world)
    with pytest.raises(BriefNotApproved):
        await world.jobs().submit_or_resume(
            run_id=run_id,
            node_id="4.4.2",
            asset_id=None,
            round=1,
            request=IMAGE,
            choice=choice(flux()),
            estimate_usd=Decimal("0.03"),
        )

    assert len(router.calls) == 0


async def test_a_reservation_that_breaches_a_cap_blocks_the_job_before_any_http(
    router: respx.Router, world: World
) -> None:
    mock_video_job(router, "video_poll_completed.json")

    job = await submit_video(world.jobs(), world, estimate="40.01")

    assert job.status == GenerationStatus.BLOCKED_BY_BUDGET
    assert job.error is not None and job.error["code"] == "estimate_exceeds_cap"
    assert len(router.calls) == 0


async def test_model_unavailable_never_produces_a_request_to_another_model(
    router: respx.Router, world: World
) -> None:
    router.post(f"{BASE}/videos").mock(return_value=response("video_unknown_model.json"))
    router.post(f"{BASE}/images").mock(return_value=response("image_unknown_model.json"))

    video = await submit_video(world.jobs(), world)
    again = await submit_video(world.jobs(), world)
    image = await world.jobs().submit_or_resume(
        run_id=world.run_id,
        node_id="4.4.2",
        asset_id=None,
        round=1,
        request=IMAGE,
        choice=choice(flux()),
        estimate_usd=Decimal("0.03"),
    )

    assert (video.status, image.status) == (GenerationStatus.FAILED, GenerationStatus.FAILED)
    assert video.error is not None and video.error["code"] == "model_unavailable"
    assert image.error is not None and image.error["code"] == "model_unavailable"
    assert again.id == video.id
    posted = [json.loads(c.request.content)["model"] for c in router.calls]
    assert posted == [VEO, FLUX]  # one request each, and only to the chosen model


# ---------------------------------------------------------------------------
# the canary
# ---------------------------------------------------------------------------


async def test_the_key_the_unsigned_url_and_reference_bytes_appear_in_no_log_and_no_request_row(
    router: respx.Router, world: World, caplog: pytest.LogCaptureFixture
) -> None:
    mock_video_job(router, "video_poll_pending.json", "video_poll_completed.json")
    router.post(f"{BASE}/images").mock(return_value=response("image_generate.json"))
    reference = ReferenceImage(
        sha256="ef" * 32, media_type="image/png", data=b"REFERENCE-CANARY-BYTES"
    )
    caplog.set_level(logging.DEBUG)

    with structlog.testing.capture_logs() as events:
        await submit_video(world.jobs(), world)
        await world.jobs().submit_or_resume(
            run_id=world.run_id,
            node_id="4.4.2",
            asset_id=None,
            round=1,
            request=IMAGE.model_copy(update={"input_references": [reference]}),
            choice=choice(flux()),
            estimate_usd=Decimal("0.03"),
        )

    unsigned = body("video_poll_completed.json")["unsigned_urls"][0]
    canaries = [
        KEY,
        unsigned,
        f"/videos/{video_job_id()}/content",
        base64.b64encode(b"REFERENCE-CANARY-BYTES").decode()[:24],
    ]
    logged = (
        "\n".join(r.getMessage() for r in caplog.records) + "\n" + json.dumps(events, default=str)
    )
    async with get_sessionmaker()() as session:
        rows = (await session.scalars(sa.select(GenerationJob))).all()
    persisted = json.dumps([[r.request, r.error] for r in rows], default=str)

    assert len(rows) == 2
    for canary in canaries:
        assert canary not in logged, canary
        assert canary not in persisted, canary
    # The reference is kept by hash, which is how Law 44 wants it recorded.
    image_row = next(r for r in rows if r.modality.value == "image")
    assert image_row.request["input_references"] == [
        {"sha256": "ef" * 32, "media_type": "image/png"}
    ]
