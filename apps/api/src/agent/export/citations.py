"""Stable, human-readable markers for the evidence a report cites.

A raw UUID is unreadable in a PDF and useless in a Word document, but the ids
are the whole provenance story — PRD §18 Law 1: an LLM never sources a fact, and
every claim names the evidence it came from. So each distinct id gets a short
marker (`E1`, `E2`, …) and the document carries an index mapping markers back to
ids.

Numbering follows first appearance in a depth-first walk of the report, which is
document order. That makes it deterministic for a given payload — the same
report renders the same markers every time, in every format — and it is what
lets the markdown, PDF and DOCX exports be compared to each other at all.

P7's Report Viewer hangs its citation popovers on these same markers, so the
mapping has to be derivable from the payload alone, with no side table.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from agent.export.contract import ResearchReport


@dataclass(frozen=True, slots=True)
class Citation:
    """One evidence id, as the document refers to it."""

    marker: str
    evidence_id: uuid.UUID

    @property
    def short_id(self) -> str:
        """The first segment of the UUID — enough to eyeball against the API."""
        return str(self.evidence_id).split("-")[0]


class CitationIndex:
    """Markers for every evidence id in one report, in document order."""

    def __init__(self, evidence_ids: Iterable[uuid.UUID]) -> None:
        self._order: dict[uuid.UUID, Citation] = {}
        for identifier in evidence_ids:
            if identifier not in self._order:
                marker = f"E{len(self._order) + 1}"
                self._order[identifier] = Citation(marker=marker, evidence_id=identifier)

    @classmethod
    def for_report(cls, report: ResearchReport) -> CitationIndex:
        return cls(report.evidence_ids())

    def __len__(self) -> int:
        return len(self._order)

    def __iter__(self) -> Iterable[Citation]:
        return iter(self._order.values())

    @property
    def citations(self) -> list[Citation]:
        return list(self._order.values())

    def marker(self, evidence_id: uuid.UUID) -> str:
        """The marker for one id.

        An id that is not in the index still gets a marker rather than an
        exception: a renderer's job is to render. It falls back to the short
        form of the id itself, which is traceable even though it is not numbered.
        """
        citation = self._order.get(evidence_id)
        if citation is None:
            return str(evidence_id).split("-")[0]
        return citation.marker

    def markers(self, evidence_ids: Sequence[uuid.UUID]) -> str:
        """`[E1, E4]` — the inline form, or the empty string when nothing is cited."""
        if not evidence_ids:
            return ""
        return "[" + ", ".join(self.marker(identifier) for identifier in evidence_ids) + "]"
