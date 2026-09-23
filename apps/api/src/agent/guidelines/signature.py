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
import hmac
import json
import secrets
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal

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
    #: What the approval actually licenses. `licences()` matches a candidate
    #: span against these as well as against `normalized_text`, and treats an
    #: empty market or language list as *unrestricted* — so all three are part
    #: of what a signer is agreeing to, and all three are in the hash.
    surface_forms: tuple[str, ...] = ()
    market_scope: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    note: str | None = None
    expires_at: datetime | None = None


def set_hash(decisions: Sequence[ClaimDecision]) -> str:
    """sha256 over the sorted `(claim_id, normalized_text, decision)` triples.

    Sorted, so the hash is a property of the *set* and not of the order a table
    happened to be in — otherwise re-sorting a column in the UI would produce a
    409 and teach signers to retry until one worked.

    The material is the three fields PRD §7.2 names **plus the three that decide
    what the approval licenses**: `surface_forms`, `market_scope` and
    `languages`. §7.2's list is necessary and not sufficient — `licences()`
    matches candidate spans against the surface forms and scopes the match by
    market and language, and an empty market or language list means *every*
    market or language. All three are writable by `GUIDELINE_EXECUTE`, held by
    `operator` and `admin` — precisely the roles denied `CLAIM_SIGN`. Leaving
    them out of the hash would let somebody who cannot sign widen what a named
    person's signature licenses, after that person read the register, without
    tripping the 409 that exists to stop exactly this.

    Each list is sorted inside the material, so reordering one is not a change.

    `note` and `expires_at` stay outside: they are what the signer is writing,
    not what the register said, and editing your own note must not invalidate
    the set you are part-way through signing.
    """
    if not decisions:
        raise ValueError(
            "a signature over no claims is not a signature — refusing to hash an empty set"
        )

    return _digest(_material(decisions, with_decision=True))


def register_hash(decisions: Sequence[ClaimDecision]) -> str:
    """The same material as `set_hash`, **minus the signer's own decision**.

    This is the anti-race token, and it is a different value from `set_hash`
    for a reason that took a real signature attempt to surface.

    §16 rule 2 gives the hash one job: *"a `set_hash` that does not match the
    server's recomputation returns 409 — the register changed under the signer
    and they must re-read it."* The register is what the rows say. It is not
    what the signer decided about them.

    Folding `decision` into the value the client echoes makes the two
    inseparable, and then the only hash the server can hand out before the
    signer has decided anything is the hash of a guess — "approve everything".
    From there, every partially-approved set disagrees with its own token and is
    refused as a race that never happened. §15.3 C.2 requires per-claim
    rejection and the `ClaimSignature.decisions` docstring calls a partially
    approved set "legal and common", so that is not an edge case; it is most of
    them.

    So the two hashes are split by what they are *for*:

    * `register_hash` — what the signer read. Handed to the client, echoed back,
      recomputed on submit. Changes only when somebody edits the register.
    * `set_hash` — what the signer said, stored on the signature and used for
      idempotency. Never travels to a client, so it never needs to be
      predictable before the decisions exist.
    """
    return _digest(_material(decisions, with_decision=False))


def _material(
    decisions: Sequence[ClaimDecision], *, with_decision: bool
) -> list[list[str | list[str]]]:
    """The hashed rows, sorted so the value is a property of the set not the order."""
    if not decisions:
        raise ValueError(
            "a signature over no claims is not a signature — refusing to hash an empty set"
        )

    seen: set[uuid.UUID] = set()
    material: list[list[str | list[str]]] = []
    for entry in decisions:
        if entry.claim_id in seen:
            raise ValueError(
                f"claim {entry.claim_id} appears twice in one signature set. "
                "One decision per claim: sorting would otherwise pick an order silently "
                "and the row would assert both that it was approved and that it was not."
            )
        seen.add(entry.claim_id)
        row: list[str | list[str]] = [str(entry.claim_id), entry.normalized_text]
        if with_decision:
            row.append(entry.decision)
        row.extend(
            [
                sorted(entry.surface_forms),
                sorted(entry.market_scope),
                sorted(entry.languages),
            ]
        )
        material.append(row)

    material.sort()
    return material


def _digest(material: list[list[str | list[str]]]) -> str:
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


# -- the step-up ------------------------------------------------------------

#: Redis key for a minted token. Keyed by the token's *hash*, so a Redis dump
#: yields nothing replayable — the same reasoning as `auth/invites.py`.
REAUTH_KEY = "reauth:{fingerprint}"

#: 32 bytes of `secrets`. There is no dictionary to attack, so SHA-256 is the
#: right primitive here even though passwords get Argon2id.
REAUTH_TOKEN_BYTES = 32


class ReauthError(Exception):
    """The step-up proof was missing, expired, already spent, or somebody else's."""


def new_reauth_token() -> str:
    """The value handed to the browser. Never persisted in the clear."""
    return secrets.token_urlsafe(REAUTH_TOKEN_BYTES)


def reauth_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class ReauthTokens:
    """Mint and spend single-use step-up proofs.

    A live session proves somebody signed in at some point; a signature needs
    the person to be *present now*. This is the difference, and it is why the
    token is single-use: without that it is a password typed once and replayable
    for the rest of its TTL.

    Spending is an atomic `GETDEL`. A plain read-then-delete would let two
    concurrent submits both pass the read, and "the signature route is racy"
    is not a sentence anyone wants attached to a legal record.
    """

    def __init__(self, redis: Any, *, ttl_seconds: int) -> None:
        self._redis = redis
        self._ttl = ttl_seconds

    async def mint(self, user_id: uuid.UUID) -> tuple[str, str]:
        """Returns `(token, token_id)`. The token is shown once; the id is audited."""
        token = new_reauth_token()
        token_id = uuid.uuid4().hex
        await self._redis.set(
            REAUTH_KEY.format(fingerprint=reauth_fingerprint(token)),
            json.dumps({"user_id": str(user_id), "token_id": token_id}),
            ex=self._ttl,
        )
        return token, token_id

    async def consume(self, user_id: uuid.UUID, token: str) -> str:
        """Spend a token and return its id, or raise `ReauthError`.

        The bound user is checked *after* the delete on purpose: a token
        presented by the wrong account is burned rather than left for the right
        one to find, because at that point it has been seen by somebody it was
        not issued to.
        """
        if not token:
            raise ReauthError("no step-up proof was supplied")
        raw = await self._redis.getdel(REAUTH_KEY.format(fingerprint=reauth_fingerprint(token)))
        if raw is None:
            raise ReauthError("that step-up proof is unknown, expired, or already used")
        payload = json.loads(raw)
        if not hmac.compare_digest(str(payload.get("user_id", "")), str(user_id)):
            raise ReauthError("that step-up proof was issued to a different account")
        return str(payload["token_id"])
