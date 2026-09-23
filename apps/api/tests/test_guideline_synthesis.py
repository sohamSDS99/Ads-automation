"""The projection, and the flattening it has to be exhaustive about (node 3.6.1).

The invariants here are the ones `docs/ai-spec-s3p6.md` names. The load-bearing
one is the last class in the file: **a section that grows a rule and does not
flatten it produces a rulebook that states the rule in prose and enforces
nothing.** That failure is silent by construction — the document reads correctly,
the linter simply never applies it — so it is asserted rather than reviewed.
"""

from __future__ import annotations

import uuid

import pytest

from agent.export.guideline_contract import (
    ContentGuideline,
    GateDecision,
    HumanTaskRef,
    RegisteredClaim,
)
from agent.guardrails import compiler
from agent.guardrails.registry import RULES
from agent.guidelines.constants import load_content_constants
from agent.guidelines.synthesis import ALL_GUIDELINE_NODES, status_for
from tests.guideline_fixtures import GENERATED_AT, LEGAL_OWNER, build, node_outputs


class TestTheRulebookIsAProjection:
    def test_every_section_is_built_from_node_output(self) -> None:
        guideline = build()
        assert guideline.brand_rules.voice.voice_words == ["clear", "direct"]
        assert guideline.brand_rules.lexicon.never[0].term == "cheap"
        assert guideline.policy_profile.applicable[0].area == "healthcare"
        assert guideline.governance.owners.legal_owner_id == LEGAL_OWNER
        assert guideline.governance.review_triggers[0].pattern == "clinically proven"

    def test_a_missing_node_leaves_its_section_empty_rather_than_failing(self) -> None:
        """§4.3's degradation contract.

        A rulebook that failed to assemble because 3.3.3 found no competitors
        would be a worse answer than one whose competitor section is empty and
        says so on its face.
        """
        outputs = node_outputs()
        del outputs["3.1.1"]
        del outputs["3.3.3"]
        guideline = build(outputs=outputs)
        assert guideline.brand_rules.voice.voice_words == []
        assert guideline.policy_profile.competitor_mentions == {}

    def test_the_summary_is_the_only_prose_the_model_supplies(self) -> None:
        guideline = build(summary="A short summary.")
        assert guideline.executive_summary == "A short summary."

    def test_a_summary_over_250_words_is_refused_by_the_contract(self) -> None:
        """§12.1. A model that writes 400 words has written the document again."""
        with pytest.raises(ValueError, match="250"):
            build(summary=" ".join(["word"] * 260))

    def test_lexicon_conflicts_are_read_not_recomputed(self) -> None:
        """3.1.2 decides what conflicts; a second opinion here is two pieces of
        code disagreeing about one set."""
        outputs = node_outputs()
        outputs["3.1.2"]["conflicts"] = [{"term": "cheap"}]
        assert build(outputs=outputs).brand_rules.lexicon.conflicts == [{"term": "cheap"}]


class TestStatusIsARuleNotAJudgement:
    def test_a_rejected_gate_beats_everything(self) -> None:
        rejected = GateDecision(gate_key="G5", status="rejected")
        approved = GateDecision(gate_key="G6", status="approved")
        assert status_for([rejected, approved], blocking_issues=0, open_blocking_tasks=0) == (
            "blocked"
        )

    def test_a_blocking_issue_blocks(self) -> None:
        approved = GateDecision(gate_key="G5", status="approved")
        assert status_for([approved], blocking_issues=1, open_blocking_tasks=0) == "blocked"

    def test_an_open_publish_task_holds_it_in_draft(self) -> None:
        approved = GateDecision(gate_key="G5", status="approved")
        assert status_for([approved], blocking_issues=0, open_blocking_tasks=1) == "draft"

    def test_every_gate_approved_and_nothing_open_is_ready(self) -> None:
        rows = [
            GateDecision(gate_key="G5", status="approved"),
            GateDecision(gate_key="G6", status="approved"),
        ]
        assert status_for(rows, blocking_issues=0, open_blocking_tasks=0) == "ready_to_publish"

    def test_no_gate_opened_at_all_is_ready_not_unfinished(self) -> None:
        """3.5.1 reused a matrix and 3.1.3 had nothing to confirm.

        A legitimate run, not an incomplete one — and treating it as incomplete
        would make the reuse path unpublishable, which is the opposite of what
        `gate_conditional` exists for.
        """
        assert status_for([], blocking_issues=0, open_blocking_tasks=0) == "ready_to_publish"

    def test_a_pending_gate_is_not_ready(self) -> None:
        rows = [
            GateDecision(gate_key="G5", status="approved"),
            GateDecision(gate_key="G6", status="pending"),
        ]
        assert status_for(rows, blocking_issues=0, open_blocking_tasks=0) == "draft"


class TestTheFlatteningIsExhaustive:
    """`rules` is the only key `compiler.compile` reads."""

    def test_every_flattened_rule_id_is_registered(self) -> None:
        """`require_registered` refuses the rest at compile time. Finding that
        out at publish, after three weeks of a section enforcing nothing, is not
        the right place."""
        for rule in build().rules:
            assert rule.rule_id in RULES, f"{rule.rule_id} is not registered"

    def test_the_whole_rulebook_compiles(self) -> None:
        guideline = build()
        ruleset = compiler.compile(
            guideline.model_dump(mode="json"),
            load_content_constants(),
            (),
            compiled_at=GENERATED_AT,
        )
        assert len(ruleset.rules) == len(guideline.rules)
        assert ruleset.ruleset_version.startswith("1.0+")

    def test_compiling_twice_gives_one_hash(self) -> None:
        """§11 assertion 2, and the whole basis of a pinnable version."""
        payload = build().model_dump(mode="json")
        constants = load_content_constants()
        first = compiler.compile(payload, constants, (), compiled_at=GENERATED_AT)
        second = compiler.compile(payload, constants, (), compiled_at=GENERATED_AT)
        assert first.hash == second.hash

    def test_each_authored_section_reaches_the_rule_list(self) -> None:
        """The regression this class exists for.

        Each of these sections is authored by a different node, and each has to
        appear as an enforceable rule. A section that stopped flattening would
        still render in the PDF, so only this assertion would notice.
        """
        categories = {rule.category for rule in build().rules}
        assert {"voice", "lexicon", "offer", "asset_spec", "disclosure", "governance"} <= (
            categories
        )

    def test_severity_comes_from_the_registry_not_the_model(self) -> None:
        """A model that labelled its banned term `advisory` must not downgrade a
        rule the registry fixes as `blocking`."""
        outputs = node_outputs()
        outputs["3.2.4"]["rules"][0]["severity"] = "advisory"
        offer = next(r for r in build(outputs=outputs).rules if r.category == "offer")
        assert offer.severity == "blocking"

    def test_a_lexicon_entry_with_no_term_is_dropped_not_emitted(self) -> None:
        """A banned-term rule with no term would compile to a matcher that bans
        nothing, and read as enforced."""
        outputs = node_outputs()
        outputs["3.1.2"]["never"] = [{"term": "", "reason": "nothing"}]
        rules = build(outputs=outputs).rules
        assert not [r for r in rules if r.rule_id == "lexicon.banned_term.v1"]
        # The `always` side is untouched, which is what makes this an assertion
        # about the empty entry rather than about the section.
        assert [r for r in rules if r.rule_id == "lexicon.required_term.v1"]

    def test_a_claim_phrase_with_regex_metacharacters_is_escaped(self) -> None:
        """A claim containing `(` or `+` is ordinary marketing copy, not a pattern."""
        claim = RegisteredClaim(
            claim_id=uuid.uuid4(),
            claim_text="Save 40% (guaranteed) + free setup",
            normalized_text="save 40 guaranteed free setup",
            status="rejected",
        )
        rules = [r for r in build(claims=[claim]).rules if r.category == "policy"]
        assert rules, "a rejected claim must produce a rule naming it"
        import re

        # Compiles, and matches the literal text rather than being read as a group.
        pattern = re.compile(rules[0].matcher.pattern, re.IGNORECASE)
        assert pattern.search("Save 40% (guaranteed) + free setup")

    def test_a_non_word_leading_character_still_matches(self) -> None:
        """`\\b` before a non-word character can never match — S3-P1's trap, one
        layer up. A claim starting `#1` would silently drop out of the
        alternation while the pattern still compiled."""
        claim = RegisteredClaim(
            claim_id=uuid.uuid4(),
            claim_text="#1 in safety",
            normalized_text="1 in safety",
            status="unsupported",
        )
        rules = [r for r in build(claims=[claim]).rules if r.category == "policy"]
        import re

        assert re.compile(rules[0].matcher.pattern, re.IGNORECASE).search("We are #1 in safety")


class TestTheDependencyList:
    def test_an_open_task_becomes_an_open_dependency(self) -> None:
        """§11 assertion 8 checks this, so it is built from the tasks rather
        than from a model's idea of what is left."""
        task = HumanTaskRef(
            task_id=uuid.uuid4(),
            task_key="H1",
            status="pending",
            blocking_for="publish",
            assignee_id=LEGAL_OWNER,
            node_id="3.2.3",
        )
        guideline = build(human_tasks=[task])
        assert any(dep.source == "3.2.3" for dep in guideline.open_dependencies)
        assert all(dep.owner != "unassigned" for dep in guideline.open_dependencies)

    def test_a_completed_task_is_not_an_open_dependency(self) -> None:
        task = HumanTaskRef(
            task_id=uuid.uuid4(),
            task_key="H1",
            status="completed",
            blocking_for="publish",
            assignee_id=LEGAL_OWNER,
        )
        assert build(human_tasks=[task]).open_dependencies == []


class TestTheNodeListMatchesTheRegistry:
    def test_all_guideline_nodes_equals_every_non_report_node(self) -> None:
        """§11: `3.6.1←{all}`. A node added later must fail this test rather
        than quietly never reaching the rulebook."""
        from agent.orchestrator.registry import get_registry

        registered = {
            node_id
            for node_id in get_registry().ids
            if node_id.startswith("3.") and not node_id.startswith("3.6.")
        }
        # 3.5.3 is S3-P9 and is not registered yet; when it is, this test is
        # what tells whoever adds it to put it in `ALL_GUIDELINE_NODES`.
        assert set(ALL_GUIDELINE_NODES) == registered


def test_the_rulebook_round_trips_through_json() -> None:
    """The payload is stored as JSONB and read back by publish, the exports and
    the diff. A field that does not survive the trip is a field that silently
    changes between the run and the document."""
    guideline = build()
    again = ContentGuideline.model_validate(guideline.model_dump(mode="json"))
    assert again.model_dump(mode="json") == guideline.model_dump(mode="json")
