"""What a connector hands back, and how it becomes a deduped row.

Two jobs live here, and both exist so that no connector has to get them right
on its own:

`content_text` — one flat, readable rendering of a payload. It is what the
embedder sees and what full-text search indexes, so a connector that forgets it
produces evidence no node can ever retrieve. Every kind gets a default
rendering; a connector overrides it only when it can do better.

`content_hash` — the dedupe key behind `UNIQUE(project_id, hash)` (PRD §6). It
is taken over the canonical form of the payload, so re-running a connector
against unchanged upstream data writes nothing, while genuinely new numbers
write a new row. Key order and float formatting cannot change the hash; a
changed value always does.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, Field, field_validator

from agent.db.models import EvidenceSource

#: Keys that carry a fetch timestamp rather than a fact. Including them in the
#: hash would defeat dedupe entirely — every refetch would look new.
VOLATILE_KEYS = frozenset({"fetched_at", "retrieved_at", "_fetched_at", "request_id"})

MAX_CONTENT_TEXT = 8_000


class EvidenceDraft(BaseModel):
    """A fact a connector retrieved, before it has an id or a home.

    `BaseConnector.fetch()` returns these; `evidence.store` turns them into rows.
    A draft deliberately knows nothing about projects, runs or the database —
    that is what makes a connector testable against a cassette alone.
    """

    source: EvidenceSource
    kind: str = Field(min_length=1, description="Sub-type within the source, e.g. search_term_pnl")
    payload: dict[str, Any] = Field(default_factory=dict)
    source_url: str | None = None
    content_text: str | None = Field(
        default=None, description="Overrides the default rendering when a connector can do better"
    )

    @field_validator("kind")
    @classmethod
    def _kind_is_a_slug(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("kind cannot be blank")
        return cleaned

    def text(self) -> str:
        """The rendering that gets embedded and indexed."""
        return self.content_text or render_content_text(self.kind, self.payload)

    def hash(self) -> str:
        return content_hash(self.source, self.kind, self.payload, self.source_url)


def _canonical(value: Any) -> Any:
    """Strip volatile keys and pin float formatting, recursively.

    `1.0` and `1` hash the same on purpose: JSON round-trips through several
    connectors and an int that became a float on the way back is not new data.
    """
    if isinstance(value, dict):
        return {
            key: _canonical(item) for key, item in sorted(value.items()) if key not in VOLATILE_KEYS
        }
    if isinstance(value, list | tuple):
        return [_canonical(item) for item in value]
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        return round(value, 10)
    return value


def content_hash(
    source: EvidenceSource | str,
    kind: str,
    payload: dict[str, Any],
    source_url: str | None = None,
) -> str:
    """The stable dedupe key for one fact within one project."""
    material = {
        "source": str(source),
        "kind": kind,
        "source_url": source_url or "",
        "payload": _canonical(payload),
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _flatten(value: Any, prefix: str = "") -> list[str]:
    parts: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            parts.extend(_flatten(item, f"{prefix}{key} "))
    elif isinstance(value, list | tuple):
        # A list of scalars reads better inline than as one line per element.
        if all(not isinstance(item, dict | list | tuple) for item in value):
            rendered = ", ".join(str(item) for item in value if item is not None)
            if rendered:
                parts.append(f"{prefix.strip()}: {rendered}")
        else:
            for item in value:
                parts.extend(_flatten(item, prefix))
    elif value is not None and value != "":
        parts.append(f"{prefix.strip()}: {value}")
    return parts


def render_content_text(kind: str, payload: dict[str, Any]) -> str:
    """Default rendering: `kind` followed by every leaf in the payload.

    Deliberately dumb and lossless rather than clever. A node asking "which
    search terms wasted money" needs the terms themselves in the index; a
    hand-written summary per kind would be the place that forgets one.
    """
    lines = [kind.replace("_", " ")]
    lines.extend(_flatten(payload))
    text = "\n".join(lines)
    return text[:MAX_CONTENT_TEXT]
