"""The media plan as a workbook (Stage 02 PRD §14).

§14's acceptance for this format is one sentence and it is the whole design:
"editing `envelope.monthly_cap_usd` on the allocation sheet updates every
dependent cell **through formulas**, with no recalculation by us."

So the allocation sheet has exactly one number a person types into — the
envelope, in `B2` — and every monthly figure below it is `=$B$2*share`. Cost
per acquisition, conversions and the quarterly total are formulas over those.
Change the envelope and the sheet re-derives itself in Excel, which is what a
finance reviewer actually wants: not a report of what we decided, but the model
we decided it with.

**What is a formula and what is a constant, and why.** A share is a constant:
it came from `allocation.split_v1`, an approver signed it at gate G3, and a
spreadsheet that let someone slide it is a spreadsheet that disagrees with the
plan. A forecast CPA is a constant for the same reason. Everything downstream
of those two — spend, conversions, the totals row — is a formula. The line
between them is exactly the line between "what was approved" and "what follows
from it", which is the only defensible place to put it.

**Scenario sheets are read-only siblings.** Each carries its own total in its
own `B2`, so a reviewer can flex the cautious case without touching the
approved one. They are what the budget owner chose between, preserved.

Every sheet of a draft carries the watermark in row 1. §14 requires it on
every page of a document; a workbook's pages are its sheets.
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import datetime
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from agent.export.plan_contract import AllocationLine, CampaignPlan, Scenario
from agent.export.plan_view import DRAFT_WATERMARK

MONEY = "#,##0.00"
MONEY_0 = "#,##0"
PCT = '0.00"%"'
NUMBER = "#,##0.0"

HEADER_FILL = PatternFill("solid", fgColor="1F2937")
HEADER_FONT = Font(color="FFFFFF", bold=True)
INPUT_FILL = PatternFill("solid", fgColor="FEF3C7")
WATERMARK_FONT = Font(color="B91C1C", bold=True, size=12)
TOTAL_FONT = Font(bold=True)

#: The one cell a person is invited to edit, on every sheet that has an
#: envelope. Named rather than spelled out at each use so the docstring, the
#: formulas and the tests cannot disagree about where it is.
ENVELOPE_CELL = "B2"

#: Row the allocation table's header sits on. Everything below is data, and
#: the formulas index from here.
TABLE_HEADER_ROW = 5

ALLOCATION_COLUMNS = (
    ("Campaign", 34),
    ("Market", 10),
    ("Funnel stage", 14),
    ("Share of budget", 16),
    ("Monthly spend", 16),
    ("Quarterly spend", 16),
    ("Forecast CPA", 14),
    ("Target CPA", 14),
    ("Est. conversions", 16),
    ("Within target?", 14),
)

FORECAST_COLUMNS = (
    ("Cluster", 24),
    ("Market", 10),
    ("Month", 10),
    ("Impressions", 14),
    ("CTR %", 10),
    ("Clicks", 12),
    ("Avg CPC", 12),
    ("CVR %", 10),
    ("Conversions", 14),
    ("Cost", 14),
    ("CPA", 12),
)


def render_budget_xlsx(plan: CampaignPlan, *, project_name: str | None = None) -> bytes:
    """The media plan as an .xlsx a person can flex."""
    book = Workbook()
    # `Workbook()` stamps `properties.created` and `.modified` from
    # `datetime.now()`, which puts a **clock inside the file**. §14 acceptance
    # 6 says exporting a frozen plan twice produces byte-identical output, and
    # it did — for as long as both renders landed in the same second. Two
    # renders either side of a tick differ in `docProps/core.xml`, so the test
    # that guards the rule failed roughly one run in a hundred and passed the
    # rest, which is how it survived S2-P5b.
    #
    # Stamping the plan's own time fixes the determinism and is the more
    # truthful metadata besides: a reader opening the file's properties wants
    # to know when the plan was sealed, not when somebody happened to press
    # export. `generated_at` is the right field for both cases — the freeze
    # rewrites it to the moment the plan was sealed, precisely so that §14's
    # frozen exports carry that date rather than the draft's.
    book.properties.created = plan.generated_at
    book.properties.modified = plan.generated_at

    # `Workbook()` ships one empty sheet named "Sheet". Dropping it here rather
    # than renaming the first real one keeps the sheet order deterministic:
    # Allocation, then one per scenario in the plan's own order, then Forecast.
    default = book.active
    if default is not None:
        book.remove(default)

    _allocation_sheet(book, plan, project_name=project_name)
    for scenario in plan.media_plan.scenarios:
        _scenario_sheet(book, plan, scenario)
    _forecast_sheet(book, plan)

    buffer = io.BytesIO()
    book.save(buffer)
    return _deterministic(buffer.getvalue(), plan.generated_at)


#: The earliest date the ZIP format can represent, and therefore the obvious
#: "no date here" value. Same constant and same reasoning as `editor_csv`.
EPOCH = (1980, 1, 1, 0, 0, 0)


def _deterministic(blob: bytes, stamped: datetime) -> bytes:
    """Take the clocks back out of a saved workbook.

    openpyxl puts **two** of them in, and setting `book.properties` before
    saving only removes one. `Workbook.save` overwrites `dcterms:modified` with
    `datetime.now()` on the way out, and `zipfile` stamps every member with the
    wall clock on top of that. Either is enough to break §14 acceptance 6 —
    "exporting a frozen plan twice produces byte-identical output" — which is
    exactly how it broke: the guarding test compared two renders taken
    milliseconds apart, so it passed unless the pair straddled a second tick,
    and it failed about one run in a hundred for a whole phase before anyone
    caught it.

    `editor_csv` already writes its ZIP this way. The difference here is that
    openpyxl owns the writing, so the normalisation is a second pass rather
    than a set of `ZipInfo`s handed in.
    """
    stamp = stamped.strftime("%Y-%m-%dT%H:%M:%SZ")
    out = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(blob)) as source,
        zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as target,
    ):
        for member in source.infolist():
            payload = source.read(member.filename)
            if member.filename == "docProps/core.xml":
                payload = MODIFIED.sub(
                    lambda match: f"{match.group(1)}{stamp}{match.group(3)}",
                    payload.decode("utf-8"),
                ).encode("utf-8")
            info = zipfile.ZipInfo(filename=member.filename, date_time=EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            target.writestr(info, payload)
    return out.getvalue()


#: `<dcterms:modified …>…</dcterms:modified>`, split so the value can be
#: replaced without rewriting the attributes around it.
MODIFIED = re.compile(r"(<dcterms:modified\b[^>]*>)([^<]*)(</dcterms:modified>)")


# ---------------------------------------------------------------------------
# sheets
# ---------------------------------------------------------------------------


def _allocation_sheet(book: Workbook, plan: CampaignPlan, *, project_name: str | None) -> None:
    """The approved split, driven by one editable envelope cell."""
    sheet = book.create_sheet("Allocation")
    envelope = plan.media_plan.envelope
    currency = envelope.currency if envelope is not None else "USD"
    total = float(envelope.monthly_cap.value) if envelope is not None else 0.0

    _watermark(sheet, plan, columns=len(ALLOCATION_COLUMNS))
    sheet["A2"] = f"Monthly envelope ({currency})"
    sheet["A2"].font = TOTAL_FONT
    sheet[ENVELOPE_CELL] = total
    sheet[ENVELOPE_CELL].number_format = MONEY
    sheet[ENVELOPE_CELL].fill = INPUT_FILL
    sheet[ENVELOPE_CELL].font = TOTAL_FONT
    sheet["C2"] = "← edit this and every figure below re-derives"
    sheet["C2"].font = Font(italic=True, color="92400E")

    sheet["A3"] = f"{project_name or 'Campaign plan'} · {plan.plan_status}" + (
        f" · v{plan.version}" if plan.version > 0 else ""
    )
    sheet["A3"].font = Font(italic=True, color="6B7280")

    _table(sheet, ALLOCATION_COLUMNS, row=TABLE_HEADER_ROW)
    first = TABLE_HEADER_ROW + 1
    for offset, line in enumerate(plan.media_plan.allocation):
        _allocation_row(sheet, first + offset, line, envelope_ref=f"${ENVELOPE_CELL}", total=total)
    last = first + len(plan.media_plan.allocation) - 1
    if plan.media_plan.allocation:
        _allocation_total(sheet, last + 1, first=first, last=last)
    sheet.freeze_panes = sheet.cell(row=first, column=1)


def _allocation_row(
    sheet: Worksheet, row: int, line: AllocationLine, *, envelope_ref: str, total: float
) -> None:
    """One campaign's line. Share and CPA are constants; the rest are formulas.

    The share is recomputed from the line's own dollars rather than copied from
    `line.pct`: the two agree in a sound plan, and where they do not, the
    dollars are what the approver signed and the percentage is a rendering of
    them. Deriving it here means the sheet's own total is always 100%.
    """
    share = line.usd / total if total > 0 else 0.0
    sheet.cell(row=row, column=1, value=line.campaign_ref)
    sheet.cell(row=row, column=2, value=line.market)
    sheet.cell(row=row, column=3, value=line.funnel_stage)

    share_cell = sheet.cell(row=row, column=4, value=share)
    share_cell.number_format = "0.00%"

    monthly = sheet.cell(row=row, column=5, value=f"={envelope_ref}*D{row}")
    monthly.number_format = MONEY

    quarterly = sheet.cell(row=row, column=6, value=f"=E{row}*3")
    quarterly.number_format = MONEY

    forecast_cpa = sheet.cell(row=row, column=7, value=line.forecast_cpa_usd)
    forecast_cpa.number_format = MONEY
    target_cpa = sheet.cell(row=row, column=8, value=line.target_cpa_usd)
    target_cpa.number_format = MONEY

    # Conversions follow the money: spend at the forecast cost per acquisition.
    # `IFERROR` rather than a guard on our side, because the person editing the
    # envelope may also clear a CPA, and a sheet that shows #DIV/0! where it
    # used to show a number is a sheet they will stop trusting.
    conversions = sheet.cell(row=row, column=9, value=f'=IFERROR(E{row}/G{row},"")')
    conversions.number_format = NUMBER

    verdict = sheet.cell(
        row=row,
        column=10,
        value=f'=IF(OR(G{row}="",H{row}=""),"",IF(G{row}<=H{row},"yes","over"))',
    )
    verdict.alignment = Alignment(horizontal="center")


def _allocation_total(sheet: Worksheet, row: int, *, first: int, last: int) -> None:
    """The totals row. Every cell a formula, including the share — which is how
    a reader sees at a glance that the allocation really does sum to 100%."""
    sheet.cell(row=row, column=1, value="Total").font = TOTAL_FONT
    for column, formula, fmt in (
        (4, f"=SUM(D{first}:D{last})", "0.00%"),
        (5, f"=SUM(E{first}:E{last})", MONEY),
        (6, f"=SUM(F{first}:F{last})", MONEY),
        (9, f"=SUM(I{first}:I{last})", NUMBER),
    ):
        cell = sheet.cell(row=row, column=column, value=formula)
        cell.number_format = fmt
        cell.font = TOTAL_FONT
    blended = sheet.cell(row=row, column=7, value=f'=IFERROR(E{row}/I{row},"")')
    blended.number_format = MONEY
    blended.font = TOTAL_FONT
    sheet.cell(row=row, column=8, value="blended").font = Font(italic=True, color="6B7280")


def _scenario_sheet(book: Workbook, plan: CampaignPlan, scenario: Scenario) -> None:
    """One of the three envelopes the budget owner chose between."""
    sheet = book.create_sheet(_sheet_name(scenario.name))
    chosen = scenario.name == plan.media_plan.chosen_scenario

    _watermark(sheet, plan, columns=len(ALLOCATION_COLUMNS))
    sheet["A2"] = f"{scenario.name.title()} monthly total"
    sheet["A2"].font = TOTAL_FONT
    sheet[ENVELOPE_CELL] = scenario.monthly_total_usd
    sheet[ENVELOPE_CELL].number_format = MONEY
    sheet[ENVELOPE_CELL].fill = INPUT_FILL
    sheet["C2"] = (
        "APPROVED — this is the scenario the plan commits to"
        if chosen
        else ("not chosen — kept so the decision can be re-argued")
    )
    sheet["C2"].font = Font(italic=True, color="047857" if chosen else "6B7280")

    sheet["A3"] = (
        f"est. clicks {_text(scenario.est_clicks)} · est. conversions "
        f"{_text(scenario.est_conv)} · est. CPA {_text(scenario.est_cpa)}"
    )
    sheet["A3"].font = Font(italic=True, color="6B7280")

    _table(sheet, ALLOCATION_COLUMNS, row=TABLE_HEADER_ROW)
    first = TABLE_HEADER_ROW + 1
    for offset, line in enumerate(scenario.allocation):
        _allocation_row(
            sheet,
            first + offset,
            line,
            envelope_ref=f"${ENVELOPE_CELL}",
            total=scenario.monthly_total_usd,
        )
    if scenario.allocation:
        _allocation_total(
            sheet,
            first + len(scenario.allocation),
            first=first,
            last=first + len(scenario.allocation) - 1,
        )
    sheet.freeze_panes = sheet.cell(row=first, column=1)


def _forecast_sheet(book: Workbook, plan: CampaignPlan) -> None:
    """The demand the whole plan rests on. Values, not formulas.

    Nothing here is derived from the envelope — it is what the keyword data and
    the account's own history said, before any budget was decided — so a
    formula would be a fiction. The CPA column is the one exception and it is a
    formula because a reader flexing the forecast wants it to move.
    """
    sheet = book.create_sheet("Forecast")
    _watermark(sheet, plan, columns=len(FORECAST_COLUMNS))
    sheet["A2"] = f"Method: {plan.media_plan.forecast_method or 'not stated'}"
    sheet["A2"].font = Font(italic=True, color="6B7280")

    _table(sheet, FORECAST_COLUMNS, row=TABLE_HEADER_ROW)
    row = TABLE_HEADER_ROW + 1
    for line in plan.media_plan.forecast:
        sheet.cell(row=row, column=1, value=line.cluster)
        sheet.cell(row=row, column=2, value=line.market)
        sheet.cell(row=row, column=3, value=line.month)
        for column, value, fmt in (
            (4, line.impressions, MONEY_0),
            (5, line.ctr_pct, "0.00"),
            (6, line.clicks, MONEY_0),
            (7, line.avg_cpc_usd, MONEY),
            (8, line.cvr_pct, "0.00"),
            (9, line.conversions, NUMBER),
            (10, line.cost_usd, MONEY),
        ):
            cell = sheet.cell(row=row, column=column, value=value)
            cell.number_format = fmt
        cpa = sheet.cell(row=row, column=11, value=f'=IFERROR(J{row}/I{row},"")')
        cpa.number_format = MONEY
        row += 1
    sheet.freeze_panes = sheet.cell(row=TABLE_HEADER_ROW + 1, column=1)


# ---------------------------------------------------------------------------
# chrome
# ---------------------------------------------------------------------------


def _watermark(sheet: Worksheet, plan: CampaignPlan, *, columns: int) -> None:
    """Row 1 of every sheet. §14: on every page, and a sheet is a page."""
    if plan.is_frozen:
        sheet["A1"] = f"FROZEN — v{plan.version} · {plan.generated_at.date().isoformat()}"
        sheet["A1"].font = Font(bold=True, color="047857")
        return
    sheet["A1"] = f"{DRAFT_WATERMARK} — figures can still change; not authority to spend"
    sheet["A1"].font = WATERMARK_FONT
    sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=max(columns, 2))


def _table(sheet: Worksheet, columns: tuple[tuple[str, int], ...], *, row: int) -> None:
    for index, (title, width) in enumerate(columns, start=1):
        cell = sheet.cell(row=row, column=index, value=title)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        sheet.column_dimensions[get_column_letter(index)].width = width


def _sheet_name(name: str) -> str:
    """Excel refuses a sheet name over 31 characters or carrying `[]:*?/\\`."""
    cleaned = "".join(character for character in name if character not in set(r"[]:*?/\\"))
    return (cleaned.title() or "Scenario")[:31]


def _text(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:,.1f}"
    return str(value)
