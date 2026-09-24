"""The two Redis calls the catalogue cache makes, with a clock the test owns.

Only the catalogue's unit tests use this — its logic is "fresh for 10 minutes,
last good for 24 hours", which a controllable clock proves faster and more
exactly than sleeping against a real server. Anything atomic (the budget
script, the semaphores) is tested against real Redis in `tests/integration/`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


class Clock:
    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 9, 24, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


class FakeRedis:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._data: dict[str, tuple[bytes, datetime | None]] = {}

    async def get(self, key: str) -> bytes | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        value, expires = entry
        if expires is not None and self._clock() >= expires:
            del self._data[key]
            return None
        return value

    async def set(self, key: str, value: bytes | str, ex: int | None = None) -> bool:
        raw = value.encode() if isinstance(value, str) else value
        expires = self._clock() + timedelta(seconds=ex) if ex is not None else None
        self._data[key] = (raw, expires)
        return True

    def keys(self) -> list[str]:
        return sorted(self._data)
