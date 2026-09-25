"""Wire shapes of the H3 routes (Stage 04 PRD §16 H3, contract rule 4)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent.creative.clearance import Decision, ExceptionKind


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExceptionDecisionIn(_In):
    exception_id: uuid.UUID
    decision: Decision
    note: str | None = Field(default=None, max_length=2000)
    #: A `new_claim` only: an earlier expiry than the constants would give.
    expires_at: datetime | None = None


class ClearRequest(_In):
    decisions: list[ExceptionDecisionIn] = Field(min_length=1)
    statement: str = Field(min_length=1, max_length=2000)
    #: The `set_hash` `GET …/exceptions` returned — the set as it was read.
    set_hash: str = Field(min_length=1)
    #: Not required by the schema on purpose: a missing proof is a 401 (§16
    #: rule 4), not a 422 that would read as a malformed request.
    reauth_token: str = ""


class Swapped(BaseModel):
    out: uuid.UUID
    into: uuid.UUID | None


class ClearReceipt(BaseModel):
    run_id: uuid.UUID
    #: `clearance.decided_hash` — the set with these decisions.
    set_hash: str
    #: `clearance.register_hash` — the set as it was read.
    register_hash: str
    decided_by: uuid.UUID
    decided_at: datetime
    statement: str
    signature_id: uuid.UUID | None
    ruleset_version: str | None
    cleared: list[uuid.UUID]
    rejected: list[uuid.UUID]
    swapped: list[Swapped]
    resumed: bool


class WithdrawRequest(_In):
    exception_ids: list[uuid.UUID] = Field(min_length=1)


class WithdrawResponse(BaseModel):
    run_id: uuid.UUID
    withdrawn: list[uuid.UUID]
    swapped: list[Swapped]
    h3_status: Literal["required", "not_required"]
    #: The register hash of what is still open; None once nothing is.
    set_hash: str | None
    resumed: bool


class ExceptionOut(BaseModel):
    exception_id: uuid.UUID
    kind: ExceptionKind
    status: Literal["open", "cleared", "rejected", "withdrawn"]
    subject: str | None
    asset_ids: list[uuid.UUID]
    occurrences: int
    evidence_ids: list[uuid.UUID]
    proposed: dict[str, Any]
    fallback_asset_ids: list[uuid.UUID]
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    decision_note: str | None
    claim_record_id: uuid.UUID | None
    signature_id: uuid.UUID | None


class H3TaskRef(BaseModel):
    task_id: uuid.UUID
    status: str
    assignee_id: uuid.UUID


class ExceptionSet(BaseModel):
    run_id: uuid.UUID
    #: `register_hash` of H3's undecided-or-decided set; None when there is none.
    set_hash: str | None
    exceptions: list[ExceptionOut]
    task: H3TaskRef | None
