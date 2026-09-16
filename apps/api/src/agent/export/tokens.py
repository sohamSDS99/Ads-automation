"""Signed, expiring capabilities for one object on the worker's file server.

`api` streams a download from `worker` over the private network (PRD §12). That
hop needs authorisation of its own. The alternative — trusting the private
network — means any process that can resolve `worker.railway.internal` can read
any path under the Volume by guessing a storage key, and storage keys contain
run ids that appear in URLs.

A token authorises **one key, for a short window, and nothing else**. It is not
a session: it carries no identity, grants no other object, and cannot be widened
by editing it, because the key it covers is inside the signature.

Format: `{expiry}.{hex digest}`, where the digest is
`HMAC-SHA256(secret, "v1\\n{key}\\n{expiry}")`. Version-prefixing the signed
message means a future format change cannot be replayed as this one.

The secret is `FILE_TOKEN_SECRET` when set. When it is not — and it is not set
in any environment provisioned before this phase — it is derived from
`APP_ENCRYPTION_KEY`, which is validated at startup and always present. That is
a deliberate choice between two failure modes: a new required variable turns
every existing deploy into a broken download, whereas a derived key is as strong
as the key it comes from and costs nothing. What it never does is fall back to
*no* authentication.
"""

from __future__ import annotations

import hashlib
import hmac
import time

from agent.config import Settings, get_settings

#: Signed-message version. Bump when the message layout changes.
VERSION = "v1"

#: How long a download token lives. Long enough for `api` to make the request it
#: just signed, short enough that a token in a log line is worthless by the time
#: anyone reads it. This is a server-to-server hop, not a link a human holds.
DEFAULT_TTL_SECONDS = 120

#: Domain separator for the derived secret. Changing this rotates every token.
_DERIVATION_INFO = b"ads-research-agent/file-token/v1"


class TokenError(Exception):
    """A token was absent, malformed, expired, or did not verify."""


def _secret(settings: Settings) -> bytes:
    configured = settings.file_token_secret.get_secret_value()
    if configured:
        return configured.encode("utf-8")
    # Derived, never absent. See the module docstring.
    return hmac.new(settings.encryption_key, _DERIVATION_INFO, hashlib.sha256).digest()


def _digest(secret: bytes, key: str, expiry: int) -> str:
    message = f"{VERSION}\n{key}\n{expiry}".encode()
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def sign(
    key: str,
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    settings: Settings | None = None,
    now: int | None = None,
) -> str:
    """Mint a token authorising a read of `key` for the next `ttl_seconds`."""
    settings = settings or get_settings()
    expiry = (now if now is not None else int(time.time())) + ttl_seconds
    return f"{expiry}.{_digest(_secret(settings), key, expiry)}"


def verify(
    key: str,
    token: str,
    *,
    settings: Settings | None = None,
    now: int | None = None,
) -> None:
    """Raise `TokenError` unless `token` authorises `key` right now.

    Expiry is checked *after* the signature. Checking it first would answer
    "is this token still valid?" for an attacker holding an unsigned guess,
    which is a free oracle for nothing gained.
    """
    settings = settings or get_settings()
    if not token:
        raise TokenError("no token supplied")

    expiry_text, _, digest = token.partition(".")
    if not digest:
        raise TokenError("malformed token")
    try:
        expiry = int(expiry_text)
    except ValueError as exc:
        raise TokenError("malformed token") from exc

    expected = _digest(_secret(settings), key, expiry)
    if not hmac.compare_digest(expected, digest):
        raise TokenError("bad signature")

    current = now if now is not None else int(time.time())
    if expiry < current:
        raise TokenError("token expired")


def is_valid(key: str, token: str, *, settings: Settings | None = None) -> bool:
    """`verify` as a predicate, for call sites that do not want the exception."""
    try:
        verify(key, token, settings=settings)
    except TokenError:
        return False
    return True
