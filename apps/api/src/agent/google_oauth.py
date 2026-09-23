"""Google's consent flow: the half of a Google credential a person supplies.

Three of a Google Ads call's five values belong to the deployment — the
developer token, issued once in one manager account's API Center, and the OAuth
client id and secret from the Cloud project. The other two belong to a person:
the refresh token their consent mints, and the id of the account that consent
can actually see. Asking every colleague for a developer token to connect their
own ad account is asking most of them not to connect, so this module exists to
ask them for the only thing they can give.

What it does, in order:

1. `consent_url` mints an unguessable state, remembers what the trip was for in
   Redis under it, and returns the URL to send the browser to.
2. `consume_state` reads that back exactly once. Expired, replayed or forged all
   look the same from here, and all mean the same thing: start again.
3. `exchange` turns Google's one-use code into a refresh token, and reads the
   granted scopes and the signed-in address out of the same response.
4. `seal` / `unseal` put the refresh token on the connection row as AES-256-GCM
   ciphertext bound to that row's id.

Nothing here touches the database or knows what a `SourceConnection` is. The
routes do that; this is the conversation with Google.
"""

from __future__ import annotations

import json
import secrets
import uuid
from base64 import urlsafe_b64decode
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx
import structlog

from agent.api import API_PREFIX
from agent.config import Settings
from agent.crypto import DecryptionError, decrypt_str, encrypt
from agent.redis_client import get_redis

log = structlog.get_logger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 — an endpoint, not a secret
REVOKE_URL = "https://oauth2.googleapis.com/revoke"

#: What the consent asks for.
#:
#: `adwords` is the one this build spends. The other two are asked for in the
#: same breath because they are the same three APIs enabled on the same Cloud
#: project, and a scope added later costs every person who already connected a
#: second trip through the consent screen — Google issues a grant for what was
#: asked at the time and nothing widens it retroactively.
#:
#: `openid` and `email` are not sensitive and are not for reading anything: they
#: are how the card can say *which* Google account a workspace is connected as,
#: which is the first question asked when a grant stops working.
SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/adwords",
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/webmasters.readonly",
    "openid",
    "email",
)

#: The scope a Google Ads pull cannot happen without. Everything else on the
#: list widens what a later build may read; this one is the connection.
ADWORDS_SCOPE = SCOPES[0]

STATE_PREFIX = "google-oauth:"
#: Long enough to read a consent screen and choose an account; short enough that
#: a state abandoned in a closed tab is not a standing invitation.
STATE_TTL_S = 900

#: Where the person is sent back to when they started from somewhere unexpected.
DEFAULT_RETURN_TO = "/settings/connections"


class GoogleOAuthError(Exception):
    """Google refused, or answered something unusable. Always has a readable reason."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ConsentState:
    """What one trip through Google's consent screen was for."""

    workspace_id: uuid.UUID
    user_id: uuid.UUID
    return_to: str


@dataclass(frozen=True, slots=True)
class Grant:
    """What came back: the long-lived half, and what it is allowed to do."""

    refresh_token: str
    scopes: tuple[str, ...] = ()
    email: str = ""

    @property
    def reaches_google_ads(self) -> bool:
        """Whether the person actually ticked the Google Ads box.

        Google shows one checkbox per sensitive scope and returns a grant for
        whatever survived. A consent that came back without `adwords` is not a
        failure Google reports — it is a 200 with a narrower scope list — so it
        has to be checked here or it becomes "connected" and then empty.
        """
        return ADWORDS_SCOPE in self.scopes


@dataclass(slots=True)
class SealedGrant:
    """A grant on its way to, or back from, a connection row."""

    ciphertext: bytes
    nonce: bytes
    values: dict[str, str] = field(default_factory=dict)


def configured(settings: Settings) -> tuple[str, str] | None:
    """The deployment's OAuth client, or None if it has not got one.

    The same client that `make google-ads-oauth` uses, deliberately: a second
    client would be a second thing to register a redirect URI on and a second
    consent screen for a person to recognise.
    """
    client_id = (
        settings.google_ads_client_id.get_secret_value() if settings.google_ads_client_id else ""
    )
    client_secret = (
        settings.google_ads_client_secret.get_secret_value()
        if settings.google_ads_client_secret
        else ""
    )
    if not client_id.strip() or not client_secret.strip():
        return None
    return client_id.strip(), client_secret.strip()


def redirect_uri(settings: Settings) -> str:
    """Where Google sends the browser back.

    Built from `app_base_url`, never from the incoming request: Google compares
    this string byte for byte against the one registered in the Cloud console,
    and the API has no ingress of its own — every browser request arrives
    through the web app's rewrite, so the app's address is the only one that is
    both registrable and reachable.
    """
    return f"{settings.app_base_url.rstrip('/')}{API_PREFIX}/connections/google/callback"


def safe_return_to(path: str) -> str:
    """A path on this app, or the Connections screen.

    `return_to` arrives from the browser and leaves in a `Location` header, so
    anything that could name another origin — an absolute URL, a
    protocol-relative `//host`, a backslash Chrome normalises into a slash — is
    replaced rather than sanitised.
    """
    candidate = (path or "").strip()
    if not candidate.startswith("/") or candidate.startswith(("//", "/\\")):
        return DEFAULT_RETURN_TO
    return candidate


async def consent_url(settings: Settings, state_for: ConsentState) -> str:
    """Mint a one-use state and build the URL to send the browser to."""
    client = configured(settings)
    if client is None:
        raise GoogleOAuthError(
            "This deployment has no Google OAuth client. Set GOOGLE_ADS_CLIENT_ID "
            "and GOOGLE_ADS_CLIENT_SECRET in the environment."
        )
    state = secrets.token_urlsafe(32)
    # Plain JSON, and it is worth saying why: nothing secret travels in a state
    # any more. The developer token this used to carry is the deployment's now,
    # so what is left is a workspace id, a user id and a path — all of which the
    # person it belongs to already knows. The unguessable key is the protection.
    await get_redis().setex(
        f"{STATE_PREFIX}{state}",
        STATE_TTL_S,
        json.dumps(
            {
                "workspace_id": str(state_for.workspace_id),
                "user_id": str(state_for.user_id),
                "return_to": safe_return_to(state_for.return_to),
            }
        ),
    )
    query = urlencode(
        {
            "client_id": client[0],
            "redirect_uri": redirect_uri(settings),
            "response_type": "code",
            "scope": " ".join(SCOPES),
            # Offline or there is no refresh token at all; `consent` because
            # Google reissues one only on a first-ever grant and the second
            # person to connect would otherwise get an access token that
            # expires in an hour; `select_account` because a browser already
            # signed in as the wrong Google user would skip the chooser and
            # authorise an account with no Google Ads in it.
            "access_type": "offline",
            "prompt": "consent select_account",
            "include_granted_scopes": "true",
            "state": state,
        }
    )
    return f"{AUTH_URL}?{query}"


async def consume_state(state: str) -> ConsentState | None:
    """Read a state back, exactly once. None means start again."""
    if not state:
        return None
    raw = await get_redis().getdel(f"{STATE_PREFIX}{state}")
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
        return ConsentState(
            workspace_id=uuid.UUID(payload["workspace_id"]),
            user_id=uuid.UUID(payload["user_id"]),
            return_to=safe_return_to(payload.get("return_to", "")),
        )
    except (ValueError, KeyError, TypeError):
        return None


async def exchange(settings: Settings, code: str) -> Grant:
    """Turn Google's one-use code into a refresh token.

    A successful exchange that carries no `refresh_token` is the common failure
    and the confusing one: it means this Google account already has a live grant
    for this client, so Google returned an access token and nothing durable. The
    message says how to clear it rather than asking the person to try again,
    which would produce exactly the same answer.
    """
    client = configured(settings)
    if client is None:
        raise GoogleOAuthError("This deployment has no Google OAuth client.")
    client_id, client_secret = client
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as http:
        response = await http.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri(settings),
                "grant_type": "authorization_code",
            },
        )
    if response.status_code >= 400:
        log.warning("google_oauth.exchange_failed", status=response.status_code)
        raise GoogleOAuthError(
            f"Google refused the authorisation ({response.status_code}). "
            "The most common cause is a redirect URI the Cloud console does not "
            f"have registered: {redirect_uri(settings)}"
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise GoogleOAuthError("Google's answer could not be read.") from exc

    refresh_token = str(body.get("refresh_token") or "")
    if not refresh_token:
        raise GoogleOAuthError(
            "Google returned no refresh token, which means this Google account "
            "already has a live grant for this app. Remove it at "
            "myaccount.google.com/permissions and connect again."
        )
    return Grant(
        refresh_token=refresh_token,
        scopes=tuple(str(body.get("scope") or "").split()),
        email=_email_from_id_token(str(body.get("id_token") or "")),
    )


async def revoke(refresh_token: str) -> bool:
    """Ask Google to forget this grant. Best effort, and never fatal.

    Deleting our row is what disconnecting means; this is the courtesy of not
    leaving a live grant on somebody's Google account afterwards. A failure here
    must not stop a disconnection — the person asked for this source to be off,
    and it is off whatever Google says.
    """
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as http:
            response = await http.post(REVOKE_URL, data={"token": refresh_token})
        return response.status_code < 400
    except httpx.HTTPError as exc:
        log.info("google_oauth.revoke_failed", error=str(exc))
        return False


def seal(values: dict[str, str], *, connection_id: uuid.UUID) -> tuple[bytes, bytes]:
    """Seal the secret half of a grant against one connection row."""
    return encrypt(json.dumps(values), aad=_aad(connection_id))


def unseal(
    ciphertext: bytes | None, nonce: bytes | None, *, connection_id: uuid.UUID
) -> dict[str, str]:
    """Open a sealed grant, or `{}` if there is none.

    A ciphertext that will not open is returned as no grant rather than raised.
    It means the encryption key changed or the row was moved, and both are
    "this workspace has to sign in again" — which is what an absent grant
    already says, through a path every caller handles.
    """
    if not ciphertext or not nonce:
        return {}
    try:
        opened: Any = json.loads(decrypt_str(ciphertext, nonce, aad=_aad(connection_id)))
    except (DecryptionError, ValueError):
        log.warning("google_oauth.grant_unreadable", connection_id=str(connection_id))
        return {}
    if not isinstance(opened, dict):
        return {}
    return {str(key): str(value) for key, value in opened.items() if value}


def _aad(connection_id: uuid.UUID) -> bytes:
    return f"source_connection:{connection_id}".encode()


def _email_from_id_token(id_token: str) -> str:
    """The signed-in address, read out of the id token Google just handed us.

    Not verified, and it does not need to be: this token arrived as the body of
    a server-to-server POST to Google over TLS, authenticated with our client
    secret. There is no third party in that exchange to forge it. Verifying a
    signature here would mean fetching and caching Google's JWKS to re-prove
    something the transport already proved.

    Used for one thing — printing which account a workspace is connected as — so
    an unreadable token costs a line on a card and nothing else.
    """
    parts = id_token.split(".")
    if len(parts) != 3:
        return ""
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(urlsafe_b64decode(padded))
    except (ValueError, TypeError):
        return ""
    return str(claims.get("email") or "") if isinstance(claims, dict) else ""
