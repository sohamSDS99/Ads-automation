"""A spec-sheet rule applies to the asset type it was written for, and no other.

`guidelines/synthesis._asset_rules` scopes every length and count bound by
campaign type *and* asset type ("An unscoped length rule would apply a
headline's 30 characters to a YouTube script"). Until S4-P5 the linter read
only the first half: `RuleScope.matches` ignored `asset_types`, so a Search
path's 15 characters failed every legal headline, and `CountMatcher` compared
its entity (an asset type, `headline`) with a surface (`rsa_headline`), so a
Search ruleset always saw "0 of headline". Every Stage 04 lint against a real
published ruleset failed. These tests pin the repaired behaviour on the rules
synthesis actually emits, not on hand-built ones.
"""

from __future__ import annotations

from agent.export.guideline_contract import AssetSpecs
from agent.guardrails.linter import lint
from agent.guardrails.matchers.assets import asset_length
from agent.guidelines.constants import get_content_constants
from agent.guidelines.synthesis import _asset_rules
from agent.schemas.guardrails import (
    SURFACE_ASSET_TYPES,
    LintResult,
    LintTarget,
    Rule,
    RuleScope,
)
from tests.guardrails.helpers import INTERNAL, NOW, ruleset, target


def _published_asset_rules() -> tuple[Rule, ...]:
    sheet = get_content_constants().asset_sheet()
    return tuple(_asset_rules(AssetSpecs(sheet=sheet, scope="unscoped"), lambda _reason: None))


def _headlines(count: int, *, campaign_type: str = "search") -> list[LintTarget]:
    return [
        target(f"Keep every SDS current {n:02d}", ref=f"h{n}", campaign_type=campaign_type)
        for n in range(count)
    ]


def test_a_path_length_rule_does_not_apply_to_a_headline() -> None:
    body = asset_length(
        authority=INTERNAL,
        message="Path is 15 characters on search.",
        maximum=15,
        scope=RuleScope(campaign_types=("search",), asset_types=("path",)),
    )
    rule = Rule(
        rule_id="asset_spec.length.v1",
        category="asset_spec",
        **body.model_dump(exclude={"rule_id", "category"}),
    )
    headline = target("Keep every SDS current now", surface="rsa_headline")
    path = target("Keep every SDS current now", surface="rsa_path")

    assert rule.scope.matches(path)
    assert not rule.scope.matches(headline)


def test_an_asset_type_rule_still_applies_to_a_surface_with_no_asset_type() -> None:
    """Narrowing is only done where the surface's asset type is known.

    `landing_page_section` has no spec-sheet asset type; a rule scoped to one
    keeps applying to it, because an unknown mapping must not switch a rule off.
    """
    scope = RuleScope(campaign_types=("search",), asset_types=("headline",))
    assert "landing_page_section" not in SURFACE_ASSET_TYPES
    assert scope.matches(target("Anything at all", surface="landing_page_section"))


def test_a_video_script_line_is_not_a_headline() -> None:
    """S4-P11: `youtube_script` is written against its own asset type.

    Unmapped, a rule scoped to `headline` applied to every voiceover line of a
    Performance Max video — "36 chars; the limit is 30" — and failed every
    script. Mapped, headline limits stay on headlines while a rule with no
    asset type (a never term, a claim, a policy) still reaches the script.
    """
    assert SURFACE_ASSET_TYPES["youtube_script"] == "video_script"
    line = target("Keep every safety data sheet current", surface="youtube_script")
    line = line.model_copy(update={"campaign_type": "performance_max"})
    headline_only = RuleScope(campaign_types=("performance_max",), asset_types=("headline",))
    everything = RuleScope(campaign_types=("performance_max",))
    assert not headline_only.matches(line)
    assert everything.matches(line)
    assert RuleScope(asset_types=("video_script",)).matches(line)


def test_every_rsa_and_pmax_text_surface_names_its_spec_sheet_asset_type() -> None:
    assert SURFACE_ASSET_TYPES["rsa_headline"] == "headline"
    assert SURFACE_ASSET_TYPES["rsa_description"] == "description"
    assert SURFACE_ASSET_TYPES["rsa_path"] == "path"
    assert SURFACE_ASSET_TYPES["pmax_headline"] == "headline"
    assert SURFACE_ASSET_TYPES["pmax_description"] == "description"
    assert SURFACE_ASSET_TYPES["long_headline"] == "long_headline"


def test_fifteen_legal_headlines_pass_the_published_asset_rules() -> None:
    """No per-headline finding, no headline-count finding.

    The set still reports that a Search ad has no descriptions yet — that is
    the submission's problem, correctly stated, not a headline's.
    """
    result = lint(_headlines(15), ruleset(rules=_published_asset_rules()), now=NOW)
    per_headline = [f.message for f in result.findings if f.target_ref != "*"]
    headline_counts = [
        f.message
        for f in result.findings
        if f.target_ref == "*" and f.message.startswith("search") and "of headline" in f.message
    ]
    assert per_headline == []
    assert headline_counts == []


def test_a_headline_over_thirty_characters_still_fails_the_published_rules() -> None:
    long = target("Keep every safety data sheet current", ref="long")
    result = lint([*_headlines(14), long], ruleset(rules=_published_asset_rules()), now=NOW)
    assert [(f.target_ref, f.rule_id) for f in result.findings if f.target_ref != "*"] == [
        ("long", "asset_spec.length.v1")
    ]


def test_a_search_count_rule_counts_rsa_headlines() -> None:
    rules = _published_asset_rules()
    two = lint(_headlines(2), ruleset(rules=rules), now=NOW)
    sixteen = lint(_headlines(16), ruleset(rules=rules), now=NOW)

    def search_headline_counts(result: LintResult) -> list[str]:
        return [
            f.message
            for f in result.findings
            if f.target_ref == "*" and f.message.startswith("search") and "headline" in f.message
        ]

    assert [m.split(".")[-2].strip() for m in search_headline_counts(two)] == [
        "There are 2 of headline; the minimum is 3"
    ]
    assert [m.split(".")[-2].strip() for m in search_headline_counts(sixteen)] == [
        "There are 16 of headline; the maximum is 15"
    ]
