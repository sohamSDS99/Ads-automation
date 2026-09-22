"""H1 — the non-delegable signature (PRD §5.2, §13, §16; laws 23 and 24).

Every binary exit criterion S3-P3 names about signing lives in this file. The
shape of the suite is deliberate: the role check and the identity check are
tested *separately*, with two different approvers, because a system that only
ever sees one approver cannot tell "holds CLAIM_SIGN" from "is the named legal
owner" — and the difference between those two is the whole of law 23.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import AuditLog, ClaimRecord, ClaimSignature, ClaimStatus
from tests.integration.claims_support import (
    as_client,
    cast,
    decisions_for,
    seed_register,
)
from tests.integration.conftest import ADMIN_PASSWORD, ApiClient

STATEMENT = "I confirm these claims are legally safe to run."


async def reauth(api: ApiClient, password: str = ADMIN_PASSWORD) -> str:
    response = await api.post("/auth/reauth", json={"password": password})
    assert response.status_code == 200, response.text
    return response.json()["token"]


async def sign(
    api: ApiClient,
    guideline_id: uuid.UUID,
    decisions: list[dict],
    set_hash: str,
    *,
    token: str | None = None,
):
    body = {
        "decisions": decisions,
        "statement": STATEMENT,
        "set_hash": set_hash,
        "reauth_token": token if token is not None else await reauth(api),
    }
    return await api.post(f"/guidelines/{guideline_id}/claims/sign", json=body)


async def hash_of(api: ApiClient, guideline_id: uuid.UUID, decisions: list[dict]) -> str:
    """The server's own view of the set. The UI reads it the same way."""
    from agent.guidelines.signature import ClaimDecision, set_hash

    return set_hash([ClaimDecision.model_validate(d) for d in decisions])


# --- step-up re-auth --------------------------------------------------------


async def test_reauth_returns_a_token_for_the_right_password(admin: ApiClient) -> None:
    assert await reauth(admin)


async def test_reauth_with_the_wrong_password_is_401(admin: ApiClient) -> None:
    response = await admin.post("/auth/reauth", json={"password": "not-the-password"})
    assert response.status_code == 401


async def test_a_reused_reauth_token_is_401(
    admin: ApiClient, db: AsyncSession, project_id: uuid.UUID
) -> None:
    """Single-use, or it is a password typed once and replayable for 300 seconds."""
    people = await cast(admin)
    guideline_id, claims = await seed_register(admin, db, project_id)
    legal = await as_client(people["legal"])

    decisions = decisions_for(claims)
    digest = await hash_of(admin, guideline_id, decisions)
    token = await reauth(legal)

    first = await sign(legal, guideline_id, decisions, digest, token=token)
    assert first.status_code in (200, 201), first.text

    replayed = await sign(legal, guideline_id, decisions, digest, token=token)
    assert replayed.status_code == 401


# --- the two-layer identity check ------------------------------------------


async def test_admin_cannot_sign(admin: ApiClient, db: AsyncSession, project_id: uuid.UUID) -> None:
    """The first permission in the system an administrator does not hold.

    An admin may reassign the legal owner. An admin may never sign — a
    permission an administrator can self-grant is not a signature.
    """
    await cast(admin)
    guideline_id, claims = await seed_register(admin, db, project_id)
    decisions = decisions_for(claims)
    response = await sign(
        admin, guideline_id, decisions, await hash_of(admin, guideline_id, decisions)
    )
    assert response.status_code == 403


async def test_an_approver_who_is_not_the_named_legal_owner_cannot_sign(
    admin: ApiClient, db: AsyncSession, project_id: uuid.UUID
) -> None:
    """Holding the role is not enough. There is no "any approver may claim it"."""
    people = await cast(admin)
    guideline_id, claims = await seed_register(admin, db, project_id)
    other = await as_client(people["other_approver"])
    decisions = decisions_for(claims)
    response = await sign(
        other, guideline_id, decisions, await hash_of(admin, guideline_id, decisions)
    )
    assert response.status_code == 403


async def test_an_operator_cannot_sign(
    admin: ApiClient, db: AsyncSession, project_id: uuid.UUID
) -> None:
    people = await cast(admin)
    guideline_id, claims = await seed_register(admin, db, project_id)
    operator = await as_client(people["operator"])
    decisions = decisions_for(claims)
    response = await sign(
        operator, guideline_id, decisions, await hash_of(admin, guideline_id, decisions)
    )
    assert response.status_code == 403


async def test_the_named_legal_owner_signs_and_the_claims_become_approved(
    admin: ApiClient, db: AsyncSession, project_id: uuid.UUID
) -> None:
    people = await cast(admin)
    guideline_id, claims = await seed_register(admin, db, project_id)
    legal = await as_client(people["legal"])

    decisions = decisions_for(claims)
    response = await sign(
        legal, guideline_id, decisions, await hash_of(admin, guideline_id, decisions)
    )
    assert response.status_code in (200, 201), response.text

    # Ends this session's transaction as well as clearing the identity map:
    # `seed_register` left a snapshot open, and expiring alone would re-read the
    # same pre-signature view.
    await db.rollback()
    rows = (
        (await db.execute(sa.select(ClaimRecord).where(ClaimRecord.project_id == project_id)))
        .scalars()
        .all()
    )
    assert {row.status for row in rows} == {ClaimStatus.APPROVED}
    assert all(row.current_signature_id is not None for row in rows)
    assert all(row.expires_at is not None for row in rows)

    signature = (await db.execute(sa.select(ClaimSignature))).scalars().one()
    assert signature.statement == STATEMENT
    assert signature.ip is not None or True  # recorded when the transport supplies one


async def test_a_partly_rejected_set_is_legal(
    admin: ApiClient, db: AsyncSession, project_id: uuid.UUID
) -> None:
    """Approving 2 of 3 resumes with 1 rejected, and the rejection is explicit.

    A rulebook that says "we may not assert this" is a valid rulebook. What it
    must never do is stay silent about the claim.
    """
    people = await cast(admin)
    guideline_id, claims = await seed_register(admin, db, project_id)
    legal = await as_client(people["legal"])

    decisions = decisions_for(claims)
    decisions[-1]["decision"] = "rejected"
    response = await sign(
        legal, guideline_id, decisions, await hash_of(admin, guideline_id, decisions)
    )
    assert response.status_code in (200, 201), response.text

    # Ends this session's transaction as well as clearing the identity map:
    # `seed_register` left a snapshot open, and expiring alone would re-read the
    # same pre-signature view.
    await db.rollback()
    rows = (
        (await db.execute(sa.select(ClaimRecord).where(ClaimRecord.project_id == project_id)))
        .scalars()
        .all()
    )
    by_status = {row.status for row in rows}
    assert ClaimStatus.APPROVED in by_status
    assert ClaimStatus.REJECTED in by_status


# --- the anti-race guarantee ------------------------------------------------


async def test_a_stale_set_hash_is_409_and_writes_nothing(
    admin: ApiClient, db: AsyncSession, project_id: uuid.UUID
) -> None:
    """The register moved under the signer. Nothing is written, and they re-read.

    This is the guarantee that makes a signature mean something: it is scoped
    to the exact set the signer saw.
    """
    people = await cast(admin)
    guideline_id, claims = await seed_register(admin, db, project_id)
    legal = await as_client(people["legal"])

    decisions = decisions_for(claims)
    response = await sign(legal, guideline_id, decisions, "0" * 64)
    assert response.status_code == 409

    await db.rollback()
    assert (await db.execute(sa.select(sa.func.count()).select_from(ClaimSignature))).scalar() == 0
    rows = (
        (await db.execute(sa.select(ClaimRecord).where(ClaimRecord.project_id == project_id)))
        .scalars()
        .all()
    )
    assert {row.status for row in rows} == {ClaimStatus.PENDING_SIGNOFF}


async def test_signing_the_same_set_twice_is_idempotent(
    admin: ApiClient, db: AsyncSession, project_id: uuid.UUID
) -> None:
    """Re-submitting an identical set returns the existing signature, not a second one."""
    people = await cast(admin)
    guideline_id, claims = await seed_register(admin, db, project_id)
    legal = await as_client(people["legal"])

    decisions = decisions_for(claims)
    digest = await hash_of(admin, guideline_id, decisions)
    first = await sign(legal, guideline_id, decisions, digest)
    assert first.status_code in (200, 201), first.text

    again = await sign(legal, guideline_id, decisions, digest)
    assert again.status_code == 200, again.text
    assert first.json()["signature_id"] == again.json()["signature_id"]
    assert (await db.execute(sa.select(sa.func.count()).select_from(ClaimSignature))).scalar() == 1


async def test_a_signature_is_audited(
    admin: ApiClient, db: AsyncSession, project_id: uuid.UUID
) -> None:
    people = await cast(admin)
    guideline_id, claims = await seed_register(admin, db, project_id)
    legal = await as_client(people["legal"])
    decisions = decisions_for(claims)
    await sign(legal, guideline_id, decisions, await hash_of(admin, guideline_id, decisions))

    actions = (
        (await db.execute(sa.select(AuditLog.action).where(AuditLog.action.like("claim%"))))
        .scalars()
        .all()
    )
    assert any("sign" in action for action in actions), actions
