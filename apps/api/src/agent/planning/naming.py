"""The pattern language behind node 2.4.1 (Stage 02 PRD §11, §12 invariant 5).

A naming convention is worth having only if something enforces it. 2.4.1 emits
a `validator_regex`, §12 invariant 5 requires every generated name to pass it,
and node 2.4.3's structure is rejected if one does not. That regex is compiled
here from the patterns and the token vocabulary — **never written by a model**.
A model-authored regex is a rule whose failures nobody can predict, and this one
decides whether a plan can be frozen.

Three jobs, all pure string work and none of it arithmetic, which is why this
sits in `planning/` rather than `calc/`:

* `compile_validator` turns `{market} | {channel} | {brand_split} | {theme}`
  into an anchored alternation over every pattern the convention declares.
* `render` builds one name from a pattern and a set of token values, refusing
  the two ways a name goes wrong quietly — a missing value leaving a literal
  brace in a campaign name, and a value carrying the separator, which invents a
  segment that the validator then reads as a different pattern.
* `collisions` compares proposed names against the live account **and against
  each other**, because a plan that names two campaigns the same thing collides
  at import time exactly as painfully as one that collides with an existing
  campaign.

**Why free tokens are a closed list.** `{theme}` and `{intent}` accept anything;
`{nonsense}` is refused. Without `FREE_TOKENS` the two are indistinguishable,
so a typo in a pattern would compile to a regex that matches everything in that
position and the convention would silently stop being enforced.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

#: `{market}`, `{brand_split}` — lowercase and underscores, so a literal brace
#: in a pattern is never mistaken for a token.
TOKEN = re.compile(r"\{([a-z_][a-z0-9_]*)\}")

#: Tokens that accept any text because no vocabulary can be agreed in advance:
#: a theme comes from the demand data and an intent label from Stage 01. Every
#: other token must arrive with its `allowed_values`, or the pattern is refused.
FREE_TOKENS = frozenset(
    {
        "theme",
        "intent",
        "product",
        "audience",
        "campaign",
        "ad_group",
        "wave",
        "test",
        "descriptor",
    }
)

#: Characters that may not appear inside a token's value, because they are how
#: patterns separate segments. Derived per pattern from its own literals rather
#: than fixed here, so a convention using `>` is checked against `>`.
_WORDISH = re.compile(r"[A-Za-z0-9 ]")

#: Suffix used to resolve a collision. `v2`, `v3`, … reads as a version of the
#: same campaign, which is what it is; a random suffix reads as a different one.
_SUFFIX = "v{index}"

#: How far the resolver will walk before giving up rather than looping.
_MAX_SUFFIX = 50


class NamingError(ValueError):
    """A convention that cannot be compiled, or a name that cannot be built."""


def compile_validator(patterns: Mapping[str, str], tokens: Mapping[str, Sequence[str]]) -> str:
    """Every declared pattern, as one anchored regex.

    Anchored and alternated rather than one regex per pattern because §12
    invariant 5 asks a single question — "does this generated name follow the
    convention" — of campaign names and ad-group names alike, and the caller
    does not always know which kind it is holding.
    """
    if not patterns:
        raise NamingError("a naming convention needs at least one pattern")

    fragments: list[str] = []
    for kind, pattern in sorted(patterns.items()):
        fragments.append(_compile_one(kind, pattern, tokens))
    return "^(?:" + "|".join(fragments) + ")$"


def _compile_one(kind: str, pattern: str, tokens: Mapping[str, Sequence[str]]) -> str:
    names = TOKEN.findall(pattern)
    if not names:
        raise NamingError(f"the {kind} pattern {pattern!r} contains no token")

    specials = _separators(pattern)
    out: list[str] = []
    cursor = 0
    for match in TOKEN.finditer(pattern):
        out.append(re.escape(pattern[cursor : match.start()]))
        out.append(_segment(kind, match.group(1), tokens, specials))
        cursor = match.end()
    out.append(re.escape(pattern[cursor:]))
    return "".join(out)


def _segment(
    kind: str, name: str, tokens: Mapping[str, Sequence[str]], specials: frozenset[str]
) -> str:
    allowed = tokens.get(name)
    if allowed:
        # Escaped, so a value like `C++` compiles to a literal rather than to a
        # repetition operator. Longest first, so `Search Partners` is not
        # shadowed by `Search`.
        options = sorted({str(value) for value in allowed}, key=lambda item: (-len(item), item))
        return "(?:" + "|".join(re.escape(option) for option in options) + ")"
    if name in FREE_TOKENS:
        if not specials:
            return ".+"
        return "[^" + "".join(re.escape(char) for char in sorted(specials)) + "]+"
    raise NamingError(
        f"the {kind} pattern names an unknown token {{{name}}} — give it "
        f"`allowed_values`, or use one of the free tokens: "
        f"{', '.join(sorted(FREE_TOKENS))}"
    )


def _separators(pattern: str) -> frozenset[str]:
    """The punctuation this pattern uses to divide segments.

    Anything that is not a letter, a digit or a space in the literal parts. A
    free token must not match these or a three-segment name passes as a
    four-segment one.
    """
    literals = TOKEN.sub("\x00", pattern)
    return frozenset(char for char in literals if char != "\x00" and not _WORDISH.match(char))


def render(pattern: str, values: Mapping[str, Any]) -> str:
    """One name, from a pattern and its token values."""
    specials = _separators(pattern)
    missing = [name for name in TOKEN.findall(pattern) if not str(values.get(name, "")).strip()]
    if missing:
        raise NamingError(
            f"no value for {', '.join('{' + name + '}' for name in sorted(set(missing)))} "
            f"in pattern {pattern!r}"
        )

    def _replace(match: re.Match[str]) -> str:
        value = " ".join(str(values[match.group(1)]).split())
        clash = specials & set(value)
        if clash:
            raise NamingError(
                f"the value {value!r} for {{{match.group(1)}}} carries the pattern's "
                f"separator ({''.join(sorted(clash))}), which would invent a segment"
            )
        return value

    return TOKEN.sub(_replace, pattern)


# ---------------------------------------------------------------------------
# collisions
# ---------------------------------------------------------------------------


def _exact_key(name: str) -> str:
    """Google Ads rejects a duplicate campaign name case-insensitively."""
    return " ".join(name.split()).casefold()


def _near_key(name: str) -> str:
    """Same letters and digits, different punctuation. A human reading a report
    would call these the same campaign, and the person importing will too."""
    return re.sub(r"[^a-z0-9]+", "", name.casefold())


def collisions(proposed: Sequence[str], existing: Sequence[str]) -> list[dict[str, Any]]:
    """Which proposed names clash, with a name that would not.

    PRD §18: a collision is a warning at plan time and blocking at freeze time,
    so every row carries a `resolution` the caller can adopt without inventing
    one. The resolution is checked against the account *and* against the names
    already accepted in this pass, so adopting all of them in order leaves no
    duplicates behind.
    """
    taken_exact = {_exact_key(name) for name in existing}
    taken_near = {_near_key(name) for name in existing}
    by_exact = {_exact_key(name): name for name in existing}
    by_near = {_near_key(name): name for name in existing}

    found: list[dict[str, Any]] = []
    for name in proposed:
        exact, near = _exact_key(name), _near_key(name)
        conflict: str | None = None
        against = ""
        if exact in taken_exact:
            conflict = "duplicate_in_plan" if exact not in by_exact else "exact"
            against = by_exact.get(exact, name)
        elif near in taken_near:
            conflict = "near"
            against = by_near.get(near, name)

        if conflict is not None:
            resolution = _resolve(name, taken_exact, taken_near)
            found.append(
                {
                    "proposed_name": name,
                    "existing_name": against,
                    "conflict_type": conflict,
                    "resolution": resolution,
                }
            )
            taken_exact.add(_exact_key(resolution))
            taken_near.add(_near_key(resolution))
        else:
            taken_exact.add(exact)
            taken_near.add(near)
    return found


def _resolve(name: str, taken_exact: set[str], taken_near: set[str]) -> str:
    for index in range(2, _MAX_SUFFIX + 1):
        candidate = f"{name} {_SUFFIX.format(index=index)}"
        if _exact_key(candidate) not in taken_exact and _near_key(candidate) not in taken_near:
            return candidate
    raise NamingError(
        f"{name!r} and {_MAX_SUFFIX} numbered variants of it are all taken — the "
        f"convention itself needs a token that separates them"
    )
