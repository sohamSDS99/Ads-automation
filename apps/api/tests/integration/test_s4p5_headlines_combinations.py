"""S4-P5 exit criteria — 4.2.1 `headline_spread` and 4.2.3 `combination_coherence`, end to end.

A creative run is started through the API, halts on G7, is approved, and runs
on through the real 4.2.1 (a scripted COPYWRITE pool), 4.2.2 and the real 4.2.3
(scripted CLASSIFY labels), then the stubs to the end.

**4.2.2 is a fixture.** The PRD orders 4.2.2 before 4.2.3 but builds it in
S4-P6, so `_FixtureDescriptions` stands in: it writes and lints descriptions
the way 4.2.2 will, and returns them through 4.2.2's real output schema,
`ClaimBoundDescriptionsOutput`, validated against the pin's licensed claims.

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
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from agent.creative import metrics
from agent.db.models import (
    CreativeAsset,
    CreativeAssetKind,
    CreativeAssetStatus,
    CreativeAssetVariant,
    Evidence,
    NodeRun,
    NodeRunStatus,
    RunStage,
    RunStatus,
)
from agent.export.guideline_contract import AssetSpecs
from agent.guidelines.constants import get_content_constants
from agent.guidelines.synthesis import _asset_rules
from agent.nodes.base import RunContext
from agent.nodes.creative._text_assets import text_asset
from agent.orchestrator.dag import Dag
from agent.orchestrator.registry import NodeRegistry, get_registry
from agent.schemas.creative_brief import CreativeBrief
from agent.schemas.guardrails import LintTarget
from agent.schemas.search_ads import PASSING, ClaimBoundDescriptionsOutput
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

#: The one pair CLASSIFY calls order_dependent, and the one it calls redundant
#: — a pair only the swap creates, so only the verification pass ever sees it.
ORDER_DEPENDENT = frozenset({"SDS Software For Your Team", "SDS Management Software"})
REDUNDANT_AFTER_SWAP = frozenset({"SDS Updates Within 24 Hours", RESERVE_DESCRIPTION})


class _Script:
    """A scripted OpenRouter that answers each node by the schema it sends."""

    def __init__(self) -> None:
        self.fake = FakeOpenRouter()
        self.pools: list[dict[str, Any]] = []
        self.label_batches: list[dict[str, dict[str, str]]] = []
        self.fake.dispatch(self._respond)

    def _respond(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        schema_spec = body["response_format"]["json_schema"]
        name, schema = schema_spec["name"], schema_spec["schema"]
        if name == "CreativeBriefDraft":
            return completion(_instance(schema, schema), model=body["model"])
        if name == "HeadlinePoolDraft":
            self.pools.append(body)
            return completion({"candidates": POOL}, model=body["model"])
        if name == "PairLabelsDraft":
            user = next(m["content"] for m in body["messages"] if m["role"] == "user")
            pairs = json.loads(user.split("PAIRS:\n", 1)[1])
            self.label_batches.append(pairs)
            return completion({key: self._label(pair) for key, pair in pairs.items()})
        raise AssertionError(f"no scripted answer for {name}")

    @staticmethod
    def _label(pair: dict[str, str]) -> str:
        texts = frozenset((pair["a"], pair["b"]))
        if texts == ORDER_DEPENDENT:
            return "order_dependent"
        if texts == REDUNDANT_AFTER_SWAP:
            return "redundant"
        return "reads_well"


class _FixtureDescriptions:
    """4.2.2 until S4-P6 builds it: descriptions written and linted as 4.2.2 will,
    returned through 4.2.2's real output schema."""

    def __init__(self) -> None:
        self.spec = (
            get_registry()
            .spec("4.2.2")
            .model_copy(update={"output_model": ClaimBoundDescriptionsOutput})
        )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        return []

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        creative = ctx.require_creative()
        brief = CreativeBrief.model_validate(ctx.output_of("4.1.1"))
        groups = []
        for group in brief.ad_groups:
            written = []
            for index, text in enumerate([*DESCRIPTIONS, RESERVE_DESCRIPTION]):
                asset_id = uuid.uuid4()
                result = creative.linter.lint_candidate(
                    LintTarget(
                        ref=str(asset_id),
                        surface="rsa_description",
                        campaign_type="search",
                        market="*",
                        language="en",
                        text=text,
                        generated_by_ai=True,
                    ),
                    now=ctx.run.started_at,  # type: ignore[arg-type]
                )
                assert result.verdict in PASSING, result.findings
                reserve = index == len(DESCRIPTIONS)
                ctx.db.add(
                    text_asset(
                        ctx,
                        asset_id=asset_id,
                        node_id="4.2.2",
                        campaign_ref=group.campaign_ref,
                        ad_group_ref=group.ad_group_ref,
                        kind=CreativeAssetKind.DESCRIPTION,
                        surface="rsa_description",
                        variant=CreativeAssetVariant.A,
                        category=None,
                        text=text,
                        fields={},
                        claim_ids=[LICENSED_CLAIM_ID],
                        status=CreativeAssetStatus.RESERVE
                        if reserve
                        else CreativeAssetStatus.LINTED,
                        lint=result,
                    )
                )
                start = text.index(CLAIM_TEXT)
                written.append(
                    {
                        "asset_id": str(asset_id),
                        "text": text,
                        "claim_ids": [CLAIM],
                        "claim_span": [start, start + len(CLAIM_TEXT)],
                        "lint": {
                            "verdict": result.verdict,
                            "ruleset_version": result.ruleset_version,
                            "rule_ids": [],
                        },
                    }
                )
            groups.append(
                {
                    "campaign_ref": group.campaign_ref,
                    "ad_group_ref": group.ad_group_ref,
                    "descriptions": written[: len(DESCRIPTIONS)],
                    "paths": ["sds", "software"],
                    "reserve": written[len(DESCRIPTIONS) :],
                    "exception_candidates": [],
                }
            )
        await ctx.db.flush()
        return ClaimBoundDescriptionsOutput.model_validate(
            {"ad_groups": groups}, context={"licensed_claim_ids": frozenset({LICENSED_CLAIM_ID})}
        )


def _registry(*, fixture_descriptions: bool = True) -> NodeRegistry:
    creative = get_registry().for_stage(RunStage.CREATIVE)
    return NodeRegistry.of(
        [
            _FixtureDescriptions()
            if node_id == "4.2.2" and fixture_descriptions
            else creative.node(node_id)
            for node_id in creative.ids
        ]
    )


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

    # --- every RSA has 15 headlines meeting quotas -------------------------
    assert len(selected) == 15
    assert group["quota_report"]["met"] is True
    quotas = {"keyword": 3, "benefit": 3, "offer": 2, "proof": 2, "objection": 2, "cta": 2}
    categories = [by_id[asset_id]["category"] for asset_id in selected]
    assert all(categories.count(c) >= n for c, n in quotas.items()), categories
    headline_rows = [a for a in assets.values() if a.kind is CreativeAssetKind.HEADLINE]
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


async def test_combination_coherence_refuses_a_stub_4_2_2(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
) -> None:
    """Until S4-P6 builds 4.2.2, 4.2.3 fails loudly rather than judge headlines alone."""
    run_id, script = await _through_g7(admin, db, workspace_id, project_id, admin_user.id)
    registry = _registry(fixture_descriptions=False)
    result = await execute(
        run_id, script.fake, registry=registry, dag=Dag.from_registry(registry), max_attempts=1
    )
    assert result.status is RunStatus.FAILED
    node = (
        await db.execute(
            sa.select(NodeRun).where(NodeRun.run_id == run_id, NodeRun.node_id == "4.2.3")
        )
    ).scalar_one()
    assert node.status is NodeRunStatus.FAILED
    assert "still a stub" in json.dumps(node.error)
    assert script.label_batches == [], "no CLASSIFY token is spent on an ad with no descriptions"
    assert (await _output(db, run_id, "4.2.1"))["ad_groups"], "4.2.1 itself succeeded"
