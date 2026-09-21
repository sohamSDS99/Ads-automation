"""The node contract (PRD §7.1).

Every node implements one interface: declare a `NodeSpec`, `gather()` evidence,
`reason()` over it. No exceptions, and no node reaches outside this file for the
model — `ctx.complete()` is the only door to the LLM, which is what makes cost
accounting, the budget cap and the prompt record impossible to bypass.

Law 1 of PRD §18 lives here too: an LLM never sources a fact. `gather()` returns
`Evidence` rows written by connectors, and anything the model asserts carries
`evidence_ids` drawn from that set — the executor checks the subset relation and
fails the node if it does not hold.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

import structlog
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import ApprovalRequiredRole, Evidence, Project, Run, RunStage
from agent.llm.gateway import LLMGateway, StructuredCompletion
from agent.llm.ledger import RunLedger
from agent.llm.router import ModelRouter, TaskClass

log = structlog.get_logger(__name__)

#: Any field with this name, at any depth of a node's output, must name evidence
#: the node actually gathered.
EVIDENCE_FIELD = "evidence_ids"


class NodeSpec(BaseModel):
    """What a node is. Declared once, read by the registry, the DAG and the UI."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    id: str = Field(description="Dotted node id, e.g. '1.3.2'")
    name: str
    stage: str = Field(description="Dotted stage id, e.g. '1.3'")
    #: Which pipeline this node belongs to — PRD §8.1's "stage", and *not*
    #: `NodeSpec.stage`, which is the dotted group inside a pipeline. One
    #: registry holds both (node ids are globally unique); `get_dag` is what
    #: partitions them, so a node declaring the wrong pipeline is a node that
    #: runs in the wrong DAG rather than one that collides with another id.
    run_stage: RunStage = RunStage.RESEARCH
    depends_on: tuple[str, ...] = ()
    gate: bool = False
    required_role: ApprovalRequiredRole | None = None
    task_class: TaskClass
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    connectors: tuple[str, ...] = ()
    version: int = Field(
        default=1,
        description=(
            "Bumped when the prompt or output shape changes. Part of the cache key, "
            "so a bumped node re-runs instead of reusing an output shaped by the old prompt."
        ),
    )

    @field_validator("id", "stage")
    @classmethod
    def _dotted(cls, value: str) -> str:
        if not value or any(part == "" for part in value.split(".")):
            raise ValueError(f"{value!r} is not a dotted id")
        return value

    @model_validator(mode="after")
    def _gate_needs_role(self) -> NodeSpec:
        # A field validator would not fire here: `required_role` defaults to
        # None and is usually not passed at all, so the only place that sees
        # both fields is an after-validator on the model.
        if self.gate and self.required_role is None:
            raise ValueError("a gate node must declare required_role — someone has to decide it")
        return self


@dataclass(slots=True)
class NodeTelemetry:
    """What one node's execution cost and how it got there.

    Filled in by `ctx.complete()`, read by the executor when `reason()` returns.
    A node that makes several calls (batched classification, for example)
    accumulates rather than overwrites.
    """

    completions: list[StructuredCompletion[Any]] = field(default_factory=list)

    @property
    def model(self) -> str | None:
        """The model that actually answered — the substitute, if one was used (PRD §16)."""
        return self.completions[-1].model if self.completions else None

    @property
    def token_in(self) -> int:
        return sum(item.usage.prompt_tokens for item in self.completions)

    @property
    def token_out(self) -> int:
        return sum(item.usage.completion_tokens for item in self.completions)

    @property
    def cost_usd(self) -> Decimal:
        return sum((item.cost_usd for item in self.completions), Decimal(0))

    @property
    def repairs(self) -> int:
        return sum(item.repairs for item in self.completions)

    @property
    def prompt(self) -> str | None:
        if not self.completions:
            return None
        return "\n\n=== next call ===\n\n".join(item.prompt for item in self.completions)


@dataclass(slots=True)
class RunContext:
    """Everything a node may touch, and nothing else."""

    run: Run
    project: Project
    db: AsyncSession
    llm: LLMGateway
    router: ModelRouter
    ledger: RunLedger
    outputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    telemetry: NodeTelemetry = field(default_factory=NodeTelemetry)
    node_id: str = ""
    #: Per-**run** scratch space, owned by the executor and shared by every
    #: node in the run. A new `RunContext` is built for each node attempt, so
    #: anything that has to outlive one node — `gather` remembering that a
    #: connector is unconfigured, for instance — belongs here rather than on
    #: the context itself. Nothing in it is persisted or resumed.
    scratch: dict[str, Any] = field(default_factory=dict)
    _progress: Callable[[str, str], Awaitable[None]] | None = None

    def output_of(self, node_id: str) -> dict[str, Any]:
        """The output of a node this one depends on."""
        try:
            return self.outputs[node_id]
        except KeyError as exc:
            raise KeyError(
                f"node {self.node_id or '?'} asked for the output of {node_id}, which has not "
                "run. Add it to depends_on."
            ) from exc

    async def progress(self, message: str) -> None:
        """Emit a free-text status line to the run console (PRD §7.3 `node.progress`)."""
        if self._progress is not None:
            await self._progress(self.node_id, message)

    async def complete[T: BaseModel](
        self,
        output_model: type[T],
        *,
        system: str,
        user: str,
        task_class: TaskClass | None = None,
    ) -> T:
        """The only way a node reaches a model.

        Routing, cost and the prompt record are handled here rather than in the
        node, so no node can spend money the ledger does not see.
        """
        choice = self.router.choose(task_class or self._task_class())
        completion = await self.llm.complete_structured(
            output_model=output_model,
            system=system,
            user=user,
            choice=choice,
            on_progress=self.progress,
        )
        self.telemetry.completions.append(completion)
        self.ledger.record(usage=completion.usage, cost=completion.cost_usd)
        return completion.value

    def _task_class(self) -> TaskClass:
        from agent.orchestrator.registry import get_registry

        return get_registry().spec(self.node_id).task_class


@runtime_checkable
class Node(Protocol):
    """The interface `executor.run_node()` drives."""

    spec: NodeSpec

    async def gather(self, ctx: RunContext) -> list[Evidence]: ...

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel: ...


class LLMNode:
    """Base class for the ordinary case: gather nothing new, prompt, validate.

    A node overrides `gather()` when it needs evidence from a connector, and
    `reason()` only when a single completion is not the right shape for it (a
    pandas computation, a batched classification). Everything else is two
    prompt methods.
    """

    spec: NodeSpec

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        return []

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        return await ctx.complete(
            self.spec.output_model,
            system=self.system_prompt(ctx),
            user=self.user_prompt(ctx, ev),
            task_class=self.spec.task_class,
        )

    def system_prompt(self, ctx: RunContext) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def user_prompt(self, ctx: RunContext, ev: Sequence[Evidence]) -> str:  # pragma: no cover
        raise NotImplementedError


def collect_evidence_ids(payload: Any) -> set[uuid.UUID]:
    """Every `evidence_ids` value anywhere in a node's output.

    Claims nest — `segments[].evidence_ids` in 1.1.2, `whitespace[]` in 1.3.4 —
    so the check walks the whole document rather than the top level.
    """
    found: set[uuid.UUID] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == EVIDENCE_FIELD and isinstance(value, list | tuple | set):
                    for item in value:
                        parsed = _as_uuid(item)
                        if parsed is not None:
                            found.add(parsed)
                else:
                    walk(value)
        elif isinstance(node, list | tuple):
            for item in node:
                walk(item)

    walk(payload)
    return found


def _as_uuid(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None
