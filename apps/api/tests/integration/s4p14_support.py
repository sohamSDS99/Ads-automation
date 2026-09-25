"""A creative run parked at H3, seeded row by row (S4-P14).

What 4.6.2 and 4.6.3 leave behind when the executor parks a run on the legal
owner: the exceptions, the assets they tie up and their precomputed fallbacks,
4.6.3's node run at `awaiting_human_task` with its output, the H3 task, and the
run itself at `awaiting_human_task`. Seeded directly so the route tests do not
depend on the nodes — `test_s4p14_nodes.py` runs the real ones.

The legal owner is an `approver` who is *not* the admin, and a second approver
who is not the owner exists: Law 23 narrows CLAIM_SIGN past the role to one
identity, and a suite with one approver could not tell the two checks apart.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.creative.clearance import ExceptionView, register_hash, view
from agent.db.models import (
    ClaimRecord,
    ClaimSignature,
    ContentGuideline,
    CreativeAsset,
    CreativeAssetKind,
    CreativeAssetStatus,
    CreativeException,
    CreativeExceptionKind,
    HumanTask,
    HumanTaskBlocking,
    HumanTaskStatus,
    NodeRun,
    NodeRunStatus,
    PolicyAmendment,
    RuleSet,
    Run,
    RunStatus,
    SignOffMatrix,
)
from agent.guardrails.matchers.claims import claim_licence
from agent.guidelines.constants import load_content_constants
from agent.schemas.creative_qa import LegalExceptionClearance
from agent.schemas.guardrails import Rule
from tests.creative.helpers import LEGAL
from tests.integration.conftest import ApiClient, build_client, make_member
from tests.integration.creative_support import TEXT_ONLY, seed_plan, seed_published

STATEMENT = "I have reviewed each exception and I am authorised to decide it."
#: What 4.6.2 raises: the clause the trigger sits in (`exceptions.unlicensed_spans`).
HEADLINE = "The #1 SDS platform for teams"
SPAN = HEADLINE


@dataclass
class Cast:
    legal: ApiClient
    legal_id: uuid.UUID
    other_approver: ApiClient
    operator: ApiClient
    viewer: ApiClient
    legal_password: str


@dataclass
class H3Run:
    run_id: uuid.UUID
    guideline_id: uuid.UUID
    task_id: uuid.UUID
    claim_id: uuid.UUID
    image_right_id: uuid.UUID
    disclaimer_id: uuid.UUID
    #: The carried description the claim ties up, and the reserve that replaces it.
    tied_id: uuid.UUID
    reserve_id: uuid.UUID
    #: The approved image the image right ties up. No reserve: it drops.
    image_id: uuid.UUID
    set_hash: str


def claim_licence_rule() -> Rule:
    """Stage 03's real claim-licence rule over the shipped English detectors —
    in the start pin *and* in the payload a MINOR recompiles, so the repin is
    what licenses the cleared claim, and a vacuous "no findings" cannot pass."""
    constants = load_content_constants()
    return claim_licence(
        tuple(d.detector_id for d in constants.detectors() if d.locale == "en"),
        authority=LEGAL,
        match_threshold=float(constants.value("claims.match_threshold")),
    )


async def member(admin: ApiClient, role: str, email: str) -> tuple[ApiClient, str]:
    address, password = await make_member(admin, role, email=email)
    api = build_client()
    response = await api.login(address, password)
    assert response.status_code == 200, response.text
    return api, password


async def cast(admin: ApiClient, db: AsyncSession) -> Cast:
    legal, legal_password = await member(admin, "approver", "legal@example.com")
    other, _ = await member(admin, "approver", "other@example.com")
    operator, _ = await member(admin, "operator", "ops@example.com")
    viewer, _ = await member(admin, "viewer", "viewer@example.com")
    legal_id = (
        await db.execute(sa.text("SELECT id FROM \"user\" WHERE email = 'legal@example.com'"))
    ).scalar_one()
    return Cast(
        legal=legal,
        legal_id=legal_id,
        other_approver=other,
        operator=operator,
        viewer=viewer,
        legal_password=legal_password,
    )


async def reauth(api: ApiClient, password: str) -> str:
    response = await api.post("/auth/reauth", json={"password": password})
    assert response.status_code == 200, response.text
    return str(response.json()["token"])


def _asset(run: Run, **extra: Any) -> CreativeAsset:
    fields: dict[str, Any] = {
        "workspace_id": run.workspace_id,
        "project_id": run.project_id,
        "creative_run_id": run.id,
        "node_id": "4.2.2",
        "campaign_ref": "Search - SDS",
        "ad_group_ref": "SDS software",
        "kind": CreativeAssetKind.DESCRIPTION,
        "surface": "rsa_description",
        "variant": "A",
        "text": "Manage every safety data sheet in one place.",
        "generated_by_ai": True,
        "status": CreativeAssetStatus.LINTED,
        "lint": {"verdict": "pass", "findings": []},
        "ruleset_version": (run.pins or [{}])[-1].get("ruleset_version"),
        "lineage": {"origin": "generated", "node_id": "4.2.2"},
        "content_hash": uuid.uuid4().hex,
    }
    fields.update(extra)
    return CreativeAsset(**fields)


async def h3_run(
    admin: ApiClient,
    db: AsyncSession,
    ws: uuid.UUID,
    project_id: uuid.UUID,
    actor: uuid.UUID,
    legal_id: uuid.UUID,
) -> H3Run:
    """A started text-only run parked at H3 with one exception of each kind."""
    await seed_plan(db, ws, project_id, actor)
    licence = claim_licence_rule()
    guideline, _ = await seed_published(
        db,
        ws,
        project_id,
        actor,
        extra_rules=(licence,),
        detectors=tuple(load_content_constants().detectors()),
        payload_rules=(licence,),
    )
    db.add(
        SignOffMatrix(
            workspace_id=ws,
            project_id=project_id,
            brand_owner_id=actor,
            legal_owner_id=legal_id,
            performance_owner_id=actor,
            set_by=actor,
        )
    )
    await db.commit()
    started = await admin.post(
        f"/projects/{project_id}/creative/runs", json={"scope": TEXT_ONLY, "media_models": []}
    )
    assert started.status_code == 202, started.text
    run_id = uuid.UUID(started.json()["run_id"])
    run = (await db.execute(sa.select(Run).where(Run.id == run_id))).scalar_one()

    tied = _asset(run, text="The #1 SDS platform, in one place.")
    reserve = _asset(run, status=CreativeAssetStatus.RESERVE)
    image = _asset(
        run,
        node_id="4.4.2",
        ad_group_ref=None,
        variant=None,
        kind=CreativeAssetKind.IMAGE,
        surface="search_image",
        text=None,
        status=CreativeAssetStatus.APPROVED,
    )
    db.add_all([tied, reserve, image])
    await db.flush()

    claim = CreativeException(
        workspace_id=ws,
        project_id=project_id,
        creative_run_id=run_id,
        kind=CreativeExceptionKind.NEW_CLAIM,
        subject_text=SPAN,
        asset_ids=[tied.id],
        occurrences=3,
        proposed={
            "claim_type": "superlative",
            "surface_forms": [SPAN],
            "substantiation": None,
            "market_scope": ["US"],
            "languages": ["en"],
        },
        evidence_ids=[],
        fallback_asset_ids=[reserve.id],
    )
    image_right = CreativeException(
        workspace_id=ws,
        project_id=project_id,
        creative_run_id=run_id,
        kind=CreativeExceptionKind.IMAGE_RIGHT,
        subject_text="recognisable person",
        asset_ids=[image.id],
        occurrences=1,
        proposed={"basis": "VISION flag on the master", "flag": "recognisable person"},
        evidence_ids=[],
        fallback_asset_ids=[],
    )
    disclaimer = CreativeException(
        workspace_id=ws,
        project_id=project_id,
        creative_run_id=run_id,
        kind=CreativeExceptionKind.DISCLAIMER,
        subject_text="Made with AI",
        asset_ids=[],
        occurrences=1,
        proposed={"text": "Made with AI", "placement": "end"},
        evidence_ids=[],
        fallback_asset_ids=[],
    )
    db.add_all([claim, image_right, disclaimer])
    await db.flush()
    views: list[ExceptionView] = [view(row) for row in (claim, image_right, disclaimer)]
    digest = register_hash(views)

    output = LegalExceptionClearance(
        status="required",
        assignee_id=legal_id,
        title="Clear 3 legal exceptions",
        instructions="Clear or reject each exception.",
        exception_ids=[claim.id, image_right.id, disclaimer.id],
        set_hash=digest,
        why="3 exceptions the pinned ruleset cannot license.",
    )
    db.add(
        NodeRun(
            run_id=run_id,
            node_id="4.6.3",
            status=NodeRunStatus.AWAITING_HUMAN_TASK,
            output=output.model_dump(mode="json"),
        )
    )
    task = HumanTask(
        workspace_id=ws,
        project_id=project_id,
        guideline_run_id=run_id,
        node_id="4.6.3",
        task_key="H3",
        title=output.title or "",
        instructions=output.instructions or "",
        assignee_id=legal_id,
        required_artifacts={},
        status=HumanTaskStatus.PENDING,
        blocking_for=HumanTaskBlocking.LAUNCH,
    )
    db.add(task)
    run.status = RunStatus.AWAITING_HUMAN_TASK
    await db.commit()
    return H3Run(
        run_id=run_id,
        guideline_id=guideline.id,
        task_id=task.id,
        claim_id=claim.id,
        image_right_id=image_right.id,
        disclaimer_id=disclaimer.id,
        tied_id=tied.id,
        reserve_id=reserve.id,
        image_id=image.id,
        set_hash=digest,
    )


async def snapshot(db: AsyncSession, h3: H3Run) -> dict[str, Any]:
    """Everything the H3 transaction may write, read fresh — for "nothing written"."""
    db.expire_all()

    async def count(model: Any) -> int:
        return int((await db.execute(sa.select(sa.func.count()).select_from(model))).scalar_one())

    exceptions = (
        (
            await db.execute(
                sa.select(CreativeException)
                .where(CreativeException.creative_run_id == h3.run_id)
                .order_by(CreativeException.id)
            )
        )
        .scalars()
        .all()
    )
    assets = (
        (
            await db.execute(
                sa.select(CreativeAsset.id, CreativeAsset.status).where(
                    CreativeAsset.creative_run_id == h3.run_id
                )
            )
        )
        .tuples()
        .all()
    )
    run = (await db.execute(sa.select(Run).where(Run.id == h3.run_id))).scalar_one()
    task = (await db.execute(sa.select(HumanTask).where(HumanTask.id == h3.task_id))).scalar_one()
    node = (
        await db.execute(
            sa.select(NodeRun).where(NodeRun.run_id == h3.run_id, NodeRun.node_id == "4.6.3")
        )
    ).scalar_one()
    return {
        "claim_records": await count(ClaimRecord),
        "claim_signatures": await count(ClaimSignature),
        "rule_sets": await count(RuleSet),
        "amendments": await count(PolicyAmendment),
        "guidelines": await count(ContentGuideline),
        "exceptions": [
            (
                row.id,
                row.status,
                row.decided_by,
                row.signature_id,
                row.claim_record_id,
                row.set_hash,
                row.human_task_id,
            )
            for row in exceptions
        ],
        "assets": sorted(assets),
        "pins": list(run.pins or []),
        "run_status": run.status,
        "task": (task.status, task.completed_by, task.submitted_payload),
        "node": (node.status, node.output),
    }
