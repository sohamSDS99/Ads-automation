"""The GDPR Article 9 blocklist (Stage 02 PRD §13 "Sensitive categories", Stage 04 §11 4.3.3).

Every Art. 9(1) category is caught by at least one ordinary phrasing; the
phrases an EHS buyer's form legitimately carries ("health and safety manager",
"European Union") are not.
"""

from __future__ import annotations

import pytest

from agent.planning.sensitive_categories import ARTICLE_9, article_9_category

CATEGORIES = {
    "racial_or_ethnic_origin",
    "political_opinions",
    "religious_or_philosophical_beliefs",
    "trade_union_membership",
    "genetic_data",
    "biometric_data",
    "health",
    "sex_life_or_sexual_orientation",
}


def test_the_list_is_article_9_1_and_nothing_else() -> None:
    assert set(ARTICLE_9) == CATEGORIES
    assert all(ARTICLE_9[category] for category in CATEGORIES)


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("What is your ethnicity?", "racial_or_ethnic_origin"),
        ("Race", "racial_or_ethnic_origin"),
        ("Which political party do you support?", "political_opinions"),
        ("Your religion", "religious_or_philosophical_beliefs"),
        ("Do you attend church?", "religious_or_philosophical_beliefs"),
        ("Are you a trade union member?", "trade_union_membership"),
        ("Labor union membership", "trade_union_membership"),
        ("Have you taken a DNA test?", "genetic_data"),
        ("Upload a fingerprint", "biometric_data"),
        ("Do you have a disability?", "health"),
        ("Any medical conditions?", "health"),
        ("Describe your health", "health"),
        ("Are you pregnant?", "health"),
        ("What is your sexual orientation?", "sex_life_or_sexual_orientation"),
        ("LGBTQ", "sex_life_or_sexual_orientation"),
    ],
)
def test_each_category_is_caught(text: str, category: str) -> None:
    assert article_9_category(text) == category


@pytest.mark.parametrize(
    "text",
    [
        "Are you the health and safety manager?",
        "Health & safety team size",
        "Environment, health and safety (EHS) lead",
        "Which European Union countries do you ship to?",
        "Company size",
        "How many safety data sheets do you manage?",
        "Job title",
        "Racing team sponsorship",
        "Preferred contact method",
        "",
    ],
)
def test_ordinary_b2b_questions_pass(text: str) -> None:
    assert article_9_category(text) is None


def test_a_health_word_beside_the_exempt_phrase_is_still_caught() -> None:
    assert article_9_category("Health and safety lead: any medical conditions?") == "health"
