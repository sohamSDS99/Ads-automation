"""The `@rule` contract (S3-P1 deliverable 2, PRD §9.1, law 22).

What is actually being proved here is that a rule cannot lie: not about its id,
not about its category, not about a severity the law fixes, and not about
having an evaluator. The last one matters most — an unevaluated rule does not
fail loudly, it passes everything, and a rulebook that silently enforces
nothing is the worst possible outcome of this phase.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import pytest

from agent.guardrails import registry
from agent.guardrails.registry import (
    MATCHER_KINDS,
    RULE_ID,
    RULES,
    LintContext,
    RuleBody,
    RuleRegistrationError,
    finding,
    kind_for,
    matcher_kind,
    require_registered,
    rule,
)
from agent.schemas.guardrails import (
    Authority,
    LengthMatcher,
    Rule,
    RuleScope,
    TermSetMatcher,
)

AUTHORITY = Authority(source="brand", reference="brand_book#p3", reviewed_at=date(2026, 9, 22))


@pytest.fixture(autouse=True)
def _isolate_registries() -> Iterator[None]:
    """The registries are process-wide. A test that registered a rule and left
    it there would change what a later test's `require_registered` accepts."""
    rules = dict(RULES)
    kinds = dict(MATCHER_KINDS)
    try:
        yield
    finally:
        RULES.clear()
        RULES.update(rules)
        MATCHER_KINDS.clear()
        MATCHER_KINDS.update(kinds)


def a_term_rule() -> RuleBody:
    return RuleBody(
        matcher=TermSetMatcher(terms=("cheap",)),
        message="`cheap` is off-brand.",
        authority=AUTHORITY,
        severity="warning",
    )


# --- rule ids ---------------------------------------------------------------


@pytest.mark.parametrize(
    "rule_id",
    ["lexicon.banned_term.v1", "claim.superlative.en.v1", "image.text_coverage.v12"],
)
def test_a_well_formed_rule_id_is_accepted(rule_id: str) -> None:
    assert RULE_ID.match(rule_id)


@pytest.mark.parametrize(
    "rule_id",
    [
        "banned_term.v1",  # no namespace
        "lexicon.banned_term",  # no version — the version IS part of the id
        "Lexicon.Banned.v1",  # upper case
        "lexicon.banned-term.v1",  # hyphen
        "lexicon..v1",
        "",
    ],
)
def test_a_malformed_rule_id_is_refused_at_decoration(rule_id: str) -> None:
    with pytest.raises(RuleRegistrationError, match="is not a rule id"):
        rule(rule_id, category="lexicon", matcher_kind="term_set")


# --- identity is stamped, never written -------------------------------------


def test_the_decorator_stamps_the_identity_the_body_cannot_reach() -> None:
    built = rule("lexicon.banned_term.v1", category="lexicon", matcher_kind="term_set")(
        a_term_rule
    )()
    assert isinstance(built, Rule)
    assert built.rule_id == "lexicon.banned_term.v1"
    assert built.category == "lexicon"
    assert built.severity == "warning"
    assert built.authority.reference == "brand_book#p3"


def test_a_registered_rule_appears_in_the_registry_with_its_docstring() -> None:
    @rule("lexicon.required_term.v1", category="lexicon", matcher_kind="term_set")
    def required_term() -> RuleBody:
        """Terms the brand requires on first use."""
        return a_term_rule()

    spec = RULES["lexicon.required_term.v1"]
    assert spec.category == "lexicon"
    assert spec.matcher_kind == "term_set"
    assert spec.doc == "Terms the brand requires on first use."


def test_two_rules_cannot_share_an_id() -> None:
    rule("lexicon.banned_term.v1", category="lexicon", matcher_kind="term_set")(a_term_rule)

    def another() -> RuleBody:
        return a_term_rule()

    with pytest.raises(RuleRegistrationError, match="already registered"):
        rule("lexicon.banned_term.v1", category="lexicon", matcher_kind="term_set")(another)


def test_re_registering_the_same_function_is_not_a_collision() -> None:
    """`importlib.reload` re-executes a module. That is not two rules."""
    decorate = rule("lexicon.banned_term.v1", category="lexicon", matcher_kind="term_set")
    decorate(a_term_rule)
    decorate(a_term_rule)  # must not raise


# --- severity ---------------------------------------------------------------


def test_a_law_fixed_severity_cannot_be_talked_down() -> None:
    """Law 24: a claim rule is blocking. A body that returns `warning` is a bug
    being smuggled past the law, not a preference."""

    def soft_claim() -> RuleBody:
        return RuleBody(
            matcher=TermSetMatcher(terms=("best",)),
            message="unlicensed claim",
            authority=AUTHORITY,
            severity="warning",
        )

    built = rule(
        "claim.licence.v1", category="claim", matcher_kind="term_set", severity="blocking"
    )(soft_claim)
    with pytest.raises(RuleRegistrationError, match="always blocking"):
        built()


def test_a_fixed_severity_is_stamped_when_the_body_leaves_it_open() -> None:
    def open_claim() -> RuleBody:
        return RuleBody(matcher=TermSetMatcher(terms=("best",)), message="m", authority=AUTHORITY)

    built = rule(
        "claim.licence.v1", category="claim", matcher_kind="term_set", severity="blocking"
    )(open_claim)()
    assert built.severity == "blocking"


def test_a_rule_with_no_severity_anywhere_is_refused() -> None:
    def no_severity() -> RuleBody:
        return RuleBody(matcher=TermSetMatcher(terms=("best",)), message="m", authority=AUTHORITY)

    built = rule("lexicon.banned_term.v1", category="lexicon", matcher_kind="term_set")(no_severity)
    with pytest.raises(RuleRegistrationError, match="declares no severity"):
        built()


def test_a_body_returning_the_wrong_matcher_kind_is_refused() -> None:
    def wrong_kind() -> RuleBody:
        return RuleBody(
            matcher=LengthMatcher(max=30), message="m", authority=AUTHORITY, severity="blocking"
        )

    built = rule("lexicon.banned_term.v1", category="lexicon", matcher_kind="term_set")(wrong_kind)
    with pytest.raises(RuleRegistrationError, match="registered for matcher kind"):
        built()


# --- matcher kinds ----------------------------------------------------------


def _prepare(matcher: object) -> object:
    return matcher


def test_a_matcher_kind_registers_its_evaluator() -> None:
    @matcher_kind("term_set", prepare=_prepare)
    def evaluate(rule_: Rule, prepared: object, target: object, ctx: LintContext) -> list[object]:
        return []

    assert MATCHER_KINDS["term_set"].applies_to == "target"
    assert kind_for(TermSetMatcher(terms=("x",))).evaluate is evaluate


def test_a_matcher_kind_must_apply_to_a_target_or_a_set() -> None:
    with pytest.raises(RuleRegistrationError, match="applies_to"):
        matcher_kind("term_set", prepare=_prepare, applies_to="sometimes")


def test_an_unregistered_matcher_kind_raises_rather_than_being_skipped() -> None:
    """A skipped matcher is a rule that passes everything."""
    MATCHER_KINDS.pop("term_set", None)
    with pytest.raises(RuleRegistrationError, match="has no evaluator"):
        kind_for(TermSetMatcher(terms=("x",)))


# --- what the compiler checks -----------------------------------------------


def a_rule(**overrides: object) -> Rule:
    payload: dict[str, object] = {
        "rule_id": "lexicon.banned_term.v1",
        "category": "lexicon",
        "severity": "warning",
        "scope": RuleScope(),
        "matcher": TermSetMatcher(terms=("cheap",)),
        "message": "m",
        "authority": AUTHORITY,
    }
    payload.update(overrides)
    return Rule.model_validate(payload)


def register_the_term_rule(*, severity: str | None = None) -> None:
    rule(
        "lexicon.banned_term.v1",
        category="lexicon",
        matcher_kind="term_set",
        severity=severity,  # type: ignore[arg-type]
    )(a_term_rule)
    matcher_kind("term_set", prepare=_prepare)(lambda r, p, t, c: [])


def test_an_unregistered_rule_cannot_enter_a_ruleset() -> None:
    register_the_term_rule()
    with pytest.raises(RuleRegistrationError, match="unregistered rule id"):
        require_registered([a_rule(rule_id="lexicon.invented.v1")])


def test_a_registered_rule_carrying_the_wrong_matcher_is_refused() -> None:
    register_the_term_rule()
    with pytest.raises(RuleRegistrationError, match="registered for"):
        require_registered([a_rule(matcher=LengthMatcher(max=30))])


def test_a_rule_whose_matcher_kind_has_no_evaluator_is_refused() -> None:
    register_the_term_rule()
    MATCHER_KINDS.pop("term_set")
    with pytest.raises(RuleRegistrationError, match="no registered evaluator"):
        require_registered([a_rule()])


def test_a_rule_contradicting_its_fixed_severity_is_refused() -> None:
    register_the_term_rule(severity="blocking")
    with pytest.raises(RuleRegistrationError, match="is always blocking"):
        require_registered([a_rule(severity="advisory")])


def test_a_well_formed_set_of_rules_passes() -> None:
    register_the_term_rule()
    require_registered([a_rule(), a_rule()])


# --- findings ---------------------------------------------------------------


def test_every_finding_carries_its_rule_s_authority() -> None:
    """PRD §9.1 item 5. A writer who is blocked can see who said so."""
    built = finding(a_rule(), "headline-1", span=(0, 5))
    assert built.authority_ref == "brand_book#p3"
    assert built.rule_id == "lexicon.banned_term.v1"
    assert built.severity == "warning"
    assert built.span == (0, 5)
    assert built.message == "m"
    assert built.indeterminate is False


def test_a_finding_can_override_the_message_with_the_measured_one() -> None:
    built = finding(a_rule(), "headline-1", message="34 characters; the limit is 30")
    assert built.message == "34 characters; the limit is 30"


def test_an_indeterminate_finding_says_so() -> None:
    """Law 31: a blocking check with no input is not a pass."""
    built = finding(a_rule(), "image-1", indeterminate=True)
    assert built.indeterminate is True


def test_the_code_version_is_stamped_and_parseable() -> None:
    assert registry.GUARDRAILS_CODE_VERSION
