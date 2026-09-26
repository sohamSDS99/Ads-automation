"""4.6.3 🔒 `legal_exception_clearance` — H3, the named legal owner only (Stage 04 PRD §11, §8.6).

`not_required` when 4.6.2 raised nothing, or every exception it raised has
since been withdrawn. Otherwise the run parks on H3 — Stage 03's PERSON-TASK
primitive (`human_task_key="H3"`, opened by the executor from this output) —
routed to the pinned `signoff_matrix.legal_owner` and to nobody else: there is
no role fallback and no administrator override (Law 23, §5.3).

The task carries the set it decides and that set's `register_hash`; the legal
owner answers it through `POST /creative-runs/{id}/exceptions/clear`, never
through the generic task submit (§16 rule 4). That route closes this node
`succeeded` with the decision and resumes the run, so a resumed executor never
asks H3 twice. No model call: whether H3 is needed was decided by 4.6.2
reading the pin, and a model must not re-decide it.
"""

from __future__ import annotations

import sqlalchemy as sa
from pydantic import BaseModel

from agent.creative import clearance
from agent.db.models import CreativeException, CreativeExceptionStatus, Evidence, RunStage
from agent.llm.router import TaskClass
from agent.nodes.base import NodeSpec, RunContext
from agent.schemas.creative_input import CreativeInput
from agent.schemas.creative_qa import EditorialLint, LegalExceptionClearance

NODE_ID = "4.6.3"
LINT_NODE = "4.6.2"


class LegalExceptionClearanceNode:
    spec = NodeSpec(
        id=NODE_ID,
        name="legal_exception_clearance",
        stage="4.6",
        run_stage=RunStage.CREATIVE,
        depends_on=(LINT_NODE,),
        task_class=TaskClass.CLASSIFY,
        input_model=CreativeInput,
        output_model=LegalExceptionClearance,
        human_task_key="H3",
        human_task_conditional=True,
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        return []

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        creative = ctx.require_creative()
        lint = EditorialLint.model_validate(ctx.output_of(LINT_NODE))
        wanted = [item.exception_id for item in lint.exceptions]
        found = {
            row.id: row
            for row in (
                await ctx.db.execute(
                    sa.select(CreativeException)
                    .where(
                        CreativeException.creative_run_id == ctx.run.id,
                        CreativeException.id.in_(wanted),
                        CreativeException.status == CreativeExceptionStatus.OPEN,
                    )
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        }
        rows = [found[key] for key in wanted if key in found]
        if not rows:
            return LegalExceptionClearance(
                status="not_required",
                why=(
                    "4.6.2 found nothing the pinned ruleset cannot license."
                    if not wanted
                    else "Every exception 4.6.2 raised was withdrawn before H3 opened."
                ),
            )
        count = len(rows)
        noun = "exception" if count == 1 else "exceptions"
        return LegalExceptionClearance(
            status="required",
            assignee_id=creative.input.signoff_matrix.legal_owner_id,
            title=f"Clear {count} legal {noun} before launch",
            instructions=(
                f"Ruleset {lint.ruleset_version} cannot license {count} {noun} in this "
                "creative run: new claims, disclaimers or image rights. Clear or reject "
                "each one. A cleared claim is signed into the claims register and mints a "
                "new ruleset version; a rejected one drops its assets for their fallbacks."
            ),
            exception_ids=[row.id for row in rows],
            set_hash=clearance.register_hash([clearance.view(row) for row in rows]),
            why=f"{count} {noun} the pinned ruleset cannot license.",
        )

    def task_required(self, ctx: RunContext, output: BaseModel) -> bool:
        """H3 opens only when there is something to clear (§11 4.6.3)."""
        return isinstance(output, LegalExceptionClearance) and output.status == "required"


LEGAL_EXCEPTION_CLEARANCE = LegalExceptionClearanceNode()
