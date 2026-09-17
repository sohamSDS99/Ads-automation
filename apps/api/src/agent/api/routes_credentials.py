"""The key vault: store, list, test, delete (PRD §14, §15 NF5).

Two rules shape every route here.

A secret goes in and never comes out. There is no read endpoint, no "reveal",
and no update — replacing a credential means writing a new one, so a partial
edit can never leave half of an old key in place.

Scope decides who may write. A workspace or project credential is spending the
organisation's money and needs `credential_write`; a user-scoped one is the
caller's own personal key (PRD §13.4 H) and needs only a session — but it is
always written for *themselves*, never on someone else's behalf, or a run's
spend would be attributed to a person who never supplied a key.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

import httpx
import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from agent.api import problems
from agent.api.middleware import client_ip
from agent.api.schemas_credentials import (
    CreateCredentialRequest,
    CredentialFieldInfo,
    CredentialKindInfo,
    CredentialListResponse,
    CredentialSummary,
    CredentialTestResponse,
)
from agent.audit import AuditAction, AuditTarget, write_audit
from agent.auth.deps import Principal, require
from agent.auth.rbac import Permission
from agent.config import get_settings
from agent.connectors import connector_class
from agent.connectors.base import ConnectorContext, ConnectorStatus
from agent.credential_kinds import KIND_SPECS, spec_for, unseal
from agent.credentials import new_credential, open_credential
from agent.db.models import Credential, CredentialKind, CredentialScope, Project, User
from agent.db.session import get_session
from agent.llm.openrouter import OpenRouterError, probe_key

log = structlog.get_logger(__name__)

router = APIRouter(tags=["credentials"])

Db = Annotated[AsyncSession, Depends(get_session)]
AnyMember = Annotated[Principal, Depends(require(Permission.READ))]


@router.get("/credentials", response_model=CredentialListResponse, summary="Stored credentials")
async def list_credentials(me: AnyMember, db: Db) -> CredentialListResponse:
    """Masked metadata only. A personal credential is visible to its owner alone."""
    rows = (
        (
            await db.execute(
                sa.select(Credential)
                .where(
                    Credential.workspace_id == me.workspace_id,
                    sa.or_(
                        Credential.scope != CredentialScope.USER,
                        Credential.user_id == me.user.id,
                    ),
                )
                .order_by(Credential.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    names = {
        row[0]: row[1]
        for row in (
            await db.execute(
                sa.select(User.id, User.name).where(User.workspace_id == me.workspace_id)
            )
        ).all()
    }
    return CredentialListResponse(
        credentials=[
            CredentialSummary(
                id=row.id,
                kind=row.kind,
                scope=row.scope,
                project_id=row.project_id,
                user_id=row.user_id,
                meta=row.meta,
                created_at=row.created_at,
                created_by=row.created_by,
                created_by_name=names.get(row.created_by),
                last_tested_at=row.last_tested_at,
                last_test_ok=row.last_test_ok,
            )
            for row in rows
        ],
        kinds=[
            CredentialKindInfo(
                kind=spec.kind,
                label=spec.label,
                description=spec.description,
                where=spec.where,
                fields=[
                    CredentialFieldInfo(
                        name=field.name,
                        label=field.label,
                        required=field.required,
                        secret=field.secret,
                        hint=field.hint,
                    )
                    for field in spec.fields
                ],
            )
            for spec in KIND_SPECS.values()
        ],
    )


@router.post(
    "/credentials",
    response_model=CredentialSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Store a credential",
)
async def create_credential(
    body: CreateCredentialRequest,
    me: AnyMember,
    request: Request,
    db: Db,
) -> CredentialSummary:
    spec = spec_for(body.kind)
    _assert_may_write(me, body.scope)

    try:
        values = spec.validate(dict(body.values))
    except ValueError as exc:
        raise problems.unprocessable(
            str(exc), fields=[field.name for field in spec.fields]
        ) from exc

    if body.project_id is not None and not await _project_exists(db, me, body.project_id):
        raise problems.not_found(f"No project {body.project_id}.")

    credential = new_credential(
        workspace_id=me.workspace_id,
        kind=body.kind,
        secret=spec.seal(values),
        created_by=me.user.id,
        scope=body.scope,
        project_id=body.project_id,
        user_id=me.user.id if body.scope is CredentialScope.USER else None,
        meta=spec.meta(values),
    )
    db.add(credential)

    write_audit(
        db,
        workspace_id=me.workspace_id,
        actor_id=me.user.id,
        action=AuditAction.CREDENTIAL_CREATED,
        target_type=AuditTarget.CREDENTIAL,
        target_id=credential.id,
        # `meta` here is the same masked hint the API returns. There is no code
        # path that puts a secret in an audit row.
        meta={"kind": body.kind.value, "scope": body.scope.value, "hints": credential.meta},
        ip=client_ip(request),
    )
    await db.commit()
    await db.refresh(credential)
    log.info(
        "credential.created",
        kind=body.kind.value,
        scope=body.scope.value,
        credential_id=str(credential.id),
    )
    return CredentialSummary(
        id=credential.id,
        kind=credential.kind,
        scope=credential.scope,
        project_id=credential.project_id,
        user_id=credential.user_id,
        meta=credential.meta,
        created_at=credential.created_at,
        created_by=credential.created_by,
        created_by_name=me.user.name,
        last_tested_at=credential.last_tested_at,
        last_test_ok=credential.last_test_ok,
    )


@router.post(
    "/credentials/{credential_id}/test",
    response_model=CredentialTestResponse,
    summary="Prove a credential works",
)
async def test_credential(
    credential_id: uuid.UUID,
    me: AnyMember,
    request: Request,
    db: Db,
) -> CredentialTestResponse:
    """Make the cheapest real call the upstream offers, and record the verdict.

    A failure is a `200` with `ok=false`, not an error status: the call the user
    asked for succeeded, and the answer is that the key is wrong. Returning
    `4xx` here would make a wrong key indistinguishable from a wrong request.
    """
    credential = await _load(db, me, credential_id)
    _assert_may_write(me, credential.scope)
    spec = spec_for(credential.kind)
    values = unseal(spec, open_credential(credential))

    outcome = await _run_test(credential.kind, spec.connector, values)

    tested_at = datetime.now(UTC)
    credential.last_tested_at = tested_at
    credential.last_test_ok = outcome.ok
    if outcome.ok and outcome.meta:
        # Merge rather than replace: the test learns the account name, while
        # the write knew the last4 and the account id.
        credential.meta = {**credential.meta, **_showable(outcome.meta)}

    write_audit(
        db,
        workspace_id=me.workspace_id,
        actor_id=me.user.id,
        action=AuditAction.CREDENTIAL_TESTED,
        target_type=AuditTarget.CREDENTIAL,
        target_id=credential.id,
        meta={"kind": credential.kind.value, "ok": outcome.ok, "detail": outcome.detail},
        ip=client_ip(request),
    )
    await db.commit()
    return CredentialTestResponse(
        id=credential.id,
        kind=credential.kind,
        ok=outcome.ok,
        detail=outcome.detail,
        meta=_showable(outcome.meta),
        tested_at=tested_at,
    )


@router.delete(
    "/credentials/{credential_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a credential",
)
async def delete_credential(
    credential_id: uuid.UUID,
    me: AnyMember,
    request: Request,
    db: Db,
) -> Response:
    credential = await _load(db, me, credential_id)
    _assert_may_write(me, credential.scope)

    write_audit(
        db,
        workspace_id=me.workspace_id,
        actor_id=me.user.id,
        action=AuditAction.CREDENTIAL_DELETED,
        target_type=AuditTarget.CREDENTIAL,
        target_id=credential.id,
        meta={"kind": credential.kind.value, "scope": credential.scope.value},
        ip=client_ip(request),
    )
    await db.delete(credential)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _assert_may_write(me: Principal, scope: CredentialScope) -> None:
    """Personal keys need a session; shared ones need `credential_write`."""
    if scope is CredentialScope.USER:
        return
    if Permission.CREDENTIAL_WRITE not in me.permissions:
        raise problems.forbidden(missing_permission=Permission.CREDENTIAL_WRITE.value)


async def _load(db: AsyncSession, me: Principal, credential_id: uuid.UUID) -> Credential:
    credential = (
        await db.execute(
            sa.select(Credential).where(
                Credential.id == credential_id, Credential.workspace_id == me.workspace_id
            )
        )
    ).scalar_one_or_none()
    if credential is None:
        raise problems.not_found(f"No credential {credential_id}.")
    if credential.scope is CredentialScope.USER and credential.user_id != me.user.id:
        # Not a 403: whose personal key exists is not this caller's business.
        raise problems.not_found(f"No credential {credential_id}.")
    return credential


async def _project_exists(db: AsyncSession, me: Principal, project_id: uuid.UUID) -> bool:
    found = (
        await db.execute(
            sa.select(Project.id).where(
                Project.id == project_id, Project.workspace_id == me.workspace_id
            )
        )
    ).scalar_one_or_none()
    return found is not None


async def _run_test(
    kind: CredentialKind, connector: str | None, values: dict[str, str]
) -> ConnectorStatus:
    """Dispatch to whichever upstream can answer cheapest."""
    settings = get_settings()
    if connector is None:
        # The only kind with no connector is OpenRouter, which is not an
        # evidence source — it is the model surface every node runs through.
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0)) as client:
            try:
                status_ = await probe_key(
                    client,
                    base_url=settings.openrouter_base_url,
                    api_key=values.get("api_key", ""),
                )
            except OpenRouterError as exc:
                return ConnectorStatus(ok=False, detail=exc.detail)
            return ConnectorStatus(ok=True, detail=status_.detail, meta=status_.as_meta())

    # No client is handed over, and that is the point: a connector's transport
    # is part of the thing being tested. `serp` reaches Google through a proxy
    # that the credential itself addresses, so a plain client built here would
    # test a route the run path never takes — and pass, or fail, for the wrong
    # reason. Every connector already builds its own when the context has none,
    # and closes it (`gather._pull` relies on the same behaviour).
    try:
        instance = connector_class(connector)(
            ConnectorContext(credentials=values, settings=settings)
        )
        return await instance.test_connection()
    except Exception as exc:  # noqa: BLE001 — a broken connector must not 500 the vault
        log.warning("credential.test_failed", kind=kind.value, error=str(exc))
        return ConnectorStatus(ok=False, detail=f"The test could not be completed: {exc}")


def _showable(meta: dict[str, object]) -> dict[str, object]:
    """Drop empty hints so the interface does not render a row of blanks."""
    return {key: value for key, value in meta.items() if value not in (None, "")}
