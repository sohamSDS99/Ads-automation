"""The run event bus (PRD §7.3).

Events are written to a **Redis Stream** per run, not published on a pub/sub
channel. Pub/sub drops anything sent while nobody is listening, and PRD §7.3
requires the opposite: the console reconnects with `Last-Event-ID` and expects
to receive what it missed. A stream entry id is monotonic and opaque, which is
exactly what an SSE `id:` field needs.

The other reason is a race that pub/sub cannot avoid: the worker starts the run
the moment the API enqueues it, and a browser that subscribes a beat later would
lose the first events. A reader with no cursor starts at `0-0` and replays the
run from the beginning, so there is no window to lose.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import structlog
from redis.asyncio import Redis

log = structlog.get_logger(__name__)

#: Entries kept per run. A 21-node run emits a few hundred; the cap is a
#: backstop against a node that streams progress in a loop.
MAX_EVENTS = 5000

#: Events outlive the run so a console opened later can still replay it. The
#: durable record is Postgres; this is the live feed.
EVENT_TTL_SECONDS = 7 * 24 * 60 * 60

#: The cursor a reader uses when it has never seen this run.
BEGINNING = "0-0"


class EventType(StrEnum):
    """The eight event types of PRD §7.3, plus `export.ready` from §12.

    §7.3 enumerates eight and §12 then says the client may "listen on the run
    SSE channel for `export.ready`" — the two sections disagree, and §12 is the
    one describing a feature, so the ninth is here.

    It is the only event that can be published after `run.completed`, which
    matters to any reader that treats the terminal event as end-of-stream: an
    export requested an hour after the run finished publishes into a feed
    nobody is holding open. `GET /exports/{job_id}` is the reliable answer, and
    this event is the fast one.
    """

    RUN_STATUS = "run.status"
    NODE_STARTED = "node.started"
    NODE_PROGRESS = "node.progress"
    NODE_TOKENS = "node.tokens"
    NODE_COMPLETED = "node.completed"
    NODE_FAILED = "node.failed"
    APPROVAL_REQUIRED = "approval.required"
    #: A named person must act. Distinct from `approval.required` so the
    #: console can render "assigned to you" rather than "awaiting approval".
    HUMAN_TASK_REQUIRED = "human_task.required"
    RUN_COMPLETED = "run.completed"
    EXPORT_READY = "export.ready"


#: After this, the stream is over and an SSE reader may close.
TERMINAL = EventType.RUN_COMPLETED


@dataclass(frozen=True, slots=True)
class RunEvent:
    """One entry, as delivered to the browser."""

    id: str
    type: EventType
    data: dict[str, Any]

    def sse(self) -> str:
        """The wire format: id, event name, one JSON data line, blank line."""
        payload = json.dumps(self.data, default=str, separators=(",", ":"))
        return f"id: {self.id}\nevent: {self.type.value}\ndata: {payload}\n\n"


def stream_key(run_id: uuid.UUID) -> str:
    return f"run:{run_id}:events"


class RunEventStream:
    """Append-only event log for one run, readable from any process."""

    def __init__(self, redis: Redis, run_id: uuid.UUID) -> None:
        self._redis = redis
        self._run_id = run_id
        self.key = stream_key(run_id)

    async def publish(self, event_type: EventType, **data: Any) -> str:
        entry_id = await self._redis.xadd(
            self.key,
            {
                "type": event_type.value,
                "data": json.dumps(data, default=str, separators=(",", ":")),
            },
            maxlen=MAX_EVENTS,
            approximate=True,
        )
        await self._redis.expire(self.key, EVENT_TTL_SECONDS)
        log.debug("run.event", run_id=str(self._run_id), type=event_type.value)
        return _text(entry_id)

    async def read(
        self, *, after: str = BEGINNING, block_ms: int = 0, count: int = 200
    ) -> list[RunEvent]:
        """Entries after `after`. With `block_ms`, waits that long for the first one."""
        if block_ms:
            raw = await self._redis.xread({self.key: after}, count=count, block=block_ms)
        else:
            raw = await self._redis.xread({self.key: after}, count=count)
        if not raw:
            return []
        events: list[RunEvent] = []
        for _key, entries in raw:
            for entry_id, fields in entries:
                events.append(_decode(entry_id, fields))
        return events

    async def drop(self) -> None:
        """Forget this run's feed. Used by tests; a run's record lives in Postgres."""
        await self._redis.delete(self.key)


def _decode(entry_id: Any, fields: dict[Any, Any]) -> RunEvent:
    decoded = {_text(key): _text(value) for key, value in fields.items()}
    raw_type = decoded.get("type", "")
    try:
        event_type = EventType(raw_type)
    except ValueError:  # pragma: no cover — only reachable across a schema change
        log.warning("run.unknown_event_type", type=raw_type)
        event_type = EventType.RUN_STATUS
    try:
        data = json.loads(decoded.get("data") or "{}")
    except json.JSONDecodeError:  # pragma: no cover
        data = {}
    return RunEvent(id=_text(entry_id), type=event_type, data=data)


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)
