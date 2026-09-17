"""Connecting Google Ads by consent rather than by paste.

The flow this covers exists because of who owns the account: the person with a
Google Ads login is usually not the person who installed this, and asking them
for a refresh token means asking them to run a terminal. What is tested here is
the part that has to be right before anyone clicks — which fields the form may
still ask for, where the browser is allowed to be sent back to, and how the
accounts a grant reaches are read.
"""

from __future__ import annotations

from agent.api.routes_credentials import _safe_return_to
from agent.config import Settings
from agent.connectors.base import ConnectorContext
from agent.connectors.google_ads import GoogleAdsConnector
from agent.credential_kinds import spec_for
from agent.db.models import CredentialKind

TEST_KEY = "dW5pdC10ZXN0LWtleS0zMi1ieXRlcy1leGFjdGx5ISE="

CREDENTIALS = {
    "developer_token": "dev-token",
    "client_id": "client-id",
    "client_secret": "client-secret",
    "refresh_token": "refresh-token",
}


def connector() -> GoogleAdsConnector:
    return GoogleAdsConnector(
        ConnectorContext(
            credentials=dict(CREDENTIALS), settings=Settings(app_encryption_key=TEST_KEY)
        )
    )


def test_the_form_asks_only_for_what_consent_cannot_supply() -> None:
    """A refresh token is not something a person has to hand, so the form must
    not have a box for it — but all five fields still exist, because pasting
    them remains a supported way to connect."""
    spec = spec_for(CredentialKind.GOOGLE_ADS)

    assert spec.oauth_provider == "google"
    typed = {field.name for field in spec.typed_fields}
    assert typed == {"developer_token", "login_customer_id"}
    assert "refresh_token" not in typed
    assert {field.name for field in spec.fields} > typed


def test_no_other_kind_claims_a_provider_it_does_not_have() -> None:
    for kind in (CredentialKind.OPENROUTER, CredentialKind.DATAFORSEO, CredentialKind.BRIGHTDATA):
        spec = spec_for(kind)
        assert spec.oauth_provider is None
        assert spec.typed_fields == spec.fields


def test_the_browser_is_only_ever_sent_back_to_this_app() -> None:
    """`return_to` arrives from the browser and leaves in a Location header."""
    assert _safe_return_to("/settings?tab=sources") == "/settings?tab=sources"
    assert _safe_return_to("https://evil.example/steal") == "/settings"
    assert _safe_return_to("//evil.example/steal") == "/settings"
    assert _safe_return_to("") == "/settings"
    assert _safe_return_to("   ") == "/settings"


async def test_a_grant_reports_every_account_it_reaches(cassette) -> None:
    with cassette("google_ads_accounts.yaml"):
        accounts = await connector().accessible_accounts()

    assert [row["customer_id"] for row in accounts] == [
        "7788990011",
        "1234567890",
        "5550001111",
    ]
    assert accounts[1]["name"] == "SDS Manager — US Search"
    assert accounts[1]["currency"] == "USD"


async def test_a_manager_account_is_flagged_as_one(cassette) -> None:
    """Which account the connect flow picks turns on this: a manager account
    holds no campaigns, so choosing it would connect a source with nothing in
    it and report success."""
    with cassette("google_ads_accounts.yaml"):
        accounts = await connector().accessible_accounts()

    managers = [row["customer_id"] for row in accounts if row["manager"]]
    assert managers == ["7788990011"]
    first_with_campaigns = next(row for row in accounts if not row["manager"])
    assert first_with_campaigns["customer_id"] == "1234567890"


async def test_an_account_that_will_not_describe_itself_is_still_listed(cassette) -> None:
    """Dropping it would make it invisible to the only screen that could have
    selected it — and a permission error on one account says nothing about the
    others."""
    with cassette("google_ads_accounts.yaml"):
        accounts = await connector().accessible_accounts()

    silent = next(row for row in accounts if row["customer_id"] == "5550001111")
    assert silent["name"] is None
    assert silent["manager"] is False
