"""The creative book, printed with WeasyPrint (Stage 04 PRD §14).

§14's contents — a cover with pins and version; the brief; per ad group the
SERP previews (mobile and desktop), the headline matrix with categories and
character counts, descriptions with their claim chips and variant B with its
hypothesis; extras; a media contact sheet with provenance per asset; landing
audits with before and after; the exceptions with the H3 receipt; a sign-off
page listing G7, G8, G8b and H3 with decider, method and timestamp — are
`creative_book.html.jinja` over `creative_view.build_book`, the context
`creative_book.md.jinja` renders. The markdown is the source of truth; the
print document is the same sections in the same order.

* **Watermarked** `DRAFT — NOT RELEASED` on every page of a package that is
  not released (acceptance 6), with its status and creative run id in every
  footer, released or not.
* **Byte-identical** across exports of one released package (acceptance 4):
  the document's dates are the `dcterms` metadata, pinned to `released_at`
  (a draft: its row's last change) — and one more nobody asked for. The worker
  image has no HarfBuzz-Subset, so WeasyPrint subsets fonts with fontTools,
  whose `TTFont.save` stamps every embedded font's `head.modified` with the
  wall clock: two exports a second apart differed inside the font streams.
  fontTools honours the reproducible-builds `SOURCE_DATE_EPOCH`, so the print
  runs with it pinned to the same time.
* **≤ 25 MB** for §14's load (acceptance 5): images are re-encoded into fixed
  boxes by `creative_view`, and videos appear as their posters.

WeasyPrint is imported inside the function, as in every other PDF module: it
binds to pango and cairo at import, which only the worker image carries.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

from agent.export.creative_sources import CreativeExportSources
from agent.export.creative_view import build_book
from agent.export.pdf import PdfBackendUnavailable
from agent.export.templating import TEMPLATE_DIR, html_env

TEMPLATE_NAME = "creative_book.html.jinja"
STYLESHEET = TEMPLATE_DIR / "creative_book.css"


def render_creative_book_html(sources: CreativeExportSources) -> str:
    """The print document as HTML, images embedded."""
    return html_env().get_template(TEMPLATE_NAME).render(**build_book(sources, images=True))


@contextmanager
def source_date_epoch(stamped: datetime) -> Iterator[None]:
    """`SOURCE_DATE_EPOCH` set to `stamped` for the block, then put back.

    Process-wide, and safe here because the print is synchronous: nothing else
    runs on the worker's event loop until `write_pdf` returns.
    """
    previous = os.environ.get("SOURCE_DATE_EPOCH")
    os.environ["SOURCE_DATE_EPOCH"] = str(int(stamped.timestamp()))
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("SOURCE_DATE_EPOCH", None)
        else:
            os.environ["SOURCE_DATE_EPOCH"] = previous


def render_creative_book_pdf(sources: CreativeExportSources) -> bytes:
    """The creative book as a PDF. Raises `PdfBackendUnavailable` without the system libraries."""
    html = render_creative_book_html(sources)
    try:
        from weasyprint import CSS, HTML  # noqa: PLC0415 — see the module docstring
    except (ImportError, OSError) as exc:  # pragma: no cover - depends on system libs
        raise PdfBackendUnavailable(
            "WeasyPrint could not load its system libraries (pango, cairo, harfbuzz). "
            "Creative books are generated in the worker image, which carries them."
        ) from exc
    document = HTML(string=html, base_url=str(TEMPLATE_DIR))
    with source_date_epoch(sources.stamped):
        pdf = document.write_pdf(stylesheets=[CSS(filename=str(STYLESHEET))])
    return bytes(pdf or b"")
