"""The consent mechanics, without a browser and without Google.

Everything here is pure enough to run on its own: building a URL, refusing a
`return_to` that names another origin, reading an email out of an id token,
and sealing a refresh token against one row. The round trip through the API is
`tests/integration/test_google_oauth_api.py`; this is the half that has to be
right before that one can be.
"""

from __future__ import annotations

import base64
import json
import uuid
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from agent import google_oauth
from agent.config import Settings, get_settings
from agent.crypto import DecryptionError, decrypt_str

CLIENT_ID = "248255318361-test.apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-test-secret"  # noqa: S105 — a fixture, not a credential


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_settings` writes through this, so every variable is undone afterwards."""
    global _set_env
    _set_env = monkeypatch.setenv


_set_env: Any = None


def _settings(**values: str) -> Settings:
    for name, value in values.items():
        _set_env(name, value)
    get_settings.cache_clear()
    return get_settings()


class _FakeRedis:
    """Enough Redis for a one-use state: `setex` in, `getdel` out.

    A real one is not worth a container in the unit suite, and the two calls
    this module makes are the whole contract — the TTL is Redis's business and
    the integration suite is what runs against a real server.
    """

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self.store[key] = value.encode()

    async def getdel(self, key: str) -> bytes | None:
        return self.store.pop(key, None)


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    fake = _FakeRedis()
    monkeypatch.setattr(google_oauth, "get_redis", lambda: fake)
    return fake


# --- the deployment's half ---------------------------------------------------


def test_a_deployment_without_an_oauth_client_has_none() -> None:
    """Rather than building a consent URL with an empty `client_id` in it."""
    assert google_oauth.configured(_settings()) is None
    assert google_oauth.configured(_settings(GOOGLE_ADS_CLIENT_ID=CLIENT_ID)) is None
    assert (
        google_oauth.configured(
            _settings(GOOGLE_ADS_CLIENT_ID=CLIENT_ID, GOOGLE_ADS_CLIENT_SECRET="   ")
        )
        is None
    )


def test_the_redirect_uri_is_the_browser_address_not_the_api() -> None:
    """Google compares this byte for byte, and `api` has no ingress of its own.

    Every browser request arrives through the web app's rewrite, so the app's
    own base URL is the only address that is both registrable in the Cloud
    console and actually reachable.
    """
    settings = _settings(APP_BASE_URL="https://ads.example.com")
    assert (
        google_oauth.redirect_uri(settings)
        == "https://ads.example.com/api/v1/connections/google/callback"
    )
    # A trailing slash on the base URL would otherwise produce a double slash,
    # which Google treats as a different string and refuses.
    assert google_oauth.redirect_uri(_settings(APP_BASE_URL="https://ads.example.com/")) == (
        "https://ads.example.com/api/v1/connections/google/callback"
    )


def test_the_scopes_include_google_ads_and_say_who_signed_in() -> None:
    assert google_oauth.ADWORDS_SCOPE == "https://www.googleapis.com/auth/adwords"
    assert google_oauth.ADWORDS_SCOPE in google_oauth.SCOPES
    assert "https://www.googleapis.com/auth/analytics.readonly" in google_oauth.SCOPES
    assert "https://www.googleapis.com/auth/webmasters.readonly" in google_oauth.SCOPES
    assert "email" in google_oauth.SCOPES


# --- where a person is sent back to -----------------------------------------


@pytest.mark.parametrize(
    "candidate",
    [
        "https://evil.example.com/steal",
        "//evil.example.com/steal",
        "/\\evil.example.com",
        "evil.example.com",
        "",
        "   ",
    ],
)
def test_a_return_to_that_could_name_another_origin_is_replaced(candidate: str) -> None:
    """It arrives from the browser and leaves in a `Location` header."""
    assert google_oauth.safe_return_to(candidate) == google_oauth.DEFAULT_RETURN_TO


def test_a_path_on_this_app_survives() -> None:
    assert google_oauth.safe_return_to("/settings/connections?tab=x") == (
        "/settings/connections?tab=x"
    )


# --- the consent URL ---------------------------------------------------------


async def test_the_consent_url_asks_for_an_offline_grant(fake_redis: _FakeRedis) -> None:
    settings = _settings(
        GOOGLE_ADS_CLIENT_ID=CLIENT_ID,
        GOOGLE_ADS_CLIENT_SECRET=CLIENT_SECRET,
        APP_BASE_URL="http://localhost:3000",
    )
    url = await google_oauth.consent_url(
        settings,
        google_oauth.ConsentState(
            workspace_id=uuid.uuid4(), user_id=uuid.uuid4(), return_to="/settings/connections"
        ),
    )

    parsed = urlparse(url)
    query = {key: value[0] for key, value in parse_qs(parsed.query).items()}
    assert parsed.netloc == "accounts.google.com"
    assert query["client_id"] == CLIENT_ID
    assert query["scope"].split() == list(google_oauth.SCOPES)
    # Without both of these Google reissues a refresh token only on a first-ever
    # grant, and the second person to connect gets an access token that expires
    # in an hour and nothing durable behind it.
    assert query["access_type"] == "offline"
    assert "consent" in query["prompt"]
    assert "select_account" in query["prompt"]
    assert query["redirect_uri"] == "http://localhost:3000/api/v1/connections/google/callback"
    assert len(query["state"]) >= 32


async def test_a_state_is_one_use(fake_redis: _FakeRedis) -> None:
    settings = _settings(GOOGLE_ADS_CLIENT_ID=CLIENT_ID, GOOGLE_ADS_CLIENT_SECRET=CLIENT_SECRET)
    workspace_id = uuid.uuid4()
    url = await google_oauth.consent_url(
        settings,
        google_oauth.ConsentState(
            workspace_id=workspace_id, user_id=uuid.uuid4(), return_to="/settings/connections"
        ),
    )
    state = parse_qs(urlparse(url).query)["state"][0]

    first = await google_oauth.consume_state(state)
    assert first is not None
    assert first.workspace_id == workspace_id
    assert await google_oauth.consume_state(state) is None


async def test_a_state_nobody_minted_is_simply_not_there(fake_redis: _FakeRedis) -> None:
    _settings()
    assert await google_oauth.consume_state("not-a-real-state") is None
    assert await google_oauth.consume_state("") is None


# --- sealing -----------------------------------------------------------------


def test_a_sealed_grant_opens_only_against_its_own_row() -> None:
    """The AAD is the connection id, so a ciphertext copied to another
    workspace's row fails to open rather than quietly authorising as somebody
    else."""
    _settings()
    mine, yours = uuid.uuid4(), uuid.uuid4()

    ciphertext, nonce = google_oauth.seal({"refresh_token": "1//secret"}, connection_id=mine)

    assert google_oauth.unseal(ciphertext, nonce, connection_id=mine) == {
        "refresh_token": "1//secret"
    }
    assert google_oauth.unseal(ciphertext, nonce, connection_id=yours) == {}
    with pytest.raises(DecryptionError):
        decrypt_str(ciphertext, nonce, aad=b"source_connection:wrong")


def test_no_grant_reads_as_no_grant_rather_than_raising() -> None:
    """A row with nothing sealed on it is every source but Google Ads."""
    _settings()
    assert google_oauth.unseal(None, None, connection_id=uuid.uuid4()) == {}
    assert google_oauth.unseal(b"", b"", connection_id=uuid.uuid4()) == {}


def test_a_grant_sealed_under_a_different_key_reads_as_absent() -> None:
    """Rotating `APP_ENCRYPTION_KEY` must mean "sign in again", not a 500.

    Absent is a state every caller already handles, and the fix it points at —
    press the button — is the correct one.
    """
    connection_id = uuid.uuid4()
    _settings()
    ciphertext, nonce = google_oauth.seal(
        {"refresh_token": "1//secret"}, connection_id=connection_id
    )

    _settings(APP_ENCRYPTION_KEY=base64.b64encode(b"a" * 32).decode())
    assert google_oauth.unseal(ciphertext, nonce, connection_id=connection_id) == {}


# --- what a grant says about itself -----------------------------------------


def test_a_consent_that_left_google_ads_unticked_is_not_a_connection() -> None:
    """Google renders one checkbox per sensitive scope and answers 200 for
    whatever survived. A grant without `adwords` would connect and read
    nothing."""
    assert google_oauth.Grant(
        refresh_token="1//x", scopes=(google_oauth.ADWORDS_SCOPE,)
    ).reaches_google_ads
    assert not google_oauth.Grant(
        refresh_token="1//x", scopes=("https://www.googleapis.com/auth/analytics.readonly",)
    ).reaches_google_ads
    assert not google_oauth.Grant(refresh_token="1//x").reaches_google_ads


def test_the_signed_in_address_is_read_out_of_the_id_token() -> None:
    claims = base64.urlsafe_b64encode(json.dumps({"email": "ops@example.com"}).encode()).decode()
    # Deliberately stripped of padding, which is how a real JWT arrives.
    token = f"header.{claims.rstrip('=')}.signature"

    assert google_oauth._email_from_id_token(token) == "ops@example.com"


@pytest.mark.parametrize(
    "token", ["", "not-a-jwt", "a.b", "a.!!!!.c", f"a.{base64.urlsafe_b64encode(b'[]').decode()}.c"]
)
def test_an_unreadable_id_token_costs_a_line_on_a_card_and_nothing_else(token: str) -> None:
    """It is used for one thing — printing which account is connected — so it
    must never be able to fail a sign-in."""
    assert google_oauth._email_from_id_token(token) == ""
