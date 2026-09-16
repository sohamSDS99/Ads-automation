"""Sealed credentials: how a secret gets into the database and back out again.

PRD §6 fixes the resolution order at call time — **user > project > workspace** —
and PRD §15 NF5 fixes everything else: AES-256-GCM, the key from the
environment, and no endpoint that returns the plaintext. Both sides of the seal
live here so a writer cannot pick a different AAD from the reader's.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.crypto import decrypt_str, encrypt
from agent.db.models import Credential, CredentialKind, CredentialScope


class MissingCredential(LookupError):
    """No credential of this kind is reachable from this project or user."""

    def __init__(self, kind: CredentialKind) -> None:
        super().__init__(
            f"no {kind.value} credential is configured for this workspace. "
            "Add one in Settings before launching a run."
        )
        self.kind = kind


def credential_aad(credential_id: uuid.UUID) -> bytes:
    """Additional authenticated data binding a ciphertext to its row.

    Without it, a ciphertext copied from one row into another would still
    decrypt; with it, the move is detected as tampering.
    """
    return str(credential_id).encode("utf-8")


def new_credential(
    *,
    workspace_id: uuid.UUID,
    kind: CredentialKind,
    secret: str,
    created_by: uuid.UUID,
    scope: CredentialScope = CredentialScope.WORKSPACE,
    project_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    meta: dict[str, str] | None = None,
) -> Credential:
    """Seal `secret` into an unsaved `Credential`. The id is minted first — it is the AAD."""
    credential_id = uuid.uuid4()
    ciphertext, nonce = encrypt(secret, aad=credential_aad(credential_id))
    return Credential(
        id=credential_id,
        workspace_id=workspace_id,
        scope=scope,
        project_id=project_id,
        user_id=user_id,
        kind=kind,
        ciphertext=ciphertext,
        nonce=nonce,
        created_by=created_by,
        meta=meta or {"last4": secret[-4:]},
    )


def open_credential(credential: Credential) -> str:
    """The plaintext secret. Never put the return value in a log or a response."""
    return decrypt_str(credential.ciphertext, credential.nonce, aad=credential_aad(credential.id))


async def resolve_secret(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    kind: CredentialKind,
    project_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
) -> str:
    """The most specific secret of `kind` visible to this caller.

    Ordering is done in SQL rather than by three round trips: `scope` is ranked
    user (0) → project (1) → workspace (2) and the first row wins.
    """
    rank = sa.case(
        (Credential.scope == CredentialScope.USER, 0),
        (Credential.scope == CredentialScope.PROJECT, 1),
        else_=2,
    )
    conditions = [
        Credential.scope == CredentialScope.WORKSPACE,
    ]
    if project_id is not None:
        conditions.append(
            sa.and_(
                Credential.scope == CredentialScope.PROJECT,
                Credential.project_id == project_id,
            )
        )
    if user_id is not None:
        conditions.append(
            sa.and_(Credential.scope == CredentialScope.USER, Credential.user_id == user_id)
        )

    stmt = (
        sa.select(Credential)
        .where(
            Credential.workspace_id == workspace_id,
            Credential.kind == kind,
            sa.or_(*conditions),
        )
        .order_by(rank, Credential.created_at.desc())
        .limit(1)
    )
    credential = (await db.execute(stmt)).scalar_one_or_none()
    if credential is None:
        raise MissingCredential(kind)
    return open_credential(credential)
