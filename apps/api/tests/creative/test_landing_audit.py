"""`creative/landing_audit.py` on real renders of the three fixture pages (S4-P7).

The exit criteria, at the level of the judgement rather than the node:

- a mismatched H1 scores below `landing.message_match_min`;
- an offer below the fold is detected per device;
- a nine-field form reduces to the minimal set code computes.

The node tests (`tests/integration/test_s4p7_landing.py`) run the same pages
through 4.5.1 and 4.5.2 with a real pin, so the proposed H1 is linted there.
"""

from __future__ import annotations

from datetime import UTC, datetime
from fractions import Fraction

import pytest
import pytest_asyncio

from agent.creative import landing_audit as audit
from agent.creative.constants import load_creative_constants
from agent.preview import landing
from agent.preview.landing import LandingRender
from agent.schemas.creative_brief import OfferBinding
from agent.schemas.guardrails import LintResult
from agent.schemas.landing import FormField, OfferAboveFold
from tests.landing_support import MISMATCHED_H1, NINE_FIELD_FORM, OFFER_BELOW_FOLD, fixture_server

THRESHOLD = load_creative_constants().landing.message_match_min.value
SDS = audit.AdGroupHeadlines(
    campaign_ref="c-sds-us",
    ad_group_ref="sds software",
    headlines=("SDS Management Software", "Keep Every SDS Current", "Book A Demo Today"),
)
OFFER = "Get 20% off the first year"
#: What CLASSIFY answers for the nine-field form in these tests.
LABELS = {
    "first_name": "none",
    "last_name": "none",
    "email": "contact_email",
    "phone": "contact_phone",
    "company": "none",
    "job_title": "job title",
    "company_size": "company size",
    "country": "none",
    "consent": "consent",
}
REQUIRED = ["job title", "company size"]


@pytest_asyncio.fixture(scope="module")
async def renders() -> dict[str, LandingRender]:
    constants = load_creative_constants().landing
    viewports = {
        "mobile": landing.parse_viewport(constants.viewport_mobile.value),
        "desktop": landing.parse_viewport(constants.viewport_desktop.value),
    }
    names = (MISMATCHED_H1, OFFER_BELOW_FOLD, NINE_FIELD_FORM)
    with fixture_server() as server:
        pages = await landing.render_pages([server.url(n) for n in names], viewports=viewports)
    return dict(zip(names, pages, strict=True))


def _lint(verdict: str) -> LintResult:
    return LintResult(
        ruleset_version="2026.09.1",
        verdict=verdict,  # type: ignore[arg-type]
        targets_checked=1,
        rules_evaluated=3,
        elapsed_ms=0,
        evaluated_at=datetime(2026, 9, 25, tzinfo=UTC),
    )


# --- message match -------------------------------------------------------------


def test_the_threshold_is_the_shipped_constant() -> None:
    assert THRESHOLD == 0.55


def test_a_mismatched_h1_scores_below_the_threshold_on_both_devices(
    renders: dict[str, LandingRender],
) -> None:
    match = audit.page_match(renders[MISMATCHED_H1], [SDS], THRESHOLD)
    assert match.verdict == "fail"
    assert match.score is not None and match.score < Fraction("0.55")
    assert {score.device for score in match.scores} == {"mobile", "desktop"}


def test_an_h1_that_echoes_the_ad_passes(renders: dict[str, LandingRender]) -> None:
    # "SDS Management Software for Chemical Safety" repeats a headline whole.
    match = audit.page_match(renders[OFFER_BELOW_FOLD], [SDS], THRESHOLD)
    assert match.verdict == "pass" and match.score == 1
    assert {score.best_headline for score in match.scores} == {"SDS Management Software"}


def test_the_page_scores_its_weakest_ad_group(renders: dict[str, LandingRender]) -> None:
    other = audit.AdGroupHeadlines("c-sds-us", "chemicals", ("Industrial Chemical Supplies",))
    match = audit.page_match(renders[OFFER_BELOW_FOLD], [SDS, other], THRESHOLD)
    by_group = {(s.ad_group_ref, s.device): s.score for s in match.scores}
    assert by_group[("sds software", "mobile")] == 1
    assert match.score is not None
    assert float(match.score) == min(s.score for s in match.scores) < 1
    assert match.verdict == "fail"


def test_an_unrendered_page_has_no_match_rather_than_a_zero() -> None:
    dead = landing.DeviceRender(
        device="mobile",
        viewport=landing.Viewport(width=390, height=844),
        user_agent="ua",
        reached=False,
        error="net::ERR_CONNECTION_REFUSED",
    )
    page = LandingRender(
        url="http://127.0.0.1:9/x",
        mobile=dead,
        desktop=dead.model_copy(update={"device": "desktop"}),
    )
    match = audit.page_match(page, [SDS], THRESHOLD)
    assert (match.score, match.verdict, match.scores) == (None, "unavailable", ())


def test_exactly_the_threshold_passes() -> None:
    assert audit.at_least(Fraction(11, 20), 0.55)
    assert not audit.at_least(Fraction(11, 20) - Fraction(1, 10**9), 0.55)


def test_the_proposed_h1_passes_lint_and_the_threshold_or_is_not_proposed() -> None:
    fits = audit.H1Candidate("SDS Management Software", Fraction(1), _lint("pass"))
    warned = audit.H1Candidate(
        "SDS Management Software Demo", Fraction(1), _lint("pass_with_warnings")
    )
    failing = audit.H1Candidate("The #1 SDS Management Software", Fraction(1), _lint("fail"))
    weak = audit.H1Candidate("Welcome to Acme", Fraction(0), _lint("pass"))
    assert audit.choose_h1([failing, fits, warned], THRESHOLD) is fits  # tie: model order
    assert audit.choose_h1([failing, warned], THRESHOLD) is warned
    assert audit.choose_h1([failing, weak], THRESHOLD) is None


def test_a_proposal_is_scored_against_every_ad_group() -> None:
    other = audit.AdGroupHeadlines("c", "chemicals", ("Industrial Chemical Supplies",))
    assert audit.proposal_score([SDS], "SDS Management Software") == 1
    assert audit.proposal_score([SDS, other], "SDS Management Software") < 1


# --- offer above the fold ------------------------------------------------------


def test_an_offer_below_the_fold_is_detected_per_device(renders: dict[str, LandingRender]) -> None:
    page = renders[OFFER_BELOW_FOLD]
    mobile, desktop = audit.offer_above_fold(page, OFFER)
    assert (mobile.device, mobile.found) == ("mobile", False)
    assert (desktop.device, desktop.found) == ("desktop", True)
    # The fold overlay can still draw the box: below the fold on mobile, above on desktop.
    assert mobile.bbox is not None and mobile.bbox.y >= page.mobile.fold_px
    assert desktop.bbox is not None and desktop.bbox.y < page.desktop.fold_px


def test_an_offer_the_page_never_shows_is_missing_everywhere(
    renders: dict[str, LandingRender],
) -> None:
    folds = audit.offer_above_fold(renders[MISMATCHED_H1], OFFER)
    assert [(f.found, f.bbox) for f in folds] == [(False, None), (False, None)]


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("Get 20% off the first year", True),
        ("get 20 OFF the first year!", True),
        ("Get 120% off the first years", False),
        ("Get 20% off", False),
    ],
)
def test_the_offer_is_a_normalised_exact_phrase(text: str, found: bool) -> None:
    assert audit.contains_phrase(text, OFFER) is found


def test_the_offer_phrase_is_the_bindings_first_rendered_value() -> None:
    binding = OfferBinding.model_validate(
        {
            "offer_record_id": "9b0f3c1e-8f1e-4c1e-9d59-7c3f7d1c2a10",
            "sku_or_set": "pro",
            "fields": {"percent_off": "percent_off", "ends_at": "ends_at"},
            "resolved": {"percent_off": " 20% off ", "ends_at": "12 Oct 2026"},
        }
    )
    assert audit.offer_phrase(binding) == "20% off"
    assert audit.offer_phrase(None) is None


# --- the form and its minimal set ----------------------------------------------


def test_the_audited_form_is_the_lead_form_with_nine_fields(
    renders: dict[str, LandingRender],
) -> None:
    form_index, fields = audit.form_fields(renders[NINE_FIELD_FORM].desktop)
    assert form_index == 0
    assert [f.name for f in fields] == list(LABELS)
    assert len(fields) == 9


def test_a_nine_field_form_reduces_to_the_computed_minimal_set(
    renders: dict[str, LandingRender],
) -> None:
    form_index, fields = audit.form_fields(renders[NINE_FIELD_FORM].desktop)
    form = audit.minimal_set(form_index, audit.with_signals(fields, LABELS), REQUIRED)
    assert form.minimal_set == ["email", "job_title", "company_size", "consent"]
    assert form.remove == ["first_name", "last_name", "phone", "company", "country"]
    assert [(k.field, k.reason, k.signal) for k in form.keep_reason] == [
        ("email", "routing_contact", "contact_email"),
        ("job_title", "required_signal", "job title"),
        ("company_size", "required_signal", "company size"),
        ("consent", "consent", "consent"),
    ]
    assert form.missing_signals == []
    assert {f.name: f.mapped_signal for f in form.fields}["first_name"] is None


def test_the_model_labels_fields_and_cannot_keep_one() -> None:
    fields = [
        FormField(name="email", type="email", required=True, mapped_signal="contact_email"),
        FormField(name="phone", type="tel", required=False, mapped_signal="contact_phone"),
        FormField(name="notes", type="textarea", required=False, mapped_signal="budget"),
    ]
    # "budget" is not a required signal of this plan, so it is not kept, and
    # only ONE contact is the routing contact.
    form = audit.minimal_set(0, fields, ["company size"])
    assert form.minimal_set == ["email"]
    assert form.remove == ["phone", "notes"]
    assert form.missing_signals == ["company size"]


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        # a required phone beats an optional email: the site already routes by it
        ((("email", "contact_email", False), ("phone", "contact_phone", True)), "phone"),
        # both optional: email before phone
        ((("phone", "contact_phone", False), ("email", "contact_email", False)), "email"),
        # two emails: document order
        ((("work", "contact_email", True), ("home", "contact_email", True)), "work"),
        ((("name", None, True),), None),
    ],
)
def test_the_routing_contact_is_one_field(
    fields: tuple[tuple[str, str | None, bool], ...], expected: str | None
) -> None:
    built = [
        FormField(name=name, type="text", required=required, mapped_signal=signal)
        for name, signal, required in fields
    ]
    chosen = audit.routing_contact(built)
    assert (chosen.name if chosen else None) == expected


def test_the_vocabulary_is_the_plans_signals_then_the_reserved_labels() -> None:
    assert audit.signal_vocabulary([" company size ", "consent", "company size"]) == (
        "company size",
        "contact_email",
        "contact_phone",
        "consent",
        "privacy",
        "none",
    )


# --- the patch and the verdict -------------------------------------------------


def _form(renders: dict[str, LandingRender]):  # type: ignore[no-untyped-def]
    form_index, fields = audit.form_fields(renders[NINE_FIELD_FORM].desktop)
    return audit.minimal_set(form_index, audit.with_signals(fields, LABELS), REQUIRED)


def test_the_patch_carries_the_h1_the_offer_block_and_the_removals(
    renders: dict[str, LandingRender],
) -> None:
    offer = audit.offer_above_fold(renders[OFFER_BELOW_FOLD], OFFER)
    patch = audit.build_patch(
        url="https://example.com/sds--demo",
        h1="SDS Management Software <script>alert(1)</script>",
        offer=offer,
        form=_form(renders),
    )
    assert patch is not None
    assert patch.h1 == "SDS Management Software <script>alert(1)</script>"
    assert patch.offer_block is not None
    assert (patch.offer_block.phrase, patch.offer_block.devices) == (OFFER, ["mobile"])
    assert patch.remove_fields == ["first_name", "last_name", "phone", "company", "country"]
    snippet = patch.html_snippet
    assert "<h1>SDS Management Software &lt;script&gt;alert(1)&lt;/script&gt;</h1>" in snippet
    assert "<script>" not in snippet
    assert f"<p>{OFFER}</p>" in snippet
    assert "- last_name (Last name)" in snippet
    # A URL with "--" cannot end the comment early.
    assert "sds- -demo" in snippet
    assert all(line.startswith(("<!-- ", "<h1>", "<p>")) for line in snippet.splitlines())


def test_nothing_to_change_is_no_patch() -> None:
    assert audit.build_patch(url="u", h1=None, offer=[], form=None) is None


def _fold(device: str, found: bool) -> OfferAboveFold:
    return OfferAboveFold(phrase=OFFER, found=found, device=device)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("reachable", "match", "offer", "remove", "expected"),
    [
        (False, "unavailable", [], [], "unreachable"),
        (True, "pass", [_fold("mobile", False), _fold("desktop", True)], [], "blocking_for_launch"),
        (True, "fail", [], [], "needs_change"),
        (True, "pass", [], ["phone"], "needs_change"),
        (True, "pass", [_fold("mobile", True), _fold("desktop", True)], [], "ok"),
        (True, "pass", [], [], "ok"),
    ],
)
def test_only_unreachable_and_a_missing_offer_block_launch(
    reachable: bool, match: str, offer: list[OfferAboveFold], remove: list[str], expected: str
) -> None:
    form = audit.minimal_set(
        0,
        [
            FormField(name="email", type="email", required=True, mapped_signal="contact_email"),
            *(FormField(name=n, type="text", required=False) for n in remove),
        ],
        [],
    )
    verdict, reasons = audit.verdict(
        reachable=reachable,
        match=match,  # type: ignore[arg-type]
        match_score=0.2 if match == "fail" else 1.0,
        threshold=THRESHOLD,
        offer=offer,
        form=form,
    )
    assert verdict == expected
    assert bool(reasons) is (expected != "ok")
