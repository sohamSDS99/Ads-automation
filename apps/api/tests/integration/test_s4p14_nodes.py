"""S4-P14 — nodes 4.6.1–4.6.3 through the real executor (Stage 04 PRD §11 4.6.1–4.6.3).

Upstream nodes are represented by what they leave behind: their assets, their
files, and their checkpointed outputs (`NodeRun` rows at `succeeded`, which the
executor treats as done and hands every later node in `ctx.outputs`). The
registry holds the real 4.6.1, 4.6.2 and 4.6.3 and inert stand-ins for the
direct dependencies, so the executor runs exactly the three nodes under test.

* 4.6.1 measures character counts with the linter's counter, renditions with
  Pillow and video with ffprobe — a 31-character headline fails;
* 4.6.2 raises one exception per claim, capped at 20 and ranked by
  occurrences, never one already cleared, and reports what did not fit;
* 4.6.3 is `not_required` when there is nothing to clear, and otherwise parks
  the run on H3 for the named legal owner — once: clearing resumes the run and
  4.6.3 is not asked again.

Runs in the worker image (ffmpeg / ffprobe), like S4-P10.
"""

from __future__ import annotations

import io
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from agent.creative import clearance
from agent.creative.lint_adapter import load as load_linter
from agent.db.models import (
    ClaimRecord,
    ClaimStatus,
    ClaimType,
    CreativeAsset,
    CreativeAssetKind,
    CreativeAssetStatus,
    CreativeException,
    CreativeExceptionKind,
    CreativeExceptionStatus,
    HumanTask,
    MediaArtifact,
    MediaArtifactDerivation,
    MediaArtifactRole,
    NodeRun,
    NodeRunStatus,
    Run,
    RunStatus,
    SignOffMatrix,
)
from agent.guardrails.normalize import normalized_text
from agent.guidelines.constants import load_content_constants
from agent.llm.router import TaskClass
from agent.nodes.creative._stub import stub
from agent.schemas.creative_qa import LegalExceptionClearance, SpecConformance
from agent.schemas.guardrails import LintResult, LintTarget
from agent.storage.local import LocalStorage
from tests.integration.conftest import ApiClient
from tests.integration.creative_support import TEXT_ONLY, seed_plan, seed_published
from tests.integration.runs_support import execute
from tests.integration.s4p14_support import STATEMENT, cast, claim_licence_rule, reauth
from tests.openrouter_fake import FakeOpenRouter

CAMPAIGN = "c-sds-us"
AD_GROUP = "sds software"
REVIEWED = "2026-09-24"
SPECS: dict[str, Any] = {
    "search": {
        "headline": {"max_chars": 30, "max_count": 15, "source": "test", "reviewed_at": REVIEWED},
        "description": {"max_chars": 90, "source": "test", "reviewed_at": REVIEWED},
        "image_landscape": {
            "ratio": "1.91:1",
            "min_px": "600x314",
            "max_bytes": 5_242_880,
            "source": "test",
            "reviewed_at": REVIEWED,
        },
        "video_landscape": {
            "ratio": "16:9",
            "min_px": "640x360",
            "min_duration_s": 1,
            "max_duration_s": 30,
            "source": "test",
            "reviewed_at": REVIEWED,
        },
    }
}
UPSTREAM = ("4.2.4", "4.2.5", "4.3.1", "4.3.2", "4.3.3", "4.4.7", "4.5.2")


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> LocalStorage:
    from agent.config import get_settings

    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    get_settings.cache_clear()
    return LocalStorage(str(tmp_path))


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


async def _start(
    admin: ApiClient, db: AsyncSession, ws: uuid.UUID, project_id: uuid.UUID, actor: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID, Any]:
    """A started text-only run, a pin with Stage 03's claim rule, a legal owner."""
    people = await cast(admin, db)
    await seed_plan(db, ws, project_id, actor, campaign_type="search")
    licence = claim_licence_rule()
    guideline, _ = await seed_published(
        db,
        ws,
        project_id,
        actor,
        extra_rules=(licence,),
        detectors=tuple(load_content_constants().detectors()),
        payload_rules=(licence,),
        asset_specs=SPECS,
    )
    db.add(
        SignOffMatrix(
            workspace_id=ws,
            project_id=project_id,
            brand_owner_id=actor,
            legal_owner_id=people.legal_id,
            performance_owner_id=actor,
            set_by=actor,
        )
    )
    await db.commit()
    started = await admin.post(
        f"/projects/{project_id}/creative/runs", json={"scope": TEXT_ONLY, "media_models": []}
    )
    assert started.status_code == 202, started.text
    return uuid.UUID(started.json()["run_id"]), guideline.id, people


async def _execute(run_id: uuid.UUID) -> Any:
    from agent.nodes.creative.n4_6_1_spec_conformance import SPEC_CONFORMANCE
    from agent.nodes.creative.n4_6_2_editorial_lint_and_exceptions import (
        EDITORIAL_LINT_AND_EXCEPTIONS,
    )
    from agent.nodes.creative.n4_6_3_legal_exception_clearance import LEGAL_EXCEPTION_CLEARANCE
    from agent.orchestrator.dag import Dag
    from agent.orchestrator.registry import NodeRegistry

    inert = [
        stub(node_id, f"upstream_{node_id}", depends_on=(), task_class=TaskClass.CLASSIFY)
        for node_id in UPSTREAM
    ]
    registry = NodeRegistry.of(
        [*inert, SPEC_CONFORMANCE, EDITORIAL_LINT_AND_EXCEPTIONS, LEGAL_EXCEPTION_CLEARANCE]
    )
    async with httpx.AsyncClient() as client:
        return await execute(
            run_id,
            FakeOpenRouter(),
            client=client,
            registry=registry,
            dag=Dag.from_registry(registry),
            max_attempts=1,
        )


async def _output(db: AsyncSession, run_id: uuid.UUID, node_id: str) -> dict[str, Any]:
    db.expire_all()
    row = (
        await db.execute(
            sa.select(NodeRun)
            .where(NodeRun.run_id == run_id, NodeRun.node_id == node_id)
            .order_by(NodeRun.attempt.desc())
            .limit(1)
        )
    ).scalar_one()
    return dict(row.output or {})


async def _upstream(
    db: AsyncSession, run_id: uuid.UUID, node_id: str, output: dict[str, Any]
) -> None:
    db.add(NodeRun(run_id=run_id, node_id=node_id, status=NodeRunStatus.SUCCEEDED, output=output))


def _text(run: Run, **extra: Any) -> CreativeAsset:
    fields: dict[str, Any] = {
        "workspace_id": run.workspace_id,
        "project_id": run.project_id,
        "creative_run_id": run.id,
        "node_id": "4.2.1",
        "campaign_ref": CAMPAIGN,
        "ad_group_ref": AD_GROUP,
        "kind": CreativeAssetKind.HEADLINE,
        "surface": "rsa_headline",
        "variant": "A",
        "text": "SDS software for your team",
        "generated_by_ai": True,
        "status": CreativeAssetStatus.LINTED,
        "lint": LintResult(
            ruleset_version=(run.pins or [{}])[-1]["ruleset_version"],
            verdict="pass",
            findings=(),
            targets_checked=1,
            rules_evaluated=1,
            elapsed_ms=0,
            evaluated_at=datetime.now(UTC),
        ).model_dump(mode="json"),
        "ruleset_version": (run.pins or [{}])[-1]["ruleset_version"],
        "lineage": {"origin": "generated", "node_id": "4.2.1"},
        "content_hash": uuid.uuid4().hex,
    }
    fields.update(extra)
    return CreativeAsset(**fields)


async def _run(db: AsyncSession, run_id: uuid.UUID) -> Run:
    db.expire_all()
    return (await db.execute(sa.select(Run).where(Run.id == run_id))).scalar_one()


# ---------------------------------------------------------------------------
# 4.6.1
# ---------------------------------------------------------------------------


def _jpeg(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (200, 80, 20)).save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


def _mp4(tmp_path: Path) -> bytes:
    out = tmp_path / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=640x360:r=30:d=2",
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
            "-shortest", "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-movflags", "+faststart", str(out),
        ],
        check=True,
    )  # fmt: skip
    return out.read_bytes()


async def test_conformance_measures_text_by_lint_images_by_pillow_and_video_by_ffprobe(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    storage: LocalStorage,
    tmp_path: Path,
) -> None:
    run_id, _, _ = await _start(admin, db, workspace_id, project_id, admin_user.id)
    run = await _run(db, run_id)
    long = _text(run, text="SDS software for every site now")  # 31 characters
    short = _text(run)
    image = _text(
        run,
        node_id="4.4.2",
        ad_group_ref=None,
        variant=None,
        kind=CreativeAssetKind.IMAGE,
        surface="search_image",
        text=None,
        status=CreativeAssetStatus.APPROVED,
    )
    video = _text(
        run,
        node_id="4.4.4",
        ad_group_ref=None,
        variant=None,
        kind=CreativeAssetKind.VIDEO,
        surface="youtube_video",
        text=None,
        status=CreativeAssetStatus.APPROVED,
    )
    dropped = _text(run, text="x" * 40, status=CreativeAssetStatus.DROPPED)
    db.add_all([long, short, image, video, dropped])
    await db.flush()
    long_id, short_id, image_id, video_id, dropped_id = (
        long.id,
        short.id,
        image.id,
        video.id,
        dropped.id,
    )
    rendition_id = uuid.uuid4()
    jpeg = _jpeg(1200, 628)
    clip = _mp4(tmp_path)
    storage.put(f"creative/{run_id}/r/landscape.jpg", jpeg, content_type="image/jpeg")
    storage.put(f"creative/{run_id}/v/landscape.mp4", clip, content_type="video/mp4")
    rendition = MediaArtifact(
        id=rendition_id,
        workspace_id=workspace_id,
        asset_id=image_id,
        role=MediaArtifactRole.RENDITION,
        storage_path=f"creative/{run_id}/r/landscape.jpg",
        media_type="image/jpeg",
        width=1200,
        height=628,
        bytes=len(jpeg),
        sha256="0" * 64,
        aspect_ratio="1.91:1",
        derivation=MediaArtifactDerivation.NATIVE,
    )
    encoded = MediaArtifact(
        workspace_id=workspace_id,
        asset_id=video_id,
        role=MediaArtifactRole.RENDITION,
        storage_path=f"creative/{run_id}/v/landscape.mp4",
        media_type="video/mp4",
        width=640,
        height=360,
        duration_ms=2000,
        bytes=len(clip),
        sha256="0" * 64,
        aspect_ratio="16:9",
        derivation=MediaArtifactDerivation.ENCODED,
    )
    db.add_all([rendition, encoded])
    await db.commit()

    result = await _execute(run_id)

    assert result.status is RunStatus.SUCCEEDED, result.error
    report = SpecConformance.model_validate(await _output(db, run_id, "4.6.1"))
    by_asset: dict[uuid.UUID, list[Any]] = {}
    for check in report.checks:
        by_asset.setdefault(check.asset_id, []).append(check)
    assert dropped_id not in by_asset
    (headline,) = by_asset[long_id]
    assert (headline.constraint, headline.expected, headline.measured) == ("max_chars", 30, 31)
    assert (headline.source, headline.verdict) == ("lint", "fail")
    assert [c.verdict for c in by_asset[short_id]] == ["pass"]
    assert {(c.constraint, c.source, c.verdict) for c in by_asset[image_id]} == {
        ("min_px", "pillow", "pass"),
        ("ratio", "pillow", "pass"),
        ("max_bytes", "pillow", "pass"),
        ("format", "pillow", "pass"),
    }
    assert {c.media_id for c in by_asset[image_id]} == {rendition_id}
    video_checks = {c.constraint: c for c in by_asset[video_id]}
    assert {c.source for c in video_checks.values()} == {"ffprobe"}
    assert set(video_checks) >= {
        "min_px",
        "ratio",
        "min_duration_s",
        "max_duration_s",
        "fps",
        "codec",
        "audio_codec",
        "format",
    }
    assert {c.verdict for c in video_checks.values()} == {"pass"}, video_checks
    assert video_checks["codec"].measured == "h264/high/yuv420p"
    assert report.failed == 1


# ---------------------------------------------------------------------------
# 4.6.2 + 4.6.3
# ---------------------------------------------------------------------------


async def test_nothing_to_clear_ends_h3_not_required_and_opens_no_task(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
) -> None:
    run_id, _, _ = await _start(admin, db, workspace_id, project_id, admin_user.id)
    run = await _run(db, run_id)
    db.add(_text(run))
    await db.commit()

    result = await _execute(run_id)

    assert result.status is RunStatus.SUCCEEDED, result.error
    lint = await _output(db, run_id, "4.6.2")
    assert lint["exceptions"] == [] and lint["not_raised"] == []
    assert [t["verdict"] for t in lint["targets"]] == ["pass"]
    card = LegalExceptionClearance.model_validate(await _output(db, run_id, "4.6.3"))
    assert card.status == "not_required" and card.assignee_id is None
    tasks = (
        (await db.execute(sa.select(HumanTask).where(HumanTask.guideline_run_id == run_id)))
        .scalars()
        .all()
    )
    assert tasks == []


def _clauses(count: int) -> list[str]:
    return [f"The #1 SDS tool for site {n}" for n in range(count)]


async def test_exceptions_are_raised_once_ranked_capped_and_h3_is_asked_once(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
) -> None:
    run_id, guideline_id, people = await _start(admin, db, workspace_id, project_id, admin_user.id)
    run = await _run(db, run_id)
    pin = run.pins[-1]["ruleset_version"]  # type: ignore[index]
    linter = await load_linter(db, workspace_id=workspace_id, pin=pin)

    # A 4.2.1 headline stored as a draft because its claim is unlicensed; the
    # same clause was also withheld twice by 4.2.2 — one claim, 3 sightings.
    top = "The #1 SDS platform for teams"
    draft_lint = linter.lint_candidate(
        LintTarget(
            ref="x",
            surface="rsa_headline",
            campaign_type="search",
            market="*",
            language="en",
            text=top,
            generated_by_ai=True,
        ),
        now=datetime.now(UTC),
    )
    draft = _text(run, text=top, status=CreativeAssetStatus.DRAFT, lint=None)
    carried = _text(
        run,
        node_id="4.2.2",
        kind=CreativeAssetKind.DESCRIPTION,
        surface="rsa_description",
        text="Every SDS current, on every site.",
    )
    db.add_all([draft, carried])
    await db.flush()
    draft_id, carried_id = draft.id, carried.id
    draft.lint = draft_lint.model_copy(
        update={
            "findings": tuple(
                f.model_copy(update={"target_ref": str(draft.id)}) for f in draft_lint.findings
            )
        }
    ).model_dump(mode="json")
    # 21 more withheld claims, one already in the register (Stage 03's to decide).
    registered = "The #1 SDS tool for site 0"
    db.add(
        ClaimRecord(
            workspace_id=workspace_id,
            project_id=project_id,
            first_seen_guideline_id=guideline_id,
            claim_text=registered,
            normalized_text=normalized_text(registered),
            claim_type=ClaimType.SUPERLATIVE,
            status=ClaimStatus.REJECTED,
        )
    )
    candidates = [{"span": top, "occurrences": 2}] + [
        {"span": clause, "occurrences": 1} for clause in _clauses(21)
    ]
    await _upstream(
        db,
        run_id,
        "4.2.2",
        {
            "ad_groups": [
                {
                    "campaign_ref": CAMPAIGN,
                    "ad_group_ref": AD_GROUP,
                    "variant": "A",
                    "exception_candidates": candidates,
                }
            ]
        },
    )
    third_party, cleared_ref = uuid.uuid4(), uuid.uuid4()
    await _upstream(
        db,
        run_id,
        "4.4.1",
        {
            "reference_refusals": {
                str(third_party): "third_party_without_image_right",
                str(cleared_ref): "third_party_without_image_right",
            }
        },
    )
    db.add(
        CreativeException(
            workspace_id=workspace_id,
            project_id=project_id,
            creative_run_id=run_id,
            kind=CreativeExceptionKind.IMAGE_RIGHT,
            subject_text=f"reference {cleared_ref}",
            proposed={
                "basis": "licence on file",
                "flag": "third_party_reference",
                "reference_id": str(cleared_ref),
            },
            status=CreativeExceptionStatus.CLEARED,
            decided_by=people.legal_id,
            decided_at=datetime.now(UTC),
        )
    )
    await db.commit()

    halted = await _execute(run_id)

    assert halted.status is RunStatus.AWAITING_HUMAN_TASK, halted.error
    lint = await _output(db, run_id, "4.6.2")
    raised = lint["exceptions"]
    assert len(raised) == 20
    assert raised[0]["subject"] == top and raised[0]["occurrences"] == 3
    assert raised[0]["asset_ids"] == [str(draft_id)]
    # What ships instead of the withheld copy: the ad group's carried description.
    assert raised[0]["fallback_asset_ids"] == [str(carried_id)]
    assert raised[0]["proposed"]["claim_type"] == "superlative"
    assert raised[0]["proposed"]["market_scope"] == ["*"]
    assert [r["occurrences"] for r in raised] == sorted(
        (r["occurrences"] for r in raised), reverse=True
    )
    subjects = {r["subject"] for r in raised}
    assert registered not in subjects and f"reference {cleared_ref}" not in subjects
    reasons = {(n["subject"], n["reason"]) for n in lint["not_raised"]}
    assert (registered, "in_register") in reasons
    # 21 claims (22 clauses less the registered one) + 1 image right = 22: 2 over the cap.
    assert sum(1 for n in lint["not_raised"] if n["reason"] == "over_cap") == 2
    rows = (
        (
            await db.execute(
                sa.select(CreativeException).where(
                    CreativeException.creative_run_id == run_id,
                    CreativeException.status == CreativeExceptionStatus.OPEN,
                )
            )
        )
        .scalars()
        .all()
    )
    assert {str(r.id) for r in rows} == {r["exception_id"] for r in raised}
    row_ids = {r.id for r in rows}
    row_hash = clearance.register_hash([clearance.view(r) for r in rows])

    card = LegalExceptionClearance.model_validate(await _output(db, run_id, "4.6.3"))
    assert card.status == "required" and card.assignee_id == people.legal_id
    assert set(card.exception_ids) == row_ids
    assert card.set_hash == row_hash
    (task,) = (
        (await db.execute(sa.select(HumanTask).where(HumanTask.guideline_run_id == run_id)))
        .scalars()
        .all()
    )
    assert task.task_key == "H3" and task.assignee_id == people.legal_id

    # The legal owner clears everything: the run resumes and H3 is not asked twice.
    listed = (await people.legal.get(f"/creative-runs/{run_id}/exceptions")).json()
    cleared = await people.legal.post(
        f"/creative-runs/{run_id}/exceptions/clear",
        json={
            "decisions": [
                {"exception_id": r["exception_id"], "decision": "cleared"} for r in raised
            ],
            "statement": STATEMENT,
            "set_hash": listed["set_hash"],
            "reauth_token": await reauth(people.legal, people.legal_password),
        },
    )
    assert cleared.status_code == 200, cleared.text
    assert (await _run(db, run_id)).status is RunStatus.QUEUED

    resumed = await _execute(run_id)

    assert resumed.status is RunStatus.SUCCEEDED, resumed.error
    tasks = (
        (await db.execute(sa.select(HumanTask).where(HumanTask.guideline_run_id == run_id)))
        .scalars()
        .all()
    )
    assert len(tasks) == 1
    attempts = (
        (
            await db.execute(
                sa.select(NodeRun).where(NodeRun.run_id == run_id, NodeRun.node_id == "4.6.3")
            )
        )
        .scalars()
        .all()
    )
    assert len(attempts) == 1 and attempts[0].status is NodeRunStatus.SUCCEEDED
    run = await _run(db, run_id)
    assert run.pins is not None and run.pins[-1]["reason"] == "h3_clearance"
