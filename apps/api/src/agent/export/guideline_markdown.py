"""Guideline markdown — the source of truth for every other format (§12.1, §14).

§14 names MD as the source PDF and DOCX are rendered from, and §12.1 applies the
same rule Stage 02 does: **the LLM does not write the final markdown** — it
fills the object. Six export formats therefore cannot diverge.

Deterministic is a testable word and this module treats it as one. Given the
same `ContentGuideline` it returns the same bytes: no clock is read, nothing is
ordered by a hash, and the one ordering decision that could wobble — rules
grouped by category — is a total sort in `guideline_view._by_category`.
§14 acceptance 2 requires two exports of a published version to be
byte-identical, and `tests/test_guideline_exports.py` renders twice and
compares.

The rendering is stored on `content_guideline.markdown` at synthesis time and
served from there, so a later template change cannot retroactively alter a
rulebook somebody has already signed. On a published guideline the database
refuses the rewrite outright (migration 0016's trigger, law 26).
"""

from __future__ import annotations

from agent.export.guideline_contract import ContentGuideline
from agent.export.guideline_view import build_context
from agent.export.markdown import normalise
from agent.export.templating import markdown_env

TEMPLATE_NAME = "guideline.md.j2"


def render_guideline_markdown(
    guideline: ContentGuideline,
    *,
    project_name: str | None = None,
    published_by_name: str = "",
    ruleset_version: str = "",
) -> str:
    """The rulebook as markdown. Same input, same bytes, every time."""
    env = markdown_env()
    template = env.get_template(TEMPLATE_NAME)
    rendered = template.render(
        **build_context(
            guideline,
            project_name=project_name,
            published_by_name=published_by_name,
            ruleset_version=ruleset_version,
        )
    )
    return normalise(rendered)
