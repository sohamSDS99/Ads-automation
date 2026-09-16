"""The five formats must not disagree (PRD §11/§12).

§11's design claim is that rendering every format from one validated object
"guarantees PDF/DOCX/JSON never diverge", and §12's acceptance is that the PDF
and the DOCX "contain every section present in the JSON". A guarantee nobody
checks is a hope, so this checks it.

The comparison is deliberately about *content that would be lost*, not about
formatting. Asserting that three renderers produce the same bytes would be a
test of the golden fixture; asserting that a finding present in the JSON reaches
a reader in every format is a test of the renderers.
"""

from __future__ import annotations

import io
import json
import zipfile

from agent.export.docx import render_docx
from agent.export.markdown import render_markdown
from agent.export.pdf import render_html
from agent.export.tabular import render_csv, render_json
from agent.export.view import SECTION_TITLES
from tests.report_support import PROJECT_NAME, golden_report


def renderings() -> dict[str, str]:
    """Every prose format, as searchable text."""
    report = golden_report()
    archive = zipfile.ZipFile(io.BytesIO(render_docx(report, project_name=PROJECT_NAME)))
    return {
        "markdown": render_markdown(report, project_name=PROJECT_NAME),
        "html": render_html(report, project_name=PROJECT_NAME),
        "docx": archive.read("word/document.xml").decode("utf-8"),
    }


def test_every_section_appears_in_every_prose_format() -> None:
    for name, text in renderings().items():
        for title in SECTION_TITLES:
            assert title in text, f"{name} is missing the section: {title}"


def test_the_launch_verdict_is_the_same_everywhere() -> None:
    """The one line a reader acts on. A format that renders it differently is a lie."""
    for name, text in renderings().items():
        assert "Go, with fixes" in text, f"{name} does not state the launch verdict"


def test_every_launch_blocker_reaches_every_prose_format() -> None:
    report = golden_report()
    for name, text in renderings().items():
        for blocker in report.launch_blockers:
            # The first clause is enough: the statements are long, and XML
            # escaping in the DOCX rewrites punctuation later in the sentence.
            opening = blocker.statement.split(":")[0].split(",")[0]
            assert opening in text, f"{name} dropped a launch blocker: {opening!r}"


def test_every_recommended_action_reaches_every_prose_format() -> None:
    report = golden_report()
    for name, text in renderings().items():
        for action in report.recommended_next_actions:
            opening = action.statement.split(";")[0].split(",")[0]
            assert opening in text, f"{name} dropped a next action: {opening!r}"


def test_evidence_markers_are_the_same_in_every_prose_format() -> None:
    """Citations are numbered by document order, so the numbering must agree."""
    report = golden_report()
    expected = len(report.evidence_ids())
    for name, text in renderings().items():
        assert f"E{expected}" in text, f"{name} is missing marker E{expected}"
        assert "E1" in text, f"{name} is missing marker E1"


def test_the_csv_holds_every_keyword_even_when_the_prose_truncates() -> None:
    """The prose formats preview; the CSV is the complete list."""
    report = golden_report()
    csv_text = render_csv(report).decode("utf-8-sig")
    for keyword in report.priced_keyword_list:
        assert keyword.term in csv_text


def test_the_json_holds_everything_the_prose_summarises() -> None:
    report = golden_report()
    payload = json.loads(render_json(report))

    # The prose caps the creative corpus; the JSON does not.
    assert len(payload["competitive_landscape"]["ads"]) == len(report.competitive_landscape.ads)
    assert len(payload["priced_keyword_list"]) == len(report.priced_keyword_list)
    assert payload["degraded_sources"] == report.degraded_sources
    assert payload["cost_usd"] == report.cost_usd
