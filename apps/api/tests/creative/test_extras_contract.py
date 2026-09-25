"""Stage 4.3 contracts — what an extra may and may not say (PRD §11 4.3, §12.2, laws 33–35).

* a sitelink in the output is on-domain, 2xx and unique in its campaign;
* **a model-written digit in a promotion or price field fails the schema** —
  in any script — and every number and date there is an `OfferBinding` field
  reference whose rendered value is what the asset shows;
* a lead form carries a resolving privacy URL and no question targeting a
  GDPR Art. 9 category;
* the three new lint surfaces reach the spec sheet as their own asset types.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from agent.schemas.creative_brief import OfferBinding
from agent.schemas.extras import (
    CampaignExtras,
    CampaignLeadForm,
    LeadForm,
    LeadFormQuestion,
    OfferAssetsOutput,
    PriceAsset,
    PriceItem,
    Promotion,
    Sitelink,
    SitelinksCalloutsSnippetsOutput,
)
from agent.schemas.guardrails import SURFACE_ASSET_TYPES, LintTarget

LINT = {"verdict": "pass", "ruleset_version": "1.0+aa", "rule_ids": []}
RECORD = uuid.UUID(int=77)
OK = {
    "status": "ok",
    "final_url_after_redirects": "https://example.com/pricing",
    "http_status": 200,
}


# ---------------------------------------------------------------------------
# the Stage 03 surface delta (§7.1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("surface", ["promotion", "price", "lead_form"])
def test_the_new_surfaces_are_their_own_asset_types(surface: str) -> None:
    # Unmapped, every asset-typed spec rule would apply to them at once.
    assert SURFACE_ASSET_TYPES[surface] == surface
    LintTarget(ref="x", surface=surface, campaign_type="search", market="US", language="en")


# ---------------------------------------------------------------------------
# 4.3.1 — sitelinks
# ---------------------------------------------------------------------------


def _sitelink(**overrides: Any) -> dict[str, Any]:
    return {
        "asset_id": str(uuid.uuid4()),
        "link_text": "See pricing",
        "line1": "Plans for every team",
        "line2": "Compare the tiers",
        "final_url": "https://example.com/pricing",
        "url_check": OK,
        "lint": LINT,
        **overrides,
    }


def _campaign(**overrides: Any) -> dict[str, Any]:
    return {
        "campaign_ref": "c-sds-us",
        "campaign_type": "search",
        "market": "US",
        "language": "en",
        **overrides,
    }


def test_a_sitelink_that_passed_its_url_check_is_valid() -> None:
    Sitelink.model_validate(_sitelink())


@pytest.mark.parametrize(
    "status", ["off_domain", "http_error", "unreachable", "too_many_redirects", "duplicate"]
)
def test_a_sitelink_whose_url_failed_is_never_a_sitelink(status: str) -> None:
    with pytest.raises(ValidationError, match="on-domain, 2xx"):
        Sitelink.model_validate(_sitelink(url_check={**OK, "status": status}))


def test_two_sitelinks_landing_on_one_page_fail_the_campaign() -> None:
    other = {**OK, "final_url_after_redirects": "https://www.example.com/pricing/"}
    with pytest.raises(ValidationError, match="unique per campaign"):
        CampaignExtras.model_validate(
            _campaign(
                sitelinks=[
                    _sitelink(),
                    _sitelink(final_url="https://example.com/plans", url_check=other),
                ]
            )
        )


def test_spec_missing_writes_nothing() -> None:
    with pytest.raises(ValidationError, match="spec_missing"):
        SitelinksCalloutsSnippetsOutput.model_validate(
            {
                "status": "spec_missing",
                "why": "no specs",
                "campaigns": [_campaign(sitelinks=[_sitelink()])],
            }
        )


# ---------------------------------------------------------------------------
# 4.3.2 — promotions and prices: law 35
# ---------------------------------------------------------------------------


def _binding(fields: dict[str, str], resolved: dict[str, str]) -> dict[str, Any]:
    return {
        "offer_record_id": str(RECORD),
        "sku_or_set": "sds-pro",
        "fields": fields,
        "resolved": resolved,
    }


def _promotion(**overrides: Any) -> dict[str, Any]:
    return {
        "asset_id": str(uuid.uuid4()),
        "campaign_ref": "c-sds-us",
        "discount_kind": "percent_off",
        "bound": {"percent_off": "20", "currency": "USD"},
        "start": "2026-09-01T00:00:00+00:00",
        "end": "2026-10-12T23:59:00+00:00",
        "final_url": "https://example.com/sds",
        "text": "SDS management software",
        "offer_binding": _binding(
            {
                "percent_off": "percent_off",
                "currency": "currency",
                "start": "effective_from",
                "end": "ends_at",
            },
            {
                "percent_off": "20",
                "currency": "USD",
                "start": "2026-09-01T00:00:00+00:00",
                "end": "2026-10-12T23:59:00+00:00",
            },
        ),
        "lint": LINT,
        **overrides,
    }


def test_a_bound_promotion_is_valid() -> None:
    Promotion.model_validate(_promotion())


@pytest.mark.parametrize(
    "text",
    [
        "20% off SDS software",
        "SDS software 2026",
        "Save on SDS tools ٢٠",  # Arabic-Indic digits
        "Save on SDS tools ２０",  # fullwidth digits
    ],
)
def test_a_model_written_digit_in_a_promotion_fails_the_schema(text: str) -> None:
    with pytest.raises(ValidationError):
        Promotion.model_validate(_promotion(text=text))


def test_a_figure_that_is_not_the_bound_value_fails() -> None:
    with pytest.raises(ValidationError, match="OfferBinding"):
        Promotion.model_validate(_promotion(bound={"percent_off": "25", "currency": "USD"}))


def test_a_date_that_is_not_the_bound_value_fails() -> None:
    with pytest.raises(ValidationError, match="OfferBinding"):
        Promotion.model_validate(_promotion(end="2026-12-31T00:00:00+00:00"))


def test_a_date_with_no_binding_fails() -> None:
    binding = _promotion()["offer_binding"]
    del binding["fields"]["end"], binding["resolved"]["end"]
    with pytest.raises(ValidationError, match="OfferBinding"):
        Promotion.model_validate(_promotion(offer_binding=binding))


def test_a_binding_to_a_field_the_offer_record_does_not_have_fails() -> None:
    binding = _promotion()["offer_binding"]
    binding["fields"]["percent_off"] = "whatever_the_model_said"
    with pytest.raises(ValidationError, match="OfferBinding"):
        Promotion.model_validate(_promotion(offer_binding=binding))


def test_a_promotion_cannot_carry_both_discount_kinds() -> None:
    with pytest.raises(ValidationError):
        Promotion.model_validate(
            _promotion(bound={"percent_off": "20", "money_off": "10.00", "currency": "USD"})
        )


def _item(**overrides: Any) -> dict[str, Any]:
    return {
        "asset_id": str(uuid.uuid4()),
        "header": "Professional plan",
        "description": "For growing EHS teams",
        "bound": {"price": "129.00", "currency": "USD"},
        "final_url": "https://example.com/sds",
        "offer_binding": _binding(
            {"price": "current_price", "currency": "currency"},
            {"price": "129.00", "currency": "USD"},
        ),
        "lint": LINT,
        **overrides,
    }


def test_a_bound_price_item_is_valid() -> None:
    PriceItem.model_validate(_item())


@pytest.mark.parametrize("field", ["header", "description"])
def test_a_model_written_digit_in_a_price_fails_the_schema(field: str) -> None:
    with pytest.raises(ValidationError):
        PriceItem.model_validate(_item(**{field: "Plan for 10 users"}))


def test_a_price_that_is_not_the_bound_value_fails() -> None:
    with pytest.raises(ValidationError, match="OfferBinding"):
        PriceItem.model_validate(_item(bound={"price": "99.00", "currency": "USD"}))


def test_one_offer_record_is_one_price_item() -> None:
    with pytest.raises(ValidationError, match="offer record"):
        PriceAsset.model_validate(
            {
                "price_asset_id": str(uuid.uuid4()),
                "campaign_ref": "c-sds-us",
                "type": "SERVICE_TIERS",
                "items": [_item(), _item()],
            }
        )


@pytest.mark.parametrize("status", ["not_required", "spec_missing"])
def test_an_offer_output_that_writes_nothing_writes_nothing(status: str) -> None:
    with pytest.raises(ValidationError, match="writes no"):
        OfferAssetsOutput.model_validate(
            {
                "status": status,
                "why": "stale",
                "offers_fresh": 0,
                "offers_stale": 1,
                "promotions": [_promotion()],
            }
        )


# ---------------------------------------------------------------------------
# 4.3.3 — lead form
# ---------------------------------------------------------------------------


def _form(**overrides: Any) -> dict[str, Any]:
    return {
        "asset_id": str(uuid.uuid4()),
        "headline": "Get an SDS demo",
        "description": "Tell us about your team and we will be in touch.",
        "cta": "REQUEST_DEMO",
        "questions": [
            {"type": "WORK_EMAIL"},
            {"type": "JOB_TITLE", "qualifies_signal": "job title"},
        ],
        "privacy_policy_url": "https://example.com/privacy",
        "privacy_url_check": {**OK, "final_url_after_redirects": "https://example.com/privacy"},
        "lint": LINT,
        **overrides,
    }


def test_a_lead_form_with_a_resolving_privacy_url_is_valid() -> None:
    LeadForm.model_validate(_form())


@pytest.mark.parametrize("status", ["off_domain", "http_error", "unreachable"])
def test_a_lead_form_whose_privacy_url_does_not_resolve_fails(status: str) -> None:
    with pytest.raises(ValidationError, match="privacy"):
        LeadForm.model_validate(
            _form(privacy_url_check={"status": status, "final_url_after_redirects": None})
        )


@pytest.mark.parametrize(
    "question",
    [
        {"type": "custom", "text": "Do you have a disability?"},
        {"type": "custom", "text": "Team size", "options": ["1-10", "Trade union member"]},
        {"type": "custom", "text": "Budget", "qualifies_signal": "religion"},
    ],
)
def test_a_question_targeting_an_article_9_category_fails(question: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="Article 9"):
        LeadFormQuestion.model_validate(question)


def test_a_custom_question_has_text() -> None:
    with pytest.raises(ValidationError, match="custom"):
        LeadFormQuestion.model_validate({"type": "custom"})


def test_the_tradeoff_counts_the_form_it_chose() -> None:
    with pytest.raises(ValidationError, match="fields_n"):
        CampaignLeadForm.model_validate(
            {
                "campaign_ref": "c-sds-us",
                "campaign_type": "search",
                "form": _form(),
                "tradeoff": {
                    "fields_n": 3,
                    "expected_leads": 90.0,
                    "expected_qualified": 60.0,
                    "calc_evidence_ids": [str(uuid.uuid4())],
                },
            }
        )


def test_a_tradeoff_cites_its_calculation() -> None:
    with pytest.raises(ValidationError):
        CampaignLeadForm.model_validate(
            {
                "campaign_ref": "c-sds-us",
                "campaign_type": "search",
                "form": _form(),
                "tradeoff": {
                    "fields_n": 2,
                    "expected_leads": 90.0,
                    "expected_qualified": 60.0,
                    "calc_evidence_ids": [],
                },
            }
        )


def test_offer_binding_is_the_brief_contract() -> None:
    # §12.2 defines one OfferBinding; the brief and the extras share it.
    assert Promotion.model_fields["offer_binding"].annotation is OfferBinding


def test_a_campaign_the_plan_gave_no_type_is_still_reported() -> None:
    # `PlannedCampaign.type` defaults to "": no spec applies, and the gap says so.
    gap = {
        "campaign_ref": "c-sds-us",
        "asset_type": "sitelink",
        "reason": "spec_missing",
        "detail": "the frozen plan names no campaign type for c-sds-us, so no spec applies",
    }
    CampaignExtras.model_validate(_campaign(campaign_type="", gaps=[gap]))
    CampaignLeadForm.model_validate(
        {
            "campaign_ref": "c-sds-us",
            "campaign_type": "",
            "gaps": [{**gap, "asset_type": "lead_form"}],
        }
    )
