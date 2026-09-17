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
    """The developer token is the only value a person has that Google will not
    hand back. A refresh token is not something anyone types, and neither is a
    manager id — `accessible_accounts` is already reading the account list that
    names it. All six fields still exist, because the sealed secret carries
    them; the claim here is only that the form asks for one."""
    spec = spec_for(CredentialKind.GOOGLE_ADS)

    assert spec.oauth_provider == "google"
    typed = {field.name for field in spec.typed_fields}
    assert typed == {"developer_token"}
    assert "refresh_token" not in typed
    assert "login_customer_id" not in typed
    assert {field.name for field in spec.fields} > typed


def test_no_other_kind_claims_a_provider_it_does_not_have() -> None:
    for kind in (CredentialKind.OPENROUTER, CredentialKind.DATAFORSEO, CredentialKind.WEBSHARE):
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
        "9998887777",
        "1234567890",
        "5550001111",
    ]
    named = next(row for row in accounts if row["customer_id"] == "1234567890")
    assert named["name"] == "SDS Manager — US Search"
    assert named["currency"] == "USD"


async def test_a_manager_account_is_flagged_as_one(cassette) -> None:
    """Which account the connect flow picks turns on this: a manager account
    holds no campaigns, so choosing it would connect a source with nothing in
    it and report success."""
    with cassette("google_ads_accounts.yaml"):
        accounts = await connector().accessible_accounts()

    managers = [row["customer_id"] for row in accounts if row["manager"]]
    assert managers == ["7788990011"]
    first_with_campaigns = next(row for row in accounts if not row["manager"])
    assert first_with_campaigns["customer_id"] == "9998887777"


async def test_an_account_that_will_not_describe_itself_is_still_listed(cassette) -> None:
    """Dropping it would make it invisible to the only screen that could have
    selected it — and a permission error on one account says nothing about the
    others."""
    with cassette("google_ads_accounts.yaml"):
        accounts = await connector().accessible_accounts()

    silent = next(row for row in accounts if row["customer_id"] == "5550001111")
    assert silent["name"] is None
    assert silent["manager"] is False


async def test_an_account_reachable_only_through_a_manager_is_found(cassette) -> None:
    """The case the old flow could not serve.

    `listAccessibleCustomers` answers with what the signed-in user reaches
    directly. For anyone working out of an MCC that is the MCC, which holds no
    campaigns — so the connect flow used to pick a manager and report success
    on a source with nothing in it. Expanding the manager is what finds the
    account that actually has the spend.
    """
    with cassette("google_ads_accounts.yaml"):
        accounts = await connector().accessible_accounts()

    child = next(row for row in accounts if row["customer_id"] == "9998887777")
    assert child["name"] == "SDS Manager — EU Search"
    assert child["manager"] is False
    assert child["via_manager"] == "7788990011", "which is what `login-customer-id` needs"


async def test_the_manager_id_is_discovered_rather_than_typed(cassette) -> None:
    """The field that left the form, and where its value comes from instead.

    An account reached directly carries no manager and must not send the header
    at all: Google rejects `login-customer-id` on an account that is not under
    that manager, so a blanket value would break the direct case.
    """
    with cassette("google_ads_accounts.yaml"):
        accounts = await connector().accessible_accounts()

    by_id = {row["customer_id"]: row for row in accounts}
    assert by_id["9998887777"]["via_manager"] == "7788990011"
    assert by_id["1234567890"]["via_manager"] is None


async def test_a_cancelled_client_account_is_not_offered(cassette) -> None:
    """It is indistinguishable from a live account but every pull returns empty."""
    with cassette("google_ads_accounts.yaml"):
        accounts = await connector().accessible_accounts()

    assert "4443332222" not in {row["customer_id"] for row in accounts}
