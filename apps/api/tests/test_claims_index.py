"""Building `claims_index` from the register — where a licence is granted or withheld.

This is the join between the database and the linter, and it is the place a
claim can be licensed by accident. `licences()` in `guardrails/matchers/claims.py`
asks only whether a `ClaimRef` says `approved` and has not expired; it never
sees the signature. So everything that makes a signature *stop counting* —
voided by a legal-owner reassignment, expired, revoked — has to be resolved
here, into the status the linter reads.

Unlicensed, unsigned, rejected, revoked and expired must all come out the same
way, because law 24 says the consequence is the same: nobody has signed for this.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from agent.db.models import ClaimRecord, ClaimSignature, ClaimStatus
from agent.guidelines.claims_index import claim_refs_for

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)


def record(
    *,
    status: ClaimStatus = ClaimStatus.APPROVED,
    expires_at: datetime | None = None,
    signature_id: uuid.UUID | None = None,
) -> ClaimRecord:
    return ClaimRecord(
        id=uuid.uuid4(),
        claim_text="The fastest SDS software",
        normalized_text="the fastest sds software",
        surface_forms=["fastest SDS software"],
        status=status,
        market_scope=["DE"],
        languages=["en"],
        expires_at=expires_at,
        current_signature_id=signature_id,
    )


def signature(
    *,
    voided_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> ClaimSignature:
    return ClaimSignature(
        id=uuid.uuid4(),
        signer_id=uuid.uuid4(),
        claim_ids=[],
        set_hash="x",
        decisions=[],
        statement="I confirm these claims are safe to run.",
        reauth_token_id="t",
        voided_at=voided_at,
        expires_at=expires_at,
    )


def only(rows: object) -> object:
    refs = claim_refs_for(rows, now=NOW)  # type: ignore[arg-type]
    assert len(refs) == 1
    return refs[0]


def test_an_approved_claim_with_a_live_signature_is_licensed() -> None:
    sig = signature(expires_at=NOW + timedelta(days=30))
    ref = only([(record(signature_id=sig.id), sig)])
    assert ref.status == "approved"  # type: ignore[attr-defined]


def test_a_voided_signature_un_licenses_its_claim() -> None:
    """The legal owner was reassigned, so their signatures voided (PRD §5.2).

    The claim row may still read `approved` — the void is recorded on the
    signature, and moving every dependent claim row is a separate write that a
    crash could interrupt. The linter must not license in that window.
    """
    sig = signature(voided_at=NOW - timedelta(hours=1))
    ref = only([(record(signature_id=sig.id), sig)])
    assert ref.status != "approved"  # type: ignore[attr-defined]


def test_an_expired_signature_un_licenses_its_claim() -> None:
    sig = signature(expires_at=NOW - timedelta(days=1))
    ref = only([(record(signature_id=sig.id), sig)])
    assert ref.status != "approved"  # type: ignore[attr-defined]


def test_an_approved_claim_with_no_signature_at_all_is_not_licensed() -> None:
    """Approved-without-a-signature should be unreachable. It must still deny.

    If it ever becomes reachable — a bad migration, a hand-edited row — the
    failure has to be a refusal to license, not a licence nobody signed.
    """
    ref = only([(record(signature_id=None), None)])
    assert ref.status != "approved"  # type: ignore[attr-defined]


def test_a_revoked_claim_is_not_licensed() -> None:
    """`revoked` has no `ClaimRef` status of its own and must not become `approved`."""
    ref = only([(record(status=ClaimStatus.REVOKED), None)])
    assert ref.status != "approved"  # type: ignore[attr-defined]


def test_a_pending_claim_is_not_licensed() -> None:
    ref = only([(record(status=ClaimStatus.PENDING_SIGNOFF), None)])
    assert ref.status != "approved"  # type: ignore[attr-defined]


def test_an_unsupported_claim_is_not_licensed() -> None:
    ref = only([(record(status=ClaimStatus.UNSUPPORTED), None)])
    assert ref.status != "approved"  # type: ignore[attr-defined]


def test_a_rejected_claim_is_not_licensed() -> None:
    ref = only([(record(status=ClaimStatus.REJECTED), None)])
    assert ref.status != "approved"  # type: ignore[attr-defined]


def test_the_claim_expiry_is_carried_to_the_linter() -> None:
    """The linter re-checks expiry against its own `now`, so it needs the date."""
    expiry = NOW + timedelta(days=10)
    sig = signature(expires_at=NOW + timedelta(days=30))
    ref = only([(record(expires_at=expiry, signature_id=sig.id), sig)])
    assert ref.expires_at == expiry  # type: ignore[attr-defined]


def test_surface_forms_and_scope_reach_the_linter() -> None:
    sig = signature(expires_at=NOW + timedelta(days=30))
    ref = only([(record(signature_id=sig.id), sig)])
    assert "fastest SDS software" in ref.surface_forms  # type: ignore[attr-defined]
    assert ref.market_scope == ("DE",)  # type: ignore[attr-defined]
    assert ref.languages == ("en",)  # type: ignore[attr-defined]


# -- the S3-P3 exit criterion, end to end ------------------------------------
#
# "an approved claim licenses its surface forms in the linter and an expired one
# does not" (PRD §21). S3-P1 proved the matcher honours a `ClaimRef`; this
# proves a real signed row becomes the `ClaimRef` that does it, through the
# shipped detectors rather than a fixture.

from agent.guardrails.matchers.claims import (  # noqa: E402
    claim_licence,
    evaluate_claims,
    prepare_claims,
)
from agent.guidelines.constants import load_content_constants  # noqa: E402
from tests.guardrails.helpers import LEGAL, context, target  # noqa: E402

_DETECTORS = load_content_constants().detectors()
_EN = tuple(d.detector_id for d in _DETECTORS if d.locale == "en")
_RULE = claim_licence(
    _EN, authority=LEGAL, match_threshold=load_content_constants().value("claims.match_threshold")
)


def lint_through_register(rows: object, copy: str = "The best SDS software") -> list:
    """Run the real linter over refs built from real rows."""
    refs = claim_refs_for(rows, now=NOW)  # type: ignore[arg-type]
    item = target(copy, market="DE", language="en")
    ctx = context([item], claims=refs, detectors=_DETECTORS, now=NOW)
    return evaluate_claims(_RULE, prepare_claims(_RULE.matcher), item, ctx)


def signed_claim(**sig_kwargs: object):
    sig = signature(**sig_kwargs)  # type: ignore[arg-type]
    claim = record(signature_id=sig.id)
    claim.normalized_text = "the best sds software"
    claim.surface_forms = ["the best SDS software"]
    return [(claim, sig)]


def test_a_signed_claim_licenses_its_surface_form_in_the_linter() -> None:
    assert lint_through_register(signed_claim(expires_at=NOW + timedelta(days=30))) == []


def test_an_expired_signature_makes_the_same_copy_blocking_again() -> None:
    findings = lint_through_register(signed_claim(expires_at=NOW - timedelta(days=1)))
    assert len(findings) == 1
    assert findings[0].severity == "blocking"


def test_a_voided_signature_makes_the_same_copy_blocking_again() -> None:
    """Reassigning the legal owner must take the licence away immediately."""
    findings = lint_through_register(signed_claim(voided_at=NOW - timedelta(hours=1)))
    assert len(findings) == 1
    assert findings[0].severity == "blocking"


def test_an_unsigned_register_licenses_nothing() -> None:
    claim = record(status=ClaimStatus.PENDING_SIGNOFF)
    claim.normalized_text = "the best sds software"
    assert lint_through_register([(claim, None)]) != []
