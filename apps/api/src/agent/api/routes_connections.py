"""Connections: which sources this workspace uses (PRD §14, §15 NF5).

Three rules shape every route here.

**No secret ever crosses this boundary in a form.** There is no endpoint that
returns a key, and none that accepts one from a person. A source is switched on
by name; its values are read from the deployment's environment. The only
credential-shaped thing in a response is the *name* of a variable and the last
four characters of what a successful test found there.

**Except the half of a credential the deployment cannot hold.** Google Ads needs
five values and the environment can only supply three: the developer token and
the OAuth client. The refresh token and the account id come from a person's
consent, because a developer token is issued once to one manager account and
requiring one per colleague is requiring most of them never to connect. So
`/connections/google/authorize` sends a browser to Google and
`/connections/google/callback` seals what comes back onto the row. That secret
arrives from Google over TLS, is never rendered, and is never readable again.

**Signing in is not the same permission as switching a source on.** Connecting
Google with your own account is additive — it is the one thing everybody in a
workspace can do for themselves — so it needs only a session. Deleting the
workspace's connection stops everybody's runs, so it still needs
`credential_write`.

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
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import urlencode

import httpx
import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from agent import google_oauth
from agent.api import problems
from agent.api.middleware import client_ip
from agent.api.schemas_connections import (
    AccessibleAccount,
    ChooseAccountRequest,
    ConnectionListResponse,
    ConnectionTestResponse,
    GoogleAuthorizeRequest,
    GoogleAuthorizeResponse,
    SourceOAuth,
    SourceSummary,
)
from agent.audit import AuditAction, AuditTarget, write_audit
from agent.auth.deps import Principal, require
from agent.auth.rbac import Permission
from agent.config import Settings, get_settings
from agent.connectors import connector_class
from agent.connectors.base import ConnectorContext, ConnectorStatus
from agent.connectors.google_ads import GoogleAdsConnector
from agent.connectors.proxy import probe as proxy_probe
from agent.credential_kinds import KIND_SPECS, OAUTH_KINDS, KindSpec, spec_for
from agent.credentials import grant_values
from agent.db.models import CredentialKind, SourceConnection
from agent.db.repos import UserRepo
from agent.db.session import get_session
from agent.google_oauth import ConsentState, GoogleOAuthError
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
    names = await _names(db, me.workspace_id, rows.values())
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
    values = spec.values(settings, grant_values(existing) if existing else {})
    if spec.oauth is not None and spec.missing_values(values):
        # The deployment's half is there and the person's half is not. Refusing
        # is the same rule as the unconfigured case above — a card that says
        # "connected" next to a run that skips the source is the lie this
        # screen exists to stop telling — but the fix is a different one, so
        # the sentence names it rather than listing variables nobody can set.
        raise problems.unprocessable(
            f"Nobody has signed in to Google for this workspace yet. "
            f"Press {spec.oauth.action} instead — it mints the rest of the credential.",
            oauth_required=spec.oauth.provider,
        )

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

    outcome = await _run_test(spec, values)
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
    return _summary(spec, connection, await _names(db, me.workspace_id, [connection]), settings)


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
        # Deleting the row is what disconnecting means. Telling Google is the
        # courtesy of not leaving a live grant on somebody's personal account
        # afterwards, and it is deliberately best-effort: the person asked for
        # this source to be off, and a Google that will not answer must not be
        # able to keep it on.
        refresh_token = grant_values(connection).get("refresh_token", "")
        revoked = await google_oauth.revoke(refresh_token) if refresh_token else None
        write_audit(
            db,
            workspace_id=me.workspace_id,
            actor_id=me.user.id,
            action=AuditAction.SOURCE_DISCONNECTED,
            target_type=AuditTarget.SOURCE,
            target_id=connection.id,
            meta={"kind": kind.value, "grant_revoked": revoked},
            ip=client_ip(request),
        )
        await db.delete(connection)
        await db.commit()
        log.info(
            "source.disconnected",
            kind=kind.value,
            workspace_id=str(me.workspace_id),
            grant_revoked=revoked,
        )
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

    connection = await _connection(db, me.workspace_id, kind)
    values = spec.values(settings, grant_values(connection) if connection else {})
    absent = spec.missing_values(values)
    if absent and spec.oauth is not None:
        raise problems.unprocessable(
            f"Nobody has signed in to Google for this workspace yet, so there is "
            f"nothing to test. Press {spec.oauth.action} first.",
            oauth_required=spec.oauth.provider,
        )

    outcome = await _run_test(spec, values)
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
# the Google consent
# ---------------------------------------------------------------------------


@router.post(
    "/connections/google/authorize",
    response_model=GoogleAuthorizeResponse,
    summary="Begin a Google sign-in",
)
async def authorize_google(body: GoogleAuthorizeRequest, me: AnyMember) -> GoogleAuthorizeResponse:
    """Mint a one-use state and hand back the URL to send the browser to.

    Any member may start one. The thing being handed over is the caller's own
    Google account, not the organisation's money, and the alternative — one
    administrator holding a developer token and pasting refresh tokens for
    everyone — is how this source stayed unconnected.

    The deployment's half is checked here rather than in the callback. Sending
    somebody to a consent screen that cannot possibly finish, and telling them
    so only after they have chosen an account and approved three scopes, is a
    worse error than refusing the button.
    """
    settings = get_settings()
    for kind in OAUTH_KINDS.get("google", ()):
        spec = spec_for(kind)
        missing = spec.missing_env_vars(settings)
        if missing:
            raise problems.unprocessable(
                f"This deployment cannot finish a Google sign-in for {spec.label}: "
                f"{', '.join(missing)} is not set. Set it in the environment and "
                "restart the API.",
                missing_env_vars=list(missing),
            )
    try:
        url = await google_oauth.consent_url(
            settings,
            ConsentState(
                workspace_id=me.workspace_id, user_id=me.user.id, return_to=body.return_to
            ),
        )
    except GoogleOAuthError as exc:
        raise problems.unprocessable(exc.detail) from exc
    log.info("google_oauth.started", workspace_id=str(me.workspace_id), user_id=str(me.user.id))
    return GoogleAuthorizeResponse(url=url)


@router.get(
    "/connections/google/callback",
    summary="Finish a Google sign-in",
    response_class=RedirectResponse,
    status_code=status.HTTP_303_SEE_OTHER,
)
async def google_callback(
    me: AnyMember,
    db: Db,
    request: Request,
    code: Annotated[str, Query(description="Google's one-use authorisation code")] = "",
    state: Annotated[str, Query(description="The one-use state from /authorize")] = "",
    error: Annotated[str, Query(description="Set when the person declined")] = "",
) -> RedirectResponse:
    """Exchange the code, find the accounts, and seal the grant onto the row.

    Every failure here ends as a redirect rather than a problem document. The
    caller is a person's browser arriving from Google: a JSON error is a blank
    page with some punctuation on it, and there is no screen behind it to
    recover from. The reason travels as a query parameter so the page they
    started from can say what happened in its own words.
    """
    consent = await google_oauth.consume_state(state)
    if consent is None:
        # Expired, already used, or never ours. All three mean: start again.
        return _back(
            google_oauth.DEFAULT_RETURN_TO,
            reason="That sign-in link has expired. Please try again.",
        )
    if consent.workspace_id != me.workspace_id:
        # The state is unguessable, but it is not a capability: whoever
        # finishes a consent must be in the workspace that started it.
        return _back(consent.return_to, reason="That sign-in belongs to another workspace.")
    if error or not code:
        return _back(
            consent.return_to,
            reason=(
                "Google did not complete the sign-in."
                if not error
                else f"Google reported: {error}."
            ),
        )

    settings = get_settings()
    try:
        grant = await google_oauth.exchange(settings, code)
    except GoogleOAuthError as exc:
        return _back(consent.return_to, reason=exc.detail)
    if not grant.reaches_google_ads:
        # Google renders one checkbox per sensitive scope and returns a 200 for
        # whatever survived. A grant without `adwords` would connect and then
        # read nothing.
        return _back(
            consent.return_to,
            reason=(
                "That sign-in did not include Google Ads. Connect again and leave "
                "the Google Ads permission ticked."
            ),
        )

    spec = spec_for(CredentialKind.GOOGLE_ADS)
    values = spec.values(settings, {"refresh_token": grant.refresh_token})
    try:
        accounts = await GoogleAdsConnector(
            ConnectorContext(credentials=values, settings=settings)
        ).accessible_accounts()
    except Exception as exc:  # noqa: BLE001 — an upstream refusal is not a 500
        log.warning("google_oauth.accounts_failed", error=str(exc))
        return _back(consent.return_to, reason=str(exc)[:300])

    # A manager account holds no campaigns, so it is the last thing to fall back
    # to rather than the first thing to pick.
    chosen = next(
        (row for row in accounts if not row["manager"]), accounts[0] if accounts else None
    )
    if chosen is None:
        return _back(
            consent.return_to,
            reason=(
                f"{grant.email or 'That Google account'} does not reach any Google Ads "
                "accounts. Sign in with the account that can see them."
            ),
        )

    connection = await _connection(db, me.workspace_id, CredentialKind.GOOGLE_ADS)
    created = connection is None
    if connection is None:
        connection = SourceConnection(
            id=uuid.uuid4(),
            workspace_id=me.workspace_id,
            kind=CredentialKind.GOOGLE_ADS,
            connected_by=me.user.id,
            meta={},
        )
        db.add(connection)
    _store_grant(connection, grant, accounts, chosen, actor_id=me.user.id)

    write_audit(
        db,
        workspace_id=me.workspace_id,
        actor_id=me.user.id,
        action=AuditAction.SOURCE_CONNECTED if created else AuditAction.SOURCE_TESTED,
        target_type=AuditTarget.SOURCE,
        target_id=connection.id,
        meta={
            "kind": CredentialKind.GOOGLE_ADS.value,
            "via": "google_oauth",
            "email": grant.email,
            "customer_id": chosen["customer_id"],
            "accounts": len(accounts),
            "scopes": list(grant.scopes),
        },
        ip=client_ip(request),
    )
    await db.commit()
    log.info(
        "google_oauth.connected",
        workspace_id=str(me.workspace_id),
        customer_id=chosen["customer_id"],
        accounts=len(accounts),
    )
    return _back(
        consent.return_to,
        google="connected",
        account=str(chosen["name"] or chosen["customer_id"]),
    )


@router.post(
    "/connections/{kind}/account",
    response_model=SourceSummary,
    summary="Choose which account a grant reads",
)
async def choose_account(
    kind: CredentialKind,
    body: ChooseAccountRequest,
    me: AnyMember,
    request: Request,
    db: Db,
) -> SourceSummary:
    """Point an existing grant at a different one of the accounts it reaches.

    One consent commonly reaches several accounts — anybody working out of a
    manager account reaches all of its clients — and the callback can only
    guess which one the research should read. This is how that guess is
    corrected, without a second trip through Google.

    Only accounts the grant already reported are accepted. Taking a customer id
    from the request body would let a person point the workspace at an account
    this consent cannot open, which fails later, somewhere else, as an
    unexplained empty pull.
    """
    spec = spec_for(kind)
    if spec.oauth is None:
        raise problems.unprocessable(f"{spec.label} has no accounts to choose between.")
    connection = await _connection(db, me.workspace_id, kind)
    if connection is None or not grant_values(connection).get("refresh_token"):
        raise problems.unprocessable(
            f"Nobody has signed in to Google for this workspace yet. Press {spec.oauth.action}."
        )

    wanted = body.customer_id.replace("-", "").strip()
    accounts = _accounts(connection)
    match = next((row for row in accounts if row.customer_id == wanted), None)
    if match is None:
        raise problems.unprocessable(
            f"This sign-in does not reach account {body.customer_id}. "
            "Connect with Google again if the account is new.",
        )

    connection.meta = {
        **(connection.meta or {}),
        "customer_id": match.customer_id,
        "account_name": match.name or "",
        **({"login_customer_id": match.via_manager} if match.via_manager else {}),
    }
    if not match.via_manager:
        # An account reached directly must send no `login-customer-id` header at
        # all — an empty one is a different request, and Google answers it as a
        # permission error on an account that is perfectly reachable.
        connection.meta.pop("login_customer_id", None)

    settings = get_settings()
    outcome = await _run_test(spec, spec.values(settings, grant_values(connection)))
    _record(connection, outcome)
    write_audit(
        db,
        workspace_id=me.workspace_id,
        actor_id=me.user.id,
        action=AuditAction.SOURCE_TESTED,
        target_type=AuditTarget.SOURCE,
        target_id=connection.id,
        meta={"kind": kind.value, "customer_id": match.customer_id, "ok": outcome.ok},
        ip=client_ip(request),
    )
    await db.commit()
    await db.refresh(connection)
    log.info("source.account_chosen", kind=kind.value, customer_id=match.customer_id)
    return _summary(spec, connection, await _names(db, me.workspace_id, [connection]), settings)


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


async def _names(
    db: AsyncSession, workspace_id: uuid.UUID, rows: Iterable[SourceConnection]
) -> dict[uuid.UUID, str]:
    """Display names for everyone a card might credit: who connected, who signed in."""
    wanted: list[uuid.UUID] = []
    for row in rows:
        wanted.append(row.connected_by)
        if row.granted_by is not None:
            wanted.append(row.granted_by)
    return await UserRepo(db, workspace_id).names(wanted)


def _back(return_to: str, *, reason: str | None = None, **params: str) -> RedirectResponse:
    """Send the browser back to the screen it started from, carrying the outcome."""
    outcome: dict[str, str] = {"google": "error", "reason": reason} if reason else dict(params)
    separator = "&" if "?" in return_to else "?"
    target = f"{get_settings().app_base_url.rstrip('/')}{return_to}{separator}{urlencode(outcome)}"
    # 303: the browser must GET the screen, whatever method got it here.
    return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)


def _store_grant(
    connection: SourceConnection,
    grant: google_oauth.Grant,
    accounts: list[dict[str, Any]],
    chosen: dict[str, Any],
    *,
    actor_id: uuid.UUID,
) -> None:
    """Write a fresh consent onto a connection row: the secret sealed, the rest shown.

    Listing the accounts is itself the cheapest real call this credential can
    make — it used the developer token, the OAuth client and the new refresh
    token together — so its success is recorded as the verdict rather than
    running a second one. A person who has just signed in should not watch a
    spinner prove what signing in already proved.
    """
    ciphertext, nonce = google_oauth.seal(
        {"refresh_token": grant.refresh_token}, connection_id=connection.id
    )
    connection.grant_ciphertext = ciphertext
    connection.grant_nonce = nonce
    connection.granted_at = datetime.now(UTC)
    connection.granted_by = actor_id

    meta = {
        **(connection.meta or {}),
        "customer_id": str(chosen["customer_id"]),
        "account_name": chosen.get("name") or "",
        "google_email": grant.email,
        "granted_scopes": list(grant.scopes),
        # Every account the grant reaches, so a workspace with more than one can
        # see what it chose between rather than wondering — and so switching
        # accounts later needs no second trip through Google.
        "accessible": [
            {
                "customer_id": str(row["customer_id"]),
                "name": row.get("name"),
                "manager": bool(row.get("manager")),
                "via_manager": row.get("via_manager"),
                "currency": row.get("currency"),
            }
            for row in accounts
        ],
    }
    if chosen.get("via_manager"):
        meta["login_customer_id"] = str(chosen["via_manager"])
    else:
        meta.pop("login_customer_id", None)
    connection.meta = meta

    connection.last_tested_at = datetime.now(UTC)
    connection.last_test_ok = True
    connection.last_test_detail = (
        f"signed in as {grant.email or 'a Google account'}; "
        f"reading {chosen.get('name') or chosen['customer_id']}"
    )[:500]


def _accounts(connection: SourceConnection) -> list[AccessibleAccount]:
    """The accounts this grant reported reaching, as the screen shows them."""
    raw = (connection.meta or {}).get("accessible")
    if not isinstance(raw, list):
        return []
    found: list[AccessibleAccount] = []
    for row in raw:
        if not isinstance(row, dict) or not row.get("customer_id"):
            continue
        found.append(
            AccessibleAccount(
                customer_id=str(row["customer_id"]),
                name=row.get("name"),
                manager=bool(row.get("manager")),
                via_manager=row.get("via_manager"),
                currency=row.get("currency"),
            )
        )
    return found


def _oauth(
    spec: KindSpec, connection: SourceConnection | None, names: dict[uuid.UUID, str]
) -> SourceOAuth | None:
    """What may be said about the consent half of this credential."""
    if spec.oauth is None:
        return None
    meta = (connection.meta or {}) if connection else {}
    granted = connection is not None and connection.grant_ciphertext is not None
    scopes = meta.get("granted_scopes")
    return SourceOAuth(
        provider=spec.oauth.provider,
        action=spec.oauth.action,
        explains=spec.oauth.explains,
        granted=granted,
        granted_at=connection.granted_at if connection else None,
        granted_by=connection.granted_by if connection else None,
        granted_by_name=(
            names.get(connection.granted_by)
            if connection and connection.granted_by is not None
            else None
        ),
        email=str(meta.get("google_email") or "") or None,
        scopes=[str(scope) for scope in scopes] if isinstance(scopes, list) else [],
        accounts=_accounts(connection) if connection else [],
        customer_id=str(meta.get("customer_id") or "") or None,
        login_customer_id=str(meta.get("login_customer_id") or "") or None,
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
        env_vars=list(spec.deployment_env_vars),
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
        oauth=_oauth(spec, connection, names),
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
