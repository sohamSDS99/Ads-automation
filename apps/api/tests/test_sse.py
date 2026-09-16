"""SSE framing, the replay cursor, and when the stream closes."""

from __future__ import annotations

import json
from typing import Any

from starlette.datastructures import Headers, QueryParams

from agent.api.sse import SSE_HEADERS, cursor_from, event_source
from agent.orchestrator.events import BEGINNING, EventType, RunEvent


class FakeRequest:
    def __init__(self, headers: dict[str, str] | None = None, query: str = "") -> None:
        self.headers = Headers(headers or {})
        self.query_params = QueryParams(query)
        self.disconnected = False

    async def is_disconnected(self) -> bool:
        return self.disconnected


class FakeStream:
    """Hands out scripted batches, then empties — the shape a heartbeat needs."""

    def __init__(self, batches: list[list[RunEvent]]) -> None:
        self.batches = batches
        self.cursors: list[str] = []

    async def read(self, *, after: str = BEGINNING, block_ms: int = 0) -> list[RunEvent]:
        self.cursors.append(after)
        return self.batches.pop(0) if self.batches else []


def event(index: int, event_type: EventType, **data: Any) -> RunEvent:
    return RunEvent(id=f"170000000{index}-0", type=event_type, data=data)


def test_the_frame_carries_id_event_and_one_json_data_line() -> None:
    frame = event(1, EventType.NODE_STARTED, node_id="0.1", attempt=1).sse()
    lines = frame.strip().split("\n")
    assert lines[0] == "id: 1700000001-0"
    assert lines[1] == "event: node.started"
    assert json.loads(lines[2].removeprefix("data: ")) == {"node_id": "0.1", "attempt": 1}
    assert frame.endswith("\n\n")


def test_the_response_headers_defeat_proxy_buffering() -> None:
    """Without `no-transform` and `X-Accel-Buffering: no` the console updates once, at the end."""
    assert SSE_HEADERS["Cache-Control"] == "no-cache, no-transform"
    assert SSE_HEADERS["X-Accel-Buffering"] == "no"


def test_a_first_connection_replays_from_the_beginning() -> None:
    """The worker starts before the browser subscribes; joining live would lose events."""
    assert cursor_from(FakeRequest()) == BEGINNING


def test_a_reconnect_resumes_from_last_event_id() -> None:
    assert cursor_from(FakeRequest({"last-event-id": "1700-5"})) == "1700-5"


def test_the_query_parameter_is_the_fallback_for_clients_that_cannot_set_headers() -> None:
    assert cursor_from(FakeRequest(query="last_event_id=1700-9")) == "1700-9"


async def test_the_stream_closes_on_the_terminal_event() -> None:
    stream = FakeStream(
        [
            [event(1, EventType.RUN_STATUS, status="running")],
            [
                event(2, EventType.NODE_COMPLETED, node_id="0.1"),
                event(3, EventType.RUN_COMPLETED, status="succeeded"),
                event(4, EventType.NODE_STARTED, node_id="0.9"),
            ],
        ]
    )
    frames = [frame async for frame in event_source(stream, FakeRequest())]

    assert len(frames) == 3, "nothing after run.completed should be delivered"
    assert "run.completed" in frames[-1]
    assert stream.cursors == [BEGINNING, "1700000001-0"]


async def test_an_empty_read_becomes_a_heartbeat_comment() -> None:
    stream = FakeStream([[], [event(1, EventType.RUN_COMPLETED, status="succeeded")]])
    frames = [frame async for frame in event_source(stream, FakeRequest())]

    assert frames[0] == ": heartbeat\n\n"
    assert "run.completed" in frames[1]


async def test_a_finished_run_drains_and_closes_without_blocking() -> None:
    stream = FakeStream([[event(1, EventType.NODE_COMPLETED, node_id="0.1")]])
    frames = [frame async for frame in event_source(stream, FakeRequest(), live=False)]

    assert len(frames) == 1, "a finished run must not hold the connection open"


async def test_a_disconnected_client_ends_the_stream() -> None:
    request = FakeRequest()
    request.disconnected = True
    stream = FakeStream([[event(1, EventType.RUN_STATUS, status="running")]])

    frames = [frame async for frame in event_source(stream, request)]
    assert frames == []
