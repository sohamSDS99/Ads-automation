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
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.creative import offers
from agent.db.models import (
    CreativeAssetKind,
    CreativeAssetStatus,
    Evidence,
    EvidenceSource,
    PlanCalc,
    RunStatus,
)
from agent.orchestrator.dag import Dag
from agent.preview import landing, urlcheck
from agent.schemas.extras import (
    LeadFormOutput,
    OfferAssetsOutput,
    SitelinksCalloutsSnippetsOutput,
)
from agent.schemas.guardrails import OfferRecord
from agent.schemas.landing import Box
from tests.integration.conftest import ApiClient
from tests.integration.creative_support import TEXT_ONLY
from tests.integration.runs_support import execute
from tests.integration.test_s4p4_brief_g7 import _g7
from tests.integration.test_s4p5_headlines_combinations import _assets, _output
from tests.integration.test_s4p6_descriptions_variant_b import _registry, _seed
from tests.integration.test_s4p7_landing import _Script as _LandingScript
from tests.integration.test_s4p7_landing import _sections
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
OFFER_SPECS: dict[str, dict[str, Any]] = {
    "promotion": {"max_chars": 20, "max_count": 2, **SPEC},
    "price": {"max_chars": 25, "min_count": 3, "max_count": 8, **SPEC},
}
LEAD_FORM_SPECS: dict[str, dict[str, Any]] = {
    "lead_form": {"max_chars": 30, "max_count": 5, **SPEC},
}
#: 2.1.4's qualified lead, and what sales agreed makes a lead junk.
REQUIRED = ["job title", "company size"]
DISQUALIFIERS = ["student"]
#: The audited landing form: seven fields, one required signal (`job title`),
#: routed by email — S4-P7's CLASSIFY labels (`test_s4p7_landing.LABELS`).
FORM = ["first_name", "last_name", "email", "phone", "company", "job_title", "country"]
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
        if name == "OfferAssetsDraft":
            self.requests.setdefault(name, []).append(body)
            schema = body["response_format"]["json_schema"]["schema"]
            defs = schema["$defs"]
            answer: dict[str, Any] = {}
            if "promotions" in schema["properties"]:
                keys = list(defs["PromotionsDraft"]["properties"])
                answer["promotions"] = {
                    key: {"text": text} for key, text in zip(keys, PROMOTION_TEXT, strict=False)
                }
            if "price" in schema["properties"]:
                keys = list(defs["PriceItemsDraft"]["properties"])
                answer["price"] = {
                    "type": "SERVICE_TIERS",
                    "items": {
                        key: {"header": header, "description": "For growing EHS teams"}
                        for key, header in zip(keys, PRICE_HEADERS, strict=False)
                    },
                }
            return completion(answer, model=body["model"])
        if name == "LeadFormDraft":
            self.requests.setdefault(name, []).append(body)
            schema = body["response_format"]["json_schema"]["schema"]
            keys = list(schema["$defs"]["LeadFormQuestionsDraft"]["properties"])
            types = {"signal_1": "JOB_TITLE", "signal_2": "COMPANY_SIZE"}
            return completion(
                {
                    "headline": "Get an SDS demo",
                    "description": "Tell us about your EHS team",
                    "cta": "REQUEST_DEMO",
                    "questions": {
                        key: {"type": types[key], "text": None, "options": []} for key in keys
                    },
                },
                model=body["model"],
            )
        return super()._respond(request)


PROMOTION_TEXT = ["SDS software", "Team SDS plan"]
PRICE_HEADERS = ["Professional plan", "Team plan", "Starter plan"]


def _offer(sku: str, *, age: timedelta = timedelta(hours=1), **fields: Any) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "sku": sku,
        "product_set": "plans",
        "list_price": 99.0,
        "current_price": 99.0,
        "currency": "USD",
        "market": "US",
        "effective_from": (now - timedelta(days=1)).isoformat(),
        "ends_at": (now + timedelta(days=18)).isoformat(),
        "observed_at": (now - age).isoformat(),
        **fields,
    }


#: Fresh: a percentage off, an amount off, no saving. Stale: a bigger saving.
FRESH = [
    _offer("SDS-PRO", list_price=129.0, reference_price=129.0, current_price=99.0),
    _offer("SDS-TEAM", list_price=59.0, current_price=49.0),
    _offer("SDS-STARTER", list_price=29.0, current_price=29.0),
]
STALE = [_offer("SDS-OLD", age=timedelta(days=30), reference_price=199.0, current_price=99.0)]


async def _offers(db: AsyncSession, project_id: uuid.UUID, rows: list[dict[str, Any]]) -> None:
    """The offer snapshot's rows, as `csv_ingest` stores them."""
    for row in rows:
        db.add(
            Evidence(
                project_id=project_id,
                source=EvidenceSource.CSV,
                kind="offer_record",
                payload=row,
                hash=f"offer-{row['sku']}",
            )
        )
    await db.commit()


@pytest.fixture
def rendered_form(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every landing page renders, carrying the seven-field form (no browser needed)."""

    async def render(
        urls: Any, *, viewports: Any, timeout_ms: int = landing.NETWORKIDLE_TIMEOUT_MS
    ) -> list[landing.LandingRender]:
        def device(name: landing.Device, url: str) -> landing.DeviceRender:
            return landing.DeviceRender(
                device=name,
                viewport=viewports[name],
                user_agent="integration-suite",
                reached=True,
                final_url=url,
                http_status=200,
                settled=True,
                fold_px=viewports[name].height,
                h1="Keep every SDS current",
                text_nodes=[
                    landing.TextNode(
                        text="Keep every SDS current", box=Box(x=0, y=40, width=300, height=40)
                    )
                ],
                controls=[
                    landing.FormControl(
                        form=0,
                        tag="input",
                        name=field,
                        id=None,
                        label=field.replace("_", " "),
                        type="email" if field == "email" else "text",
                        required=field == "email",
                        visible=True,
                    )
                    for field in FORM
                ],
            )

        return [
            landing.LandingRender(
                url=url, mobile=device("mobile", url), desktop=device("desktop", url)
            )
            for url in urls
        ]

    monkeypatch.setattr(landing, "render_pages", render)


async def _crm(db: AsyncSession, project_id: uuid.UUID) -> None:
    """100 historical deals: 30 won, 40 lost to a student, 30 lost on price."""
    rows = [("crm_won", f"won-{n}", None) for n in range(30)]
    rows += [("crm_lost", f"student-{n}", "Student project") for n in range(40)]
    rows += [("crm_lost", f"price-{n}", "Price") for n in range(30)]
    for kind, account, reason in rows:
        db.add(
            Evidence(
                project_id=project_id,
                source=EvidenceSource.CSV,
                kind=kind,
                payload={"account_name": account, "close_reason": reason},
                hash=f"{kind}-{account}",
            )
        )
    await db.commit()


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
    plan: dict[str, Any] | None = None,
) -> tuple[uuid.UUID, _Script, RunStatus]:
    """A creative run started, approved at G7, and run to its end."""
    await _seed(db, ws, project_id, actor, extra_specs={"search": extra_specs or {}}, plan=plan)
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
    await _offers(db, project_id, FRESH)
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
    # 4.3.2: fresh offers, but the pin has neither surface.
    offer_output = OfferAssetsOutput.model_validate(await _output(db, run_id, "4.3.2"))
    assert offer_output.status == "spec_missing"
    assert offer_output.offers_fresh == 3
    assert {(g.asset_type, g.reason) for g in offer_output.gaps} == {
        ("promotion", "spec_missing"),
        ("price", "spec_missing"),
    }
    assert "OfferAssetsDraft" not in script.requests
    rows = await _assets(db, run_id)
    assert not [row for row in rows.values() if row.node_id in ("4.3.1", "4.3.2")]


# ---------------------------------------------------------------------------
# 4.3.2
# ---------------------------------------------------------------------------


async def test_every_promotion_and_price_figure_is_an_offer_binding(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    web: _Web,
) -> None:
    await _offers(db, project_id, [*FRESH, *STALE])
    run_id, script, _ = await _run(
        admin, db, workspace_id, project_id, admin_user.id, extra_specs=OFFER_SPECS
    )
    output = OfferAssetsOutput.model_validate(await _output(db, run_id, "4.3.2"))
    assert output.status == "required"
    assert (output.offers_fresh, output.offers_stale) == (3, 1)

    # --- the model saw no figure and could write none ------------------------
    (request,) = script.requests["OfferAssetsDraft"]
    schema = json.dumps(request["response_format"]["json_schema"]["schema"])
    assert '"number"' not in schema and '"integer"' not in schema
    user = next(m["content"] for m in request["messages"] if m["role"] == "user")
    listed = _sections(user)["OFFERS"]
    shown = json.dumps([entry for group in listed.values() for entry in group.values()])
    assert not any(ch.isdigit() for ch in shown), shown
    assert "USD" not in shown
    assert "SDS-OLD" not in user  # stale: skipped, never guessed

    # --- every number and date is a binding, rendered from the record -------
    records = {row["sku"]: OfferRecord.model_validate(row) for row in FRESH}
    by_sku = {p.offer_binding.sku_or_set: p for p in output.promotions}
    assert set(by_sku) == {"SDS-PRO", "SDS-TEAM"}  # SDS-STARTER saves nothing
    pro, team = by_sku["SDS-PRO"], by_sku["SDS-TEAM"]
    assert (pro.discount_kind, pro.bound.percent_off, pro.bound.currency) == (
        "percent_off",
        "23",
        "USD",
    )
    assert (team.discount_kind, team.bound.money_off) == ("money_off", "10.00")
    for sku, promotion in by_sku.items():
        binding = promotion.offer_binding
        assert binding.offer_record_id == offers.record_id(records[sku])
        assert binding.resolved == offers.resolve(records[sku], binding.fields)
        assert promotion.end == records[sku].ends_at.isoformat()  # type: ignore[union-attr]
        assert not any(ch.isdigit() for ch in promotion.text)
    (price,) = output.prices
    assert price.type == "SERVICE_TIERS"
    assert {item.offer_binding.sku_or_set: item.bound.price for item in price.items} == {
        "SDS-PRO": "99.00",
        "SDS-STARTER": "29.00",
        "SDS-TEAM": "49.00",
    }

    # --- rows: linted at creation, the binding stored beside the figures ----
    rows = [row for row in (await _assets(db, run_id)).values() if row.node_id == "4.3.2"]
    assert {row.kind for row in rows} == {CreativeAssetKind.PROMOTION, CreativeAssetKind.PRICE}
    assert len(rows) == 2 + 3
    for row in rows:
        assert row.status is CreativeAssetStatus.LINTED
        assert row.lint["verdict"] in ("pass", "pass_with_warnings")
        assert row.offer_binding is not None
        shown = row.fields["bound"]
        assert all(shown[key] == row.offer_binding["resolved"][key] for key in shown)
        assert row.offer_binding["sku_or_set"] != "SDS-OLD"


async def test_stale_offers_are_not_required_and_ask_no_model(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    web: _Web,
) -> None:
    await _offers(db, project_id, STALE)
    run_id, script, _ = await _run(
        admin, db, workspace_id, project_id, admin_user.id, extra_specs=OFFER_SPECS
    )
    output = OfferAssetsOutput.model_validate(await _output(db, run_id, "4.3.2"))
    assert output.status == "not_required"
    assert (output.offers_fresh, output.offers_stale) == (0, 1)
    assert "never guessed" in output.why
    assert "OfferAssetsDraft" not in script.requests
    rows = await _assets(db, run_id)
    assert not [row for row in rows.values() if row.node_id == "4.3.2"]


# ---------------------------------------------------------------------------
# 4.3.3
# ---------------------------------------------------------------------------


async def test_the_lead_form_has_a_resolving_privacy_url_and_the_tradeoff_writes_evidence(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    web: _Web,
    rendered_form: None,
) -> None:
    await _crawl(db, project_id, CRAWLED)
    await _crm(db, project_id)
    run_id, script, _ = await _run(
        admin,
        db,
        workspace_id,
        project_id,
        admin_user.id,
        extra_specs=LEAD_FORM_SPECS,
        plan={"required_signals": REQUIRED, "disqualifiers": DISQUALIFIERS},
    )
    output = LeadFormOutput.model_validate(await _output(db, run_id, "4.3.3"))
    assert output.status == "required"
    (campaign,) = output.campaigns
    assert campaign.campaign_ref == "c-sds-us"
    assert campaign.gaps == []
    form, tradeoff = campaign.form, campaign.tradeoff
    assert form is not None and tradeoff is not None

    # --- the privacy URL resolved 2xx on-domain ------------------------------
    assert form.privacy_policy_url == f"{SITE}/privacy"
    assert form.privacy_url_check.status == "ok"
    assert form.privacy_url_check.http_status == 200
    assert [str(r.url) for r in web.requests] == [f"{SITE}/privacy"]

    # --- the contact and the chosen signals, nothing else; no Art. 9 ---------
    assert [(q.type, q.qualifies_signal) for q in form.questions] == [
        ("EMAIL", None),
        ("JOB_TITLE", "job title"),
        ("COMPANY_SIZE", "company size"),
    ]
    (request,) = script.requests["LeadFormDraft"]
    user = next(m["content"] for m in request["messages"] if m["role"] == "user")
    assert _sections(user)["QUESTIONS"] == {"signal_1": "job title", "signal_2": "company size"}
    assert "Student project" not in user  # CRM aggregates never reach a prompt

    # --- the trade-off is a calc/ result with PlanCalc + derived Evidence ----
    assert (tradeoff.fields_n, tradeoff.expected_leads, tradeoff.expected_qualified) == (
        3,
        152.42,
        152.42,
    )
    (evidence_id,) = tradeoff.calc_evidence_ids
    calc = (
        await db.execute(sa.select(PlanCalc).where(PlanCalc.evidence_id == evidence_id))
    ).scalar_one()
    assert (calc.plan_run_id, calc.node_id, calc.formula_id) == (
        run_id,
        "4.3.3",
        "leadform.field_tradeoff_v1",
    )
    assert calc.inputs["history_fields_n"] == len(FORM)
    assert calc.inputs["history_signals_n"] == 1
    assert calc.inputs["lost_reasons"] == {"Price": 30, "Student project": 40}
    assert [option["fields_n"] for option in calc.result["options"]] == [2, 3]
    evidence = await db.get(Evidence, evidence_id)
    assert evidence is not None
    assert (evidence.source, evidence.kind) == (EvidenceSource.DERIVED, "calc_leadform")
    assert evidence.payload["result"]["chosen"]["fields_n"] == 3

    # --- one row, linted at creation ----------------------------------------
    (row,) = [r for r in (await _assets(db, run_id)).values() if r.node_id == "4.3.3"]
    assert row.kind is CreativeAssetKind.LEAD_FORM
    assert row.status is CreativeAssetStatus.LINTED
    assert row.fields["privacy_url_check"]["status"] == "ok"
    assert row.fields["tradeoff"]["calc_evidence_ids"] == [str(evidence_id)]


async def test_no_privacy_url_no_form_and_an_article_9_signal_is_never_asked(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    web: _Web,
) -> None:
    web.pages[f"{SITE}/privacy"] = (404, None)
    await _crawl(db, project_id, CRAWLED)
    run_id, script, _ = await _run(
        admin,
        db,
        workspace_id,
        project_id,
        admin_user.id,
        extra_specs=LEAD_FORM_SPECS,
        plan={"required_signals": ["job title", "health condition"]},
    )
    output = LeadFormOutput.model_validate(await _output(db, run_id, "4.3.3"))
    assert output.status == "required"
    (campaign,) = output.campaigns
    assert campaign.form is None
    assert campaign.tradeoff is None
    reasons = {gap.reason: gap.detail for gap in campaign.gaps}
    assert set(reasons) == {"art9_signal", "no_crm_history", "no_privacy_policy_url"}
    assert "health condition" in reasons["art9_signal"]
    assert (
        "404" in reasons["no_privacy_policy_url"]
        or "http_error" in reasons["no_privacy_policy_url"]
    )
    assert "LeadFormDraft" not in script.requests
    rows = await _assets(db, run_id)
    assert not [row for row in rows.values() if row.node_id == "4.3.3"]


async def test_the_lead_form_is_not_required_without_a_lead_gen_objective(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    web: _Web,
) -> None:
    run_id, script, _ = await _run(
        admin,
        db,
        workspace_id,
        project_id,
        admin_user.id,
        extra_specs=LEAD_FORM_SPECS,
        plan={"objective": "awareness"},
    )
    output = LeadFormOutput.model_validate(await _output(db, run_id, "4.3.3"))
    assert output.status == "not_required"
    assert output.campaigns == []
    assert "LeadFormDraft" not in script.requests
