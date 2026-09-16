"""Invite tokens — the only way an account is ever created after bootstrap.

The token is shown once, at creation, and never stored: the row holds only its
SHA-256. A database dump therefore cannot be replayed into account takeover, and
a lost link genuinely has to be reissued rather than looked up.

SHA-256 is the right primitive here even though passwords get Argon2id. This
token is 32 bytes of `secrets` output, so there is no dictionary to attack and
nothing for a slow KDF to buy; what matters is that the comparison is
constant-time and the plaintext is never at rest.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import Invite, UserRole

TOKEN_BYTES = 32
EXPIRY = timedelta(days=7)


def new_token() -> str:
    """The value that goes in the emailed link. Never persisted."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def tokens_match(token: str, token_hash: str) -> bool:
    return hmac.compare_digest(hash_token(token), token_hash)


def expires_at(*, now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)) + EXPIRY


class InviteState(StrEnum):
    """Why a token cannot be used — each one gets its own screen in the UI."""

    VALID = "valid"
    INVALID = "invalid"
    EXPIRED = "expired"
    ACCEPTED = "accepted"


def state_of(invite: Invite | None, *, now: datetime | None = None) -> InviteState:
    if invite is None:
        return InviteState.INVALID
    if invite.accepted_at is not None:
        return InviteState.ACCEPTED
    if invite.expires_at <= (now or datetime.now(UTC)):
        return InviteState.EXPIRED
    return InviteState.VALID


def build(
    *,
    workspace_id: uuid.UUID,
    email: str,
    role: UserRole,
    invited_by: uuid.UUID,
    token: str,
) -> Invite:
    return Invite(
        workspace_id=workspace_id,
        email=email,
        role=role,
        token_hash=hash_token(token),
        invited_by=invited_by,
        expires_at=expires_at(),
    )


async def find_by_token(db: AsyncSession, token: str) -> Invite | None:
    """Look an invite up by the value in the link.

    The lookup is by hash, so the query is indexable and the plaintext token
    never reaches the database — not even as a bind parameter in a slow-query
    log.
    """
    if not token:
        return None
    result = await db.execute(sa.select(Invite).where(Invite.token_hash == hash_token(token)))
    return result.scalar_one_or_none()
