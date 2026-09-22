"""The claims register, and the one signature an administrator cannot give.

Law 23 is enforced here in three layers, and all three are required:

1. **Role.** `require(Permission.CLAIM_SIGN)` — held by `approver` and, uniquely
   in this system, *not* by `admin`.
2. **Identity.** The caller must be the project's named `legal_owner`. There is
   no "any approver may claim it" fallback, which is exactly the fallback an
   ordinary gate permits.
3. **Presence.** A single-use step-up proof, minted against the current password
   within the last `SIGNATURE_REAUTH_TTL_SECONDS`.

On top of those, `set_hash` scopes the signature to the set the signer actually
read. The server recomputes it from its *own* view of the register — never from
the text the client sent — so a claim edited between the read and the submit
produces a mismatch and a 409 that writes nothing.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Depends, Request, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from agent.api import problems
from agent.api.middleware import client_ip, user_agent
from agent.api.schemas_claims import (
    ClaimList,
    ClaimPatch,
    ClaimSummary,
    RevokeRequest,
    SignatureReceipt,
    SignRequest,
)
from agent.audit import AuditAction, AuditTarget, write_audit
from agent.auth.deps import Principal, get_redis_client, require
from agent.auth.rbac import Permission
from agent.config import Settings, get_settings
from agent.db.models import (
    ClaimRecord,
    ClaimSignature,
    ClaimStatus,
    ContentGuideline,
    SignatureMethod,
    SignOffMatrix,
)
from agent.db.session import get_session
from agent.guardrails.normalize import normalize
from agent.guidelines.constants import load_content_constants
from agent.guidelines.signature import ClaimDecision, ReauthError, ReauthTokens, set_hash

log = structlog.get_logger(__name__)

router = APIRouter(tags=["claims"])

Db = Annotated[AsyncSession, Depends(get_session)]
RedisDep = Annotated[Redis, Depends(get_redis_client)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
AnyMember = Annotated[Principal, Depends(require(Permission.READ))]
Editor = Annotated[Principal, Depends(require(Permission.GUIDELINE_EXECUTE))]
Signer = Annotated[Principal, Depends(require(Permission.CLAIM_SIGN))]

#: Statuses a claim may be in and still be signable. A claim already carrying a
#: live decision is re-signed only by way of a new set, never edited in place.
SIGNABLE = (ClaimStatus.UNSUPPORTED, ClaimStatus.PENDING_SIGNOFF, ClaimStatus.EXPIRED)


# ---------------------------------------------------------------------------
# reading the register
# ---------------------------------------------------------------------------


@router.get(
    "/guidelines/{guideline_id}/claims",
    response_model=ClaimList,
    summary="The claims register for one guideline's project",
)
async def list_claims(guideline_id: uuid.UUID, me: AnyMember, db: Db) -> ClaimList:
    guideline = await _guideline(db, guideline_id, me)
    claims = await _claims_of(db, guideline)
    matrix = await _matrix(db, guideline.project_id)
    pending = [claim for claim in claims if claim.status in SIGNABLE]
    return ClaimList(
        claims=[ClaimSummary.model_validate(claim) for claim in claims],
        set_hash=_hash_of(pending) if pending else None,
        legal_owner_id=matrix.legal_owner_id if matrix is not None else None,
    )


@router.patch(
    "/guidelines/{guideline_id}/claims/{claim_id}",
    response_model=ClaimSummary,
    summary="Edit a claim draft before anybody signs it",
)
async def edit_claim(
    guideline_id: uuid.UUID,
    claim_id: uuid.UUID,
    body: ClaimPatch,
    me: Editor,
    request: Request,
    db: Db,
) -> ClaimSummary:
    """Editable only while unsigned.

    A claim carrying a live signature describes something a named person put
    their name to. Editing it in place would leave the signature attached to
    wording nobody agreed to, so it is refused rather than versioned here.
    """
    guideline = await _guideline(db, guideline_id, me)
    claim = await _claim(db, claim_id, guideline)
    if claim.current_signature_id is not None or claim.status is ClaimStatus.APPROVED:
        raise problems.Problem(
            status_code=status.HTTP_409_CONFLICT,
            title="Claim is signed",
            detail=(
                "This claim carries a signature. Revoke it first — editing the wording "
                "under a signature would leave a named person attached to text they "
                "never read."
            ),
            type_=problems.TYPE_CONFLICT,
        )

    if body.claim_text is not None:
        claim.claim_text = body.claim_text
        # The same `normalize` node 3.2.1 uses and the same one the linter's
        # licence pass applies. A second folding here would give the register
        # two dialects: an edited claim would hash differently from a harvested
        # one carrying the same words, and whether a signer got a 409 would
        # depend on which route last touched the row.
        claim.normalized_text = normalize(
            body.claim_text, locale=(claim.languages or ["en"])[0]
        ).text
    for field in ("surface_forms", "market_scope", "languages"):
        value = getattr(body, field)
        if value is not None:
            setattr(claim, field, value)

    write_audit(
        db,
        workspace_id=me.workspace_id,
        actor_id=me.user.id,
        action=AuditAction.CLAIM_EDITED,
        target_type=AuditTarget.CLAIM,
        target_id=claim.id,
        ip=client_ip(request),
    )
    await db.commit()
    await db.refresh(claim)
    return ClaimSummary.model_validate(claim)


# ---------------------------------------------------------------------------
# H1 — the signature
# ---------------------------------------------------------------------------


@router.post(
    "/guidelines/{guideline_id}/claims/sign",
    response_model=SignatureReceipt,
    summary="Sign a claim set (the named legal owner only)",
)
async def sign_claims(
    guideline_id: uuid.UUID,
    body: SignRequest,
    me: Signer,
    request: Request,
    db: Db,
    redis: RedisDep,
    settings: SettingsDep,
) -> SignatureReceipt:
    """Record which claims a named person says are safe to run.

    The order of the checks below is deliberate. Identity is asserted before the
    step-up token is spent, so somebody who is not the legal owner cannot burn a
    proof; the register is re-hashed before anything is written, so a 409 leaves
    the database untouched.
    """
    guideline = await _guideline(db, guideline_id, me)

    # -- layer 2: identity ---------------------------------------------------
    matrix = await _matrix(db, guideline.project_id)
    if matrix is None:
        raise problems.Problem(
            status_code=status.HTTP_409_CONFLICT,
            title="No sign-off matrix",
            detail=(
                "This project names no legal owner, so there is nobody a signature "
                "could be routed to. Decide G6 first."
            ),
            type_=problems.TYPE_CONFLICT,
        )
    if matrix.legal_owner_id != me.user.id:
        # Deliberately not 404: the caller may legitimately read this register.
        # What they may not do is sign it, and saying so plainly is what stops
        # somebody assuming the button is broken.
        raise problems.Problem(
            status_code=status.HTTP_403_FORBIDDEN,
            title="Not the named legal owner",
            detail=(
                "Only the person named as this project's legal owner may sign its "
                "claims. Holding the approver role is not enough, and an administrator "
                "cannot sign at all."
            ),
            type_=problems.TYPE_FORBIDDEN,
        )

    # -- the set, as the server sees it -------------------------------------
    wanted = [entry.claim_id for entry in body.decisions]
    if len(set(wanted)) != len(wanted):
        raise problems.Problem(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            title="Duplicate claim",
            detail="A claim appears twice in this set. One decision per claim.",
            type_=problems.TYPE_VALIDATION,
        )

    claims = {claim.id: claim for claim in await _claims_of(db, guideline, only=wanted)}
    missing = [str(cid) for cid in wanted if cid not in claims]
    if missing:
        raise problems.Problem(
            status_code=status.HTTP_409_CONFLICT,
            title="Register has moved",
            detail=(
                f"{len(missing)} claim(s) in this set are no longer in the register. "
                "Re-read it and sign again."
            ),
            type_=problems.TYPE_CONFLICT,
        )

    # Built from the server's `normalized_text`, never from the client's. That
    # is the whole mechanism: if the wording changed after the signer read it,
    # the hashes disagree and nothing is written.
    server_view = [
        ClaimDecision(
            claim_id=entry.claim_id,
            normalized_text=claims[entry.claim_id].normalized_text,
            decision=entry.decision,
            note=entry.note,
            expires_at=entry.expires_at,
        )
        for entry in body.decisions
    ]
    recomputed = set_hash(server_view)

    if recomputed != body.set_hash:
        # Before the token is spent, so a signer who has to re-read the register
        # is not also made to re-type their password for an attempt that wrote
        # nothing.
        raise problems.Problem(
            status_code=status.HTTP_409_CONFLICT,
            title="Register has moved",
            detail=(
                "The claims register changed after you read it, so this signature was "
                "not recorded. Re-read the register and sign again."
            ),
            type_=problems.TYPE_CONFLICT,
        )

    # -- layer 3: presence ---------------------------------------------------
    #
    # Spent *before* the idempotency check, and the order is the whole point.
    # PRD §16 rule 2 wants an identical resubmit to return the existing
    # signature; CS2 wants a reused token to be 401. Checking idempotency first
    # satisfies the former by defeating the latter — a captured request could be
    # replayed forever and never reach the single-use check. Spending the token
    # first keeps both: a genuine retry mints a fresh proof (which the drawer
    # does on every submit anyway) and still gets the original signature back.
    tokens = ReauthTokens(redis, ttl_seconds=settings.signature_reauth_ttl_seconds)
    try:
        token_id = await tokens.consume(me.user.id, body.reauth_token)
    except ReauthError as exc:
        raise problems.Problem(
            status_code=status.HTTP_401_UNAUTHORIZED,
            title="Step-up required",
            detail=f"{exc}. Confirm your password and sign again.",
            type_=problems.TYPE_UNAUTHENTICATED,
        ) from exc

    existing = await _signature_by_hash(db, guideline.project_id, recomputed)
    if existing is not None and existing.voided_at is None:
        # Idempotent on the set hash (PRD §16 rule 2): a retried submit — a
        # flaky connection, a double click — returns the signature that already
        # exists rather than minting a second one over identical material.
        return _receipt(existing)

    now = datetime.now(UTC)
    signature = ClaimSignature(
        workspace_id=me.workspace_id,
        project_id=guideline.project_id,
        signer_id=me.user.id,
        claim_ids=list(wanted),
        set_hash=recomputed,
        decisions=[entry.model_dump(mode="json") for entry in body.decisions],
        statement=body.statement,
        method=SignatureMethod.STEP_UP_PASSWORD,
        reauth_token_id=token_id,
        ip=client_ip(request),
        user_agent=user_agent(request),
        signed_at=now,
        expires_at=_signature_expiry(now),
    )
    db.add(signature)
    await db.flush()

    approved = rejected = 0
    for entry in body.decisions:
        claim = claims[entry.claim_id]
        if entry.decision == "approved":
            approved += 1
            claim.status = ClaimStatus.APPROVED
            claim.current_signature_id = signature.id
            claim.expires_at = entry.expires_at or _claim_expiry(claim, now)
        else:
            rejected += 1
            # A rejected claim keeps the signature that rejected it: the
            # register has to be able to show who said no, and when.
            claim.status = ClaimStatus.REJECTED
            claim.current_signature_id = signature.id
            claim.expires_at = None

    write_audit(
        db,
        workspace_id=me.workspace_id,
        actor_id=me.user.id,
        action=AuditAction.CLAIM_SIGNED,
        target_type=AuditTarget.CLAIM_SIGNATURE,
        target_id=signature.id,
        meta={
            "guideline_id": str(guideline.id),
            "set_hash": recomputed,
            "approved": approved,
            "rejected": rejected,
        },
        ip=client_ip(request),
    )
    await db.commit()
    await db.refresh(signature)
    log.info(
        "claims.signed",
        signature_id=str(signature.id),
        approved=approved,
        rejected=rejected,
    )
    return _receipt(signature)


@router.post(
    "/claims/{claim_id}/revoke",
    response_model=SignatureReceipt,
    summary="Withdraw a signature (the signer only)",
)
async def revoke_signature(
    claim_id: uuid.UUID,
    body: RevokeRequest,
    me: Signer,
    request: Request,
    db: Db,
) -> SignatureReceipt:
    """Void the signature covering a claim and send it back for signing.

    Voiding is a write to the three void columns and nothing else — the row is
    append-only, and a correction is a new signature rather than an edit.
    """
    claim = (
        await db.execute(
            sa.select(ClaimRecord).where(
                ClaimRecord.id == claim_id, ClaimRecord.workspace_id == me.workspace_id
            )
        )
    ).scalar_one_or_none()
    if claim is None:
        raise problems.not_found(f"No claim {claim_id}.")
    if claim.current_signature_id is None:
        raise problems.Problem(
            status_code=status.HTTP_409_CONFLICT,
            title="Nothing to revoke",
            detail="This claim carries no signature.",
            type_=problems.TYPE_CONFLICT,
        )

    signature = (
        await db.execute(
            sa.select(ClaimSignature).where(ClaimSignature.id == claim.current_signature_id)
        )
    ).scalar_one()
    if signature.signer_id != me.user.id:
        raise problems.Problem(
            status_code=status.HTTP_403_FORBIDDEN,
            title="Not your signature",
            detail="Only the person who signed may revoke it.",
            type_=problems.TYPE_FORBIDDEN,
        )
    if signature.voided_at is not None:
        return _receipt(signature)

    now = datetime.now(UTC)
    signature.voided_at = now
    signature.voided_by = me.user.id
    signature.void_reason = body.reason

    # Every claim this signature covered goes back for signing, not just the one
    # the caller named: the signature was over a set, and half of it surviving
    # would mean claims licensed by a signature that no longer exists.
    covered = (
        (
            await db.execute(
                sa.select(ClaimRecord).where(ClaimRecord.current_signature_id == signature.id)
            )
        )
        .scalars()
        .all()
    )
    for row in covered:
        row.status = ClaimStatus.PENDING_SIGNOFF
        row.current_signature_id = None
        row.expires_at = None

    write_audit(
        db,
        workspace_id=me.workspace_id,
        actor_id=me.user.id,
        action=AuditAction.CLAIM_SIGNATURE_REVOKED,
        target_type=AuditTarget.CLAIM_SIGNATURE,
        target_id=signature.id,
        meta={"reason": body.reason, "claims_returned": len(covered)},
        ip=client_ip(request),
    )
    await db.commit()
    await db.refresh(signature)
    return _receipt(signature)


@router.get(
    "/claims/{claim_id}/signature",
    response_model=SignatureReceipt,
    summary="The signature receipt for one claim",
)
async def claim_signature(claim_id: uuid.UUID, me: AnyMember, db: Db) -> SignatureReceipt:
    claim = (
        await db.execute(
            sa.select(ClaimRecord).where(
                ClaimRecord.id == claim_id, ClaimRecord.workspace_id == me.workspace_id
            )
        )
    ).scalar_one_or_none()
    if claim is None or claim.current_signature_id is None:
        raise problems.not_found(f"No signature for claim {claim_id}.")
    signature = (
        await db.execute(
            sa.select(ClaimSignature).where(ClaimSignature.id == claim.current_signature_id)
        )
    ).scalar_one()
    return _receipt(signature)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _guideline(db: AsyncSession, guideline_id: uuid.UUID, me: Principal) -> ContentGuideline:
    row = (
        await db.execute(
            sa.select(ContentGuideline).where(
                ContentGuideline.id == guideline_id,
                ContentGuideline.workspace_id == me.workspace_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise problems.not_found(f"No guideline {guideline_id}.")
    return row


async def _claim(db: AsyncSession, claim_id: uuid.UUID, guideline: ContentGuideline) -> ClaimRecord:
    row = (
        await db.execute(
            sa.select(ClaimRecord).where(
                ClaimRecord.id == claim_id,
                ClaimRecord.project_id == guideline.project_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise problems.not_found(f"No claim {claim_id}.")
    return row


async def _claims_of(
    db: AsyncSession, guideline: ContentGuideline, *, only: list[uuid.UUID] | None = None
) -> list[ClaimRecord]:
    """The project's live claims. Superseded rows are history, not register."""
    query = sa.select(ClaimRecord).where(
        ClaimRecord.project_id == guideline.project_id,
        ClaimRecord.workspace_id == guideline.workspace_id,
        ClaimRecord.superseded_by.is_(None),
    )
    if only is not None:
        query = query.where(ClaimRecord.id.in_(only))
    return list((await db.execute(query.order_by(ClaimRecord.created_at))).scalars().all())


async def _matrix(db: AsyncSession, project_id: uuid.UUID) -> SignOffMatrix | None:
    return (
        (
            await db.execute(
                sa.select(SignOffMatrix).where(
                    SignOffMatrix.project_id == project_id,
                    SignOffMatrix.superseded_at.is_(None),
                )
            )
        )
        .scalars()
        .first()
    )


async def _signature_by_hash(
    db: AsyncSession, project_id: uuid.UUID, digest: str
) -> ClaimSignature | None:
    return (
        (
            await db.execute(
                sa.select(ClaimSignature)
                .where(
                    ClaimSignature.project_id == project_id,
                    ClaimSignature.set_hash == digest,
                )
                .order_by(ClaimSignature.signed_at.desc())
            )
        )
        .scalars()
        .first()
    )


def _hash_of(claims: list[ClaimRecord]) -> str:
    """The hash of "approve everything outstanding", which is what the drawer opens on."""
    return set_hash(
        [
            ClaimDecision(
                claim_id=claim.id, normalized_text=claim.normalized_text, decision="approved"
            )
            for claim in claims
        ]
    )


def _claim_expiry(claim: ClaimRecord, now: datetime) -> datetime:
    """Computed from constants, never chosen by a person or a model.

    A register without expiry is a register of things that used to be true, and
    a quantified claim goes stale faster than a qualitative one.
    """
    constants = load_content_constants()
    quantified = {"quantified", "comparative"}
    key = (
        "claims.quantified_expiry_days"
        if str(claim.claim_type.value) in quantified
        else "claims.default_expiry_days"
    )
    return now + timedelta(days=int(constants.value(key)))


def _signature_expiry(now: datetime) -> datetime:
    """A signature outlives the longest claim expiry it could grant."""
    constants = load_content_constants()
    longest = max(
        int(constants.value("claims.default_expiry_days")),
        int(constants.value("claims.quantified_expiry_days")),
    )
    return now + timedelta(days=longest)


def _receipt(signature: ClaimSignature) -> SignatureReceipt:
    decisions = signature.decisions or []
    return SignatureReceipt(
        signature_id=signature.id,
        signer_id=signature.signer_id,
        set_hash=signature.set_hash,
        statement=signature.statement,
        method=str(signature.method),
        signed_at=signature.signed_at,
        expires_at=signature.expires_at,
        approved_count=sum(1 for d in decisions if d.get("decision") == "approved"),
        rejected_count=sum(1 for d in decisions if d.get("decision") == "rejected"),
        voided_at=signature.voided_at,
        void_reason=signature.void_reason,
    )
