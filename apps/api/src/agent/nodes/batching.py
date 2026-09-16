"""Asking a model the same question about 2,000 things.

PRD §10 1.4.2 says intent classification is "batched 100/call", and node 1.3.2
has the same shape over a creative corpus. Both need the same three things, so
they are written once here:

* **Bounded fan-out.** 20 batches issued at once would be 20 concurrent
  OpenRouter calls from one node, inside a wave that is already running four
  nodes. `CONCURRENCY` keeps a single node's share of the provider polite.
* **A partial answer beats no answer.** One batch failing after the gateway's
  own retries and repair pass should not throw away the other nineteen — but a
  node that quietly returns a fifth of its work is worse than one that fails. So
  failures are tolerated up to a share and fatal past it, and the count comes
  back either way for the node to report.
* **Nothing is silently dropped.** `BatchOutcome` carries how many batches ran
  and how many failed. A node that does not surface that is a node presenting
  1,600 classified terms as if they were the 2,000 it was given.

Every call still goes through `ctx.complete()`, so batching changes nothing
about cost accounting, routing or the prompt record.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import structlog
from pydantic import BaseModel

from agent.llm.router import TaskClass
from agent.nodes.base import RunContext

log = structlog.get_logger(__name__)

#: Items per call. PRD §10 1.4.2's number, and the default for everything else
#: because a batch large enough to be cheap is also large enough that one
#: rejected response costs real work.
BATCH_SIZE = 100

#: Concurrent calls from one node.
CONCURRENCY = 4

#: The share of batches that may fail before the node does. A fifth of a
#: keyword list is a degraded answer worth reporting; half of it is not an
#: answer.
TOLERATE = 0.2


@dataclass(frozen=True, slots=True)
class BatchOutcome[T: BaseModel]:
    """What came back, and what did not."""

    results: list[T]
    batches: int
    failed: int

    @property
    def degraded(self) -> bool:
        return self.failed > 0

    def note(self, label: str) -> str:
        """A line a node can put straight into its `coverage` list."""
        return f"{label}: {self.failed} of {self.batches} batches failed"


def chunks[I](items: Sequence[I], size: int) -> list[Sequence[I]]:
    """Split into runs of `size`, the last one short."""
    if size < 1:
        raise ValueError("batch size must be at least 1")
    return [items[start : start + size] for start in range(0, len(items), size)]


async def in_batches[T: BaseModel, I](
    ctx: RunContext,
    items: Sequence[I],
    *,
    output_model: type[T],
    system: str,
    user_for: Callable[[Sequence[I], int, int], str],
    size: int = BATCH_SIZE,
    concurrency: int = CONCURRENCY,
    task_class: TaskClass | None = None,
    tolerate: float = TOLERATE,
    label: str = "batch",
) -> BatchOutcome[T]:
    """Run one completion per batch of `items`, concurrently and in order.

    `user_for(batch, index, total)` builds the prompt for one batch; the index
    and total are passed so a prompt can say "part 3 of 20", which measurably
    stops a model from re-answering the whole job from the fragment it can see.
    """
    batched = chunks(items, size)
    if not batched:
        return BatchOutcome(results=[], batches=0, failed=0)

    gate = asyncio.Semaphore(concurrency)
    done = 0
    total = len(batched)

    async def one(batch: Sequence[I], index: int) -> T:
        nonlocal done
        async with gate:
            result = await ctx.complete(
                output_model,
                system=system,
                user=user_for(batch, index, total),
                task_class=task_class,
            )
        done += 1
        await ctx.progress(f"{label}: {done}/{total} batches")
        return result

    settled = await asyncio.gather(
        *(one(batch, index) for index, batch in enumerate(batched)),
        return_exceptions=True,
    )

    results: list[T] = []
    failures: list[Exception] = []
    for outcome in settled:
        if isinstance(outcome, BaseException) and not isinstance(outcome, Exception):
            # `return_exceptions=True` captures `CancelledError` too, and
            # counting a cancelled run as "one batch failed, within tolerance"
            # would let a cancel produce a cheerful partial result instead of
            # stopping. Anything that is not an ordinary Exception propagates.
            raise outcome
        if isinstance(outcome, Exception):
            failures.append(outcome)
        else:
            results.append(outcome)

    if failures:
        log.warning(
            "batching.partial",
            label=label,
            failed=len(failures),
            batches=total,
            first_error=str(failures[0])[:300],
        )
    # Strictly greater: a single failure out of five is exactly 0.2 and is the
    # case this tolerance exists for. Raising the original exception rather than
    # a summary keeps the executor's error classification intact.
    if len(failures) > tolerate * total:
        raise failures[0]

    return BatchOutcome(results=results, batches=total, failed=len(failures))


def index_by(rows: Sequence[Any], key: str) -> dict[str, Any]:
    """Model answers keyed by the field that ties them back to the input.

    Later duplicates lose: a model that answers the same term twice across two
    batches gets its first answer kept, which is the one whose batch carried the
    term.
    """
    found: dict[str, Any] = {}
    for row in rows:
        value = getattr(row, key, None)
        if isinstance(value, str) and value and value not in found:
            found[value] = row
    return found
