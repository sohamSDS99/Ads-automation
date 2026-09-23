"""§14's six acceptance clauses, each as an assertion (Stage 03 PRD §14).

The two that carry the most weight are determinism and the watermark, and both
are tested the way they actually fail rather than the way they are stated:

* **determinism under a moved clock**, not two renders in a row. Two fast
  renders match even when the archive stamps members with `datetime.now()`;
  that is precisely how S2-P5b's XLSX shipped broken for a whole phase, failing
  about one run in a hundred.
* **the watermark on every page**, not on page one. A mark that appears once
  and scrolls away satisfies a naive check and fails §14's actual requirement.
"""

from __future__ import annotations

import datetime as dt
import io
import zipfile
from typing import Any
from unittest.mock import patch

import pytest

from agent.db.models import ExportFormat
from agent.export.guideline_markdown import render_guideline_markdown
from agent.export.guideline_pdf import render_guideline_html
from agent.export.guideline_view import DRAFT_WATERMARK
from agent.export.guideline_xlsx import render_guideline_xlsx
from agent.export.jobs import CONTENT_GUIDELINE_FORMATS, render_guideline
from agent.export.ruleset_json import RulesetExportError, verify
from agent.guardrails import compiler
from agent.guidelines.constants import load_content_constants
from tests.guideline_fixtures import GENERATED_AT, build


def compiled_ruleset() -> tuple[dict[str, Any], str]:
    guideline = build()
    ruleset = compiler.compile(
        guideline.model_dump(mode="json"),
        load_content_constants(),
        (),
        compiled_at=GENERATED_AT,
    )
    return ruleset.model_dump(mode="json"), ruleset.hash


def render(fmt: ExportFormat, guideline: Any = None) -> bytes:
    compiled, digest = compiled_ruleset()
    return render_guideline(
        fmt,
        guideline if guideline is not None else build(),
        project_name="SDS Manager",
        compiled_ruleset=compiled,
        ruleset_hash=digest,
    ).payload


class TestAllSixFormatsGenerate:
    """§21's exit criterion: "all six exports generate"."""

    @pytest.mark.parametrize("fmt", sorted(CONTENT_GUIDELINE_FORMATS, key=lambda f: f.value))
    def test_it_produces_bytes(self, fmt: ExportFormat) -> None:
        assert len(render(fmt)) > 0

    def test_the_set_is_exactly_the_six_section_14_names(self) -> None:
        assert {f.value for f in CONTENT_GUIDELINE_FORMATS} == {
            "pdf",
            "docx",
            "md",
            "json",
            "xlsx",
            "ruleset_json",
        }


class TestTwoExportsAreByteIdentical:
    """§14 acceptance 2, tested the way it fails."""

    @pytest.mark.parametrize("fmt", sorted(CONTENT_GUIDELINE_FORMATS, key=lambda f: f.value))
    def test_identical_across_a_moved_clock(self, fmt: ExportFormat) -> None:
        """Two renders an hour apart, not two in a row.

        `zipfile` stamps archive members with `datetime.now()`, so DOCX and XLSX
        match when rendered inside one second and differ across a tick. A test
        comparing two fast renders passes for months and proves nothing.
        """
        first = render(fmt)

        real = dt.datetime

        class Shifted(dt.datetime):
            @classmethod
            def now(cls, tz: Any = None) -> Any:
                return real.now(tz) + dt.timedelta(hours=1)

        with patch.object(dt, "datetime", Shifted):
            second = render(fmt)
        assert first == second

    def test_the_zip_formats_carry_no_wall_clock(self) -> None:
        """The mechanism behind the clause above, asserted directly."""
        for fmt in (ExportFormat.DOCX, ExportFormat.XLSX):
            members = zipfile.ZipFile(io.BytesIO(render(fmt))).infolist()
            assert {m.date_time for m in members} == {(1980, 1, 1, 0, 0, 0)}, fmt


class TestTheDraftWatermark:
    """§14: "a draft claims register must not be able to circulate as a legal
    sign-off record" — in any format."""

    def test_markdown_carries_it(self) -> None:
        assert DRAFT_WATERMARK in render_guideline_markdown(build(), project_name="X")

    def test_the_print_html_repeats_it_on_every_page(self) -> None:
        """`position: fixed` inside a paged context, not a block in the flow.

        A block would render once and scroll away. The assertion is on the
        class the stylesheet fixes, because that is the mechanism §14's
        requirement actually rests on.
        """
        html = render_guideline_html(build(), project_name="X")
        assert 'class="watermark"' in html
        assert 'class="draft-banner"' in html

        css = __import__("pathlib").Path("src/agent/export/templates/guideline.css").read_text()
        block = css.split(".watermark {", 1)[1].split("}", 1)[0]
        assert "position: fixed" in block

    def test_every_xlsx_sheet_carries_it(self) -> None:
        """A workbook's pages are its sheets."""
        import openpyxl

        book = openpyxl.load_workbook(io.BytesIO(render_guideline_xlsx(build())))
        for name in book.sheetnames:
            assert DRAFT_WATERMARK in str(book[name]["A1"].value), name

    def test_a_published_rulebook_is_not_watermarked(self) -> None:
        guideline = build()
        guideline.status = "published"
        assert DRAFT_WATERMARK not in render_guideline_markdown(guideline, project_name="X")


class TestTheRulesetHandoff:
    """§14 acceptance 1: the hash matches the row byte for byte."""

    def test_a_faithful_ruleset_verifies(self) -> None:
        compiled, digest = compiled_ruleset()
        verify(compiled, stored_hash=digest)  # does not raise

    def test_a_tampered_rule_list_is_refused(self) -> None:
        compiled, digest = compiled_ruleset()
        compiled["rules"] = []
        with pytest.raises(RulesetExportError, match="does not match"):
            verify(compiled, stored_hash=digest)

    def test_an_unparseable_pin_is_refused(self) -> None:
        """The versions are recovered from `ruleset_version`; a pin that does
        not parse is a ruleset whose version is unreadable."""
        compiled, digest = compiled_ruleset()
        compiled["ruleset_version"] = "not-a-version"
        with pytest.raises(RulesetExportError):
            verify(compiled, stored_hash=digest)

    def test_exporting_without_a_ruleset_is_refused_by_name(self) -> None:
        from agent.export.jobs import ExportError

        with pytest.raises(ExportError, match="no compiled ruleset"):
            render_guideline(
                ExportFormat.RULESET_JSON, build(), project_name="X", compiled_ruleset=None
            )


class TestTheFormatsAgree:
    """§14 acceptance 3: every section present in the JSON is present in the PDF."""

    def test_markdown_and_print_html_render_the_same_sections(self) -> None:
        guideline = build()
        markdown = render_guideline_markdown(guideline, project_name="X")
        html = render_guideline_html(guideline, project_name="X")
        for heading in (
            "Brand voice",
            "claims register",
            "Google policy",
            "Asset specifications",
            "Every rule",
        ):
            assert heading in markdown, f"{heading} missing from markdown"
            assert heading in html, f"{heading} missing from the print document"

    def test_the_rule_count_is_the_same_in_both(self) -> None:
        guideline = build()
        expected = f"{len(guideline.rules)} rule"
        assert expected in render_guideline_markdown(guideline, project_name="X")
        assert expected in render_guideline_html(guideline, project_name="X")


class TestTheFilename:
    def test_it_carries_the_version(self) -> None:
        """Two files called `content-guidelines-acme.pdf` in one downloads
        folder is how a legal owner signs the wrong one."""
        compiled, digest = compiled_ruleset()
        rendered = render_guideline(
            ExportFormat.PDF,
            build(version_major=3),
            project_name="SDS Manager",
            compiled_ruleset=compiled,
            ruleset_hash=digest,
        )
        assert rendered.filename == "content-guidelines-sds-manager-v3.0-2026-09-23.pdf"
