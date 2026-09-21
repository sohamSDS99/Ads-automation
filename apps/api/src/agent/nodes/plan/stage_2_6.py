"""Stage 2.6 — the plan (Stage 02 PRD §11, §12).

Two nodes and one loop between them, and it is the same shape as Stage 1.6
because it is the same problem one stage later.

`plan_synthesis` (2.6.1) turns seventeen node outputs into the one
`CampaignPlan` everything downstream reads. Most of that is assembly and it
lives in `planning/plan_synthesis.py`; what happens *here* is the model call
that writes the prose no projection can produce — the executive summary, and
the assumptions and risks as cited `Claim`s — and the write of the
`campaign_plan` row the Plan Viewer, the exports and the freeze all read.

`plan_critique` (2.6.2) reads the finished plan back with a **different model
family** (`TaskClass.CRITIQUE` routes cross-vendor for exactly this reason)
and combines two things that are not the same kind of check:

* **§11's ten assertions, computed.** `planning/critique.py` decides all ten
  in Python. §11 says the checklist is "fixed and asserted in tests, not left
  to the model's discretion", and an allocation that misses its envelope by
  0.8% is a fact, not an opinion.
* **What only a reader can see.** A rationale that argues for a scenario other
  than the one chosen, a hypothesis that contradicts a target, prose that
  oversells a degraded forecast. The model is asked for those and nothing else.

One blocking issue buys exactly one re-synthesis. **The re-synthesis runs here,
inside 2.6.2**, for the reason `stage_1_6` gives at length: the executor is a
wavefront over a DAG with no facility for a node to send another node round
again, and building one for a single documented case would be a large change
to the most load-bearing file in the repo. The cost of the second call lands
on 2.6.2's `NodeRun`, which is the only visible difference.

**2.6.2 is what decides `plan_status`.** 2.6.1 cannot: it runs before the
critique, so `ready_to_freeze` is not a state it is ever entitled to write.
The rule is `planning.plan_synthesis.status_for` and it is four lines of
Python rather than a judgement — which matters, because it is the gate
between a draft and something a person can seal.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

import sqlalchemy as sa
import structlog
from pydantic import BaseModel, Field

from agent.db.models import (
    Approval,
    ApprovalStatus,
    CampaignPlanStatus,
    Evidence,
    PlanCalc,
    ResearchAcceptance,
    RunStage,
    User,
)
from agent.db.models import (
    CampaignPlan as CampaignPlanRow,
)
from agent.db.repos import CampaignPlanRepo
from agent.export.contract import Claim, Confidence
from agent.export.plan_contract import CampaignPlan, GateDecision, PlanSource, PlanStatus
from agent.export.plan_markdown import render_plan_markdown
from agent.llm.router import TaskClass
from agent.nodes import prompts
from agent.nodes.base import NodeSpec, RunContext
from agent.orchestrator.registry import get_registry
from agent.planning import critique as checks
from agent.planning.plan_synthesis import CalcIndex, CalcRef, PlanFacts, assemble, status_for

log = structlog.get_logger(__name__)

#: Every node the plan is built from. §11: `2.6.1←{all}`. Written out rather
#: than derived, because `depends_on` is read at import time and the registry
#: is what imports this module. `test_plan_nodes_2_6.py` asserts it equals
#: every non-report plan node the registry knows, so a node added later fails
#: the suite instead of quietly never reaching the plan.
ALL_PLAN_NODES: tuple[str, ...] = (
    "2.1.1",
    "2.1.2",
    "2.1.3",
    "2.1.4",
    "2.2.1",
    "2.2.2",
    "2.2.3",
    "2.2.4",
    "2.2.5",
    "2.3.1",
    "2.3.2",
    "2.3.3",
    "2.4.1",
    "2.4.2",
    "2.4.3",
    "2.5.1",
    "2.5.2",
    "2.5.3",
)

#: The four gates §11 declares, and the node that owns each.
GATE_NODES: dict[str, str] = {"G1": "2.1.3", "G2": "2.1.4", "G3": "2.2.4", "G4": "2.3.1"}

#: Evidence rows loaded for the plan to cite from. The union of what seventeen
#: nodes cited is normally a few hundred; the cap guards a runaway node, and a
#: plan that hits it loses citations rather than the whole run.
MAX_EVIDENCE = 4_000

#: How many evidence rows the model is offered to cite assumptions and risks
#: from. Bounds one prompt, not the plan.
MAX_CITABLE = 50

#: One evidence row's payload, as shown to the model when it picks citations.
CITATION_CHARS = 220

#: How much of the rendered plan the critic reads. A 40-campaign plan's
#: markdown runs past any context worth paying for, and the ten assertions
#: have already read the payload in full — this is for the prose.
CRITIQUE_CHARS = 60_000


class NoCalculations(RuntimeError):
    """The plan run produced no `PlanCalc` row, so nothing in it is traceable."""


def _warnings(issues: list[checks.Issue]) -> list[checks.Issue]:
    """The warning-severity findings. A list comprehension, not a count — the
    isolation guard reads `sum(...)` over a field as arithmetic, and it is
    right not to try to tell a tally from a total."""
    return [issue for issue in issues if issue.severity == "warning"]


# ---------------------------------------------------------------------------
# 2.6.1 — plan_synthesis
# ---------------------------------------------------------------------------


class WrittenClaim(BaseModel):
    """A claim the model wants to make, before its citations are checked."""

    statement: str
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "medium"


class PlanNarrative(BaseModel):
    """The only part of the plan a model writes."""

    executive_summary: str = Field(
        default="",
        description=(
            "At most 250 words. What we are going to buy, what it should return, what has "
            "to be true for that, and what is still open."
        ),
    )
    assumptions: list[WrittenClaim] = Field(default_factory=list)
    risks: list[WrittenClaim] = Field(default_factory=list)


class PlanSynthesisOutput(BaseModel):
    """2.6.1's node output — a receipt, not the plan.

    The plan itself carries the whole account structure and lives in its own
    table; copying it into `node_run.output` as well would double the largest
    write in the run and make the run console unreadable. What stays here is
    what the console shows and what 2.6.2 needs.
    """

    plan_id: str
    schema_version: str
    plan_status: PlanStatus
    executive_summary: str
    campaigns: int = 0
    ad_groups: int = 0
    keywords: int = 0
    experiments: int = 0
    gates_approved: int = 0
    open_dependencies: int = 0
    blocking_dependencies: int = 0
    numbers: int = 0
    dropped_claims: int = 0
    #: Rows and figures that did not fit the contract and were left out rather
    #: than taking the run down. Non-zero means §12 and a node output have
    #: drifted apart and want reconciling.
    dropped_rows: list[str] = Field(default_factory=list)
    markdown_chars: int = 0
    cost_usd: float = 0.0
    constants_version: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    #: Every `PlanCalc` row the finished plan's figures resolve to. Not a
    #: calculation 2.6.1 made — it makes none — but the set it asserts every
    #: number in the plan belongs to, which the executor then checks against
    #: the `derived` rows this node gathered. §9.1's citation rule holds for
    #: the plan exactly as it does for the nodes that built it.
    calc_evidence_ids: list[uuid.UUID] = Field(min_length=1)


class PlanSynthesisNode:
    """2.6.1 — the handoff artifact to Stage 03."""

    spec = NodeSpec(
        id="2.6.1",
        name="plan_synthesis",
        stage="2.6",
        run_stage=RunStage.PLAN,
        depends_on=ALL_PLAN_NODES,
        task_class=TaskClass.SYNTHESIZE,
        input_model=BaseModel,
        output_model=PlanSynthesisOutput,
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        """Exactly the evidence the upstream nodes cited.

        The plan is a fold of their findings, so its citations are a subset of
        theirs by construction. Loading them here is what lets the executor's
        provenance check mean something for the plan as well.
        """
        rows = await _cited_evidence(ctx)
        ctx.scratch[self.spec.id] = rows
        return rows

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        plan, markdown, extra = await synthesise(ctx, ev)
        stored = await _store(ctx, plan, markdown)
        counts = plan.account_structure.counts()
        await ctx.progress(
            f"{plan.plan_status} — {counts['campaigns']} campaigns, "
            f"{counts['keywords']} keywords, {len(plan.numbers())} traceable figures"
        )
        return PlanSynthesisOutput(
            plan_id=str(stored.id),
            schema_version=plan.schema_version,
            plan_status=plan.plan_status,
            executive_summary=plan.executive_summary,
            campaigns=counts["campaigns"],
            ad_groups=counts["ad_groups"],
            keywords=counts["keywords"],
            experiments=len(plan.experiment_backlog),
            gates_approved=len([row for row in plan.decisions if row.is_approved]),
            open_dependencies=len(plan.open_dependencies),
            blocking_dependencies=len(plan.blocking_dependencies),
            numbers=len(plan.numbers()),
            dropped_claims=extra["dropped_claims"],
            dropped_rows=extra["dropped_rows"],
            markdown_chars=len(markdown),
            cost_usd=plan.cost_usd,
            constants_version=plan.constants_version,
            evidence_ids=[str(value) for value in plan.evidence_ids()],
            calc_evidence_ids=plan.calc_evidence_ids(),
        )


async def synthesise(
    ctx: RunContext,
    evidence: list[Evidence],
    *,
    critique: list[dict[str, Any]] | None = None,
    plan_status: PlanStatus | None = None,
) -> tuple[CampaignPlan, str, dict[str, Any]]:
    """Build one plan. Shared by 2.6.1 and by 2.6.2's single re-run."""
    plan_context = ctx.require_plan()
    outputs = dict(ctx.outputs)
    dropped_rows: list[str] = []
    known = {row.id: _digest(row) for row in evidence}

    decisions = await _gate_decisions(ctx)
    acceptance_id = await _acceptance_id(ctx)
    calcs = await _calc_index(ctx)
    if not len(calcs):
        # `calc_evidence_ids` has `min_length=1`, so this would otherwise
        # surface as a validation error on the last node of a forty-minute
        # run. A plan run with no `PlanCalc` row means every upstream node
        # skipped its arithmetic, and saying that beats a schema message.
        raise NoCalculations(
            "this plan run wrote no PlanCalc row, so no figure in the plan resolves to a "
            "calculation and §12 invariant 1 cannot hold. Check whether the 2.1 and 2.2 "
            "branches ran at all — `GET /plans/{plan_run_id}/calcs` is the fastest look."
        )
    facts = PlanFacts(
        project_id=ctx.project.id,
        plan_run_id=ctx.run.id,
        source=_source(ctx, acceptance_id),
        calcs=calcs,
        decisions=decisions,
        constants_version=plan_context.constants.version,
        cost_usd=float(ctx.ledger.spent_usd),
        generated_at=datetime.now(UTC),
    )

    narrative = await ctx.complete(
        PlanNarrative,
        system=prompts.system_prompt(
            "You are writing the executive layer of a paid-search campaign plan for the "
            "people who have just agreed to fund it. Every number, target, budget and "
            "campaign in it is already decided and computed — your job is to say what the "
            "plan commits to and what it depends on, not to re-decide any of it. Write "
            "plainly, in full sentences, with no marketing register and no hedging."
        ),
        user=prompts.compose(
            prompts.project_block(ctx.project),
            prompts.computed_block(
                "what was approved (final — write prose consistent with it)",
                _decision_block(decisions),
            ),
            prompts.computed_block("the money (2.2.4)", _money_block(outputs)),
            prompts.computed_block(
                "what each campaign is aiming at (2.1.3)", _targets_block(outputs)
            ),
            prompts.computed_block("what we can afford to pay (2.1.2)", _economics_block(outputs)),
            prompts.computed_block("the channel slate agreed at G4 (2.3.1)", _slate_block(outputs)),
            prompts.computed_block(
                "the account we will build (2.4.2, 2.4.3)", _structure_block(outputs)
            ),
            prompts.computed_block(
                "how we will measure it (2.5.1, 2.5.2)", _measurement_block(outputs)
            ),
            prompts.computed_block("what we will test first (2.5.3)", _backlog_block(outputs)),
            prompts.computed_block(
                "what is still unresolved",
                {
                    "launch_blockers": [
                        claim.statement for claim in plan_context.input.launch_blockers
                    ],
                    "degraded_sources": list(plan_context.input.degraded_sources),
                },
            ),
            prompts.computed_block(
                "evidence you may cite (use these ids verbatim)", _citable(known)
            ),
            _critique_block(critique),
            "TASK\n"
            "  1. An executive summary of at most 250 words: what this plan buys, what it "
            "should return, and the first thing that has to happen. It must agree with the "
            "figures above and must not restate them all.\n"
            "  2. The assumptions this plan rests on — the things that, if untrue, make the "
            "forecast wrong. Each with at least one `evidence_ids` value copied from the "
            "list above.\n"
            "  3. The risks: what could go wrong once the money is spent, cited the same "
            "way, ordered by what costs the most.",
        ),
    )

    dropped, assumptions = _claims(narrative.assumptions, known)
    dropped_risks, risks = _claims(narrative.risks, known)

    plan = assemble(
        facts=facts,
        outputs=outputs,
        summary=_summary(narrative.executive_summary, decisions, outputs),
        assumptions=assumptions,
        risks=risks,
        launch_blockers=plan_context.input.launch_blockers,
        plan_status=plan_status or status_for(decisions, blocking_issues=0),
        critique_issues=critique or (),
        drop=dropped_rows.append,
    )
    return (
        plan,
        render_plan_markdown(plan, project_name=ctx.project.name),
        {
            "dropped_claims": dropped + dropped_risks,
            "dropped_rows": sorted(set(dropped_rows))[:20],
        },
    )


# ---------------------------------------------------------------------------
# 2.6.2 — plan_critique
# ---------------------------------------------------------------------------


class ReadingIssue(BaseModel):
    """What the critic model is asked for: judgement, never arithmetic."""

    severity: Literal["blocking", "warning", "note"] = "note"
    section: str = Field(default="", description="Which part of the plan is wrong.")
    finding: str
    fix: str = Field(default="", description="What would make it right.")


class PlanReading(BaseModel):
    """2.6.2's model output."""

    issues: list[ReadingIssue] = Field(default_factory=list)
    summary_consistent: bool = Field(
        default=True,
        description="Does the executive summary agree with the approved figures?",
    )
    contradictions: list[str] = Field(default_factory=list)


class PlanCritiqueOutput(BaseModel):
    """2.6.2's node output — and the thing that decides whether a plan may freeze."""

    plan_id: str
    plan_status: PlanStatus
    issues: list[dict[str, Any]] = Field(default_factory=list)
    blocking: int = 0
    warnings: int = 0
    #: Which of §11's ten failed, by name. Stable where the finding text is not.
    failed_checks: list[str] = Field(default_factory=list)
    assertions_run: int = 0
    summary_consistent: bool = True
    resynthesised: bool = False
    resynthesis_resolved: int = 0
    resynthesis_remaining: int = 0
    gates_approved: int = 0
    evidence_ids: list[str] = Field(default_factory=list)
    calc_evidence_ids: list[uuid.UUID] = Field(min_length=1)


class PlanCritiqueNode:
    """2.6.2 — ten computed assertions, one reader from another model family."""

    spec = NodeSpec(
        id="2.6.2",
        name="plan_critique",
        stage="2.6",
        run_stage=RunStage.PLAN,
        depends_on=("2.6.1",),
        task_class=TaskClass.CRITIQUE,
        input_model=PlanSynthesisOutput,
        output_model=PlanCritiqueOutput,
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        rows = await _cited_evidence(ctx)
        ctx.scratch[self.spec.id] = rows
        return rows

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        repo = CampaignPlanRepo(ctx.db, ctx.run.workspace_id)
        stored = await repo.for_run(ctx.run.id)
        if stored is None:  # pragma: no cover — 2.6.1 is a hard dependency
            raise RuntimeError("2.6.2 ran with no plan to critique")
        plan = CampaignPlan.model_validate(stored.payload)
        blockers = ctx.require_plan().input.launch_blockers

        asserted = checks.run_checks(plan, launch_blockers=blockers)
        reading = await self._read(ctx, plan, stored.markdown, asserted)
        issues = [*asserted, *_as_issues(reading)]
        blocking = checks.blocking(issues)

        resynthesised = False
        remaining: list[checks.Issue] = []
        if blocking:
            # §11: one re-run, with the critique appended. Not a loop.
            await ctx.progress(f"{len(blocking)} blocking issue(s) — re-synthesising the plan once")
            revised, markdown, _ = await synthesise(
                ctx, ev, critique=[issue.as_dict() for issue in issues], plan_status="draft"
            )
            remaining = checks.blocking(checks.run_checks(revised, launch_blockers=blockers))
            revised.plan_status = status_for(revised.decisions, blocking_issues=len(remaining))
            revised.critique_issues = [issue.as_dict() for issue in remaining]
            await _store(ctx, revised, markdown)
            plan = revised
            issues = [*remaining, *_as_issues(reading)]
            resynthesised = True

        status = status_for(plan.decisions, blocking_issues=len(checks.blocking(issues)))
        await _finalise(ctx, plan, status, issues)
        await ctx.progress(
            f"{status} — {len(checks.blocking(issues))} blocking, {len(_warnings(issues))} warnings"
        )
        return PlanCritiqueOutput(
            plan_id=str(stored.id),
            plan_status=status,
            issues=[issue.as_dict() for issue in issues],
            blocking=len(checks.blocking(issues)),
            warnings=len(_warnings(issues)),
            failed_checks=sorted({issue.check for issue in issues if issue.check}),
            assertions_run=len(checks.CHECK_NAMES),
            summary_consistent=reading.summary_consistent,
            resynthesised=resynthesised,
            resynthesis_resolved=len(blocking) - len(remaining) if resynthesised else 0,
            resynthesis_remaining=len(remaining),
            gates_approved=len([row for row in plan.decisions if row.is_approved]),
            evidence_ids=[str(value) for value in plan.evidence_ids()],
            calc_evidence_ids=plan.calc_evidence_ids(),
        )

    async def _read(
        self,
        ctx: RunContext,
        plan: CampaignPlan,
        markdown: str,
        asserted: list[checks.Issue],
    ) -> PlanReading:
        return await ctx.complete(
            PlanReading,
            system=prompts.system_prompt(
                "You are reviewing a campaign plan another model assembled, for a reader "
                "who is about to spend the budget in it. Ten mechanical checks have already "
                "run and their results are shown to you — do not repeat them and do not "
                "check any arithmetic yourself. You are looking for what only a reader can "
                "see: a rationale that argues for a different choice than the one made, two "
                "sections that contradict each other, a summary that oversells a forecast "
                "the plan itself calls degraded, a hypothesis that contradicts a target. "
                "Say 'blocking' only when a reader acting on this plan would spend money "
                "badly."
            ),
            user=prompts.compose(
                prompts.computed_block(
                    "what the mechanical checks already found (do not restate these)",
                    [issue.as_dict() for issue in asserted] or "nothing — all ten passed",
                ),
                prompts.computed_block(
                    "the approved decisions and the state of the plan",
                    {
                        "plan_status": plan.plan_status,
                        "gates": [
                            {"gate": row.gate_key, "status": row.status, "note": row.note}
                            for row in plan.decisions
                        ],
                        "degraded_sources": plan.source.degraded_sources,
                        "open_dependencies": [item.task for item in plan.blocking_dependencies],
                    },
                ),
                f"THE PLAN\n{markdown[:CRITIQUE_CHARS]}",
                "TASK\n"
                "  List the issues you actually find. An empty list is the correct answer "
                "for a sound plan, and inventing one to look thorough costs a re-run of the "
                "whole synthesis. For each: severity, the section, what is wrong, and the "
                "fix. Then say whether the executive summary agrees with the approved "
                "figures.",
            ),
            task_class=TaskClass.CRITIQUE,
        )


# ---------------------------------------------------------------------------
# shared
# ---------------------------------------------------------------------------


async def _cited_evidence(ctx: RunContext) -> list[Evidence]:
    """Everything the plan may cite: what the nodes cited, and what they computed.

    Two sets, loaded together because the executor checks them separately.
    `evidence_ids` on the plan must be a subset of what this node gathered;
    `calc_evidence_ids` must be a subset of the **`derived`** rows among them.
    2.6.1 runs no formula of its own, so every `derived` row it cites was
    written by an upstream node and read back here — which the executor
    explicitly allows (`gather.collect` has always been able to read stored
    `derived` rows), and which is the only way the plan's claim that every
    figure in it resolves to a recorded calculation can be machine-checked.
    """
    wanted: dict[uuid.UUID, None] = {}
    for output in ctx.outputs.values():
        for value in _walk(output, "evidence_ids"):
            wanted.setdefault(value, None)
        for value in _walk(output, "calc_evidence_ids"):
            wanted.setdefault(value, None)
        if len(wanted) >= MAX_EVIDENCE:
            break
    for value in await _plan_calc_evidence_ids(ctx):
        wanted.setdefault(value, None)
    if not wanted:
        return []
    rows = await ctx.db.execute(
        sa.select(Evidence).where(
            Evidence.project_id == ctx.project.id,
            Evidence.id.in_(list(wanted)[:MAX_EVIDENCE]),
        )
    )
    return list(rows.scalars().all())


async def _plan_calc_evidence_ids(ctx: RunContext) -> list[uuid.UUID]:
    """The `derived` row behind every `PlanCalc` of this run."""
    rows = await ctx.db.execute(
        sa.select(PlanCalc.evidence_id).where(PlanCalc.plan_run_id == ctx.run.id)
    )
    return [value for value in rows.scalars().all() if value is not None]


async def _calc_index(ctx: RunContext) -> CalcIndex:
    """The run's `PlanCalc` rows, for resolving a figure to its calculation."""
    rows = await ctx.db.execute(
        sa.select(PlanCalc.node_id, PlanCalc.formula_id, PlanCalc.evidence_id).where(
            PlanCalc.plan_run_id == ctx.run.id
        )
    )
    return CalcIndex(
        CalcRef(node_id=node_id, formula_id=formula_id, evidence_id=evidence_id)
        for node_id, formula_id, evidence_id in rows.all()
    )


async def _gate_decisions(ctx: RunContext) -> list[GateDecision]:
    """The four gates, as the plan records them (§12 invariant 3).

    Read from `Approval` rather than from the node outputs, because the output
    is the *proposal* and the approval is the decision — including the edits an
    approver made to it and the note they left, which is the whole point of
    auditing a gate.
    """
    rows = (
        await ctx.db.execute(
            sa.select(Approval, User.name)
            .join(User, User.id == Approval.decided_by, isouter=True)
            .where(Approval.run_id == ctx.run.id)
            .order_by(Approval.created_at.asc())
        )
    ).all()
    registry = get_registry()
    by_gate: dict[str, GateDecision] = {}
    for approval, decider_name in rows:
        key = (approval.gate_key or "").strip().upper()
        if key not in GATE_NODES:
            continue
        name = ""
        if approval.node_id in registry:
            name = registry.spec(approval.node_id).name
        by_gate[key] = GateDecision(
            gate_key=key,
            node_id=approval.node_id,
            name=name,
            status=_approval_status(approval.status),
            decided_by=approval.decided_by,
            decided_by_name=decider_name or "",
            decided_at=approval.decided_at,
            note=approval.decision_note or "",
            edits_applied=_edits(approval),
        )
    # A gate that never opened is carried as `pending` rather than omitted:
    # invariant 3 counts four entries, and a missing one is a plan that looks
    # like it needed three approvals.
    for key, node_id in GATE_NODES.items():
        by_gate.setdefault(
            key,
            GateDecision(
                gate_key=key,
                node_id=node_id,
                name=registry.spec(node_id).name if node_id in registry else "",
                status="pending",
            ),
        )
    return [by_gate[key] for key in ("G1", "G2", "G3", "G4")]


def _approval_status(
    status: ApprovalStatus,
) -> Literal["approved", "rejected", "pending", "expired"]:
    mapping: dict[ApprovalStatus, Literal["approved", "rejected", "pending", "expired"]] = {
        ApprovalStatus.APPROVED: "approved",
        ApprovalStatus.REJECTED: "rejected",
        ApprovalStatus.PENDING: "pending",
        ApprovalStatus.EXPIRED: "expired",
    }
    return mapping.get(status, "pending")


def _edits(approval: Approval) -> list[dict[str, Any]]:
    """What the approver changed. Empty on a clean approval."""
    edited = approval.edited_proposal or {}
    rows = edited.get("edits_applied") if isinstance(edited, dict) else None
    return [dict(row) for row in rows or [] if isinstance(row, dict)]


async def _acceptance_id(ctx: RunContext) -> uuid.UUID:
    """The acceptance this plan was planned from (§12's `source.acceptance_id`).

    Looked up rather than carried on `PlanInput`, and the reason is `Run.
    input_hash`. `PlanInput.content_hash()` is what makes PT3's determinism
    and the cache-reuse check work; adding an id that changes every time the
    same research is re-accepted would break cache reuse for a field that is
    provenance rather than input.

    The lookup is exact, not a guess: §7.2 puts a unique constraint on
    `research_acceptance.run_id`, so one research run has at most one
    acceptance row and there is nothing to disambiguate.
    """
    found = (
        await ctx.db.execute(
            sa.select(ResearchAcceptance.id).where(
                ResearchAcceptance.run_id == ctx.require_plan().input.research_run_id
            )
        )
    ).scalar_one_or_none()
    if found is None:  # pragma: no cover — the run cannot start without one (§4, E1)
        raise RuntimeError(
            "this plan run has no research acceptance, which `POST /projects/{id}/plan/runs` "
            "refuses to start without. The row was deleted mid-run."
        )
    return found


def _source(ctx: RunContext, acceptance_id: uuid.UUID) -> PlanSource:
    plan_input = ctx.require_plan().input
    return PlanSource(
        research_run_id=plan_input.research_run_id,
        report_id=plan_input.research_report_id,
        acceptance_id=acceptance_id,
        accepted_by=plan_input.accepted_by,
        accepted_at=plan_input.accepted_at,
        research_schema_version=plan_input.research_schema_version,
        launch_readiness=plan_input.launch_readiness,
        override_reason=plan_input.override_reason,
        degraded_sources=list(plan_input.degraded_sources),
    )


async def _store(ctx: RunContext, plan: CampaignPlan, markdown: str) -> CampaignPlanRow:
    """Persist the plan and commit. One row per run; the second write updates."""
    repo = CampaignPlanRepo(ctx.db, ctx.run.workspace_id)
    stored = await repo.upsert(
        plan_run_id=ctx.run.id,
        project_id=ctx.project.id,
        acceptance_id=plan.source.acceptance_id,
        schema_version=plan.schema_version,
        payload=plan.model_dump(mode="json"),
        markdown=markdown,
        status=_row_status(plan.plan_status),
    )
    await ctx.db.commit()
    return stored


async def _finalise(
    ctx: RunContext, plan: CampaignPlan, status: PlanStatus, issues: list[checks.Issue]
) -> None:
    """Write the critique's verdict onto the stored plan.

    The payload carries the issues so an exported PDF shows its own review — a
    document that says "ready to freeze" while the critique that said otherwise
    lives somewhere else is how a blocked plan gets circulated as an approved
    one.
    """
    plan.plan_status = status
    plan.critique_issues = [issue.as_dict() for issue in issues]
    await _store(ctx, plan, render_plan_markdown(plan, project_name=ctx.project.name))


def _row_status(status: PlanStatus) -> CampaignPlanStatus:
    return {
        "draft": CampaignPlanStatus.DRAFT,
        "blocked": CampaignPlanStatus.BLOCKED,
        "ready_to_freeze": CampaignPlanStatus.READY_TO_FREEZE,
        "frozen": CampaignPlanStatus.FROZEN,
    }[status]


def _as_issues(reading: PlanReading) -> list[checks.Issue]:
    """The model's findings, in the same shape as the computed ones."""
    return [
        checks.Issue(
            severity=row.severity,
            section=row.section or "plan",
            finding=row.finding,
            fix=row.fix,
            check="reader",
        )
        for row in reading.issues
        if row.finding.strip()
    ]


def _claims(
    written: list[WrittenClaim],
    known: dict[uuid.UUID, str],
    *,
    confidence: Confidence | None = None,
) -> tuple[int, list[Claim]]:
    """Validate the model's citations. Returns (dropped, claims).

    A claim citing an id that does not exist is not repaired into one citing a
    different id — that would attach real evidence to an assertion it never
    supported. The bad ids are removed and a claim left with none is dropped.
    """
    kept: list[Claim] = []
    dropped = 0
    for item in written:
        valid: list[uuid.UUID] = []
        for value in item.evidence_ids:
            try:
                parsed = uuid.UUID(str(value))
            except (ValueError, AttributeError, TypeError):
                continue
            if parsed in known and parsed not in valid:
                valid.append(parsed)
        if not valid or not item.statement.strip():
            dropped += 1
            continue
        kept.append(
            Claim(
                statement=item.statement.strip(),
                evidence_ids=valid,
                confidence=confidence or item.confidence,
            )
        )
    return dropped, kept


def _summary(written: str, decisions: list[GateDecision], outputs: dict[str, Any]) -> str:
    """The summary, guaranteed non-empty and inside §12's word budget.

    An empty summary is replaced by the decision stated plainly, and a long one
    is cut at the budget — a model that writes 400 words must not cost the run
    its last node.
    """
    text = " ".join(written.split())
    if not text:
        envelope = ((outputs.get("2.2.4") or {}).get("envelope") or {}).get("monthly_cap_usd")
        approved = len([row for row in decisions if row.is_approved])
        text = f"A campaign plan with {approved} of four gates approved" + (
            f", committing ${envelope:,.0f} a month." if envelope else "."
        )
    words = text.split()
    if len(words) > 250:
        text = " ".join(words[:249]) + "…"
    return text


def _digest(row: Evidence) -> str:
    """One evidence row, short enough that fifty of them fit in a prompt."""
    import json

    body = json.dumps(row.payload, default=str, separators=(", ", ": "), sort_keys=True)
    return f"{row.kind}: {body[:CITATION_CHARS]}"


def _citable(known: dict[uuid.UUID, str]) -> list[dict[str, str]]:
    return [
        {"evidence_id": str(key), "content": value}
        for key, value in list(known.items())[:MAX_CITABLE]
    ]


def _walk(node: Any, field_name: str) -> list[uuid.UUID]:
    found: list[uuid.UUID] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == field_name and isinstance(value, list):
                for item in value:
                    try:
                        found.append(uuid.UUID(str(item)))
                    except (ValueError, AttributeError, TypeError):
                        continue
            else:
                found.extend(_walk(value, field_name))
    elif isinstance(node, list | tuple):
        for item in node:
            found.extend(_walk(item, field_name))
    return found


def _critique_block(critique: list[dict[str, Any]] | None) -> str:
    """The second pass's extra instruction. Empty on the first pass."""
    if not critique:
        return ""
    return prompts.compose(
        prompts.computed_block("a reviewer's issues with your previous draft", critique),
        "REVISION\n"
        "  This is your one revision. The issues above are mostly about figures and "
        "structure, which are not yours to change — what you can fix is prose that "
        "misstates them. Rewrite the summary, the assumptions and the risks so they "
        "describe the plan as it actually is, including what the reviewer found wrong "
        "with it.",
    )


# ---------------------------------------------------------------------------
# prompt blocks — projections, never arithmetic
# ---------------------------------------------------------------------------


def _decision_block(decisions: list[GateDecision]) -> list[dict[str, Any]]:
    return [
        {
            "gate": row.gate_key,
            "what_was_decided": row.name,
            "status": row.status,
            "by": row.decided_by_name or "(unassigned)",
            "note": row.note,
            "edits": len(row.edits_applied),
        }
        for row in decisions
    ]


def _money_block(outputs: dict[str, Any]) -> dict[str, Any]:
    approved = outputs.get("2.2.4") or {}
    return {
        "chosen_scenario": approved.get("chosen_scenario"),
        "envelope": approved.get("envelope"),
        "experiment_reserve_usd": approved.get("experiment_reserve_usd"),
        "allocation": (approved.get("allocation") or [])[:12],
        "degraded_sources": approved.get("degraded_sources"),
    }


def _targets_block(outputs: dict[str, Any]) -> dict[str, Any]:
    targets = outputs.get("2.1.3") or {}
    return {
        "north_star": targets.get("north_star"),
        "objectives": (targets.get("objectives") or [])[:12],
    }


def _economics_block(outputs: dict[str, Any]) -> dict[str, Any]:
    economics = outputs.get("2.1.2") or {}
    return {
        "blended": economics.get("blended"),
        "by_segment": (economics.get("by_segment") or [])[:6],
    }


def _slate_block(outputs: dict[str, Any]) -> dict[str, Any]:
    slate = outputs.get("2.3.1") or {}
    return {"slate": (slate.get("slate") or [])[:12], "rejected": (slate.get("rejected") or [])[:6]}


def _structure_block(outputs: dict[str, Any]) -> dict[str, Any]:
    structure = outputs.get("2.4.2") or {}
    check = outputs.get("2.4.3") or {}
    campaigns = structure.get("campaigns") or []
    return {
        "campaign_count": len(campaigns),
        "campaigns": [
            {
                "name": row.get("name"),
                "type": row.get("type"),
                "market": row.get("market"),
                "monthly_budget_usd": row.get("monthly_budget_usd"),
                "ad_groups": len(row.get("ad_groups") or []),
            }
            for row in campaigns[:12]
            if isinstance(row, dict)
        ],
        "structure_verdict": check.get("structure_verdict"),
        "orphan_terms": (structure.get("orphan_terms") or [])[:10],
    }


def _measurement_block(outputs: dict[str, Any]) -> dict[str, Any]:
    truth = outputs.get("2.5.1") or {}
    offline = outputs.get("2.5.2") or {}
    return {
        "primary_source": truth.get("primary_source"),
        "upload": offline.get("upload"),
        "consent": offline.get("consent"),
        "prerequisites": [
            *(truth.get("prerequisites") or []),
            *(offline.get("prerequisites") or []),
        ][:10],
    }


def _backlog_block(outputs: dict[str, Any]) -> dict[str, Any]:
    backlog = outputs.get("2.5.3") or {}
    return {
        "tests": (backlog.get("tests") or [])[:8],
        "reserve_committed_usd": backlog.get("reserve_committed_usd"),
        "not_yet": (backlog.get("not_yet") or [])[:5],
    }


def _registered_plan_nodes() -> tuple[str, ...]:
    """Every non-report *plan* node the registry knows. Used by the suite, not at import."""
    return tuple(
        spec.id
        for spec in get_registry().specs()
        if spec.run_stage is RunStage.PLAN and not spec.id.startswith("2.6.")
    )


plan_synthesis = PlanSynthesisNode()
plan_critique = PlanCritiqueNode()
