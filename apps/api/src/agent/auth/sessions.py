"""Server-side sessions in Redis. No JWTs — a session must die the instant it is revoked.

**Signing out is the only thing that ends a session.** That is the default, and
it is a deliberate departure from PRD §6.1.3, which specified two clocks:

* a **12-hour idle window**, the Redis key's TTL, reset on every authenticated
  request;
* a **30-day outer bound**, stamped at creation and never moved.

Both are now configuration (`session_idle_timeout_hours`,
`session_absolute_lifetime_days`) and both default to off. The idle window was
the one that hurt: it signed the workspace's only admin out overnight, every
night, and bought nothing — this is a single-workspace internal tool behind
invite-only accounts, not a shared terminal.

What it did *not* buy is worth stating, because it is the reason turning it off
is safe. None of the revocations that actually protect the workspace ran on
those clocks: disabling a user, changing their role and changing their password
all call `revoke_all_for_user` and take effect on the next request (PRD §6.1.7),
and authorization is re-read from the `user` row on every call rather than
cached in the session. A timer expiring was never what stopped a bad actor; it
only ever stopped a good one from staying signed in.

Set either value to a positive number to put the PRD's clocks back, per
deployment, without a code change.

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

from agent.config import Settings, get_settings

SESSION_KEY_PREFIX = "session:"
USER_SESSIONS_KEY_PREFIX = "user_sessions:"

SID_BYTES = 32  # 256 bits, per PRD §6.1.3
CSRF_BYTES = 32


def idle_timeout(settings: Settings) -> timedelta | None:
    """How long a session may sit unused, or None when it may sit forever."""
    hours = settings.session_idle_timeout_hours
    return timedelta(hours=hours) if hours > 0 else None


def absolute_lifetime(settings: Settings) -> timedelta | None:
    """The outer bound on a session, or None when there is not one."""
    days = settings.session_absolute_lifetime_days
    return timedelta(days=days) if days > 0 else None


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
    #: None when the deployment sets no outer bound, which is the default. It is
    #: `None` rather than a date far in the future so that `/auth/sessions` can
    #: say "until you sign out" instead of printing a year nobody meant.
    absolute_expires_at: datetime | None
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
                "absolute_expires_at": (
                    self.absolute_expires_at.isoformat() if self.absolute_expires_at else None
                ),
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
                # `.get`, not `[...]`: a session written before the outer bound
                # became optional carries a date, one written after may carry
                # null, and neither must log its owner out on the upgrade.
                absolute_expires_at=(
                    datetime.fromisoformat(data["absolute_expires_at"])
                    if data.get("absolute_expires_at")
                    else None
                ),
                ip=data.get("ip"),
                user_agent=data.get("user_agent"),
            )
        except (ValueError, KeyError, TypeError):
            # A payload this process cannot parse is a payload it must not trust.
            return None


class SessionStore:
    """Every read and write of session state goes through here."""

    def __init__(self, redis: Redis, settings: Settings | None = None) -> None:
        self._redis = redis
        self._settings = settings or get_settings()

    @property
    def idle_timeout(self) -> timedelta | None:
        return idle_timeout(self._settings)

    @property
    def absolute_lifetime(self) -> timedelta | None:
        return absolute_lifetime(self._settings)

    async def create(
        self,
        *,
        user_id: uuid.UUID,
        workspace_id: uuid.UUID,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> SessionRecord:
        now = _now()
        bound = self.absolute_lifetime
        record = SessionRecord(
            sid=new_sid(),
            user_id=user_id,
            workspace_id=workspace_id,
            csrf_token=new_csrf_token(),
            created_at=now,
            last_seen_at=now,
            absolute_expires_at=now + bound if bound else None,
            ip=ip,
            user_agent=user_agent,
        )
        pipe = self._redis.pipeline()
        # No `ex` at all when there is no idle window: a key written with
        # `ex=None` through the keyword would raise, and one written with a TTL
        # of zero would be gone before the response reached the browser.
        idle = self.idle_timeout
        if idle is not None:
            pipe.set(_session_key(record.sid), record.to_json(), ex=idle)
        else:
            pipe.set(_session_key(record.sid), record.to_json())
        pipe.sadd(_user_key(user_id), record.sid)
        if bound is not None:
            pipe.expire(_user_key(user_id), bound)
        else:
            # An index that expires while the sessions it names do not would
            # silently break "disable this user" — the revocation walks this set.
            pipe.persist(_user_key(user_id))
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
        if record.absolute_expires_at is not None and _now() >= record.absolute_expires_at:
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
        # alive past it by a client that calls just inside the idle window.
        idle = self.idle_timeout
        remaining = (
            refreshed.absolute_expires_at - now
            if refreshed.absolute_expires_at is not None
            else None
        )
        candidates = [window for window in (idle, remaining) if window is not None]
        if not candidates:
            # Neither clock is set, so the key carries no expiry at all — this
            # is the default, and the whole point of it: only signing out, or a
            # revocation, ends the session.
            await self._redis.set(_session_key(record.sid), refreshed.to_json())
            return refreshed

        ttl = min(candidates)
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
            expired = record is not None and (
                record.absolute_expires_at is not None and _now() >= record.absolute_expires_at
            )
            if record is None or expired:
                stale.append(sid)
                continue
            records.append(record)

        if stale:
            await cast(Any, self._redis.srem(_user_key(user_id), *stale))

        records.sort(key=lambda r: r.created_at, reverse=True)
        return records
