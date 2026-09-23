"""Stage 3.6 — the rulebook (Stage 03 PRD §11, §12.1).

Two nodes and one loop between them, and it is the same shape as 1.6 and 2.6
because it is the same problem one stage later.

`guideline_synthesis` (3.6.1) turns sixteen node outputs into the one
`ContentGuideline` the Rulebook Viewer, the six exports, the diff engine and the
publish transaction all read. Most of that is assembly and it lives in
`guidelines/synthesis.py`; what happens *here* is the model call that writes the
prose no projection can produce — the executive summary, and the assumptions and
risks as cited `Claim`s — and the write of the `content_guideline` payload.

`guideline_critique` (3.6.2) reads the finished rulebook back with a **different
model family** (`TaskClass.CRITIQUE` routes cross-vendor for exactly this
reason) and combines two things that are not the same kind of check:

* **§11's ten assertions, computed.** `guidelines/critique.py` decides all ten
  in Python. §11 says the checklist is "fixed and asserted in tests, not left to
  the model's discretion", and an approved claim with no live signature is a
  fact, not an opinion.
* **What only a reader can see.** A summary describing rules the register does
  not contain, a voice profile that contradicts the lexicon, a rule whose
  `message` tells a writer to do the thing another rule forbids. The model is
  asked for those and nothing else.

One blocking issue buys exactly one re-synthesis. **The re-synthesis runs here,
inside 3.6.2**, for the reason `stage_1_6` and `stage_2_6` both give: the
executor is a wavefront over a DAG with no facility for a node to send another
node round again, and building one for a single documented case would be a large
change to the most load-bearing file in the repo. The cost of the second call
lands on 3.6.2's `NodeRun`, which is the only visible difference.

**3.6.2 is what decides `status`.** 3.6.1 cannot: it runs before the critique, so
`ready_to_publish` is not a state it is ever entitled to write. The rule is
`guidelines.synthesis.status_for` and it is a function rather than a judgement —
which matters, because it is the gate between a draft and a document somebody
signs.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
import structlog
from pydantic import BaseModel, Field

from agent.db.models import (
    Approval,
    ApprovalStatus,
    ClaimSignature,
    ContentGuideline as GuidelineRow,
    Evidence,
    GuidelineStatus,
    HumanTask,
    HumanTaskStatus,
    RunStage,
)
from agent.export.contract import Claim, Confidence
from agent.export.guideline_contract import (
    ContentGuideline,
    CritiqueIssue,
    GateDecision,
    HumanTaskRef,
    RegisteredClaim,
    SignatureRef,
)
from agent.export.guideline_markdown import render_guideline_markdown
from agent.guidelines import claims_index, critique as checks, versions
from agent.guidelines.constants import load_content_constants
from agent.guidelines.synthesis import (
    ALL_GUIDELINE_NODES,
    GuidelineFacts,
    assemble,
    status_for,
)
from agent.llm.router import TaskClass
from agent.nodes import prompts
from agent.nodes.base import NodeSpec, RunContext

log = structlog.get_logger(__name__)

#: How much of the rulebook the critic reads. The whole document would be past
#: every context window a `CRITIQUE` model offers, and the sections a reader can
#: usefully contradict are at the front.
CRITIQUE_CHARS = 60_000


# ---------------------------------------------------------------------------
# 3.6.1 guideline_synthesis
# ---------------------------------------------------------------------------


class WrittenClaim(BaseModel):
    """One assertion 3.6.1 makes in prose, with the evidence behind it."""

    statement: str = Field(min_length=1)
    evidence_ids: list[uuid.UUID] = Field(default_factory=list)
    confidence: Confidence = "medium"


class GuidelineNarrative(BaseModel):
    """The only thing the model writes.

    No `status` field, and its absence is the design. 3.6.1 runs before the
    critique, so `ready_to_publish` is not a state it can be entitled to name —
    and a model handed the field would name it, because the document it has just
    read looks finished.
    """

    executive_summary: str = Field(min_length=1)
    assumptions: list[WrittenClaim] = Field(default_factory=list)
    risks: list[WrittenClaim] = Field(default_factory=list)


class GuidelineSynthesisOutput(BaseModel):
    """3.6.1 — the rulebook, as the run records having produced it."""

    guideline_id: str
    schema_version: str
    guideline_status: str
    executive_summary: str
    version: str
    rules: int
    claims: int
    unsupported_claims: int
    policy_areas: int
    asset_specs: int
    review_triggers: int
    open_dependencies: int
    dropped_sections: list[str]
    dropped_claims: int
    markdown_chars: int
    constants_version: str
    cost_usd: float
    evidence_ids: list[str]


class GuidelineSynthesisNode:
    """3.6.1 — sixteen outputs, one rulebook."""

    spec = NodeSpec(
        id="3.6.1",
        name="guideline_synthesis",
        stage="3.6",
        run_stage=RunStage.GUIDELINE,
        depends_on=ALL_GUIDELINE_NODES,
        task_class=TaskClass.SYNTHESIZE,
        input_model=BaseModel,
        output_model=GuidelineSynthesisOutput,
        connectors=(),
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        """Exactly the evidence the upstream nodes cited.

        The rulebook is a fold of their findings, so its citations are a subset
        of theirs by construction. Loading them here is what lets the executor's
        provenance check mean something for the rulebook as well.
        """
        rows = await _cited_evidence(ctx)
        ctx.scratch[self.spec.id] = rows
        return rows

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        guideline, markdown, extra = await synthesise(ctx, ev)
        await _store(ctx, guideline, markdown)
        await ctx.progress(
            f"{guideline.status} — {len(guideline.rules)} rules, "
            f"{len(guideline.claims_register.claims)} claims, "
            f"{len(guideline.open_dependencies)} open dependencies"
        )
        return GuidelineSynthesisOutput(
            guideline_id=str(guideline.guideline_id),
            schema_version=guideline.schema_version,
            guideline_status=guideline.status,
            executive_summary=guideline.executive_summary,
            version=guideline.version,
            rules=len(guideline.rules),
            claims=len(guideline.claims_register.claims),
            unsupported_claims=guideline.claims_register.unsupported_count,
            policy_areas=len(guideline.policy_profile.applicable),
            asset_specs=sum(len(v) for v in guideline.asset_specs.sheet.specs.values()),
            review_triggers=len(guideline.governance.review_triggers),
            open_dependencies=len(guideline.open_dependencies),
            dropped_sections=extra["dropped"],
            dropped_claims=extra["dropped_claims"],
            markdown_chars=len(markdown),
            constants_version=guideline.constants_version,
            cost_usd=guideline.cost_usd,
            evidence_ids=[str(value) for value in _evidence_ids(guideline)],
        )


async def synthesise(
    ctx: RunContext,
    evidence: list[Evidence],
    *,
    critique: list[dict[str, Any]] | None = None,
) -> tuple[ContentGuideline, str, dict[str, Any]]:
    """Build one rulebook. Shared by 3.6.1 and by 3.6.2's single re-synthesis."""
    constants = load_content_constants()
    row = await versions.ensure_draft(
        ctx.db,
        workspace_id=ctx.run.workspace_id,
        project_id=ctx.project.id,
        run_id=ctx.run.id,
        mode=_run_mode(ctx),
    )
    claims = await _registered_claims(ctx)
    decisions = await _gate_decisions(ctx)
    tasks = await _human_tasks(ctx)
    signatures = await _signatures(ctx)

    facts = GuidelineFacts(
        project_id=ctx.project.id,
        guideline_run_id=ctx.run.id,
        guideline_id=row.id,
        version_major=row.version_major,
        version_minor=row.version_minor,
        mode=row.mode.value,
        bindings=row.bindings or {},
        unbound_inputs=list(row.unbound_inputs or []),
        degraded_sources=_degraded(ctx),
        constants_version=constants.version,
        generated_at=datetime.now(UTC),
        cost_usd=float(ctx.ledger.spent),
    )

    narrative = await _write(ctx, facts, critique=critique)
    known = {item.id: (item.content_text or "") for item in evidence}
    dropped_claims, written = _claims(
        [*narrative.assumptions, *narrative.risks], known
    )

    built = assemble(
        ctx.outputs,
        facts,
        constants=constants,
        claims=claims,
        decisions=decisions,
        signatures=signatures,
        human_tasks=tasks,
        executive_summary=narrative.executive_summary,
        written_claims=written,
    )
    guideline = built.guideline
    # 3.6.1 does not choose this. `status_for` is the rule, and with no critique
    # yet run `blocking_issues` is 0 — which is why a clean run lands
    # `ready_to_publish` only after 3.6.2 has actually looked.
    guideline.status = status_for(
        guideline.decisions,
        blocking_issues=0 if critique is None else len(critique),
        open_blocking_tasks=_blocking_tasks(tasks),
    )
    markdown = render_guideline_markdown(guideline, project_name=ctx.project.name)
    return guideline, markdown, {"dropped": built.dropped, "dropped_claims": dropped_claims}


async def _write(
    ctx: RunContext,
    facts: GuidelineFacts,
    *,
    critique: list[dict[str, Any]] | None,
) -> GuidelineNarrative:
    """The one model call. A summary, the assumptions and the risks."""
    outputs = ctx.outputs
    correction = ""
    if critique:
        correction = prompts.computed_block(
            "a previous draft of this summary was rejected — fix exactly these",
            critique[:20],
        )
    return await ctx.complete(
        GuidelineNarrative,
        system=prompts.system_prompt(
            "You write the opening summary of an advertising content rulebook for the "
            "brand, legal and performance owners who have to live by it. The rules "
            "themselves are already decided and are not yours to restate or to change. "
            "You say what this rulebook is, what it was built from, and what it does not "
            "cover. Every assumption and every risk you state cites evidence by id; a "
            "statement you cannot cite is one you do not make."
        ),
        user=prompts.compose(
            prompts.project_block(ctx.project),
            prompts.computed_block(
                "what this rulebook was built from",
                {
                    "mode": facts.mode,
                    "unbound_inputs": list(facts.unbound_inputs),
                    "degraded_sources": list(facts.degraded_sources),
                    "version": f"{facts.version_major}.{facts.version_minor}",
                },
            ),
            prompts.computed_block(
                "the sections already decided (do not restate them, summarise what they mean)",
                {
                    "voice_words": (outputs.get("3.1.1") or {}).get("voice_words"),
                    "banned_terms": len((outputs.get("3.1.2") or {}).get("never") or []),
                    "claims": len((outputs.get("3.2.2") or {}).get("claims") or []),
                    "unsupported": (outputs.get("3.2.2") or {}).get("unsupported_count"),
                    "policy_areas": len((outputs.get("3.3.1") or {}).get("applicable") or []),
                    "not_applicable": len((outputs.get("3.3.1") or {}).get("not_applicable") or []),
                    "asset_scope": (outputs.get("3.4.1") or {}).get("scope"),
                    "review_triggers": len((outputs.get("3.5.2") or {}).get("triggers") or []),
                },
            ),
            prompts.evidence_block(ctx.scratch.get("3.6.1") or []),
            correction,
            "The summary is at most 250 words. Anything longer is the document written twice.",
        ),
    )


# ---------------------------------------------------------------------------
# 3.6.2 guideline_critique
# ---------------------------------------------------------------------------


class ReadingIssue(BaseModel):
    severity: str = "warning"
    section: str = ""
    finding: str = ""
    fix: str = ""


class GuidelineReading(BaseModel):
    """What a reader sees that no predicate can."""

    issues: list[ReadingIssue] = Field(default_factory=list)
    summary_consistent: bool = True
    verdict: str = "pass"


class GuidelineCritiqueOutput(BaseModel):
    """3.6.2 — the verdict, and the status it decides."""

    guideline_id: str
    guideline_status: str
    issues: list[dict[str, Any]]
    blocking: int
    warnings: int
    failed_checks: list[str]
    assertions_run: int
    summary_consistent: bool
    resynthesised: bool
    resynthesis_resolved: int
    resynthesis_remaining: int
    verdict: str


class GuidelineCritiqueNode:
    """3.6.2 — ten computed assertions, one reader from another model family."""

    spec = NodeSpec(
        id="3.6.2",
        name="guideline_critique",
        stage="3.6",
        run_stage=RunStage.GUIDELINE,
        depends_on=("3.6.1",),
        task_class=TaskClass.CRITIQUE,
        input_model=GuidelineSynthesisOutput,
        output_model=GuidelineCritiqueOutput,
        connectors=(),
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        rows = await _cited_evidence(ctx)
        ctx.scratch[self.spec.id] = rows
        ctx.scratch["3.6.1"] = rows
        return rows

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        row = await versions.for_run(ctx.db, ctx.run.id)
        if row is None or not row.payload:  # pragma: no cover — 3.6.1 is a hard dependency
            raise RuntimeError("3.6.2 ran with no rulebook to critique")

        guideline = ContentGuideline.model_validate(row.payload)
        now = datetime.now(UTC)
        hashes = await _signature_hashes(ctx)

        asserted = checks.run_checks(guideline, now=now, signature_set_hashes=hashes)
        reading = await self._read(ctx, guideline, row.markdown or "", asserted)
        issues = [*asserted, *_as_issues(reading)]
        blocking = checks.blocking(issues)

        resynthesised = False
        remaining: list[CritiqueIssue] = []
        if blocking:
            # §11: one re-synthesis, with the critique appended. Not a loop.
            await ctx.progress(
                f"{len(blocking)} blocking issue(s) — re-synthesising the rulebook once"
            )
            revised, markdown, _ = await synthesise(
                ctx, ev, critique=[issue.model_dump(mode="json") for issue in issues]
            )
            remaining = checks.blocking(
                checks.run_checks(revised, now=now, signature_set_hashes=hashes)
            )
            revised.critique_issues = list(remaining)
            await _store(ctx, revised, markdown)
            guideline = revised
            issues = [*remaining, *_as_issues(reading)]
            resynthesised = True

        status = status_for(
            guideline.decisions,
            blocking_issues=len(checks.blocking(issues)),
            open_blocking_tasks=_blocking_tasks(await _human_tasks(ctx)),
        )
        await _finalise(ctx, guideline, status, issues)
        warnings = [issue for issue in issues if issue.severity != "blocking"]
        await ctx.progress(
            f"{status} — {len(checks.blocking(issues))} blocking, {len(warnings)} warnings"
        )
        return GuidelineCritiqueOutput(
            guideline_id=str(guideline.guideline_id),
            guideline_status=status,
            issues=[issue.model_dump(mode="json") for issue in issues],
            blocking=len(checks.blocking(issues)),
            warnings=len(warnings),
            failed_checks=sorted({issue.check for issue in issues if issue.check}),
            assertions_run=10,
            summary_consistent=reading.summary_consistent,
            resynthesised=resynthesised,
            resynthesis_resolved=len(blocking) - len(remaining) if resynthesised else 0,
            resynthesis_remaining=len(remaining),
            verdict="fail" if checks.blocking(issues) else "pass",
        )

    async def _read(
        self,
        ctx: RunContext,
        guideline: ContentGuideline,
        markdown: str,
        asserted: list[CritiqueIssue],
    ) -> GuidelineReading:
        return await ctx.complete(
            GuidelineReading,
            system=prompts.system_prompt(
                "You are reviewing a content rulebook another model assembled, for the "
                "brand and legal owners who are about to be bound by it. Ten mechanical "
                "checks have already run and their results are shown to you — do not "
                "repeat them and do not re-derive them. Report only what a careful reader "
                "sees and a predicate cannot: a summary describing rules the register does "
                "not contain, a voice profile that contradicts the lexicon, two rules that "
                "cannot both be satisfied, a section that oversells how much was actually "
                "read. A finding you cannot point at a section of the document is not a "
                "finding, and inventing one to look thorough costs a full re-synthesis."
            ),
            user=prompts.compose(
                prompts.computed_block(
                    "the ten mechanical checks have already reported",
                    [issue.model_dump(mode="json") for issue in asserted[:40]],
                ),
                f"THE RULEBOOK\n{markdown[:CRITIQUE_CHARS]}",
            ),
            task_class=TaskClass.CRITIQUE,
        )


# ---------------------------------------------------------------------------
# reading the run
# ---------------------------------------------------------------------------


async def _cited_evidence(ctx: RunContext) -> list[Evidence]:
    """Every evidence row the upstream nodes cited, loaded once."""
    wanted: set[uuid.UUID] = set()
    for output in ctx.outputs.values():
        wanted |= _ids_in(output)
    if not wanted:
        return []
    rows = (
        (
            await ctx.db.execute(
                sa.select(Evidence).where(
                    Evidence.id.in_(wanted), Evidence.project_id == ctx.project.id
                )
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


def _ids_in(payload: Any) -> set[uuid.UUID]:
    from agent.nodes.base import collect_evidence_ids

    return collect_evidence_ids(payload)


async def _registered_claims(ctx: RunContext) -> list[RegisteredClaim]:
    """The claims register, from `claim_record` rather than from node output.

    The rows are what the legal owner signs and what `claims_index` compiles, so
    they are the register. 3.2.2's output is the draft that produced them, and a
    rulebook built from the draft would not show a rejection the signer made.
    """
    rows = await versions.claims_with_signatures(ctx.db, ctx.project.id)
    now = datetime.now(UTC)
    return [
        RegisteredClaim(
            claim_id=claim.id,
            claim_text=claim.claim_text,
            normalized_text=claim.normalized_text,
            surface_forms=[str(item) for item in (claim.surface_forms or [])],
            claim_type=claim.claim_type.value,
            status=claim.status.value,
            risk_tier=claim.risk_tier.value,
            market_scope=list(claim.market_scope or []),
            languages=list(claim.languages or []),
            substantiation=dict(claim.substantiation or {}),
            expires_at=claim.expires_at,
            signature_id=(
                signature.id
                if claims_index.signature_is_live(signature, now=now)
                else None
            ),
            evidence_ids=list(claim.evidence_ids or []),
        )
        for claim, signature in rows
    ]


async def _gate_decisions(ctx: RunContext) -> list[GateDecision]:
    """G5 and G6 for this run. Latest row wins on a duplicate."""
    rows = (
        (
            await ctx.db.execute(
                sa.select(Approval)
                .where(Approval.run_id == ctx.run.id)
                .order_by(Approval.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    found: dict[str, GateDecision] = {}
    for approval in rows:
        key = (approval.gate_key or "").strip().upper()
        if key not in {"G5", "G6"}:
            continue
        found[key] = GateDecision(
            gate_key=key,  # type: ignore[arg-type]  # filtered above
            node_id=approval.node_id,
            status=_approval_status(approval.status),
            decided_by=approval.decided_by,
            decided_at=approval.decided_at,
            note=approval.decision_note or "",
            edits_applied=_edits(approval),
        )
    return [found[key] for key in sorted(found)]


def _approval_status(status: ApprovalStatus) -> str:
    return {
        ApprovalStatus.APPROVED: "approved",
        ApprovalStatus.REJECTED: "rejected",
        ApprovalStatus.PENDING: "pending",
    }.get(status, "pending")


def _edits(approval: Approval) -> list[dict[str, Any]]:
    """What the approver changed before approving. Empty on a clean approval."""
    if not approval.edited_proposal:
        return []
    return [{"field": key, "value": value} for key, value in approval.edited_proposal.items()]


async def _human_tasks(ctx: RunContext) -> list[HumanTaskRef]:
    rows = (
        (
            await ctx.db.execute(
                sa.select(HumanTask)
                .where(HumanTask.guideline_run_id == ctx.run.id)
                .order_by(HumanTask.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    return [
        HumanTaskRef(
            task_id=task.id,
            task_key=task.task_key,  # type: ignore[arg-type]  # 'H1' | 'H2'
            status=task.status.value,
            blocking_for=task.blocking_for.value,
            assignee_id=task.assignee_id,
            node_id=task.node_id or "",
        )
        for task in rows
        if task.task_key in {"H1", "H2"}
    ]


async def _signatures(ctx: RunContext) -> list[SignatureRef]:
    rows = (
        (
            await ctx.db.execute(
                sa.select(ClaimSignature)
                .where(ClaimSignature.project_id == ctx.project.id)
                .order_by(ClaimSignature.signed_at.asc())
            )
        )
        .scalars()
        .all()
    )
    return [
        SignatureRef(
            signature_id=signature.id,
            signer_id=signature.signer_id,
            set_hash=signature.set_hash,
            signed_at=signature.signed_at,
            expires_at=signature.expires_at,
            voided_at=signature.voided_at,
            claim_count=len(signature.claim_ids or []),
        )
        for signature in rows
    ]


async def _signature_hashes(ctx: RunContext) -> dict[uuid.UUID, str]:
    """What each signature was actually taken over, for §11 assertion 3."""
    rows = (
        (
            await ctx.db.execute(
                sa.select(ClaimSignature.id, ClaimSignature.set_hash).where(
                    ClaimSignature.project_id == ctx.project.id
                )
            )
        )
        .all()
    )
    return {row[0]: row[1] for row in rows}


def _blocking_tasks(tasks: list[HumanTaskRef]) -> int:
    """Open person-tasks that block **publish**. H2 blocks launch, not publish."""
    return len(
        [
            task
            for task in tasks
            if task.status not in {HumanTaskStatus.COMPLETED.value, "cancelled"}
            and task.blocking_for == "publish"
        ]
    )


def _degraded(ctx: RunContext) -> list[str]:
    """Sources a node reported it could not read, deduplicated across the run."""
    found: set[str] = set()
    for output in ctx.outputs.values():
        value = output.get("degraded_sources") if isinstance(output, dict) else None
        if isinstance(value, list):
            found |= {str(item) for item in value if item}
    scratch = ctx.scratch.get("degraded_sources")
    if isinstance(scratch, set | list):
        found |= {str(item) for item in scratch if item}
    return sorted(found)


def _run_mode(ctx: RunContext) -> Any:
    from agent.db.models import GuidelineMode

    bindings = ctx.run.bindings if isinstance(ctx.run.bindings, dict) else {}
    try:
        return GuidelineMode(str(bindings.get("mode")))
    except ValueError:
        return GuidelineMode.STANDALONE


# ---------------------------------------------------------------------------
# writing it back
# ---------------------------------------------------------------------------


async def _store(ctx: RunContext, guideline: ContentGuideline, markdown: str) -> GuidelineRow:
    """Persist the rulebook and commit. One row per run; the second write updates."""
    row = await versions.ensure_draft(
        ctx.db,
        workspace_id=ctx.run.workspace_id,
        project_id=ctx.project.id,
        run_id=ctx.run.id,
        mode=_run_mode(ctx),
    )
    row.payload = guideline.model_dump(mode="json")
    row.markdown = markdown
    row.status = _row_status(guideline.status)
    row.unbound_inputs = list(guideline.unbound_inputs)
    await ctx.db.commit()
    return row


async def _finalise(
    ctx: RunContext,
    guideline: ContentGuideline,
    status: str,
    issues: list[CritiqueIssue],
) -> None:
    """Write the critique's verdict onto the stored rulebook.

    The payload carries the issues so an exported PDF shows its own review — a
    document that says `ready_to_publish` while the critique that said otherwise
    lives somewhere else is how a blocked rulebook circulates as an approved one.
    """
    guideline.status = status  # type: ignore[assignment]  # from `status_for`
    guideline.critique_issues = list(issues)
    await _store(
        ctx, guideline, render_guideline_markdown(guideline, project_name=ctx.project.name)
    )


def _row_status(status: str) -> GuidelineStatus:
    return {
        "draft": GuidelineStatus.DRAFT,
        "blocked": GuidelineStatus.BLOCKED,
        "ready_to_publish": GuidelineStatus.READY_TO_PUBLISH,
        "published": GuidelineStatus.PUBLISHED,
    }[status]


def _as_issues(reading: GuidelineReading) -> list[CritiqueIssue]:
    """The model's findings, in the same shape as the computed ones.

    A severity the model invented is narrowed to the three §11 allows, defaulting
    to `warning`. A model cannot promote its own reading to `blocking` and buy a
    re-synthesis with it — that decision belongs to the ten assertions.
    """
    found: list[CritiqueIssue] = []
    for row in reading.issues:
        if not row.finding.strip():
            continue
        severity = row.severity if row.severity in {"warning", "note"} else "warning"
        found.append(
            CritiqueIssue(
                severity=severity,  # type: ignore[arg-type]  # narrowed above
                section=row.section or "rulebook",
                finding=row.finding,
                fix=row.fix,
                check="reader",
            )
        )
    return found


def _claims(
    written: list[WrittenClaim], known: dict[uuid.UUID, str]
) -> tuple[int, list[Claim]]:
    """Validate the model's citations. Returns (dropped, claims).

    A claim citing an id that does not exist is not repaired into one citing a
    different id — that would attach real evidence to an assertion it never
    supported. The bad ids are removed and a claim left with none is dropped.
    """
    kept: list[Claim] = []
    dropped = 0
    for item in written:
        valid = [value for value in item.evidence_ids if value in known]
        if not valid:
            dropped += 1
            continue
        kept.append(
            Claim(statement=item.statement, evidence_ids=valid, confidence=item.confidence)
        )
    return dropped, kept


def _evidence_ids(guideline: ContentGuideline) -> list[uuid.UUID]:
    found: list[uuid.UUID] = []
    for claim in guideline.written_claims:
        for value in claim.evidence_ids:
            if value not in found:
                found.append(value)
    for rule in guideline.rules:
        for value in rule.evidence_ids:
            if value not in found:
                found.append(value)
    return found


#: Module-level instances. The registry discovers instances, not classes.
guideline_synthesis = GuidelineSynthesisNode()
guideline_critique = GuidelineCritiqueNode()
