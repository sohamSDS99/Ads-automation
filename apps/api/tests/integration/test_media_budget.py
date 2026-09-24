"""Law 43 — BUDGET IS RESERVED BEFORE SPEND — against real Redis.

The reservation is one Lua script that reads spent + reserved and checks
**both** caps before it adds anything, so fifty workers reserving at once can
never jointly overshoot either one. A fake Redis would make this trivially
true (it is single-threaded in-process); the point is the real server.
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import pytest

from agent.media.budget import BudgetCaps, MediaBudget
from agent.media.semaphore import MediaSemaphore, SemaphoreTimeout
from agent.redis_client import get_redis

CAPS = BudgetCaps(max_creative_cost_usd=Decimal("50.00"), max_media_cost_usd=Decimal("40.00"))


def budget() -> MediaBudget:
    return MediaBudget(get_redis())


async def _fifty(run_id: uuid.UUID, *, text_spent: Decimal, usd: Decimal) -> list[bool]:
    ledger = budget()
    return list(
        await asyncio.gather(
            *[
                ledger.reserve(
                    run_id, "image", usd, job_id=uuid.uuid4(), caps=CAPS, text_spent_usd=text_spent
                )
                for _ in range(50)
            ]
        )
    )


async def test_fifty_concurrent_reservations_never_pass_the_media_cap() -> None:
    run_id = uuid.uuid4()

    granted = await _fifty(run_id, text_spent=Decimal(0), usd=Decimal("1.00"))

    state = await budget().state(run_id)
    assert granted.count(True) == 40
    assert state.reserved_usd == Decimal("40.00") and state.spent_usd == Decimal(0)


async def test_fifty_concurrent_reservations_never_pass_the_creative_cap() -> None:
    run_id = uuid.uuid4()

    # $15 of text already spent: text + media may not pass $50, so 35 fit.
    granted = await _fifty(run_id, text_spent=Decimal("15.00"), usd=Decimal("1.00"))

    assert granted.count(True) == 35
    assert (await budget().state(run_id)).reserved_usd == Decimal("35.00")


async def test_uneven_concurrent_reservations_stay_under_both_caps() -> None:
    run_id = uuid.uuid4()
    ledger = budget()
    amounts = [Decimal(f"{(i % 7) + 0.37:.2f}") for i in range(50)]

    granted = await asyncio.gather(
        *[
            ledger.reserve(
                run_id, "video", usd, job_id=uuid.uuid4(), caps=CAPS, text_spent_usd=Decimal("12.5")
            )
            for usd in amounts
        ]
    )

    state = await ledger.state(run_id)
    assert state.reserved_usd == sum(a for a, ok in zip(amounts, granted, strict=True) if ok)
    assert state.reserved_usd <= CAPS.max_media_cost_usd
    assert Decimal("12.5") + state.reserved_usd <= CAPS.max_creative_cost_usd
    # Something was refused, or the caps were never in play.
    assert not all(granted)


async def test_a_reservation_is_idempotent_per_job() -> None:
    run_id, job_id = uuid.uuid4(), uuid.uuid4()
    ledger = budget()

    first = await ledger.reserve(run_id, "video", Decimal("3"), job_id=job_id, caps=CAPS)
    again = await ledger.reserve(run_id, "video", Decimal("3"), job_id=job_id, caps=CAPS)

    assert first and again
    assert (await ledger.state(run_id)).reserved_usd == Decimal("3")


async def test_reconcile_moves_the_reservation_to_the_actual_cost_once() -> None:
    run_id, job_id = uuid.uuid4(), uuid.uuid4()
    ledger = budget()
    await ledger.reserve(run_id, "video", Decimal("0.10"), job_id=job_id, caps=CAPS)

    assert await ledger.reconcile(job_id, Decimal("0.2125"))
    assert not await ledger.reconcile(job_id, Decimal("0.2125"))  # a resumed worker repeats it
    await ledger.release(job_id)  # and a release after reconcile changes nothing

    state = await ledger.state(run_id)
    assert (state.reserved_usd, state.spent_usd) == (Decimal(0), Decimal("0.2125"))


async def test_release_frees_a_reservation_and_is_idempotent() -> None:
    run_id, job_id = uuid.uuid4(), uuid.uuid4()
    ledger = budget()
    await ledger.reserve(run_id, "image", Decimal("39.99"), job_id=job_id, caps=CAPS)

    assert not await ledger.reserve(run_id, "image", Decimal("1"), job_id=uuid.uuid4(), caps=CAPS)
    assert await ledger.release(job_id)
    assert not await ledger.release(job_id)
    assert await ledger.reserve(run_id, "image", Decimal("1"), job_id=uuid.uuid4(), caps=CAPS)


async def test_committed_spend_is_a_floor_when_redis_lost_its_state() -> None:
    run_id = uuid.uuid4()
    ledger = budget()

    # Redis restarted: its counters are gone, but $39.50 of media is committed
    # on GenerationJob rows. The floor keeps a $1 job from fitting.
    refused = await ledger.reserve(
        run_id,
        "image",
        Decimal("1"),
        job_id=uuid.uuid4(),
        caps=CAPS,
        media_spent_floor_usd=Decimal("39.50"),
    )
    fits = await ledger.reserve(
        run_id,
        "image",
        Decimal("0.50"),
        job_id=uuid.uuid4(),
        caps=CAPS,
        media_spent_floor_usd=Decimal("39.50"),
    )

    assert (refused, fits) == (False, True)


async def test_a_reservation_rounds_up_to_the_micro_dollar() -> None:
    run_id = uuid.uuid4()
    ledger = budget()

    await ledger.reserve(run_id, "image", Decimal("0.0000001"), job_id=uuid.uuid4(), caps=CAPS)

    assert (await ledger.state(run_id)).reserved_usd == Decimal("0.000001")


# ---------------------------------------------------------------------------
# semaphores: media:image (4), media:video (2)
# ---------------------------------------------------------------------------


async def test_a_semaphore_never_admits_more_than_its_limit() -> None:
    semaphore = MediaSemaphore(get_redis(), "video", limit=2, lease_seconds=30, poll_seconds=0.01)
    inside = 0
    peak = 0

    async def work() -> None:
        nonlocal inside, peak
        async with semaphore.hold(timeout_seconds=10):
            inside += 1
            peak = max(peak, inside)
            await asyncio.sleep(0.05)
            inside -= 1

    await asyncio.gather(*[work() for _ in range(7)])

    assert peak == 2


async def test_a_dead_holders_slot_frees_when_its_lease_expires() -> None:
    semaphore = MediaSemaphore(get_redis(), "image", limit=1, lease_seconds=1, poll_seconds=0.05)
    # A holder that was kill -9'd never releases: take the slot and walk away.
    assert await semaphore.acquire() is not None

    with pytest.raises(SemaphoreTimeout):
        async with semaphore.hold(timeout_seconds=0.3):
            pass
    async with semaphore.hold(timeout_seconds=5):
        pass
