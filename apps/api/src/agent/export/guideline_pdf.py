"""Guideline PDF, printed with WeasyPrint (Stage 03 PRD §14).

The Stage 03 sibling of `export/plan_pdf.py` and it keeps that module's one hard
rule: **WeasyPrint is imported inside the function, never at module scope.** It
binds to pango, cairo and harfbuzz at import time, and the `api` image is
deliberately slim and browser-free — exports are generated in `worker`, which
owns the Volume. A module-level import would make `agent.export` unimportable in
the API process, which is where the routes live.

§14's PDF requirements — cover, TOC, voice and lexicon, visual identity, the
full claims register with status and expiry per claim, policy profile, asset
spec sheet, governance and the sign-off page listing G5, G6, H1 and H2 with
decider, method and timestamp — are satisfied by `templates/guideline.html.j2`
and `templates/guideline.css`. This module assembles the document and hands it
to the printer.

The 10 MB ceiling (§14 acceptance 3, at 400 rules, 120 claims and 20 logo
plates) is held by the caps in the template rather than by anything here: the
register renders 500 claims and each rule table 300 rows, and both say how many
they omitted. The XLSX and the ruleset JSON are the exports that carry
everything. An uncapped rule section does not "sometimes" breach the ceiling at
400 rules — a 4,000-rule lexicon always would.
"""

from __future__ import annotations

from pathlib import Path

from agent.export.guideline_contract import ContentGuideline
from agent.export.guideline_view import build_context
from agent.export.pdf import PdfBackendUnavailable
from agent.export.templating import TEMPLATE_DIR, html_env

TEMPLATE_NAME = "guideline.html.j2"
STYLESHEET = TEMPLATE_DIR / "guideline.css"


def render_guideline_html(
    guideline: ContentGuideline,
    *,
    project_name: str | None = None,
    published_by_name: str = "",
    ruleset_version: str = "",
) -> str:
    """The print document as HTML. Useful on its own for debugging a layout."""
    context = build_context(
        guideline,
        project_name=project_name,
        published_by_name=published_by_name,
        ruleset_version=ruleset_version,
    )
    return html_env().get_template(TEMPLATE_NAME).render(**context)


def render_guideline_pdf(
    guideline: ContentGuideline,
    *,
    project_name: str | None = None,
    published_by_name: str = "",
    ruleset_version: str = "",
) -> bytes:
    """The rulebook as a PDF. Raises `PdfBackendUnavailable` if system libs are absent."""
    html = render_guideline_html(
        guideline,
        project_name=project_name,
        published_by_name=published_by_name,
        ruleset_version=ruleset_version,
    )
    return guideline_html_to_pdf(html)


def guideline_html_to_pdf(html: str, *, base_url: Path | None = None) -> bytes:
    """Print one HTML string against the guideline stylesheet."""
    try:
        from weasyprint import CSS, HTML  # noqa: PLC0415 — see the module docstring
    except (ImportError, OSError) as exc:  # pragma: no cover - depends on system libs
        raise PdfBackendUnavailable(
            "WeasyPrint could not load its system libraries (pango, cairo, harfbuzz). "
            "Guideline PDFs are generated in the worker image, which carries them."
        ) from exc

    document = HTML(string=html, base_url=str(base_url or TEMPLATE_DIR))
    return bytes(document.write_pdf(stylesheets=[CSS(filename=str(STYLESHEET))]) or b"")
