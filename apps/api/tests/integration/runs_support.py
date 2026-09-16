"""Shared machinery for the run tests: scripted model answers and a worker call.

`execute` is deliberately the same shape as `agent.worker.execute_run` — its own
session, the shared Redis, one HTTP client for the whole run — so what these
tests exercise is the path the worker actually takes.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx

from agent.orchestrator.executor import ExecutionResult, RunExecutor
from tests.openrouter_fake import FakeOpenRouter, completion

#: Node 0.1 (`project_brief`) and node 0.2 (`brief_critique`) outputs.
BRIEF = {"summary": "sells safety data sheet management", "keywords": ["sds", "compliance"]}
CRITIQUE = {"verdict": "thin", "missing": ["pricing", "competitors"]}


def script_two_node_run(fake: FakeOpenRouter) -> None:
    """One good answer per node, in wave order."""
    fake.queue(completion(BRIEF), completion(CRITIQUE))


async def execute(
    run_id: uuid.UUID,
    fake: FakeOpenRouter,
    *,
    client: httpx.AsyncClient | None = None,
    **kwargs: Any,
) -> ExecutionResult:
    """Run the DAG the way the worker does, against the scripted provider."""
    from agent.db.session import get_sessionmaker
    from agent.redis_client import get_redis

    owned = client or fake.client()
    try:
        async with get_sessionmaker()() as session:
            executor = RunExecutor(
                db=session,
                redis=get_redis(),
                http_client=owned,
                backoff_base=0.0,
                **kwargs,
            )
            return await executor.execute(run_id)
    finally:
        if client is None:
            await owned.aclose()


async def launch(admin: Any, project_id: uuid.UUID, **body: Any) -> dict[str, Any]:
    response = await admin.post(f"/projects/{project_id}/runs", json=body or {})
    assert response.status_code == 201, response.text
    return dict(response.json())
