"""S4-P11 — 4.4.4 `video_production` up to downloaded clips (PRD §8.4, §9.4
video 1–2, §11 4.4.4, §18; Laws 36–38). Post-production (S4-P12) is stubbed
here and proven in `test_s4p12_video_postprod.py`.

What is proven here, each against real Postgres and Redis and a respx OpenRouter:

* one clip per shot of the plan, per ratio the model makes — each duration one
  of the model's `supported_durations` — polled to completion and downloaded in
  the worker, with a `MediaArtifact(role=clip)` of the facts ffprobe read back;
* `kill -9` while polling: the resumed node re-polls, never re-POSTs, and never
  asks for a new script (a new script is new prompts, new keys, a second bill);
* a `timed_out` clip completes and downloads on Check again, and the node then
  finishes without a second POST;
* `unknown_submit_state` goes to a human: a gap naming the job, never a re-POST;
* `not_required` with video off or no video surface; `spec_missing` when the
  video spec carries no duration; a budget that runs out mid-video is a gap.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
import respx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import (
    CreativeAsset,
    CreativeAssetKind,
    CreativeAssetStatus,
    Evidence,
    GenerationJob,
    GenerationStatus,
    MediaArtifact,
    MediaArtifactRole,
    NodeRun,
    NodeRunStatus,
    Project,
    RunStatus,
)
from agent.storage.local import LocalStorage
from agent.worker import check_generation_job
from tests.integration.conftest import ApiClient
from tests.integration.s4p9_support import SPEC, approve_g7, output_of
from tests.integration.s4p11_support import (
    TEXT,
    Clock,
    SimulatedKill,
    Videos,
    media_jobs,
    providers,
    run_video,
    script_calls,
    start_video_run,
)
from tests.integration.test_run_stream import frames
from tests.media.openrouter_mock import video_bytes

pytestmark = pytest.mark.usefixtures("storage")


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> LocalStorage:
    from agent.config import get_settings

    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    get_settings.cache_clear()
    return LocalStorage(str(tmp_path))


@pytest.fixture
def ids(workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any) -> tuple[Any, ...]:
    return workspace_id, project_id, admin_user.id


@pytest.fixture(autouse=True)
def _clips_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests prove 4.4.4 up to downloaded clips. The post-production the
    node runs after them (S4-P12: ffmpeg, OCR, a registered logo to verify) is
    proven in `test_s4p12_video_postprod.py`; here it makes nothing."""
    from agent.nodes.creative import n4_4_4_video_production as node

    async def nothing(self: Any, made: Any) -> list[Any]:
        return []

    monkeypatch.setattr(node._PostProduction, "video", nothing)


async def _halted_then_approved(
    admin: ApiClient, db: AsyncSession, ids: tuple[Any, ...], clock: Clock, **start: Any
) -> uuid.UUID:
    run_id = await start_video_run(admin, db, *ids, **start)
    await run_video(run_id, media_jobs(clock))  # halts on G7
    await approve_g7(admin, db, run_id)
    return run_id


async def _jobs(db: AsyncSession, run_id: uuid.UUID) -> list[GenerationJob]:
    rows = await db.execute(
        sa.select(GenerationJob)
        .where(GenerationJob.creative_run_id == run_id)
        .order_by(GenerationJob.created_at)
        .execution_options(populate_existing=True)
    )
    return list(rows.scalars().all())


async def _clips(db: AsyncSession, run_id: uuid.UUID) -> list[MediaArtifact]:
    rows = await db.execute(
        sa.select(MediaArtifact)
        .join(CreativeAsset, CreativeAsset.id == MediaArtifact.asset_id)
        .where(
            CreativeAsset.creative_run_id == run_id, MediaArtifact.role == MediaArtifactRole.CLIP
        )
        .execution_options(populate_existing=True)
    )
    return list(rows.scalars().all())


async def _node(db: AsyncSession, run_id: uuid.UUID) -> NodeRun:
    row = (
        (
            await db.execute(
                sa.select(NodeRun)
                .where(NodeRun.run_id == run_id, NodeRun.node_id == "4.4.4")
                .order_by(NodeRun.attempt.desc())
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .first()
    )
    assert row is not None
    return row


# ---------------------------------------------------------------------------
# the path
# ---------------------------------------------------------------------------


async def test_one_clip_per_shot_per_ratio_is_submitted_polled_and_downloaded(
    admin: ApiClient, db: AsyncSession, ids: tuple[Any, ...]
) -> None:
    clock = Clock()
    videos = Videos(lambda _job, n: ("pending", "in_progress", "completed")[min(n, 3) - 1])
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        chat = providers(router, videos)
        run_id = await _halted_then_approved(admin, db, ids, clock)
        result = await run_video(run_id, media_jobs(clock))

    assert result.status is RunStatus.SUCCEEDED
    output = await output_of(db, run_id, "4.4.4")
    assert output["status"] == "produced"
    assert output["gaps"] == [], output["gaps"]
    [made] = output["videos"]
    assert made["duration_s"] == 10
    assert made["duration_window"] == {
        "asset_types": ["video_landscape", "video_portrait"],
        "min_s": 10,
        "max_s": None,
    }
    assert [c["duration_s"] for c in made["shot_plan"]["clips"]] == [6, 4]
    assert made["ratio_plan"] == {"16:9": {"plan": "native"}, "9:16": {"plan": "native"}}
    # Law 36: every clip asks for a duration the model lists, at a ratio it paints.
    assert len(videos.posts) == 4
    assert {post["duration"] for post in videos.posts} == {6, 4}
    assert sorted(post["aspect_ratio"] for post in videos.posts) == ["16:9", "16:9", "9:16", "9:16"]
    assert all(
        post["resolution"] == "720p" and post["generate_audio"] is False for post in videos.posts
    )
    for post in videos.posts:
        assert "do not depict the product" in post["prompt"]
        assert "no text, captions, subtitles" in post["prompt"]
    assert [p["prompt"].split(".")[0] for p in videos.posts[:2]] == [
        "Shot 1 of 2 of one continuous video ad",
        "Shot 2 of 2 of one continuous video ad",
    ]

    # Downloaded in the worker, recorded as what ffprobe read back.
    assert sorted(videos.downloads) == sorted(f"gen-vid-s4p11-{n}" for n in range(1, 5))
    clips = await _clips(db, run_id)
    assert len(clips) == 4
    assert {(c.width, c.height, c.duration_ms) for c in clips} == {(1280, 720, 4000)}
    assert {c.media_type for c in clips} == {"video/mp4"}
    assert all(c.probe["codec"] == "h264" and c.job_id is not None for c in clips)
    assert [c["status"] for c in made["clips"]] == ["completed"] * 4
    assert {c["media_id"] for c in made["clips"]} == {str(c.id) for c in clips}

    # The script: timed by the plan, linted at creation, and it works with sound off.
    script = made["script"]
    assert [(b["t0"], b["t1"]) for b in script["beats"]] == [(0, 6), (6, 10)]
    assert all(c["text"] for c in script["captions"])
    script_row = await db.get(CreativeAsset, uuid.UUID(made["script_asset_id"]))
    assert script_row is not None
    assert script_row.kind is CreativeAssetKind.VIDEO_SCRIPT
    assert script_row.status is CreativeAssetStatus.LINTED
    assert script_row.surface == "youtube_script"
    assert len(script_calls(chat)) == 1

    # The shot plan is a derived row this node computed.
    [plan_id] = made["shot_plan"]["calc_evidence_ids"]
    evidence = await db.get(Evidence, uuid.UUID(plan_id))
    assert evidence is not None and evidence.kind == "calc_shot_plan"

    # node.progress on every poll (§8.4).
    raw = (await admin.get(f"/runs/{run_id}/events")).text
    stream = frames(raw)
    progress = [
        e for e in stream if e["event"] == "node.progress" and e["data"].get("node_id") == "4.4.4"
    ]
    polls = sum(videos.polls.values())
    assert len([e for e in progress if "poll" in e["data"]["message"]]) == polls

    # An unsigned URL carries the key's authority: never in an output, an event or a row.
    for job in await _jobs(db, run_id):
        assert "/content" not in json.dumps(job.request) and "/content" not in str(job.error)
    assert "/content" not in json.dumps(output)
    assert "/content" not in raw


async def test_kill_while_polling_resumes_without_a_second_post_or_a_new_script(
    admin: ApiClient, db: AsyncSession, ids: tuple[Any, ...]
) -> None:
    clock = Clock()
    videos = Videos(lambda _job, n: ("pending", "in_progress", "completed")[min(n, 3) - 1])
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        chat = providers(router, videos)
        run_id = await _halted_then_approved(admin, db, ids, clock)
        with pytest.raises(BaseException) as killed:  # noqa: PT011 — any shape the kill takes
            await run_video(run_id, media_jobs(clock, kill_at="in_progress"))
        assert _killed(killed.value)
        posted_before = len(videos.posts)
        assert posted_before == 4, "every clip was submitted before the wait began"
        assert not await _clips(db, run_id)

        # The worker restarts: a fresh MediaJobs and executor, no kill.
        result = await run_video(run_id, media_jobs(clock))

    assert result.status is RunStatus.SUCCEEDED
    assert len(videos.posts) == 4, "a clip was POSTed twice after the kill"
    assert len(script_calls(chat)) == 1, "the resumed node asked for a new script"
    assert len(await _jobs(db, run_id)) == 4
    assert len(await _clips(db, run_id)) == 4
    assert sorted(videos.downloads) == sorted(f"gen-vid-s4p11-{n}" for n in range(1, 5))


def _killed(exc: BaseException) -> bool:
    if isinstance(exc, SimulatedKill):
        return True
    return isinstance(exc, BaseExceptionGroup) and any(_killed(e) for e in exc.exceptions)


async def test_a_timed_out_clip_completes_and_downloads_on_check_again(
    admin: ApiClient, db: AsyncSession, ids: tuple[Any, ...], signed_in_as: Any
) -> None:
    clock = Clock()
    alive = {"done": False}
    videos = Videos(lambda _job, _n: "completed" if alive["done"] else "pending")
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        providers(router, videos)
        run_id = await _halted_then_approved(admin, db, ids, clock)
        result = await run_video(run_id, media_jobs(clock))

        assert result.status is RunStatus.FAILED
        node = await _node(db, run_id)
        assert node.status is NodeRunStatus.FAILED
        assert "timed out" in node.error["message"] and "Check again" in node.error["message"]
        jobs = await _jobs(db, run_id)
        assert {job.status for job in jobs} == {GenerationStatus.TIMED_OUT}
        assert len(videos.posts) == 4
        assert not videos.downloads

        # OpenRouter finishes the renders; an operator presses Check again.
        alive["done"] = True
        operator = await signed_in_as("operator")
        for job in jobs:
            queued = await operator.post(f"/generation-jobs/{job.id}/check")
            assert queued.status_code == 202, queued.text
            assert await check_generation_job({}, str(job.id)) == {
                "job_id": str(job.id),
                "status": "completed",
            }
        assert len(videos.downloads) == 4

        retried = await admin.post(f"/runs/{run_id}/retry-failed")
        assert retried.status_code == 202, retried.text
        result = await run_video(run_id, media_jobs(clock))

    assert result.status is RunStatus.SUCCEEDED
    assert len(videos.posts) == 4, "Check again or the retry re-POSTed a video"
    assert len(videos.downloads) == 4, "a clip was downloaded twice"
    output = await output_of(db, run_id, "4.4.4")
    assert [c["status"] for c in output["videos"][0]["clips"]] == ["completed"] * 4
    stored = await _clips(db, run_id)
    assert len(stored) == 4
    assert all(LocalStorage_bytes(clip) == video_bytes() for clip in stored)


def LocalStorage_bytes(clip: MediaArtifact) -> bytes:  # noqa: N802 — reads like the call it is
    from agent.config import get_settings
    from agent.storage.backend import get_storage

    return get_storage(get_settings()).get(clip.storage_path)


async def test_unknown_submit_state_goes_to_a_human_and_is_never_re_posted(
    admin: ApiClient, db: AsyncSession, ids: tuple[Any, ...], signed_in_as: Any
) -> None:
    clock = Clock()
    videos = Videos()
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        providers(router, videos)
        run_id = await _halted_then_approved(admin, db, ids, clock)
        # Killed after OpenRouter accepted the first POST, before its id was saved.
        with pytest.raises(BaseException) as killed:  # noqa: PT011
            await run_video(run_id, media_jobs(clock, kill_at="after_post"))
        assert _killed(killed.value)
        result = await run_video(run_id, media_jobs(clock))

    assert result.status is RunStatus.SUCCEEDED
    output = await output_of(db, run_id, "4.4.4")
    [made] = output["videos"]
    unknown = [c for c in made["clips"] if c["status"] == "unknown_submit_state"]
    assert len(unknown) == 1
    [gap] = [g for g in output["gaps"] if g["reason"] == "unknown_submit_state"]
    assert unknown[0]["job_id"] in gap["detail"]
    # The first POST reached OpenRouter; it is never sent again — a human decides.
    assert len(videos.posts) == 4
    operator = await signed_in_as("operator")
    refused = await operator.post(f"/generation-jobs/{unknown[0]['job_id']}/check")
    assert refused.status_code == 409, refused.text
    assert len(videos.posts) == 4


async def test_a_failed_clip_is_a_gap_and_is_never_re_posted(
    admin: ApiClient, db: AsyncSession, ids: tuple[Any, ...]
) -> None:
    clock = Clock()
    videos = Videos(lambda job, _n: "failed" if job == "gen-vid-s4p11-2" else "completed")
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        providers(router, videos)
        run_id = await _halted_then_approved(admin, db, ids, clock)
        result = await run_video(run_id, media_jobs(clock))

    assert result.status is RunStatus.SUCCEEDED
    output = await output_of(db, run_id, "4.4.4")
    [gap] = [g for g in output["gaps"] if g["reason"] == "generation_failed"]
    assert gap["ratio"] == "16:9" and gap["clip_index"] == 1
    assert "Content policy violation" in gap["detail"]
    assert len(videos.posts) == 4
    assert len(await _clips(db, run_id)) == 3


# ---------------------------------------------------------------------------
# nothing to make
# ---------------------------------------------------------------------------


async def test_with_video_off_the_node_is_not_required_and_spends_nothing(
    admin: ApiClient, db: AsyncSession, ids: tuple[Any, ...]
) -> None:
    clock = Clock()
    videos = Videos()
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        providers(router, videos)
        run_id = await _halted_then_approved(admin, db, ids, clock, video=False)
        await run_video(run_id, media_jobs(clock))

    output = await output_of(db, run_id, "4.4.4")
    assert output["status"] == "not_required"
    assert "video is off" in output["why"].lower()
    assert not videos.posts


async def test_no_video_surface_in_the_slate_is_not_required(
    admin: ApiClient, db: AsyncSession, ids: tuple[Any, ...]
) -> None:
    clock = Clock()
    videos = Videos()
    no_video = {"performance_max": {**TEXT, "image_landscape": {"ratio": "1.91:1", **SPEC}}}
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        chat = providers(router, videos)
        run_id = await _halted_then_approved(admin, db, ids, clock, specs=no_video)
        await run_video(run_id, media_jobs(clock))

    output = await output_of(db, run_id, "4.4.4")
    assert output["status"] == "not_required"
    assert "No campaign in the slate has a video surface" in output["why"]
    assert not videos.posts and not script_calls(chat)


async def test_a_video_spec_with_no_duration_is_spec_missing_and_nothing_is_asked_or_spent(
    admin: ApiClient, db: AsyncSession, ids: tuple[Any, ...]
) -> None:
    clock = Clock()
    videos = Videos()
    undated = {"performance_max": {**TEXT, "video_landscape": {"ratio": "16:9", **SPEC}}}
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        chat = providers(router, videos)
        run_id = await _halted_then_approved(admin, db, ids, clock, specs=undated)
        result = await run_video(run_id, media_jobs(clock))

    assert result.status is RunStatus.SUCCEEDED
    output = await output_of(db, run_id, "4.4.4")
    assert output["videos"] == []
    [gap] = output["gaps"]
    assert gap["reason"] == "spec_missing"
    assert "asset_specs.performance_max.video_landscape.min_duration_s" in gap["detail"]
    assert not videos.posts and not script_calls(chat)


async def test_a_budget_that_runs_out_mid_video_is_a_gap_and_stops_the_submits(
    admin: ApiClient, db: AsyncSession, ids: tuple[Any, ...]
) -> None:
    clock = Clock()
    videos = Videos()
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        providers(router, videos)
        run_id = await _halted_then_approved(admin, db, ids, clock)
        project = await db.get(Project, ids[1])
        assert project is not None
        # $0.20 of media: the first 6 s clip ($0.18) fits, the 4 s one ($0.12) does not.
        project.settings = {**(project.settings or {}), "max_media_cost_usd": 0.20}
        await db.commit()
        result = await run_video(run_id, media_jobs(clock))

    assert result.status is RunStatus.SUCCEEDED
    output = await output_of(db, run_id, "4.4.4")
    [gap] = [g for g in output["gaps"] if g["reason"] == "blocked_by_budget"]
    assert gap["ratio"] == "16:9"
    assert output["videos"][0]["degraded"] == ["video"]
    assert len(videos.posts) == 1, "a submit went out after the cap refused one"
