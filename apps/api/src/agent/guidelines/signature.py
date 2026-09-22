"""Claim signatures — the set hash, and the step-up that authorises one.

Two mechanisms live here, and they answer two different attacks.

`set_hash` answers *the register moved*. A signature is scoped to the exact
claims the signer read, so the material is hashed and re-hashed server-side at
submit time; a mismatch is a 409 that writes nothing (PRD §16 rule 2). Without
it, a claim edited — or added — between the signer opening the drawer and
pressing submit would arrive inside a signature nobody read.

The step-up token answers *is this still them*. A live session proves somebody
logged in at some point. A signature needs the person to be present now, so the
signing route demands a single-use token minted against the current password
within `SIGNATURE_REAUTH_TTL_SECONDS` (PRD §5.2 layer 3, CS2).

Neither mechanism is the authorization itself. The identity narrowing — this
account *is* the project's named legal owner — happens in the route, because it
needs the database. What is here is pure enough to test without one.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

#: The two terminal decisions a signer may record. There is no "abstain": a
#: claim nobody decided stays `pending_signoff` and is simply absent from the
#: set, which keeps "undecided" and "decided not to allow" distinguishable.
Decision = Literal["approved", "rejected"]


class ClaimDecision(BaseModel):
    """One claim, and what the signer said about it.

    `note` and `expires_at` are the signer's own annotations and deliberately
    sit outside the hash — see `set_hash`.
    """

    model_config = ConfigDict(frozen=True)

    claim_id: uuid.UUID
    normalized_text: str
    decision: Decision
    note: str | None = None
    expires_at: datetime | None = None


def set_hash(decisions: Sequence[ClaimDecision]) -> str:
    """sha256 over the sorted `(claim_id, normalized_text, decision)` triples.

    Sorted, so the hash is a property of the *set* and not of the order a table
    happened to be in — otherwise re-sorting a column in the UI would produce a
    409 and teach signers to retry until one worked.

    The material is exactly the three fields PRD §7.2 names. `note` and
    `expires_at` are excluded on purpose: they are what the signer is writing,
    not what the register said, and a signer editing their own note must not
    invalidate the set they are part-way through signing.
    """
    if not decisions:
        raise ValueError(
            "a signature over no claims is not a signature — refusing to hash an empty set"
        )

    seen: set[uuid.UUID] = set()
    material: list[list[str]] = []
    for entry in decisions:
        if entry.claim_id in seen:
            raise ValueError(
                f"claim {entry.claim_id} appears twice in one signature set. "
                "One decision per claim: sorting would otherwise pick an order silently "
                "and the row would assert both that it was approved and that it was not."
            )
        seen.add(entry.claim_id)
        material.append([str(entry.claim_id), entry.normalized_text, entry.decision])

    material.sort()
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
