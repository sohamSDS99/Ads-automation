"""`creative/lint_adapter.py` (Stage 04 PRD §4.3 rule 2, law 33).

Two properties. The adapter is a *pass-through*: the verdict it returns is the
verdict `guardrails.linter.lint()` returns for the pinned ruleset, the snapshot
offers and the `now` it was handed — it adds no rule and drops no argument.
And it is the *only* Stage 04 caller: a second caller is a second place a
matcher could be reimplemented or an argument forgotten.
"""

from __future__ import annotations

import ast
from datetime import timedelta
from pathlib import Path

import pytest

from agent.creative import lint_adapter
from agent.creative.lint_adapter import LintAdapterError, PinnedLinter, current_pin
from agent.guardrails.linter import lint as guardrails_lint
from agent.schemas.guardrails import LintResult, LintTarget
from tests.guardrails.fixture import NOW, golden_offers, golden_ruleset, golden_targets

SRC = Path(lint_adapter.__file__).resolve().parents[1]

#: Every Stage 04 module and package (PRD §6.1). `lint_adapter.py` is the one
#: that may import the linter.
STAGE_04 = (
    SRC / "creative",
    SRC / "nodes" / "creative",
    SRC / "media",
    SRC / "orchestrator" / "creative_input.py",
    SRC / "api" / "routes_creative.py",
    SRC / "api" / "routes_media.py",
)


def _verdict(result: LintResult) -> dict[str, object]:
    """Everything but `elapsed_ms`, which is a stopwatch, not a verdict."""
    return result.model_dump(exclude={"elapsed_ms"})


def test_the_verdict_is_the_linters_for_the_pin_the_offers_and_now() -> None:
    ruleset, offers, targets = golden_ruleset(), golden_offers(), golden_targets(40)
    pinned = PinnedLinter(ruleset=ruleset, offer_records=tuple(offers))
    assert _verdict(pinned.lint(targets, now=NOW)) == _verdict(
        guardrails_lint(targets, ruleset, now=NOW, offers=offers)
    )


def test_the_offer_records_reach_the_linter() -> None:
    ruleset, targets = golden_ruleset(), golden_targets(40)
    with_offers = PinnedLinter(ruleset=ruleset, offer_records=tuple(golden_offers()))
    without = PinnedLinter(ruleset=ruleset, offer_records=())
    assert _verdict(with_offers.lint(targets, now=NOW)) != _verdict(without.lint(targets, now=NOW))


def test_now_reaches_the_linter() -> None:
    ruleset, offers, targets = golden_ruleset(), golden_offers(), golden_targets(40)
    pinned = PinnedLinter(ruleset=ruleset, offer_records=tuple(offers))
    later = NOW + timedelta(days=3650)
    assert _verdict(pinned.lint(targets, now=later)) == _verdict(
        guardrails_lint(targets, ruleset, now=later, offers=offers)
    )
    assert pinned.lint(targets, now=later).evaluated_at == later


def test_the_result_names_the_pin_it_was_linted_against() -> None:
    ruleset = golden_ruleset()
    pinned = PinnedLinter(ruleset=ruleset, offer_records=())
    assert pinned.pin == ruleset.ruleset_version
    assert pinned.lint(golden_targets(3), now=NOW).ruleset_version == ruleset.ruleset_version


def test_the_current_pin_is_the_last_one_appended() -> None:
    class _Run:
        pins = [
            {"ruleset_version": "1.0+aaaa", "reason": "start", "at": "2026-09-24T10:00:00+00:00"},
            {"ruleset_version": "1.1+bbbb", "reason": "h3_clearance", "at": "2026-09-24T11:00:00"},
        ]

    assert current_pin(_Run()) == "1.1+bbbb"  # type: ignore[arg-type]


@pytest.mark.parametrize("pins", [None, []])
def test_a_run_without_a_pin_has_no_rules_to_lint_against(pins: object) -> None:
    class _Run:
        id = "r"

    run = _Run()
    run.pins = pins  # type: ignore[attr-defined]
    with pytest.raises(LintAdapterError, match="no ruleset pin"):
        current_pin(run)  # type: ignore[arg-type]


def _imports_the_linter(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "agent.guardrails.linter":
                return True
            if node.module == "agent.guardrails" and any(
                alias.name == "linter" for alias in node.names
            ):
                return True
        if isinstance(node, ast.Import) and any(
            alias.name.startswith("agent.guardrails.linter") for alias in node.names
        ):
            return True
    return False


def test_lint_adapter_is_the_only_stage_04_caller_of_the_linter() -> None:
    files: list[Path] = []
    for root in STAGE_04:
        files.extend(sorted(root.rglob("*.py")) if root.is_dir() else [root])
    assert len(files) > 10, "the Stage 04 scan found almost nothing — are the paths right?"
    callers = sorted(str(path.relative_to(SRC)) for path in files if _imports_the_linter(path))
    assert callers == ["creative/lint_adapter.py"]


# ---------------------------------------------------------------------------
# a candidate is linted alone, at creation (law 33)
# ---------------------------------------------------------------------------


def _published() -> PinnedLinter:
    """A pin carrying the spec-sheet rules synthesis really emits."""
    from agent.export.guideline_contract import AssetSpecs
    from agent.guidelines.constants import get_content_constants
    from agent.guidelines.synthesis import _asset_rules

    sheet = get_content_constants().asset_sheet()
    rules = tuple(_asset_rules(AssetSpecs(sheet=sheet, scope="unscoped"), lambda _reason: None))
    return PinnedLinter(
        ruleset=golden_ruleset().model_copy(update={"rules": rules}), offer_records=()
    )


def _headline(text: str) -> LintTarget:
    return LintTarget(
        ref="h1",
        surface="rsa_headline",
        campaign_type="search",
        market="US",
        language="en",
        text=text,
    )


def test_a_candidate_is_not_judged_by_the_submissions_asset_counts() -> None:
    """One headline is not "fewer than three headlines" — the assembled ad is."""
    pinned = _published()
    alone = pinned.lint([_headline("Keep every SDS current")], now=NOW)
    candidate = pinned.lint_candidate(_headline("Keep every SDS current"), now=NOW)

    assert alone.verdict == "fail"
    assert {f.target_ref for f in alone.findings} == {"*"}
    assert candidate.verdict == "pass", candidate.findings
    assert candidate.targets_checked == 1


def test_a_candidate_still_meets_every_per_target_rule() -> None:
    result = _published().lint_candidate(_headline("Keep every safety data sheet current"), now=NOW)
    assert result.verdict == "fail"
    assert [(f.target_ref, f.rule_id) for f in result.findings] == [("h1", "asset_spec.length.v1")]
