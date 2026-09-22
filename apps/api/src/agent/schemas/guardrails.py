"""The guardrails contracts — what a rule is, and what linting one produces.

Stage 03 PRD §12.2 and §12.3. Stage 02 has `calc/` so that no *number* comes
from a model; Stage 03 has `guardrails/` so that no *verdict* does. These are
the shapes that make the second half of that sentence checkable: a `Rule`
carries its own matcher and its own authority, and a `LintFinding` cannot exist
without naming both. A writer who is blocked can always see who said so.

Three properties are load-bearing, and all three are structural rather than
conventional:

* **Everything is frozen and forbids extras.** A `RuleSet` is hashed and then
  persisted immutably (law 26); a model that could grow a key after compilation
  is a model whose hash stops meaning anything.
* **`Matcher` is a closed discriminated union.** There is no matcher that holds
  a string to be `eval`'d and no matcher that calls a model — adding a kind
  means adding it here, in `guardrails/matchers/`, and in the compiler's
  round-trip test. That closure is what makes law 22 enforceable rather than
  aspirational.
* **Nothing here reads a clock.** `LintResult.evaluated_at` is passed in, not
  taken. The linter is a function of its arguments, so the same targets against
  the same ruleset give the same findings in any process, on any day.

`Rule` is deliberately the *only* spelling of a rule. PRD §12.1 calls the
pre-compilation form `RuleDraft`; the fields are identical, and a second model
with the same shape is the most expensive kind of duplication — two definitions
of one thing, diverging quietly. S3-P6 builds `ContentGuideline.rules` out of
these.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

#: Bumped when a consumer could be handed a `RuleSet` it does not understand.
GUARDRAILS_SCHEMA_VERSION: Final[Literal["1.0"]] = "1.0"

#: PRD §9.2. The category is what a reader groups findings by; the severity is
#: what stops a launch. They are independent — a `lexicon` rule can be advisory
#: and a `claim` rule is always blocking (law 24).
RuleCategory = Literal[
    "voice",
    "lexicon",
    "claim",
    "offer",
    "policy",
    "asset_spec",
    "image",
    "disclosure",
    "governance",
    "learned",
]

#: `blocking` stops the asset. `warning` ships but is counted. `advisory` is a
#: note to a writer. Nothing auto-resolves — law 28 forbids an auto-approve.
Severity = Literal["blocking", "warning", "advisory"]

#: Who says so. Every finding resolves to one of these, and a finding that
#: cannot name its authority is a bug (PRD §9.1 item 5).
AuthoritySource = Literal[
    "brand",
    "google_policy",
    "legal_signature",
    "internal",
    "learned_disapproval",
]

#: PRD §12.2. The surface is where copy lands, and it is the main axis a rule
#: is scoped on — a 30-character limit means nothing without "on a headline".
Surface = Literal[
    "rsa_headline",
    "rsa_description",
    "rsa_path",
    "long_headline",
    "pmax_headline",
    "pmax_description",
    "asset_group_description",
    "display_text",
    "youtube_script",
    "sitelink",
    "callout",
    "structured_snippet",
    "business_name",
    "landing_page_section",
]

#: The claim-shaped-language families of PRD §9.3. A detector does not decide
#: whether a claim is *true* — it decides that a sentence is making one, which
#: is the part that can be done deterministically.
ClaimFamily = Literal[
    "superlative",
    "comparative",
    "quantified",
    "guarantee",
    "certification",
    "endorsement",
]

#: Regex flags, named rather than numeric so a compiled ruleset serialises to
#: something a person can read and a hash can be taken of.
RegexFlag = Literal["i", "m", "s", "x"]


class _Contract(BaseModel):
    """Frozen, closed, and hashable as JSON. Every model in this file.

    `extra="forbid"` is not tidiness: a `RuleSet` is hashed, persisted and then
    made immutable by a database trigger. A key that could be silently carried
    along is a key that changes the hash without changing the rules.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


# ---------------------------------------------------------------------------
# Scope and authority
# ---------------------------------------------------------------------------


class Authority(_Contract):
    """Who said so, and when anybody last checked.

    `reference` is a locator whose meaning follows `source`: a policy URL for
    `google_policy`, a `ClaimSignature` id for `legal_signature`, a
    `content_constants.yaml` key for `internal`, a disapproval id for
    `learned_disapproval`, and an evidence-backed brand-book span for `brand`.
    It is one field rather than five nullable ones because a finding renders it
    as one line: *"Google Ads policy, reviewed 2026-09-22"*.
    """

    source: AuthoritySource
    reference: str = Field(min_length=1)
    reviewed_at: date


class RuleScope(_Contract):
    """Where a rule applies. **An empty list means "everywhere"**, not "nowhere".

    That default is the whole reason Stage 03 can start cold (law 21): a run
    with no plan bound does not know the campaign slate, so its rules are
    emitted unscoped and apply to every surface. Binding a plan *narrows* the
    scope; a missing binding widens it. The inverse — empty meaning "matches
    nothing" — would make an unbound run produce a rulebook that silently
    enforces nothing, which is the failure mode this stage exists to avoid.
    """

    markets: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    campaign_types: tuple[str, ...] = ()
    asset_types: tuple[str, ...] = ()
    surfaces: tuple[Surface, ...] = ()

    def matches(self, target: LintTarget) -> bool:
        """Does this rule apply to this target?

        Each axis is an independent AND: a rule scoped to `markets=["DE"]` and
        `surfaces=["rsa_headline"]` applies only to German headlines. Case is
        folded on the free-text axes because a market arrives as `de` from one
        connector and `DE` from another, and a rule that silently stopped
        applying because of that would be undetectable.
        """
        return (
            _in_scope(self.markets, target.market)
            and _in_scope(self.languages, target.language)
            and _in_scope(self.campaign_types, target.campaign_type)
            and (not self.surfaces or target.surface in self.surfaces)
        )


def _in_scope(allowed: tuple[str, ...], value: str) -> bool:
    return not allowed or value.casefold() in {item.casefold() for item in allowed}


# ---------------------------------------------------------------------------
# Matchers — PRD §12.3
# ---------------------------------------------------------------------------


class RegexMatcher(_Contract):
    """A pattern that, when it matches, is the finding.

    Compiled once by the compiler and never by the linter. The pattern is
    stored as source text so the ruleset stays serialisable and diffable — a
    compiled pattern object has no honest JSON rendering.
    """

    kind: Literal["regex"] = "regex"
    pattern: str = Field(min_length=1)
    flags: tuple[RegexFlag, ...] = ()
    locale: str | None = None


class TermSetMatcher(_Contract):
    """A vocabulary rule: these words are banned, or these words are required.

    `mode` is the one addition to §12.3's sketch, and it is not optional: the
    rule taxonomy in §9.2 gives *both* "banned term `cheap`" and "required term
    `Safety Data Sheet`" as lexicon examples, and without a polarity the second
    cannot be expressed at all. `forbid` finds a hit; `require` finds a miss.

    `match` trades precision for reach. `exact` compares normalized surface
    forms. `stem` folds inflection with Snowball. `lemma` resolves to a
    dictionary head word, which is what catches `cheapest` for a ban on
    `cheap` — Snowball leaves `cheapest` alone. Both libraries are offline,
    pinned, and stamped into `compiler_version`, so a library bump changes the
    ruleset hash rather than silently changing verdicts under a stable one.
    """

    kind: Literal["term_set"] = "term_set"
    terms: tuple[str, ...] = Field(min_length=1)
    match: Literal["exact", "lemma", "stem"] = "exact"
    mode: Literal["forbid", "require"] = "forbid"
    locale: str = "en"


class LengthMatcher(_Contract):
    """A size bound on one target's text. The asset-spec workhorse."""

    kind: Literal["length"] = "length"
    min: int | None = Field(default=None, ge=0)
    max: int | None = Field(default=None, ge=0)
    unit: Literal["chars", "words", "graphemes"] = "chars"


class CountMatcher(_Contract):
    """A bound on *how many* targets of a kind a set contains.

    The one matcher that is not a function of a single target: "fewer than
    three headlines" is a property of the whole submission, so the linter
    evaluates it once per lint call over the targets in scope rather than once
    per target. A finding from this matcher is attached to the set, not to any
    one piece of copy — see `LintFinding.target_ref`.
    """

    kind: Literal["count"] = "count"
    min: int | None = Field(default=None, ge=0)
    max: int | None = Field(default=None, ge=0)
    entity: str = Field(min_length=1)


class RatioMatcher(_Contract):
    """A bound on a measured image metric (PRD §9.5).

    The metric arrives as a number somebody else measured — S3-P5's OCR pass.
    This phase evaluates the rule; it does not produce the measurement. When
    the metric is absent the verdict is `indeterminate`, never `pass`
    (law 31).
    """

    kind: Literal["ratio"] = "ratio"
    metric: str = Field(min_length=1)
    max: float | None = None
    min: float | None = None


class EnumAllowMatcher(_Contract):
    """An allow-list on one scalar field of the target."""

    kind: Literal["enum_allow"] = "enum_allow"
    field: str = Field(min_length=1)
    allowed: tuple[str, ...] = Field(min_length=1)


class ClaimLicenceMatcher(_Contract):
    """Default deny on claim-shaped language (law 24, PRD §9.3).

    Runs the named detectors over the target, then asks the ruleset's
    `claims_index` whether each candidate span is licensed. Unlicensed,
    unsigned, rejected and expired all behave identically: blocking. Nothing is
    licensed because it "reads true" — the licence is a signature or it does
    not exist.
    """

    kind: Literal["claim_licence"] = "claim_licence"
    detector_ids: tuple[str, ...] = Field(min_length=1)
    licence_source: Literal["claims_index"] = "claims_index"
    #: `claims.match_threshold`, compiled in rather than read from the constants
    #: file at lint time. A ruleset has to fully determine its own verdicts:
    #: Stage 04 is handed one and nothing else, and two processes holding
    #: different constants files must not disagree about the same ruleset.
    match_threshold: float = Field(default=0.88, ge=0.0, le=1.0)


class OfferBindingMatcher(_Contract):
    """A price, discount or deadline checked against real offer data (§9.4).

    The offer rows are passed into `lint()` as an argument. They are never
    fetched here — a matcher that could fetch is a matcher whose verdict
    depends on when it ran.
    """

    kind: Literal["offer_binding"] = "offer_binding"
    construction: Literal[
        "from_price",
        "percent_off",
        "amount_off",
        "countdown",
        "free_trial",
        "price_match",
    ]
    field: str = Field(min_length=1)
    source: Literal["offer_record"] = "offer_record"
    tolerance: float = Field(default=0.0, ge=0.0)


class DisclosureMatcher(_Contract):
    """Generated creative must say so, where the policy says to say it."""

    kind: Literal["disclosure"] = "disclosure"
    trigger: Literal["generated_by_ai"] = "generated_by_ai"
    required_text: str = Field(min_length=1)
    placement: Literal["prefix", "suffix", "anywhere"] = "anywhere"


Matcher = Annotated[
    RegexMatcher
    | TermSetMatcher
    | LengthMatcher
    | CountMatcher
    | RatioMatcher
    | EnumAllowMatcher
    | ClaimLicenceMatcher
    | OfferBindingMatcher
    | DisclosureMatcher,
    Field(discriminator="kind"),
]


# ---------------------------------------------------------------------------
# Rules and the compiled set
# ---------------------------------------------------------------------------


class Rule(_Contract):
    """One enforceable rule. PRD §12.1's `RuleDraft` and §12.2's `Rule` are this.

    `message` is written *to a person who is blocked* — "Headlines are 30
    characters; this is 34" beats "length violation". `fix_hint` is the
    optional second line that says what to do instead.
    """

    rule_id: str = Field(min_length=1)
    category: RuleCategory
    severity: Severity
    scope: RuleScope = RuleScope()
    matcher: Matcher
    message: str = Field(min_length=1)
    fix_hint: str | None = None
    authority: Authority
    evidence_ids: tuple[UUID, ...] = ()


class ClaimRef(_Contract):
    """A registered claim and the surface forms its signature licenses.

    `expires_at` is compared against the `now` passed into `lint()`. A claim
    that has lapsed stops licensing its forms on the next lint — no sweep
    required, no stale cache to invalidate.
    """

    claim_id: UUID
    normalized_text: str = Field(min_length=1)
    surface_forms: tuple[str, ...] = ()
    status: Literal["draft", "approved", "rejected", "expired"]
    market_scope: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    expires_at: datetime | None = None
    signature_id: UUID | None = None


class DetectorSpec(_Contract):
    """One claim-shaped-language pattern, from `content_constants.yaml`.

    Detectors are data, not code, so that adding "we are the only X" to the
    superlative family is a constants edit and a MINOR version bump rather than
    a deploy.
    """

    detector_id: str = Field(min_length=1)
    family: ClaimFamily
    locale: str = "en"
    pattern: str = Field(min_length=1)
    flags: tuple[RegexFlag, ...] = ("i",)
    description: str = ""


class AssetSpec(_Contract):
    """Google's shape for one asset type, with the provenance of every number.

    Every field is optional because the specs are not uniform — a path has a
    character limit and no ratio, an image has a ratio and no character limit.
    `source` travels with the numbers all the way to the finding, which is what
    stops an `unverified` limit from being enforced as though Google had said
    it (PRD §9.6, Q7).
    """

    max_chars: int | None = Field(default=None, ge=1)
    min_count: int | None = Field(default=None, ge=0)
    max_count: int | None = Field(default=None, ge=0)
    ratio: str | None = None
    min_px: str | None = None
    max_bytes: int | None = Field(default=None, ge=1)
    source: str = Field(min_length=1)
    reviewed_at: date


class AssetSpecSheet(_Contract):
    """`campaign_type -> asset_type -> AssetSpec`, as compiled into a ruleset."""

    specs: dict[str, dict[str, AssetSpec]] = Field(default_factory=dict)

    def for_campaign(self, campaign_type: str) -> dict[str, AssetSpec]:
        return self.specs.get(campaign_type, {})


class LogoTemplate(_Contract):
    """A registered logo, for the S3-P5 image match. Carried, not used, here."""

    asset_id: UUID
    label: str = Field(min_length=1)
    phash: str = Field(min_length=1)
    min_score: float = Field(ge=0.0, le=1.0)


class DisclosureRule(_Contract):
    """Where an AI-disclosure string is required, and what it must say."""

    disclosure_id: str = Field(min_length=1)
    surfaces: tuple[Surface, ...] = ()
    markets: tuple[str, ...] = ()
    required_text: str = Field(min_length=1)
    placement: Literal["prefix", "suffix", "anywhere"] = "anywhere"


class RuleSet(_Contract):
    """The machine artifact, and the whole of what Stage 04 is allowed to read.

    PRD §12.2. Compiled by `guardrails/compiler.py` and by nothing else, hashed
    over its own canonical rendering, and written once — a database trigger
    rejects any later `UPDATE` (law 26). A creative asset records the
    `ruleset_version` it was linted against, so it can be re-checked a year
    later against exactly the rules that applied when it was made.
    """

    schema_version: Literal["1.0"] = GUARDRAILS_SCHEMA_VERSION
    ruleset_version: str = Field(min_length=1)
    project_id: UUID
    guideline_id: UUID
    compiler_version: str = Field(min_length=1)
    constants_version: str = Field(min_length=1)
    compiled_at: datetime

    rules: tuple[Rule, ...] = ()
    claims_index: tuple[ClaimRef, ...] = ()
    detectors: tuple[DetectorSpec, ...] = ()
    asset_specs: AssetSpecSheet = AssetSpecSheet()
    disclosure_requirements: tuple[DisclosureRule, ...] = ()
    logo_templates: tuple[LogoTemplate, ...] = ()
    hash: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# Linting — PRD §12.2
# ---------------------------------------------------------------------------


class LintTarget(_Contract):
    """One piece of copy or one image, with everything a rule needs to scope it.

    `ref` is the caller's identifier and is echoed back on every finding. The
    linter never dereferences it, so Stage 04 can use whatever it likes —
    an asset id, a row number, a draft slug.
    """

    ref: str = Field(min_length=1)
    surface: Surface
    campaign_type: str = Field(min_length=1)
    market: str = Field(min_length=1)
    language: str = Field(min_length=1)
    text: str | None = None
    image_ref: str | None = None
    generated_by_ai: bool = False
    #: Deterministic image metrics measured upstream (S3-P5). Absent means the
    #: measurement did not run, which makes an image rule `indeterminate` — a
    #: blocking detector fails closed (law 31).
    image_metrics: dict[str, float] | None = None


class LintFinding(_Contract):
    """One rule, one target, one reason. Never a bare "violation".

    `authority_ref` duplicates `Rule.authority.reference` onto the finding on
    purpose: findings travel to a writer's screen and to Stage 04's logs
    without the ruleset attached, and "who said so" has to survive that trip.
    """

    target_ref: str
    rule_id: str = Field(min_length=1)
    severity: Severity
    span: tuple[int, int] | None = None
    message: str = Field(min_length=1)
    fix_hint: str | None = None
    authority_ref: str = Field(min_length=1)
    claim_id: UUID | None = None
    #: `indeterminate` is a verdict, not an error: a blocking check whose input
    #: was unavailable must not read as a pass (law 31).
    indeterminate: bool = False


class LintResult(_Contract):
    """Everything one `lint()` call produced.

    `elapsed_ms` is the only field that legitimately differs between two runs
    of the same inputs, which is why the determinism test excludes it and
    nothing else.
    """

    ruleset_version: str = Field(min_length=1)
    verdict: Literal["pass", "pass_with_warnings", "fail"]
    findings: tuple[LintFinding, ...] = ()
    targets_checked: int = Field(ge=0)
    rules_evaluated: int = Field(ge=0)
    elapsed_ms: int = Field(ge=0)
    evaluated_at: datetime


class OfferRecord(_Contract):
    """Live offer data, passed into `lint()` for `offers.py` to check against.

    PRD §9.4 and Q4: it arrives through `csv_ingest` today and may come from a
    pricing feed later. Either way it is an *argument*, because a matcher that
    could fetch its own prices is a matcher whose verdict depends on when it
    ran.
    """

    sku: str = Field(min_length=1)
    product_set: str = Field(min_length=1)
    list_price: float = Field(ge=0)
    current_price: float = Field(ge=0)
    currency: str = Field(min_length=1)
    market: str = Field(min_length=1)
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    reference_price: float | None = Field(default=None, ge=0)
    ends_at: datetime | None = None
    extensions: int = Field(default=0, ge=0)
    #: When this row was read. A finding computed against offer data older than
    #: the staleness window carries a warning alongside it (Q4).
    observed_at: datetime | None = None
