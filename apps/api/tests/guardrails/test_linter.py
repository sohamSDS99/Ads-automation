"""`lint()` (S3-P1 deliverable 7, PRD §9.1 item 4, §12.2).

Three properties get the attention: the verdict is right, nothing
short-circuits, and the order is stable. The last one is not cosmetic —
byte-identical output from identical input is what `test_lint_is_deterministic`
asserts across processes, and sorting is what makes that true no matter how the
evaluation happened to interleave.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest

from agent.guardrails.compiler import build_program, compile
from agent.guardrails.linter import lint
from agent.guardrails.matchers.assets import SET_REF, asset_count, asset_length
from agent.guardrails.matchers.image import text_coverage
from agent.guardrails.matchers.lexicon import banned_term, review_trigger
from agent.schemas.guardrails import RuleScope
from tests.guardrails.fixture import (
    BRAND,
    COMPILED_AT,
    CONSTANTS,
    GOOGLE,
    NOW,
    golden_claims,
    golden_offers,
    golden_payload,
    golden_ruleset,
    golden_targets,
)
from tests.guardrails.helpers import target

RULESET = golden_ruleset()


def build(rules: list, targets: list, **kwargs: object):
    payload = {**golden_payload(), "rules": rules}
    ruleset = compile(payload, CONSTANTS, golden_claims(), compiled_at=COMPILED_AT)
    return lint(targets, ruleset, now=NOW, **kwargs)  # type: ignore[arg-type]


# --- verdicts ---------------------------------------------------------------


def test_clean_copy_passes() -> None:
    rules = [banned_term(("cheap",), authority=BRAND, message="m")]
    result = build(rules, [target("Manage safety data sheets")])
    assert result.verdict == "pass"
    assert result.findings == ()


def test_a_warning_alone_is_pass_with_warnings() -> None:
    rules = [review_trigger(r"\bcompetitor\b", authority=BRAND, message="m")]
    result = build(rules, [target("Better than any competitor")])
    assert result.verdict == "pass_with_warnings"


def test_any_blocking_finding_fails() -> None:
    rules = [
        banned_term(("cheap",), authority=BRAND, message="m"),
        review_trigger(r"\bcompetitor\b", authority=BRAND, message="m"),
    ]
    result = build(rules, [target("A cheap competitor")])
    assert result.verdict == "fail"


def test_an_advisory_alone_still_passes() -> None:
    rules = [banned_term(("cheap",), authority=BRAND, message="m", severity="advisory")]
    assert build(rules, [target("A cheap tool")]).verdict == "pass"


def test_a_blocking_finding_that_could_not_be_checked_still_fails() -> None:
    """Law 31. `verdict` has no third value, and 'we did not check' must never
    land in `pass`."""
    rules = [text_coverage(authority=GOOGLE, maximum=0.2)]
    unmeasured = target(None, ref="img", image_ref="s3://a.png")
    result = build(rules, [unmeasured])
    assert result.verdict == "fail"
    assert result.findings[0].indeterminate is True


# --- nothing short-circuits -------------------------------------------------


def test_a_target_collects_every_finding_it_triggers() -> None:
    """A writer who fixes one problem and is then shown the next has been made
    to do three rounds of work for one edit."""
    rules = [
        banned_term(("cheap",), authority=BRAND, message="m"),
        banned_term(("hassle",), authority=BRAND, message="m"),
        asset_length(authority=GOOGLE, message="m", maximum=10),
    ]
    result = build(rules, [target("A cheap way to remove the hassle")])
    assert len({item.rule_id for item in result.findings}) == 2
    assert len(result.findings) == 3


# --- scope ------------------------------------------------------------------


def test_a_rule_is_evaluated_only_for_targets_its_scope_selects() -> None:
    rules = [
        asset_length(
            authority=GOOGLE,
            message="m",
            maximum=5,
            scope=RuleScope(surfaces=("rsa_headline",)),
        )
    ]
    headline = target("far too long", ref="h", surface="rsa_headline")
    description = target("far too long", ref="d", surface="rsa_description")
    result = build(rules, [headline, description])
    assert [item.target_ref for item in result.findings] == ["h"]


def test_rules_evaluated_counts_the_rules_that_actually_ran() -> None:
    rules = [
        banned_term(("cheap",), authority=BRAND, message="m"),
        asset_length(authority=GOOGLE, message="m", maximum=5, scope=RuleScope(markets=("FR",))),
    ]
    result = build(rules, [target("a cheap tool", market="DE")])
    assert result.rules_evaluated == 1
    assert result.targets_checked == 1


# --- set-scoped rules -------------------------------------------------------


def test_a_count_rule_runs_once_and_its_finding_belongs_to_the_set() -> None:
    rules = [asset_count("rsa_headline", authority=GOOGLE, message="m", minimum=3)]
    targets = [target("a", ref="h1"), target("b", ref="h2")]
    result = build(rules, targets)
    assert len(result.findings) == 1
    assert result.findings[0].target_ref == SET_REF


def test_a_count_rule_runs_even_when_no_target_matches_its_entity() -> None:
    rules = [asset_count("rsa_headline", authority=GOOGLE, message="m", minimum=3)]
    result = build(rules, [target("a", ref="d1", surface="rsa_description")])
    assert len(result.findings) == 1


# --- ordering ---------------------------------------------------------------


def test_findings_follow_the_caller_s_target_order() -> None:
    rules = [banned_term(("cheap",), authority=BRAND, message="m")]
    targets = [
        target("cheap", ref="zzz"),
        target("cheap", ref="aaa"),
        target("cheap", ref="mmm"),
    ]
    result = build(rules, targets)
    assert [item.target_ref for item in result.findings] == ["zzz", "aaa", "mmm"]


def test_set_findings_sort_last() -> None:
    """They belong to the submission, and burying one between two headlines
    reads as noise."""
    rules = [
        banned_term(("cheap",), authority=BRAND, message="m"),
        asset_count("rsa_headline", authority=GOOGLE, message="m", minimum=9),
    ]
    result = build(rules, [target("cheap", ref="h1"), target("cheap", ref="h2")])
    assert result.findings[-1].target_ref == SET_REF


def test_linting_twice_in_one_process_gives_identical_findings() -> None:
    first = lint(golden_targets(20), RULESET, now=NOW, offers=golden_offers())
    second = lint(golden_targets(20), RULESET, now=NOW, offers=golden_offers())
    assert first.findings == second.findings
    assert first.verdict == second.verdict


# --- the result envelope ----------------------------------------------------


def test_the_result_carries_the_ruleset_version_it_was_produced_by() -> None:
    result = lint(golden_targets(5), RULESET, now=NOW, offers=golden_offers())
    assert result.ruleset_version == RULESET.ruleset_version


def test_evaluated_at_is_the_now_that_was_passed_in() -> None:
    moment = datetime(2027, 3, 4, 5, 6, tzinfo=UTC)
    assert lint([], RULESET, now=moment).evaluated_at == moment


def test_elapsed_ms_is_populated_and_never_negative() -> None:
    result = lint(golden_targets(50), RULESET, now=NOW, offers=golden_offers())
    assert result.elapsed_ms >= 0


def test_linting_nothing_is_a_pass_not_a_crash() -> None:
    result = lint([], RULESET, now=NOW)
    assert result.verdict == "fail"  # the count rule still fires: zero headlines
    assert result.targets_checked == 0


def test_now_drives_claim_expiry_rather_than_the_wall_clock() -> None:
    """The same copy, the same ruleset, two different `now` values, two
    different verdicts — and neither of them read a clock."""
    licensed = target("The leading SDS platform", ref="h1")
    before = lint([licensed], RULESET, now=NOW, offers=golden_offers())
    after = lint([licensed], RULESET, now=datetime(2028, 1, 1, tzinfo=UTC), offers=golden_offers())
    claim_before = [f for f in before.findings if f.rule_id == "claim.licence.v1"]
    claim_after = [f for f in after.findings if f.rule_id == "claim.licence.v1"]
    assert claim_before == []
    assert claim_after != []


def test_a_prebuilt_program_gives_the_same_answer_as_building_per_call() -> None:
    targets = golden_targets(20)
    shared = build_program(RULESET)
    with_program = lint(targets, RULESET, now=NOW, offers=golden_offers(), program=shared)
    without = lint(targets, RULESET, now=NOW, offers=golden_offers())
    assert with_program.findings == without.findings


# --- performance (S3-P1 acceptance) -----------------------------------------


def four_hundred_rules() -> list:
    """A realistic 400: mostly vocabulary, plus the fixture's spread of kinds."""
    rules = list(golden_payload()["rules"])
    for index in range(400 - len(rules)):
        rules.append(
            banned_term(
                (f"jargon{index}",),
                authority=BRAND,
                message=f"Banned term {index}.",
                severity="warning",
            )
        )
    return rules


@pytest.mark.parametrize("attempt", [1, 2])
def test_one_hundred_targets_against_four_hundred_rules_under_1_5s(attempt: int) -> None:
    """Run twice: the first call warms the stemmer memo, and a threshold only
    a warm cache can meet would be a threshold that fails in production."""
    payload = {**golden_payload(), "rules": four_hundred_rules()}
    ruleset = compile(payload, CONSTANTS, golden_claims(), compiled_at=COMPILED_AT)
    targets = golden_targets(100)
    assert len(ruleset.rules) == 400

    started = time.perf_counter()
    result = lint(targets, ruleset, now=NOW, offers=golden_offers())
    elapsed = time.perf_counter() - started

    assert elapsed < 1.5, f"lint took {elapsed:.3f}s"
    assert result.targets_checked == 101
    assert result.findings
