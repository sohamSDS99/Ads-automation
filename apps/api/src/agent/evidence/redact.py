"""Law 30's deterministic redaction pass."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

PHONE = re.compile(
    r"(?<![\w@.])(?:\+\d{1,3}[\s.-]?)?(?:\(\d{2,5}\)|\d{2,5})(?:[\s.-]\d{2,4}){1,4}(?![\w])"
)

#: Below this, a run of grouped digits is a price or a model number rather than
#: somebody's telephone number.
MIN_PHONE_DIGITS = 7

#: A date carries enough digits to look like a phone number, and redacting one
#: is not a harmless over-reach: 3.2.4 binds countdown and deadline offers to
#: real dates, so eating `2026-09-30` invents a deadline rather than hiding a
#: person.
DATE = re.compile(r"\A(?:\d{4}[-./]\d{1,2}[-./]\d{1,2}|\d{1,2}[-./]\d{1,2}[-./]\d{2,4})\Z")


def _phone(match: re.Match[str]) -> str:
    found = match.group()
    if DATE.match(found.strip()):
        return found
    digits = sum(character.isdigit() for character in found)
    return "[phone]" if digits >= MIN_PHONE_DIGITS else found


#: A street line: a building number, up to four capitalised words, and a
#: thoroughfare word. Requiring the number *and* the thoroughfare word is what
#: keeps "Rated 5 stars" and "Sign up in 2 minutes" out of the net — one signal
#: alone is ordinary marketing copy.
STREET = re.compile(
    r"\b\d{1,6}[A-Za-z]?\s+(?:[A-Z][\w\'-]*\s+){0,4}"
    r"(?:Street|St|Road|Rd|Avenue|Ave|Lane|Ln|Drive|Dr|Boulevard|Blvd|Way|Square|Sq"
    r"|Place|Pl|Court|Ct|Parkway|Pkwy|Terrace|Close|Crescent|Gardens|Row|Walk)\b",
)

UK_POSTCODE = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b")

US_ZIP = re.compile(r"\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b")


def redact_pii(text: str) -> str:
    """Strip emails, telephone numbers and postal addresses from one string.

    Pure: no clock, no network, no ORM, so the same input redacts the same way
    in any process on any day — the property the canary test relies on.

    **What this does not do.** Detecting an arbitrary postal address is not a
    regex problem, and pretending otherwise would be the dangerous kind of
    false confidence. What is caught is the shape that actually appears in ad
    and site copy: a numbered street line, a UK postcode, a US state-and-ZIP.
    A bare personal name is not caught here at all — §11's critique assertion
    10 is what covers names in the payload.
    """
    text = EMAIL.sub("[email]", text)
    text = STREET.sub("[address]", text)
    text = UK_POSTCODE.sub("[address]", text)
    text = US_ZIP.sub("[address]", text)
    return PHONE.sub(_phone, text)


def redact_payload(value: Any) -> Any:
    """`redact_pii` over every string in a nested structure, returning a copy.

    A copy, not an edit. `Evidence` rows stay faithful to what the connector
    actually retrieved — 3.2.1 has to harvest the claims a site really makes,
    and an evidence store quietly rewritten to suit a prompt is no longer
    evidence. Only the rendering handed to a model is cleaned.

    Keys are left alone: a payload key is a field name the connector chose, not
    copy somebody wrote.
    """
    if isinstance(value, str):
        return redact_pii(value)
    if isinstance(value, Mapping):
        return {key: redact_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_payload(item) for item in value)
    return value
