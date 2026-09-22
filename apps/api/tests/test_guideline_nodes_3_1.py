"""Stage 3.1 — brand rules. Nodes 3.1.1 `voice_profile` and 3.1.2 `lexicon_rules`.

Two invariants carry this whole stage, and neither can be a line in a prompt.

**3.1.1's examples are quoted, never invented** (§11). A model told not to invent
still invents, so every `do_example` is checked against the corpus the node
actually gathered and a fabricated one fails the node.

**3.1.2's entries are machine-checkable** (§11). "Every entry compiles to a
matcher" is the difference between a rulebook and a wish: an entry that cannot
become a `TermSetMatcher` enforces nothing at lint time, so it is moved into
`conflicts[]` with a reason rather than published as a rule.
"""

from __future__ import annotations

import pytest

from agent.nodes.base import NodeContractError
from agent.nodes.content.stage_3_1 import lexicon_rules, voice_profile
from tests.guideline_support import evidence, harness

BEST = "Find any safety data sheet in seconds."
WORST = "Cheap SDS software, best prices!!!"


def corpus() -> list:
    return [
        evidence("creative_history", BEST, {"performance_label": "BEST"}),
        evidence("creative_history", WORST, {"performance_label": "LOW"}),
        evidence("site_pages", "We replace the binder with a searchable library."),
    ]


def voice_answer(**overrides) -> dict:
    answer = {
        "voice_words": ["plain", "exact", "calm"],
        "definition_per_word": {
            "plain": "short words",
            "exact": "a number or nothing",
            "calm": "no exclamation marks",
        },
        "do_examples": [{"text": BEST, "source_ref": "creative_history", "why": "concrete"}],
        "dont_examples": [
            {"text": WORST, "rewritten_as": "SDS software priced per site.", "why": "shouts"}
        ],
        "register": {"formality": "professional", "person": "second", "tense": "present"},
        "readability_targets": {"max_sentence_words": 18, "max_syllables_per_word": 3},
    }
    answer.update(overrides)
    return answer


class TestVoiceProfile:
    async def test_it_profiles_voice_from_the_corpus(self) -> None:
        rows = corpus()
        h = harness("3.1.1", answers={"VoiceDraft": voice_answer()})

        out = await voice_profile.reason(h.ctx, rows)

        assert out.voice_words == ["plain", "exact", "calm"]
        assert out.do_examples[0].text == BEST

    async def test_an_invented_do_example_fails_the_node(self) -> None:
        """§11: examples are quoted from real best-performing copy, never invented."""
        rows = corpus()
        h = harness(
            "3.1.1",
            answers={
                "VoiceDraft": voice_answer(
                    do_examples=[
                        {
                            "text": "The world's most loved SDS platform.",
                            "source_ref": "creative_history",
                            "why": "aspirational",
                        }
                    ]
                )
            },
        )

        with pytest.raises(NodeContractError, match="not quoted"):
            await voice_profile.reason(h.ctx, rows)

    async def test_a_dont_example_must_also_be_real_copy(self) -> None:
        """A rewrite is ours. The thing being rewritten has to be theirs."""
        rows = corpus()
        h = harness(
            "3.1.1",
            answers={
                "VoiceDraft": voice_answer(
                    dont_examples=[
                        {
                            "text": "Nobody ever wrote this.",
                            "rewritten_as": "x",
                            "why": "invented",
                        }
                    ]
                )
            },
        )

        with pytest.raises(NodeContractError, match="not quoted"):
            await voice_profile.reason(h.ctx, rows)

    async def test_a_rewrite_does_not_have_to_appear_in_the_corpus(self) -> None:
        """`rewritten_as` is the one field the model is supposed to author."""
        rows = corpus()
        h = harness("3.1.1", answers={"VoiceDraft": voice_answer()})

        out = await voice_profile.reason(h.ctx, rows)

        assert out.dont_examples[0].rewritten_as == "SDS software priced per site."

    async def test_personal_data_in_creative_history_never_reaches_the_prompt(self) -> None:
        """Law 30, at the point the data enters."""
        rows = [
            *corpus(),
            evidence("creative_history", "Call 0800 123 4567 or email ops@sdsmanager.com"),
        ]
        h = harness("3.1.1", answers={"VoiceDraft": voice_answer()})

        await voice_profile.reason(h.ctx, rows)
        prompt = h.llm.every_prompt()

        assert "0800 123 4567" not in prompt
        assert "ops@sdsmanager.com" not in prompt

    async def test_it_runs_with_no_creative_history_at_all(self) -> None:
        """§10.3: no connected account means site copy alone, and the run continues."""
        rows = [evidence("site_pages", "We replace the binder with a searchable library.")]
        h = harness(
            "3.1.1",
            answers={
                "VoiceDraft": voice_answer(
                    do_examples=[
                        {
                            "text": "We replace the binder with a searchable library.",
                            "source_ref": "site_pages",
                            "why": "plain",
                        }
                    ],
                    # The default don't-example quotes an ad, and this corpus has
                    # none — the point of the test is that site copy alone is
                    # enough, not that a quote may come from nowhere.
                    dont_examples=[],
                )
            },
        )

        out = await voice_profile.reason(h.ctx, rows)

        assert out.input_mode == "unbound"

    async def test_it_reports_bound_mode_when_creative_history_exists(self) -> None:
        h = harness("3.1.1", answers={"VoiceDraft": voice_answer()})

        out = await voice_profile.reason(h.ctx, corpus())

        assert out.input_mode == "bound"


def lexicon_answer(**overrides) -> dict:
    answer = {
        "always": [
            {
                "term": "Safety Data Sheet",
                "surface_forms": ["Safety Data Sheet", "SDS"],
                "context": "first mention in a headline",
                "locale": "en",
                "severity": "warning",
            }
        ],
        "never": [
            {
                "term": "cheap",
                "surface_forms": ["cheap", "cheapest"],
                "reason": "positions on price, not on compliance",
                "locale": "en",
                "severity": "blocking",
                "suggested_replacement": "good value",
            }
        ],
        "case_and_spelling": [{"canonical": "SDS Manager", "variants": ["sds manager", "SDSM"]}],
    }
    answer.update(overrides)
    return answer


class TestLexicon:
    async def test_every_never_entry_compiles_to_a_forbidding_matcher(self) -> None:
        h = harness("3.1.2", answers={"LexiconDraft": lexicon_answer()})

        out = await lexicon_rules.reason(h.ctx, [])
        matcher = lexicon_rules.matcher_for(out.never[0], mode="forbid")

        assert matcher.kind == "term_set"
        assert matcher.mode == "forbid"
        assert "cheapest" in matcher.terms

    async def test_every_always_entry_compiles_to_a_requiring_matcher(self) -> None:
        h = harness("3.1.2", answers={"LexiconDraft": lexicon_answer()})

        out = await lexicon_rules.reason(h.ctx, [])
        matcher = lexicon_rules.matcher_for(out.always[0], mode="require")

        assert matcher.mode == "require"

    async def test_an_entry_with_no_usable_surface_form_becomes_a_conflict(self) -> None:
        """An entry that enforces nothing is not published as if it enforced something."""
        h = harness(
            "3.1.2",
            answers={
                "LexiconDraft": lexicon_answer(
                    never=[
                        {
                            "term": "   ",
                            "surface_forms": ["  ", ""],
                            "reason": "empty",
                            "locale": "en",
                            "severity": "blocking",
                        }
                    ]
                )
            },
        )

        out = await lexicon_rules.reason(h.ctx, [])

        assert out.never == []
        assert len(out.conflicts) == 1
        assert "compile" in out.conflicts[0].why

    async def test_a_term_that_is_both_always_and_never_becomes_a_conflict(self) -> None:
        """§11: no term is both in the same (market, language, surface) scope."""
        h = harness(
            "3.1.2",
            answers={
                "LexiconDraft": lexicon_answer(
                    always=[
                        {
                            "term": "value",
                            "surface_forms": ["value"],
                            "context": "always say value",
                            "locale": "en",
                            "severity": "warning",
                        }
                    ],
                    never=[
                        {
                            "term": "value",
                            "surface_forms": ["value"],
                            "reason": "never say value",
                            "locale": "en",
                            "severity": "blocking",
                        }
                    ],
                )
            },
        )

        out = await lexicon_rules.reason(h.ctx, [])

        assert len(out.conflicts) == 1
        assert out.conflicts[0].term == "value"

    async def test_every_published_entry_really_does_compile(self) -> None:
        """The §21 exit criterion, asserted over whatever the node emitted."""
        h = harness("3.1.2", answers={"LexiconDraft": lexicon_answer()})

        out = await lexicon_rules.reason(h.ctx, [])

        for entry in out.always:
            assert lexicon_rules.matcher_for(entry, mode="require") is not None
        for entry in out.never:
            assert lexicon_rules.matcher_for(entry, mode="forbid") is not None


class TestSpecs:
    def test_3_1_2_depends_on_the_voice_profile(self) -> None:
        assert lexicon_rules.spec.depends_on == ("3.1.1",)

    def test_neither_node_is_a_gate(self) -> None:
        assert voice_profile.spec.gate is False
        assert lexicon_rules.spec.gate is False


def visual_answer(**overrides) -> dict:
    answer = {
        "logo": {
            "clear_space_ratio": 1.0,
            "min_width_px": 120,
            "permitted_variants": ["full colour", "mono"],
            "forbidden_treatments": ["stretch", "recolour"],
        },
        "colour": {
            "tokens": [{"name": "Signal orange", "hex": "#c2410c", "role": "accent"}],
            "pairs_meeting_contrast": [],
            "forbidden_pairs": [],
        },
        "imagery": {
            "permitted_subjects": ["a real workplace"],
            "forbidden_subjects": ["stock handshakes"],
            "treatment_notes": [],
            "stock_policy": "licensed only",
        },
    }
    answer.update(overrides)
    return answer


def brand_book_rows() -> list:
    return [
        evidence(
            "brand_book_span",
            "Clear space around the logo is 1x its cap height.",
            {"page": 4, "bbox": [72, 600, 300, 612]},
        ),
        evidence("brand_book_colour", "#c2410c", {"page": 4, "hex": "#c2410c"}),
        evidence("brand_book_asset", "brand asset 400x120 on page 4", {"page": 4}),
    ]


class TestVisualIdentity:
    async def test_it_derives_rules_from_the_brand_book(self) -> None:
        from agent.nodes.content.stage_3_1 import visual_identity_rules

        h = harness("3.1.3", answers={"VisualDraft": visual_answer()})

        out = await visual_identity_rules.reason(h.ctx, brand_book_rows())

        assert out.logo.clear_space_ratio == 1.0
        assert out.colour.tokens[0].hex == "#c2410c"
        assert out.extraction_confidence == "high"

    async def test_with_no_brand_book_the_confidence_is_low_and_says_so(self) -> None:
        """§10.2: the gate card tells the brand owner what they are confirming."""
        from agent.nodes.content.stage_3_1 import visual_identity_rules

        h = harness(
            "3.1.3",
            answers={
                "VisualDraft": visual_answer(
                    logo={
                        "clear_space_ratio": None,
                        "min_width_px": None,
                        "permitted_variants": [],
                        "forbidden_treatments": [],
                    },
                    colour={"tokens": [], "pairs_meeting_contrast": [], "forbidden_pairs": []},
                )
            },
        )

        out = await visual_identity_rules.reason(h.ctx, [])

        assert out.extraction_confidence == "low"
        assert "live creative" in out.derivation_note

    async def test_a_colour_the_brand_book_never_showed_is_dropped(self) -> None:
        """A hex nobody sampled is a colour the model chose. That is not a brand token."""
        from agent.nodes.content.stage_3_1 import visual_identity_rules

        h = harness(
            "3.1.3",
            answers={
                "VisualDraft": visual_answer(
                    colour={
                        "tokens": [
                            {"name": "Signal orange", "hex": "#c2410c", "role": "accent"},
                            {"name": "Invented teal", "hex": "#0d9488", "role": "accent"},
                        ],
                        "pairs_meeting_contrast": [],
                        "forbidden_pairs": [],
                    }
                )
            },
        )

        out = await visual_identity_rules.reason(h.ctx, brand_book_rows())

        assert [token.hex for token in out.colour.tokens] == ["#c2410c"]
        assert "#0d9488" in out.dropped_colours

    async def test_it_is_a_gate_on_g5_for_an_approver(self) -> None:
        from agent.nodes.content.stage_3_1 import visual_identity_rules

        spec = visual_identity_rules.spec

        assert spec.gate is True
        assert spec.gate_key == "G5"
        assert spec.gate_conditional is False
        assert spec.required_role.value == "approver"

    def test_it_depends_on_the_matrix_and_the_voice_profile(self) -> None:
        from agent.nodes.content.stage_3_1 import visual_identity_rules

        assert set(visual_identity_rules.spec.depends_on) == {"3.5.1", "3.1.1"}


class TestGathering:
    """What each node asks the evidence store for, and what it tolerates missing.

    Every Stage 03 source is optional by construction (law 21, §10.2 Q2, §10.3):
    a project with no Google Ads account and no parseable brand book is a
    first-class path, not a degraded one, so none of these needs may report a
    gap that would put "insufficient evidence" on a rulebook that has plenty.
    """

    async def test_gathering_nothing_at_all_is_not_an_error(self) -> None:
        """Law 21's cold start, at the gather layer.

        A bare project has no stored evidence and no connected source, so every
        need misses and every pull declines for want of a credential. That has
        to come back as an empty corpus rather than an exception — 3.1.1 still
        has to run and say what it could not see.
        """
        h = harness("3.1.1", queries=[[], [], [], [], [], [], [], []])

        rows = await voice_profile.gather(h.ctx)

        assert rows == []

    async def test_every_source_the_voice_profile_reads_is_optional(self) -> None:
        """§10.3: no connected account means site copy alone, and the run continues."""
        needs = voice_profile.needs()

        assert [need.kind for need in needs] == [
            "creative_history",
            "site_pages",
            "brand_book_span",
        ]
        assert all(need.optional for need in needs)

    async def test_the_lexicon_reads_the_brand_book_and_the_ads(self) -> None:
        needs = lexicon_rules.needs()

        assert [need.kind for need in needs] == ["brand_book_span", "creative_history"]
        assert all(need.optional for need in needs)

    async def test_visual_identity_reads_spans_assets_and_colours(self) -> None:
        from agent.nodes.content.stage_3_1 import visual_identity_rules

        needs = visual_identity_rules.needs()

        assert [need.kind for need in needs] == [
            "brand_book_span",
            "brand_book_asset",
            "brand_book_colour",
        ]
        assert all(need.optional for need in needs)

    async def test_the_brand_book_is_pulled_through_its_own_connector(self) -> None:
        from agent.nodes.content.stage_3_1 import visual_identity_rules

        needs = visual_identity_rules.needs()

        assert {need.connector for need in needs} == {"brand_book"}
