"""Reading a node's stored output without trusting its shape.

Every Stage 03 module that projects a node output into a contract needs the same
handful of narrowings — `synthesis` assembling the rulebook, `register`
materialising the claims — and each had its own copy. Five near-identical
`_text`/`_strings`/`_dicts` pairs is five places for one of them to drift into
accepting something the other rejects, over the same JSONB column.

**Every function here is total.** Given anything at all it returns the empty
value for its type rather than raising, because the callers read a `node_run.
output` that a model shaped and a migration may have widened: a rulebook that
failed to assemble because 3.3.3 returned `null` where a list was expected would
lose an hour of run to a field nobody reads. Refusal belongs in the node that
authored the value — where it can say which model call broke its contract — not
in the projection three nodes downstream.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any


def text(value: Any) -> str:
    """A stripped string, or empty. Never `"None"`."""
    return str(value).strip() if value is not None else ""


def optional_text(value: Any) -> str | None:
    """`None` rather than `""`, for fields where absent and blank differ."""
    return text(value) or None


def strings(value: Any) -> list[str]:
    """A list of non-empty strings. A bare `str` is not a sequence of them."""
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    return [str(item).strip() for item in value if item is not None and str(item).strip()]


def mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def mappings(value: Any) -> list[dict[str, Any]]:
    """Only the mapping members. A list mixing dicts and scalars loses the scalars."""
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def integer(value: Any) -> int | None:
    """`bool` is excluded deliberately — `True` is not the count 1."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def identifier(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def identifiers(value: Any) -> list[uuid.UUID]:
    """Parseable ids, de-duplicated, in first-seen order.

    Order is preserved rather than sorted: a node's citation list is read back
    in the order it cited, and re-ordering it would churn a payload diff for no
    change in meaning.
    """
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    found: list[uuid.UUID] = []
    for item in value:
        parsed = identifier(item)
        if parsed is not None and parsed not in found:
            found.append(parsed)
    return found
