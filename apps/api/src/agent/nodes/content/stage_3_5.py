"""Stage 3.5 — who signs off. Node 3.5.1 and gate G6.

3.5.1 executes first and depends on nothing (PRD §11's DAG edges, law 28): you
cannot route a non-delegable signature without a named owner, so the matrix is
the root and H1 hangs off it.

Two things about this node are unlike every gate before it.

**It can have nothing to ask.** When a current `SignOffMatrix` already names the
three owners, the answer is on file and re-asking would mean putting somebody's
own unchanged decision back in front of them. That is what `gate_conditional`
and `gate_required()` are for — see `nodes/base.NodeSpec.gate_conditional`.

**It validates identity rather than trusting it.** Everything the model returns
is checked against real, active workspace membership before it can become a
proposal, and the legal owner is checked against law 23: `CLAIM_SIGN` is held by
`approver` and *not* by `admin`. An admin legal owner is not a slightly-wrong
suggestion — it is an owner no signature could ever be routed to.

The node proposes. G6 decides. The row is written when the gate is approved
(`api/routes_approvals.py`), because a gate node does not re-execute on approval.
"""

from __future__ import annotations

import uuid
from typing import Literal

import sqlalchemy as sa
from pydantic import BaseModel, Field

from agent.db.models import (
    ApprovalRequiredRole,
    Evidence,
    Membership,
    RunStage,
    SignOffMatrix,
    User,
    UserRole,
    UserStatus,
)
from agent.llm.router import TaskClass
from agent.nodes import prompts
from agent.nodes.base import NodeContractError, NodeSpec, RunContext

#: The gate key G6 is written onto. Imported by the decision route, which turns
#: an approved proposal into the `signoff_matrix` row.
SIGNOFF_GATE = "G6"

#: Roles that may hold the legal signature (law 23). `admin` is deliberately
#: absent and this is the only place that says so for this node.
SIGNING_ROLES = frozenset({UserRole.APPROVER})


class SignOffProposal(BaseModel):
    """What the model is asked for: three ids and why."""

    brand_owner_id: uuid.UUID
    legal_owner_id: uuid.UUID
    performance_owner_id: uuid.UUID
    rationale: str = Field(min_length=1)


class Owners(BaseModel):
    brand_owner_id: uuid.UUID
    legal_owner_id: uuid.UUID
    performance_owner_id: uuid.UUID


class SignOffMatrixOutput(BaseModel):
    """3.5.1 ⛳ G6 — who owns brand, legal and performance sign-off.

    People appear as ids and never as names or email addresses: §11's critique
    assertion 10 forbids a personal name anywhere in the payload, and the
    payload is what gets exported, diffed and handed to Stage 04.
    """

    owners: Owners
    rationale: str
    reused: bool
    status: Literal["reused", "proposed"]
    #: Set only on the reuse path — the version of the matrix already on file.
    matrix_version: int | None = None


class SignOffMatrixNode:
    """3.5.1 ⛳ G6 — the DAG root."""

    spec = NodeSpec(
        id="3.5.1",
        name="signoff_matrix",
        stage="3.5",
        run_stage=RunStage.GUIDELINE,
        depends_on=(),
        gate=True,
        gate_conditional=True,
        gate_key=SIGNOFF_GATE,
        required_role=ApprovalRequiredRole.APPROVER,
        task_class=TaskClass.CLASSIFY,
        input_model=BaseModel,
        output_model=SignOffMatrixOutput,
        connectors=(),
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        """Nothing to pull. The roster is the database, not a connector."""
        return []

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        current = await self._current_matrix(ctx)
        if current is not None:
            return SignOffMatrixOutput(
                owners=Owners(
                    brand_owner_id=current.brand_owner_id,
                    legal_owner_id=current.legal_owner_id,
                    performance_owner_id=current.performance_owner_id,
                ),
                rationale=(
                    "A current sign-off matrix is already on file for this project; "
                    "its owners are carried forward unchanged."
                ),
                reused=True,
                status="reused",
                matrix_version=current.version,
            )

        roster = await self._eligible(ctx)
        if not roster:
            raise NodeContractError(
                "3.5.1 found no active workspace members, so there is nobody to own "
                "brand, legal or performance sign-off."
            )
        signers = [entry for entry in roster if entry[1].role in SIGNING_ROLES]
        if not signers:
            raise NodeContractError(
                "3.5.1 found no eligible approver in this workspace. Law 23 holds "
                "CLAIM_SIGN with `approver` and not with `admin`, so a legal owner "
                "cannot be named until somebody holds that role."
            )

        proposal = await ctx.complete(
            SignOffProposal,
            system=prompts.system_prompt(
                "You nominate the three people who own brand, legal and performance "
                "sign-off for an advertising rulebook. You choose only from the roster "
                "you are given, you never invent a person, and the legal owner must be "
                "somebody whose role is approver."
            ),
            user=prompts.compose(
                prompts.project_block(ctx.project),
                prompts.computed_block(
                    "the people in this workspace (choose only these ids)",
                    [
                        {"id": str(user.id), "role": membership.role.value}
                        for user, membership in roster
                    ],
                ),
                prompts.computed_block(
                    "the ids eligible to hold the legal signature",
                    [str(user.id) for user, _membership in signers],
                ),
            ),
        )

        self._validate(proposal, roster=roster, signers=signers)
        return SignOffMatrixOutput(
            owners=Owners(
                brand_owner_id=proposal.brand_owner_id,
                legal_owner_id=proposal.legal_owner_id,
                performance_owner_id=proposal.performance_owner_id,
            ),
            rationale=proposal.rationale,
            reused=False,
            status="proposed",
        )

    def gate_required(self, ctx: RunContext, output: BaseModel) -> bool:
        """G6 opens only when there is a decision to make (PRD §11)."""
        return not getattr(output, "reused", False)

    async def _current_matrix(self, ctx: RunContext) -> SignOffMatrix | None:
        result = await ctx.db.execute(
            sa.select(SignOffMatrix)
            .where(
                SignOffMatrix.project_id == ctx.project.id,
                SignOffMatrix.superseded_at.is_(None),
            )
            .limit(1)
        )
        return result.scalars().first()

    async def _eligible(self, ctx: RunContext) -> list[tuple[User, Membership]]:
        """Active accounts with an active membership of this workspace.

        Both statuses, not just one: `membership.status` is an unaccepted
        invitation to *this* workspace and `user.status` is the account itself,
        so a disabled account with a live membership would otherwise be
        nominated to sign something they cannot log in to do.
        """
        result = await ctx.db.execute(
            sa.select(User, Membership)
            .join(Membership, Membership.user_id == User.id)
            .where(
                Membership.workspace_id == ctx.run.workspace_id,
                Membership.status == UserStatus.ACTIVE,
                User.status == UserStatus.ACTIVE,
            )
            .order_by(User.id)
        )
        return [(row[0], row[1]) for row in result.all()]

    def _validate(
        self,
        proposal: SignOffProposal,
        *,
        roster: list[tuple[User, Membership]],
        signers: list[tuple[User, Membership]],
    ) -> None:
        known = {user.id for user, _membership in roster}
        may_sign = {user.id for user, _membership in signers}
        for field_name in ("brand_owner_id", "legal_owner_id", "performance_owner_id"):
            chosen = getattr(proposal, field_name)
            if chosen not in known:
                raise NodeContractError(
                    f"3.5.1 named {chosen} as {field_name.removesuffix('_id')}, and that is "
                    "not a member of this workspace."
                )
        if proposal.legal_owner_id not in may_sign:
            raise NodeContractError(
                f"3.5.1 named {proposal.legal_owner_id} as legal owner, and that person is "
                "not an approver. Law 23 narrows CLAIM_SIGN to `approver` and excludes "
                "`admin`, so no signature could ever be routed to them."
            )


#: Module-level instance. The registry discovers instances, not classes.
signoff_matrix = SignOffMatrixNode()
