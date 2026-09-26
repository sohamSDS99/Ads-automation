"""S4-P24 — the `kill -9` injection suite, with real kills (Stage 04 PRD §17
CC5, CC12; §18).

Until now every crash in this codebase was simulated: a `BaseException` raised
at a checkpoint, which Python unwinds — `finally` blocks run, context managers
exit, a semaphore slot is handed back. Here the worker is a separate process
and it dies by `SIGKILL`: the parent asserts the child's exit status is
`-SIGKILL`, and resumes in a *fresh* process, the way a restarted container
would.

CC5 "Submit once": the real `MediaJobs` is killed at each of the five
`submit_or_resume` states (plus §18's `submitting_committed`, and after
`completed` is committed); OpenRouter is a real HTTP server in this process
that counts every POST, so the count outlives the worker that sent it.

CC12 "Resume": a creative run's executor is killed mid-DAG, mid-regeneration
after G8, and mid-poll; an api (uvicorn) is killed right after it committed a
G8 decision. After each, no node that had succeeded runs again, no decided G8
item is asked again, and an in-flight video is polled, never re-POSTed.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
import respx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import (
    Approval,
    ApprovalStatus,
    AssetDecision,
    CreativeAsset,
    GenerationJob,
    GenerationStatus,
    Run,
    RunStage,
    RunStatus,
)
from agent.db.session import get_sessionmaker
from agent.media.budget import MediaBudget
from agent.orchestrator.dag import Dag
from agent.orchestrator.state import RunLock
from agent.redis_client import get_redis
from agent.scheduling.reaper import QUEUED_GRACE_SECONDS, reap_stale_runs
from agent.schemas.creative_input import CreativeInput
from agent.storage.local import LocalStorage
from tests.integration.conftest import ApiClient
from tests.integration.runs_support import execute
from tests.integration.s4p9_support import approve_g7, image_model, start_image_run
from tests.integration.s4p11_support import (
    Clock,
    Videos,
    media_jobs,
    providers,
    run_video,
    start_video_run,
)
from tests.integration.s4p14_support import past_h3
from tests.integration.s4p24_support import (
    KILLED,
    Children,
    MockOpenRouter,
    Outcome,
    asked,
    assert_untouched,
    free_port,
    node_runs,
    poll_until,
    recover_after_worker_kill,
    run_status,
    succeeded,
)
from tests.integration.test_media_jobs import IMAGE, _approve_brief, _creative_run
from tests.integration.test_s4p4_brief_g7 import _g7
from tests.integration.test_s4p6_descriptions_variant_b import _registry, _seed
from tests.integration.test_s4p8_extras import (
    CRAWLED,
    DISQUALIFIERS,
    EXTRA_SPECS,
    FRESH,
    OFFER_SPECS,
    REQUIRED,
    _crawl,
    _offers,
    _Script,
)
from tests.integration.test_s4p10_image_renditions import SEARCH, _Painter
from tests.integration.test_s4p13_asset_review import TICKED, _gate, _item
from tests.integration.test_s4p13_asset_review import _execute as media_chain
from tests.integration.test_s4p16_package import GOLDEN_SCOPE
from tests.media.openrouter_mock import video_bytes

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def children(tmp_path: Path) -> AsyncIterator[Children]:
    started = Children(tmp_path)
    yield started
    await started.reap()


@pytest.fixture
def openrouter() -> Iterator[MockOpenRouter]:
    with MockOpenRouter() as server:
        yield server


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> LocalStorage:
    """One storage directory for this process and every child (they inherit it)."""
    from agent.config import get_settings

    monkeypatch.setenv("STORAGE_DIR", str(tmp_path / "volume"))
    get_settings.cache_clear()
    return LocalStorage(str(tmp_path / "volume"))


# ---------------------------------------------------------------------------
# CC5 — submit once, under a real kill
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def spend_run(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> uuid.UUID:
    """A creative run whose brief G7 approved: the job layer may spend on it."""
    run_id = await _creative_run(db, workspace_id, project_id, admin_user.id)
    await _approve_brief(db, workspace_id, project_id, run_id)
    return run_id


async def _only_job(run_id: uuid.UUID) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    sa.select(GenerationJob).where(GenerationJob.creative_run_id == run_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1, f"one idempotency key, one row: {[r.id for r in rows]}"
        row = rows[0]
        return {
            "id": row.id,
            "status": row.status,
            "key": row.idempotency_key,
            "openrouter_job_id": row.openrouter_job_id,
            "attempts": row.attempts,
            "polls": row.polls,
            "cost_usd": row.cost_usd,
            "request": row.request,
        }


def _media_args(
    kind: str, run_id: uuid.UUID, server: MockOpenRouter, tmp_path: Path
) -> tuple[str, ...]:
    return (
        "media",
        "--kind",
        kind,
        "--run",
        str(run_id),
        "--base",
        server.base_url,
        "--storage",
        str(tmp_path / "media"),
    )


async def _budget(run_id: uuid.UUID) -> tuple[Decimal, Decimal]:
    state = await MediaBudget(get_redis()).state(run_id)
    return state.spent_usd, state.reserved_usd


#: (kill point, row status the kill leaves, POSTs before the kill, final status, POSTs in all)
VIDEO_KILLS = [
    ("queued", GenerationStatus.QUEUED, 0, GenerationStatus.COMPLETED, 1),
    # The UPDATE to `submitting` was flushed, not committed: it dies with the
    # connection, the row is still `queued`, and the resume submits it once.
    ("submitting", GenerationStatus.QUEUED, 0, GenerationStatus.COMPLETED, 1),
    # §18: committed `submitting`, then dead — before the POST or after it,
    # nobody can tell. Surfaced, never re-POSTed.
    (
        "submitting_committed",
        GenerationStatus.SUBMITTING,
        0,
        GenerationStatus.UNKNOWN_SUBMIT_STATE,
        0,
    ),  # fmt: skip
    ("after_post", GenerationStatus.SUBMITTING, 1, GenerationStatus.UNKNOWN_SUBMIT_STATE, 1),
    ("submitted", GenerationStatus.SUBMITTED, 1, GenerationStatus.COMPLETED, 1),
    ("in_progress", GenerationStatus.IN_PROGRESS, 1, GenerationStatus.COMPLETED, 1),
    ("completed", GenerationStatus.COMPLETED, 1, GenerationStatus.COMPLETED, 1),
]


@pytest.mark.parametrize(
    ("point", "left", "posted_before", "final", "posted"),
    VIDEO_KILLS,
    ids=[case[0] for case in VIDEO_KILLS],
)
async def test_cc5_a_video_killed_at_each_submit_state_is_posted_at_most_once(
    spend_run: uuid.UUID,
    children: Children,
    openrouter: MockOpenRouter,
    tmp_path: Path,
    point: str,
    left: GenerationStatus,
    posted_before: int,
    final: GenerationStatus,
    posted: int,
) -> None:
    args = _media_args("video", spend_run, openrouter, tmp_path)

    await children.killed(f"video-{point}", *args, "--kill-at", point)

    at_kill = await _only_job(spend_run)
    assert at_kill["status"] is left, at_kill
    assert len(openrouter.video_posts) == posted_before
    if point in ("submitting", "submitting_committed", "after_post"):
        # The dead worker's video slot is still held: no `finally` ran to
        # hand it back, and only its lease running out frees it (§9.5).
        assert await get_redis().zcard("media:video") == 1

    # A fresh worker: the same request resumes, then "Check again".
    resumed = await children.finish(f"video-{point}-resume", *args, "--check")

    assert len(openrouter.video_posts) == posted <= 1, f"{point}: a video was POSTed twice"
    job = await _only_job(spend_run)
    assert (job["id"], job["key"]) == (at_kill["id"], at_kill["key"])
    assert job["status"] is final, (job, resumed.result())
    spent, reserved = await _budget(spend_run)
    if final is GenerationStatus.COMPLETED:
        [job_id] = openrouter.video_job_ids()
        assert job["openrouter_job_id"] == job_id
        assert openrouter.downloads == [job_id], "downloaded once, never again on resume"
        stored = LocalStorage(str(tmp_path / "media")).get(
            f"creative/{spend_run}/media/unassigned/{job['id']}-0.mp4"
        )
        assert stored == video_bytes()
        assert job["cost_usd"] == Decimal("0.12")
        assert (spent, reserved) == (Decimal("0.12"), Decimal(0))
    else:
        # Surfaced to a human, never auto-resubmitted — not on resume, not on
        # Check again — and nothing to poll. The reservation is kept: the job
        # may exist and may be billed.
        assert resumed.result()["seen"] == {
            "resumed": "unknown_submit_state",
            "checked": "unknown_submit_state",
        }
        assert job["openrouter_job_id"] is None
        assert openrouter.polls == {} and openrouter.downloads == []
        assert (spent, reserved) == (Decimal(0), Decimal("0.12"))


#: (kill point, row status the kill leaves, POSTs before the kill)
IMAGE_KILLS = [
    ("queued", GenerationStatus.QUEUED, 0),
    ("submitting", GenerationStatus.QUEUED, 0),
    ("submitting_committed", GenerationStatus.SUBMITTING, 0),
    ("completed", GenerationStatus.COMPLETED, 1),
]


@pytest.mark.parametrize(
    ("point", "left", "posted_before"), IMAGE_KILLS, ids=[case[0] for case in IMAGE_KILLS]
)
async def test_cc5_an_image_is_re_posted_only_when_no_completed_row_exists(
    spend_run: uuid.UUID,
    children: Children,
    openrouter: MockOpenRouter,
    tmp_path: Path,
    point: str,
    left: GenerationStatus,
    posted_before: int,
) -> None:
    args = _media_args("image", spend_run, openrouter, tmp_path)

    await children.killed(f"image-{point}", *args, "--kill-at", point)

    at_kill = await _only_job(spend_run)
    assert at_kill["status"] is left, at_kill
    assert len(openrouter.image_posts) == posted_before

    resumed = await children.finish(f"image-{point}-resume", *args, "--check")

    seen = resumed.result()["seen"]
    job = await _only_job(spend_run)
    assert (job["id"], job["key"]) == (at_kill["id"], at_kill["key"]), "same row, same key"
    assert job["status"] is GenerationStatus.COMPLETED
    assert len(openrouter.image_posts) == 1, f"{point}: {openrouter.image_posts}"
    if point == "completed":
        # A `completed` row exists: neither the resume nor Check again POSTs.
        assert seen == {"resumed": "completed", "checked": "completed"}
        assert job["attempts"] == 1
    elif point == "submitting_committed":
        # Unknown whether OpenRouter got it: the resume surfaces it, and Check
        # again re-POSTs it — once, under the same key (an image re-POST is
        # safe: a failed generation is unbilled).
        assert seen == {"resumed": "unknown_submit_state", "checked": "completed"}
        assert job["attempts"] == 2
    else:
        assert seen == {"resumed": "completed", "checked": "completed"}
        assert job["attempts"] == 1
    [post] = openrouter.image_posts
    assert (post["model"], post["prompt"], post["aspect_ratio"]) == (
        IMAGE.model,
        job["request"]["prompt"],
        job["request"]["aspect_ratio"],
    )
    stored = LocalStorage(str(tmp_path / "media")).get(
        f"creative/{spend_run}/media/unassigned/{job['id']}-0.jpg"
    )
    assert stored
    # Law 43 across the kill: what the job cost is spent, nothing stays reserved.
    assert await _budget(spend_run) == (Decimal("0.015"), Decimal(0))


# ---------------------------------------------------------------------------
# CC12 — resume: a worker killed mid-DAG
# ---------------------------------------------------------------------------


async def test_cc12_a_worker_killed_mid_dag_re_executes_no_node_it_finished(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    children: Children,
) -> None:
    """The golden text run (S4-P16's), killed with 4.2.4's model call in
    flight and nine nodes finished; reaped, retried, and resumed in a fresh
    worker that asks no model anything for a node that had finished."""
    await _offers(db, project_id, FRESH)
    await _crawl(db, project_id, CRAWLED)
    await _seed(
        db,
        workspace_id,
        project_id,
        admin_user.id,
        extra_specs={"search": {**EXTRA_SPECS, **OFFER_SPECS}},
        plan={"required_signals": REQUIRED, "disqualifiers": DISQUALIFIERS},
    )
    started = await admin.post(
        f"/projects/{project_id}/creative/runs", json={"scope": GOLDEN_SCOPE, "media_models": []}
    )
    assert started.status_code == 202, started.text
    run_id = uuid.UUID(started.json()["run_id"])
    registry = _registry()
    halted = await execute(
        run_id, _Script().fake, registry=registry, dag=Dag.from_registry(registry)
    )
    assert halted.status is RunStatus.AWAITING_APPROVAL, halted.error
    decided = await admin.post(
        f"/approvals/{(await _g7(db, run_id)).id}", json={"decision": "approve"}
    )
    assert decided.status_code == 200, decided.text

    golden = ("run", "--scenario", "golden", "--run", str(run_id))
    worker = await children.spawn("golden-worker", *golden, "--hang-node", "4.2.4")

    async def mid_dag() -> list[dict[str, Any]] | None:
        rows = await node_runs(run_id)
        hung = any(r["node_id"] == "4.2.4" and r["status"] == "running" for r in rows)
        return rows if hung and len(succeeded(rows)) >= 9 else None

    await poll_until(mid_dag, what="4.2.4 in flight with nine nodes finished", child=worker)
    worker.kill()
    assert await worker.wait() == KILLED

    before = succeeded(await node_runs(run_id))
    assert len(before) >= 9 and not {"4.2.4", "4.3.1", "4.6.1", "4.7.2"} & set(before), before
    assert "4.2.4" in await recover_after_worker_kill(admin, run_id)

    resumed = Outcome.of(await children.finish("golden-resume", *golden))

    assert asked(resumed.requests, before) == {}, "a finished node asked a model again"
    assert "unattributed" not in resumed.requests, resumed.requests
    assert resumed.requests.get("4.2.4"), "the node in flight ran again"
    assert_untouched(before, await node_runs(run_id), context="after the resume")

    async def again() -> Outcome:
        after_h3 = Outcome.of(await children.finish("golden-after-h3", *golden))
        assert asked(after_h3.requests, before) == {}
        return after_h3

    result = await past_h3(admin, run_id, resumed, again)

    assert result.status is RunStatus.SUCCEEDED, result.error
    final = await node_runs(run_id)
    assert_untouched(before, final, context="at the end")
    assert len(succeeded(final)) == len(registry.ids)


# ---------------------------------------------------------------------------
# CC12 — decided G8 items are never re-asked
# ---------------------------------------------------------------------------


async def _to_g8(
    admin: ApiClient, db: AsyncSession, ids: tuple[Any, ...]
) -> tuple[uuid.UUID, uuid.UUID]:
    """An image run with three concepts, to a pending G8: approve one, reject
    one, regenerate one is then possible. Everything before G8 runs here, in
    process, on S4-P13's harness — no kill happens before G8."""
    run_id = await start_image_run(admin, db, *ids, capability=image_model(), specs=SEARCH)
    run = (
        await db.execute(
            sa.select(Run).where(Run.id == run_id).execution_options(populate_existing=True)
        )
    ).scalar_one()
    stored = CreativeInput.model_validate(run.creative_input)
    widened = stored.model_copy(
        update={"scope": stored.scope.model_copy(update={"concepts_per_campaign": 3})}
    )
    run.creative_input = widened.model_dump(mode="json", by_alias=True)
    run.input_hash = widened.content_hash()
    await db.commit()
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        _Painter().install(router)
        await media_chain(run_id)  # halts on G7
        await approve_g7(admin, db, run_id)
        await media_chain(run_id)  # halts on G8
    g8 = await _gate(db, run_id, "G8")
    assert g8 is not None and g8.status is ApprovalStatus.PENDING
    return run_id, g8.id


async def _card(approval_id: uuid.UUID) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        approval = await session.get(Approval, approval_id)
        assert approval is not None
        return {
            "id": approval.id,
            "status": approval.status.value,
            "decided_at": approval.decided_at,
            "decided_by": approval.decided_by,
            "proposal": approval.proposal,
        }


async def _decided(approval_id: uuid.UUID) -> list[dict[str, Any]]:
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            sa.select(AssetDecision)
            .where(AssetDecision.approval_id == approval_id)
            .order_by(AssetDecision.asset_id)
        )
        return [
            {
                "id": row.id,
                "asset_id": row.asset_id,
                "decision": row.decision.value,
                "round": row.round,
                "decided_by": row.decided_by,
                "decided_at": row.decided_at,
                "note": row.note,
            }
            for row in rows.scalars()
        ]


async def _gates(run_id: uuid.UUID) -> dict[str, list[dict[str, Any]]]:
    async with get_sessionmaker()() as session:
        rows = await session.execute(sa.select(Approval).where(Approval.run_id == run_id))
        found: dict[str, list[dict[str, Any]]] = {}
        for row in rows.scalars():
            found.setdefault(row.gate_key or row.node_id, []).append(
                {"id": row.id, "status": row.status.value, "proposal": row.proposal}
            )
        return found


async def _statuses(asset_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            sa.select(CreativeAsset.id, CreativeAsset.status).where(CreativeAsset.id.in_(asset_ids))
        )
        return {asset_id: status.value for asset_id, status in rows.all()}


async def _regeneration(run_id: uuid.UUID) -> tuple[list[dict[str, Any]], list[uuid.UUID]]:
    """4.4.6's generation jobs and the assets it made."""
    async with get_sessionmaker()() as session:
        jobs = await session.execute(
            sa.select(GenerationJob)
            .where(GenerationJob.creative_run_id == run_id, GenerationJob.node_id == "4.4.6")
            .order_by(GenerationJob.created_at)
        )
        made = await session.execute(
            sa.select(CreativeAsset.id).where(
                CreativeAsset.creative_run_id == run_id, CreativeAsset.node_id == "4.4.6"
            )
        )
        return (
            [
                {
                    "id": j.id,
                    "status": j.status.value,
                    "attempts": j.attempts,
                    "key": j.idempotency_key,
                }
                for j in jobs.scalars()
            ],
            list(made.scalars()),
        )


def _decisions_for(card: dict[str, Any]) -> tuple[dict[str, Any], dict[str, uuid.UUID]]:
    keep, drop, again = (item["asset_id"] for item in card["proposal"]["items"])
    body = {
        "decision": "approve",
        "edited_proposal": {
            "items": [
                _item(keep, "approve", checklist=TICKED),
                _item(drop, "reject", note="Off-brief."),
                _item(again, "regenerate", note="Warmer light, closer framing."),
            ]
        },
    }
    return body, {"keep": uuid.UUID(keep), "drop": uuid.UUID(drop), "again": uuid.UUID(again)}


async def _assert_g8_not_re_asked(
    run_id: uuid.UUID,
    g8_id: uuid.UUID,
    *,
    card: dict[str, Any],
    decided: list[dict[str, Any]],
    assets: dict[str, uuid.UUID],
    statuses: dict[uuid.UUID, str],
) -> None:
    """G8 is exactly as it was decided — one row, its three decisions
    untouched, none added — and G8b asks about the regenerated asset only."""
    assert await _card(g8_id) == card
    assert await _decided(g8_id) == decided, "a decided G8 item changed or was decided again"
    gates = await _gates(run_id)
    assert [g["id"] for g in gates["G8"]] == [g8_id], "G8 was opened again"
    [g8b] = gates["G8b"]
    assert g8b["status"] == "pending"
    jobs, [child] = await _regeneration(run_id)
    items = g8b["proposal"]["items"]
    assert [i["asset_id"] for i in items] == [str(child)], "G8b re-asked a decided item"
    assert items[0]["regenerated_from"] == str(assets["again"])
    assert await _statuses([assets["keep"], assets["drop"]]) == {
        assets["keep"]: statuses[assets["keep"]],
        assets["drop"]: statuses[assets["drop"]],
    }
    assert statuses[assets["keep"]] == "approved" and statuses[assets["drop"]] == "rejected"
    assert jobs and all(j["status"] == "completed" and j["attempts"] == 1 for j in jobs), jobs


async def test_cc12_a_worker_killed_mid_regeneration_re_asks_no_decided_g8_item(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    children: Children,
    storage: LocalStorage,
) -> None:
    run_id, g8_id = await _to_g8(admin, db, (workspace_id, project_id, admin_user.id))
    body, assets = _decisions_for(await _card(g8_id))
    decided = await admin.post(f"/approvals/{g8_id}", json=body)
    assert decided.status_code == 200, decided.text
    card, decisions = await _card(g8_id), await _decided(g8_id)
    assert {d["asset_id"]: d["decision"] for d in decisions} == {
        assets["keep"]: "approve",
        assets["drop"]: "reject",
        assets["again"]: "regenerate",
    }
    statuses = await _statuses(list(assets.values()))

    with MockOpenRouter(paint=True) as openrouter:
        images = (
            "run",
            "--scenario",
            "images",
            "--run",
            str(run_id),
            "--base",
            openrouter.base_url,
        )
        # 4.4.6's first job (the master) completes; the worker dies as its
        # second (the relay) is queued.
        await children.killed("regen-worker", *images, "--kill-at", "queued", "--kill-nth", "2")

        before = succeeded(await node_runs(run_id))
        assert {"4.4.5"} <= set(before) and "4.4.6" not in before, before
        jobs, made = await _regeneration(run_id)
        assert [j["status"] for j in jobs] == ["completed", "queued"], jobs
        assert len(made) == 1 and len(openrouter.image_posts) == 1
        assert "4.4.6" in await recover_after_worker_kill(admin, run_id)

        resumed = Outcome.of(await children.finish("regen-resume", *images))

        assert resumed.status is RunStatus.AWAITING_APPROVAL, await _errors(run_id)
        assert resumed.awaiting == ("4.4.7",)
        after, again_made = await _regeneration(run_id)
        assert again_made == made, "the resume made a second regenerated asset"
        assert [j["id"] for j in after] == [j["id"] for j in jobs]
        # The completed master was never POSTed again; the relay was, once.
        assert len(openrouter.image_posts) == len(after) == 2, openrouter.image_posts
    assert asked(resumed.requests, before) == {}, "a finished node asked a model again"
    assert_untouched(before, await node_runs(run_id), context="after the resume")
    await _assert_g8_not_re_asked(
        run_id, g8_id, card=card, decided=decisions, assets=assets, statuses=statuses
    )


# ---------------------------------------------------------------------------
# CC12 — an api killed right after it committed a G8 decision
# ---------------------------------------------------------------------------


async def _killed_inside(
    children: Children, workspace: Any, kill_in: str, path: str, body: dict[str, Any]
) -> None:
    """Serve the real app under uvicorn in its own process, sign in over TCP,
    and send `path` — inside which the api SIGKILLs itself at `kill_in`. The
    connection drops with no response; the process's exit status says how."""
    port = free_port()
    server = await children.spawn(
        f"api-{kill_in}", "api", "--port", str(port), "--kill-in", kill_in
    )
    base = f"http://127.0.0.1:{port}"

    async def up() -> bool:
        try:
            async with httpx.AsyncClient(base_url=base) as probe:
                return (await probe.get("/api/v1/health")).status_code == 200
        except httpx.TransportError:
            return False

    await poll_until(up, what="uvicorn to answer", child=server)
    browser = ApiClient(httpx.AsyncClient(base_url=base, timeout=30.0))
    async with browser.raw:
        signed_in = await browser.login(workspace.admin_email, workspace.admin_password)
        assert signed_in.status_code == 200, signed_in.text
        with pytest.raises(httpx.TransportError):
            await browser.post(path, json=body)
    assert await server.wait() == KILLED, server.logs()


async def test_cc12_an_api_killed_right_after_it_started_a_run_leaves_nothing_unclosable(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    workspace: Any,
    children: Children,
) -> None:
    """The other place an api crash lands: a run's start — its row, project
    lock and audit committed, arq never told. No node exists, so none can run
    twice; what must not happen is a run that says `queued` for ever on a
    project nobody can launch on. The reaper's never-started sweep is what
    closes it (PRD §16), for a creative run as for a research one."""
    await _seed(db, workspace_id, project_id, admin_user.id)
    start = {"scope": GOLDEN_SCOPE, "media_models": []}

    await _killed_inside(
        children, workspace, "launch", f"/projects/{project_id}/creative/runs", start
    )

    async with get_sessionmaker()() as session:
        run_id, status, started_at = (
            await session.execute(
                sa.select(Run.id, Run.status, Run.started_at).where(
                    Run.project_id == project_id, Run.stage == RunStage.CREATIVE
                )
            )
        ).one()
    assert (status, started_at) == (RunStatus.QUEUED, None)
    assert await _arq_jobs(run_id) == [], "the dead api enqueued nothing"
    assert await node_runs(run_id) == []
    lock = RunLock(get_redis(), RunStage.CREATIVE)
    held = await lock.holder(project_id)
    assert held is not None and held.run_id == run_id

    # Inside the grace a queued run is left alone: a busy queue is normal.
    async with get_sessionmaker()() as session:
        assert (await reap_stale_runs(session, get_redis())).total == 0
    # Past it, nothing will ever pick the run up.
    later = datetime.now(UTC) + timedelta(seconds=QUEUED_GRACE_SECONDS + 60)
    async with get_sessionmaker()() as session:
        outcome = await reap_stale_runs(session, get_redis(), now=later)

    assert outcome.never_started == (run_id,), outcome
    assert await run_status(run_id) == "failed"
    assert await lock.holder(project_id) is None, "the project is still locked by a dead launch"
    assert await node_runs(run_id) == []
    relaunched = await admin.post(f"/projects/{project_id}/creative/runs", json=start)
    assert relaunched.status_code == 202, relaunched.text


async def _arq_jobs(run_id: uuid.UUID) -> list[str]:
    members = await get_redis().zrange("arq:queue", 0, -1)
    names = [m.decode() if isinstance(m, bytes) else str(m) for m in members]
    return sorted(n for n in names if n.startswith(f"run:{run_id}"))


@pytest.mark.parametrize(
    ("kill_in", "left"),
    [("resume", RunStatus.AWAITING_APPROVAL), ("enqueue", RunStatus.QUEUED)],
    ids=["after_the_decision_committed", "after_the_run_was_queued"],
)
async def test_cc12_an_api_killed_after_a_g8_decision_re_executes_nothing_and_re_asks_nothing(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    workspace: Any,
    children: Children,
    storage: LocalStorage,
    kill_in: str,
    left: RunStatus,
) -> None:
    """`api` runs no node (the worker does), so what an api crash can leave
    behind is a committed decision whose run never reached the queue. The api
    here is uvicorn in its own process, killed by SIGKILL inside the decision
    request — after its transaction committed, before arq had the job."""
    run_id, g8_id = await _to_g8(admin, db, (workspace_id, project_id, admin_user.id))
    body, assets = _decisions_for(await _card(g8_id))
    rows_before = await node_runs(run_id)
    queued_before = await _arq_jobs(run_id)

    await _killed_inside(children, workspace, kill_in, f"/approvals/{g8_id}", body)

    # What the dead api left: the decision, durable; the run, never queued.
    card, decisions = await _card(g8_id), await _decided(g8_id)
    assert card["status"] == "approved"
    assert sorted(d["decision"] for d in decisions) == ["approve", "regenerate", "reject"]
    assert await run_status(run_id) == left.value
    assert await _arq_jobs(run_id) == queued_before, "the dead api enqueued nothing"
    # No node ran in the api: only the gate's own row moved, to its decision.
    rows_after = await node_runs(run_id)
    assert [r for r in rows_after if r["node_id"] != "4.4.5"] == [
        r for r in rows_before if r["node_id"] != "4.4.5"
    ]
    [gate_row] = [r for r in rows_after if r["node_id"] == "4.4.5"]
    assert gate_row["status"] == "succeeded" and gate_row["attempt"] == 1
    statuses = await _statuses(list(assets.values()))
    before = succeeded(rows_after)

    # Nothing re-queues such a run by itself (see the report): an operator
    # cancels it and retries it — the same re-entry a reaped run takes.
    cancelled = await admin.post(f"/runs/{run_id}/cancel")
    assert cancelled.status_code == 202, cancelled.text
    retried = await admin.post(f"/runs/{run_id}/retry-failed")
    assert retried.status_code == 202, retried.text

    with MockOpenRouter(paint=True) as openrouter:
        images = (
            "run",
            "--scenario",
            "images",
            "--run",
            str(run_id),
            "--base",
            openrouter.base_url,
        )
        resumed = Outcome.of(await children.finish("api-crash-worker", *images))
        assert len(openrouter.image_posts) == len((await _regeneration(run_id))[0])

    assert resumed.status is RunStatus.AWAITING_APPROVAL and resumed.awaiting == ("4.4.7",)
    assert asked(resumed.requests, before) == {}, "a finished node asked a model again"
    assert_untouched(before, await node_runs(run_id), context="after the api crash")
    await _assert_g8_not_re_asked(
        run_id, g8_id, card=card, decided=decisions, assets=assets, statuses=statuses
    )


# ---------------------------------------------------------------------------
# CC12 — an in-flight video resumes polling
# ---------------------------------------------------------------------------


async def test_cc12_a_worker_killed_while_polling_resumes_the_saved_job_and_never_re_posts(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    children: Children,
    storage: LocalStorage,
) -> None:
    """S4-P11's run to 4.4.4, killed the first time a clip reports
    `in_progress`; the resumed node polls the `openrouter_job_id`s it saved,
    POSTs nothing and writes no new script (§18)."""
    run_id = await start_video_run(admin, db, workspace_id, project_id, admin_user.id)
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        providers(router, Videos())
        await run_video(run_id, media_jobs(Clock()))  # halts on G7
        await approve_g7(admin, db, run_id)

    with MockOpenRouter() as openrouter:
        video = ("run", "--scenario", "video", "--run", str(run_id), "--base", openrouter.base_url)
        await children.killed("poll-worker", *video, "--kill-at", "in_progress")

        before = succeeded(await node_runs(run_id))
        submitted = await _video_jobs(run_id)
        posted = openrouter.video_job_ids()
        assert posted and sorted(j["openrouter_job_id"] for j in submitted) == sorted(posted)
        assert "in_progress" in {j["status"] for j in submitted}
        assert openrouter.downloads == [] and "4.4.4" not in before
        assert "4.4.4" in await recover_after_worker_kill(admin, run_id)

        resumed = Outcome.of(await children.finish("poll-resume", *video))

        assert resumed.status is RunStatus.SUCCEEDED, resumed.error
        assert openrouter.video_job_ids() == posted, "a clip was POSTed again after the kill"
        assert sorted(openrouter.downloads) == sorted(posted), "each saved job, downloaded once"
        # The in-progress jobs were polled on, past where the dead worker stopped.
        assert all(openrouter.polls[job] >= 3 for job in posted), openrouter.polls
    done = await _video_jobs(run_id)
    assert [j["id"] for j in done] == [j["id"] for j in submitted]
    assert {j["status"] for j in done} == {"completed"} and all(j["attempts"] == 1 for j in done)
    assert not [k for k in resumed.schemas if k.endswith(":VideoScriptDraft")], resumed.schemas
    assert asked(resumed.requests, before) == {}
    assert_untouched(before, await node_runs(run_id), context="after the resume")


async def _errors(run_id: uuid.UUID) -> list[Any]:
    """Every failed node's error, for an assertion message."""
    from agent.db.models import NodeRun

    async with get_sessionmaker()() as session:
        rows = await session.execute(
            sa.select(NodeRun.node_id, NodeRun.attempt, NodeRun.error).where(
                NodeRun.run_id == run_id, NodeRun.status == "failed"
            )
        )
        return [tuple(row) for row in rows.all()]


async def _video_jobs(run_id: uuid.UUID) -> list[dict[str, Any]]:
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            sa.select(GenerationJob)
            .where(GenerationJob.creative_run_id == run_id)
            .order_by(GenerationJob.created_at)
        )
        return [
            {
                "id": j.id,
                "status": j.status.value,
                "attempts": j.attempts,
                "openrouter_job_id": j.openrouter_job_id,
            }
            for j in rows.scalars()
        ]
