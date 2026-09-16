"""Request and response models for the run API (PRD §14), exported to contracts."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agent.db.models import NodeRunStatus, RunMode, RunStatus, RunTrigger
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


class RunResponse(BaseModel):
    """`GET /runs/{id}` — the full state of a run, including its DAG."""

    id: uuid.UUID
    project_id: uuid.UUID
    status: RunStatus
    mode: RunMode
    trigger: RunTrigger
    triggered_by: uuid.UUID | None
    selected_node_ids: list[str]
    cost_usd: Decimal
    token_in: int
    token_out: int
    started_at: datetime | None
    finished_at: datetime | None
    error: dict[str, Any] | None
    nodes: list[NodeState]
    edges: list[DagEdge]


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
