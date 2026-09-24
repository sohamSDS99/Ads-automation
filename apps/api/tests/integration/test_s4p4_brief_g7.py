"""S4-P4 exit criteria (Stage 04 PRD §21.3), end to end on a real stack.

1. A run halts on G7 with a brief ≤ 600 words whose every line has a SourceRef.
2. An operator deciding G7 gets 403 (and so does an approver it is not routed to).
3. A media submit before G7 is refused in the job layer.
4. Editing the brief at decision re-hashes it.
5. A constant without `source` fails startup naming the key —
   `tests/creative/test_creative_constants.py` (the api and the worker).
6. `gateway.complete_structured(IMAGE_GEN)` raises — `tests/creative/test_task_classes.py`.

Plus what the executor now asserts for a creative run: the stored input is the
hashed input, and `media` / `lint_required` are checked after `reason()`.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from agent.creative import brief as briefs
from agent.creative import lint_adapter
from agent.db.models import (
    Approval,
    ApprovalStatus,
    AuditLog,
    CreativeAsset,
    CreativeAssetKind,
    CreativeAssetStatus,
    Evidence,
    EvidenceSource,
    GenerationJob,
    GenerationModality,
    GenerationStatus,
    NodeRun,
    NodeRunStatus,
    Run,
    RunStage,
    RunStatus,
    SignOffMatrix,
    User,
)
from agent.db.models import (
    CreativeBrief as CreativeBriefRow,
)
from agent.llm.router import TaskClass
from agent.media.jobs import BriefNotApproved
from agent.nodes.base import NodeSpec, RunContext
from agent.orchestrator.dag import Dag
from agent.orchestrator.registry import NodeRegistry
from agent.schemas.creative_brief import MAX_RENDERED_WORDS, CreativeBrief
from agent.schemas.creative_input import CreativeInput
from tests.integration.conftest import ApiClient, build_client, make_member
from tests.integration.creative_support import TEXT_ONLY, seed_both
from tests.integration.runs_support import execute
from tests.integration.test_media_jobs import BASE, IMAGE, World, choice, flux
from tests.media.openrouter_mock import response
from tests.openrouter_fake import FakeOpenRouter, completion

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# a scripted model that can only answer from the schema it was sent
# ---------------------------------------------------------------------------


def _resolve(root: dict[str, Any], ref: str) -> dict[str, Any]:
    node: Any = root
    for part in ref.removeprefix("#/").split("/"):
        node = node[part]
    return dict(node)


def _instance(node: dict[str, Any], root: dict[str, Any]) -> Any:
    """A valid answer to `node`, choosing the first option of every enum."""
    if "$ref" in node:
        return _instance(_resolve(root, node["$ref"]), root)
    if "anyOf" in node:
        options = [item for item in node["anyOf"] if item.get("type") != "null"]
        return _instance(options[0], root)
    if "const" in node:
        return node["const"]
    if "enum" in node:
        return node["enum"][0]
    kind = node.get("type")
    if kind == "object":
        return {key: _instance(value, root) for key, value in node["properties"].items()}
    if kind == "array":
        return [_instance(node["items"], root)]
    if kind == "string":
        return "Keep every safety data sheet current"
    if kind == "integer":
        return 1
    if kind == "boolean":
        return False
    raise AssertionError(f"no answer for {node}")


def _brief_writer(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    schema = body["response_format"]["json_schema"]["schema"]
    return completion(_instance(schema, schema), model=body["model"])


def _scripted() -> FakeOpenRouter:
    fake = FakeOpenRouter()
    fake.dispatch(_brief_writer)
    return fake


# ---------------------------------------------------------------------------
# the fixture: a started creative run whose G7 routes to a named approver
# ---------------------------------------------------------------------------


async def _approver(admin: ApiClient, db: AsyncSession, email: str) -> tuple[ApiClient, uuid.UUID]:
    address, password = await make_member(admin, "approver", email=email)
    client = build_client()
    assert (await client.login(address, password)).status_code == 200
    user = (await db.execute(sa.select(User).where(User.email == address))).scalar_one()
    return client, user.id


async def _start(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    actor: uuid.UUID,
    *,
    performance_owner: uuid.UUID | None = None,
) -> uuid.UUID:
    await seed_both(db, workspace_id, project_id, actor)
    if performance_owner is not None:
        await db.execute(
            sa.update(SignOffMatrix)
            .where(SignOffMatrix.project_id == project_id)
            .values(performance_owner_id=performance_owner)
        )
        await db.commit()
    started = await admin.post(
        f"/projects/{project_id}/creative/runs", json={"scope": TEXT_ONLY, "media_models": []}
    )
    assert started.status_code == 202, started.text
    return uuid.UUID(started.json()["run_id"])


async def _g7(db: AsyncSession, run_id: uuid.UUID) -> Approval:
    return (
        await db.execute(
            sa.select(Approval)
            .where(Approval.run_id == run_id, Approval.gate_key == "G7")
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def _brief_row(db: AsyncSession, run_id: uuid.UUID) -> CreativeBriefRow:
    # `populate_existing` rather than `expire_all()`: expiring every loaded row
    # turns the next attribute read into a lazy load, which async cannot do.
    return (
        await db.execute(
            sa.select(CreativeBriefRow)
            .where(CreativeBriefRow.creative_run_id == run_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


def _lines(payload: dict[str, Any]) -> list[dict[str, Any]]:
    lines = [payload["objective"], payload["angle"], *payload["audience"], *payload["exclusions"]]
    for group in payload["ad_groups"]:
        lines += [group["primary_message"], group["angle_b"]]
    return lines


# ---------------------------------------------------------------------------
# 1. the run halts on G7 with a sourced, one-page brief
# ---------------------------------------------------------------------------


async def test_a_run_halts_on_g7_with_a_one_page_brief_every_line_sourced(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
) -> None:
    owner_client, owner_id = await _approver(admin, db, "perf@example.com")
    await owner_client.raw.aclose()
    run_id = await _start(
        admin, db, workspace_id, project_id, admin_user.id, performance_owner=owner_id
    )

    result = await execute(run_id, _scripted())

    assert result.status is RunStatus.AWAITING_APPROVAL
    assert result.awaiting == ("4.1.1",)
    ran = (await db.execute(sa.select(NodeRun.node_id).where(NodeRun.run_id == run_id))).scalars()
    assert set(ran) == {"4.1.1"}, "nothing may run past G7"

    approval = await _g7(db, run_id)
    assert approval.node_id == "4.1.1" and approval.status is ApprovalStatus.PENDING
    assert approval.assignee_id == owner_id, "G7 routes to the pinned performance owner"

    row = await _brief_row(db, run_id)
    brief = CreativeBrief.model_validate(row.payload)
    assert row.approved_hash is None
    assert row.brief_hash == brief.brief_hash == approval.proposal["brief_hash"]
    assert brief.brief_hash == briefs.brief_hash(brief)
    assert brief.rendered_word_count <= MAX_RENDERED_WORDS
    assert brief.rendered_word_count == briefs.word_count(row.markdown)
    assert all(line["sources"] for line in _lines(row.payload))
    assert {source["stage"] for line in _lines(row.payload) for source in line["sources"]}

    # The media plan is calc's, cited as derived rows this run produced.
    calc = (
        (
            await db.execute(
                sa.select(Evidence).where(Evidence.id.in_(brief.media_plan.calc_evidence_ids))
            )
        )
        .scalars()
        .all()
    )
    assert {e.source for e in calc} == {EvidenceSource.DERIVED}
    assert {e.kind for e in calc} == {"calc_media_cost", "calc_ratio_plan"}

    run = await db.get(Run, run_id)
    assert run is not None and run.creative_input is not None
    assert CreativeInput.model_validate(run.creative_input).content_hash() == run.input_hash


# ---------------------------------------------------------------------------
# 2. who may decide G7
# ---------------------------------------------------------------------------


async def test_an_operator_deciding_g7_gets_403(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    signed_in_as: Any,
) -> None:
    run_id = await _start(admin, db, workspace_id, project_id, admin_user.id)
    await execute(run_id, _scripted())
    approval = await _g7(db, run_id)

    operator = await signed_in_as("operator")
    refused = await operator.post(f"/approvals/{approval.id}", json={"decision": "approve"})
    assert refused.status_code == 403, refused.text

    assert (await _g7(db, run_id)).status is ApprovalStatus.PENDING
    assert (await _brief_row(db, run_id)).approved_hash is None


async def test_an_approver_g7_is_not_routed_to_gets_403(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
) -> None:
    owner, owner_id = await _approver(admin, db, "perf2@example.com")
    other, _ = await _approver(admin, db, "brand2@example.com")
    try:
        run_id = await _start(
            admin, db, workspace_id, project_id, admin_user.id, performance_owner=owner_id
        )
        await execute(run_id, _scripted())
        approval = await _g7(db, run_id)
        refused = await other.post(f"/approvals/{approval.id}", json={"decision": "approve"})
        assert refused.status_code == 403, refused.text
        assert refused.json()["title"] == "Assigned to someone else"
    finally:
        await owner.raw.aclose()
        await other.raw.aclose()


# ---------------------------------------------------------------------------
# 3. no media before G7, enforced in the job layer
# ---------------------------------------------------------------------------


async def test_a_media_submit_before_g7_is_refused_in_the_job_layer(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    tmp_path: Path,
) -> None:
    run_id = await _start(admin, db, workspace_id, project_id, admin_user.id)
    await execute(run_id, _scripted())
    assert (await _brief_row(db, run_id)).approved_hash is None

    with respx.mock(assert_all_called=False) as router:
        router.post(f"{BASE}/images").mock(return_value=response("image_generate.json"))
        with pytest.raises(BriefNotApproved, match="not approved"):
            await (
                World(tmp_path, run_id)
                .jobs()
                .submit_or_resume(
                    run_id=run_id,
                    node_id="4.4.2",
                    asset_id=None,
                    round=1,
                    request=IMAGE,
                    choice=choice(flux()),
                    estimate_usd=Decimal("0.03"),
                )
            )
        assert len(router.calls) == 0


# ---------------------------------------------------------------------------
# 4. deciding G7: approved_hash = brief_hash, and an edit is re-hashed
# ---------------------------------------------------------------------------


async def test_editing_the_brief_at_decision_re_hashes_it(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    tmp_path: Path,
) -> None:
    run_id = await _start(admin, db, workspace_id, project_id, admin_user.id)
    await execute(run_id, _scripted())
    approval = await _g7(db, run_id)
    before = await _brief_row(db, run_id)
    old_hash = before.brief_hash

    edited = dict(approval.proposal)
    edited["angle"] = {**edited["angle"], "text": "Audit-ready sheets, without the chase."}
    decided = await admin.post(
        f"/approvals/{approval.id}", json={"decision": "approve", "edited_proposal": edited}
    )
    assert decided.status_code == 200, decided.text

    row = await _brief_row(db, run_id)
    assert row.brief_hash != old_hash
    assert row.approved_hash == row.brief_hash
    assert row.approval_id == approval.id
    assert row.payload["angle"]["text"] == "Audit-ready sheets, without the chase."
    assert briefs.brief_hash(CreativeBrief.model_validate(row.payload)) == row.brief_hash
    gate_run = (
        await db.execute(
            sa.select(NodeRun).where(NodeRun.run_id == run_id, NodeRun.node_id == "4.1.1")
        )
    ).scalar_one()
    assert gate_run.status is NodeRunStatus.SUCCEEDED
    assert gate_run.output is not None and gate_run.output["brief_hash"] == row.brief_hash
    audit = (
        (await db.execute(sa.select(AuditLog).where(AuditLog.target_id == approval.id)))
        .scalars()
        .all()
    )
    assert any((entry.meta or {}).get("approved_hash") == row.brief_hash for entry in audit)

    # The spend gate now opens, and the approved brief is frozen.
    await World(tmp_path, run_id).jobs().assert_g7_approved(run_id)
    row.payload = {**row.payload, "angle": {"text": "sneaky", "sources": []}}
    with pytest.raises(sa.exc.DBAPIError, match="immutable"):
        await db.commit()
    await db.rollback()

    # And the branch resumes past G7 through the 23 stubs to a finished run.
    resumed = await execute(run_id, _scripted())
    assert resumed.status is RunStatus.SUCCEEDED


async def test_approving_without_an_edit_stamps_the_proposed_hash(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
) -> None:
    run_id = await _start(admin, db, workspace_id, project_id, admin_user.id)
    await execute(run_id, _scripted())
    approval = await _g7(db, run_id)
    proposed = approval.proposal["brief_hash"]

    decided = await admin.post(f"/approvals/{approval.id}", json={"decision": "approve"})
    assert decided.status_code == 200, decided.text
    row = await _brief_row(db, run_id)
    assert row.brief_hash == row.approved_hash == proposed


async def test_an_edit_to_what_code_wrote_is_a_422_and_writes_nothing(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
) -> None:
    run_id = await _start(admin, db, workspace_id, project_id, admin_user.id)
    await execute(run_id, _scripted())
    approval = await _g7(db, run_id)

    edited = dict(approval.proposal)
    edited["media_plan"] = {**edited["media_plan"], "total_usd": "0.01"}
    refused = await admin.post(
        f"/approvals/{approval.id}", json={"decision": "approve", "edited_proposal": edited}
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["field"] == "media_plan"

    assert (await _g7(db, run_id)).status is ApprovalStatus.PENDING
    assert (await _brief_row(db, run_id)).approved_hash is None


# ---------------------------------------------------------------------------
# the executor's creative half
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mutation", "code"),
    [("missing", "creative_input_missing"), ("tampered", "creative_input_tampered")],
)
async def test_a_run_whose_input_is_not_the_hashed_input_fails_before_a_node(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    mutation: str,
    code: str,
) -> None:
    run_id = await _start(admin, db, workspace_id, project_id, admin_user.id)
    run = await db.get(Run, run_id)
    assert run is not None and run.creative_input is not None
    if mutation == "missing":
        run.creative_input = None
    else:
        run.creative_input = {**run.creative_input, "constants_version": "someone-else"}
    await db.commit()

    fake = _scripted()
    result = await execute(run_id, fake)
    assert result.status is RunStatus.FAILED
    assert result.error is not None and result.error["code"] == code
    assert fake.requests == [], "no token is spent on an input that is not the pinned one"


class _Out(BaseModel):
    ok: bool = True


class _WritesAnAsset:
    """A creative node that persists one asset past draft, linted or not."""

    def __init__(self, *, lint_required: bool, linted: bool) -> None:
        self.linted = linted
        self.spec = NodeSpec(
            id="4.9.1",
            name="writes_an_asset",
            stage="4.9",
            run_stage=RunStage.CREATIVE,
            task_class=TaskClass.COPYWRITE,
            input_model=_Out,
            output_model=_Out,
            lint_required=lint_required,
        )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        return []

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        creative = ctx.require_creative()
        target = {
            "ref": "h1",
            "surface": "rsa_headline",
            "campaign_type": "search",
            "market": "US",
            "language": "en",
            "text": "Keep every SDS current",
        }
        from agent.schemas.guardrails import LintTarget

        result = creative.linter.lint([LintTarget.model_validate(target)], now=ctx.run.started_at)
        ctx.db.add(
            CreativeAsset(
                workspace_id=ctx.run.workspace_id,
                project_id=ctx.project.id,
                creative_run_id=ctx.run.id,
                node_id=self.spec.id,
                campaign_ref="c-sds-us",
                kind=CreativeAssetKind.HEADLINE,
                surface="rsa_headline",
                text="Keep every SDS current",
                generated_by_ai=True,
                status=CreativeAssetStatus.LINTED,
                lint=result.model_dump(mode="json") if self.linted else None,
                ruleset_version=creative.linter.pin if self.linted else None,
                content_hash="0" * 64,
            )
        )
        return _Out()


class _SubmitsVideo(_WritesAnAsset):
    """Declares image, leaves a video job behind."""

    def __init__(self) -> None:
        super().__init__(lint_required=False, linted=False)
        self.spec = self.spec.model_copy(update={"media": ("image",)})

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        ctx.db.add(
            GenerationJob(
                workspace_id=ctx.run.workspace_id,
                project_id=ctx.project.id,
                creative_run_id=ctx.run.id,
                node_id=self.spec.id,
                modality=GenerationModality.VIDEO,
                model_id="acme/video",
                capability_hash="c",
                request={},
                idempotency_key=uuid.uuid4().hex,
                status=GenerationStatus.QUEUED,
                estimate_usd=Decimal("0.10"),
            )
        )
        return _Out()


@pytest.mark.parametrize(
    ("node", "succeeds", "message"),
    [
        (_WritesAnAsset(lint_required=True, linted=True), True, None),
        (_WritesAnAsset(lint_required=True, linted=False), False, "without a passing LintResult"),
        (_WritesAnAsset(lint_required=False, linted=True), False, "does not declare lint_required"),
        (_SubmitsVideo(), False, "submitted video generation"),
    ],
)
async def test_the_executor_asserts_media_and_lint_required(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    node: Any,
    succeeds: bool,
    message: str | None,
) -> None:
    run_id = await _start(admin, db, workspace_id, project_id, admin_user.id)
    registry = NodeRegistry.of([node])
    result = await execute(
        run_id, _scripted(), registry=registry, dag=Dag.from_registry(registry), max_attempts=1
    )
    node_run = (
        await db.execute(
            sa.select(NodeRun).where(NodeRun.run_id == run_id, NodeRun.node_id == "4.9.1")
        )
    ).scalar_one()
    if succeeds:
        assert result.status is RunStatus.SUCCEEDED, node_run.error
        return
    assert result.status is RunStatus.FAILED
    assert node_run.status is NodeRunStatus.FAILED
    assert message in json.dumps(node_run.error)


# ---------------------------------------------------------------------------
# the lint adapter's pin
# ---------------------------------------------------------------------------


async def test_the_adapter_loads_a_pin_and_refuses_a_foreign_hash(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
) -> None:
    _, _, ruleset = await seed_both(db, workspace_id, project_id, admin_user.id)
    pinned = await lint_adapter.load(
        db, workspace_id=workspace_id, pin=ruleset.ruleset_version, expected_hash=ruleset.hash
    )
    assert pinned.pin == ruleset.ruleset_version
    with pytest.raises(lint_adapter.LintAdapterError, match="pinned"):
        await lint_adapter.load(
            db, workspace_id=workspace_id, pin=ruleset.ruleset_version, expected_hash="nope"
        )
    with pytest.raises(lint_adapter.LintAdapterError, match="no ruleset"):
        await lint_adapter.load(db, workspace_id=workspace_id, pin="9.9+missing")
