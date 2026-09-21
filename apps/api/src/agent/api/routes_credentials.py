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

import json
import secrets
import uuid
from base64 import b64decode, b64encode
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import urlencode

import httpx
import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from agent.api import API_PREFIX, problems
from agent.api.middleware import client_ip
from agent.api.schemas_credentials import (
    CreateCredentialRequest,
    CredentialFieldInfo,
    CredentialKindInfo,
    CredentialListResponse,
    CredentialSummary,
    CredentialTestResponse,
    GoogleAdsAuthorizeRequest,
    GoogleAdsAuthorizeResponse,
    KindTestResponse,
)
from agent.audit import AuditAction, AuditTarget, write_audit
from agent.auth.deps import Principal, require
from agent.auth.rbac import Permission
from agent.config import get_settings
from agent.connectors import connector_class
from agent.connectors.base import ConnectorContext, ConnectorStatus
from agent.connectors.google_ads import GoogleAdsConnector
from agent.connectors.proxy import probe as proxy_probe
from agent.credential_kinds import KIND_SPECS, KindSpec, spec_for, unseal
from agent.credentials import (
    MissingCredential,
    env_secret,
    new_credential,
    open_credential,
    resolve_secret,
)
from agent.crypto import DecryptionError, decrypt_str, encrypt
from agent.db.models import Credential, CredentialKind, CredentialScope, Project
from agent.db.repos import UserRepo
from agent.db.session import get_session
from agent.llm.openrouter import OpenRouterError, probe_key
from agent.redis_client import get_redis

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
    names = await UserRepo(db, me.workspace_id).names()
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
                oauth_provider=spec.oauth_provider,
                oauth_ready=_oauth_ready(spec),
                oauth_fields=list(spec.oauth_fields),
                env_var=spec.env_var,
                env_configured=env_secret(spec.kind) is not None,
                env_last4=_env_last4(spec.kind),
            )
            for spec in KIND_SPECS.values()
        ],
    )


def _env_last4(kind: CredentialKind) -> str | None:
    """Enough of the environment's key to recognise it, and no more.

    The same four characters the vault shows for a stored secret, so a person
    comparing "what is in my file" with "what is this deployment using" is
    comparing like with like.
    """
    secret = env_secret(kind)
    return secret[-4:] if secret else None


def _oauth_ready(spec: KindSpec) -> bool:
    """Whether consent is a real option here, not just a declared one.

    The kind says it *can* be connected by consent; this says the deployment
    can *run* it. They come apart on a deployment with no Google OAuth client
    configured, and the interface needs the difference: that is the only case
    where asking someone to paste five values by hand is a kindness rather than
    the thing this screen exists to avoid.
    """
    if spec.oauth_provider != "google":
        return False
    settings = get_settings()
    return bool(settings.google_ads_oauth_client_id and settings.google_ads_oauth_client_secret)


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


@router.post(
    "/credentials/kinds/{kind}/test",
    response_model=KindTestResponse,
    summary="Prove whatever currently supplies a kind",
)
async def test_kind(
    kind: CredentialKind,
    me: AnyMember,
    request: Request,
    db: Db,
) -> KindTestResponse:
    """Test the key this workspace would actually run with.

    `/credentials/{id}/test` can only test a row, which leaves the case this
    endpoint exists for: a deployment that configured its sources in a file and
    has no rows at all. Asking "does my key work" should not require storing one
    first.

    It resolves through `resolve_secret`, so it answers for whatever would be
    used — a workspace override if one exists, the environment otherwise — and
    says which of the two it found.
    """
    _assert_may_write(me, CredentialScope.WORKSPACE)
    spec = spec_for(kind)
    try:
        secret = await resolve_secret(db, workspace_id=me.workspace_id, kind=kind)
    except MissingCredential:
        raise problems.unprocessable(
            f"Nothing supplies {spec.label} in this workspace — no stored credential"
            + (f" and no {spec.env_var}." if spec.env_var else "."),
            fields=[spec.fields[0].name],
        ) from None

    from_env = env_secret(kind) == secret
    outcome = await _run_test(kind, spec.connector, unseal(spec, secret))

    write_audit(
        db,
        workspace_id=me.workspace_id,
        actor_id=me.user.id,
        action=AuditAction.CREDENTIAL_TESTED,
        target_type=AuditTarget.CREDENTIAL,
        target_id=None,
        meta={
            "kind": kind.value,
            "ok": outcome.ok,
            "detail": outcome.detail,
            "source": "environment" if from_env else "vault",
        },
        ip=client_ip(request),
    )
    await db.commit()
    return KindTestResponse(
        kind=kind,
        source="environment" if from_env else "vault",
        ok=outcome.ok,
        detail=outcome.detail,
        meta=_showable(outcome.meta),
        tested_at=datetime.now(UTC),
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


# --- Google's consent screen -------------------------------------------------
#
# Why this exists next to the form: the Google Ads account belongs to whoever
# runs the advertising, and that is rarely the person who installed this. Asking
# them to produce a refresh token means asking them to run a terminal. Asking
# them for a click means the token never exists outside this process — nobody
# reads it out, pastes it into chat, or leaves it in a downloads folder.
#
# The OAuth *client* is the deployment's (env), the *grant* is the workspace's
# (vault). The developer token is neither: it is typed once here, carried
# through the round trip sealed, and sealed again with the rest at the end.

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 — an endpoint
GOOGLE_ADS_SCOPE = "https://www.googleapis.com/auth/adwords"
OAUTH_STATE_PREFIX = "google-ads-oauth:"
#: Long enough to read a consent screen and choose an account; short enough that
#: a state abandoned in a closed tab is not a standing invitation.
OAUTH_STATE_TTL_S = 900


def _oauth_redirect_uri() -> str:
    """Where Google sends the browser back.

    Built from `app_base_url`, not from the incoming request: Google compares
    this string byte for byte against the registered one, and the API itself has
    no ingress — every browser request arrives through the web app's rewrite, so
    the app's own base URL is the only address that is actually reachable.
    """
    return f"{get_settings().app_base_url}{API_PREFIX}/credentials/google-ads/callback"


def _safe_return_to(path: str) -> str:
    """A path on this app, or the settings screen.

    `return_to` arrives from the browser and leaves in a `Location` header, so
    anything that could name another origin — an absolute URL, a
    protocol-relative `//host` — is replaced rather than sanitised.
    """
    candidate = (path or "").strip()
    if not candidate.startswith("/") or candidate.startswith("//"):
        return "/settings"
    return candidate


def _back(return_to: str, **params: str) -> RedirectResponse:
    """Send the browser back to the screen it started from, carrying the outcome."""
    separator = "&" if "?" in return_to else "?"
    target = f"{get_settings().app_base_url}{return_to}{separator}{urlencode(params)}"
    # 303: the browser must GET the screen, whatever method got it here.
    return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)


@router.post(
    "/credentials/google-ads/authorize",
    response_model=GoogleAdsAuthorizeResponse,
    summary="Begin a Google Ads consent",
)
async def authorize_google_ads(
    body: GoogleAdsAuthorizeRequest, me: AnyMember
) -> GoogleAdsAuthorizeResponse:
    """Mint a one-use state and hand back the consent URL to send the browser to."""
    _assert_may_write(me, CredentialScope.WORKSPACE)
    settings = get_settings()
    if not settings.google_ads_oauth_client_id or not settings.google_ads_oauth_client_secret:
        raise problems.unprocessable(
            "This deployment has no Google OAuth client configured. Set "
            "GOOGLE_ADS_OAUTH_CLIENT_ID and GOOGLE_ADS_OAUTH_CLIENT_SECRET.",
            fields=["developer_token"],
        )

    state = secrets.token_urlsafe(32)
    payload = json.dumps(
        {
            "workspace_id": str(me.workspace_id),
            "user_id": str(me.user.id),
            "developer_token": body.developer_token,
            "return_to": _safe_return_to(body.return_to),
        }
    )
    # Sealed with the vault's own key and bound to this state: fifteen minutes
    # in Redis is still a secret at rest (PRD §15 NF5).
    ciphertext, nonce = encrypt(payload, aad=state.encode())
    await get_redis().setex(
        f"{OAUTH_STATE_PREFIX}{state}",
        OAUTH_STATE_TTL_S,
        json.dumps({"c": b64encode(ciphertext).decode(), "n": b64encode(nonce).decode()}),
    )

    query = urlencode(
        {
            "client_id": settings.google_ads_oauth_client_id,
            "redirect_uri": _oauth_redirect_uri(),
            "response_type": "code",
            "scope": GOOGLE_ADS_SCOPE,
            # Offline or there is no refresh token; `consent` or Google reissues
            # one only on a first-ever grant; `select_account` because a browser
            # already signed in as the wrong Google user would otherwise skip
            # the chooser and authorise an account with no Google Ads at all.
            "access_type": "offline",
            "prompt": "consent select_account",
            "state": state,
        }
    )
    log.info("google_ads.oauth_started", workspace_id=str(me.workspace_id))
    return GoogleAdsAuthorizeResponse(url=f"{GOOGLE_AUTH_URL}?{query}")


@router.get(
    "/credentials/google-ads/callback",
    summary="Finish a Google Ads consent",
    response_class=RedirectResponse,
    status_code=status.HTTP_303_SEE_OTHER,
)
async def google_ads_callback(
    me: AnyMember,
    db: Db,
    request: Request,
    code: Annotated[str, Query(description="Google's authorisation code")] = "",
    state: Annotated[str, Query(description="The one-use state from /authorize")] = "",
    error: Annotated[str, Query(description="Set when the person declined")] = "",
) -> RedirectResponse:
    """Exchange the code, find the account, and seal the grant into the vault.

    Every failure here ends as a redirect rather than a problem document: the
    caller is a person's browser arriving from Google, and a JSON error is a
    dead end for them. The reason travels as a query parameter so the screen
    they started from can say what happened.
    """
    _assert_may_write(me, CredentialScope.WORKSPACE)
    raw = await get_redis().getdel(f"{OAUTH_STATE_PREFIX}{state}") if state else None
    if raw is None:
        # Expired, already used, or never ours. All three mean: start again.
        return _back("/settings", google_ads="error", reason="This consent link has expired.")

    envelope = json.loads(raw)
    try:
        payload = json.loads(
            decrypt_str(b64decode(envelope["c"]), b64decode(envelope["n"]), aad=state.encode())
        )
    except (DecryptionError, KeyError, ValueError):
        return _back("/settings", google_ads="error", reason="That consent could not be read.")

    return_to = _safe_return_to(payload.get("return_to", ""))
    if payload.get("workspace_id") != str(me.workspace_id):
        # The state is unguessable, but it is not a capability: whoever finishes
        # a consent must be in the workspace that started it.
        return _back(return_to, google_ads="error", reason="That consent belongs elsewhere.")
    if error or not code:
        return _back(return_to, google_ads="error", reason=error or "Google sent no code.")

    settings = get_settings()
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
        exchange = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.google_ads_oauth_client_id,
                "client_secret": settings.google_ads_oauth_client_secret,
                "redirect_uri": _oauth_redirect_uri(),
                "grant_type": "authorization_code",
            },
        )
    if exchange.status_code >= 400:
        log.warning("google_ads.oauth_exchange_failed", status=exchange.status_code)
        return _back(return_to, google_ads="error", reason="Google refused the authorisation.")
    refresh_token = str(exchange.json().get("refresh_token") or "")
    if not refresh_token:
        return _back(
            return_to,
            google_ads="error",
            reason=(
                "Google returned no refresh token. Remove this app at "
                "myaccount.google.com/permissions and connect again."
            ),
        )

    values = {
        "developer_token": str(payload["developer_token"]),
        "client_id": settings.google_ads_oauth_client_id,
        "client_secret": settings.google_ads_oauth_client_secret,
        "refresh_token": refresh_token,
    }
    try:
        accounts = await GoogleAdsConnector(
            ConnectorContext(credentials=values, settings=settings)
        ).accessible_accounts()
    except Exception as exc:  # noqa: BLE001 — an upstream refusal is not a 500
        log.warning("google_ads.oauth_accounts_failed", error=str(exc))
        return _back(return_to, google_ads="error", reason=str(exc)[:300])

    # A manager account holds no campaigns, so it is the last thing to fall back
    # to rather than the first thing to pick.
    chosen = next(
        (row for row in accounts if not row["manager"]), accounts[0] if accounts else None
    )
    if chosen is None:
        return _back(
            return_to,
            google_ads="error",
            reason="That Google account reaches no Google Ads accounts.",
        )
    values["customer_id"] = str(chosen["customer_id"])
    # The manager id is the one Google needs in `login-customer-id`, and it is
    # known here rather than typed: `accessible_accounts()` found this account by
    # expanding that manager, so it recorded which one. An account reached
    # directly has no manager and must not send the header at all.
    if chosen.get("via_manager"):
        values["login_customer_id"] = str(chosen["via_manager"])

    spec = spec_for(CredentialKind.GOOGLE_ADS)
    credential = new_credential(
        workspace_id=me.workspace_id,
        kind=CredentialKind.GOOGLE_ADS,
        secret=spec.seal(spec.validate(values)),
        created_by=uuid.UUID(str(payload["user_id"])),
        scope=CredentialScope.WORKSPACE,
        meta={
            **spec.meta(values),
            "connected_by": "google_oauth",
            "account_name": chosen["name"] or "",
            # Every account the grant reaches, so a workspace with more than one
            # can see what it chose between rather than wondering.
            "accessible": [
                {"customer_id": row["customer_id"], "name": row["name"], "manager": row["manager"]}
                for row in accounts
            ],
        },
    )
    db.add(credential)
    write_audit(
        db,
        workspace_id=me.workspace_id,
        actor_id=me.user.id,
        action=AuditAction.CREDENTIAL_CREATED,
        target_type=AuditTarget.CREDENTIAL,
        target_id=credential.id,
        meta={
            "kind": CredentialKind.GOOGLE_ADS.value,
            "scope": CredentialScope.WORKSPACE.value,
            "via": "google_oauth",
            "hints": credential.meta,
        },
        ip=client_ip(request),
    )
    await db.commit()
    log.info(
        "google_ads.oauth_connected",
        workspace_id=str(me.workspace_id),
        customer_id=values["customer_id"],
        accounts=len(accounts),
    )
    return _back(
        return_to,
        google_ads="connected",
        account=chosen["name"] or values["customer_id"],
    )


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
    if kind is CredentialKind.WEBSHARE:
        # Neither a connector nor a model surface: a transport that several
        # connectors borrow. `proxy.probe` proves the key, then proves that
        # bytes actually return through the exit — the half a `/proxy/config/`
        # read alone would miss.
        return await proxy_probe(values.get("api_key", ""), settings)
    if connector is None:
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
    # is part of the thing being tested: a connector that reaches its upstream
    # through a proxy the credential itself addresses would, against a plain
    # client built here, test a route the run path never takes — and pass, or
    # fail, for the wrong reason. Every connector builds its own when the
    # context has none,
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
