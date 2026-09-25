"""4.3.2 `offer_assets` — promotion and price assets (PRD §11 4.3.2, law 35).

**The model never writes a number or a date in an offer.** Every figure a
promotion or price shows is an `OfferBinding` field reference, rendered in
code from the run's offer snapshot (`creative/offers.py`); the model writes
header text only, and its draft schema has no number, no date and no string
that may carry a digit.

1. **`not_required` when no offer is fresh** — every record in the snapshot is
   older than `offers.offer_max_age_days` at the run's start, undated, or not
   live then. Stale offers are skipped, never guessed; no model is asked.
2. **The specs come from the pin** — `promotion` (`max_chars`, `max_count`)
   and `price` (`max_chars`, `min_count`, `max_count`) for each campaign's
   type. One the sheet lacks is a recorded `spec_missing` gap; when no
   campaign has either, the node is `spec_missing` and asks no model.
3. **Code decides which offers** — per campaign, the usable offers of its
   market: each one with a saving becomes a promotion (a documented reference
   price is a percentage, else an amount off), up to the spec's count; the
   largest same-currency group becomes one price asset's items, within the
   spec's counts. Too few, no saving, or a figure the record cannot honestly
   render (a deadline without a timezone) is a recorded gap.
4. **Every item is linted at creation** (law 33) — a promotion's text as
   `promotion`, a price item's header and description as `price`. An
   unlicensed claim span withholds it (law 34); any other failure stays
   `draft`; fewer passing price items than the spec's minimum leaves the
   whole price asset in `draft`.

Nothing is written until every campaign has been built; an attempt that
follows a failed one first clears what that attempt wrote.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, create_model

from agent.creative import brief as briefs
from agent.creative import exceptions, offers
from agent.creative.offers import OfferBindingError
from agent.db.models import (
    CreativeAsset,
    CreativeAssetKind,
    CreativeAssetStatus,
    Evidence,
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
from agent.nodes.creative.n4_3_1_sitelinks_callouts_snippets import campaign_slots, spec_gap
from agent.schemas.creative_brief import CreativeBrief, OfferBinding
from agent.schemas.extras import (
    ExtraGap,
    GapReason,
    NoDigitText,
    OfferAssetsOutput,
    PriceAsset,
    Promotion,
)
from agent.schemas.guardrails import AssetSpec, ClaimRef, LintResult, LintTarget, OfferRecord

NODE_ID = "4.3.2"

PROMOTION_NEEDS: Final = ("max_chars", "max_count")
PRICE_NEEDS: Final = ("max_chars", "min_count", "max_count")

SYSTEM = """You write the words of Google Ads promotion and price assets.

Every number, price, percentage, currency and date on these assets is filled in
by the system from the advertiser's offer data. You write none of them.

Rules, all of them hard:
- Write words only: no digit anywhere, not in a product name, not spelled out as
  a figure. Never copy an SKU code.
{rules}
- State no fact about the product that PROOF POINTS does not state.
- No exclamation marks. Plain, specific language in the brand's voice. Never use a
  word from NEVER.
"""


# ---------------------------------------------------------------------------
# the parts that decide — pure, and tested on their own
# ---------------------------------------------------------------------------


def not_required_reason(found: offers.Usable, *, max_age_days: int) -> str | None:
    """Why there is no offer to write for, or None when there is one."""
    if found.usable:
        return None
    if not (found.stale or found.not_live):
        return "the offer snapshot has no offer records"
    parts = []
    if found.stale:
        parts.append(
            f"{len(found.stale)} older than {max_age_days} days or undated at the run's start"
        )
    if found.not_live:
        parts.append(f"{len(found.not_live)} not live at the run's start")
    return (
        f"no fresh, live offer: {'; '.join(parts)}. Stale offers are skipped, never guessed "
        "(law 35)"
    )


@dataclass(slots=True)
class Plan:
    promotions: list[OfferRecord] = field(default_factory=list)
    price_items: list[OfferRecord] = field(default_factory=list)
    gaps: list[tuple[GapReason, str]] = field(default_factory=list)


def _bindable(record: OfferRecord, fields: Mapping[str, str] | None) -> bool:
    if fields is None:
        return False
    try:
        offers.resolve(record, fields)
    except OfferBindingError:
        return False
    return True


def plan_campaign(
    records: Sequence[OfferRecord], *, promotion: AssetSpec | None, price: AssetSpec | None
) -> Plan:
    """Which of a campaign's usable offers become promotions and which price items."""
    plan = Plan()
    ordered = sorted(records, key=lambda record: (record.product_set, record.sku))
    if promotion is not None:
        discounted = [record for record in ordered if offers.promotion_fields(record) is not None]
        bindable = [
            record for record in discounted if _bindable(record, offers.promotion_fields(record))
        ]
        plan.promotions = bindable[: int(promotion.max_count or 0)]
        if not discounted:
            plan.gaps.append(("no_discount", "no usable offer saves anything on its price"))
        elif len(bindable) < len(discounted):
            plan.gaps.append(
                (
                    "unbindable_offer",
                    f"{len(discounted) - len(bindable)} discounted offer(s) carry a figure the "
                    "record cannot honestly render (a date without a timezone)",
                )
            )
    if price is not None:
        by_currency: dict[str, list[OfferRecord]] = defaultdict(list)
        for record in ordered:
            if _bindable(record, offers.price_fields(record)):
                by_currency[record.currency.strip().upper()].append(record)
        group = max(sorted(by_currency.items()), key=lambda item: len(item[1]), default=("", []))[1]
        minimum = int(price.min_count or 0)
        if len(group) < max(minimum, 1):
            plan.gaps.append(
                (
                    "too_few_offers",
                    f"{len(group)} usable offer(s) in one currency; a price asset needs {minimum}",
                )
            )
        else:
            plan.price_items = group[: int(price.max_count or 0)]
    return plan


class _Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")


def draft_model(
    promotion_keys: Sequence[str], price_keys: Sequence[str], *, price_types: Sequence[str]
) -> type[BaseModel]:
    """Header text only: one text per promotion, a header and description per price item."""
    fields: dict[str, Any] = {}
    if promotion_keys:
        text = create_model("PromotionTextDraft", __base__=_Draft, text=(NoDigitText, ...))
        per_promotion: dict[str, Any] = {key: (text, ...) for key in promotion_keys}
        promotions = create_model("PromotionsDraft", __base__=_Draft, **per_promotion)
        fields["promotions"] = (promotions, ...)
    if price_keys:
        item = create_model(
            "PriceItemDraft",
            __base__=_Draft,
            header=(NoDigitText, ...),
            description=(NoDigitText, ...),
        )
        per_item: dict[str, Any] = {key: (item, ...) for key in price_keys}
        items = create_model("PriceItemsDraft", __base__=_Draft, **per_item)
        price_type: Any = Literal[tuple(price_types)]
        price = create_model(
            "PriceDraft", __base__=_Draft, type=(price_type, ...), items=(items, ...)
        )
        fields["price"] = (price, ...)
    return create_model("OfferAssetsDraft", __base__=_Draft, **fields)


# ---------------------------------------------------------------------------
# the node
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Built:
    promotions: list[Promotion] = field(default_factory=list)
    prices: list[PriceAsset] = field(default_factory=list)
    gaps: list[ExtraGap] = field(default_factory=list)
    rows: list[CreativeAsset] = field(default_factory=list)
    spans: list[str] = field(default_factory=list)
    attempted: bool = False


class OfferAssetsNode:
    spec = NodeSpec(
        id=NODE_ID,
        name="offer_assets",
        stage="4.3",
        run_stage=RunStage.CREATIVE,
        depends_on=("4.1.1",),
        task_class=TaskClass.COPYWRITE,
        input_model=CreativeBrief,
        output_model=OfferAssetsOutput,
        lint_required=True,
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        # The brief, the pinned offer snapshot and the pin's spec sheet.
        return []

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        creative = ctx.require_creative()
        brief = CreativeBrief.model_validate(ctx.output_of("4.1.1"))
        await clear_earlier_attempts(ctx, NODE_ID)
        # The run's start, not the wall clock, as in 4.2.1.
        now = ctx.run.started_at or datetime.now(UTC)
        max_age = int(creative.constants.offers.offer_max_age_days.value)
        found = offers.usable(creative.input.offer_records, now=now, max_age_days=max_age)
        counts = {"offers_fresh": len(found.usable), "offers_stale": len(found.stale)}
        reason = not_required_reason(found, max_age_days=max_age)
        if reason is not None:
            return OfferAssetsOutput(status="not_required", why=reason, **counts)

        campaigns = creative.input.account_structure.campaigns
        types = {campaign.type.strip().lower() for campaign in campaigns}
        found_slots = campaign_slots(brief, slots(brief, campaigns, types=types))
        licensed = briefs.licensed_claims(creative.linter.ruleset, now)
        built = [
            await _write(ctx, brief, slot, found.usable, licensed, now) for slot in found_slots
        ]
        for item in built:
            ctx.db.add_all(item.rows)
        await ctx.db.flush()

        gaps = [gap for item in built for gap in item.gaps]
        output: dict[str, Any] = {
            **counts,
            "promotions": [p for item in built for p in item.promotions],
            "prices": [p for item in built for p in item.prices],
            "gaps": gaps,
            "exception_candidates": exceptions.candidates(
                span for item in built for span in item.spans
            ),
        }
        if not any(item.attempted for item in built):
            return OfferAssetsOutput(
                status="spec_missing",
                why=(
                    f"ruleset {creative.linter.ruleset.ruleset_version} has no promotion or "
                    "price spec for any campaign in scope; Stage 04 never guesses a Google "
                    "limit (§9.5)"
                ),
                **{**output, "promotions": [], "prices": []},
            )
        return OfferAssetsOutput(
            status="required",
            why=(
                f"{counts['offers_fresh']} fresh, live offer(s) across {len(built)} campaign(s); "
                f"{counts['offers_stale']} stale offer(s) skipped"
            ),
            **output,
        )


def _spec(sheet: Mapping[str, AssetSpec], asset_type: str, needs: Sequence[str]) -> list[str]:
    spec = sheet.get(asset_type)
    if spec is None:
        return [asset_type]
    return [f"{asset_type}.{name}" for name in needs if getattr(spec, name) is None]


async def _write(
    ctx: RunContext,
    brief: CreativeBrief,
    slot: Slot,
    usable: Sequence[OfferRecord],
    licensed: Sequence[ClaimRef],
    now: datetime,
) -> Built:
    creative = ctx.require_creative()
    ruleset = creative.linter.ruleset
    campaign_ref = slot.brief.campaign_ref
    built = Built()

    def gap(asset_type: str, reason: GapReason, detail: str) -> None:
        built.gaps.append(
            ExtraGap(campaign_ref=campaign_ref, asset_type=asset_type, reason=reason, detail=detail)
        )

    sheet = ruleset.asset_specs.for_campaign(slot.campaign_type)
    specs: dict[str, AssetSpec] = {}
    for asset_type, needs in (("promotion", PROMOTION_NEEDS), ("price", PRICE_NEEDS)):
        missing = _spec(sheet, asset_type, needs)
        if missing:
            gap(
                asset_type,
                "spec_missing",
                spec_gap(ruleset.ruleset_version, slot, missing),
            )
        else:
            specs[asset_type] = sheet[asset_type]
    if not specs:
        return built
    built.attempted = True

    records = offers.in_market(usable, slot.market)
    if not records:
        for asset_type in specs:
            gap(asset_type, "no_fresh_offer_in_market", f"no usable offer in market {slot.market}")
        return built
    plan = plan_campaign(records, promotion=specs.get("promotion"), price=specs.get("price"))
    for reason, detail in plan.gaps:
        gap("price" if reason == "too_few_offers" else "promotion", reason, detail)
    if not (plan.promotions or plan.price_items):
        return built

    promotion_keys = {f"offer_{n}": record for n, record in enumerate(plan.promotions, start=1)}
    price_keys = {f"offer_{n}": record for n, record in enumerate(plan.price_items, start=1)}
    draft: Any = await ctx.complete(
        draft_model(
            list(promotion_keys),
            list(price_keys),
            price_types=[str(v) for v in creative.constants.extras.price_types.value],
        ),
        system=SYSTEM.format(rules=_rules(specs, promotion_keys, price_keys)),
        user=_user_prompt(brief, slot, promotion_keys, price_keys, licensed),
        task_class=TaskClass.COPYWRITE,
    )

    final_url = str(slot.brief.landing_url)
    lint = _Linter(ctx, slot, now)
    for key, record in promotion_keys.items():
        fields = offers.promotion_fields(record)
        assert fields is not None  # plan_campaign kept only offers with a saving
        binding = offers.bind(record, fields)
        text = getattr(draft.promotions, key).text
        asset_id = uuid.uuid4()
        result, fate, spans = lint.run(asset_id, "promotion", [text])
        if fate == "exception":
            built.spans.extend(spans)
            continue
        kind = next(name for name in ("percent_off", "money_off") if name in fields)
        facts = {
            "occasion": None,
            "discount_kind": kind,
            "bound": {kind: binding.resolved[kind], "currency": binding.resolved["currency"]},
            "start": binding.resolved.get("start"),
            "end": binding.resolved.get("end"),
            "final_url": final_url,
        }
        built.rows.append(
            _row(
                ctx,
                slot,
                asset_id,
                CreativeAssetKind.PROMOTION,
                "promotion",
                text,
                facts,
                binding,
                result,
                fate,
            )
        )
        if fate == "eligible":
            built.promotions.append(
                Promotion.model_validate(
                    {
                        "asset_id": asset_id,
                        "campaign_ref": campaign_ref,
                        **facts,
                        "text": text,
                        "offer_binding": binding,
                        "lint": lint_ref(result),
                    }
                )
            )

    if price_keys:
        price_asset_id = uuid.uuid4()
        price_type = str(draft.price.type)
        items: list[dict[str, Any]] = []
        rows: list[CreativeAsset] = []
        for key, record in price_keys.items():
            binding = offers.bind(record, offers.price_fields(record))
            item = getattr(draft.price.items, key)
            asset_id = uuid.uuid4()
            result, fate, spans = lint.run(asset_id, "price", [item.header, item.description])
            if fate == "exception":
                built.spans.extend(spans)
                continue
            facts = {
                "description": item.description,
                "type": price_type,
                "qualifier": None,
                "price_asset_id": str(price_asset_id),
                "bound": {
                    "price": binding.resolved["price"],
                    "currency": binding.resolved["currency"],
                },
                "final_url": final_url,
            }
            rows.append(
                _row(
                    ctx,
                    slot,
                    asset_id,
                    CreativeAssetKind.PRICE,
                    "price",
                    item.header,
                    facts,
                    binding,
                    result,
                    fate,
                )
            )
            if fate == "eligible":
                items.append(
                    {
                        "asset_id": asset_id,
                        "header": item.header,
                        "description": item.description,
                        "bound": facts["bound"],
                        "final_url": final_url,
                        "offer_binding": binding,
                        "lint": lint_ref(result),
                    }
                )
        minimum = int(specs["price"].min_count or 0)
        if len(items) < max(minimum, 1):
            # An incomplete price asset is not an asset: every item stays a draft.
            for row in rows:
                row.status = CreativeAssetStatus.DRAFT
            gap(
                "price",
                "below_min_count",
                f"{len(items)} item(s) passed; the spec's minimum is {minimum}",
            )
        else:
            built.prices.append(
                PriceAsset.model_validate(
                    {
                        "price_asset_id": price_asset_id,
                        "campaign_ref": campaign_ref,
                        "type": price_type,
                        "items": items,
                    }
                )
            )
        built.rows.extend(rows)

    await ctx.progress(
        f"{campaign_ref}: {len(built.promotions)} promotion(s) and "
        f"{sum(len(p.items) for p in built.prices)} price item(s) passed lint, every figure "
        "bound to an offer record"
    )
    return built


class _Linter:
    """Lints one offer asset's model-written lines at creation (law 33)."""

    def __init__(self, ctx: RunContext, slot: Slot, now: datetime) -> None:
        self.linter = ctx.require_creative().linter
        self.slot = slot
        self.now = now

    def run(
        self, asset_id: uuid.UUID, surface: Literal["promotion", "price"], lines: Sequence[str]
    ) -> tuple[LintResult, str, list[str]]:
        targets = [
            LintTarget(
                ref=f"{asset_id}:{index}",
                surface=surface,
                campaign_type=self.slot.campaign_type,
                market=self.slot.market,
                language=self.slot.language,
                text=line,
                generated_by_ai=True,
            )
            for index, line in enumerate(lines)
        ]
        spans = [
            span
            for target in targets
            for span in exceptions.unlicensed_spans(
                self.linter.lint_candidate(target, now=self.now),
                text=target.text or "",
                ruleset=self.linter.ruleset,
            )
        ]
        result = self.linter.lint_candidates(targets, now=self.now)
        return result, screen(result.verdict, unlicensed=spans, quoted=True), spans


def _row(
    ctx: RunContext,
    slot: Slot,
    asset_id: uuid.UUID,
    kind: CreativeAssetKind,
    surface: str,
    text: str,
    facts: Mapping[str, Any],
    binding: OfferBinding,
    result: LintResult,
    fate: str,
) -> CreativeAsset:
    return text_asset(
        ctx,
        asset_id=asset_id,
        node_id=NODE_ID,
        campaign_ref=slot.brief.campaign_ref,
        ad_group_ref=None,
        kind=kind,
        surface=surface,
        variant=None,
        category="offer",
        text=text,
        fields=facts,
        claim_ids=[],
        status=CreativeAssetStatus.LINTED if fate == "eligible" else CreativeAssetStatus.DRAFT,
        lint=result,
        offer_binding=binding.model_dump(mode="json"),
    )


def _rules(
    specs: Mapping[str, AssetSpec],
    promotion_keys: Mapping[str, OfferRecord],
    price_keys: Mapping[str, OfferRecord],
) -> str:
    lines: list[str] = []
    if promotion_keys:
        lines.append(
            f"- For each promotion in OFFERS, write the item being promoted in at most "
            f"{specs['promotion'].max_chars} characters: what is on offer, never its terms."
        )
    if price_keys:
        lines.append(
            f"- For the price asset, choose its type and, for each item in OFFERS, write a "
            f"header and a description of at most {specs['price'].max_chars} characters each."
        )
    return "\n".join(lines)


def _user_prompt(
    brief: CreativeBrief,
    slot: Slot,
    promotion_keys: Mapping[str, OfferRecord],
    price_keys: Mapping[str, OfferRecord],
    licensed: Sequence[ClaimRef],
) -> str:
    def describe(record: OfferRecord) -> dict[str, str]:
        return {"product_set": record.product_set, "product": record.sku}

    return render_prompt(
        {
            "CAMPAIGN": {
                "campaign": slot.brief.campaign_ref,
                "theme": slot.brief.theme,
                "primary_message": slot.brief.primary_message.text,
                "language": slot.language,
            },
            "BRIEF": brief_section(brief),
            "OFFERS": {
                "promotions": {
                    key: {
                        **describe(record),
                        "discount": "a percentage off"
                        if "percent_off" in (offers.promotion_fields(record) or {})
                        else "an amount off",
                    }
                    for key, record in promotion_keys.items()
                },
                "price_items": {key: describe(record) for key, record in price_keys.items()},
            },
            "PROOF POINTS": proof_points(licensed),
            "VOICE": list(brief.non_negotiables.voice_words),
            "NEVER": list(brief.non_negotiables.never_terms),
        }
    )


OFFER_ASSETS = OfferAssetsNode()
