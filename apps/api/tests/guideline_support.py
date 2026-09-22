"""A `RunContext` for one Stage 03 node, without a database or a model.

The plan suite's `plan_support.harness` does the same job for Stage 02 and is
not reusable here: a guideline node has no `PlanInput` and no calc runner, and
law 21 says it must run on a project that has neither. What it does have that a
plan node does not is direct `ctx.db` access — 3.5.1 reads the workspace roster
and the current sign-off matrix — so this harness scripts the session too.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from agent.db.models import (
    Evidence,
    EvidenceSource,
    Membership,
    Project,
    Run,
    RunStage,
    User,
    UserRole,
    UserStatus,
)
from agent.llm.router import TaskClass
from agent.nodes.base import RunContext

WORKSPACE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a0")
PROJECT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b0")
RUN_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c0")


@dataclass(slots=True)
class Usage:
    prompt_tokens: int = 10
    completion_tokens: int = 20


@dataclass(slots=True)
class Completion:
    """`plan_support`'s stub, restated rather than imported.

    Importing one test module from another couples two suites that have no
    reason to move together — and `tests/` is a package, so the import would
    work and the coupling would be invisible.
    """

    value: BaseModel
    model: str = "test/model"
    usage: Usage = field(default_factory=Usage)
    cost_usd: Decimal = Decimal("0.001")
    repairs: int = 0
    latency_ms: int = 1
    prompt: str = ""


@dataclass(slots=True)
class ScriptedLLM:
    """Answers each completion from a table keyed by output-model name."""

    answers: dict[str, Any]
    prompts: list[tuple[str, str, str]] = field(default_factory=list)

    async def complete_structured(
        self, *, output_model: type[BaseModel], system: str, user: str, choice: Any, **_: Any
    ) -> Completion:
        name = output_model.__name__
        if name not in self.answers:
            raise AssertionError(f"no scripted answer for {name}")
        self.prompts.append((name, system, user))
        return Completion(value=output_model.model_validate(self.answers[name]), prompt=user)

    def user_prompt(self, output_model: str) -> str:
        return next(user for name, _system, user in self.prompts if name == output_model)

    def every_prompt(self) -> str:
        """Every system and user prompt this run produced, concatenated.

        What the canary test searches. One string rather than a list because
        the assertion is "this never appeared anywhere", and a loop over parts
        that silently iterated nothing would pass just as happily.
        """
        return "\n".join(f"{system}\n{user}" for _name, system, user in self.prompts)


class StubRouter:
    def choose(self, task_class: TaskClass) -> str:
        return "test/model"

    def chain(self, task_class: TaskClass) -> tuple[str, ...]:
        return ("test/model",)


@dataclass(slots=True)
class StubLedger:
    spent: Decimal = Decimal(0)

    def record(self, *, usage: Any, cost: Decimal) -> None:
        self.spent += cost


@dataclass(slots=True)
class ScriptedResult:
    """What one `session.execute()` hands back."""

    rows: list[Any]

    def scalars(self) -> ScriptedResult:
        return self

    def all(self) -> list[Any]:
        return list(self.rows)

    def scalar_one_or_none(self) -> Any:
        return self.rows[0] if self.rows else None

    def first(self) -> Any:
        return self.rows[0] if self.rows else None


@dataclass(slots=True)
class ScriptedSession:
    """An `AsyncSession` stand-in that answers queries in the order they arrive.

    Deliberately order-dependent rather than clever: a node whose queries are
    reordered is a node whose behaviour changed, and a fake that matched on
    the SQL text would hide that behind a passing test.
    """

    results: list[list[Any]] = field(default_factory=list)
    executed: int = 0

    async def execute(self, _statement: Any, *_args: Any, **_kwargs: Any) -> ScriptedResult:
        if self.executed >= len(self.results):
            raise AssertionError(
                f"the node ran {self.executed + 1} queries and the test scripted "
                f"{len(self.results)}"
            )
        rows = self.results[self.executed]
        self.executed += 1
        return ScriptedResult(rows)


def person(name: str, role: UserRole) -> tuple[User, Membership]:
    """One workspace member: the account and the grant that makes them eligible."""
    user = User(id=uuid.uuid4(), email=f"{name}@sdsmanager.com", name=name.title())
    user.status = UserStatus.ACTIVE
    membership = Membership(
        id=uuid.uuid4(),
        workspace_id=WORKSPACE_ID,
        user_id=user.id,
        role=role,
        status=UserStatus.ACTIVE,
    )
    return user, membership


def evidence(kind: str, text: str, payload: dict[str, Any] | None = None) -> Evidence:
    """One unsaved evidence row, with the id a node would cite."""
    return Evidence(
        id=uuid.uuid4(),
        project_id=PROJECT_ID,
        source=EvidenceSource.WEB,
        kind=kind,
        payload=payload or {},
        content_text=text,
        hash=uuid.uuid4().hex,
    )


@dataclass(slots=True)
class Harness:
    ctx: RunContext
    llm: ScriptedLLM
    db: ScriptedSession


def harness(
    node_id: str,
    *,
    answers: dict[str, Any] | None = None,
    queries: list[list[Any]] | None = None,
    outputs: dict[str, dict[str, Any]] | None = None,
    settings: dict[str, Any] | None = None,
    bindings: dict[str, Any] | None = None,
) -> Harness:
    """A `RunContext` for one guideline node."""
    llm = ScriptedLLM(answers=answers or {})
    db = ScriptedSession(results=queries or [])
    run = Run(id=RUN_ID, workspace_id=WORKSPACE_ID, stage=RunStage.GUIDELINE)
    run.bindings = bindings or {}
    ctx = RunContext(
        run=run,
        project=Project(
            id=PROJECT_ID,
            workspace_id=WORKSPACE_ID,
            name="SDS Manager",
            domain="sdsmanager.com",
            product_context={},
            markets=["GB"],
            settings=settings or {},
        ),
        db=db,  # type: ignore[arg-type]
        llm=llm,  # type: ignore[arg-type]
        router=StubRouter(),  # type: ignore[arg-type]
        ledger=StubLedger(),  # type: ignore[arg-type]
        outputs=outputs or {},
        node_id=node_id,
    )
    return Harness(ctx=ctx, llm=llm, db=db)
