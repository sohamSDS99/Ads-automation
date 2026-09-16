"""The SSE feed: what the console sees, and what a reconnect gets."""

from __future__ import annotations

import json
from typing import Any

from agent.db.models import RunStatus
from tests.integration.conftest import ApiClient
from tests.integration.runs_support import (
    execute,
    launch_chain,
    script_two_node_run,
    unparseable,
)
from tests.openrouter_fake import FakeOpenRouter


def frames(body: str) -> list[dict[str, Any]]:
    """Parse an `text/event-stream` body into `{id, event, data}` dicts."""
    parsed: list[dict[str, Any]] = []
    for block in body.split("\n\n"):
        if not block.strip() or block.startswith(":"):
            continue
        fields: dict[str, Any] = {}
        for line in block.splitlines():
            key, _, value = line.partition(": ")
            fields[key] = json.loads(value) if key == "data" else value
        parsed.append(fields)
    return parsed


async def test_the_stream_replays_the_whole_run_in_order(admin: ApiClient, project: Any) -> None:
    created = await launch_chain(admin, project.id)
    fake = FakeOpenRouter()
    script_two_node_run(fake)
    await execute(created["id"], fake)

    response = await admin.get(f"/runs/{created['id']}/events")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache, no-transform"
    assert response.headers["x-accel-buffering"] == "no"

    events = frames(response.text)
    assert [event["event"] for event in events] == [
        "run.status",  # queued, written by the API before the worker sees it
        "run.status",  # running
        "node.started",
        "node.progress",  # which model and which rung of the ladder
        "node.tokens",
        "node.completed",
        "node.started",
        "node.progress",
        "node.tokens",
        "node.completed",
        "run.completed",
    ]
    assert events[3]["data"]["message"] == "google/gemini-2.5-flash · strict_schema"
    assert events[0]["data"]["status"] == RunStatus.QUEUED
    assert events[2]["data"]["node_id"] == "1.1.2"
    assert events[-1]["data"]["status"] == RunStatus.SUCCEEDED
    assert events[-1]["data"]["cost_usd"] == "0.0004"


async def test_every_frame_carries_a_resumable_id(admin: ApiClient, project: Any) -> None:
    created = await launch_chain(admin, project.id)
    fake = FakeOpenRouter()
    script_two_node_run(fake)
    await execute(created["id"], fake)

    events = frames((await admin.get(f"/runs/{created['id']}/events")).text)
    ids = [event["id"] for event in events]
    assert all(ids), "an SSE frame without an id cannot be resumed from"
    assert ids == sorted(ids), "Redis stream ids are monotonic; the feed must stay ordered"


async def test_a_reconnect_receives_only_what_it_missed(admin: ApiClient, project: Any) -> None:
    created = await launch_chain(admin, project.id)
    fake = FakeOpenRouter()
    script_two_node_run(fake)
    await execute(created["id"], fake)

    everything = frames((await admin.get(f"/runs/{created['id']}/events")).text)
    cursor = everything[3]["id"]

    resumed = frames(
        (await admin.get(f"/runs/{created['id']}/events", headers={"Last-Event-ID": cursor})).text
    )
    assert [event["id"] for event in resumed] == [event["id"] for event in everything[4:]]


async def test_a_failed_node_is_reported_on_the_stream(
    admin: ApiClient, project: Any, monkeypatch: Any
) -> None:
    monkeypatch.setattr("agent.llm.gateway.BACKOFF_BASE_SECONDS", 0.0)
    created = await launch_chain(admin, project.id)
    fake = FakeOpenRouter()
    fake.always(unparseable())
    await execute(created["id"], fake)

    events = frames((await admin.get(f"/runs/{created['id']}/events")).text)
    failures = [event for event in events if event["event"] == "node.failed"]
    assert len(failures) == 3, "three attempts, each reported"
    assert [failure["data"]["will_retry"] for failure in failures] == [True, True, False]
    assert events[-1]["data"]["status"] == RunStatus.FAILED
