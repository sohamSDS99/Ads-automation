"""The queue hop: the job the API enqueues is the job the worker runs.

Every other run test calls the executor directly, which proves the DAG but not
the wiring — a renamed function or a job that never gets registered would pass
all of them and fail in production with a run stuck at `queued` forever. This
test drains the real queue with a real arq worker.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
from arq.connections import RedisSettings
from arq.worker import Worker

from agent.db.models import NodeRunStatus, RunStatus
from agent.llm.gateway import LLMGateway, RateLimiter
from agent.llm.ledger import ModelCatalogue
from agent.queue import EXECUTE_RUN
from agent.worker import WorkerSettings
from tests.integration.conftest import REAL_REDIS_URL, ApiClient
from tests.integration.runs_support import launch, script_two_node_run
from tests.openrouter_fake import FakeOpenRouter


def patched_gateway(
    client: httpx.AsyncClient,
) -> Any:
    """Stand in for `build_gateway` so the worker talks to the fake, not to OpenRouter."""

    def build(*, api_key: str, settings: Any, client_: Any = None, **_: Any) -> Any:
        catalogue = ModelCatalogue(client, base_url=settings.openrouter_base_url, api_key=api_key)
        gateway = LLMGateway(
            client=client,
            api_key=api_key,
            catalogue=catalogue,
            base_url=settings.openrouter_base_url,
            limiter=RateLimiter(rate=10_000, concurrency=8),
        )
        return gateway, client

    return build


def test_the_worker_registers_the_function_the_api_enqueues() -> None:
    assert [function.__name__ for function in WorkerSettings.functions] == [EXECUTE_RUN]
    # arq's default job timeout is 300s; a full run is allowed 45 minutes.
    assert WorkerSettings.job_timeout >= 45 * 60
    # The executor owns retries. A job-level retry would re-enter a live run.
    assert WorkerSettings.max_tries == 1


async def test_a_launched_run_is_executed_by_a_real_arq_worker(
    admin: ApiClient, project: Any, monkeypatch: Any
) -> None:
    created = await launch(admin, project.id)

    fake = FakeOpenRouter()
    script_two_node_run(fake)
    async with fake.client() as client:
        monkeypatch.setattr("agent.orchestrator.executor.build_gateway", patched_gateway(client))
        worker = Worker(
            functions=WorkerSettings.functions,
            redis_settings=RedisSettings.from_dsn(REAL_REDIS_URL),
            burst=True,
            poll_delay=0.01,
            max_jobs=1,
            handle_signals=False,
        )
        try:
            await worker.main()
        finally:
            await worker.close()

    assert worker.jobs_complete == 1, "the enqueued job was never picked up"
    assert worker.jobs_failed == 0

    state = (await admin.get(f"/runs/{created['id']}")).json()
    assert state["status"] == RunStatus.SUCCEEDED
    assert [node["status"] for node in state["nodes"]] == [
        NodeRunStatus.SUCCEEDED,
        NodeRunStatus.SUCCEEDED,
    ]
    assert uuid.UUID(state["id"]) == uuid.UUID(created["id"])
