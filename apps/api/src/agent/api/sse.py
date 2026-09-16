"""Server-Sent Events for the run console (PRD §7.3, §5.2 rule 5).

Three details are load-bearing and easy to lose:

* **`no-transform` and `X-Accel-Buffering: no`.** Railway's edge and any proxy
  in front of it will happily buffer a `text/event-stream` into uselessness
  otherwise, and the symptom is a console that updates once, at the end.
* **A 15s heartbeat comment.** Silent connections get idled out; a comment line
  costs three bytes and is ignored by `EventSource`.
* **Replay from `Last-Event-ID`.** The cursor is a Redis stream id, so a
  reconnecting browser receives exactly what it missed — and a first connection
  (no header) replays the run from its first event rather than joining live,
  which is what closes the race against a worker that started before the
  browser subscribed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import structlog
from starlette.requests import Request
from starlette.responses import StreamingResponse

from agent.orchestrator.events import BEGINNING, TERMINAL, RunEventStream

log = structlog.get_logger(__name__)

MEDIA_TYPE = "text/event-stream"
HEARTBEAT_SECONDS = 15

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


def cursor_from(request: Request) -> str:
    """Where to resume: the `Last-Event-ID` header, its query fallback, or the start.

    `EventSource` sends the header automatically on reconnect but cannot set one
    on the first request, so the query parameter exists for clients that track
    the cursor themselves.
    """
    header = request.headers.get("last-event-id")
    query = request.query_params.get("last_event_id")
    return header or query or BEGINNING


async def event_source(
    stream: RunEventStream,
    request: Request,
    *,
    after: str = BEGINNING,
    live: bool = True,
    heartbeat_seconds: int = HEARTBEAT_SECONDS,
) -> AsyncIterator[str]:
    """Yield SSE frames until the run completes or the client goes away.

    `live=False` drains what is already recorded and closes — the right shape
    for a run that finished before anyone opened the console.
    """
    cursor = after
    while True:
        if await request.is_disconnected():
            return

        events = await stream.read(after=cursor, block_ms=heartbeat_seconds * 1000 if live else 0)
        if not events:
            if not live:
                return
            yield ": heartbeat\n\n"
            continue

        for event in events:
            cursor = event.id
            yield event.sse()
            if event.type is TERMINAL:
                return


def sse_response(
    stream: RunEventStream,
    request: Request,
    *,
    after: str = BEGINNING,
    live: bool = True,
) -> StreamingResponse:
    return StreamingResponse(
        event_source(stream, request, after=after, live=live),
        media_type=MEDIA_TYPE,
        headers=SSE_HEADERS,
    )
