"""CSV and JSON exports (PRD §12).

The CSV is the one export a person feeds to another tool, so its shape is a
contract with Google Ads Editor rather than a presentation choice. These tests
pin the column names, the order, the encoding and the default status, because
every one of those is something a well-meaning refactor would "tidy".
"""

from __future__ import annotations

import csv
import io
import json

from agent.export.tabular import (
    COLUMNS,
    DEFAULT_STATUS,
    EDITOR_COLUMNS,
    SEASONALITY_COLUMNS,
    render_csv,
    render_json,
    render_json_from_payload,
)
from tests.report_support import golden_payload, golden_report, minimal_report


def parsed_csv() -> list[dict[str, str]]:
    text = render_csv(golden_report()).decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def test_the_editor_columns_come_first_and_in_editors_spelling() -> None:
    """Editor maps by header. A renamed column is a file that does not import."""
    assert COLUMNS[: len(EDITOR_COLUMNS)] == EDITOR_COLUMNS
    assert EDITOR_COLUMNS[:4] == ("Campaign", "Ad Group", "Keyword", "Criterion Type")


def test_every_keyword_becomes_one_row() -> None:
    rows = parsed_csv()
    assert len(rows) == len(golden_report().priced_keyword_list)
    assert rows[0]["Keyword"] == "sds management software"
    assert rows[0]["Campaign"] == "GB"
    assert rows[0]["Ad Group"] == "Transactional"
    assert rows[0]["Criterion Type"] == "Exact"


def test_every_row_lands_paused() -> None:
    """An export that arrives enabled is one careless import from spending money."""
    assert {row["Status"] for row in parsed_csv()} == {DEFAULT_STATUS}


def test_max_cpc_is_the_top_of_the_observed_range() -> None:
    rows = parsed_csv()
    assert rows[0]["Max CPC"] == "6.80"
    assert rows[0]["CPC High"] == "6.80"


def test_a_keyword_with_no_destination_leaves_the_url_empty() -> None:
    gap = next(
        row for row in parsed_csv() if row["Keyword"] == "chemical inventory management system"
    )
    assert gap["Final URL"] == ""
    assert gap["Page Verdict"] == "gap"


def test_an_absent_seasonality_curve_is_blank_not_zero() -> None:
    """Zero is a reading. Blank is the absence of one."""
    rows = parsed_csv()
    priced = next(row for row in rows if row["Keyword"] == "sds management software")
    unpriced = next(row for row in rows if row["Keyword"] == "reach compliance software")

    assert priced["Seasonality Jan"] == "1.210"
    assert [unpriced[column] for column in SEASONALITY_COLUMNS] == [""] * 12


def test_the_file_is_utf8_with_a_bom_and_crlf() -> None:
    """Excel reads BOM-less UTF-8 as Latin-1 and mangles every German keyword."""
    payload = render_csv(golden_report())
    assert payload.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in payload
    assert b"sicherheitsdatenblatt software" in payload


def test_an_empty_report_still_produces_a_header_row() -> None:
    payload = render_csv(minimal_report()).decode("utf-8-sig")
    assert payload.splitlines()[0].startswith("Campaign,Ad Group,Keyword")
    assert len(payload.strip().splitlines()) == 1


def test_json_round_trips_the_report() -> None:
    restored = json.loads(render_json(golden_report()))
    assert restored["schema_version"] == "1.0"
    assert restored["launch_readiness"] == "go_with_fixes"
    assert len(restored["priced_keyword_list"]) == 5
    # UUIDs and datetimes come out as strings, not reprs.
    assert restored["run_id"] == "22222222-2222-4222-8222-222222222222"
    assert restored["generated_at"].startswith("2026-03-04T09:30")


def test_json_is_not_ascii_escaped() -> None:
    """A German keyword should stay readable, not become \\u00f6."""
    payload = golden_payload()
    payload["open_questions"] = ["Können wir das prüfen?"]

    from_payload = render_json_from_payload(payload).decode("utf-8")
    assert "Können wir das prüfen?" in from_payload
    assert "\\u00f6" not in from_payload

    from agent.export.contract import ResearchReport

    rendered = render_json(ResearchReport.model_validate(payload)).decode("utf-8")
    assert "Können wir das prüfen?" in rendered


def test_the_stored_payload_is_dumped_without_a_validation_round_trip() -> None:
    """What the run wrote is what Stage 02 reads, extra keys included."""
    payload = golden_payload()
    payload["future_field"] = {"added": "by a later contract"}
    restored = json.loads(render_json_from_payload(payload))
    assert restored["future_field"] == {"added": "by a later contract"}
