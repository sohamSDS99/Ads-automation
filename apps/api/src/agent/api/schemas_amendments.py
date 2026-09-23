"""Wire shapes for the policy amendment inbox (PRD §8.6, §15.3 G, §16)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class VoidedSignature(BaseModel):
    """A signature this amendment took away, named rather than counted.

    §15.3 G requires a `signature_affecting` row to *name* every voided
    signature and re-queued claim. A count would be cheaper and would hide the
    one fact the person reading it needs: whose signature, on what.
    """

    signature_id: uuid.UUID
    signer_id: uuid.UUID
    signer_name: str | None = None
    signed_at: datetime
    voided_at: datetime | None = None
    void_reason: str | None = None
    claim_count: int = 0


class AmendmentSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    project_name: str | None = None
    #: 'policy_watch' | 'claim_expiry' | 'disapproval' | 'manual'
    origin: str
    detected_at: datetime
    #: 'mechanical' | 'substantive' | 'signature_affecting' | 'unclassified'
    change_kind: str
    #: 'open' | 'needs_review' | 'applied' | 'dismissed' | 'auto_applied'
    status: str
    source_id: uuid.UUID | None = None
    source_label: str | None = None
    source_url: str | None = None
    diff: dict[str, Any] | None = None
    proposed_rule_changes: dict[str, Any] | None = None
    rationale: str | None = None
    #: The MINOR a `mechanical` row already produced, so the inbox can say so
    #: rather than offering an Apply button for work that is done.
    applied_ruleset_version: str | None = None
    reviewed_by: uuid.UUID | None = None
    reviewed_by_name: str | None = None
    reviewed_at: datetime | None = None
    review_note: str | None = None
    voided_signatures: list[VoidedSignature] = Field(default_factory=list)
    requeued_claim_ids: list[uuid.UUID] = Field(default_factory=list)
    #: The server's answer to "may this caller act on this row", already
    #: accounting for status. `guideline_publish`, and never re-derived.
    can_decide: bool = False


class AmendmentList(BaseModel):
    items: list[AmendmentSummary]
    #: Rows still waiting on a person, whatever filter was applied.
    open_count: int = 0


class AmendmentDismiss(BaseModel):
    """Dismissal needs a reason. It is the only artifact this branch leaves."""

    reason: str = Field(min_length=1, max_length=2000)


class AmendmentDecided(BaseModel):
    amendment: AmendmentSummary
    #: Set when applying minted a MINOR.
    ruleset_version: str | None = None
