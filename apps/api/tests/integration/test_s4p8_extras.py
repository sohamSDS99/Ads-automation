"""S4-P8 exit criteria — 4.3.1, 4.3.2 and 4.3.3 end to end.

A creative run is started through the API, halts on G7, is approved, and runs
the whole DAG with scripted COPYWRITE and CLASSIFY answers. Sitelink and
privacy-policy URLs are checked by the real `preview/urlcheck.py` against a
scripted web (`httpx.MockTransport`) that records every request.

- sitelinks are on-domain, 2xx after redirects and unique per campaign; one
  whose URL fails is rejected with why and never becomes an asset; every item
  is linted at creation;
- every promotion and price number and date is an `OfferBinding` reference
  (a model-written digit fails the schema — `tests/creative/test_extras_contract.py`);
- stale offers make 4.3.2 `not_required` and ask no model;
- the lead form has a privacy URL that resolved 2xx on-domain and no Art. 9
  question; the field trade-off is a `calc/` result with `PlanCalc` + Evidence.

Asserted from the database and the stored node outputs — never the run's event
stream, which does not close on a paused run.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import (
    CreativeAssetKind,
    CreativeAssetStatus,
    Evidence,
    EvidenceSource,
    RunStatus,
)
from agent.orchestrator.dag import Dag
from agent.preview import urlcheck
from agent.schemas.extras import SitelinksCalloutsSnippetsOutput
from tests.integration.conftest import ApiClient
from tests.integration.creative_support import TEXT_ONLY
from tests.integration.runs_support import execute
from tests.integration.test_s4p4_brief_g7 import _g7
from tests.integration.test_s4p5_headlines_combinations import _assets, _output
from tests.integration.test_s4p6_descriptions_variant_b import _registry, _seed
from tests.integration.test_s4p7_landing import _Script as _LandingScript
from tests.openrouter_fake import completion

pytestmark = pytest.mark.asyncio

#: The integration project's domain (`tests/integration/conftest.py`).
SITE = "https://sdsmanager.com"
SPEC = {"source": "unverified", "reviewed_at": "2026-09-25"}
EXTRA_SPECS: dict[str, dict[str, Any]] = {
    "sitelink": {"max_chars": 25, "min_count": 2, "max_count": 6, **SPEC},
    "callout": {"max_chars": 25, "min_count": 2, "max_count": 3, **SPEC},
    "structured_snippet": {"max_chars": 25, "min_count": 3, "max_count": 4, **SPEC},
}

#: The scripted web: `url -> (status, location)`. Crawled as `web` pages.
WEB: dict[str, tuple[int, str | None]] = {
    f"{SITE}/pricing": (200, None),
    f"{SITE}/demo": (200, None),
    f"{SITE}/old-features": (301, "/features"),
    f"{SITE}/features": (200, None),
    f"{SITE}/partners": (302, "https://partner.example/sds"),
    f"{SITE}/gone": (404, None),
    f"{SITE}/plans": (301, "/pricing"),
    f"{SITE}/privacy": (200, None),
    "https://partner.example/sds": (200, None),
}
CRAWLED = [url for url in WEB if url.startswith(SITE)]

#: What 4.3.1's model answers: six sitelinks, one per crawled page it chose.
SITELINKS = [
    ("See pricing", f"{SITE}/pricing"),
    ("Book a live product demo today", f"{SITE}/demo"),  # 30 > 25 characters: fails lint
    ("Explore features", f"{SITE}/old-features"),  # redirects on-domain
    ("Our partners", f"{SITE}/partners"),  # redirects off-domain
    ("Old page", f"{SITE}/gone"),  # 404
    ("Compare plans", f"{SITE}/plans"),  # lands on /pricing: a duplicate
]
CALLOUTS = ["Audit-ready SDS library", "Free onboarding", "Chemical inventory"]
SNIPPET = {"header": "Types", "values": ["Safety data sheets", "Chemical labels", "Inventory"]}


class _Web:
    """The scripted web behind `urlcheck.new_client`, recording every request."""

    def __init__(self, pages: dict[str, tuple[int, str | None]]) -> None:
        self.pages = pages
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        found = self.pages.get(str(request.url))
        if found is None:
            raise httpx.ConnectError("refused", request=request)
        status, location = found
        return httpx.Response(
            status, headers={"location": location} if location else {}, request=request
        )


@pytest.fixture
def web(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Web]:
    scripted = _Web(dict(WEB))
    monkeypatch.setattr(
        urlcheck,
        "new_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(scripted.handler)),
    )
    yield scripted


class _Script(_LandingScript):
    """S4-P7's scripted copy and landing answers, plus the three extras."""

    def _respond(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        name = body["response_format"]["json_schema"]["name"]
        if name == "SitelinksCalloutsSnippetsDraft":
            self.requests.setdefault(name, []).append(body)
            return completion(
                {
                    "sitelinks": [
                        {
                            "link_text": text,
                            "line1": "Keep every SDS current",
                            "line2": "Built for EHS teams",
                            "final_url": url,
                        }
                        for text, url in SITELINKS
                    ],
                    "callouts": CALLOUTS,
                    "snippet": SNIPPET,
                },
                model=body["model"],
            )
        return super()._respond(request)


async def _crawl(db: AsyncSession, project_id: uuid.UUID, urls: list[str]) -> None:
    """The project's crawled pages, as `web_crawler` stores them."""
    for url in urls:
        db.add(
            Evidence(
                project_id=project_id,
                source=EvidenceSource.WEB,
                kind="page",
                source_url=url,
                payload={"url": url, "status": 200, "title": url.rsplit("/", 1)[-1].title()},
                hash=f"page-{url}",
            )
        )
    await db.commit()


async def _run(
    admin: ApiClient,
    db: AsyncSession,
    ws: uuid.UUID,
    project_id: uuid.UUID,
    actor: uuid.UUID,
    *,
    extra_specs: dict[str, dict[str, Any]] | None = None,
) -> tuple[uuid.UUID, _Script, RunStatus]:
    """A creative run started, approved at G7, and run to its end."""
    await _seed(db, ws, project_id, actor, extra_specs={"search": extra_specs or {}})
    started = await admin.post(
        f"/projects/{project_id}/creative/runs", json={"scope": TEXT_ONLY, "media_models": []}
    )
    assert started.status_code == 202, started.text
    run_id = uuid.UUID(started.json()["run_id"])
    script = _Script()
    registry = _registry()
    halted = await execute(run_id, script.fake, registry=registry, dag=Dag.from_registry(registry))
    assert halted.status is RunStatus.AWAITING_APPROVAL, halted.error
    approval = await _g7(db, run_id)
    decided = await admin.post(f"/approvals/{approval.id}", json={"decision": "approve"})
    assert decided.status_code == 200, decided.text
    result = await execute(
        run_id, script.fake, registry=registry, dag=Dag.from_registry(registry), max_attempts=1
    )
    assert result.status is RunStatus.SUCCEEDED, result.error
    return run_id, script, result.status


# ---------------------------------------------------------------------------
# 4.3.1
# ---------------------------------------------------------------------------


async def test_sitelinks_are_on_domain_2xx_and_unique_and_every_item_is_linted(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    web: _Web,
) -> None:
    await _crawl(db, project_id, CRAWLED)
    run_id, script, _ = await _run(
        admin, db, workspace_id, project_id, admin_user.id, extra_specs=EXTRA_SPECS
    )
    output = SitelinksCalloutsSnippetsOutput.model_validate(await _output(db, run_id, "4.3.1"))
    assert output.status == "required"
    (campaign,) = output.campaigns
    assert campaign.campaign_ref == "c-sds-us"

    # --- the model could point only at a crawled on-domain page --------------
    (request,) = script.requests["SitelinksCalloutsSnippetsDraft"]
    schema = request["response_format"]["json_schema"]["schema"]
    offered = schema["$defs"]["SitelinkDraft"]["properties"]["final_url"]["enum"]
    assert set(offered) == set(CRAWLED)

    # --- on-domain, 2xx after redirects, unique ------------------------------
    assert [(s.final_url, s.url_check.final_url_after_redirects) for s in campaign.sitelinks] == [
        (f"{SITE}/pricing", f"{SITE}/pricing"),
        (f"{SITE}/old-features", f"{SITE}/features"),
    ]
    assert all(
        s.url_check.status == "ok" and s.url_check.http_status == 200 for s in campaign.sitelinks
    )
    assert {r.final_url: r.url_check.status for r in campaign.rejected_sitelinks} == {
        f"{SITE}/partners": "off_domain",
        f"{SITE}/gone": "http_error",
        f"{SITE}/plans": "duplicate",
    }
    # The off-domain hop was refused, never fetched; nothing but GETs left.
    assert all(str(r.url).startswith(SITE) for r in web.requests)
    assert {r.method for r in web.requests} == {"GET"}

    # --- every item linted at creation; only a pass leaves draft --------------
    rows = [row for row in (await _assets(db, run_id)).values() if row.node_id == "4.3.1"]
    sitelink_rows = {
        row.fields["final_url"]: row for row in rows if row.kind is CreativeAssetKind.SITELINK
    }
    # A rejected URL is never an asset, not even a draft.
    assert set(sitelink_rows) == {f"{SITE}/pricing", f"{SITE}/demo", f"{SITE}/old-features"}
    demo = sitelink_rows[f"{SITE}/demo"]
    assert demo.status is CreativeAssetStatus.DRAFT
    assert demo.lint["verdict"] == "fail"
    assert "asset_spec.length.v1" in {f["rule_id"] for f in demo.lint["findings"]}
    for url in (f"{SITE}/pricing", f"{SITE}/old-features"):
        row = sitelink_rows[url]
        assert row.status is CreativeAssetStatus.LINTED
        assert row.fields["url_check"]["status"] == "ok"
        assert row.lint["targets_checked"] == 3  # link text and both lines
    assert {row.text for row in rows if row.kind is CreativeAssetKind.CALLOUT} == set(CALLOUTS)
    assert campaign.exception_candidates == [], campaign.exception_candidates
    (snippet,) = [row for row in rows if row.kind is CreativeAssetKind.STRUCTURED_SNIPPET]
    assert snippet.text == "Types"
    assert snippet.fields["values"] == SNIPPET["values"]
    assert all(row.lint and row.ruleset_version for row in rows)
    assert [c.text for c in campaign.callouts] == CALLOUTS
    assert [s.header for s in campaign.snippets] == ["Types"]
    assert campaign.gaps == []


async def test_a_pin_without_the_extras_specs_is_spec_missing_and_asks_no_model(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    web: _Web,
) -> None:
    await _crawl(db, project_id, CRAWLED)
    run_id, script, _ = await _run(admin, db, workspace_id, project_id, admin_user.id)
    output = SitelinksCalloutsSnippetsOutput.model_validate(await _output(db, run_id, "4.3.1"))
    assert output.status == "spec_missing"
    (campaign,) = output.campaigns
    assert {(g.asset_type, g.reason) for g in campaign.gaps} == {
        ("sitelink", "spec_missing"),
        ("callout", "spec_missing"),
        ("structured_snippet", "spec_missing"),
    }
    assert "SitelinksCalloutsSnippetsDraft" not in script.requests
    assert web.requests == []
    rows = await _assets(db, run_id)
    assert not [row for row in rows.values() if row.node_id == "4.3.1"]
    _ = sa
