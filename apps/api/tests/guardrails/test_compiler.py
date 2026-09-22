"""Compilation (S3-P1 deliverable 6, PRD §9.1 item 3, law 26).

The hash is the whole subject. It is the claim that two compilations produced
the same rules, it is what a published `RuleSet` row is keyed by, and it is
what an asset pins so it can be audited a year later against the rules that
actually applied. So the tests here are mostly about what moves it and what
must not.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from tests.guardrails.fixture import (
    BRAND,
    COMPILED_AT,
    CONSTANTS,
    golden_claims,
    golden_payload,
    golden_ruleset,
)

from agent.guardrails.compiler import (
    CompileError,
    build_program,
    canonical,
    compile,
    compiler_version,
)
from agent.guardrails.matchers.lexicon import banned_term
from agent.guardrails.registry import RuleRegistrationError
from agent.schemas.guardrails import RegexMatcher, Rule


def build(**payload_overrides: object):
    payload = {**golden_payload(), **payload_overrides}
    return compile(payload, CONSTANTS, golden_claims(), compiled_at=COMPILED_AT)


# --- what the hash covers ---------------------------------------------------


def test_compiling_the_same_guideline_twice_gives_the_same_hash() -> None:
    assert build().hash == build().hash


def test_the_hash_ignores_when_it_was_compiled() -> None:
    """Two compiles of an unchanged guideline a minute apart are the same
    ruleset. A hash that said otherwise would mint a new version every time
    somebody pressed the button and strand every asset pinned to the old one."""
    early = compile(
        golden_payload(), CONSTANTS, golden_claims(), compiled_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    late = compile(
        golden_payload(), CONSTANTS, golden_claims(), compiled_at=datetime(2026, 9, 22, tzinfo=UTC)
    )
    assert early.hash == late.hash
    assert early.compiled_at != late.compiled_at


def test_changing_a_rule_moves_the_hash() -> None:
    extra = [*golden_payload()["rules"], banned_term(("tedious",), authority=BRAND, message="m")]
    assert build(rules=extra).hash != build().hash


def test_changing_a_claim_moves_the_hash() -> None:
    """A claim that expired licenses different copy, so it is part of the
    ruleset's meaning even though it is not a rule."""
    claims = golden_claims()
    changed = claims[0].model_copy(update={"expires_at": datetime(2030, 1, 1, tzinfo=UTC)})
    other = compile(golden_payload(), CONSTANTS, [changed], compiled_at=COMPILED_AT)
    assert other.hash != build().hash


def test_changing_the_constants_version_moves_the_hash() -> None:
    overridden = CONSTANTS.merged({"claims.match_threshold": 0.95})
    other = compile(golden_payload(), overridden, golden_claims(), compiled_at=COMPILED_AT)
    assert other.hash != build().hash
    assert other.constants_version != build().constants_version


def test_reordering_the_rules_in_the_payload_does_not_move_the_hash() -> None:
    """Rules are sorted before hashing, so the order a synthesis node happened
    to emit them in is not part of the ruleset's identity."""
    reversed_rules = list(reversed(golden_payload()["rules"]))
    assert build(rules=reversed_rules).hash == build().hash


def test_the_version_string_carries_the_hash_prefix() -> None:
    ruleset = build()
    assert ruleset.ruleset_version == f"2.3+{ruleset.hash[:8]}"


def test_the_compiler_version_pins_the_language_libraries() -> None:
    """A simplemma upgrade can change what `lemma` resolves to, which changes
    verdicts under a ruleset whose hash never moved."""
    assert "snowball/" in compiler_version()
    assert "simplemma/" in compiler_version()
    assert build().compiler_version == compiler_version()


# --- ordering ---------------------------------------------------------------


def test_rules_come_out_sorted_by_id() -> None:
    ids = [rule.rule_id for rule in build().rules]
    assert ids == sorted(ids)


def test_two_rules_sharing_an_id_are_ordered_stably() -> None:
    """A brand can legitimately ban two different term sets, so the id alone is
    not a total order and an unstable tiebreak would move the hash."""
    ruleset = build()
    banned = [rule for rule in ruleset.rules if rule.rule_id == "lexicon.banned_term.v1"]
    assert len(banned) == 2
    assert build().rules == ruleset.rules


def test_detectors_and_claims_are_sorted_too() -> None:
    ruleset = build()
    assert [d.detector_id for d in ruleset.detectors] == sorted(
        d.detector_id for d in ruleset.detectors
    )


# --- what compilation refuses -----------------------------------------------


def test_an_unregistered_rule_cannot_be_compiled() -> None:
    """Law 22, at the only place it can be enforced before persistence."""
    invented = Rule.model_validate(
        {
            "rule_id": "invented.rule.v1",
            "category": "lexicon",
            "severity": "blocking",
            "matcher": RegexMatcher(pattern="x"),
            "message": "m",
            "authority": {
                "source": "internal",
                "reference": "nowhere",
                "reviewed_at": date(2026, 9, 22),
            },
        }
    )
    with pytest.raises(RuleRegistrationError, match="unregistered rule id"):
        build(rules=[invented])


def test_a_rule_nobody_can_evaluate_is_refused_at_compile_not_at_lint() -> None:
    """A broken pattern must fail when the rulebook is built, not later in
    front of a writer."""
    broken = Rule.model_validate(
        {
            "rule_id": "governance.review_trigger.v1",
            "category": "governance",
            "severity": "warning",
            "matcher": RegexMatcher(pattern="(unclosed"),
            "message": "m",
            "authority": {
                "source": "brand",
                "reference": "bb",
                "reviewed_at": date(2026, 9, 22),
            },
        }
    )
    with pytest.raises(CompileError, match="not a valid pattern"):
        build(rules=[broken])


@pytest.mark.parametrize("missing", ["project_id", "guideline_id"])
def test_a_payload_missing_its_identity_is_refused(missing: str) -> None:
    payload = golden_payload()
    del payload[missing]
    with pytest.raises(CompileError, match=missing):
        compile(payload, CONSTANTS, golden_claims(), compiled_at=COMPILED_AT)


def test_a_payload_with_no_rules_list_is_refused() -> None:
    payload = golden_payload()
    del payload["rules"]
    with pytest.raises(CompileError, match="no `rules` list"):
        compile(payload, CONSTANTS, golden_claims(), compiled_at=COMPILED_AT)


def test_a_malformed_rule_names_its_index() -> None:
    with pytest.raises(CompileError, match=r"rules\[1\]"):
        build(rules=[*golden_payload()["rules"][:1], {"rule_id": "nope"}])


def test_a_non_integer_version_is_refused() -> None:
    with pytest.raises(CompileError, match="version_major"):
        build(version_major="two")


# --- canonical rendering ----------------------------------------------------


def test_an_integral_float_and_an_int_are_one_value() -> None:
    """A ruleset round-trips through JSON; a threshold that came back as a
    float must not read as a different threshold."""
    assert canonical({"a": 1.0}) == canonical({"a": 1})


def test_canonical_sorts_keys_at_every_depth() -> None:
    assert list(canonical({"b": {"d": 1, "c": 2}, "a": 3})) == ["a", "b"]
    assert list(canonical({"b": {"d": 1, "c": 2}, "a": 3})["b"]) == ["c", "d"]


# --- the compiled program ---------------------------------------------------


def test_compiling_builds_every_matcher_and_splits_set_rules_out() -> None:
    program = build_program(golden_ruleset())
    assert program.per_set, "the count rule should be set-scoped"
    assert all(entry.kind.applies_to == "set" for entry in program.per_set)
    assert all(entry.kind.applies_to == "target" for entry in program.per_target)
    assert len(program.per_set) + len(program.per_target) == len(golden_ruleset().rules)


def test_every_shipped_matcher_kind_is_exercised_by_the_fixture() -> None:
    """A fixture that never built a `ratio` matcher would not prove the
    compiler can build one."""
    kinds = {rule.matcher.kind for rule in golden_ruleset().rules}
    from agent.guardrails.registry import MATCHER_KINDS

    assert kinds == set(MATCHER_KINDS)


def test_the_asset_specs_and_detectors_ride_along() -> None:
    ruleset = golden_ruleset()
    assert ruleset.asset_specs.for_campaign("search")["headline"].max_chars == 30
    assert ruleset.detectors
    assert ruleset.disclosure_requirements
    assert ruleset.logo_templates
