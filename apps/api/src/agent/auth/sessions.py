"""Server-side sessions in Redis. No JWTs — a session must die the instant it is revoked.

Two clocks, both from PRD §6.1.3:

* **12-hour idle window.** This is the Redis key's TTL, reset on every
  authenticated request. Expiry is therefore enforced by Redis itself, so a bug
  in this module can fail closed but never resurrect a stale session.
* **30-day outer bound.** `absolute_expires_at` is stamped at creation and never
  moved. Activity slides the idle window forward inside that bound, never past
  it, so no session outlives a month however busy its owner is.

`user_sessions:{user_id}` is a set of that user's live sids. It exists so
disabling a user can revoke every session in one round trip, and so
`GET /auth/sessions` can list them. Entries whose session key has already
expired are pruned lazily on read.
"""

from __future__ import annotations

import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Self, cast

from redis.asyncio import Redis

SESSION_KEY_PREFIX = "session:"
USER_SESSIONS_KEY_PREFIX = "user_sessions:"

IDLE_TIMEOUT = timedelta(hours=12)
ABSOLUTE_LIFETIME = timedelta(days=30)

SID_BYTES = 32  # 256 bits, per PRD §6.1.3
CSRF_BYTES = 32


def new_sid() -> str:
    return secrets.token_urlsafe(SID_BYTES)


def new_csrf_token() -> str:
    return secrets.token_urlsafe(CSRF_BYTES)


def _session_key(sid: str) -> str:
    return f"{SESSION_KEY_PREFIX}{sid}"


def _user_key(user_id: uuid.UUID) -> str:
    return f"{USER_SESSIONS_KEY_PREFIX}{user_id}"


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """What the server knows about one signed-in browser.

    Deliberately holds no role or permission set. Authorization is read from the
    `user` row on every request, so a role change or a disable takes effect on
    the next call rather than whenever the session happens to be rebuilt.
    """

    sid: str
    user_id: uuid.UUID
    workspace_id: uuid.UUID
    csrf_token: str
    created_at: datetime
    last_seen_at: datetime
    absolute_expires_at: datetime
    ip: str | None
    user_agent: str | None

    def to_json(self) -> str:
        return json.dumps(
            {
                "user_id": str(self.user_id),
                "workspace_id": str(self.workspace_id),
                "csrf_token": self.csrf_token,
                "created_at": self.created_at.isoformat(),
                "last_seen_at": self.last_seen_at.isoformat(),
                "absolute_expires_at": self.absolute_expires_at.isoformat(),
                "ip": self.ip,
                "user_agent": self.user_agent,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, sid: str, payload: str) -> Self | None:
        try:
            data: dict[str, Any] = json.loads(payload)
            return cls(
                sid=sid,
                user_id=uuid.UUID(data["user_id"]),
                workspace_id=uuid.UUID(data["workspace_id"]),
                csrf_token=data["csrf_token"],
                created_at=datetime.fromisoformat(data["created_at"]),
                last_seen_at=datetime.fromisoformat(data["last_seen_at"]),
                absolute_expires_at=datetime.fromisoformat(data["absolute_expires_at"]),
                ip=data.get("ip"),
                user_agent=data.get("user_agent"),
            )
        except (ValueError, KeyError, TypeError):
            # A payload this process cannot parse is a payload it must not trust.
            return None


class SessionStore:
    """Every read and write of session state goes through here."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def create(
        self,
        *,
        user_id: uuid.UUID,
        workspace_id: uuid.UUID,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> SessionRecord:
        now = _now()
        record = SessionRecord(
            sid=new_sid(),
            user_id=user_id,
            workspace_id=workspace_id,
            csrf_token=new_csrf_token(),
            created_at=now,
            last_seen_at=now,
            absolute_expires_at=now + ABSOLUTE_LIFETIME,
            ip=ip,
            user_agent=user_agent,
        )
        pipe = self._redis.pipeline()
        pipe.set(_session_key(record.sid), record.to_json(), ex=IDLE_TIMEOUT)
        pipe.sadd(_user_key(user_id), record.sid)
        pipe.expire(_user_key(user_id), ABSOLUTE_LIFETIME)
        await pipe.execute()
        return record

    async def read(self, sid: str) -> SessionRecord | None:
        """The session, or None if it is missing, unparseable or past its outer bound."""
        raw = await self._redis.get(_session_key(sid))
        if raw is None:
            return None
        record = SessionRecord.from_json(sid, raw.decode() if isinstance(raw, bytes) else raw)
        if record is None:
            await self.revoke(sid)
            return None
        if _now() >= record.absolute_expires_at:
            await self.revoke(sid, user_id=record.user_id)
            return None
        return record

    async def touch(self, record: SessionRecord) -> SessionRecord:
        """Slide the idle window forward, never past the outer bound."""
        now = _now()
        refreshed = SessionRecord(
            sid=record.sid,
            user_id=record.user_id,
            workspace_id=record.workspace_id,
            csrf_token=record.csrf_token,
            created_at=record.created_at,
            last_seen_at=now,
            absolute_expires_at=record.absolute_expires_at,
            ip=record.ip,
            user_agent=record.user_agent,
        )
        # The key never outlives the absolute bound, so a session cannot be kept
        # alive past 30 days by a client that calls once every 11 hours.
        ttl = min(IDLE_TIMEOUT, refreshed.absolute_expires_at - now)
        if ttl.total_seconds() <= 0:
            await self.revoke(record.sid, user_id=record.user_id)
            return refreshed
        await self._redis.set(_session_key(record.sid), refreshed.to_json(), ex=ttl)
        return refreshed

    async def revoke(self, sid: str, *, user_id: uuid.UUID | None = None) -> None:
        pipe = self._redis.pipeline()
        pipe.delete(_session_key(sid))
        if user_id is not None:
            pipe.srem(_user_key(user_id), sid)
        await pipe.execute()

    async def _members(self, key: str) -> list[str]:
        """`smembers` is typed sync-or-async by redis-py; on an async client it is async."""
        raw = cast(set[bytes | str], await cast(Any, self._redis.smembers(key)))
        return [item.decode() if isinstance(item, bytes) else item for item in raw]

    async def revoke_all_for_user(self, user_id: uuid.UUID) -> int:
        """Kill every session this user has. Returns how many were live.

        Called when a user is disabled, when their role changes and when they
        change their own password — the three cases where a still-valid cookie
        would otherwise outlive the decision (PRD §6.1.7).
        """
        decoded = await self._members(_user_key(user_id))
        pipe = self._redis.pipeline()
        for sid in decoded:
            pipe.delete(_session_key(sid))
        pipe.delete(_user_key(user_id))
        results = await pipe.execute()
        return sum(1 for deleted in results[:-1] if deleted)

    async def list_for_user(self, user_id: uuid.UUID) -> list[SessionRecord]:
        """Live sessions, newest first. Prunes sids whose key has already expired."""
        decoded = await self._members(_user_key(user_id))
        if not decoded:
            return []

        pipe = self._redis.pipeline()
        for sid in decoded:
            pipe.get(_session_key(sid))
        payloads = await pipe.execute()

        records: list[SessionRecord] = []
        stale: list[str] = []
        for sid, payload in zip(decoded, payloads, strict=True):
            if payload is None:
                stale.append(sid)
                continue
            record = SessionRecord.from_json(
                sid, payload.decode() if isinstance(payload, bytes) else payload
            )
            if record is None or _now() >= record.absolute_expires_at:
                stale.append(sid)
                continue
            records.append(record)

        if stale:
            await cast(Any, self._redis.srem(_user_key(user_id), *stale))

        records.sort(key=lambda r: r.created_at, reverse=True)
        return records
