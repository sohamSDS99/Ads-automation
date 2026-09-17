"""The Google Ads consent round trip, through the API.

Two properties are worth the stack these tests need. The first is that the
developer token survives the round trip without ever being readable: it is typed
before consent and sealed after it, and in between it sits in Redis under a
state key. The second is that every way the trip can fail ends as a redirect the
person can read, not as a problem document their browser will render as JSON.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from agent.redis_client import get_redis
from tests.integration.conftest import ApiClient

DEVELOPER_TOKEN = "dev-token-not-real-22ch"
CLIENT_ID = "248255318361-test.apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-test-secret"  # noqa: S105 — a fixture, not a credential


@pytest.fixture
def oauth_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deployment that has an OAuth client, which is the environment's business."""
    from agent.config import get_settings

    monkeypatch.setenv("GOOGLE_ADS_OAUTH_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("GOOGLE_ADS_OAUTH_CLIENT_SECRET", CLIENT_SECRET)
    get_settings.cache_clear()


async def start(admin: ApiClient, **overrides: Any) -> Any:
    body = {"developer_token": DEVELOPER_TOKEN, "return_to": "/settings", **overrides}
    return await admin.post("/credentials/google-ads/authorize", json=body)


async def test_a_deployment_without_an_oauth_client_says_so(admin: ApiClient) -> None:
    """Rather than sending someone to a consent screen that cannot work."""
    response = await start(admin)

    assert response.status_code == 422, response.text
    assert "GOOGLE_ADS_OAUTH_CLIENT_ID" in response.text


async def test_the_consent_url_asks_google_for_what_the_connector_needs(
    admin: ApiClient, oauth_client: None
) -> None:
    response = await start(admin)
    assert response.status_code == 200, response.text

    url = urlparse(response.json()["url"])
    query = {key: value[0] for key, value in parse_qs(url.query).items()}
    assert url.netloc == "accounts.google.com"
    assert query["client_id"] == CLIENT_ID
    assert query["scope"] == "https://www.googleapis.com/auth/adwords"
    # Without both of these Google reissues a refresh token only on a first-ever
    # grant, and the second person to connect gets an access token that expires.
    assert query["access_type"] == "offline"
    assert "consent" in query["prompt"]
    # The redirect must be the browser's address, not the API's: `api` has no
    # ingress, so every request arrives through the web app's rewrite.
    assert query["redirect_uri"] == ("http://localhost:3000/api/v1/credentials/google-ads/callback")


async def test_the_developer_token_is_not_readable_while_it_waits(
    admin: ApiClient, oauth_client: None
) -> None:
    """It spends fifteen minutes in Redis between the form and the callback.
    Fifteen minutes is still at rest."""
    response = await start(admin)
    state = {
        key: value[0] for key, value in parse_qs(urlparse(response.json()["url"]).query).items()
    }["state"]

    stored = await get_redis().get(f"google-ads-oauth:{state}")
    assert stored is not None
    assert DEVELOPER_TOKEN.encode() not in stored


async def test_only_a_credential_writer_may_start_one(
    signed_in_as: Any, oauth_client: None
) -> None:
    operator = await signed_in_as("operator")
    response = await start(operator)

    assert response.status_code == 403, response.text


async def test_a_state_that_is_not_ours_ends_as_a_readable_redirect(
    admin: ApiClient, oauth_client: None
) -> None:
    """Expired, already used, or forged — all three mean start again, and the
    person is in a browser, so they must be told in a page and not in JSON."""
    response = await admin.get(
        "/credentials/google-ads/callback?code=4/abc&state=not-a-real-state",
        follow_redirects=False,
    )

    assert response.status_code == 303
    location = urlparse(response.headers["location"])
    assert location.path == "/settings"
    assert parse_qs(location.query)["google_ads"] == ["error"]


async def test_a_used_state_cannot_be_replayed(admin: ApiClient, oauth_client: None) -> None:
    """The state is one-use: the callback consumes it before it does anything
    that could be repeated."""
    started = await start(admin)
    state = {
        key: value[0] for key, value in parse_qs(urlparse(started.json()["url"]).query).items()
    }["state"]

    first = await admin.get(
        f"/credentials/google-ads/callback?error=access_denied&state={state}",
        follow_redirects=False,
    )
    second = await admin.get(
        f"/credentials/google-ads/callback?error=access_denied&state={state}",
        follow_redirects=False,
    )

    assert first.status_code == 303
    assert "access_denied" in first.headers["location"]
    # The second attempt no longer knows which screen it came from, which is
    # exactly what a consumed state should look like.
    assert second.status_code == 303
    assert "This+consent+link+has+expired" in second.headers["location"]
