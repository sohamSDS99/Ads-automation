"""4.3.1 `sitelinks_callouts_snippets` — the campaign-level extras (PRD §11 4.3.1).

Per campaign the approved brief covers:

1. **The specs come from the pin** — `sitelink`, `callout` and
   `structured_snippet` for the campaign's type, each with `max_chars` and
   `max_count`. One the sheet lacks is a recorded `spec_missing` gap for that
   campaign and is not asked for; Stage 04 never guesses a Google limit (§9.5).
   When no campaign has any of the three, the node is `spec_missing` and asks
   no model.
2. **A sitelink points only at a page we have seen** — the project's crawled
   `web` pages and 4.5.1's landing pages, on the project's domain, one URL per
   page. The draft schema takes a `final_url` from that pool and nowhere else,
   and a snippet header only from `extras.snippet_headers`.
3. **Every sitelink URL is checked** (`preview/urlcheck.py`): on-domain at
   every hop, 2xx after redirects, and unique within the campaign after them.
   A sitelink whose URL fails is a `RejectedSitelink` — shown with why, never
   an asset.
4. **Every item is linted at creation** through `lint_adapter` (law 33): each
   of a sitelink's three lines, each callout, each snippet value. An
   unlicensed claim-shaped span withholds the item whole and becomes an
   exception candidate (law 34); any other failure stays `draft`; a pass is
   `linted`. Fewer passing than a spec's `min_count` is a recorded gap — the
   extras are the campaign's to launch without, not the run's to fail on.

Nothing is written until every campaign has been built; an attempt that
follows a failed one first clears what that attempt wrote.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any, Final, Literal

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, create_model

from agent.creative import brief as briefs
from agent.creative import exceptions
from agent.db.models import (
    CreativeAsset,
    CreativeAssetKind,
    CreativeAssetStatus,
    Evidence,
    EvidenceSource,
    RunStage,
)
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
from agent.preview import urlcheck
from agent.schemas.creative_brief import CreativeBrief
from agent.schemas.extras import (
    CampaignExtras,
    ExtraGap,
    SitelinksCalloutsSnippetsOutput,
    UrlCheckRef,
)
from agent.schemas.guardrails import AssetSpec, ClaimRef, LintResult, LintTarget
from agent.schemas.landing import LandingMessageMatchOutput

NODE_ID = "4.3.1"

#: The spec each extra is written against, and the fields it needs from it.
NEEDS: Final[Mapping[str, tuple[str, ...]]] = {
    "sitelink": ("max_chars", "max_count"),
    "callout": ("max_chars", "max_count"),
    "structured_snippet": ("max_chars", "max_count"),
}
#: Stage 03 names a surface per extra, and its asset type is its own spec.
SURFACES: Final[Mapping[str, str]] = {name: name for name in NEEDS}
KINDS: Final[Mapping[str, CreativeAssetKind]] = {
    "sitelink": CreativeAssetKind.SITELINK,
    "callout": CreativeAssetKind.CALLOUT,
    "structured_snippet": CreativeAssetKind.STRUCTURED_SNIPPET,
}
#: The `web_crawler` evidence kind of a crawled page (`payload.url`, `.title`).
WEB_PAGE_KIND: Final = "page"

SYSTEM = """You write the campaign-level extras of one Google Ads campaign.

Rules, all of them hard:
{rules}
- Every line is at most {max_chars} characters, counted with spaces, whatever
  the spec says for its first line — the lint enforces the surface's limit on
  every line of it.
- State no fact about the product that PROOF POINTS does not state, and write
  no number, price, percentage or date that a proof point does not state.
- No exclamation marks. Plain, specific language in the brand's voice. Never use a
  word from NEVER.
"""


@dataclass(frozen=True, slots=True)
class Page:
    url: str
    title: str | None


@dataclass(frozen=True, slots=True)
class Counts:
    sitelinks: int
    callouts: int
    #: (min, max) values of the one snippet, or None when there is no snippet.
    snippet_values: tuple[int, int] | None


# ---------------------------------------------------------------------------
# the parts that decide — pure, and tested on their own
# ---------------------------------------------------------------------------


def candidate_pages(found: Iterable[tuple[str, str | None]], *, domain: str) -> list[Page]:
    """The pages a sitelink may point at: on `domain`, first URL seen per page, in order."""
    pages: list[Page] = []
    seen: set[str] = set()
    for url, title in found:
        url = url.strip()
        if not urlcheck.on_domain(url, domain):
            continue
        page = urlcheck.canonical(url)
        if page in seen:
            continue
        seen.add(page)
        pages.append(Page(url=url, title=title))
    return pages


class _Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")


def draft_model(pool: Sequence[str], counts: Counts, *, headers: Sequence[str]) -> type[BaseModel]:
    """The campaign's draft: URLs from `pool`, headers from `headers`, the spec's counts."""
    line = Annotated[str, Field(min_length=1)]
    fields: dict[str, Any] = {}
    if counts.sitelinks:
        url: Any = Literal[tuple(pool)]
        sitelink = create_model(
            "SitelinkDraft",
            __base__=_Draft,
            link_text=(line, ...),
            line1=(line, ...),
            line2=(line, ...),
            final_url=(url, ...),
        )
        fields["sitelinks"] = (
            list[sitelink],
            Field(min_length=counts.sitelinks, max_length=counts.sitelinks),
        )
    if counts.callouts:
        fields["callouts"] = (
            list[line],
            Field(min_length=counts.callouts, max_length=counts.callouts),
        )
    if counts.snippet_values is not None:
        low, high = counts.snippet_values
        header: Any = Literal[tuple(headers)]
        snippet = create_model(
            "StructuredSnippetDraft",
            __base__=_Draft,
            header=(header, ...),
            values=(list[line], Field(min_length=low, max_length=high)),
        )
        fields["snippet"] = (snippet, ...)
    return create_model("SitelinksCalloutsSnippetsDraft", __base__=_Draft, **fields)


def spec_gap(ruleset_version: str, slot: Slot, missing: Sequence[str]) -> str:
    """Why a campaign has no spec for an extra — including a plan that named no type."""
    if not slot.campaign_type:
        return (
            f"the frozen plan names no campaign type for {slot.brief.campaign_ref}, so no spec "
            "applies"
        )
    return f"ruleset {ruleset_version} has no asset_specs.{slot.campaign_type}.{', '.join(missing)}"


def campaign_slots(brief: CreativeBrief, slots_: Sequence[Slot]) -> list[Slot]:
    """One slot per campaign the brief covers — its first ad group — in brief order."""
    firsts: dict[str, Slot] = {}
    for slot in slots_:
        firsts.setdefault(slot.brief.campaign_ref, slot)
    return list(firsts.values())


# ---------------------------------------------------------------------------
# the node
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Built:
    campaign: CampaignExtras
    rows: list[CreativeAsset] = field(default_factory=list)
    attempted: bool = False


class SitelinksCalloutsSnippetsNode:
    spec = NodeSpec(
        id=NODE_ID,
        name="sitelinks_callouts_snippets",
        stage="4.3",
        run_stage=RunStage.CREATIVE,
        depends_on=("4.5.1",),
        task_class=TaskClass.COPYWRITE,
        input_model=LandingMessageMatchOutput,
        output_model=SitelinksCalloutsSnippetsOutput,
        lint_required=True,
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        # The project's crawled pages: what a sitelink may point at.
        return list(
            (
                await ctx.db.execute(
                    sa.select(Evidence)
                    .where(
                        Evidence.project_id == ctx.project.id,
                        Evidence.source == EvidenceSource.WEB,
                        Evidence.kind == WEB_PAGE_KIND,
                    )
                    .order_by(Evidence.fetched_at, Evidence.id)
                )
            )
            .scalars()
            .all()
        )

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        creative = ctx.require_creative()
        brief = CreativeBrief.model_validate(ctx.output_of("4.1.1"))
        matched = LandingMessageMatchOutput.model_validate(ctx.output_of("4.5.1"))
        await clear_earlier_attempts(ctx, NODE_ID)

        campaigns = creative.input.account_structure.campaigns
        types = {campaign.type.strip().lower() for campaign in campaigns}
        found = campaign_slots(brief, slots(brief, campaigns, types=types))
        pool = candidate_pages(
            [
                *(
                    (str((row.payload or {}).get("url") or ""), (row.payload or {}).get("title"))
                    for row in ev
                ),
                *((page.final_url or page.url, None) for page in matched.pages if page.reachable),
            ],
            domain=ctx.project.domain,
        )
        # The run's start, not the wall clock, as in 4.2.1.
        now = ctx.run.started_at or datetime.now(UTC)
        licensed = briefs.licensed_claims(creative.linter.ruleset, now)
        built = [await _write(ctx, brief, slot, pool, licensed, now) for slot in found]

        for item in built:
            ctx.db.add_all(item.rows)
        await ctx.db.flush()
        if not any(item.attempted for item in built):
            return SitelinksCalloutsSnippetsOutput(
                status="spec_missing",
                why=(
                    f"ruleset {creative.linter.ruleset.ruleset_version} has no sitelink, callout "
                    "or structured_snippet spec for any campaign in scope; Stage 04 never "
                    "guesses a Google limit (§9.5)"
                ),
                campaigns=[item.campaign for item in built],
            )
        return SitelinksCalloutsSnippetsOutput(
            status="required",
            why=f"{len(built)} campaign(s) in scope, {len(pool)} on-domain page(s) to link to",
            campaigns=[item.campaign for item in built],
        )


def _specs(ctx: RunContext, slot: Slot) -> tuple[dict[str, AssetSpec], list[ExtraGap]]:
    ruleset = ctx.require_creative().linter.ruleset
    sheet = ruleset.asset_specs.for_campaign(slot.campaign_type)
    specs: dict[str, AssetSpec] = {}
    gaps: list[ExtraGap] = []
    for asset_type, needed in NEEDS.items():
        spec = sheet.get(asset_type)
        missing = (
            [asset_type]
            if spec is None
            else [f"{asset_type}.{name}" for name in needed if getattr(spec, name) is None]
        )
        if spec is None or missing:
            gaps.append(
                ExtraGap(
                    campaign_ref=slot.brief.campaign_ref,
                    asset_type=asset_type,
                    reason="spec_missing",
                    detail=spec_gap(ruleset.ruleset_version, slot, missing),
                )
            )
            continue
        specs[asset_type] = spec
    return specs, gaps


async def _write(
    ctx: RunContext,
    brief: CreativeBrief,
    slot: Slot,
    pool: Sequence[Page],
    licensed: Sequence[ClaimRef],
    now: datetime,
) -> Built:
    creative = ctx.require_creative()
    campaign_ref = slot.brief.campaign_ref
    specs, gaps = _specs(ctx, slot)
    base = {
        "campaign_ref": campaign_ref,
        "campaign_type": slot.campaign_type,
        "market": slot.market,
        "language": slot.language,
    }
    sitelink_spec = specs.get("sitelink")
    sitelinks = min(int(sitelink_spec.max_count or 0), len(pool)) if sitelink_spec else 0
    if sitelink_spec is not None and not pool:
        gaps.append(
            ExtraGap(
                campaign_ref=campaign_ref,
                asset_type="sitelink",
                reason="no_candidate_urls",
                detail=f"no crawled or landing page on {ctx.project.domain} to link to",
            )
        )
    snippet_spec = specs.get("structured_snippet")
    counts = Counts(
        sitelinks=sitelinks,
        callouts=int(specs["callout"].max_count or 0) if "callout" in specs else 0,
        snippet_values=(
            (max(int(snippet_spec.min_count or 1), 1), int(snippet_spec.max_count or 1))
            if snippet_spec is not None
            else None
        ),
    )
    if not (counts.sitelinks or counts.callouts or counts.snippet_values):
        return Built(campaign=CampaignExtras(**base, gaps=gaps))

    headers = [str(value) for value in creative.constants.extras.snippet_headers.value]
    draft: Any = await ctx.complete(
        draft_model([page.url for page in pool], counts, headers=headers),
        system=SYSTEM.format(
            rules=_rules(counts, specs),
            max_chars=min(int(spec.max_chars or 0) for spec in specs.values()),
        ),
        user=_user_prompt(brief, slot, pool, licensed),
        task_class=TaskClass.COPYWRITE,
    )

    writer = _Writer(ctx, slot, now)
    sitelink_items: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    if counts.sitelinks:
        checks = await urlcheck.check_all(
            [item.final_url for item in draft.sitelinks], domain=ctx.project.domain
        )
        marked = urlcheck.unique([checks[item.final_url] for item in draft.sitelinks])
        for item, check in zip(draft.sitelinks, marked, strict=True):
            ref = UrlCheckRef(
                status=check.status,
                final_url_after_redirects=check.final_url,
                http_status=check.http_status,
            )
            if check.status != "ok":
                rejected.append(
                    {"link_text": item.link_text, "final_url": item.final_url, "url_check": ref}
                )
                continue
            passed = writer.add(
                "sitelink",
                texts=[item.link_text, item.line1, item.line2],
                text=item.link_text,
                fields={
                    "line1": item.line1,
                    "line2": item.line2,
                    "final_url": item.final_url,
                    "url_check": ref.model_dump(mode="json"),
                },
            )
            if passed is not None:
                asset_id, lint = passed
                sitelink_items.append(
                    {
                        "asset_id": asset_id,
                        "link_text": item.link_text,
                        "line1": item.line1,
                        "line2": item.line2,
                        "final_url": item.final_url,
                        "url_check": ref,
                        "lint": lint,
                    }
                )
    callout_items: list[dict[str, Any]] = []
    for text in getattr(draft, "callouts", []) if counts.callouts else []:
        passed = writer.add("callout", texts=[text], text=text, fields={})
        if passed is not None:
            callout_items.append({"asset_id": passed[0], "text": text, "lint": passed[1]})
    snippet_items: list[dict[str, Any]] = []
    if counts.snippet_values is not None:
        values = list(draft.snippet.values)
        passed = writer.add(
            "structured_snippet",
            texts=values,
            text=str(draft.snippet.header),
            fields={"values": values},
        )
        if passed is not None:
            snippet_items.append(
                {
                    "asset_id": passed[0],
                    "header": str(draft.snippet.header),
                    "values": values,
                    "lint": passed[1],
                }
            )

    passing = {
        "sitelink": len(sitelink_items),
        "callout": len(callout_items),
        "structured_snippet": len(snippet_items),
    }
    for asset_type, spec in specs.items():
        if asset_type == "structured_snippet":
            continue  # one snippet; its min_count is a count of values
        minimum = int(spec.min_count or 0)
        if passing[asset_type] < minimum:
            gaps.append(
                ExtraGap(
                    campaign_ref=campaign_ref,
                    asset_type=asset_type,
                    reason="below_min_count",
                    detail=f"{passing[asset_type]} passed; the spec's minimum is {minimum}",
                )
            )

    campaign = CampaignExtras.model_validate(
        {
            **base,
            "sitelinks": sitelink_items,
            "rejected_sitelinks": rejected,
            "callouts": callout_items,
            "snippets": snippet_items,
            "gaps": gaps,
            "exception_candidates": exceptions.candidates(writer.spans),
        }
    )
    await ctx.progress(
        f"{campaign_ref}: {len(sitelink_items)} sitelink(s), {len(callout_items)} callout(s), "
        f"{len(snippet_items)} snippet(s) passed lint; {len(rejected)} sitelink URL(s) rejected"
    )
    return Built(campaign=campaign, rows=writer.rows, attempted=True)


class _Writer:
    """Lints one extra at creation (law 33) and builds its row by what the lint found."""

    def __init__(self, ctx: RunContext, slot: Slot, now: datetime) -> None:
        self.ctx = ctx
        self.slot = slot
        self.now = now
        self.rows: list[CreativeAsset] = []
        self.spans: list[str] = []

    def _target(self, ref: str, surface: str, text: str) -> LintTarget:
        return LintTarget(
            ref=ref,
            surface=surface,
            campaign_type=self.slot.campaign_type,
            market=self.slot.market,
            language=self.slot.language,
            text=text,
            generated_by_ai=True,
        )

    def add(
        self, asset_type: str, *, texts: Sequence[str], text: str, fields: Mapping[str, Any]
    ) -> tuple[uuid.UUID, Any] | None:
        """Lint every line; return `(asset_id, LintRef)` when the item passed, else None."""
        creative = self.ctx.require_creative()
        linter = creative.linter
        asset_id = uuid.uuid4()
        surface = SURFACES[asset_type]
        targets = [
            self._target(f"{asset_id}:{index}", surface, line) for index, line in enumerate(texts)
        ]
        unlicensed = [
            span
            for target in targets
            for span in exceptions.unlicensed_spans(
                linter.lint_candidate(target, now=self.now),
                text=target.text or "",
                ruleset=linter.ruleset,
            )
        ]
        result: LintResult = linter.lint_candidates(targets, now=self.now)
        fate = screen(result.verdict, unlicensed=unlicensed, quoted=True)
        if fate == "exception":
            # Law 34: withheld whole, never an asset.
            self.spans.extend(unlicensed)
            return None
        self.rows.append(
            text_asset(
                self.ctx,
                asset_id=asset_id,
                node_id=NODE_ID,
                campaign_ref=self.slot.brief.campaign_ref,
                ad_group_ref=None,
                kind=KINDS[asset_type],
                surface=surface,
                variant=None,
                category=None,
                text=text,
                fields=fields,
                claim_ids=[],
                status=CreativeAssetStatus.LINTED
                if fate == "eligible"
                else CreativeAssetStatus.DRAFT,
                lint=result,
            )
        )
        return (asset_id, lint_ref(result)) if fate == "eligible" else None


def _rules(counts: Counts, specs: Mapping[str, AssetSpec]) -> str:
    lines: list[str] = []
    if counts.sitelinks:
        lines.append(
            f"- Write exactly {counts.sitelinks} sitelinks: link text of at most "
            f"{specs['sitelink'].max_chars} characters and two description lines, each "
            "pointing at a different page from PAGES — its final_url is that page's url."
        )
    if counts.callouts:
        lines.append(
            f"- Write exactly {counts.callouts} callouts of at most "
            f"{specs['callout'].max_chars} characters, each a different reason to choose us."
        )
    if counts.snippet_values is not None:
        low, high = counts.snippet_values
        lines.append(
            f"- Write one structured snippet: a header from the allowed list and {low} to "
            f"{high} values of at most {specs['structured_snippet'].max_chars} characters, "
            "each a real instance of that header."
        )
    return "\n".join(lines)


def _user_prompt(
    brief: CreativeBrief, slot: Slot, pool: Sequence[Page], licensed: Sequence[ClaimRef]
) -> str:
    return render_prompt(
        {
            "CAMPAIGN": {
                "campaign": slot.brief.campaign_ref,
                "campaign_type": slot.campaign_type,
                "theme": slot.brief.theme,
                "primary_message": slot.brief.primary_message.text,
                "landing_url": str(slot.brief.landing_url),
                "language": slot.language,
            },
            "BRIEF": brief_section(brief),
            "PAGES": [{"url": page.url, "title": page.title} for page in pool],
            "PROOF POINTS": proof_points(licensed),
            "VOICE": list(brief.non_negotiables.voice_words),
            "NEVER": list(brief.non_negotiables.never_terms),
        }
    )


SITELINKS_CALLOUTS_SNIPPETS = SitelinksCalloutsSnippetsNode()
