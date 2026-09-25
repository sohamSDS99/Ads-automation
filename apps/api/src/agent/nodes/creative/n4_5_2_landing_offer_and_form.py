"""4.5.2 `landing_offer_and_form` — offer above the fold, the minimal form, the patch (PRD §11 4.5).

For every page 4.5.1 audited, judged from the `landing_render` / `landing_dom`
Evidence 4.5.1 stored — the page is never rendered twice:

1. **Offer above the fold** — the brief's offer (its first rendered
   `OfferBinding` value, law 35) as a normalised exact phrase in a text node
   whose box top is above the fold, per device. No offer in the brief is
   nothing to look for.
2. **Field → lead signal** — CLASSIFY labels each field of the form a visitor
   fills in with one of `lead_definition.required_signals`, a contact channel,
   consent, privacy or `none`: one required enum per field, so every field is
   labelled exactly once and none can be invented.
3. **The minimal set, in code** — `required_signals` ∪ consent/privacy ∪ the
   routing contact field (`creative/landing_audit.minimal_set`). The model
   labels; it never keeps a field.
4. **The patch and the verdict** — the `LandingPagePatch` (4.5.1's linted H1,
   the offer block, the fields to remove, escaped markup) and the verdict. Only
   `unreachable` and a missing offer above the fold block launch (Q10).

Law 41: the patch is a proposal for the site owner. Each `landing_page_audit`
row 4.5.1 wrote is set absolutely from this node's output, so a retry
overwrites rather than compounds.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Literal

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, create_model

from agent.creative import landing_audit as audit
from agent.db.models import Evidence, LandingAuditVerdict, LandingPageAudit, RunStage
from agent.llm.router import TaskClass
from agent.nodes.base import NodeContractError, NodeSpec, RunContext
from agent.preview import landing
from agent.preview.landing import LandingRender
from agent.schemas.creative_brief import CreativeBrief
from agent.schemas.landing import (
    CONSENT,
    CONTACT_EMAIL,
    CONTACT_PHONE,
    NO_SIGNAL,
    PRIVACY,
    FormAudit,
    FormField,
    LandingAuditPage,
    LandingMessageMatchOutput,
    LandingOfferAndFormOutput,
    LandingPageMatch,
)

NODE_ID = "4.5.2"
#: Fields labelled per CLASSIFY call, as 4.2.3 batches its pairs.
CLASSIFY_BATCH = 50

SYSTEM = """You label the fields of a lead form on a landing page. For each field, choose
the one lead signal it collects:

- one of REQUIRED_SIGNALS, when the field asks for exactly that;
- contact_email or contact_phone, when it asks for the address a lead is reached at;
- consent, when it asks the visitor to agree to be contacted or to terms;
- privacy, when it asks the visitor to acknowledge the privacy policy;
- none, for anything else.

Label every field. Judge only by the field's label, name and type.
"""


class _Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LandingOfferAndFormNode:
    spec = NodeSpec(
        id=NODE_ID,
        name="landing_offer_and_form",
        stage="4.5",
        run_stage=RunStage.CREATIVE,
        depends_on=("4.5.1",),
        task_class=TaskClass.CLASSIFY,
        input_model=LandingMessageMatchOutput,
        output_model=LandingOfferAndFormOutput,
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        """The Evidence 4.5.1 cited: the renders are judged from what was stored."""
        matched = LandingMessageMatchOutput.model_validate(ctx.output_of("4.5.1"))
        cited = {item for page in matched.pages for item in page.evidence_ids}
        if not cited:
            return []
        return list(
            (
                await ctx.db.execute(
                    sa.select(Evidence).where(
                        Evidence.id.in_(cited), Evidence.project_id == ctx.project.id
                    )
                )
            )
            .scalars()
            .all()
        )

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        creative = ctx.require_creative()
        threshold = creative.constants.landing.message_match_min.value
        matched = LandingMessageMatchOutput.model_validate(ctx.output_of("4.5.1"))
        brief = CreativeBrief.model_validate(ctx.output_of("4.1.1"))
        phrase = audit.offer_phrase(brief.offer)
        lead = creative.input.lead_definition
        required = list(lead.required_signals) if lead is not None else []
        renders = landing.from_evidence(
            (row.kind, row.payload)
            for row in ev
            if row.kind in (landing.RENDER_KIND, landing.DOM_KIND)
        )

        judged: list[LandingAuditPage] = []
        for page in matched.pages:
            judged.append(
                await _judge(
                    ctx,
                    page,
                    renders.get(page.url),
                    phrase=phrase,
                    required=required,
                    threshold=threshold,
                )
            )
        await _write(ctx, judged)
        return LandingOfferAndFormOutput(pages=judged)


async def _judge(
    ctx: RunContext,
    page: LandingPageMatch,
    render: LandingRender | None,
    *,
    phrase: str | None,
    required: Sequence[str],
    threshold: float,
) -> LandingAuditPage:
    if not page.reachable:
        return LandingAuditPage(
            audit_id=page.audit_id,
            url=page.url,
            verdict="unreachable",
            reasons=[_unreachable(page, render)],
            evidence_ids=list(page.evidence_ids),
        )
    if render is None:
        raise NodeContractError(
            f"4.5.1 audited {page.url} as reachable but its landing_render / landing_dom "
            "Evidence for both devices is not among the rows it cited"
        )
    offer = audit.offer_above_fold(render, phrase) if phrase else []
    form = await _form(ctx, render, required)
    h1 = (
        page.proposed_h1.text
        if page.message_match.verdict == "fail" and page.proposed_h1 is not None
        else None
    )
    verdict, reasons = audit.verdict(
        reachable=True,
        match=page.message_match.verdict,
        match_score=page.message_match.score,
        threshold=threshold,
        offer=offer,
        form=form,
    )
    if page.message_match.verdict == "fail" and h1 is None and page.proposed_h1_note:
        reasons.append(page.proposed_h1_note)
    await ctx.progress(f"{page.url}: {verdict}")
    return LandingAuditPage(
        audit_id=page.audit_id,
        url=page.url,
        offer_above_fold=offer,
        form=form,
        patch=audit.build_patch(url=page.url, h1=h1, offer=offer, form=form),
        verdict=verdict,
        reasons=reasons,
        evidence_ids=list(page.evidence_ids),
    )


def _unreachable(page: LandingPageMatch, render: LandingRender | None) -> str:
    if render is None:
        return f"{page.url} did not render on both devices, so nothing on it could be checked."
    parts = []
    for device in landing.DEVICES:
        rendered = render.device(device)
        if rendered.ok:
            continue
        why = (
            f"answered HTTP {rendered.http_status}"
            if rendered.reached and rendered.http_status is not None and not rendered.error
            else f"did not answer ({rendered.error})"
        )
        parts.append(f"on {device} it {why}")
    return f"{page.url} cannot be audited: {'; '.join(parts)}."


async def _form(
    ctx: RunContext, render: LandingRender, required: Sequence[str]
) -> FormAudit | None:
    """The desktop render's lead form, labelled by CLASSIFY and reduced in code."""
    form_index, fields = audit.form_fields(render.desktop)
    if not fields:
        return None
    labels = await label_fields(ctx, fields, audit.signal_vocabulary(required))
    return audit.minimal_set(form_index, audit.with_signals(fields, labels), required)


async def label_fields(
    ctx: RunContext, fields: Sequence[FormField], vocabulary: Sequence[str]
) -> dict[str, str]:
    """CLASSIFY every field, `CLASSIFY_BATCH` per call, one required enum each."""
    signal: Any = Literal.__getitem__(tuple(vocabulary))
    required = [item for item in vocabulary if item not in audit.RESERVED_SIGNALS]
    labels: dict[str, str] = {}
    for start in range(0, len(fields), CLASSIFY_BATCH):
        batch = fields[start : start + CLASSIFY_BATCH]
        keys = [f"f{index:02d}" for index in range(len(batch))]
        schema = create_model(
            "FormFieldSignalsDraft",
            __base__=_Draft,
            **{key: (signal, ...) for key in keys},  # type: ignore[call-overload]
        )
        shown = {
            key: {"name": field.name, "label": field.label, "type": field.type}
            for key, field in zip(keys, batch, strict=True)
        }
        answer = await ctx.complete(
            schema,
            system=SYSTEM,
            user="REQUIRED_SIGNALS:\n"
            + json.dumps(required, ensure_ascii=False)
            + "\n\nOTHER LABELS:\n"
            + json.dumps([CONTACT_EMAIL, CONTACT_PHONE, CONSENT, PRIVACY, NO_SIGNAL])
            + "\n\nFIELDS:\n"
            + json.dumps(shown, ensure_ascii=False, indent=1),
            task_class=TaskClass.CLASSIFY,
        )
        for key, field in zip(keys, batch, strict=True):
            labels[field.name] = str(getattr(answer, key))
    return labels


async def _write(ctx: RunContext, judged: Sequence[LandingAuditPage]) -> None:
    """Each row 4.5.1 wrote, set to this page's offer, form, patch and verdict."""
    ids = [page.audit_id for page in judged]
    rows = {
        row.id: row
        for row in (
            await ctx.db.execute(
                sa.select(LandingPageAudit).where(
                    LandingPageAudit.creative_run_id == ctx.run.id, LandingPageAudit.id.in_(ids)
                )
            )
        )
        .scalars()
        .all()
    }
    missing = sorted(str(item) for item in set(ids) - set(rows))
    if missing:
        raise NodeContractError(f"4.5.1 wrote no landing_page_audit row for: {', '.join(missing)}")
    for page in judged:
        row = rows[page.audit_id]
        metrics = {
            key: value
            for key, value in (row.metrics or {}).items()
            if key not in ("offer_above_fold", "form", "reasons")
        }
        row.metrics = {
            **metrics,
            "offer_above_fold": [item.model_dump(mode="json") for item in page.offer_above_fold],
            "form": page.form.model_dump(mode="json") if page.form is not None else None,
            "reasons": list(page.reasons),
        }
        row.patch = page.patch.model_dump(mode="json") if page.patch is not None else None
        row.verdict = LandingAuditVerdict(page.verdict)
    await ctx.db.flush()


LANDING_OFFER_AND_FORM = LandingOfferAndFormNode()
