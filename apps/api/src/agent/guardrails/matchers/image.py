"""Image rules, evaluated over metrics somebody else measured.

Stage 03 PRD §9.5. The measuring — normalise, downscale, OCR, union the text
bounding boxes, ORB and perceptual-hash the logos — is S3-P5 and runs in the
worker with `tesseract`. What lives here is the *rule*: a threshold applied to
a number, which is the half that has to be reproducible.

Keeping those apart is the entire point. This rule blocks an asset from being
produced, and a blocking rule that returns a different answer on a re-run
destroys trust in the whole rulebook inside a week. Measurement is allowed to
be expensive and is allowed to need a native binary; adjudication is not
allowed to be either.

**A missing metric is never a pass** (law 31). If OCR did not run, or ran and
produced nothing for this image, the verdict is `indeterminate` and the finding
says which metric was absent. The alternative — treating "we did not measure"
as "it measured fine" — is how an unreviewed image reaches a live campaign
wearing a green tick.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.guardrails.registry import (
    EVERYWHERE,
    GuardrailsError,
    LintContext,
    RuleBody,
    finding,
    matcher_kind,
    rule,
)
from agent.schemas.guardrails import (
    Authority,
    LintFinding,
    LintTarget,
    Matcher,
    RatioMatcher,
    Rule,
    RuleScope,
)


@dataclass(frozen=True, slots=True)
class PreparedRatio:
    metric: str
    maximum: float | None
    minimum: float | None


def prepare_ratio(matcher: Matcher) -> PreparedRatio:
    if not isinstance(matcher, RatioMatcher):  # pragma: no cover - registry guarantees it
        raise GuardrailsError(f"expected a RatioMatcher, got {type(matcher).__name__}")
    if matcher.max is None and matcher.min is None:
        raise GuardrailsError(
            f"ratio rule on {matcher.metric!r} has neither a max nor a min and enforces nothing"
        )
    return PreparedRatio(metric=matcher.metric, maximum=matcher.max, minimum=matcher.min)


@matcher_kind("ratio", prepare=prepare_ratio)
def evaluate_ratio(
    rule_: Rule, prepared: Any, target: LintTarget | None, ctx: LintContext
) -> list[LintFinding]:
    """Compare one measured metric against its threshold, or fail closed."""
    if target is None or target.image_ref is None:
        return []

    metrics = target.image_metrics
    if metrics is None or prepared.metric not in metrics:
        return [
            finding(
                rule_,
                target.ref,
                message=(
                    f"{rule_.message} {prepared.metric!r} was not measured for this image, "
                    f"so it was not checked."
                ),
                indeterminate=True,
            )
        ]

    measured = metrics[prepared.metric]
    if prepared.maximum is not None and measured > prepared.maximum:
        return [
            finding(
                rule_,
                target.ref,
                message=(
                    f"{rule_.message} {prepared.metric} is {measured:.3f}; the maximum is "
                    f"{prepared.maximum:.3f}."
                ),
            )
        ]
    if prepared.minimum is not None and measured < prepared.minimum:
        return [
            finding(
                rule_,
                target.ref,
                message=(
                    f"{rule_.message} {prepared.metric} is {measured:.3f}; the minimum is "
                    f"{prepared.minimum:.3f}."
                ),
            )
        ]
    return []


@rule("image.text_coverage.v1", category="image", matcher_kind="ratio", severity="blocking")
def text_coverage(
    *,
    authority: Authority,
    maximum: float,
    message: str = "Too much of this image is text.",
    scope: RuleScope = EVERYWHERE,
    fix_hint: str | None = "Move the message into the headline and leave the image to the image.",
) -> RuleBody:
    """The share of an image's area covered by text (PRD §9.5)."""
    return RuleBody(
        matcher=RatioMatcher(metric="text_coverage_ratio", max=maximum),
        message=message,
        authority=authority,
        scope=scope,
        fix_hint=fix_hint,
    )


@rule("image.logo_match.v1", category="image", matcher_kind="ratio", severity="blocking")
def logo_match(
    *,
    authority: Authority,
    minimum: float,
    message: str = "No registered logo was recognised in this image.",
    scope: RuleScope = EVERYWHERE,
    fix_hint: str | None = "Use an approved logo asset at its registered proportions.",
) -> RuleBody:
    """The best logo match score against the registered templates (PRD §9.5)."""
    return RuleBody(
        matcher=RatioMatcher(metric="logo_match_score", min=minimum),
        message=message,
        authority=authority,
        scope=scope,
        fix_hint=fix_hint,
    )
