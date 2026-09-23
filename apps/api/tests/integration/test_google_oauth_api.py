"""The Google consent round trip, through the API.

Four properties are worth the stack these tests need.

The refresh token is never readable. It arrives from Google, is sealed onto the
connection row, and no endpoint returns it — not the list, not the card, not
the audit log.

Every way the trip can fail ends as a redirect a person can read. The caller is
a browser arriving from another origin, so a problem document is a blank page
with some punctuation on it.

Anybody in the workspace can do it. That is the whole point: a developer token
is issued once to one manager account, and requiring one per colleague is
requiring most of them never to connect.

And a grant is what makes the source usable. A row without one resolves to
`MissingCredential`, so a run skips Google Ads and says why, rather than
pulling nothing from an account it never had.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent import google_oauth
from agent.config import get_settings
from agent.credentials import MissingCredential, resolve_values
from agent.db.models import AuditLog, CredentialKind, SourceConnection
from agent.redis_client import get_redis
from tests.integration.conftest import ApiClient

REFRESH_TOKEN = "1//04-not-a-real-refresh-token"  # noqa: S105 — a fixture
DEPLOYMENT_ENV = {
    "GOOGLE_ADS_DEVELOPER_TOKEN": "dev-token-not-real",
    "GOOGLE_ADS_CLIENT_ID": "248255318361-test.apps.googleusercontent.com",
    "GOOGLE_ADS_CLIENT_SECRET": "GOCSPX-test-secret",
}

#: One manager and two accounts under it, which is the shape that used to be got
#: wrong: `listAccessibleCustomers` answers with the MCC alone, and an MCC holds
#: no campaigns.
ACCOUNTS = [
    {
        "customer_id": "1112223330",
        "name": "Agency MCC",
        "manager": True,
        "via_manager": None,
        "currency": "EUR",
    },
    {
        "customer_id": "4445556660",
        "name": "SDS Manager",
        "manager": False,
        "via_manager": "1112223330",
        "currency": "EUR",
    },
    {
        "customer_id": "7778889990",
        "name": "SDS Analytics",
        "manager": False,
        "via_manager": "1112223330",
        "currency": "USD",
    },
]


@pytest.fixture
def deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deployment holding the half a person cannot supply."""
    for name, value in DEPLOYMENT_ENV.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()


@pytest.fixture
def google(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Google, as far as this round trip is concerned: a code, and some accounts.

    Stubbed at the two seams the callback actually crosses. Reaching the real
    endpoints would make this suite fail for reasons that have nothing to do
    with the code, and the shape of a token response is covered against a mock
    transport in `tests/test_google_oauth.py`.
    """
    seen: dict[str, Any] = {"accounts_called": 0}

    async def exchange(_settings: Any, code: str) -> google_oauth.Grant:
        seen["code"] = code
        return google_oauth.Grant(
            refresh_token=REFRESH_TOKEN,
            scopes=google_oauth.SCOPES,
            email="ops@example.com",
        )

    async def accessible(self: Any) -> list[dict[str, Any]]:
        seen["accounts_called"] += 1
        seen["credentials"] = dict(self.context.credentials)
        return [dict(row) for row in ACCOUNTS]

    monkeypatch.setattr("agent.api.routes_connections.google_oauth.exchange", exchange)
    monkeypatch.setattr(
        "agent.connectors.google_ads.GoogleAdsConnector.accessible_accounts", accessible
    )
    return seen


async def begin(client: ApiClient, return_to: str = "/settings/connections") -> Any:
    return await client.post("/connections/google/authorize", json={"return_to": return_to})


def state_from(response: Any) -> str:
    return parse_qs(urlparse(response.json()["url"]).query)["state"][0]


async def finish(client: ApiClient, state: str, **params: str) -> Any:
    query = "&".join(f"{key}={value}" for key, value in {"state": state, **params}.items())
    return await client.get(f"/connections/google/callback?{query}", follow_redirects=False)


async def sign_in(client: ApiClient) -> Any:
    """The whole happy path, for the tests that need a connected workspace."""
    return await finish(client, state_from(await begin(client)), code="4/auth-code")


# --- starting ----------------------------------------------------------------


async def test_a_deployment_without_the_developer_token_refuses_before_google(
    admin: ApiClient,
) -> None:
    """Rather than after a person has chosen an account and approved three scopes."""
    response = await begin(admin)

    assert response.status_code == 422, response.text
    assert "GOOGLE_ADS_DEVELOPER_TOKEN" in response.text


async def test_the_consent_url_is_built_for_this_deployment(
    admin: ApiClient, deployment: None
) -> None:
    response = await begin(admin)
    assert response.status_code == 200, response.text

    url = urlparse(response.json()["url"])
    query = {key: value[0] for key, value in parse_qs(url.query).items()}
    assert url.netloc == "accounts.google.com"
    assert query["client_id"] == DEPLOYMENT_ENV["GOOGLE_ADS_CLIENT_ID"]
    assert google_oauth.ADWORDS_SCOPE in query["scope"]
    assert query["access_type"] == "offline"
    # The browser's address, not the API's: `api` has no ingress, so every
    # request arrives through the web app's rewrite.
    assert query["redirect_uri"].endswith("/api/v1/connections/google/callback")


async def test_anybody_in_the_workspace_may_start_one(signed_in_as: Any, deployment: None) -> None:
    """The distinction this whole feature turns on.

    Switching a source on spends the organisation's developer token, so it needs
    an admin. Signing in with your own Google account does not, and gating it
    behind one is what left this source unconnected: only the person holding the
    developer token could ever finish it.
    """
    for role in ("operator", "approver", "viewer"):
        caller = await signed_in_as(role)
        response = await begin(caller)
        assert response.status_code == 200, f"{role}: {response.text}"


async def test_nothing_in_a_waiting_state_is_worth_stealing(
    admin: ApiClient, deployment: None
) -> None:
    """It sits in Redis for fifteen minutes between the button and the callback.

    What it holds is a workspace id, a user id and a path — all of which belong
    to the person it was minted for. The secret arrives later, from Google, and
    never passes through here.
    """
    stored = await get_redis().get(f"google-oauth:{state_from(await begin(admin))}")

    assert stored is not None
    payload = json.loads(stored)
    assert set(payload) == {"workspace_id", "user_id", "return_to"}


# --- the ways it fails -------------------------------------------------------


async def test_a_state_that_is_not_ours_ends_as_a_readable_redirect(
    admin: ApiClient, deployment: None
) -> None:
    """Expired, already used, or forged — all three mean start again, and the
    person is in a browser, so they must be told in a page and not in JSON."""
    response = await finish(admin, "not-a-real-state", code="4/auth-code")

    assert response.status_code == 303
    location = urlparse(response.headers["location"])
    assert location.path == "/settings/connections"
    assert parse_qs(location.query)["google"] == ["error"]
    assert "expired" in parse_qs(location.query)["reason"][0]


async def test_a_used_state_cannot_be_replayed(admin: ApiClient, deployment: None) -> None:
    """The callback consumes it before it does anything that could be repeated."""
    state = state_from(await begin(admin))

    first = await finish(admin, state, error="access_denied")
    second = await finish(admin, state, error="access_denied")

    assert "access_denied" in first.headers["location"]
    assert "expired" in second.headers["location"]


async def test_declining_at_google_says_so_rather_than_failing_silently(
    admin: ApiClient, deployment: None
) -> None:
    response = await finish(admin, state_from(await begin(admin)), error="access_denied")

    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["google"] == ["error"]
    assert "access_denied" in query["reason"][0]


async def test_a_consent_without_the_google_ads_box_is_refused(
    admin: ApiClient, deployment: None, google: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Google renders one checkbox per sensitive scope and answers 200 for
    whatever survived. Accepting this would connect a source that reads
    nothing."""

    async def narrow(_settings: Any, _code: str) -> google_oauth.Grant:
        return google_oauth.Grant(
            refresh_token=REFRESH_TOKEN,
            scopes=("https://www.googleapis.com/auth/analytics.readonly",),
            email="ops@example.com",
        )

    monkeypatch.setattr("agent.api.routes_connections.google_oauth.exchange", narrow)

    response = await sign_in(admin)

    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["google"] == ["error"]
    assert "Google Ads" in query["reason"][0]
    assert google["accounts_called"] == 0


async def test_a_grant_that_reaches_no_ad_accounts_is_refused(
    admin: ApiClient, deployment: None, google: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Signing in with the wrong Google account is the commonest mistake, and it
    used to produce a connected card and empty pulls."""

    async def none(_self: Any) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr("agent.connectors.google_ads.GoogleAdsConnector.accessible_accounts", none)

    response = await sign_in(admin)

    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["google"] == ["error"]
    assert "ops@example.com" in query["reason"][0]


async def test_a_consent_minted_for_another_workspace_is_refused(
    admin: ApiClient, deployment: None, google: dict[str, Any]
) -> None:
    """The state is unguessable, but it is not a capability.

    Whoever finishes a consent must be in the workspace that started it —
    otherwise a state leaked out of one workspace's browser would attach a
    Google account to whichever workspace redeemed it.
    """
    settings = get_settings()
    url = await google_oauth.consent_url(
        settings,
        google_oauth.ConsentState(
            workspace_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            return_to="/settings/connections",
        ),
    )
    foreign = parse_qs(urlparse(url).query)["state"][0]

    response = await finish(admin, foreign, code="4/auth-code")

    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["google"] == ["error"]
    assert "another workspace" in query["reason"][0]
    assert google["accounts_called"] == 0


async def test_the_callback_needs_a_session(client: ApiClient, deployment: None) -> None:
    """A signed-out browser arriving from Google is sent to sign in, not given a
    workspace to attach an account to."""
    response = await client.get(
        "/connections/google/callback?code=4/auth-code&state=whatever", follow_redirects=False
    )

    assert response.status_code == 401, response.text


# --- the happy path ----------------------------------------------------------


async def test_signing_in_connects_the_source_and_picks_a_real_account(
    admin: ApiClient, deployment: None, google: dict[str, Any], db: AsyncSession
) -> None:
    """A manager account holds no campaigns, so it is the last resort, not the
    default. Picking it is the defect that made every pull come back empty."""
    response = await sign_in(admin)

    assert response.status_code == 303
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["google"] == ["connected"]
    assert query["account"] == ["SDS Manager"]
    assert google["code"] == "4/auth-code"
    # The listing ran with the deployment's half and the person's half joined.
    assert google["credentials"]["developer_token"] == DEPLOYMENT_ENV["GOOGLE_ADS_DEVELOPER_TOKEN"]
    assert google["credentials"]["refresh_token"] == REFRESH_TOKEN

    row = (
        await db.execute(
            sa.select(SourceConnection).where(SourceConnection.kind == CredentialKind.GOOGLE_ADS)
        )
    ).scalar_one()
    assert row.grant_ciphertext is not None
    assert row.granted_at is not None
    assert row.meta["customer_id"] == "4445556660"
    assert row.meta["login_customer_id"] == "1112223330"
    assert row.meta["google_email"] == "ops@example.com"
    assert len(row.meta["accessible"]) == 3


async def test_the_refresh_token_is_never_readable_afterwards(
    admin: ApiClient, deployment: None, google: dict[str, Any], db: AsyncSession
) -> None:
    await sign_in(admin)

    listing = await admin.get("/connections")
    audit = await admin.get("/audit")
    row = (
        await db.execute(
            sa.select(SourceConnection).where(SourceConnection.kind == CredentialKind.GOOGLE_ADS)
        )
    ).scalar_one()

    assert REFRESH_TOKEN not in listing.text
    assert REFRESH_TOKEN not in audit.text
    assert REFRESH_TOKEN.encode() not in bytes(row.grant_ciphertext or b"")
    assert json.dumps(row.meta).count(REFRESH_TOKEN) == 0


async def test_the_card_reports_who_signed_in_and_what_they_granted(
    admin: ApiClient, deployment: None, google: dict[str, Any]
) -> None:
    await sign_in(admin)

    sources = {row["kind"]: row for row in (await admin.get("/connections")).json()["sources"]}
    oauth = sources["google_ads"]["oauth"]

    assert sources["google_ads"]["connected"] is True
    assert sources["google_ads"]["last_test_ok"] is True
    assert oauth["granted"] is True
    assert oauth["email"] == "ops@example.com"
    assert oauth["granted_by_name"]
    assert google_oauth.ADWORDS_SCOPE in oauth["scopes"]
    assert oauth["customer_id"] == "4445556660"
    assert [row["customer_id"] for row in oauth["accounts"]] == [
        "1112223330",
        "4445556660",
        "7778889990",
    ]


async def test_an_operator_can_sign_in_for_the_whole_workspace(
    admin: ApiClient, signed_in_as: Any, deployment: None, google: dict[str, Any]
) -> None:
    """The colleague who has the Google Ads account is rarely the administrator."""
    operator = await signed_in_as("operator")

    response = await sign_in(operator)

    assert parse_qs(urlparse(response.headers["location"]).query)["google"] == ["connected"]
    sources = {row["kind"]: row for row in (await admin.get("/connections")).json()["sources"]}
    assert sources["google_ads"]["oauth"]["granted"] is True


async def test_signing_in_again_replaces_the_grant_without_a_second_row(
    admin: ApiClient, deployment: None, google: dict[str, Any], db: AsyncSession
) -> None:
    await sign_in(admin)
    first = (
        await db.execute(
            sa.select(SourceConnection).where(SourceConnection.kind == CredentialKind.GOOGLE_ADS)
        )
    ).scalar_one()
    first_id, first_ciphertext = first.id, first.grant_ciphertext

    await sign_in(admin)
    # The API rewrote the row on its own session. Without this, SQLAlchemy's
    # identity map hands back the instance this session already loaded and the
    # assertion below compares the old ciphertext with itself.
    db.expire_all()
    rows = (
        (
            await db.execute(
                sa.select(SourceConnection).where(
                    SourceConnection.kind == CredentialKind.GOOGLE_ADS
                )
            )
        )
        .scalars()
        .all()
    )

    assert len(rows) == 1
    assert rows[0].id == first_id
    # A fresh nonce every time, so the same token does not seal to the same
    # bytes — and the row is genuinely rewritten rather than left alone.
    assert rows[0].grant_ciphertext != first_ciphertext


# --- choosing which account to read -----------------------------------------


async def test_the_account_can_be_changed_without_a_second_trip_to_google(
    admin: ApiClient, deployment: None, google: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    await sign_in(admin)

    async def ok(self: Any) -> Any:
        from agent.connectors.base import ConnectorStatus

        return ConnectorStatus(
            ok=True,
            detail=f"connected to {self.context.credentials['customer_id']}",
            meta={"customer_id": self.context.credentials["customer_id"]},
        )

    monkeypatch.setattr("agent.connectors.google_ads.GoogleAdsConnector.test_connection", ok)

    response = await admin.post(
        "/connections/google_ads/account", json={"customer_id": "777-888-9990"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["oauth"]["customer_id"] == "7778889990"
    assert response.json()["last_test_ok"] is True
    assert google["accounts_called"] == 1


async def test_only_an_account_the_grant_reported_may_be_chosen(
    admin: ApiClient, deployment: None, google: dict[str, Any]
) -> None:
    """Otherwise a workspace points at an account this consent cannot open, and
    finds out later, somewhere else, as an unexplained empty pull."""
    await sign_in(admin)

    response = await admin.post(
        "/connections/google_ads/account", json={"customer_id": "9999999999"}
    )

    assert response.status_code == 422, response.text
    assert "does not reach" in response.json()["detail"]


async def test_choosing_before_signing_in_says_which_button_to_press(
    admin: ApiClient, deployment: None
) -> None:
    response = await admin.post(
        "/connections/google_ads/account", json={"customer_id": "4445556660"}
    )

    assert response.status_code == 422, response.text
    assert "Connect with Google" in response.json()["detail"]


# --- what a run sees ---------------------------------------------------------


async def test_a_run_resolves_the_grant_the_person_gave(
    admin: ApiClient, deployment: None, google: dict[str, Any], db: AsyncSession
) -> None:
    """The point of all of it: the worker's gather node gets five values, three
    from the environment and two from a consent, in the shape the connector has
    always expected."""
    await sign_in(admin)
    workspace_id = (await admin.get("/workspace")).json()["id"]

    values = await resolve_values(
        db, workspace_id=uuid.UUID(workspace_id), kind=CredentialKind.GOOGLE_ADS
    )

    assert values["developer_token"] == DEPLOYMENT_ENV["GOOGLE_ADS_DEVELOPER_TOKEN"]
    assert values["refresh_token"] == REFRESH_TOKEN
    assert values["customer_id"] == "4445556660"
    assert values["login_customer_id"] == "1112223330"


async def test_a_connection_with_no_grant_degrades_rather_than_pretending(
    admin: ApiClient, deployment: None, google: dict[str, Any], db: AsyncSession
) -> None:
    """A revoked consent, or a rotated encryption key. Both mean "sign in
    again", which is a sentence a person can act on."""
    await sign_in(admin)
    workspace_id = uuid.UUID((await admin.get("/workspace")).json()["id"])
    row = (
        await db.execute(
            sa.select(SourceConnection).where(SourceConnection.kind == CredentialKind.GOOGLE_ADS)
        )
    ).scalar_one()
    row.grant_ciphertext = None
    row.grant_nonce = None
    await db.commit()

    with pytest.raises(MissingCredential) as raised:
        await resolve_values(db, workspace_id=workspace_id, kind=CredentialKind.GOOGLE_ADS)

    assert raised.value.reason == "not_authorised"
    assert "Connect with Google" in raised.value.detail


# --- letting go --------------------------------------------------------------


async def test_disconnecting_hands_the_grant_back_to_google(
    admin: ApiClient,
    deployment: None,
    google: dict[str, Any],
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deleting the row is what disconnecting means; telling Google is the
    courtesy of not leaving a live grant on somebody's personal account."""
    handed_back: list[str] = []

    async def revoke(token: str) -> bool:
        handed_back.append(token)
        return True

    monkeypatch.setattr("agent.api.routes_connections.google_oauth.revoke", revoke)
    await sign_in(admin)

    response = await admin.delete("/connections/google_ads")

    assert response.status_code == 204
    assert handed_back == [REFRESH_TOKEN]
    remaining = (
        (
            await db.execute(
                sa.select(SourceConnection).where(
                    SourceConnection.kind == CredentialKind.GOOGLE_ADS
                )
            )
        )
        .scalars()
        .all()
    )
    assert remaining == []


async def test_a_google_that_will_not_answer_cannot_keep_a_source_connected(
    admin: ApiClient, deployment: None, google: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refuse(_token: str) -> bool:
        return False

    monkeypatch.setattr("agent.api.routes_connections.google_oauth.revoke", refuse)
    await sign_in(admin)

    assert (await admin.delete("/connections/google_ads")).status_code == 204
    sources = {row["kind"]: row for row in (await admin.get("/connections")).json()["sources"]}
    assert sources["google_ads"]["connected"] is False
    assert sources["google_ads"]["oauth"]["granted"] is False


async def test_the_sign_in_is_written_to_the_audit_log(
    admin: ApiClient, deployment: None, google: dict[str, Any], db: AsyncSession
) -> None:
    await sign_in(admin)

    entries = (
        (await db.execute(sa.select(AuditLog).order_by(AuditLog.created_at.desc()))).scalars().all()
    )
    connected = next(entry for entry in entries if entry.meta.get("via") == "google_oauth")

    assert connected.meta["email"] == "ops@example.com"
    assert connected.meta["customer_id"] == "4445556660"
    assert connected.actor_id is not None
