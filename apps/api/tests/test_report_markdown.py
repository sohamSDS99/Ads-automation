"""Markdown rendering (PRD §11/§12).

The markdown is the source of truth for the other formats, and PRD §11 calls the
template deterministic. Both of those are claims a test can hold to account, so
these do.
"""

from __future__ import annotations

from agent.export.contract import PricedKeyword
from agent.export.markdown import normalise, render_markdown
from agent.export.view import KEYWORD_PREVIEW_LIMIT, SECTION_TITLES
from tests.report_support import PROJECT_NAME, golden_report, many_keywords, minimal_report


def render() -> str:
    return render_markdown(golden_report(), project_name=PROJECT_NAME)


def _table_rows(markdown: str, heading: str) -> list[str]:
    """Every `|`-delimited line of the one table under `heading`.

    Scoped to a single table on purpose: the document holds several with
    different column counts, and comparing rows across them would compare
    nothing.
    """
    lines = markdown.splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith(heading))
    rows: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("|"):
            rows.append(line)
        elif rows:
            break
    return rows


def test_rendering_is_deterministic() -> None:
    """Same report, same bytes. Everything downstream rests on this."""
    assert render() == render()


def test_every_section_is_present() -> None:
    markdown = render()
    for title in SECTION_TITLES:
        assert title in markdown, f"section missing from the markdown: {title}"


def test_the_header_carries_the_run_identity() -> None:
    markdown = render()
    report = golden_report()
    assert PROJECT_NAME in markdown
    assert str(report.run_id) in markdown
    assert "4.87 USD" in markdown


def test_claims_carry_their_citations() -> None:
    markdown = render()
    # First blocker cites the first two evidence ids, which are E1 and E2.
    assert "[E1, E2]" in markdown
    assert "## 8. Evidence index" in markdown
    assert "aaaaaaaa-0000-4000-8000-000000000001" in markdown


def test_degraded_sources_are_called_out() -> None:
    assert "Degraded sources" in render()


def test_a_spend_estimate_states_its_method() -> None:
    """PRD §10, 1.3.3: never present an estimate as fact."""
    markdown = render()
    assert "None of it is reported fact" in markdown
    assert "Impression share against our own observed impressions" in markdown


def test_missing_values_render_as_a_placeholder_not_a_crash() -> None:
    markdown = render()
    # `quote_request_de` has never converted: no date, and no "— days" either.
    assert "| quote_request_de | no_recent_conversions | — | — |" in markdown
    assert "— days" not in markdown


def test_a_pipe_in_a_keyword_cannot_break_the_table() -> None:
    """Keywords come from search queries. One will contain a pipe eventually."""
    report = golden_report()
    report.priced_keyword_list[0].term = "sds | software"
    markdown = render_markdown(report, project_name=PROJECT_NAME)

    assert "sds \\| software" in markdown

    rows = _table_rows(markdown, "### Priced keywords")
    assert any("sds \\| software" in row for row in rows)
    # Count SEPARATORS, not `|` characters: an escaped pipe is still a pipe in
    # the text, and counting it would fail a row that renders perfectly.
    widths = {row.replace("\\|", "").count("|") for row in rows}
    assert len(widths) == 1, f"the escaped pipe changed a row's column count: {rows}"


def test_a_long_keyword_list_is_truncated_and_says_so() -> None:
    """A table quietly cut to 50 rows reads as the complete list."""
    report = golden_report()
    report.priced_keyword_list = many_keywords(KEYWORD_PREVIEW_LIMIT + 25)
    markdown = render_markdown(report, project_name=PROJECT_NAME)

    assert f"top {KEYWORD_PREVIEW_LIMIT} worth buying first" in markdown
    assert f"{KEYWORD_PREVIEW_LIMIT + 25} keywords are in the CSV export" in markdown
    # Highest volume first, and the cap honoured exactly.
    assert "keyword 0000" in markdown
    assert f"keyword {KEYWORD_PREVIEW_LIMIT:04d}" not in markdown


def test_a_short_keyword_list_claims_no_truncation() -> None:
    markdown = render()
    assert "worth buying first)" not in markdown
    assert "are in the CSV export" not in markdown


def test_scrape_fragments_do_not_lead_the_keyword_preview() -> None:
    """The headline table must not open with what the classifier already binned.

    A vendor site-scrape returns debris — two-letter strings carrying six-figure
    volumes — and sorting the preview on volume alone put those at the top of
    the report's most-read table while the terms worth bidding on sat below the
    cut. Intent decides the tier; volume only orders within it.
    """
    report = golden_report()
    report.priced_keyword_list = [
        PricedKeyword(
            term="c h",
            market="US",
            intent="irrelevant",
            volume=1_000_000,
            cpc_low=0.42,
            cpc_high=1.63,
        ),
        PricedKeyword(
            term="ehs software",
            market="US",
            intent="commercial_investigation",
            volume=1_000,
            cpc_low=14.7,
            cpc_high=84.06,
        ),
        PricedKeyword(
            term="sds sheets",
            market="US",
            intent="transactional",
            volume=8_100,
            cpc_low=1.78,
            cpc_high=4.34,
        ),
    ]
    rows = _table_rows(render_markdown(report, project_name=PROJECT_NAME), "### Priced keywords")

    # `_table_rows` yields the header and its separator first.
    terms = [row.split("|")[1].strip() for row in rows][2:]
    assert terms[0] == "sds sheets", terms
    assert terms[1] == "ehs software", terms
    # Still present — the preview is a view of the CSV, not a different list.
    assert terms[-1] == "c h", terms


def test_an_empty_report_still_renders() -> None:
    """A run that degraded badly still has to produce a document."""
    markdown = render_markdown(minimal_report(), project_name=None)
    for title in SECTION_TITLES:
        assert title in markdown
    assert "No launch blockers were recorded." in markdown
    assert "No next actions were recorded." in markdown
    assert "cites no evidence" in markdown


def test_normalise_collapses_gaps_and_strips_trailing_space() -> None:
    assert normalise("a\n\n\n\n\nb") == "a\n\nb\n"
    assert normalise("a   \nb\t\n") == "a\nb\n"
    assert normalise("a") == "a\n"
