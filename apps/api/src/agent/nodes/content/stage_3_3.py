"""Stage 3.3 — Google's rules for our industry. Nodes 3.3.1–3.3.4 and H2.

3.3.1 is the root of the stage and everything else hangs off it. It reads the
policy snapshots `policy/watcher.py` stores and decides which policy areas
apply to *this* advertiser — and, just as importantly, which do not.

**The `why_not` list matters as much as the `why`.** §11 says so outright, and
it is the one thing about this node that is easy to get wrong: a map that lists
only what applies is indistinguishable from a map that was never built. The
record that we checked alcohol and it does not apply is what makes the rulebook
auditable a year later, and `not_applicable[]` is asserted non-empty in tests
for exactly that reason.

3.3.2 is the stage's person-task. It is the only node in Stage 03 that can
legitimately *decline to halt*: when 3.3.1 finds nothing requiring verification
there is no task to open, and opening one anyway would put "verify nothing" in
front of a company officer. §11: "Emits `status='not_required'` and creates no
task when 3.3.1 finds nothing."

3.3.3 and 3.3.4 are ordinary drafting nodes over the same snapshots. The one
rule worth stating twice lives in 3.3.3: personalization rules are generated as
*prohibitions*. The system records which attributes an ad may not imply
knowledge of, and never a viewer attribute (§13).
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

import sqlalchemy as sa
from pydantic import BaseModel, Field

from agent.db.models import Evidence, RunStage, SignOffMatrix
from agent.llm.router import TaskClass
from agent.nodes import prompts
from agent.nodes.base import NodeContractError, NodeSpec, RunContext
from agent.policy import watcher

#: The areas `policy_sources.yaml` can carry. A snapshot whose area is not one
#: of these still reaches the model — the list orders the prompt, it does not
#: filter it — but an unknown area is logged rather than silently grouped.
KNOWN_AREAS = (
    "editorial",
    "restricted_content",
    "trademark",
    "personalization",
    "verification",
    "disclosure",
)


# ---------------------------------------------------------------------------
# 3.3.1 policy_surface_map
# ---------------------------------------------------------------------------


class ApplicableArea(BaseModel):
    area: str = Field(min_length=1)
    policy_ref: str = Field(min_length=1)
    why_applicable: str = Field(min_length=1)
    markets: list[str] = Field(default_factory=list)
    obligations: list[str] = Field(default_factory=list)
    evidence_ids: list[uuid.UUID] = Field(default_factory=list)


class NotApplicableArea(BaseModel):
    """The record that we looked. §11 weights this equally with `applicable`."""

    area: str = Field(min_length=1)
    why_not: str = Field(min_length=1)


class RequiresVerification(BaseModel):
    kind: str = Field(min_length=1)
    markets: list[str] = Field(default_factory=list)
    blocking_for: Literal["launch", "publish"] = "launch"
    policy_ref: str | None = None


class OpenInterpretation(BaseModel):
    """Something we could not resolve. Written down instead of guessed."""

    area: str = Field(min_length=1)
    question: str = Field(min_length=1)
    why_unresolved: str = Field(min_length=1)


class PolicySurfaceDraft(BaseModel):
    applicable: list[ApplicableArea] = Field(default_factory=list)
    not_applicable: list[NotApplicableArea] = Field(default_factory=list)
    requires_verification: list[RequiresVerification] = Field(default_factory=list)
    open_interpretation: list[OpenInterpretation] = Field(default_factory=list)


class PolicySurfaceMapOutput(PolicySurfaceDraft):
    """3.3.1 — which of Google's policy areas bear on this advertiser."""

    #: Areas whose page could not be read at all. Distinct from
    #: `not_applicable`: "we checked and it does not apply" and "we could not
    #: check" must never render as the same thing.
    unreadable_areas: list[str] = Field(default_factory=list)
    input_mode: Literal["bound", "unbound"] = "unbound"


class PolicySurfaceMapNode:
    """3.3.1 — the root of stage 3.3."""

    spec = NodeSpec(
        id="3.3.1",
        name="policy_surface_map",
        stage="3.3",
        run_stage=RunStage.GUIDELINE,
        depends_on=(),
        task_class=TaskClass.CLASSIFY,
        input_model=BaseModel,
        output_model=PolicySurfaceMapOutput,
        connectors=("web_crawler",),
        optional_inputs=("markets", "product_context", "compliance_guardrails"),
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        return await watcher.ensure_snapshots(
            ctx.db, project_id=ctx.project.id, workspace_id=ctx.run.workspace_id
        )

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        snapshots = [row for row in ev if row.kind == watcher.POLICY_SNAPSHOT]
        if not snapshots:
            raise NodeContractError(
                "3.3.1 could not read a single policy page, so there is no basis for "
                "saying which of Google's policies apply. A policy map built from no "
                "policy text would be invention, and law 21 permits a narrower scope "
                "but never an invented one."
            )

        bound = _bound_inputs(ctx)
        seen_areas = {str((row.payload or {}).get("area") or "unknown") for row in snapshots}
        missing = [area for area in KNOWN_AREAS if area not in seen_areas]

        draft = await ctx.complete(
            PolicySurfaceDraft,
            system=prompts.system_prompt(
                "You decide which Google advertising policy areas apply to one "
                "advertiser, reading only the policy text you are shown. For every area "
                "you are given you return either an `applicable` entry or a "
                "`not_applicable` entry with a reason — an area you say nothing about "
                "is an area nobody can prove was checked. You never state an obligation "
                "that is not in the text in front of you. When the text does not settle "
                "a question, you put it in `open_interpretation` rather than deciding it."
            ),
            user=prompts.compose(
                prompts.project_block(ctx.project),
                prompts.computed_block("what we know about this advertiser", bound),
                prompts.computed_block(
                    "the policy areas you must each rule on",
                    [area for area in KNOWN_AREAS if area in seen_areas],
                ),
                _policy_block(snapshots),
            ),
        )

        evidence_ids = {row.id for row in snapshots}
        _assert_cited(draft, evidence_ids)
        _assert_ruled_on(draft, seen_areas)
        return PolicySurfaceMapOutput(
            **draft.model_dump(),
            unreadable_areas=missing,
            input_mode="bound" if bound else "unbound",
        )


def _bound_inputs(ctx: RunContext) -> dict[str, Any]:
    """Whatever of the optional bindings actually resolved (law 21).

    Absent bindings widen the map rather than blocking it: with no market list
    the model rules on every area globally, and `input_mode='unbound'` records
    that the scope was not narrowed by anything.
    """
    source = getattr(ctx, "guideline", None) or ctx.scratch.get("guideline_input")
    bound: dict[str, Any] = {}
    for field in ("markets", "product_context", "compliance_guardrails"):
        value = getattr(source, field, None) if source is not None else None
        if value:
            bound[field] = value
    return bound


def _assert_cited(draft: PolicySurfaceDraft, evidence_ids: set[uuid.UUID]) -> None:
    """Every applicable area names a snapshot it was read from.

    The executor already enforces the subset relation on `evidence_ids`. This
    adds the part it cannot know: an `applicable` entry with an *empty* list is
    a policy obligation with no source, which is the one shape law 1 exists to
    stop.
    """
    for entry in draft.applicable:
        if not entry.evidence_ids:
            raise NodeContractError(
                f"3.3.1 said the {entry.area!r} policy area applies but cited no policy "
                "text for it. An obligation nobody can trace back to a page is an "
                "obligation the model wrote."
            )
        stray = set(entry.evidence_ids) - evidence_ids
        if stray:
            raise NodeContractError(
                f"3.3.1 cited evidence it did not gather for {entry.area!r}: "
                f"{sorted(str(item) for item in stray)}"
            )


def _assert_ruled_on(draft: PolicySurfaceDraft, seen_areas: set[str]) -> None:
    """Every area we hold policy text for gets a verdict, either way.

    §11: "the `why_not` list matters as much as the `why`". Silence about an
    area reads identically to never having checked it, so silence is a node
    failure rather than an empty section in the rulebook.
    """
    ruled = {entry.area for entry in draft.applicable} | {
        entry.area for entry in draft.not_applicable
    }
    unruled = sorted(area for area in seen_areas if area in KNOWN_AREAS and area not in ruled)
    if unruled:
        raise NodeContractError(
            f"3.3.1 read policy text for {unruled} and said nothing about "
            "them — neither applicable nor not_applicable. An area with no verdict is "
            "indistinguishable from an area nobody checked."
        )


policy_surface_map = PolicySurfaceMapNode()


# ---------------------------------------------------------------------------
# 3.3.2 verification_attestation — H2
# ---------------------------------------------------------------------------


class VerificationAttestationOutput(BaseModel):
    """3.3.2 🔒 H2 — a named officer attests, or nothing is required.

    `status` carries the whole decision. `not_required` is a complete, normal,
    tested outcome — not a degraded one — and when it is set the executor must
    not open a task, which is why `assignee_id` is None in that case rather
    than pointing at somebody with nothing to do.
    """

    status: Literal["not_required", "required"]
    task_key: Literal["H2"] = "H2"
    blocking_for: Literal["launch"] = "launch"
    assignee_id: uuid.UUID | None = None
    title: str | None = None
    instructions: str | None = None
    required_artifacts: dict[str, Any] = Field(default_factory=dict)
    #: What 3.3.1 said needs verifying. Empty exactly when `not_required`.
    verification_kinds: list[str] = Field(default_factory=list)
    markets: list[str] = Field(default_factory=list)
    why: str = Field(min_length=1)


class VerificationAttestationNode:
    """3.3.2 🔒 H2 — blocks launch, not publish.

    Like 3.2.3 this node makes **no model call**. Whether verification is
    required was already decided by 3.3.1 reading Google's own page; asking a
    model to re-decide it would be asking it to overrule evidence.
    """

    spec = NodeSpec(
        id="3.3.2",
        name="verification_attestation",
        stage="3.3",
        run_stage=RunStage.GUIDELINE,
        depends_on=("3.3.1", "3.5.1"),
        task_class=TaskClass.CLASSIFY,
        input_model=BaseModel,
        output_model=VerificationAttestationOutput,
        connectors=(),
        human_task_key="H2",
        human_task_conditional=True,
    )

    def task_required(self, ctx: RunContext, output: BaseModel) -> bool:
        """H2 opens only when Google actually requires verification (§11).

        Reads the node's own validated output rather than re-querying 3.3.1,
        for the reason `gate_required` does: the answer must agree with the row
        about to be checkpointed.
        """
        return getattr(output, "status", "required") == "required"

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        return []

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        surface = ctx.outputs.get("3.3.1") or {}
        required = list(surface.get("requires_verification") or [])

        if not required:
            # The whole point of this branch. No task, no assignee, no halt.
            return VerificationAttestationOutput(
                status="not_required",
                why=(
                    "3.3.1 found nothing in the policy text that requires advertiser "
                    "verification for this advertiser's products and markets."
                ),
            )

        kinds = [str(item.get("kind")) for item in required if item.get("kind")]
        markets = sorted({market for item in required for market in (item.get("markets") or [])})
        assignee = await self._officer(ctx)
        if assignee is None:
            # Q3: nobody is named for this. The run blocks rather than routing
            # a non-delegable act to whoever happens to hold a role — that
            # fallback is what law 23 removes.
            raise NodeContractError(
                "3.3.2 found that Google requires advertiser verification "
                f"({', '.join(kinds)}) but no sign-off matrix names anyone to perform "
                "it. `no_eligible_assignee`: a person-task cannot fall back to a role, "
                "so an admin must name the company officer before this run can continue."
            )

        return VerificationAttestationOutput(
            status="required",
            assignee_id=assignee,
            title=f"Complete advertiser verification ({', '.join(kinds)})",
            instructions=(
                "Google requires this advertiser to be verified before these ads can "
                "run. Complete the verification tasks in the Google Ads account, then "
                "record the reference and attach the documents you submitted. This "
                "blocks launch, not publication — the rulebook can be published while "
                "verification is outstanding, and it will say so."
            ),
            required_artifacts={
                "document_kinds": kinds,
                "submitted_reference": "the reference Google gave you for the submission",
                "submitted_at": "when you submitted it",
                "expires_at": "when the verification lapses, if it does",
            },
            verification_kinds=kinds,
            markets=markets,
            why="; ".join(
                f"{item.get('kind')}: {item.get('policy_ref') or 'policy text'}"
                for item in required
            ),
        )

    async def _officer(self, ctx: RunContext) -> uuid.UUID | None:
        """The named company officer.

        The legal owner, because the matrix has no fourth role and Q3 leaves
        the officer unassigned by design. Naming the legal owner is a proposal
        the assignee can hand over (`HumanTaskHandover`), not an assumption
        baked into a role lookup.
        """
        matrix = (
            (
                await ctx.db.execute(
                    sa.select(SignOffMatrix)
                    .where(
                        SignOffMatrix.project_id == ctx.project.id,
                        SignOffMatrix.superseded_at.is_(None),
                    )
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        return matrix.legal_owner_id if matrix is not None else None


verification_attestation = VerificationAttestationNode()


# ---------------------------------------------------------------------------
# 3.3.3 competitive_and_personalization_rules
# ---------------------------------------------------------------------------


class CompetitorMentions(BaseModel):
    policy_ref: str = Field(min_length=1)
    permitted: list[str] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)
    trademark_notes: list[str] = Field(default_factory=list)
    per_market_variance: list[str] = Field(default_factory=list)


class PersonalizationRules(BaseModel):
    """Prohibitions only.

    §13: the system never records a viewer attribute; it records which
    attributes an ad may not imply knowledge of. Every field here is therefore
    a "may not", and there is deliberately no field that could hold one.
    """

    forbidden_implications: list[str] = Field(default_factory=list)
    sensitive_inference_categories: list[str] = Field(default_factory=list)
    remarketing_copy_rules: list[str] = Field(default_factory=list)


class CompetitiveAndPersonalizationOutput(BaseModel):
    """3.3.3 — "may we name them", and "may an ad imply we know something"."""

    competitor_mentions: CompetitorMentions
    personalization: PersonalizationRules
    evidence_ids: list[uuid.UUID] = Field(default_factory=list)


class CompetitiveAndPersonalizationNode:
    """3.3.3 — competitor references and personalized advertising."""

    spec = NodeSpec(
        id="3.3.3",
        name="competitive_and_personalization_rules",
        stage="3.3",
        run_stage=RunStage.GUIDELINE,
        depends_on=("3.3.1",),
        task_class=TaskClass.EXTRACT,
        input_model=BaseModel,
        output_model=CompetitiveAndPersonalizationOutput,
        connectors=(),
        optional_inputs=("competitor_creative", "markets"),
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        return await watcher.ensure_snapshots(
            ctx.db, project_id=ctx.project.id, workspace_id=ctx.run.workspace_id
        )

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        relevant = _snapshots_for(ev, "trademark", "personalization", "editorial")
        if not relevant:
            raise NodeContractError(
                "3.3.3 holds no trademark or personalization policy text. Rules about "
                "naming a competitor written without the trademark policy in front of "
                "them would be invention."
            )
        output = await ctx.complete(
            CompetitiveAndPersonalizationOutput,
            system=prompts.system_prompt(
                "You write two sets of rules from Google's policy text: whether and how "
                "this advertiser may name a competitor, and what an ad may not imply it "
                "knows about the person seeing it. Every personalization rule is a "
                "prohibition — you never describe an audience, a viewer attribute or a "
                "targeting strategy, only what copy may not imply. You state only what "
                "the policy text supports."
            ),
            user=prompts.compose(
                prompts.project_block(ctx.project),
                prompts.computed_block(
                    "the policy areas that apply here",
                    (ctx.outputs.get("3.3.1") or {}).get("applicable") or [],
                ),
                _policy_block(relevant),
            ),
        )
        return output


competitive_and_personalization_rules = CompetitiveAndPersonalizationNode()


# ---------------------------------------------------------------------------
# 3.3.4 ai_disclosure_rules
# ---------------------------------------------------------------------------

#: The four triggers §11 names. Closed, because `matchers/disclosure.py`
#: compiles against exactly these and a fifth would compile to nothing.
DISCLOSURE_TRIGGERS = ("synthetic_image", "synthetic_video", "synthetic_voice", "generated_copy")


class DisclosureRuleDraft(BaseModel):
    trigger: Literal["synthetic_image", "synthetic_video", "synthetic_voice", "generated_copy"]
    surfaces: list[str] = Field(default_factory=list)
    markets: list[str] = Field(default_factory=list)
    required_text: str = Field(min_length=1)
    placement: str = Field(min_length=1)
    applied_at: Literal["publish", "upload"] = "publish"
    policy_ref: str = Field(min_length=1)
    evidence_ids: list[uuid.UUID] = Field(default_factory=list)


class AiDisclosureOutput(BaseModel):
    """3.3.4 — compiled into rules the linter applies, not a note to remember.

    `internal_policy_addendum` ships empty and editable (Q12): the only
    disclosure obligations we can evidence are Google's and the regulations
    Google surfaces. A company policy that is stricter has to be *stated*
    somewhere before it can be enforced, and inventing one here would enforce
    a rule nobody wrote.
    """

    disclosure_rules: list[DisclosureRuleDraft] = Field(default_factory=list)
    internal_policy_addendum: str = ""
    #: Surfaces on which generated content is permitted but no rule was
    #: produced. Critique assertion 9 checks disclosure covers every such
    #: surface; naming the gaps here is what lets it fail loudly.
    uncovered_surfaces: list[str] = Field(default_factory=list)


class AiDisclosureNode:
    """3.3.4 — AI-disclosure rules, applied automatically at publish."""

    spec = NodeSpec(
        id="3.3.4",
        name="ai_disclosure_rules",
        stage="3.3",
        run_stage=RunStage.GUIDELINE,
        depends_on=("3.3.1",),
        task_class=TaskClass.EXTRACT,
        input_model=BaseModel,
        output_model=AiDisclosureOutput,
        connectors=(),
        optional_inputs=("markets", "measurement_consent"),
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        return await watcher.ensure_snapshots(
            ctx.db, project_id=ctx.project.id, workspace_id=ctx.run.workspace_id
        )

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        relevant = _snapshots_for(ev, "disclosure")
        if not relevant:
            # Fails closed (law 31). A disclosure rulebook with no disclosure
            # policy text behind it would let generated creative ship
            # unlabelled while looking checked.
            raise NodeContractError(
                "3.3.4 holds no disclosure policy text. Two sources cover this area — "
                "Google's election-ad synthetic content policy and the EU/India/New "
                "York AI labelling page — and neither could be read. A disclosure rule "
                "invented without them would be worse than none, because the rulebook "
                "would look as though the question had been settled."
            )
        draft = await ctx.complete(
            AiDisclosureOutput,
            system=prompts.system_prompt(
                "You turn disclosure policy text into machine-checkable rules. Each rule "
                "names one trigger — synthetic_image, synthetic_video, synthetic_voice or "
                "generated_copy — the surfaces and markets it binds on, the exact text "
                "that must appear, and where. You quote required text from the policy "
                "rather than composing your own wording. Some obligations come from "
                "regulation in specific markets rather than from Google's own policy: "
                "scope those rules to the markets the text names and to no others. You "
                "leave internal_policy_addendum empty — an internal policy you were not "
                "shown is one you must not invent."
            ),
            user=prompts.compose(
                prompts.project_block(ctx.project),
                prompts.computed_block("the four triggers you may use", list(DISCLOSURE_TRIGGERS)),
                prompts.computed_block(
                    "the policy areas that apply here",
                    (ctx.outputs.get("3.3.1") or {}).get("applicable") or [],
                ),
                _policy_block(relevant),
            ),
        )
        # Q12 is a decision, not a suggestion: whatever the model returned for
        # the addendum is discarded rather than trusted. A model-written
        # internal policy would be enforced by the linter as though a person
        # had set it.
        return AiDisclosureOutput(
            disclosure_rules=draft.disclosure_rules,
            internal_policy_addendum="",
            uncovered_surfaces=draft.uncovered_surfaces,
        )


ai_disclosure_rules = AiDisclosureNode()


# ---------------------------------------------------------------------------
# shared
# ---------------------------------------------------------------------------


#: How much of one policy page reaches a prompt. The political-content page is
#: ~43 KB of text on its own, and six of those would spend the $6 guideline cap
#: on a single node. Truncation is *stated* in the block rather than silent —
#: a model that cannot see the end of a policy must be able to say so in
#: `open_interpretation` instead of ruling on text it never read.
MAX_POLICY_CHARS = 12_000


def _policy_block(rows: list[Evidence]) -> str:
    """The policy text, each page carrying the id needed to cite it.

    Not `prompts.evidence_block`: that renders `payload` and takes a `Gathered`.
    A policy snapshot's substance is its `content_text`, and a block that showed
    only the payload would hand the model a label and a byte count to write
    advertising law from.
    """
    rendered = []
    for row in rows:
        payload = row.payload or {}
        text = row.content_text or ""
        clipped = text[:MAX_POLICY_CHARS]
        rendered.append(
            {
                "evidence_id": str(row.id),
                "label": payload.get("label"),
                "area": payload.get("area"),
                "url": row.source_url,
                "text": clipped,
                "truncated": len(text) > MAX_POLICY_CHARS,
                "omitted_chars": max(0, len(text) - MAX_POLICY_CHARS),
            }
        )
    return prompts.computed_block(
        "Google policy text. Cite a page by copying its evidence_id. Where `truncated` "
        "is true you have not been shown the whole policy — say so in open_interpretation "
        "rather than ruling on the part you cannot see",
        rendered,
    )


def _snapshots_for(rows: list[Evidence], *areas: str) -> list[Evidence]:
    """Policy snapshots for the named areas, newest first.

    Falls back to every snapshot when none match: an area whose source was
    disabled in Settings should narrow the evidence, not empty it, and a node
    that raised because somebody turned off one URL would be a node that fails
    for a configuration choice rather than for a missing policy.
    """
    snapshots = [row for row in rows if row.kind == watcher.POLICY_SNAPSHOT]
    wanted = {area for area in areas}
    picked = [row for row in snapshots if str((row.payload or {}).get("area")) in wanted]
    # `fetched_at` is a server default, so it is None on a row `ensure_snapshots`
    # added in this transaction and has not committed — which is most of them on
    # a cold-start run. Sorting on it directly raises `TypeError: '<' not
    # supported between instances of 'NoneType'`, and it would have raised in
    # production on the first guideline run of any new project, not just here.
    # A just-fetched row is the newest thing there is, so None sorts first.
    return sorted(
        picked or snapshots,
        key=lambda row: (row.fetched_at is not None, row.fetched_at),
        reverse=True,
    )
