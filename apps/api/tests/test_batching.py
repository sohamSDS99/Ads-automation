"""`batching.py` — asking a model the same question about two thousand things.

Driven against a stub context rather than the gateway: what is under test is the
fan-out, the tolerance and the bookkeeping, and a real provider would only add
scheduling noise to assertions about which items reached which call.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import pytest
from pydantic import BaseModel

from agent.nodes import batching


class Answer(BaseModel):
    items: list[str] = []


class StubContext:
    """Everything `in_batches` touches, and nothing else."""

    def __init__(self, *, fail_batches: set[int] | None = None, delay: float = 0.0) -> None:
        self.prompts: list[str] = []
        self.progress_lines: list[str] = []
        self.fail_batches = fail_batches or set()
        self.delay = delay
        self.in_flight = 0
        self.peak_in_flight = 0

    async def complete(
        self, output_model: type[BaseModel], *, system: str, user: str, task_class: Any = None
    ) -> Any:
        index = len(self.prompts)
        self.prompts.append(user)
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if index in self.fail_batches:
                raise RuntimeError(f"batch {index} refused")
            return output_model(items=[user])
        finally:
            self.in_flight -= 1

    async def progress(self, message: str) -> None:
        self.progress_lines.append(message)


def prompt_for(batch: Sequence[Any], index: int, total: int) -> str:
    return f"{index}/{total}:" + ",".join(str(item) for item in batch)


async def run(items: Sequence[Any], ctx: StubContext, **kwargs: Any) -> Any:
    return await batching.in_batches(
        ctx,  # type: ignore[arg-type]  # a stub with the two methods it uses
        items,
        output_model=Answer,
        system="s",
        user_for=prompt_for,
        **kwargs,
    )


def test_chunking_keeps_every_item_exactly_once() -> None:
    items = list(range(10))
    batched = batching.chunks(items, 4)
    assert [len(batch) for batch in batched] == [4, 4, 2]
    assert [item for batch in batched for item in batch] == items


def test_a_zero_batch_size_is_rejected_rather_than_looping_forever() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        batching.chunks([1, 2], 0)


async def test_every_item_reaches_exactly_one_call() -> None:
    ctx = StubContext()
    outcome = await run(list(range(250)), ctx, size=100)

    assert outcome.batches == 3
    assert outcome.failed == 0
    sent = [line.split(":", 1)[1] for line in sorted(ctx.prompts)]
    assert sorted(item for line in sent for item in line.split(",")) == sorted(
        str(value) for value in range(250)
    )


async def test_each_call_is_told_which_part_of_the_job_it_is() -> None:
    ctx = StubContext()
    await run(list(range(30)), ctx, size=10)
    assert sorted(line.split(":", 1)[0] for line in ctx.prompts) == ["0/3", "1/3", "2/3"]


async def test_fan_out_is_bounded() -> None:
    ctx = StubContext(delay=0.01)
    await run(list(range(100)), ctx, size=10, concurrency=3)
    assert ctx.peak_in_flight <= 3
    assert len(ctx.prompts) == 10


async def test_nothing_to_do_makes_no_calls() -> None:
    ctx = StubContext()
    outcome = await run([], ctx, size=10)
    assert (outcome.batches, outcome.results, ctx.prompts) == (0, [], [])


async def test_one_failed_batch_in_five_is_reported_not_fatal() -> None:
    ctx = StubContext(fail_batches={2})
    outcome = await run(list(range(50)), ctx, size=10)

    assert outcome.batches == 5
    assert outcome.failed == 1
    assert len(outcome.results) == 4
    assert outcome.degraded
    assert outcome.note("intent") == "intent: 1 of 5 batches failed"


async def test_losing_too_much_of_the_job_fails_the_node() -> None:
    """A node quietly returning half its work is worse than one that fails."""
    ctx = StubContext(fail_batches={0, 1, 2})
    with pytest.raises(RuntimeError, match="refused"):
        await run(list(range(50)), ctx, size=10)


async def test_progress_counts_batches_as_they_land() -> None:
    ctx = StubContext()
    await run(list(range(30)), ctx, size=10, label="intent classification")
    assert ctx.progress_lines[-1] == "intent classification: 3/3 batches"


def test_the_first_answer_for_a_key_is_the_one_that_is_kept() -> None:
    class Row(BaseModel):
        term: str
        value: int

    found = batching.index_by([Row(term="a", value=1), Row(term="a", value=2)], "term")
    assert found["a"].value == 1


async def test_a_cancelled_batch_stops_the_node_rather_than_degrading_it() -> None:
    """A cancel must not come back as "one batch failed, within tolerance"."""

    class CancellingContext(StubContext):
        async def complete(self, output_model: type[BaseModel], **kwargs: Any) -> Any:
            index = len(self.prompts)
            self.prompts.append(kwargs["user"])
            if index == 0:
                raise asyncio.CancelledError
            return output_model(items=[])

    ctx = CancellingContext()
    with pytest.raises(asyncio.CancelledError):
        await run(list(range(50)), ctx, size=10)
