"""Text out of the four formats a business context actually arrives in.

PDF, DOCX, CSV and plain text/markdown. Each extractor returns *sections* rather
than one flat string, because a section label — "page 4", "Pricing", "row 120" —
is what lets a node's citation point at a place in the file rather than at the
file. The chunker downstream never merges across a section boundary for the same
reason.

What this module refuses is as important as what it accepts:

* **Legacy `.doc`** is a binary OLE format `python-docx` cannot open. The error
  says "save it as .docx", because the alternative — a 500 from deep inside a
  parser — tells the person nothing they can act on.
* **A PDF with no text layer** is the common scan-of-a-brochure case. `pypdf`
  reports success and empty strings, so the emptiness is detected here and
  named.
* **An encrypted PDF** is refused rather than silently read as zero pages.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field

import structlog

log = structlog.get_logger(__name__)

#: The evidence `kind` an uploaded document's passages are stored under. Lives
#: here rather than in a node module because both the writer (the upload route)
#: and every reader (the nodes) import it, and a typo in one of the two would
#: produce a project whose documents are stored and never read.
BRAND_DOC = "brand_doc"

#: Extension → the label shown when it is refused or accepted. Extension rather
#: than the browser's `Content-Type`: Safari sends `application/octet-stream`
#: for a `.docx` and Windows sends `application/vnd.ms-excel` for some `.csv`s,
#: so the filename is the more reliable of the two signals.
SUPPORTED: dict[str, str] = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".txt": "text/plain",
    ".md": "text/markdown",
}

#: Refused with a sentence that says what to do instead. Anything not in either
#: map gets the generic "not a document we can read" message.
REFUSED: dict[str, str] = {
    ".doc": "Legacy .doc files cannot be read. Open it in Word and save as .docx or PDF.",
    ".pages": "Pages files cannot be read. Export it as PDF or Word (.docx).",
    ".key": "Keynote files cannot be read. Export it as PDF.",
    ".ppt": "PowerPoint files cannot be read. Export it as PDF.",
    ".pptx": "PowerPoint files cannot be read. Export it as PDF.",
    ".xls": "Spreadsheets cannot be read directly. Save the sheet as CSV.",
    ".xlsx": "Spreadsheets cannot be read directly. Save the sheet as CSV.",
    ".rtf": "RTF cannot be read. Save it as .docx, PDF or plain text.",
    ".zip": "Archives cannot be read. Upload the documents inside it one at a time.",
}

#: Rows of a CSV rendered into one section. Small enough that a section stays
#: quotable, large enough that a 2,000-row price list is not 2,000 sections.
CSV_ROWS_PER_SECTION = 20

#: Cells past this are truncated. A CSV export with a 40kB "notes" column is a
#: real thing, and one cell must not become the whole document.
MAX_CELL_CHARS = 400


class DocumentError(ValueError):
    """The upload cannot become text. The message is shown to the person verbatim."""


@dataclass(frozen=True, slots=True)
class Section:
    """One addressable part of a document: where it is, and what it says."""

    label: str
    text: str


@dataclass(slots=True)
class ExtractedDocument:
    """What came out of one file."""

    filename: str
    media_type: str
    sections: list[Section]
    #: Pages for a PDF, rows for a CSV, paragraphs for a DOCX. Reported so the
    #: person can compare it against what they think they uploaded.
    unit: str = ""
    unit_count: int = 0
    #: True when extraction stopped at `max_chars`. Never silent: the upload
    #: response carries it and the UI says so.
    truncated: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n\n".join(section.text for section in self.sections)

    @property
    def char_count(self) -> int:
        return sum(len(section.text) for section in self.sections)


def supported_extensions() -> list[str]:
    """What the file picker should accept, in the order the UI lists them."""
    return list(SUPPORTED)


def extract(filename: str, content: bytes, *, max_chars: int) -> ExtractedDocument:
    """Read `content` as the format its name claims, or refuse with a reason."""
    if not content:
        raise DocumentError("That file is empty.")

    name = (filename or "document").strip() or "document"
    suffix = _suffix(name)
    if suffix in REFUSED:
        raise DocumentError(REFUSED[suffix])
    if suffix not in SUPPORTED:
        raise DocumentError(
            "That file type cannot be read. Upload a PDF, Word (.docx), CSV, or plain text file."
        )

    reader = {
        ".pdf": _from_pdf,
        ".docx": _from_docx,
        ".csv": _from_delimited,
        ".tsv": _from_delimited,
        ".txt": _from_text,
        ".md": _from_text,
    }[suffix]
    document = reader(name, content)
    document.media_type = SUPPORTED[suffix]
    _apply_budget(document, max_chars)

    if not document.char_count:
        raise DocumentError("No text could be read from that file.")
    log.info(
        "document.extracted",
        filename=name,
        sections=len(document.sections),
        chars=document.char_count,
        truncated=document.truncated,
    )
    return document


def _suffix(filename: str) -> str:
    _, dot, suffix = filename.rpartition(".")
    return f".{suffix.lower()}" if dot else ""


def _apply_budget(document: ExtractedDocument, max_chars: int) -> None:
    """Keep whole sections until the budget runs out, then stop.

    Truncating mid-section would hand a node half a sentence and an id that
    claims to be the whole of "page 7". Dropping whole sections loses the tail
    of a long document, which the response says out loud.
    """
    if max_chars <= 0 or document.char_count <= max_chars:
        return
    kept: list[Section] = []
    spent = 0
    for section in document.sections:
        if spent + len(section.text) > max_chars:
            break
        kept.append(section)
        spent += len(section.text)
    dropped = len(document.sections) - len(kept)
    if not kept:
        # One enormous section — a single-page text dump. Cut it at the budget
        # rather than reject a file we can partly read.
        head = document.sections[0]
        kept = [Section(label=head.label, text=head.text[:max_chars])]
        dropped = len(document.sections) - 1
    document.sections = kept
    document.truncated = True
    document.warnings.append(
        f"Only the first {spent or max_chars:,} characters were kept; "
        f"{dropped:,} further {document.unit or 'section'}s were not read."
    )


# --- per-format readers -----------------------------------------------------


def _from_pdf(filename: str, content: bytes) -> ExtractedDocument:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(content))
    except PdfReadError as exc:
        raise DocumentError(f"That PDF could not be opened: {exc}") from exc

    if reader.is_encrypted:
        # An empty user password is common in "protected" exports and decrypts
        # silently; anything else needs a password we do not have.
        try:
            opened = reader.decrypt("")
        except Exception as exc:  # noqa: BLE001 — pypdf raises several types here
            raise DocumentError("That PDF is password-protected.") from exc
        if not opened:
            raise DocumentError(
                "That PDF is password-protected. Save an unprotected copy and upload that."
            )

    sections: list[Section] = []
    for number, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as exc:  # noqa: BLE001 — a damaged page must not lose the rest
            log.warning("document.pdf_page_failed", filename=filename, page=number, error=str(exc))
            continue
        cleaned = _tidy(text)
        if cleaned:
            sections.append(Section(label=f"page {number}", text=cleaned))

    if not sections:
        raise DocumentError(
            "That PDF has no text layer — it looks like a scan or an image export. "
            "Upload a text PDF, a Word file, or paste the content into the box above."
        )

    document = ExtractedDocument(
        filename=filename,
        media_type=SUPPORTED[".pdf"],
        sections=sections,
        unit="page",
        unit_count=len(reader.pages),
    )
    unread = len(reader.pages) - len(sections)
    if unread > 0:
        document.warnings.append(
            f"{unread} of {len(reader.pages)} pages held no readable text "
            "(images or scans) and were skipped."
        )
    return document


def _from_docx(filename: str, content: bytes) -> ExtractedDocument:
    import docx
    from docx.opc.exceptions import PackageNotFoundError

    try:
        document = docx.Document(io.BytesIO(content))
    except PackageNotFoundError as exc:
        raise DocumentError(
            "That file is not a readable .docx. If it came from an older Word, "
            "open it and save as .docx."
        ) from exc

    sections: list[Section] = []
    heading = "start of document"
    buffer: list[str] = []
    paragraphs = 0

    def flush() -> None:
        body = _tidy("\n".join(buffer))
        if body:
            sections.append(Section(label=heading, text=body))
        buffer.clear()

    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if not text:
            continue
        paragraphs += 1
        # Word's own outline is the only section boundary this format offers,
        # and it is the one a reader would have used too.
        if paragraph.style is not None and (paragraph.style.name or "").startswith("Heading"):
            flush()
            heading = text
            buffer.append(text)
            continue
        buffer.append(text)
    flush()

    # Tables carry the pricing grid often enough to be worth reading, and Word
    # keeps them out of `paragraphs` entirely.
    for index, table in enumerate(document.tables, start=1):
        rows = [
            " | ".join(cell.text.strip()[:MAX_CELL_CHARS] for cell in row.cells)
            for row in table.rows
        ]
        body = _tidy("\n".join(row for row in rows if row.replace("|", "").strip()))
        if body:
            sections.append(Section(label=f"table {index}", text=body))

    if not sections:
        raise DocumentError("That Word file contains no text.")
    return ExtractedDocument(
        filename=filename,
        media_type=SUPPORTED[".docx"],
        sections=sections,
        unit="paragraph",
        unit_count=paragraphs,
    )


def _from_delimited(filename: str, content: bytes) -> ExtractedDocument:
    """A CSV read as prose, not as a schema.

    The Data sources step already imports a CRM export properly, with a column
    mapping and one evidence row per deal. This path is for the other kind of
    spreadsheet — a price list, a product matrix — where the value is the words
    in the cells. Each row is rendered `header: value` so a model reading one
    row in isolation still knows what the numbers mean.
    """
    text = _decode(content)
    sample = text[:8192]
    try:
        dialect: type[csv.Dialect] | csv.Dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel_tab if filename.lower().endswith(".tsv") else csv.excel

    reader = csv.reader(io.StringIO(text), dialect)
    try:
        rows = list(reader)
    except csv.Error as exc:
        raise DocumentError(f"That file could not be parsed as a table: {exc}") from exc
    if not rows:
        raise DocumentError("That file has no rows.")

    headers = [cell.strip() or f"column {i + 1}" for i, cell in enumerate(rows[0])]
    body = rows[1:]
    if not body:
        # A single line is a header row or a one-line note; either way the text
        # is the content.
        return ExtractedDocument(
            filename=filename,
            media_type=SUPPORTED[".csv"],
            sections=[Section(label="row 1", text=_tidy(", ".join(headers)))],
            unit="row",
            unit_count=1,
        )

    sections: list[Section] = []
    for start in range(0, len(body), CSV_ROWS_PER_SECTION):
        block = body[start : start + CSV_ROWS_PER_SECTION]
        lines = []
        for offset, row in enumerate(block):
            pairs = [
                f"{headers[i] if i < len(headers) else f'column {i + 1}'}: "
                f"{cell.strip()[:MAX_CELL_CHARS]}"
                for i, cell in enumerate(row)
                if cell.strip()
            ]
            if pairs:
                lines.append(f"row {start + offset + 2}) " + "; ".join(pairs))
        if lines:
            first = start + 2
            last = start + len(block) + 1
            sections.append(Section(label=f"rows {first}–{last}", text=_tidy("\n".join(lines))))

    if not sections:
        raise DocumentError("That file has headers but no data rows.")
    return ExtractedDocument(
        filename=filename,
        media_type=SUPPORTED[".csv"],
        sections=sections,
        unit="row",
        unit_count=len(body),
    )


def _from_text(filename: str, content: bytes) -> ExtractedDocument:
    """Plain text and markdown, split on markdown headings where there are any."""
    text = _tidy(_decode(content))
    if not text:
        raise DocumentError("That file contains no text.")

    sections: list[Section] = []
    heading = "start of document"
    buffer: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^\s{0,3}(#{1,6})\s+(.*\S)\s*$", line)
        if match:
            body = "\n".join(buffer).strip()
            if body:
                sections.append(Section(label=heading, text=body))
            buffer = [line.strip()]
            heading = match.group(2)
            continue
        buffer.append(line)
    tail = "\n".join(buffer).strip()
    if tail:
        sections.append(Section(label=heading, text=tail))

    return ExtractedDocument(
        filename=filename,
        media_type=SUPPORTED[".md"] if filename.lower().endswith(".md") else SUPPORTED[".txt"],
        sections=sections or [Section(label="start of document", text=text)],
        unit="line",
        unit_count=text.count("\n") + 1,
    )


# --- shared helpers ---------------------------------------------------------


def _decode(content: bytes) -> str:
    """UTF-8, then the two encodings Excel actually writes, then give up cleanly."""
    for encoding in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            return content.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
    # Latin-1 decodes any byte sequence, so this cannot raise; mojibake is a
    # better outcome than refusing a file whose text is 99% readable.
    return content.decode("latin-1", errors="replace")


def _tidy(text: str) -> str:
    """Collapse the whitespace a PDF extractor leaves behind.

    Worth doing before anything is measured or budgeted: a PDF page routinely
    extracts as a third whitespace, and a chunk budget spent on blank lines is a
    chunk of the document the model never sees.
    """
    cleaned = text.replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n")
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r" *\n *", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()
