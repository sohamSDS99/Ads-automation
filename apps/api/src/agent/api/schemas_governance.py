"""Wire shapes for the sign-off matrix and the guideline gate approvers (PRD §11, §16)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class OwnerSlot(BaseModel):
    """One of the three ownership slots, with enough to render a name not an id."""

    user_id: uuid.UUID
    name: str | None = None
    email: str | None = None
    role: str | None = None


class SignOffMatrixOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    brand_owner: OwnerSlot
    legal_owner: OwnerSlot
    performance_owner: OwnerSlot
    version: int
    set_by: uuid.UUID
    set_by_name: str | None = None
    set_at: datetime
    #: True when the caller may change it. `settings_write`, and the server's
    #: answer rather than the client's guess.
    can_edit: bool = False


class SignOffMatrixState(BaseModel):
    """The current matrix, or plainly the absence of one.

    A project with no matrix is a normal early state, not an error: G6 has not
    been decided yet. The screen says so and links to the gate, which is why
    this is a `200` with `matrix: null` rather than a `404`.
    """

    matrix: SignOffMatrixOut | None = None
    #: Who could be named. Only active members; only `approver` may hold legal.
    eligible_owners: list[OwnerSlot] = Field(default_factory=list)
    eligible_legal_owners: list[OwnerSlot] = Field(default_factory=list)
    can_edit: bool = False


class SignOffMatrixUpdate(BaseModel):
    """Proposed owners.

    `reason` is required whenever the legal owner changes and ignored otherwise.
    The route decides which case this is — a client that decided for itself
    could route a legal-owner change through the no-ceremony path.
    """

    brand_owner_id: uuid.UUID
    legal_owner_id: uuid.UUID
    performance_owner_id: uuid.UUID
    reason: str | None = Field(default=None, max_length=2000)


class MatrixChangePreview(BaseModel):
    """What a proposed matrix would cost, counted server-side (PRD §15.3 C.6).

    Returned both by the preview and by the write, so the number the person
    confirmed and the number that happened come from one query.
    """

    legal_owner_changes: bool = False
    from_legal_owner_id: uuid.UUID | None = None
    from_legal_owner_name: str | None = None
    to_legal_owner_id: uuid.UUID | None = None
    to_legal_owner_name: str | None = None
    voided_signature_ids: list[uuid.UUID] = Field(default_factory=list)
    requeued_claim_ids: list[uuid.UUID] = Field(default_factory=list)
    voided_count: int = 0
    requeued_count: int = 0
    #: True when a reason must accompany the write. Mirrors the route's own
    #: rule so the form can require the field before the round trip.
    reason_required: bool = False


class GuidelineApproversPatch(BaseModel):
    """Who decides G5 and G6. `None` clears the assignment back to any approver."""

    G5: uuid.UUID | None = None
    G6: uuid.UUID | None = None
