"""Plan markdown — the source of truth for every other plan format (§12.1, §14).

§12.1: "`markdown` is rendered from the object by a deterministic Jinja2
template. **The LLM does not write the final markdown** — it fills the object.
PDF, DOCX and JSON therefore cannot diverge."

Deterministic is a testable word and this module treats it as one. Given the
same `CampaignPlan` it returns the same bytes: no clock is read, nothing is
ordered by a hash, and the one ordering decision that could wobble — the test
backlog — is a total sort on `(rank, id)`. `tests/test_plan_markdown.py`
renders a plan twice and compares.

The rendering is stored on `campaign_plan.markdown` at synthesis time and
served from there, so a later template change cannot retroactively alter a
plan somebody has already signed. On a frozen plan the database refuses the
rewrite outright (law 17).
"""

from __future__ import annotations

from agent.export.markdown import normalise
from agent.export.plan_contract import CampaignPlan
from agent.export.plan_view import build_context
from agent.export.templating import markdown_env

TEMPLATE_NAME = "plan.md.j2"


def render_plan_markdown(
    plan: CampaignPlan, *, project_name: str | None = None, frozen_by_name: str = ""
) -> str:
    """The plan as markdown. Same input, same bytes, every time."""
    env = markdown_env()
    template = env.get_template(TEMPLATE_NAME)
    rendered = template.render(
        **build_context(plan, project_name=project_name, frozen_by_name=frozen_by_name)
    )
    return normalise(rendered)
