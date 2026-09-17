"""Wire shape for `GET /runs/{id}/diff` (PRD §14).

A thin projection of `export.diff`. The dataclasses over there are the model —
they are what the tests assert on and what a future markdown renderer would
read — and these are what goes over the wire, so the API contract can gain a
field without the diff engine growing a Pydantic dependency.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from agent.export import diff as engine


class FieldChangeModel(BaseModel):
    field: str = Field(description="The changed field, or the section title for a scalar.")
    before: Any = None
    after: Any = None


class ItemDiffModel(BaseModel):
    key: str = Field(description="Identity this record was matched on across the two runs.")
    label: str
    status: Literal["added", "removed", "changed"]
    changes: list[FieldChangeModel] = Field(default_factory=list)


class SectionDiffModel(BaseModel):
    path: str = Field(description="Dotted path into the report, e.g. `demand_map.negatives`.")
    title: str
    added: int = 0
    removed: int = 0
    changed: int = 0
    total: int = 0
    items: list[ItemDiffModel] = Field(default_factory=list)
    truncated: bool = Field(
        default=False,
        description=(
            "True when more records changed than are listed. The counts above are always "
            "exact; only `items` is capped."
        ),
    )


class RunDiffResponse(BaseModel):
    run_id: uuid.UUID
    against_run_id: uuid.UUID
    generated_at: datetime
    against_generated_at: datetime
    #: Whether `against` came from `parent_run_id` rather than from the caller.
    against_is_parent: bool = False
    scalars: list[FieldChangeModel] = Field(default_factory=list)
    sections: list[SectionDiffModel] = Field(default_factory=list)
    unchanged: bool = Field(
        default=False,
        description="True when nothing the report tracks differs between the two runs.",
    )


def to_response(result: engine.ReportDiff, *, against_is_parent: bool) -> RunDiffResponse:
    """Project the engine's result, dropping sections with nothing to say.

    Unchanged sections are omitted rather than sent as zeroes: the viewer renders
    what it is given, and a list of twenty-six "no change" rows buries the three
    that matter.
    """
    return RunDiffResponse(
        run_id=result.run_id,
        against_run_id=result.against_run_id,
        generated_at=result.generated_at,
        against_generated_at=result.against_generated_at,
        against_is_parent=against_is_parent,
        scalars=[
            FieldChangeModel(field=change.field, before=change.before, after=change.after)
            for change in result.scalars
        ],
        sections=[_section(section) for section in result.changed_sections],
        unchanged=result.is_empty,
    )


def _section(section: engine.SectionDiff) -> SectionDiffModel:
    return SectionDiffModel(
        path=section.path,
        title=section.title,
        added=section.added,
        removed=section.removed,
        changed=section.changed,
        total=section.total,
        truncated=section.truncated,
        items=[
            ItemDiffModel(
                key=item.key,
                label=item.label,
                status=item.status,
                changes=[
                    FieldChangeModel(field=change.field, before=change.before, after=change.after)
                    for change in item.changes
                ],
            )
            for item in section.items
        ],
    )
