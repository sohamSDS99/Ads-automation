"""`lint()`. The one public way to get a verdict out of a ruleset.

Stage 03 PRD §9.1 item 4 and §12.2. Stage 04 imports this in-process; external
consumers reach it through `POST /guidelines/{id}/lint`. There is no third
path, and **no caller may evaluate rules itself** — a second implementation of
a matcher is a second set of verdicts, and the point of compiling a ruleset is
that the rules an asset was checked against can be named a year later.

The contract is narrow on purpose:

* `now` is passed in. Claim expiry and offer windows are the only time-
  dependent parts of the system, and they are arguments so that two runs of the
  same lint agree with each other.
* Nothing short-circuits. A target collects every finding it triggers, because
  a writer who fixes one problem and is then shown the next one has been made
  to do three rounds of work for one edit.
* Findings come back in a stable order — caller's target order, then rule id,
  then span. Byte-identical output from identical input is a tested property
  (`test_lint_is_deterministic`), and sorting is what makes it true regardless
  of how evaluation happened to interleave.

**A blocking finding that is `indeterminate` still fails.** Law 31: a check
whose input never arrived is not a pass, and `LintResult.verdict` has no third
value to put it in. The finding carries `indeterminate=True` so a person can
see the difference between "this is wrong" and "this could not be checked",
and both stop the asset.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import datetime

from agent.guardrails.compiler import Program, build_program
from agent.guardrails.normalize import normalize
from agent.guardrails.registry import LintContext
from agent.schemas.guardrails import (
    LintFinding,
    LintResult,
    LintTarget,
    OfferRecord,
    RuleSet,
)


def lint(
    targets: Sequence[LintTarget],
    ruleset: RuleSet,
    *,
    now: datetime,
    offers: Sequence[OfferRecord] = (),
    program: Program | None = None,
) -> LintResult:
    """Evaluate `ruleset` against `targets` as of `now`.

    `offers` is the live price data `matchers/offers.py` checks against; a
    matcher that could fetch its own prices is a matcher whose verdict depends
    on when it ran, so it arrives here instead.

    `program` lets a caller linting repeatedly against one ruleset — the
    playground, a 200-variant corpus — build the matchers once. Omitted, they
    are built per call, which is once per lint and never per target.
    """
    started = time.perf_counter()
    built = program or build_program(ruleset)

    ordered_targets = tuple(targets)
    context = LintContext(
        ruleset=ruleset,
        now=now,
        targets=ordered_targets,
        normalized={
            target.ref: normalize(target.text or "", locale=target.language)
            for target in ordered_targets
        },
        offers=tuple(offers),
    )

    findings: list[LintFinding] = []
    evaluated: set[str] = set()
    position = {target.ref: index for index, target in enumerate(ordered_targets)}

    for target in ordered_targets:
        for entry in built.per_target:
            if not entry.rule.scope.matches(target):
                continue
            evaluated.add(entry.rule.rule_id)
            findings.extend(entry.kind.evaluate(entry.rule, entry.prepared, target, context))

    for entry in built.per_set:
        evaluated.add(entry.rule.rule_id)
        findings.extend(entry.kind.evaluate(entry.rule, entry.prepared, None, context))

    findings.sort(key=lambda item: _order(item, position, len(ordered_targets)))

    blocking = any(item.severity == "blocking" for item in findings)
    warning = any(item.severity == "warning" for item in findings)
    elapsed = int((time.perf_counter() - started) * 1000)

    return LintResult(
        ruleset_version=ruleset.ruleset_version,
        verdict="fail" if blocking else "pass_with_warnings" if warning else "pass",
        findings=tuple(findings),
        targets_checked=len(ordered_targets),
        rules_evaluated=len(evaluated),
        elapsed_ms=elapsed,
        evaluated_at=now,
    )


def _order(
    item: LintFinding, position: dict[str, int], set_rank: int
) -> tuple[int, str, int, int, str]:
    """Caller's target order first, so a writer reads their own copy in order.

    Set-scoped findings sort last: they belong to the submission rather than to
    any one asset, and burying them between two headlines reads as noise.
    """
    span = item.span or (-1, -1)
    return (
        position.get(item.target_ref, set_rank),
        item.rule_id,
        span[0],
        span[1],
        item.message,
    )
