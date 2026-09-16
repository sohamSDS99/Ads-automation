"""Markdown rendering — the source of truth for every other format (PRD §12).

PRD §11: "`markdown` is rendered from this object by a deterministic Jinja2
template — the LLM does not write the final markdown. This guarantees PDF/DOCX/
JSON never diverge."

Deterministic is a testable word, so the module treats it as one. Given the same
`ResearchReport` this function returns the same bytes: no clock is read, nothing
is sorted by a hash, and the one ordering decision that could wobble — which
keywords make the preview table — is a total sort on (volume, cpc, term).
`tests/test_report_markdown.py` renders the golden report twice and compares.

The rendering is stored on `Report.markdown` at synthesis time (P5b) and served
from there, so a later template change cannot retroactively alter a report
someone has already read and acted on.
"""

from __future__ import annotations

import re

from agent.export.contract import ResearchReport
from agent.export.templating import markdown_env
from agent.export.view import build_context

TEMPLATE_NAME = "report.md.j2"

#: Three or more blank lines collapse to two. Conditional blocks in a long
#: template leave ragged gaps behind, and the alternative — whitespace control
#: on every `{% if %}` — makes the template unreadable to protect an invisible
#: character.
_EXCESS_BLANK_LINES = re.compile(r"\n{3,}")


def render_markdown(report: ResearchReport, *, project_name: str | None = None) -> str:
    """The report as markdown. Same input, same bytes, every time."""
    env = markdown_env()
    template = env.get_template(TEMPLATE_NAME)
    rendered = template.render(**build_context(report, project_name=project_name))
    return normalise(rendered)


def normalise(markdown: str) -> str:
    """Collapse runaway blank lines and guarantee exactly one trailing newline."""
    collapsed = _EXCESS_BLANK_LINES.sub("\n\n", markdown)
    # Trailing whitespace on a line is invisible in review and shows up as a diff
    # forever after; strip it once here rather than policing it in the template.
    lines = [line.rstrip() for line in collapsed.split("\n")]
    return "\n".join(lines).strip("\n") + "\n"
