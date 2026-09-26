"""pytest plugin (`-p tests.integration.s4p24_pinned_inputs`): pin what a run
takes from outside its `CreativeInput` and its cassette, so two processes can
be compared byte for byte (PRD §17 CC9).

A run takes two such inputs:

**Row ids.** Every table's primary key defaults to `uuid.uuid4`, which the
models capture at import. The plugin is loaded with `-p`, so it runs before any
conftest imports `agent`, and replaces `uuid.uuid4` with a draw from the
calling task's own stream. One process-wide stream would not be enough: a wave
runs its nodes concurrently, so which node draws next depends on I/O timing,
and the same ids would land on different rows. Instead, every asyncio task gets
a stream derived from its parent's stream and its creation index under that
parent. A task creates its children in program order, so a task's ids are a
function of its place in the call tree, never of the scheduler.

**Decision times.** `decided_at` is written by the approvals layer
(`orchestrator.approvals.utcnow`) and by the exceptions routes
(`datetime.now`). Both are pinned to a clock that advances one second per read.
Every other clock is left alone: offer freshness and claim expiry read the
real time, and so do the rows the fixtures seed.

The seed is `S4P24_UUID_SEED`, 0 by default.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import os
import random
import uuid
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

SEED = int(os.environ.get("S4P24_UUID_SEED", "0"))


class _Stream:
    def __init__(self, key: str) -> None:
        self.key = key
        digest = hashlib.sha256(f"{SEED}:{key}".encode()).digest()
        self._random = random.Random(int.from_bytes(digest[:8], "big"))
        self._children = 0

    def uuid(self) -> uuid.UUID:
        return uuid.UUID(int=self._random.getrandbits(128), version=4)

    def child(self) -> _Stream:
        self._children += 1
        return _Stream(f"{self.key}/{self._children}")


_MAIN = _Stream("main")
_STREAM: contextvars.ContextVar[_Stream | None] = contextvars.ContextVar(
    "s4p24_uuid_stream", default=None
)


def _current() -> _Stream:
    return _STREAM.get() or _MAIN


def _pinned_uuid4() -> uuid.UUID:
    return _current().uuid()


uuid.uuid4 = _pinned_uuid4

_create_task = asyncio.BaseEventLoop.create_task


def _forking_create_task(
    self: asyncio.BaseEventLoop,
    coro: Coroutine[Any, Any, Any],
    *,
    name: str | None = None,
    context: contextvars.Context | None = None,
    **rest: Any,
) -> asyncio.Task[Any]:
    if context is None:
        context = contextvars.copy_context()
        context.run(_STREAM.set, _current().child())
    return _create_task(self, coro, name=name, context=context, **rest)


asyncio.BaseEventLoop.create_task = _forking_create_task  # type: ignore[method-assign]


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


_CLOCK = _Clock()


class _PinnedDatetime(datetime):
    @classmethod
    def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
        return _CLOCK()


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    from agent.api import routes_creative_exceptions
    from agent.orchestrator import approvals

    approvals.utcnow = _CLOCK  # type: ignore[assignment]
    routes_creative_exceptions.datetime = _PinnedDatetime  # type: ignore[misc]
