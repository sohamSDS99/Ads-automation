"""The attention summary and the history counts (Stage 03 PRD §15.1, §15.3 A).

S3-P7 adds exactly one read endpoint, and this is what it owes. The rail's two
badges and the landing's third block are the only consumers, so the assertions
are written the way those surfaces read it: a red dot is *the caller's* open
task, an amber dot is the project's, and neither may light up for a row that is
no longer waiting on anybody.

Three of these would have caught a plausible wrong implementation:

* `not_required` and `expired` are not open. `HumanTaskStatus.NOT_REQUIRED` is
  a *finding* — 3.3.1 decided the obligation does not apply — and counting it
  would put a permanent red dot on every project that is working correctly.
* `auto_applied` amendments are reviewed by construction. A mechanical change
  applied itself and minted its MINOR, which law 29 says is the correct end of
  its life; counting it as unreviewed inverts the rule.
* An expiring claim only counts when it is `approved`. An unsupported or
  rejected claim licenses nothing today, so its expiry date changes nothing.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import (
    AmendmentChangeKind,
    AmendmentOrigin,
    AmendmentStatus,
    ClaimRecord,
    ClaimStatus,
    ClaimType,
    ContentGuideline,
    GuidelineMode,
    GuidelineStatus,
    HumanTask,
    HumanTaskBlocking,
    HumanTaskStatus,
    PolicyAmendment,
    Run,
    RunStage,
    RunStatus,
    RunTrigger,
    User,
)
from tests.integration.conftest import ApiClient, make_member

pytestmark = pytest.mark.asyncio


async def _guideline(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    *,
    status: GuidelineStatus = GuidelineStatus.DRAFT,
    major: int = 1,
    payload: dict[str, Any] | None = None,
    signature_stale: bool = False,
) -> ContentGuideline:
    run = Run(
        workspace_id=workspace_id,
        project_id=project_id,
        stage=RunStage.GUIDELINE,
        status=RunStatus.SUCCEEDED,
        trigger=RunTrigger.MANUAL,
        bindings={},
    )
    db.add(run)
    await db.flush()
    guideline = ContentGuideline(
        workspace_id=workspace_id,
        project_id=project_id,
        guideline_run_id=run.id,
        schema_version="1.0",
        version_major=major,
        version_minor=0,
        status=status,
        mode=GuidelineMode.STANDALONE,
        bindings={},
        payload=payload,
        signature_stale=signature_stale,
    )
    db.add(guideline)
    await db.flush()
    return guideline


async def _task(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    assignee_id: uuid.UUID,
    *,
    status: HumanTaskStatus = HumanTaskStatus.PENDING,
    key: str = "H1",
) -> HumanTask:
    task = HumanTask(
        workspace_id=workspace_id,
        project_id=project_id,
        task_key=key,
        title=f"{key} task",
        instructions="Do the thing only a person can do.",
        assignee_id=assignee_id,
        status=status,
        blocking_for=(HumanTaskBlocking.PUBLISH if key == "H1" else HumanTaskBlocking.LAUNCH),
    )
    db.add(task)
    await db.flush()
    return task


async def _claim(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    guideline_id: uuid.UUID,
    *,
    status: ClaimStatus,
    expires_in_days: int | None,
    text: str,
) -> None:
    db.add(
        ClaimRecord(
            workspace_id=workspace_id,
            project_id=project_id,
            first_seen_guideline_id=guideline_id,
            claim_text=text,
            normalized_text=text.lower(),
            claim_type=ClaimType.QUANTIFIED,
            status=status,
            expires_at=(
                None
                if expires_in_days is None
                else datetime.now(UTC) + timedelta(days=expires_in_days)
            ),
        )
    )
    await db.flush()


async def _amendment(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    *,
    status: AmendmentStatus,
    kind: AmendmentChangeKind = AmendmentChangeKind.SUBSTANTIVE,
) -> None:
    db.add(
        PolicyAmendment(
            workspace_id=workspace_id,
            project_id=project_id,
            origin=AmendmentOrigin.POLICY_WATCH,
            change_kind=kind,
            status=status,
        )
    )
    await db.flush()


async def test_a_quiet_project_reports_nothing_waiting(
    admin: ApiClient, project_id: uuid.UUID
) -> None:
    """The empty state is an answer, and the badges must not render for it."""
    body = (await admin.get(f"/projects/{project_id}/guidelines/attention")).json()
    assert body["open_tasks"] == []
    assert body["open_tasks_total"] == 0
    assert body["my_open_tasks"] == 0
    assert body["expiring_claims"] == 0
    assert body["earliest_expiry"] is None
    assert body["unreviewed_amendments"] == 0
    assert body["signature_stale"] is False


async def test_my_open_tasks_is_the_caller_not_the_project(
    admin: ApiClient, project_id: uuid.UUID, db: AsyncSession, workspace_id: uuid.UUID
) -> None:
    """The red dot is *yours*, so the server decides it — not the client.

    Deriving `mine` on the client would mean shipping the current user id into
    a rail that has no other use for it, and getting it wrong is invisible: a
    dot that is always on reads exactly like a dot that is correctly on.
    """
    me = (await admin.get("/auth/me")).json()
    # `make_member` returns (email, password) — the id has to be looked up.
    other_email, _ = await make_member(admin, "approver", email="legal@example.com")
    other_id = (await db.execute(sa.select(User.id).where(User.email == other_email))).scalar_one()

    await _task(db, workspace_id, project_id, uuid.UUID(me["id"]))
    await _task(db, workspace_id, project_id, other_id, key="H2")
    await db.commit()

    body = (await admin.get(f"/projects/{project_id}/guidelines/attention")).json()
    assert body["open_tasks_total"] == 2, "both are listed — somebody else's blocks me too"
    assert body["my_open_tasks"] == 1
    mine = [task for task in body["open_tasks"] if task["mine"]]
    assert len(mine) == 1
    assert mine[0]["assignee_name"], "a row that cannot name its assignee is not actionable"
    assert {task["blocking_for"] for task in body["open_tasks"]} == {"publish", "launch"}


@pytest.mark.parametrize(
    "status",
    [HumanTaskStatus.COMPLETED, HumanTaskStatus.NOT_REQUIRED, HumanTaskStatus.EXPIRED],
)
async def test_settled_tasks_are_not_open(
    admin: ApiClient,
    project_id: uuid.UUID,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    status: HumanTaskStatus,
) -> None:
    """`not_required` is a finding, not an omission — and never a red dot."""
    me = (await admin.get("/auth/me")).json()
    await _task(db, workspace_id, project_id, uuid.UUID(me["id"]), status=status)
    await db.commit()

    body = (await admin.get(f"/projects/{project_id}/guidelines/attention")).json()
    assert body["open_tasks_total"] == 0
    assert body["my_open_tasks"] == 0


async def test_only_approved_claims_can_expire_into_anything(
    admin: ApiClient, project_id: uuid.UUID, db: AsyncSession, workspace_id: uuid.UUID
) -> None:
    """An unsupported claim licenses nothing today; its date changes nothing."""
    guideline = await _guideline(db, workspace_id, project_id)
    await _claim(
        db,
        workspace_id,
        project_id,
        guideline.id,
        status=ClaimStatus.APPROVED,
        expires_in_days=10,
        text="Soonest",
    )
    await _claim(
        db,
        workspace_id,
        project_id,
        guideline.id,
        status=ClaimStatus.APPROVED,
        expires_in_days=25,
        text="Later",
    )
    await _claim(
        db,
        workspace_id,
        project_id,
        guideline.id,
        status=ClaimStatus.APPROVED,
        expires_in_days=90,
        text="Outside the window",
    )
    await _claim(
        db,
        workspace_id,
        project_id,
        guideline.id,
        status=ClaimStatus.UNSUPPORTED,
        expires_in_days=5,
        text="Unsupported",
    )
    await _claim(
        db,
        workspace_id,
        project_id,
        guideline.id,
        status=ClaimStatus.APPROVED,
        expires_in_days=None,
        text="No expiry",
    )
    await db.commit()

    body = (await admin.get(f"/projects/{project_id}/guidelines/attention")).json()
    assert body["expiring_claims"] == 2, "10 and 25 days; 90 is outside, unsupported does not count"
    assert body["expiry_window_days"] == 30
    earliest = datetime.fromisoformat(body["earliest_expiry"])
    assert (earliest - datetime.now(UTC)).days < 11, "the *first* one, not any of them"


async def test_auto_applied_amendments_are_reviewed_by_construction(
    admin: ApiClient, project_id: uuid.UUID, db: AsyncSession, workspace_id: uuid.UUID
) -> None:
    """Law 29: a mechanical change applied itself. Nobody owes it anything."""
    await _amendment(
        db,
        workspace_id,
        project_id,
        status=AmendmentStatus.AUTO_APPLIED,
        kind=AmendmentChangeKind.MECHANICAL,
    )
    await _amendment(db, workspace_id, project_id, status=AmendmentStatus.APPLIED)
    await _amendment(db, workspace_id, project_id, status=AmendmentStatus.DISMISSED)
    await _amendment(db, workspace_id, project_id, status=AmendmentStatus.OPEN)
    await _amendment(
        db,
        workspace_id,
        project_id,
        status=AmendmentStatus.NEEDS_REVIEW,
        kind=AmendmentChangeKind.SIGNATURE_AFFECTING,
    )
    await db.commit()

    body = (await admin.get(f"/projects/{project_id}/guidelines/attention")).json()
    assert body["unreviewed_amendments"] == 2
    assert body["signature_affecting_amendments"] == 1


async def test_signature_stale_follows_the_published_version(
    admin: ApiClient, project_id: uuid.UUID, db: AsyncSession, workspace_id: uuid.UUID
) -> None:
    """A stale draft is not a stale rulebook — only what is serving counts."""
    await _guideline(
        db, workspace_id, project_id, status=GuidelineStatus.DRAFT, signature_stale=True
    )
    await db.commit()
    body = (await admin.get(f"/projects/{project_id}/guidelines/attention")).json()
    assert body["signature_stale"] is False, "a draft cannot make the published rulebook stale"

    published = await _guideline(
        db,
        workspace_id,
        project_id,
        status=GuidelineStatus.PUBLISHED,
        major=2,
        signature_stale=True,
    )
    assert published.id is not None
    await db.commit()
    body = (await admin.get(f"/projects/{project_id}/guidelines/attention")).json()
    assert body["signature_stale"] is True


async def test_a_viewer_may_read_the_summary(
    admin: ApiClient, project_id: uuid.UUID, signed_in_as: Any
) -> None:
    """It is a count of work, not the work — no claim text, no instructions."""
    assert admin is not None  # the fixture creates the workspace this reads
    viewer_client = await signed_in_as("viewer")
    response = await viewer_client.get(f"/projects/{project_id}/guidelines/attention")
    assert response.status_code == 200, response.text
    assert set(response.json()) >= {"open_tasks", "expiring_claims", "unreviewed_amendments"}


async def test_attention_404s_for_a_project_that_is_not_visible(admin: ApiClient) -> None:
    body = await admin.get(f"/projects/{uuid.uuid4()}/guidelines/attention")
    assert body.status_code == 404


# ---------------------------------------------------------------------------
# the history counts (§15.3 A block 4)
# ---------------------------------------------------------------------------


async def test_history_counts_come_from_the_payload_and_survive_its_absence(
    admin: ApiClient, project_id: uuid.UUID, db: AsyncSession, workspace_id: uuid.UUID
) -> None:
    """Counted in SQL off the payload, and NULL-safe at both levels.

    A draft written before 3.6.1 has no payload at all, and one halted
    mid-synthesis can carry the key without the list — `jsonb_array_length`
    raises on either, so the guard is a type check as well as a NULL check.
    """
    await _guideline(db, workspace_id, project_id, major=1, payload=None)
    await _guideline(db, workspace_id, project_id, major=2, payload={"rules": []})
    await _guideline(
        db,
        workspace_id,
        project_id,
        major=3,
        payload={
            "rules": [{"rule_id": "a"}, {"rule_id": "b"}, {"rule_id": "c"}],
            "claims_register": {"claims": [{"claim_id": "x"}, {"claim_id": "y"}]},
        },
    )
    # The shape that would crash a naive `jsonb_array_length`.
    await _guideline(
        db,
        workspace_id,
        project_id,
        major=4,
        payload={"rules": {"not": "a list"}, "claims_register": {}},
    )
    await db.commit()

    versions = (await admin.get(f"/projects/{project_id}/guidelines")).json()["versions"]
    by_major = {item["version_major"]: item for item in versions}
    assert by_major[1]["rule_count"] == 0 and by_major[1]["claim_count"] == 0
    assert by_major[2]["rule_count"] == 0
    assert by_major[3]["rule_count"] == 3
    assert by_major[3]["claim_count"] == 2
    assert by_major[4]["rule_count"] == 0, "a non-array must be 0, not a 500"


async def test_published_by_name_is_resolved_not_a_uuid(
    admin: ApiClient, project_id: uuid.UUID, db: AsyncSession, workspace_id: uuid.UUID
) -> None:
    me = (await admin.get("/auth/me")).json()
    guideline = await _guideline(db, workspace_id, project_id, status=GuidelineStatus.PUBLISHED)
    await db.execute(
        sa.update(ContentGuideline)
        .where(ContentGuideline.id == guideline.id)
        .values(published_by=uuid.UUID(me["id"]), published_at=datetime.now(UTC))
    )
    await db.commit()

    versions = (await admin.get(f"/projects/{project_id}/guidelines")).json()["versions"]
    published = next(item for item in versions if item["status"] == "published")
    assert published["published_by_name"] == me["name"]
    assert published["published_by_name"] != published["published_by"]


async def test_an_unpublished_version_has_no_publisher_name(
    admin: ApiClient, project_id: uuid.UUID, db: AsyncSession, workspace_id: uuid.UUID
) -> None:
    """The outer join must not drop the row, and must not invent a name."""
    await _guideline(db, workspace_id, project_id)
    await db.commit()
    versions = (await admin.get(f"/projects/{project_id}/guidelines")).json()["versions"]
    assert len(versions) == 1
    assert versions[0]["published_by_name"] == ""
