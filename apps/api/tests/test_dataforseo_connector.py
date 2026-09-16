"""DataForSEO, replayed from its cassettes.

Two things matter here beyond parsing. First, the vendor returns HTTP 200 with a
failed task inside — the failure mode that silently produces empty evidence if
nobody checks `status_code`. Second, the nodes must depend on `KeywordProvider`
rather than on this class, because PRD §9.3 requires the vendor to be swappable.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

import pytest

from agent.config import Settings
from agent.connectors.base import ConnectorAuthError, ConnectorContext, ConnectorDegraded
from agent.connectors.dataforseo import TASK_OK, DataForSEOConnector, KeywordProvider, _monthly

Cassette = Callable[[str], AbstractContextManager[None]]

TEST_KEY = "dW5pdC10ZXN0LWtleS0zMi1ieXRlcy1leGFjdGx5ISE="
CREDENTIALS = {"login": "someone@example.com", "password": "api-password"}

PARAMS: dict[str, Any] = {
    "domain": "sdsmanager.com",
    "seeds": ["sds software"],
    "serp_keywords": ["sds management software"],
}


def connector(**credentials: str) -> DataForSEOConnector:
    return DataForSEOConnector(
        ConnectorContext(
            credentials={**CREDENTIALS, **credentials},
            settings=Settings(app_encryption_key=TEST_KEY),
        )
    )


def by_kind(drafts: list[Any], kind: str) -> list[Any]:
    return [draft for draft in drafts if draft.kind == kind]


def test_the_connector_satisfies_the_vendor_neutral_protocol() -> None:
    """PRD §9.3: swapping to SEMrush must not touch a node."""
    assert isinstance(connector(), KeywordProvider)


def test_monthly_searches_are_normalised() -> None:
    assert _monthly([{"year": 2025, "month": 6, "search_volume": 2600}]) == [
        {"year": 2025, "month": 6, "search_volume": 2600}
    ]
    assert _monthly(None) == []
    assert _monthly(["not a dict"]) == []


async def test_fetch_produces_all_three_prd_kinds(cassette: Cassette) -> None:
    with cassette("dataforseo_demand.yaml"):
        drafts = await connector().fetch(PARAMS)
    assert {draft.kind for draft in drafts} == {
        "keyword_metrics",
        "serp_snapshot",
        "domain_competitor",
    }


async def test_keyword_metrics_carry_volume_cpc_and_seasonality(cassette: Cassette) -> None:
    with cassette("dataforseo_demand.yaml"):
        drafts = await connector().fetch(PARAMS)
    row = next(
        d
        for d in by_kind(drafts, "keyword_metrics")
        if d.payload["keyword"] == "sds management software"
    )
    assert row.payload["search_volume"] == 2400
    assert row.payload["cpc"] == 14.2
    assert row.payload["competition_index"] == 87
    assert len(row.payload["monthly_searches"]) == 2


async def test_labs_rows_are_unwrapped_to_the_same_shape(cassette: Cassette) -> None:
    """Keywords Data is flat, Labs nests under `keyword_info`. One payload either way."""
    with cassette("dataforseo_demand.yaml"):
        drafts = await connector().fetch(PARAMS)
    idea = next(d for d in by_kind(drafts, "keyword_metrics") if d.payload["origin"] == "ideas")
    assert idea.payload["keyword"] == "ghs labelling software"
    assert idea.payload["search_volume"] == 720


async def test_site_and_idea_keywords_are_distinguishable(cassette: Cassette) -> None:
    """Stage 1.4 treats "we already rank for this" differently from "this is new"."""
    with cassette("dataforseo_demand.yaml"):
        drafts = await connector().fetch(PARAMS)
    origins = {d.payload["origin"] for d in by_kind(drafts, "keyword_metrics")}
    assert origins == {"site", "ideas"}


async def test_serp_snapshot_keeps_organic_ranks_only(cassette: Cassette) -> None:
    with cassette("dataforseo_demand.yaml"):
        drafts = await connector().fetch(PARAMS)
    snapshot = by_kind(drafts, "serp_snapshot")[0].payload
    assert snapshot["keyword"] == "sds management software"
    assert [row["domain"] for row in snapshot["results"]] == [
        "chemwatch.net",
        "www.sdsmanager.com",
    ]
    assert snapshot["results"][0]["rank"] == 1


async def test_competitor_paid_metrics_are_extracted(cassette: Cassette) -> None:
    with cassette("dataforseo_demand.yaml"):
        drafts = await connector().fetch(PARAMS)
    competitor = by_kind(drafts, "domain_competitor")[0].payload
    assert competitor["competitor_domain"] == "chemwatch.net"
    assert competitor["paid_keyword_count"] == 140
    assert competitor["paid_estimated_traffic_cost"] == 21400.0


async def test_a_failed_task_inside_a_200_degrades_rather_than_lies(
    cassette: Cassette,
) -> None:
    """The vendor's real failure mode. Silently returning [] would look like "no demand"."""
    with cassette("dataforseo_task_failure.yaml"), pytest.raises(ConnectorDegraded) as caught:
        await connector().fetch({"domain": "sdsmanager.com"})
    # Degraded, not empty-and-successful: the run continues and the report can
    # say coverage was reduced instead of concluding there is no demand.
    assert caught.value.drafts == []
    assert "keywords_for_site" in caught.value.reason


def test_task_ok_is_the_documented_sentinel() -> None:
    assert TASK_OK == 20000


async def test_missing_credentials_fail_before_any_request() -> None:
    bare = DataForSEOConnector(
        ConnectorContext(credentials={}, settings=Settings(app_encryption_key=TEST_KEY))
    )
    with pytest.raises(ConnectorAuthError, match="login"):
        await bare.fetch({"domain": "x.test"})


async def test_a_fetch_without_a_domain_is_refused(cassette: Cassette) -> None:
    from agent.connectors.base import ConnectorError

    with pytest.raises(ConnectorError, match="domain"):
        await connector().fetch({})
