"""H3 → Stage 03 → repin: the claim half of the H3 transaction (Stage 04 PRD §8.6, §4.4).

A legal owner's decision on a `new_claim` exception becomes Stage 03 register
rows, exactly as a Stage 03 signature would have made them:

1. one `ClaimRecord(origin='creative_exception', status=approved|rejected)` per
   claim, scoped to the markets and languages the exception was found in;
2. one append-only `ClaimSignature` over that claim subset — the server's view
   of each claim, hashed with Stage 03's own `set_hash`;
3. if anything was approved, a Stage 03 MINOR (`lifecycle.mint_reviewed_minor`,
   the signer as its reviewer) and `{ruleset_version, reason:'h3_clearance'}`
   appended to `Run.pins` — the **only** mid-run repin there is (§4.4).

Nothing here commits: the route's one transaction holds all of it, so a
failure anywhere writes nothing at all.

A span that already has a register row is not a *new* claim — Stage 03 owns
that row's lifecycle, and `check_register` refuses the decision before any
proof is spent rather than let H3 overrule it through a side door.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.creative.clearance import ClearanceError, Decision
from agent.db.models import (
    AmendmentOrigin,
    ClaimRecord,
    ClaimSignature,
    ClaimStatus,
    ClaimType,
    ContentGuideline,
    CreativeException,
    Run,
    SignatureMethod,
)
from agent.guardrails.normalize import normalized_text
from agent.guidelines.constants import get_content_constants
from agent.guidelines.signature import ClaimDecision, claim_expiry, set_hash, signature_expiry
from agent.policy import lifecycle
from agent.schemas.creative_input import CreativeInput

H3_CLEARANCE = "h3_clearance"
ORIGIN = "creative_exception"


@dataclass(frozen=True, slots=True)
class ClaimDecisionIn:
    """One legal owner's decision on one `new_claim`, as the route received it."""

    decision: Decision
    note: str | None = None
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Signed:
    signature: ClaimSignature
    #: exception id → the register row written for it.
    records: dict[uuid.UUID, ClaimRecord]

    @property
    def approved(self) -> bool:
        return any(row.status is ClaimStatus.APPROVED for row in self.records.values())


def claim_text(row: CreativeException) -> tuple[str, str]:
    """`(claim_text, normalized_text)` of a `new_claim` — normalised the way
    the linter folds a span, in the exception's first language."""
    text = (row.subject_text or "").strip()
    if not text:
        raise ClearanceError(
            "claim_without_text", f"Exception {row.id} names no claim text. Nothing was written."
        )
    languages = _texts(row.proposed.get("languages"))
    return text, normalized_text(text, locale=languages[0] if languages else "en")


async def check_register(
    db: AsyncSession, project_id: uuid.UUID, claims: Sequence[CreativeException]
) -> None:
    """Refuse before anything is spent if a claim is already in the register.

    `uq_claim_record_current` allows one current row per normalised text, so
    the insert would fail anyway — this names the reason instead, and does it
    before the step-up token is consumed.
    """
    texts: dict[str, uuid.UUID] = {}
    for row in claims:
        _, normalized = claim_text(row)
        if normalized in texts:
            raise ClearanceError(
                "duplicate_claim",
                f"Exceptions {texts[normalized]} and {row.id} state the same claim "
                f"({normalized!r}). Nothing was written.",
            )
        texts[normalized] = row.id
    if not texts:
        return
    found = (
        (
            await db.execute(
                sa.select(ClaimRecord.normalized_text, ClaimRecord.status).where(
                    ClaimRecord.project_id == project_id,
                    ClaimRecord.superseded_by.is_(None),
                    ClaimRecord.normalized_text.in_(list(texts)),
                )
            )
        )
        .tuples()
        .all()
    )
    if found:
        named = ", ".join(f"{text!r} ({status.value})" for text, status in found)
        raise ClearanceError(
            "claim_in_register",
            f"The claims register already holds {named}; its decision belongs to the "
            "Stage 03 register, not to H3. Nothing was written.",
        )


async def record_claims(
    db: AsyncSession,
    run: Run,
    claims: Sequence[CreativeException],
    decisions: Mapping[uuid.UUID, ClaimDecisionIn],
    *,
    signer_id: uuid.UUID,
    statement: str,
    reauth_token_id: str,
    ip: Any,
    user_agent: str | None,
    now: datetime,
) -> Signed | None:
    """§8.6 step 2: the register rows and the one signature over them."""
    if not claims:
        return None
    guideline_id = pinned_guideline_id(run)
    records: dict[uuid.UUID, ClaimRecord] = {}
    for row in claims:
        text, normalized = claim_text(row)
        proposed = row.proposed or {}
        approved = decisions[row.id].decision == "cleared"
        records[row.id] = ClaimRecord(
            workspace_id=run.workspace_id,
            project_id=run.project_id,
            first_seen_guideline_id=guideline_id,
            claim_text=text,
            normalized_text=normalized,
            surface_forms=list(_texts(proposed.get("surface_forms")) or (text,)),
            claim_type=ClaimType(str(proposed.get("claim_type"))),
            market_scope=list(_texts(proposed.get("market_scope"))),
            languages=list(_texts(proposed.get("languages"))),
            substantiation=proposed.get("substantiation"),
            evidence_ids=list(row.evidence_ids or ()),
            status=ClaimStatus.APPROVED if approved else ClaimStatus.REJECTED,
            origin=ORIGIN,
        )
    db.add_all(records.values())
    await db.flush()

    # The server's view, never the request's: the signature is the readable
    # content of an append-only legal record (S3-P3).
    server_view = [
        ClaimDecision(
            claim_id=records[row.id].id,
            normalized_text=records[row.id].normalized_text,
            decision="approved" if decisions[row.id].decision == "cleared" else "rejected",
            surface_forms=_texts(records[row.id].surface_forms),
            market_scope=_texts(records[row.id].market_scope),
            languages=_texts(records[row.id].languages),
            note=decisions[row.id].note,
            expires_at=decisions[row.id].expires_at,
        )
        for row in claims
    ]
    signature = ClaimSignature(
        workspace_id=run.workspace_id,
        project_id=run.project_id,
        signer_id=signer_id,
        claim_ids=[records[row.id].id for row in claims],
        set_hash=set_hash(server_view),
        decisions=[entry.model_dump(mode="json") for entry in server_view],
        statement=statement,
        method=SignatureMethod.STEP_UP_PASSWORD,
        reauth_token_id=reauth_token_id,
        ip=ip,
        user_agent=user_agent,
        signed_at=now,
        expires_at=signature_expiry(now),
    )
    db.add(signature)
    await db.flush()
    for row in claims:
        record = records[row.id]
        record.current_signature_id = signature.id
        if record.status is ClaimStatus.APPROVED:
            record.expires_at = decisions[row.id].expires_at or claim_expiry(
                record.claim_type.value, now
            )
        else:
            # A rejected claim keeps the signature that rejected it: the
            # register has to show who said no, and when.
            record.expires_at = None
    await db.flush()
    return Signed(signature=signature, records=records)


async def repin(db: AsyncSession, run: Run, *, reviewed_by: uuid.UUID, now: datetime) -> str:
    """§8.6 step 4: mint the MINOR the cleared claims need and pin the run to it.

    Minted from the run's *pinned* guideline, so the run stays reproducible
    against the rulebook it was made under plus what H3 licensed. Stage 03's
    `mint_minor` recompiles against the register as it now is, which is what
    makes the cleared claims licence copy.
    """
    guideline = await db.get(ContentGuideline, pinned_guideline_id(run))
    if guideline is None:  # pragma: no cover — FK from the pinned ruleset
        raise ClearanceError("guideline_missing", "The run's pinned guideline no longer exists.")
    ruleset, _ = await lifecycle.mint_reviewed_minor(
        db,
        guideline,
        origin=AmendmentOrigin.CREATIVE_EXCEPTION,
        reviewed_by=reviewed_by,
        rationale=(
            f"H3 legal exception clearance on creative run {run.id}: the named legal "
            "owner's signature licenses the cleared claims."
        ),
        constants=get_content_constants(),
        now=now,
    )
    # Reassigned, never appended in place: `pins` is plain JSONB, and an
    # in-place append is invisible to the unit of work.
    run.pins = [
        *(run.pins or []),
        {"ruleset_version": ruleset.ruleset_version, "reason": H3_CLEARANCE, "at": now.isoformat()},
    ]
    await db.flush()
    return ruleset.ruleset_version


def pinned_guideline_id(run: Run) -> uuid.UUID:
    return CreativeInput.model_validate(run.creative_input).ruleset_ref.guideline_id


def _texts(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(str(item) for item in value)
