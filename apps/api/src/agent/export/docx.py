"""DOCX rendering with python-docx (PRD §12).

§12's acceptance is specific: "DOCX opens in Word 2019+ with a working TOC after
F9". Two things have to be true for that, and neither is python-docx's default.

**Real heading styles.** A TOC field collects paragraphs by style, not by font
size. A document whose headings are 16pt bold body text produces an empty TOC
that looks like a bug in Word. So every heading here is written with the
`Heading 1/2/3` styles from `templates/reference.docx`, and the styles carry the
look — which also means the document can be restyled by editing that file rather
than this one.

**A real field, marked dirty.** The TOC is written as a `w:fldChar` field run
carrying the `TOC \\o "1-3" \\h \\z \\u` instruction, with `w:dirty="true"` and
`<w:updateFields/>` in settings. Word then fills it on open. Writing the entries
by hand as text would look right and stop being true the moment the document is
edited, which is the failure mode §12's F9 clause is guarding against.

The content is the same eight sections as the markdown and the PDF, in the same
order, drawn from the same context. `tests/test_report_parity.py` compares them.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING, Any

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

from agent.export.contract import ResearchReport
from agent.export.templating import TEMPLATE_DIR, fmt_money
from agent.export.view import SECTION_TITLES, build_print_context

if TYPE_CHECKING:  # pragma: no cover — typing only
    from docx.document import Document as DocxDocument
    from docx.table import Table
    from docx.text.paragraph import Paragraph

REFERENCE_DOCX = TEMPLATE_DIR / "reference.docx"

#: `Table Grid` ships in the reference template. A table with no style has no
#: borders in Word, which turns every table in the document into loose text.
TABLE_STYLE = "Table Grid"

ACCENT = RGBColor(0x0B, 0x6B, 0x5E)
MUTED = RGBColor(0x66, 0x70, 0x79)


# ---------------------------------------------------------------------------
# Word field plumbing
# ---------------------------------------------------------------------------


def _field(paragraph: Paragraph, instruction: str, *, dirty: bool = True) -> None:
    """Write a real Word field: begin → instrText → separate → placeholder → end.

    `fldSimple` would be shorter and is what most examples use, but Word treats
    a simple field's cached result as authoritative and will not always refresh
    it; the split form with `w:dirty` is what actually updates on open.
    """
    run = paragraph.add_run()
    begin = run._r.makeelement(qn("w:fldChar"), {})
    begin.set(qn("w:fldCharType"), "begin")
    if dirty:
        begin.set(qn("w:dirty"), "true")
    run._r.append(begin)

    instruction_run = paragraph.add_run()
    text = instruction_run._r.makeelement(qn("w:instrText"), {})
    text.set(qn("xml:space"), "preserve")
    text.text = instruction
    instruction_run._r.append(text)

    separate_run = paragraph.add_run()
    separate = separate_run._r.makeelement(qn("w:fldChar"), {})
    separate.set(qn("w:fldCharType"), "separate")
    separate_run._r.append(separate)

    # The placeholder is what a reader sees before Word refreshes the field. It
    # has to say something useful, because a PDF-printed-from-Word that never
    # got refreshed will show exactly this.
    placeholder = paragraph.add_run("Press F5, then F9 to build the table of contents.")
    placeholder.italic = True
    placeholder.font.color.rgb = MUTED

    end_run = paragraph.add_run()
    end = end_run._r.makeelement(qn("w:fldChar"), {})
    end.set(qn("w:fldCharType"), "end")
    end_run._r.append(end)


def _mark_fields_dirty(document: DocxDocument) -> None:
    """`<w:updateFields w:val="true"/>` — Word offers to update on open."""
    settings = document.settings.element
    existing = settings.find(qn("w:updateFields"))
    if existing is None:
        existing = settings.makeelement(qn("w:updateFields"), {})
        settings.append(existing)
    existing.set(qn("w:val"), "true")


def _page_number_footer(document: DocxDocument, run_id: str) -> None:
    """`Run <id>` on the left, `PAGE / NUMPAGES` on the right — as fields, not text."""
    footer = document.sections[0].footer
    paragraph = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
    paragraph.text = ""
    paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT

    label = paragraph.add_run(f"Run {run_id}\t")
    label.font.size = Pt(7.5)
    label.font.color.rgb = MUTED

    _field(paragraph, " PAGE ", dirty=False)
    separator = paragraph.add_run(" / ")
    separator.font.size = Pt(8)
    _field(paragraph, " NUMPAGES ", dirty=False)
    for run in paragraph.runs:
        run.font.size = run.font.size or Pt(8)
        if run.font.color.rgb is None:
            run.font.color.rgb = MUTED


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------


def _clear_body(document: DocxDocument) -> None:
    """Drop whatever the reference template had in it, keeping its styles.

    A reference document exists for its styles; any sample text inside it would
    otherwise appear at the top of every report.
    """
    body = document.element.body
    for child in list(body):
        if child.tag == qn("w:sectPr"):
            continue
        body.remove(child)


def _heading(document: DocxDocument, text: str, level: int) -> Paragraph:
    paragraph: Paragraph = document.add_heading(text, level=level)
    if level == 1:
        paragraph.paragraph_format.page_break_before = True
    return paragraph


def _table(document: DocxDocument, headers: list[str], rows: list[list[Any]]) -> Table | None:
    """A native Word table.

    Returns None for an empty row set: a header row with nothing under it reads
    as "we looked and found zero", which is not what an absent section means.
    """
    if not rows:
        return None
    table: Table = document.add_table(rows=1, cols=len(headers))
    table.style = TABLE_STYLE
    header_cells = table.rows[0].cells
    for index, header in enumerate(headers):
        header_cells[index].text = ""
        run = header_cells[index].paragraphs[0].add_run(header)
        run.bold = True
        run.font.size = Pt(8)
    for row in rows:
        cells = table.add_row().cells
        for index, value in enumerate(row[: len(headers)]):
            cells[index].text = ""
            run = cells[index].paragraphs[0].add_run(str(value))
            run.font.size = Pt(8)
    return table


def _bullets(document: DocxDocument, items: list[str], style: str = "List Bullet") -> None:
    for item in items:
        document.add_paragraph(str(item), style=style)


def _claims(document: DocxDocument, claims: list[Any], citations: Any) -> None:
    """A numbered claim, its confidence, and the evidence it cites."""
    for claim in claims:
        paragraph = document.add_paragraph(style="List Number")
        paragraph.add_run(claim.statement)
        confidence = paragraph.add_run(f"  ({claim.confidence} confidence) ")
        confidence.italic = True
        confidence.font.size = Pt(8)
        confidence.font.color.rgb = MUTED
        marker = paragraph.add_run(citations.markers(claim.evidence_ids))
        marker.font.size = Pt(8)
        marker.font.color.rgb = ACCENT


def _note(document: DocxDocument, text: str) -> Paragraph:
    paragraph = document.add_paragraph()
    run = paragraph.add_run(text)
    run.font.size = Pt(8)
    run.font.color.rgb = MUTED
    return paragraph


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------


def render_docx(report: ResearchReport, *, project_name: str | None = None) -> bytes:
    """The report as a Word document, with a TOC field Word will fill on open."""
    context = build_print_context(report, project_name=project_name)
    citations = context["cite"]

    document: DocxDocument = (
        Document(str(REFERENCE_DOCX)) if REFERENCE_DOCX.is_file() else Document()
    )
    _clear_body(document)

    _cover(document, report, context)
    _toc(document)

    _summary(document, report, context, citations)
    _business(document, report, context)
    _account(document, report, context)
    _competition(document, report, context)
    _demand(document, report, context)
    _readiness(document, report, context)
    _actions(document, report, citations)
    _evidence(document, context)

    _page_number_footer(document, str(report.run_id))
    _mark_fields_dirty(document)

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _cover(document: DocxDocument, report: ResearchReport, context: dict[str, Any]) -> None:
    kicker = document.add_paragraph()
    kicker_run = kicker.add_run("PAID ADS RESEARCH")
    kicker_run.bold = True
    kicker_run.font.size = Pt(9)
    kicker_run.font.color.rgb = ACCENT

    title = document.add_heading("Stage 01 — Marketing Intelligence", level=0)
    title.paragraph_format.space_after = Pt(4)

    subtitle = document.add_paragraph()
    subtitle_run = subtitle.add_run(context["project_name"] or "—")
    subtitle_run.font.size = Pt(14)
    subtitle_run.font.color.rgb = MUTED

    verdict = document.add_paragraph()
    verdict_run = verdict.add_run(f"Launch readiness: {context['readiness_label']}")
    verdict_run.bold = True
    verdict_run.font.size = Pt(12)
    verdict_run.font.color.rgb = ACCENT

    _table(
        document,
        ["", ""],
        [
            ["Generated", report.generated_at.strftime("%Y-%m-%d %H:%M UTC")],
            ["Run", str(report.run_id)],
            ["Project id", str(report.project_id)],
            ["Research cost", fmt_money(report.cost_usd, "USD")],
            ["Schema", report.schema_version],
        ],
    )

    if report.degraded_sources:
        _note(
            document,
            f"Degraded sources: {', '.join(report.degraded_sources)}. Findings drawn from them "
            "are thinner than the rest of this report.",
        )


def _toc(document: DocxDocument) -> None:
    heading = document.add_heading("Contents", level=1)
    heading.paragraph_format.page_break_before = True
    paragraph = document.add_paragraph()
    _field(paragraph, r' TOC \o "1-3" \h \z \u ')
    document.add_paragraph().add_run().add_break(WD_BREAK.PAGE)


def _summary(
    document: DocxDocument,
    report: ResearchReport,
    context: dict[str, Any],
    citations: Any,
) -> None:
    _heading(document, SECTION_TITLES[0], 1)
    document.add_paragraph(report.executive_summary)

    _heading(document, f"Launch readiness: {context['readiness_label']}", 2)
    if report.launch_blockers:
        count = len(report.launch_blockers)
        document.add_paragraph(
            f"{count} blocker{'' if count == 1 else 's'} must be cleared before launch."
        )
        _claims(document, report.launch_blockers, citations)
    else:
        document.add_paragraph("No launch blockers were recorded.")


def _business(document: DocxDocument, report: ResearchReport, context: dict[str, Any]) -> None:
    business = report.business_context
    _heading(document, SECTION_TITLES[1], 1)

    if business.products:
        _heading(document, "Offer economics", 2)
        _table(
            document,
            ["Product", "Price model", "ACV", "Gross margin", "Delivery notes"],
            context["product_rows"],
        )
        document.add_paragraph(
            f"LTV estimate {fmt_money(business.ltv_estimate)} · "
            f"Target CAC {fmt_money(business.target_cac)} · "
            f"Payback {business.payback_months or '—'} months"
        )

    if business.segments:
        _heading(document, "Ideal customer profile", 2)
        for segment in business.segments:
            _heading(document, segment.label, 3)
            details = []
            if segment.share_of_revenue_pct is not None:
                details.append(f"{segment.share_of_revenue_pct:g}% of revenue")
            if segment.triggers:
                details.append("Buying triggers: " + ", ".join(segment.triggers))
            if segment.jobs_to_be_done:
                details.append("Jobs to be done: " + ", ".join(segment.jobs_to_be_done))
            if segment.firmographics:
                # A sentence from node 1.1.2 or a map from a CRM rollup; the
                # contract accepts both, so this has to render both.
                details.append(
                    "Firmographics: "
                    + (
                        ", ".join(
                            f"{key}={value}" for key, value in sorted(segment.firmographics.items())
                        )
                        if isinstance(segment.firmographics, dict)
                        else str(segment.firmographics)
                    )
                )
            _bullets(document, details)

    if business.exclusions:
        _heading(document, "Who we are not selling to", 2)
        _table(
            document,
            ["Persona", "Disqualifier", "Observable signal", "Suggested negatives"],
            context["exclusion_rows"],
        )

    if business.markets:
        _heading(document, "Markets", 2)
        _table(
            document,
            ["Market", "Language", "Currency", "Demand months", "Dead months"],
            context["market_rows"],
        )

    compliance = business.compliance
    if compliance:
        _heading(document, "Compliance guardrails", 2)
        if compliance.prohibited_claims:
            document.add_paragraph("Never claim:")
            _bullets(document, compliance.prohibited_claims)
        if compliance.required_disclaimers:
            document.add_paragraph("Always disclose:")
            _bullets(document, compliance.required_disclaimers)
        _table(document, ["Regulated term", "Rule"], context["regulated_rows"])


def _account(document: DocxDocument, report: ResearchReport, context: dict[str, Any]) -> None:
    learnings = report.account_learnings
    _heading(document, SECTION_TITLES[2], 1)

    if context["performance_rows"]:
        _heading(document, "Winners and losers", 2)
        _table(document, ["Outcome", "Campaign", "Result", "Period"], context["performance_rows"])

    if learnings.structural_findings:
        _heading(document, "Structural findings", 2)
        _bullets(document, learnings.structural_findings)

    if context["profitable_rows"]:
        _heading(document, "Search terms that paid", 2)
        _table(
            document,
            ["Term", "Cost", "Conversions", "CPA", "ROAS"],
            context["profitable_rows"],
        )

    if context["wasteful_rows"]:
        _heading(document, "Search terms that did not", 2)
        _table(
            document,
            ["Term", "Cost", "Conversions", "Recommended action"],
            context["wasteful_rows"],
        )
        document.add_paragraph(
            f"Wasted spend across the terms above: {fmt_money(context['wasted_spend'])}."
        )

    if learnings.tried_and_failed:
        _heading(document, "Do not repeat", 2)
        _bullets(
            document,
            [
                f"{experiment.what} ({experiment.when or '—'}) — {experiment.outcome or '—'} "
                f"{experiment.do_not_repeat_reason or ''}".strip()
                for experiment in learnings.tried_and_failed
            ],
        )


def _competition(document: DocxDocument, report: ResearchReport, context: dict[str, Any]) -> None:
    competition = report.competitive_landscape
    _heading(document, SECTION_TITLES[3], 1)

    if context["competitor_rows"]:
        _heading(document, "Competitor set", 2)
        _table(
            document, ["Domain", "Name", "Threat", "Overlap", "Basis"], context["competitor_rows"]
        )

    if context["cluster_rows"]:
        _heading(document, "What they are all saying", 2)
        _table(document, ["Theme", "Ads", "Advertisers"], context["cluster_rows"])

    if context["ad_rows"]:
        _heading(document, "Creative corpus", 2)
        total = context["ads_total"]
        document.add_paragraph(f"{total} ad{'' if total == 1 else 's'} were captured.")
        if context["ads_truncated"]:
            _note(
                document,
                f"The {len(context['ad_rows'])} below are the first {len(context['ad_rows'])}; "
                "the rest are in the JSON export.",
            )
        _table(
            document,
            ["Advertiser", "Headline", "Angle", "Offer", "CTA", "Seen"],
            context["ad_rows"],
        )

    if context["spend_rows"]:
        _heading(document, "Estimated spend", 2)
        _note(
            document,
            "Every figure below is an estimate with a stated method. None of it is reported fact.",
        )
        _table(
            document,
            ["Competitor", "Estimated monthly spend", "Method", "Confidence", "Peaks"],
            context["spend_rows"],
        )

    if competition.whitespace or competition.recommended_claim:
        _heading(document, "Whitespace", 2)
        if competition.recommended_claim:
            paragraph = document.add_paragraph()
            paragraph.add_run("Recommended claim: ").bold = True
            paragraph.add_run(competition.recommended_claim)
        for gap in competition.whitespace:
            _heading(document, gap.claim, 3)
            _bullets(
                document,
                [
                    f"Why nobody is saying it: {gap.why_unsaid or '—'}",
                    f"Our proof: {gap.our_proof or '—'}",
                    f"Risk: {gap.risk or '—'}",
                ],
            )
        if competition.substantiation_required:
            document.add_paragraph("Substantiation required before this claim runs:")
            _bullets(document, competition.substantiation_required)


def _demand(document: DocxDocument, report: ResearchReport, context: dict[str, Any]) -> None:
    demand = report.demand_map
    _heading(document, SECTION_TITLES[4], 1)
    document.add_paragraph(
        f"{demand.total_keywords:,} keywords were classified and priced. The complete list is the "
        "CSV export; the table below is a preview."
    )

    if context["keyword_rows"]:
        suffix = (
            f" (top {len(context['keyword_rows'])} worth buying first)"
            if context["keywords_truncated"]
            else ""
        )
        _heading(document, f"Priced keywords{suffix}", 2)
        _table(
            document,
            [
                "Term",
                "Market",
                "Intent",
                "Volume",
                "CPC low",
                "CPC high",
                "Competition",
                "Destination",
                "Verdict",
            ],
            context["keyword_rows"],
        )
        if context["keywords_truncated"]:
            _note(
                document,
                f"{context['keywords_total']} keywords are in the CSV export. "
                f"{len(context['keyword_rows'])} are shown here.",
            )

    if context["mapping_rows"]:
        _heading(document, "Keyword to page", 2)
        _table(
            document, ["Cluster", "Destination", "Relevance", "Verdict"], context["mapping_rows"]
        )

    if demand.content_gaps:
        _heading(document, "Content gaps", 2)
        _bullets(
            document,
            [f"{gap.cluster} needs a {gap.required_page_type}." for gap in demand.content_gaps],
        )

    if context["negative_rows"]:
        _heading(document, "Negative keyword blocklist", 2)
        _table(document, ["Term", "Match type", "Source", "Reason"], context["negative_rows"])


def _readiness(document: DocxDocument, report: ResearchReport, context: dict[str, Any]) -> None:
    readiness = report.readiness
    _heading(document, SECTION_TITLES[5], 1)

    if context["page_rows"]:
        _heading(document, "Landing pages", 2)
        _table(
            document,
            ["URL", "LCP", "CLS", "Mobile", "Form fields", "Severity", "Issues"],
            context["page_rows"],
        )

    if context["conversion_rows"]:
        _heading(document, "Conversion tracking", 2)
        _table(
            document,
            ["Action", "Status", "Last conversion", "Stale for"],
            context["conversion_rows"],
        )

    probe = readiness.synthetic_check
    if probe:
        observed = "Yes" if probe.observed_in_ads_api else "No"
        fired = probe.fired_at.strftime("%Y-%m-%d %H:%M UTC") if probe.fired_at else "—"
        document.add_paragraph(
            f"Synthetic conversion probe: fired {fired}, observed in the Ads API: {observed}. "
            f"Verdict: {probe.verdict or '—'}."
        )

    if readiness.alerts:
        document.add_paragraph("Alerts:")
        _bullets(document, readiness.alerts)

    if context["audience_rows"]:
        _heading(document, "Audience lists", 2)
        _table(
            document,
            ["List", "Size", "Consent basis", "Markets", "Usable", "Blocker"],
            context["audience_rows"],
        )

    if context["scenario_rows"]:
        _heading(document, "Opportunity sizing", 2)
        _table(
            document,
            [
                "Monthly budget",
                "Est. clicks",
                "Est. conversions",
                "Est. CPA",
                "Est. revenue",
                "Interval",
            ],
            context["scenario_rows"],
        )
        _bullets(
            document,
            [
                f"{fmt_money(scenario.budget_usd_month, 'USD', 0)}/month assumes: "
                + "; ".join(scenario.assumptions)
                for scenario in readiness.scenarios
                if scenario.assumptions
            ],
        )


def _actions(document: DocxDocument, report: ResearchReport, citations: Any) -> None:
    _heading(document, SECTION_TITLES[6], 1)
    if report.recommended_next_actions:
        _claims(document, report.recommended_next_actions, citations)
    else:
        document.add_paragraph("No next actions were recorded.")

    if report.open_questions:
        _heading(document, "Open questions", 2)
        _bullets(document, report.open_questions)


def _evidence(document: DocxDocument, context: dict[str, Any]) -> None:
    _heading(document, SECTION_TITLES[7], 1)
    rows = context["evidence_rows"]
    if rows:
        document.add_paragraph(
            "Every claim above cites the evidence it rests on. These are the row ids in this "
            "workspace's evidence store."
        )
        _table(document, ["Marker", "Evidence id"], rows)
    else:
        document.add_paragraph(
            "This report cites no evidence, which for a completed run is itself a finding."
        )
