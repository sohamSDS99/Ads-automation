"""The six plan formats, against §14's binary acceptance list.

§14 states six acceptance criteria and every one of them is a test here, named
so a failure says which clause broke:

1. `EDITOR_CSV` imports with zero errors, and its counts equal the plan's.
2. PDF ≤ 10 MB at scale; every section in the JSON is in the PDF.
3. DOCX opens in Word 2019+ and its TOC populates on F9.
4. XLSX: editing the envelope updates every dependent cell *through formulas*.
5. A draft export is watermarked on page 1 and every page after it.
6. Exporting a frozen plan twice produces byte-identical output.

Clause 1's "imports with zero errors" and clause 3's "opens in Word" cannot be
asserted from Python — they need Google Ads Editor and Word. What is asserted
is everything those two programs read: the exact column headers, the match-type
spellings, the entity counts, the `Heading` styles and the `fldChar` field.
That is the checkable part, and the uncheckable part is named here rather than
quietly claimed.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile

import pytest

from agent.db.models import ExportFormat
from agent.export.budget_xlsx import ENVELOPE_CELL, TABLE_HEADER_ROW, render_budget_xlsx
from agent.export.editor_csv import (
    AD_GROUP_COLUMNS,
    CAMPAIGN_COLUMNS,
    KEYWORD_COLUMNS,
    NEGATIVE_COLUMNS,
    STATUS,
    render_editor_csv,
)
from agent.export.jobs import CAMPAIGN_PLAN_FORMATS, plan_filename_for, render_plan
from agent.export.plan_docx import render_plan_docx
from agent.export.plan_markdown import render_plan_markdown
from agent.export.plan_view import DRAFT_WATERMARK, SECTION_TITLES
from tests import plan_fixture as fixture

#: §14 acceptance 2. A plan with 40 campaigns and 4,000 keywords.
PDF_CEILING_BYTES = 10 * 1024 * 1024


def read_zip(payload: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def read_csv(payload: bytes) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(payload.decode("utf-8-sig"))))


def big_plan(campaigns: int = 40, keywords_each: int = 100) -> object:
    """A plan at §14's stated scale: 40 campaigns, 4,000 keywords."""
    from agent.export.plan_contract import PlannedKeyword

    built = [
        fixture.campaign(
            name=f"US | Search | Group-{index:02d}",
            ref=f"c{index}",
            monthly=1_000.0,
            keywords=[
                PlannedKeyword(
                    term=f"keyword {index} {position}",
                    match_type="phrase",
                    forecast_cpc_usd=4.0,
                    search_volume=100,
                )
                for position in range(keywords_each)
            ],
        )
        for index in range(campaigns)
    ]
    plan = fixture.plan()
    plan.account_structure.campaigns = built
    return plan


# ---------------------------------------------------------------------------
# 1. EDITOR_CSV
# ---------------------------------------------------------------------------


def test_the_editor_bundle_holds_four_sheets_and_a_manifest() -> None:
    files = read_zip(render_editor_csv(fixture.plan()))
    assert set(files) == {
        "campaigns.csv",
        "ad_groups.csv",
        "keywords.csv",
        "negatives.csv",
        "manifest.txt",
    }


def test_every_sheet_carries_the_headers_editor_expects() -> None:
    files = read_zip(render_editor_csv(fixture.plan()))
    for name, columns in (
        ("campaigns.csv", CAMPAIGN_COLUMNS),
        ("ad_groups.csv", AD_GROUP_COLUMNS),
        ("keywords.csv", KEYWORD_COLUMNS),
        ("negatives.csv", NEGATIVE_COLUMNS),
    ):
        header = files[name].decode("utf-8-sig").split("\r\n")[0]
        assert header == ",".join(columns), name


def test_the_imported_counts_equal_the_plans_own_counts() -> None:
    """§14 acceptance 1, the half that can be checked without Editor."""
    plan = fixture.plan()
    files = read_zip(render_editor_csv(plan))
    counts = plan.account_structure.counts()
    assert len(read_csv(files["campaigns.csv"])) == counts["campaigns"]
    assert len(read_csv(files["ad_groups.csv"])) == counts["ad_groups"]
    assert len(read_csv(files["keywords.csv"])) == counts["keywords"]


def test_the_counts_hold_at_forty_campaigns_and_four_thousand_keywords() -> None:
    plan = big_plan()
    files = read_zip(render_editor_csv(plan))  # type: ignore[arg-type]
    counts = plan.account_structure.counts()  # type: ignore[attr-defined]
    assert counts == {"campaigns": 40, "ad_groups": 40, "keywords": 4_000}
    assert len(read_csv(files["keywords.csv"])) == 4_000


def test_everything_imports_paused() -> None:
    files = read_zip(render_editor_csv(fixture.plan()))
    for name in ("campaigns.csv", "ad_groups.csv", "keywords.csv"):
        rows = read_csv(files[name])
        assert rows, name
        assert all(row["Status"] == STATUS for row in rows), name


def test_match_types_use_editors_own_spelling() -> None:
    rows = read_csv(read_zip(render_editor_csv(fixture.plan()))["keywords.csv"])
    assert {row["Criterion Type"] for row in rows} == {"Phrase", "Exact"}


def test_a_campaign_negative_and_an_ad_group_negative_are_spelled_differently() -> None:
    rows = read_csv(read_zip(render_editor_csv(fixture.plan()))["negatives.csv"])
    kinds = {row["Criterion Type"] for row in rows}
    assert "Campaign Negative Phrase" in kinds
    assert "Negative Phrase" in kinds
    # An ad-group negative names its ad group; a campaign one does not.
    for row in rows:
        assert bool(row["Ad Group"]) == row["Criterion Type"].startswith("Negative")


def test_account_negatives_are_named_in_the_manifest_not_silently_dropped() -> None:
    """Editor manages account negatives as a shared list this bundle cannot make."""
    manifest = read_zip(render_editor_csv(fixture.plan()))["manifest.txt"].decode()
    assert "ADD THESE BY HAND" in manifest
    for term in fixture.plan().account_structure.account_negatives:
        assert term in manifest


def test_a_smart_bidding_ad_group_carries_no_max_cpc() -> None:
    """Google ignores it, and a number Google ignores reads as the bid."""
    rows = read_csv(read_zip(render_editor_csv(fixture.plan()))["ad_groups.csv"])
    assert all(row["Max CPC"] == "" for row in rows)


def test_a_target_lands_in_the_column_its_strategy_reads() -> None:
    rows = read_csv(read_zip(render_editor_csv(fixture.plan()))["campaigns.csv"])
    assert rows[0]["Bid Strategy Type"] == "Target CPA"
    assert rows[0]["Target CPA"] == "900.00"
    assert rows[0]["Target ROAS"] == ""


def test_every_sheet_is_crlf_and_carries_a_bom() -> None:
    files = read_zip(render_editor_csv(fixture.plan()))
    for name in ("campaigns.csv", "keywords.csv"):
        assert files[name].startswith(b"\xef\xbb\xbf"), name
        assert b"\r\n" in files[name], name


def test_a_draft_bundle_warns_in_the_manifest() -> None:
    manifest = read_zip(render_editor_csv(fixture.plan()))["manifest.txt"].decode()
    assert manifest.startswith("DRAFT — NOT APPROVED. Do not import.")
    assert "THIS PLAN IS NOT FROZEN." in manifest


def test_a_frozen_bundle_does_not() -> None:
    manifest = read_zip(render_editor_csv(fixture.frozen_plan()))["manifest.txt"].decode()
    assert manifest.startswith("Frozen plan")
    assert "DRAFT" not in manifest


# ---------------------------------------------------------------------------
# 4. XLSX — the formulas are the feature
# ---------------------------------------------------------------------------


def workbook(plan: object) -> object:
    from openpyxl import load_workbook

    return load_workbook(io.BytesIO(render_budget_xlsx(plan)))  # type: ignore[arg-type]


def test_the_workbook_has_an_allocation_sheet_a_scenario_sheet_each_and_a_forecast() -> None:
    book = workbook(fixture.plan())
    assert book.sheetnames == ["Allocation", "Expected", "Cautious", "Forecast"]  # type: ignore[attr-defined]


def test_the_envelope_is_a_value_and_every_spend_cell_is_a_formula_on_it() -> None:
    """§14 acceptance 4, stated exactly: edit B2 and the sheet re-derives."""
    sheet = workbook(fixture.plan())["Allocation"]  # type: ignore[index]
    assert sheet[ENVELOPE_CELL].value == 40_000.0
    first = TABLE_HEADER_ROW + 1
    assert sheet[f"E{first}"].value == f"=${ENVELOPE_CELL}*D{first}"
    assert sheet[f"F{first}"].value == f"=E{first}*3"


def test_conversions_follow_the_money_through_a_formula() -> None:
    sheet = workbook(fixture.plan())["Allocation"]  # type: ignore[index]
    first = TABLE_HEADER_ROW + 1
    assert sheet[f"I{first}"].value == f'=IFERROR(E{first}/G{first},"")'


def test_the_share_column_is_a_constant_because_an_approver_signed_it() -> None:
    sheet = workbook(fixture.plan())["Allocation"]  # type: ignore[index]
    first = TABLE_HEADER_ROW + 1
    # 25,000 / 40,000 = 0.625, and it is a number rather than a formula.
    assert sheet[f"D{first}"].value == pytest.approx(0.625)
    assert sheet[f"D{first + 1}"].value == pytest.approx(0.375)


def test_the_totals_row_is_formulas_all_the_way_across() -> None:
    sheet = workbook(fixture.plan())["Allocation"]  # type: ignore[index]
    first = TABLE_HEADER_ROW + 1
    total_row = first + 2
    assert sheet[f"A{total_row}"].value == "Total"
    assert sheet[f"D{total_row}"].value == f"=SUM(D{first}:D{first + 1})"
    assert sheet[f"E{total_row}"].value == f"=SUM(E{first}:E{first + 1})"


def test_each_scenario_sheet_drives_off_its_own_total() -> None:
    book = workbook(fixture.plan())
    expected = book["Expected"]  # type: ignore[index]
    cautious = book["Cautious"]  # type: ignore[index]
    assert expected[ENVELOPE_CELL].value == 40_000.0
    assert cautious[ENVELOPE_CELL].value == 24_000.0
    # And the chosen one says so, so a reviewer flexing `Cautious` knows it is
    # not what the plan commits to.
    assert "APPROVED" in str(expected["C2"].value)
    assert "not chosen" in str(cautious["C2"].value)


def test_every_sheet_of_a_draft_workbook_is_watermarked() -> None:
    """§14 acceptance 5. A workbook's pages are its sheets."""
    book = workbook(fixture.plan())
    for name in book.sheetnames:  # type: ignore[attr-defined]
        assert DRAFT_WATERMARK in str(book[name]["A1"].value), name  # type: ignore[index]


def test_a_frozen_workbook_carries_its_version_instead() -> None:
    book = workbook(fixture.frozen_plan())
    for name in book.sheetnames:  # type: ignore[attr-defined]
        first = str(book[name]["A1"].value)  # type: ignore[index]
        assert "FROZEN — v3" in first, name
        assert DRAFT_WATERMARK not in first, name


# ---------------------------------------------------------------------------
# 2 & 5. PDF
# ---------------------------------------------------------------------------


def test_every_section_in_the_json_is_in_the_pdfs_html() -> None:
    """§14 acceptance 2, second clause."""
    from agent.export.plan_pdf import render_plan_html

    html = render_plan_html(fixture.plan(), project_name="SDS Manager")
    for title in SECTION_TITLES:
        assert title in html, title


def test_the_draft_watermark_is_a_fixed_element_so_it_repeats_on_every_page() -> None:
    """§14 acceptance 5. `position: fixed` in a paged context is what repeats it."""
    from agent.export.plan_pdf import STYLESHEET, render_plan_html

    html = render_plan_html(fixture.plan(), project_name="SDS Manager")
    assert f'class="watermark">{DRAFT_WATERMARK}' in html
    assert "draft-banner" in html
    css = STYLESHEET.read_text()
    assert ".watermark {" in css
    assert "position: fixed;" in css


def test_a_frozen_plan_carries_no_watermark_and_names_who_froze_it() -> None:
    from agent.export.plan_pdf import render_plan_html

    html = render_plan_html(
        fixture.frozen_plan(), project_name="SDS Manager", frozen_by_name="Soham Sarker"
    )
    assert DRAFT_WATERMARK not in html
    assert "Soham Sarker" in html


def test_the_pdf_prints_and_stays_under_the_ceiling_at_scale() -> None:
    """§14 acceptance 2, first clause: ≤ 10 MB at 40 campaigns and 4,000 keywords."""
    pytest.importorskip("weasyprint", reason="WeasyPrint's native libraries are not installed")
    from agent.export.plan_pdf import render_plan_pdf

    payload = render_plan_pdf(big_plan(), project_name="SDS Manager")  # type: ignore[arg-type]
    assert payload.startswith(b"%PDF")
    assert len(payload) <= PDF_CEILING_BYTES, f"{len(payload):,} bytes"


# ---------------------------------------------------------------------------
# 3. DOCX
# ---------------------------------------------------------------------------


def document(plan: object) -> object:
    from docx import Document

    return Document(io.BytesIO(render_plan_docx(plan)))  # type: ignore[arg-type]


def test_the_docx_carries_a_real_toc_field_marked_dirty() -> None:
    """§14 acceptance 3: the TOC populates on F9 only if it is a field."""
    payload = render_plan_docx(fixture.plan())
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        body = archive.read("word/document.xml").decode()
        settings = archive.read("word/settings.xml").decode()
    assert 'TOC \\o "1-3" \\h \\z \\u' in body
    assert 'w:dirty="true"' in body
    assert "updateFields" in settings


def test_the_docx_uses_real_heading_styles_so_the_toc_can_collect_them() -> None:
    doc = document(fixture.plan())
    styles = {p.style.name for p in doc.paragraphs}  # type: ignore[attr-defined]
    assert "Heading 1" in styles
    assert "Heading 2" in styles


def test_every_section_title_appears_in_the_docx() -> None:
    doc = document(fixture.plan())
    text = "\n".join(p.text for p in doc.paragraphs)  # type: ignore[attr-defined]
    for title in SECTION_TITLES:
        assert title in text, title


def test_the_draft_watermark_is_in_the_header_which_word_repeats() -> None:
    """A Word document has no `position: fixed`; the header is the mechanism."""
    doc = document(fixture.plan())
    header = doc.sections[0].header  # type: ignore[attr-defined]
    assert DRAFT_WATERMARK in "\n".join(p.text for p in header.paragraphs)


def test_a_frozen_docx_header_says_frozen() -> None:
    doc = document(fixture.frozen_plan())
    header = "\n".join(p.text for p in doc.sections[0].header.paragraphs)  # type: ignore[attr-defined]
    assert "FROZEN — v3" in header
    assert DRAFT_WATERMARK not in header


# ---------------------------------------------------------------------------
# 6. Reproducibility, and the format table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fmt", [ExportFormat.MD, ExportFormat.JSON, ExportFormat.EDITOR_CSV, ExportFormat.XLSX]
)
def test_a_frozen_plan_exports_byte_identically_twice(fmt: ExportFormat) -> None:
    """§14 acceptance 6. Anything reading a clock breaks this."""
    plan = fixture.frozen_plan()
    first = render_plan(fmt, plan, project_name="SDS Manager")
    second = render_plan(fmt, plan, project_name="SDS Manager")
    assert first.payload == second.payload, fmt.value


def test_the_workbook_carries_the_plans_clock_and_not_the_wall_clock() -> None:
    """Why the parametrised test above stopped being a coin toss (S2-P7).

    `openpyxl` puts two clocks into an .xlsx: `Workbook()` sets
    `dcterms:created` from `datetime.now()`, `save` overwrites
    `dcterms:modified` with it again, and `zipfile` stamps every member on top.
    Two renders therefore matched only when they landed in the same second, so
    the byte-identity test passed on almost every run and failed on about one
    in a hundred — which is the worst kind of green, and it stayed that way for
    a whole phase.

    Comparing two renders cannot catch that without sleeping through a tick, so
    this asserts the property directly: there is no `now()` anywhere in the
    file.
    """
    plan = fixture.frozen_plan()
    blob = render_budget_xlsx(plan, project_name="SDS Manager")
    stamp = plan.generated_at.strftime("%Y-%m-%dT%H:%M:%SZ")

    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        core = archive.read("docProps/core.xml").decode()
        assert core.count(stamp) == 2, f"core.xml does not carry the plan's time twice:\n{core}"
        stamped = {member.date_time for member in archive.infolist()}
        assert stamped == {(1980, 1, 1, 0, 0, 0)}, f"a ZIP member carries a wall clock: {stamped}"


def test_the_workbook_still_opens_after_the_timestamps_are_normalised() -> None:
    """The control for the test above.

    Rewriting every ZIP member is the kind of fix that can leave a file that
    compares equal and opens in nothing. Read it back with the library Excel's
    own format is defined by, and check a formula survived.
    """
    from openpyxl import load_workbook

    plan = fixture.frozen_plan()
    book = load_workbook(io.BytesIO(render_budget_xlsx(plan, project_name="SDS Manager")))
    assert book.sheetnames[0] == "Allocation"
    formulas = [
        cell.value
        for row in book["Allocation"].iter_rows()
        for cell in row
        if isinstance(cell.value, str) and cell.value.startswith("=")
    ]
    assert formulas, "the allocation sheet lost its live formulas"


def test_the_docx_differs_only_where_python_docx_stamps_a_time() -> None:
    """DOCX is excluded from the byte-identical parametrisation above, honestly.

    `python-docx` writes a `dcterms:created` timestamp into `docProps/core.xml`
    from the reference template, and the reference is fixed — so in practice
    two renders do match. This asserts the part that is actually guaranteed:
    the document body, which is what a reader sees.
    """
    plan = fixture.frozen_plan()
    bodies = []
    for _ in range(2):
        with zipfile.ZipFile(io.BytesIO(render_plan_docx(plan))) as archive:
            bodies.append(archive.read("word/document.xml"))
    assert bodies[0] == bodies[1]


def test_markdown_is_deterministic() -> None:
    plan = fixture.plan()
    assert render_plan_markdown(plan, project_name="X") == render_plan_markdown(
        plan, project_name="X"
    )


def test_the_six_formats_are_exactly_what_prd_14_names() -> None:
    assert {
        ExportFormat.PDF,
        ExportFormat.DOCX,
        ExportFormat.MD,
        ExportFormat.JSON,
        ExportFormat.EDITOR_CSV,
        ExportFormat.XLSX,
    } == CAMPAIGN_PLAN_FORMATS
    # `csv` is deliberately absent: a campaign tree flattened to one CSV is a
    # shape nothing imports, and the Editor bundle is the tabular deliverable.
    assert ExportFormat.CSV not in CAMPAIGN_PLAN_FORMATS


def test_a_plan_filename_carries_its_version_so_two_downloads_do_not_collide() -> None:
    frozen = fixture.frozen_plan()
    name = plan_filename_for(
        ExportFormat.PDF,
        project_name="SDS Manager",
        version=frozen.version,
        generated_at=frozen.generated_at,
    )
    assert name == "campaign-plan-sds-manager-v3-2026-09-22.pdf"


def test_an_unfrozen_plan_says_draft_in_its_filename() -> None:
    draft = fixture.plan()
    name = plan_filename_for(
        ExportFormat.XLSX,
        project_name="SDS Manager",
        version=draft.version,
        generated_at=draft.generated_at,
    )
    assert name == "campaign-plan-sds-manager-draft-2026-09-22.xlsx"


def test_the_json_export_is_the_stored_payload_verbatim() -> None:
    import json

    plan = fixture.frozen_plan()
    stored = plan.model_dump(mode="json")
    rendered = render_plan(ExportFormat.JSON, plan, project_name="X", stored_payload=stored).payload
    assert json.loads(rendered) == stored


def test_the_markdown_export_prefers_what_the_run_stored() -> None:
    """A later template edit must not change a plan somebody has already signed."""
    plan = fixture.frozen_plan()
    rendered = render_plan(
        ExportFormat.MD, plan, project_name="X", stored_markdown="# what was signed\n"
    )
    assert rendered.payload == b"# what was signed\n"


# ---------------------------------------------------------------------------
# the worker path — every format rendered from a stored JSONB payload
# ---------------------------------------------------------------------------


def stored_round_trip(plan: object) -> object:
    """What the worker actually holds: the plan after a trip through JSONB.

    `generate_export` reads `campaign_plan.payload` and validates it — it does
    not receive the in-memory model the run built. Every test above starts
    from that model, so none of them would notice a field that survives in
    Python and not in JSON.
    """
    from agent.export.plan_contract import CampaignPlan

    return CampaignPlan.model_validate(json.loads(json.dumps(plan.model_dump(mode="json"))))  # type: ignore[attr-defined]


def test_a_decimal_money_field_is_stored_as_a_string_and_comes_back_a_decimal() -> None:
    """Pydantic serialises `Decimal` to a JSON **string**, losslessly.

    Worth pinning rather than discovering: anything reading the raw payload
    without the contract — a zod schema, a diff, Stage 03 — gets `"40000"`
    and not `40000`. Raised by the S2-P6c session, whose normaliser compared
    a stringified value against a dict and reported a whole media plan as
    rewritten. Nothing here reads `.value` without validating first, and this
    test is what keeps that true.
    """
    from decimal import Decimal

    payload = fixture.plan().model_dump(mode="json")
    raw = payload["media_plan"]["envelope"]["monthly_cap"]["value"]
    assert isinstance(raw, str), "a Decimal is JSON-serialised as a string"

    restored = stored_round_trip(fixture.plan())
    value = restored.media_plan.envelope.monthly_cap.value  # type: ignore[attr-defined]
    assert isinstance(value, Decimal)
    assert value == Decimal("40000")


@pytest.mark.parametrize(
    "fmt",
    [ExportFormat.MD, ExportFormat.JSON, ExportFormat.EDITOR_CSV, ExportFormat.XLSX],
)
def test_every_format_renders_from_a_stored_payload_not_just_a_live_model(
    fmt: ExportFormat,
) -> None:
    from agent.export.jobs import render_plan

    live = render_plan(fmt, fixture.frozen_plan(), project_name="SDS Manager")
    stored = render_plan(fmt, stored_round_trip(fixture.frozen_plan()), project_name="SDS Manager")  # type: ignore[arg-type]
    assert stored.payload == live.payload, f"{fmt.value} differs after a JSONB round trip"


def test_the_money_survives_the_round_trip_into_the_workbook() -> None:
    """The one format that does arithmetic on `Number.value` rather than
    formatting it. A string-typed value reaching `float()` would raise; a
    silently-coerced one would put the wrong share in every row."""
    from openpyxl import load_workbook

    book = load_workbook(io.BytesIO(render_budget_xlsx(stored_round_trip(fixture.plan()))))  # type: ignore[arg-type]
    sheet = book["Allocation"]
    assert sheet[ENVELOPE_CELL].value == 40_000.0
    first = TABLE_HEADER_ROW + 1
    assert sheet[f"D{first}"].value == pytest.approx(0.625)


def test_the_markdown_states_the_envelope_after_a_round_trip() -> None:
    markdown = render_plan_markdown(stored_round_trip(fixture.plan()), project_name="X")  # type: ignore[arg-type]
    assert "40,000.00 USD a month" in markdown


def test_the_fixture_itself_is_deterministic() -> None:
    """Guards every byte-comparison above.

    A fixture minting `uuid4()` per call returns a different plan each time,
    and a test comparing two renderings then fails for a reason that has
    nothing to do with the renderer. Cost me one confusing failure.
    """
    assert fixture.plan().model_dump(mode="json") == fixture.plan().model_dump(mode="json")
