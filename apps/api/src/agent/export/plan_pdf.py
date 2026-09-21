"""Plan PDF, printed with WeasyPrint (Stage 02 PRD §14).

The Stage 02 sibling of `export/pdf.py` and it keeps that module's one hard
rule: **WeasyPrint is imported inside the function, never at module scope.** It
binds to pango, cairo and harfbuzz at import time, and the `api` image is
deliberately slim and browser-free — exports are generated in `worker`, which
owns the Volume. A module-level import would make `agent.export` unimportable
in the API process, which is where the routes live.

§14's PDF requirements — cover, TOC, objectives, media plan with forecast
charts, channel-slate timeline, full account structure, measurement plan,
ranked test backlog, the four gate decisions — are satisfied by
`templates/plan.html.j2` and `templates/plan.css`. This module assembles the
document and hands it to the printer.

The 10 MB ceiling (§14 acceptance 2, at 40 campaigns and 4,000 keywords) is
held by `plan_view.CAMPAIGN_PREVIEW_LIMIT` rather than by anything here: the
document renders 25 campaigns and says how many it omitted, and the Editor
CSV is the export that carries all of them. An uncapped structure section
does not "sometimes" breach the ceiling at 4,000 keywords — it always does.
"""

from __future__ import annotations

from pathlib import Path

from agent.export.pdf import PdfBackendUnavailable
from agent.export.plan_charts import build_plan_charts
from agent.export.plan_contract import CampaignPlan
from agent.export.plan_view import build_print_context
from agent.export.templating import TEMPLATE_DIR, html_env

TEMPLATE_NAME = "plan.html.j2"
STYLESHEET = TEMPLATE_DIR / "plan.css"


def render_plan_html(
    plan: CampaignPlan, *, project_name: str | None = None, frozen_by_name: str = ""
) -> str:
    """The print document as HTML. Useful on its own for debugging a layout."""
    context = build_print_context(
        plan,
        project_name=project_name,
        frozen_by_name=frozen_by_name,
        charts=build_plan_charts(plan),
    )
    return html_env().get_template(TEMPLATE_NAME).render(**context)


def render_plan_pdf(
    plan: CampaignPlan, *, project_name: str | None = None, frozen_by_name: str = ""
) -> bytes:
    """The plan as a PDF. Raises `PdfBackendUnavailable` if the system libs are absent."""
    html = render_plan_html(plan, project_name=project_name, frozen_by_name=frozen_by_name)
    return plan_html_to_pdf(html)


def plan_html_to_pdf(html: str, *, base_url: Path | None = None) -> bytes:
    """Print one HTML string against the plan stylesheet."""
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
