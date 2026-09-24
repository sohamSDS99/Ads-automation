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
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

import structlog
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from agent.calc.registry import FORMULAS
from agent.db.models import ApprovalRequiredRole, Evidence, EvidenceSource, Project, Run, RunStage
from agent.llm.gateway import LLMGateway, StructuredCompletion
from agent.llm.ledger import RunLedger
from agent.llm.router import ModelRouter, TaskClass

if TYPE_CHECKING:  # pragma: no cover - import cycle at runtime, not at type time
    from agent.orchestrator.plan_calc import PlanCalcRunner
    from agent.planning.constants import PlanningConstants
    from agent.schemas.plan_input import PlanInput

log = structlog.get_logger(__name__)


class NodeContractError(RuntimeError):
    """A node broke the contract this module defines.

    Declared here rather than in the executor because `RunContext` raises it:
    the executor imports it back, so there is still one class and one message
    style for "the node did something the shape does not allow".
    """


#: Any field with this name, at any depth of a node's output, must name evidence
#: the node actually gathered.
EVIDENCE_FIELD = "evidence_ids"

#: The Stage 02 counterpart (PRD §9.1 item 4). Where `evidence_ids` says "a
#: connector observed this", `calc_evidence_ids` says "a registered formula
#: computed this" — and the executor checks it against the `derived` rows the
#: node actually produced, so a number the model invented cannot cite its way
#: into a plan.
CALC_EVIDENCE_FIELD = "calc_evidence_ids"


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
    #: The `formula_id`s this node is permitted to invoke (Stage 02 PRD §8.1).
    #: Enforced at runtime by `ctx.plan.calc.run()`, which is the only way a
    #: node reaches `agent/calc/` at all — so this is an allow-list rather
    #: than documentation, and a node that grows a new calculation has to say
    #: so here before it can make one.
    calc: tuple[str, ...] = ()
    #: Whether this gate has a question to ask *every* time it is reached.
    #:
    #: False for every gate before Stage 03: reaching the node meant halting.
    #: 3.5.1 is the first that can legitimately have nothing to ask — when a
    #: current `SignOffMatrix` already names the three owners, the answer is on
    #: file, and halting would be asking somebody to re-approve their own
    #: unchanged decision (PRD §11).
    #:
    #: Declared here rather than left implicit in the node so that the registry,
    #: the DAG and `gates.py` still see a gate that exists. The node then
    #: answers *this* run with `gate_required()`, and the registry refuses the
    #: declaration unless both halves are present.
    gate_conditional: bool = False
    #: 'G1'..'G4' for plan gates. Written onto `Approval.gate_key`, which is
    #: what routes the card to `Project.settings.plan_approvers[gate_key]` and
    #: what the four-gate freeze in S2-P5 counts.
    gate_key: str | None = None
    #: 'H1' | 'H2'. A node that halts for exactly one named person rather than
    #: for any holder of a role (Stage 03 PRD §8.1 item 2, §8.4). Mutually
    #: exclusive with `gate_key`: an approval and a person-task are different
    #: primitives, and the difference is the whole of law 23.
    human_task_key: str | None = None
    #: Whether this person-task has somebody to ask on *every* run.
    #:
    #: False for H1: reaching 3.2.3 means there are claims and somebody has to
    #: sign them. True for H2, which is the first person-task that can
    #: legitimately have nothing to ask — §11 requires 3.3.2 to emit
    #: `status='not_required'` and create **no task** when 3.3.1 found nothing
    #: needing verification, and opening one anyway would put "verify nothing"
    #: in front of a company officer.
    #:
    #: The exact twin of `gate_conditional`, down to the registry refusing the
    #: declaration unless `task_required()` is implemented alongside it. Two
    #: mechanisms for "this halt is conditional" would eventually disagree
    #: about which one the executor consults.
    human_task_conditional: bool = False
    #: The `GuidelineInput` fields this node reads *when they are bound*. Every
    #: one of them can be None on a standalone run, and the unbound golden
    #: fixture exercises exactly that (PRD §4.3, §8.1 item 2).
    optional_inputs: tuple[str, ...] = ()
    #: Stage 04 PRD §8.1 item 2: the modalities this node may submit to
    #: `media/jobs.py`. A submit outside the list raises in the job layer, and
    #: the executor re-checks every `GenerationJob` the node left behind.
    media: tuple[Literal["image", "video"], ...] = ()
    #: Stage 04 PRD §8.1 item 2 and law 33: every `CreativeAsset` this node
    #: persisted with `status != draft` must carry a passing `LintResult`
    #: against the run's current pin. The executor asserts it after `reason()`.
    lint_required: bool = False
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

    @model_validator(mode="after")
    def _conditional_needs_a_gate(self) -> NodeSpec:
        if self.gate_conditional and not self.gate:
            raise ValueError(
                "gate_conditional is set on a node that is not a gate. There is no gate to "
                "skip, so the flag can only mislead a reader of the spec."
            )
        return self

    @model_validator(mode="after")
    def _plan_gate_needs_a_key(self) -> NodeSpec:
        """A plan gate without a `gate_key` is a gate nothing can route or count.

        Only plan gates: the three research gates predate `Approval.gate_key`
        and their rows read `R0`, which migration 0013 chose deliberately over
        inventing labels for history. Requiring a key of them now would be
        that invention arriving through the back door.
        """
        if self.gate and self.run_stage is RunStage.PLAN and not self.gate_key:
            raise ValueError(
                f"plan gate {self.id} declares no gate_key — "
                "G1..G4 is how the card is routed and how the freeze counts it"
            )
        if self.gate and self.run_stage is RunStage.CREATIVE and not self.gate_key:
            # Stage 04 PRD §5.3: G7 routes to the performance owner and G8/G8b
            # to the brand owner, by key. An unkeyed creative gate reaches nobody.
            raise ValueError(
                f"creative gate {self.id} declares no gate_key — G7, G8 and G8b are how "
                "the card reaches the owner the sign-off matrix names"
            )
        if self.gate_key and not self.gate:
            raise ValueError(
                f"node {self.id} declares gate_key {self.gate_key!r} but is not a gate"
            )
        return self

    @model_validator(mode="after")
    def _not_both_gate_and_person_task(self) -> NodeSpec:
        """A node is an approval or a person-task, never both (PRD §8.1 item 2).

        The two resume differently and on purpose. An approval may be confirmed
        by any holder of `required_role`, and an admin holds every role. A
        person-task is assigned to one named identity with no role fallback and
        no admin override — that is law 23, and for H1 it is what makes the
        signature a signature.

        A node declaring both would carry two resumption paths, and the
        role-based one is precisely the one an admin could walk through. The
        contradiction is refused at import rather than discovered the first time
        somebody signs something they should not have been able to.
        """
        if self.human_task_key and (self.gate or self.gate_key):
            raise ValueError(
                f"node {self.id} declares both a gate and a person-task "
                f"(gate_key={self.gate_key!r}, human_task_key={self.human_task_key!r}). "
                "An approval routes to a role; a person-task routes to one named "
                "person with no admin fallback. A node cannot be both."
            )
        return self

    @model_validator(mode="after")
    def _media_and_lint_are_creative(self) -> NodeSpec:
        """`media` and `lint_required` mean something only in the creative DAG.

        Declared on a research or plan node they would read as enforced while
        nothing checks them — no other stage submits media or writes assets.
        """
        if (self.media or self.lint_required) and self.run_stage is not RunStage.CREATIVE:
            raise ValueError(
                f"node {self.id} declares media/lint_required but runs in the "
                f"{self.run_stage.value} DAG; only creative nodes submit media or emit assets"
            )
        if len(set(self.media)) != len(self.media):
            raise ValueError(f"node {self.id} declares a media modality twice: {self.media}")
        return self

    @model_validator(mode="after")
    def _calc_is_registered(self) -> NodeSpec:
        """Every permitted formula must exist.

        Import time, not run time. A typo in the allow-list would otherwise
        surface as "this node may not call the formula it is trying to call",
        forty minutes into a run, and read like a policy decision rather than
        the misspelling it is.
        """
        unknown = [formula_id for formula_id in self.calc if formula_id not in FORMULAS]
        if unknown:
            raise ValueError(
                f"node {self.id} permits unregistered formula(s): {', '.join(sorted(unknown))}. "
                f"Registered: {', '.join(sorted(FORMULAS))}"
            )
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


@dataclass(frozen=True, slots=True)
class PlanContext:
    """What a plan node gets that a research node does not (Stage 02 PRD §8.1).

    Built once per run by the executor and shared by every node in it. Three
    things travel together because a plan node needs all three or none: the
    research it is planning from, the arithmetic layer it must route every
    number through, and the constants that arithmetic was parameterised with.

    Absent on a research run. `ctx.require_plan()` is how a node asks for it,
    and the error it raises names the node — a plan node that ended up in the
    research DAG is a registration bug, and "NoneType has no attribute input"
    would not say so.
    """

    #: The accepted research, assembled once at run start (law 13). Read-only
    #: by construction: `PlanInput` is a frozen model.
    input: PlanInput
    #: The only door to `agent/calc/`, scoped to one node's allow-list.
    calc: PlanCalcRunner
    #: `planning_constants.yaml` with this project's overrides merged in.
    constants: PlanningConstants


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
    #: Stage 02 only. None on a research run, and on a plan run only if the
    #: executor could not build a `PlanInput` — which it treats as fatal, so a
    #: node never sees that second case.
    plan: PlanContext | None = None
    _progress: Callable[[str, str], Awaitable[None]] | None = None

    def require_plan(self) -> PlanContext:
        """The plan context, or a failure that says which node asked for it."""
        if self.plan is None:
            raise NodeContractError(
                f"node {self.node_id or '?'} asked for the plan context on a "
                f"{self.run.stage.value} run. A plan node must declare "
                "run_stage=RunStage.PLAN."
            )
        return self.plan

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


@runtime_checkable
class ConditionalGate(Protocol):
    """A gate node that decides, per run, whether it has anything to ask.

    Implemented only alongside `NodeSpec.gate_conditional`; the registry refuses
    either half on its own. `output` is the node's validated output model, so a
    node answers from what it just produced rather than by re-reading the world.
    """

    def gate_required(self, ctx: RunContext, output: BaseModel) -> bool: ...


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
    return collect_ids(payload, EVIDENCE_FIELD)


def collect_calc_evidence_ids(payload: Any) -> set[uuid.UUID]:
    """Every `calc_evidence_ids` value anywhere in a plan node's output.

    Separate from the call above rather than one call with a flag, because the
    two sets are checked against different things: `evidence_ids` against what
    the node gathered, `calc_evidence_ids` against the `derived` rows it
    produced. Conflating them would let a node cite a connector's row as the
    calculation behind a number.
    """
    return collect_ids(payload, CALC_EVIDENCE_FIELD)


def collect_ids(payload: Any, field_name: str) -> set[uuid.UUID]:
    """Every UUID under `field_name`, at any depth of a node's output."""
    found: set[uuid.UUID] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == field_name and isinstance(value, list | tuple | set):
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


def derived_ids(evidence: Iterable[Evidence]) -> set[uuid.UUID]:
    """The `derived` rows among a node's gathered evidence.

    This is the set `calc_evidence_ids` must be a subset of (PRD §9.1 item 4).
    A plan node's `gather()` returns Stage 01 evidence *and* the rows its calc
    step just wrote; only the second kind is a calculation.
    """
    return {row.id for row in evidence if row.source is EvidenceSource.DERIVED}


def _as_uuid(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None
