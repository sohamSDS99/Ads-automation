"""The two machine-readable exports: CSV for a person, JSON for Stage 02.

**CSV** is `priced_keyword_list` and nothing else (PRD §12). Its leading columns
are the ones Google Ads Editor recognises for a keyword import, in the spelling
Editor uses, so the file can be opened and imported rather than reshaped first.
The research columns follow; Editor maps by header and leaves what it does not
recognise alone, and a human reading the file in Excel wants the volume and the
CPC range next to the term.

`Campaign` and `Ad Group` are filled from the market and the intent the research
already established. That is the least invented structure that still produces an
importable file — Stage 02 owns real campaign architecture, and a blank Campaign
column would make every row fail on import.

**JSON** is the payload verbatim. It is the handoff artifact, so it is not
prettified into something lossy: same keys, same nesting, same values that the
markdown and the PDF were rendered from.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any

from agent.export.contract import PricedKeyword, ResearchReport
from agent.export.templating import MONTH_NAMES

#: Google Ads Editor's own column names, in its own spelling and order.
EDITOR_COLUMNS = (
    "Campaign",
    "Ad Group",
    "Keyword",
    "Criterion Type",
    "Max CPC",
    "Final URL",
    "Status",
)

#: What the research adds. Editor ignores these; a person reading the sheet does not.
RESEARCH_COLUMNS = (
    "Market",
    "Intent",
    "Funnel Stage",
    "Monthly Volume",
    "CPC Low",
    "CPC High",
    "Competition",
    "YoY Trend",
    "Page Verdict",
)

SEASONALITY_COLUMNS = tuple(f"Seasonality {month}" for month in MONTH_NAMES)

COLUMNS = EDITOR_COLUMNS + RESEARCH_COLUMNS + SEASONALITY_COLUMNS

#: Every keyword lands paused. An export that arrives enabled is one careless
#: import away from spending money on a list nobody has reviewed.
DEFAULT_STATUS = "Paused"


def _editor_match_type(keyword: PricedKeyword) -> str:
    """Editor spells match types with an initial capital."""
    return keyword.match_type.capitalize()


def _campaign_name(keyword: PricedKeyword) -> str:
    return keyword.market or "Unassigned"


def _ad_group_name(keyword: PricedKeyword) -> str:
    return (keyword.intent or "unclassified").replace("_", " ").title()


def _number(value: float | int | None, places: int = 2) -> str:
    """Plain decimals, no thousands separators — this is a file for a machine."""
    if value is None:
        return ""
    return f"{float(value):.{places}f}"


def keyword_rows(report: ResearchReport) -> list[dict[str, Any]]:
    """One dict per keyword, keyed by the column headers above."""
    rows: list[dict[str, Any]] = []
    for keyword in report.priced_keyword_list:
        row: dict[str, Any] = {
            "Campaign": _campaign_name(keyword),
            "Ad Group": _ad_group_name(keyword),
            "Keyword": keyword.term,
            "Criterion Type": _editor_match_type(keyword),
            # Editor reads Max CPC as a bid. The top of the observed range is the
            # honest ceiling to start from; it is a research figure, not a
            # recommendation, and the Status column keeps it from spending.
            "Max CPC": _number(keyword.cpc_high),
            "Final URL": keyword.best_url or "",
            "Status": DEFAULT_STATUS,
            "Market": keyword.market or "",
            "Intent": keyword.intent or "",
            "Funnel Stage": keyword.funnel_stage or "",
            "Monthly Volume": keyword.volume if keyword.volume is not None else "",
            "CPC Low": _number(keyword.cpc_low),
            "CPC High": _number(keyword.cpc_high),
            "Competition": _number(keyword.competition, 3),
            "YoY Trend": _number(keyword.trend_yoy, 3),
            "Page Verdict": keyword.verdict or "",
        }
        # An absent seasonality curve leaves twelve empty cells rather than
        # twelve zeroes: zero is a reading, blank is the absence of one.
        seasonality = keyword.seasonality_index
        for index, column in enumerate(SEASONALITY_COLUMNS):
            row[column] = _number(seasonality[index], 3) if seasonality else ""
        rows.append(row)
    return rows


def render_csv(report: ResearchReport) -> bytes:
    """The priced keyword list as UTF-8 CSV with a BOM.

    The BOM is there because the first thing that happens to this file is that
    someone opens it in Excel, and Excel reads a BOM-less UTF-8 file as Latin-1 —
    which turns every German keyword into mojibake. Editor and pandas both
    tolerate it.

    Line terminator is CRLF per RFC 4180 rather than the platform default, so
    the bytes do not depend on which machine rendered them.
    """
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=list(COLUMNS),
        lineterminator="\r\n",
        quoting=csv.QUOTE_MINIMAL,
    )
    writer.writeheader()
    writer.writerows(keyword_rows(report))
    return buffer.getvalue().encode("utf-8-sig")


def render_json(report: ResearchReport) -> bytes:
    """The whole report, as Stage 02 will read it.

    `mode="json"` so UUIDs and datetimes serialise to strings rather than
    repr()-ing; `ensure_ascii=False` so a German keyword stays readable instead
    of becoming an escape sequence.
    """
    payload = report.model_dump(mode="json")
    return json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"


def render_json_from_payload(payload: dict[str, Any]) -> bytes:
    """The stored `Report.payload`, dumped without a validation round trip.

    Used by the export job so the JSON a client downloads is byte-for-byte what
    the run wrote, even if the contract has since gained a field with a default.
    """
    return json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"
