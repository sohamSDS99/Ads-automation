"""Wire shapes for recurring runs (PRD §14 `POST /schedules`, `PATCH /schedules/{id}`).

Two of these fields — `description` and `upcoming` — are derived, never stored.
They exist because PRD §13.4 E asks for a "human-readable preview" and the only
honest preview of a cron expression is the actual instants it resolves to. The
server computes both from the same parser the poller fires on, so the preview
cannot promise a time the scheduler would not pick.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent.scheduling import cron

#: How many future firings the preview shows. Three is enough to see the shape
#: of a weekly schedule without turning the panel into a calendar.
PREVIEW_COUNT = 3


def _validate_cron(value: str) -> str:
    """Reject at the edge, in the user's words, rather than storing a dead row."""
    try:
        cron.parse(value)
    except cron.CronError as exc:
        raise ValueError(str(exc)) from exc
    return value.strip().lower()


def _validate_timezone(value: str) -> str:
    try:
        cron.load_timezone(value)
    except cron.CronError as exc:
        raise ValueError(str(exc)) from exc
    return value


class ScheduleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: uuid.UUID
    cron: str = Field(
        description="Five-field cron, or a @macro. Minute hour day-of-month month day-of-week.",
        examples=["0 3 * * 1", "@weekly"],
    )
    timezone: str = Field(
        default="UTC",
        description="IANA zone the expression is read in. The run fires at that wall-clock time "
        "year round, so a summer-time change moves the UTC instant, not the local one.",
        examples=["UTC", "Europe/Copenhagen"],
    )
    enabled: bool = True

    _check_cron = field_validator("cron")(_validate_cron)
    _check_timezone = field_validator("timezone")(_validate_timezone)


class ScheduleUpdate(BaseModel):
    """Every field optional. An omitted field is left alone; there is no clearing."""

    model_config = ConfigDict(extra="forbid")

    cron: str | None = None
    timezone: str | None = None
    enabled: bool | None = None

    @field_validator("cron")
    @classmethod
    def _cron(cls, value: str | None) -> str | None:
        return None if value is None else _validate_cron(value)

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, value: str | None) -> str | None:
        return None if value is None else _validate_timezone(value)


class SchedulePreviewRequest(BaseModel):
    """Check an expression before saving it. Writes nothing."""

    model_config = ConfigDict(extra="forbid")

    cron: str
    timezone: str = "UTC"

    _check_cron = field_validator("cron")(_validate_cron)
    _check_timezone = field_validator("timezone")(_validate_timezone)


class SchedulePreview(BaseModel):
    """What an expression means, in words and in instants."""

    cron: str
    timezone: str
    description: str = Field(description="One sentence a non-engineer can check the schedule by.")
    upcoming: list[datetime] = Field(
        default_factory=list,
        description=f"The next {PREVIEW_COUNT} firings, in UTC. Empty only if none exist.",
    )


class ScheduleResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    project_name: str
    cron: str
    timezone: str
    enabled: bool
    description: str
    upcoming: list[datetime] = Field(default_factory=list)
    next_at: datetime | None = Field(
        default=None,
        description="When the poller will next claim this row. None while disabled.",
    )
    last_run_id: uuid.UUID | None = None
    last_run_at: datetime | None = None
    last_run_status: str | None = None
    created_by: uuid.UUID
    created_by_name: str


class ScheduleList(BaseModel):
    items: list[ScheduleResponse] = Field(default_factory=list)
