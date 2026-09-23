"""Seeding a register worth signing.

S3-P3's routes are the unit under test, so these helpers put a guideline and
its claims in front of them directly rather than driving the whole 19-node DAG
to get there. The run itself is created through the real entry route, because
that is what makes the rows consistent with what the executor would have
written.
"""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import (
    ClaimRecord,
    ClaimStatus,
    ClaimType,
    ContentGuideline,
    GuidelineMode,
    GuidelineStatus,
    Run,
    SignOffMatrix,
    User,
)
from tests.integration.conftest import ApiClient, build_client, make_member

#: The copy the register is about. Claim-shaped on purpose.
CLAIMS = (
    "The best SDS software",
    "40% faster than the alternative",
    "ISO 9001 certified",
)


async def cast(admin: ApiClient) -> dict[str, tuple[str, str]]:
    """A legal owner, a second approver who is *not* the owner, and an operator.

    The second approver is the important one: law 23 narrows CLAIM_SIGN past the
    role to one identity, and a suite with only one approver could not tell the
    role check from the identity check.
    """
    return {
        "legal": await make_member(admin, "approver", email="legal@example.com"),
        "other_approver": await make_member(admin, "approver", email="other@example.com"),
        "operator": await make_member(admin, "operator", email="ops@example.com"),
    }


async def as_client(who: tuple[str, str]) -> ApiClient:
    api = build_client()
    response = await api.login(*who)
    assert response.status_code == 200, response.text
    return api


async def user_id_of(db: AsyncSession, email: str) -> uuid.UUID:
    row = (await db.execute(sa.select(User).where(User.email == email))).scalar_one()
    return row.id


async def seed_register(
    admin: ApiClient,
    db: AsyncSession,
    project_id: uuid.UUID,
    *,
    legal_email: str = "legal@example.com",
    status: ClaimStatus = ClaimStatus.PENDING_SIGNOFF,
) -> tuple[uuid.UUID, list[ClaimRecord]]:
    """A guideline with three claims awaiting signature, and a matrix naming the owner."""
    started = await admin.post(f"/projects/{project_id}/guidelines/runs", json={})
    assert started.status_code == 202, started.text
    run_id = uuid.UUID(started.json()["run_id"])

    run = (await db.execute(sa.select(Run).where(Run.id == run_id))).scalar_one()
    legal_id = await user_id_of(db, legal_email)
    admin_id = uuid.UUID((await admin.get("/auth/me")).json()["id"])

    db.add(
        SignOffMatrix(
            workspace_id=run.workspace_id,
            project_id=project_id,
            brand_owner_id=admin_id,
            legal_owner_id=legal_id,
            performance_owner_id=admin_id,
            version=1,
            set_by=admin_id,
        )
    )

    # The entry route creates the draft row now (S3-P6): `ClaimRecord.
    # first_seen_guideline_id` is NOT NULL and 3.2.3 registers claims a dozen
    # nodes before 3.6.1 exists to write a payload. `guideline_run_id` is
    # UNIQUE, so adding a second row here would abort the transaction — this
    # helper reads the one the route made, which is also what the sign route
    # will resolve.
    guideline = (
        await db.execute(
            sa.select(ContentGuideline).where(ContentGuideline.guideline_run_id == run_id)
        )
    ).scalar_one_or_none()
    if guideline is None:  # pragma: no cover — the route creates it
        guideline = ContentGuideline(
            workspace_id=run.workspace_id,
            project_id=project_id,
            guideline_run_id=run_id,
            schema_version="1.0",
            version_major=1,
            version_minor=0,
            status=GuidelineStatus.DRAFT,
            mode=GuidelineMode.STANDALONE,
            bindings={},
            unbound_inputs=[],
        )
        db.add(guideline)
    await db.flush()

    claims = [
        ClaimRecord(
            workspace_id=run.workspace_id,
            project_id=project_id,
            first_seen_guideline_id=guideline.id,
            claim_text=text,
            normalized_text=text.casefold(),
            surface_forms=[text],
            claim_type=ClaimType.SUPERLATIVE,
            market_scope=["DE"],
            languages=["en"],
            observed_on=[],
            evidence_ids=[],
            status=status,
        )
        for text in CLAIMS
    ]
    for claim in claims:
        db.add(claim)
    await db.commit()
    for claim in claims:
        await db.refresh(claim)
    return guideline.id, claims


def decisions_for(claims: list[ClaimRecord], verdict: str = "approved") -> list[dict[str, Any]]:
    """What the drawer sends back, and what it hashes.

    The licensing fields are carried because the *client* has to hash what it
    read: the server rebuilds them from the row, so a client hashing without
    them would disagree and get a 409 on every submit.
    """
    return [
        {
            "claim_id": str(claim.id),
            "normalized_text": claim.normalized_text,
            "decision": verdict,
            "surface_forms": list(claim.surface_forms or []),
            "market_scope": list(claim.market_scope or []),
            "languages": list(claim.languages or []),
        }
        for claim in claims
    ]
