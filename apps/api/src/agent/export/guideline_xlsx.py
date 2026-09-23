"""The rulebook as a workbook — two books in one (Stage 03 PRD §14).

§14 asks for "the asset spec sheet (one sheet per campaign type, one column per
constraint) and the claims register (claim, type, status, risk, evidence refs,
signer, signed date, expiry), with conditional formatting on expiry". So: one
`Claims` sheet, one `Rules` sheet, and one sheet per campaign type.

**The conditional format is a real conditional format, not a pre-coloured
cell.** §14 acceptance 5 — "the XLSX claims sheet flags every claim expiring
within 30 days by conditional format" — would be satisfiable by writing a red
fill onto the rows that qualify today, and that file would be wrong tomorrow.
A `CellIsRule` re-evaluates when the reader opens it, which is the difference
between a report of what was expiring and a sheet that tells you what is.

**Determinism is borrowed, not re-solved.** `export/archives.normalise_zip`
exists because openpyxl writes *two* clocks into a saved workbook —
`dcterms:modified` on save, and a wall-clock stamp on every zip member — and
either one breaks "two exports are byte-identical". That cost S2-P5b a phase of
a test failing one run in a hundred. Calling it is the whole point of it being
solved once.

Every sheet of a draft carries the watermark in row 1. §14 requires it on every
page of a document; a workbook's pages are its sheets.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from openpyxl import Workbook
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from agent.export.archives import normalise_zip
from agent.export.budget_xlsx import HEADER_FILL, HEADER_FONT, WATERMARK_FONT
from agent.export.guideline_contract import ContentGuideline
from agent.export.guideline_view import DRAFT_WATERMARK

DATE = "yyyy-mm-dd"

#: §14 acceptance 5. Amber inside 30 days, red once past.
EXPIRING_FILL = PatternFill("solid", fgColor="FEF3C7")
EXPIRED_FILL = PatternFill("solid", fgColor="FEE2E2")

#: How wide a column of prose gets before it is left to wrap.
MAX_WIDTH = 60


def render_guideline_xlsx(guideline: ContentGuideline, *, project_name: str | None = None) -> bytes:
    """The rulebook as an .xlsx: claims, rules and one sheet per campaign type."""
    book = Workbook()
    # The rulebook's own time, not the exporter's — the same argument
    # `budget_xlsx` makes at length. A reader opening the file's properties
    # wants to know when the rulebook was generated, and stamping a clock here
    # would break byte-identical re-export besides.
    book.properties.created = guideline.generated_at
    book.properties.modified = guideline.generated_at

    default = book.active
    if default is not None:
        book.remove(default)

    draft = guideline.status != "published"
    _claims_sheet(book, guideline, draft=draft, project_name=project_name)
    _rules_sheet(book, guideline, draft=draft)
    for campaign_type in sorted(guideline.asset_specs.sheet.specs):
        _spec_sheet(book, guideline, campaign_type, draft=draft)

    import io

    buffer = io.BytesIO()
    book.save(buffer)
    return normalise_zip(buffer.getvalue(), guideline.generated_at)


# ---------------------------------------------------------------------------
# sheets
# ---------------------------------------------------------------------------


def _claims_sheet(
    book: Workbook,
    guideline: ContentGuideline,
    *,
    draft: bool,
    project_name: str | None,
) -> None:
    """The register legal reads, with expiry flagged by a live conditional rule."""
    sheet = book.create_sheet("Claims")
    row = _watermark(sheet, draft=draft)

    sheet.cell(row=row, column=1, value=f"Claims register — {project_name or ''}").font = Font(
        bold=True, size=13
    )
    row += 1
    sheet.cell(
        row=row,
        column=1,
        value=(
            f"Version {guideline.version} · {guideline.status} · "
            f"generated {guideline.generated_at:%Y-%m-%d}"
        ),
    )
    row += 2

    headers = [
        "Claim",
        "Type",
        "Status",
        "Risk",
        "Markets",
        "Languages",
        "Evidence refs",
        "Signature",
        "Expires",
    ]
    header_row = row
    _headers(sheet, headers, row=row)
    row += 1

    for claim in guideline.claims_register.claims:
        values: list[Any] = [
            claim.claim_text,
            claim.claim_type,
            claim.status,
            claim.risk_tier,
            ", ".join(claim.market_scope),
            ", ".join(claim.languages),
            str(len(claim.evidence_ids)),
            str(claim.signature_id) if claim.signature_id else "",
            claim.expires_at.replace(tzinfo=None) if claim.expires_at else None,
        ]
        for column, value in enumerate(values, start=1):
            cell = sheet.cell(row=row, column=column, value=value)
            if column == len(values) and value is not None:
                cell.number_format = DATE
        row += 1

    _expiry_rules(sheet, guideline, first_data_row=header_row + 1, last_row=row - 1)
    _autosize(sheet, headers)
    sheet.freeze_panes = sheet.cell(row=header_row + 1, column=1)


def _expiry_rules(
    sheet: Worksheet, guideline: ContentGuideline, *, first_data_row: int, last_row: int
) -> None:
    """§14 acceptance 5, as a rule Excel re-evaluates rather than a fill we chose.

    The comparison dates are constants because the workbook must not depend on
    the reader's clock for *reproducibility* — two exports of one published
    version have to be byte-identical, and `=TODAY()` in a formula string is
    still a constant string, so both hold. The window is measured from
    `generated_at`, which is what `guideline_view._expiring` uses for the PDF's
    list: the two must agree, and they only can if both are functions of the
    payload.
    """
    if last_row < first_data_row:
        return
    column = get_column_letter(9)
    span = f"{column}{first_data_row}:{column}{last_row}"
    horizon = (guideline.generated_at + timedelta(days=30)).strftime("%Y-%m-%d")
    today = guideline.generated_at.strftime("%Y-%m-%d")
    sheet.conditional_formatting.add(
        span, _cell_rule("lessThan", [f'DATEVALUE("{today}")'], EXPIRED_FILL)
    )
    sheet.conditional_formatting.add(
        span,
        _cell_rule("between", [f'DATEVALUE("{today}")', f'DATEVALUE("{horizon}")'], EXPIRING_FILL),
    )


def _cell_rule(operator: str, formula: list[str], fill: PatternFill) -> Any:
    """`CellIsRule`, typed.

    `types-openpyxl` stubs the workbook and cell APIs but leaves the formatting
    rules untyped, so calling the constructor directly fails strict mode. One
    wrapper keeps the escape hatch in a single place rather than at every call
    site, and gives the next person a name to grep when the stubs catch up.
    """
    return CellIsRule(operator=operator, formula=formula, fill=fill)  # type: ignore[no-untyped-call]


def _rules_sheet(book: Workbook, guideline: ContentGuideline, *, draft: bool) -> None:
    """Every compiled rule, so a writer can search the one that blocked them."""
    sheet = book.create_sheet("Rules")
    row = _watermark(sheet, draft=draft)
    headers = ["Rule id", "Category", "Severity", "Message", "Fix", "Authority", "Scope"]
    header_row = row
    _headers(sheet, headers, row=row)
    row += 1

    for rule in guideline.rules:
        scope = rule.scope
        parts = [
            f"{name}: {', '.join(values)}"
            for name, values in (
                ("markets", scope.markets),
                ("languages", scope.languages),
                ("campaigns", scope.campaign_types),
                ("assets", scope.asset_types),
                ("surfaces", scope.surfaces),
            )
            if values
        ]
        values_row: list[Any] = [
            rule.rule_id,
            rule.category,
            rule.severity,
            rule.message,
            rule.fix_hint or "",
            f"{rule.authority.source}: {rule.authority.reference}",
            "; ".join(parts) or "everywhere",
        ]
        for column, value in enumerate(values_row, start=1):
            sheet.cell(row=row, column=column, value=value)
        row += 1

    _autosize(sheet, headers)
    sheet.freeze_panes = sheet.cell(row=header_row + 1, column=1)


def _spec_sheet(
    book: Workbook, guideline: ContentGuideline, campaign_type: str, *, draft: bool
) -> None:
    """One sheet per campaign type, one column per constraint (§14)."""
    sheet = book.create_sheet(_sheet_name(campaign_type))
    row = _watermark(sheet, draft=draft)
    headers = [
        "Asset type",
        "Max chars",
        "Min count",
        "Max count",
        "Ratio",
        "Min px",
        "Max bytes",
        "Source",
        "Reviewed",
    ]
    header_row = row
    _headers(sheet, headers, row=row)
    row += 1

    specs = guideline.asset_specs.sheet.specs.get(campaign_type, {})
    for asset_type, spec in sorted(specs.items()):
        values: list[Any] = [
            asset_type,
            spec.max_chars,
            spec.min_count,
            spec.max_count,
            spec.ratio or "",
            spec.min_px or "",
            spec.max_bytes,
            spec.source,
            spec.reviewed_at,
        ]
        for column, value in enumerate(values, start=1):
            cell = sheet.cell(row=row, column=column, value=value)
            if column == len(values) and value is not None:
                cell.number_format = DATE
        row += 1

    minimum = next(
        (
            item
            for item in guideline.asset_specs.launch_minimums
            if item.campaign_type == campaign_type
        ),
        None,
    )
    if minimum is not None:
        row += 1
        sheet.cell(row=row, column=1, value="Launch minimum").font = Font(bold=True)
        row += 1
        for entry in minimum.required_assets:
            sheet.cell(row=row, column=1, value=str(entry.get("asset_type", "")))
            sheet.cell(row=row, column=2, value=entry.get("count"))
            row += 1

    _autosize(sheet, headers)
    sheet.freeze_panes = sheet.cell(row=header_row + 1, column=1)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _watermark(sheet: Worksheet, *, draft: bool) -> int:
    """Row 1 on a draft; returns the first row the caller may write to."""
    if not draft:
        return 1
    cell = sheet.cell(row=1, column=1, value=f"{DRAFT_WATERMARK} — not a legal sign-off record")
    cell.font = WATERMARK_FONT
    return 3


def _headers(sheet: Worksheet, headers: list[str], *, row: int) -> None:
    for column, title in enumerate(headers, start=1):
        cell = sheet.cell(row=row, column=column, value=title)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center")


def _autosize(sheet: Worksheet, headers: list[str]) -> None:
    """Width from the longest cell, capped. Deterministic: no measurement of fonts."""
    for column in range(1, len(headers) + 1):
        letter = get_column_letter(column)
        longest = max(
            (len(str(cell.value)) for cell in sheet[letter] if cell.value is not None),
            default=len(headers[column - 1]),
        )
        sheet.column_dimensions[letter].width = min(max(longest + 2, 10), MAX_WIDTH)


def _sheet_name(campaign_type: str) -> str:
    """Excel refuses `[]:*?/\\` and anything over 31 characters."""
    cleaned = "".join("-" if character in "[]:*?/\\" else character for character in campaign_type)
    return cleaned[:31] or "specs"
