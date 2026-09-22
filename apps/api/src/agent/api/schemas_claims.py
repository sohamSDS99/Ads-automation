"""Wire shapes for the claims register and the non-delegable signature."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ClaimSummary(BaseModel):
    """One row of the register, as the claims table renders it."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    claim_text: str
    normalized_text: str
    surface_forms: list[str] = Field(default_factory=list)
    claim_type: str
    market_scope: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    risk_tier: str
    status: str
    expires_at: datetime | None = None
    evidence_ids: list[uuid.UUID] = Field(default_factory=list)
    current_signature_id: uuid.UUID | None = None


class ClaimList(BaseModel):
    claims: list[ClaimSummary]
    #: What the signer must send back. Computed server-side so the UI never
    #: derives it — a client that computed its own hash could not detect a
    #: register that moved, which is the only thing the hash is for.
    set_hash: str | None = None
    #: Present so the table can say "awaiting signature from …" to everyone who
    #: is not that person.
    legal_owner_id: uuid.UUID | None = None


class ClaimDecisionIn(BaseModel):
    claim_id: uuid.UUID
    normalized_text: str
    decision: Literal["approved", "rejected"]
    note: str | None = Field(default=None, max_length=2000)
    expires_at: datetime | None = None


class SignRequest(BaseModel):
    decisions: list[ClaimDecisionIn] = Field(min_length=1)
    statement: str = Field(min_length=1, max_length=4000)
    #: The hash of the set the signer actually read.
    set_hash: str = Field(min_length=1, max_length=128)
    reauth_token: str = Field(min_length=1, max_length=512)


class SignatureReceipt(BaseModel):
    """What the signer is shown afterwards, and can export."""

    model_config = ConfigDict(from_attributes=True)

    signature_id: uuid.UUID
    signer_id: uuid.UUID
    set_hash: str
    statement: str
    method: str
    signed_at: datetime
    expires_at: datetime | None = None
    approved_count: int
    rejected_count: int
    voided_at: datetime | None = None
    void_reason: str | None = None


class RevokeRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class ClaimPatch(BaseModel):
    """Editing a draft before anybody signs it."""

    claim_text: str | None = Field(default=None, min_length=1, max_length=2000)
    surface_forms: list[str] | None = None
    market_scope: list[str] | None = None
    languages: list[str] | None = None
