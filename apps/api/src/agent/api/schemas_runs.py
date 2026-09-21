"""Request and response models for the run API (PRD §14), exported to contracts."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent.db.models import NodeRunStatus, RunMode, RunStage, RunStatus, RunTrigger
from agent.llm.router import TaskClass


class LaunchRunRequest(BaseModel):
    """`POST /projects/{id}/runs` — `{mode, node_filter?, reuse_cache?}`."""

    mode: RunMode = RunMode.FULL
    node_ids: list[str] | None = Field(
        default=None,
        description=(
            "Partial runs only. The selection is widened to include everything "
            "these nodes depend on, because a node cannot run without its inputs."
        ),
    )
    reuse_cache: bool = Field(
        default=False,
        description="Reuse a previous succeeded output when the input hash and node version match.",
    )


class DagEdge(BaseModel):
    """`source` must succeed before `target` may start."""

    source: str
    target: str


class NodeState(BaseModel):
    """One node of the DAG, with whatever this run has done to it so far."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    stage: str
    task_class: TaskClass
    depends_on: list[str]
    gate: bool
    status: NodeRunStatus | None = Field(
        default=None, description="Null until the run reaches this node."
    )
    attempt: int | None = None
    model: str | None = None
    token_in: int | None = None
    token_out: int | None = None
    cost_usd: Decimal | None = None
    latency_ms: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: dict[str, Any] | None = None


class DegradedSource(BaseModel):
    """One source that did not fully answer during this run (PRD §15 NF4).

    The console renders these as a banner and the report repeats them, so
    `detail` carries the connector's own words — for the Transparency Center
    that is the name of the selector that stopped matching, which is the whole
    point of PRD §16's first row.
    """

    kind: str = Field(description="The kind of evidence that came back thin, e.g. `creative`.")
    nodes: list[str] = Field(
        default_factory=list, description="Which nodes hit it. Sorted, for a stable banner."
    )
    detail: str | None = Field(
        default=None, description="What the connector said went wrong. Shown verbatim."
    )
    severity: Literal["degraded", "unavailable"] = Field(
        default="degraded",
        description=(
            "`degraded` — a connector malfunctioned mid-run and the report is thinner for it. "
            "`unavailable` — nothing of this kind is connected, which is a setup state and "
            "not a fault."
        ),
    )


class RunResponse(BaseModel):
    """`GET /runs/{id}` — the full state of a run, including its DAG."""

    id: uuid.UUID
    project_id: uuid.UUID
    stage: RunStage = Field(
        default=RunStage.RESEARCH,
        description=(
            "Which DAG this run executes. The console reads it to refuse a run opened "
            "under the wrong stage's route, so a research id pasted into the plan "
            "console says so instead of rendering a plan-shaped shell around research "
            "nodes."
        ),
    )
    status: RunStatus
    mode: RunMode
    trigger: RunTrigger
    triggered_by: uuid.UUID | None
    triggered_by_name: str | None = Field(
        default=None,
        description=(
            "Display name behind `triggered_by`. Null for a scheduled run, which has no "
            "actor, and for a person since removed from the workspace."
        ),
    )
    parent_run_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "The run this one follows — the newest succeeded run of the same project at the "
            "moment this one launched. Null for a project's first run. The Report Viewer's "
            "compare toggle is offered only when this is set."
        ),
    )
    selected_node_ids: list[str]
    cost_usd: Decimal
    token_in: int
    token_out: int
    started_at: datetime | None
    finished_at: datetime | None
    error: dict[str, Any] | None
    nodes: list[NodeState]
    edges: list[DagEdge]
    degraded_sources: list[DegradedSource] = Field(
        default_factory=list,
        description=(
            "Sources that did not fully answer. Derived from the nodes' own `coverage` "
            "output, so it is durable and survives a page reload — the SSE stream is not "
            "the only place this appears."
        ),
    )


class NodeRunDetail(BaseModel):
    """`GET /runs/{id}/nodes/{node_id}` — output, evidence, prompt, metrics."""

    run_id: uuid.UUID
    node_id: str
    name: str
    stage: str
    status: NodeRunStatus
    attempt: int
    input_hash: str | None
    output: dict[str, Any] | None
    evidence_ids: list[uuid.UUID]
    prompt: str | None
    model: str | None
    token_in: int
    token_out: int
    cost_usd: Decimal
    latency_ms: int | None
    started_at: datetime | None
    finished_at: datetime | None
    error: dict[str, Any] | None


class RunViewer(BaseModel):
    """One person with this run's console open (PRD §13.4 B)."""

    id: uuid.UUID
    name: str


class PresenceResponse(BaseModel):
    """`POST /runs/{id}/presence` — check in, and see who else is here.

    `total` counts everyone watching; `viewers` is capped, because a console
    drawing forty avatars is not telling anyone anything.
    """

    viewers: list[RunViewer]
    total: int
