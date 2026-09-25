"""S4-P12 — 4.4.4 post-production: the clips a run downloaded become finished,
verified 16:9 and 9:16 renditions (Stage 04 PRD §9.4 video 3–5, §11 4.4.4, §18;
Laws 38, 39).

Real Postgres and Redis, a respx OpenRouter answering each clip with the
committed fixture clip of the ratio and length it asked for, and the real
ffmpeg / libass / tesseract / exiftool of the worker image:

* every required ratio ends in a `rendition` (+ 480p `preview` + `poster`)
  whose own bytes say H.264 High, yuv420p, 30 fps, +faststart, an AAC track,
  the brand inside 5 s and captions that OCR at ≥ 0.85;
* a retried node clears what it post-produced and makes it again from the
  same clips — no POST, no duplicate rows;
* verification failing (no registered logo) is a blocking gap, never a file;
* too little free Volume space is a gap before ffmpeg runs;
* ffmpeg failing is retried once with conservative arguments — its stderr
  tail kept on the artifact — and failing twice is a gap with both tails;
* a spec maximum leaves room for the end card.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import (
    CreativeAsset,
    Evidence,
    EvidenceSource,
    GenerationJob,
    MediaArtifact,
    MediaArtifactRole,
    Run,
)
from agent.imaging.precheck import template_from_bytes
from agent.postprod import verify
from agent.postprod import video as post
from agent.postprod.image import COMPOSITE
from agent.postprod.probe import probe_video
from agent.schemas.creative_input import CreativeInput
from agent.storage.local import LocalStorage
from tests.integration.conftest import ApiClient
from tests.integration.creative_support import TEXT_ONLY, seed_plan, seed_published, seed_signoff
from tests.integration.s4p9_support import SPEC, approve_g7, output_of, published_rules
from tests.integration.s4p11_support import (
    TEXT,
    Clock,
    SimulatedKill,
    Videos,
    media_jobs,
    providers,
    run_video,
    video_choice,
    video_model,
)
from tests.postprod import video_support as fixtures

pytestmark = [
    pytest.mark.usefixtures("storage"),
    pytest.mark.skipif(
        any(shutil.which(t) is None for t in ("ffmpeg", "ffprobe", "exiftool", "tesseract"))
        or not Path(post.FONTS_DIR).is_dir(),
        reason="video post-production runs in the worker image",
    ),
]

#: A 10 s floor at the sizes Google publishes for the two orientations' minimums.
SPECS = {
    "performance_max": {
        **TEXT,
        "video_landscape": {"ratio": "16:9", "min_duration_s": 10, "min_px": "1280x720", **SPEC},
        "video_portrait": {"ratio": "9:16", "min_duration_s": 10, "min_px": "720x1280", **SPEC},
    }
}
LOGO_RULES = {"clear_space_ratio": 0.25}
TOKENS = [
    {"name": "sand", "hex": "#f1e9da", "role": "neutral"},
    {"name": "ink", "hex": "#1d3557", "role": "primary"},
]
#: (ratio, seconds) → the committed clip the provider "renders".
CLIPS = {
    ("16:9", 6): fixtures.LANDSCAPE[0],
    ("16:9", 4): fixtures.LANDSCAPE[1],
    ("9:16", 6): fixtures.PORTRAIT[0],
    ("9:16", 4): fixtures.PORTRAIT[1],
}


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> LocalStorage:
    from agent.config import get_settings

    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    get_settings.cache_clear()
    return LocalStorage(str(tmp_path))


@pytest.fixture
def ids(workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any) -> tuple[Any, ...]:
    return workspace_id, project_id, admin_user.id


class FixtureVideos(Videos):
    """Each job answered with the fixture clip of its requested ratio and length."""

    def __init__(self) -> None:
        super().__init__()
        self.asked: dict[str, tuple[str, int]] = {}

    def _submit(self, request: httpx.Request) -> httpx.Response:
        response = super()._submit(request)
        job = f"gen-vid-s4p11-{len(self.posts)}"
        payload = self.posts[-1]
        self.asked[job] = (str(payload["aspect_ratio"]), int(payload["duration"]))
        return response

    def _content(self, request: httpx.Request, job: str) -> httpx.Response:
        self.downloads.append(job)
        content = CLIPS[self.asked[job]].read_bytes()
        return httpx.Response(200, content=content, headers={"content-type": "video/mp4"})


async def _registered_logos(
    db: AsyncSession, storage: LocalStorage, project_id: uuid.UUID
) -> tuple[dict[str, Any], ...]:
    """Both fixture variants as Stage 03 registers loose logo uploads: the file
    on the Volume, a `brand_book_asset` row, and a template built at
    `ocr_working_width_px` with `logo_match_score_min`."""
    templates = []
    for plate, ink, _, label in fixtures.VARIANTS:
        content = fixtures.logo_bytes(plate, ink)
        key = f"brand/{project_id}/{label.replace(' ', '-')}.png"
        storage.put(key, content, content_type="image/png")
        row = Evidence(
            project_id=project_id,
            source=EvidenceSource.WEB,
            kind="brand_book_asset",
            payload={"asset_path": key, "page": None, "width_px": 600, "height_px": 200},
            content_text="brand asset 600x200",
            hash=hashlib.sha256(content).hexdigest(),
            fetched_at=datetime.now(UTC),
        )
        db.add(row)
        await db.flush()
        template = template_from_bytes(
            content,
            asset_id=row.id,
            label=label,
            min_score=fixtures.TEMPLATE_MIN_SCORE,
            working_width=fixtures.TEMPLATE_WORKING_WIDTH,
        )
        templates.append(
            {
                "asset_id": str(template.asset_id),
                "label": template.label,
                "phash": template.phash,
                "min_score": template.min_score,
                "descriptors_b64": template.descriptors_b64,
                "keypoint_count": template.keypoint_count,
            }
        )
    await db.commit()
    return tuple(templates)


async def _start(
    admin: ApiClient,
    db: AsyncSession,
    storage: LocalStorage,
    ids: tuple[Any, ...],
    *,
    specs: dict[str, Any] = SPECS,
    logos: bool = True,
) -> uuid.UUID:
    """A video run as S4-P11's `start_video_run` makes it, with registered
    logos, the brand's logo rules and colour tokens."""
    workspace_id, project_id, actor = ids
    templates = await _registered_logos(db, storage, project_id) if logos else ()
    await seed_plan(db, workspace_id, project_id, actor, campaign_type="performance_max")
    await seed_published(
        db, workspace_id, project_id, actor, asset_specs=specs,
        extra_rules=published_rules(specs), logo_templates=templates, logo_rules=LOGO_RULES,
    )  # fmt: skip
    await seed_signoff(db, workspace_id, project_id, actor)
    await db.commit()
    started = await admin.post(
        f"/projects/{project_id}/creative/runs", json={"scope": TEXT_ONLY, "media_models": []}
    )
    assert started.status_code == 202, started.text
    run_id = uuid.UUID(started.json()["run_id"])
    run = (
        await db.execute(
            sa.select(Run).where(Run.id == run_id).execution_options(populate_existing=True)
        )
    ).scalar_one()
    stored = CreativeInput.model_validate(run.creative_input)
    context = stored.creative_context
    identity = context.visual_identity.model_copy(update={"colour": {"tokens": TOKENS}})
    widened = stored.model_copy(
        update={
            "scope": stored.scope.model_copy(update={"video": True, "campaign_refs": ["c-sds-us"]}),
            "media_models": [video_choice(video_model())],
            "creative_context": context.model_copy(update={"visual_identity": identity}),
        }
    )
    run.creative_input = widened.model_dump(mode="json", by_alias=True)
    run.input_hash = widened.content_hash()
    await db.commit()
    return run_id


async def _produce(
    admin: ApiClient, db: AsyncSession, run_id: uuid.UUID, videos: Videos
) -> dict[str, Any]:
    clock = Clock()
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        providers(router, videos)
        await run_video(run_id, media_jobs(clock))  # halts on G7
        await approve_g7(admin, db, run_id)
        await run_video(run_id, media_jobs(clock))
    return await output_of(db, run_id, "4.4.4")


async def _artifacts(db: AsyncSession, run_id: uuid.UUID, role: MediaArtifactRole) -> list[Any]:
    rows = await db.execute(
        sa.select(MediaArtifact)
        .join(CreativeAsset, CreativeAsset.id == MediaArtifact.asset_id)
        .where(CreativeAsset.creative_run_id == run_id, MediaArtifact.role == role)
        .execution_options(populate_existing=True)
    )
    return list(rows.scalars().all())


def _killed(exc: BaseException) -> bool:
    if isinstance(exc, SimulatedKill):
        return True
    return isinstance(exc, BaseExceptionGroup) and any(_killed(e) for e in exc.exceptions)


def _only_video(out: dict[str, Any]) -> dict[str, Any]:
    assert out["status"] == "produced", out
    (made,) = out["videos"]
    return made


async def test_the_downloaded_clips_become_verified_16x9_and_9x16_renditions(
    admin: ApiClient, db: AsyncSession, storage: LocalStorage, ids: tuple[Any, ...]
) -> None:
    run_id = await _start(admin, db, storage, ids)
    videos = FixtureVideos()
    out = await _produce(admin, db, run_id, videos)
    made = _only_video(out)
    assert [g for g in out["gaps"] if g["reason"] != "blocked_by_budget"] == [], out["gaps"]
    by_ratio = {r["ratio"]: r for r in made["renditions"]}
    assert set(by_ratio) == {"16:9", "9:16"}
    assert by_ratio["16:9"]["px"] == "1280x720" and by_ratio["9:16"]["px"] == "720x1280"
    for rendition in by_ratio.values():
        assert rendition["derivation"] == "native"
        assert rendition["duration_ms"] == 12_000  # 10 s of clips + the 2 s end card
        assert rendition["brand_first_at_ms"] <= 5000
        assert rendition["caption_ocr_min_similarity"] >= 0.85
        assert rendition["captions_burned"] is True
        assert rendition["verification"]["passed"] is True
        assert rendition["verification"]["faststart"] is True
        assert len(rendition["verification"]["logo_frames"]) == 6
        assert rendition["disclosure"]["xmp_digital_source_type"] == COMPOSITE
        assert rendition["failed_attempts"] == []
    # The 16:9 clips carry a tone; the 9:16 clips carry nothing but get a silent AAC track.
    assert by_ratio["16:9"]["has_audio"] is True
    assert by_ratio["9:16"]["has_audio"] is False

    masters = {row.id: row for row in await _artifacts(db, run_id, MediaArtifactRole.RENDITION)}
    previews = await _artifacts(db, run_id, MediaArtifactRole.PREVIEW)
    posters = await _artifacts(db, run_id, MediaArtifactRole.POSTER)
    assert len(masters) == len(previews) == len(posters) == 2
    for rendition in by_ratio.values():
        row = masters[uuid.UUID(rendition["media_id"])]
        content = storage.get(row.storage_path)
        assert row.storage_path == f"creative/{run_id}/media/{row.asset_id}/{row.id}.mp4"
        facts = probe_video(content)
        assert (facts.codec, facts.profile, facts.pix_fmt, facts.fps) == ("h264", "High",
                                                                          "yuv420p", 30.0)  # fmt: skip
        assert facts.has_audio and facts.audio_codec == "aac"
        assert verify.is_faststart(verify.top_level_boxes(content))
        assert row.sha256 == hashlib.sha256(content).hexdigest() == facts.sha256
        assert row.transform is not None and row.transform["sx"] == row.transform["sy"]
        assert row.transform["logo"] is not None and row.transform["ffmpeg_failures"] == []
        assert {c["media_id"] for c in row.transform["clips"]} <= {
            str(a.id) for a in await _artifacts(db, run_id, MediaArtifactRole.CLIP)
        }
        assert row.disclosure == rendition["disclosure"]
    for preview in previews:
        assert preview.derived_from in masters
        assert (
            preview.storage_path == f"creative/{run_id}/previews/{preview.derived_from}_preview.mp4"
        )
        assert {(preview.width, preview.height)} <= {(854, 480), (480, 854)}
        assert preview.transform is not None and preview.transform["sx"] == preview.transform["sy"]
    for poster in posters:
        assert poster.derived_from in masters
        assert storage.get(poster.storage_path)[:3] == b"\xff\xd8\xff"
        assert poster.disclosure is not None


async def test_a_worker_killed_mid_post_production_resumes_without_orphans_or_a_post(
    admin: ApiClient,
    db: AsyncSession,
    storage: LocalStorage,
    ids: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`kill -9` after the 16:9 master is on the Volume and before the node
    commits: every row it flushed is gone, its files are not. The restarted
    worker POSTs nothing, remakes both ratios at the SAME keys, and leaves no
    second copy of any file."""
    from agent.nodes.creative import n4_4_4_video_production as node

    run_id = await _start(admin, db, storage, ids)
    videos = FixtureVideos()
    real = node._PostProduction._store
    killed: list[str] = []

    async def killed_on_the_second(self: Any, made: Any, ratio: str, *args: Any) -> Any:
        if not killed and made.renditions:
            killed.append(ratio)
            raise SimulatedKill("after the first master was written")
        return await real(self, made, ratio, *args)

    monkeypatch.setattr(node._PostProduction, "_store", killed_on_the_second)
    clock = Clock()
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        providers(router, videos)
        await run_video(run_id, media_jobs(clock))
        await approve_g7(admin, db, run_id)
        with pytest.raises(BaseException) as caught:  # noqa: PT011 — any shape the kill takes
            await run_video(run_id, media_jobs(clock))
        assert _killed(caught.value)
        assert killed == ["9:16"]
        assert await _artifacts(db, run_id, MediaArtifactRole.RENDITION) == []  # rolled back
        posts = len(videos.posts)
        # The worker restarts: a fresh MediaJobs and executor, no kill.
        await run_video(run_id, media_jobs(clock))
    made = _only_video(await output_of(db, run_id, "4.4.4"))
    assert len(videos.posts) == posts  # submit once: no clip is POSTed again
    assert {r["ratio"] for r in made["renditions"]} == {"16:9", "9:16"}
    rows = await _artifacts(db, run_id, MediaArtifactRole.RENDITION)
    assert len(rows) == 2
    assert len(await _artifacts(db, run_id, MediaArtifactRole.PREVIEW)) == 2
    assert len(await _artifacts(db, run_id, MediaArtifactRole.POSTER)) == 2
    clips = await _artifacts(db, run_id, MediaArtifactRole.CLIP)
    (asset_id,) = {row.asset_id for row in rows}
    on_disk = sorted(
        info.key for info in storage.iter_objects(f"creative/{run_id}/media/{asset_id}")
    )
    assert on_disk == sorted([*(c.storage_path for c in clips), *(r.storage_path for r in rows)])
    previews = sorted(info.key for info in storage.iter_objects(f"creative/{run_id}/previews"))
    assert len(previews) == 4


async def test_a_retried_node_clears_what_it_post_produced_and_remakes_it(
    admin: ApiClient,
    db: AsyncSession,
    storage: LocalStorage,
    ids: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A node that FAILED after its 16:9 rendition was flushed keeps those rows;
    retry-failed deletes them (and their files) before remaking both."""
    from agent.nodes.creative import n4_4_4_video_production as node

    run_id = await _start(admin, db, storage, ids)
    videos = FixtureVideos()
    real = node._PostProduction._store
    failed: list[str] = []

    async def fails_on_the_second(self: Any, made: Any, ratio: str, *args: Any) -> Any:
        if not failed and made.renditions:
            failed.append(ratio)
            raise RuntimeError("volume went read-only")
        return await real(self, made, ratio, *args)

    monkeypatch.setattr(node._PostProduction, "_store", fails_on_the_second)
    clock = Clock()
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        providers(router, videos)
        await run_video(run_id, media_jobs(clock))
        await approve_g7(admin, db, run_id)
        await run_video(run_id, media_jobs(clock))
        assert failed == ["9:16"]
        posts = len(videos.posts)
        retried = await admin.post(f"/runs/{run_id}/retry-failed")
        assert retried.status_code in (200, 202), retried.text
        await run_video(run_id, media_jobs(clock))
    made = _only_video(await output_of(db, run_id, "4.4.4"))
    assert len(videos.posts) == posts
    assert {r["ratio"] for r in made["renditions"]} == {"16:9", "9:16"}
    assert len(await _artifacts(db, run_id, MediaArtifactRole.RENDITION)) == 2
    assert len(await _artifacts(db, run_id, MediaArtifactRole.PREVIEW)) == 2


async def test_a_video_without_a_registered_logo_fails_verification_and_ships_nothing(
    admin: ApiClient, db: AsyncSession, storage: LocalStorage, ids: tuple[Any, ...]
) -> None:
    run_id = await _start(admin, db, storage, ids, logos=False)
    out = await _produce(admin, db, run_id, FixtureVideos())
    made = _only_video(out)
    assert made["renditions"] == []
    failed = [g for g in out["gaps"] if g["reason"] == "verification_failed"]
    assert {g["ratio"] for g in failed} == {"16:9", "9:16"}
    for gap in failed:
        assert gap["verification"]["passed"] is False
        assert "registers no logo" in gap["detail"]
    assert await _artifacts(db, run_id, MediaArtifactRole.RENDITION) == []


async def test_too_little_free_space_is_a_gap_before_ffmpeg_runs(
    admin: ApiClient,
    db: AsyncSession,
    storage: LocalStorage,
    ids: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = await _start(admin, db, storage, ids)
    ran: list[list[str]] = []
    real = post.run_ffmpeg

    def counting(args: list[str], **kwargs: Any) -> str:
        ran.append(args)
        return real(args, **kwargs)

    monkeypatch.setattr(post, "run_ffmpeg", counting)
    monkeypatch.setattr(LocalStorage, "free_bytes", lambda self: 1_000)
    out = await _produce(admin, db, run_id, FixtureVideos())
    made = _only_video(out)
    assert made["renditions"] == []
    short = [g for g in out["gaps"] if g["reason"] == "storage_insufficient"]
    assert {g["ratio"] for g in short} == {"16:9", "9:16"}
    assert "1,000 bytes free" in short[0]["detail"]
    assert ran == []


async def test_ffmpeg_failing_once_is_retried_conservatively_and_the_tail_is_kept(
    admin: ApiClient,
    db: AsyncSession,
    storage: LocalStorage,
    ids: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = await _start(admin, db, storage, ids)
    real = post.run_ffmpeg
    failed: list[str] = []

    def flaky(args: list[str], *, what: str, **kwargs: Any) -> str:
        if what == "assembly (standard)":
            failed.append(what)
            raise post.FfmpegFailed(-9, "…\nKilled: out of memory at frame 211\n", what)
        return real(args, what=what, **kwargs)

    monkeypatch.setattr(post, "run_ffmpeg", flaky)
    made = _only_video(await _produce(admin, db, run_id, FixtureVideos()))
    assert len(failed) == 2  # one per ratio, each retried
    for rendition in made["renditions"]:
        (attempt,) = rendition["failed_attempts"]
        assert attempt["args"] == "standard" and attempt["exit_code"] == -9
        assert "out of memory at frame 211" in attempt["stderr_tail"]
    for row in await _artifacts(db, run_id, MediaArtifactRole.RENDITION):
        assert row.transform is not None
        assert row.transform["encoder_args"]["args"] == "conservative"
        assert "out of memory" in row.transform["ffmpeg_failures"][0]["stderr_tail"]


async def test_ffmpeg_failing_twice_is_a_gap_with_both_stderr_tails(
    admin: ApiClient,
    db: AsyncSession,
    storage: LocalStorage,
    ids: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = await _start(admin, db, storage, ids)
    real = post.run_ffmpeg

    def broken(args: list[str], *, what: str, **kwargs: Any) -> str:
        if what.startswith("assembly"):
            raise post.FfmpegFailed(1, f"[{what}] Error while filtering: Invalid argument\n", what)
        return real(args, what=what, **kwargs)

    monkeypatch.setattr(post, "run_ffmpeg", broken)
    out = await _produce(admin, db, run_id, FixtureVideos())
    assert _only_video(out)["renditions"] == []
    gaps = [g for g in out["gaps"] if g["reason"] == "assembly_failed"]
    assert {g["ratio"] for g in gaps} == {"16:9", "9:16"}
    for gap in gaps:
        assert [a["args"] for a in gap["attempts"]] == ["standard", "conservative"]
        assert (
            "[assembly (conservative)] Error while filtering" in gap["attempts"][1]["stderr_tail"]
        )
    assert await _artifacts(db, run_id, MediaArtifactRole.RENDITION) == []


async def test_a_spec_maximum_keeps_room_for_the_end_card(
    admin: ApiClient, db: AsyncSession, storage: LocalStorage, ids: tuple[Any, ...]
) -> None:
    """At most 8 s: the script is 6 s (one clip), the file 6 + 2 = 8 s."""
    capped = json.loads(json.dumps(SPECS))
    for asset_type in ("video_landscape", "video_portrait"):
        spec = capped["performance_max"][asset_type]
        spec.pop("min_duration_s")
        spec["max_duration_s"] = 8
    run_id = await _start(admin, db, storage, ids, specs=capped)
    videos = FixtureVideos()
    made = _only_video(await _produce(admin, db, run_id, videos))
    assert made["duration_s"] == 6
    assert {int(p["duration"]) for p in videos.posts} == {6}
    assert {r["duration_ms"] for r in made["renditions"]} == {8000}
    jobs = (
        await db.execute(sa.select(GenerationJob).where(GenerationJob.creative_run_id == run_id))
    ).scalars()
    assert len(list(jobs)) == 2  # one 6 s clip per ratio
