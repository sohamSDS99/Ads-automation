"""S4-P11 harness: a creative run with video in scope, executed through 4.4.4.

The run is started through the API as S4-P9's is, then its stored
`CreativeInput` is given video scope and one video model, re-hashed so the
executor's tamper check holds. Everything OpenRouter says comes from respx:
chat completions answered from the schema each request carries (S4-P9's
`Provider`), and `/videos` from the recorded S4-P1 responses — one job id per
POST, the clip OpenRouter actually returned for every download. Each POST is
counted, because a second POST of a video is a second bill.

The executor gets a `MediaJobs` with a fake clock (a poll's sleep advances it,
so a 900 s timeout takes milliseconds) and an optional kill point: the kill is a
`BaseException` nothing in the job layer or the node catches, as `kill -9`
catches nothing.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import respx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.config import get_settings
from agent.db.models import Run
from agent.db.session import get_sessionmaker
from agent.media.capability import capability_hash
from agent.media.constants import media_constants
from agent.media.http import MediaApi
from agent.media.images import ImageClient
from agent.media.jobs import MediaJobs
from agent.media.types import CapabilityRecord, Descriptor, PriceLine, VideoCaps
from agent.media.videos import VideoClient
from agent.redis_client import get_redis
from agent.schemas.creative_input import CreativeInput, MediaModelChoice
from agent.storage.backend import get_storage
from tests.integration.conftest import ApiClient
from tests.integration.creative_support import TEXT_ONLY, seed_plan, seed_published, seed_signoff
from tests.integration.runs_support import execute
from tests.integration.s4p9_support import SPEC, Provider, published_rules
from tests.media.openrouter_mock import BASE, VEO, body, video_bytes
from tests.openrouter_fake import FakeOpenRouter

KEY = "sk-or-v1-canary-s4p11-0123456789abcdef"
TEXT = {
    "headline": {"max_chars": 30, "min_count": 3, "max_count": 15, **SPEC},
    "description": {"max_chars": 90, "min_count": 2, "max_count": 4, **SPEC},
}
#: Two video surfaces with a 10 s floor: veo's 4/6/8 s clips cut 10 s as 6 + 4.
PMAX_VIDEO = {
    "performance_max": {
        **TEXT,
        "video_landscape": {"ratio": "16:9", "min_duration_s": 10, **SPEC},
        "video_portrait": {"ratio": "9:16", "min_duration_s": 10, **SPEC},
    }
}


def video_model() -> CapabilityRecord:
    return CapabilityRecord(
        modality="video",
        model_id=VEO,
        params={"generate_audio": Descriptor(kind="boolean"), "seed": Descriptor(kind="boolean")},
        video=VideoCaps(durations=[4, 6, 8], resolutions=["720p"], aspect_ratios=["16:9", "9:16"]),
        pricing=[
            PriceLine(
                billable="output_video",
                unit="second",
                usd=Decimal("0.03"),
                variant="720p",
                audio=False,
                sku="duration_seconds_without_audio_720p",
            )
        ],
        input_modalities=["text"],
    )


def video_choice(record: CapabilityRecord) -> MediaModelChoice:
    return MediaModelChoice(
        modality="video",
        model_id=record.model_id,
        capability=record.model_dump(mode="json"),
        capability_hash=capability_hash(record),
        defaults={"resolution": "720p", "generate_audio": False},
    )


async def start_video_run(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    actor: uuid.UUID,
    *,
    specs: dict[str, Any] | None = None,
    video: bool = True,
    campaign_type: str = "performance_max",
) -> uuid.UUID:
    specs = PMAX_VIDEO if specs is None else specs
    await seed_plan(db, workspace_id, project_id, actor, campaign_type=campaign_type)
    await seed_published(
        db, workspace_id, project_id, actor, asset_specs=specs, extra_rules=published_rules(specs)
    )
    await seed_signoff(db, workspace_id, project_id, actor)
    await db.commit()
    started = await admin.post(
        f"/projects/{project_id}/creative/runs", json={"scope": TEXT_ONLY, "media_models": []}
    )
    assert started.status_code == 202, started.text
    run_id = uuid.UUID(started.json()["run_id"])
    if not video:
        return run_id
    run = (
        await db.execute(
            sa.select(Run).where(Run.id == run_id).execution_options(populate_existing=True)
        )
    ).scalar_one()
    stored = CreativeInput.model_validate(run.creative_input)
    widened = stored.model_copy(
        update={
            "scope": stored.scope.model_copy(update={"video": True, "campaign_refs": ["c-sds-us"]}),
            "media_models": [video_choice(video_model())],
        }
    )
    run.creative_input = widened.model_dump(mode="json", by_alias=True)
    run.input_hash = widened.content_hash()
    await db.commit()
    return run_id


# ---------------------------------------------------------------------------
# the job layer the executor runs on
# ---------------------------------------------------------------------------


class SimulatedKill(BaseException):
    """kill -9 at a checkpoint: nothing catches it."""


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)
        self.slept: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)


def media_jobs(clock: Clock, *, kill_at: str | None = None) -> MediaJobs:
    """`MediaJobs` as the executor builds it, on a fake clock; `kill_at` names
    the checkpoint that kills the "worker" — once."""
    fired: list[str] = []

    def checkpoint(name: str) -> None:
        if name == kill_at and not fired:
            fired.append(name)
            raise SimulatedKill(name)

    api = MediaApi(client=httpx.AsyncClient(), api_key=KEY, base_url=BASE)
    settings = get_settings()
    return MediaJobs(
        sessionmaker=get_sessionmaker(),
        redis=get_redis(),
        images=ImageClient(api, sleep=clock.sleep),
        videos=VideoClient(api),
        storage=get_storage(settings),
        constants=media_constants(),
        clock=clock,
        sleep=clock.sleep,
        checkpoint=checkpoint,
        defaults=settings,
    )


async def run_video(run_id: uuid.UUID, jobs: MediaJobs) -> Any:
    """4.1.1 (G7) → 4.4.1 → 4.4.4, and only them — other phases' real nodes
    are not scripted here and must not decide whether 4.4.4 ran."""
    from agent.nodes.creative.n4_1_1_creative_brief import CREATIVE_BRIEF
    from agent.nodes.creative.n4_4_1_creative_concepts import CREATIVE_CONCEPTS
    from agent.nodes.creative.n4_4_4_video_production import VIDEO_PRODUCTION
    from agent.orchestrator.dag import Dag
    from agent.orchestrator.registry import NodeRegistry

    registry = NodeRegistry.of([CREATIVE_BRIEF, CREATIVE_CONCEPTS, VIDEO_PRODUCTION])
    async with httpx.AsyncClient() as client:
        return await execute(
            run_id,
            FakeOpenRouter(),
            client=client,
            registry=registry,
            dag=Dag.from_registry(registry),
            media=jobs,
        )


# ---------------------------------------------------------------------------
# OpenRouter's /videos, answered from the S4-P1 recordings
# ---------------------------------------------------------------------------


def _with_id(payload: Any, job: str) -> Any:
    recorded_id = body("video_submit.json")["id"]
    return json.loads(json.dumps(payload).replace(recorded_id, job))


class Videos:
    """One job id per POST. `status(job, n)` is the n-th poll's state (1-based)."""

    def __init__(self, status: Callable[[str, int], str] | None = None) -> None:
        self.status = status or (lambda _job, _n: "completed")
        self.posts: list[dict[str, Any]] = []
        self.polls: dict[str, int] = {}
        self.downloads: list[str] = []

    def install(self, router: respx.Router) -> None:
        base = re.escape(BASE)
        router.post(f"{BASE}/videos").mock(side_effect=self._submit)
        router.get(url__regex=rf"^{base}/videos/(?P<job>[^/?]+)/content").mock(
            side_effect=self._content
        )
        router.get(url__regex=rf"^{base}/videos/(?P<job>[^/?]+)$").mock(side_effect=self._poll)

    def _submit(self, request: httpx.Request) -> httpx.Response:
        self.posts.append(json.loads(request.content))
        job = f"gen-vid-s4p11-{len(self.posts)}"
        return httpx.Response(202, json=_with_id(body("video_submit.json"), job))

    def _poll(self, request: httpx.Request, job: str) -> httpx.Response:
        count = self.polls[job] = self.polls.get(job, 0) + 1
        state = self.status(job, count)
        return httpx.Response(200, json=_with_id(body(f"video_poll_{state}.json"), job))

    def _content(self, request: httpx.Request, job: str) -> httpx.Response:
        self.downloads.append(job)
        return httpx.Response(200, content=video_bytes(), headers={"content-type": "video/mp4"})


def providers(router: respx.Router, videos: Videos) -> Provider:
    chat = Provider(images=[])
    chat.install(router)
    videos.install(router)
    return chat


def script_calls(chat: Provider) -> list[dict[str, Any]]:
    """The chat requests that asked for a video script."""
    return [
        call
        for call in chat.chat
        if call["response_format"]["json_schema"]["schema"].get("title") == "VideoScriptDraft"
    ]
