"""The processes the S4-P24 `kill -9` suite kills (`test_s4p24_kill9.py`).

    python -m tests.integration.s4p24_kill9_child media  --kind video --run … --base … [--kill-at S]
    python -m tests.integration.s4p24_kill9_child run    --scenario golden|images|video --run … [--hang-node N | --kill-at S --kill-nth K]
    python -m tests.integration.s4p24_kill9_child api    --port P --kill-in resume|enqueue|launch

Each is the real code under test, not a model of it:

* `media` builds the real `MediaJobs` against the parent's mock OpenRouter (a
  real HTTP server) and submits, resumes and checks one job, as a worker does.
* `run` drives the real `RunExecutor` over a creative run — the golden text
  run's scripted copy, or S4-P9's schema-answering provider with media going to
  the parent's mock OpenRouter — as `worker.execute_run` does.
* `api` serves the real FastAPI app under uvicorn on 127.0.0.1.

A kill is `os.kill(os.getpid(), SIGKILL)`: nothing after it runs — no
`finally`, no `__aexit__`, no rollback; Postgres drops the connection's open
transaction, and Redis keeps whatever the process wrote. A run that finishes
prints one `RESULT {json}` line for the parent to read.

The integration conftest's autouse patches (landing pages and SERP previews
unrendered, the gateway's rate limiter out of the clock) and the golden run's
scripted web are applied here by hand: there is no pytest in this process.
"""

from __future__ import annotations

import argparse
import asyncio
import contextvars
import json
import os
import signal
import uuid
from collections import Counter
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import httpx

#: Which node the running coroutine belongs to, for attributing model requests.
CURRENT_NODE: contextvars.ContextVar[str | None] = contextvars.ContextVar("node", default=None)


def die() -> None:
    """kill -9, from inside."""
    os.kill(os.getpid(), signal.SIGKILL)


def emit(payload: dict[str, Any]) -> None:
    print("RESULT " + json.dumps(payload, default=str), flush=True)


def killer(kill_at: str | None, nth: int = 1) -> Callable[[str], None]:
    """A `MediaJobs` checkpoint that SIGKILLs this process the `nth` time it
    reaches `kill_at`."""
    seen: Counter[str] = Counter()

    def checkpoint(name: str) -> None:
        seen[name] += 1
        if name == kill_at and seen[name] == nth:
            die()

    return checkpoint


async def _fast(_seconds: float) -> None:
    await asyncio.sleep(0)


async def _close() -> None:
    from agent.db.session import dispose_engine
    from agent.redis_client import close_redis

    await close_redis()
    await dispose_engine()


# ---------------------------------------------------------------------------
# media: one job through the real job layer
# ---------------------------------------------------------------------------


async def media(args: argparse.Namespace) -> None:
    from agent.db.session import get_sessionmaker
    from agent.media.budget import MediaBudget
    from agent.media.constants import media_constants
    from agent.media.http import MediaApi
    from agent.media.images import ImageClient
    from agent.media.jobs import MediaJobs
    from agent.media.videos import VideoClient
    from agent.redis_client import get_redis
    from agent.storage.local import LocalStorage
    from tests.integration.test_media_jobs import IMAGE, KEY, VIDEO, choice, flux, veo

    if args.kill_at == "completed":
        # No checkpoint sits here: the job's `completed` row is committed, and
        # the budget reservation is still to be reconciled. A kill between the
        # two is the one a Postgres commit and a Redis call cannot make atomic.
        def reconcile(_self: Any, _job_id: uuid.UUID, _actual: Decimal) -> Any:
            die()

        MediaBudget.reconcile = reconcile  # type: ignore[method-assign,assignment]

    video = args.kind == "video"
    picked = choice(veo() if video else flux())
    async with httpx.AsyncClient() as client:
        api = MediaApi(client=client, api_key=KEY, base_url=args.base)
        jobs = MediaJobs(
            sessionmaker=get_sessionmaker(),
            redis=get_redis(),
            images=ImageClient(api, sleep=_fast),
            videos=VideoClient(api),
            storage=LocalStorage(args.storage),
            constants=media_constants(),
            sleep=_fast,
            checkpoint=killer(args.kill_at),
        )
        job = await jobs.submit_or_resume(
            run_id=uuid.UUID(args.run),
            node_id="4.4.4" if video else "4.4.2",
            asset_id=None,
            round=1,
            request=VIDEO if video else IMAGE,
            choice=picked,
            estimate_usd=Decimal("0.12" if video else "0.03"),
        )
        seen = {"resumed": job.status.value}
        if job.openrouter_job_id is not None:
            job = await jobs.await_video(job.id)
            seen["awaited"] = job.status.value
        if args.check:
            job = await jobs.check(job.id, choice=picked)
            seen["checked"] = job.status.value
    await _close()
    emit({"job_id": str(job.id), "status": job.status.value, "seen": seen})


# ---------------------------------------------------------------------------
# run: the executor over a creative run
# ---------------------------------------------------------------------------


def _unrendered(mp: Any) -> None:
    import tests.integration.conftest as integration

    integration.unrendered_landing_pages.__wrapped__(mp)  # type: ignore[attr-defined]
    integration.unrendered_serp_previews.__wrapped__(mp)  # type: ignore[attr-defined]
    integration.fast_llm_limiter.__wrapped__(mp)  # type: ignore[attr-defined]


def _attribute(registry: Any) -> None:
    """Tag every model request with the node that made it: each node's
    `gather` and `reason` run with `CURRENT_NODE` set (tasks they start inherit
    it), and the transport reads it."""
    for node_id in registry.ids:
        node = registry.node(node_id)
        for name in ("gather", "reason"):
            original = getattr(node, name)

            async def tagged(*a: Any, _run: Any = original, _id: str = node_id, **k: Any) -> Any:
                token = CURRENT_NODE.set(_id)
                try:
                    return await _run(*a, **k)
                finally:
                    CURRENT_NODE.reset(token)

            setattr(node, name, tagged)


def _media_chain(*, images: bool) -> Any:
    from agent.nodes.creative.n4_1_1_creative_brief import CREATIVE_BRIEF
    from agent.nodes.creative.n4_4_1_creative_concepts import CREATIVE_CONCEPTS
    from agent.nodes.creative.n4_4_4_video_production import VIDEO_PRODUCTION
    from agent.orchestrator.registry import NodeRegistry

    if not images:  # S4-P11's chain: 4.1.1 (G7) → 4.4.1 → 4.4.4
        return NodeRegistry.of([CREATIVE_BRIEF, CREATIVE_CONCEPTS, VIDEO_PRODUCTION])
    from agent.nodes.creative.n4_4_2_image_masters import IMAGE_MASTERS
    from agent.nodes.creative.n4_4_3_image_renditions import IMAGE_RENDITIONS
    from agent.nodes.creative.n4_4_5_ai_asset_review import AI_ASSET_REVIEW
    from agent.nodes.creative.n4_4_6_asset_regeneration import ASSET_REGENERATION
    from agent.nodes.creative.n4_4_7_ai_asset_review_final import AI_ASSET_REVIEW_FINAL

    # S4-P13's chain: the media nodes and their review, and only them.
    return NodeRegistry.of(
        [
            CREATIVE_BRIEF,
            CREATIVE_CONCEPTS,
            IMAGE_MASTERS,
            IMAGE_RENDITIONS,
            VIDEO_PRODUCTION,
            AI_ASSET_REVIEW,
            ASSET_REGENERATION,
            AI_ASSET_REVIEW_FINAL,
        ]
    )


def _media_jobs(args: argparse.Namespace) -> Any:
    from agent.config import get_settings
    from agent.db.session import get_sessionmaker
    from agent.media.constants import media_constants
    from agent.media.http import MediaApi
    from agent.media.images import ImageClient
    from agent.media.jobs import MediaJobs
    from agent.media.videos import VideoClient
    from agent.redis_client import get_redis
    from agent.storage.backend import get_storage

    settings = get_settings()
    api = MediaApi(
        client=httpx.AsyncClient(timeout=30.0), api_key="sk-or-test-key", base_url=args.base
    )
    return MediaJobs(
        sessionmaker=get_sessionmaker(),
        redis=get_redis(),
        images=ImageClient(api, sleep=_fast),
        videos=VideoClient(api),
        storage=get_storage(settings),
        constants=media_constants(),
        sleep=_fast,
        checkpoint=killer(args.kill_at, args.kill_nth),
        defaults=settings,
    )


async def run(args: argparse.Namespace) -> None:
    import pytest

    from agent.orchestrator.dag import Dag
    from agent.preview import urlcheck
    from tests.integration.runs_support import execute
    from tests.openrouter_fake import FakeOpenRouter

    mp = pytest.MonkeyPatch()
    _unrendered(mp)
    if args.scenario == "golden":
        from tests.integration.test_s4p6_descriptions_variant_b import _registry
        from tests.integration.test_s4p8_extras import WEB, _Script, _Web

        scripted = _Web(dict(WEB))
        mp.setattr(
            urlcheck,
            "new_client",
            lambda: httpx.AsyncClient(transport=httpx.MockTransport(scripted.handler)),
        )
        fake = _Script().fake
        registry = _registry()
    else:
        from tests.integration.s4p9_support import Provider

        if args.scenario == "video":
            # S4-P11's harness: clips are proven here, post-production in S4-P12.
            from agent.nodes.creative import n4_4_4_video_production as n444

            async def nothing(_self: Any, _made: Any) -> list[Any]:
                return []

            mp.setattr(n444._PostProduction, "video", nothing)
        provider = Provider(images=[])
        fake = FakeOpenRouter()
        fake.dispatch(provider._chat)
        registry = _media_chain(images=args.scenario == "images")
    _attribute(registry)

    asked: Counter[str] = Counter()
    schemas: Counter[str] = Counter()

    async def transport(request: httpx.Request) -> httpx.Response:
        if not request.url.path.endswith("/models"):
            node = CURRENT_NODE.get() or "unattributed"
            asked[node] += 1
            spec = json.loads(request.content).get("response_format", {}).get("json_schema", {})
            schemas[f"{node}:{spec.get('schema', {}).get('title') or spec.get('name')}"] += 1
            if node == args.hang_node:
                # The model call is in flight and never answers: the worker is
                # mid-node when the parent's SIGKILL lands.
                await asyncio.Event().wait()
        return fake.handler(request)

    media = _media_jobs(args) if args.base else None
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        result = await execute(
            uuid.UUID(args.run),
            fake,
            client=client,
            registry=registry,
            dag=Dag.from_registry(registry),
            max_attempts=1,
            media=media,
        )
    await _close()
    emit(
        {
            "status": result.status.value,
            "awaiting": list(result.awaiting),
            "error": result.error,
            "executed": result.nodes_executed,
            "requests": dict(asked),
            "schemas": dict(schemas),
        }
    )


# ---------------------------------------------------------------------------
# api: the FastAPI app under uvicorn
# ---------------------------------------------------------------------------


def api(args: argparse.Namespace) -> None:
    import uvicorn

    from agent.api import routes_approvals
    from agent.api.routes_media import get_media_catalogue
    from agent.main import create_app
    from tests.media.replay import replay_catalogue

    async def killed(*_args: Any, **_kwargs: Any) -> Any:
        die()

    if args.kill_in == "resume":
        # After the decision's transaction committed, before the run is re-queued.
        routes_approvals._resume = killed  # type: ignore[assignment]
    elif args.kill_in == "enqueue":
        # After the run was committed `queued`, before arq has the job.
        routes_approvals.enqueue_run = killed  # type: ignore[assignment]
    elif args.kill_in == "launch":
        # A run's start: its row, lock and audit committed, arq never told.
        from agent.orchestrator import launch

        launch.enqueue_run = killed  # type: ignore[assignment]
    app = create_app()
    # As `conftest.build_client` does for every app a test builds: the media
    # catalogue is the recorded one (no test reaches live OpenRouter).
    app.dependency_overrides[get_media_catalogue] = replay_catalogue
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning", lifespan="on")


def main() -> None:
    parser = argparse.ArgumentParser(prog="s4p24_kill9_child")
    sub = parser.add_subparsers(dest="mode", required=True)

    one = sub.add_parser("media")
    one.add_argument("--kind", choices=["video", "image"], required=True)
    one.add_argument("--run", required=True)
    one.add_argument("--base", required=True)
    one.add_argument("--storage", required=True)
    one.add_argument("--kill-at")
    one.add_argument("--check", action="store_true")

    dag = sub.add_parser("run")
    dag.add_argument("--scenario", choices=["golden", "images", "video"], required=True)
    dag.add_argument("--run", required=True)
    dag.add_argument("--base")
    dag.add_argument("--hang-node")
    dag.add_argument("--kill-at")
    dag.add_argument("--kill-nth", type=int, default=1)

    serve = sub.add_parser("api")
    serve.add_argument("--port", type=int, required=True)
    serve.add_argument("--kill-in", choices=["resume", "enqueue", "launch"], required=True)

    args = parser.parse_args()
    if args.mode == "media":
        asyncio.run(media(args))
    elif args.mode == "run":
        asyncio.run(run(args))
    else:
        api(args)


if __name__ == "__main__":
    main()
