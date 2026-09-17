"""Who else is watching this run (PRD §13.4 B).

Presence is informational: it puts faces on a console so two people do not
silently duplicate each other's decision. Nothing depends on it, which is why it
lives entirely in Redis and expires on its own.

PRD §13.4 calls it "a Redis `run:{id}:viewers` set, TTL 30s, refreshed on the
SSE heartbeat". It is a **sorted** set here, scored by the moment each viewer
last checked in, because a plain set has one TTL for the whole key — every
viewer would expire together, or nobody would. Scoring the members lets a single
viewer age out while the rest stay, which is what "TTL 30s" has to mean when it
is applied to a member rather than a key.

The refresh is a client call rather than a server-side effect of the SSE
heartbeat: that heartbeat is written by a generator that has already handed its
database session back (`routes_runs.stream_events`), and an open console must
also be able to say "still here" while a *finished* run streams nothing at all.
"""

from __future__ import annotations

import time
import uuid

import structlog
from redis.asyncio import Redis

log = structlog.get_logger(__name__)

#: How long a check-in counts for. The console refreshes every 10s, so a viewer
#: has to miss three in a row before their avatar goes.
VIEWER_TTL_SECONDS = 30

#: The key itself outlives its members by a wide margin so that a console left
#: open on a long-finished run keeps working; it is deleted by Redis, never by
#: us, the moment everyone stops checking in.
KEY_TTL_SECONDS = 10 * 60

#: A console showing every avatar of a 40-viewer run would be a wall of
#: initials. The extras are counted, not drawn — the API returns this many.
MAX_VIEWERS = 12


def viewers_key(run_id: uuid.UUID) -> str:
    return f"run:{run_id}:viewers"


class RunPresence:
    """The viewers of one run."""

    def __init__(self, redis: Redis, run_id: uuid.UUID) -> None:
        self.redis = redis
        self.run_id = run_id
        self.key = viewers_key(run_id)

    async def check_in(self, user_id: uuid.UUID, *, now: float | None = None) -> list[uuid.UUID]:
        """Record that `user_id` is watching, and return everyone who is.

        One round trip: the add, the prune and the read are pipelined, so a
        console polling every 10s costs one network hop, not three.
        """
        moment = time.time() if now is None else now
        pipe = self.redis.pipeline()
        pipe.zadd(self.key, {str(user_id): moment})
        pipe.zremrangebyscore(self.key, "-inf", moment - VIEWER_TTL_SECONDS)
        pipe.expire(self.key, KEY_TTL_SECONDS)
        pipe.zrange(self.key, 0, -1)
        *_, members = await pipe.execute()
        return _parse(members)

    async def viewers(self, *, now: float | None = None) -> list[uuid.UUID]:
        """Everyone currently watching, without claiming to be one of them."""
        moment = time.time() if now is None else now
        members = await self.redis.zrangebyscore(self.key, moment - VIEWER_TTL_SECONDS, "+inf")
        return _parse(members)

    async def check_out(self, user_id: uuid.UUID) -> None:
        """Leave immediately, rather than fading out over the next 30 seconds.

        Best effort: a closed laptop never gets here, which is what the TTL is
        for.
        """
        await self.redis.zrem(self.key, str(user_id))


def _parse(members: list[bytes | str]) -> list[uuid.UUID]:
    """Ids only. A member that is not one is dropped rather than raising.

    Nothing writes a non-uuid member today, and presence is decoration: a
    malformed entry left by some future writer should cost an avatar, not the
    console.
    """
    found: list[uuid.UUID] = []
    for member in members:
        raw = member.decode() if isinstance(member, bytes) else member
        try:
            found.append(uuid.UUID(raw))
        except ValueError:
            log.warning("presence.unparsable_member", member=raw)
    return found
