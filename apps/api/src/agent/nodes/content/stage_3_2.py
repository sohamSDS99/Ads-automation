"""Stage 3.2 — what we are allowed to claim. Nodes 3.2.1 and 3.2.2.

Two nodes, two refusals, and the refusals are the point.

**3.2.1 harvests; it does not propose.** It reads the copy we have already
published — live ads, site pages, the brand book — and reports the claims
already in it. Every candidate must be observable in the corpus the node
gathered, checked by set membership rather than by asking the model nicely. A
claim produced without being seen is invention, and invention is how a sentence
nobody ever wrote ends up in front of a legal owner with a tick box beside it.

**3.2.2 binds each claim to its evidence, or leaves it unsupported.** It may
return `unsupported` or `pending_signoff` and nothing else. `approved` is
reachable through exactly one path in this system — a named human's
step-up-authenticated signature — and a `CLASSIFY` call that could reach it
would make law 24 a suggestion and the signature decorative.

The arithmetic is ours too. The model picks the *basis* for an expiry
(qualitative or quantified); the number of days is a lookup in
`content_constants.yaml`. Stage 02's law 2 applies to dates as much as to money.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

from agent.db.models import ClaimRiskTier, ClaimStatus, ClaimType, Evidence, RunStage
from agent.evidence.redact import redact_pii
from agent.guardrails.normalize import normalize
from agent.guidelines.constants import load_content_constants
from agent.llm.router import TaskClass
from agent.nodes import gather, prompts
from agent.nodes.base import NodeContractError, NodeSpec, RunContext
from agent.nodes.stage_1_1 import PAGE

#: The evidence a claim can be observed in. Site copy and live ads carry most
#: of them; the brand book carries the ones marketing believes are settled.
CLAIM_KINDS = frozenset({"creative_history", PAGE, "page", "brand_book_span"})

#: The two statuses 3.2.2 may return. Anything else is a contract breach — see
#: the module docstring.
DRAFTABLE = {ClaimStatus.UNSUPPORTED.value, ClaimStatus.PENDING_SIGNOFF.value}


# ---------------------------------------------------------------------------
# 3.2.1 claim_harvest
# ---------------------------------------------------------------------------


class Observation(BaseModel):
    """Where we already say it. The reference must exist in the corpus."""

    surface: str
    url_or_ad_id: str
    first_seen: str | None = None


class CandidateDraft(BaseModel):
    claim_text: str = Field(min_length=1)
    surface_forms: list[str] = Field(default_factory=list)
    claim_type: ClaimType
    observed_on: list[Observation] = Field(default_factory=list)
    market_scope: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)


class ClaimHarvestDraft(BaseModel):
    candidates: list[CandidateDraft] = Field(default_factory=list)
    detector_recall_note: str = ""


class Candidate(CandidateDraft):
    #: Computed here, never accepted from the model. The linter normalizes the
    #: same way at lint time, and two different normalizations would silently
    #: stop the licence pass matching anything.
    normalized_text: str


class ClaimHarvestOutput(BaseModel):
    """3.2.1 — the claims we already make, everywhere, including forgotten copy."""

    candidates: list[Candidate]
    detector_recall_note: str
    corpus_size: int


class ClaimHarvestNode:
    """3.2.1 — harvest claim-shaped language from copy we have already published."""

    spec = NodeSpec(
        id="3.2.1",
        name="claim_harvest",
        stage="3.2",
        run_stage=RunStage.GUIDELINE,
        depends_on=(),
        task_class=TaskClass.EXTRACT,
        input_model=BaseModel,
        output_model=ClaimHarvestOutput,
        connectors=("google_ads", "web_crawler", "brand_book"),
        optional_inputs=("differentiation_claim", "competitor_creative"),
    )

    def needs(self, domain: str | None) -> tuple[gather.Need, ...]:
        """All optional. A project with only a website still has claims to harvest."""
        return (
            gather.Need("creative_history", connector="google_ads", optional=True),
            gather.Need(
                PAGE,
                connector="web_crawler",
                params={"domain": domain, "kinds": [PAGE]},
                optional=True,
            ),
            gather.Need("brand_book_span", connector="brand_book", optional=True),
        )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        found = await gather.collect(ctx, *self.needs(ctx.project.domain))
        return found.evidence

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        corpus = [row for row in ev if row.kind in CLAIM_KINDS]
        refs = _corpus_refs(corpus)

        draft = await ctx.complete(
            ClaimHarvestDraft,
            system=prompts.system_prompt(
                "You find the factual claims a company already makes in its own "
                "published copy. You copy claims out of the text you are shown — you "
                "never write a new claim, never improve one, and never include a claim "
                "you cannot point at. For each one, name the exact page or ad it "
                "appears on, using only the references you are given."
            ),
            user=prompts.compose(
                prompts.project_block(ctx.project),
                prompts.computed_block(
                    "copy this company has already published", _corpus_block(corpus)
                ),
                prompts.computed_block(
                    "the only references you may cite in observed_on", sorted(refs)
                ),
                _binding_block(ctx),
            ),
        )

        candidates = [
            Candidate(
                **entry.model_dump(),
                normalized_text=normalize(
                    entry.claim_text, locale=(entry.languages or ["en"])[0]
                ).text,
            )
            for entry in _validated(draft.candidates, refs=refs)
        ]
        return ClaimHarvestOutput(
            candidates=candidates,
            detector_recall_note=draft.detector_recall_note,
            corpus_size=len(corpus),
        )


def _validated(candidates: list[CandidateDraft], *, refs: set[str]) -> list[CandidateDraft]:
    """Every candidate must be observable in what we actually read."""
    for entry in candidates:
        if not entry.observed_on:
            raise NodeContractError(
                f"3.2.1 returned the claim {entry.claim_text!r} without saying where it is "
                "observed. This node harvests what we already say; a claim with no "
                "observation is a claim the model wrote."
            )
        unknown = [item.url_or_ad_id for item in entry.observed_on if item.url_or_ad_id not in refs]
        if unknown:
            raise NodeContractError(
                f"3.2.1 said {entry.claim_text!r} appears at "
                f"{', '.join(sorted(unknown))}, which is not in the corpus it was shown. "
                "Only references drawn from the gathered evidence are admissible."
            )
    return candidates


# ---------------------------------------------------------------------------
# 3.2.2 claim_substantiation
# ---------------------------------------------------------------------------


class ClaimVerdictDraft(BaseModel):
    claim_index: int
    #: Deliberately `str` rather than a narrow Literal. The point is to catch a
    #: model reaching for `approved` and say *why* it is refused, which a
    #: schema-validation error could not do.
    status: str
    risk_tier: ClaimRiskTier = ClaimRiskTier.MEDIUM
    expiry_basis: Literal["qualitative", "quantified"] = "qualitative"
    substantiation: dict[str, Any] = Field(default_factory=dict)
    evidence_ids: list[uuid.UUID] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)


class ClaimSubstantiationDraft(BaseModel):
    claims: list[ClaimVerdictDraft] = Field(default_factory=list)


class ClaimVerdict(BaseModel):
    claim_index: int
    claim_text: str
    normalized_text: str
    claim_type: ClaimType
    status: ClaimStatus
    risk_tier: ClaimRiskTier
    substantiation: dict[str, Any]
    evidence_ids: list[uuid.UUID]
    gaps: list[str]
    #: Days, computed from constants. The signer may still override the date.
    proposed_expiry_days: int


class ClaimSubstantiationOutput(BaseModel):
    """3.2.2 — what stands behind each claim, and what does not."""

    claims: list[ClaimVerdict]
    unsupported_count: int
    expiry_basis: str


class ClaimSubstantiationNode:
    """3.2.2 — bind each harvested claim to its evidence, or mark it unsupported."""

    spec = NodeSpec(
        id="3.2.2",
        name="claim_substantiation",
        stage="3.2",
        run_stage=RunStage.GUIDELINE,
        depends_on=("3.2.1",),
        task_class=TaskClass.CLASSIFY,
        input_model=BaseModel,
        output_model=ClaimSubstantiationOutput,
        connectors=(),
        optional_inputs=("compliance_guardrails",),
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        """Nothing new to pull: 3.2.1 already read the corpus."""
        return []

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        harvested = (ctx.outputs.get("3.2.1") or {}).get("candidates") or []
        if not harvested:
            return ClaimSubstantiationOutput(claims=[], unsupported_count=0, expiry_basis="none")

        known = {row.id for row in ev}
        known |= {
            uuid.UUID(str(item))
            for item in (ctx.scratch.get("evidence_ids") or [])
            if _is_uuid(item)
        }

        draft = await ctx.complete(
            ClaimSubstantiationDraft,
            system=prompts.system_prompt(
                "You decide what evidence stands behind a claim. You cite only evidence "
                "you were given, by id. If there is none, you say the claim is "
                "unsupported and you leave it unsupported — you never describe evidence "
                "that was not shown to you, and you never approve anything: approval is "
                "a person's signature, not your opinion."
            ),
            user=prompts.compose(
                prompts.project_block(ctx.project),
                prompts.computed_block("the claims to substantiate, in order", harvested),
                prompts.computed_block(
                    "the only evidence you may cite, by id",
                    [
                        {
                            "evidence_id": str(row.id),
                            "kind": row.kind,
                            "text": redact_pii(row.content_text or ""),
                        }
                        for row in ev
                    ],
                ),
            ),
        )

        verdicts = [
            self._verdict(entry, harvested=harvested, known=known) for entry in draft.claims
        ]
        return ClaimSubstantiationOutput(
            claims=verdicts,
            unsupported_count=sum(1 for v in verdicts if v.status is ClaimStatus.UNSUPPORTED),
            expiry_basis=(
                ", ".join(sorted({e.expiry_basis for e in draft.claims}))
                if draft.claims
                else "none"
            ),
        )

    def _verdict(
        self,
        entry: ClaimVerdictDraft,
        *,
        harvested: list[dict[str, Any]],
        known: set[uuid.UUID],
    ) -> ClaimVerdict:
        if entry.status not in DRAFTABLE:
            raise NodeContractError(
                f"3.2.2 returned status {entry.status!r} for claim {entry.claim_index}. "
                "This node may only return 'unsupported' or 'pending_signoff': a claim "
                "becomes approved when the named legal owner signs for it, and nothing "
                "a model returns can stand in for that signature (law 24)."
            )

        fabricated = [str(item) for item in entry.evidence_ids if item not in known]
        if fabricated:
            raise NodeContractError(
                f"3.2.2 cited evidence that does not resolve for claim {entry.claim_index}: "
                f"{', '.join(sorted(fabricated))}. Substantiation that names a row nobody "
                "can open is worse than none, because it reads as evidence."
            )

        try:
            source = harvested[entry.claim_index]
        except IndexError as exc:
            raise NodeContractError(
                f"3.2.2 returned a verdict for claim {entry.claim_index}, but 3.2.1 "
                f"harvested only {len(harvested)}."
            ) from exc

        # A claim with nothing behind it stays unsupported whatever the model
        # said. The model never manufactures substantiation, and it does not get
        # to promote a claim by asserting a status either.
        status = ClaimStatus.PENDING_SIGNOFF if entry.evidence_ids else ClaimStatus.UNSUPPORTED
        return ClaimVerdict(
            claim_index=entry.claim_index,
            claim_text=str(source.get("claim_text", "")),
            normalized_text=str(source.get("normalized_text", "")),
            claim_type=ClaimType(source.get("claim_type", ClaimType.SUPERLATIVE.value)),
            status=status,
            risk_tier=entry.risk_tier,
            substantiation=entry.substantiation,
            evidence_ids=list(entry.evidence_ids),
            gaps=list(entry.gaps),
            proposed_expiry_days=_expiry_days(entry.expiry_basis),
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _expiry_days(basis: str) -> int:
    """A lookup, not a judgement. Quantified claims go stale faster."""
    constants = load_content_constants()
    key = "claims.quantified_expiry_days" if basis == "quantified" else "claims.default_expiry_days"
    return int(constants.value(key))


def _corpus_refs(corpus: list[Evidence]) -> set[str]:
    """Every reference a candidate is allowed to cite."""
    refs: set[str] = set()
    for row in corpus:
        payload = row.payload or {}
        for key in ("ad_id", "url", "asset_id", "resource_name", "page"):
            value = payload.get(key)
            if value:
                refs.add(str(value))
    return refs


def _corpus_block(corpus: list[Evidence]) -> list[dict[str, Any]]:
    """The corpus as the model sees it — redacted, and carrying its reference.

    `redact_pii` is a deterministic pass, not an instruction in the prompt:
    historic ad copy carries real names and addresses, and law 30 is not
    satisfied by asking a model to look away.
    """
    rows: list[dict[str, Any]] = []
    for row in corpus:
        payload = row.payload or {}
        rows.append(
            {
                "ref": str(
                    payload.get("ad_id") or payload.get("url") or payload.get("asset_id") or ""
                ),
                "kind": row.kind,
                "performance_label": payload.get("performance_label"),
                "text": redact_pii(row.content_text or ""),
            }
        )
    return rows


def _binding_block(ctx: RunContext) -> str:
    """Stage 01's differentiation claim and competitor creative, when bound.

    Absent on a standalone run, and absent is a legal state: the harvest simply
    has less to compare against, which narrows nothing it reports.
    """
    differentiation = ctx.outputs.get("1.3.4") or {}
    competitors = ctx.outputs.get("1.3.2") or {}
    if not differentiation and not competitors:
        return ""
    return prompts.computed_block(
        "optional context from research (absent on a standalone run)",
        {"differentiation": differentiation, "competitor_creative": competitors},
    )


def _is_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return False
    return True


#: Module-level instances. The registry discovers instances, not classes.
claim_harvest = ClaimHarvestNode()
claim_substantiation = ClaimSubstantiationNode()
