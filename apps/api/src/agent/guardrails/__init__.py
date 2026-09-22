"""The deterministic rule layer. Every verdict in Stage 03 is produced here.

Global law 22: the LLM drafts rules; it never adjudicates them. A model may
propose that `cheap` is off-brand or that a claim needs signing. Whether a
given headline breaks a rule is decided by a registered `@rule`, evaluated
against data that arrived as an argument, citing a `rule_id` and an authority.

Importing this package registers every matcher kind and every rule, so
`RULES` and `MATCHER_KINDS` are complete for anything that walks them — the
compiler, the linter and the purity guard all do.

**Nothing in this package may call a model, touch the network, touch the ORM,
or read the clock.** Purity is the feature: a verdict that changes between runs
is worse than no verdict at all, because people build on it before they notice.
`scripts/check_guardrails_purity.py` fails the build on any of it.
"""

from __future__ import annotations

from agent.guardrails import normalize
from agent.guardrails.matchers import assets, claims, disclosure, image, lexicon, offers
from agent.guardrails.registry import (
    EVERYWHERE,
    GUARDRAILS_CODE_VERSION,
    MATCHER_KINDS,
    RULES,
    GuardrailsError,
    LintContext,
    MatcherKind,
    RuleBody,
    RuleRegistrationError,
    RuleSpec,
    finding,
    kind_for,
    matcher_kind,
    require_registered,
    rule,
)

__all__ = [
    "EVERYWHERE",
    "GUARDRAILS_CODE_VERSION",
    "MATCHER_KINDS",
    "RULES",
    "GuardrailsError",
    "LintContext",
    "MatcherKind",
    "RuleBody",
    "RuleRegistrationError",
    "RuleSpec",
    "assets",
    "claims",
    "disclosure",
    "finding",
    "image",
    "kind_for",
    "lexicon",
    "matcher_kind",
    "normalize",
    "offers",
    "require_registered",
    "rule",
]
