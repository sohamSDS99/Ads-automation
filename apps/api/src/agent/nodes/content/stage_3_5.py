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

import re
import uuid
from typing import Literal

import sqlalchemy as sa
import structlog
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
from agent.guidelines import signoff
from agent.llm.router import TaskClass
from agent.nodes import prompts
from agent.nodes.base import NodeContractError, NodeSpec, RunContext

log = structlog.get_logger(__name__)

#: The gate key G6 is written onto. Imported by the decision route, which turns
#: an approved proposal into the `signoff_matrix` row.
SIGNOFF_GATE = "G6"

#: Law 23's signing roles, re-exported from `guidelines/signoff.py`. The rule
#: lives there because the G6 *decision* has to apply it too — an approver can
#: rewrite this node's proposal in a form before approving it, so a check that
#: existed only here would guard the path nobody attacks.
SIGNING_ROLES = signoff.SIGNING_ROLES


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
        """One rule, borrowed rather than restated (`guidelines/signoff.py`)."""
        try:
            signoff.assert_eligible(
                {
                    "brand_owner_id": proposal.brand_owner_id,
                    "legal_owner_id": proposal.legal_owner_id,
                    "performance_owner_id": proposal.performance_owner_id,
                },
                roster=roster,
            )
        except signoff.SignOffError as exc:
            # Re-raised as a node failure so the executor treats it as one: a
            # model that names somebody who does not work here has broken the
            # node's contract, not a person's input validation.
            raise NodeContractError(f"3.5.1 {exc}") from exc


#: Module-level instance. The registry discovers instances, not classes.
signoff_matrix = SignOffMatrixNode()


# ---------------------------------------------------------------------------
# 3.5.2 legal_review_triggers
# ---------------------------------------------------------------------------


class ReviewTriggerDraft(BaseModel):
    """One pattern that should never ship unread, as the model proposes it."""

    id: str = Field(default="", max_length=64)
    pattern_kind: Literal["term", "claim_type", "campaign_type", "market", "asset_type"]
    pattern: str = Field(min_length=1)
    why: str = Field(min_length=1)
    reviewer_role: str
    severity: Literal["blocking", "warning", "advisory"] = "warning"


class ReviewTriggersDraft(BaseModel):
    triggers: list[ReviewTriggerDraft] = Field(default_factory=list)
    always_review: list[str] = Field(default_factory=list)


class LegalReviewTriggersOutput(BaseModel):
    """3.5.2 — the copy that goes to a human before it goes to Google.

    `reviewer_role` is a **role**. Routing to a named person is `SignOffMatrix`'s
    job and it is non-delegable; a trigger naming an identity would be a second,
    weaker path to the same decision, and the weaker one is the one an operator
    could edit.
    """

    triggers: list[ReviewTriggerDraft]
    always_review: list[str]
    #: Why the list is empty, when it is. §4.3: a project with nothing risky to
    #: say has nothing to route, and that is a finding rather than a failure.
    reason: str = ""


#: Who a trigger may route to. The four workspace roles and nothing else — an
#: id here would be an identity, which is the one thing this node may not name.
REVIEWER_ROLES: frozenset[str] = frozenset(role.value for role in UserRole)


class LegalReviewTriggersNode:
    """3.5.2 — what must not ship unread, and who reads it.

    **§21 assigns this node to no phase.** §11 defines it, §12.1's `Governance`
    section is built from it, `3.6.1←{all}` depends on it, and S3-P1 registered
    `governance.review_trigger.v1` for the rules it compiles to. The phase table
    simply skips it between S3-P5's 3.4.x and S3-P9's 3.5.3. It is built here
    because S3-P6 is the phase whose exit criterion is a full-DAG run, and a
    rulebook missing a third of its governance section is not one.
    """

    spec = NodeSpec(
        id="3.5.2",
        name="legal_review_triggers",
        stage="3.5",
        run_stage=RunStage.GUIDELINE,
        depends_on=("3.2.2", "3.3.1"),
        task_class=TaskClass.CLASSIFY,
        input_model=BaseModel,
        output_model=LegalReviewTriggersOutput,
        connectors=(),
        optional_inputs=("compliance_guardrails",),
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        """Nothing new. Both inputs are node outputs already in the context."""
        return []

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        claims = (ctx.outputs.get("3.2.2") or {}).get("claims") or []
        applicable = (ctx.outputs.get("3.3.1") or {}).get("applicable") or []

        if not claims and not applicable:
            return LegalReviewTriggersOutput(
                triggers=[],
                always_review=[],
                reason=(
                    "No substantiated claims and no applicable policy area, so there is "
                    "nothing that needs a second read before it ships."
                ),
            )

        draft = await ctx.complete(
            ReviewTriggersDraft,
            system=prompts.system_prompt(
                "You decide which wording in an advertising account must be read by a "
                "human before it runs. You route to a role, never to a person. You "
                "propose a trigger only where there is a claim or a policy area behind "
                "it, and you never invent a risk the inputs do not show."
            ),
            user=prompts.compose(
                prompts.project_block(ctx.project),
                prompts.computed_block(
                    "claims and their risk tiers",
                    [
                        {
                            "claim_text": row.get("claim_text"),
                            "claim_type": row.get("claim_type"),
                            "risk_tier": row.get("risk_tier"),
                            "status": row.get("status"),
                        }
                        for row in claims[:200]
                    ],
                ),
                prompts.computed_block(
                    "Google policy areas that apply to this account",
                    [
                        {
                            "area": row.get("area"),
                            "obligations": row.get("obligations"),
                            "markets": row.get("markets"),
                        }
                        for row in applicable[:100]
                    ],
                ),
                prompts.computed_block("the roles you may route to", sorted(REVIEWER_ROLES)),
            ),
        )

        triggers = [item for item in draft.triggers if self._keep(item, ctx)]
        return LegalReviewTriggersOutput(
            triggers=triggers,
            always_review=[line.strip() for line in draft.always_review if line.strip()],
            reason="" if triggers else "The model proposed no trigger this input supports.",
        )

    def _keep(self, trigger: ReviewTriggerDraft, ctx: RunContext) -> bool:
        """Drop a trigger that names a person or cannot be evaluated.

        Dropped rather than raised. One malformed trigger among thirty is a
        model slip, and failing the node would throw away the other twenty-nine
        along with the hour of run that produced them — where a missing trigger
        costs a second read nobody was going to get anyway. A trigger that
        reached `guardrails/` uncompilable would be worse: it reads as enforced
        and matches nothing.
        """
        if trigger.reviewer_role not in REVIEWER_ROLES:
            # The failure this catches is a UUID, which is an identity. Law 23
            # keeps identity routing in `SignOffMatrix` and nowhere else.
            log.warning(
                "3.5.2.trigger_dropped",
                why="reviewer_role is not a workspace role",
                reviewer_role=trigger.reviewer_role[:64],
            )
            return False
        if trigger.pattern_kind == "term":
            try:
                re.compile(trigger.pattern)
            except re.error as exc:
                log.warning("3.5.2.trigger_dropped", why=f"pattern does not compile: {exc}")
                return False
        return True


#: Module-level instance. The registry discovers instances, not classes.
legal_review_triggers = LegalReviewTriggersNode()
