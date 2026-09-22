"""Generated creative has to say so, where the policy says to say it.

Stage 03 PRD §9.2's `disclosure` category and law 30's neighbour: a disclosure
rule is the only one that fires on a *property of how the copy was made*
rather than on the copy itself. `LintTarget.generated_by_ai` is that property,
and Stage 04 is what sets it — it knows which assets a model wrote.

A target that was not generated is silently fine. That is deliberate and worth
stating: this rule must never nag a human writer into adding a disclosure that
would be false.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.guardrails.normalize import Normalized, normalize
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
    DisclosureMatcher,
    LintFinding,
    LintTarget,
    Matcher,
    Rule,
    RuleScope,
)


@dataclass(frozen=True, slots=True)
class PreparedDisclosure:
    required: str
    placement: str


def prepare_disclosure(matcher: Matcher) -> PreparedDisclosure:
    if not isinstance(matcher, DisclosureMatcher):  # pragma: no cover - registry guarantees it
        raise GuardrailsError(f"expected a DisclosureMatcher, got {type(matcher).__name__}")
    required = normalize(matcher.required_text).text
    if not required:
        raise GuardrailsError("a disclosure rule whose required text is empty enforces nothing")
    return PreparedDisclosure(required=required, placement=matcher.placement)


@matcher_kind("disclosure", prepare=prepare_disclosure)
def evaluate_disclosure(
    rule_: Rule, prepared: Any, target: LintTarget | None, ctx: LintContext
) -> list[LintFinding]:
    """Generated copy missing its disclosure, or carrying it in the wrong place."""
    if target is None or not target.generated_by_ai:
        return []
    if target.text is None:
        return []

    folded: Normalized = ctx.normalized[target.ref]
    if prepared.placement == "prefix":
        present = folded.text.startswith(prepared.required)
    elif prepared.placement == "suffix":
        present = folded.text.endswith(prepared.required)
    else:
        present = prepared.required in folded.text

    if present:
        return []
    where = {"prefix": "at the start", "suffix": "at the end", "anywhere": "somewhere"}[
        prepared.placement
    ]
    return [
        finding(
            rule_,
            target.ref,
            message=f"{rule_.message} It must carry {prepared.required!r} {where}.",
        )
    ]


@rule(
    "disclosure.ai_generated.v1",
    category="disclosure",
    matcher_kind="disclosure",
    severity="blocking",
)
def ai_generated(
    *,
    authority: Authority,
    required_text: str,
    placement: str = "anywhere",
    message: str = "This copy was generated and carries no AI disclosure.",
    scope: RuleScope = EVERYWHERE,
    fix_hint: str | None = "Add the disclosure, or have a person write the asset.",
) -> RuleBody:
    """Disclosure on generated creative, where a policy requires one."""
    return RuleBody(
        matcher=DisclosureMatcher(required_text=required_text, placement=placement),  # type: ignore[arg-type]
        message=message,
        authority=authority,
        scope=scope,
        fix_hint=fix_hint,
    )
