"""S4-P24 harness for the real `kill -9` suite (Stage 04 PRD §17 CC5, CC12; §18).

Three pieces, each built so that nothing the assertions need lives inside the
process that gets killed:

* `MockOpenRouter` — OpenRouter's media endpoints as a real HTTP server on
  127.0.0.1, in a thread of the *test* process. It serves the recorded
  fixtures (`tests/fixtures/openrouter/`) and counts every POST /videos and
  POST /images, so a count survives the SIGKILL of the worker that sent it.
* `Children` — the worker (and api) processes: `python -m
  tests.integration.s4p24_kill9_child …`, same container, same database, same
  Redis, same storage directory. A child that kills itself does it with
  `os.kill(os.getpid(), SIGKILL)`; the parent reads the verdict from the exit
  status (`-SIGKILL`), so no `finally`, no rollback and no `__aexit__` ran.
* Row snapshots read on fresh sessions, as plain values, so a comparison
  before and after a kill never touches an expired ORM instance.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import signal
import socket
import sys
import threading
import uuid
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from tests.integration.s4p11_support import _with_id
from tests.media.openrouter_mock import body, video_bytes

API_ROOT = Path(__file__).resolve().parents[2]
CHILD = "tests.integration.s4p24_kill9_child"
KILLED = -signal.SIGKILL
#: How long any one child may take. Generous: a child that hangs is a failure,
#: and the fixture kills whatever is left at teardown.
CHILD_TIMEOUT_S = 120.0

_VIDEO = re.compile(r"^/api/v1/videos/(?P<job>[^/?]+)$")
_CONTENT = re.compile(r"^/api/v1/videos/(?P<job>[^/?]+)/content$")


# ---------------------------------------------------------------------------
# OpenRouter's media endpoints, over real HTTP
# ---------------------------------------------------------------------------


class MockOpenRouter:
    """POST /videos, GET /videos/{id}, GET /videos/{id}/content, POST /images.

    One job id per video POST (`gen-vid-s4p24-{n}`); the n-th poll of a job
    answers `video_states[n-1]`, the last one repeating — so a worker killed
    mid-wait and resumed carries on down the same job's sequence, exactly as
    OpenRouter would. Images answer the recorded body; with `paint=True` they
    are photographs at the requested aspect ratio (S4-P10's `_photo`), each a
    different one, which is what 4.4.2's lint and ranking need.
    """

    PREFIX = "/api/v1"
    SIZES = {"1:1": (512, 512), "16:9": (1024, 576)}

    def __init__(
        self,
        *,
        video_states: tuple[str, ...] = ("pending", "in_progress", "completed"),
        paint: bool = False,
    ) -> None:
        self.video_states = video_states
        self.paint = paint
        self.lock = threading.Lock()
        self.video_posts: list[dict[str, Any]] = []
        self.image_posts: list[dict[str, Any]] = []
        self.polls: dict[str, int] = {}
        self.downloads: list[str] = []
        self.unexpected: list[str] = []
        self._painted = 1000
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_class())
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}{self.PREFIX}"

    def __enter__(self) -> MockOpenRouter:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()

    def video_job_ids(self) -> list[str]:
        with self.lock:
            return [f"gen-vid-s4p24-{n}" for n in range(1, len(self.video_posts) + 1)]

    # -- answers -------------------------------------------------------------

    def answer(self, method: str, path: str, raw: bytes) -> tuple[int, str, bytes]:
        if method == "POST" and path == f"{self.PREFIX}/videos":
            with self.lock:
                self.video_posts.append(json.loads(raw))
                job = f"gen-vid-s4p24-{len(self.video_posts)}"
            return 202, "application/json", _json(_with_id(body("video_submit.json"), job))
        if method == "GET" and (match := _CONTENT.match(path)):
            with self.lock:
                self.downloads.append(match["job"])
            return 200, "video/mp4", video_bytes()
        if method == "GET" and (match := _VIDEO.match(path)):
            with self.lock:
                count = self.polls[match["job"]] = self.polls.get(match["job"], 0) + 1
            state = self.video_states[min(count, len(self.video_states)) - 1]
            payload = _with_id(body(f"video_poll_{state}.json"), match["job"])
            return 200, "application/json", _json(payload)
        if method == "POST" and path == f"{self.PREFIX}/images":
            return 200, "application/json", self._image(raw)
        with self.lock:
            self.unexpected.append(f"{method} {path}")
        return 404, "application/json", _json({"error": {"message": f"no route {path}"}})

    def _image(self, raw: bytes) -> bytes:
        payload = json.loads(raw)
        count = int(payload.get("n") or 1)
        with self.lock:
            self.image_posts.append(
                {
                    "model": payload.get("model"),
                    "prompt": payload.get("prompt"),
                    "aspect_ratio": payload.get("aspect_ratio"),
                    "n": count,
                    "references": len(payload.get("input_references") or []),
                    "body_sha256": hashlib.sha256(raw).hexdigest(),
                }
            )
            first = self._painted
            self._painted += count
        answer = body("image_generate.json")
        if self.paint and payload.get("aspect_ratio") in self.SIZES:
            from tests.integration.test_s4p10_image_renditions import _photo

            size = self.SIZES[payload["aspect_ratio"]]
            answer["data"] = [
                {
                    "b64_json": base64.b64encode(_photo(size, first + i)).decode(),
                    "media_type": "image/png",
                }
                for i in range(count)
            ]
        return _json(answer)

    def _handler_class(self) -> type[BaseHTTPRequestHandler]:
        mock = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _serve(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length) if length else b""
                status, content_type, payload = mock.answer(
                    self.command, self.path.split("?", 1)[0], raw
                )
                self.send_response(status)
                self.send_header("content-type", content_type)
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = _serve  # noqa: N815 — BaseHTTPRequestHandler's naming
            do_POST = _serve  # noqa: N815

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return None

        return Handler


def _json(payload: Any) -> bytes:
    return json.dumps(payload).encode()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


# ---------------------------------------------------------------------------
# child processes
# ---------------------------------------------------------------------------


@dataclass
class Child:
    name: str
    process: asyncio.subprocess.Process
    out: Path
    err: Path

    @property
    def returncode(self) -> int | None:
        return self.process.returncode

    async def wait(self, within: float = CHILD_TIMEOUT_S) -> int:
        try:
            return await asyncio.wait_for(self.process.wait(), within)
        except TimeoutError:
            self.process.kill()
            await self.process.wait()
            raise AssertionError(
                f"child {self.name} did not finish in {within:g} s\n{self.logs()}"
            ) from None

    def kill(self) -> None:
        """A real SIGKILL, from outside."""
        if self.process.returncode is None:
            os.kill(self.process.pid, signal.SIGKILL)

    def logs(self) -> str:
        tail = self.err.read_text(errors="replace")[-6000:]
        out = self.out.read_text(errors="replace")[-3000:]
        return f"--- {self.name} stderr (tail) ---\n{tail}\n--- stdout (tail) ---\n{out}"

    def result(self) -> dict[str, Any]:
        lines = [line for line in self.out.read_text().splitlines() if line.startswith("RESULT ")]
        assert lines, f"child {self.name} printed no RESULT\n{self.logs()}"
        return dict(json.loads(lines[-1].removeprefix("RESULT ")))


@dataclass
class Children:
    """Every process a test starts, killed at teardown if still alive — a live
    child holding a row lock would deadlock the next test's TRUNCATE."""

    root: Path
    started: list[Child] = field(default_factory=list)

    async def spawn(self, name: str, *args: str, env: dict[str, str] | None = None) -> Child:
        out, err = self.root / f"{name}.out", self.root / f"{name}.err"
        with out.open("wb") as stdout, err.open("wb") as stderr:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                CHILD,
                *args,
                stdout=stdout,
                stderr=stderr,
                cwd=API_ROOT,
                env={**os.environ, "PYTHONUNBUFFERED": "1", **(env or {})},
            )
        child = Child(name, process, out, err)
        self.started.append(child)
        return child

    async def finish(self, name: str, *args: str, env: dict[str, str] | None = None) -> Child:
        """Spawn and wait; the child must exit 0."""
        child = await self.spawn(name, *args, env=env)
        code = await child.wait()
        assert code == 0, f"child {name} exited {code}\n{child.logs()}"
        return child

    async def killed(self, name: str, *args: str, env: dict[str, str] | None = None) -> Child:
        """Spawn and wait for it to SIGKILL itself at the checkpoint it was given."""
        child = await self.spawn(name, *args, env=env)
        code = await child.wait()
        assert code == KILLED, f"child {name} exited {code}, not -SIGKILL\n{child.logs()}"
        return child

    async def reap(self) -> None:
        for child in self.started:
            if child.process.returncode is None:
                child.kill()
                await child.process.wait()


async def poll_until[T](
    probe: Callable[[], Awaitable[T | None]],
    *,
    what: str,
    child: Child | None = None,
    within: float = 60.0,
) -> T:
    """Read the database until `probe` returns something truthy. A child that
    exits while we wait fails at once, with its logs."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while True:
        found = await probe()
        if found:
            return found
        if child is not None and child.returncode is not None:
            raise AssertionError(
                f"child {child.name} exited {child.returncode} before {what}\n{child.logs()}"
            )
        if loop.time() >= deadline:
            detail = child.logs() if child is not None else ""
            raise AssertionError(f"timed out after {within:g} s waiting for {what}\n{detail}")
        await asyncio.sleep(0.05)


# ---------------------------------------------------------------------------
# rows, as plain values
# ---------------------------------------------------------------------------


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


async def node_runs(run_id: uuid.UUID) -> list[dict[str, Any]]:
    from agent.db.models import NodeRun
    from agent.db.session import get_sessionmaker

    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                sa.select(NodeRun)
                .where(NodeRun.run_id == run_id)
                .order_by(NodeRun.node_id, NodeRun.attempt)
            )
        ).scalars()
        return [
            {
                "id": row.id,
                "node_id": row.node_id,
                "attempt": row.attempt,
                "status": row.status.value,
                "finished_at": row.finished_at,
                "output": digest(row.output),
            }
            for row in rows
        ]


def succeeded(snapshot: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {row["node_id"]: row for row in snapshot if row["status"] == "succeeded"}


def assert_untouched(
    before: dict[str, dict[str, Any]], after: list[dict[str, Any]], *, context: str
) -> None:
    """Every node that had succeeded still has exactly its one row — same id,
    same attempt, same finish time, same output. A re-execution, even one
    served from cache, would add an attempt or rewrite the row."""
    by_node: dict[str, list[dict[str, Any]]] = {}
    for row in after:
        by_node.setdefault(row["node_id"], []).append(row)
    for node_id, row in before.items():
        rows = by_node.get(node_id, [])
        assert rows == [row], f"{context}: node {node_id} was re-executed: {row} -> {rows}"


async def run_status(run_id: uuid.UUID) -> str:
    from agent.db.models import Run
    from agent.db.session import get_sessionmaker

    async with get_sessionmaker()() as session:
        status = await session.scalar(sa.select(Run.status).where(Run.id == run_id))
        assert status is not None
        return str(status.value)


async def recover_after_worker_kill(admin: Any, run_id: uuid.UUID) -> list[str]:
    """What production does after a worker dies mid-run (PRD §16): the reaper
    finds a `running` run with no heartbeat and fails it, closing the nodes it
    left in flight; an operator presses retry-failed, which re-queues it.

    The heartbeat key expires 5 minutes after the last beat; deleting it here
    stands in for that wait — the only thing the reaper reads is whether it
    exists. Returns the nodes the reaper found in flight.

    The dead worker's project lock is still held when it dies (nothing ran to
    release it); once the run is reaped, the project is free for a new run of
    the same pipeline — not only after the lock's TTL, hours later.
    """
    from agent.db.models import Run
    from agent.db.session import get_sessionmaker
    from agent.orchestrator.heartbeat import heartbeat_key
    from agent.orchestrator.state import RunLock
    from agent.redis_client import get_redis
    from agent.scheduling.reaper import reap_stale_runs

    async with get_sessionmaker()() as session:
        project_id, stage, status = (
            await session.execute(
                sa.select(Run.project_id, Run.stage, Run.status).where(Run.id == run_id)
            )
        ).one()
    assert status.value == "running", "a killed worker leaves its run running"
    redis = get_redis()
    lock = RunLock(redis, stage)
    held = await lock.holder(project_id)
    assert held is not None and held.run_id == run_id, "the dead worker held its project lock"
    await redis.delete(heartbeat_key(run_id))
    async with get_sessionmaker()() as session:
        outcome = await reap_stale_runs(session, redis)
    assert outcome.orphaned == (run_id,), outcome
    async with get_sessionmaker()() as session:
        error = await session.scalar(sa.select(Run.error).where(Run.id == run_id))
    assert error is not None and error["code"] == "reaped", error
    assert await lock.holder(project_id) is None, (
        f"the reaped run still holds the {stage.value} lock"
    )
    retried = await admin.post(f"/runs/{run_id}/retry-failed")
    assert retried.status_code == 202, retried.text
    return list(error.get("nodes_in_flight") or [])


@dataclass(frozen=True)
class Outcome:
    """A child executor's `ExecutionResult`, in the shape `past_h3` reads."""

    status: Any
    awaiting: tuple[str, ...]
    #: Model requests per node that made them.
    requests: dict[str, int]
    #: Model requests per `node:SchemaTitle`.
    schemas: dict[str, int]
    error: Any

    @classmethod
    def of(cls, child: Child) -> Outcome:
        from agent.db.models import RunStatus

        raw = child.result()
        return cls(
            status=RunStatus(raw["status"]),
            awaiting=tuple(raw["awaiting"]),
            requests=dict(raw["requests"]),
            schemas=dict(raw["schemas"]),
            error=raw["error"],
        )


def asked(requests: dict[str, int], nodes: Iterator[str] | set[str]) -> dict[str, int]:
    """The model requests `requests` attributes to any of `nodes`."""
    wanted = set(nodes)
    return {node: n for node, n in requests.items() if node in wanted and n}
