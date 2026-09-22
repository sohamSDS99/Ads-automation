"""The `@rule` contract. Every verdict in Stage 03 comes through here.

Stage 03 PRD §9.1 and global law 22: **the LLM drafts rules; it never
adjudicates them.** A model may propose that `cheap` is off-brand. Whether a
given headline breaks that rule is decided by a registered rule, evaluated
deterministically, citing a `rule_id` and an authority. There is no path from a
model's opinion to a blocking verdict.

This module is the half of that law the code can enforce on its own, and it is
the same shape as `calc/registry.py` for the same reasons:

* a rule cannot enter a `RuleSet` without being registered, because
  `compiler.py` refuses an unknown `rule_id`;
* a rule cannot mis-state its own identity, because `rule_id`, `category` and a
  fixed `severity` are stamped by the decorator and never written by the body —
  the body returns a `RuleBody`, which has nowhere to put an id;
* two rules cannot share an id, because registration refuses a duplicate at
  import time;
* a matcher kind cannot be evaluated without an evaluator, because the kind
  registry is what the linter dispatches on, and an unregistered kind raises
  at compile time rather than being quietly skipped at lint time.

That last one is the difference between a rulebook that is wrong and a rulebook
that is silently empty, which is the failure mode worth the most paranoia: a
linter that evaluates nothing returns `pass` for everything.

Purity is enforced from outside, by `scripts/check_guardrails_purity.py`:
nothing in `agent/guardrails/` may import the ORM, HTTP, an LLM client, or read
a clock. A rule that needs today's date takes it as an argument.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import wraps
from typing import Any, Final, ParamSpec
from uuid import UUID

from agent.schemas.guardrails import (
    Authority,
    LintFinding,
    LintTarget,
    Matcher,
    OfferRecord,
    Rule,
    RuleCategory,
    RuleScope,
    RuleSet,
    Severity,
)

#: Bumped when the *evaluation* of any registered rule changes. It is one half
#: of `compiler_version`; the constants file's version and the pinned language
#: libraries are the others. A verdict produced under `guardrails/1.0` can be
#: told apart from one produced under `guardrails/1.1` even if nothing else
#: moved.
GUARDRAILS_CODE_VERSION: Final[str] = "1.0"

#: `namespace.name.vN`, with at least one dot before the version. The version
#: is part of the id, so changing what a rule *means* means registering `.v2`
#: alongside `.v1` rather than silently redefining what old findings claimed.
RULE_ID: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+\.v\d+$")

#: The default scope. An empty `RuleScope` applies everywhere, which is what
#: lets an unbound run emit rules that still enforce something (law 21); a
#: shared singleton keeps that reading the same in every constructor signature.
EVERYWHERE: Final[RuleScope] = RuleScope()


class RuleRegistrationError(RuntimeError):
    """Two rules claiming one id, an id that is not a rule id, or an unknown kind."""


class GuardrailsError(ValueError):
    """Inputs a rule cannot produce an honest verdict from."""


# ---------------------------------------------------------------------------
# What an evaluator is given
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LintContext:
    """Everything an evaluator may read. All of it arrives as an argument.

    `now` is the clock, passed in. `offers` is live pricing, passed in. There
    is nothing here an evaluator could have fetched for itself, which is what
    makes two runs of the same lint agree.
    """

    ruleset: RuleSet
    now: datetime
    #: Every target in this lint call, so a `CountMatcher` can ask how many
    #: headlines the submission actually has.
    targets: tuple[LintTarget, ...]
    #: `target.ref -> Normalized`, folded once per target and shared by every
    #: rule. Re-folding per rule would be the whole cost of a 400-rule set.
    normalized: Mapping[str, Any]
    offers: tuple[OfferRecord, ...] = ()


#: Builds whatever a matcher needs before the target loop starts — a compiled
#: pattern, a stemmed term set, a claim lookup. Called once per rule per lint,
#: never per target.
Prepare = Callable[[Matcher], Any]

#: Evaluates one prepared rule. `target` is `None` for a set-scoped matcher.
Evaluate = Callable[[Rule, Any, LintTarget | None, LintContext], list[LintFinding]]


@dataclass(frozen=True, slots=True)
class MatcherKind:
    """One entry in the kind registry: how to prepare it, and how to run it."""

    kind: str
    prepare: Prepare
    evaluate: Evaluate
    #: `target` runs once per in-scope target. `set` runs once per lint call
    #: over all of them — "fewer than three headlines" is a property of the
    #: submission, not of any one headline.
    applies_to: str = "target"


#: Every registered matcher kind, keyed by its discriminator. Populated by the
#: modules in `guardrails/matchers/`, which `guardrails/__init__.py` imports.
MATCHER_KINDS: dict[str, MatcherKind] = {}


def matcher_kind(
    kind: str, *, prepare: Prepare, applies_to: str = "target"
) -> Callable[[Evaluate], Evaluate]:
    """Register the evaluator for one `Matcher` kind."""
    if applies_to not in ("target", "set"):
        raise RuleRegistrationError(f"{kind}: applies_to must be 'target' or 'set'")

    def decorate(fn: Evaluate) -> Evaluate:
        origin = f"{fn.__module__}.{fn.__qualname__}"
        existing = MATCHER_KINDS.get(kind)
        # Compared by origin rather than identity so that re-importing a module
        # — which `importlib.reload` does, and the isolation tests do — is not
        # mistaken for two kinds colliding.
        registered_origin = (
            f"{existing.evaluate.__module__}.{existing.evaluate.__qualname__}" if existing else None
        )
        if registered_origin is not None and registered_origin != origin:
            raise RuleRegistrationError(f"matcher kind {kind!r} is already registered")
        MATCHER_KINDS[kind] = MatcherKind(
            kind=kind, prepare=prepare, evaluate=fn, applies_to=applies_to
        )
        return fn

    return decorate


# ---------------------------------------------------------------------------
# What a rule constructor returns
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RuleBody:
    """What a rule constructor returns: the rule, without its identity.

    Splitting this from `Rule` is what makes the decorator load-bearing. A
    constructor cannot claim a `rule_id` it was not registered under, and it
    cannot claim a `category` its registration did not declare, because it
    never writes either.
    """

    matcher: Matcher
    #: Written to the person who is blocked. "Headlines are 30 characters; this
    #: is 34" beats "length violation".
    message: str
    authority: Authority
    scope: RuleScope = RuleScope()
    #: Left `None` when the registration fixed it. A `claim` rule is always
    #: blocking (law 24); a banned term might be blocking for one brand and a
    #: warning for another, so that one is the constructor's call.
    severity: Severity | None = None
    fix_hint: str | None = None
    evidence_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True, slots=True)
class RuleSpec:
    """A registered rule constructor, for the registry listing and the guards."""

    rule_id: str
    category: RuleCategory
    matcher_kind: str
    fn: Callable[..., Rule]
    #: `module.qualname` of the undecorated constructor, for the duplicate check.
    origin: str
    doc: str
    severity: Severity | None = None


#: Every registered rule, keyed by id. Import-order independent: each matcher
#: module registers its own, and `guardrails/__init__.py` imports them all so
#: `RULES` is complete for anyone who imports the package.
RULES: dict[str, RuleSpec] = {}

P = ParamSpec("P")


def rule(
    rule_id: str,
    *,
    category: RuleCategory,
    matcher_kind: str,
    severity: Severity | None = None,
) -> Callable[[Callable[P, RuleBody]], Callable[P, Rule]]:
    """Register a rule constructor and stamp its identity onto every rule.

    `severity` fixes the severity when the law does — `claim` rules are always
    blocking — and leaves it to the constructor when it is genuinely a brand's
    choice. A body that returns a severity the registration fixed differently
    is a registration error, not a silent override.
    """
    if not RULE_ID.match(rule_id):
        raise RuleRegistrationError(f"{rule_id!r} is not a rule id — expected namespace.name.vN")

    def decorate(fn: Callable[P, RuleBody]) -> Callable[P, Rule]:
        origin = f"{fn.__module__}.{fn.__qualname__}"
        existing = RULES.get(rule_id)
        if existing is not None and existing.origin != origin:
            raise RuleRegistrationError(f"{rule_id} is already registered by {existing.origin}")

        @wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> Rule:
            body = fn(*args, **kwargs)
            if body.matcher.kind != matcher_kind:
                raise RuleRegistrationError(
                    f"{rule_id} is registered for matcher kind {matcher_kind!r} "
                    f"but returned {body.matcher.kind!r}"
                )
            if severity is not None and body.severity not in (None, severity):
                raise RuleRegistrationError(
                    f"{rule_id} is always {severity}; it returned {body.severity!r}"
                )
            resolved = severity or body.severity
            if resolved is None:
                raise RuleRegistrationError(
                    f"{rule_id} declares no severity, and its registration fixes none"
                )
            return Rule(
                rule_id=rule_id,
                category=category,
                severity=resolved,
                scope=body.scope,
                matcher=body.matcher,
                message=body.message,
                fix_hint=body.fix_hint,
                authority=body.authority,
                evidence_ids=body.evidence_ids,
            )

        summary = next((line for line in (fn.__doc__ or "").splitlines() if line.strip()), "")
        RULES[rule_id] = RuleSpec(
            rule_id=rule_id,
            category=category,
            matcher_kind=matcher_kind,
            fn=wrapper,
            origin=origin,
            doc=summary.strip(),
            severity=severity,
        )
        return wrapper

    return decorate


# ---------------------------------------------------------------------------
# What the compiler checks before anything is hashed
# ---------------------------------------------------------------------------


def require_registered(rules: Sequence[Rule]) -> None:
    """Refuse any rule the registry does not know. Called by `compiler.compile`.

    This is where law 22 stops being a comment. A rule drafted by a model,
    hand-edited in the console, or replayed from an old payload reaches a
    `RuleSet` only if a registered constructor of that id exists and the
    matcher kind it carries has an evaluator — otherwise it would compile
    cleanly, evaluate nothing, and read as a rulebook that passes everything.
    """
    unknown = sorted({rule.rule_id for rule in rules if rule.rule_id not in RULES})
    if unknown:
        raise RuleRegistrationError(
            "unregistered rule id(s) cannot enter a RuleSet: " + ", ".join(unknown)
        )

    wrong_kind = sorted(
        f"{rule.rule_id} carries {rule.matcher.kind!r}, registered for "
        f"{RULES[rule.rule_id].matcher_kind!r}"
        for rule in rules
        if rule.matcher.kind != RULES[rule.rule_id].matcher_kind
    )
    if wrong_kind:
        raise RuleRegistrationError("; ".join(wrong_kind))

    no_evaluator = sorted(
        {rule.matcher.kind for rule in rules if rule.matcher.kind not in MATCHER_KINDS}
    )
    if no_evaluator:
        raise RuleRegistrationError(
            "matcher kind(s) with no registered evaluator: " + ", ".join(no_evaluator)
        )

    fixed = sorted(
        f"{rule.rule_id} is always {RULES[rule.rule_id].severity}, not {rule.severity}"
        for rule in rules
        if RULES[rule.rule_id].severity is not None
        and rule.severity != RULES[rule.rule_id].severity
    )
    if fixed:
        raise RuleRegistrationError("; ".join(fixed))


def kind_for(matcher: Matcher) -> MatcherKind:
    """The registered evaluator for a matcher. Raises rather than skipping."""
    registered = MATCHER_KINDS.get(matcher.kind)
    if registered is None:
        raise RuleRegistrationError(f"matcher kind {matcher.kind!r} has no evaluator")
    return registered


def finding(
    rule: Rule,
    target_ref: str,
    *,
    message: str | None = None,
    span: tuple[int, int] | None = None,
    claim_id: UUID | None = None,
    indeterminate: bool = False,
    severity: Severity | None = None,
) -> LintFinding:
    """Build a finding that carries its rule's authority. The only constructor.

    Every evaluator goes through this, so PRD §9.1 item 5 — *a finding that
    cannot name its authority is a bug* — is true by construction rather than
    by each matcher remembering to copy the field.

    `severity` overrides the rule's own, and exists for exactly one shape: a
    note attached to a finding rather than a verdict of its own. The offer
    staleness warning (Q4) rides alongside a blocking price finding and must
    not itself be a second reason to fail.
    """
    return LintFinding(
        target_ref=target_ref,
        rule_id=rule.rule_id,
        severity=severity or rule.severity,
        span=span,
        message=message or rule.message,
        fix_hint=rule.fix_hint,
        authority_ref=rule.authority.reference,
        claim_id=claim_id,
        indeterminate=indeterminate,
    )
