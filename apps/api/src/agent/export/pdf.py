"""PDF rendering with WeasyPrint (PRD §12).

§12's requirements — A4, a cover page with project, date and run id, an
automatic TOC via CSS counters, a running header and footer with page numbers,
inline SVG charts, embedded creative screenshots — are all satisfied in
`templates/report.css`. This module's job is narrower: assemble the document,
resolve the images, and hand it to the printer.

**WeasyPrint is imported inside the function, never at module scope.** It binds
to pango, cairo and harfbuzz at import time, and the `api` image is deliberately
slim and browser-free (PRD §5.1) — exports are generated in `worker`, which owns
the Volume. A module-level import would make `agent.export` unimportable in the
API process, which is exactly where the routes live. Importing lazily keeps the
route module importable and turns a missing system library into one clear error
on the one code path that actually needs it.

The 15 MB acceptance ceiling is held by capping how many screenshots are
embedded, and the document says how many it left out. 200 PNG screenshots
inlined as base64 is 40–60 MB before the text is counted, so an uncapped
gallery does not "sometimes" breach the ceiling — it always does.
"""

from __future__ import annotations

import base64
import mimetypes
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from agent.export.charts import build_charts
from agent.export.contract import ResearchReport
from agent.export.templating import TEMPLATE_DIR, html_env
from agent.export.view import build_print_context

log = structlog.get_logger(__name__)

TEMPLATE_NAME = "report.html.j2"
STYLESHEET = TEMPLATE_DIR / "report.css"

#: How many creative screenshots are reproduced in the PDF. See the module
#: docstring: this is what keeps the file under §12's 15 MB ceiling. The gallery
#: prints how many it omitted, so the cap is never silent.
SCREENSHOT_LIMIT = 18

#: Refuse to inline a single image larger than this. One oversized capture would
#: otherwise consume the whole budget the cap above is protecting.
SCREENSHOT_MAX_BYTES = 600 * 1024

#: An image loader takes a storage key and returns the bytes, or None when the
#: object is gone. `storage.backend.StorageBackend.get` wrapped to return None
#: rather than raise is the expected implementation.
ImageLoader = Callable[[str], bytes | None]


@dataclass(frozen=True, slots=True)
class Screenshot:
    """One creative capture, ready to inline."""

    advertiser: str
    headline: str | None
    data_uri: str


def _data_uri(key: str, payload: bytes) -> str:
    media_type, _ = mimetypes.guess_type(key)
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:{media_type or 'image/png'};base64,{encoded}"


def collect_screenshots(
    report: ResearchReport,
    loader: ImageLoader | None,
    *,
    limit: int = SCREENSHOT_LIMIT,
    max_bytes: int = SCREENSHOT_MAX_BYTES,
) -> tuple[list[Screenshot], int]:
    """Resolve up to `limit` creative screenshots. Returns them and the total available.

    A missing or oversized object is skipped with a log line, never raised: a
    screenshot that fell off the Volume is a degraded figure, not a failed
    export, and the report is still the deliverable without it.
    """
    candidates = [ad for ad in report.competitive_landscape.ads if ad.screenshot_path]
    total = len(candidates)
    if loader is None or not candidates:
        return [], total

    resolved: list[Screenshot] = []
    for ad in candidates:
        if len(resolved) >= limit:
            break
        key = ad.screenshot_path or ""
        try:
            payload = loader(key)
        except Exception as exc:  # noqa: BLE001 — a bad key must not fail the export
            log.warning("export.screenshot_unreadable", key=key, error=str(exc))
            continue
        if not payload:
            log.warning("export.screenshot_missing", key=key)
            continue
        if len(payload) > max_bytes:
            log.warning("export.screenshot_oversized", key=key, bytes=len(payload))
            continue
        resolved.append(
            Screenshot(
                advertiser=ad.advertiser, headline=ad.headline, data_uri=_data_uri(key, payload)
            )
        )
    return resolved, total


def render_html(
    report: ResearchReport,
    *,
    project_name: str | None = None,
    image_loader: ImageLoader | None = None,
) -> str:
    """The print document as HTML. Useful on its own for debugging a layout."""
    screenshots, total = collect_screenshots(report, image_loader)
    context: dict[str, Any] = build_print_context(
        report,
        project_name=project_name,
        charts=build_charts(report),
        screenshots=screenshots,
        screenshot_total=total,
    )
    template = html_env().get_template(TEMPLATE_NAME)
    return template.render(**context)


def render_pdf(
    report: ResearchReport,
    *,
    project_name: str | None = None,
    image_loader: ImageLoader | None = None,
) -> bytes:
    """The report as a PDF. Raises `PdfBackendUnavailable` if the system libs are absent."""
    html = render_html(report, project_name=project_name, image_loader=image_loader)
    return html_to_pdf(html)


class PdfBackendUnavailable(RuntimeError):
    """WeasyPrint could not be loaded — its native dependencies are missing.

    Raised instead of `ImportError` so the export job can record a message that
    names the actual problem. `ImportError: cannot load library 'libgobject'`
    reaches an operator as a mystery; this does not.
    """


def html_to_pdf(html: str, *, base_url: Path | None = None) -> bytes:
    """Print one HTML string. The only place WeasyPrint is touched."""
    try:
        from weasyprint import CSS, HTML  # noqa: PLC0415 — see the module docstring
    except (ImportError, OSError) as exc:  # pragma: no cover — environment-shaped
        raise PdfBackendUnavailable(
            "WeasyPrint could not load its native dependencies (pango, cairo, "
            "harfbuzz). PDF export runs in the `worker` service, whose image "
            f"installs them; the slim `api` image does not. Underlying error: {exc}"
        ) from exc

    document = HTML(string=html, base_url=str(base_url or TEMPLATE_DIR))
    payload = document.write_pdf(stylesheets=[CSS(filename=str(STYLESHEET))])
    if payload is None:  # pragma: no cover — write_pdf only returns None with a target
        raise PdfBackendUnavailable("WeasyPrint returned no document")
    return bytes(payload)
