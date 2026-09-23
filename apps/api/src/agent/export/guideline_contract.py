"""The `ContentGuideline` contract (Stage 03 PRD §12.1) — the human-readable artifact.

The Stage 03 twin of `export/plan_contract.py`, inheriting its two rules
unchanged: the model fills the object and a Jinja2 template renders it, so six
export formats cannot disagree; and a statement without evidence is not a
finding.

Stage 03 adds a third, and it is the one this whole module exists to carry.

**A rule without a resolvable authority is not a rule.** §12.1 invariant 2:
every `Rule.authority` resolves, and a non-`internal` source carries at least
one `evidence_id`. An advertising rulebook is read by people who are about to
be told they may not say something, and "because the system says so" is not an
answer any of them will accept. `guidelines/critique.py` assertion 1 proves it
against the rules of this run rather than against a promise.

**Why `Rule` and not §12.1's `RuleDraft`.** S3-P1 shipped `Rule` in
`schemas/guardrails.py` as the single spelling — the compiler, the linter and
the matcher union all name it — and a second class differing only in that its
matcher is not yet built would be two vocabularies for one thing, with a
conversion nobody would remember to keep total. The draft/compiled distinction
is real and it is carried where it belongs: a `Rule` inside a `ContentGuideline`
is a draft, the same `Rule` inside a `RuleSet` has been through
`compiler.build_program` and is known to be evaluable.

**The interiors are permissive on purpose**, for the reason `plan_contract`
gives at length: §11's node outputs are richer than §12's sketch and will get
richer, and a guideline run that fails at 3.6.1 because 3.1.2 grew a field has
lost an hour of work and a legal signature's worth of human attention to a
validation error that cost nobody anything to allow.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent.export.contract import Claim
from agent.schemas.guardrails import AssetSpecSheet, DisclosureRule, LogoTemplate, Rule

#: Bumped when this contract changes shape. Stage 04 reads it before parsing,
#: and `ContentGuideline.schema_version` records the version a stored guideline
#: was built under. Typed as the literal so the field the contract is versioned
#: by cannot be widened to `str` by a default.
GUIDELINE_SCHEMA_VERSION: Final[Literal["1.0"]] = "1.0"

#: §12.1: the executive summary is capped at 250 words, as in Stages 01 and 02.
EXECUTIVE_SUMMARY_MAX_WORDS = 250

#: §12.4's lifecycle, as the *payload* sees it. `superseded` is deliberately
#: absent, exactly as it is in `plan_contract`: it is a fact about a row's
#: relationship to a newer row, which the payload of an immutable published
#: guideline cannot learn without being rewritten — and migration 0016's
#: trigger forbids rewriting it. `ContentGuideline.status` on the table carries
#: the full set; this carries what was true when the rulebook was written.
GuidelineStatus = Literal["draft", "blocked", "ready_to_publish", "published"]

#: §4.2. Derived from what actually resolved, never taken from a client.
GuidelineMode = Literal["standalone", "research_linked", "plan_linked", "fully_linked"]

#: §11's critique severities. Wider than `Rule.severity` — a rulebook can carry
#: a `note` about itself that no rule could carry about a writer's copy.
IssueSeverity = Literal["blocking", "warning", "note"]


class GuidelineModel(BaseModel):
    """Base for every record in the rulebook. Extras ride along into the JSON."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)


# ---------------------------------------------------------------------------
# 3.1 — brand rules
# ---------------------------------------------------------------------------


class VoiceExample(GuidelineModel):
    """One quoted line of real copy, and why it is here.

    §11 is explicit that 3.1.1's examples are "quoted from real best-performing
    copy, never invented", so `source_ref` is not decoration — it is the only
    thing separating this from a model's idea of how the brand sounds.
    """

    text: str = Field(min_length=1)
    source_ref: str = ""
    why: str = ""
    #: Present on `dont_examples` only.
    rewritten_as: str | None = None


class VoiceProfile(GuidelineModel):
    """3.1.1 `voice_profile`."""

    voice_words: list[str] = Field(default_factory=list)
    definition_per_word: dict[str, str] = Field(default_factory=dict)
    do_examples: list[VoiceExample] = Field(default_factory=list)
    dont_examples: list[VoiceExample] = Field(default_factory=list)
    #: §11 spells this `register`, which is a classmethod on every pydantic
    #: model (`ABCMeta.register`) and shadowing it raises at class-definition
    #: time. `voice_register` is the payload key; `assemble` reads 3.1.1's
    #: `register` and writes it here, which is the only place the two spellings
    #: meet. The alias means a payload using either name still validates.
    voice_register: dict[str, Any] = Field(default_factory=dict, alias="register")
    readability_targets: dict[str, Any] = Field(default_factory=dict)
    input_mode: str = ""
    evidence_ids: list[uuid.UUID] = Field(default_factory=list)


class LexiconEntry(GuidelineModel):
    """One term we always use, or never do. Compiles to a matcher or it is not here."""

    term: str = Field(min_length=1)
    surface_forms: list[str] = Field(default_factory=list)
    severity: str = "warning"
    locale: str | None = None
    context: str = ""
    reason: str = ""
    suggested_replacement: str | None = None


class Lexicon(GuidelineModel):
    """3.1.2 `lexicon_rules`."""

    always: list[LexiconEntry] = Field(default_factory=list)
    never: list[LexiconEntry] = Field(default_factory=list)
    case_and_spelling: list[dict[str, Any]] = Field(default_factory=list)
    #: §11 critique assertion 5 asserts this is empty. Computed by 3.1.2, not
    #: re-derived here: a conflict the authoring node did not see is a conflict
    #: two pieces of code disagree about.
    conflicts: list[dict[str, Any]] = Field(default_factory=list)


class VisualIdentity(GuidelineModel):
    """3.1.3 `visual_identity_rules` ⛳ G5."""

    logo: dict[str, Any] = Field(default_factory=dict)
    colour: dict[str, Any] = Field(default_factory=dict)
    imagery: dict[str, Any] = Field(default_factory=dict)
    extraction_confidence: float | None = None
    evidence_ids: list[uuid.UUID] = Field(default_factory=list)


class BrandRules(GuidelineModel):
    """§12.1 — 3.1.1, 3.1.2, 3.1.3."""

    voice: VoiceProfile = Field(default_factory=VoiceProfile)
    lexicon: Lexicon = Field(default_factory=Lexicon)
    visual_identity: VisualIdentity = Field(default_factory=VisualIdentity)


# ---------------------------------------------------------------------------
# 3.2 — the claims register
# ---------------------------------------------------------------------------


class RegisteredClaim(GuidelineModel):
    """One claim, its substantiation and its licence state.

    `status` is the register's own vocabulary (`ClaimStatus`), not the linter's
    narrowed one — a reader of the rulebook needs to know *why* something is
    unlicensed, and `unsupported` and `rejected` are very different
    conversations. `claims_index.ref_status` is what narrows it for the
    compiler, and it is the only thing that does.
    """

    claim_id: uuid.UUID
    claim_text: str = ""
    normalized_text: str = ""
    surface_forms: list[str] = Field(default_factory=list)
    claim_type: str = ""
    status: str = "unsupported"
    risk_tier: str = ""
    market_scope: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    substantiation: dict[str, Any] = Field(default_factory=dict)
    gaps: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None
    signature_id: uuid.UUID | None = None
    evidence_ids: list[uuid.UUID] = Field(default_factory=list)


class OfferRule(GuidelineModel):
    """3.2.4 `offer_integrity_rules` — one construction and what binds it."""

    id: str = ""
    construction: str = ""
    requirement: str = ""
    data_binding: dict[str, Any] = Field(default_factory=dict)
    severity: str = "blocking"


class ClaimsRegister(GuidelineModel):
    """§12.1 — 3.2.1, 3.2.2, 3.2.3, 3.2.4."""

    claims: list[RegisteredClaim] = Field(default_factory=list)
    unsupported_count: int = 0
    expiry_basis: str = ""
    detector_recall_note: str = ""
    offer_rules: list[OfferRule] = Field(default_factory=list)
    live_violations: list[dict[str, Any]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 3.3 — the policy profile
# ---------------------------------------------------------------------------


class PolicyArea(GuidelineModel):
    """One Google policy area and why it does or does not apply to us."""

    area: str = ""
    policy_ref: str = ""
    why_applicable: str = ""
    why_not: str = ""
    markets: list[str] = Field(default_factory=list)
    obligations: list[str] = Field(default_factory=list)
    evidence_ids: list[uuid.UUID] = Field(default_factory=list)
    #: §11 critique assertion 7: an applicable area produces ≥1 rule, or says
    #: plainly that none is needed and why. The absence of both is the defect.
    no_rule_needed: str | None = None


class PolicyProfile(GuidelineModel):
    """§12.1 — 3.3.1, 3.3.2, 3.3.3, 3.3.4."""

    applicable: list[PolicyArea] = Field(default_factory=list)
    not_applicable: list[PolicyArea] = Field(default_factory=list)
    requires_verification: list[dict[str, Any]] = Field(default_factory=list)
    open_interpretation: list[dict[str, Any]] = Field(default_factory=list)
    attestation: dict[str, Any] = Field(default_factory=dict)
    competitor_mentions: dict[str, Any] = Field(default_factory=dict)
    personalization: dict[str, Any] = Field(default_factory=dict)
    disclosure_rules: list[DisclosureRule] = Field(default_factory=list)
    internal_policy_addendum: str = ""


# ---------------------------------------------------------------------------
# 3.4 — asset specs
# ---------------------------------------------------------------------------


class LaunchMinimum(GuidelineModel):
    """3.4.2 — the shortest honest answer to "what must exist before launch"."""

    campaign_type: str = ""
    required_assets: list[dict[str, Any]] = Field(default_factory=list)
    optional_but_recommended: list[dict[str, Any]] = Field(default_factory=list)
    blocking_for_launch: bool = False


class AssetSpecs(GuidelineModel):
    """§12.1 — 3.4.1, 3.4.2, 3.4.3.

    `sheet` is the §12.2 `AssetSpecSheet`, shared verbatim with the compiled
    `RuleSet`: the specs a designer reads and the specs the linter enforces are
    the same object, or one of them is wrong.
    """

    sheet: AssetSpecSheet = Field(default_factory=AssetSpecSheet)
    scope: Literal["scoped", "unscoped"] = "unscoped"
    launch_minimums: list[LaunchMinimum] = Field(default_factory=list)
    readiness_checklist: list[dict[str, Any]] = Field(default_factory=list)
    image_rules: list[dict[str, Any]] = Field(default_factory=list)
    logo_templates: list[LogoTemplate] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 3.5 — governance
# ---------------------------------------------------------------------------


class Owners(GuidelineModel):
    """3.5.1. Ids, never names — §11 critique assertion 10."""

    brand_owner_id: uuid.UUID | None = None
    legal_owner_id: uuid.UUID | None = None
    performance_owner_id: uuid.UUID | None = None


class ReviewTrigger(GuidelineModel):
    """3.5.2 — copy that must not ship unread, and who reads it.

    `reviewer_role` is a role and never an identity. Routing to a named person
    is `SignOffMatrix`'s job, it is non-delegable, and a trigger that named one
    would be a second and weaker path to the same decision.
    """

    id: str = ""
    pattern_kind: Literal["term", "claim_type", "campaign_type", "market", "asset_type"] = "term"
    pattern: str = ""
    why: str = ""
    reviewer_role: str = ""
    severity: str = "warning"


class LearnedRule(GuidelineModel):
    """3.5.3 — a rejected ad that became a rule. Empty until S3-P9."""

    rule_id: str = ""
    policy_topic: str = ""
    construction: str = ""
    example_disapproval_id: uuid.UUID | None = None
    first_seen: datetime | None = None
    occurrences: int = 0


class Governance(GuidelineModel):
    """§12.1 — 3.5.1, 3.5.2, 3.5.3."""

    owners: Owners = Field(default_factory=Owners)
    owners_rationale: str = ""
    matrix_reused: bool = False
    review_triggers: list[ReviewTrigger] = Field(default_factory=list)
    always_review: list[str] = Field(default_factory=list)
    learned_rules: list[LearnedRule] = Field(default_factory=list)
    unresolved_disapprovals: list[dict[str, Any]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# the decisions and dependencies a publish is checked against
# ---------------------------------------------------------------------------


class GateDecision(GuidelineModel):
    """G5 or G6, as the rulebook records it (§12.1 invariant 1)."""

    gate_key: Literal["G5", "G6"]
    node_id: str = ""
    name: str = ""
    status: Literal["approved", "rejected", "pending", "expired"]
    decided_by: uuid.UUID | None = None
    decided_at: datetime | None = None
    note: str = ""
    edits_applied: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def is_approved(self) -> bool:
        return self.status == "approved"


class SignatureRef(GuidelineModel):
    """H1, as §12.1 names it — signer, set hash, when, until when.

    No `statement` and no `decisions[]`. Both live on the `claim_signature`
    row, which is append-only and is the record of what was signed; copying
    them into a mutable draft payload would create a second, editable account
    of a legal signature.
    """

    signature_id: uuid.UUID
    signer_id: uuid.UUID
    set_hash: str = ""
    signed_at: datetime | None = None
    expires_at: datetime | None = None
    voided_at: datetime | None = None
    claim_count: int = 0


class HumanTaskRef(GuidelineModel):
    """H1 or H2, with the thing it blocks (§12.1)."""

    task_id: uuid.UUID
    task_key: Literal["H1", "H2"]
    status: str = "pending"
    blocking_for: str = ""
    assignee_id: uuid.UUID | None = None
    node_id: str = ""


class Dependency(GuidelineModel):
    """Something outstanding, with an owner and what it blocks (§12.1).

    `owner` defaults to `unassigned` for the reason `plan_contract.Dependency`
    gives: a blocking dependency nobody owns is a blocker nobody will clear.
    """

    task: str = Field(min_length=1)
    owner: str = "unassigned"
    blocking_for: str = ""
    source: str = ""


class CritiqueIssue(GuidelineModel):
    """One finding from 3.6.2, computed or read (§11)."""

    severity: IssueSeverity
    section: str = ""
    finding: str = ""
    fix: str = ""
    #: Which of the ten this is, or `reader` for the model's own findings.
    #: Not in §11's sketch and load-bearing in practice: the re-synthesis
    #: prompt cites it and the tests assert on it.
    check: str = ""


# ---------------------------------------------------------------------------
# the artifact
# ---------------------------------------------------------------------------


class ContentGuideline(GuidelineModel):
    """§12.1. One version of the rulebook, as a document rather than a program.

    The compiled twin is `schemas/guardrails.RuleSet`, and the relationship
    between them is the whole of §12.2: this object is what a person reads and
    signs, that one is what Stage 04 executes. They share `rules`,
    `asset_specs` and `logo_templates` by construction rather than by copy —
    `compiler.compile` reads them straight off this payload — so a rule a
    reader cannot find in the PDF cannot be enforced against their copy.
    """

    schema_version: Literal["1.0"] = GUIDELINE_SCHEMA_VERSION
    project_id: uuid.UUID
    guideline_run_id: uuid.UUID
    #: The `content_guideline` row id. Named `guideline_id` because that is
    #: what `compiler.compile` reads out of the payload and what the `RuleSet`
    #: records; §12.1 omits it, and a ruleset that cannot name the guideline it
    #: was compiled from is an audit trail with a hole in it.
    guideline_id: uuid.UUID
    version_major: int = 1
    version_minor: int = 0
    generated_at: datetime
    mode: GuidelineMode = "standalone"
    bindings: dict[str, Any] = Field(default_factory=dict)
    unbound_inputs: list[str] = Field(default_factory=list)

    executive_summary: str = ""
    status: GuidelineStatus = "draft"

    brand_rules: BrandRules = Field(default_factory=BrandRules)
    claims_register: ClaimsRegister = Field(default_factory=ClaimsRegister)
    policy_profile: PolicyProfile = Field(default_factory=PolicyProfile)
    asset_specs: AssetSpecs = Field(default_factory=AssetSpecs)
    governance: Governance = Field(default_factory=Governance)

    #: Everything above, flattened for compilation. The compiler reads this key
    #: and no other, which is why a section that grows a rule has to flatten it
    #: here or the linter will never apply it.
    rules: list[Rule] = Field(default_factory=list)
    #: Shared verbatim with the `RuleSet` — see `AssetSpecs.sheet`.
    disclosure_requirements: list[DisclosureRule] = Field(default_factory=list)
    logo_templates: list[LogoTemplate] = Field(default_factory=list)

    decisions: list[GateDecision] = Field(default_factory=list)
    signatures: list[SignatureRef] = Field(default_factory=list)
    human_tasks: list[HumanTaskRef] = Field(default_factory=list)
    open_dependencies: list[Dependency] = Field(default_factory=list)
    degraded_sources: list[str] = Field(default_factory=list)
    constants_version: str = ""
    cost_usd: float = 0.0

    #: The prose assertions 3.6.1 wrote, each cited. §12.1 invariant 4 and
    #: Stage 01 law 1, unchanged.
    written_claims: list[Claim] = Field(default_factory=list)
    #: 3.6.2's findings. Carried on the payload so an exported PDF shows its
    #: own review — a document that says `ready_to_publish` while the critique
    #: saying otherwise lives elsewhere is how a blocked rulebook circulates as
    #: an approved one. Same argument as `CampaignPlan.critique_issues`.
    critique_issues: list[CritiqueIssue] = Field(default_factory=list)

    @field_validator("executive_summary")
    @classmethod
    def _summary_length(cls, value: str) -> str:
        """§12.1: ≤ 250 words.

        Checked on the contract rather than in the node so that a payload
        arriving from anywhere — a re-synthesis, a test fixture, a hand-edited
        row — is held to it. A model that writes 400 words has not written a
        slightly-long summary; it has written the document again.
        """
        words = len(value.split())
        if words > EXECUTIVE_SUMMARY_MAX_WORDS:
            raise ValueError(
                f"executive_summary is {words} words; §12.1 caps it at "
                f"{EXECUTIVE_SUMMARY_MAX_WORDS}"
            )
        return value

    @property
    def version(self) -> str:
        """`"2.3"`. The version a reader sees, on every page of every export."""
        return f"{self.version_major}.{self.version_minor}"
