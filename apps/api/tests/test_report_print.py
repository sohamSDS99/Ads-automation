"""The printed formats: HTML/PDF (WeasyPrint) and DOCX (python-docx), PRD §12.

§12 attaches two acceptance clauses to these, and both are checked here as
mechanism rather than appearance: the PDF's page furniture comes from CSS that
must actually be wired up, and the DOCX's TOC must be a field Word will fill —
not text that merely looks like one.

The PDF tests skip when WeasyPrint's native libraries are absent, so a laptop
without pango can still run the unit suite. They do NOT skip in either container
(both images install them), and `scripts/verify-p5a.sh` asserts real PDF bytes
against the running stack, so a skip here cannot pass the phase.
"""

from __future__ import annotations

import io
import re
import zipfile

import pytest

from agent.export.docx import render_docx
from agent.export.pdf import SCREENSHOT_LIMIT, collect_screenshots, render_html, render_pdf
from agent.export.templating import TEMPLATE_DIR
from agent.export.view import SECTION_TITLES
from tests.report_support import PROJECT_NAME, golden_report, minimal_report


@pytest.fixture(scope="module")
def pdf_bytes() -> bytes:
    """The golden report, printed. Skipped only when pango is missing."""
    pytest.importorskip("weasyprint", reason="WeasyPrint's native libraries are not installed")
    return render_pdf(golden_report(), project_name=PROJECT_NAME)


# ---------------------------------------------------------------------------
# print HTML
# ---------------------------------------------------------------------------


def test_the_print_html_carries_every_section() -> None:
    html = render_html(golden_report(), project_name=PROJECT_NAME)
    for title in SECTION_TITLES:
        assert f">{title}</h2>" in html, f"not a heading in the print HTML: {title}"


def test_the_toc_links_resolve_to_real_anchors() -> None:
    """`target-counter(attr(href), page)` silently prints 0 for a dangling href."""
    html = render_html(golden_report(), project_name=PROJECT_NAME)
    hrefs = set(re.findall(r'class="toc-link" href="#([^"]+)"', html))
    ids = set(re.findall(r'<section id="([^"]+)"', html))
    assert hrefs, "the contents page rendered no links"
    assert hrefs <= ids, f"contents entries point nowhere: {sorted(hrefs - ids)}"


def test_the_stylesheet_wires_up_the_page_furniture() -> None:
    """§12 asks for a cover, an auto TOC and page numbers. All three live in CSS."""
    css = (TEMPLATE_DIR / "report.css").read_text(encoding="utf-8")
    assert "size: A4" in css
    assert "target-counter(attr(href), page)" in css
    assert 'content: counter(page) " / " counter(pages)' in css
    assert "@page cover" in css
    assert "string-set: section-title content()" in css
    assert "display: table-header-group" in css, "long tables must repeat their header"


def test_charts_are_inline_svg_with_no_external_reference() -> None:
    html = render_html(golden_report(), project_name=PROJECT_NAME)
    assert html.count("<svg") == 3
    assert '<img src="http' not in html


def test_screenshots_are_capped_and_the_document_says_so() -> None:
    report = golden_report()
    ad = report.competitive_landscape.ads[0]
    report.competitive_landscape.ads = [ad.model_copy() for _ in range(SCREENSHOT_LIMIT + 10)]

    resolved, total = collect_screenshots(report, lambda _key: b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    assert total == SCREENSHOT_LIMIT + 10
    assert len(resolved) == SCREENSHOT_LIMIT

    html = render_html(
        report,
        project_name=PROJECT_NAME,
        image_loader=lambda _key: b"\x89PNG\r\n\x1a\n" + b"0" * 64,
    )
    assert f"{SCREENSHOT_LIMIT} of {SCREENSHOT_LIMIT + 10} screenshots" in html


def test_an_oversized_or_missing_screenshot_is_skipped_not_fatal() -> None:
    """A capture that fell off the Volume is a degraded figure, not a failed export."""
    report = golden_report()

    missing, total = collect_screenshots(report, lambda _key: None)
    assert total == 2 and missing == []

    oversized, _ = collect_screenshots(report, lambda _key: b"0" * 5_000_000)
    assert oversized == []

    exploding, _ = collect_screenshots(report, _raise)
    assert exploding == []


def _raise(_key: str) -> bytes:
    raise OSError("volume not mounted")


def test_no_screenshots_are_embedded_without_a_loader() -> None:
    """`api` has no Volume; rendering there must not pretend otherwise."""
    resolved, total = collect_screenshots(golden_report(), None)
    assert resolved == [] and total == 2


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


def test_the_pdf_is_a_pdf(pdf_bytes: bytes) -> None:
    assert pdf_bytes.startswith(b"%PDF-")
    assert len(pdf_bytes) > 10_000


def test_the_pdf_stays_well_under_the_15mb_ceiling(pdf_bytes: bytes) -> None:
    """§12's acceptance, measured rather than assumed."""
    assert len(pdf_bytes) < 15 * 1024 * 1024


def test_an_empty_report_still_prints() -> None:
    pytest.importorskip("weasyprint", reason="WeasyPrint's native libraries are not installed")
    assert render_pdf(minimal_report()).startswith(b"%PDF-")


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def docx_zip() -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(render_docx(golden_report(), project_name=PROJECT_NAME)))


def part(archive: zipfile.ZipFile, name: str) -> str:
    return archive.read(name).decode("utf-8")


def test_the_toc_is_a_real_field_marked_dirty(docx_zip: zipfile.ZipFile) -> None:
    """§12: "DOCX opens in Word 2019+ with a working TOC after F9"."""
    document = part(docx_zip, "word/document.xml")
    assert 'TOC \\o "1-3" \\h \\z \\u' in document
    assert 'w:dirty="true"' in document
    assert 'w:fldCharType="begin"' in document
    assert 'w:fldCharType="end"' in document
    # `fldSimple` caches its result and Word will not reliably refresh it.
    assert "fldSimple" not in document


def test_word_is_told_to_update_fields_on_open(docx_zip: zipfile.ZipFile) -> None:
    assert "updateFields" in part(docx_zip, "word/settings.xml")


def test_headings_use_real_styles_so_the_toc_can_collect_them(
    docx_zip: zipfile.ZipFile,
) -> None:
    """A TOC field collects by style, not by font size."""
    document = part(docx_zip, "word/document.xml")
    used = set(re.findall(r'w:pStyle w:val="(Heading\d)"', document))
    assert {"Heading1", "Heading2", "Heading3"} <= used


def test_the_footer_numbers_pages_with_fields_not_text(docx_zip: zipfile.ZipFile) -> None:
    footers = [name for name in docx_zip.namelist() if name.startswith("word/footer")]
    assert footers, "the document has no footer"
    combined = "".join(part(docx_zip, name) for name in footers)
    assert " PAGE " in combined
    assert " NUMPAGES " in combined


def test_every_section_reaches_the_docx(docx_zip: zipfile.ZipFile) -> None:
    document = part(docx_zip, "word/document.xml")
    for title in SECTION_TITLES:
        assert title in document, f"missing from the DOCX: {title}"


def test_tables_are_native_word_tables(docx_zip: zipfile.ZipFile) -> None:
    document = part(docx_zip, "word/document.xml")
    assert document.count("<w:tbl>") >= 10
    assert 'w:tblStyle w:val="TableGrid"' in document


def test_an_empty_report_still_produces_a_document() -> None:
    archive = zipfile.ZipFile(io.BytesIO(render_docx(minimal_report())))
    document = part(archive, "word/document.xml")
    for title in SECTION_TITLES:
        assert title in document
    assert "cites no evidence" in document
