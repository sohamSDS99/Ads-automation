"""Uploaded business-context documents: bytes in, citable passages out.

A person setting up a project knows far more about the brand than five text
boxes can hold — the pricing sheet, the positioning deck, the objection
handling one-pager. This package is the path those files take to become
something a node may cite: sniff the format, pull the text out of it, split it
into passages small enough to sit in a prompt, and hand them to
`evidence.store` as ordinary evidence rows.

Two rules shape everything here:

* **Extraction failures are loud.** A scanned PDF has no text layer and
  `pypdf` returns empty strings for every page rather than an error. Treating
  that as "an empty document" would leave the person looking at a green
  checkmark next to a file the run cannot read, so it raises instead.
* **Nothing reaches a model uncited.** The text does not go into the project's
  configuration block, where it would be repeated to every node and be
  unciteable. It becomes evidence, with an id per passage, which is the only
  channel PRD §18 law 1 allows a factual claim to come through.
"""

from agent.documents.chunk import Passage, passages, to_drafts
from agent.documents.extract import (
    BRAND_DOC,
    DocumentError,
    ExtractedDocument,
    Section,
    extract,
    supported_extensions,
)

__all__ = [
    "BRAND_DOC",
    "DocumentError",
    "ExtractedDocument",
    "Passage",
    "Section",
    "extract",
    "passages",
    "supported_extensions",
    "to_drafts",
]
