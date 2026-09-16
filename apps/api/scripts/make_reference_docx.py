#!/usr/bin/env python
"""Generate `src/agent/export/templates/reference.docx`.

PRD §12 renders DOCX through "python-docx + a `reference.docx` style template".
The template is a binary, so this script is how it is made: the look of the Word
export is a reviewable diff here rather than an opaque blob in git, and anyone
can regenerate it after changing a colour or a font.

It only defines styles. The document body is emptied by `export/docx.py` on
every render (`_clear_body`), so anything typed into the template by hand would
be discarded — which is the right behaviour, and the reason this file writes no
content.

Run: `uv run python scripts/make_reference_docx.py`
"""

from __future__ import annotations

import sys
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt, RGBColor

OUT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "agent"
    / "export"
    / "templates"
    / "reference.docx"
)

#: Matches `templates/report.css`, so the PDF and the DOCX are the same document.
ACCENT = RGBColor(0x0B, 0x6B, 0x5E)
INK = RGBColor(0x1F, 0x29, 0x33)
MUTED = RGBColor(0x66, 0x70, 0x79)

#: A face Word has on every platform. A reference template that names a font the
#: reader does not have is a document that silently reflows.
BODY_FONT = "Calibri"
HEADING_FONT = "Calibri"


def main() -> int:
    document = Document()

    normal = document.styles["Normal"]
    normal.font.name = BODY_FONT
    normal.font.size = Pt(10)
    normal.font.color.rgb = INK
    normal.paragraph_format.space_after = Pt(6)

    title = document.styles["Title"]
    title.font.name = HEADING_FONT
    title.font.size = Pt(26)
    title.font.bold = True
    title.font.color.rgb = INK
    title.paragraph_format.space_after = Pt(4)

    # Heading 1–3 carry the TOC. `\o "1-3"` in the field instruction collects
    # exactly these three levels, so a fourth would silently never appear.
    for level, size, colour, space_before in (
        (1, 18, ACCENT, 0),
        (2, 12.5, INK, 12),
        (3, 10.5, INK, 8),
    ):
        style = document.styles[f"Heading {level}"]
        style.font.name = HEADING_FONT
        style.font.size = Pt(size)
        style.font.bold = True
        style.font.color.rgb = colour
        style.paragraph_format.space_before = Pt(space_before)
        style.paragraph_format.space_after = Pt(4)
        style.paragraph_format.keep_with_next = True

    for name in ("List Bullet", "List Number"):
        style = document.styles[name]
        style.font.name = BODY_FONT
        style.font.size = Pt(10)
        style.paragraph_format.space_after = Pt(3)

    footer = document.styles["Footer"]
    footer.font.name = BODY_FONT
    footer.font.size = Pt(8)
    footer.font.color.rgb = MUTED
    footer.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.LEFT

    # `Table Grid` is what gives a Word table its borders. Without a styled
    # table, every table in the report renders as loose text.
    table_style = document.styles["Table Grid"]
    table_style.font.name = BODY_FONT
    table_style.font.size = Pt(8)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(OUT))
    print(f"wrote {OUT.relative_to(Path.cwd())} ({OUT.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
