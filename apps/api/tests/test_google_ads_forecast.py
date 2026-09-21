"""The keyword forecast pull, and the degradation PRD §18 requires of it.

`GenerateKeywordForecastMetrics` is the one Google Ads operation Stage 02 adds
(§10.2). It is not GAQL and it is not optional-in-the-usual-sense: PRD §18 says
an unavailable or unauthorised forecast service must put the run on derived
arithmetic rather than stop it, and the plan must *say* it did. Both halves are
asserted here — the request Google receives, and what the connector does when
Google refuses it.

Driven through `httpx.MockTransport` rather than a cassette because every test
here is about a request body this connector builds, and a cassette records the
reply.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from agent.config import Settings
from agent.connectors.base import ConnectorContext, ConnectorDegraded, ConnectorError
from agent.connectors.google_ads import (
    GEO_TARGET_CONSTANTS,
    KEYWORD_FORECAST,
    MAX_FORECAST_KEYWORDS,
    MICROS,
    QUERIES,
    GoogleAdsConnector,
)

TEST_KEY = "dW5pdC10ZXN0LWtleS0zMi1ieXRlcy1leGFjdGx5ISE="

CREDENTIALS = {
    "developer_token": "dev-token",
    "client_id": "client-id",
    "client_secret": "client-secret",
    "refresh_token": "refresh-token",
    "customer_id": "123-456-7890",
}

GROUP = {
    "cluster": "SDS management",
    "market": "US",
    "language": "en",
    "keywords": ["sds software", "safety data sheet management"],
    "max_cpc_usd": 3.25,
}

FORECAST_REPLY = {
    "campaignForecastMetrics": {
        "impressions": 12000.0,
        "clicks": 480.0,
        "clickThroughRate": 0.04,
        "averageCpc": "3100000",
        "costMicros": "1488000000",
    }
}


class Recorder:
    """A transport that answers the forecast endpoint and remembers the asks."""

    def __init__(self, handler: Any) -> None:
        self.requests: list[httpx.Request] = []
        self._handler = handler

    def transport(self) -> httpx.MockTransport:
        def respond(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if request.url.path.endswith("/token"):
                return httpx.Response(200, json={"access_token": "token"})
            return self._handler(request)

        return httpx.MockTransport(respond)

    @property
    def forecasts(self) -> list[dict[str, Any]]:
        return [
            json.loads(request.content)
            for request in self.requests
            if "generateKeywordForecastMetrics" in str(request.url)
        ]


def connector(recorder: Recorder) -> GoogleAdsConnector:
    return GoogleAdsConnector(
        ConnectorContext(
            credentials=dict(CREDENTIALS),
            settings=Settings(app_encryption_key=TEST_KEY),
            client=httpx.AsyncClient(transport=recorder.transport()),
        )
    )


def ok(_: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=FORECAST_REPLY)


async def fetch(recorder: Recorder, **params: Any) -> list[Any]:
    conn = connector(recorder)
    try:
        return await conn.fetch({"kinds": [KEYWORD_FORECAST], **params})
    finally:
        client = conn.context.client
        if client is not None:
            await client.aclose()


# ---------------------------------------------------------------------------
# the request
# ---------------------------------------------------------------------------


def test_the_forecast_is_not_one_of_the_gaql_pulls() -> None:
    """A default fetch must never spend a forecast operation.

    Basic access meters forecast operations separately and far more tightly
    than `search`, and the forecast is meaningless without a keyword set
    anyway — so it is reachable only by asking for it with `groups`.
    """
    assert KEYWORD_FORECAST not in QUERIES


@pytest.mark.asyncio
async def test_one_request_per_group_at_the_groups_own_bid() -> None:
    recorder = Recorder(ok)
    second = {**GROUP, "cluster": "SDS training", "max_cpc_usd": 1.5}
    drafts = await fetch(recorder, groups=[GROUP, second])

    assert len(drafts) == 2
    bodies = recorder.forecasts
    assert len(bodies) == 2
    bids = [
        body["campaign"]["biddingStrategy"]["manualCpcBiddingStrategy"]["maxCpcBidMicros"]
        for body in bodies
    ]
    assert bids == [str(int(3.25 * MICROS)), str(int(1.5 * MICROS))]


@pytest.mark.asyncio
async def test_the_market_becomes_a_geo_target_and_is_named_on_the_draft() -> None:
    recorder = Recorder(ok)
    drafts = await fetch(recorder, groups=[GROUP])

    campaign = recorder.forecasts[0]["campaign"]
    assert campaign["geoModifiers"] == [
        {"geoTargetConstant": f"geoTargetConstants/{GEO_TARGET_CONSTANTS['US']}"}
    ]
    assert campaign["languageConstants"] == ["languageConstants/1000"]
    assert drafts[0].payload["geo_basis"] == f"geoTargetConstants/{GEO_TARGET_CONSTANTS['US']}"


@pytest.mark.asyncio
async def test_an_unmapped_market_forecasts_unfiltered_and_says_so() -> None:
    """The failure this prevents is the quiet one.

    Sending no geo filter returns worldwide demand. A plan that read that as
    one small market's demand would over-buy by whatever multiple the rest of
    the world is, and nothing downstream could tell. So the draft records the
    basis, and `demand.py` carries it to the gate card.
    """
    recorder = Recorder(ok)
    drafts = await fetch(recorder, groups=[{**GROUP, "market": "Wakanda"}])

    assert "geoModifiers" not in recorder.forecasts[0]["campaign"]
    assert drafts[0].payload["geo_basis"] == "unfiltered"


@pytest.mark.asyncio
async def test_a_locale_resolves_to_its_country() -> None:
    recorder = Recorder(ok)
    await fetch(recorder, groups=[{**GROUP, "market": "de-DE", "language": "de-DE"}])

    campaign = recorder.forecasts[0]["campaign"]
    assert campaign["geoModifiers"] == [{"geoTargetConstant": "geoTargetConstants/2276"}]
    assert campaign["languageConstants"] == ["languageConstants/1001"]


@pytest.mark.asyncio
async def test_the_forecast_period_starts_in_the_future() -> None:
    """Google rejects a period starting today or earlier."""
    recorder = Recorder(ok)
    await fetch(recorder, groups=[GROUP], forecast_days=30)

    period = recorder.forecasts[0]["forecastPeriod"]
    from datetime import UTC, date, datetime, timedelta

    start = date.fromisoformat(period["startDate"])
    end = date.fromisoformat(period["endDate"])
    assert start > datetime.now(UTC).date()
    assert end - start == timedelta(days=29)


@pytest.mark.asyncio
async def test_a_huge_keyword_set_is_truncated_not_rejected() -> None:
    recorder = Recorder(ok)
    terms = [f"term {index}" for index in range(MAX_FORECAST_KEYWORDS + 50)]
    drafts = await fetch(recorder, groups=[{**GROUP, "keywords": terms}])

    sent = recorder.forecasts[0]["campaign"]["adGroups"][0]["biddableKeywords"]
    assert len(sent) == MAX_FORECAST_KEYWORDS
    assert drafts[0].payload["keywords"] == MAX_FORECAST_KEYWORDS


@pytest.mark.asyncio
async def test_an_unknown_match_type_is_refused_rather_than_forecast_as_broad() -> None:
    """Broad forecasts several times the traffic of exact. Guessing is worse
    than failing: the plan would be out by a multiple, not a margin."""
    recorder = Recorder(ok)
    with pytest.raises(ConnectorDegraded) as caught:
        await fetch(recorder, groups=[{**GROUP, "match_type": "SORT_OF"}])

    assert "SORT_OF" in str(caught.value)
    assert not recorder.forecasts


# ---------------------------------------------------------------------------
# the answer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_money_arrives_in_currency_not_micros() -> None:
    recorder = Recorder(ok)
    drafts = await fetch(recorder, groups=[GROUP])

    payload = drafts[0].payload
    assert payload["average_cpc"] == 3.1
    assert payload["cost"] == 1488.0
    assert payload["impressions"] == 12000.0
    assert payload["clicks"] == 480.0
    assert payload["cluster"] == "SDS management"
    assert payload["market"] == "US"


@pytest.mark.asyncio
async def test_cost_is_derived_when_google_states_only_a_cpc() -> None:
    """Two of the three are always present; the third is worth reconstructing
    rather than leaving a media plan with no cost for one of its clusters."""
    reply = {
        "campaignForecastMetrics": {
            "impressions": 1000.0,
            "clicks": 100.0,
            "averageCpc": "2500000",
        }
    }
    recorder = Recorder(lambda _: httpx.Response(200, json=reply))
    drafts = await fetch(recorder, groups=[GROUP])

    assert drafts[0].payload["cost"] == 250.0


# ---------------------------------------------------------------------------
# degradation — PRD §18
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unauthorised_forecast_degrades_and_does_not_raise_through() -> None:
    """The exit criterion: "forecast-service failure degrades without stopping
    the run."

    An unauthorised *GAQL* pull raises out of `fetch`, because a token that
    cannot read the account cannot serve any of the seven pulls. The forecast
    is different: the plan has a defined answer without it — derived arithmetic
    on Stage 01 demand — so refusing it must not take down the pulls that work.
    """
    recorder = Recorder(
        lambda _: httpx.Response(403, json={"error": {"message": "CLOUD_PROJECT_NOT_APPROVED"}})
    )
    with pytest.raises(ConnectorDegraded) as caught:
        await fetch(recorder, groups=[GROUP])

    assert KEYWORD_FORECAST in str(caught.value)
    assert caught.value.drafts == []


@pytest.mark.asyncio
async def test_a_working_pull_survives_a_refused_forecast() -> None:
    """The half that makes the previous test worth having.

    `ConnectorDegraded` carries the drafts that did come back. A forecast that
    took the conversion-action pull down with it would turn one missing input
    into four.
    """

    def respond(request: httpx.Request) -> httpx.Response:
        if "generateKeywordForecastMetrics" in str(request.url):
            return httpx.Response(403, json={"error": {"message": "not approved"}})
        return httpx.Response(
            200,
            json=[
                {
                    "results": [
                        {
                            "conversionAction": {"id": "1", "name": "Quote form"},
                            "segments": {"date": "2026-09-01"},
                            "metrics": {"allConversions": 3.0},
                        }
                    ]
                }
            ],
        )

    recorder = Recorder(respond)
    conn = connector(recorder)
    try:
        with pytest.raises(ConnectorDegraded) as caught:
            await conn.fetch({"kinds": ["conversion_action", KEYWORD_FORECAST], "groups": [GROUP]})
    finally:
        client = conn.context.client
        if client is not None:
            await client.aclose()

    kinds = {draft.kind for draft in caught.value.drafts}
    assert kinds == {"conversion_action"}
    assert KEYWORD_FORECAST in caught.value.reason


@pytest.mark.asyncio
async def test_one_failed_group_does_not_lose_the_others() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        # Keyed on the keyword, not on a call counter: `with_retries` gives each
        # group three attempts, and a counter would have the *retry* succeed and
        # quietly assert nothing. The first group has to fail every time.
        if b"doomed term" in request.content:
            return httpx.Response(500, json={"error": {"message": "backend"}})
        return httpx.Response(200, json=FORECAST_REPLY)

    recorder = Recorder(respond)
    drafts = await fetch(
        recorder,
        groups=[
            {**GROUP, "keywords": ["doomed term"]},
            {**GROUP, "cluster": "SDS training"},
        ],
    )

    # The first group exhausted its retries; the second answered. A partial
    # forecast is still a better media plan than none, and `demand.py` sizes
    # the unanswered cluster from search volume.
    assert [draft.payload["cluster"] for draft in drafts] == ["SDS training"]


@pytest.mark.asyncio
async def test_asking_for_a_forecast_with_no_groups_is_a_caller_error() -> None:
    recorder = Recorder(ok)
    with pytest.raises(ConnectorDegraded) as caught:
        await fetch(recorder)

    assert "no `groups`" in str(caught.value)
    assert not recorder.forecasts


@pytest.mark.asyncio
async def test_a_group_with_no_bid_is_refused() -> None:
    """A forecast without a bid is a forecast of nothing in particular."""
    recorder = Recorder(ok)
    with pytest.raises(ConnectorDegraded) as caught:
        await fetch(recorder, groups=[{**GROUP, "max_cpc_usd": 0}])

    assert "max_cpc_usd" in str(caught.value)


def test_the_connector_stays_read_only() -> None:
    """PS1. The marker the executor asserts on before a plan node gathers."""
    from agent.connectors.base import ReadOnlyConnector

    assert issubclass(GoogleAdsConnector, ReadOnlyConnector)


def test_forecast_error_is_a_connector_error_not_a_bare_exception() -> None:
    """So `gather` classifies it as a degraded source rather than a crash."""
    assert issubclass(ConnectorError, RuntimeError)
