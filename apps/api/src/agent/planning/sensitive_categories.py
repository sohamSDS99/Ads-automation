"""The GDPR Article 9(1) special categories, as a blocklist (Stage 02 PRD §13).

Stage 02 §13: "Scoring signals are firmographic and behavioural. Health, union
membership, religion and the rest of the GDPR Article 9 list are rejected at
schema level via a blocklist." Stage 04 §11 4.3.3 names the same list for the
lead form: no question may target an Art. 9 category. One list, here, so the
two stages cannot disagree about what is sensitive.

Matching is on whole words and phrases of the case-folded text, with the
punctuation turned to spaces — "Race" is caught, "Racing" is not. Before
matching, the phrases a chemical-safety buyer's form legitimately carries are
taken out ("health and safety manager" is a job, not health data), and only
they are: a health word *beside* one is still caught.

Stage 02's 2.1.4 is the caller its PRD names, and it has never been wired to
a blocklist; that is owed a ruling (docs/stage-04-questions.md § S4-P8).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

#: Art. 9(1), in the order the Regulation lists it, each with the ordinary
#: words a form question about it would use.
ARTICLE_9: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "racial_or_ethnic_origin": ("race", "racial", "ethnic", "ethnicity", "ethnic origin"),
        "political_opinions": (
            "political",
            "politics",
            "political party",
            "party affiliation",
            "voting intention",
            "who did you vote for",
        ),
        "religious_or_philosophical_beliefs": (
            "religion",
            "religious",
            "faith",
            "philosophical belief",
            "philosophical beliefs",
            "church",
            "mosque",
            "synagogue",
            "temple attendance",
        ),
        "trade_union_membership": (
            "trade union",
            "trade unions",
            "labor union",
            "labour union",
            "union member",
            "union membership",
        ),
        "genetic_data": ("genetic", "genetics", "genome", "dna"),
        "biometric_data": (
            "biometric",
            "biometrics",
            "fingerprint",
            "fingerprints",
            "facial recognition",
            "face scan",
            "iris scan",
            "retina scan",
            "voiceprint",
        ),
        "health": (
            "health",
            "medical",
            "diagnosis",
            "disability",
            "disabilities",
            "disabled",
            "illness",
            "disease",
            "medication",
            "pregnant",
            "pregnancy",
            "mental health",
            "blood type",
        ),
        "sex_life_or_sexual_orientation": (
            "sex life",
            "sexual",
            "sexuality",
            "sexual orientation",
            "gay",
            "lesbian",
            "bisexual",
            "lgbt",
            "lgbtq",
        ),
    }
)

#: Job and team names that contain a listed word and are not about the
#: person's own special-category data. Removed before matching, exactly.
EXEMPT_PHRASES: Final = (
    "environment health and safety",
    "environmental health and safety",
    "health and safety",
    "occupational health and safety",
    "european union",
)

_NON_WORD = re.compile(r"[^\w]+")


def _normalise(text: str) -> str:
    folded = text.casefold().replace("&", " and ")
    return f" {' '.join(_NON_WORD.sub(' ', folded).split())} "


_TERMS: Final = tuple(
    (category, _normalise(term)) for category, terms in ARTICLE_9.items() for term in terms
)
_EXEMPT: Final = tuple(_normalise(phrase) for phrase in EXEMPT_PHRASES)


def article_9_category(text: str) -> str | None:
    """The first Art. 9 category `text` targets, or None."""
    haystack = _normalise(text)
    for phrase in _EXEMPT:
        haystack = haystack.replace(phrase, " ")
    for category, term in _TERMS:
        if term in haystack:
            return category
    return None
