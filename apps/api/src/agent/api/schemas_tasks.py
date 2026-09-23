"""Wire shapes for person-tasks (PRD §8.4, §15.3 D).

The one thing these schemas exist to make impossible: a card that offers a
control to somebody who may not use it. `can_submit` is computed by the server
on every row, and the interface renders from it rather than re-deriving
"assignee, or approver, or admin" — because the correct answer is *only the
assignee*, and an interface that re-derived it would eventually get that wrong
in the direction that matters (PRD §15.3 D).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class HumanTaskSummary(BaseModel):
    """One person-task, as the inbox and the card both render it."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    project_name: str | None = None
    guideline_run_id: uuid.UUID | None = None
    node_id: str | None = None
    #: 'H1' | 'H2'. What the card branches on.
    task_key: str
    title: str
    instructions: str
    assignee_id: uuid.UUID
    #: Rendered prominently, and shown to everyone — a task nobody can see the
    #: owner of is a task that stalls silently.
    assignee_name: str | None = None
    assignee_email: str | None = None
    required_artifacts: dict[str, Any] = Field(default_factory=dict)
    attachment_paths: list[str] = Field(default_factory=list)
    submitted_payload: dict[str, Any] | None = None
    status: str
    #: 'publish' | 'launch'. The chip that says what stops without this.
    blocking_for: str
    completed_by: uuid.UUID | None = None
    completed_at: datetime | None = None
    due_at: datetime | None = None
    created_at: datetime
    #: The server's answer to "may *this* caller act". There is no second
    #: implementation of this rule anywhere in the frontend.
    can_submit: bool = False
    #: True when the caller holds `user_manage` and could hand this over.
    can_reassign: bool = False


class HumanTaskList(BaseModel):
    items: list[HumanTaskSummary]
    #: Outstanding tasks assigned to the caller, whatever filter was applied.
    #: The sidebar badge sums this with its approvals twin, and a filtered list
    #: must not be able to make the badge lie.
    mine_open: int = 0


class HumanTaskSubmit(BaseModel):
    """What the assignee attests to.

    `payload` is free-form because H1 and H2 attest to different things and a
    third person-task will attest to a third. `artifacts_confirmed` is not: it
    is the checklist, and the route refuses a submission that skips one.
    """

    payload: dict[str, Any] = Field(default_factory=dict)
    artifacts_confirmed: list[str] = Field(default_factory=list)
    reference: str | None = Field(default=None, max_length=500)


class HumanTaskAttachment(BaseModel):
    """One stored file. `path` is a storage key, never a browser-reachable URL."""

    path: str
    filename: str
    size_bytes: int


class HumanTaskReassign(BaseModel):
    """Handing a non-delegable duty to somebody else.

    The reason is mandatory and stored. Reassignment is meant to be expensive:
    the cheap version of this is an administrator quietly doing the task
    themselves, which is the one thing the whole subsystem exists to prevent.
    """

    to_user_id: uuid.UUID
    reason: str = Field(min_length=1, max_length=2000)


class ReassignPreview(BaseModel):
    """What a handover would cost, counted by the server before anybody confirms.

    The interface states these numbers *before* the confirm control is
    reachable (PRD §15.3 C.6). A count the client estimated would eventually
    disagree with what actually happened, and the one place that must never
    happen is the screen that voids somebody's legal signature.
    """

    task_id: uuid.UUID
    from_user_id: uuid.UUID
    from_user_name: str | None = None
    to_user_id: uuid.UUID
    to_user_name: str | None = None
    #: Signatures this handover would void, named rather than counted.
    voided_signature_ids: list[uuid.UUID] = Field(default_factory=list)
    #: Claims that return to `pending_signoff` and must be signed again.
    requeued_claim_ids: list[uuid.UUID] = Field(default_factory=list)
    voided_count: int = 0
    requeued_count: int = 0
