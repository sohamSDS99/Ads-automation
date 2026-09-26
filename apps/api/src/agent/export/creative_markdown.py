"""The creative book as markdown — the source of truth for the PDF (Stage 04 PRD §14).

`creative_book.md.jinja` over `creative_view.build_book`, the same context
`creative_book_pdf` prints, so the two cannot drift apart. Same package, same
bytes: no clock is read and nothing is ordered by a hash. Images are named,
not embedded — the files ship with the package and in the Editor ZIP.
"""

from __future__ import annotations

from agent.export.creative_sources import CreativeExportSources
from agent.export.creative_view import build_book
from agent.export.markdown import normalise
from agent.export.templating import markdown_env

TEMPLATE_NAME = "creative_book.md.jinja"


def render_creative_markdown(sources: CreativeExportSources) -> str:
    template = markdown_env().get_template(TEMPLATE_NAME)
    return normalise(template.render(**build_book(sources, images=False)))
