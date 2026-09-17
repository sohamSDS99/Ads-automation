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
    BaseConnector,
    ConnectorAuthError,
    ConnectorError,
    ConnectorRateLimited,
    ConnectorStatus,
    EvidenceDraft,
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

USER_LISTS = """
SELECT user_list.id, user_list.name, user_list.description, user_list.type,
       user_list.membership_status, user_list.membership_life_span,
       user_list.size_for_display, user_list.size_for_search,
       user_list.eligible_for_search, user_list.eligible_for_display,
       user_list.closing_reason
FROM user_list
"""

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


def _dig(row: dict[str, Any], path: str) -> Any:
    """Read `a.b.c` out of the nested JSON the REST API returns."""
    current: Any = row
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


class GoogleAdsConnector(BaseConnector):
    """24 months of our own account, as seven kinds of evidence."""

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

    def _window(self, params: dict[str, Any]) -> tuple[str, str, str, str]:
        """The four date bounds the query templates interpolate.

        `recent_start` is deliberately not `start`: conversion actions are asked
        for *per day* so node 1.5.2 can compute staleness, and two years of daily
        rows per action is a large answer to a question only the last quarter can
        answer. Ninety days is the same window the change log uses.
        """
        end = date.fromisoformat(params["end"]) if params.get("end") else datetime.now(UTC).date()
        months = int(params.get("months") or self.settings.google_ads_lookback_months)
        start = end - timedelta(days=months * 30)
        recent = end - timedelta(days=RECENT_DAYS)
        return (
            start.isoformat(),
            end.isoformat(),
            recent.isoformat() + " 00:00:00",
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
                detail = response.text[:200]
                raise ConnectorAuthError(
                    f"Google Ads refused the request ({response.status_code}): {detail}"
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

        start, end, change_start, recent_start = self._window(params)
        wanted = params.get("kinds") or list(QUERIES)
        drafts: list[EvidenceDraft] = []
        failures: list[str] = []

        owned_client = self.context.client is None
        client = self.context.client or build_client(self.settings)
        try:
            for kind in wanted:
                template = QUERIES.get(kind)
                if template is None:
                    failures.append(f"{kind}: unknown query")
                    continue
                query = template.format(
                    start=start, end=end, change_start=change_start, recent_start=recent_start
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
