"""Shape rules: how long a piece of copy may be, and how many of it there are.

Stage 03 PRD §9.2's `asset_spec` and `voice` categories. Three matcher kinds
live here because all three ask about a target's *shape* rather than its words.

`CountMatcher` is the one that does not fit the usual loop, and it is worth
being explicit about why. "Fewer than three headlines" is not a property of any
headline — asking each one whether the set is too small would report the same
problem three times, or none. So a count rule is evaluated once per lint call
over every in-scope target, and its finding is attached to the set rather than
to a piece of copy. `LintFinding.target_ref` carries `*` for those.

Lengths are measured on the text the advertiser **submits**, not on the folded
text. Google counts the characters that arrive; folding removes zero-width
characters and turns `ß` into `ss`, so a headline that measured 30 folded could
be 31 on the way in and get rejected by the platform after passing here.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any

from agent.guardrails.matchers.lexicon import TOKEN
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
    CountMatcher,
    EnumAllowMatcher,
    LengthMatcher,
    LintFinding,
    LintTarget,
    Matcher,
    Rule,
    RuleScope,
    Severity,
    Surface,
)

#: The ref a set-scoped finding carries. A count problem belongs to the
#: submission, and pinning it on one arbitrary headline would send the writer
#: to fix the wrong thing.
SET_REF = "*"


def measure(text: str, unit: str) -> int:
    """Length in the unit a spec is written in.

    `graphemes` counts what a reader would call a character: combining marks
    ride along with the letter they modify rather than counting separately.

    ponytail: this approximates grapheme clusters by ignoring combining marks,
    which is right for European copy and wrong for emoji sequences and some
    Indic scripts. The exact answer needs a UAX #29 segmenter (the `regex`
    package's `\\X`); add that dependency when a market needs it, not before.
    """
    if unit == "words":
        return len(TOKEN.findall(text))
    if unit == "graphemes":
        return sum(1 for character in text if not unicodedata.combining(character))
    return len(text)


# ---------------------------------------------------------------------------
# LengthMatcher
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PreparedLength:
    minimum: int | None
    maximum: int | None
    unit: str


def prepare_length(matcher: Matcher) -> PreparedLength:
    if not isinstance(matcher, LengthMatcher):  # pragma: no cover - registry guarantees it
        raise GuardrailsError(f"expected a LengthMatcher, got {type(matcher).__name__}")
    if matcher.min is None and matcher.max is None:
        raise GuardrailsError("a length rule with neither a min nor a max enforces nothing")
    if matcher.min is not None and matcher.max is not None and matcher.min > matcher.max:
        raise GuardrailsError(f"length min {matcher.min} is above max {matcher.max}")
    return PreparedLength(minimum=matcher.min, maximum=matcher.max, unit=matcher.unit)


@matcher_kind("length", prepare=prepare_length)
def evaluate_length(
    rule_: Rule, prepared: Any, target: LintTarget | None, ctx: LintContext
) -> list[LintFinding]:
    """Measured on the submitted text, and the finding says both numbers."""
    if target is None or target.text is None:
        return []
    size = measure(target.text, prepared.unit)
    if prepared.maximum is not None and size > prepared.maximum:
        return [
            finding(
                rule_,
                target.ref,
                message=(
                    f"{rule_.message} This is {size} {prepared.unit}; the limit is "
                    f"{prepared.maximum}."
                ),
                span=(0, len(target.text)),
            )
        ]
    if prepared.minimum is not None and size < prepared.minimum:
        return [
            finding(
                rule_,
                target.ref,
                message=(
                    f"{rule_.message} This is {size} {prepared.unit}; the minimum is "
                    f"{prepared.minimum}."
                ),
                span=(0, len(target.text)),
            )
        ]
    return []


# ---------------------------------------------------------------------------
# CountMatcher
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PreparedCount:
    minimum: int | None
    maximum: int | None
    entity: str


def prepare_count(matcher: Matcher) -> PreparedCount:
    if not isinstance(matcher, CountMatcher):  # pragma: no cover - registry guarantees it
        raise GuardrailsError(f"expected a CountMatcher, got {type(matcher).__name__}")
    if matcher.min is None and matcher.max is None:
        raise GuardrailsError("a count rule with neither a min nor a max enforces nothing")
    return PreparedCount(minimum=matcher.min, maximum=matcher.max, entity=matcher.entity)


@matcher_kind("count", prepare=prepare_count, applies_to="set")
def evaluate_count(
    rule_: Rule, prepared: Any, target: LintTarget | None, ctx: LintContext
) -> list[LintFinding]:
    """How many targets of one surface the submission holds.

    Counted over the targets the *rule's own scope* selects, so a rule scoped
    to German search headlines does not count the French ones. A submission
    holding none of the entity at all is still checked: zero is the count a
    minimum most often catches, and skipping it would make an empty ad group
    pass.
    """
    matching = [
        item
        for item in ctx.targets
        if item.surface == prepared.entity and rule_.scope.matches(item)
    ]
    count = len(matching)
    if prepared.maximum is not None and count > prepared.maximum:
        return [
            finding(
                rule_,
                SET_REF,
                message=(
                    f"{rule_.message} There are {count} of {prepared.entity}; the maximum "
                    f"is {prepared.maximum}."
                ),
            )
        ]
    if prepared.minimum is not None and count < prepared.minimum:
        return [
            finding(
                rule_,
                SET_REF,
                message=(
                    f"{rule_.message} There are {count} of {prepared.entity}; the minimum "
                    f"is {prepared.minimum}."
                ),
            )
        ]
    return []


# ---------------------------------------------------------------------------
# EnumAllowMatcher
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PreparedEnum:
    field: str
    allowed: frozenset[str]
    written: tuple[str, ...]


#: Fields a rule may allow-list. Restricted on purpose: `getattr` over an
#: arbitrary name would let a compiled rule read `text` and compare it as an
#: enum, which is a lexicon rule wearing the wrong matcher and would silently
#: never match.
ENUM_FIELDS = frozenset({"market", "language", "campaign_type", "surface"})


def prepare_enum(matcher: Matcher) -> PreparedEnum:
    if not isinstance(matcher, EnumAllowMatcher):  # pragma: no cover - registry guarantees it
        raise GuardrailsError(f"expected an EnumAllowMatcher, got {type(matcher).__name__}")
    if matcher.field not in ENUM_FIELDS:
        raise GuardrailsError(
            f"{matcher.field!r} is not an allow-listable field — one of "
            f"{', '.join(sorted(ENUM_FIELDS))}"
        )
    return PreparedEnum(
        field=matcher.field,
        allowed={value.casefold() for value in matcher.allowed},
        written=matcher.allowed,
    )


@matcher_kind("enum_allow", prepare=prepare_enum)
def evaluate_enum(
    rule_: Rule, prepared: Any, target: LintTarget | None, ctx: LintContext
) -> list[LintFinding]:
    if target is None:
        return []
    value = str(getattr(target, prepared.field))
    if value.casefold() in prepared.allowed:
        return []
    return [
        finding(
            rule_,
            target.ref,
            message=(
                f"{rule_.message} {prepared.field} is {value!r}; allowed: "
                f"{', '.join(prepared.written)}."
            ),
        )
    ]


# ---------------------------------------------------------------------------
# The rules these kinds serve
# ---------------------------------------------------------------------------


@rule("asset_spec.length.v1", category="asset_spec", matcher_kind="length", severity="blocking")
def asset_length(
    *,
    authority: Authority,
    message: str,
    maximum: int | None = None,
    minimum: int | None = None,
    unit: str = "chars",
    scope: RuleScope = EVERYWHERE,
    fix_hint: str | None = None,
) -> RuleBody:
    """A platform character or word limit. Blocking: the platform rejects it anyway."""
    return RuleBody(
        matcher=LengthMatcher(min=minimum, max=maximum, unit=unit),  # type: ignore[arg-type]
        message=message,
        authority=authority,
        scope=scope,
        fix_hint=fix_hint,
    )


@rule("asset_spec.count.v1", category="asset_spec", matcher_kind="count", severity="blocking")
def asset_count(
    entity: str,
    *,
    authority: Authority,
    message: str,
    minimum: int | None = None,
    maximum: int | None = None,
    scope: RuleScope = EVERYWHERE,
    fix_hint: str | None = None,
) -> RuleBody:
    """How many assets of a kind a campaign type requires or permits."""
    return RuleBody(
        matcher=CountMatcher(min=minimum, max=maximum, entity=entity),
        message=message,
        authority=authority,
        scope=scope,
        fix_hint=fix_hint,
    )


@rule("voice.sentence_length.v1", category="voice", matcher_kind="length")
def sentence_length(
    *,
    authority: Authority,
    message: str,
    maximum: int,
    severity: Severity = "advisory",
    surfaces: tuple[Surface, ...] = (),
    fix_hint: str | None = None,
) -> RuleBody:
    """Brand voice: how long a sentence may run on a surface.

    Advisory by default. A house style is a preference, and blocking on one
    would put the brand team's taste on the same footing as Google's policy.
    """
    return RuleBody(
        matcher=LengthMatcher(max=maximum, unit="words"),
        message=message,
        authority=authority,
        scope=RuleScope(surfaces=surfaces),
        severity=severity,
        fix_hint=fix_hint,
    )


@rule(
    "asset_spec.allowed_value.v1",
    category="asset_spec",
    matcher_kind="enum_allow",
    severity="blocking",
)
def allowed_value(
    field: str,
    allowed: tuple[str, ...],
    *,
    authority: Authority,
    message: str,
    scope: RuleScope = EVERYWHERE,
    fix_hint: str | None = None,
) -> RuleBody:
    """An allow-list on a target's market, language, campaign type or surface."""
    return RuleBody(
        matcher=EnumAllowMatcher(field=field, allowed=allowed),
        message=message,
        authority=authority,
        scope=scope,
        fix_hint=fix_hint,
    )
