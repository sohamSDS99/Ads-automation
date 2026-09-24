"""S4-P6 exit criteria — 4.2.2 `claim_bound_descriptions`, end to end.

A creative run is started through the API, halts on G7, is approved, and runs
on through the real 4.2.1, 4.2.2 and 4.2.3 (scripted COPYWRITE and CLASSIFY
answers), then the rest of the DAG.

**The pin carries Stage 03's real claim-licence rule and the shipped
detectors**, beside the spec-sheet rules synthesis emits. So "#1" is found by
the rule a published ruleset carries, not by anything Stage 04 wrote, and a
description that says it becomes an exception candidate the same way it would
in production.

Asserted from the database and the stored node outputs, never from the run's
event stream: `/runs/{id}/events` never closes on a paused run.
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import CreativeAssetKind, CreativeAssetStatus, RunStage, RunStatus
from agent.export.guideline_contract import AssetSpecs
from agent.guardrails.matchers.claims import claim_licence
from agent.guidelines.constants import get_content_constants, load_content_constants
from agent.guidelines.synthesis import _asset_rules
from agent.orchestrator.dag import Dag
from agent.orchestrator.registry import NodeRegistry, get_registry
from agent.schemas.guardrails import Authority
from agent.schemas.search_ads import ClaimBoundDescriptionsOutput
from tests.integration.conftest import ApiClient
from tests.integration.creative_support import (
    DRAFT_CLAIM_ID,
    LICENSED_CLAIM_ID,
    TEXT_ONLY,
    seed_plan,
    seed_published,
    seed_signoff,
)
from tests.integration.runs_support import execute
from tests.integration.test_s4p4_brief_g7 import _g7, _instance
from tests.integration.test_s4p5_headlines_combinations import _assets, _output
from tests.openrouter_fake import FakeOpenRouter, completion

pytestmark = pytest.mark.asyncio

KEYWORDS = [
    {"term": "sds software", "search_volume": 900},
    {"term": "sds management software", "search_volume": 400},
    {"term": "safety data sheet software", "search_volume": 300},
]
CLAIM = str(LICENSED_CLAIM_ID)
CLAIM_TEXT = "SDS updates within 24 hours"


def _h(text: str, category: str, *, keyword_ref: str | None = None, claims: bool = False) -> dict:
    return {
        "text": text,
        "category": category,
        "keyword_ref": keyword_ref,
        "claim_ids": [CLAIM] if claims else [],
        "dki": False,
    }


#: Twenty-five headlines with no claim-shaped language: the claim rule passes them.
POOL_A: list[dict[str, Any]] = [
    _h("SDS Software For Your Team", "keyword", keyword_ref="sds software"),
    _h("SDS Management Software", "keyword", keyword_ref="sds management software"),
    _h("Safety Data Sheet Software", "keyword", keyword_ref="safety data sheet software"),
    _h("Simple SDS Software", "keyword", keyword_ref="sds software"),
    _h("Every Sheet Current, Always", "benefit"),
    _h("Audit-Ready Chemical Records", "benefit"),
    _h("One Library For Every Site", "benefit"),
    _h("Less Paperwork Every Week", "benefit"),
    _h("Find Any SDS In Seconds", "benefit"),
    _h("Clear Records For Audits", "benefit"),
    _h("Free Trial For Your Team", "offer"),
    _h("Start Your Free Trial", "offer"),
    _h("Try It Free This Month", "offer"),
    _h("SDS Updates Within 24 Hours", "proof", claims=True),
    _h("Sheets Refreshed In 24 Hours", "proof", claims=True),
    _h("Updates Within 24 Hours", "proof", claims=True),
    _h("No IT Team Needed", "objection"),
    _h("Set Up In One Afternoon", "objection"),
    _h("Works With Your Files", "objection"),
    _h("Cancel Any Time", "objection"),
    _h("No Long Contracts", "objection"),
    _h("Book Your Demo Today", "cta"),
    _h("Book A Walkthrough Now", "cta"),
    _h("Book Time With Our Team", "cta"),
    _h("Book A Call With Us", "cta"),
]
assert len(POOL_A) == 25


def _d(text: str, quote: str = CLAIM_TEXT) -> dict[str, Any]:
    return {"text": text, "claim_ids": [CLAIM], "claim_text": quote}


SELECTED_A = [
    f"Every sheet on file stays current: {CLAIM_TEXT}.",
    f"One searchable library for every site, with {CLAIM_TEXT}.",
    f"Audit-ready records without the paperwork. {CLAIM_TEXT}.",
    f"{CLAIM_TEXT}, so your team never works from an old sheet.",
]
RESERVE_A = f"{CLAIM_TEXT} keep your chemical inventory audit-ready."
#: "#1" is superlative claim-shaped language no claim licenses — twice.
UNLICENSED_1 = f"The #1 SDS software, with {CLAIM_TEXT}."
UNLICENSED_2 = f"{CLAIM_TEXT} from the #1 team in chemical safety."
#: 107 characters: fails `asset_spec.length.v1`, and stays draft.
TOO_LONG = (
    f"{CLAIM_TEXT} for every safety data sheet in every one of your warehouses, labs and offices."
)
#: Eight descriptions, as COPYWRITE answers 4.2.2's schema, and two paths.
DESCRIPTIONS_A: dict[str, Any] = {
    "descriptions": [
        _d(SELECTED_A[0]),
        _d(UNLICENSED_1),
        _d(SELECTED_A[1]),
        _d(SELECTED_A[2]),
        _d(TOO_LONG),
        _d(UNLICENSED_2),
        _d(SELECTED_A[3], quote="sds UPDATES within 24 hours"),
        _d(RESERVE_A),
    ],
    "paths": ["sds", "software"],
}
assert len(DESCRIPTIONS_A["descriptions"]) == 8


class _Script:
    """A scripted OpenRouter that answers each node by the schema it sends."""

    def __init__(self) -> None:
        self.fake = FakeOpenRouter()
        self.requests: dict[str, list[dict[str, Any]]] = {}
        self.fake.dispatch(self._respond)

    def _respond(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        schema_spec = body["response_format"]["json_schema"]
        name, schema = schema_spec["name"], schema_spec["schema"]
        self.requests.setdefault(name, []).append(body)
        if name == "CreativeBriefDraft":
            return completion(_instance(schema, schema), model=body["model"])
        if name == "HeadlinePoolDraft":
            return completion({"candidates": POOL_A}, model=body["model"])
        if name == "DescriptionPoolDraft":
            return completion(DESCRIPTIONS_A, model=body["model"])
        if name == "PairLabelsDraft":
            user = next(m["content"] for m in body["messages"] if m["role"] == "user")
            pairs = json.loads(user.split("PAIRS:\n", 1)[1])
            return completion({key: "reads_well" for key in pairs})
        raise AssertionError(f"no scripted answer for {name}")


def _registry() -> NodeRegistry:
    return get_registry().for_stage(RunStage.CREATIVE)


async def _seed(db: AsyncSession, ws: uuid.UUID, project_id: uuid.UUID, actor: uuid.UUID) -> None:
    sheet = get_content_constants().asset_sheet()
    specs = tuple(_asset_rules(AssetSpecs(sheet=sheet, scope="unscoped"), lambda _why: None))
    constants = load_content_constants()
    detectors = constants.detectors()
    licence = claim_licence(
        tuple(d.detector_id for d in detectors if d.locale == "en"),
        authority=Authority(
            source="legal_signature", reference="sig", reviewed_at=date(2026, 9, 24)
        ),
        match_threshold=float(constants.value("claims.match_threshold")),
    )
    await seed_plan(db, ws, project_id, actor, campaign_type="search", keywords=KEYWORDS)
    await seed_published(
        db,
        ws,
        project_id,
        actor,
        asset_specs=sheet.model_dump(mode="json")["specs"],
        extra_rules=(*specs, licence),
        detectors=tuple(detectors),
    )
    await seed_signoff(db, ws, project_id, actor)
    await db.commit()


async def _run(
    admin: ApiClient, db: AsyncSession, ws: uuid.UUID, project_id: uuid.UUID, actor: uuid.UUID
) -> tuple[uuid.UUID, _Script, RunStatus]:
    """A creative run started, halted on G7, approved, and run to its end."""
    await _seed(db, ws, project_id, actor)
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
    return run_id, script, result.status


async def test_every_description_stands_on_a_licensed_claim_and_no_unlicensed_span_ships(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
) -> None:
    run_id, script, status = await _run(admin, db, workspace_id, project_id, admin_user.id)
    assert status is RunStatus.SUCCEEDED

    # --- the model could cite only what the pin licenses ---------------------
    (request,) = script.requests["DescriptionPoolDraft"]
    item = request["response_format"]["json_schema"]["schema"]["$defs"]["DescriptionDraft"]
    claim_ids = item["properties"]["claim_ids"]
    # One licensed claim renders as `const`, several as `enum`: either way the
    # draft claim is not on the menu, and a description must pick one.
    assert claim_ids["items"].get("enum", [claim_ids["items"].get("const")]) == [CLAIM]
    assert claim_ids["minItems"] == 1

    output = await _output(db, run_id, "4.2.2")
    (group,) = output["ad_groups"]

    # --- every description carries >= 1 claim licensed at the pin -----------
    assert [d["text"] for d in group["descriptions"]] == SELECTED_A
    for description in [*group["descriptions"], *group["reserve"]]:
        assert description["claim_ids"] == [CLAIM]
        start, end = description["claim_span"]
        assert description["text"][start:end].casefold() == CLAIM_TEXT.casefold()
    assert [d["text"] for d in group["reserve"]] == [RESERVE_A]
    assert group["paths"] == ["sds", "software"]
    # A schema validator, not a check after the fact: the stored output is
    # valid against this pin and invalid against one that licenses another claim.
    ClaimBoundDescriptionsOutput.model_validate(
        output, context={"licensed_claim_ids": frozenset({LICENSED_CLAIM_ID})}
    )
    with pytest.raises(ValidationError, match="not licensed at the pin"):
        ClaimBoundDescriptionsOutput.model_validate(
            output, context={"licensed_claim_ids": frozenset({DRAFT_CLAIM_ID})}
        )

    # --- an unlicensed claim span is an exception candidate, never an asset --
    assert group["exception_candidates"] == [{"span": "#1", "occurrences": 2}]
    assets = await _assets(db, run_id)
    assert not [a for a in assets.values() if a.text and "#1" in a.text]
    descriptions = {a.text: a for a in assets.values() if a.kind is CreativeAssetKind.DESCRIPTION}
    assert set(descriptions) == {*SELECTED_A, RESERVE_A, TOO_LONG}
    assert {descriptions[text].status for text in SELECTED_A} == {CreativeAssetStatus.LINTED}
    assert descriptions[RESERVE_A].status is CreativeAssetStatus.RESERVE
    too_long = descriptions[TOO_LONG]
    assert too_long.status is CreativeAssetStatus.DRAFT, "a lint failure never leaves draft"
    assert [f["rule_id"] for f in too_long.lint["findings"]] == ["asset_spec.length.v1"]  # type: ignore[index]
    assert all(a.node_id == "4.2.2" and a.variant.value == "A" for a in descriptions.values())  # type: ignore[union-attr]
    paths = {a.text: a.status for a in assets.values() if a.kind is CreativeAssetKind.PATH}
    assert paths == {"sds": CreativeAssetStatus.LINTED, "software": CreativeAssetStatus.LINTED}

    # --- 4.2.3 consumed the real 4.2.2 ----------------------------------------
    (ad,) = (await _output(db, run_id, "4.2.3"))["ads"]
    assert ad["descriptions"] == [d["asset_id"] for d in group["descriptions"]]
