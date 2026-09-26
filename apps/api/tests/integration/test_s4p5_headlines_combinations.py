"""S4-P5 exit criteria — 4.2.1 `headline_spread` and 4.2.3 `combination_coherence`, end to end.

A creative run is started through the API, halts on G7, is approved, and runs
on through the real 4.2.1 (a scripted COPYWRITE pool), the real 4.2.2 (a
scripted pool of descriptions) and the real 4.2.3 (scripted CLASSIFY labels),
then the rest of the DAG.

**4.2.3 consumes the real 4.2.2.** Until S4-P6 a fixture stood in for it. Now
the descriptions 4.2.3 pairs and repairs from are the ones 4.2.2 wrote, linted
and selected, read through `ClaimBoundDescriptionsOutput` against the pin's
licensed claims.

**The pin carries the spec-sheet rules synthesis really emits** (headline 30,
description 90, path 15 characters, and the asset counts), so every length
here is enforced by the Stage 03 linter, the way a published ruleset enforces
it — not by a hand-built rule that happens to agree.

Asserted from the database and the stored node outputs, never from the run's
event stream: `/runs/{id}/events` never closes on a paused run.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.creative import metrics
from agent.db.models import (
    CreativeAsset,
    CreativeAssetKind,
    CreativeAssetStatus,
    NodeRun,
    NodeRunStatus,
    RunStage,
    RunStatus,
)
from agent.export.guideline_contract import AssetSpecs
from agent.guidelines.constants import get_content_constants
from agent.guidelines.synthesis import _asset_rules
from agent.orchestrator.dag import Dag
from agent.orchestrator.registry import NodeRegistry, get_registry
from tests.integration.conftest import ApiClient
from tests.integration.creative_support import (
    LICENSED_CLAIM_ID,
    TEXT_ONLY,
    seed_plan,
    seed_published,
    seed_signoff,
)
from tests.integration.runs_support import execute
from tests.integration.test_s4p4_brief_g7 import _g7, _instance
from tests.integration.variant_b_support import (
    DESCRIPTIONS_B,
    POOL_B,
    TEXTS_B,
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


def _h(
    text: str,
    category: str,
    *,
    keyword_ref: str | None = None,
    claims: bool = False,
    dki: bool = False,
) -> dict[str, Any]:
    return {
        "text": text,
        "category": category,
        "keyword_ref": keyword_ref,
        "claim_ids": [CLAIM] if claims else [],
        "dki": dki,
    }


#: A DKI headline 36 characters as written and 26 on its default text.
DKI_FITS = "{KeyWord:Safety Data Sheet Software}"
#: A DKI headline whose default text is 35 characters (45 as written): over the limit.
DKI_TOO_LONG = "{KeyWord:Online SDS Management Software Tool}"
DKI_TOO_LONG_DEFAULT = "Online SDS Management Software Tool"
#: The planted near-duplicate pair.
NEAR, TWIN = "Find Any SDS In Seconds", "Find Any SDS In A Second"
#: The planted offer: 20% here, 30% in a description.
OFFER_20 = "Save 20% On Annual Plans"

#: Twenty-five headlines, as COPYWRITE answers 4.2.1's schema.
POOL: list[dict[str, Any]] = [
    _h("SDS Software For Your Team", "keyword", keyword_ref="sds software"),
    _h("SDS Management Software", "keyword", keyword_ref="sds management software"),
    _h(DKI_FITS, "keyword", keyword_ref="safety data sheet software", dki=True),
    _h(DKI_TOO_LONG, "keyword", keyword_ref="sds management software", dki=True),
    _h(NEAR, "benefit"),
    _h(TWIN, "benefit"),
    _h("Every Sheet Current, Always", "benefit"),
    _h("Audit-Ready Chemical Records", "benefit"),
    _h("One Library For Every Site", "benefit"),
    _h("Less Paperwork Every Week", "benefit"),
    _h("Keep Every Safety Data Sheet Current", "benefit"),  # 36: fails lint
    _h(OFFER_20, "offer"),
    _h("Free Trial For Your Team", "offer"),
    _h("SDS Updates Within 24 Hours", "proof", claims=True),
    _h("Sheets Refreshed In 24 Hours", "proof", claims=True),
    _h("Rated Best By EHS Teams", "proof"),  # a proof with no claim: dropped
    _h("Trusted By EHS Managers", "proof"),  # a proof with no claim: dropped
    _h("No IT Team Needed", "objection"),
    _h("Set Up In One Afternoon", "objection"),
    _h("Works With Your Files", "objection"),
    _h("Cancel Any Time", "objection"),
    _h("Book Your Demo Today", "cta"),
    _h("Book A Walkthrough Now", "cta"),
    _h("Book Time With Our Team", "cta"),
    _h("Book A Call With Us", "cta"),
]
assert len(POOL) == 25

CLAIM_TEXT = "SDS updates within 24 hours"
#: (text, the claim span) — four descriptions 4.2.2 would write, then one reserve.
OFFER_30 = f"{CLAIM_TEXT}. Now 30% off every annual plan."
DESCRIPTIONS = [
    OFFER_30,
    f"Every sheet on file stays current: {CLAIM_TEXT}.",
    f"One searchable library for every site, with {CLAIM_TEXT}.",
    f"Audit-ready records without the paperwork. {CLAIM_TEXT}.",
]
RESERVE_DESCRIPTION = f"{CLAIM_TEXT} keep your chemical inventory audit-ready."
#: 107 characters: fails `asset_spec.length.v1`, stays draft, and is never a reserve.
TOO_LONG_DESCRIPTION = (
    f"{CLAIM_TEXT} for every safety data sheet in every one of your warehouses, labs and offices."
)
#: Cites the claim but does not contain the words it quotes: dropped.
MISQUOTED = "Every sheet stays current on every site you run."
#: A second reserve that repeats the 30% offer. It conflicts with the 20%
#: headline too, so the repair round passes over it for the one that offers nothing.
OFFER_30_AGAIN = f"{CLAIM_TEXT}, and 30% off every annual plan."


def _d(text: str) -> dict[str, Any]:
    return {"text": text, "claim_ids": [CLAIM], "claim_text": CLAIM_TEXT}


#: Eight descriptions and two paths, as COPYWRITE answers 4.2.2's schema.
DESCRIPTION_POOL: dict[str, Any] = {
    "descriptions": [
        *(_d(text) for text in DESCRIPTIONS),
        _d(RESERVE_DESCRIPTION),
        _d(TOO_LONG_DESCRIPTION),
        _d(MISQUOTED),
        _d(OFFER_30_AGAIN),
    ],
    "paths": ["sds", "software"],
}
assert len(DESCRIPTION_POOL["descriptions"]) == 8

#: The one pair CLASSIFY calls order_dependent, and the one it calls redundant
#: — a pair only the swap creates, so only the verification pass ever sees it.
ORDER_DEPENDENT = frozenset({"SDS Software For Your Team", "SDS Management Software"})
REDUNDANT_AFTER_SWAP = frozenset({"SDS Updates Within 24 Hours", RESERVE_DESCRIPTION})


class _Script:
    """A scripted OpenRouter that answers each node by the schema it sends.

    4.2.4 writes variant B through the same schemas; it is answered from
    `variant_b_support` and kept out of what this test counts, which is A's.
    """

    def __init__(self) -> None:
        self.fake = FakeOpenRouter()
        self.pools: list[dict[str, Any]] = []
        self.description_pools: list[dict[str, Any]] = []
        self.label_batches: list[dict[str, dict[str, str]]] = []
        self.variant_b: list[str] = []
        self.fake.dispatch(self._respond)

    def _respond(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        schema_spec = body["response_format"]["json_schema"]
        name, schema = schema_spec["name"], schema_spec["schema"]
        if name == "CreativeBriefDraft":
            return completion(_instance(schema, schema), model=body["model"])
        if name in ("HeadlinePoolDraft", "DescriptionPoolDraft") and is_variant_b(body):
            self.variant_b.append(name)
            answer = {"candidates": POOL_B} if name == "HeadlinePoolDraft" else DESCRIPTIONS_B
            return completion(answer, model=body["model"])
        if name == "HeadlinePoolDraft":
            self.pools.append(body)
            return completion({"candidates": POOL}, model=body["model"])
        if name == "DescriptionPoolDraft":
            self.description_pools.append(body)
            return completion(DESCRIPTION_POOL, model=body["model"])
        if name == "PairLabelsDraft":
            user = next(m["content"] for m in body["messages"] if m["role"] == "user")
            pairs = json.loads(user.split("PAIRS:\n", 1)[1])
            if any({pair["a"], pair["b"]} & TEXTS_B for pair in pairs.values()):
                self.variant_b.append(name)
                return completion({key: "reads_well" for key in pairs})
            self.label_batches.append(pairs)
            return completion({key: self._label(pair) for key, pair in pairs.items()})
        if name == "CreativeCritiqueDraft":
            # 4.7.2's reader (S4-P16): it may only warn or note, and here it has nothing.
            return completion({"issues": []}, model=body["model"])
        raise AssertionError(f"no scripted answer for {name}")

    @staticmethod
    def _label(pair: dict[str, str]) -> str:
        texts = frozenset((pair["a"], pair["b"]))
        if texts == ORDER_DEPENDENT:
            return "order_dependent"
        if texts == REDUNDANT_AFTER_SWAP:
            return "redundant"
        return "reads_well"


def _registry() -> NodeRegistry:
    return get_registry().for_stage(RunStage.CREATIVE)


async def _seed(db: AsyncSession, ws: uuid.UUID, project_id: uuid.UUID, actor: uuid.UUID) -> None:
    sheet = get_content_constants().asset_sheet()
    rules = tuple(_asset_rules(AssetSpecs(sheet=sheet, scope="unscoped"), lambda _why: None))
    await seed_plan(db, ws, project_id, actor, campaign_type="search", keywords=KEYWORDS)
    await seed_published(
        db,
        ws,
        project_id,
        actor,
        asset_specs=sheet.model_dump(mode="json")["specs"],
        extra_rules=rules,
    )
    await seed_signoff(db, ws, project_id, actor)
    await db.commit()


async def _through_g7(
    admin: ApiClient, db: AsyncSession, ws: uuid.UUID, project_id: uuid.UUID, actor: uuid.UUID
) -> tuple[uuid.UUID, _Script]:
    """A started creative run, halted on G7 and approved."""
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
    return run_id, script


async def _output(db: AsyncSession, run_id: uuid.UUID, node_id: str) -> dict[str, Any]:
    row = (
        await db.execute(
            sa.select(NodeRun)
            .where(
                NodeRun.run_id == run_id,
                NodeRun.node_id == node_id,
                NodeRun.status == NodeRunStatus.SUCCEEDED,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return dict(row.output or {})


async def _assets(db: AsyncSession, run_id: uuid.UUID) -> dict[uuid.UUID, CreativeAsset]:
    rows = (
        (
            await db.execute(
                sa.select(CreativeAsset)
                .where(CreativeAsset.creative_run_id == run_id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    return {row.id: row for row in rows}


async def test_headlines_are_spread_selected_paired_repaired_and_pinned(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
) -> None:
    run_id, script = await _through_g7(admin, db, workspace_id, project_id, admin_user.id)

    # What a failed earlier attempt of 4.2.1 left behind — committed with its
    # failure, as the executor does — must not survive the attempt that works.
    stale = CreativeAsset(
        workspace_id=workspace_id,
        project_id=project_id,
        creative_run_id=run_id,
        node_id="4.2.1",
        campaign_ref="c-sds-us",
        ad_group_ref="sds software",
        kind=CreativeAssetKind.HEADLINE,
        surface="rsa_headline",
        text="Left by a failed attempt",
        generated_by_ai=True,
        status=CreativeAssetStatus.DRAFT,
        content_hash="0" * 64,
    )
    db.add(stale)
    await db.commit()
    stale_id = stale.id

    registry = _registry()
    result = await execute(
        run_id, script.fake, registry=registry, dag=Dag.from_registry(registry), max_attempts=1
    )
    assert result.status is RunStatus.SUCCEEDED, result.error

    spread = await _output(db, run_id, "4.2.1")
    coherence = await _output(db, run_id, "4.2.3")
    assets = await _assets(db, run_id)
    assert stale_id not in assets

    (group,) = spread["ad_groups"]
    by_text = {c["text"]: c for c in group["candidates"]}
    by_id = {uuid.UUID(c["asset_id"]): c for c in group["candidates"]}
    selected = [uuid.UUID(ref) for ref in group["selected"]]
    assert len(group["candidates"]) == 25 and len(script.pools) == 1
    assert script.variant_b[:2] == ["HeadlinePoolDraft", "DescriptionPoolDraft"], "B ran too"

    # --- every RSA has 15 headlines meeting quotas -------------------------
    assert len(selected) == 15
    assert group["quota_report"]["met"] is True
    quotas = {"keyword": 3, "benefit": 3, "offer": 2, "proof": 2, "objection": 2, "cta": 2}
    categories = [by_id[asset_id]["category"] for asset_id in selected]
    assert all(categories.count(c) >= n for c, n in quotas.items()), categories
    headline_rows = [  # A's: 4.2.4 writes B's headlines beside them
        a for a in assets.values() if a.kind is CreativeAssetKind.HEADLINE and a.node_id == "4.2.1"
    ]
    assert sorted(a.id for a in headline_rows if a.status is CreativeAssetStatus.LINTED) == sorted(
        selected
    )

    # --- a planted near-duplicate is never selected --------------------------
    chosen_texts = {by_id[asset_id]["default_text"] for asset_id in selected}
    assert metrics.similarity(NEAR, TWIN) >= 0.80
    assert len({NEAR, TWIN} & chosen_texts) == 1
    loser = by_text[TWIN] if NEAR in chosen_texts else by_text[NEAR]
    assert loser["outcome"] == "reserve" and loser["reason"].startswith("near_duplicate of")

    # --- DKI is validated on the default text --------------------------------
    fits, too_long = by_text[DKI_FITS], by_text[DKI_TOO_LONG]
    assert len(DKI_FITS) > 30 and len(fits["default_text"]) <= 30
    assert fits["dki"] is True and fits["lint"]["verdict"] == "pass"
    assert uuid.UUID(fits["asset_id"]) in selected
    assert too_long["outcome"] == "failed_lint"
    assert too_long["lint"]["rule_ids"] == ["asset_spec.length.v1"]
    too_long_row = assets[uuid.UUID(too_long["asset_id"])]
    assert too_long_row.status is CreativeAssetStatus.DRAFT, "a lint failure never leaves draft"
    (finding,) = too_long_row.lint["findings"]  # type: ignore[index]
    # Measured on the default text (35), not as written (45).
    assert len(DKI_TOO_LONG_DEFAULT) == 35 and len(DKI_TOO_LONG) == 45
    assert "This is 35 chars; the limit is 30." in finding["message"]
    assert too_long_row.text == DKI_TOO_LONG, "the row keeps the insertion as written"

    # Every other outcome, as the pool planted them.
    assert by_text["Keep Every Safety Data Sheet Current"]["outcome"] == "failed_lint"
    assert by_text["Rated Best By EHS Teams"]["outcome"] == "dropped"
    assert "licensed claim" in by_text["Rated Best By EHS Teams"]["reason"]
    assert assets[uuid.UUID(by_text["Rated Best By EHS Teams"]["asset_id"])].status is (
        CreativeAssetStatus.DROPPED
    )

    # --- 4.2.3 pairs what the real 4.2.2 wrote, linted and selected ---------
    assert len(script.description_pools) == 1
    (written,) = (await _output(db, run_id, "4.2.2"))["ad_groups"]
    assert [d["text"] for d in written["descriptions"]] == DESCRIPTIONS
    assert [d["text"] for d in written["reserve"]] == [RESERVE_DESCRIPTION, OFFER_30_AGAIN]
    rows = {a.text: a for a in assets.values() if a.kind is CreativeAssetKind.DESCRIPTION}
    assert rows[TOO_LONG_DESCRIPTION].status is CreativeAssetStatus.DRAFT
    assert rows[MISQUOTED].status is CreativeAssetStatus.DROPPED
    assert rows[OFFER_30_AGAIN].status is CreativeAssetStatus.RESERVE

    # --- a planted offer conflict is flagged and swapped from reserve --------
    (ad,) = coherence["ads"]
    descriptions = {a.text: a for a in assets.values() if a.kind is CreativeAssetKind.DESCRIPTION}
    offer_30, reserve = descriptions[OFFER_30], descriptions[RESERVE_DESCRIPTION]
    offer_20 = uuid.UUID(by_text[OFFER_20]["asset_id"])
    assert offer_20 in selected
    (swap,) = ad["swaps"]
    assert uuid.UUID(swap["out"]) == offer_30.id
    assert uuid.UUID(swap["in_from_reserve"]) == reserve.id
    assert swap["why"] == f"offer_conflict with {offer_20}"
    assert offer_30.status is CreativeAssetStatus.RESERVE
    assert reserve.status is CreativeAssetStatus.LINTED
    assert reserve.lineage == {
        "origin": "reserve_swap",
        "parent_id": str(offer_30.id),
        "node_id": "4.2.3",
    }
    assert str(offer_30.id) not in ad["descriptions"] and str(reserve.id) in ad["descriptions"]
    assert not any("offer_conflict" in pair["flags"] for pair in ad["pairs"])

    # --- every pair, labelled in batches of 50, one repair round -------------
    assert len(ad["headlines"]) == 15 and len(ad["descriptions"]) == 4
    assert len(ad["pairs"]) == 105 + 60 + 6
    sizes = [len(batch) for batch in script.label_batches]
    assert sizes == [50, 50, 50, 21, 18], "171 pairs in fours, then the 18 the swap formed"
    assert ad["repair_rounds"] == 1
    # The swap formed a pair CLASSIFY calls redundant. One round means it is
    # reported, not repaired again.
    redundant = [(pair["a"], pair["b"]) for pair in ad["pairs"] if pair["label"] == "redundant"]
    assert redundant == [(by_text["SDS Updates Within 24 Hours"]["asset_id"], str(reserve.id))]
    assert ad["unresolved"] == [list(item) for item in redundant]

    # --- pins only for order_dependent pairs ---------------------------------
    (order_pair,) = [p for p in ad["pairs"] if p["label"] == "order_dependent"]
    assert {p["asset_id"] for p in ad["pins"]} == {order_pair["a"], order_pair["b"]}
    assert {p["asset_id"]: p["position"] for p in ad["pins"]} == {
        order_pair["a"]: "H1",
        order_pair["b"]: "H2",
    }
    pinned = {str(a.id): a.pin_position for a in assets.values() if a.pin_position is not None}
    assert pinned == {order_pair["a"]: "H1", order_pair["b"]: "H2"}
