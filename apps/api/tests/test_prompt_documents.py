"""How uploaded documents reach a node's prompt.

`gather` returns evidence newest-first, which is the right order for metrics
rows and no order at all for the pages of one PDF. These tests pin the three
properties that make the difference between a document a model can read and a
shuffled pile of paragraphs: reading order, a budget, and a citation
instruction that only appears when there is something to cite.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from agent.db.models import Evidence, EvidenceSource
from agent.documents import BRAND_DOC
from agent.nodes import prompts
from agent.nodes.gather import Gathered, Need, collect, coverage_notes


def passage(filename: str, section: str, index: int, text: str) -> Evidence:
    return Evidence(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        source=EvidenceSource.UPLOAD,
        kind=BRAND_DOC,
        payload={
            "document_id": str(uuid.uuid4()),
            "filename": filename,
            "section": section,
            "passage": index,
            "text": text,
        },
        hash=f"{filename}-{index}",
    )


def test_passages_are_rendered_in_reading_order_not_fetch_order() -> None:
    shuffled = [
        passage("brand.pdf", "page 3", 3, "third"),
        passage("brand.pdf", "page 1", 1, "first"),
        passage("brand.pdf", "page 2", 2, "second"),
    ]
    block = prompts.documents_block(Gathered(evidence=shuffled), BRAND_DOC)

    assert block.index("first") < block.index("second") < block.index("third")


def test_every_passage_arrives_with_the_id_a_node_must_copy() -> None:
    rows = [passage("brand.pdf", "page 1", 1, "the only sentence")]
    block = prompts.documents_block(Gathered(evidence=rows), BRAND_DOC)

    assert f"id: {rows[0].id}" in block
    assert "[brand.pdf — page 1]" in block
    # Prose, not the payload JSON a model would quote the braces out of.
    assert '{"text"' not in block


def test_the_block_is_budgeted_and_says_what_it_left_out() -> None:
    rows = [passage("brand.pdf", f"page {i}", i, "x" * 400) for i in range(1, 21)]
    block = prompts.documents_block(Gathered(evidence=rows), BRAND_DOC, budget=1_000)

    assert len(block) < 3_000
    assert "further passages were not included" in block


def test_no_documents_renders_nothing_at_all() -> None:
    empty = prompts.documents_block(Gathered(evidence=[]), BRAND_DOC)

    assert empty == ""
    # `compose` drops it, so the prompt has no heading with nothing under it.
    assert "business context" not in prompts.compose("PROJECT\n  name: x", empty)


def test_the_citation_instruction_appears_only_when_there_is_something_to_cite() -> None:
    rows = [passage("brand.pdf", "page 1", 1, "a sentence worth citing")]
    filled = prompts.documents_block(Gathered(evidence=rows), BRAND_DOC)

    with_documents = prompts.cite_from("EVIDENCE — our own pages", prompts.documents_cite(filled))
    without = prompts.cite_from("EVIDENCE — our own pages", prompts.documents_cite(""))

    assert prompts.DOCUMENTS_LABEL in with_documents
    assert prompts.DOCUMENTS_LABEL not in without
    assert without.count(",") == 0


async def test_an_optional_need_that_finds_nothing_is_not_a_coverage_gap() -> None:
    """Nobody uploading no documents has degraded anything.

    `coverage` travels all the way to the report's `degraded_sources`, so an
    optional source reported as missing would put "insufficient evidence" on a
    section that has all the evidence it was ever going to get. This drives
    `collect` itself rather than asserting on the flag, because the flag is not
    the behaviour — the missing list is.
    """
    found = await collect(_EmptyStore(), Need("crm_won"), Need(BRAND_DOC, optional=True))

    assert found.missing == ["crm_won"]
    assert coverage_notes(found) == ["crm_won: unavailable"]


class _EmptyResult:
    def scalars(self) -> _EmptyResult:
        return self

    def all(self) -> list[Evidence]:
        return []


class _EmptyDb:
    async def execute(self, *_args: object, **_kwargs: object) -> _EmptyResult:
        return _EmptyResult()


class _EmptyStore:
    """The parts of `RunContext` that `collect` touches, and nothing else."""

    def __init__(self) -> None:
        self.db = _EmptyDb()
        self.project = SimpleNamespace(id=uuid.uuid4())
        self.scratch: dict[str, object] = {}
