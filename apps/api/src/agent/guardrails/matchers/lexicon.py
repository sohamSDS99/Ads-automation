"""Vocabulary and pattern rules: the words a brand bans, and the ones it owes.

Stage 03 PRD §9.2, the `lexicon`, `governance`, `policy` and `learned`
categories. Two matcher kinds live here because they answer the same question —
*does this text contain that* — and splitting them across files would put the
tokenizer in one and its only other caller in another.

`TermSetMatcher` is the interesting one. Banning `cheap` has to catch
`cheapest`, and there are two honest ways to do that and one dishonest one.
The dishonest one is a substring test, which also catches `cheapskate` and, more
embarrassingly, bans `class` for a rule about `ass`. The honest ones are
stemming and lemmatisation, and they are not interchangeable:

* `stem` folds inflection by chopping suffixes (Snowball). `running -> run`.
  It leaves `cheapest` alone, because English superlatives are not a suffix
  Snowball strips.
* `lemma` resolves to a dictionary head word. `cheapest -> cheap`, `ran -> run`,
  `best -> good`. It catches what stemming misses and costs a dictionary.

Both libraries are offline, pinned, and stamped into `compiler_version`, so a
library upgrade changes the ruleset hash rather than quietly changing verdicts
underneath a stable one. That is the whole reason `LANGUAGE_LIB_VERSION` exists.

Matching is over *token windows*, never substrings: a term of three words is
compared against three consecutive tokens. `Safety Data Sheet` matches
`safety data sheets` under lemma and matches nothing under a substring test
that happened to be written against the singular.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from importlib.metadata import version
from typing import Any, Final

import simplemma
import snowballstemmer

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
    LintFinding,
    LintTarget,
    Matcher,
    RegexMatcher,
    Rule,
    RuleScope,
    Severity,
    TermSetMatcher,
)

#: Pinned into every `RuleSet`'s `compiler_version`. A Snowball or simplemma
#: upgrade can change what `lemma` resolves to, which changes verdicts; that
#: must move the hash, or an asset audited a year from now would be checked
#: against rules that no longer mean what they meant.
LANGUAGE_LIB_VERSION: Final[str] = (
    f"snowball/{version('snowballstemmer')}+simplemma/{version('simplemma')}"
)

#: Snowball names its algorithms in English. simplemma takes ISO codes, so the
#: locale passes through untouched there.
SNOWBALL_LANGUAGES: Final[dict[str, str]] = {
    "en": "english",
    "de": "german",
    "fr": "french",
    "es": "spanish",
    "it": "italian",
    "nl": "dutch",
    "pt": "portuguese",
    "sv": "swedish",
    "da": "danish",
    "fi": "finnish",
    "no": "norwegian",
    "ru": "russian",
}

#: Words, as a lexicon rule means them. Unicode-aware, so `straße` is one token.
TOKEN: Final[re.Pattern[str]] = re.compile(r"\w+", re.UNICODE)

REGEX_FLAGS: Final[dict[str, int]] = {
    "i": re.IGNORECASE,
    "m": re.MULTILINE,
    "s": re.DOTALL,
    "x": re.VERBOSE,
}


def language_of(locale: str) -> str:
    return locale.split("-")[0].split("_")[0].casefold()


@lru_cache(maxsize=200_000)
def stem(word: str, language: str) -> str:
    """One word, stemmed. Memoised because ad copy repeats itself relentlessly.

    The cache is a pure memo — same word, same language, same answer forever —
    so it changes throughput and nothing else.
    """
    algorithm = SNOWBALL_LANGUAGES.get(language)
    if algorithm is None:
        raise GuardrailsError(
            f"no stemmer for locale {language!r} — supported: "
            f"{', '.join(sorted(SNOWBALL_LANGUAGES))}"
        )
    stemmed: str = snowballstemmer.stemmer(algorithm).stemWord(word)
    return stemmed


@lru_cache(maxsize=200_000)
def lemma(word: str, language: str) -> str:
    """One word, lemmatised. Raises for a language simplemma cannot serve."""
    try:
        resolved: str = simplemma.lemmatize(word, lang=language)
    except ValueError as exc:  # simplemma raises for an unsupported language
        raise GuardrailsError(f"no lemmatiser for locale {language!r}: {exc}") from exc
    return resolved


def warm(language: str = "en") -> None:
    """Load simplemma's dictionary for `language` now rather than on the first lint.

    simplemma reads a language's data lazily, on its first `lemmatize` — about
    100 ms for English, paid by whoever lints first in a process. The Ad
    Studio's lint preview has a person waiting on it (Stage 04 PRD §15.4 E), so
    the api pays it at startup instead. Changes latency, never a verdict.
    """
    lemma("sheets", language)


def transform(word: str, mode: str, language: str) -> str:
    if mode == "stem":
        return stem(word, language)
    if mode == "lemma":
        return lemma(word, language)
    return word


# ---------------------------------------------------------------------------
# TermSetMatcher
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PreparedTerms:
    """Terms folded and transformed once, grouped by how many words they span."""

    #: `word count -> {transformed phrase -> the term as it was written}`. The
    #: original spelling is kept so a finding can quote the *rule's* wording
    #: rather than the stemmed rubble the comparison ran on.
    by_length: dict[int, dict[str, str]]
    mode: str
    language: str
    require: bool


def prepare_terms(matcher: Matcher) -> PreparedTerms:
    if not isinstance(matcher, TermSetMatcher):  # pragma: no cover - registry guarantees it
        raise GuardrailsError(f"expected a TermSetMatcher, got {type(matcher).__name__}")
    language = language_of(matcher.locale)
    by_length: dict[int, dict[str, str]] = {}
    for term in matcher.terms:
        words = TOKEN.findall(normalize(term, locale=matcher.locale).text)
        if not words:
            raise GuardrailsError(f"term {term!r} normalises to nothing")
        key = " ".join(transform(word, matcher.match, language) for word in words)
        by_length.setdefault(len(words), {})[key] = term
    return PreparedTerms(
        by_length=by_length,
        mode=matcher.match,
        language=language,
        require=matcher.mode == "require",
    )


def tokens(folded: Normalized) -> list[tuple[str, int, int]]:
    """`(word, start, end)` over the folded text, spans in folded coordinates."""
    return [(match.group(), match.start(), match.end()) for match in TOKEN.finditer(folded.text)]


def hits(prepared: PreparedTerms, folded: Normalized) -> list[tuple[str, int, int]]:
    """Every term occurrence, as `(term as written, folded start, folded end)`.

    Windows of n tokens for an n-word term. A term never matches across a
    sentence boundary it did not ask for, because the window is contiguous
    tokens and nothing else.
    """
    found: list[tuple[str, int, int]] = []
    words = tokens(folded)
    transformed = [transform(word, prepared.mode, prepared.language) for word, _, _ in words]
    for size, terms in sorted(prepared.by_length.items()):
        for index in range(len(words) - size + 1):
            phrase = " ".join(transformed[index : index + size])
            written = terms.get(phrase)
            if written is not None:
                found.append((written, words[index][1], words[index + size - 1][2]))
    return found


@matcher_kind("term_set", prepare=prepare_terms)
def evaluate_terms(
    rule_: Rule, prepared: Any, target: LintTarget | None, ctx: LintContext
) -> list[LintFinding]:
    """A banned term found, or a required term missing."""
    if target is None or target.text is None:
        return []
    folded: Normalized = ctx.normalized[target.ref]
    found = hits(prepared, folded)

    if prepared.require:
        if found:
            return []
        return [finding(rule_, target.ref)]

    return [
        finding(
            rule_,
            target.ref,
            message=f"{rule_.message} Found {written!r}.",
            span=folded.source_span(start, end),
        )
        for written, start, end in found
    ]


# ---------------------------------------------------------------------------
# RegexMatcher
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PreparedPattern:
    pattern: re.Pattern[str]


def prepare_pattern(matcher: Matcher) -> PreparedPattern:
    if not isinstance(matcher, RegexMatcher):  # pragma: no cover - registry guarantees it
        raise GuardrailsError(f"expected a RegexMatcher, got {type(matcher).__name__}")
    flags = 0
    for flag in matcher.flags:
        flags |= REGEX_FLAGS[flag]
    try:
        return PreparedPattern(pattern=re.compile(matcher.pattern, flags))
    except re.error as exc:
        raise GuardrailsError(f"{matcher.pattern!r} is not a valid pattern: {exc}") from exc


@matcher_kind("regex", prepare=prepare_pattern)
def evaluate_pattern(
    rule_: Rule, prepared: Any, target: LintTarget | None, ctx: LintContext
) -> list[LintFinding]:
    """Every match is a finding. Patterns run against folded text."""
    if target is None or target.text is None:
        return []
    folded: Normalized = ctx.normalized[target.ref]
    return [
        finding(rule_, target.ref, span=folded.source_span(match.start(), match.end()))
        for match in prepared.pattern.finditer(folded.text)
    ]


# ---------------------------------------------------------------------------
# The rules these kinds serve (PRD §9.2)
# ---------------------------------------------------------------------------


@rule("lexicon.banned_term.v1", category="lexicon", matcher_kind="term_set")
def banned_term(
    terms: tuple[str, ...],
    *,
    authority: Authority,
    message: str,
    severity: Severity = "blocking",
    scope: RuleScope = EVERYWHERE,
    fix_hint: str | None = None,
    match: str = "lemma",
    locale: str = "en",
) -> RuleBody:
    """Words the brand does not use.

    `lemma` by default: a ban on `cheap` that misses `cheapest` is a ban
    somebody will route around by accident on their first day.
    """
    return RuleBody(
        matcher=TermSetMatcher(terms=terms, match=match, mode="forbid", locale=locale),
        message=message,
        authority=authority,
        scope=scope,
        severity=severity,
        fix_hint=fix_hint,
    )


@rule("lexicon.required_term.v1", category="lexicon", matcher_kind="term_set")
def required_term(
    terms: tuple[str, ...],
    *,
    authority: Authority,
    message: str,
    severity: Severity = "warning",
    scope: RuleScope = EVERYWHERE,
    fix_hint: str | None = None,
    match: str = "lemma",
    locale: str = "en",
) -> RuleBody:
    """Words the brand owes — a product's full name, a regulatory term.

    Any one of `terms` satisfies it. A rule that demanded all of them would be
    several rules wearing one id, and the finding could not say which was
    missing.
    """
    return RuleBody(
        matcher=TermSetMatcher(terms=terms, match=match, mode="require", locale=locale),
        message=message,
        authority=authority,
        scope=scope,
        severity=severity,
        fix_hint=fix_hint,
    )


@rule("governance.review_trigger.v1", category="governance", matcher_kind="regex")
def review_trigger(
    pattern: str,
    *,
    authority: Authority,
    message: str,
    scope: RuleScope = EVERYWHERE,
    fix_hint: str | None = None,
) -> RuleBody:
    """Copy that has to go past a human before it goes anywhere else.

    A warning rather than a block on purpose: governance routes, it does not
    refuse (§9.2). Blocking here would train writers to avoid the words rather
    than to get the review.
    """
    return RuleBody(
        matcher=RegexMatcher(pattern=pattern),
        message=message,
        authority=authority,
        scope=scope,
        severity="warning",
        fix_hint=fix_hint,
    )


@rule("policy.restricted_phrase.v1", category="policy", matcher_kind="regex", severity="blocking")
def restricted_phrase(
    pattern: str,
    *,
    authority: Authority,
    message: str,
    scope: RuleScope = EVERYWHERE,
    fix_hint: str | None = None,
) -> RuleBody:
    """A restricted-category construction Google's policy applies to us."""
    return RuleBody(
        matcher=RegexMatcher(pattern=pattern),
        message=message,
        authority=authority,
        scope=scope,
        fix_hint=fix_hint,
    )


@rule(
    "learned.disapproval_construction.v1",
    category="learned",
    matcher_kind="regex",
    severity="blocking",
)
def disapproval_construction(
    pattern: str,
    *,
    authority: Authority,
    message: str,
    scope: RuleScope = EVERYWHERE,
    fix_hint: str | None = None,
) -> RuleBody:
    """The exact construction that got an ad disapproved once already.

    Always blocking: the authority is a disapproval that actually happened, and
    there is no version of "we know this gets rejected" that is advisory.
    """
    return RuleBody(
        matcher=RegexMatcher(pattern=pattern),
        message=message,
        authority=authority,
        scope=scope,
        fix_hint=fix_hint,
    )
