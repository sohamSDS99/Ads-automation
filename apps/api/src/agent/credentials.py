"""Which sources a workspace may use, and the values the deployment supplies.

Resolving a source is two questions now, not four. PRD §6's
*user > project > workspace* ladder described a vault holding a copy of every
key at every scope, and there is no vault: a secret lives in the deployment's
environment and nowhere else. So a run asks

1. has an administrator connected this source for this workspace
   (`SourceConnection`), and
2. does the environment actually supply its half (`KindSpec.missing_env_vars`)

and gets the values, or `MissingCredential`. Every caller already treats that
exception as "this source is not configured", so degradation (PRD §16) behaves
exactly as before — a run thins its report rather than failing, except for the
model surface, which nothing can proceed without.

Why the connection is a separate fact from the configuration: one deployment
serves several workspaces, and a key being *present* is not the same as a
workspace being *entitled to spend it*. Deleting the row is how an administrator
withdraws that without touching anyone else's deployment.

Google Ads adds a third question, because two of its five values are not the
deployment's to hold. A developer token is issued once to one manager account,
so requiring one per person means most people never connect; what each person
*can* give is their consent, and that is what mints the refresh token and names
the account. So the row carries that grant — sealed — and resolving merges the
deployment's half with the workspace's, in that order. A source whose consent
has never been given, or has been revoked, raises the same `MissingCredential`
every caller already degrades on, with a reason that names the fix.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent import google_oauth
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
        #: `not_connected`, `not_configured` or `not_authorised`. Three reasons
        #: because they have three different fixes: switch it on, set a
        #: variable, or sign in to Google.
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
    connection = await connection_for(db, workspace_id=workspace_id, kind=kind)
    if connection is None:
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
    values = spec.values(settings, grant_values(connection))
    absent = spec.missing_values(values)
    if absent:
        # Reached when a consent was revoked at Google's end, or when the
        # encryption key changed underneath a sealed grant. Both read as "sign
        # in again", which is one click, rather than as a deployment fault
        # nobody using the product can act on.
        raise MissingCredential(
            kind,
            "not_authorised",
            f"{spec.label} is connected, but nobody has signed in to Google for this "
            "workspace. Open Settings → Connections and press Connect with Google.",
        )
    return values


def grant_values(connection: SourceConnection) -> dict[str, str]:
    """The half of this connection's credential a person's consent supplied.

    Two stores, one for each kind of value. The refresh token is sealed in
    `grant_ciphertext`, because it is a secret and no endpoint may read it back.
    The customer id and the manager it is reached through are in `meta`, because
    they are exactly what the card has to *show*, and a value that must be
    displayed has no business being encrypted — it would only mean decrypting it
    on every list request and keeping two copies in step.
    """
    spec = spec_for(connection.kind)
    if spec.oauth is None:
        return {}
    values = google_oauth.unseal(
        connection.grant_ciphertext, connection.grant_nonce, connection_id=connection.id
    )
    meta = connection.meta or {}
    for field in spec.fields:
        if field.granted and not field.secret and meta.get(field.name):
            values[field.name] = str(meta[field.name])
    return values


async def connection_for(
    db: AsyncSession, *, workspace_id: uuid.UUID, kind: CredentialKind
) -> SourceConnection | None:
    """This workspace's row for one source, or None. The row is the decision."""
    return (
        await db.execute(
            sa.select(SourceConnection).where(
                SourceConnection.workspace_id == workspace_id,
                SourceConnection.kind == kind,
            )
        )
    ).scalar_one_or_none()


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
