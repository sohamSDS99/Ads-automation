"""S4-P24 — PRD §17 CC2's hard caps when a job bills MORE than its estimate.

CC2: "Hard caps `max_creative_cost_usd` ($50) and `max_media_cost_usd` ($40)
never exceeded — reservation precedes submit". The existing suite proves the
reservation half (`test_media_budget.py`: fifty concurrent reservations never
pass either cap; `test_media_jobs.py`: a breaching reservation blocks the job
before any HTTP). What it never asked is what happens AFTER the submit, when
`usage.cost` comes back larger than the reservation:

`media/budget.py` `_RESERVE` compares `spent + reserved + estimate` with the
caps; `_RECONCILE` then *replaces* the reservation with the actual cost —
unconditionally, because the money is already spent. So committed spend can
end above a cap by `actual − estimate` of every job in flight when it filled.

The job here is REAL: `wan__video_*` is the recorded 2026-09-24 alibaba/wan-3.0
job (2 s, 480p). Its catalogue SKU `duration_seconds_480p` $0.05 prices it at
$0.10 (`video_job_price`, the estimate 4.4.4 reserves), and OpenRouter billed
`usage.cost` $0.2125 — 2.125× (fixtures README; docs/stage-04-questions.md
S4-P1 item 2, which asks for a ruling and got none). The "never exceeded" tests
below are therefore `xfail(strict=True, raises=AssertionError)` carrying the
measured overshoot: they turn into failures the day a fix lands. Their
preconditions raise `Precondition` (not AssertionError), so a broken fixture
can never hide inside the xfail.

What the system DOES do after an overshoot is asserted as passing: every
further reservation is refused, so the overshoot is bounded by the jobs that
were already in flight — it is never compounded.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.calc.media import video_job_price
from agent.db.models import GenerationJob, GenerationStatus, Project, Run
from agent.db.session import get_sessionmaker
from agent.media.budget import BudgetCaps, MediaBudget
from agent.media.catalogue import normalise_video_models
from agent.media.constants import media_constants
from agent.media.types import CapabilityRecord, VideoRequest
from agent.redis_client import get_redis
from tests.integration.test_media_jobs import World, _approve_brief, _creative_run, choice
from tests.media.openrouter_mock import BASE, WAN, body, response, video_bytes

CONSTANTS = media_constants()
CAPS = BudgetCaps(max_creative_cost_usd=Decimal("50.00"), max_media_cost_usd=Decimal("40.00"))
WAN_JOB = str(body("wan__video_submit.json")["id"])
#: What OpenRouter billed for the recorded job.
WAN_BILLED = Decimal(str(body("wan__video_poll_completed.json")["usage"]["cost"]))
WAN_REQUEST = VideoRequest(
    model=WAN,
    prompt="slow push-in on a ceramic mug on a wooden desk, steam rising, morning light",
    duration=2,
    resolution="480p",
    aspect_ratio="16:9",
    generate_audio=False,
)


class Precondition(RuntimeError):
    """A fixture fact the threshold assertion stands on. Not an AssertionError,
    so it can never be absorbed by an `xfail(raises=AssertionError)`."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise Precondition(message)


def wan() -> CapabilityRecord:
    return next(
        record
        for record in normalise_video_models(body("videos_models.json"))
        if record.model_id == WAN
    )


def wan_estimate() -> Decimal:
    """The reservation 4.4.4 makes for this request (`n4_4_4_video_production`)."""
    params = WAN_REQUEST.model_dump(mode="json", exclude_none=True, exclude={"model", "prompt"})
    return video_job_price(wan(), params, CONSTANTS).usd


@pytest.fixture
def router() -> Iterator[respx.Router]:
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        yield mock


def mock_wan(router: respx.Router) -> dict[str, respx.Route]:
    """The recorded wan-3.0 job: submit 202 → pending → completed ($0.2125)."""
    polls = [response("wan__video_poll_pending.json"), response("wan__video_poll_completed.json")]

    def poll(_request: httpx.Request) -> httpx.Response:
        return polls.pop(0) if len(polls) > 1 else polls[0]

    return {
        "submit": router.post(f"{BASE}/videos").mock(
            return_value=response("wan__video_submit.json")
        ),
        "poll": router.get(f"{BASE}/videos/{WAN_JOB}").mock(side_effect=poll),
        "content": router.get(f"{BASE}/videos/{WAN_JOB}/content").mock(
            return_value=httpx.Response(
                200, content=video_bytes(), headers={"content-type": "video/mp4"}
            )
        ),
    }


@pytest.fixture
async def world(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    tmp_path: Path,
) -> World:
    run_id = await _creative_run(db, workspace_id, project_id, admin_user.id)
    await _approve_brief(db, workspace_id, project_id, run_id)
    # The shipped defaults, spelled out on the project so the test does not
    # depend on the environment's.
    project = await db.get(Project, project_id)
    assert project is not None
    project.settings = {
        **(project.settings or {}),
        "max_creative_cost_usd": str(CAPS.max_creative_cost_usd),
        "max_media_cost_usd": str(CAPS.max_media_cost_usd),
    }
    await db.commit()
    return World(tmp_path, run_id)


async def already_spent(run_id: uuid.UUID, usd: Decimal) -> None:
    """Media this run committed before the job under test (earlier jobs,
    reserved and reconciled at their estimate)."""
    ledger = MediaBudget(get_redis())
    earlier = uuid.uuid4()
    require(
        await ledger.reserve(run_id, "image", usd, job_id=earlier, caps=CAPS),
        f"the earlier ${usd} did not fit",
    )
    require(await ledger.reconcile(earlier, usd), "the earlier job did not reconcile")


async def set_text_spend(run_id: uuid.UUID, usd: Decimal) -> None:
    async with get_sessionmaker()() as session:
        await session.execute(sa.update(Run).where(Run.id == run_id).values(cost_usd=usd))
        await session.commit()


async def submit_wan(world: World, *, round: int = 1) -> GenerationJob:
    jobs = world.jobs()
    job = await jobs.submit_or_resume(
        run_id=world.run_id,
        node_id="4.4.4",
        asset_id=None,
        round=round,
        request=WAN_REQUEST,
        choice=choice(wan()),
        estimate_usd=wan_estimate(),
    )
    if job.openrouter_job_id is not None:
        job = await jobs.await_video(job.id)
    return job


def test_the_recorded_wan_job_is_under_estimated_by_its_catalogue_price() -> None:
    """The premise of every test below, measured: estimate $0.10, billed $0.2125."""
    assert wan_estimate() == Decimal("0.10")
    assert Decimal("0.2125") == WAN_BILLED


# ---------------------------------------------------------------------------
# CC2: "never exceeded" — measured NOT MET
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="CC2 NOT MET: measured committed media $40.1125 against max_media_cost_usd $40.00 "
    "(overshoot $0.1125): the recorded wan-3.0 job reserved its $0.10 estimate into the last "
    "$0.10 of headroom and reconcile booked the $0.2125 it billed. A reservation is the "
    "catalogue estimate and reconcile cannot refuse money already spent; the fix (reserve "
    "estimate x observed per-model ratio, or a safety margin) awaits a ruling "
    "(docs/stage-04-questions.md S4-P1 item 2).",
)
async def test_cc2_media_cap_holds_when_a_job_bills_more_than_its_estimate(
    router: respx.Router, world: World
) -> None:
    routes = mock_wan(router)
    await already_spent(world.run_id, CAPS.max_media_cost_usd - wan_estimate())  # $39.90

    job = await submit_wan(world)

    require(routes["submit"].call_count == 1, "the job was not submitted")
    require(job.status == GenerationStatus.COMPLETED, f"job ended {job.status}")
    require(job.cost_usd == WAN_BILLED, f"billed {job.cost_usd}")
    state = await MediaBudget(get_redis()).state(world.run_id)
    require(state.reserved_usd == 0, f"reservation left behind: {state.reserved_usd}")
    assert state.spent_usd <= CAPS.max_media_cost_usd, (
        f"committed media ${state.spent_usd} > cap ${CAPS.max_media_cost_usd} "
        f"(overshoot ${state.spent_usd - CAPS.max_media_cost_usd})"
    )


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="CC2 NOT MET: measured text $10.20 + media $39.9125 = $50.1125 against "
    "max_creative_cost_usd $50.00 (overshoot $0.1125), media itself under its $40 cap: the "
    "same under-estimated wan-3.0 job, this time filling the creative cap's headroom.",
)
async def test_cc2_creative_cap_holds_when_a_job_bills_more_than_its_estimate(
    router: respx.Router, world: World
) -> None:
    mock_wan(router)
    text = Decimal("10.20")
    await set_text_spend(world.run_id, text)
    # 10.20 text + 39.70 media + the 0.10 estimate = exactly the $50 creative cap,
    # while media (39.80) stays under its own $40.
    await already_spent(world.run_id, CAPS.max_creative_cost_usd - text - wan_estimate())

    job = await submit_wan(world)

    require(job.status == GenerationStatus.COMPLETED, f"job ended {job.status}")
    state = await MediaBudget(get_redis()).state(world.run_id)
    require(state.spent_usd <= CAPS.max_media_cost_usd, "the media cap bound first")
    total = text + state.spent_usd
    assert total <= CAPS.max_creative_cost_usd, (
        f"text ${text} + media ${state.spent_usd} = ${total} > cap "
        f"${CAPS.max_creative_cost_usd} (overshoot ${total - CAPS.max_creative_cost_usd})"
    )


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="CC2 NOT MET: measured worst case $85.00 committed against max_media_cost_usd "
    "$40.00 (overshoot $45.00 = 112.5% of the cap): 400 concurrent jobs of the recorded "
    "wan-3.0 shape all fit at their $0.10 estimate ($40.00 reserved, the cap exactly) and each "
    "reconciled at the $0.2125 it billed. The bound is (actual/estimate - 1) x cap for the "
    "worst-billing model in flight.",
)
async def test_cc2_worst_case_media_overshoot_at_the_recorded_wan_billing_ratio() -> None:
    run_id = uuid.uuid4()
    ledger = MediaBudget(get_redis())
    estimate = wan_estimate()
    jobs = [uuid.uuid4() for _ in range(int(CAPS.max_media_cost_usd / estimate) + 50)]  # 450

    granted = await asyncio.gather(
        *[ledger.reserve(run_id, "video", estimate, job_id=job, caps=CAPS) for job in jobs]
    )
    reserved = await ledger.state(run_id)
    require(reserved.reserved_usd == CAPS.max_media_cost_usd, f"{reserved}")
    require(granted.count(True) == 400, f"{granted.count(True)} granted")
    await asyncio.gather(
        *[ledger.reconcile(job, WAN_BILLED) for job, ok in zip(jobs, granted, strict=True) if ok]
    )

    state = await ledger.state(run_id)
    require(state.reserved_usd == 0, f"{state}")
    assert state.spent_usd <= CAPS.max_media_cost_usd, (
        f"committed ${state.spent_usd} > cap ${CAPS.max_media_cost_usd} "
        f"(overshoot ${state.spent_usd - CAPS.max_media_cost_usd})"
    )


# ---------------------------------------------------------------------------
# what the system DOES do — passing
# ---------------------------------------------------------------------------


async def test_cc2_after_an_overshoot_the_next_job_is_blocked_before_any_http(
    router: respx.Router, world: World
) -> None:
    """Reconcile can carry spend past a cap once; it can never be compounded:
    with spent > cap, `_RESERVE` refuses every further estimate, however small,
    and the job ends `blocked_by_budget` before a request leaves the process."""
    routes = mock_wan(router)
    await already_spent(world.run_id, CAPS.max_media_cost_usd - wan_estimate())
    first = await submit_wan(world)
    over = (await MediaBudget(get_redis()).state(world.run_id)).spent_usd
    posts = routes["submit"].call_count

    second = await submit_wan(world, round=2)

    assert first.status == GenerationStatus.COMPLETED
    assert over == Decimal("40.1125")
    assert second.status == GenerationStatus.BLOCKED_BY_BUDGET
    assert second.error is not None and second.error["code"] == "estimate_exceeds_cap"
    assert routes["submit"].call_count == posts == 1
    assert (await MediaBudget(get_redis()).state(world.run_id)).spent_usd == over


async def test_cc3_each_job_row_keeps_the_estimate_and_the_actual_per_model(
    router: respx.Router, world: World
) -> None:
    """CC3's raw material, the part that exists: every `GenerationJob` row keeps
    the model, the estimate it reserved and the `usage.cost` it was billed, so
    `|actual − estimate| / estimate` per model is computable from the rows.
    (Nothing computes or shows it — "tracked in Settings" is not built.)"""
    mock_wan(router)

    await submit_wan(world)

    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                sa.select(
                    GenerationJob.model_id, GenerationJob.estimate_usd, GenerationJob.cost_usd
                )
            )
        ).one()
    assert row.model_id == WAN
    assert (row.estimate_usd, row.cost_usd) == (Decimal("0.1000"), Decimal("0.2125"))
    assert abs(row.cost_usd - row.estimate_usd) / row.estimate_usd == Decimal("1.125")
