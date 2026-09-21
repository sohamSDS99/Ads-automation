"""Connections: which sources this workspace uses (PRD §14, §15 NF5).

Three rules shape every route here.

**No secret ever crosses this boundary — in either direction.** There is no
endpoint that returns a key, and now none that accepts one. A source is
switched on by name; its values are read from the deployment's environment. The
only credential-shaped thing in a response is the *name* of a variable and the
last four characters of what a successful test found there.

**The row is the decision.** `POST …/connect` creates it, `DELETE …` removes it,
and there is no third state to get out of step with anything. Connecting a
source that is already connected re-runs its test and leaves the original
decision — and the name of whoever made it — intact.

**Connecting proves itself.** The click is one click, so the test that used to
be a separate step rides along with it: connect stores the decision, makes the
cheapest real call the upstream offers, and records the verdict. A failed test
does not undo the connection — an upstream that is briefly down is not a reason
to strand an administrator — it is reported on the card as "not working", with
the upstream's own sentence.
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
from agent.api.schemas_connections import (
    ConnectionListResponse,
    ConnectionTestResponse,
    SourceSummary,
)
from agent.audit import AuditAction, AuditTarget, write_audit
from agent.auth.deps import Principal, require
from agent.auth.rbac import Permission
from agent.config import Settings, get_settings
from agent.connectors import connector_class
from agent.connectors.base import ConnectorContext, ConnectorStatus
from agent.connectors.proxy import probe as proxy_probe
from agent.credential_kinds import KIND_SPECS, KindSpec, spec_for
from agent.db.models import CredentialKind, SourceConnection
from agent.db.repos import UserRepo
from agent.db.session import get_session
from agent.llm.openrouter import OpenRouterError, probe_key

log = structlog.get_logger(__name__)

router = APIRouter(tags=["connections"])

Db = Annotated[AsyncSession, Depends(get_session)]
AnyMember = Annotated[Principal, Depends(require(Permission.READ))]
#: Switching a source on or off spends the organisation's money either way —
#: one by starting, one by stopping. Same permission the vault used to need.
Admin = Annotated[Principal, Depends(require(Permission.CREDENTIAL_WRITE))]


@router.get("/connections", response_model=ConnectionListResponse, summary="Every source")
async def list_connections(me: AnyMember, db: Db) -> ConnectionListResponse:
    """Every source this build knows about, with this workspace's decision on each.

    The catalogue is not spelled out by the browser. It is `KIND_SPECS`, so a
    source added to the backend gets a card with no change on the screen.
    """
    rows = {row.kind: row for row in await _connections(db, me.workspace_id)}
    names = await UserRepo(db, me.workspace_id).names([row.connected_by for row in rows.values()])
    settings = get_settings()
    return ConnectionListResponse(
        sources=[
            _summary(spec, rows.get(spec.kind), names, settings) for spec in KIND_SPECS.values()
        ]
    )


@router.post(
    "/connections/{kind}/connect",
    response_model=SourceSummary,
    summary="Switch a source on",
)
async def connect_source(
    kind: CredentialKind,
    me: Admin,
    request: Request,
    db: Db,
) -> SourceSummary:
    """Record the decision, then prove it works. Nothing is typed, and nothing is stored.

    Refusing an unconfigured source is the one hard stop: a connection to a
    deployment that supplies no key is a card that says "connected" next to a
    run that skips the source, and that lie is exactly what this screen exists
    to stop telling.
    """
    spec = spec_for(kind)
    settings = get_settings()
    missing = spec.missing_env_vars(settings)
    if missing:
        raise problems.unprocessable(
            f"This deployment supplies no {spec.label} credential. "
            f"Set {', '.join(missing)} in the environment and restart the API.",
            missing_env_vars=list(missing),
        )

    existing = await _connection(db, me.workspace_id, kind)
    connection = existing
    if connection is None:
        connection = SourceConnection(
            id=uuid.uuid4(),
            workspace_id=me.workspace_id,
            kind=kind,
            connected_by=me.user.id,
        )
        db.add(connection)
        write_audit(
            db,
            workspace_id=me.workspace_id,
            actor_id=me.user.id,
            action=AuditAction.SOURCE_CONNECTED,
            target_type=AuditTarget.SOURCE,
            target_id=connection.id,
            meta={"kind": kind.value, "env_vars": list(spec.env_vars)},
            ip=client_ip(request),
        )

    outcome = await _run_test(spec, spec.from_env(settings))
    _record(connection, outcome)
    if existing is not None:
        # Connecting something already connected is not a second decision, but
        # it did re-run the test and overwrite the verdict. Recorded as a test,
        # so the log never shows state changing with nothing that changed it.
        write_audit(
            db,
            workspace_id=me.workspace_id,
            actor_id=me.user.id,
            action=AuditAction.SOURCE_TESTED,
            target_type=AuditTarget.SOURCE,
            target_id=connection.id,
            meta={"kind": kind.value, "ok": outcome.ok, "detail": outcome.detail},
            ip=client_ip(request),
        )
    await db.commit()
    await db.refresh(connection)

    log.info("source.connected", kind=kind.value, ok=outcome.ok, workspace_id=str(me.workspace_id))
    names = await UserRepo(db, me.workspace_id).names([connection.connected_by])
    return _summary(spec, connection, names, settings)


@router.delete(
    "/connections/{kind}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Switch a source off",
)
async def disconnect_source(
    kind: CredentialKind,
    me: Admin,
    request: Request,
    db: Db,
) -> Response:
    """Delete the decision. The deployment's key is untouched — it was never ours.

    Deleting a connection that is not there answers `204` rather than `404`:
    the caller asked for this source to be off, and it is off. A screen that
    double-submits should not get an error for the state it wanted.
    """
    connection = await _connection(db, me.workspace_id, kind)
    if connection is not None:
        write_audit(
            db,
            workspace_id=me.workspace_id,
            actor_id=me.user.id,
            action=AuditAction.SOURCE_DISCONNECTED,
            target_type=AuditTarget.SOURCE,
            target_id=connection.id,
            meta={"kind": kind.value},
            ip=client_ip(request),
        )
        await db.delete(connection)
        await db.commit()
        log.info("source.disconnected", kind=kind.value, workspace_id=str(me.workspace_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/connections/{kind}/test",
    response_model=ConnectionTestResponse,
    summary="Prove a source answers",
)
async def test_source(
    kind: CredentialKind,
    me: AnyMember,
    request: Request,
    db: Db,
) -> ConnectionTestResponse:
    """Make the cheapest real call the upstream offers, and say what came back.

    A failure is a `200` with `ok=false`, not an error status: the call the
    caller asked for succeeded, and the answer is that the key is wrong.
    Returning `4xx` would make a wrong key indistinguishable from a wrong
    request.

    Any member may run it. It returns no secret, it changes nothing, and the
    person watching a run skip a source is often not the person who can
    reconnect it — telling them *why* is the point.

    A disconnected source is still testable, and answers "would this work if I
    switched it on". That verdict is not stored, because there is no row to
    store it on and inventing one would make a test look like a decision.
    """
    spec = spec_for(kind)
    settings = get_settings()
    missing = spec.missing_env_vars(settings)
    if missing:
        raise problems.unprocessable(
            f"This deployment supplies no {spec.label} credential. "
            f"Set {', '.join(missing)} in the environment and restart the API.",
            missing_env_vars=list(missing),
        )

    outcome = await _run_test(spec, spec.from_env(settings))
    connection = await _connection(db, me.workspace_id, kind)
    if connection is not None:
        _record(connection, outcome)

    write_audit(
        db,
        workspace_id=me.workspace_id,
        actor_id=me.user.id,
        action=AuditAction.SOURCE_TESTED,
        target_type=AuditTarget.SOURCE,
        target_id=connection.id if connection is not None else None,
        meta={"kind": kind.value, "ok": outcome.ok, "detail": outcome.detail},
        ip=client_ip(request),
    )
    await db.commit()
    return ConnectionTestResponse(
        kind=kind,
        ok=outcome.ok,
        detail=outcome.detail,
        meta=_showable(outcome.meta),
        tested_at=datetime.now(UTC),
        recorded=connection is not None,
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _connections(db: AsyncSession, workspace_id: uuid.UUID) -> list[SourceConnection]:
    return list(
        (
            await db.execute(
                sa.select(SourceConnection).where(SourceConnection.workspace_id == workspace_id)
            )
        )
        .scalars()
        .all()
    )


async def _connection(
    db: AsyncSession, workspace_id: uuid.UUID, kind: CredentialKind
) -> SourceConnection | None:
    return (
        await db.execute(
            sa.select(SourceConnection).where(
                SourceConnection.workspace_id == workspace_id,
                SourceConnection.kind == kind,
            )
        )
    ).scalar_one_or_none()


def _record(connection: SourceConnection, outcome: ConnectorStatus) -> None:
    """Write a verdict onto a connection, keeping the hints a failure cannot refresh."""
    connection.last_tested_at = datetime.now(UTC)
    connection.last_test_ok = outcome.ok
    connection.last_test_detail = outcome.detail[:500]
    if outcome.ok:
        # Merge rather than replace: a probe that proves the key may not name
        # the account, and the account name from the last successful test is
        # still true.
        connection.meta = {**(connection.meta or {}), **_showable(outcome.meta)}


def _summary(
    spec: KindSpec,
    connection: SourceConnection | None,
    names: dict[uuid.UUID, str],
    settings: Settings,
) -> SourceSummary:
    missing = spec.missing_env_vars(settings)
    return SourceSummary(
        kind=spec.kind,
        label=spec.label,
        description=spec.description,
        required_for_runs=spec.required_for_runs,
        env_vars=list(spec.env_vars),
        missing_env_vars=list(missing),
        configured=not missing,
        connected=connection is not None,
        connected_at=connection.connected_at if connection else None,
        connected_by=connection.connected_by if connection else None,
        connected_by_name=names.get(connection.connected_by) if connection else None,
        last_tested_at=connection.last_tested_at if connection else None,
        last_test_ok=connection.last_test_ok if connection else None,
        last_test_detail=connection.last_test_detail if connection else None,
        meta=(connection.meta or {}) if connection else {},
    )


async def _run_test(spec: KindSpec, values: dict[str, str]) -> ConnectorStatus:
    """Dispatch to whichever upstream can answer cheapest."""
    settings = get_settings()
    if spec.kind is CredentialKind.WEBSHARE:
        # Neither a connector nor a model surface: a transport that several
        # connectors borrow. `proxy.probe` proves the key, then proves that
        # bytes actually return through the exit — the half a `/proxy/config/`
        # read alone would miss.
        return await proxy_probe(values.get("api_key", ""), settings)
    if spec.connector is None:
        # The remaining kind with no connector is OpenRouter, which is not an
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
    # is part of the thing being tested. A connector that reaches its upstream
    # through a proxy the credential itself addresses would, against a plain
    # client built here, test a route the run path never takes — and pass, or
    # fail, for the wrong reason. Every connector builds its own when the
    # context has none, and closes it (`gather._pull` relies on the same
    # behaviour).
    try:
        instance = connector_class(spec.connector)(
            ConnectorContext(credentials=values, settings=settings)
        )
        return await instance.test_connection()
    except Exception as exc:  # noqa: BLE001 — a broken connector must not 500 this screen
        log.warning("source.test_failed", kind=spec.kind.value, error=str(exc))
        return ConnectorStatus(ok=False, detail=f"The test could not be completed: {exc}")


def _showable(meta: dict[str, object]) -> dict[str, object]:
    """Drop empty hints so the interface does not render a row of blanks."""
    return {key: value for key, value in meta.items() if value not in (None, "")}
