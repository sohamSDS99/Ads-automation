"""Stage 1.6 — the report (PRD §10, §11).

Two nodes and one loop between them.

`report_synthesis` (1.6.1) turns nineteen node outputs into the one
`ResearchReport` everything downstream reads. Most of that is assembly, and it
lives in `nodes/synthesis.py`; what happens *here* is the model call that writes
the prose no projection can produce — the executive summary, the blockers and
next actions as cited claims, the open questions — and the write of the
`report` row that `GET /reports/{run_id}` has been 404-ing on since P5a.

`report_critique` (1.6.2) reads the finished object back with a **different
model family** (`TaskClass.CRITIQUE` routes cross-vendor for exactly this
reason) and looks for the four failures PRD §10 names: unsupported claims,
contradictions between sections, missing evidence ids, and a launch-readiness
verdict the prose does not match. One blocking issue buys one re-synthesis, and
only one — a loop that keeps going until a critic is happy is a loop that can
spend a budget without converging.

**The re-synthesis is executed here, inside 1.6.2.** PRD §10 says "1.6.1 re-runs
once"; the executor is a wavefront over a DAG with no facility for a node to
send another node round again, and inventing one for a single documented case
would be a large amount of new machinery in the most load-bearing file in the
repo. So the *function* 1.6.1 runs is re-run, with the critique appended, and
the same `report` row is rewritten. The difference is visible in exactly one
place — the second call's cost lands on 1.6.2's `NodeRun` — and the alternative
was worse. It is called out in the pull request rather than buried here.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

import sqlalchemy as sa
import structlog
from pydantic import BaseModel, Field

from agent.db.models import Evidence, RunStage
from agent.db.repos import ReportRepo
from agent.export.contract import Claim, Confidence, ResearchReport
from agent.export.markdown import render_markdown
from agent.llm.router import TaskClass
from agent.nodes import prompts, synthesis
from agent.nodes.base import NodeSpec, RunContext
from agent.orchestrator.registry import get_registry

log = structlog.get_logger(__name__)

#: Every node whose output the report is built from. PRD §10: `1.6.1←{all}`.
#: Written out rather than derived, because `depends_on` is read at import time
#: and the registry is what imports this module. `test_stage_1_6.py` asserts it
#: equals every non-report node the registry knows, so a node added later fails
#: the suite instead of quietly never reaching the report.
ALL_RESEARCH_NODES: tuple[str, ...] = (
    "1.1.1",
    "1.1.2",
    "1.1.3",
    "1.1.4",
    "1.1.5",
    "1.2.1",
    "1.2.2",
    "1.2.3",
    "1.3.1",
    "1.3.2",
    "1.3.3",
    "1.3.4",
    "1.4.1",
    "1.4.2",
    "1.4.3",
    "1.4.4",
    "1.4.5",
    "1.5.1",
    "1.5.2",
    "1.5.3",
    "1.5.4",
)

#: Evidence rows loaded for the report to cite. The union of what nineteen nodes
#: cited is normally a few hundred; the cap is a guard against a runaway node,
#: and a report that hits it loses citations rather than the whole run.
MAX_EVIDENCE = 4_000

#: Characters of one evidence payload shown to the model when it picks citations.
CITATION_CHARS = 240


# ---------------------------------------------------------------------------
# 1.6.1 — report_synthesis
# ---------------------------------------------------------------------------


class WrittenClaim(BaseModel):
    """A claim the model wants to make, before it has been checked."""

    statement: str
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "medium"


class ReportNarrative(BaseModel):
    """The only part of the report a model writes."""

    executive_summary: str = Field(
        default="",
        description="At most 250 words. What we found, what it means, what happens next.",
    )
    launch_blockers: list[WrittenClaim] = Field(default_factory=list)
    recommended_next_actions: list[WrittenClaim] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


class ReportSynthesisOutput(BaseModel):
    """1.6.1's node output — a receipt, not the report.

    The report itself is 5,000 keywords wide and lives in its own table; copying
    it into `node_run.output` as well would double the largest write in the run
    and make the run-console payload unreadable. What stays here is what the
    console shows and what 1.6.2 needs, plus `evidence_ids` — which is not
    decoration: the executor checks a node's cited ids against what it gathered,
    and that check is the report's provenance guarantee.
    """

    report_id: str
    schema_version: str
    launch_readiness: Literal["go", "go_with_fixes", "no_go"]
    executive_summary: str
    blockers: int = 0
    fixables: int = 0
    uncited_blockers: list[str] = Field(default_factory=list)
    dropped_claims: int = 0
    #: Rows that did not fit the report contract and were left out rather than
    #: taking the run down with them. See `synthesis._safe`. Non-zero here means
    #: a node's output and §11 have drifted apart and want reconciling.
    dropped_rows: list[str] = Field(default_factory=list)
    sections: dict[str, int] = Field(default_factory=dict)
    priced_keywords: int = 0
    markdown_chars: int = 0
    cost_usd: float = 0.0
    degraded_sources: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class ReportSynthesisNode:
    """1.6.1 — the handoff artifact to Stage 02."""

    spec = NodeSpec(
        id="1.6.1",
        name="report_synthesis",
        stage="1.6",
        depends_on=ALL_RESEARCH_NODES,
        task_class=TaskClass.SYNTHESIZE,
        input_model=BaseModel,
        output_model=ReportSynthesisOutput,
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        """Exactly the evidence the upstream nodes cited — no more, no less.

        The report is a fold of their findings, so its citations are a subset of
        theirs by construction. Loading them here is what lets the executor's
        provenance check mean something for the report as well.
        """
        wanted = synthesis.cited_evidence_ids(ctx.outputs)[:MAX_EVIDENCE]
        rows = await _load_evidence(ctx, wanted)
        ctx.scratch[self.spec.id] = rows
        return rows

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        report, markdown, extra = await synthesise(ctx, ev)
        stored = await _store(ctx, report, markdown)
        await ctx.progress(
            f"{report.launch_readiness} — {len(report.launch_blockers)} blockers, "
            f"{len(report.priced_keyword_list)} keywords"
        )
        return ReportSynthesisOutput(
            report_id=str(stored.id),
            schema_version=report.schema_version,
            launch_readiness=report.launch_readiness,
            executive_summary=report.executive_summary,
            blockers=len(report.launch_blockers),
            fixables=extra["fixables"],
            uncited_blockers=extra["uncited"],
            dropped_claims=extra["dropped"],
            dropped_rows=extra["dropped_rows"],
            sections=_section_counts(report),
            priced_keywords=len(report.priced_keyword_list),
            markdown_chars=len(markdown),
            cost_usd=report.cost_usd,
            degraded_sources=list(report.degraded_sources),
            open_questions=list(report.open_questions),
            evidence_ids=[str(value) for value in report.evidence_ids()],
        )


async def synthesise(
    ctx: RunContext, evidence: list[Evidence], *, critique: list[dict[str, Any]] | None = None
) -> tuple[ResearchReport, str, dict[str, Any]]:
    """Write one report. Shared by 1.6.1 and by 1.6.2's single re-run.

    Returns the report, its rendered markdown and the bookkeeping the node
    output reports — how many blockers could not be cited, and how many of the
    model's claims were dropped for citing evidence that does not exist.
    """
    outputs = dict(ctx.outputs)
    dropped_rows: list[str] = []
    launch_readiness, blocking, fixable = synthesis.verdict(outputs)
    known = {row.id: _digest(row) for row in evidence}

    narrative = await ctx.complete(
        ReportNarrative,
        system=prompts.system_prompt(
            "You are writing the executive layer of a paid-search research report for the "
            "people who will fund and run the campaign. The findings are already established "
            "and the launch verdict has already been decided from them — your job is to say "
            "what they mean, not to re-decide them. Write plainly, in full sentences, with no "
            "marketing register and no hedging."
        ),
        user=prompts.compose(
            prompts.project_block(ctx.project),
            prompts.computed_block(
                "the launch verdict (final — write prose consistent with it)",
                {
                    "launch_readiness": launch_readiness,
                    "blocking": [fact.statement for fact in blocking],
                    "to_fix": [fact.statement for fact in fixable],
                },
            ),
            prompts.computed_block("what we sell (1.1.1)", outputs.get("1.1.1", {})),
            prompts.computed_block(
                "what our own account already proved (1.2.1, 1.2.2 totals)",
                {
                    "history": (outputs.get("1.2.1") or {}).get("structural_findings"),
                    "pnl": (outputs.get("1.2.2") or {}).get("totals"),
                },
            ),
            prompts.computed_block(
                "the competition (1.3.3, 1.3.4)",
                {
                    "spend_estimates": (outputs.get("1.3.3") or {}).get("estimates", [])[:8],
                    "recommended_claim": (outputs.get("1.3.4") or {}).get("recommended_claim"),
                },
            ),
            prompts.computed_block(
                "demand (1.4.3 totals, 1.4.5 gaps)",
                {
                    "totals": (outputs.get("1.4.3") or {}).get("totals"),
                    "content_gaps": (outputs.get("1.4.5") or {}).get("content_gaps", [])[:10],
                },
            ),
            prompts.computed_block(
                "readiness (1.5.*)",
                {
                    "pages_by_severity": _severity_counts(outputs),
                    "tracking_alerts": (outputs.get("1.5.2") or {}).get("alerts"),
                    "synthetic_check": (outputs.get("1.5.2") or {}).get("synthetic_check"),
                    "scenarios": (outputs.get("1.5.4") or {}).get("scenarios"),
                },
            ),
            prompts.computed_block(
                "evidence you may cite (use these ids verbatim)",
                synthesis.citable(outputs, known),
            ),
            _critique_block(critique),
            "TASK\n"
            "  1. An executive summary of at most 250 words: what we found, whether we can "
            "launch, and the first thing to do. It must agree with the verdict above.\n"
            "  2. Any launch blocker the verdict does not already name, each with at least "
            "one `evidence_ids` value copied from the list above.\n"
            "  3. Three to seven recommended next actions, each cited the same way, ordered "
            "by what earns or saves the most.\n"
            "  4. Open questions: what a person still has to answer. No citations needed.",
        ),
    )

    dropped, blockers = _claims(narrative.launch_blockers, known, confidence="high")
    dropped_actions, actions = _claims(narrative.recommended_next_actions, known)
    computed = [fact.as_claim() for fact in blocking]
    uncited = [fact.statement for fact in blocking if fact.as_claim() is None]

    report = synthesis.assemble(
        project=ctx.project,
        run=ctx.run,
        outputs=outputs,
        summary=_summary(narrative.executive_summary, launch_readiness, blocking),
        launch_readiness=launch_readiness,
        launch_blockers=[claim for claim in computed if claim is not None] + blockers,
        next_actions=actions,
        open_questions=[
            *(f"Unresolved blocker with no citable evidence: {text}" for text in uncited),
            *[str(item) for item in narrative.open_questions],
        ],
        cost_usd=float(ctx.ledger.spent_usd),
        drop=dropped_rows.append,
    )
    return (
        report,
        render_markdown(report, project_name=ctx.project.name),
        {
            "fixables": len(fixable),
            "uncited": uncited,
            "dropped": dropped + dropped_actions,
            "dropped_rows": sorted(set(dropped_rows))[:20],
        },
    )


# ---------------------------------------------------------------------------
# 1.6.2 — report_critique
# ---------------------------------------------------------------------------


class CritiqueIssue(BaseModel):
    severity: Literal["blocking", "major", "minor"] = "minor"
    section: str = Field(default="", description="Which part of the report is wrong.")
    issue: str
    fix: str = Field(default="", description="What would make it right.")


class ReportCritique(BaseModel):
    """1.6.2's model output — the four checks PRD §10 names."""

    issues: list[CritiqueIssue] = Field(default_factory=list)
    verdict_consistent: bool = Field(
        default=True,
        description="Does the prose agree with the stated launch verdict?",
    )
    unsupported_claims: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)


class ReportCritiqueOutput(BaseModel):
    """1.6.2's node output."""

    report_id: str
    issues: list[CritiqueIssue] = Field(default_factory=list)
    blocking: int = 0
    verdict_consistent: bool = True
    resynthesised: bool = False
    resynthesis_resolved: int = 0
    launch_readiness: Literal["go", "go_with_fixes", "no_go"] = "go_with_fixes"
    missing_evidence: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class ReportCritiqueNode:
    """1.6.2 — a second model, from another family, reading the first one's work."""

    spec = NodeSpec(
        id="1.6.2",
        name="report_critique",
        stage="1.6",
        depends_on=("1.6.1",),
        task_class=TaskClass.CRITIQUE,
        input_model=ReportSynthesisOutput,
        output_model=ReportCritiqueOutput,
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        wanted = synthesis.cited_evidence_ids(ctx.outputs)[:MAX_EVIDENCE]
        rows = await _load_evidence(ctx, wanted)
        ctx.scratch[self.spec.id] = rows
        return rows

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        repo = ReportRepo(ctx.db, ctx.run.workspace_id)
        stored = await repo.for_run(ctx.run.id)
        if stored is None:  # pragma: no cover — 1.6.1 is a hard dependency
            raise RuntimeError("1.6.2 ran with no report to critique")
        report = ResearchReport.model_validate(stored.payload)

        known = {row.id for row in ev}
        missing = [str(value) for value in report.evidence_ids() if value not in known]
        critique = await self._critique(ctx, report, stored.markdown, missing)
        blocking = [issue for issue in critique.issues if issue.severity == "blocking"]

        resynthesised = False
        resolved = 0
        if blocking:
            # PRD §10: one re-run, with the critique appended. Not a loop.
            await ctx.progress(f"critique found {len(blocking)} blocking issues — re-synthesising")
            revised, markdown, _ = await synthesise(
                ctx, ev, critique=[issue.model_dump() for issue in critique.issues]
            )
            await _store(ctx, revised, markdown)
            report = revised
            resynthesised = True
            resolved = len(blocking)

        return ReportCritiqueOutput(
            report_id=str(stored.id),
            issues=critique.issues,
            blocking=len(blocking),
            verdict_consistent=critique.verdict_consistent,
            resynthesised=resynthesised,
            resynthesis_resolved=resolved,
            launch_readiness=report.launch_readiness,
            missing_evidence=missing[:50],
            evidence_ids=[str(value) for value in report.evidence_ids()],
        )

    async def _critique(
        self, ctx: RunContext, report: ResearchReport, markdown: str, missing: list[str]
    ) -> ReportCritique:
        return await ctx.complete(
            ReportCritique,
            system=prompts.system_prompt(
                "You are reviewing a research report another model wrote, for a reader who "
                "will spend money on it. You are looking for four things and nothing else: a "
                "claim the evidence does not support, two sections that contradict each "
                "other, a citation that points at nothing, and prose that does not match the "
                "stated launch verdict. Say 'blocking' only when a reader acting on the "
                "report would be misled into spending money badly."
            ),
            user=prompts.compose(
                prompts.computed_block(
                    "the stated verdict and its blockers",
                    {
                        "launch_readiness": report.launch_readiness,
                        "launch_blockers": [claim.statement for claim in report.launch_blockers],
                        "open_questions": report.open_questions,
                        "degraded_sources": report.degraded_sources,
                    },
                ),
                prompts.computed_block("citations that resolve to no evidence row", missing[:20]),
                f"THE REPORT\n{markdown[:60_000]}",
                "TASK\n"
                "  List the issues you actually find. An empty list is the correct answer for "
                "a sound report, and inventing an issue to look thorough wastes a re-run of "
                "the whole synthesis. For each: severity, the section, what is wrong, and the "
                "fix. Then say whether the prose agrees with the launch verdict.",
            ),
            task_class=TaskClass.CRITIQUE,
        )


# ---------------------------------------------------------------------------
# shared
# ---------------------------------------------------------------------------


async def _load_evidence(ctx: RunContext, wanted: list[uuid.UUID]) -> list[Evidence]:
    """The evidence rows behind a set of ids, scoped to this project."""
    if not wanted:
        return []
    rows = await ctx.db.execute(
        sa.select(Evidence).where(Evidence.project_id == ctx.project.id, Evidence.id.in_(wanted))
    )
    return list(rows.scalars().all())


async def _store(ctx: RunContext, report: ResearchReport, markdown: str) -> Any:
    """Persist the report and commit. One row per run; the second write updates."""
    repo = ReportRepo(ctx.db, ctx.run.workspace_id)
    stored = await repo.upsert(
        run_id=ctx.run.id,
        schema_version=report.schema_version,
        payload=report.model_dump(mode="json"),
        markdown=markdown,
    )
    await ctx.db.commit()
    return stored


def _claims(
    written: list[WrittenClaim],
    known: dict[uuid.UUID, str],
    *,
    confidence: Confidence | None = None,
) -> tuple[int, list[Claim]]:
    """Validate the model's citations. Returns (dropped, claims).

    A claim citing an id that does not exist is not repaired into one citing a
    different id — that would attach real evidence to an assertion it never
    supported. The bad ids are removed, and a claim left with none is dropped
    and counted, which is what `dropped_claims` on the node output reports.
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


def _summary(written: str, launch_readiness: str, blocking: list[synthesis.Fact]) -> str:
    """The summary, guaranteed non-empty and within §11's word budget.

    `ResearchReport.executive_summary` has `min_length=1` and a 250-word cap. A
    model that returns an empty string, or a 400-word essay, must not cost the
    run its last node — so an empty summary is replaced by the verdict stated
    plainly, and a long one is cut at the word budget.
    """
    text = " ".join(written.split())
    if not text:
        blockers = "; ".join(fact.statement for fact in blocking[:3])
        text = f"Launch readiness: {launch_readiness.replace('_', ' ')}." + (
            f" Blocking: {blockers}" if blockers else ""
        )
    words = text.split()
    if len(words) > 250:
        text = " ".join(words[:249]) + "…"
    return text


def _digest(row: Evidence) -> str:
    """One evidence row, short enough that sixty of them fit in a prompt."""
    import json

    body = json.dumps(row.payload, default=str, separators=(", ", ": "), sort_keys=True)
    return f"{row.kind}: {body[:CITATION_CHARS]}"


def _section_counts(report: ResearchReport) -> dict[str, int]:
    return {
        "products": len(report.business_context.products),
        "segments": len(report.business_context.segments),
        "markets": len(report.business_context.markets),
        "profitable_terms": len(report.account_learnings.profitable_terms),
        "wasteful_terms": len(report.account_learnings.wasteful_terms),
        "competitors": len(report.competitive_landscape.competitors),
        "ads": len(report.competitive_landscape.ads),
        "negatives": len(report.demand_map.negatives),
        "mapping": len(report.demand_map.mapping),
        "pages": len(report.readiness.pages),
        "conversion_actions": len(report.readiness.conversion_actions),
        "audience_lists": len(report.readiness.lists),
        "scenarios": len(report.readiness.scenarios),
        "next_actions": len(report.recommended_next_actions),
    }


def _severity_counts(outputs: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for page in (outputs.get("1.5.1") or {}).get("pages") or []:
        if isinstance(page, dict):
            key = str(page.get("severity") or "none")
            counts[key] = counts.get(key, 0) + 1
    return counts


def _critique_block(critique: list[dict[str, Any]] | None) -> str:
    """The second pass's extra instruction. Empty on the first pass."""
    if not critique:
        return ""
    return prompts.compose(
        prompts.computed_block("a reviewer's issues with your previous draft", critique),
        "REVISION\n"
        "  This is your one revision. Fix every blocking issue above. Do not restate the "
        "verdict differently unless the issue says the prose contradicts it — the verdict "
        "itself is computed and is not yours to change.",
    )


def _registered_research_nodes() -> tuple[str, ...]:
    """Every non-report *research* node the registry knows. Used by the suite, not at import.

    The `run_stage` filter is not cosmetic: the registry holds both pipelines
    (Stage 02 PRD §8.1), and without it 1.6.1 would declare a dependency on
    plan nodes that run in a different DAG and can never satisfy it.
    """
    return tuple(
        spec.id
        for spec in get_registry().specs()
        if spec.run_stage is RunStage.RESEARCH and not spec.id.startswith("1.6.")
    )


report_synthesis = ReportSynthesisNode()
report_critique = ReportCritiqueNode()
