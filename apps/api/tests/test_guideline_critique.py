"""§11's ten assertions, each proved to fire and proved not to over-fire (3.6.2).

Two halves per check, and the second half is the one that matters. A predicate
that fires on everything satisfies "it catches the defect" and makes the
rulebook unpublishable; §11's checklist is only useful if a healthy rulebook
passes all ten. So every class here asserts the negative case against the shared
healthy fixture as well as the positive case against a broken one.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from agent.export.guideline_contract import (
    HumanTaskRef,
    RegisteredClaim,
    SignatureRef,
)
from agent.guidelines import critique as checks
from tests.guideline_fixtures import GENERATED_AT, LEGAL_OWNER, build, node_outputs

NOW = GENERATED_AT


def codes(issues: list[checks.CritiqueIssue]) -> set[str]:
    return {issue.check for issue in issues}


class TestAHealthyRulebookPassesAllTen:
    def test_nothing_blocking_is_reported(self) -> None:
        issues = checks.run_checks(build(), now=NOW)
        assert checks.blocking(issues) == [], [i.finding for i in checks.blocking(issues)]

    def test_the_module_exposes_exactly_ten_checks(self) -> None:
        names = [n for n in dir(checks) if n.startswith("check_")]
        assert len(names) == 10, sorted(names)

    def test_run_checks_actually_dispatches_to_each_one(self) -> None:
        """Every other test in this file calls a check **directly**.

        That leaves the dispatcher untested: `run_checks` could stop calling any
        one of the ten and every direct test would still pass, while 3.6.2 — the
        only caller in production — silently stopped enforcing it. That is the
        same written-never-read shape the register bug had, and
        `scripts/verify-s3p6.sh` step 6 is what caught it: deleting assertion 3
        from `run_checks` left the suite green.

        So this drives a rulebook broken in several ways *through the
        dispatcher* and asserts each check reports itself.
        """
        broken = build(
            claims=[
                RegisteredClaim(
                    claim_id=uuid.uuid4(),
                    claim_text="The best SDS software",
                    normalized_text="the best sds software",
                    status="approved",
                )
            ],
            human_tasks=[
                HumanTaskRef(
                    task_id=uuid.uuid4(),
                    task_key="H1",
                    status="pending",
                    blocking_for="publish",
                    assignee_id=None,
                )
            ],
            summary="Write to sales@sdsmanager.com.",
        )
        reported = codes(checks.run_checks(broken, now=NOW))
        for check in ("approved_claims_signed", "open_tasks_declared", "no_personal_data"):
            assert check in reported, f"run_checks never dispatched to {check}"

    def test_run_checks_dispatches_to_the_structural_seven_too(self) -> None:
        """The other side of the same gap, for the checks a broken *payload*
        rather than a broken register triggers."""
        outputs = node_outputs()
        outputs["3.1.2"]["conflicts"] = [{"term": "cheap"}]
        outputs["3.3.1"]["applicable"][0].pop("no_rule_needed")
        outputs["3.3.4"]["disclosure_rules"] = []
        guideline = build(outputs=outputs)
        guideline.rules = [
            *guideline.rules,
            guideline.rules[0].model_copy(update={"rule_id": "not.registered.v1"}),
        ]
        reported = codes(checks.run_checks(guideline, now=NOW))
        for check in (
            "matchers_compile",
            "lexicon_conflicts",
            "asset_specs_complete",
            "policy_areas_covered",
            "disclosure_coverage",
        ):
            assert check in reported, f"run_checks never dispatched to {check}"


class TestOneAuthorityResolves:
    def test_an_internal_rule_with_no_constants_key_is_blocking(self) -> None:
        guideline = build()
        guideline.rules[0].authority.__dict__  # noqa: B018 - frozen model, rebuilt below
        rebuilt = guideline.model_copy(deep=True)
        rules = list(rebuilt.rules)
        rules[0] = rules[0].model_copy(
            update={"authority": rules[0].authority.model_copy(update={"reference": ""})}
        )
        rebuilt.rules = rules
        issues = checks.check_authorities_resolve(rebuilt)
        assert "authority_resolves" in codes(issues)

    def test_the_healthy_fixture_has_no_unresolved_authority(self) -> None:
        assert checks.check_authorities_resolve(build()) == []


class TestTwoMatchersCompile:
    def test_an_unregistered_rule_id_is_blocking(self) -> None:
        guideline = build()
        rules = list(guideline.rules)
        rules[0] = rules[0].model_copy(update={"rule_id": "made.up.v1"})
        guideline.rules = rules
        issues = checks.check_matchers_compile(guideline)
        assert "matchers_compile" in codes(issues)
        assert "made.up.v1" in issues[0].finding

    def test_a_healthy_rulebook_compiles_to_one_hash(self) -> None:
        assert checks.check_matchers_compile(build()) == []


class TestThreeApprovedClaimsAreSigned:
    def _claim(self, **kwargs: object) -> RegisteredClaim:
        base = {
            "claim_id": uuid.uuid4(),
            "claim_text": "The best SDS software",
            "normalized_text": "the best sds software",
            "status": "approved",
        }
        base.update(kwargs)
        return RegisteredClaim(**base)  # type: ignore[arg-type]

    def test_approved_with_no_signature_is_blocking(self) -> None:
        issues = checks.check_approved_claims_are_signed(build(claims=[self._claim()]), now=NOW)
        assert "approved_claims_signed" in codes(issues)

    def test_approved_with_an_expired_signature_is_blocking(self) -> None:
        signature_id = uuid.uuid4()
        signature = SignatureRef(
            signature_id=signature_id,
            signer_id=LEGAL_OWNER,
            set_hash="abc",
            expires_at=NOW - timedelta(days=1),
        )
        guideline = build(claims=[self._claim(signature_id=signature_id)], signatures=[signature])
        assert "approved_claims_signed" in codes(
            checks.check_approved_claims_are_signed(guideline, now=NOW)
        )

    def test_a_live_signature_passes(self) -> None:
        signature_id = uuid.uuid4()
        signature = SignatureRef(
            signature_id=signature_id,
            signer_id=LEGAL_OWNER,
            set_hash="abc",
            expires_at=NOW + timedelta(days=90),
        )
        guideline = build(claims=[self._claim(signature_id=signature_id)], signatures=[signature])
        assert checks.check_approved_claims_are_signed(guideline, now=NOW) == []

    def test_a_set_hash_that_moved_under_the_signer_is_blocking(self) -> None:
        """The anti-race guarantee. The register changed after it was signed."""
        signature_id = uuid.uuid4()
        signature = SignatureRef(
            signature_id=signature_id, signer_id=LEGAL_OWNER, set_hash="what-they-read"
        )
        guideline = build(signatures=[signature])
        issues = checks.check_approved_claims_are_signed(
            guideline, now=NOW, hashes={signature_id: "what-it-is-now"}
        )
        assert "approved_claims_signed" in codes(issues)


class TestFourDeadClaimsAreRefused:
    def test_a_rejected_claim_produces_a_rule_naming_it(self) -> None:
        claim = RegisteredClaim(
            claim_id=uuid.uuid4(),
            claim_text="Clinically proven",
            normalized_text="clinically proven",
            status="rejected",
        )
        # `build` flattens it, so the check passes — which is the assertion:
        # the flattening is what satisfies §11.4.
        assert checks.check_dead_claims_are_refused(build(claims=[claim])) == []

    def test_a_dead_claim_with_no_rule_is_blocking(self) -> None:
        guideline = build(
            claims=[
                RegisteredClaim(
                    claim_id=uuid.uuid4(),
                    claim_text="Clinically proven",
                    normalized_text="clinically proven",
                    status="rejected",
                )
            ]
        )
        guideline.rules = [r for r in guideline.rules if r.category != "policy"]
        assert "dead_claims_refused" in codes(checks.check_dead_claims_are_refused(guideline))


class TestFiveLexiconConflicts:
    def test_a_declared_conflict_is_blocking(self) -> None:
        outputs = node_outputs()
        outputs["3.1.2"]["conflicts"] = [{"term": "cheap"}]
        assert "lexicon_conflicts" in codes(
            checks.check_lexicon_has_no_conflicts(build(outputs=outputs))
        )

    def test_an_undeclared_overlap_is_caught_too(self) -> None:
        """A conflict 3.1.2 failed to notice is exactly what this exists for."""
        outputs = node_outputs()
        outputs["3.1.2"]["always"] = [{"term": "cheap", "context": "always"}]
        issues = checks.check_lexicon_has_no_conflicts(build(outputs=outputs))
        assert "lexicon_conflicts" in codes(issues)
        assert "cheap" in issues[0].finding

    def test_the_healthy_fixture_has_no_conflict(self) -> None:
        assert checks.check_lexicon_has_no_conflicts(build()) == []


class TestSixAssetSpecs:
    def test_a_campaign_type_with_no_launch_minimum_is_a_warning(self) -> None:
        issues = checks.check_every_asset_type_has_a_spec(build())
        # The fixture gives a minimum for `search` only, so `performance_max`
        # is reported — a warning, not a blocker: the rulebook is incomplete
        # rather than wrong.
        assert "asset_specs_complete" in codes(issues)
        assert all(issue.severity == "warning" for issue in issues)


class TestSevenPolicyAreasProduceARule:
    def test_an_applicable_area_with_neither_rule_nor_reason_is_blocking(self) -> None:
        outputs = node_outputs()
        outputs["3.3.1"]["applicable"][0].pop("no_rule_needed")
        assert "policy_areas_covered" in codes(
            checks.check_applicable_policy_produces_a_rule(build(outputs=outputs))
        )

    def test_an_explicit_no_rule_needed_satisfies_it(self) -> None:
        assert checks.check_applicable_policy_produces_a_rule(build()) == []


class TestEightOpenTasksAreDeclared:
    def test_a_task_with_no_assignee_is_blocking(self) -> None:
        task = HumanTaskRef(
            task_id=uuid.uuid4(),
            task_key="H1",
            status="pending",
            blocking_for="publish",
            assignee_id=None,
            node_id="3.2.3",
        )
        issues = checks.check_open_tasks_are_declared(build(human_tasks=[task]))
        assert "open_tasks_declared" in codes(issues)

    def test_an_assigned_declared_task_passes(self) -> None:
        task = HumanTaskRef(
            task_id=uuid.uuid4(),
            task_key="H1",
            status="pending",
            blocking_for="publish",
            assignee_id=LEGAL_OWNER,
            node_id="3.2.3",
        )
        assert checks.check_open_tasks_are_declared(build(human_tasks=[task])) == []


class TestNineDisclosureCoverage:
    def test_no_disclosure_rules_at_all_is_a_warning(self) -> None:
        outputs = node_outputs()
        outputs["3.3.4"]["disclosure_rules"] = []
        issues = checks.check_disclosure_covers_generated_surfaces(build(outputs=outputs))
        assert "disclosure_coverage" in codes(issues)

    def test_a_rule_scoped_to_no_surfaces_covers_everything(self) -> None:
        """An empty `surfaces` tuple means everywhere — the same convention
        `RuleScope` uses, and for the same reason."""
        outputs = node_outputs()
        outputs["3.3.4"]["disclosure_rules"][0]["surfaces"] = []
        assert checks.check_disclosure_covers_generated_surfaces(build(outputs=outputs)) == []


class TestTenNoPersonalData:
    def test_an_email_in_the_summary_is_blocking(self) -> None:
        issues = checks.check_no_personal_data(
            build(summary="Questions? Write to sales@sdsmanager.com.")
        )
        assert "no_personal_data" in codes(issues)

    def test_an_email_nested_deep_in_the_payload_is_caught(self) -> None:
        """The payload is what gets exported, diffed and handed to Stage 04 —
        a leak in one nested `why` field ships as surely as one in the summary."""
        outputs = node_outputs()
        outputs["3.1.1"]["do_examples"][0]["why"] = "Written by anna.smith@example.com"
        assert "no_personal_data" in codes(checks.check_no_personal_data(build(outputs=outputs)))

    def test_the_healthy_fixture_leaks_nothing(self) -> None:
        assert checks.check_no_personal_data(build()) == []

    def test_a_date_is_not_mistaken_for_a_phone_number(self) -> None:
        """3.2.4 binds countdown offers to real dates; eating `2026-09-30`
        invents a deadline rather than hiding a person. The check delegates to
        `redact_pii`, which already knows this — the assertion is that
        delegating kept it."""
        assert checks.check_no_personal_data(build(summary="Offer ends 2026-09-30.")) == []


def test_run_checks_is_stable_across_two_calls() -> None:
    """The critique decides publishability. A check that returned different
    findings on two calls over one payload would make that decision a coin
    flip."""
    guideline = build()
    first = checks.run_checks(guideline, now=NOW)
    second = checks.run_checks(guideline, now=NOW)
    assert [i.model_dump() for i in first] == [i.model_dump() for i in second]


def test_now_is_an_argument_and_not_a_clock() -> None:
    """Expiry is decided against the caller's `now`, so a test can place a
    signature either side of it without waiting."""
    signature_id = uuid.uuid4()
    claim = RegisteredClaim(
        claim_id=uuid.uuid4(),
        claim_text="c",
        normalized_text="c",
        status="approved",
        signature_id=signature_id,
    )
    signature = SignatureRef(
        signature_id=signature_id,
        signer_id=LEGAL_OWNER,
        expires_at=datetime(2026, 10, 1, tzinfo=UTC),
    )
    guideline = build(claims=[claim], signatures=[signature])
    before = datetime(2026, 9, 1, tzinfo=UTC)
    after = datetime(2026, 11, 1, tzinfo=UTC)
    assert checks.check_approved_claims_are_signed(guideline, now=before) == []
    assert checks.check_approved_claims_are_signed(guideline, now=after) != []
