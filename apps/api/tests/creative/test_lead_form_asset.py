"""4.3.3 `lead_form_asset` — the parts that decide (PRD §11 4.3.3, §13 lead forms).

The node's database, URL-check, calculation and model work is proved in
`tests/integration/test_s4p8_extras.py`. Here:

* `not_required` unless a campaign in scope has a `lead_gen` objective;
* the privacy policy URL is found among the project's own crawled pages;
* a required signal in a GDPR Art. 9 category is never asked, and a question
  the model words into one withholds the form;
* the form asks the routing contact and the chosen signals — nothing outside
  `required_signals` ∪ routing contact (§13);
* the history the trade-off is anchored on is read from 4.5.2's audit.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from agent.export.plan_contract import Objectives
from agent.nodes.creative.n4_3_3_lead_form_asset import (
    assemble_questions,
    crm_aggregates,
    draft_model,
    form_history,
    lead_gen_campaigns,
    privacy_candidates,
    screen_questions,
    screen_signals,
)
from agent.schemas.landing import FormAudit, FormField

DOMAIN = "sdsmanager.com"
TYPES = ("EMAIL", "JOB_TITLE", "COMPANY_SIZE")
CTAS = ("REQUEST_DEMO", "LEARN_MORE")


def _objectives(*pairs: tuple[str, str]) -> Objectives:
    return Objectives.model_validate(
        {
            "campaign_objectives": [
                {"campaign_ref": ref, "objective": objective, "primary_kpi": "cpl"}
                for ref, objective in pairs
            ]
        }
    )


def test_only_lead_gen_campaigns_in_scope_get_a_form() -> None:
    objectives = _objectives(
        ("c-sds-us", "lead_gen"), ("c-brand", "awareness"), ("c-x", "lead_gen")
    )
    assert lead_gen_campaigns(objectives, ["c-sds-us", "c-brand"]) == ["c-sds-us"]
    assert lead_gen_campaigns(_objectives(("c-brand", "awareness")), ["c-brand"]) == []


def test_the_objective_is_matched_as_the_plan_writes_it() -> None:
    assert lead_gen_campaigns(_objectives(("c-1", " Lead_Gen ")), ["c-1"]) == ["c-1"]


def test_the_privacy_policy_is_one_of_our_own_pages() -> None:
    found = [
        ("https://sdsmanager.com/pricing", "Pricing"),
        ("https://sdsmanager.com/legal/privacy-policy", "Privacy policy"),
        ("https://sdsmanager.com/privacy", "Privacy"),
        ("https://partner.net/privacy", "Partner privacy"),
        ("https://sdsmanager.com/legal", "Privacy and cookies"),
    ]
    assert privacy_candidates(found, domain=DOMAIN) == [
        "https://sdsmanager.com/legal",
        "https://sdsmanager.com/privacy",
        "https://sdsmanager.com/legal/privacy-policy",
    ]


def test_an_article_9_signal_is_never_asked() -> None:
    allowed, blocked = screen_signals(["job title", "health condition", "company size"])
    assert allowed == ["job title", "company size"]
    assert blocked == [("health condition", "health")]


def test_the_history_is_the_audited_form() -> None:
    audit = FormAudit(
        form_index=0,
        fields=[
            FormField(name="first_name", type="text", required=True, mapped_signal=None),
            FormField(name="email", type="email", required=True, mapped_signal="contact_email"),
            FormField(name="phone", type="tel", required=False, mapped_signal="contact_phone"),
            FormField(name="job_title", type="text", required=False, mapped_signal="job title"),
        ],
    )
    assert form_history(audit, ["job title", "company size"]) == (4, 1, "EMAIL")


def test_a_phone_only_form_routes_by_phone() -> None:
    audit = FormAudit(
        form_index=0,
        fields=[FormField(name="tel", type="tel", required=True, mapped_signal="contact_phone")],
    )
    assert form_history(audit, ["job title"]) == (1, 0, "PHONE_NUMBER")


def test_crm_aggregates_count_rows_and_reasons() -> None:
    won, lost = crm_aggregates(
        [{"account_name": "a"}, {"account_name": "b"}],
        [{"close_reason": "Student"}, {"close_reason": " Student"}, {"close_reason": None}],
    )
    assert won == 2
    assert lost == {"Student": 2, "": 1}


def _draft(**overrides: Any) -> dict[str, Any]:
    return {
        "headline": "Get an SDS demo",
        "description": "Tell us about your team.",
        "cta": "REQUEST_DEMO",
        "questions": {
            "signal_1": {"type": "JOB_TITLE", "text": None, "options": []},
            "signal_2": {"type": "custom", "text": "How many sites do you run?", "options": []},
        },
        **overrides,
    }


def test_the_draft_offers_only_googles_question_types_and_ctas() -> None:
    model = draft_model(["signal_1", "signal_2"], question_types=TYPES, cta_types=CTAS)
    model.model_validate(_draft())
    with pytest.raises(ValidationError):
        model.model_validate(_draft(cta="BUY_NOW"))
    bad = _draft()
    bad["questions"]["signal_1"]["type"] = "RELIGION"
    with pytest.raises(ValidationError):
        model.model_validate(bad)
    # The model does not choose what a question qualifies: code does.
    extra = _draft()
    extra["questions"]["signal_1"]["qualifies_signal"] = "budget"
    with pytest.raises(ValidationError):
        model.model_validate(extra)


def test_the_form_is_the_contact_and_the_chosen_signals_nothing_else() -> None:
    model = draft_model(["signal_1", "signal_2"], question_types=TYPES, cta_types=CTAS)
    draft = model.model_validate(_draft())
    questions = assemble_questions("EMAIL", ["job title", "company size"], draft)
    assert questions == [
        {"type": "EMAIL", "text": None, "options": [], "qualifies_signal": None},
        {"type": "JOB_TITLE", "text": None, "options": [], "qualifies_signal": "job title"},
        {
            "type": "custom",
            "text": "How many sites do you run?",
            "options": [],
            "qualifies_signal": "company size",
        },
    ]


@pytest.mark.parametrize(
    "question",
    [
        {"type": "custom", "text": "Do you have a disability?", "options": []},
        {"type": "custom", "text": "Team size", "options": ["Union member", "Trade union member"]},
    ],
)
def test_a_question_worded_into_article_9_is_found(question: dict[str, Any]) -> None:
    questions = [{"type": "EMAIL", "text": None, "options": [], "qualifies_signal": None}]
    questions.append({**question, "qualifies_signal": "company size"})
    found = screen_questions(questions)
    assert found is not None and found[0] in ("health", "trade_union_membership")


def test_ordinary_questions_pass_the_screen() -> None:
    questions = [
        {"type": "EMAIL", "text": None, "options": [], "qualifies_signal": None},
        {
            "type": "custom",
            "text": "Health and safety team size",
            "options": ["1-5"],
            "qualifies_signal": "x",
        },
    ]
    assert screen_questions(questions) is None
