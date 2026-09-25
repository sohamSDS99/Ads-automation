"""S4-P7 exit criteria — 4.5.1 and 4.5.2 end to end, against the three fixture pages.

A creative run is started through the API, halts on G7, is approved, and runs
the whole DAG with scripted COPYWRITE and CLASSIFY answers — but **a real
Chromium** renders the Search ad group's landing page, served from
`tests/fixtures/landing/` by a local server that logs every request it gets.
So this needs a test image with a browser (the worker image; see the PR).

- a mismatched H1 scores below `landing.message_match_min`, and the proposed
  H1 is linted at the run's pin — a failing proposal is never the one proposed;
- an offer below the fold is detected per device;
- a nine-field form reduces to the minimal set computed in code;
- no request other than GET leaves the browser: the renderer's route
  interception blocked the page's writes, and the server saw only GETs.

Asserted from the database, the stored node outputs and the landing routes —
never the run's event stream, which does not close on a paused run.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import Evidence, LandingPageAudit, Run, RunStage, RunStatus
from agent.orchestrator.dag import Dag
from agent.orchestrator.registry import get_registry
from agent.preview import landing
from agent.storage import get_storage
from tests.integration.conftest import ApiClient
from tests.integration.creative_support import TEXT_ONLY
from tests.integration.runs_support import execute
from tests.integration.test_s4p4_brief_g7 import _g7
from tests.integration.test_s4p5_headlines_combinations import _output
from tests.integration.test_s4p6_descriptions_variant_b import _Script as _CopyScript
from tests.integration.test_s4p6_descriptions_variant_b import _seed
from tests.landing_support import (
    MISMATCHED_H1,
    NINE_FIELD_FORM,
    OFFER_BELOW_FOLD,
    FixtureServer,
    fixture_server,
)
from tests.openrouter_fake import completion

pytestmark = pytest.mark.asyncio

REAL_RENDER_PAGES = landing.render_pages
THRESHOLD = 0.55
#: What CLASSIFY answers for the nine-field form.
LABELS = {
    "first_name": "none",
    "last_name": "none",
    "email": "contact_email",
    "phone": "contact_phone",
    "company": "none",
    "job_title": "job title",
    "company_size": "company size",
    "country": "none",
    "consent": "consent",
}
REQUIRED = ["job title", "company size"]


@pytest.fixture
def real_landing_renderer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo the suite's `unrendered_landing_pages`: this module renders for real."""
    monkeypatch.setattr(landing, "render_pages", REAL_RENDER_PAGES)


def _sections(user: str) -> dict[str, Any]:
    """`render_prompt`'s `NAME:\\n<json>` sections, back into values."""
    return {
        name: json.loads(value)
        for name, value in (part.split(":\n", 1) for part in user.split("\n\n"))
    }


class _Script(_CopyScript):
    """S4-P6's scripted copy, plus the two landing requests.

    H1 proposals: the first candidate says "#1" — a claim the pin does not
    license — and echoes the ad best; the second echoes it and says nothing
    it may not; the third echoes nothing. Code must propose the second.
    """

    def __init__(self) -> None:
        super().__init__()
        self.h1: list[dict[str, Any]] = []
        self.fields: list[dict[str, Any]] = []

    def _respond(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        name = body["response_format"]["json_schema"]["name"]
        user = next(m["content"] for m in body["messages"] if m["role"] == "user")
        if name == "H1ProposalsDraft":
            self.h1.append(body)
            ads = _sections(user)["ADS"]
            lead = ads[0]["headlines"][0]
            return completion(
                {
                    "candidates": [
                        {"text": f"The #1 {lead}"},
                        {"text": f"{lead} for chemical safety teams"},
                        {"text": "Welcome to our company"},
                    ]
                },
                model=body["model"],
            )
        if name == "FormFieldSignalsDraft":
            self.fields.append(body)
            shown = json.loads(user.split("FIELDS:\n", 1)[1])
            return completion(
                {key: LABELS.get(field["name"], "none") for key, field in shown.items()},
                model=body["model"],
            )
        return super()._respond(request)


async def _run(
    admin: ApiClient,
    db: AsyncSession,
    ws: uuid.UUID,
    project_id: uuid.UUID,
    actor: uuid.UUID,
    *,
    url: str,
    required_signals: list[str] | None = None,
) -> tuple[uuid.UUID, _Script]:
    """A creative run started, approved at G7, and run to its end."""
    await _seed(
        db,
        ws,
        project_id,
        actor,
        plan={"landing_url": url, "required_signals": required_signals},
    )
    started = await admin.post(
        f"/projects/{project_id}/creative/runs", json={"scope": TEXT_ONLY, "media_models": []}
    )
    assert started.status_code == 202, started.text
    run_id = uuid.UUID(started.json()["run_id"])
    script = _Script()
    registry = get_registry().for_stage(RunStage.CREATIVE)
    halted = await execute(run_id, script.fake, registry=registry, dag=Dag.from_registry(registry))
    assert halted.status is RunStatus.AWAITING_APPROVAL, halted.error
    decided = await admin.post(
        f"/approvals/{(await _g7(db, run_id)).id}", json={"decision": "approve"}
    )
    assert decided.status_code == 200, decided.text
    result = await execute(
        run_id, script.fake, registry=registry, dag=Dag.from_registry(registry), max_attempts=1
    )
    assert result.status is RunStatus.SUCCEEDED, result.error
    return run_id, script


async def _audit(db: AsyncSession, run_id: uuid.UUID) -> LandingPageAudit:
    (row,) = (
        (
            await db.execute(
                sa.select(LandingPageAudit)
                .where(LandingPageAudit.creative_run_id == run_id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    return row


async def _evidence(db: AsyncSession, row: LandingPageAudit, kind: str) -> list[Evidence]:
    return list(
        (
            await db.execute(
                sa.select(Evidence).where(Evidence.id.in_(row.evidence_ids), Evidence.kind == kind)
            )
        )
        .scalars()
        .all()
    )


def _only_gets(server: FixtureServer) -> None:
    assert server.methods() == {"GET"}
    assert not {"/collect", "/profile", "/beacon", "/lead", "/consent"} & {
        hit.path for hit in server.hits
    }


async def test_a_mismatched_h1_scores_below_the_threshold_with_a_linted_proposed_h1(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    real_landing_renderer: None,
) -> None:
    with fixture_server() as server:
        url = server.url(MISMATCHED_H1)
        run_id, script = await _run(admin, db, workspace_id, project_id, admin_user.id, url=url)
        _only_gets(server)

    matched = await _output(db, run_id, "4.5.1")
    (page,) = matched["pages"]
    assert page["url"] == url and page["reachable"] is True
    assert page["h1"] == {
        "mobile": "Welcome to Acme Industrial Group",
        "desktop": "Welcome to Acme Industrial Group",
    }
    match = page["message_match"]
    assert match["metric"] == "match.token_trigram_v1"
    assert match["threshold"] == THRESHOLD
    assert match["verdict"] == "fail" and match["score"] < THRESHOLD
    assert {score["device"] for score in match["scores"]} == {"mobile", "desktop"}

    # The model was shown the ad group's final A headlines — 4.2.3's selection.
    coherence = await _output(db, run_id, "4.2.3")
    spread = await _output(db, run_id, "4.2.1")
    texts = {c["asset_id"]: c["default_text"] for g in spread["ad_groups"] for c in g["candidates"]}
    (ad,) = [ad for ad in coherence["ads"] if ad["variant"] == "A"]
    (request,) = script.h1
    user = next(m["content"] for m in request["messages"] if m["role"] == "user")
    (shown,) = _sections(user)["ADS"]
    assert shown["headlines"] == [texts[asset_id] for asset_id in ad["headlines"]]
    lead = shown["headlines"][0]

    # The "#1" proposal matched as well and failed lint: never proposed.
    proposed = page["proposed_h1"]
    assert proposed is not None, page["proposed_h1_note"]
    assert proposed["text"] == f"{lead} for chemical safety teams"
    assert proposed["score"] >= THRESHOLD
    assert proposed["lint"]["verdict"] in ("pass", "pass_with_warnings")

    row = await _audit(db, run_id)
    run = await db.get(Run, run_id)
    assert run is not None
    assert row.metrics["proposed_h1_lint"]["ruleset_version"] == run.pins[-1]["ruleset_version"]
    assert row.metrics["message_match"]["score"] == match["score"]
    assert row.verdict.value == "needs_change"
    assert row.patch is not None and row.patch["h1"] == proposed["text"]
    assert row.patch["offer_block"] is None and row.patch["remove_fields"] == []

    # DOM facts and the score are Evidence; the screenshots are stored.
    (metric,) = (
        (await db.execute(sa.select(Evidence).where(Evidence.id.in_(match["evidence_ids"]))))
        .scalars()
        .all()
    )
    assert (metric.source.value, metric.kind) == ("derived", "metric_message_match")
    assert metric.payload["score"] == match["score"]
    doms = await _evidence(db, row, "landing_dom")
    assert {e.payload["device"] for e in doms} == {"mobile", "desktop"}
    assert {e.payload["h1"] for e in doms} == {"Welcome to Acme Industrial Group"}
    storage = get_storage()
    assert set(row.screenshots) == {"mobile", "desktop"}
    for key in row.screenshots.values():
        assert storage.exists(key) and storage.get(key).startswith(b"\x89PNG")

    # The landing routes.
    listed = await admin.get(f"/creative-runs/{run_id}/landing-audits")
    assert listed.status_code == 200, listed.text
    (item,) = listed.json()["items"]
    assert (item["id"], item["url"], item["verdict"]) == (str(row.id), url, "needs_change")
    assert item["has_patch"] is True
    as_json = await admin.get(f"/landing-audits/{row.id}/patch", params={"format": "json"})
    assert as_json.status_code == 200, as_json.text
    assert as_json.json()["h1"] == proposed["text"]
    as_html = await admin.get(f"/landing-audits/{row.id}/patch", params={"format": "html"})
    assert as_html.status_code == 200, as_html.text
    assert as_html.headers["content-type"].startswith("text/html")
    assert f"<h1>{proposed['text']}</h1>" in as_html.text
    assert "default-src 'none'" in as_html.headers["content-security-policy"]


async def test_an_offer_below_the_fold_is_detected_per_device(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    real_landing_renderer: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Until S4-P8's offers.py, 4.1.1 writes `offer: null` into every brief; the
    # phrase a binding would render is supplied here, and 4.5.2 does the rest.
    monkeypatch.setattr(
        "agent.creative.landing_audit.offer_phrase", lambda _offer: "Get 20% off the first year"
    )
    with fixture_server() as server:
        run_id, _script = await _run(
            admin, db, workspace_id, project_id, admin_user.id, url=server.url(OFFER_BELOW_FOLD)
        )
        _only_gets(server)

    (page,) = (await _output(db, run_id, "4.5.2"))["pages"]
    folds = {fold["device"]: fold for fold in page["offer_above_fold"]}
    assert folds["mobile"]["found"] is False
    assert folds["desktop"]["found"] is True
    assert folds["mobile"]["bbox"]["y"] >= 844 > folds["desktop"]["bbox"]["y"]
    # The H1 echoes the ad, so the offer is the only reason — and it blocks launch.
    assert page["verdict"] == "blocking_for_launch"
    assert page["patch"]["offer_block"] == {
        "phrase": "Get 20% off the first year",
        "devices": ["mobile"],
    }
    assert page["patch"]["h1"] is None
    row = await _audit(db, run_id)
    assert row.verdict.value == "blocking_for_launch"
    assert row.metrics["offer_above_fold"] == page["offer_above_fold"]


async def test_a_nine_field_form_reduces_to_the_computed_minimal_set(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    real_landing_renderer: None,
) -> None:
    with fixture_server() as server:
        run_id, script = await _run(
            admin,
            db,
            workspace_id,
            project_id,
            admin_user.id,
            url=server.url(NINE_FIELD_FORM),
            required_signals=REQUIRED,
        )
        _only_gets(server)

    # One CLASSIFY call: nine fields, each one required enum over the vocabulary.
    (request,) = script.fields
    schema = request["response_format"]["json_schema"]["schema"]
    assert sorted(schema["required"]) == [f"f{i:02d}" for i in range(9)]
    assert schema["properties"]["f00"]["enum"] == [
        *REQUIRED,
        "contact_email",
        "contact_phone",
        "consent",
        "privacy",
        "none",
    ]

    (page,) = (await _output(db, run_id, "4.5.2"))["pages"]
    form = page["form"]
    assert [field["name"] for field in form["fields"]] == list(LABELS)
    assert form["minimal_set"] == ["email", "job_title", "company_size", "consent"]
    assert form["remove"] == ["first_name", "last_name", "phone", "company", "country"]
    assert [(k["field"], k["reason"]) for k in form["keep_reason"]] == [
        ("email", "routing_contact"),
        ("job_title", "required_signal"),
        ("company_size", "required_signal"),
        ("consent", "consent"),
    ]
    assert page["patch"]["remove_fields"] == form["remove"]
    assert page["verdict"] == "needs_change"
    assert page["offer_above_fold"] == []  # the brief carries no offer

    # The page tried four writes on load; each was blocked in the browser.
    row = await _audit(db, run_id)
    renders = await _evidence(db, row, "landing_render")
    assert len(renders) == 2
    for evidence in renders:
        blocked = {(b["method"], b["url"].rsplit("/", 1)[1]) for b in evidence.payload["blocked"]}
        assert {("POST", "collect"), ("PUT", "profile"), ("POST", "beacon"), ("POST", "lead")} <= (
            blocked
        )
    assert row.metrics["obscured_by_overlay"] == {"mobile": True, "desktop": False}


async def test_an_unreachable_page_is_recorded_and_asks_no_model(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    real_landing_renderer: None,
) -> None:
    with fixture_server() as server:
        closed = server.url(MISMATCHED_H1)
    run_id, script = await _run(admin, db, workspace_id, project_id, admin_user.id, url=closed)
    assert script.h1 == [] and script.fields == []
    (page,) = (await _output(db, run_id, "4.5.2"))["pages"]
    assert page["verdict"] == "unreachable" and page["patch"] is None
    assert "did not answer" in page["reasons"][0]
    row = await _audit(db, run_id)
    assert row.final_url is None and row.http_status is None
    missing = await admin.get(f"/landing-audits/{row.id}/patch")
    assert missing.status_code == 404
    assert missing.headers["content-type"].startswith("application/problem+json")
    unknown = await admin.get(f"/landing-audits/{uuid.uuid4()}/patch")
    assert unknown.status_code == 404
