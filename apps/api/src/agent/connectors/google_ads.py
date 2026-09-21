"""Google Ads — our own account history (PRD §9.1).

Talks to the Google Ads **REST** surface over `httpx` rather than through the
`google-ads` Python SDK. Three reasons, in order of weight:

1. The SDK is gRPC-first. P2's acceptance criterion is that every connector is
   replayable from a recorded cassette, and a cassette records HTTP. Over REST
   the whole connector is testable with no account and no network.
2. It keeps protobuf, grpcio and a ~50MB dependency tree out of the `api` image
   for the sake of five queries whose shape we control anyway.
3. The SDK's config lives in a YAML file and environment variables it reads
   itself, which fights the AES-GCM vault that is supposed to hold these
   secrets (PRD Law 4).

The GAQL is unchanged from §9.1 — same five pulls, same 24-month window,
segmented by month.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import structlog

from agent.connectors.base import (
    ConnectorAuthError,
    ConnectorError,
    ConnectorRateLimited,
    ConnectorStatus,
    EvidenceDraft,
    ReadOnlyConnector,
    build_client,
    with_retries,
)
from agent.db.models import EvidenceSource

log = structlog.get_logger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 — an endpoint, not a secret

#: How far back the per-day pulls reach. Ninety days answers "is this
#: conversion action still receiving conversions" without asking for two
#: years of daily rows per action.
RECENT_DAYS = 90

#: The change log is not ours to window. Google: "Queries for Change Event data
#: must filter by date within the past 30 days and be limited to a maximum of
#: 10,000 rows." Asking for the same ninety days as the conversion pull is a
#: rejected query, not a longer history — and because `fetch` degrades a failed
#: pull rather than failing the run, it would have cost the change log silently.
#: 29, not 30, because the bound is evaluated in the account's timezone and ours
#: is UTC: a query built at 23:50 UTC must still be inside the window there.
CHANGE_EVENT_DAYS = 29

#: Google reports money in micros. Every currency amount crosses this boundary
#: exactly once, here, so no node ever has to remember the factor.
MICROS = 1_000_000

CAMPAIGN_PERFORMANCE = """
SELECT campaign.id, campaign.name, campaign.status, campaign.advertising_channel_type,
       segments.month,
       metrics.cost_micros, metrics.conversions, metrics.conversions_value,
       metrics.impressions, metrics.clicks, metrics.ctr, metrics.average_cpc
FROM campaign
WHERE segments.date BETWEEN '{start}' AND '{end}'
ORDER BY segments.month
"""

SEARCH_TERMS = """
SELECT search_term_view.search_term, campaign.name, ad_group.name,
       segments.month,
       metrics.cost_micros, metrics.conversions, metrics.conversions_value,
       metrics.impressions, metrics.clicks
FROM search_term_view
WHERE segments.date BETWEEN '{start}' AND '{end}'
"""

CREATIVE_HISTORY = """
SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status,
       ad_group_ad.ad.responsive_search_ad.headlines,
       ad_group_ad.ad.responsive_search_ad.descriptions,
       ad_group_ad.ad.final_urls, ad_group.name, campaign.name,
       metrics.impressions, metrics.clicks, metrics.conversions, metrics.cost_micros
FROM ad_group_ad
WHERE segments.date BETWEEN '{start}' AND '{end}'
"""

CHANGE_EVENTS = """
SELECT change_event.change_date_time, change_event.change_resource_type,
       change_event.resource_change_operation, change_event.changed_fields,
       change_event.user_email, campaign.name
FROM change_event
WHERE change_event.change_date_time >= '{change_start}'
  AND change_event.change_date_time <= '{change_end}'
ORDER BY change_event.change_date_time DESC
LIMIT 10000
"""

IMPRESSION_SHARE = """
SELECT ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type,
       campaign.name, segments.month,
       metrics.search_impression_share, metrics.search_budget_lost_impression_share,
       metrics.search_rank_lost_impression_share,
       metrics.cost_micros, metrics.conversions, metrics.impressions
FROM keyword_view
WHERE segments.date BETWEEN '{start}' AND '{end}'
"""

CONVERSION_ACTIONS = """
SELECT conversion_action.id, conversion_action.name, conversion_action.status,
       conversion_action.type, conversion_action.category,
       conversion_action.counting_type, conversion_action.primary_for_goal,
       conversion_action.tag_snippets,
       segments.date, metrics.all_conversions
FROM conversion_action
WHERE segments.date BETWEEN '{recent_start}' AND '{end}'
"""

#: What a freshly granted consent is asked about itself. Not one of the seven
#: pulls: it runs once, at connect time, to turn the ids
#: `listAccessibleCustomers` returns into accounts a person can recognise.
ACCOUNT_SUMMARY = """
SELECT customer.id, customer.descriptive_name, customer.manager,
       customer.currency_code, customer.time_zone
FROM customer
LIMIT 1
"""

#: The accounts a manager holds. `listAccessibleCustomers` answers with what the
#: signed-in user can reach *directly*, which for anyone working out of an MCC is
#: the MCC and nothing else — and a manager account holds no campaigns, so a
#: grant like that used to resolve to an account with nothing in it. Expanding
#: the manager is what turns one consent into the real ad accounts underneath it,
#: and it is also where `login_customer_id` comes from: the manager we asked.
#: `level > 0` drops the manager's own row from its own client list.
CLIENT_ACCOUNTS = """
SELECT customer_client.id, customer_client.descriptive_name,
       customer_client.manager, customer_client.currency_code,
       customer_client.status, customer_client.level
FROM customer_client
WHERE customer_client.level > 0
"""

USER_LISTS = """
SELECT user_list.id, user_list.name, user_list.description, user_list.type,
       user_list.membership_status, user_list.membership_life_span,
       user_list.size_for_display, user_list.size_for_search,
       user_list.eligible_for_search, user_list.eligible_for_display,
       user_list.closing_reason
FROM user_list
"""

#: Stage 02 PRD §10.2's forecast row. Not a GAQL pull — `GenerateKeywordForecast
#: Metrics` is a custom method on `KeywordPlanIdeaService` — so it sits outside
#: `QUERIES` and `fetch` branches to it by name.
KEYWORD_FORECAST = "keyword_forecast"

#: How far ahead a forecast is asked for, and for how long. Google refuses a
#: period starting in the past, so the window is measured from tomorrow; thirty
#: days because every threshold the plan tests a forecast against
#: (`learning.*_min_conv_30d`) is a 30-day one.
FORECAST_LEAD_DAYS = 1
FORECAST_DAYS = 30

#: ISO-3166 alpha-2 → Google's geo target constant. Only the markets this
#: product actually plans for; an unmapped market forecasts **without** a geo
#: filter, and the draft says so in `geo_basis` rather than silently returning
#: worldwide demand as if it were that market's.
#:
#: Resolved from a table rather than through `GeoTargetConstantService.Suggest`
#: because the suggestion service matches on a free-text name and would turn a
#: typo into a confident forecast for the wrong country.
GEO_TARGET_CONSTANTS: dict[str, str] = {
    "US": "2840",
    "GB": "2826",
    "UK": "2826",
    "DE": "2276",
    "FR": "2250",
    "ES": "2724",
    "IT": "2380",
    "NL": "2528",
    "SE": "2752",
    "NO": "2578",
    "DK": "2208",
    "FI": "2246",
    "PL": "2616",
    "CA": "2124",
    "AU": "2036",
    "IE": "2372",
    "BE": "2056",
    "AT": "2040",
    "CH": "2756",
}

#: ISO-639-1 → Google's language constant. Same rule: unmapped means unfiltered.
LANGUAGE_CONSTANTS: dict[str, str] = {
    "en": "1000",
    "de": "1001",
    "fr": "1002",
    "es": "1003",
    "it": "1004",
    "nl": "1010",
    "sv": "1015",
    "da": "1009",
    "fi": "1011",
    "no": "1013",
    "pl": "1030",
    "pt": "1014",
}

#: Google's own spellings. A group asking for anything else is a caller bug, so
#: it is rejected rather than quietly forecast as broad — broad match forecasts
#: several times the traffic of exact, and a plan built on the wrong one is out
#: by a multiple rather than a margin.
MATCH_TYPES = frozenset({"EXACT", "PHRASE", "BROAD"})

#: Keywords per forecast request. Google's own limit on a keyword plan ad group
#: is higher, but a request this size already answers for a cluster, and a
#: cluster is the grain the media plan is built at.
MAX_FORECAST_KEYWORDS = 500

#: query → the `Evidence.kind` its rows become.
QUERIES: dict[str, str] = {
    "campaign_perf": CAMPAIGN_PERFORMANCE,
    "search_term_pnl": SEARCH_TERMS,
    "creative_history": CREATIVE_HISTORY,
    "change_log": CHANGE_EVENTS,
    "keyword_impression_share": IMPRESSION_SHARE,
    "conversion_action": CONVERSION_ACTIONS,
    "audience_list": USER_LISTS,
}

#: The `send_to` target inside a conversion action's event snippet. It is the
#: only thing that ties a tag firing in a browser (node 1.5.2's probe) to a
#: *named* conversion action in the account — without it the probe can say a tag
#: fired but not which conversion it was supposed to record.
SEND_TO = re.compile(r"AW-\d+/[A-Za-z0-9_-]+")


def _micros(value: Any) -> float:
    try:
        return round(int(value) / MICROS, 4)
    except (TypeError, ValueError):
        return 0.0


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _send_to(snippets: Any) -> str | None:
    """The `AW-<id>/<label>` a conversion action's event snippet fires against."""
    if not isinstance(snippets, list):
        return None
    for snippet in snippets:
        if not isinstance(snippet, dict):
            continue
        match = SEND_TO.search(str(snippet.get("eventSnippet") or ""))
        if match:
            return match.group(0)
    return None


#: The refusals a first connection actually hits, and what to do about each.
#: Google states them once, in a nested `GoogleAdsFailure`, and the bare HTTP
#: status says only "no" — so the code is dug out and answered by name.
AUTH_HINTS: dict[str, str] = {
    "DEVELOPER_TOKEN_NOT_APPROVED": (
        "the developer token is still at test-account access, so it can only "
        "reach a Google Ads test account. Apply for Basic access under Tools & "
        "Settings -> API Center in the manager account that owns the token."
    ),
    "DEVELOPER_TOKEN_PROHIBITED": (
        "this developer token is not permitted to use the API with this Cloud "
        "project. Check the token belongs to the manager account you authorised."
    ),
    "USER_PERMISSION_DENIED": (
        "the Google account that granted the refresh token cannot see this "
        "customer id. Either authorise an account with access, or set the "
        "manager (MCC) id so the call is made through the manager."
    ),
    "CUSTOMER_NOT_ENABLED": "the Google Ads account is cancelled or not yet activated.",
    "NOT_ADS_USER": "the authorised Google account has no Google Ads account at all.",
    "CUSTOMER_NOT_FOUND": "no account with that customer id — check for a typo.",
}


def _auth_detail(response: httpx.Response) -> str:
    """Google's own words for the refusal, plus the fix when we know it."""
    try:
        body: Any = response.json()
    except ValueError:
        return response.text[:200]
    error = body[0] if isinstance(body, list) and body else body
    if not isinstance(error, dict):
        return response.text[:200]
    error = error.get("error") or error
    message = str(error.get("message") or "")[:200]
    code = ""
    for detail in error.get("details") or []:
        for item in (detail or {}).get("errors") or []:
            for value in ((item or {}).get("errorCode") or {}).values():
                code = str(value)
                message = str(item.get("message") or message)[:200]
                break
            if code:
                break
        if code:
            break
    hint = AUTH_HINTS.get(code)
    parts = [part for part in (code, message, hint) if part]
    return " — ".join(parts) or response.text[:200]


def _geo_constant(market: Any) -> str | None:
    """Google's geo target id for a market, or None to forecast unfiltered.

    Accepts the two spellings a project actually carries: a bare country code
    (`"DE"`) and a locale (`"de-DE"`, `"en-GB"`). The country is the right-hand
    part of a locale, which is why this does not simply upper-case the string.
    """
    text = str(market or "").strip()
    if not text:
        return None
    country = text.rsplit("-", 1)[-1].upper() if "-" in text else text.upper()
    return GEO_TARGET_CONSTANTS.get(country)


def _language_constant(language: Any) -> str | None:
    text = str(language or "").strip().lower()
    if not text:
        return None
    return LANGUAGE_CONSTANTS.get(text.split("-", 1)[0])


def _dig(row: dict[str, Any], path: str) -> Any:
    """Read `a.b.c` out of the nested JSON the REST API returns."""
    current: Any = row
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


class GoogleAdsConnector(ReadOnlyConnector):
    """24 months of our own account, as seven kinds of evidence.

    `ReadOnlyConnector` is the marker Stage 02 asserts on (law 12, PRD §17
    PS1): every operation in here is a GAQL `search` or an account listing, so
    a plan run reaching Google Ads through this class cannot change anything.
    Stage 04's mutating client is a different class, and the day it is written
    it must not inherit this one.
    """

    name = "google_ads"
    source = EvidenceSource.GOOGLE_ADS

    CREDENTIAL_FIELDS = (
        "developer_token",
        "client_id",
        "client_secret",
        "refresh_token",
        "customer_id",
    )

    def __init__(self, context: Any = None) -> None:
        super().__init__(context)
        self._access_token: str | None = None

    # --- auth --------------------------------------------------------------

    async def _token(self, client: httpx.AsyncClient) -> str:
        """Exchange the stored refresh token for an access token.

        Cached for the life of the connector instance, which is one fetch. A
        refresh token that Google has revoked fails here with a 400, and that is
        an auth error, not something a retry fixes.
        """
        if self._access_token:
            return self._access_token
        client_id, client_secret, refresh_token = self.context.require(
            "client_id", "client_secret", "refresh_token"
        )
        response = await client.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if response.status_code >= 400:
            raise ConnectorAuthError(
                f"Google refused the refresh token ({response.status_code}). "
                "Re-authorise the account in Settings."
            )
        token = response.json().get("access_token")
        if not token:
            raise ConnectorAuthError("Google returned no access_token")
        self._access_token = str(token)
        return self._access_token

    def _headers(self, token: str) -> dict[str, str]:
        developer_token = self.context.credentials["developer_token"]
        headers = {
            "Authorization": f"Bearer {token}",
            "developer-token": developer_token,
            "Content-Type": "application/json",
        }
        # Set only for manager-account access; sending it otherwise is an error.
        manager = self.context.credentials.get("login_customer_id")
        if manager:
            headers["login-customer-id"] = manager.replace("-", "")
        return headers

    # --- fetching ----------------------------------------------------------

    def _window(self, params: dict[str, Any]) -> tuple[str, str, str, str, str]:
        """The five date bounds the query templates interpolate.

        `recent_start` is deliberately not `start`: conversion actions are asked
        for *per day* so node 1.5.2 can compute staleness, and two years of daily
        rows per action is a large answer to a question only the last quarter can
        answer.

        The change-log pair is deliberately not derived from `end` either. Google
        keeps 30 days of change events and rejects a query reaching past that, so
        the window is measured from *now* whatever `end` says — a backfill asking
        for June gets the change log Google still holds, or nothing, but never a
        rejected query that would have taken the whole pull down with it.
        """
        end = date.fromisoformat(params["end"]) if params.get("end") else datetime.now(UTC).date()
        months = int(params.get("months") or self.settings.google_ads_lookback_months)
        start = end - timedelta(days=months * 30)
        recent = end - timedelta(days=RECENT_DAYS)
        today = datetime.now(UTC).date()
        change_start = today - timedelta(days=CHANGE_EVENT_DAYS)
        return (
            start.isoformat(),
            end.isoformat(),
            change_start.isoformat() + " 00:00:00",
            today.isoformat() + " 23:59:59",
            recent.isoformat(),
        )

    async def _search(
        self, client: httpx.AsyncClient, customer_id: str, query: str
    ) -> list[dict[str, Any]]:
        url = (
            f"{self.settings.google_ads_base_url}/{self.settings.google_ads_api_version}"
            f"/customers/{customer_id}/googleAds:searchStream"
        )
        token = await self._token(client)

        async def call() -> list[dict[str, Any]]:
            response = await client.post(
                url, headers=self._headers(token), json={"query": query.strip()}
            )
            if response.status_code == 429:
                raise ConnectorRateLimited("Google Ads rate limit")
            if response.status_code in (401, 403):
                raise ConnectorAuthError(
                    f"Google Ads refused the request ({response.status_code}): "
                    f"{_auth_detail(response)}"
                )
            if response.status_code == 404:
                # A retired API version answers with the front end's HTML 404,
                # not a Google Ads error — so say which version asked.
                raise ConnectorAuthError(
                    f"Google Ads has no {self.settings.google_ads_api_version} endpoint "
                    "(that version is retired). Update google_ads_api_version."
                )
            response.raise_for_status()
            body = response.json()
            # searchStream returns an array of chunks, each holding `results`.
            chunks = body if isinstance(body, list) else [body]
            rows: list[dict[str, Any]] = []
            for chunk in chunks:
                rows.extend(chunk.get("results") or [])
            return rows

        return await with_retries(
            call, attempts=self.settings.connector_max_retries, label="google_ads"
        )

    async def fetch(self, params: dict[str, Any]) -> list[EvidenceDraft]:
        """Run the wanted pulls. A pull that fails degrades the run, it does not end it."""
        customer_id = str(
            params.get("customer_id") or self.context.credentials.get("customer_id") or ""
        ).replace("-", "")
        if not customer_id:
            raise ConnectorAuthError("missing credential values: customer_id")
        self.context.require(*self.CREDENTIAL_FIELDS)

        start, end, change_start, change_end, recent_start = self._window(params)
        wanted = params.get("kinds") or list(QUERIES)
        drafts: list[EvidenceDraft] = []
        failures: list[str] = []

        owned_client = self.context.client is None
        client = self.context.client or build_client(self.settings)
        try:
            for kind in wanted:
                if kind == KEYWORD_FORECAST:
                    # Not GAQL, and deliberately never in the default `wanted`
                    # set: a forecast is asked for a *named* set of keywords at
                    # a named bid, so it only happens when a caller passes the
                    # groups. A failure here degrades like any other pull.
                    try:
                        drafts.extend(await self._forecast(client, customer_id, params))
                    except ConnectorAuthError as exc:
                        # PRD §18: an unauthorised forecast service degrades the
                        # run onto derived arithmetic. It does **not** raise the
                        # way an unauthorised GAQL pull does, because the plan
                        # has a defined answer without it and stopping would
                        # take down four other pulls that are working.
                        log.warning("google_ads.forecast_unauthorised", error=str(exc))
                        failures.append(f"{kind}: {exc}")
                    except ConnectorError as exc:
                        log.warning("google_ads.forecast_failed", error=str(exc))
                        failures.append(f"{kind}: {exc}")
                    continue
                template = QUERIES.get(kind)
                if template is None:
                    failures.append(f"{kind}: unknown query")
                    continue
                query = template.format(
                    start=start,
                    end=end,
                    change_start=change_start,
                    change_end=change_end,
                    recent_start=recent_start,
                )
                try:
                    rows = await self._search(client, customer_id, query)
                except ConnectorAuthError:
                    raise
                except ConnectorError as exc:
                    log.warning("google_ads.pull_failed", kind=kind, error=str(exc))
                    failures.append(f"{kind}: {exc}")
                    continue
                drafts.extend(self._to_drafts(kind, rows, customer_id))
        finally:
            if owned_client:
                await client.aclose()

        if failures:
            from agent.connectors.base import ConnectorDegraded

            raise ConnectorDegraded("; ".join(failures), drafts)
        return drafts

    # --- forecasting --------------------------------------------------------

    async def _forecast(
        self, client: httpx.AsyncClient, customer_id: str, params: dict[str, Any]
    ) -> list[EvidenceDraft]:
        """One forecast per keyword group (Stage 02 PRD §10.2).

        `GenerateKeywordForecastMetrics` answers at **campaign** grain, not per
        keyword: one request, one set of impressions/clicks/cost for the whole
        keyword set it was given. The per-keyword variant lives on
        `KeywordPlanService` and needs a saved `KeywordPlan` resource, which is
        a mutate — forbidden here by law 12 and asserted by PS1. So the caller
        sends the groups it wants priced, one request each, and the grain of
        the answer is the grain of the media plan: cluster x market.

        A group that fails does not take the others down. The drafts that did
        come back are returned and `fetch` degrades the rest, because a plan
        forecast for nine clusters out of ten is worth having and is honest
        about the tenth.
        """
        groups = params.get("groups") or []
        if not groups:
            raise ConnectorError(
                "keyword_forecast was requested with no `groups` — a forecast is asked for a "
                "named set of keywords at a named bid, never for the account at large"
            )

        start, end = self._forecast_period(params)
        url = (
            f"{self.settings.google_ads_base_url}/{self.settings.google_ads_api_version}"
            f"/customers/{customer_id}:generateKeywordForecastMetrics"
        )
        token = await self._token(client)
        drafts: list[EvidenceDraft] = []
        failures: list[str] = []

        for group in groups:
            label = f"{group.get('cluster') or '(unnamed)'}/{group.get('market') or '-'}"
            try:
                body = self._forecast_body(group, start=start, end=end)
            except ValueError as exc:
                failures.append(f"{label}: {exc}")
                continue

            async def call(payload: dict[str, Any] = body) -> dict[str, Any]:
                response = await client.post(url, headers=self._headers(token), json=payload)
                if response.status_code == 429:
                    raise ConnectorRateLimited("Google Ads rate limit")
                if response.status_code in (401, 403):
                    raise ConnectorAuthError(
                        f"the forecast service refused the request ({response.status_code}): "
                        f"{_auth_detail(response)}"
                    )
                if response.status_code == 404:
                    raise ConnectorAuthError(
                        f"Google Ads has no {self.settings.google_ads_api_version} forecast "
                        "endpoint (that version is retired). Update google_ads_api_version."
                    )
                response.raise_for_status()
                parsed = response.json()
                return parsed if isinstance(parsed, dict) else {}

            try:
                answer = await with_retries(
                    call, attempts=self.settings.connector_max_retries, label="google_ads_forecast"
                )
            except ConnectorAuthError:
                # Unauthorised is a property of the token, not of this group:
                # the next nine requests would fail identically and cost nine
                # more round trips to learn it.
                raise
            except ConnectorError as exc:
                failures.append(f"{label}: {exc}")
                continue

            drafts.append(self._forecast_draft(group, answer, body, customer_id))

        if failures and not drafts:
            raise ConnectorError("; ".join(failures))
        if failures:
            log.warning("google_ads.forecast_partial", failures=failures, ok=len(drafts))
        return drafts

    def _forecast_period(self, params: dict[str, Any]) -> tuple[str, str]:
        """The window to forecast. Google refuses one that starts in the past."""
        days = int(params.get("forecast_days") or FORECAST_DAYS)
        if days < 1:
            raise ConnectorError(f"forecast_days must be at least 1, got {days}")
        start = datetime.now(UTC).date() + timedelta(days=FORECAST_LEAD_DAYS)
        return start.isoformat(), (start + timedelta(days=days - 1)).isoformat()

    def _forecast_body(self, group: dict[str, Any], *, start: str, end: str) -> dict[str, Any]:
        """One `GenerateKeywordForecastMetricsRequest`.

        The bid is the caller's: it is the cluster's own volume-weighted CPC
        from the research, so the forecast answers "what does this demand cost
        at what we already know it costs" rather than at a figure this
        connector chose.
        """
        terms = (str(term).strip() for term in (group.get("keywords") or []))
        keywords = [term for term in terms if term]
        if not keywords:
            raise ValueError("no keywords")
        if len(keywords) > MAX_FORECAST_KEYWORDS:
            keywords = keywords[:MAX_FORECAST_KEYWORDS]

        match_type = str(group.get("match_type") or "PHRASE").upper()
        if match_type not in MATCH_TYPES:
            raise ValueError(f"match_type {match_type!r} is not one of {sorted(MATCH_TYPES)}")

        bid_usd = float(group.get("max_cpc_usd") or 0)
        if bid_usd <= 0:
            raise ValueError("max_cpc_usd must be positive — a forecast needs a bid")
        bid_micros = str(int(round(bid_usd * MICROS)))

        campaign: dict[str, Any] = {
            "keywordPlanNetwork": "GOOGLE_SEARCH",
            "biddingStrategy": {"manualCpcBiddingStrategy": {"maxCpcBidMicros": bid_micros}},
            "adGroups": [
                {
                    "maxCpcBidMicros": bid_micros,
                    "biddableKeywords": [
                        {"keyword": {"text": term, "matchType": match_type}} for term in keywords
                    ],
                }
            ],
        }
        geo = _geo_constant(group.get("market"))
        if geo:
            campaign["geoModifiers"] = [{"geoTargetConstant": f"geoTargetConstants/{geo}"}]
        language = _language_constant(group.get("language"))
        if language:
            campaign["languageConstants"] = [f"languageConstants/{language}"]

        return {"campaign": campaign, "forecastPeriod": {"startDate": start, "endDate": end}}

    def _forecast_draft(
        self,
        group: dict[str, Any],
        answer: dict[str, Any],
        body: dict[str, Any],
        customer_id: str,
    ) -> EvidenceDraft:
        """Google's answer for one cluster, in the units the plan reads.

        `cost` is preferred over `clicks x averageCpc` where Google supplies it:
        the two disagree by rounding, and the one Google states is the one a
        reader would find in the interface.
        """
        metrics = answer.get("campaignForecastMetrics") or {}
        impressions = _number(metrics.get("impressions"))
        clicks = _number(metrics.get("clicks"))
        average_cpc = _micros(metrics.get("averageCpc") or metrics.get("averageCpcMicros"))
        cost = _micros(metrics.get("costMicros"))
        if not cost and clicks and average_cpc:
            cost = round(clicks * average_cpc, 2)

        market = str(group.get("market") or "").strip()
        geo = _geo_constant(market)
        period = body.get("forecastPeriod") or {}
        payload = {
            "cluster": str(group.get("cluster") or "").strip() or None,
            "market": market or None,
            "keywords": len(body["campaign"]["adGroups"][0]["biddableKeywords"]),
            "match_type": body["campaign"]["adGroups"][0]["biddableKeywords"][0]["keyword"][
                "matchType"
            ],
            "max_cpc_usd": round(float(group.get("max_cpc_usd") or 0), 2),
            "impressions": impressions,
            "clicks": clicks,
            "ctr": _number(metrics.get("clickThroughRate")),
            "average_cpc": average_cpc,
            "cost": cost,
            "forecast_start": period.get("startDate"),
            "forecast_end": period.get("endDate"),
            # Named rather than implied: an unmapped market is forecast against
            # worldwide demand, and a plan that reads that as local demand would
            # over-buy by whatever multiple the rest of the world is.
            "geo_basis": f"geoTargetConstants/{geo}" if geo else "unfiltered",
        }
        return self.draft(
            KEYWORD_FORECAST,
            payload,
            source_url=f"https://ads.google.com/aw/keywordplanner?ocid={customer_id}",
            content_text=(
                f"Google forecast for {payload['cluster'] or 'keywords'} in "
                f"{payload['market'] or 'all markets'}: {impressions:,.0f} impressions, "
                f"{clicks:,.0f} clicks at ${average_cpc:,.2f} CPC over "
                f"{period.get('startDate')} to {period.get('endDate')}"
            ),
        )

    def _to_drafts(
        self, kind: str, rows: list[dict[str, Any]], customer_id: str
    ) -> list[EvidenceDraft]:
        builder = {
            "campaign_perf": self._campaign,
            "search_term_pnl": self._search_term,
            "creative_history": self._creative,
            "change_log": self._change,
            "keyword_impression_share": self._keyword,
            "conversion_action": self._conversion_action,
            "audience_list": self._user_list,
        }[kind]
        url = f"https://ads.google.com/aw/overview?ocid={customer_id}"
        return [self.draft(kind, builder(row), source_url=url) for row in rows]

    def _campaign(self, row: dict[str, Any]) -> dict[str, Any]:
        cost = _micros(_dig(row, "metrics.costMicros"))
        conversions = _number(_dig(row, "metrics.conversions"))
        value = _number(_dig(row, "metrics.conversionsValue"))
        return {
            "campaign_id": _dig(row, "campaign.id"),
            "campaign": _dig(row, "campaign.name"),
            "status": _dig(row, "campaign.status"),
            "channel": _dig(row, "campaign.advertisingChannelType"),
            "month": _dig(row, "segments.month"),
            "cost": cost,
            "conversions": conversions,
            "conversion_value": value,
            "impressions": _number(_dig(row, "metrics.impressions")),
            "clicks": _number(_dig(row, "metrics.clicks")),
            # Derived here rather than by a node: PRD Law 3 puts arithmetic in
            # Python, and these two are the ones every stage asks for first.
            "cpa": round(cost / conversions, 2) if conversions else None,
            "roas": round(value / cost, 3) if cost else None,
        }

    def _search_term(self, row: dict[str, Any]) -> dict[str, Any]:
        cost = _micros(_dig(row, "metrics.costMicros"))
        conversions = _number(_dig(row, "metrics.conversions"))
        return {
            "search_term": _dig(row, "searchTermView.searchTerm"),
            "campaign": _dig(row, "campaign.name"),
            "ad_group": _dig(row, "adGroup.name"),
            "month": _dig(row, "segments.month"),
            "cost": cost,
            "conversions": conversions,
            "conversion_value": _number(_dig(row, "metrics.conversionsValue")),
            "impressions": _number(_dig(row, "metrics.impressions")),
            "clicks": _number(_dig(row, "metrics.clicks")),
            "cpa": round(cost / conversions, 2) if conversions else None,
            # The number stage 1.2 exists to find: spend that bought nothing.
            "wasted_spend": cost if not conversions else 0.0,
        }

    def _creative(self, row: dict[str, Any]) -> dict[str, Any]:
        headlines = _dig(row, "adGroupAd.ad.responsiveSearchAd.headlines") or []
        descriptions = _dig(row, "adGroupAd.ad.responsiveSearchAd.descriptions") or []
        return {
            "ad_id": _dig(row, "adGroupAd.ad.id"),
            "ad_type": _dig(row, "adGroupAd.ad.type"),
            "status": _dig(row, "adGroupAd.status"),
            "headlines": [item.get("text") for item in headlines if isinstance(item, dict)],
            "descriptions": [item.get("text") for item in descriptions if isinstance(item, dict)],
            "final_urls": _dig(row, "adGroupAd.ad.finalUrls") or [],
            "ad_group": _dig(row, "adGroup.name"),
            "campaign": _dig(row, "campaign.name"),
            "impressions": _number(_dig(row, "metrics.impressions")),
            "clicks": _number(_dig(row, "metrics.clicks")),
            "conversions": _number(_dig(row, "metrics.conversions")),
            "cost": _micros(_dig(row, "metrics.costMicros")),
        }

    def _change(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "changed_at": _dig(row, "changeEvent.changeDateTime"),
            "resource_type": _dig(row, "changeEvent.changeResourceType"),
            "operation": _dig(row, "changeEvent.resourceChangeOperation"),
            "changed_fields": _dig(row, "changeEvent.changedFields"),
            "user_email": _dig(row, "changeEvent.userEmail"),
            "campaign": _dig(row, "campaign.name"),
        }

    def _keyword(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "keyword": _dig(row, "adGroupCriterion.keyword.text"),
            "match_type": _dig(row, "adGroupCriterion.keyword.matchType"),
            "campaign": _dig(row, "campaign.name"),
            "month": _dig(row, "segments.month"),
            "impression_share": _number(_dig(row, "metrics.searchImpressionShare")),
            "lost_is_budget": _number(_dig(row, "metrics.searchBudgetLostImpressionShare")),
            "lost_is_rank": _number(_dig(row, "metrics.searchRankLostImpressionShare")),
            "cost": _micros(_dig(row, "metrics.costMicros")),
            "conversions": _number(_dig(row, "metrics.conversions")),
            "impressions": _number(_dig(row, "metrics.impressions")),
        }

    def _conversion_action(self, row: dict[str, Any]) -> dict[str, Any]:
        """One conversion action on one day.

        The row is per-day on purpose: "when did this last record a conversion"
        is the question node 1.5.2 asks, and it is answerable from dated rows
        and not from a lifetime total.
        """
        snippets = _dig(row, "conversionAction.tagSnippets") or []
        return {
            "conversion_action_id": _dig(row, "conversionAction.id"),
            "name": _dig(row, "conversionAction.name"),
            "status": _dig(row, "conversionAction.status"),
            "action_type": _dig(row, "conversionAction.type"),
            "category": _dig(row, "conversionAction.category"),
            "counting_type": _dig(row, "conversionAction.countingType"),
            "primary_for_goal": _dig(row, "conversionAction.primaryForGoal"),
            "send_to": _send_to(snippets),
            "date": _dig(row, "segments.date"),
            "conversions": _number(_dig(row, "metrics.allConversions")),
        }

    def _user_list(self, row: dict[str, Any]) -> dict[str, Any]:
        """One audience list, as the account describes it.

        Note what is *not* here: consent basis. The API has no such field, and
        inventing one would be the exact failure node 1.5.3's gate exists to
        prevent — so the connector reports size, type and eligibility, and a
        data officer supplies the lawful basis.
        """
        return {
            "user_list_id": _dig(row, "userList.id"),
            "name": _dig(row, "userList.name"),
            "description": _dig(row, "userList.description"),
            "list_type": _dig(row, "userList.type"),
            "membership_status": _dig(row, "userList.membershipStatus"),
            "membership_life_span_days": _dig(row, "userList.membershipLifeSpan"),
            "size_for_display": _dig(row, "userList.sizeForDisplay"),
            "size_for_search": _dig(row, "userList.sizeForSearch"),
            "eligible_for_search": _dig(row, "userList.eligibleForSearch"),
            "eligible_for_display": _dig(row, "userList.eligibleForDisplay"),
            "closing_reason": _dig(row, "userList.closingReason"),
        }

    async def accessible_accounts(self) -> list[dict[str, Any]]:
        """Every account this consent reaches, named, with managers flagged.

        The moment after an OAuth grant is the only one where nothing is known
        about the account except that somebody approved it. Asking Google which
        customers the grant reaches — and then asking each what it is called —
        is what turns "authorised" into a customer id a person recognises,
        rather than one copied off a dashboard and hoped for.

        A customer that will not describe itself is still returned, unnamed. It
        is a real account the consent reaches, and dropping it here would make
        it invisible to the only screen that could have selected it.
        """
        self.context.require("developer_token", "client_id", "client_secret", "refresh_token")
        owned = self.context.client is None
        client = self.context.client or build_client(self.settings)
        base = f"{self.settings.google_ads_base_url}/{self.settings.google_ads_api_version}"
        try:
            token = await self._token(client)
            listing = await client.get(
                f"{base}/customers:listAccessibleCustomers", headers=self._headers(token)
            )
            if listing.status_code in (401, 403):
                raise ConnectorAuthError(
                    f"Google Ads refused the account list ({listing.status_code}): "
                    f"{_auth_detail(listing)}"
                )
            listing.raise_for_status()
            accounts: list[dict[str, Any]] = []
            seen: set[str] = set()
            for resource in listing.json().get("resourceNames") or []:
                customer_id = str(resource).rsplit("/", 1)[-1]
                if customer_id in seen:
                    continue
                seen.add(customer_id)
                account = await self._describe(client, base, token, customer_id)
                accounts.append(account)
                if not account["manager"]:
                    continue
                # A manager is kept in the list — it is genuinely reachable, and
                # naming it is how the interface can say which MCC was used —
                # but the accounts under it are the ones that hold campaigns.
                for child in await self._clients(client, base, token, customer_id):
                    if child["customer_id"] in seen:
                        continue
                    seen.add(child["customer_id"])
                    accounts.append(child)
            return accounts
        finally:
            if owned:
                await client.aclose()

    async def _describe(
        self, client: httpx.AsyncClient, base: str, token: str, customer_id: str
    ) -> dict[str, Any]:
        """One account's own description, or just its id if it will not give one."""
        account: dict[str, Any] = {
            "customer_id": customer_id,
            "name": None,
            "manager": False,
            "currency": None,
            # Reached directly, so no `login-customer-id` header is owed.
            "via_manager": None,
        }
        try:
            # `login-customer-id` set to the account itself: that is what a
            # manager account requires, and what a direct account ignores.
            response = await client.post(
                f"{base}/customers/{customer_id}/googleAds:searchStream",
                headers={**self._headers(token), "login-customer-id": customer_id},
                json={"query": ACCOUNT_SUMMARY.strip()},
            )
            if response.status_code >= 400:
                return account
            body = response.json()
            chunks = body if isinstance(body, list) else [body]
            rows = [row for chunk in chunks for row in (chunk.get("results") or [])]
        except (httpx.HTTPError, ValueError):
            return account
        if rows:
            customer = rows[0].get("customer") or {}
            account["name"] = customer.get("descriptiveName")
            account["manager"] = bool(customer.get("manager"))
            account["currency"] = customer.get("currencyCode")
        return account

    async def _clients(
        self, client: httpx.AsyncClient, base: str, token: str, manager_id: str
    ) -> list[dict[str, Any]]:
        """The accounts under one manager, each tagged with the manager to call through.

        A manager that will not list its clients returns nothing rather than
        raising: the grant may still reach a usable account another way, and one
        unreadable MCC must not cost a person the whole connection.

        Cancelled and suspended clients are dropped here rather than downstream.
        They would be picked as the account to connect — `status` is the only
        thing distinguishing them from a live account — and then every pull
        against them would come back empty.
        """
        try:
            response = await client.post(
                f"{base}/customers/{manager_id}/googleAds:searchStream",
                headers={**self._headers(token), "login-customer-id": manager_id},
                json={"query": CLIENT_ACCOUNTS.strip()},
            )
            if response.status_code >= 400:
                log.info(
                    "google_ads.clients_unavailable",
                    manager_id=manager_id,
                    status=response.status_code,
                )
                return []
            body = response.json()
            chunks = body if isinstance(body, list) else [body]
            rows = [row for chunk in chunks for row in (chunk.get("results") or [])]
        except (httpx.HTTPError, ValueError) as exc:
            log.info("google_ads.clients_failed", manager_id=manager_id, error=str(exc))
            return []

        clients: list[dict[str, Any]] = []
        for row in rows:
            child = row.get("customerClient") or {}
            child_id = str(child.get("id") or "").strip()
            if not child_id:
                continue
            if str(child.get("status") or "ENABLED").upper() not in ("ENABLED", "UNSPECIFIED"):
                continue
            clients.append(
                {
                    "customer_id": child_id,
                    "name": child.get("descriptiveName"),
                    "manager": bool(child.get("manager")),
                    "currency": child.get("currencyCode"),
                    "via_manager": manager_id,
                }
            )
        return clients

    async def test_connection(self) -> ConnectorStatus:
        """One cheap GAQL row. Proves the token, the developer token and the customer id."""
        customer_id = str(self.context.credentials.get("customer_id") or "").replace("-", "")
        if not customer_id:
            return ConnectorStatus(ok=False, detail="customer_id is not set")
        owned = self.context.client is None
        client = self.context.client or build_client(self.settings)
        try:
            rows = await self._search(
                client, customer_id, "SELECT customer.descriptive_name FROM customer LIMIT 1"
            )
        except ConnectorError as exc:
            return ConnectorStatus(ok=False, detail=str(exc))
        finally:
            if owned:
                await client.aclose()
        account = _dig(rows[0], "customer.descriptiveName") if rows else None
        return ConnectorStatus(
            ok=True,
            detail=f"connected to {account or customer_id}",
            meta={"customer_id": customer_id, "account_name": account},
        )
