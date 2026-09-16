"""Google Ads, replayed from `google_ads_search.yaml`.

Every assertion here is about the boundary this connector owns: micros to
currency, Google's nested JSON to a flat payload, and the arithmetic PRD Law 3
says must happen in Python rather than in a model.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.config import Settings
from agent.connectors.base import ConnectorAuthError, ConnectorContext
from agent.connectors.google_ads import MICROS, QUERIES, GoogleAdsConnector, _micros

TEST_KEY = "dW5pdC10ZXN0LWtleS0zMi1ieXRlcy1leGFjdGx5ISE="

CREDENTIALS = {
    "developer_token": "dev-token",
    "client_id": "client-id",
    "client_secret": "client-secret",
    "refresh_token": "refresh-token",
    "customer_id": "123-456-7890",
}


def connector(**credentials: str) -> GoogleAdsConnector:
    return GoogleAdsConnector(
        ConnectorContext(
            credentials={**CREDENTIALS, **credentials},
            settings=Settings(app_encryption_key=TEST_KEY),
        )
    )


def by_kind(drafts: list[Any], kind: str) -> list[Any]:
    return [draft for draft in drafts if draft.kind == kind]


def test_all_five_prd_pulls_are_declared() -> None:
    """PRD §9.1 lists five; a missing one is a silently narrower research corpus."""
    assert set(QUERIES) == {
        "campaign_perf",
        "search_term_pnl",
        "creative_history",
        "change_log",
        "keyword_impression_share",
    }


def test_micros_conversion() -> None:
    assert _micros("4820000000") == 4820.0
    assert _micros(None) == 0.0
    assert _micros("not-a-number") == 0.0
    assert MICROS == 1_000_000


async def test_fetch_produces_every_kind(cassette) -> None:
    with cassette("google_ads_search.yaml"):
        drafts = await connector().fetch({"end": "2025-06-30"})
        kinds = {draft.kind for draft in drafts}
        assert kinds == set(QUERIES)
        assert all(draft.source == "google_ads" for draft in drafts)


async def test_campaign_costs_arrive_in_currency_not_micros(cassette) -> None:
    with cassette("google_ads_search.yaml"):
        campaigns = by_kind(await connector().fetch({"end": "2025-06-30"}), "campaign_perf")
        brand = next(row for row in campaigns if row.payload["campaign"] == "Brand — Exact")
        assert brand.payload["cost"] == 4820.0
        assert brand.payload["conversions"] == 61.0


async def test_cpa_and_roas_are_computed_in_python(cassette) -> None:
    """PRD Law 3: arithmetic here, labels and prose in the model."""
    with cassette("google_ads_search.yaml"):
        campaigns = by_kind(await connector().fetch({"end": "2025-06-30"}), "campaign_perf")
        brand = next(row for row in campaigns if row.payload["campaign"] == "Brand — Exact")
        assert brand.payload["cpa"] == pytest.approx(79.02, abs=0.01)
        assert brand.payload["roas"] == pytest.approx(50.62, abs=0.01)


async def test_zero_conversions_yields_null_cpa_not_a_crash(cassette) -> None:
    """The division that would take down a whole pull if it were done naively."""
    with cassette("google_ads_search.yaml"):
        terms = by_kind(await connector().fetch({"end": "2025-06-30"}), "search_term_pnl")
        dud = next(row for row in terms if row.payload["search_term"] == "free sds sheets download")
        assert dud.payload["cpa"] is None
        assert dud.payload["wasted_spend"] == 1880.0


async def test_converting_terms_record_no_wasted_spend(cassette) -> None:
    with cassette("google_ads_search.yaml"):
        terms = by_kind(await connector().fetch({"end": "2025-06-30"}), "search_term_pnl")
        good = next(
            row for row in terms if row.payload["search_term"] == "safety data sheet software"
        )
        assert good.payload["wasted_spend"] == 0.0
        assert good.payload["cpa"] == pytest.approx(237.78, abs=0.01)


async def test_rsa_assets_are_flattened_to_text(cassette) -> None:
    """Google nests each headline as `{"text": ...}`; nodes want the strings."""
    with cassette("google_ads_search.yaml"):
        creatives = by_kind(await connector().fetch({"end": "2025-06-30"}), "creative_history")
        payload = creatives[0].payload
        assert payload["headlines"] == [
            "SDS Management Software",
            "Compliance Without the Binders",
        ]
        assert payload["final_urls"] == ["https://www.sdsmanager.com/us/"]


async def test_change_events_keep_the_actor(cassette) -> None:
    with cassette("google_ads_search.yaml"):
        changes = by_kind(await connector().fetch({"end": "2025-06-30"}), "change_log")
        assert changes[0].payload["user_email"] == "soham@sdsmanager.com"
        assert changes[0].payload["operation"] == "UPDATE"


async def test_impression_share_survives_as_a_fraction(cassette) -> None:
    with cassette("google_ads_search.yaml"):
        rows = by_kind(await connector().fetch({"end": "2025-06-30"}), "keyword_impression_share")
        assert rows[0].payload["impression_share"] == pytest.approx(0.31)
        assert rows[0].payload["lost_is_budget"] == pytest.approx(0.42)


async def test_selecting_one_kind_runs_one_query(cassette) -> None:
    """A partial run must not pull — or pay for — the other four."""
    with cassette("google_ads_search.yaml"):
        drafts = await connector().fetch({"end": "2025-06-30", "kinds": ["campaign_perf"]})
        assert {draft.kind for draft in drafts} == {"campaign_perf"}


async def test_evidence_hashes_are_unique_per_row(cassette) -> None:
    """Dedupe is only useful if two genuinely different rows hash differently."""
    with cassette("google_ads_search.yaml"):
        drafts = await connector().fetch({"end": "2025-06-30"})
        assert len({draft.hash() for draft in drafts}) == len(drafts)


async def test_a_hyphenated_customer_id_is_normalised(cassette) -> None:
    """Google shows `123-456-7890`; the API path needs `1234567890`."""
    with cassette("google_ads_search.yaml"):
        drafts = await connector().fetch({"end": "2025-06-30", "kinds": ["campaign_perf"]})
        assert drafts, "the cassette path only matches once the dashes are stripped"


async def test_missing_credentials_fail_before_any_request() -> None:
    """No cassette is bound, so a network call would raise a different error."""
    bare = GoogleAdsConnector(
        ConnectorContext(
            credentials={"customer_id": "123"}, settings=Settings(app_encryption_key=TEST_KEY)
        )
    )
    with pytest.raises(ConnectorAuthError, match="developer_token"):
        await bare.fetch({})


async def test_a_missing_customer_id_is_named() -> None:
    bare = GoogleAdsConnector(
        ConnectorContext(credentials={}, settings=Settings(app_encryption_key=TEST_KEY))
    )
    with pytest.raises(ConnectorAuthError, match="customer_id"):
        await bare.fetch({})
