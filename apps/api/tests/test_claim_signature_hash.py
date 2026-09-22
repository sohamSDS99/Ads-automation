"""`set_hash` — the anti-race guarantee for a legal signature (PRD §7.2, §16 rule 2).

A signature is scoped to the exact claim set the signer read. If the register
moved between the read and the submit, the recomputed hash differs and the
route returns 409 having written nothing. These tests pin the properties that
promise rests on: the hash must be blind to presentation order and sensitive to
every part of the set that a signer would care about having changed.
"""

from __future__ import annotations

import uuid

import pytest

from agent.guidelines.signature import ClaimDecision, set_hash


def decision(
    claim_id: uuid.UUID | None = None,
    *,
    text: str = "the fastest sds software",
    verdict: str = "approved",
) -> ClaimDecision:
    return ClaimDecision(
        claim_id=claim_id or uuid.uuid4(),
        normalized_text=text,
        decision=verdict,
    )


def test_the_same_set_hashes_the_same_way() -> None:
    one = decision()
    assert set_hash([one]) == set_hash([one])


def test_presentation_order_does_not_change_the_hash() -> None:
    """The signer sorted the table by risk; the hash must not care.

    Without this the UI could produce a 409 purely by re-sorting a column,
    which would train signers to retry until it worked — the precise habit a
    set-scoped signature exists to prevent.
    """
    a, b, c = decision(), decision(), decision()
    assert set_hash([a, b, c]) == set_hash([c, a, b])


def test_changing_a_decision_changes_the_hash() -> None:
    claim = uuid.uuid4()
    approved = decision(claim, verdict="approved")
    rejected = decision(claim, verdict="rejected")
    assert set_hash([approved]) != set_hash([rejected])


def test_changing_the_claim_text_changes_the_hash() -> None:
    """The register moved under the signer — that is the whole point.

    The same claim id whose wording was edited after the signer read it is a
    different thing to put a name to.
    """
    claim = uuid.uuid4()
    before = decision(claim, text="the fastest sds software")
    after = decision(claim, text="the fastest sds software in europe")
    assert set_hash([before]) != set_hash([after])


def test_adding_a_claim_changes_the_hash() -> None:
    a = decision()
    assert set_hash([a]) != set_hash([a, decision()])


def test_removing_a_claim_changes_the_hash() -> None:
    a, b = decision(), decision()
    assert set_hash([a, b]) != set_hash([a])


def test_an_empty_set_is_refused() -> None:
    """Signing nothing is not a signature, and it must not produce a hash.

    An empty set would otherwise hash to a stable constant, which any caller
    could submit — a signature over no claims that nonetheless exists as a row.
    """
    with pytest.raises(ValueError, match="no claims"):
        set_hash([])


def test_a_duplicate_claim_id_is_refused() -> None:
    """One decision per claim. Two would make the set ambiguous.

    Sorting would silently pick an order and the hash would be stable, so the
    contradiction would survive all the way to a signed row asserting both that
    a claim was approved and that it was rejected.
    """
    claim = uuid.uuid4()
    with pytest.raises(ValueError, match="twice"):
        set_hash([decision(claim, verdict="approved"), decision(claim, verdict="rejected")])


def test_the_note_and_expiry_are_not_part_of_the_set() -> None:
    """The hash covers the register, not the signer's own annotations.

    PRD §7.2 names the material exactly: (claim_id, normalized_text, decision).
    A signer editing their own note or expiry must not invalidate the set they
    are in the middle of signing.
    """
    claim = uuid.uuid4()
    bare = ClaimDecision(claim_id=claim, normalized_text="x", decision="approved")
    annotated = ClaimDecision(
        claim_id=claim,
        normalized_text="x",
        decision="approved",
        note="cleared with outside counsel",
    )
    assert set_hash([bare]) == set_hash([annotated])
