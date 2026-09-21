"""CRM exports — closed-won and closed-lost (PRD §9.5).

The interesting problem is not parsing CSV, it is that the file comes out of
someone else's CRM with someone else's column names. So the connector is built
around an explicit mapping from source column to canonical field, and it can
*propose* that mapping (`suggest_mapping`) without ever silently applying a
guess — the wizard shows the proposal, a human confirms it, and the confirmed
mapping is what runs.

Validation is row-level and non-fatal by design. A 4,000-row export with nine
bad dates should import 3,991 rows and hand back nine numbered complaints,
because the alternative — rejecting the file — means the user edits a CSV in
Excel and tries again, which is how data gets quietly mangled.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

import structlog

from agent.connectors.base import (
    ConnectorDegraded,
    ConnectorError,
    ConnectorStatus,
    EvidenceDraft,
    ReadOnlyConnector,
)
from agent.db.models import EvidenceSource

log = structlog.get_logger(__name__)

Outcome = Literal["won", "lost"]

FieldType = Literal["text", "int", "decimal", "date"]


@dataclass(frozen=True, slots=True)
class CanonicalField:
    name: str
    type: FieldType
    required: bool = False
    #: Header fragments that suggest this field. Matched case- and
    #: punctuation-insensitively against the source column.
    aliases: tuple[str, ...] = ()


#: PRD §9.5's canonical field list, in the order the mapping UI should show them.
CANONICAL_FIELDS: tuple[CanonicalField, ...] = (
    CanonicalField(
        "account_name", "text", required=True, aliases=("account", "company", "customer", "name")
    ),
    CanonicalField("industry", "text", aliases=("industry", "sector", "vertical")),
    CanonicalField(
        "employee_count", "int", aliases=("employee", "employees", "headcount", "company size")
    ),
    CanonicalField("country", "text", aliases=("country", "region", "market", "location")),
    CanonicalField(
        "deal_value", "decimal", aliases=("value", "amount", "revenue", "acv", "arr", "deal size")
    ),
    CanonicalField(
        "close_reason", "text", aliases=("reason", "close reason", "lost reason", "outcome")
    ),
    CanonicalField("source", "text", aliases=("source", "channel", "lead source", "origin")),
    CanonicalField(
        "created_at", "date", aliases=("created", "date", "close date", "created at", "opened")
    ),
)

FIELDS_BY_NAME = {item.name: item for item in CANONICAL_FIELDS}

#: Tried in order. ISO first because it is unambiguous.
DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d-%m-%Y",
    "%d %b %Y",
    "%b %d, %Y",
    "%Y-%m-%dT%H:%M:%S",
)

CURRENCY = re.compile(r"[^\d.,\-]")


@dataclass(slots=True)
class RowError:
    """One complaint, addressed to a line number a human can find in Excel."""

    row: int
    column: str
    value: str
    problem: str


@dataclass(slots=True)
class ColumnPreview:
    """What the mapping UI needs to render itself."""

    headers: list[str] = field(default_factory=list)
    sample_rows: list[dict[str, str]] = field(default_factory=list)
    suggested_mapping: dict[str, str] = field(default_factory=dict)
    row_count: int = 0


@dataclass(slots=True)
class IngestResult:
    drafts: list[EvidenceDraft] = field(default_factory=list)
    errors: list[RowError] = field(default_factory=list)
    skipped: int = 0

    @property
    def accepted(self) -> int:
        return len(self.drafts)


def _normalise_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def suggest_mapping(headers: list[str]) -> dict[str, str]:
    """Propose `{source column: canonical field}`. A proposal, never applied on its own.

    Exact normalised match wins over an alias substring, and each canonical
    field is claimed at most once, so a file with both "Company" and
    "Company Size" does not map both to `account_name`.
    """
    mapping: dict[str, str] = {}
    claimed: set[str] = set()

    for header in headers:
        normalised = _normalise_header(header)
        for candidate in CANONICAL_FIELDS:
            if candidate.name in claimed:
                continue
            if normalised == candidate.name.replace("_", " "):
                mapping[header] = candidate.name
                claimed.add(candidate.name)
                break

    for header in headers:
        if header in mapping:
            continue
        normalised = _normalise_header(header)
        best: tuple[int, str] | None = None
        for candidate in CANONICAL_FIELDS:
            if candidate.name in claimed:
                continue
            for alias in candidate.aliases:
                if alias in normalised:
                    # Longest alias wins: "company size" must beat "company".
                    score = len(alias)
                    if best is None or score > best[0]:
                        best = (score, candidate.name)
        if best is not None:
            mapping[header] = best[1]
            claimed.add(best[1])
    return mapping


def coerce(value: str, field_type: FieldType) -> Any:
    """Turn a spreadsheet cell into a typed value, or raise `ValueError` saying why."""
    text = (value or "").strip()
    if not text:
        return None
    if field_type == "text":
        return text
    if field_type == "int":
        cleaned = CURRENCY.sub("", text).replace(",", "")
        try:
            return int(float(cleaned))
        except ValueError as exc:
            raise ValueError("not a whole number") from exc
    if field_type == "decimal":
        cleaned = CURRENCY.sub("", text)
        # "1.234,56" is European; "1,234.56" is not. The last separator wins.
        if "," in cleaned and "." in cleaned:
            cleaned = (
                cleaned.replace(".", "").replace(",", ".")
                if cleaned.rfind(",") > cleaned.rfind(".")
                else cleaned.replace(",", "")
            )
        elif "," in cleaned:
            parts = cleaned.split(",")
            cleaned = cleaned.replace(",", "." if len(parts[-1]) == 2 else "")
        try:
            return float(Decimal(cleaned))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("not a number") from exc
    if field_type == "date":
        for fmt in DATE_FORMATS:
            try:
                return datetime.strptime(text, fmt).replace(tzinfo=UTC).date().isoformat()
            except ValueError:
                continue
        raise ValueError("unrecognised date format")
    raise ValueError(f"unknown field type {field_type}")  # pragma: no cover


def _decode(content: bytes) -> str:
    """CRM exports arrive as UTF-8, UTF-8-BOM or cp1252. Try in that order."""
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def _reader(text: str) -> csv.DictReader[str]:
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return csv.DictReader(io.StringIO(text), dialect=dialect)


def preview(content: bytes, *, sample_size: int = 5) -> ColumnPreview:
    """Headers, a few rows, and a proposed mapping — everything the wizard shows."""
    text = _decode(content)
    reader = _reader(text)
    headers = [header for header in (reader.fieldnames or []) if header]
    if not headers:
        raise ConnectorError("the file has no header row")
    rows = []
    count = 0
    for index, row in enumerate(reader):
        count += 1
        if index < sample_size:
            rows.append({key: (row.get(key) or "") for key in headers})
    return ColumnPreview(
        headers=headers,
        sample_rows=rows,
        suggested_mapping=suggest_mapping(headers),
        row_count=count,
    )


class CsvIngestConnector(ReadOnlyConnector):
    """Canonicalises a CRM export into `crm_won` / `crm_lost` evidence.

    Read-only by nature — it parses an upload and writes Evidence — and marked
    as such because Stage 02's nodes 2.1.1, 2.1.2 and 2.1.4 read the CRM
    through it, and the executor refuses an unmarked connector on a plan run.
    """

    name = "csv_ingest"
    source = EvidenceSource.CSV

    def ingest(
        self,
        content: bytes,
        *,
        mapping: dict[str, str],
        outcome: Outcome,
        filename: str | None = None,
    ) -> IngestResult:
        """Parse and validate. Returns accepted rows *and* the complaints together."""
        if outcome not in ("won", "lost"):
            raise ConnectorError(f"outcome must be 'won' or 'lost', got {outcome!r}")
        if len(content) > self.settings.csv_max_bytes:
            raise ConnectorError(
                f"file is {len(content)} bytes; the limit is {self.settings.csv_max_bytes}"
            )
        unknown = set(mapping.values()) - set(FIELDS_BY_NAME)
        if unknown:
            raise ConnectorError(f"unknown canonical field(s): {', '.join(sorted(unknown))}")

        required = {item.name for item in CANONICAL_FIELDS if item.required}
        missing = required - set(mapping.values())
        if missing:
            raise ConnectorError(
                f"mapping is missing required field(s): {', '.join(sorted(missing))}"
            )

        kind = f"crm_{outcome}"
        result = IngestResult()
        reader = _reader(_decode(content))

        for index, raw in enumerate(reader, start=2):  # row 1 is the header
            if index - 1 > self.settings.csv_max_rows:
                result.skipped += 1
                continue
            record: dict[str, Any] = {}
            row_errors: list[RowError] = []
            for column, canonical in mapping.items():
                spec = FIELDS_BY_NAME[canonical]
                raw_value = (raw.get(column) or "").strip()
                try:
                    record[canonical] = coerce(raw_value, spec.type)
                except ValueError as exc:
                    # Explicit None, not an absent key: every accepted row then
                    # has the same shape, which keeps content hashes comparable
                    # and distinguishes "mapped but unusable" from "not mapped".
                    record[canonical] = None
                    row_errors.append(RowError(index, column, raw_value[:80], str(exc)))

            # A bad value in an optional field costs that field; a bad value in
            # a required one costs the row.
            if any(FIELDS_BY_NAME[mapping[error.column]].required for error in row_errors):
                result.errors.extend(row_errors)
                result.skipped += 1
                continue
            if not record.get("account_name"):
                result.errors.append(RowError(index, "account_name", "", "required field is empty"))
                result.skipped += 1
                continue

            result.errors.extend(row_errors)
            record["outcome"] = outcome
            if filename:
                record["source_file"] = filename
            result.drafts.append(self.draft(kind, record))

        log.info(
            "csv_ingest.parsed",
            outcome=outcome,
            accepted=result.accepted,
            skipped=result.skipped,
            errors=len(result.errors),
        )
        return result

    async def fetch(self, params: dict[str, Any]) -> list[EvidenceDraft]:
        """`params`: `{content: bytes, mapping: {...}, outcome: 'won'|'lost', filename?}`."""
        content = params.get("content")
        if not isinstance(content, bytes | bytearray):
            raise ConnectorError("csv_ingest.fetch needs `content` as bytes")
        result = self.ingest(
            bytes(content),
            mapping=dict(params.get("mapping") or {}),
            outcome=params.get("outcome", "won"),
            filename=params.get("filename"),
        )
        if result.errors:
            raise ConnectorDegraded(
                f"{len(result.errors)} row error(s), {result.skipped} row(s) skipped",
                result.drafts,
            )
        return result.drafts

    async def test_connection(self) -> ConnectorStatus:
        return ConnectorStatus(ok=True, detail="no credentials required")
