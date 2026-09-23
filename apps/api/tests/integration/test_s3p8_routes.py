"""S3-P8's route layer: person-tasks, governance, amendments and the text linter.

Each test here is one of §21's S3-P8 exit criteria expressed as a refusal,
because every one of these routes is defined by what it will not do:

* the attestation is refused to everybody but one named person,
* the reassign preview must return the *exact* number of signatures it voids,
* a legal-owner change without a reason is refused,
* the linter is open to a `viewer` and writes nothing.

The signature path itself is S3-P3's and is covered by `test_claim_signature`.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import (
    AmendmentChangeKind,
    AmendmentOrigin,
    AmendmentStatus,
    ClaimRecord,
    ClaimSignature,
    ClaimStatus,
    HumanTask,
    HumanTaskBlocking,
    HumanTaskStatus,
    PolicyAmendment,
    Run,
    SignatureMethod,
    SignOffMatrix,
)
from tests.integration.claims_support import (
    as_client,
    cast,
    decisions_for,
    seed_register,
    user_id_of,
)
from tests.integration.conftest import ApiClient

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _open_task(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    assignee_id: uuid.UUID,
    task_key: str = "H2",
    required: dict | None = None,
) -> HumanTask:
    run = (await db.execute(sa.select(Run).where(Run.project_id == project_id))).scalars().first()
    assert run is not None
    task = HumanTask(
        workspace_id=run.workspace_id,
        project_id=project_id,
        guideline_run_id=run.id,
        node_id="3.3.2",
        task_key=task_key,
        title="Confirm the verification badge",
        instructions="Upload the certificate and attest that it is current.",
        assignee_id=assignee_id,
        required_artifacts=required if required is not None else {},
        status=HumanTaskStatus.PENDING,
        blocking_for=HumanTaskBlocking.LAUNCH,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return task


async def _sign(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    signer_id: uuid.UUID,
    claims: list[ClaimRecord],
) -> uuid.UUID:
    """A live signature over `claims`, written directly.

    The real path is S3-P3's and needs a step-up token; what these tests need
    is the *state* a signature leaves behind, so the void arithmetic has
    something true to count.
    """
    run = (await db.execute(sa.select(Run).where(Run.project_id == project_id))).scalars().first()
    assert run is not None
    signature = ClaimSignature(
        workspace_id=run.workspace_id,
        project_id=project_id,
        signer_id=signer_id,
        claim_ids=[claim.id for claim in claims],
        set_hash="deadbeef",
        decisions=[{"claim_id": str(c.id), "decision": "approved"} for c in claims],
        statement="I confirm these claims are substantiated.",
        method=SignatureMethod.STEP_UP_PASSWORD,
        reauth_token_id=uuid.uuid4().hex,
    )
    db.add(signature)
    await db.flush()
    for claim in claims:
        claim.status = ClaimStatus.APPROVED
        claim.current_signature_id = signature.id
    await db.commit()
    await db.refresh(signature)
    # The id, not the row. Callers roll back to see the route's writes, and a
    # rollback expires every attribute on this instance — reading one afterwards
    # tries to refresh it from a sync frame and raises MissingGreenlet.
    return signature.id


# ---------------------------------------------------------------------------
# person-tasks
# ---------------------------------------------------------------------------


async def test_only_the_assignee_may_submit_and_the_control_says_so(
    admin: ApiClient, db: AsyncSession, project_id: uuid.UUID
) -> None:
    """A person-task has no role fallback, and `can_submit` says so per caller."""
    people = await cast(admin)
    await seed_register(admin, db, project_id)
    legal_id = await user_id_of(db, "legal@example.com")
    task = await _open_task(db, project_id=project_id, assignee_id=legal_id)

    # The assignee sees the control.
    legal = await as_client(people["legal"])
    mine = await legal.get("/human-tasks?mine=true")
    assert mine.status_code == 200, mine.text
    row = next(item for item in mine.json()["items"] if item["id"] == str(task.id))
    assert row["can_submit"] is True
    assert mine.json()["mine_open"] == 1

    # The other approver holds ATTEST_SUBMIT and is still refused: the role is
    # necessary and nowhere near sufficient.
    other = await as_client(people["other_approver"])
    theirs = await other.get("/human-tasks")
    assert theirs.status_code == 200
    seen = next(item for item in theirs.json()["items"] if item["id"] == str(task.id))
    assert seen["can_submit"] is False
    assert seen["assignee_email"] == "legal@example.com"
    assert theirs.json()["mine_open"] == 0

    refused = await other.post(
        f"/human-tasks/{task.id}/submit",
        json={"payload": {"reauth_token": "anything"}, "artifacts_confirmed": []},
    )
    assert refused.status_code == 403, refused.text
    assert "assigned to" in refused.json()["detail"]


async def test_an_admin_cannot_submit_an_attestation(
    admin: ApiClient, db: AsyncSession, project_id: uuid.UUID
) -> None:
    """`ATTEST_SUBMIT` is subtracted from every admin. Law 23, at the route."""
    await cast(admin)
    await seed_register(admin, db, project_id)
    legal_id = await user_id_of(db, "legal@example.com")
    task = await _open_task(db, project_id=project_id, assignee_id=legal_id)

    refused = await admin.post(
        f"/human-tasks/{task.id}/submit",
        json={"payload": {"reauth_token": "x"}, "artifacts_confirmed": []},
    )
    # 403 from the permission dependency, before identity is even consulted.
    assert refused.status_code == 403, refused.text


async def test_the_checklist_is_the_gate(
    admin: ApiClient, db: AsyncSession, project_id: uuid.UUID
) -> None:
    """A submission that skipped a required artifact is not the attestation asked for."""
    people = await cast(admin)
    await seed_register(admin, db, project_id)
    legal_id = await user_id_of(db, "legal@example.com")
    task = await _open_task(
        db,
        project_id=project_id,
        assignee_id=legal_id,
        required={"items": ["certificate", "screenshot"]},
    )

    legal = await as_client(people["legal"])
    refused = await legal.post(
        f"/human-tasks/{task.id}/submit",
        json={"payload": {"reauth_token": "x"}, "artifacts_confirmed": ["certificate"]},
    )
    assert refused.status_code == 422, refused.text
    assert "screenshot" in refused.json()["detail"]


async def test_submitting_without_a_step_up_is_refused(
    admin: ApiClient, db: AsyncSession, project_id: uuid.UUID
) -> None:
    """Presence is proved at the submit, not inherited from the session."""
    people = await cast(admin)
    await seed_register(admin, db, project_id)
    legal_id = await user_id_of(db, "legal@example.com")
    task = await _open_task(db, project_id=project_id, assignee_id=legal_id)

    legal = await as_client(people["legal"])
    refused = await legal.post(
        f"/human-tasks/{task.id}/submit", json={"payload": {}, "artifacts_confirmed": []}
    )
    assert refused.status_code == 401, refused.text
    assert refused.json()["title"] == "Step-up required"


async def test_submitting_with_a_step_up_completes_and_hides_the_token_id(
    admin: ApiClient, db: AsyncSession, project_id: uuid.UUID
) -> None:
    people = await cast(admin)
    await seed_register(admin, db, project_id)
    legal_id = await user_id_of(db, "legal@example.com")
    task = await _open_task(db, project_id=project_id, assignee_id=legal_id)

    legal = await as_client(people["legal"])
    minted = await legal.post("/auth/reauth", json={"password": people["legal"][1]})
    assert minted.status_code == 200, minted.text

    done = await legal.post(
        f"/human-tasks/{task.id}/submit",
        json={
            "payload": {"reauth_token": minted.json()["token"], "note": "badge is current"},
            "artifacts_confirmed": [],
            "reference": "CERT-2026-11",
        },
    )
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["status"] == "completed"
    assert body["can_submit"] is False
    # §16 rule 6: no token, and no identifier of one, reaches a browser.
    assert "reauth_token" not in body["submitted_payload"]
    assert "reauth_token_id" not in body["submitted_payload"]
    assert body["submitted_payload"]["reference"] == "CERT-2026-11"

    # Single-use: the same proof cannot complete a second task.
    again = await _open_task(db, project_id=project_id, assignee_id=legal_id, task_key="H2b")
    replay = await legal.post(
        f"/human-tasks/{again.id}/submit",
        json={
            "payload": {"reauth_token": minted.json()["token"]},
            "artifacts_confirmed": [],
        },
    )
    assert replay.status_code == 401, replay.text


async def test_reassign_preview_counts_exactly_what_it_will_void(
    admin: ApiClient, db: AsyncSession, project_id: uuid.UUID
) -> None:
    """§15.3 C.6: the dialog states the exact number before confirmation."""
    await cast(admin)
    _guideline_id, claims = await seed_register(admin, db, project_id)
    legal_id = await user_id_of(db, "legal@example.com")
    other_id = await user_id_of(db, "other@example.com")
    signature_id = await _sign(db, project_id=project_id, signer_id=legal_id, claims=claims)
    claim_ids = [claim.id for claim in claims]
    task = await _open_task(db, project_id=project_id, assignee_id=legal_id, task_key="H1")

    preview = await admin.get(f"/human-tasks/{task.id}/reassign-preview?to_user_id={other_id}")
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["voided_count"] == 1
    assert body["requeued_count"] == len(claim_ids)
    assert body["voided_signature_ids"] == [str(signature_id)]
    assert body["to_user_name"]

    # And the write matches the preview exactly.
    done = await admin.post(
        f"/human-tasks/{task.id}/reassign",
        json={"to_user_id": str(other_id), "reason": "legal owner left the company"},
    )
    assert done.status_code == 200, done.text
    assert done.json()["voided_count"] == body["voided_count"]
    assert done.json()["requeued_count"] == body["requeued_count"]

    await db.rollback()
    voided = (
        await db.execute(sa.select(ClaimSignature).where(ClaimSignature.id == signature_id))
    ).scalar_one()
    assert voided.voided_at is not None
    assert "legal owner left the company" in (voided.void_reason or "")
    requeued = (
        (await db.execute(sa.select(ClaimRecord).where(ClaimRecord.id.in_(claim_ids))))
        .scalars()
        .all()
    )
    assert all(row.status is ClaimStatus.PENDING_SIGNOFF for row in requeued)
    assert all(row.current_signature_id is None for row in requeued)


async def test_reassigning_an_h2_voids_nothing_and_says_so(
    admin: ApiClient, db: AsyncSession, project_id: uuid.UUID
) -> None:
    """An attestation licenses nothing, so zero is the honest count, not a hidden dialog."""
    await cast(admin)
    _guideline_id, claims = await seed_register(admin, db, project_id)
    legal_id = await user_id_of(db, "legal@example.com")
    other_id = await user_id_of(db, "other@example.com")
    await _sign(db, project_id=project_id, signer_id=legal_id, claims=claims)
    task = await _open_task(db, project_id=project_id, assignee_id=legal_id, task_key="H2")

    preview = await admin.get(f"/human-tasks/{task.id}/reassign-preview?to_user_id={other_id}")
    assert preview.status_code == 200, preview.text
    assert preview.json()["voided_count"] == 0
    assert preview.json()["requeued_count"] == 0


# ---------------------------------------------------------------------------
# the sign-off matrix
# ---------------------------------------------------------------------------


async def test_a_project_with_no_matrix_answers_200_not_404(
    admin: ApiClient, project_id: uuid.UUID
) -> None:
    """ "G6 is not decided yet" is a normal state, and the screen must render it."""
    state = await admin.get(f"/projects/{project_id}/signoff-matrix")
    assert state.status_code == 200, state.text
    assert state.json()["matrix"] is None


async def test_only_an_approver_is_offered_as_legal_owner(
    admin: ApiClient, db: AsyncSession, project_id: uuid.UUID
) -> None:
    """Law 23 in the payload: a form that offered an admin would offer an impossibility."""
    await cast(admin)
    state = await admin.get(f"/projects/{project_id}/signoff-matrix")
    assert state.status_code == 200, state.text
    body = state.json()
    legal_emails = {slot["email"] for slot in body["eligible_legal_owners"]}
    all_emails = {slot["email"] for slot in body["eligible_owners"]}
    assert "legal@example.com" in legal_emails
    assert "ops@example.com" in all_emails
    assert "ops@example.com" not in legal_emails
    assert all(slot["role"] == "approver" for slot in body["eligible_legal_owners"])


async def test_changing_the_legal_owner_needs_a_reason_and_voids_what_it_said(
    admin: ApiClient, db: AsyncSession, project_id: uuid.UUID
) -> None:
    people = await cast(admin)
    _guideline_id, claims = await seed_register(admin, db, project_id)
    legal_id = await user_id_of(db, "legal@example.com")
    other_id = await user_id_of(db, "other@example.com")
    admin_id = uuid.UUID((await admin.get("/auth/me")).json()["id"])
    signature_id = await _sign(db, project_id=project_id, signer_id=legal_id, claims=claims)

    preview = await admin.get(
        f"/projects/{project_id}/signoff-matrix/preview?legal_owner_id={other_id}"
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["voided_count"] == 1
    assert preview.json()["reason_required"] is True

    refused = await admin.put(
        f"/projects/{project_id}/signoff-matrix",
        json={
            "brand_owner_id": str(admin_id),
            "legal_owner_id": str(other_id),
            "performance_owner_id": str(admin_id),
        },
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["title"] == "Reason required"

    await db.rollback()
    still_live = (
        await db.execute(sa.select(ClaimSignature).where(ClaimSignature.id == signature_id))
    ).scalar_one()
    assert still_live.voided_at is None, "a refused write must void nothing"

    accepted = await admin.put(
        f"/projects/{project_id}/signoff-matrix",
        json={
            "brand_owner_id": str(admin_id),
            "legal_owner_id": str(other_id),
            "performance_owner_id": str(admin_id),
            "reason": "handing legal review to the new counsel",
        },
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["matrix"]["version"] == 2
    assert accepted.json()["matrix"]["legal_owner"]["email"] == "other@example.com"

    await db.rollback()
    now_void = (
        await db.execute(sa.select(ClaimSignature).where(ClaimSignature.id == signature_id))
    ).scalar_one()
    assert now_void.voided_at is not None
    # Exactly one current matrix: the partial unique index, observed.
    current = (
        (
            await db.execute(
                sa.select(SignOffMatrix).where(
                    SignOffMatrix.project_id == project_id,
                    SignOffMatrix.superseded_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(current) == 1
    assert people  # the cast is what made two approvers available


async def test_naming_a_non_approver_as_legal_owner_is_refused(
    admin: ApiClient, db: AsyncSession, project_id: uuid.UUID
) -> None:
    await cast(admin)
    admin_id = uuid.UUID((await admin.get("/auth/me")).json()["id"])
    ops_id = await user_id_of(db, "ops@example.com")

    refused = await admin.put(
        f"/projects/{project_id}/signoff-matrix",
        json={
            "brand_owner_id": str(admin_id),
            "legal_owner_id": str(ops_id),
            "performance_owner_id": str(admin_id),
            "reason": "trying it on",
        },
    )
    assert refused.status_code == 422, refused.text
    assert "operator" in refused.json()["detail"]


async def test_an_unchanged_matrix_writes_no_new_version(
    admin: ApiClient, db: AsyncSession, project_id: uuid.UUID
) -> None:
    """Re-saving the same owners must not burn a version number."""
    await cast(admin)
    await seed_register(admin, db, project_id)
    legal_id = await user_id_of(db, "legal@example.com")
    admin_id = uuid.UUID((await admin.get("/auth/me")).json()["id"])

    again = await admin.put(
        f"/projects/{project_id}/signoff-matrix",
        json={
            "brand_owner_id": str(admin_id),
            "legal_owner_id": str(legal_id),
            "performance_owner_id": str(admin_id),
        },
    )
    assert again.status_code == 200, again.text
    assert again.json()["matrix"]["version"] == 1


async def test_a_viewer_cannot_change_the_matrix(
    admin: ApiClient, db: AsyncSession, project_id: uuid.UUID
) -> None:
    from tests.integration.conftest import make_member

    viewer_creds = await make_member(admin, "viewer", email="viewer@example.com")
    viewer = await as_client(viewer_creds)
    admin_id = uuid.UUID((await admin.get("/auth/me")).json()["id"])

    readable = await viewer.get(f"/projects/{project_id}/signoff-matrix")
    assert readable.status_code == 200, readable.text
    assert readable.json()["can_edit"] is False

    refused = await viewer.put(
        f"/projects/{project_id}/signoff-matrix",
        json={
            "brand_owner_id": str(admin_id),
            "legal_owner_id": str(admin_id),
            "performance_owner_id": str(admin_id),
        },
    )
    assert refused.status_code == 403, refused.text


# ---------------------------------------------------------------------------
# amendments
# ---------------------------------------------------------------------------


async def _amendment(
    db: AsyncSession,
    project_id: uuid.UUID,
    *,
    kind: AmendmentChangeKind = AmendmentChangeKind.SUBSTANTIVE,
    status: AmendmentStatus = AmendmentStatus.NEEDS_REVIEW,
) -> PolicyAmendment:
    run = (await db.execute(sa.select(Run).where(Run.project_id == project_id))).scalars().first()
    assert run is not None
    row = PolicyAmendment(
        workspace_id=run.workspace_id,
        project_id=project_id,
        origin=AmendmentOrigin.POLICY_WATCH,
        change_kind=kind,
        status=status,
        diff={"added": ["a new prohibition"]},
        rationale="Google added a clause.",
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def test_the_inbox_is_readable_by_all_and_decidable_by_the_publisher(
    admin: ApiClient, db: AsyncSession, project_id: uuid.UUID
) -> None:
    people = await cast(admin)
    await seed_register(admin, db, project_id)
    row = await _amendment(db, project_id)

    operator = await as_client(people["operator"])
    seen = await operator.get("/policy-amendments")
    assert seen.status_code == 200, seen.text
    item = next(entry for entry in seen.json()["items"] if entry["id"] == str(row.id))
    # An operator reads it and cannot act on it.
    assert item["can_decide"] is False
    assert seen.json()["open_count"] == 1

    refused = await operator.post(f"/policy-amendments/{row.id}/dismiss", json={"reason": "no"})
    assert refused.status_code == 403, refused.text

    approver = await as_client(people["legal"])
    theirs = await approver.get("/policy-amendments")
    decidable = next(entry for entry in theirs.json()["items"] if entry["id"] == str(row.id))
    assert decidable["can_decide"] is True


async def test_an_auto_applied_amendment_offers_no_control_and_refuses_reapply(
    admin: ApiClient, db: AsyncSession, project_id: uuid.UUID
) -> None:
    """A `mechanical` row applied itself; re-applying would mint a version nothing caused."""
    people = await cast(admin)
    await seed_register(admin, db, project_id)
    row = await _amendment(
        db,
        project_id,
        kind=AmendmentChangeKind.MECHANICAL,
        status=AmendmentStatus.AUTO_APPLIED,
    )

    approver = await as_client(people["legal"])
    listed = await approver.get("/policy-amendments")
    item = next(entry for entry in listed.json()["items"] if entry["id"] == str(row.id))
    assert item["can_decide"] is False
    assert listed.json()["open_count"] == 0

    refused = await approver.post(f"/policy-amendments/{row.id}/apply")
    assert refused.status_code == 409, refused.text
    assert "already been applied" in refused.json()["detail"]


async def test_dismissing_requires_a_reason_and_records_it(
    admin: ApiClient, db: AsyncSession, project_id: uuid.UUID
) -> None:
    people = await cast(admin)
    await seed_register(admin, db, project_id)
    row = await _amendment(db, project_id)
    approver = await as_client(people["legal"])

    empty = await approver.post(f"/policy-amendments/{row.id}/dismiss", json={"reason": ""})
    assert empty.status_code == 422, empty.text

    done = await approver.post(
        f"/policy-amendments/{row.id}/dismiss",
        json={"reason": "the clause does not apply to our verticals"},
    )
    assert done.status_code == 200, done.text
    assert done.json()["amendment"]["status"] == "dismissed"
    assert done.json()["amendment"]["review_note"].startswith("the clause does not apply")
    assert done.json()["amendment"]["reviewed_by_name"]


async def test_a_signature_affecting_row_names_the_signatures_it_voided(
    admin: ApiClient, db: AsyncSession, project_id: uuid.UUID
) -> None:
    """§15.3 G: names, not a count. A number cannot tell you whose signature went."""
    await cast(admin)
    _guideline_id, claims = await seed_register(admin, db, project_id)
    legal_id = await user_id_of(db, "legal@example.com")
    signature_id = await _sign(db, project_id=project_id, signer_id=legal_id, claims=claims)

    row = await _amendment(db, project_id, kind=AmendmentChangeKind.SIGNATURE_AFFECTING)
    # What `_signature_affecting` leaves behind, reproduced.
    await db.execute(
        sa.update(ClaimSignature)
        .where(ClaimSignature.id == signature_id)
        .values(voided_at=sa.func.now(), void_reason="voided by policy amendment")
    )
    await db.execute(
        sa.update(ClaimRecord)
        .where(ClaimRecord.id.in_([claim.id for claim in claims]))
        .values(status=ClaimStatus.PENDING_SIGNOFF, current_signature_id=None)
    )
    row.voided_signature_ids = [signature_id]
    await db.commit()

    listed = await admin.get(f"/policy-amendments?project_id={project_id}")
    assert listed.status_code == 200, listed.text
    item = next(entry for entry in listed.json()["items"] if entry["id"] == str(row.id))
    assert item["change_kind"] == "signature_affecting"
    assert len(item["voided_signatures"]) == 1
    named = item["voided_signatures"][0]
    assert named["signer_name"]
    assert named["claim_count"] == len(claims)
    assert sorted(item["requeued_claim_ids"]) == sorted(str(c.id) for c in claims)


# ---------------------------------------------------------------------------
# the linter playground
# ---------------------------------------------------------------------------


async def test_a_viewer_can_lint_and_the_route_writes_nothing(
    admin: ApiClient, db: AsyncSession, project_id: uuid.UUID
) -> None:
    """§15.3 F: open to every role, because it has no side effects."""
    from tests.integration.conftest import make_member

    await cast(admin)
    guideline_id, _claims = await seed_register(admin, db, project_id)
    viewer = await as_client(await make_member(admin, "viewer", email="viewer@example.com"))

    before = (await db.execute(sa.select(sa.func.count()).select_from(ClaimRecord))).scalar()

    response = await viewer.post(
        f"/guidelines/{guideline_id}/lint",
        json={
            "targets": [
                {
                    "ref": "headline-1",
                    "surface": "rsa_headline",
                    "campaign_type": "search",
                    "market": "DE",
                    "language": "en",
                    "text": "The best SDS software",
                }
            ]
        },
    )
    # Either a verdict, or an honest 409 that no rules have compiled yet — never
    # a 403, and never a 500.
    assert response.status_code in (200, 409), response.text
    if response.status_code == 200:
        body = response.json()
        assert body["result"]["verdict"] in ("pass", "pass_with_warnings", "fail")
        assert body["result"]["ruleset_version"]

    after = (await db.execute(sa.select(sa.func.count()).select_from(ClaimRecord))).scalar()
    assert before == after, "the linter must write nothing"


async def test_lint_refuses_an_empty_target_list(
    admin: ApiClient, db: AsyncSession, project_id: uuid.UUID
) -> None:
    await cast(admin)
    guideline_id, _claims = await seed_register(admin, db, project_id)
    response = await admin.post(f"/guidelines/{guideline_id}/lint", json={"targets": []})
    assert response.status_code == 422, response.text


# ---------------------------------------------------------------------------
# the anti-race hash — the regression this phase exists to have caught
# ---------------------------------------------------------------------------


async def test_a_set_with_a_rejection_in_it_can_actually_be_signed(
    admin: ApiClient, db: AsyncSession, project_id: uuid.UUID
) -> None:
    """A partial approval is "legal and common". It must not read as a race.

    The defect this pins: `ClaimList.set_hash` was the hash of *approve
    everything outstanding*, while the sign route recomputed over the decisions
    actually sent. The two agreed only when every claim was approved — so the
    moment a signer rejected one, the server answered `409 Register has moved`
    about a register nothing had touched, and the signature could never be
    recorded.

    Invisible to the suite before this, because every existing test signs an
    all-approved set, which is the one case where the two values coincide.
    """
    people = await cast(admin)
    guideline_id, claims = await seed_register(admin, db, project_id)
    legal = await as_client(people["legal"])

    listed = await legal.get(f"/guidelines/{guideline_id}/claims")
    assert listed.status_code == 200, listed.text
    set_hash = listed.json()["set_hash"]
    assert set_hash, "the register must hand out a hash to sign against"

    minted = await legal.post("/auth/reauth", json={"password": people["legal"][1]})
    assert minted.status_code == 200, minted.text

    # Read off the ORM rows once, before anything else touches the session:
    # a later commit expires them and the attribute access would then try to
    # refresh from inside a sync context.
    snapshot = [(claim.id, claim.normalized_text) for claim in claims]

    # Reject the first, approve the rest, and edit one expiry — §21 A1 verbatim.
    decisions = [
        {
            "claim_id": str(claim_id),
            "normalized_text": normalized,
            "decision": "rejected" if index == 0 else "approved",
            "note": "not substantiated" if index == 0 else None,
            "expires_at": "2027-06-30T00:00:00Z" if index == 1 else None,
        }
        for index, (claim_id, normalized) in enumerate(snapshot)
    ]

    signed = await legal.post(
        f"/guidelines/{guideline_id}/claims/sign",
        json={
            "decisions": decisions,
            "statement": "I confirm these claims are substantiated.",
            "set_hash": set_hash,
            "reauth_token": minted.json()["token"],
        },
    )
    assert signed.status_code == 200, signed.text
    receipt = signed.json()
    assert receipt["approved_count"] == len(snapshot) - 1
    assert receipt["rejected_count"] == 1

    await db.rollback()
    ids = [claim_id for claim_id, _ in snapshot]
    rows = {
        row.id: row
        for row in (
            (await db.execute(sa.select(ClaimRecord).where(ClaimRecord.id.in_(ids))))
            .scalars()
            .all()
        )
    }
    assert rows[ids[0]].status is ClaimStatus.REJECTED
    assert all(rows[claim_id].status is ClaimStatus.APPROVED for claim_id in ids[1:])


async def test_editing_the_register_under_the_signer_still_409s(
    admin: ApiClient, db: AsyncSession, project_id: uuid.UUID
) -> None:
    """The other half: the 409 must still fire when the register genuinely moves.

    Without this, the fix above could be "stop checking" and nothing would
    notice. What changed is *what* is hashed, not whether it is.
    """
    people = await cast(admin)
    guideline_id, claims = await seed_register(admin, db, project_id)
    legal = await as_client(people["legal"])

    listed = await legal.get(f"/guidelines/{guideline_id}/claims")
    stale_hash = listed.json()["set_hash"]

    # Somebody holding GUIDELINE_EXECUTE widens what the first claim licenses,
    # which is exactly the edit the hash exists to catch.
    edited = await admin.patch(
        f"/guidelines/{guideline_id}/claims/{claims[0].id}",
        json={"surface_forms": ["something wider", "and wider still"]},
    )
    assert edited.status_code == 200, edited.text

    minted = await legal.post("/auth/reauth", json={"password": people["legal"][1]})
    refused = await legal.post(
        f"/guidelines/{guideline_id}/claims/sign",
        json={
            "decisions": decisions_for(claims),
            "statement": "I confirm these claims are substantiated.",
            "set_hash": stale_hash,
            "reauth_token": minted.json()["token"],
        },
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["title"] == "Register has moved"
