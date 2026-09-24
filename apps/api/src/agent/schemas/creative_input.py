"""`CreativeInput` and `CreativeContext` — what crosses into Stage 04 (PRD §4.3).

Two contracts, one per upstream boundary Stage 04 has that Stage 02 did not:

* **`CreativeContext`** is Stage 03's one read-only projection of a published
  guideline — voice, lexicon guidance, visual identity, disclosure,
  competitor and personalization rules — at exactly one pinned
  `ruleset_version`. It resolves the Law 27 gap: Stage 04 may not read
  `ContentGuideline.payload`, but the writer-facing guidance lives only there.
  It carries guidance, never verdicts: enforcement stays in the `RuleSet`.
* **`CreativeInput`** is everything one creative run is allowed to know,
  assembled once at start by `orchestrator/creative_input.py`, hashed into
  `Run.input_hash`, and passed read-only to every node (§4.3 rule 1).

Both follow `PlanInput`'s three rules — frozen, `extra="forbid"`, and carrying
their own hash — and its fourth, the one that matters most: **upstream
sections are imported, never re-declared.** A second spelling of
`AccountStructure` would be two shapes for the one container every ad is
written into. Where §4.3 names a type the upstream contract does not have
(`AudienceSlice`, `DifferentiationClaim`, the `*Ref`s) it is declared here as
the smallest wrapper over upstream types that says what the PRD comment says.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent.export.contract import (
    IcpExclusion,
    IcpSegment,
    KeywordPageMapping,
    MessageCluster,
    PageAudit,
    Whitespace,
)
from agent.export.guideline_contract import LexiconEntry, VisualIdentity, VoiceProfile
from agent.export.plan_contract import (
    AccountStructure,
    ChannelSlate,
    Dependency,
    NamingConvention,
    Objectives,
    QualifiedLead,
)
from agent.schemas.guardrails import DisclosureRule, OfferRecord

#: This contract's own version. Bumped when a creative node could read a
#: `CreativeInput` it does not understand.
CREATIVE_INPUT_SCHEMA_VERSION: Final[Literal["1.0"]] = "1.0"
#: `CreativeContext`'s version, independent of the guideline's.
CREATIVE_CONTEXT_SCHEMA_VERSION: Final[Literal["1.0"]] = "1.0"


def canonical_hash(payload: dict[str, Any]) -> str:
    """sha256 over canonical JSON: sorted keys, no whitespace, UTF-8.

    `sort_keys` is the part that makes it canonical. The projection copies
    free-form dicts out of a JSONB payload, and the order a dict's keys come
    back in is an implementation detail of whoever built it; two processes
    must hash the same context identically regardless.
    """
    rendered = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _Carried(BaseModel):
    """A Stage 03 section whose source is a free-form dict.

    `policy_profile.competitor_mentions` and `.personalization` are
    `dict[str, Any]` on the guideline contract. Declaring fields here that the
    source does not declare would be inventing a schema for them, so the keys
    ride along verbatim (`extra="allow"`) — typed as *the* competitor rules,
    open about their interior.
    """

    model_config = ConfigDict(frozen=True, extra="allow")


# ---------------------------------------------------------------------------
# CreativeContext — the Stage 03 projection
# ---------------------------------------------------------------------------


class LexiconGuidance(_Frozen):
    """3.1.2 always/never, as writer guidance. The `RuleSet` enforces them."""

    always: list[LexiconEntry] = Field(default_factory=list)
    never: list[LexiconEntry] = Field(default_factory=list)
    case_and_spelling: list[dict[str, Any]] = Field(default_factory=list)


class CompetitorRules(_Carried):
    """3.3.3 `competitor_mentions`, verbatim."""


class PersonalizationRules(_Carried):
    """3.3.x `personalization`, verbatim."""


class CreativeContext(_Frozen):
    """Stage 03 delta — a projection, not a new source of truth (§4.3)."""

    schema_version: Literal["1.0"] = CREATIVE_CONTEXT_SCHEMA_VERSION
    guideline_id: uuid.UUID
    guideline_version: str
    ruleset_version: str
    voice: VoiceProfile
    lexicon_guidance: LexiconGuidance
    visual_identity: VisualIdentity
    disclosure_rules: list[DisclosureRule] = Field(default_factory=list)
    competitor_rules: CompetitorRules
    personalization_rules: PersonalizationRules
    #: `canonical_hash` over every field above. Computed by the projection;
    #: `hash_of` is the one definition both ends use.
    hash: str

    @staticmethod
    def hash_of(fields: dict[str, Any]) -> str:
        """The hash of a context's JSON-mode fields, excluding `hash` itself."""
        return canonical_hash({key: value for key, value in fields.items() if key != "hash"})

    def computed_hash(self) -> str:
        return self.hash_of(self.model_dump(mode="json", by_alias=True))


# ---------------------------------------------------------------------------
# the run's choices
# ---------------------------------------------------------------------------


class MediaModelChoice(_Frozen):
    """One user-selected media model, with the catalogue record it was chosen from."""

    modality: Literal["image", "video"]
    #: From the live catalogue, and on the admin allowlist.
    model_id: str = Field(min_length=1)
    #: Pins a provider. None = OpenRouter routes within the model.
    provider_tag: str | None = None
    #: The catalogue record at selection time (supported_parameters |
    #: supported_durations/resolutions/aspect_ratios, pricing). Law 36: every
    #: request is validated against *this*, not whatever the catalogue says
    #: mid-run.
    capability: dict[str, Any]
    capability_hash: str = Field(min_length=1)
    #: resolution, quality, duration, generate_audio… validated vs capability.
    defaults: dict[str, Any] = Field(default_factory=dict)


class CreativeScope(_Frozen):
    #: ⊆ plan.account_structure.campaigns[].campaign_ref. Empty = all of them;
    #: the builder expands it, so a stored input always names what it covered.
    campaign_refs: list[str] = Field(default_factory=list)
    images: bool
    video: bool
    concepts_per_campaign: Literal[2, 3]


# ---------------------------------------------------------------------------
# the pins
# ---------------------------------------------------------------------------


class PlanRef(_Frozen):
    plan_id: uuid.UUID
    version: int
    schema_version: str
    plan_run_id: uuid.UUID


class ResearchRef(_Frozen):
    """Reached through `plan.source` — never a third precondition (§4.1)."""

    research_run_id: uuid.UUID
    report_id: uuid.UUID
    acceptance_id: uuid.UUID


class RuleSetRef(_Frozen):
    guideline_id: uuid.UUID
    ruleset_version: str
    hash: str


class CreativeContextRef(_Frozen):
    hash: str


class MediaReferenceRef(_Frozen):
    """A non-retired `MediaReference` row, with its rights attestation (§10.3).

    Never the bytes and never the storage path: nothing in the input is a
    thing a provider could be handed (Law 44).
    """

    reference_id: uuid.UUID
    kind: Literal["product_reference", "style_reference"]
    origin: Literal["own", "licensed", "third_party"]
    sha256: str
    media_type: str
    width: int
    height: int
    product_ref: str | None = None
    rights_statement: str
    attested_by: uuid.UUID
    attested_at: datetime


class SignOffMatrixRef(_Frozen):
    """The current matrix: who G7, G8 and H3 route to."""

    matrix_id: uuid.UUID
    version: int
    brand_owner_id: uuid.UUID
    legal_owner_id: uuid.UUID
    performance_owner_id: uuid.UUID


# ---------------------------------------------------------------------------
# Stage 01 slices without a single upstream type
# ---------------------------------------------------------------------------


class AudienceSlice(_Frozen):
    """1.1.2 best customers and 1.1.3 who we do not want."""

    best_customers: list[IcpSegment] = Field(default_factory=list)
    not_wanted: list[IcpExclusion] = Field(default_factory=list)


class DifferentiationClaim(_Frozen):
    """1.3.4 — the recommended claim and the whitespace it came from."""

    recommended_claim: str | None = None
    whitespace: list[Whitespace] = Field(default_factory=list)
    substantiation_required: list[str] = Field(default_factory=list)


#: 1.4.5 and 1.5.1 already have exactly the shapes §4.3 names; the aliases are
#: the PRD's words for them, not new types.
PageMapping = KeywordPageMapping
LandingAuditRef = PageAudit


# ---------------------------------------------------------------------------
# CreativeInput
# ---------------------------------------------------------------------------


class CreativeInput(_Frozen):
    """Everything Stage 04 is allowed to know about Stages 01–03 (§4.3)."""

    schema_version: Literal["1.0"] = CREATIVE_INPUT_SCHEMA_VERSION
    project_id: uuid.UUID
    creative_run_id: uuid.UUID
    # -- pins — all required, all hashed -----------------------------------
    plan_ref: PlanRef
    research_ref: ResearchRef
    ruleset_ref: RuleSetRef
    context_ref: CreativeContextRef
    scope: CreativeScope
    #: Empty when `scope.images` and `scope.video` are both false.
    media_models: list[MediaModelChoice] = Field(default_factory=list)

    # -- Stage 01 (accepted report) ------------------------------------------
    audience: AudienceSlice
    differentiation: DifferentiationClaim
    #: 1.3.2/1.3.3 — angles to avoid echoing.
    competitor_messages: list[MessageCluster] = Field(default_factory=list)
    keyword_page_map: list[PageMapping] = Field(default_factory=list)
    landing_audit: list[LandingAuditRef] = Field(default_factory=list)
    # -- Stage 02 (frozen plan) ----------------------------------------------
    objectives: Objectives
    #: `None` when the frozen plan carries none. §4.3 types it as required;
    #: the plan contract does not guarantee it, and an empty `QualifiedLead()`
    #: in its place would read as "sales agreed no signals".
    lead_definition: QualifiedLead | None
    channel_slate: ChannelSlate
    #: The containers.
    account_structure: AccountStructure
    #: `None` for the same reason as `lead_definition`.
    naming: NamingConvention | None
    # -- Stage 03 (published) ------------------------------------------------
    creative_context: CreativeContext
    offer_records: list[OfferRecord] = Field(default_factory=list)
    offer_snapshot_at: datetime
    references: list[MediaReferenceRef] = Field(default_factory=list)
    signoff_matrix: SignOffMatrixRef
    #: plan open_dependencies + guideline open H2.
    inherited_dependencies: list[Dependency] = Field(default_factory=list)
    constants_version: str

    def content_hash(self) -> str:
        """sha256 over the canonical JSON. What `Run.input_hash` stores."""
        return canonical_hash(self.model_dump(mode="json", by_alias=True))
