"""Keyword demand and SERP ownership (PRD §9.3).

The public type here is `KeywordProvider`, not `DataForSEOConnector`. PRD §9.3
requires the adapter to be provider-neutral so SEMrush or Ahrefs can be swapped
in "without touching nodes" — so the nodes depend on the protocol, and the
vendor lives behind it.

DataForSEO's API shape is consistent and worth stating once: every call is a
POST whose body is a *list* of task objects, and every response is
`{"tasks": [{"status_code": …, "result": [...]}]}`. A task can fail while the
HTTP request succeeds with a 200, so the per-task status code is checked as
carefully as the transport one.
"""

from __future__ import annotations

import base64
from typing import Any, Protocol, runtime_checkable

import httpx
import structlog

from agent.connectors.base import (
    BaseConnector,
    ConnectorAuthError,
    ConnectorDegraded,
    ConnectorError,
    ConnectorRateLimited,
    ConnectorStatus,
    EvidenceDraft,
    build_client,
    with_retries,
)
from agent.db.models import EvidenceSource

log = structlog.get_logger(__name__)

#: DataForSEO signals success per task with 20000, not with the HTTP status.
TASK_OK = 20000

ENDPOINTS = {
    "keywords_for_site": "/keywords_data/google_ads/keywords_for_site/live",
    "keywords_for_keywords": "/keywords_data/google_ads/keywords_for_keywords/live",
    "search_volume": "/keywords_data/google_ads/search_volume/live",
    "keyword_ideas": "/dataforseo_labs/google/keyword_ideas/live",
    "serp_organic": "/serp/google/organic/live/advanced",
    "competitors_domain": "/dataforseo_labs/google/competitors_domain/live",
}


@runtime_checkable
class KeywordProvider(Protocol):
    """What a keyword vendor has to be able to answer.

    Nodes depend on this, never on DataForSEO. Swapping vendor means writing a
    second class that satisfies these four methods.
    """

    async def keywords_for_site(
        self, domain: str, *, location: str, language: str
    ) -> list[dict[str, Any]]: ...

    async def search_volume(
        self, keywords: list[str], *, location: str, language: str
    ) -> list[dict[str, Any]]: ...

    async def keyword_ideas(
        self, seeds: list[str], *, location: str, language: str, limit: int = 1000
    ) -> list[dict[str, Any]]: ...

    async def serp(self, keyword: str, *, location: str, language: str) -> dict[str, Any]: ...


def _monthly(rows: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """12 months of `{year, month, search_volume}`, which is the seasonality signal."""
    return [
        {
            "year": item.get("year"),
            "month": item.get("month"),
            "search_volume": item.get("search_volume"),
        }
        for item in (rows or [])
        if isinstance(item, dict)
    ]


class DataForSEOConnector(BaseConnector):
    """Demand-side evidence: volume, CPC, competition, seasonality, SERP ownership."""

    name = "dataforseo"
    source = EvidenceSource.DATAFORSEO

    CREDENTIAL_FIELDS = ("login", "password")

    # --- transport ---------------------------------------------------------

    def _auth_header(self) -> str:
        login, password = self.context.require("login", "password")
        token = base64.b64encode(f"{login}:{password}".encode()).decode()
        return f"Basic {token}"

    async def _post(
        self, client: httpx.AsyncClient, endpoint: str, tasks: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        url = self.settings.dataforseo_base_url.rstrip("/") + endpoint

        async def call() -> list[dict[str, Any]]:
            response = await client.post(
                url,
                json=tasks,
                headers={
                    "Authorization": self._auth_header(),
                    "Content-Type": "application/json",
                },
            )
            if response.status_code == 429:
                raise ConnectorRateLimited("DataForSEO rate limit")
            if response.status_code == 401:
                raise ConnectorAuthError("DataForSEO rejected the login")
            response.raise_for_status()
            body = response.json()
            returned = body.get("tasks") or []
            results: list[dict[str, Any]] = []
            failed: list[str] = []
            for task in returned:
                status = task.get("status_code")
                if status != TASK_OK:
                    # A 200 carrying a failed task is this vendor's normal
                    # failure mode, and the dangerous one: returning [] here
                    # would reach a node as "this keyword has no demand".
                    message = f"{status}: {task.get('status_message')}"
                    log.warning("dataforseo.task_failed", endpoint=endpoint, detail=message)
                    failed.append(message)
                    continue
                results.extend(task.get("result") or [])
            if failed and not results:
                raise ConnectorError(f"{endpoint} failed — {'; '.join(failed)}")
            return results

        return await with_retries(
            call, attempts=self.settings.connector_max_retries, label="dataforseo"
        )

    # --- KeywordProvider ---------------------------------------------------

    async def keywords_for_site(
        self, domain: str, *, location: str = "United States", language: str = "English"
    ) -> list[dict[str, Any]]:
        client, owned = self._client()
        try:
            return await self._post(
                client,
                ENDPOINTS["keywords_for_site"],
                [{"target": domain, "location_name": location, "language_name": language}],
            )
        finally:
            if owned:
                await client.aclose()

    async def search_volume(
        self, keywords: list[str], *, location: str = "United States", language: str = "English"
    ) -> list[dict[str, Any]]:
        client, owned = self._client()
        try:
            # The endpoint caps a task at 1000 keywords; chunk rather than truncate.
            results: list[dict[str, Any]] = []
            for start in range(0, len(keywords), 1000):
                batch = keywords[start : start + 1000]
                results.extend(
                    await self._post(
                        client,
                        ENDPOINTS["search_volume"],
                        [
                            {
                                "keywords": batch,
                                "location_name": location,
                                "language_name": language,
                            }
                        ],
                    )
                )
            return results
        finally:
            if owned:
                await client.aclose()

    async def keyword_ideas(
        self,
        seeds: list[str],
        *,
        location: str = "United States",
        language: str = "English",
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        client, owned = self._client()
        try:
            results = await self._post(
                client,
                ENDPOINTS["keyword_ideas"],
                [
                    {
                        "keywords": seeds,
                        "location_name": location,
                        "language_name": language,
                        "limit": limit,
                    }
                ],
            )
            # Labs endpoints wrap their rows one level deeper than Keywords Data.
            flattened: list[dict[str, Any]] = []
            for result in results:
                flattened.extend(result.get("items") or [])
            return flattened
        finally:
            if owned:
                await client.aclose()

    async def serp(
        self, keyword: str, *, location: str = "United States", language: str = "English"
    ) -> dict[str, Any]:
        client, owned = self._client()
        try:
            results = await self._post(
                client,
                ENDPOINTS["serp_organic"],
                [{"keyword": keyword, "location_name": location, "language_name": language}],
            )
            return results[0] if results else {}
        finally:
            if owned:
                await client.aclose()

    async def competitors_domain(
        self, domain: str, *, location: str = "United States", language: str = "English"
    ) -> list[dict[str, Any]]:
        client, owned = self._client()
        try:
            results = await self._post(
                client,
                ENDPOINTS["competitors_domain"],
                [{"target": domain, "location_name": location, "language_name": language}],
            )
            flattened: list[dict[str, Any]] = []
            for result in results:
                flattened.extend(result.get("items") or [])
            return flattened
        finally:
            if owned:
                await client.aclose()

    def _client(self) -> tuple[httpx.AsyncClient, bool]:
        if self.context.client is not None:
            return self.context.client, False
        return build_client(self.settings), True

    # --- connector ---------------------------------------------------------

    async def fetch(self, params: dict[str, Any]) -> list[EvidenceDraft]:
        """Volume for the site's own keywords, ideas from seeds, and SERP ownership.

        `params`: `{domain, seeds?, serp_keywords?, volume_for?, location?, language?}`.

        `volume_for` is a different question from the rest and answers it alone:
        node 1.4.1 discovers a keyword universe from several sources, and node
        1.4.3 has to price *those* terms — including the ones that came from our
        own search-term report or a competitor's ad copy and were never in this
        vendor's idea list. Discovery is skipped when it is set, because
        re-running `keywords_for_site` would spend quota to answer a question
        nobody asked.
        """
        self.context.require(*self.CREDENTIAL_FIELDS)
        domain = str(params.get("domain") or "").strip()
        if not domain:
            raise ConnectorError("dataforseo.fetch needs a `domain`")
        location = params.get("location") or "United States"
        language = params.get("language") or "English"
        seeds = list(params.get("seeds") or [])
        serp_keywords = list(params.get("serp_keywords") or [])
        volume_for = [str(term).strip() for term in (params.get("volume_for") or []) if term]

        drafts: list[EvidenceDraft] = []
        failures: list[str] = []
        client, owned = self._client()
        # Hand the borrowed client to every sub-call so one connection pool
        # serves the whole fetch and a cassette sees one consistent transport.
        self.context.client = client
        try:
            if volume_for:
                try:
                    for row in await self.search_volume(
                        volume_for, location=location, language=language
                    ):
                        drafts.append(self._keyword_draft(row, domain, origin="volume"))
                except ConnectorError as exc:
                    failures.append(f"search_volume: {exc}")
                if failures:
                    raise ConnectorDegraded("; ".join(failures), drafts)
                return drafts

            try:
                for row in await self.keywords_for_site(
                    domain, location=location, language=language
                ):
                    drafts.append(self._keyword_draft(row, domain, origin="site"))
            except ConnectorError as exc:
                failures.append(f"keywords_for_site: {exc}")

            if seeds:
                try:
                    for row in await self.keyword_ideas(
                        seeds, location=location, language=language
                    ):
                        drafts.append(self._keyword_draft(row, domain, origin="ideas"))
                except ConnectorError as exc:
                    failures.append(f"keyword_ideas: {exc}")

            for keyword in serp_keywords:
                try:
                    drafts.append(
                        self._serp_draft(
                            await self.serp(keyword, location=location, language=language),
                            keyword,
                        )
                    )
                except ConnectorError as exc:
                    failures.append(f"serp[{keyword}]: {exc}")

            try:
                for row in await self.competitors_domain(
                    domain, location=location, language=language
                ):
                    drafts.append(self._competitor_draft(row, domain))
            except ConnectorError as exc:
                failures.append(f"competitors_domain: {exc}")
        finally:
            if owned:
                self.context.client = None
                await client.aclose()

        if failures:
            raise ConnectorDegraded("; ".join(failures), drafts)
        return drafts

    def _keyword_draft(self, row: dict[str, Any], domain: str, *, origin: str) -> EvidenceDraft:
        # Keywords Data returns the metrics flat; Labs nests them under
        # `keyword_info`. One shape reaches the payload either way.
        nested = row.get("keyword_info")
        info: dict[str, Any] = nested if isinstance(nested, dict) else row
        keyword_data = row.get("keyword_data")
        keyword = row.get("keyword") or (
            keyword_data.get("keyword") if isinstance(keyword_data, dict) else None
        )
        return self.draft(
            "keyword_metrics",
            {
                "keyword": keyword,
                "search_volume": info.get("search_volume"),
                "cpc": info.get("cpc"),
                "competition": info.get("competition"),
                "competition_index": info.get("competition_index"),
                "low_top_of_page_bid": info.get("low_top_of_page_bid"),
                "high_top_of_page_bid": info.get("high_top_of_page_bid"),
                "monthly_searches": _monthly(info.get("monthly_searches")),
                "origin": origin,
                "domain": domain,
            },
        )

    def _serp_draft(self, result: dict[str, Any], keyword: str) -> EvidenceDraft:
        items = [item for item in (result.get("items") or []) if isinstance(item, dict)]
        return self.draft(
            "serp_snapshot",
            {
                "keyword": keyword,
                "total_results": result.get("se_results_count"),
                "results": [
                    {
                        "rank": item.get("rank_absolute"),
                        "domain": item.get("domain"),
                        "title": item.get("title"),
                        "description": item.get("description"),
                        "url": item.get("url"),
                    }
                    for item in items
                    if item.get("type") == "organic"
                ][:20],
            },
            source_url=result.get("check_url"),
        )

    def _competitor_draft(self, row: dict[str, Any], domain: str) -> EvidenceDraft:
        metrics = row.get("metrics") or {}
        paid = metrics.get("paid") if isinstance(metrics, dict) else None
        return self.draft(
            "domain_competitor",
            {
                "competitor_domain": row.get("domain"),
                "for_domain": domain,
                "avg_position": row.get("avg_position"),
                "intersections": row.get("intersections"),
                "paid_keyword_count": (paid or {}).get("count"),
                "paid_estimated_traffic_cost": (paid or {}).get("estimated_paid_traffic_cost"),
            },
        )

    async def test_connection(self) -> ConnectorStatus:
        """One keyword's volume. The cheapest call that proves the login works."""
        client, owned = self._client()
        try:
            rows = await self._post(
                client,
                ENDPOINTS["search_volume"],
                [
                    {
                        "keywords": ["safety data sheet software"],
                        "location_name": "United States",
                        "language_name": "English",
                    }
                ],
            )
        except ConnectorError as exc:
            return ConnectorStatus(ok=False, detail=str(exc))
        finally:
            if owned:
                await client.aclose()
        return ConnectorStatus(
            ok=True,
            detail=f"connected, {len(rows)} row(s) returned",
            meta={"probe_rows": len(rows)},
        )
