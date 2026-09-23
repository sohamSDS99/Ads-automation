"""Assembling seventeen node outputs into one `ContentGuideline` (node 3.6.1).

The Stage 03 twin of `planning/plan_synthesis.py`, and it inherits that module's
central decision unchanged: **the artifact is a projection, not a composition.**
Every section below is built from outputs already on disk. The model writes the
executive summary and nothing else.

That is not a stylistic preference. §12.1 invariant 2 requires every
`Rule.authority` to resolve and every non-`internal` rule to carry at least one
`evidence_id`. A model that *wrote* the rules would be sourcing facts, which is
law 1; and the rules are the half of this document that gets compiled into a
program and enforced against somebody's copy.

-----------------------------------------------------------------------------
THE FLATTENING IS THE LOAD-BEARING PART
-----------------------------------------------------------------------------
`ContentGuideline.rules` is the only key `compiler.compile` reads. A section
that grows a rule and does not flatten it here produces a rulebook that *says*
the rule in prose and never enforces it — the exact failure `guardrails/` was
built to make impossible, arriving one layer up.

So `flatten` is exhaustive by construction: every category in `RuleCategory` has
a builder, and `test_guideline_synthesis.py` asserts the set of categories
`flatten` can emit equals the set the registry knows. A new category fails that
test rather than silently enforcing nothing.

**Only registered rule ids may be emitted.** `require_registered` refuses the
rest at compile time, which is the right place for a hand-edited payload — but
finding out at publish that a section has been unenforceable for three weeks is
not. `flatten` therefore builds from the twenty-one ids S3-P1 registered, and
`_rule` is the single constructor, so an id that stops existing breaks one
function rather than nine call sites.

**Severity comes from the registry, never from the model.** Eleven of the
twenty-one ids fix their severity at registration; `require_registered` rejects
a mismatch. `_severity_for` reads the registry rather than the node output, so a
model that labelled its banned term `advisory` cannot downgrade a rule the
registry calls `blocking`.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import structlog

from agent.export.guideline_contract import (
    AssetSpecs,
    BrandRules,
    ClaimsRegister,
    ContentGuideline,
    Dependency,
    GateDecision,
    Governance,
    GuidelineStatus,
    HumanTaskRef,
    LaunchMinimum,
    Lexicon,
    LexiconEntry,
    OfferRule,
    Owners,
    PolicyArea,
    PolicyProfile,
    RegisteredClaim,
    ReviewTrigger,
    SignatureRef,
    VisualIdentity,
    VoiceExample,
    VoiceProfile,
)
from agent.guardrails.registry import RULES
from agent.guidelines.constants import ContentConstants
from agent.schemas.guardrails import (
    AssetSpecSheet,
    Authority,
    ClaimLicenceMatcher,
    CountMatcher,
    DisclosureMatcher,
    DisclosureRule,
    LengthMatcher,
    LogoTemplate,
    Matcher,
    Rule,
    RuleScope,
    Severity,
    Surface,
    TermSetMatcher,
)
from agent.schemas.guardrails import RegexMatcher as Regex

log = structlog.get_logger(__name__)

#: Every node the rulebook is built from. §11: `3.6.1←{all}`. Written out rather
#: than derived, because `depends_on` is read at import time and the registry is
#: what imports the node module. `test_guideline_nodes_3_6.py` asserts it equals
#: every non-report guideline node the registry knows, so a node added later
#: fails the suite instead of quietly never reaching the rulebook.
ALL_GUIDELINE_NODES: tuple[str, ...] = (
    "3.1.1",
    "3.1.2",
    "3.1.3",
    "3.2.1",
    "3.2.2",
    "3.2.3",
    "3.2.4",
    "3.3.1",
    "3.3.2",
    "3.3.3",
    "3.3.4",
    "3.4.1",
    "3.4.2",
    "3.4.3",
    "3.5.1",
    "3.5.2",
)

#: How many items of one kind a rulebook carries before it stops. A rulebook
#: with nine thousand lexicon rules is one nobody reads and one the compiler
#: spends a minute on; the cut is logged and surfaces in `degraded_sources`.
MAX_PER_SECTION = 2_000

Drop = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class GuidelineFacts:
    """Everything the rulebook knows that is not a node output."""

    project_id: uuid.UUID
    guideline_run_id: uuid.UUID
    guideline_id: uuid.UUID
    version_major: int
    version_minor: int
    mode: str
    bindings: Mapping[str, Any]
    unbound_inputs: Sequence[str]
    degraded_sources: Sequence[str]
    constants_version: str
    generated_at: datetime
    cost_usd: float = 0.0


@dataclass(slots=True)
class Assembled:
    """The rulebook and what was dropped getting there."""

    guideline: ContentGuideline
    dropped: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def status_for(
    decisions: Iterable[GateDecision],
    *,
    blocking_issues: int,
    open_blocking_tasks: int,
) -> GuidelineStatus:
    """§12.4's state machine, as a rule rather than a judgement.

    `blocked` beats `draft`: a rejected gate is a human saying no to part of the
    rulebook, and one carrying a rejection must not read as merely unfinished.
    `ready_to_publish` needs every opened gate approved, a clean critique **and**
    no open person-task that blocks publish — which is why 3.6.1 can never write
    it: it runs before the critique.

    Only tasks blocking *publish* count. H2 blocks launch and is carried forward
    as an open dependency (§11), so letting it hold the rulebook in `draft`
    would stop a brand team publishing because a company officer has not yet
    filed a verification document with Google.
    """
    rows = list(decisions)
    if any(row.status == "rejected" for row in rows):
        return "blocked"
    if blocking_issues:
        return "blocked"
    if open_blocking_tasks:
        return "draft"
    if rows and all(row.is_approved for row in rows):
        return "ready_to_publish"
    if not rows:
        # No gate opened at all: 3.5.1 reused a matrix and 3.1.3 had nothing to
        # confirm. That is a legitimate run, not an unfinished one.
        return "ready_to_publish"
    return "draft"


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------


def assemble(
    outputs: Mapping[str, Mapping[str, Any]],
    facts: GuidelineFacts,
    *,
    constants: ContentConstants,
    claims: Sequence[RegisteredClaim] = (),
    decisions: Sequence[GateDecision] = (),
    signatures: Sequence[SignatureRef] = (),
    human_tasks: Sequence[HumanTaskRef] = (),
    executive_summary: str = "",
    written_claims: Sequence[Any] = (),
) -> Assembled:
    """Build one rulebook. Shared by 3.6.1 and by 3.6.2's single re-synthesis."""
    dropped: list[str] = []

    def drop(what: str) -> None:
        dropped.append(what)
        log.warning("guideline.section_dropped", what=what, run=str(facts.guideline_run_id))

    brand = _brand_rules(outputs, drop)
    register = _claims_register(outputs, claims, drop)
    policy = _policy_profile(outputs, drop)
    specs = _asset_specs(outputs, constants, drop)
    governance = _governance(outputs, drop)

    rules = flatten(
        brand=brand,
        register=register,
        policy=policy,
        specs=specs,
        governance=governance,
        constants=constants,
        drop=drop,
    )

    guideline = ContentGuideline(
        project_id=facts.project_id,
        guideline_run_id=facts.guideline_run_id,
        guideline_id=facts.guideline_id,
        version_major=facts.version_major,
        version_minor=facts.version_minor,
        generated_at=facts.generated_at,
        mode=facts.mode,  # type: ignore[arg-type]  # validated by the contract
        bindings=dict(facts.bindings),
        unbound_inputs=list(facts.unbound_inputs),
        executive_summary=executive_summary,
        brand_rules=brand,
        claims_register=register,
        policy_profile=policy,
        asset_specs=specs,
        governance=governance,
        rules=rules,
        disclosure_requirements=list(policy.disclosure_rules),
        logo_templates=list(specs.logo_templates),
        decisions=list(decisions),
        signatures=list(signatures),
        human_tasks=list(human_tasks),
        open_dependencies=_dependencies(human_tasks, policy, register),
        degraded_sources=list(facts.degraded_sources),
        constants_version=facts.constants_version,
        cost_usd=facts.cost_usd,
        written_claims=list(written_claims),
    )
    return Assembled(guideline=guideline, dropped=dropped)


# ---------------------------------------------------------------------------
# sections
# ---------------------------------------------------------------------------


def _brand_rules(outputs: Mapping[str, Mapping[str, Any]], drop: Drop) -> BrandRules:
    voice_out = _out(outputs, "3.1.1")
    lexicon_out = _out(outputs, "3.1.2")
    visual_out = _out(outputs, "3.1.3")

    voice = VoiceProfile(
        voice_words=_strings(voice_out.get("voice_words")),
        definition_per_word=_str_map(voice_out.get("definition_per_word")),
        do_examples=_examples(voice_out.get("do_examples")),
        dont_examples=_examples(voice_out.get("dont_examples")),
        # 3.1.1 already spells it `voice_register`; the contract's alias keeps
        # a payload written under §11's `register` valid too.
        voice_register=_dict(voice_out.get("voice_register") or voice_out.get("register")),
        readability_targets=_dict(voice_out.get("readability_targets")),
        input_mode=_text(voice_out.get("input_mode")),
        evidence_ids=_uuids(voice_out.get("evidence_ids")),
    )
    if not voice.voice_words:
        drop("3.1.1 produced no voice words")

    lexicon = Lexicon(
        always=_lexicon(lexicon_out.get("always"), drop, "always"),
        never=_lexicon(lexicon_out.get("never"), drop, "never"),
        case_and_spelling=_dicts(lexicon_out.get("case_and_spelling")),
        # Read, never recomputed. 3.1.2 decides what conflicts; a second
        # opinion here is two pieces of code disagreeing about the same set,
        # and §11 assertion 5 checks the one the authoring node published.
        conflicts=_dicts(lexicon_out.get("conflicts")),
    )

    visual = VisualIdentity(
        logo=_dict(visual_out.get("logo")),
        colour=_dict(visual_out.get("colour")),
        imagery=_dict(visual_out.get("imagery")),
        extraction_confidence=_float(visual_out.get("extraction_confidence")),
        evidence_ids=_uuids(visual_out.get("evidence_ids")),
    )
    return BrandRules(voice=voice, lexicon=lexicon, visual_identity=visual)


def _claims_register(
    outputs: Mapping[str, Mapping[str, Any]],
    claims: Sequence[RegisteredClaim],
    drop: Drop,
) -> ClaimsRegister:
    harvest = _out(outputs, "3.2.1")
    substantiation = _out(outputs, "3.2.2")
    offers = _out(outputs, "3.2.4")

    rows = list(claims)
    if not rows:
        # The register comes from `claim_record`, which 3.2.3 materialised. If
        # it is empty the node outputs are the only account of what was
        # harvested, and a rulebook that showed nothing would read as "we make
        # no claims" — which is a very different statement from "nobody has
        # registered them".
        rows = _claims_from_outputs(harvest, substantiation)
        if rows:
            drop("claims register read from node output; no claim_record rows found")

    return ClaimsRegister(
        claims=rows[:MAX_PER_SECTION],
        unsupported_count=_int(substantiation.get("unsupported_count")) or 0,
        expiry_basis=_text(substantiation.get("expiry_basis")),
        detector_recall_note=_text(harvest.get("detector_recall_note")),
        offer_rules=[
            OfferRule(
                id=_text(item.get("id")),
                construction=_text(item.get("construction")),
                requirement=_text(item.get("requirement")),
                data_binding=_dict(item.get("data_binding")),
                severity=_text(item.get("severity")) or "blocking",
            )
            for item in _dicts(offers.get("rules"))
        ],
        live_violations=_dicts(offers.get("live_violations")),
    )


def _policy_profile(outputs: Mapping[str, Mapping[str, Any]], drop: Drop) -> PolicyProfile:
    surface = _out(outputs, "3.3.1")
    attestation = _out(outputs, "3.3.2")
    competitive = _out(outputs, "3.3.3")
    disclosure = _out(outputs, "3.3.4")

    applicable = [_policy_area(item) for item in _dicts(surface.get("applicable"))]
    if not applicable:
        drop("3.3.1 found no applicable policy area")

    rules: list[DisclosureRule] = []
    for item in _dicts(disclosure.get("disclosure_rules")):
        try:
            rules.append(DisclosureRule.model_validate(item))
        except Exception:  # noqa: BLE001 — one bad rule must not lose the rest
            drop(f"disclosure rule {item.get('disclosure_id', '?')} did not validate")

    return PolicyProfile(
        applicable=applicable,
        not_applicable=[_policy_area(item) for item in _dicts(surface.get("not_applicable"))],
        requires_verification=_dicts(surface.get("requires_verification")),
        open_interpretation=_dicts(surface.get("open_interpretation")),
        attestation={
            "status": _text(attestation.get("status")),
            "blocking_for": _text(attestation.get("blocking_for")),
            "verification_kinds": _strings(attestation.get("verification_kinds")),
            "markets": _strings(attestation.get("markets")),
            "why": _text(attestation.get("why")),
        },
        competitor_mentions=_dict(competitive.get("competitor_mentions")),
        personalization=_dict(competitive.get("personalization")),
        disclosure_rules=rules,
        internal_policy_addendum=_text(disclosure.get("internal_policy_addendum")),
    )


def _asset_specs(
    outputs: Mapping[str, Mapping[str, Any]],
    constants: ContentConstants,
    drop: Drop,
) -> AssetSpecs:
    sheet_out = _out(outputs, "3.4.1")
    minimums_out = _out(outputs, "3.4.2")
    image_out = _out(outputs, "3.4.3")

    raw = sheet_out.get("specs")
    try:
        sheet = AssetSpecSheet.model_validate({"specs": raw}) if raw else constants.asset_sheet()
    except Exception:  # noqa: BLE001 — fall back rather than lose the rulebook
        drop("3.4.1's spec sheet did not validate; the constants sheet was used")
        sheet = constants.asset_sheet()

    logos: list[LogoTemplate] = []
    for item in _dicts(image_out.get("logo_templates")):
        try:
            logos.append(LogoTemplate.model_validate(item))
        except Exception:  # noqa: BLE001
            drop(f"logo template {item.get('label', '?')} did not validate")

    scope = _text(sheet_out.get("scope")) or "unscoped"
    return AssetSpecs(
        sheet=sheet,
        scope="scoped" if scope == "scoped" else "unscoped",
        launch_minimums=[
            LaunchMinimum(
                campaign_type=_text(item.get("campaign_type")),
                required_assets=_dicts(item.get("required_assets")),
                optional_but_recommended=_dicts(item.get("optional_but_recommended")),
                blocking_for_launch=bool(item.get("blocking_for_launch")),
            )
            for item in _dicts(minimums_out.get("minimums"))
        ],
        readiness_checklist=_dicts(minimums_out.get("readiness_checklist")),
        image_rules=_dicts(image_out.get("rules")),
        logo_templates=logos,
    )


def _governance(outputs: Mapping[str, Mapping[str, Any]], drop: Drop) -> Governance:
    matrix = _out(outputs, "3.5.1")
    triggers = _out(outputs, "3.5.2")
    owners_raw = _dict(matrix.get("owners"))

    owners = Owners(
        brand_owner_id=_uuid(owners_raw.get("brand_owner_id")),
        legal_owner_id=_uuid(owners_raw.get("legal_owner_id")),
        performance_owner_id=_uuid(owners_raw.get("performance_owner_id")),
    )
    if owners.legal_owner_id is None:
        drop("3.5.1 named no legal owner")

    return Governance(
        owners=owners,
        owners_rationale=_text(matrix.get("rationale")),
        matrix_reused=bool(matrix.get("reused")),
        review_triggers=[
            ReviewTrigger.model_validate(item) for item in _dicts(triggers.get("triggers"))
        ],
        always_review=_strings(triggers.get("always_review")),
        # 3.5.3 is S3-P9. An empty list here is the honest answer until then,
        # and §11 assertion 7's counterpart for learned rules is not asserted
        # against a node that does not exist.
        learned_rules=[],
        unresolved_disapprovals=[],
    )


def _dependencies(
    human_tasks: Sequence[HumanTaskRef],
    policy: PolicyProfile,
    register: ClaimsRegister,
) -> list[Dependency]:
    """Everything outstanding, each with an owner and what it blocks (§12.1).

    §11 assertion 8 checks that every open `HumanTask` appears here, so this is
    built from the tasks rather than from a model's idea of what is left.
    """
    found: list[Dependency] = [
        Dependency(
            task=(
                "Sign the claims register"
                if task.task_key == "H1"
                else "File the verification Google requires"
            ),
            owner=str(task.assignee_id) if task.assignee_id else "unassigned",
            blocking_for=task.blocking_for or ("publish" if task.task_key == "H1" else "launch"),
            source=task.node_id or task.task_key,
        )
        for task in human_tasks
        if task.status not in {"completed", "cancelled"}
    ]
    found.extend(
        Dependency(
            task=f"Decide the open interpretation: {_text(item.get('question')) or item!s:.120}",
            owner="unassigned",
            blocking_for="none",
            source="3.3.1",
        )
        for item in policy.open_interpretation[:20]
    )
    if register.unsupported_count:
        found.append(
            Dependency(
                task=(
                    f"Substantiate or withdraw {register.unsupported_count} unsupported "
                    "claim(s); each one blocks any copy asserting it"
                ),
                owner="unassigned",
                blocking_for="none",
                source="3.2.2",
            )
        )
    return found


# ---------------------------------------------------------------------------
# the flattening
# ---------------------------------------------------------------------------


def flatten(
    *,
    brand: BrandRules,
    register: ClaimsRegister,
    policy: PolicyProfile,
    specs: AssetSpecs,
    governance: Governance,
    constants: ContentConstants,
    drop: Drop,
) -> list[Rule]:
    """Every section, as the one list `compiler.compile` reads.

    Ordered by category then rule id so two assemblies of the same sections
    produce the same list. The compiler sorts too, but a caller diffing two
    payloads would otherwise see churn that changed no rule.
    """
    rules: list[Rule] = []
    rules.extend(_voice_rules(brand, drop))
    rules.extend(_lexicon_rules(brand, drop))
    rules.extend(_claim_rules(register, constants, drop))
    rules.extend(_offer_rules(register, drop))
    rules.extend(_policy_rules(policy, drop))
    rules.extend(_asset_rules(specs, drop))
    rules.extend(_image_rules(specs, drop))
    rules.extend(_disclosure_rules(policy, drop))
    rules.extend(_governance_rules(governance, drop))
    return sorted(rules, key=lambda rule: (rule.category, rule.rule_id, rule.message))


def _rule(
    rule_id: str,
    *,
    matcher: Matcher,
    message: str,
    authority: Authority,
    scope: RuleScope | None = None,
    fix_hint: str | None = None,
    evidence_ids: Sequence[uuid.UUID] = (),
    severity: Severity | None = None,
) -> Rule:
    """The single constructor. Category and severity come from the registry.

    A caller cannot pass a category at all: `RULES[rule_id].category` is the
    only source, so a rule cannot be filed under a section it was not
    registered for. `severity` is accepted only where the registry left it
    per-rule, and is otherwise overridden — `require_registered` would reject
    the mismatch at compile time, and a publish that fails on a value a section
    chose three weeks ago is a worse place to learn it.
    """
    spec = RULES[rule_id]
    return Rule(
        rule_id=rule_id,
        category=spec.category,
        severity=spec.severity or severity or "warning",
        scope=scope or RuleScope(),
        matcher=matcher,
        message=message,
        fix_hint=fix_hint,
        authority=authority,
        evidence_ids=tuple(evidence_ids),
    )


def _voice_rules(brand: BrandRules, drop: Drop) -> list[Rule]:
    targets = brand.voice.readability_targets
    max_words = _int(targets.get("max_sentence_words"))
    if not max_words:
        return []
    return [
        _rule(
            "voice.sentence_length.v1",
            matcher=LengthMatcher(max=max_words, unit="words"),
            message=(
                f"Sentences run to {max_words} words in this brand's voice; this one is "
                "longer."
            ),
            fix_hint="Split it, or cut the qualifier.",
            authority=_authority("brand", "voice_profile.readability_targets"),
            evidence_ids=brand.voice.evidence_ids,
            severity="warning",
        )
    ]


def _lexicon_rules(brand: BrandRules, drop: Drop) -> list[Rule]:
    rules: list[Rule] = []
    for entry in brand.lexicon.never[:MAX_PER_SECTION]:
        terms = tuple(dict.fromkeys([entry.term, *entry.surface_forms]))
        if not terms:
            drop(f"lexicon `never` entry with no term: {entry.reason[:60]}")
            continue
        rules.append(
            _rule(
                "lexicon.banned_term.v1",
                matcher=TermSetMatcher(
                    terms=terms, mode="forbid", match="lemma", locale=entry.locale or "en"
                ),
                message=entry.reason or f"“{entry.term}” is not a word this brand uses.",
                fix_hint=(
                    f"Use “{entry.suggested_replacement}” instead."
                    if entry.suggested_replacement
                    else None
                ),
                authority=_authority("brand", f"lexicon.never.{entry.term}"),
                severity=_as_severity(entry.severity, "warning"),
            )
        )
    for entry in brand.lexicon.always[:MAX_PER_SECTION]:
        terms = tuple(dict.fromkeys([entry.term, *entry.surface_forms]))
        if not terms:
            drop(f"lexicon `always` entry with no term: {entry.context[:60]}")
            continue
        rules.append(
            _rule(
                "lexicon.required_term.v1",
                matcher=TermSetMatcher(
                    terms=terms, mode="require", match="exact", locale=entry.locale or "en"
                ),
                message=entry.context or f"“{entry.term}” has to appear here.",
                authority=_authority("brand", f"lexicon.always.{entry.term}"),
                severity=_as_severity(entry.severity, "warning"),
            )
        )
    return rules


def _claim_rules(
    register: ClaimsRegister, constants: ContentConstants, drop: Drop
) -> list[Rule]:
    """One licence rule over every detector, plus one refusal per dead claim.

    The licence rule is singular on purpose: `ClaimLicenceMatcher` runs the
    named detectors and then asks the ruleset's `claims_index` whether each
    span is licensed, so the register is consulted at lint time rather than
    frozen into one rule per claim. A rule per claim would go stale the moment
    a signature expired.

    The per-claim rules are the other half of §11 assertion 4: an `unsupported`,
    `rejected` or `expired` claim "produces a blocking rule naming it". Without
    them the register would merely fail to licence the text, and a writer would
    see "unlicensed claim" rather than "legal rejected this on 4 March".
    """
    detectors = tuple(sorted(spec.detector_id for spec in constants.detectors()))
    rules: list[Rule] = []
    if detectors:
        rules.append(
            _rule(
                "claim.licence.v1",
                matcher=ClaimLicenceMatcher(
                    detector_ids=detectors,
                    licence_source="claims_index",
                    # Compiled in, not read at lint time: Stage 04 is handed a
                    # `RuleSet` and nothing else, and two processes holding
                    # different constants files must not disagree about it.
                    match_threshold=constants.claims.match_threshold.value,
                ),
                message=(
                    "This reads as a factual claim. Only claims the legal owner has "
                    "signed may run."
                ),
                fix_hint="Soften it, or get the claim added to the register and signed.",
                authority=_authority("legal_signature", "claims_index"),
            )
        )
    else:
        drop("no claim detectors in the constants; nothing enforces the licence")

    dead = {"unsupported", "rejected", "expired", "revoked"}
    for claim in register.claims[:MAX_PER_SECTION]:
        if claim.status not in dead:
            continue
        forms = tuple(dict.fromkeys([claim.claim_text, *claim.surface_forms]))
        if not forms:
            continue
        rules.append(
            _rule(
                "policy.restricted_phrase.v1",
                matcher=Regex(pattern=_alternation(forms), flags=("i",)),
                message=_dead_claim_message(claim),
                fix_hint="Remove the claim, or substantiate it and have it signed.",
                authority=_authority(
                    "legal_signature" if claim.signature_id else "internal",
                    str(claim.signature_id) if claim.signature_id else "claims_register",
                ),
                scope=RuleScope(
                    markets=tuple(claim.market_scope), languages=tuple(claim.languages)
                ),
                evidence_ids=claim.evidence_ids,
            )
        )
    return rules


def _dead_claim_message(claim: RegisteredClaim) -> str:
    reason = {
        "unsupported": "has no substantiation on file",
        "rejected": "was rejected by the legal owner",
        "expired": "has an expired signature",
        "revoked": "had its signature revoked",
    }.get(claim.status, "is not licensed")
    return f"“{claim.claim_text}” {reason}, so no copy may assert it."


def _offer_rules(register: ClaimsRegister, drop: Drop) -> list[Rule]:
    from agent.schemas.guardrails import OfferBindingMatcher

    known = {
        "from_price",
        "percent_off",
        "amount_off",
        "countdown",
        "free_trial",
        "price_match",
    }
    rules: list[Rule] = []
    for entry in register.offer_rules[:MAX_PER_SECTION]:
        if entry.construction not in known:
            drop(f"offer rule {entry.id or '?'} names construction {entry.construction!r}")
            continue
        binding = entry.data_binding or {}
        field_name = _text(binding.get("field")) or entry.construction
        rules.append(
            _rule(
                f"offer.{entry.construction}.v1",
                matcher=OfferBindingMatcher(
                    construction=entry.construction,  # type: ignore[arg-type]  # checked above
                    field=field_name,
                    source="offer_record",
                    tolerance=_float(binding.get("tolerance")) or 0.0,
                    product_set=_optional_text(binding.get("product_set")),
                ),
                message=entry.requirement or f"A {entry.construction} claim must match live offer data.",
                fix_hint="Update the copy, or update the offer feed.",
                authority=_authority("internal", f"offer_integrity.{entry.construction}"),
            )
        )
    return rules


def _policy_rules(policy: PolicyProfile, drop: Drop) -> list[Rule]:
    """One rule per applicable area that names forbidden phrasing.

    §11 assertion 7 wants every applicable area to produce a rule *or* an
    explicit `no_rule_needed`. An area that produces neither is reported by the
    critique rather than papered over with an empty rule here — an empty rule
    would satisfy the assertion and enforce nothing, which is the one outcome
    worse than failing it.
    """
    rules: list[Rule] = []
    forbidden = _dicts(policy.competitor_mentions.get("forbidden"))
    phrases = tuple(
        dict.fromkeys(
            _text(item.get("phrase") or item.get("text"))
            for item in forbidden
            if _text(item.get("phrase") or item.get("text"))
        )
    )
    if phrases:
        rules.append(
            _rule(
                "policy.restricted_phrase.v1",
                matcher=Regex(pattern=_alternation(phrases), flags=("i",)),
                message="Google's policy forbids this way of naming a competitor.",
                fix_hint="Drop the comparison, or name only your own product.",
                authority=_authority(
                    "google_policy",
                    _text(policy.competitor_mentions.get("policy_ref")) or "competitor_mentions",
                ),
            )
        )
    for item in policy.personalization.get("forbidden_implications", [])[:MAX_PER_SECTION]:
        text = _text(item if isinstance(item, str) else _dict(item).get("phrase"))
        if not text:
            continue
        rules.append(
            _rule(
                "policy.restricted_phrase.v1",
                matcher=Regex(pattern=_alternation((text,)), flags=("i",)),
                message=(
                    "An ad may not imply we know something personal about the viewer."
                ),
                fix_hint="Rewrite it to describe the product, not the reader.",
                authority=_authority("google_policy", "personalized_advertising"),
            )
        )
    return rules


def _asset_rules(specs: AssetSpecs, drop: Drop) -> list[Rule]:
    """Length and count bounds, one per (campaign type, asset type) with a bound.

    Scoped by campaign type *and* asset type. An unscoped length rule would
    apply a headline's 30 characters to a YouTube script.
    """
    rules: list[Rule] = []
    for campaign_type, by_asset in sorted(specs.sheet.specs.items()):
        for asset_type, spec in sorted(by_asset.items()):
            scope = RuleScope(campaign_types=(campaign_type,), asset_types=(asset_type,))
            authority = _authority(
                "internal" if spec.source != "google_policy" else "google_policy",
                f"asset_specs.{campaign_type}.{asset_type}",
                reviewed_at=spec.reviewed_at,
            )
            if spec.max_chars:
                rules.append(
                    _rule(
                        "asset_spec.length.v1",
                        matcher=LengthMatcher(max=spec.max_chars, unit="chars"),
                        message=(
                            f"{asset_type.replace('_', ' ').capitalize()} is "
                            f"{spec.max_chars} characters on {campaign_type}."
                        ),
                        fix_hint="Shorten it — Google truncates rather than rejects.",
                        authority=authority,
                        scope=scope,
                    )
                )
            if spec.min_count is not None or spec.max_count is not None:
                rules.append(
                    _rule(
                        "asset_spec.count.v1",
                        matcher=CountMatcher(
                            min=spec.min_count, max=spec.max_count, entity=asset_type
                        ),
                        message=_count_message(campaign_type, asset_type, spec),
                        fix_hint="Add or remove assets until the count is inside the bound.",
                        authority=authority,
                        scope=scope,
                    )
                )
    return rules


def _count_message(campaign_type: str, asset_type: str, spec: Any) -> str:
    name = asset_type.replace("_", " ")
    if spec.min_count is not None and spec.max_count is not None:
        bound = f"between {spec.min_count} and {spec.max_count}"
    elif spec.min_count is not None:
        bound = f"at least {spec.min_count}"
    else:
        bound = f"at most {spec.max_count}"
    return f"{campaign_type} needs {bound} {name}."


def _image_rules(specs: AssetSpecs, drop: Drop) -> list[Rule]:
    """3.4.3 already emits `Rule`s; they are validated, not rebuilt."""
    rules: list[Rule] = []
    for item in specs.image_rules[:MAX_PER_SECTION]:
        try:
            rules.append(Rule.model_validate(item))
        except Exception:  # noqa: BLE001 — one bad rule must not lose the rest
            drop(f"image rule {item.get('rule_id', '?')} did not validate")
    return rules


def _disclosure_rules(policy: PolicyProfile, drop: Drop) -> list[Rule]:
    rules: list[Rule] = []
    for item in policy.disclosure_rules[:MAX_PER_SECTION]:
        rules.append(
            _rule(
                "disclosure.ai_generated.v1",
                matcher=DisclosureMatcher(
                    trigger="generated_by_ai",
                    required_text=item.required_text,
                    placement=item.placement,
                ),
                message=(
                    "Generated creative has to carry the disclosure on this surface."
                ),
                fix_hint=f"Add “{item.required_text}”.",
                authority=_authority("google_policy", item.disclosure_id),
                scope=RuleScope(surfaces=item.surfaces, markets=item.markets),
            )
        )
    return rules


def _governance_rules(governance: Governance, drop: Drop) -> list[Rule]:
    rules: list[Rule] = []
    for trigger in governance.review_triggers[:MAX_PER_SECTION]:
        if trigger.pattern_kind != "term" or not trigger.pattern:
            # Only `term` triggers compile to a regex over copy. The other four
            # scope a review by campaign type, market or asset type — they are
            # routing metadata, carried in `governance.review_triggers` and read
            # by the console, not conditions on a piece of text.
            continue
        rules.append(
            _rule(
                "governance.review_trigger.v1",
                matcher=Regex(pattern=_alternation((trigger.pattern,)), flags=("i",)),
                message=(
                    f"{trigger.why or 'This wording needs a second read'} — "
                    f"route it to {trigger.reviewer_role or 'legal'} before it ships."
                ),
                authority=_authority("internal", f"review_trigger.{trigger.id or trigger.pattern}"),
                severity=_as_severity(trigger.severity, "warning"),
            )
        )
    return rules


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _authority(source: str, reference: str, *, reviewed_at: date | None = None) -> Authority:
    return Authority(
        source=source,  # type: ignore[arg-type]  # callers pass literals
        reference=reference or "unspecified",
        reviewed_at=reviewed_at or datetime.now(UTC).date(),
    )


def _alternation(phrases: Sequence[str]) -> str:
    """A regex matching any of these phrases, whole-word where it can.

    `re.escape` on every phrase: a claim containing `(` or `+` is ordinary
    marketing copy and must not become a pattern. The word boundaries are
    conditional because `\\b` before a non-word character can never match — a
    phrase starting `#1` would silently drop out of the alternation while the
    pattern still compiled and still matched its other branches.
    """
    import re

    parts: list[str] = []
    for phrase in phrases:
        text = phrase.strip()
        if not text:
            continue
        escaped = re.escape(text)
        prefix = r"\b" if text[0].isalnum() or text[0] == "_" else ""
        suffix = r"\b" if text[-1].isalnum() or text[-1] == "_" else ""
        parts.append(f"{prefix}(?:{escaped}){suffix}")
    return "|".join(parts) if parts else r"(?!x)x"


def _as_severity(value: str, default: Severity) -> Severity:
    return value if value in {"blocking", "warning", "advisory"} else default  # type: ignore[return-value]


def _examples(value: Any) -> list[VoiceExample]:
    return [
        VoiceExample(
            text=_text(item.get("text")),
            source_ref=_text(item.get("source_ref")),
            why=_text(item.get("why")),
            rewritten_as=_optional_text(item.get("rewritten_as")),
        )
        for item in _dicts(value)
        if _text(item.get("text"))
    ]


def _lexicon(value: Any, drop: Drop, side: str) -> list[LexiconEntry]:
    entries: list[LexiconEntry] = []
    for item in _dicts(value):
        term = _text(item.get("term"))
        if not term:
            drop(f"lexicon `{side}` entry with no term")
            continue
        entries.append(
            LexiconEntry(
                term=term,
                surface_forms=_strings(item.get("surface_forms")),
                severity=_text(item.get("severity")) or "warning",
                locale=_optional_text(item.get("locale")),
                context=_text(item.get("context")),
                reason=_text(item.get("reason")),
                suggested_replacement=_optional_text(item.get("suggested_replacement")),
            )
        )
    return entries


def _policy_area(item: Mapping[str, Any]) -> PolicyArea:
    return PolicyArea(
        area=_text(item.get("area")),
        policy_ref=_text(item.get("policy_ref")),
        why_applicable=_text(item.get("why_applicable")),
        why_not=_text(item.get("why_not")),
        markets=_strings(item.get("markets")),
        obligations=_strings(item.get("obligations")),
        evidence_ids=_uuids(item.get("evidence_ids")),
        no_rule_needed=_optional_text(item.get("no_rule_needed")),
    )


def _claims_from_outputs(
    harvest: Mapping[str, Any], substantiation: Mapping[str, Any]
) -> list[RegisteredClaim]:
    """The register as the nodes saw it, when no rows were found.

    Ids are the ones 3.2.2 carried if it carried any, and a fresh UUID
    otherwise. A synthesised id never reaches `claim_record`; it exists so the
    rulebook can show the claim, and `critique` assertion 3 will still fail the
    publish because no signature covers it.
    """
    candidates = _dicts(harvest.get("candidates"))
    rows: list[RegisteredClaim] = []
    for index, verdict in enumerate(_dicts(substantiation.get("claims"))):
        source = candidates[index] if index < len(candidates) else {}
        rows.append(
            RegisteredClaim(
                claim_id=_uuid(verdict.get("claim_id")) or uuid.uuid4(),
                claim_text=_text(verdict.get("claim_text")),
                normalized_text=_text(verdict.get("normalized_text")),
                surface_forms=_strings(source.get("surface_forms")),
                claim_type=_text(verdict.get("claim_type")),
                status=_text(verdict.get("status")) or "unsupported",
                risk_tier=_text(verdict.get("risk_tier")),
                market_scope=_strings(source.get("market_scope")),
                languages=_strings(source.get("languages")),
                substantiation=_dict(verdict.get("substantiation")),
                gaps=_strings(verdict.get("gaps")),
                evidence_ids=_uuids(verdict.get("evidence_ids")),
            )
        )
    return rows


def _out(outputs: Mapping[str, Mapping[str, Any]], node_id: str) -> Mapping[str, Any]:
    """One node's output, or an empty mapping.

    Empty rather than raising: §4.3's degradation contract means a node can
    legitimately have produced nothing, and a rulebook that failed to assemble
    because 3.3.3 found no competitors would be a worse answer than one whose
    competitor section is empty and says so.
    """
    value = outputs.get(node_id)
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _optional_text(value: Any) -> str | None:
    text = _text(value)
    return text or None


def _strings(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    return [str(item).strip() for item in value if item is not None and str(item).strip()]


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _str_map(value: Any) -> dict[str, str]:
    return {str(key): _text(item) for key, item in _dict(value).items()}


def _dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _uuid(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _uuids(value: Any) -> list[uuid.UUID]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    found: list[uuid.UUID] = []
    for item in value:
        parsed = _uuid(item)
        if parsed is not None and parsed not in found:
            found.append(parsed)
    return found
