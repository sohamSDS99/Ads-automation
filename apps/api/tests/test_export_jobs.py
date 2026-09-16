"""The export job's pure half: naming, keys and the render dispatch (PRD §12).

The database half lives in `tests/integration/test_report_api.py`. What is here
is everything that can be wrong without a database: a format with no renderer, a
filename built from a project name somebody typed, a key that does not land
where §12 says it should.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from agent.db.models import ExportFormat
from agent.export.jobs import (
    EXTENSIONS,
    MEDIA_TYPES,
    filename_for,
    render,
    slugify,
    storage_key,
)
from tests.report_support import PROJECT_NAME, golden_report

GENERATED = datetime(2026, 3, 4, 9, 30, tzinfo=UTC)


def test_every_format_has_a_media_type_and_an_extension() -> None:
    """A new format must not reach a browser as application/octet-stream."""
    assert set(MEDIA_TYPES) == set(ExportFormat)
    assert set(EXTENSIONS) == set(ExportFormat)


def test_every_format_renders() -> None:
    report = golden_report()
    for fmt in ExportFormat:
        if fmt is ExportFormat.PDF:
            pytest.importorskip("weasyprint", reason="native libraries are not installed")
        rendered = render(fmt, report, project_name=PROJECT_NAME)
        assert rendered.payload, f"{fmt.value} rendered nothing"
        assert rendered.filename.endswith(f".{EXTENSIONS[fmt]}")
        assert rendered.media_type == MEDIA_TYPES[fmt]


def test_markdown_prefers_what_the_run_stored() -> None:
    """A later template edit must not change a report someone already acted on."""
    rendered = render(
        ExportFormat.MD,
        golden_report(),
        project_name=PROJECT_NAME,
        stored_markdown="# As it was written at run time\n",
    )
    assert rendered.payload == b"# As it was written at run time\n"


def test_json_prefers_the_stored_payload() -> None:
    rendered = render(
        ExportFormat.JSON,
        golden_report(),
        project_name=PROJECT_NAME,
        stored_payload={"schema_version": "1.0", "written": "by the run"},
    )
    assert b'"written": "by the run"' in rendered.payload


def test_the_filename_carries_the_reports_date_not_todays() -> None:
    """Re-downloading a March report in June must not produce a June filename."""
    assert (
        filename_for(ExportFormat.PDF, project_name=PROJECT_NAME, generated_at=GENERATED)
        == "paid-ads-research-northwind-safety-2026-03-04.pdf"
    )


def test_a_project_name_cannot_become_a_path() -> None:
    """Project names arrive from users."""
    name = filename_for(ExportFormat.CSV, project_name="../../etc/passwd", generated_at=GENERATED)
    assert "/" not in name
    assert ".." not in name
    assert name == "paid-ads-research-etc-passwd-2026-03-04.csv"


def test_a_project_with_no_usable_name_still_gets_a_filename() -> None:
    for name in (None, "", "   ", "!!!", "—"):
        produced = filename_for(ExportFormat.MD, project_name=name, generated_at=GENERATED)
        assert produced.endswith(".md")
        assert produced.startswith("paid-ads-research")


def test_slugify_is_bounded() -> None:
    assert len(slugify("x" * 500)) <= 60
    assert slugify("") == "report"
    assert slugify("Nörthwind  Safety!") == "n-rthwind-safety"


def test_the_storage_key_is_where_the_prd_says() -> None:
    run_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
    key = storage_key(run_id, "paid-ads-research-2026-03-04.pdf")
    assert key == f"exports/{run_id}/paid-ads-research-2026-03-04.pdf"
    assert not key.startswith("/"), "a storage key is relative, never an absolute path"
