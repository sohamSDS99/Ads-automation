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
    """The register hash over exactly the claims these decisions name.

    Computed here rather than read from `GET /claims`, and the distinction
    matters. `ClaimList.set_hash` covers the claims currently *signable*, which
    is the set the drawer submits and therefore the right value for a client.
    Several tests below sign a narrower set on purpose — re-deciding one claim
    that is already approved, for instance — and no endpoint offers a hash for
    an arbitrary subset. So this exercises the sign route directly.

    It used to call `set_hash`, which folds the signer's own decision into the
    value. That made it impossible for any real client to produce a matching
    hash for a partially-approved set, and because this helper never touched the
    endpoint, nothing here noticed. The end-to-end contract — list, decide,
    sign — is covered by `test_s3p8_routes`.
    """
    from agent.guidelines.signature import ClaimDecision, register_hash

    return register_hash([ClaimDecision.model_validate(d) for d in decisions])


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
    # Pre-existing from S3-P3, corrected here because it failed `ruff` (SIM222):
    # `x is not None or True` is always True, so this line asserted nothing while
    # reading like a check on the receipt. Whether an IP is recorded depends on the
    # transport supplying `request.client`, which the ASGI test client does not
    # guarantee — so the invariant that is actually always true is that the column
    # is never written as an empty string, which is what a naive
    # `request.client.host or ""` would produce.
    # `str()` because the column is `INET`: asyncpg hands back an
    # `ipaddress.IPv4Address`, not text, so `.strip()` raised AttributeError and
    # this assertion has been failing since it was written. Pre-existing on
    # main; fixed here because S3-P8 touches this file and leaving a red test in
    # it would hide the next real failure.
    assert signature.ip is None or str(signature.ip).strip()


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


async def test_editing_a_claim_normalizes_it_the_way_the_linter_will(
    admin: ApiClient, db: AsyncSession, project_id: uuid.UUID
) -> None:
    """One normalization, not two.

    `normalized_text` is the column the licence pass matches on and the column
    `set_hash` is computed over. If the PATCH route folds text its own way, an
    edited claim hashes differently from a harvested one carrying the same
    words, and the register develops two dialects — the signer's 409 then
    depends on which route last touched the row.
    """
    from agent.guardrails.normalize import normalize

    await cast(admin)
    guideline_id, claims = await seed_register(admin, db, project_id)

    # Fullwidth digits and a zero-width joiner: exactly what `normalize` exists
    # to fold and what `casefold().strip()` leaves alone.
    awkward = "Ｔｈｅ best‍ SDS software"
    response = await admin.patch(
        f"/guidelines/{guideline_id}/claims/{claims[0].id}", json={"claim_text": awkward}
    )
    assert response.status_code == 200, response.text
    assert response.json()["normalized_text"] == normalize(awkward, locale="en").text


# --- the security review's findings, as regressions -------------------------


async def test_widening_a_claim_after_the_signer_read_it_is_a_409(
    admin: ApiClient, db: AsyncSession, project_id: uuid.UUID
) -> None:
    """An operator cannot broaden what a signature licenses (finding 1).

    `licences()` matches candidate copy against `surface_forms` and scopes the
    match by `market_scope` and `languages` — and an empty market or language
    list means *every* market or language. All three are writable with
    GUIDELINE_EXECUTE, which `operator` and `admin` hold and which is exactly
    the pair denied CLAIM_SIGN. If they sat outside the hash, somebody who
    cannot sign could widen a named person's signature after that person read
    the register, and the 409 would never fire.
    """
    people = await cast(admin)
    guideline_id, claims = await seed_register(admin, db, project_id)
    legal = await as_client(people["legal"])

    # The signer reads the register and computes the hash of what they saw.
    decisions = decisions_for(claims)
    digest = await hash_of(admin, guideline_id, decisions)

    # An operator widens one claim while the drawer is open.
    operator = await as_client(people["operator"])
    widened = await operator.patch(
        f"/guidelines/{guideline_id}/claims/{claims[0].id}",
        json={"surface_forms": ["guaranteed cheapest in Europe"], "market_scope": []},
    )
    assert widened.status_code == 200, widened.text

    response = await sign(legal, guideline_id, decisions, digest)
    assert response.status_code == 409, response.text
    await db.rollback()
    assert (await db.execute(sa.select(sa.func.count()).select_from(ClaimSignature))).scalar() == 0


async def test_the_stored_decisions_are_the_servers_text_not_the_clients(
    admin: ApiClient, db: AsyncSession, project_id: uuid.UUID
) -> None:
    """The append-only record must not freeze text that was never in the register (finding 2)."""
    people = await cast(admin)
    guideline_id, claims = await seed_register(admin, db, project_id)
    legal = await as_client(people["legal"])

    decisions = decisions_for(claims)
    digest = await hash_of(admin, guideline_id, decisions)
    # Read before the rollback below expires these rows.
    expected_text = {claim.normalized_text for claim in claims}
    for entry in decisions:
        entry["normalized_text"] = "wording that was never in the register"

    response = await sign(legal, guideline_id, decisions, digest)
    assert response.status_code in (200, 201), response.text

    await db.rollback()
    signature = (await db.execute(sa.select(ClaimSignature))).scalars().one()
    stored = {entry["normalized_text"] for entry in signature.decisions}
    assert "wording that was never in the register" not in stored
    assert stored == expected_text


async def test_re_signing_after_a_reversal_takes_effect(
    admin: ApiClient, db: AsyncSession, project_id: uuid.UUID
) -> None:
    """Idempotency must not fail open on a superseded signature (finding 3).

    Reject, then approve, then reject again. The third set hashes the same as
    the first, whose signature was superseded but never voided — so matching on
    the hash alone would hand back the original receipt and leave the claim
    approved, showing the signer a success screen over a decision that never
    took effect.
    """
    people = await cast(admin)
    guideline_id, claims = await seed_register(admin, db, project_id)
    legal = await as_client(people["legal"])
    one = claims[:1]
    claim_id = one[0].id  # read before any rollback expires the row

    rejected = decisions_for(one, "rejected")
    first = await sign(legal, guideline_id, rejected, await hash_of(admin, guideline_id, rejected))
    assert first.status_code in (200, 201), first.text

    approved = decisions_for(one, "approved")
    second = await sign(legal, guideline_id, approved, await hash_of(admin, guideline_id, approved))
    assert second.status_code in (200, 201), second.text
    await db.rollback()
    row = (await db.execute(sa.select(ClaimRecord).where(ClaimRecord.id == claim_id))).scalar_one()
    assert row.status is ClaimStatus.APPROVED

    third = await sign(legal, guideline_id, rejected, await hash_of(admin, guideline_id, rejected))
    assert third.status_code in (200, 201), third.text
    await db.rollback()
    row = (await db.execute(sa.select(ClaimRecord).where(ClaimRecord.id == claim_id))).scalar_one()
    assert row.status is ClaimStatus.REJECTED, "the reversal was reported but never applied"


async def test_reauth_refuses_an_account_with_no_password_hash(
    admin: ApiClient, db: AsyncSession
) -> None:
    """The constant-time placeholder is not a password (finding 4).

    `dummy_hash()` is a real Argon2id hash of a fixed literal in this repo. Its
    job is to make an unknown email cost the same as a wrong password on the
    sign-in path, where a separate `stored_hash is not None` check stops anyone
    authenticating with it. Verifying against it here would make that literal a
    working password for any account without a hash — and this endpoint mints
    the presence layer of a non-delegable signature.
    """
    from agent.auth import passwords
    from agent.db.models import User

    me = (await admin.get("/auth/me")).json()
    await db.execute(
        sa.update(User).where(User.id == uuid.UUID(me["id"])).values(password_hash=None)
    )
    await db.commit()

    placeholder = "\x00unknown-account-constant-time-placeholder\x00"
    assert passwords.verify(passwords.dummy_hash(), placeholder), "premise: it does match"

    response = await admin.post("/auth/reauth", json={"password": placeholder})
    assert response.status_code == 401
