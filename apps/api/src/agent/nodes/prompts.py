"""Shared prompt scaffolding for the research nodes.

Three things every node prompt has to get right, written once here rather than
eight times:

* **Grounding.** PRD §18 law 1 — the model is told, in its own instructions,
  that it is reading evidence rather than recalling facts, and that a claim it
  cannot attach an `evidence_id` to does not belong in the answer.
* **Citations that survive validation.** The executor fails a node whose output
  cites an id it did not gather, so the ids have to reach the model verbatim and
  come back verbatim. Evidence is rendered with its id on its own line, and the
  instruction is to copy, never to construct.
* **Empty is an answer.** PRD §16: "never hallucinate history". When a source is
  missing the prompt says so explicitly and asks for empty arrays, because a
  model given no data and no permission to say "none" will fill the gap.
"""

from __future__ import annotations

import json
from typing import Any

from agent.db.models import Evidence, Project
from agent.nodes.gather import Gathered

#: Appended to every node's system prompt.
GROUNDING_RULES = """
You are a paid-search research analyst working strictly from supplied evidence.

Rules, in order of importance:
1. Every factual claim must come from the EVIDENCE or COMPUTED sections below.
   You have no other source. Do not use general knowledge about this company,
   its market or its competitors.
2. Cite with `evidence_ids`: copy the exact `id:` values shown. Never invent,
   shorten or reformat an id. A claim you cannot cite must be left out.
3. If a section is empty or marked unavailable, return an empty array for what
   it would have supported. Saying "no evidence" is a correct answer; inferring
   plausible numbers is not.
4. Do not restate, recompute or round any number given under COMPUTED. Those
   figures are final and are merged into the result without passing through you.
5. Write plain prose in text fields — no markdown, no bullet characters.
""".strip()

#: Rows of raw evidence rendered into one prompt. Large enough to be
#: representative, small enough to leave room for the computed tables that carry
#: the actual arithmetic.
SAMPLE_ROWS = 40

#: Characters of one rendered row. A crawled page body would otherwise crowd out
#: every other row in the section.
MAX_ROW_CHARS = 600

#: Characters of uploaded business-context documents one node may read. Prose
#: needs more room per row than a metrics row does, so `documents_block` has its
#: own budget rather than borrowing `MAX_ROW_CHARS` — but it is a budget, not an
#: absence of one: a 400-page brand book must not become the whole prompt.
MAX_DOCUMENT_CHARS = 12_000

#: The heading `documents_block` writes and `cite_from` names. One constant,
#: because a citation instruction that points at a section title that does not
#: exist is how a node ends up with an empty `evidence_ids` and no explanation.
DOCUMENTS_LABEL = "EVIDENCE — business context documents"


def system_prompt(role: str) -> str:
    """One node's role, followed by the rules every node shares."""
    return f"{role.strip()}\n\n{GROUNDING_RULES}"


def project_block(project: Project) -> str:
    """What the run is about. Configuration, not evidence — never cited."""
    return "\n".join(
        (
            "PROJECT",
            f"  name: {project.name}",
            f"  domain: {project.domain}",
            f"  markets: {_compact(project.markets)}",
            f"  product_context: {_compact(project.product_context)}",
        )
    )


def evidence_block(
    gathered: Gathered,
    kind: str,
    *,
    title: str | None = None,
    limit: int = SAMPLE_ROWS,
) -> str:
    """Raw rows of one kind, each with the id the model must copy to cite it."""
    rows = gathered.of(kind)
    heading = f"EVIDENCE — {title or kind}"
    if not rows:
        return f"{heading}\n  (no rows available)"
    shown = rows[:limit]
    lines = [f"{heading} ({len(shown)} of {len(rows)} rows)"]
    lines.extend(_render(row) for row in shown)
    if len(rows) > len(shown):
        lines.append(f"  … {len(rows) - len(shown)} further rows not shown")
    return "\n".join(lines)


def documents_block(
    gathered: Gathered,
    kind: str,
    *,
    budget: int = MAX_DOCUMENT_CHARS,
) -> str:
    """The business context someone uploaded, in reading order, with its ids.

    Not `evidence_block`. Three differences, each of which matters for prose:

    * **Order.** `gather` returns rows newest-first, which for the passages of
      one document is no order at all — page 9 above page 2 reads as a
      different document. These are sorted back into the order they were
      written in.
    * **Room.** A passage is a paragraph, and `MAX_ROW_CHARS` would cut most of
      them in half. The budget here is per block rather than per row.
    * **Shape.** The text is rendered as text, not as a JSON payload. A model
      quoting from `{"text": "…"}` quotes the braces too.

    Returns the empty string when nothing was uploaded, which `compose` drops —
    a heading with nothing under it invites a model to explain the absence.
    """
    rows = gathered.of(kind)
    if not rows:
        return ""

    ordered = sorted(
        rows,
        key=lambda row: (
            str(row.payload.get("filename", "")),
            int(row.payload.get("passage", 0) or 0),
        ),
    )
    lines = [f"{DOCUMENTS_LABEL} (uploaded by this workspace)"]
    spent = 0
    shown = 0
    for row in ordered:
        text = str(row.payload.get("text", "")).strip()
        if not text:
            continue
        if spent + len(text) > budget:
            break
        filename = row.payload.get("filename", "document")
        section = row.payload.get("section", "")
        where = f"{filename} — {section}" if section else str(filename)
        lines.append(f"  - id: {row.id}\n    [{where}]\n    {text}")
        spent += len(text)
        shown += 1
    if shown < len(ordered):
        lines.append(
            f"  … {len(ordered) - shown} further passages were not included in this prompt."
        )
    return "\n".join(lines)


def computed_block(title: str, rows: Any) -> str:
    """A table this node already computed in pandas.

    Rendered as JSON rather than prose so the model reads it as data it may
    reference by key, and told plainly that the numbers are not its to restate.
    `rows` is any JSON-serialisable shape — a table, one record, or a bare list
    of names the node wants echoed back.
    """
    body = json.dumps(rows, default=str, indent=2, sort_keys=False)
    return f"COMPUTED — {title}\n{body}"


def coverage_block(gathered: Gathered) -> str:
    """What could not be read, named rather than left as an absence."""
    if not gathered.missing and not gathered.degraded:
        return ""
    lines = ["COVERAGE"]
    for kind in gathered.missing:
        reason = gathered.degraded.get(kind, "no data has been collected for this project")
        lines.append(f"  {kind}: UNAVAILABLE — {reason}")
    for kind, reason in gathered.degraded.items():
        if kind not in gathered.missing:
            lines.append(f"  {kind}: PARTIAL — {reason}")
    lines.append(
        "  Return empty arrays for anything these sources would have supported, "
        "and do not substitute assumptions for them."
    )
    return "\n".join(lines)


def compose(*blocks: str) -> str:
    """Join the blocks a node built, dropping the ones that turned out empty."""
    return "\n\n".join(block.strip() for block in blocks if block and block.strip())


def documents_cite(block: str) -> str:
    """`DOCUMENTS_LABEL` if that block has content, otherwise nothing.

    Lets a node write `cite_from(own_label, documents_cite(docs))` without a
    conditional: with no documents uploaded the label drops out, and the model
    is never told to cite a section it cannot see.
    """
    return DOCUMENTS_LABEL if block.strip() else ""


def cite_from(*labels: str) -> str:
    """The instruction that names where this node's ids come from.

    Empty labels are dropped, so an optional section can be passed
    unconditionally.
    """
    joined = ", ".join(label for label in labels if label and label.strip())
    return (
        f"CITATIONS\n  Take every `evidence_ids` value from {joined}. "
        "Copy the ids exactly as written; any other value is rejected."
    )


def _render(row: Evidence) -> str:
    payload = _compact(row.payload)
    if len(payload) > MAX_ROW_CHARS:
        payload = payload[:MAX_ROW_CHARS] + "…"
    return f"  - id: {row.id}\n    {payload}"


def _compact(value: Any) -> str:
    return json.dumps(value, default=str, separators=(", ", ": "), sort_keys=True)
