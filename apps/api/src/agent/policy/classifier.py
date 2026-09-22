"""Amendment classification (PRD §8.6). The model proposes; it never decides.

One LLM call, `CLASSIFY` class, and its entire output is a *proposal*: a class,
a confidence and a rationale. What happens next is `lifecycle.py`, which is
plain code with no model in it. The two are separate modules because the split
is the safety property — law 29 says a policy change never silently rewrites a
signed rule, and a consequence the model could reach into is a consequence the
model could get wrong.

Two rules are applied to the proposal here, before it leaves this module:

**Confidence below the floor is `unclassified`**, and §8.6 resolves that to
`substantive`. The floor lives in `content_constants.yaml`
(`amendment.confidence_floor`, default 0.8) rather than in this file, because
law 25 puts thresholds in configuration with a source and a reviewed date.

**A diff touching a signature-backed rule is `signature_affecting`, whatever
the model said.** This is a deterministic override, not a prompt instruction.
The model is told about the signed claims, but a model that forgets them must
not be able to downgrade a signature-voiding change to `mechanical` — so the
check is re-run over its answer and can only ever escalate.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal

import sqlalchemy as sa
import structlog
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import (
    AmendmentChangeKind,
    ClaimRecord,
    ClaimSignature,
    ClaimStatus,
    PolicyAmendment,
)
from agent.guidelines.constants import ContentConstants, get_content_constants
from agent.llm.gateway import LLMGateway
from agent.llm.router import ModelRouter, TaskClass

log = structlog.get_logger(__name__)

SYSTEM = (
    "You classify a change to an advertising policy page. You are given the previous "
    "text, the current text and a unified diff. You say which of three kinds of change "
    "it is, how confident you are, and why. You never decide what to do about it — "
    "another system does that, deterministically, from your classification.\n\n"
    "mechanical: a value inside an existing rule changed — a character limit, an asset "
    "count, a dimension, a file-size cap. No rule was added or removed and no authority "
    "changed.\n"
    "substantive: a rule was added or removed, a policy area started or stopped applying, "
    "or a restricted category changed.\n"
    "signature_affecting: the change touches a rule that a named person has personally "
    "signed for. You are given the list of signed claims; say this when the change bears "
    "on any of them.\n\n"
    "If the diff is ambiguous, lower your confidence rather than guessing a class. "
    "A low-confidence answer is routed to a human, which is the correct outcome for an "
    "ambiguous policy change."
)


class ClassificationProposal(BaseModel):
    """What the model is asked for. A proposal, and the type name says so."""

    change_kind: Literal["mechanical", "substantive", "signature_affecting"]
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(min_length=1)
    #: Which of the signed claims, if any, the change bears on. Indices into
    #: the list the prompt was given — never claim text, which would put a
    #: claim into a payload it has no business being in.
    affected_claim_indices: list[int] = Field(default_factory=list)
    proposed_rule_changes: list[str] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Classification:
    """The proposal after this module's two deterministic rules ran over it."""

    change_kind: AmendmentChangeKind
    confidence: float
    rationale: str
    affected_claim_ids: tuple[uuid.UUID, ...]
    proposed_rule_changes: tuple[str, ...]
    #: True when the floor demoted a confident-looking answer to `unclassified`.
    below_floor: bool
    #: True when the signature check escalated the model's class.
    escalated: bool

    @property
    def effective_kind(self) -> AmendmentChangeKind:
        """What §8.6's consequence table is keyed on.

        `unclassified` is not a fourth behaviour. It is `substantive` with a
        different reason written next to it, and `lifecycle.py` reads this.
        """
        if self.change_kind is AmendmentChangeKind.UNCLASSIFIED:
            return AmendmentChangeKind.SUBSTANTIVE
        return self.change_kind


async def signed_claims(
    db: AsyncSession, project_id: uuid.UUID
) -> list[tuple[ClaimRecord, ClaimSignature]]:
    """Approved claims covered by a live signature, oldest first.

    A voided or expired signature does not count: the claim it covered is
    already un-licensed, so a policy change bearing on it is not
    *signature*-affecting — there is no live signature left to void.
    """
    result = await db.execute(
        sa.select(ClaimRecord, ClaimSignature)
        # Joined on `current_signature_id` rather than by searching
        # `ClaimSignature.claim_ids`: signatures are append-only, so a claim
        # signed twice appears in two arrays, and the array search would return
        # the superseded signature alongside the live one.
        .join(ClaimSignature, ClaimSignature.id == ClaimRecord.current_signature_id)
        .where(
            ClaimRecord.project_id == project_id,
            ClaimRecord.status == ClaimStatus.APPROVED,
            ClaimSignature.voided_at.is_(None),
        )
        .order_by(ClaimSignature.signed_at, ClaimRecord.id)
    )
    return [(row[0], row[1]) for row in result.all()]


async def classify(
    db: AsyncSession,
    amendment: PolicyAmendment,
    *,
    llm: LLMGateway,
    router: ModelRouter,
    constants: ContentConstants | None = None,
    project_id: uuid.UUID | None = None,
) -> Classification:
    """Classify one amendment. The only model call in the policy subsystem."""
    conf = constants or get_content_constants()
    floor = float(conf.amendment.confidence_floor.value)
    covered = await signed_claims(db, project_id or amendment.project_id)

    diff = amendment.diff or {}
    proposal = await _propose(amendment, diff=diff, covered=covered, llm=llm, router=router)

    kind = AmendmentChangeKind(proposal.change_kind)
    below_floor = proposal.confidence < floor

    # Rule 1 — the floor. Ambiguity resolves toward the human, always.
    if below_floor:
        kind = AmendmentChangeKind.UNCLASSIFIED

    # Rule 2 — the signature override. Escalate only; a model that decided a
    # signature-voiding change was mechanical must not be able to say so, and a
    # model that over-reports must not be able to un-escalate a real one.
    affected: tuple[uuid.UUID, ...] = ()
    if covered:
        picked = _resolve_indices(proposal.affected_claim_indices, covered)
        if picked:
            affected = picked
            if kind is not AmendmentChangeKind.SIGNATURE_AFFECTING:
                log.info(
                    "amendment.escalated_to_signature_affecting",
                    amendment_id=str(amendment.id),
                    model_said=proposal.change_kind,
                    claims=len(picked),
                )
            kind = AmendmentChangeKind.SIGNATURE_AFFECTING
        elif proposal.change_kind == "signature_affecting":
            # The model said signature_affecting and named nothing. Believe the
            # class, not the empty list: it is the conservative half of the
            # answer, and lifecycle voids by claim so an empty set voids nothing.
            kind = AmendmentChangeKind.SIGNATURE_AFFECTING

    escalated = kind is AmendmentChangeKind.SIGNATURE_AFFECTING and (
        proposal.change_kind != "signature_affecting"
    )
    return Classification(
        change_kind=kind,
        confidence=proposal.confidence,
        rationale=proposal.rationale,
        affected_claim_ids=affected,
        proposed_rule_changes=tuple(proposal.proposed_rule_changes),
        below_floor=below_floor,
        escalated=escalated,
    )


def _resolve_indices(
    indices: list[int], covered: list[tuple[ClaimRecord, ClaimSignature]]
) -> tuple[uuid.UUID, ...]:
    """Turn the model's indices into claim ids, dropping anything out of range.

    Out-of-range is dropped rather than raised: an index the model invented is
    a bad citation, and the cost of raising would be an amendment that never
    gets classified at all. The escalation still happens on whatever resolved.
    """
    picked: list[uuid.UUID] = []
    for index in indices:
        if 0 <= index < len(covered):
            claim, _signature = covered[index]
            if claim.id not in picked:
                picked.append(claim.id)
    return tuple(picked)


async def _propose(
    amendment: PolicyAmendment,
    *,
    diff: dict[str, Any],
    covered: list[tuple[ClaimRecord, ClaimSignature]],
    llm: LLMGateway,
    router: ModelRouter,
) -> ClassificationProposal:
    """The single completion. Kept apart so `classify` reads as its two rules."""
    unified = "\n".join(diff.get("unified") or [])
    claim_lines = [
        f"{index}. {claim.normalized_text or claim.claim_text}"
        for index, (claim, _sig) in enumerate(covered)
    ]
    user = "\n\n".join(
        [
            f"Policy page: {diff.get('label') or amendment.rationale or 'unknown'}",
            f"URL: {diff.get('url') or 'unknown'}",
            f"Area: {diff.get('area') or 'unknown'}",
            (
                f"Diff summary: {diff.get('added', 0)} added, {diff.get('removed', 0)} removed"
                + (
                    "; the diff was truncated, so treat it as at least this large"
                    if diff.get("truncated")
                    else ""
                )
                + (
                    "; the previous text is no longer stored, so the diff may read as though "
                    "the whole page is new — weigh the added/removed counts accordingly"
                    if diff.get("before_unavailable")
                    else ""
                )
            ),
            "Unified diff:\n" + (unified or "(empty)"),
            (
                "Claims a named person has personally signed for (index. text):\n"
                + "\n".join(claim_lines)
                if claim_lines
                else "No claims are currently covered by a live signature."
            ),
        ]
    )
    # `choose` carries the temperature and top_p §9.8 pins for CLASSIFY (0 and
    # 1), so they are not restated here — a second statement of the same
    # setting is a second thing to forget to change.
    completion = await llm.complete_structured(
        output_model=ClassificationProposal,
        system=SYSTEM,
        user=user,
        choice=router.choose(TaskClass.CLASSIFY),
    )
    return completion.value
