"""The read side of a campaign plan (Stage 02 PRD §15.3 D, F; §16 "The plan").

`GET /plans/{plan_run_id}`, its structure pages and its version diff. Three
routes, and what these tests are actually about is the seam between them and
node 2.6.1 — which ships in S2-P5b. The reader has to work against a payload it
did not write, may only partly recognise, and must never crash on.

So the shapes asserted here are:

* a **whole** plan renders every field the Plan Viewer and the freeze dialog
  read in one response, including the four gate decisions, the critique verdict
  and the tree's totals. The dialog states the campaign and keyword counts
  before anyone types a version, and a dialog that opened four requests to say
  so would render in pieces;
* an **empty** payload answers 200 with zeroes rather than 500. A plan run that
  halted at a gate has a row and no payload, and the gate decisions somebody
  came to read are on that response;
* the **workspace boundary**, which `campaign_plan` carries itself;
* **pagination by campaign**, including that the totals are the plan's and not
  the page's, and that a cursor naming a campaign the payload no longer holds
  says so instead of silently restarting at page one.
"""

from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import (
    Approval,
    ApprovalRequiredRole,
    ApprovalStatus,
    CampaignPlan,
    CampaignPlanStatus,
    NodeRun,
    NodeRunStatus,
    Report,
    ResearchAcceptance,
    Run,
    RunStage,
    RunStatus,
    RunTrigger,
)
from tests.integration.conftest import ApiClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from plan_payload import campaign_plan_payload  # noqa: E402

pytestmark = pytest.mark.asyncio

GATES = (("G1", "2.1.3"), ("G2", "2.1.4"), ("G3", "2.2.5"), ("G4", "2.3.3"))


async def _seed(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    status: CampaignPlanStatus = CampaignPlanStatus.READY_TO_FREEZE,
    version: int = 0,
    payload_overrides: dict[str, Any] | None = None,
    empty_payload: bool = False,
    decide_gates: bool = True,
    critique: dict[str, Any] | None = None,
    reuse: ResearchAcceptance | None = None,
) -> CampaignPlan:
    """A plan row with its run, its acceptance and its four approvals.

    Everything a `CampaignPlan` cannot exist without: `acceptance_id` is
    `ON DELETE RESTRICT` because a plan that cannot name what it was planned
    from is not auditable, and `source_run_id` on the run is CHECKed by
    migration 0013.

    `reuse` exists because two plan versions in one project **must** share an
    acceptance: `uq_research_acceptance_current` is partial-unique on
    `project_id WHERE superseded_by IS NULL`, so a project has exactly one
    current acceptance at a time. That is also the honest model of versioning —
    two plan runs off the same signed-off research is how a project gets a
    version 2 after a budget was renegotiated.
    """
    if reuse is not None:
        acceptance = reuse
        research_id = reuse.run_id
        report_id = reuse.report_id
        return await _plan_from(
            db,
            project_id=project_id,
            workspace_id=workspace_id,
            user_id=user_id,
            acceptance=acceptance,
            research_id=research_id,
            report_id=report_id,
            status=status,
            version=version,
            payload_overrides=payload_overrides,
            empty_payload=empty_payload,
            decide_gates=decide_gates,
            critique=critique,
        )

    research = Run(
        workspace_id=workspace_id,
        project_id=project_id,
        triggered_by=user_id,
        trigger=RunTrigger.MANUAL,
        status=RunStatus.SUCCEEDED,
        stage=RunStage.RESEARCH,
    )
    db.add(research)
    await db.flush()

    report = Report(
        run_id=research.id,
        schema_version="1.0",
        payload={"launch_readiness": "go", "degraded_sources": []},
        markdown="# report",
    )
    db.add(report)
    await db.flush()

    acceptance = ResearchAcceptance(
        workspace_id=workspace_id,
        project_id=project_id,
        run_id=research.id,
        report_id=report.id,
        accepted_by=user_id,
        launch_readiness_at_acceptance="go",
    )
    db.add(acceptance)
    await db.flush()

    return await _plan_from(
        db,
        project_id=project_id,
        workspace_id=workspace_id,
        user_id=user_id,
        acceptance=acceptance,
        research_id=research.id,
        report_id=report.id,
        status=status,
        version=version,
        payload_overrides=payload_overrides,
        empty_payload=empty_payload,
        decide_gates=decide_gates,
        critique=critique,
    )


async def _plan_from(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    acceptance: ResearchAcceptance,
    research_id: uuid.UUID,
    report_id: uuid.UUID,
    status: CampaignPlanStatus,
    version: int,
    payload_overrides: dict[str, Any] | None,
    empty_payload: bool,
    decide_gates: bool,
    critique: dict[str, Any] | None,
) -> CampaignPlan:
    """One plan run and one plan row against an acceptance that already exists."""
    plan_run = Run(
        workspace_id=workspace_id,
        project_id=project_id,
        triggered_by=user_id,
        trigger=RunTrigger.MANUAL,
        status=RunStatus.SUCCEEDED,
        stage=RunStage.PLAN,
        source_run_id=research_id,
    )
    db.add(plan_run)
    await db.flush()

    if decide_gates:
        for gate_key, node_id in GATES:
            db.add(
                Approval(
                    run_id=plan_run.id,
                    node_id=node_id,
                    gate_key=gate_key,
                    status=ApprovalStatus.APPROVED,
                    required_role=ApprovalRequiredRole.APPROVER,
                    proposal={"gate": gate_key},
                    decided_by=user_id,
                )
            )

    if critique is not None:
        db.add(
            NodeRun(
                run_id=plan_run.id,
                node_id="2.6.2",
                status=NodeRunStatus.SUCCEEDED,
                output=critique,
                # `checked_at` on the response is this column. Without it the
                # fallback path reports a verdict with no time against it, which
                # reads as "never checked" on the freeze dialog.
                finished_at=datetime.now(UTC),
            )
        )

    payload: dict[str, Any] = {}
    if not empty_payload:
        payload = campaign_plan_payload(
            project_id=project_id,
            plan_run_id=plan_run.id,
            research_run_id=research_id,
            report_id=report_id,
            acceptance_id=acceptance.id,
            accepted_by=user_id,
            decider_id=user_id,
            **(payload_overrides or {}),
        )

    plan = CampaignPlan(
        workspace_id=workspace_id,
        project_id=project_id,
        plan_run_id=plan_run.id,
        acceptance_id=acceptance.id,
        schema_version="1.0",
        version=version,
        status=status,
        payload=payload,
        markdown="# plan" if not empty_payload else "",
    )
    db.add(plan)
    await db.commit()
    return plan


@pytest_asyncio.fixture
async def seeded(
    admin: ApiClient, db: AsyncSession, project: Any, workspace_id: uuid.UUID
) -> CampaignPlan:
    me = (await admin.get("/auth/me")).json()
    return await _seed(
        db,
        project_id=project.id,
        workspace_id=workspace_id,
        user_id=uuid.UUID(me["id"]),
        critique={"verdict": "pass", "blocking": [], "advisory": ["Wave 2 has no owner."]},
    )


# ---------------------------------------------------------------------------
# GET /plans/{plan_run_id}
# ---------------------------------------------------------------------------


async def test_a_whole_plan_arrives_in_one_response(admin: ApiClient, seeded: CampaignPlan) -> None:
    response = await admin.get(f"/plans/{seeded.plan_run_id}")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["status"] == "ready_to_freeze"
    assert body["schema_version"] == "1.0"
    assert body["payload"]["executive_summary"]
    # The four gate decisions, in gate order, whatever order they were decided.
    assert [gate["gate_key"] for gate in body["gates"]] == ["G1", "G2", "G3", "G4"]
    assert all(gate["status"] == "approved" for gate in body["gates"])
    assert all(gate["decided_by_name"] for gate in body["gates"])
    # The freeze dialog quotes these before anyone types a version.
    assert body["totals"] == {"campaigns": 4, "ad_groups": 12, "keywords": 96}
    # From the payload's `critique_issues[]`, not the node run: the payload is
    # what an exported PDF carries, and a document saying "ready to freeze"
    # while its critique lives elsewhere is how a blocked plan circulates as
    # approved. `warning` and `note` are both advisory; nothing blocking, so
    # the verdict derives to `pass`.
    assert body["critique"]["verdict"] == "pass"
    assert body["critique"]["blocking"] == []
    assert len(body["critique"]["advisory"]) == 2
    assert body["critique"]["advisory"][0].startswith("channel_slate: ")
    assert "Fix: " in body["critique"]["advisory"][0]
    # The header renders the source even though the payload carries its own copy.
    assert body["source"]["launch_readiness"] == "go"


async def test_an_unfrozen_plan_reports_the_version_a_freeze_would_mint(
    admin: ApiClient, seeded: CampaignPlan
) -> None:
    """§12.2 mints at freeze, so the number §15.3 E asks for does not exist yet.

    The dialog cannot compute it — that would be the frontend deriving a version
    the server is about to choose — so the response carries it.
    """
    body = (await admin.get(f"/plans/{seeded.plan_run_id}")).json()
    assert body["version"] == 0
    assert body["next_version"] == 1


async def test_next_version_counts_superseded_plans_too(
    admin: ApiClient, db: AsyncSession, project: Any, workspace_id: uuid.UUID
) -> None:
    """A superseded plan is not `frozen`, and its version must never be reused.

    `version` is what a signed-off plan is called for the rest of its life. A
    rule that counted only frozen rows would hand version 2 to a second plan
    after the first was superseded, and two plans in one project would answer to
    the same name.
    """
    me = (await admin.get("/auth/me")).json()
    user_id = uuid.UUID(me["id"])
    first = await _seed(
        db,
        project_id=project.id,
        workspace_id=workspace_id,
        user_id=user_id,
        status=CampaignPlanStatus.SUPERSEDED,
        version=1,
    )
    acceptance = await db.get(ResearchAcceptance, first.acceptance_id)
    assert acceptance is not None
    current = await _seed(
        db,
        project_id=project.id,
        workspace_id=workspace_id,
        user_id=user_id,
        reuse=acceptance,
    )

    body = (await admin.get(f"/plans/{current.plan_run_id}")).json()
    assert body["next_version"] == 2


async def test_an_empty_payload_is_answered_not_refused(
    admin: ApiClient, db: AsyncSession, project: Any, workspace_id: uuid.UUID
) -> None:
    """A plan run that halted before 2.6.1 has a row and no payload.

    The gate decisions on that response are the reason somebody opened the
    page. Refusing the whole plan because one section is missing would take
    them away.
    """
    me = (await admin.get("/auth/me")).json()
    plan = await _seed(
        db,
        project_id=project.id,
        workspace_id=workspace_id,
        user_id=uuid.UUID(me["id"]),
        status=CampaignPlanStatus.DRAFT,
        empty_payload=True,
        decide_gates=False,
    )

    body = (await admin.get(f"/plans/{plan.plan_run_id}")).json()
    assert body["payload"] == {}
    assert body["totals"] == {"campaigns": 0, "ad_groups": 0, "keywords": 0}
    assert body["critique"] is None
    # Every gate is still a named row: "G4 — the run never reached this gate" is
    # the answer to why the freeze button is refusing.
    assert [gate["status"] for gate in body["gates"]] == ["not_reached"] * 4


async def test_a_blocking_critique_is_read_from_the_payload(
    admin: ApiClient, db: AsyncSession, project: Any, workspace_id: uuid.UUID
) -> None:
    """The most dangerous thing this endpoint could get wrong.

    An earlier version read only the 2.6.2 node run, in shapes 2.6.2 does not
    emit, so the freeze dialog would have reported "no critique recorded" on a
    plan with blocking issues. The verdict is derived from the list rather than
    read as a label, so the two cannot disagree.
    """
    me = (await admin.get("/auth/me")).json()
    plan = await _seed(
        db,
        project_id=project.id,
        workspace_id=workspace_id,
        user_id=uuid.UUID(me["id"]),
    )
    await db.execute(
        sa.update(CampaignPlan)
        .where(CampaignPlan.id == plan.id)
        .values(
            payload=dict(plan.payload)
            | {
                "critique_issues": [
                    {
                        "severity": "blocking",
                        "section": "media_plan",
                        "finding": "The allocation does not sum to the envelope.",
                        "fix": "Re-run the budget gate.",
                        "check": "1_allocation_sums",
                    }
                ]
            }
        )
    )
    await db.commit()

    body = (await admin.get(f"/plans/{plan.plan_run_id}")).json()
    assert body["critique"]["verdict"] == "blocking_issues"
    assert body["critique"]["blocking"] == [
        "media_plan: The allocation does not sum to the envelope. Fix: Re-run the budget gate."
    ]


async def test_a_payload_without_critique_issues_falls_back_to_the_node_run(
    admin: ApiClient, db: AsyncSession, project: Any, workspace_id: uuid.UUID
) -> None:
    """A run that reached 2.6.2 before its payload learned to carry the review."""
    me = (await admin.get("/auth/me")).json()
    plan = await _seed(
        db,
        project_id=project.id,
        workspace_id=workspace_id,
        user_id=uuid.UUID(me["id"]),
        critique={"verdict": "pass", "blocking": [], "advisory": ["From the node run."]},
    )
    payload = dict(plan.payload)
    payload.pop("critique_issues", None)
    await db.execute(
        sa.update(CampaignPlan).where(CampaignPlan.id == plan.id).values(payload=payload)
    )
    await db.commit()

    body = (await admin.get(f"/plans/{plan.plan_run_id}")).json()
    assert body["critique"]["advisory"] == ["From the node run."]
    assert body["critique"]["checked_at"] is not None


async def test_a_run_with_no_plan_is_a_404_that_says_why(
    admin: ApiClient, db: AsyncSession, project: Any, workspace_id: uuid.UUID
) -> None:
    me = (await admin.get("/auth/me")).json()
    run = Run(
        workspace_id=workspace_id,
        project_id=project.id,
        triggered_by=uuid.UUID(me["id"]),
        trigger=RunTrigger.MANUAL,
        status=RunStatus.RUNNING,
        stage=RunStage.RESEARCH,
    )
    db.add(run)
    await db.commit()

    response = await admin.get(f"/plans/{run.id}")
    assert response.status_code == 404
    assert "synthesised" in response.json()["detail"]


async def test_another_workspaces_plan_is_not_readable(
    admin: ApiClient, second_client: ApiClient, seeded: CampaignPlan, workspace: Any
) -> None:
    """`campaign_plan` carries its own `workspace_id`, unlike `plan_calc`."""
    response = await second_client.get(f"/plans/{seeded.plan_run_id}")
    assert response.status_code in (401, 404)


async def test_the_history_is_newest_first_even_when_every_draft_is_version_zero(
    admin: ApiClient, db: AsyncSession, project: Any, workspace_id: uuid.UUID
) -> None:
    """§15.3 A asks for "newest first", and after 0014 that is not version order.

    Every unfrozen plan sits at version 0, so `ORDER BY version DESC` puts a v1
    frozen last week above a draft created this morning. The compare screen
    takes the first two rows as the newer and older side of its diff, so the
    wrong order here renders every delta backwards — which is how this was
    found: by reading the browser check's assertion about the sign of a
    six-thousand-dollar envelope change.
    """
    me = (await admin.get("/auth/me")).json()
    user_id = uuid.UUID(me["id"])

    frozen = await _seed(
        db,
        project_id=project.id,
        workspace_id=workspace_id,
        user_id=user_id,
        status=CampaignPlanStatus.FROZEN,
        version=1,
    )
    acceptance = await db.get(ResearchAcceptance, frozen.acceptance_id)
    assert acceptance is not None
    draft = await _seed(
        db,
        project_id=project.id,
        workspace_id=workspace_id,
        user_id=user_id,
        reuse=acceptance,
        status=CampaignPlanStatus.READY_TO_FREEZE,
    )

    items = (await admin.get(f"/projects/{project.id}/plans")).json()["items"]
    assert [row["id"] for row in items] == [str(draft.id), str(frozen.id)], (
        "the draft was created second and must sort first; version order would invert these"
    )
    assert items[0]["version"] == 0
    assert items[1]["version"] == 1


# ---------------------------------------------------------------------------
# GET /plans/{plan_run_id}/structure
# ---------------------------------------------------------------------------


async def test_the_structure_paginates_by_campaign_and_totals_the_whole_plan(
    admin: ApiClient, db: AsyncSession, project: Any, workspace_id: uuid.UUID
) -> None:
    me = (await admin.get("/auth/me")).json()
    plan = await _seed(
        db,
        project_id=project.id,
        workspace_id=workspace_id,
        user_id=uuid.UUID(me["id"]),
        payload_overrides={
            "campaigns": 7,
            "ad_groups_per_campaign": 2,
            "keywords_per_ad_group": 3,
        },
    )

    first = (await admin.get(f"/plans/{plan.plan_run_id}/structure?limit=3")).json()
    assert len(first["campaigns"]) == 3
    assert first["next_cursor"]
    # The plan's totals, never the page's: a reader on page one still needs to
    # know how much tree there is, and the freeze dialog quotes the same numbers.
    assert first["totals"] == {"campaigns": 7, "ad_groups": 14, "keywords": 42}
    assert first["validator_regex"]
    assert first["campaigns"][0]["ad_group_count"] == 2
    assert first["campaigns"][0]["keyword_count"] == 6

    seen = list(first["campaigns"])
    cursor = first["next_cursor"]
    while cursor:
        page = (
            await admin.get(f"/plans/{plan.plan_run_id}/structure?limit=3&cursor={cursor}")
        ).json()
        seen.extend(page["campaigns"])
        cursor = page["next_cursor"]

    assert len(seen) == 7
    # No campaign appears twice and none is skipped, which is the only thing a
    # cursor has to guarantee.
    assert len({(row["campaign_ref"], row["market"]) for row in seen}) == 7


async def test_a_stale_cursor_says_so_rather_than_restarting(
    admin: ApiClient, seeded: CampaignPlan
) -> None:
    """Silently answering page one would read as "you have reached the end"."""
    response = await admin.get(f"/plans/{seeded.plan_run_id}/structure?cursor=gone@XX")
    assert response.status_code == 422
    assert "rewritten" in response.json()["detail"]


async def test_the_badge_and_the_tick_come_from_the_nodes_that_decided_them(
    admin: ApiClient, db: AsyncSession, project: Any, workspace_id: uuid.UUID
) -> None:
    """2.4.3's verdict and 2.4.1's regex, per campaign and per name.

    `name_valid` has three states and the third is the point: `None` means
    unchecked, which is what an unnamed convention produces, and a green tick on
    an unchecked name is the screen asserting something nobody verified.
    """
    me = (await admin.get("/auth/me")).json()
    plan = await _seed(
        db,
        project_id=project.id,
        workspace_id=workspace_id,
        user_id=uuid.UUID(me["id"]),
        payload_overrides={"campaigns": 2, "invalid_names": ("theme1-de-create-01",)},
    )

    page = (await admin.get(f"/plans/{plan.plan_run_id}/structure")).json()
    by_name = {row["name"]: row for row in page["campaigns"]}
    assert by_name["theme1-de-create-01"]["name_valid"] is False
    assert by_name["theme1-us-capture-00"]["name_valid"] is True
    assert page["invalid_names"] == ["theme1-de-create-01"]

    # The badge names the threshold and the shortfall, not just "below".
    verdicts = {row["campaign_ref"]: row for row in page["campaigns"]}
    for row in verdicts.values():
        assert row["verdict"] in ("clears", "below_threshold")
        assert row["threshold"] == 30.0
        assert row["forecast_conv_30d"] is not None


async def test_an_unchecked_tree_is_not_reported_as_a_clean_one(
    admin: ApiClient,
    db: AsyncSession,
    project: Any,
    second_project_id: uuid.UUID,
    workspace_id: uuid.UUID,
) -> None:
    """`null` and `[]` are different answers and must not render the same.

    `null` means node 2.4.2 never checked; `[]` means it checked and everything
    passed. Collapsing the two reports a clean bill of health on a tree nobody
    validated — the same error as a green tick on an unchecked name, one level
    up. `name_valid` is withheld in that state for exactly the same reason.
    """
    me = (await admin.get("/auth/me")).json()
    user_id = uuid.UUID(me["id"])

    # Two projects, not two plans in one. `uq_campaign_plan_project_version` as
    # shipped rejects a second version-0 row per project — the defect S2-P5b
    # fixes in migration 0014 with a partial unique index `WHERE version > 0`,
    # which is not on this branch. This test is about whether an unchecked tree
    # reads as a clean one; entangling it with version numbering would make it
    # fail for a reason it is not about.
    unchecked = await _seed(
        db,
        project_id=project.id,
        workspace_id=workspace_id,
        user_id=user_id,
        payload_overrides={"campaigns": 2, "declare_findings": False},
    )
    page = (await admin.get(f"/plans/{unchecked.plan_run_id}/structure")).json()
    assert page["invalid_names"] is None
    assert page["duplicate_terms"] is None
    assert all(row["name_valid"] is None for row in page["campaigns"])
    assert all(
        group["name_valid"] is None for row in page["campaigns"] for group in row["ad_groups"]
    )

    checked = await _seed(
        db,
        project_id=second_project_id,
        workspace_id=workspace_id,
        user_id=user_id,
        payload_overrides={"campaigns": 2, "duplicate_terms": ("sds software",)},
    )
    page = (await admin.get(f"/plans/{checked.plan_run_id}/structure")).json()
    assert page["invalid_names"] == []
    assert page["duplicate_terms"] == ["sds software"]
    assert all(row["name_valid"] is True for row in page["campaigns"])
    # The regex and the collision pass both come from
    # `account_structure.naming_convention`, where `plan_contract.py` puts them.
    assert page["validator_regex"]
    assert page["collision_check"] == "checked"


async def test_a_limit_outside_the_range_is_refused(admin: ApiClient, seeded: CampaignPlan) -> None:
    for limit in (0, 41):
        response = await admin.get(f"/plans/{seeded.plan_run_id}/structure?limit={limit}")
        assert response.status_code == 422, limit


# ---------------------------------------------------------------------------
# GET /plans/{plan_run_id}/diff
# ---------------------------------------------------------------------------


async def test_two_versions_diff_by_section(
    admin: ApiClient, db: AsyncSession, project: Any, workspace_id: uuid.UUID
) -> None:
    me = (await admin.get("/auth/me")).json()
    user_id = uuid.UUID(me["id"])
    older = await _seed(
        db,
        project_id=project.id,
        workspace_id=workspace_id,
        user_id=user_id,
        status=CampaignPlanStatus.SUPERSEDED,
        version=1,
        payload_overrides={"envelope_usd": 48_000.0, "campaigns": 4},
    )
    acceptance = await db.get(ResearchAcceptance, older.acceptance_id)
    assert acceptance is not None
    newer = await _seed(
        db,
        project_id=project.id,
        workspace_id=workspace_id,
        user_id=user_id,
        reuse=acceptance,
        payload_overrides={"envelope_usd": 60_000.0, "campaigns": 5},
    )

    body = (await admin.get(f"/plans/{newer.plan_run_id}/diff?against={older.plan_run_id}")).json()

    assert body["version"] == 0
    assert body["against_version"] == 1
    assert body["unchanged"] is False
    scalars = {row["field"]: (row["before"], row["after"]) for row in body["scalars"]}
    assert scalars["Monthly envelope"] == (48_000.0, 60_000.0)
    sections = {row["path"]: row for row in body["sections"]}
    assert sections["account_structure.campaigns"]["added"] == 1
    # Unchanged sections are omitted, not sent as zero rows.
    assert all(row["total"] > 0 for row in body["sections"])


async def test_a_plan_cannot_be_diffed_against_another_project(
    admin: ApiClient,
    db: AsyncSession,
    project: Any,
    second_project_id: uuid.UUID,
    workspace_id: uuid.UUID,
) -> None:
    """Every section would report as changed, which is true and useless."""
    me = (await admin.get("/auth/me")).json()
    user_id = uuid.UUID(me["id"])
    mine = await _seed(db, project_id=project.id, workspace_id=workspace_id, user_id=user_id)
    theirs = await _seed(
        db, project_id=second_project_id, workspace_id=workspace_id, user_id=user_id
    )

    response = await admin.get(f"/plans/{mine.plan_run_id}/diff?against={theirs.plan_run_id}")
    assert response.status_code == 422
    assert "different projects" in response.json()["detail"]


async def test_a_plan_is_not_diffed_against_itself(admin: ApiClient, seeded: CampaignPlan) -> None:
    response = await admin.get(f"/plans/{seeded.plan_run_id}/diff?against={seeded.plan_run_id}")
    assert response.status_code == 422


async def test_a_viewer_can_read_every_one_of_these_and_change_nothing(
    signed_in_as: Any, seeded: CampaignPlan
) -> None:
    """§21's S2-P6 exit criterion: a `viewer` reads all of it and changes none.

    The three routes are READ, so a viewer gets them. There is nothing here to
    assert about mutation because this phase adds no mutating route — the freeze
    transaction is S2-P5b's, and `test_authz_matrix` covers what a role may not
    call.
    """
    viewer = await signed_in_as("viewer")
    for path in ("", "/structure"):
        response = await viewer.get(f"/plans/{seeded.plan_run_id}{path}")
        assert response.status_code == 200, f"{path} -> {response.text}"
