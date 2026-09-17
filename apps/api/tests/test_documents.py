"""Uploaded documents: what comes out of each format, and what is refused.

The interesting cases here are all failures, because the failures are the ones
that used to be silent. A scanned PDF extracts as a page of empty strings and a
legacy `.doc` is a binary blob `python-docx` cannot open — both of which can
look like "an empty document" if nobody checks.
"""

from __future__ import annotations

import csv
import io
import zlib

import pytest

from agent.db.models import EvidenceSource
from agent.documents import BRAND_DOC, DocumentError, extract, passages, to_drafts
from agent.documents.chunk import MAX_PASSAGES, MIN_CHARS, TARGET_CHARS

LOREM = (
    "SDS Manager replaces the binder of paper safety data sheets with a searchable "
    "library. Suppliers push updated sheets automatically, and the compliance report "
    "a national regulator asks for is generated rather than assembled by hand. "
)


# --- fixtures: real files, built in memory ----------------------------------


def pdf_bytes(pages: list[str]) -> bytes:
    """A minimal but genuine PDF, one text object per page.

    Hand-built rather than rendered: WeasyPrint needs pango and cairo, which
    only the worker image carries, and this suite has to run on a laptop.
    """
    objects: list[bytes] = []
    page_ids = [4 + index * 2 for index, _ in enumerate(pages)]

    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    for index, text in enumerate(pages):
        content_id = page_ids[index] + 1
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 3 0 R >> >> "
                f"/MediaBox [0 0 612 792] /Contents {content_id} 0 R >>"
            ).encode()
        )
        escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode()
        objects.append(
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
        )

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    start = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode()
    )
    return bytes(out)


def image_only_pdf() -> bytes:
    """One page with a compressed image XObject and no text operators at all."""
    pixels = zlib.compress(b"\xff\xff\xff" * 16)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /XObject << /Im0 5 0 R >> >> >>",
        b"<< /Length 24 >>\nstream\nq 612 0 0 792 0 0 cm Q\nendstream",
        b"<< /Type /XObject /Subtype /Image /Width 4 /Height 4 /ColorSpace /DeviceRGB "
        b"/BitsPerComponent 8 /Filter /FlateDecode /Length "
        + str(len(pixels)).encode()
        + b" >>\nstream\n"
        + pixels
        + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    start = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode()
    )
    return bytes(out)


def docx_bytes(paragraphs: list[tuple[str, str]], table: list[list[str]] | None = None) -> bytes:
    """`[(style, text)]` into a real .docx. Style "Heading 1" makes a section."""
    import docx

    document = docx.Document()
    for style, text in paragraphs:
        document.add_paragraph(text, style=style or None)
    if table:
        grid = document.add_table(rows=len(table), cols=len(table[0]))
        for row_index, row in enumerate(table):
            for cell_index, value in enumerate(row):
                grid.cell(row_index, cell_index).text = value
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def csv_bytes(rows: list[list[str]]) -> bytes:
    buffer = io.StringIO()
    csv.writer(buffer).writerows(rows)
    return buffer.getvalue().encode()


# --- what each format produces ----------------------------------------------


def test_a_pdf_becomes_one_section_per_page() -> None:
    document = extract("pricing.pdf", pdf_bytes(["Tier one is 49 EUR", "Tier two is 99 EUR"]), max_chars=100_000)

    assert [section.label for section in document.sections] == ["page 1", "page 2"]
    assert "Tier one is 49 EUR" in document.sections[0].text
    assert document.unit == "page"
    assert document.unit_count == 2
    assert document.media_type == "application/pdf"
    assert not document.warnings


def test_a_pdf_page_with_no_text_is_reported_not_swallowed() -> None:
    """One readable page and one image page: the file is accepted, the gap is named."""
    readable = pdf_bytes(["The only page with words on it"])
    document = extract("half-scan.pdf", readable, max_chars=100_000)
    assert len(document.sections) == 1

    with pytest.raises(DocumentError, match="no text layer"):
        extract("scan.pdf", image_only_pdf(), max_chars=100_000)


def test_an_encrypted_pdf_is_refused_with_a_reason() -> None:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for page in PdfReader(io.BytesIO(pdf_bytes(["Confidential pricing"]))).pages:
        writer.add_page(page)
    writer.encrypt("hunter2")
    buffer = io.BytesIO()
    writer.write(buffer)

    with pytest.raises(DocumentError, match="password-protected"):
        extract("locked.pdf", buffer.getvalue(), max_chars=100_000)


def test_a_docx_splits_on_its_headings_and_reads_its_tables() -> None:
    content = docx_bytes(
        [
            ("Heading 1", "Positioning"),
            ("", "We sell to EHS managers at mid-sized manufacturers."),
            ("Heading 1", "Pricing"),
            ("", "From 49 EUR per site per month."),
        ],
        table=[["Plan", "Price"], ["Team", "49 EUR"]],
    )
    document = extract("brand.docx", content, max_chars=100_000)

    labels = [section.label for section in document.sections]
    # No "start of document" section: this file opens with a heading, so there
    # is no prose before the first one to put anywhere.
    assert labels == ["Positioning", "Pricing", "table 1"]
    assert "EHS managers" in document.sections[0].text
    assert "Team | 49 EUR" in document.sections[-1].text


def test_a_csv_is_read_as_prose_with_its_headers_on_every_row() -> None:
    content = csv_bytes(
        [["Plan", "Price", "Seats"], ["Team", "49", "10"], ["Site", "199", "unlimited"]]
    )
    document = extract("plans.csv", content, max_chars=100_000)

    assert document.unit == "row"
    assert document.unit_count == 2
    body = document.text
    assert "Plan: Team; Price: 49; Seats: 10" in body
    # The header names travel with the values, so one row read alone still says
    # what its numbers are.
    assert "Price: 199" in body


def test_markdown_splits_on_its_own_headings() -> None:
    content = b"# Who we are\nA compliance company.\n\n## What we charge\nFrom 49 EUR.\n"
    document = extract("about.md", content, max_chars=100_000)

    assert [section.label for section in document.sections] == ["Who we are", "What we charge"]
    assert document.media_type == "text/markdown"


def test_utf16_and_windows_encodings_survive() -> None:
    document = extract("notes.txt", "Pris: 49 € per måned".encode("utf-16"), max_chars=100_000)
    assert "49 € per måned" in document.text


# --- what is refused --------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("old.doc", "save as .docx"),
        ("deck.pptx", "Export it as PDF"),
        ("sheet.xlsx", "Save the sheet as CSV"),
        ("bundle.zip", "one at a time"),
        ("photo.png", "cannot be read"),
        ("noextension", "cannot be read"),
    ],
)
def test_unsupported_formats_say_what_to_do_instead(filename: str, expected: str) -> None:
    with pytest.raises(DocumentError, match=expected):
        extract(filename, b"whatever the bytes are", max_chars=100_000)


def test_an_empty_file_is_refused() -> None:
    with pytest.raises(DocumentError, match="empty"):
        extract("empty.txt", b"", max_chars=100_000)


def test_the_character_budget_drops_whole_pages_and_says_so() -> None:
    pages = [LOREM * 3 for _ in range(6)]
    document = extract("long.pdf", pdf_bytes(pages), max_chars=1_000)

    assert document.truncated
    assert document.char_count <= 1_000
    assert "were not read" in document.warnings[0]
    # Whole sections, never half of one: a citation that says "page 3" has to
    # be the whole of page 3.
    assert all(section.text for section in document.sections)


# --- passages ---------------------------------------------------------------


def test_passages_stay_inside_a_section_and_keep_reading_order() -> None:
    document = extract("brand.md", (f"# One\n{LOREM * 8}\n\n# Two\n{LOREM * 8}").encode(), max_chars=100_000)
    found = passages(document)

    assert len(found) > 2
    assert [item.index for item in found] == list(range(len(found)))
    labels = [item.label for item in found]
    # Every "One" passage precedes every "Two" passage.
    assert labels == sorted(labels, key=lambda label: 0 if label == "One" else 1)
    assert all(len(item.text) <= TARGET_CHARS + 50 for item in found)


def test_passages_never_cut_a_word_in_half() -> None:
    document = extract("long.txt", (LOREM * 20).encode(), max_chars=100_000)
    words = {word.strip(".,;:").lower() for word in LOREM.split()}
    for item in passages(document):
        for word in item.text.split():
            stripped = word.strip(".,;:").lower()
            if stripped.isalpha() and len(stripped) > 3:
                assert stripped in words, stripped


def test_a_stub_section_is_folded_into_its_neighbour() -> None:
    document = extract("tiny.md", b"# A\nshort\n\n# B\n" + (LOREM * 2).encode(), max_chars=100_000)
    found = passages(document)

    assert all(len(item.text) >= MIN_CHARS for item in found[:-1])
    assert "short" in found[0].text


def test_a_document_cannot_write_more_than_the_passage_ceiling() -> None:
    body = "\n\n".join(f"# Section {index}\n{LOREM * 2}" for index in range(MAX_PASSAGES + 50))
    document = extract("huge.md", body.encode(), max_chars=5_000_000)
    assert len(passages(document)) == MAX_PASSAGES


# --- drafts -----------------------------------------------------------------


def test_drafts_carry_the_document_id_so_two_files_never_share_a_row() -> None:
    document = extract("brand.txt", (LOREM * 3).encode(), max_chars=100_000)
    first = to_drafts(document, document_id="11111111-1111-1111-1111-111111111111")
    second = to_drafts(document, document_id="22222222-2222-2222-2222-222222222222")

    assert first[0].source is EvidenceSource.UPLOAD
    assert first[0].kind == BRAND_DOC
    assert first[0].payload["filename"] == "brand.txt"
    assert first[0].payload["passage"] == 1
    # Identical text, different documents: deleting one must not take the
    # other's evidence with it, which only holds if the hashes differ.
    assert {draft.hash() for draft in first}.isdisjoint({draft.hash() for draft in second})


def test_the_embedded_text_is_the_prose_not_the_json() -> None:
    document = extract("brand.txt", (LOREM * 2).encode(), max_chars=100_000)
    draft = to_drafts(document, document_id="33333333-3333-3333-3333-333333333333")[0]
    text = draft.text()

    assert text.startswith("brand.txt — ")
    assert "payload" not in text
    assert "SDS Manager replaces" in text
