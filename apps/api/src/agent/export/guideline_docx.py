"""Guideline DOCX, with a TOC Word fills on open (Stage 03 PRD §14).

The Stage 03 sibling of `export/plan_docx.py`, and it reuses that module's
primitives outright — `_field`, `_table`, `_heading`, `_clear_body` — rather
than restating them. Two implementations of "write a real Word field" would
eventually disagree about `w:dirty`, and the TOC would quietly stop populating
in one of the documents.

§14's acceptance for this format: "DOCX opens in Word 2019+ and its TOC
populates on F9." That needs real `Heading 1/2/3` styles from
`templates/reference.docx` and a real `fldChar` field marked dirty, both of
which live in `export/docx.py` already.

**This is the format legal will actually redline**, which is why the claims
register is a native table rather than a rendering of one: a reviewer needs to
put a comment on one claim's row and strike a word in its text, and neither is
possible in an image or a code block.

**Two exports of one rulebook are byte-identical**, which python-docx does not
give you: `zipfile` stamps every archive member with `datetime.now()`. Nothing
in the repository asserted DOCX determinism before S3-P6, so `plan_docx.py` and
`docx.py` still carry that defect — see `export/archives`.

**The draft watermark goes in the header.** A Word document has no
`position: fixed`, so the mark cannot be painted once and repeated the way the
PDF does it. Word repeats a header on every page by definition — which is
exactly §14's requirement, arrived at by the one route Word actually offers.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING, Any

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt, RGBColor

from agent.export.archives import normalise_zip
from agent.export.docx import (
    MUTED,
    REFERENCE_DOCX,
    _clear_body,
    _field,
    _heading,
    _mark_fields_dirty,
    _note,
    _page_number_footer,
    _table,
)
from agent.export.guideline_contract import ContentGuideline
from agent.export.guideline_view import build_context
from agent.export.templating import fmt_date, fmt_datetime, fmt_text

if TYPE_CHECKING:  # pragma: no cover — typing only
    from docx.document import Document as DocxDocument

DRAFT_RED = RGBColor(0xB9, 0x1C, 0x1C)
PUBLISHED_GREEN = RGBColor(0x04, 0x78, 0x57)

#: What the tables cut at, matching the PDF. Each says what it omitted.
CLAIM_ROWS = 500
RULE_ROWS = 300


def render_guideline_docx(
    guideline: ContentGuideline,
    *,
    project_name: str | None = None,
    published_by_name: str = "",
    ruleset_version: str = "",
) -> bytes:
    """The rulebook as a Word document."""
    context = build_context(
        guideline,
        project_name=project_name,
        published_by_name=published_by_name,
        ruleset_version=ruleset_version,
    )
    document: DocxDocument = Document(str(REFERENCE_DOCX))
    _clear_body(document)
    _status_header(document, guideline, context)
    _page_number_footer(document, str(guideline.guideline_run_id))

    _cover(document, guideline, context)
    _toc(document)
    _summary(document, guideline, context)
    _voice(document, context)
    _lexicon(document, context)
    _visual(document, context)
    _claims(document, guideline, context)
    _policy(document, context)
    _specs(document, context)
    _governance(document, guideline, context)
    _rules(document, context)

    _mark_fields_dirty(document)
    buffer = io.BytesIO()
    document.save(buffer)
    # `zipfile` stamps every member with the wall clock, so two renders that
    # straddle a second tick differ — see `export/archives`. Two exports of a
    # published rulebook have to be byte-identical (§14 acceptance 2), and a
    # test comparing two fast renders would pass for months without catching it.
    return normalise_zip(buffer.getvalue(), guideline.generated_at)


# ---------------------------------------------------------------------------
# chrome
# ---------------------------------------------------------------------------


def _status_header(
    document: DocxDocument, guideline: ContentGuideline, context: dict[str, Any]
) -> None:
    """§14's watermark, by the only mechanism Word repeats on every page."""
    header = document.sections[0].header
    paragraph = header.paragraphs[0] if header.paragraphs else header.add_paragraph()
    paragraph.text = ""
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if context["published"]:
        run = paragraph.add_run(
            f"PUBLISHED — v{guideline.version} · {guideline.generated_at.date().isoformat()}"
        )
        run.font.color.rgb = PUBLISHED_GREEN
    else:
        run = paragraph.add_run(f"{context['watermark']} — not a legal sign-off record")
        run.font.color.rgb = DRAFT_RED
    run.bold = True
    run.font.size = Pt(8)


def _cover(document: DocxDocument, guideline: ContentGuideline, context: dict[str, Any]) -> None:
    title = document.add_heading("Content Guidelines", level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.LEFT
    subtitle = document.add_paragraph()
    run = subtitle.add_run(context["project_name"])
    run.font.size = Pt(14)

    facts = [
        ["Status", context["status_label"]],
        ["Version", context["version"]],
        ["Mode", context["mode_label"]],
        ["Guideline run", str(guideline.guideline_run_id)],
        ["Generated", fmt_datetime(guideline.generated_at)],
        ["Constants", f"{guideline.constants_version} · schema {guideline.schema_version}"],
    ]
    if context["ruleset_version"]:
        facts.append(["Ruleset", context["ruleset_version"]])
    if context["published_by_name"]:
        facts.append(["Published by", context["published_by_name"]])
    _table(document, ["", ""], facts)

    if context["unbound"]:
        _note(
            document,
            f"Built without {', '.join(context['unbound'])}. Those inputs were not bound to "
            "this run, so the rules below were inferred from what was available.",
        )
    if context["degraded"]:
        _note(
            document,
            f"Degraded sources: {', '.join(context['degraded'])}. Some inputs could not be "
            "read in full.",
        )


def _toc(document: DocxDocument) -> None:
    _heading(document, "Contents", 1)
    paragraph = document.add_paragraph()
    _field(paragraph, r'TOC \o "1-3" \h \z \u')
    _note(document, "Press F9 in Word to populate this table of contents.")


# ---------------------------------------------------------------------------
# sections
# ---------------------------------------------------------------------------


def _summary(document: DocxDocument, guideline: ContentGuideline, context: dict[str, Any]) -> None:
    _heading(document, "Executive summary", 1)
    document.add_paragraph(fmt_text(guideline.executive_summary))
    if context["blocking_issues"]:
        _heading(document, "Blocking issues", 2)
        _table(
            document,
            ["Section", "Finding", "Fix"],
            [[issue.section, issue.finding, issue.fix] for issue in context["blocking_issues"]],
        )


def _voice(document: DocxDocument, context: dict[str, Any]) -> None:
    voice = context["brand"].voice
    _heading(document, "Brand voice", 1)
    if not voice.voice_words:
        _note(document, "No voice profile was produced for this project.")
        return
    document.add_paragraph(f"This brand sounds: {', '.join(voice.voice_words)}")
    if voice.definition_per_word:
        _table(
            document,
            ["Word", "What it means here"],
            [[word, text] for word, text in voice.definition_per_word.items()],
        )
    if voice.do_examples:
        _heading(document, "Do — quoted from real copy", 2)
        _table(
            document,
            ["Example", "Why"],
            [[item.text, item.why] for item in voice.do_examples],
        )
    if voice.dont_examples:
        _heading(document, "Don't", 2)
        _table(
            document,
            ["Example", "Instead"],
            [[item.text, item.rewritten_as or ""] for item in voice.dont_examples],
        )


def _lexicon(document: DocxDocument, context: dict[str, Any]) -> None:
    lexicon = context["brand"].lexicon
    _heading(document, "Words we use, and words we don't", 1)
    if lexicon.never:
        _heading(document, "Never", 2)
        _table(
            document,
            ["Term", "Why", "Use instead", "Severity"],
            [
                [entry.term, entry.reason, entry.suggested_replacement or "", entry.severity]
                for entry in lexicon.never
            ],
        )
    if lexicon.always:
        _heading(document, "Always", 2)
        _table(
            document,
            ["Term", "Where", "Severity"],
            [[entry.term, entry.context, entry.severity] for entry in lexicon.always],
        )
    if lexicon.conflicts:
        _note(
            document,
            f"{len(lexicon.conflicts)} unresolved conflict(s) in this lexicon. A term that is "
            "both required and banned cannot be satisfied.",
        )


def _visual(document: DocxDocument, context: dict[str, Any]) -> None:
    visual = context["brand"].visual_identity
    _heading(document, "Visual identity", 1)
    for title, section in (
        ("Logo", visual.logo),
        ("Colour", visual.colour),
        ("Imagery", visual.imagery),
    ):
        if not section:
            continue
        _heading(document, title, 2)
        _table(
            document,
            ["", ""],
            [[key.replace("_", " "), fmt_text(value)] for key, value in section.items()],
        )


def _claims(document: DocxDocument, guideline: ContentGuideline, context: dict[str, Any]) -> None:
    register = context["register"]
    _heading(document, "The claims register", 1)
    document.add_paragraph(
        f"{len(register.claims)} claim(s)"
        + (
            f", of which {register.unsupported_count} are unsupported."
            if register.unsupported_count
            else "."
        )
    )
    if register.claims:
        _table(
            document,
            ["Claim", "Type", "Status", "Risk", "Expires", "Markets"],
            [
                [
                    claim.claim_text,
                    claim.claim_type,
                    claim.status,
                    claim.risk_tier,
                    fmt_date(claim.expires_at),
                    ", ".join(claim.market_scope),
                ]
                for claim in register.claims[:CLAIM_ROWS]
            ],
        )
        if len(register.claims) > CLAIM_ROWS:
            _note(
                document,
                f"Showing the first {CLAIM_ROWS} of {len(register.claims)} claims. The full "
                "register is in the JSON and XLSX exports.",
            )
    else:
        _note(document, "No claims are registered for this project.")

    if context["expiring_soon"]:
        _heading(document, "Expiring within 30 days", 2)
        _table(
            document,
            ["Claim", "Expires"],
            [[claim.claim_text, fmt_date(claim.expires_at)] for claim in context["expiring_soon"]],
        )
    if register.offer_rules:
        _heading(document, "Offer integrity", 2)
        _table(
            document,
            ["Construction", "Requirement", "Severity"],
            [[rule.construction, rule.requirement, rule.severity] for rule in register.offer_rules],
        )


def _policy(document: DocxDocument, context: dict[str, Any]) -> None:
    policy = context["policy"]
    _heading(document, "Google policy", 1)
    if policy.applicable:
        _heading(document, "Applies to us", 2)
        _table(
            document,
            ["Area", "Why", "Markets", "Obligations"],
            [
                [
                    area.area,
                    area.why_applicable,
                    ", ".join(area.markets),
                    ", ".join(area.obligations),
                ]
                for area in policy.applicable
            ],
        )
    if policy.not_applicable:
        _heading(document, "Checked, and does not apply", 2)
        _note(
            document,
            "The record that we looked is worth as much as the record of what we found.",
        )
        _table(
            document,
            ["Area", "Why not"],
            [[area.area, area.why_not] for area in policy.not_applicable],
        )
    if policy.disclosure_rules:
        _heading(document, "AI disclosure", 2)
        _table(
            document,
            ["Surfaces", "Markets", "Required text", "Placement"],
            [
                [
                    ", ".join(rule.surfaces) or "every surface",
                    ", ".join(rule.markets) or "every market",
                    rule.required_text,
                    rule.placement,
                ]
                for rule in policy.disclosure_rules
            ],
        )


def _specs(document: DocxDocument, context: dict[str, Any]) -> None:
    specs = context["specs"]
    _heading(document, "Asset specifications", 1)
    document.add_paragraph(f"Scope: {specs.scope}.")
    for campaign_type, by_asset in specs.sheet.specs.items():
        _heading(document, campaign_type, 2)
        _table(
            document,
            ["Asset", "Max chars", "Count", "Ratio", "Max bytes", "Source"],
            [
                [
                    asset_type,
                    spec.max_chars if spec.max_chars else "—",
                    f"{spec.min_count if spec.min_count is not None else '—'}"
                    f"–{spec.max_count if spec.max_count is not None else '—'}",
                    spec.ratio or "",
                    spec.max_bytes if spec.max_bytes else "—",
                    spec.source,
                ]
                for asset_type, spec in by_asset.items()
            ],
        )
    if specs.launch_minimums:
        _heading(document, "The launch minimum", 2)
        _table(
            document,
            ["Campaign type", "Required", "Blocks launch"],
            [
                [
                    entry.campaign_type,
                    f"{len(entry.required_assets)} asset type(s)",
                    "Yes" if entry.blocking_for_launch else "No",
                ]
                for entry in specs.launch_minimums
            ],
        )


def _governance(
    document: DocxDocument, guideline: ContentGuideline, context: dict[str, Any]
) -> None:
    governance = context["governance"]
    _heading(document, "Governance and sign-off", 1)
    _table(
        document,
        ["Role", "Owner"],
        [
            ["Brand owner", str(governance.owners.brand_owner_id or "unassigned")],
            ["Legal owner", str(governance.owners.legal_owner_id or "unassigned")],
            ["Performance owner", str(governance.owners.performance_owner_id or "unassigned")],
        ],
    )
    if governance.owners_rationale:
        document.add_paragraph(governance.owners_rationale)

    if governance.review_triggers:
        _heading(document, "Route to a human before it ships", 2)
        _table(
            document,
            ["Trigger", "Kind", "Reviewer", "Severity"],
            [
                [t.pattern, t.pattern_kind, t.reviewer_role, t.severity]
                for t in governance.review_triggers
            ],
        )

    # §14's sign-off page: G5, G6, H1 and H2 with decider, method and timestamp.
    _heading(document, "The sign-off page", 2)
    rows: list[list[Any]] = [
        [
            f"{decision.gate_key} — {decision.name or decision.node_id}",
            decision.status,
            str(decision.decided_by or ""),
            fmt_datetime(decision.decided_at),
        ]
        for decision in guideline.decisions
    ]
    rows.extend(
        [
            "H1 — claims signature",
            f"{signature.claim_count} claims" + (", voided" if signature.voided_at else ""),
            str(signature.signer_id),
            fmt_datetime(signature.signed_at),
        ]
        for signature in guideline.signatures
    )
    rows.extend(
        [
            f"{task.task_key} — person task",
            f"{task.status} (blocks {task.blocking_for})",
            str(task.assignee_id or "unassigned"),
            "",
        ]
        for task in guideline.human_tasks
    )
    _table(document, ["Decision", "Outcome", "Decider", "When"], rows)

    if guideline.open_dependencies:
        _heading(document, "Still outstanding", 2)
        _table(
            document,
            ["What", "Owner", "Blocks"],
            [[item.task, item.owner, item.blocking_for] for item in guideline.open_dependencies],
        )


def _rules(document: DocxDocument, context: dict[str, Any]) -> None:
    _heading(document, "Every rule", 1)
    document.add_paragraph(
        f"{context['rule_count']} rule(s), compiled into the ruleset Stage 04 enforces."
    )
    for category, rules in context["rules_by_category"]:
        _heading(document, f"{category.replace('_', ' ').title()} ({len(rules)})", 2)
        _table(
            document,
            ["Rule", "Severity", "What it says", "Authority"],
            [
                [
                    rule.rule_id,
                    rule.severity,
                    rule.message,
                    f"{rule.authority.source}: {rule.authority.reference}",
                ]
                for rule in rules[:RULE_ROWS]
            ],
        )
        if len(rules) > RULE_ROWS:
            paragraph = _note(
                document,
                f"Showing the first {RULE_ROWS} of {len(rules)}. The full set is in the "
                "ruleset JSON export.",
            )
            for run in paragraph.runs:
                run.font.color.rgb = MUTED
