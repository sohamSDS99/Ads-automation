"""Stage 4.3 contracts — the extras (Stage 04 PRD §11 4.3.1–4.3.3, §12.2, laws 33–35).

What each output may say, enforced where it is built:

* **4.3.1** — a sitelink in the output is on-domain, answered 2xx after
  redirects, and lands on a page no other sitelink of its campaign lands on.
  One whose URL failed is a `RejectedSitelink`, never a `Sitelink`.
* **4.3.2** — law 35. A model-written field (`text`, `header`, `description`)
  carries **no digit in any script**, so a price, a percentage or a date the
  model typed fails the schema. Every number and date the asset shows is an
  `OfferBinding` field reference, and what it shows is exactly the binding's
  rendered value — a figure that is not the bound one fails too.
* **4.3.3** — a lead form's privacy policy URL resolved 2xx on-domain; no
  question, option or qualified signal targets a GDPR Art. 9 category (the
  Stage 02 blocklist); the trade-off counts the form it chose and cites its
  `calc/` evidence.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent.planning.sensitive_categories import article_9_category
from agent.preview.urlcheck import UrlStatus, canonical
from agent.schemas.creative_brief import OfferBinding
from agent.schemas.search_ads import ExceptionCandidate, LintRef

EXTRAS_SCHEMA_VERSION: Literal["1.0"] = "1.0"

#: A field the model writes on a promotion or price asset (law 35: header text
#: only). `\D` is Unicode-aware, so "٢٠" and "２０" fail exactly as "20" does.
NoDigitText = Annotated[str, Field(min_length=1, pattern=r"^\D*$")]

#: Why an extra was not written for a campaign. Each is a recorded outcome,
#: never a guess (§9.5, law 35).
GapReason = Literal[
    "spec_missing",
    "no_candidate_urls",
    "below_min_count",
    "over_max_count",
    "no_landing_url",
    "no_fresh_offer_in_market",
    "no_discount",
    "too_few_offers",
    "unbindable_offer",
    "no_lead_definition",
    "no_crm_history",
    "no_audited_form",
    "no_privacy_policy_url",
    "art9_signal",
    "art9_question",
    "failed_lint",
]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class UrlCheckRef(_Frozen):
    """`preview.urlcheck.UrlCheck`, as an output carries it."""

    status: UrlStatus
    final_url_after_redirects: str | None = None
    http_status: int | None = None


class ExtraGap(_Frozen):
    campaign_ref: str = Field(min_length=1)
    asset_type: str = Field(min_length=1)
    reason: GapReason
    detail: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# 4.3.1 — sitelinks, callouts, structured snippets
# ---------------------------------------------------------------------------


class Sitelink(_Frozen):
    asset_id: UUID
    link_text: str = Field(min_length=1)
    line1: str = Field(min_length=1)
    line2: str = Field(min_length=1)
    final_url: str = Field(min_length=1)
    url_check: UrlCheckRef
    lint: LintRef

    @model_validator(mode="after")
    def _url_passed(self) -> Sitelink:
        if self.url_check.status != "ok" or not self.url_check.final_url_after_redirects:
            raise ValueError(
                f"a sitelink's URL is on-domain, 2xx after redirects and unique; "
                f"{self.final_url} is {self.url_check.status}"
            )
        return self


class RejectedSitelink(_Frozen):
    """A sitelink the model wrote whose URL failed — shown with why, never an asset."""

    link_text: str = Field(min_length=1)
    final_url: str = Field(min_length=1)
    url_check: UrlCheckRef

    @model_validator(mode="after")
    def _url_failed(self) -> RejectedSitelink:
        if self.url_check.status == "ok":
            raise ValueError("a rejected sitelink is one whose URL check failed")
        return self


class Callout(_Frozen):
    asset_id: UUID
    text: str = Field(min_length=1)
    lint: LintRef


class StructuredSnippet(_Frozen):
    asset_id: UUID
    #: One of `extras.snippet_headers` — the draft schema offers no other.
    header: str = Field(min_length=1)
    values: list[str] = Field(min_length=1)
    lint: LintRef


class CampaignExtras(_Frozen):
    campaign_ref: str = Field(min_length=1)
    #: The frozen plan's type; "" when it names none, and then no spec applies.
    campaign_type: str
    market: str = Field(min_length=1)
    language: str = Field(min_length=1)
    sitelinks: list[Sitelink] = Field(default_factory=list)
    rejected_sitelinks: list[RejectedSitelink] = Field(default_factory=list)
    callouts: list[Callout] = Field(default_factory=list)
    snippets: list[StructuredSnippet] = Field(default_factory=list)
    gaps: list[ExtraGap] = Field(default_factory=list)
    exception_candidates: list[ExceptionCandidate] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_per_campaign(self) -> CampaignExtras:
        pages = [
            canonical(item.url_check.final_url_after_redirects or "") for item in self.sitelinks
        ]
        if len(pages) != len(set(pages)):
            raise ValueError(
                f"{self.campaign_ref}: sitelink URLs are unique per campaign after redirects"
            )
        return self

    def writes_anything(self) -> bool:
        return bool(self.sitelinks or self.callouts or self.snippets)


class SitelinksCalloutsSnippetsOutput(_Frozen):
    schema_version: Literal["1.0"] = EXTRAS_SCHEMA_VERSION
    #: `spec_missing`: the pin's spec sheet has none of the three for any
    #: campaign in scope — nothing is written, and the gaps say which.
    status: Literal["required", "spec_missing"]
    why: str = Field(min_length=1)
    campaigns: list[CampaignExtras] = Field(default_factory=list)

    @model_validator(mode="after")
    def _status_matches(self) -> SitelinksCalloutsSnippetsOutput:
        if self.status == "spec_missing" and any(c.writes_anything() for c in self.campaigns):
            raise ValueError("a spec_missing output writes no sitelink, callout or snippet")
        return self


# ---------------------------------------------------------------------------
# 4.3.2 — promotions and prices (law 35)
# ---------------------------------------------------------------------------

#: The `OfferRecord` fields a binding may name, and the two figures derived
#: from them in `creative/offers.py` exactly as Stage 03's offer matchers
#: derive them (`percent_off`, `amount_off`). Nothing else is a field.
OfferRef = Literal[
    "current_price",
    "list_price",
    "reference_price",
    "currency",
    "effective_from",
    "effective_to",
    "ends_at",
    "percent_off",
    "money_off",
]
OFFER_REFS: Final[frozenset[str]] = frozenset(OfferRef.__args__)  # type: ignore[attr-defined]

#: Which record field each asset field may be bound to.
PROMOTION_REFS: Final[dict[str, frozenset[str]]] = {
    "percent_off": frozenset({"percent_off"}),
    "money_off": frozenset({"money_off"}),
    "currency": frozenset({"currency"}),
    "start": frozenset({"effective_from"}),
    "end": frozenset({"ends_at", "effective_to"}),
}
PRICE_REFS: Final[dict[str, frozenset[str]]] = {
    "price": frozenset({"current_price"}),
    "currency": frozenset({"currency"}),
}


def _check_binding(
    binding: OfferBinding, shown: dict[str, str | None], allowed: dict[str, frozenset[str]]
) -> None:
    """Every figure shown is bound, to a legal field, and is the bound value."""
    wanted = {key for key, value in shown.items() if value is not None}
    if set(binding.fields) != wanted:
        raise ValueError(
            f"OfferBinding binds {sorted(binding.fields)}, but the asset shows {sorted(wanted)}: "
            "every number and date is a field reference (law 35)"
        )
    for key, ref in binding.fields.items():
        if ref not in OFFER_REFS or ref not in allowed.get(key, frozenset()):
            raise ValueError(f"OfferBinding binds {key} to {ref!r}, which is not an offer field")
    if set(binding.resolved) != set(binding.fields):
        raise ValueError("OfferBinding.resolved renders exactly the fields it binds")
    for key in wanted:
        if shown[key] != binding.resolved[key]:
            raise ValueError(
                f"{key} shows {shown[key]!r}, but its OfferBinding renders "
                f"{binding.resolved[key]!r}"
            )


class PromotionBound(_Frozen):
    percent_off: str | None = None
    money_off: str | None = None
    currency: str = Field(min_length=1)
    #: §11 names both; `OfferRecord` carries neither, so neither is ever bound
    #: and neither is ever written (docs/stage-04-questions.md § S4-P8).
    promo_code: None = None
    orders_over: None = None


class Promotion(_Frozen):
    asset_id: UUID
    campaign_ref: str = Field(min_length=1)
    #: No offer record names an occasion and the model may not choose one.
    occasion: None = None
    discount_kind: Literal["percent_off", "money_off"]
    bound: PromotionBound
    start: str | None = None
    end: str | None = None
    final_url: str = Field(min_length=1)
    text: NoDigitText
    offer_binding: OfferBinding
    lint: LintRef

    @model_validator(mode="after")
    def _every_figure_is_bound(self) -> Promotion:
        other = "money_off" if self.discount_kind == "percent_off" else "percent_off"
        if getattr(self.bound, self.discount_kind) is None or getattr(self.bound, other):
            raise ValueError(f"a {self.discount_kind} promotion shows its discount and no other")
        _check_binding(
            self.offer_binding,
            {
                self.discount_kind: getattr(self.bound, self.discount_kind),
                "currency": self.bound.currency,
                "start": self.start,
                "end": self.end,
            },
            PROMOTION_REFS,
        )
        return self


class PriceBound(_Frozen):
    price: str = Field(min_length=1)
    currency: str = Field(min_length=1)
    #: `OfferRecord` has no unit; the item is priced as the record prices it.
    unit: None = None


class PriceItem(_Frozen):
    asset_id: UUID
    header: NoDigitText
    description: NoDigitText
    bound: PriceBound
    final_url: str = Field(min_length=1)
    offer_binding: OfferBinding
    lint: LintRef

    @model_validator(mode="after")
    def _every_figure_is_bound(self) -> PriceItem:
        _check_binding(
            self.offer_binding,
            {"price": self.bound.price, "currency": self.bound.currency},
            PRICE_REFS,
        )
        return self


class PriceAsset(_Frozen):
    price_asset_id: UUID
    campaign_ref: str = Field(min_length=1)
    #: One of `extras.price_types` — the draft schema offers no other.
    type: str = Field(min_length=1)
    #: None: each item is one record's exact current price, so no "from",
    #: "up to" or "average" is true of it.
    qualifier: None = None
    items: list[PriceItem] = Field(min_length=1)

    @model_validator(mode="after")
    def _one_item_per_record(self) -> PriceAsset:
        records = [item.offer_binding.offer_record_id for item in self.items]
        if len(records) != len(set(records)):
            raise ValueError("each price item binds a different offer record")
        return self


class OfferAssetsOutput(_Frozen):
    schema_version: Literal["1.0"] = EXTRAS_SCHEMA_VERSION
    #: `not_required`: no offer in the snapshot is fresh (≤ `offer_max_age_days`)
    #: and live at the run's start — stale offers are skipped, never guessed.
    #: `spec_missing`: the pin's spec sheet has neither surface for any campaign.
    status: Literal["not_required", "spec_missing", "required"]
    why: str = Field(min_length=1)
    offers_fresh: int = Field(ge=0)
    offers_stale: int = Field(ge=0)
    promotions: list[Promotion] = Field(default_factory=list)
    prices: list[PriceAsset] = Field(default_factory=list)
    gaps: list[ExtraGap] = Field(default_factory=list)
    exception_candidates: list[ExceptionCandidate] = Field(default_factory=list)

    @model_validator(mode="after")
    def _status_matches(self) -> OfferAssetsOutput:
        if self.status != "required" and (self.promotions or self.prices):
            raise ValueError(f"a {self.status} output writes no promotion or price")
        return self


# ---------------------------------------------------------------------------
# 4.3.3 — lead form
# ---------------------------------------------------------------------------

CUSTOM_QUESTION: Final = "custom"


class LeadFormQuestion(_Frozen):
    #: One of `extras.lead_form_question_types`, or `custom`.
    type: str = Field(min_length=1)
    text: str | None = None
    options: list[str] = Field(default_factory=list)
    #: The `lead_definition.required_signals` entry the answer qualifies.
    qualifies_signal: str | None = None

    @model_validator(mode="after")
    def _asks_nothing_sensitive(self) -> LeadFormQuestion:
        if self.type == CUSTOM_QUESTION and not (self.text or "").strip():
            raise ValueError("a custom question has text")
        for said in (self.type, self.text or "", self.qualifies_signal or "", *self.options):
            category = article_9_category(said)
            if category is not None:
                raise ValueError(
                    f"a lead form question may not target a GDPR Article 9 category "
                    f"({category}): {said!r}"
                )
        return self


class LeadForm(_Frozen):
    asset_id: UUID
    headline: str = Field(min_length=1)
    description: str = Field(min_length=1)
    #: One of `extras.lead_form_cta_types`.
    cta: str = Field(min_length=1)
    questions: list[LeadFormQuestion] = Field(min_length=1)
    privacy_policy_url: str = Field(min_length=1)
    privacy_url_check: UrlCheckRef
    lint: LintRef

    @model_validator(mode="after")
    def _privacy_resolves(self) -> LeadForm:
        if self.privacy_url_check.status != "ok":
            raise ValueError(
                f"privacy_policy_url must resolve 2xx on-domain; {self.privacy_policy_url} is "
                f"{self.privacy_url_check.status}"
            )
        return self


class LeadFormTradeoff(_Frozen):
    """`leadform.field_tradeoff_v1`'s chosen point, and the calculation behind it."""

    fields_n: int = Field(ge=1)
    expected_leads: float = Field(ge=0)
    expected_qualified: float = Field(ge=0)
    #: `calc_evidence_ids`, not §11's `calc_evidence_id`, so the executor
    #: checks it is a `derived` row this node produced (Stage 02 §9.1 item 4).
    calc_evidence_ids: list[UUID] = Field(min_length=1)


class CampaignLeadForm(_Frozen):
    campaign_ref: str = Field(min_length=1)
    #: The frozen plan's type; "" when it names none, and then no spec applies.
    campaign_type: str
    form: LeadForm | None = None
    #: None when there is no history to weigh (the gap says which input); the
    #: form then asks every required signal — the lead definition itself.
    tradeoff: LeadFormTradeoff | None = None
    gaps: list[ExtraGap] = Field(default_factory=list)
    exception_candidates: list[ExceptionCandidate] = Field(default_factory=list)

    @model_validator(mode="after")
    def _counts_match(self) -> CampaignLeadForm:
        if (
            self.form is not None
            and self.tradeoff is not None
            and len(self.form.questions) != self.tradeoff.fields_n
        ):
            raise ValueError(
                f"the form asks {len(self.form.questions)} question(s), but the trade-off chose "
                f"fields_n={self.tradeoff.fields_n}"
            )
        return self


class LeadFormOutput(_Frozen):
    schema_version: Literal["1.0"] = EXTRAS_SCHEMA_VERSION
    #: `not_required` unless a campaign objective is `lead_gen`.
    status: Literal["not_required", "spec_missing", "required"]
    why: str = Field(min_length=1)
    campaigns: list[CampaignLeadForm] = Field(default_factory=list)

    @model_validator(mode="after")
    def _status_matches(self) -> LeadFormOutput:
        if self.status != "required" and any(c.form is not None for c in self.campaigns):
            raise ValueError(f"a {self.status} output writes no lead form")
        return self
