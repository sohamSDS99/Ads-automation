"""4.3.3 `lead_form_asset` — the lead form and its field-count trade-off (PRD §11 4.3.3, §13).

**`not_required` unless a campaign objective is `lead_gen`** — read from the
frozen plan's objectives, for the campaigns the approved brief covers.
Otherwise, per such campaign:

1. **The spec comes from the pin** — `lead_form` (`max_chars`, `max_count`)
   for the campaign's type. Missing is a recorded `spec_missing` gap; when no
   campaign has it the node is `spec_missing` and asks no model.
2. **The form asks the routing contact and the lead definition's required
   signals, nothing else** (§13). A required signal in a GDPR Art. 9 category
   (the Stage 02 blocklist, `planning/sensitive_categories.py`) is never asked
   and is recorded. How many signals is `leadform.field_tradeoff_v1`'s
   answer, computed in `gather()` over the CRM's historical junk-lead rate and
   anchored on 4.5.2's audited form, and recorded as `PlanCalc` + `derived`
   Evidence the output cites. Without that history (no CRM rows, no audited
   form, no lead definition) there is no trade-off to weigh, the gap says
   which, and the form asks every required signal — the definition itself.
3. **`privacy_policy_url` must resolve 2xx on-domain** — one of the project's
   own crawled pages, checked by `preview/urlcheck.py`. None resolves: no form.
4. **COPYWRITE words it** — the headline, description and call to action
   (from `extras.lead_form_cta_types`), and for each chosen signal a question
   of a type from `extras.lead_form_question_types` or `custom`. Code decides
   what each question qualifies. A question, option or type the model words
   into an Art. 9 category withholds the form whole.
5. **Every line is linted at creation** as `lead_form` (law 33); an
   unlicensed claim span withholds the form (law 34), any other failure
   leaves it `draft`.

Nothing is written until every campaign has been built; an attempt that
follows a failed one first clears what that attempt wrote.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Literal
from urllib.parse import urlsplit

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, create_model

from agent.calc.derived import DerivedWriter
from agent.calc.leadform import field_tradeoff_v1
from agent.calc.registry import CalcError
from agent.creative import brief as briefs
from agent.creative import exceptions
from agent.creative.landing_audit import routing_contact
from agent.db.models import (
    CreativeAssetKind,
    CreativeAssetStatus,
    Evidence,
    EvidenceSource,
    RunStage,
)
from agent.evidence.store import EvidenceStore
from agent.export.plan_contract import Objectives
from agent.llm.router import TaskClass
from agent.nodes.base import NodeSpec, RunContext
from agent.nodes.creative._ad_groups import (
    Slot,
    brief_section,
    proof_points,
    render_prompt,
    slots,
)
from agent.nodes.creative._text_assets import clear_earlier_attempts, lint_ref, text_asset
from agent.nodes.creative.n4_2_2_claim_bound_descriptions import screen
from agent.nodes.creative.n4_3_1_sitelinks_callouts_snippets import WEB_PAGE_KIND
from agent.planning.crm import CRM_LOST, CRM_WON
from agent.planning.sensitive_categories import article_9_category
from agent.preview import urlcheck
from agent.schemas.creative_brief import CreativeBrief
from agent.schemas.extras import (
    CUSTOM_QUESTION,
    CampaignLeadForm,
    ExtraGap,
    GapReason,
    LeadFormOutput,
    LeadFormTradeoff,
    UrlCheckRef,
)
from agent.schemas.guardrails import AssetSpec, ClaimRef, LintTarget
from agent.schemas.landing import (
    CONTACT_EMAIL,
    CONTACT_PHONE,
    FormAudit,
    LandingMessageMatchOutput,
    LandingOfferAndFormOutput,
)

NODE_ID = "4.3.3"
LEAD_GEN: Final = "lead_gen"
NEEDS: Final = ("max_chars", "max_count")
#: 4.5.2's routing-contact signals, as Google's predefined question types.
CONTACT_TYPES: Final[Mapping[str, str]] = {CONTACT_EMAIL: "EMAIL", CONTACT_PHONE: "PHONE_NUMBER"}
#: The contact a form with no audited one routes by — 4.5.2's first choice.
DEFAULT_CONTACT: Final = "EMAIL"
PRIVACY: Final = "privacy"

SYSTEM = """You write one Google Ads lead form: what a searcher sees and answers
before sending their details.

Rules, all of them hard:
- The headline is at most {max_chars} characters; so is the description and every
  question's text. Count characters, spaces included.
- For each entry in QUESTIONS write exactly one question that finds out that
  signal. Prefer a predefined question type that asks exactly it, with no text;
  otherwise use "custom" with the question's text and, where the answers are a
  short fixed list, its options.
- Ask about the business and the person's role only. Never ask about health,
  ethnicity, religion or beliefs, politics, union membership, sex life or
  orientation, genetic or biometric data.
- State no fact about the product that PROOF POINTS does not state. No
  exclamation marks. Never use a word from NEVER.
"""


# ---------------------------------------------------------------------------
# the parts that decide — pure, and tested on their own
# ---------------------------------------------------------------------------


def lead_gen_campaigns(objectives: Objectives, covered: Sequence[str]) -> list[str]:
    """The covered campaigns whose frozen objective is `lead_gen`, in brief order."""
    lead_gen = {
        item.campaign_ref
        for item in objectives.campaign_objectives
        if item.objective.strip().lower() == LEAD_GEN
    }
    return [ref for ref in dict.fromkeys(covered) if ref in lead_gen]


def privacy_candidates(found: Iterable[tuple[str, str | None]], *, domain: str) -> list[str]:
    """The project's own pages that are its privacy policy, the shortest path first."""
    pages: dict[str, str] = {}
    for url, title in found:
        url = url.strip()
        if not urlcheck.on_domain(url, domain):
            continue
        if PRIVACY in urlsplit(url).path.casefold() or PRIVACY in (title or "").casefold():
            pages.setdefault(urlcheck.canonical(url), url)
    return sorted(pages.values(), key=lambda url: (len(urlsplit(url).path), url))


def screen_signals(signals: Sequence[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """`(askable, [(signal, Art. 9 category)])` — a sensitive signal is never asked."""
    askable: list[str] = []
    blocked: list[tuple[str, str]] = []
    for signal in dict.fromkeys(item.strip() for item in signals if item.strip()):
        category = article_9_category(signal)
        if category is None:
            askable.append(signal)
        else:
            blocked.append((signal, category))
    return askable, blocked


def form_history(audit: FormAudit, signals: Sequence[str]) -> tuple[int, int, str]:
    """`(fields, signals it asked, contact type)` of the audited landing form."""
    carried = {item.mapped_signal for item in audit.fields}
    contact = routing_contact(audit.fields)
    contact_type = (
        CONTACT_TYPES.get(contact.mapped_signal or "", DEFAULT_CONTACT)
        if contact
        else DEFAULT_CONTACT
    )
    return len(audit.fields), sum(1 for signal in signals if signal in carried), contact_type


def crm_aggregates(
    won: Sequence[Mapping[str, Any]], lost: Sequence[Mapping[str, Any]]
) -> tuple[int, dict[str, int]]:
    """Won rows, and lost rows by close reason — aggregates, never a row (Stage 02 law 20)."""
    reasons = Counter(str(row.get("close_reason") or "").strip() for row in lost)
    return len(won), dict(sorted(reasons.items()))


class _Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")


def draft_model(
    keys: Sequence[str], *, question_types: Sequence[str], cta_types: Sequence[str]
) -> type[BaseModel]:
    """Headline, description, CTA, and one question per chosen signal."""
    kind: Any = Literal[(*question_types, CUSTOM_QUESTION)]
    question = create_model(
        "LeadFormQuestionDraft",
        __base__=_Draft,
        type=(kind, ...),
        text=(str | None, ...),
        options=(list[str], ...),
    )
    per_signal: dict[str, Any] = {key: (question, ...) for key in keys}
    questions = create_model("LeadFormQuestionsDraft", __base__=_Draft, **per_signal)
    cta: Any = Literal[tuple(cta_types)]
    return create_model(
        "LeadFormDraft",
        __base__=_Draft,
        headline=(str, Field(min_length=1)),
        description=(str, Field(min_length=1)),
        cta=(cta, ...),
        questions=(questions, ...),
    )


def assemble_questions(
    contact_type: str, chosen: Sequence[str], draft: Any
) -> list[dict[str, Any]]:
    """The routing contact, then one question per chosen signal — what code, not the model, asks."""
    questions: list[dict[str, Any]] = [
        {"type": contact_type, "text": None, "options": [], "qualifies_signal": None}
    ]
    for index, signal in enumerate(chosen, start=1):
        item = getattr(draft.questions, f"signal_{index}")
        text = (item.text or "").strip() or None
        questions.append(
            {
                "type": str(item.type),
                "text": text,
                "options": [option.strip() for option in item.options if option.strip()],
                "qualifies_signal": signal,
            }
        )
    return questions


def screen_questions(questions: Sequence[Mapping[str, Any]]) -> tuple[str, str] | None:
    """`(category, what said it)` of the first question targeting an Art. 9 category."""
    for question in questions:
        said = [str(question["type"]), question.get("text") or "", *question.get("options", [])]
        for text in said:
            category = article_9_category(text)
            if category is not None:
                return category, text
    return None


# ---------------------------------------------------------------------------
# the node
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Prepared:
    """What `gather()` settled for one campaign, before any model is asked."""

    slot: Slot
    spec: AssetSpec | None
    signals: list[str]
    contact_type: str
    gaps: list[ExtraGap] = field(default_factory=list)
    tradeoff: dict[str, Any] | None = None
    calc_evidence_id: uuid.UUID | None = None


class LeadFormAssetNode:
    spec = NodeSpec(
        id=NODE_ID,
        name="lead_form_asset",
        stage="4.3",
        run_stage=RunStage.CREATIVE,
        depends_on=("4.5.2",),
        task_class=TaskClass.COPYWRITE,
        input_model=LandingOfferAndFormOutput,
        output_model=LeadFormOutput,
        lint_required=True,
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        """The CRM aggregates, the audited form and the trade-off, computed and made citable."""
        prepared = _prepare(ctx)
        ctx.scratch[NODE_ID] = prepared
        if not prepared:
            return []
        rows = await _evidence(ctx, (CRM_WON, CRM_LOST), EvidenceSource.CSV)
        won, lost = crm_aggregates(
            [row.payload or {} for row in rows if row.kind == CRM_WON],
            [row.payload or {} for row in rows if row.kind == CRM_LOST],
        )
        creative = ctx.require_creative()
        writer = DerivedWriter(
            ctx.db,
            store=EvidenceStore(ctx.db, ctx.run.workspace_id),
            project_id=ctx.project.id,
            plan_run_id=ctx.run.id,
        )
        disqualifiers = (
            list(creative.input.lead_definition.disqualifiers)
            if creative.input.lead_definition
            else []
        )
        audits = _audits(ctx)
        calc_ids: list[uuid.UUID] = []
        for item in prepared:
            if item.spec is None or not _can_weigh(item, won, lost):
                continue
            audit = audits.get(item.slot.brief.campaign_ref)
            if audit is None:
                item.gaps.append(
                    _gap(
                        item.slot,
                        "no_audited_form",
                        "4.5.2 audited no form on this campaign's landing page to anchor "
                        "the CRM history on",
                    )
                )
                continue
            fields_n, asked, item.contact_type = form_history(audit, item.signals)
            try:
                result = field_tradeoff_v1(
                    won=won,
                    lost_reasons=lost,
                    disqualifiers=disqualifiers,
                    signals=item.signals,
                    history_fields_n=fields_n,
                    history_signals_n=asked,
                    retention_per_field=float(
                        creative.constants.extras.lead_form_field_retention.value
                    ),
                    constants_version=creative.constants.version,
                )
            except CalcError as exc:
                item.gaps.append(_gap(item.slot, "no_crm_history", str(exc)))
                continue
            item.calc_evidence_id = await writer.record(result, node_id=NODE_ID)
            item.tradeoff = result.result
            calc_ids.append(item.calc_evidence_id)
        derived = (
            list(
                (await ctx.db.execute(sa.select(Evidence).where(Evidence.id.in_(calc_ids))))
                .scalars()
                .all()
            )
            if calc_ids
            else []
        )
        pages = await _evidence(ctx, (WEB_PAGE_KIND,), EvidenceSource.WEB)
        return [*derived, *pages]

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        await clear_earlier_attempts(ctx, NODE_ID)
        prepared: list[_Prepared] = ctx.scratch.get(NODE_ID) or []
        if not prepared:
            return LeadFormOutput(
                status="not_required",
                why="no campaign in this run's scope has a lead_gen objective in the frozen plan",
            )
        creative = ctx.require_creative()
        if all(item.spec is None for item in prepared):
            return LeadFormOutput(
                status="spec_missing",
                why=(
                    f"ruleset {creative.linter.ruleset.ruleset_version} has no lead_form spec for "
                    "any lead_gen campaign in scope; Stage 04 never guesses a Google limit (§9.5)"
                ),
                campaigns=[_campaign(item) for item in prepared],
            )
        brief = CreativeBrief.model_validate(ctx.output_of("4.1.1"))
        # The run's start, not the wall clock, as in 4.2.1.
        now = ctx.run.started_at or datetime.now(UTC)
        licensed = briefs.licensed_claims(creative.linter.ruleset, now)
        privacy = privacy_candidates(
            (
                (str((row.payload or {}).get("url") or ""), (row.payload or {}).get("title"))
                for row in ev
                if row.source == EvidenceSource.WEB
            ),
            domain=ctx.project.domain,
        )
        campaigns = [
            await _write(ctx, brief, item, privacy, licensed, now)
            if item.spec is not None
            else _campaign(item)
            for item in prepared
        ]
        await ctx.db.flush()
        written = sum(1 for item in campaigns if item.form is not None)
        return LeadFormOutput(
            status="required",
            why=f"{len(campaigns)} lead_gen campaign(s) in scope; {written} lead form(s) written",
            campaigns=campaigns,
        )


def _gap(slot: Slot, reason: GapReason, detail: str) -> ExtraGap:
    return ExtraGap(
        campaign_ref=slot.brief.campaign_ref, asset_type="lead_form", reason=reason, detail=detail
    )


def _campaign(item: _Prepared, **fields: Any) -> CampaignLeadForm:
    return CampaignLeadForm(
        campaign_ref=item.slot.brief.campaign_ref,
        campaign_type=item.slot.campaign_type,
        gaps=item.gaps,
        **fields,
    )


def _prepare(ctx: RunContext) -> list[_Prepared]:
    """The lead_gen campaigns in scope, their spec and the signals they may ask."""
    creative = ctx.require_creative()
    brief = CreativeBrief.model_validate(ctx.output_of("4.1.1"))
    campaigns = creative.input.account_structure.campaigns
    types = {campaign.type.strip().lower() for campaign in campaigns}
    firsts: dict[str, Slot] = {}
    for slot in slots(brief, campaigns, types=types):
        firsts.setdefault(slot.brief.campaign_ref, slot)
    refs = lead_gen_campaigns(creative.input.objectives, list(firsts))
    ruleset = creative.linter.ruleset
    definition = creative.input.lead_definition
    askable, blocked = screen_signals(list(definition.required_signals) if definition else [])
    prepared: list[_Prepared] = []
    for ref in refs:
        slot = firsts[ref]
        spec = ruleset.asset_specs.for_campaign(slot.campaign_type).get("lead_form")
        missing = (
            ["lead_form"]
            if spec is None
            else [f"lead_form.{n}" for n in NEEDS if getattr(spec, n) is None]
        )
        item = _Prepared(
            slot=slot, spec=None if missing else spec, signals=askable, contact_type=DEFAULT_CONTACT
        )
        if missing:
            item.gaps.append(
                _gap(
                    slot,
                    "spec_missing",
                    f"ruleset {ruleset.ruleset_version} has no "
                    f"asset_specs.{slot.campaign_type}.{', '.join(missing)}",
                )
            )
        else:
            if definition is None:
                item.gaps.append(
                    _gap(
                        slot,
                        "no_lead_definition",
                        "the frozen plan carries no lead definition (2.1.4)",
                    )
                )
            item.gaps.extend(
                _gap(
                    slot,
                    "art9_signal",
                    f"required signal {signal!r} is a GDPR Art. 9 category ({category}) "
                    "and is never asked",
                )
                for signal, category in blocked
            )
        prepared.append(item)
    return prepared


def _can_weigh(item: _Prepared, won: int, lost: Mapping[str, int]) -> bool:
    if not item.signals:
        return False
    if not (won or sum(lost.values())):
        item.gaps.append(
            _gap(
                item.slot,
                "no_crm_history",
                "no crm_won or crm_lost evidence to measure junk leads against",
            )
        )
        return False
    return True


def _audits(ctx: RunContext) -> dict[str, FormAudit]:
    """Each campaign's audited landing form: 4.5.1 names the ad groups, 4.5.2 the form."""
    brief = CreativeBrief.model_validate(ctx.output_of("4.1.1"))
    matched = LandingMessageMatchOutput.model_validate(ctx.output_of("4.5.1"))
    audited = LandingOfferAndFormOutput.model_validate(ctx.output_of("4.5.2"))
    forms = {
        page.audit_id: page.form
        for page in audited.pages
        if page.form is not None and page.form.fields
    }
    found: dict[str, FormAudit] = {}
    for group in brief.ad_groups:
        for page in matched.pages:
            form = forms.get(page.audit_id)
            if form is not None and group.ad_group_ref in page.ad_group_refs:
                found.setdefault(group.campaign_ref, form)
    return found


async def _evidence(
    ctx: RunContext, kinds: Sequence[str], source: EvidenceSource
) -> list[Evidence]:
    return list(
        (
            await ctx.db.execute(
                sa.select(Evidence)
                .where(
                    Evidence.project_id == ctx.project.id,
                    Evidence.source == source,
                    Evidence.kind.in_(kinds),
                )
                .order_by(Evidence.fetched_at, Evidence.id)
            )
        )
        .scalars()
        .all()
    )


async def _write(
    ctx: RunContext,
    brief: CreativeBrief,
    item: _Prepared,
    privacy: Sequence[str],
    licensed: Sequence[ClaimRef],
    now: datetime,
) -> CampaignLeadForm:
    creative = ctx.require_creative()
    slot, spec = item.slot, item.spec
    assert spec is not None
    chosen = list(item.signals)
    tradeoff: LeadFormTradeoff | None = None
    if item.tradeoff is not None and item.calc_evidence_id is not None:
        best = item.tradeoff["chosen"]
        chosen = chosen[: int(best["signals_asked"])]
        tradeoff = LeadFormTradeoff(
            fields_n=int(best["fields_n"]),
            expected_leads=float(best["expected_leads"]),
            expected_qualified=float(best["expected_qualified"]),
            calc_evidence_ids=[item.calc_evidence_id],
        )
    if 1 + len(chosen) > int(spec.max_count or 0):
        item.gaps.append(
            _gap(
                slot,
                "over_max_count",
                f"the form needs {1 + len(chosen)} questions; the spec allows {spec.max_count}",
            )
        )
        return _campaign(item, tradeoff=tradeoff)

    checks = await urlcheck.check_all(privacy, domain=ctx.project.domain)
    resolved = next((checks[url] for url in privacy if checks[url].status == "ok"), None)
    if resolved is None:
        item.gaps.append(
            _gap(
                slot,
                "no_privacy_policy_url",
                f"no crawled page on {ctx.project.domain} is a privacy policy that resolves "
                "2xx on-domain"
                + (
                    f" ({', '.join(f'{url}: {checks[url].status}' for url in privacy)})"
                    if privacy
                    else ""
                ),
            )
        )
        return _campaign(item, tradeoff=tradeoff)

    keys = [f"signal_{index}" for index in range(1, len(chosen) + 1)]
    draft: Any = await ctx.complete(
        draft_model(
            keys,
            question_types=[
                str(v) for v in creative.constants.extras.lead_form_question_types.value
            ],
            cta_types=[str(v) for v in creative.constants.extras.lead_form_cta_types.value],
        ),
        system=SYSTEM.format(max_chars=spec.max_chars),
        user=_user_prompt(brief, slot, dict(zip(keys, chosen, strict=True)), licensed),
        task_class=TaskClass.COPYWRITE,
    )
    questions = assemble_questions(item.contact_type, chosen, draft)
    sensitive = screen_questions(questions)
    if sensitive is not None:
        category, said = sensitive
        item.gaps.append(
            _gap(
                slot,
                "art9_question",
                f"the form was withheld: {said!r} targets a GDPR Art. 9 category ({category})",
            )
        )
        return _campaign(item, tradeoff=tradeoff)

    asset_id = uuid.uuid4()
    lines = [draft.headline, draft.description, *(q["text"] for q in questions if q["text"])]
    targets = [
        LintTarget(
            ref=f"{asset_id}:{index}",
            surface="lead_form",
            campaign_type=slot.campaign_type,
            market=slot.market,
            language=slot.language,
            text=line,
            generated_by_ai=True,
        )
        for index, line in enumerate(lines)
    ]
    linter = creative.linter
    spans = [
        span
        for target in targets
        for span in exceptions.unlicensed_spans(
            linter.lint_candidate(target, now=now), text=target.text or "", ruleset=linter.ruleset
        )
    ]
    result = linter.lint_candidates(targets, now=now)
    fate = screen(result.verdict, unlicensed=spans, quoted=True)
    candidates = exceptions.candidates(spans)
    if fate == "exception":
        return _campaign(item, tradeoff=tradeoff, exception_candidates=candidates)

    if fate != "eligible":
        item.gaps.append(
            _gap(
                slot,
                "failed_lint",
                "the lead form failed lint at the pin and stays a draft: "
                + ", ".join(lint_ref(result).rule_ids),
            )
        )
    check = UrlCheckRef(
        status=resolved.status,
        final_url_after_redirects=resolved.final_url,
        http_status=resolved.http_status,
    )
    form = {
        "asset_id": asset_id,
        "headline": draft.headline,
        "description": draft.description,
        "cta": str(draft.cta),
        "questions": questions,
        "privacy_policy_url": resolved.url,
        "privacy_url_check": check,
        "lint": lint_ref(result),
    }
    ctx.db.add(
        text_asset(
            ctx,
            asset_id=asset_id,
            node_id=NODE_ID,
            campaign_ref=slot.brief.campaign_ref,
            ad_group_ref=None,
            kind=CreativeAssetKind.LEAD_FORM,
            surface="lead_form",
            variant=None,
            category=None,
            text=draft.headline,
            fields={
                "description": draft.description,
                "cta": str(draft.cta),
                "questions": questions,
                "privacy_policy_url": resolved.url,
                "privacy_url_check": check.model_dump(mode="json"),
                "tradeoff": tradeoff.model_dump(mode="json") if tradeoff else None,
            },
            claim_ids=[],
            status=CreativeAssetStatus.LINTED if fate == "eligible" else CreativeAssetStatus.DRAFT,
            lint=result,
        )
    )
    await ctx.progress(
        f"{slot.brief.campaign_ref}: a {len(questions)}-question lead form "
        + (f"(the trade-off's choice of {tradeoff.fields_n}) " if tradeoff else "")
        + f"with privacy policy {resolved.url}; lint {result.verdict}"
    )
    return _campaign(
        item,
        form=form if fate == "eligible" else None,
        tradeoff=tradeoff,
        exception_candidates=candidates,
    )


def _user_prompt(
    brief: CreativeBrief, slot: Slot, signals: Mapping[str, str], licensed: Sequence[ClaimRef]
) -> str:
    return render_prompt(
        {
            "CAMPAIGN": {
                "campaign": slot.brief.campaign_ref,
                "theme": slot.brief.theme,
                "primary_message": slot.brief.primary_message.text,
                "language": slot.language,
            },
            "BRIEF": brief_section(brief),
            "QUESTIONS": dict(signals),
            "PROOF POINTS": proof_points(licensed),
            "VOICE": list(brief.non_negotiables.voice_words),
            "NEVER": list(brief.non_negotiables.never_terms),
        }
    )


LEAD_FORM_ASSET = LeadFormAssetNode()
