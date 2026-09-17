"""What ends a session, and what does not.

The default is that signing out is the only thing that does. That is a departure
from PRD §6.1.3's 12-hour idle window and 30-day outer bound, and the reason it
is safe to make is asserted here rather than argued: the revocations that
actually protect the workspace never ran on those clocks.

The Redis double is deliberately dumb — it records the `ex` argument and nothing
else. The decision under test is "what TTL, if any, does this write carry", and
a real Redis would only be able to confirm it by sleeping for twelve hours.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from agent.api.middleware import csrf_cookie_kwargs, session_cookie_kwargs
from agent.auth.sessions import SessionRecord, SessionStore

TEST_KEY = "dW5pdC10ZXN0LWtleS0zMi1ieXRlcy1leGFjdGx5ISE="
USER = uuid.uuid4()
WORKSPACE = uuid.uuid4()


class FakePipeline:
    def __init__(self, redis: "FakeRedis") -> None:
        self._redis = redis
        self._queued: list[tuple[str, tuple[Any, ...]]] = []

    def set(self, key: str, value: str, ex: Any = None) -> "FakePipeline":
        self._queued.append(("set", (key, value, ex)))
        return self

    def sadd(self, key: str, member: str) -> "FakePipeline":
        self._queued.append(("sadd", (key, member)))
        return self

    def expire(self, key: str, ttl: Any) -> "FakePipeline":
        self._queued.append(("expire", (key, ttl)))
        return self

    def persist(self, key: str) -> "FakePipeline":
        self._queued.append(("persist", (key,)))
        return self

    def delete(self, key: str) -> "FakePipeline":
        self._queued.append(("delete", (key,)))
        return self

    def srem(self, key: str, member: str) -> "FakePipeline":
        self._queued.append(("srem", (key, member)))
        return self

    async def execute(self) -> list[Any]:
        results: list[Any] = []
        for name, args in self._queued:
            self._redis.calls.append((name, args))
            if name == "set":
                key, value, ex = args
                self._redis.values[key] = value
                self._redis.expiries[key] = ex
            elif name == "sadd":
                self._redis.sets.setdefault(args[0], set()).add(args[1])
            elif name == "expire":
                self._redis.expiries[args[0]] = args[1]
            elif name == "persist":
                self._redis.expiries[args[0]] = None
            elif name == "delete":
                results.append(1 if self._redis.values.pop(args[0], None) else 0)
                continue
            results.append(True)
        self._queued.clear()
        return results


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expiries: dict[str, Any] = {}
        self.sets: dict[str, set[str]] = {}
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def pipeline(self) -> FakePipeline:
        return FakePipeline(self)

    async def set(self, key: str, value: str, ex: Any = None) -> bool:
        self.calls.append(("set", (key, value, ex)))
        self.values[key] = value
        self.expiries[key] = ex
        return True

    async def get(self, key: str) -> str | None:
        return self.values.get(key)


def store(**overrides: Any) -> tuple[SessionStore, FakeRedis]:
    from agent.config import Settings

    redis = FakeRedis()
    settings = Settings(app_encryption_key=TEST_KEY, **overrides)
    return SessionStore(redis, settings), redis


def ttl_of(redis: FakeRedis, sid: str) -> Any:
    return redis.expiries[f"session:{sid}"]


# ---------------------------------------------------------------------------
# the default: only signing out ends it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_new_session_carries_no_expiry_at_all() -> None:
    """The twelve-hour window signed the workspace's only admin out overnight."""
    sessions, redis = store()

    record = await sessions.create(user_id=USER, workspace_id=WORKSPACE)

    assert ttl_of(redis, record.sid) is None
    assert record.absolute_expires_at is None


@pytest.mark.asyncio
async def test_using_the_session_does_not_give_it_one() -> None:
    sessions, redis = store()
    record = await sessions.create(user_id=USER, workspace_id=WORKSPACE)

    refreshed = await sessions.touch(record)

    assert ttl_of(redis, record.sid) is None
    assert refreshed.absolute_expires_at is None
    assert refreshed.last_seen_at >= record.last_seen_at


@pytest.mark.asyncio
async def test_the_user_index_outlives_nothing_it_names() -> None:
    """`revoke_all_for_user` walks this set.

    If it expired while the sessions it names did not, disabling a user would
    silently leave them signed in — the one failure that would make turning the
    clocks off actually dangerous.
    """
    sessions, redis = store()
    await sessions.create(user_id=USER, workspace_id=WORKSPACE)

    assert redis.expiries[f"user_sessions:{USER}"] is None
    assert ("persist", (f"user_sessions:{USER}",)) in redis.calls


@pytest.mark.asyncio
async def test_a_session_that_is_read_back_is_still_live() -> None:
    sessions, _ = store()
    record = await sessions.create(user_id=USER, workspace_id=WORKSPACE)

    assert await sessions.read(record.sid) is not None


# ---------------------------------------------------------------------------
# the clocks still work when a deployment wants them
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_idle_window_is_applied_when_one_is_configured() -> None:
    sessions, redis = store(session_idle_timeout_hours=12)

    record = await sessions.create(user_id=USER, workspace_id=WORKSPACE)

    assert ttl_of(redis, record.sid) == timedelta(hours=12)
    assert record.absolute_expires_at is None, "an idle window is not an outer bound"


@pytest.mark.asyncio
async def test_an_outer_bound_is_stamped_when_one_is_configured() -> None:
    sessions, redis = store(session_absolute_lifetime_days=30)

    record = await sessions.create(user_id=USER, workspace_id=WORKSPACE)

    assert record.absolute_expires_at is not None
    assert record.absolute_expires_at - record.created_at == timedelta(days=30)
    assert redis.expiries[f"user_sessions:{USER}"] == timedelta(days=30)


@pytest.mark.asyncio
async def test_the_idle_window_never_slides_past_the_outer_bound() -> None:
    """PRD §6.1.3's actual invariant, and it still holds when both are set."""
    sessions, redis = store(session_idle_timeout_hours=12, session_absolute_lifetime_days=30)
    record = await sessions.create(user_id=USER, workspace_id=WORKSPACE)

    nearly_over = SessionRecord(
        sid=record.sid,
        user_id=record.user_id,
        workspace_id=record.workspace_id,
        csrf_token=record.csrf_token,
        created_at=record.created_at,
        last_seen_at=record.last_seen_at,
        absolute_expires_at=datetime.now(UTC) + timedelta(hours=2),
        ip=None,
        user_agent=None,
    )
    await sessions.touch(nearly_over)

    ttl = ttl_of(redis, record.sid)
    assert ttl <= timedelta(hours=2), "two hours left on the bound, not twelve on the window"


@pytest.mark.asyncio
async def test_a_session_past_its_outer_bound_is_revoked_on_read() -> None:
    sessions, redis = store(session_absolute_lifetime_days=30)
    record = await sessions.create(user_id=USER, workspace_id=WORKSPACE)
    expired = SessionRecord(
        sid=record.sid,
        user_id=record.user_id,
        workspace_id=record.workspace_id,
        csrf_token=record.csrf_token,
        created_at=record.created_at,
        last_seen_at=record.last_seen_at,
        absolute_expires_at=datetime.now(UTC) - timedelta(seconds=1),
        ip=None,
        user_agent=None,
    )
    redis.values[f"session:{record.sid}"] = expired.to_json()

    assert await sessions.read(record.sid) is None


# ---------------------------------------------------------------------------
# upgrading an install that already has sessions in Redis
# ---------------------------------------------------------------------------


def test_a_session_written_before_the_bound_became_optional_still_parses() -> None:
    """Otherwise the deploy that removes the clocks logs everyone out on the way."""
    stamped = SessionRecord(
        sid="sid",
        user_id=USER,
        workspace_id=WORKSPACE,
        csrf_token="csrf",
        created_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
        absolute_expires_at=datetime.now(UTC) + timedelta(days=30),
        ip=None,
        user_agent=None,
    )
    read_back = SessionRecord.from_json("sid", stamped.to_json())

    assert read_back is not None
    assert read_back.absolute_expires_at == stamped.absolute_expires_at


def test_a_session_with_no_bound_round_trips_as_none() -> None:
    unbounded = SessionRecord(
        sid="sid",
        user_id=USER,
        workspace_id=WORKSPACE,
        csrf_token="csrf",
        created_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
        absolute_expires_at=None,
        ip=None,
        user_agent=None,
    )
    read_back = SessionRecord.from_json("sid", unbounded.to_json())

    assert read_back is not None
    assert read_back.absolute_expires_at is None


# ---------------------------------------------------------------------------
# the cookies
# ---------------------------------------------------------------------------


def test_the_two_cookies_die_together() -> None:
    """They used to differ — session 30 days, CSRF 12 hours.

    A browser holding a live session and an expired CSRF token gets a 403 on
    every write and no way to recover but signing out, which looked like the
    app being broken rather than a cookie having lapsed.
    """
    from agent.config import Settings

    settings = Settings(app_encryption_key=TEST_KEY)

    assert csrf_cookie_kwargs(settings)["max_age"] == session_cookie_kwargs(settings)["max_age"]


def test_the_cookie_lasts_as_long_as_a_browser_will_keep_one() -> None:
    """Chrome clamps any cookie past 400 days, so that is the ceiling."""
    from agent.config import Settings

    settings = Settings(app_encryption_key=TEST_KEY)

    assert session_cookie_kwargs(settings)["max_age"] == 400 * 24 * 60 * 60
    assert session_cookie_kwargs(settings)["httponly"] is True
