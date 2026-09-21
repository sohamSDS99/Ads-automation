"""Which sources a workspace may use, and the values the deployment supplies.

Resolving a source is two questions now, not four. PRD §6's
*user > project > workspace* ladder described a vault holding a copy of every
key at every scope, and there is no vault: a secret lives in the deployment's
environment and nowhere else. So a run asks

1. has an administrator connected this source for this workspace
   (`SourceConnection`), and
2. does the environment actually supply its values (`KindSpec.from_env`)

and gets the values, or `MissingCredential`. Every caller already treats that
exception as "this source is not configured", so degradation (PRD §16) behaves
exactly as before — a run thins its report rather than failing, except for the
model surface, which nothing can proceed without.

Why the connection is a separate fact from the configuration: one deployment
serves several workspaces, and a key being *present* is not the same as a
workspace being *entitled to spend it*. Deleting the row is how an administrator
withdraws that without touching anyone else's deployment.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.config import get_settings
from agent.credential_kinds import spec_for
from agent.db.models import CredentialKind, SourceConnection


class MissingCredential(LookupError):
    """Nothing usable supplies this source: either not connected, or not configured.

    Both cases are one exception because every caller does the same thing with
    them — skip the source and record why. `reason` is there for the callers
    that report it to a person, so the sentence can name the fix.
    """

    def __init__(self, kind: CredentialKind, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        #: `not_connected` or `not_configured`.
        self.reason = reason
        self.detail = detail


async def resolve_values(
    db: AsyncSession, *, workspace_id: uuid.UUID, kind: CredentialKind
) -> dict[str, str]:
    """This source's credential values for this workspace, or `MissingCredential`.

    The returned mapping is keyed by `FieldSpec.name`, which is the shape
    `ConnectorContext.credentials` has always expected — connectors did not
    change when the vault went away, because what reaches them is identical.
    """
    spec = spec_for(kind)
    if not await is_connected(db, workspace_id=workspace_id, kind=kind):
        raise MissingCredential(
            kind,
            "not_connected",
            f"{spec.label} is not connected for this workspace. "
            "An administrator can switch it on under Settings → Connections.",
        )
    settings = get_settings()
    missing = spec.missing_env_vars(settings)
    if missing:
        raise MissingCredential(
            kind,
            "not_configured",
            f"{spec.label} is connected, but this deployment supplies no credential for it. "
            f"Set {', '.join(missing)} in the environment.",
        )
    return spec.from_env(settings)


async def is_connected(db: AsyncSession, *, workspace_id: uuid.UUID, kind: CredentialKind) -> bool:
    """Whether this workspace has switched this source on."""
    found = (
        await db.execute(
            sa.select(SourceConnection.id).where(
                SourceConnection.workspace_id == workspace_id,
                SourceConnection.kind == kind,
            )
        )
    ).scalar_one_or_none()
    return found is not None


async def connected_kinds(db: AsyncSession, *, workspace_id: uuid.UUID) -> set[CredentialKind]:
    """Every source this workspace has switched on, in one query.

    For the callers that need the whole picture at once — project readiness asks
    about three sources and should not ask three times.
    """
    rows = (
        await db.execute(
            sa.select(SourceConnection.kind).where(SourceConnection.workspace_id == workspace_id)
        )
    ).scalars()
    return set(rows)
