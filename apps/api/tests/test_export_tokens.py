"""Download capabilities (PRD §12).

A token authorises one storage key for a short window. These tests are about the
ways that could quietly stop being true: a signature that does not cover the
key, an expiry nobody checks, a secret that silently becomes empty.
"""

from __future__ import annotations

import time

import pytest

from agent.config import Settings, get_settings
from agent.export.tokens import (
    DEFAULT_TTL_SECONDS,
    TokenError,
    is_valid,
    sign,
    verify,
)

KEY = "exports/22222222-2222-4222-8222-222222222222/report.pdf"
OTHER = "exports/22222222-2222-4222-8222-222222222222/other.pdf"


def test_a_fresh_token_verifies() -> None:
    verify(KEY, sign(KEY))


def test_a_token_is_worthless_for_another_key() -> None:
    """The key is inside the signature, so a token cannot be widened by editing it."""
    token = sign(KEY)
    assert is_valid(KEY, token)
    assert not is_valid(OTHER, token)
    assert not is_valid(KEY + "/../secrets", token)


def test_a_tampered_signature_is_rejected() -> None:
    expiry, _, digest = sign(KEY).partition(".")
    flipped = digest[:-1] + ("0" if digest[-1] != "0" else "1")
    assert not is_valid(KEY, f"{expiry}.{flipped}")


def test_extending_the_expiry_invalidates_the_signature() -> None:
    """The expiry is signed, not merely attached."""
    expiry, _, digest = sign(KEY).partition(".")
    assert not is_valid(KEY, f"{int(expiry) + 86_400}.{digest}")


def test_an_expired_token_is_rejected() -> None:
    with pytest.raises(TokenError, match="expired"):
        verify(KEY, sign(KEY, ttl_seconds=-1))


def test_expiry_is_checked_after_the_signature() -> None:
    """An unsigned guess must not learn whether its expiry would have been valid."""
    with pytest.raises(TokenError, match="bad signature"):
        verify(KEY, f"{int(time.time()) - 10}.{'0' * 64}")


def test_malformed_tokens_are_rejected_without_raising_something_else() -> None:
    for candidate in ("", "nonsense", ".", "abc.def", f"{'x' * 10}.{'0' * 64}"):
        assert not is_valid(KEY, candidate), candidate


def test_the_default_ttl_is_short() -> None:
    """This is a server-to-server hop, not a link a human holds."""
    assert DEFAULT_TTL_SECONDS <= 300


def test_an_unset_secret_derives_one_rather_than_disabling_authentication() -> None:
    """FILE_TOKEN_SECRET was added with an empty default; unset must still sign."""
    settings = get_settings()
    assert settings.file_token_secret.get_secret_value() == ""

    token = sign(KEY, settings=settings)
    verify(KEY, token, settings=settings)
    assert not is_valid(OTHER, token, settings=settings)


def test_a_configured_secret_is_used_and_rotates_every_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    derived = sign(KEY)

    monkeypatch.setenv("FILE_TOKEN_SECRET", "a-configured-signing-secret")
    get_settings.cache_clear()
    configured = get_settings()
    assert configured.file_token_secret.get_secret_value() == "a-configured-signing-secret"

    # Tokens minted under the old secret stop verifying, which is the point of
    # being able to set one.
    assert not is_valid(KEY, derived, settings=configured)
    verify(KEY, sign(KEY, settings=configured), settings=configured)


def test_changing_the_encryption_key_rotates_derived_tokens() -> None:
    """The derived secret is only as stable as the key it comes from."""
    import base64

    first = Settings(app_encryption_key=base64.b64encode(b"a" * 32).decode())
    second = Settings(app_encryption_key=base64.b64encode(b"b" * 32).decode())

    token = sign(KEY, settings=first)
    verify(KEY, token, settings=first)
    assert not is_valid(KEY, token, settings=second)
