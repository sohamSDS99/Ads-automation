"""Law 43: BUDGET IS RESERVED BEFORE SPEND (PRD §9.3, §23.1 item 6).

Two caps bind a creative run: `max_creative_cost_usd` (text + media) and
`max_media_cost_usd` (media alone). Before every media submit the job's
estimate is **reserved** in Redis by one Lua script that reads what the run has
spent and reserved and checks both caps before it adds anything — so any
number of workers reserving at once cannot jointly overshoot either. On
completion the reservation is **reconciled** to `usage.cost`; on a failure it
is **released**.

Amounts are integer micro-dollars inside Redis, so the script's arithmetic is
exact (a float `HINCRBYFLOAT` drifts in the last place, and a cap check is a
comparison). A reservation rounds *up* to the micro-dollar.

Reservations are per job and idempotent: a resumed worker that reserves,
reconciles or releases a job a second time changes nothing. `reserve` takes
the job id for that reason — `reconcile(job_id)` and `release(job_id)` have to
find it.

Redis is the atomic guard, not the record: `GenerationJob.cost_usd` is. If
Redis loses its state, the caller passes the committed media spend as
`media_spent_floor_usd` and the script never counts below it. The text half of
the creative cap is `Run.cost_usd`, passed in as `text_spent_usd`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from agent.media.types import Modality

MICROS = Decimal(1_000_000)
#: Longer than any run holds a reservation; refreshed on every write.
KEY_TTL_SECONDS = 7 * 24 * 3600

_RUN = "media:budget:run:"
_JOB = "media:budget:job:"

# KEYS[1] run hash {spent, reserved}   KEYS[2] job hash {run, micros, modality, state}
# ARGV: micros, text_micros, cap_creative, cap_media, floor, modality, run_id, ttl
_RESERVE = """
local state = redis.call('HGET', KEYS[2], 'state')
if state == 'reserved' or state == 'reconciled' then return 1 end
local spent = tonumber(redis.call('HGET', KEYS[1], 'spent') or '0')
local floor = tonumber(ARGV[5])
if floor > spent then
  spent = floor
  redis.call('HSET', KEYS[1], 'spent', floor)
end
local reserved = tonumber(redis.call('HGET', KEYS[1], 'reserved') or '0')
local usd = tonumber(ARGV[1])
if spent + reserved + usd > tonumber(ARGV[4]) then return 0 end
if tonumber(ARGV[2]) + spent + reserved + usd > tonumber(ARGV[3]) then return 0 end
redis.call('HINCRBY', KEYS[1], 'reserved', usd)
redis.call('HSET', KEYS[2], 'run', ARGV[7], 'micros', usd, 'modality', ARGV[6], 'state', 'reserved')
redis.call('EXPIRE', KEYS[1], ARGV[8])
redis.call('EXPIRE', KEYS[2], ARGV[8])
return 1
"""

# KEYS[1] job hash   ARGV: actual micros, run key prefix
_RECONCILE = """
local state = redis.call('HGET', KEYS[1], 'state')
if (not state) or state == 'reconciled' then return 0 end
local run = ARGV[2] .. redis.call('HGET', KEYS[1], 'run')
if state == 'reserved' then
  redis.call('HINCRBY', run, 'reserved', -tonumber(redis.call('HGET', KEYS[1], 'micros')))
end
redis.call('HINCRBY', run, 'spent', tonumber(ARGV[1]))
redis.call('HSET', KEYS[1], 'state', 'reconciled', 'actual', ARGV[1])
return 1
"""

# KEYS[1] job hash   ARGV: run key prefix
_RELEASE = """
if redis.call('HGET', KEYS[1], 'state') ~= 'reserved' then return 0 end
local run = ARGV[1] .. redis.call('HGET', KEYS[1], 'run')
redis.call('HINCRBY', run, 'reserved', -tonumber(redis.call('HGET', KEYS[1], 'micros')))
redis.call('HSET', KEYS[1], 'state', 'released')
return 1
"""


@dataclass(frozen=True, slots=True)
class BudgetCaps:
    max_creative_cost_usd: Decimal
    max_media_cost_usd: Decimal


@dataclass(frozen=True, slots=True)
class BudgetState:
    spent_usd: Decimal
    reserved_usd: Decimal


@dataclass(frozen=True, slots=True)
class SpendLine:
    spent_usd: Decimal
    reserved_usd: Decimal
    cap_usd: Decimal


@dataclass(frozen=True, slots=True)
class RunSpend:
    """Both caps as the console draws them (PRD §15.4 C): spent and reserved,
    against each cap — the three numbers `_RESERVE` compares before it grants
    anything, so the meter and the guard cannot disagree about "how close"."""

    total: SpendLine
    media: SpendLine


def text_spend(run_cost_usd: Decimal | None, media_committed_usd: Decimal) -> Decimal:
    """The text half of the creative cap. `Run.cost_usd` is the run's ledger
    and already counts committed media, which is not counted twice."""
    return max(Decimal(0), Decimal(run_cost_usd or 0) - media_committed_usd)


def run_spend(
    *,
    caps: BudgetCaps,
    run_cost_usd: Decimal | None,
    media_committed_usd: Decimal,
    state: BudgetState,
) -> RunSpend:
    # The script never counts media below what is committed (`floor`), so
    # neither does the meter: a Redis flush must not show a cap reset.
    media = max(state.spent_usd, media_committed_usd)
    text = text_spend(run_cost_usd, media_committed_usd)
    return RunSpend(
        total=SpendLine(
            spent_usd=text + media,
            reserved_usd=state.reserved_usd,
            cap_usd=caps.max_creative_cost_usd,
        ),
        media=SpendLine(
            spent_usd=media, reserved_usd=state.reserved_usd, cap_usd=caps.max_media_cost_usd
        ),
    )


def resolve_media_caps(
    *,
    project_settings: dict[str, Any] | None,
    workspace_settings: dict[str, Any] | None,
    defaults: Any,
) -> BudgetCaps:
    """Both caps, narrowest scope first: project, then workspace, then the
    environment — the rule `orchestrator.budget.resolve_cost_cap` applies to
    every other stage's cap, applied to the two keys Law 43 names."""

    def cap(key: str) -> Decimal:
        for settings in (project_settings, workspace_settings):
            raw = (settings or {}).get(key)
            if raw is None:
                continue
            try:
                return Decimal(str(raw))
            except (InvalidOperation, ValueError):
                continue
        return Decimal(str(getattr(defaults, key)))

    return BudgetCaps(
        max_creative_cost_usd=cap("max_creative_cost_usd"),
        max_media_cost_usd=cap("max_media_cost_usd"),
    )


class MediaBudget:
    def __init__(self, redis: Any) -> None:
        self._redis = redis
        self._reserve = redis.register_script(_RESERVE)
        self._reconcile = redis.register_script(_RECONCILE)
        self._release = redis.register_script(_RELEASE)

    async def reserve(
        self,
        run_id: uuid.UUID,
        modality: Modality,
        usd: Decimal,
        *,
        job_id: uuid.UUID,
        caps: BudgetCaps,
        text_spent_usd: Decimal = Decimal(0),
        media_spent_floor_usd: Decimal = Decimal(0),
    ) -> bool:
        """Reserve `usd` for `job_id` if both caps still hold. False = refused."""
        granted = await self._reserve(
            keys=[f"{_RUN}{run_id}", f"{_JOB}{job_id}"],
            args=[
                _micros(usd, up=True),
                _micros(text_spent_usd),
                _micros(caps.max_creative_cost_usd),
                _micros(caps.max_media_cost_usd),
                _micros(media_spent_floor_usd),
                modality,
                str(run_id),
                KEY_TTL_SECONDS,
            ],
        )
        return bool(granted)

    async def reconcile(self, job_id: uuid.UUID, actual_usd: Decimal) -> bool:
        """Replace the job's reservation with what it cost. False if it was
        already reconciled, or was never reserved."""
        return bool(
            await self._reconcile(keys=[f"{_JOB}{job_id}"], args=[_micros(actual_usd), _RUN])
        )

    async def release(self, job_id: uuid.UUID) -> bool:
        """Give the reservation back. False if there was none outstanding."""
        return bool(await self._release(keys=[f"{_JOB}{job_id}"], args=[_RUN]))

    async def state(self, run_id: uuid.UUID) -> BudgetState:
        raw = await self._redis.hgetall(f"{_RUN}{run_id}")
        values = {_text(key): int(value) for key, value in raw.items()}
        return BudgetState(
            spent_usd=Decimal(values.get("spent", 0)) / MICROS,
            reserved_usd=Decimal(values.get("reserved", 0)) / MICROS,
        )


def _micros(usd: Decimal, *, up: bool = False) -> int:
    rounding = ROUND_CEILING if up else ROUND_HALF_UP
    return int((Decimal(usd) * MICROS).to_integral_value(rounding=rounding))


def _text(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value
