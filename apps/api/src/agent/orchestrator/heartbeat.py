"""Proof that a worker is still inside a run.

PRD §16: "Worker killed → run marked `failed` by a startup reaper (heartbeat >
5 min stale); resumable from last checkpoint." This is the heartbeat half. The
reaper is in `scheduling/reaper.py`.

It is a **background task**, not a beat at each node boundary, and that choice
is the whole design. Node boundaries are minutes apart on this DAG — the
Transparency Center connector alone can hold one node open for longer than the
staleness window — so a boundary heartbeat would have the reaper killing healthy
runs. A task that ticks on a timer stops the instant the process does, which is
the event being detected, and keeps ticking through a node that is merely slow.

Absence *is* the signal: the key carries a TTL and the reaper asks whether it
exists. Redis owns the clock, so a worker and an API container with skewed
clocks cannot disagree about whether a run is alive.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import UTC, datetime
from types import TracebackType

import structlog
from redis.asyncio import Redis

log = structlog.get_logger(__name__)

#: PRD §16's threshold. A run whose key has expired has been silent this long.
STALE_AFTER_SECONDS = 5 * 60

#: Tick an order of magnitude faster than the window, so a missed tick — a
#: garbage-collection pause, a slow Redis round trip — is never mistaken for a
#: dead process.
BEAT_INTERVAL_SECONDS = 30


def heartbeat_key(run_id: uuid.UUID) -> str:
    return f"run:{run_id}:heartbeat"


class RunHeartbeat:
    """Writes `run:{id}:heartbeat` every 30s for as long as it is held.

    Used as an async context manager around the body of `execute()`::

        async with RunHeartbeat(redis, run_id):
            ...

    **Leaving the block stops beating but does not delete the key.** Deleting it
    is tidier and is a race: `execute()` still has to roll up the ledger, skip
    unreached nodes, write the terminal status and expire open gates *after* the
    wave loop ends, and during those round trips the run is still `running` in
    Postgres. A reaper tick landing in that window would find a running run with
    no heartbeat and fail a run that was seconds from succeeding.

    Letting the key expire on its own costs nothing: the reaper only ever looks
    at runs whose stored status is `running` or `queued`, so a lingering key on
    a finished run is never consulted, and a worker that is killed leaves no
    beat either way. `clear()` is still available for a caller that genuinely
    wants the key gone — the tests use it.
    """

    def __init__(
        self,
        redis: Redis,
        run_id: uuid.UUID,
        *,
        interval: float = BEAT_INTERVAL_SECONDS,
        ttl: int = STALE_AFTER_SECONDS,
    ) -> None:
        self._redis = redis
        self._run_id = run_id
        self._interval = interval
        self._ttl = ttl
        self._task: asyncio.Task[None] | None = None

    async def beat(self) -> None:
        """One tick. Public so the reaper's tests can stand a run up without a task."""
        await self._redis.set(
            heartbeat_key(self._run_id),
            datetime.now(UTC).isoformat(),
            ex=self._ttl,
        )

    async def clear(self) -> None:
        await self._redis.delete(heartbeat_key(self._run_id))

    async def __aenter__(self) -> RunHeartbeat:
        # Beat once synchronously before returning. Otherwise a run that is
        # killed in its first second never wrote a key at all, and looks to the
        # reaper exactly like one that was never picked up.
        await self.beat()
        self._task = asyncio.create_task(self._loop(), name=f"heartbeat:{self._run_id}")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self.beat()
            except Exception as exc:  # noqa: BLE001 — a lost beat is not a lost run
                # Logged, not raised: Redis being briefly unreachable should not
                # take down a 40-minute run. If it stays unreachable the key
                # expires and the reaper does its job, which is correct.
                log.warning("run.heartbeat_failed", run_id=str(self._run_id), error=str(exc))


async def is_alive(redis: Redis, run_id: uuid.UUID) -> bool:
    """Whether a worker has beaten for this run within the staleness window."""
    return bool(await redis.exists(heartbeat_key(run_id)))
