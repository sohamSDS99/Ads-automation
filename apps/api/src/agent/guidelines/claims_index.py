"""The register, as the linter sees it.

`guardrails/` is pure — no ORM, no clock, no network — so somebody has to turn
`ClaimRecord` rows and their signatures into the `ClaimRef` list a `RuleSet`
carries. This is that somebody, and it lives in `guidelines/` rather than
`guardrails/` for exactly that reason: it touches models, and the purity guard
would fail the build if it did so next door.

**Why this module is load-bearing.** `matchers/claims.licences()` asks one
question of a `ClaimRef`: does it say `approved`, and has it not expired. It
never sees the signature. So every way a signature can stop counting — voided
when a legal owner was reassigned, lapsed past its own expiry, revoked by the
signer — has to be resolved *here*, into the status the linter reads. A claim
row still reading `approved` while its signature is void is not a contradiction
to paper over: the void is recorded on the signature, and moving every
dependent claim row is a second write that a crash can interrupt. This function
is what makes that window deny instead of licence.

Law 24 in one line: `approved` is the only value that licenses anything, and it
is reachable only with a live signature behind it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Literal

from agent.db.models import ClaimRecord, ClaimSignature, ClaimStatus
from agent.schemas.guardrails import ClaimRef

#: What `ClaimRef.status` may hold. Narrower than `ClaimStatus`, because the
#: linter only needs to know whether something licenses — the process states
#: that led there are the register's business, not the matcher's.
RefStatus = Literal["draft", "approved", "rejected", "expired"]


def signature_is_live(signature: ClaimSignature | None, *, now: datetime) -> bool:
    """A signature counts when it exists, was not voided, and has not lapsed."""
    if signature is None:
        return False
    if signature.voided_at is not None:
        return False
    return not (signature.expires_at is not None and signature.expires_at <= now)


def ref_status(claim: ClaimRecord, signature: ClaimSignature | None, *, now: datetime) -> RefStatus:
    """The status the linter will act on.

    Default-deny is expressed as structure rather than as a comment: the only
    branch returning `approved` is the one that has already proved a live
    signature, and every other path falls through to a value that denies.
    """
    if claim.status is ClaimStatus.REJECTED or claim.status is ClaimStatus.REVOKED:
        # Revoked is a decision to withdraw, not an absence of one. It reads as
        # `rejected` so a reader of the ruleset sees a refusal rather than a gap.
        return "rejected"
    if claim.status is ClaimStatus.EXPIRED:
        return "expired"
    if claim.status is ClaimStatus.APPROVED:
        if signature_is_live(signature, now=now):
            return "approved"
        if signature is not None and signature.voided_at is None:
            # Present, unvoided, and past its expiry.
            return "expired"
        # Voided, or approved with no signature at all. The latter should be
        # unreachable; if a bad migration or a hand-edited row ever makes it
        # reachable, the failure is a refusal to license rather than a licence
        # nobody signed.
        return "draft"
    # unsupported, pending_signoff — nobody has signed yet.
    return "draft"


def claim_refs_for(
    rows: Iterable[tuple[ClaimRecord, ClaimSignature | None]], *, now: datetime
) -> tuple[ClaimRef, ...]:
    """Build the `claims_index` a `RuleSet` compiles in.

    Sorted by claim id so the compile is reproducible: `compiler.py` sorts too,
    but a caller that diffed two indexes would otherwise see spurious churn.
    """
    refs = [
        ClaimRef(
            claim_id=claim.id,
            normalized_text=claim.normalized_text,
            surface_forms=_strings(claim.surface_forms),
            status=ref_status(claim, signature, now=now),
            market_scope=_strings(claim.market_scope),
            languages=_strings(claim.languages),
            expires_at=claim.expires_at,
            signature_id=signature.id if signature is not None else None,
        )
        for claim, signature in rows
    ]
    return tuple(sorted(refs, key=lambda ref: str(ref.claim_id)))


def _strings(value: Sequence[object] | None) -> tuple[str, ...]:
    """JSONB and text[] both arrive as loose lists; the contract wants strings."""
    if not value:
        return ()
    return tuple(str(item) for item in value if item is not None and str(item).strip())
