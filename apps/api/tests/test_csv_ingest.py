"""Column mapping, type coercion, and the row-level error report (PRD §9.5)."""

from __future__ import annotations

import pytest

from agent.config import Settings
from agent.connectors.base import ConnectorContext, ConnectorDegraded, ConnectorError
from agent.connectors.csv_ingest import (
    CANONICAL_FIELDS,
    CsvIngestConnector,
    coerce,
    preview,
    suggest_mapping,
)

TEST_KEY = "dW5pdC10ZXN0LWtleS0zMi1ieXRlcy1leGFjdGx5ISE="

GOOD = (
    b"Company,Industry,Deal Amount,Close Date,Country\n"
    b"Acme Ltd,Manufacturing,12000.50,2025-03-01,Norway\n"
    b"Beta Inc,Chemicals,8400,2025-04-15,Germany\n"
)

MESSY = (
    b"Company,Deal Amount,Close Date\n"
    b"Acme Ltd,1200.50,2025-03-01\n"
    b",500,2025-03-02\n"
    b"Gamma GmbH,not-a-number,bogus-date\n"
)


def connector() -> CsvIngestConnector:
    return CsvIngestConnector(ConnectorContext(settings=Settings(app_encryption_key=TEST_KEY)))


# --- mapping proposals ---------------------------------------------------


def test_the_prd_canonical_fields_are_all_present() -> None:
    assert {item.name for item in CANONICAL_FIELDS} == {
        "account_name",
        "industry",
        "employee_count",
        "country",
        "deal_value",
        "close_reason",
        "source",
        "created_at",
    }


def test_only_account_name_is_required() -> None:
    """A CRM export missing industry is still usable; one missing the company is not."""
    assert [item.name for item in CANONICAL_FIELDS if item.required] == ["account_name"]


def test_headers_are_matched_case_and_punctuation_insensitively() -> None:
    mapping = suggest_mapping(["ACCOUNT_NAME", "Deal  Value"])
    assert mapping["ACCOUNT_NAME"] == "account_name"
    assert mapping["Deal  Value"] == "deal_value"


def test_the_longest_alias_wins() -> None:
    """ "Company Size" must not be claimed by `account_name`'s "company" alias."""
    mapping = suggest_mapping(["Company", "Company Size"])
    assert mapping["Company"] == "account_name"
    assert mapping["Company Size"] == "employee_count"


def test_a_canonical_field_is_claimed_at_most_once() -> None:
    mapping = suggest_mapping(["Company", "Customer", "Account"])
    assert list(mapping.values()).count("account_name") == 1


def test_an_unrecognisable_header_is_simply_unmapped() -> None:
    """A guess would be worse than nothing; the UI shows it blank for a human to set."""
    assert "Internal Ref 77" not in suggest_mapping(["Company", "Internal Ref 77"])


# --- coercion ------------------------------------------------------------


def test_us_and_european_decimals_both_parse() -> None:
    assert coerce("1,234.56", "decimal") == 1234.56
    assert coerce("1.234,56", "decimal") == 1234.56


def test_currency_symbols_are_stripped() -> None:
    assert coerce("$12,000", "decimal") == 12000.0
    assert coerce("€8 400", "int") == 8400


def test_several_date_formats_parse_to_iso() -> None:
    assert coerce("2025-03-01", "date") == "2025-03-01"
    assert coerce("01/03/2025", "date") == "2025-03-01"
    assert coerce("1 Mar 2025", "date") == "2025-03-01"


def test_an_empty_cell_is_none_not_an_error() -> None:
    assert coerce("", "decimal") is None
    assert coerce("   ", "date") is None


def test_an_unparseable_value_says_what_is_wrong() -> None:
    with pytest.raises(ValueError, match="not a number"):
        coerce("banana", "decimal")
    with pytest.raises(ValueError, match="date format"):
        coerce("32/13/2025", "date")


# --- preview -------------------------------------------------------------


def test_preview_reports_headers_rows_and_a_proposal() -> None:
    inspected = preview(GOOD)
    assert inspected.headers == ["Company", "Industry", "Deal Amount", "Close Date", "Country"]
    assert inspected.row_count == 2
    assert inspected.suggested_mapping["Company"] == "account_name"


def test_preview_handles_a_semicolon_delimited_export() -> None:
    """European Excel writes `;`. Sniffing beats making the user re-export."""
    inspected = preview(b"Company;Deal Amount\nAcme;12\n")
    assert inspected.headers == ["Company", "Deal Amount"]


def test_a_utf8_bom_does_not_become_part_of_the_first_header() -> None:
    """Excel writes a BOM, and `\\ufeffCompany` matches no alias at all."""
    inspected = preview("Company,Deal Amount\nAcme,12\n".encode("utf-8-sig"))
    assert inspected.headers[0] == "Company"


def test_a_headerless_file_is_refused() -> None:
    with pytest.raises(ConnectorError, match="header"):
        preview(b"")


# --- ingest --------------------------------------------------------------


def test_a_clean_file_produces_one_draft_per_row() -> None:
    result = connector().ingest(GOOD, mapping=preview(GOOD).suggested_mapping, outcome="won")
    assert result.accepted == 2
    assert result.skipped == 0
    assert result.errors == []
    assert {draft.kind for draft in result.drafts} == {"crm_won"}


def test_the_outcome_picks_the_evidence_kind() -> None:
    result = connector().ingest(GOOD, mapping=preview(GOOD).suggested_mapping, outcome="lost")
    assert {draft.kind for draft in result.drafts} == {"crm_lost"}
    assert all(draft.payload["outcome"] == "lost" for draft in result.drafts)


def test_a_bad_optional_value_costs_the_field_not_the_row() -> None:
    """Rejecting the row would mean a user edits a CSV in Excel — how data gets mangled."""
    result = connector().ingest(MESSY, mapping=preview(MESSY).suggested_mapping, outcome="won")
    gamma = next(d for d in result.drafts if d.payload["account_name"] == "Gamma GmbH")
    assert gamma.payload["deal_value"] is None
    assert gamma.payload["created_at"] is None


def test_a_missing_required_value_costs_the_row() -> None:
    result = connector().ingest(MESSY, mapping=preview(MESSY).suggested_mapping, outcome="won")
    assert result.skipped == 1
    assert "" not in [d.payload["account_name"] for d in result.drafts]


def test_errors_name_the_line_number_a_human_can_find() -> None:
    """Row 2 is the first data row: the header is line 1, as Excel shows it."""
    result = connector().ingest(MESSY, mapping=preview(MESSY).suggested_mapping, outcome="won")
    assert {error.row for error in result.errors} == {3, 4}
    assert any(error.problem == "not a number" for error in result.errors)


def test_a_mapping_missing_a_required_field_is_refused_outright() -> None:
    with pytest.raises(ConnectorError, match="account_name"):
        connector().ingest(GOOD, mapping={"Deal Amount": "deal_value"}, outcome="won")


def test_an_unknown_canonical_field_is_refused() -> None:
    with pytest.raises(ConnectorError, match="unknown canonical"):
        connector().ingest(
            GOOD, mapping={"Company": "account_name", "Industry": "not_a_field"}, outcome="won"
        )


def test_an_invalid_outcome_is_refused() -> None:
    with pytest.raises(ConnectorError, match="won"):
        connector().ingest(GOOD, mapping={"Company": "account_name"}, outcome="maybe")  # type: ignore[arg-type]


def test_an_oversized_file_is_refused_before_parsing() -> None:
    small = CsvIngestConnector(
        ConnectorContext(settings=Settings(app_encryption_key=TEST_KEY, csv_max_bytes=10))
    )
    with pytest.raises(ConnectorError, match="limit"):
        small.ingest(GOOD, mapping={"Company": "account_name"}, outcome="won")


async def test_fetch_degrades_rather_than_discarding_good_rows() -> None:
    with pytest.raises(ConnectorDegraded) as caught:
        await connector().fetch(
            {"content": MESSY, "mapping": preview(MESSY).suggested_mapping, "outcome": "won"}
        )
    assert len(caught.value.drafts) == 2


async def test_fetch_needs_bytes() -> None:
    with pytest.raises(ConnectorError, match="bytes"):
        await connector().fetch({"content": "a string", "mapping": {}, "outcome": "won"})


def test_the_filename_is_recorded_on_every_row() -> None:
    """Provenance: a year later, "which export was this" is a real question."""
    result = connector().ingest(
        GOOD, mapping=preview(GOOD).suggested_mapping, outcome="won", filename="won-q1.csv"
    )
    assert all(draft.payload["source_file"] == "won-q1.csv" for draft in result.drafts)


def test_two_identical_rows_hash_identically() -> None:
    """Dedupe depends on it: re-uploading the same export must not double the corpus."""
    first = connector().ingest(GOOD, mapping=preview(GOOD).suggested_mapping, outcome="won")
    second = connector().ingest(GOOD, mapping=preview(GOOD).suggested_mapping, outcome="won")
    assert [d.hash() for d in first.drafts] == [d.hash() for d in second.drafts]
