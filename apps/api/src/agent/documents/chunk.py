"""Sections into passages, passages into evidence drafts.

A passage is the unit a node cites, so its size is a prompt decision rather than
a storage one. Too large and four of them fill a context window; too small and a
citation points at half a sentence. `TARGET_CHARS` is set where a passage is
still a quotable paragraph — roughly 250 words — and passages never span two
sections, so an id that says "page 4" is only ever page 4.

Splitting prefers, in order: a blank line, a sentence end, a line break, a word
boundary. Cutting mid-word is the one outcome worth avoiding entirely: it
produces text a model reads as a typo and quotes back as one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from agent.db.models import EvidenceSource
from agent.documents.extract import BRAND_DOC, ExtractedDocument, Section
from agent.evidence.normalize import EvidenceDraft

#: Characters per passage, before the overlap is added.
TARGET_CHARS = 1_100

#: A passage shorter than this is folded into the next one rather than stored on
#: its own. A one-line heading is not evidence of anything by itself.
MIN_CHARS = 120

#: Carried from the end of one passage into the start of the next, so a sentence
#: that straddles a boundary is readable in at least one of them.
OVERLAP_CHARS = 120

#: A ceiling per document, independent of the character budget. A 400-page PDF
#: that squeaks under `document_max_chars` would otherwise write thousands of
#: rows, each of which is embedded, and the upload request would time out.
MAX_PASSAGES = 400

_SENTENCE_END = re.compile(r"(?<=[.!?;:])\s+")


@dataclass(frozen=True, slots=True)
class Passage:
    """One citable piece of a document."""

    index: int
    label: str
    text: str


def passages(document: ExtractedDocument) -> list[Passage]:
    """Split an extracted document into passages, in reading order."""
    found: list[Passage] = []
    for section in document.sections:
        for body in _split(section):
            if len(found) >= MAX_PASSAGES:
                return found
            found.append(Passage(index=len(found), label=section.label, text=body))
    return _absorb_stubs(found)


def to_drafts(
    document: ExtractedDocument,
    *,
    document_id: str,
    found: list[Passage] | None = None,
) -> list[EvidenceDraft]:
    """Passages as evidence drafts, one row each.

    `document_id` is part of every payload, which makes the content hash
    document-scoped: the same paragraph in two uploaded files produces two rows
    rather than one shared row that deleting either file would take away from
    the other.
    """
    items = passages(document) if found is None else found
    total = len(items)
    return [
        EvidenceDraft(
            source=EvidenceSource.UPLOAD,
            kind=BRAND_DOC,
            payload={
                "document_id": document_id,
                "filename": document.filename,
                "section": item.label,
                "passage": item.index + 1,
                "passages": total,
                "text": item.text,
            },
            # The default rendering would embed the JSON keys alongside the
            # prose. What the retriever should match on is the prose, with just
            # enough of a header to keep the source identifiable.
            content_text=f"{document.filename} — {item.label}\n{item.text}",
        )
        for item in items
    ]


def _split(section: Section) -> list[str]:
    """One section into passage-sized pieces, cutting at the best nearby seam."""
    text = section.text.strip()
    if not text:
        return []
    if len(text) <= TARGET_CHARS:
        return [text]

    pieces: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + TARGET_CHARS, len(text))
        if end < len(text):
            end = _seam(text, start, end)
        piece = text[start:end].strip()
        if piece:
            pieces.append(piece)
        if end >= len(text):
            break
        # Step back by the overlap, but never behind where this piece started —
        # a section of short lines could otherwise loop forever.
        start = max(end - OVERLAP_CHARS, start + 1)
    return pieces


def _seam(text: str, start: int, end: int) -> int:
    """The best place to cut at or before `end`, never before halfway."""
    floor = start + TARGET_CHARS // 2
    window = text[start:end]

    paragraph = window.rfind("\n\n")
    if paragraph != -1 and start + paragraph > floor:
        return start + paragraph

    sentences = list(_SENTENCE_END.finditer(window))
    for match in reversed(sentences):
        if start + match.end() > floor:
            return start + match.end()

    line = window.rfind("\n")
    if line != -1 and start + line > floor:
        return start + line

    space = window.rfind(" ")
    if space != -1 and start + space > floor:
        return start + space
    return end


def _absorb_stubs(found: list[Passage]) -> list[Passage]:
    """Fold a too-short passage into its neighbour, keeping the neighbour's label.

    Headings and one-line rows arrive as their own sections and would otherwise
    become evidence rows carrying three words. A model citing one of those has
    cited nothing, and the row still costs an embedding.
    """
    if len(found) < 2:
        return found
    merged: list[Passage] = []
    carry: str | None = None
    for item in found:
        text = f"{carry}\n{item.text}" if carry else item.text
        carry = None
        if len(text) < MIN_CHARS:
            carry = text
            continue
        merged.append(Passage(index=len(merged), label=item.label, text=text))
    if carry:
        if merged:
            last = merged[-1]
            merged[-1] = Passage(index=last.index, label=last.label, text=f"{last.text}\n{carry}")
        else:
            merged.append(Passage(index=0, label=found[0].label, text=carry))
    return merged
