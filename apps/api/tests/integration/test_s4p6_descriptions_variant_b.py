"""S4-P6 exit criteria — 4.2.2, 4.2.4 and 4.2.5, end to end.

A creative run is started through the API, halts on G7, is approved, and runs
on through the real 4.2.1, 4.2.2, 4.2.3, 4.2.4 and 4.2.5 (scripted COPYWRITE
and CLASSIFY answers), then the rest of the DAG.

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
import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import (
    CreativeAssetKind,
    CreativeAssetStatus,
    NodeRun,
    NodeRunStatus,
    RunStage,
    RunStatus,
)
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
from tests.integration.s4p14_support import past_h3
from tests.integration.test_s4p4_brief_g7 import _g7, _instance
from tests.integration.test_s4p5_headlines_combinations import _assets, _output
from tests.integration.variant_b_support import (
    DESCRIPTIONS_B,
    POOL_B,
    SELECTED_B,
    is_variant_b,
)
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


def _brief(schema: dict[str, Any]) -> dict[str, Any]:
    """`_instance`'s brief, with every ad group in scope briefed once (4.1.1's rule)."""
    answer = _instance(schema, schema)
    slot = schema["$defs"]["AdGroupDraft"]["properties"]["slot"]
    keys = slot.get("enum") or [slot["const"]]
    answer["ad_groups"] = [{**answer["ad_groups"][0], "slot": key} for key in keys]
    return answer


PMAX = {
    "name": "PMax - SDS - US",
    "campaign_ref": "c-pmax-us",
    "type": "performance_max",
    "ad_groups": [
        {
            "name": "sds asset group",
            "theme": "SDS management",
            "landing_url": "https://example.com/sds",
            "primary_message": "Keep every SDS current",
        }
    ],
}
SEARCH_ONLY = {
    "slate": [{"campaign_type": "search", "market": "US", "campaign_refs": ["c-sds-us"]}]
}
WITH_PMAX = {
    "slate": [
        *SEARCH_ONLY["slate"],
        {"campaign_type": "performance_max", "market": "US", "campaign_refs": ["c-pmax-us"]},
    ]
}
#: 36 characters: fails the Performance Max headline limit, and stays draft.
PMAX_TOO_LONG = "Keep Every Safety Data Sheet Current"
PMAX_UNLICENSED = f"The #1 SDS library for every site, with {CLAIM_TEXT}."
#: As COPYWRITE answers 4.2.5's schema for the Performance Max asset group:
#: the spec's max counts — 15 headlines, 5 long headlines, 5 descriptions.
ASSET_GROUP: dict[str, Any] = {
    "headlines": [
        "Chemical Records, Sorted",
        "Every Site, One Library",
        "Sheets Your Crews Can Find",
        "Hazard Data At Hand",
        "Less Binder Work",
        "Answers For Inspectors",
        "Plain Steps For Spills",
        "One Login, Every Site",
        "Current Sheets, Always",
        "Labels From The Latest",
        "Built For EHS Teams",
        "Set Up In An Afternoon",
        "See A Guided Tour",
        "Start With One Site",
        PMAX_TOO_LONG,
    ],
    "long_headlines": [
        "Keep every safety data sheet on every site current",
        "One searchable library for every chemical you store",
        "Hazard, first-aid and spill steps where your crews work",
        "Chemical records that are ready when an inspector asks",
        "Move off binders without losing a single sheet",
    ],
    "descriptions": [
        {"text": f"Every sheet stays current: {CLAIM_TEXT}.", "claim_ids": [CLAIM]},
        {"text": f"One library for every site, with {CLAIM_TEXT}.", "claim_ids": [CLAIM]},
        {"text": PMAX_UNLICENSED, "claim_ids": [CLAIM]},
        {"text": f"Crews find the right sheet fast. {CLAIM_TEXT}.", "claim_ids": [CLAIM]},
        {"text": f"{CLAIM_TEXT}, from the supplier to the shop floor.", "claim_ids": [CLAIM]},
    ],
    "business_name": "Example SDS",
}


def _paraphrase(text: str) -> str:
    """A's copy said again in other words — the B 4.2.4 must refuse."""
    for old, new in (("Your", "The"), ("Every", "Each"), ("every", "each"), ("stays", "is kept")):
        text = text.replace(old, new)
    return text


#: B's pools as a paraphrase of A's: every line reworded, none rethought.
PARAPHRASED_POOL = [{**h, "text": _paraphrase(h["text"])} for h in POOL_A]
PARAPHRASED_DESCRIPTIONS = {
    "descriptions": [
        {**item, "text": _paraphrase(item["text"])} for item in DESCRIPTIONS_A["descriptions"]
    ],
    "paths": ["sds", "software"],
}


class _Script:
    """A scripted OpenRouter that answers each node by the schema it sends.

    `paraphrase` answers 4.2.4's B requests with A's copy reworded.
    """

    def __init__(self, *, paraphrase: bool = False) -> None:
        self.fake = FakeOpenRouter()
        self.requests: dict[str, list[dict[str, Any]]] = {}
        self.paraphrase = paraphrase
        self.fake.dispatch(self._respond)

    def _respond(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        schema_spec = body["response_format"]["json_schema"]
        name, schema = schema_spec["name"], schema_spec["schema"]
        self.requests.setdefault(name, []).append(body)
        if name == "CreativeBriefDraft":
            return completion(_brief(schema), model=body["model"])
        if name == "AssetGroupTextDraft":
            return completion(ASSET_GROUP, model=body["model"])
        if name in ("HeadlinePoolDraft", "DescriptionPoolDraft") and is_variant_b(body):
            self.requests.setdefault(f"{name}:B", []).append(body)
            if name == "HeadlinePoolDraft":
                pool = PARAPHRASED_POOL if self.paraphrase else POOL_B
                return completion({"candidates": pool}, model=body["model"])
            answer = PARAPHRASED_DESCRIPTIONS if self.paraphrase else DESCRIPTIONS_B
            return completion(answer, model=body["model"])
        if name == "HeadlinePoolDraft":
            return completion({"candidates": POOL_A}, model=body["model"])
        if name == "DescriptionPoolDraft":
            return completion(DESCRIPTIONS_A, model=body["model"])
        if name == "PairLabelsDraft":
            user = next(m["content"] for m in body["messages"] if m["role"] == "user")
            pairs = json.loads(user.split("PAIRS:\n", 1)[1])
            return completion({key: "reads_well" for key in pairs})
        if name == "CreativeCritiqueDraft":
            # 4.7.2's reader (S4-P16): it may only warn or note, and here it has nothing.
            return completion({"issues": []}, model=body["model"])
        raise AssertionError(f"no scripted answer for {name}")


def _registry() -> NodeRegistry:
    return get_registry().for_stage(RunStage.CREATIVE)


async def _seed(
    db: AsyncSession,
    ws: uuid.UUID,
    project_id: uuid.UUID,
    actor: uuid.UUID,
    *,
    pmax: bool = False,
    business_name_spec: bool = True,
    plan: dict[str, Any] | None = None,
    extra_specs: dict[str, dict[str, dict[str, Any]]] | None = None,
) -> None:
    """`extra_specs` (`campaign_type -> asset_type -> spec`) are added to the pin's sheet."""
    sheet = get_content_constants().asset_sheet()
    if extra_specs:
        raw = sheet.model_dump(mode="json")
        for campaign_type, specs in extra_specs.items():
            raw["specs"].setdefault(campaign_type, {}).update(specs)
        sheet = type(sheet).model_validate(raw)
    if pmax and business_name_spec:
        # The shipped sheet has no Performance Max business name; a pin that
        # carries one is what 4.2.5 needs before it can write that asset group.
        raw = sheet.model_dump(mode="json")
        raw["specs"]["performance_max"]["business_name"] = {
            "max_chars": 25,
            "max_count": 1,
            "source": "unverified",
            "reviewed_at": "2026-09-25",
        }
        sheet = type(sheet).model_validate(raw)
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
    await seed_plan(
        db,
        ws,
        project_id,
        actor,
        campaign_type="search",
        keywords=KEYWORDS,
        extra_campaigns=[PMAX] if pmax else [],
        channel_slate=WITH_PMAX if pmax else SEARCH_ONLY,
        **(plan or {}),
    )
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
    admin: ApiClient,
    db: AsyncSession,
    ws: uuid.UUID,
    project_id: uuid.UUID,
    actor: uuid.UUID,
    *,
    paraphrase: bool = False,
    pmax: bool = False,
    business_name_spec: bool = True,
) -> tuple[uuid.UUID, _Script, RunStatus]:
    """A creative run started, halted on G7, approved, and run to its end."""
    await _seed(db, ws, project_id, actor, pmax=pmax, business_name_spec=business_name_spec)
    started = await admin.post(
        f"/projects/{project_id}/creative/runs", json={"scope": TEXT_ONLY, "media_models": []}
    )
    assert started.status_code == 202, started.text
    run_id = uuid.UUID(started.json()["run_id"])
    script = _Script(paraphrase=paraphrase)
    registry = _registry()
    halted = await execute(run_id, script.fake, registry=registry, dag=Dag.from_registry(registry))
    assert halted.status is RunStatus.AWAITING_APPROVAL, halted.error
    approval = await _g7(db, run_id)
    decided = await admin.post(f"/approvals/{approval.id}", json={"decision": "approve"})
    assert decided.status_code == 200, decided.text
    result = await execute(
        run_id, script.fake, registry=registry, dag=Dag.from_registry(registry), max_attempts=1
    )
    result = await past_h3(
        admin,
        run_id,
        result,
        lambda: execute(
            run_id, script.fake, registry=registry, dag=Dag.from_registry(registry), max_attempts=1
        ),
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
    (request,) = [r for r in script.requests["DescriptionPoolDraft"] if not is_variant_b(r)]
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
    # One exception per distinct claim, not per trigger (S4-P14): the same "#1"
    # in two clauses is two claims a legal owner signs separately.
    assert group["exception_candidates"] == [
        {"span": UNLICENSED_1.rstrip("."), "occurrences": 1},
        {"span": UNLICENSED_2.rstrip("."), "occurrences": 1},
    ]
    assets = await _assets(db, run_id)
    assert not [a for a in assets.values() if a.text and "#1" in a.text]
    # A's rows: 4.2.4 writes B's beside them in the same run.
    a_rows = [a for a in assets.values() if a.node_id == "4.2.2"]
    descriptions = {a.text: a for a in a_rows if a.kind is CreativeAssetKind.DESCRIPTION}
    assert set(descriptions) == {*SELECTED_A, RESERVE_A, TOO_LONG}
    assert {descriptions[text].status for text in SELECTED_A} == {CreativeAssetStatus.LINTED}
    assert descriptions[RESERVE_A].status is CreativeAssetStatus.RESERVE
    too_long = descriptions[TOO_LONG]
    assert too_long.status is CreativeAssetStatus.DRAFT, "a lint failure never leaves draft"
    assert [f["rule_id"] for f in too_long.lint["findings"]] == ["asset_spec.length.v1"]  # type: ignore[index]
    assert all(a.node_id == "4.2.2" and a.variant.value == "A" for a in descriptions.values())  # type: ignore[union-attr]
    paths = {a.text: a.status for a in a_rows if a.kind is CreativeAssetKind.PATH}
    assert paths == {"sds": CreativeAssetStatus.LINTED, "software": CreativeAssetStatus.LINTED}

    # --- 4.2.5 is not_required on a search-only slate --------------------------
    assert await _output(db, run_id, "4.2.5") == {
        "schema_version": "1.0",
        "status": "not_required",
        "why": (
            "the channel slate has no Performance Max, Demand Gen or Display campaign, so "
            "there is no asset group to write text for"
        ),
        "asset_groups": [],
    }
    assert "AssetGroupTextDraft" not in script.requests, "no model asked"
    assert not [a for a in assets.values() if a.node_id == "4.2.5"]

    # --- 4.2.3 consumed the real 4.2.2 ----------------------------------------
    (ad,) = (await _output(db, run_id, "4.2.3"))["ads"]
    assert ad["descriptions"] == [d["asset_id"] for d in group["descriptions"]]


async def test_variant_b_is_a_second_message_with_a_hypothesis_and_a_primary_metric(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
) -> None:
    run_id, script, status = await _run(admin, db, workspace_id, project_id, admin_user.id)
    assert status is RunStatus.SUCCEEDED

    # --- B went through the 4.2.1–4.2.3 path, led by angle_b, shown A --------
    (b_pool,) = script.requests["HeadlinePoolDraft:B"]
    (b_descriptions,) = script.requests["DescriptionPoolDraft:B"]
    brief = await _output(db, run_id, "4.1.1")
    (group_brief,) = brief["ad_groups"]
    for request in (b_pool, b_descriptions):
        user = next(m["content"] for m in request["messages"] if m["role"] == "user")
        assert json.dumps(group_brief["angle_b"]["text"], ensure_ascii=False) in user
        assert "VARIANT A:" in user and SELECTED_A[0] in user

    (b,) = (await _output(db, run_id, "4.2.4"))["ad_groups"]
    (a,) = (await _output(db, run_id, "4.2.3"))["ads"]
    ad = b["ad_b"]
    assert ad["variant"] == "B" and ad["angle"] == group_brief["angle_b"]["text"]
    assert ad["pair_report"]["variant"] == "B"
    assert len(ad["headlines"]) == 15 and len(ad["descriptions"]) == 4
    assert ad["paths"] == DESCRIPTIONS_B["paths"]
    assert ad["final_url"] == group_brief["landing_url"]

    # --- distinct from A, by copy.distinctness_v1, at least the minimum ------
    assert b["distinctness_metric"] == "copy.distinctness_v1"
    assert b["variant_min_distance"] == 0.65
    assert b["distinctness_vs_a"] == ad["distinctness_vs_a"] >= 0.65

    # --- a hypothesis and a primary metric, both stated by code --------------
    assert b["hypothesis"] == ad["hypothesis"]
    assert group_brief["angle_b"]["text"] in b["hypothesis"]
    assert group_brief["primary_message"]["text"] in b["hypothesis"]
    assert b["primary_metric"] == group_brief["kpi"] == "cost per qualified lead"
    assert group_brief["kpi"] in b["hypothesis"]

    # --- B is B's own copy: rows of 4.2.4, variant B; A's untouched ----------
    assets = await _assets(db, run_id)
    b_rows = {a_.id: a_ for a_ in assets.values() if a_.node_id == "4.2.4"}
    carried = [uuid.UUID(x) for x in (*ad["headlines"], *ad["descriptions"])]
    assert set(carried) <= set(b_rows)
    assert {b_rows[x].status for x in carried} == {CreativeAssetStatus.LINTED}
    assert {row.variant.value for row in b_rows.values()} == {"B"}  # type: ignore[union-attr]
    a_carried = {uuid.UUID(x) for x in (*a["headlines"], *a["descriptions"])}
    assert not a_carried & set(b_rows)
    assert {assets[x].status for x in a_carried} == {CreativeAssetStatus.LINTED}
    assert {assets[x].variant.value for x in a_carried} == {"A"}  # type: ignore[union-attr]
    b_descriptions_rows = [
        row for row in b_rows.values() if row.kind is CreativeAssetKind.DESCRIPTION
    ]
    assert {row.text for row in b_descriptions_rows} == {
        item["text"] for item in DESCRIPTIONS_B["descriptions"]
    }
    assert [d["text"] for d in b["descriptions"]["descriptions"]] == SELECTED_B
    assert all(d["claim_ids"] == [CLAIM] for d in b["descriptions"]["descriptions"])
    assert b["descriptions"]["exception_candidates"] == []
    assert len(b["headlines"]["candidates"]) == len(POOL_B)


async def test_a_b_that_paraphrases_a_fails_validation(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
) -> None:
    run_id, script, status = await _run(
        admin, db, workspace_id, project_id, admin_user.id, paraphrase=True
    )
    assert status is RunStatus.FAILED
    node = (
        await db.execute(
            sa.select(NodeRun).where(NodeRun.run_id == run_id, NodeRun.node_id == "4.2.4")
        )
    ).scalar_one()
    assert node.status is NodeRunStatus.FAILED
    error = json.dumps(node.error)
    assert "B paraphrases A" in error and "copy.variant_min_distance 0.65" in error
    assert len(script.requests["HeadlinePoolDraft:B"]) == 1, "one attempt, bounded"

    # Nothing of the refused B was written; A stands as 4.2.3 left it.
    assets = await _assets(db, run_id)
    assert not [row for row in assets.values() if row.node_id == "4.2.4"]
    assert not [row for row in assets.values() if row.variant and row.variant.value == "B"]
    (a,) = (await _output(db, run_id, "4.2.3"))["ads"]
    assert {assets[uuid.UUID(x)].status for x in a["headlines"]} == {CreativeAssetStatus.LINTED}


async def test_a_performance_max_asset_group_gets_its_text_all_linted(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
) -> None:
    run_id, script, status = await _run(
        admin, db, workspace_id, project_id, admin_user.id, pmax=True
    )
    assert status is RunStatus.SUCCEEDED

    # --- the spec's counts, and only licensed claims, on the menu -------------
    (request,) = script.requests["AssetGroupTextDraft"]
    schema = request["response_format"]["json_schema"]["schema"]
    for field, count in (("headlines", 15), ("long_headlines", 5), ("descriptions", 5)):
        assert (
            schema["properties"][field]["minItems"]
            == schema["properties"][field]["maxItems"]
            == count
        )
    claim_ids = schema["$defs"]["AssetGroupDescriptionDraft"]["properties"]["claim_ids"]
    assert claim_ids["items"].get("enum", [claim_ids["items"].get("const")]) == [CLAIM]
    system = request["messages"][0]["content"]
    system = system if isinstance(system, str) else system[0]["text"]
    assert "Performance Max" in system and "at most 25 characters" in system

    output = await _output(db, run_id, "4.2.5")
    assert output["status"] == "required"
    (group,) = output["asset_groups"]
    assert (group["campaign_ref"], group["ad_group_ref"], group["campaign_type"]) == (
        "c-pmax-us",
        "sds asset group",
        "performance_max",
    )
    assert [line["text"] for line in group["headlines"]] == ASSET_GROUP["headlines"][:14]
    assert [line["text"] for line in group["long_headlines"]] == ASSET_GROUP["long_headlines"]
    assert group["business_name"]["text"] == "Example SDS"
    lines = [
        *group["headlines"],
        *group["long_headlines"],
        *group["descriptions"],
        group["business_name"],
    ]
    assert {line["lint"]["verdict"] for line in lines} == {"pass"}, "all linted, all passed"

    # --- a description is claim-bound here too; "#1" is withheld -------------
    assert len(group["descriptions"]) == 4
    assert all(line["claim_ids"] == [CLAIM] for line in group["descriptions"])
    assert group["exception_candidates"] == [
        {"span": PMAX_UNLICENSED.rstrip("."), "occurrences": 1}
    ]

    # --- the rows: linted as Performance Max text, the failure left draft ----
    assets = await _assets(db, run_id)
    rows = [a for a in assets.values() if a.node_id == "4.2.5"]
    assert not [a for a in rows if a.text and "#1" in a.text], "never an asset"
    surfaces = {(a.kind, a.surface) for a in rows}
    assert surfaces == {
        (CreativeAssetKind.HEADLINE, "pmax_headline"),
        (CreativeAssetKind.LONG_HEADLINE, "long_headline"),
        (CreativeAssetKind.DESCRIPTION, "pmax_description"),
        (CreativeAssetKind.BUSINESS_NAME, "business_name"),
    }
    by_text = {a.text: a for a in rows}
    assert by_text[PMAX_TOO_LONG].status is CreativeAssetStatus.DRAFT
    assert [f["rule_id"] for f in by_text[PMAX_TOO_LONG].lint["findings"]] == [  # type: ignore[index]
        "asset_spec.length.v1"
    ]
    assert {a.status for a in rows if a.text != PMAX_TOO_LONG} == {CreativeAssetStatus.LINTED}
    assert len(rows) == 15 + 5 + 4 + 1
    assert all(a.variant is None and a.campaign_ref == "c-pmax-us" for a in rows)

    # The Search ad group was written as before, beside it.
    (search,) = (await _output(db, run_id, "4.2.2"))["ad_groups"]
    assert search["campaign_ref"] == "c-sds-us"


async def test_a_pin_without_the_asset_groups_specs_fails_naming_them(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
) -> None:
    """The shipped sheet has no Performance Max business name: spec_missing, never a guess."""
    run_id, script, status = await _run(
        admin, db, workspace_id, project_id, admin_user.id, pmax=True, business_name_spec=False
    )
    assert status is RunStatus.FAILED
    node = (
        await db.execute(
            sa.select(NodeRun).where(NodeRun.run_id == run_id, NodeRun.node_id == "4.2.5")
        )
    ).scalar_one()
    assert node.status is NodeRunStatus.FAILED
    error = json.dumps(node.error)
    assert "spec_missing" in error and "asset_specs.performance_max.business_name" in error
    assert "AssetGroupTextDraft" not in script.requests, "no model is asked to guess"
    assert not [a for a in (await _assets(db, run_id)).values() if a.node_id == "4.2.5"]
