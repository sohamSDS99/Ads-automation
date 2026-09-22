"""One golden ruleset and one target corpus, rebuildable from nothing.

This module is imported by the compiler and linter tests *and* executed as a
script by the two-process determinism tests:

    python -m tests.guardrails.fixture compile   -> the ruleset hash
    python -m tests.guardrails.fixture lint      -> the findings, as JSON

Everything here is a literal. No clock, no database, no randomness — the whole
value of the fixture is that a second process, started fresh and with a
different hash seed, builds byte-identically the same thing. If it needed a
running stack to reproduce, it could not prove what it is here to prove.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, date, datetime
from uuid import UUID

from agent.guardrails.compiler import compile as compile_ruleset
from agent.guardrails.linter import lint
from agent.guardrails.matchers.assets import (
    allowed_value,
    asset_count,
    asset_length,
    sentence_length,
)
from agent.guardrails.matchers.claims import claim_licence
from agent.guardrails.matchers.disclosure import ai_generated
from agent.guardrails.matchers.image import text_coverage
from agent.guardrails.matchers.lexicon import (
    banned_term,
    disapproval_construction,
    required_term,
    review_trigger,
)
from agent.guardrails.matchers.offers import countdown, from_price, percent_off
from agent.guidelines.constants import load_content_constants
from agent.schemas.guardrails import (
    Authority,
    ClaimRef,
    LintTarget,
    OfferRecord,
    RuleScope,
)

PROJECT_ID = UUID("3f2504e0-4f89-11d3-9a0c-0305e82c3301")
GUIDELINE_ID = UUID("3f2504e0-4f89-11d3-9a0c-0305e82c3302")
CLAIM_ID = UUID("3f2504e0-4f89-11d3-9a0c-0305e82c3303")
LOGO_ID = UUID("3f2504e0-4f89-11d3-9a0c-0305e82c3304")

#: Fixed, because a compiled_at read from a clock would be the one thing in the
#: fixture that differed between the two processes.
COMPILED_AT = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)
NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
REVIEWED = date(2026, 9, 22)

BRAND = Authority(source="brand", reference="brand_book#p12", reviewed_at=REVIEWED)
LEGAL = Authority(source="legal_signature", reference="sig-7f3a", reviewed_at=REVIEWED)
GOOGLE = Authority(
    source="google_policy",
    reference="https://support.google.com/adspolicy/answer/6008942",
    reviewed_at=REVIEWED,
)
INTERNAL = Authority(
    source="internal", reference="content_constants.yaml#image_policy", reviewed_at=REVIEWED
)
LEARNED = Authority(source="learned_disapproval", reference="disapproval-114", reviewed_at=REVIEWED)

CONSTANTS = load_content_constants()
EN_DETECTORS = tuple(d.detector_id for d in CONSTANTS.detectors() if d.locale == "en")


def golden_rules() -> list:
    """Sixteen rules spanning every one of the nine matcher kinds."""
    return [
        banned_term(("cheap", "hassle"), authority=BRAND, message="Off-brand vocabulary."),
        banned_term(("solution",), authority=BRAND, message="Say what it is.", severity="advisory"),
        required_term(
            ("safety data sheet", "sds"),
            authority=BRAND,
            message="Name the product on first use.",
            scope=RuleScope(surfaces=("rsa_description",)),
        ),
        claim_licence(EN_DETECTORS, authority=LEGAL, match_threshold=0.88),
        review_trigger(r"\bcompetitor\b", authority=BRAND, message="Legal reviews comparisons."),
        disapproval_construction(
            r"\bmiracle\b", authority=LEARNED, message="This construction was disapproved."
        ),
        asset_length(
            authority=GOOGLE,
            message="Search headlines are 30 characters.",
            maximum=30,
            scope=RuleScope(surfaces=("rsa_headline",)),
        ),
        asset_length(
            authority=GOOGLE,
            message="Search descriptions are 90 characters.",
            maximum=90,
            scope=RuleScope(surfaces=("rsa_description",)),
        ),
        asset_count(
            "rsa_headline",
            authority=GOOGLE,
            message="Search needs at least three headlines.",
            minimum=3,
        ),
        sentence_length(
            authority=BRAND,
            message="Keep headlines to eight words.",
            maximum=8,
            surfaces=("rsa_headline",),
        ),
        from_price(authority=GOOGLE, product_set="software"),
        percent_off(authority=GOOGLE),
        countdown(authority=GOOGLE),
        text_coverage(authority=INTERNAL, maximum=0.20),
        allowed_value(
            "market",
            ("DE", "AT", "CH"),
            authority=INTERNAL,
            message="Not launched in that market.",
        ),
        ai_generated(authority=GOOGLE, required_text="AI-generated"),
    ]


def golden_payload() -> dict:
    return {
        "project_id": str(PROJECT_ID),
        "guideline_id": str(GUIDELINE_ID),
        "version_major": 2,
        "version_minor": 3,
        "rules": golden_rules(),
        "disclosure_requirements": [
            {
                "disclosure_id": "disclosure.ai.v1",
                "surfaces": ["rsa_headline", "rsa_description"],
                "required_text": "AI-generated",
                "placement": "anywhere",
            }
        ],
        "logo_templates": [
            {"asset_id": str(LOGO_ID), "label": "primary", "phash": "f0e1d2c3", "min_score": 0.62}
        ],
    }


def golden_claims() -> list[ClaimRef]:
    return [
        ClaimRef(
            claim_id=CLAIM_ID,
            normalized_text="the leading sds management platform",
            surface_forms=("leading sds platform",),
            status="approved",
            market_scope=("DE",),
            languages=("en",),
            expires_at=datetime(2027, 6, 1, tzinfo=UTC),
        )
    ]


def golden_offers() -> list[OfferRecord]:
    return [
        OfferRecord(
            sku="sds-pro",
            product_set="software",
            list_price=99.0,
            current_price=49.0,
            reference_price=99.0,
            currency="EUR",
            market="DE",
            ends_at=datetime(2026, 12, 31, 23, 59, tzinfo=UTC),
            observed_at=datetime(2026, 9, 21, tzinfo=UTC),
        )
    ]


#: Deliberately varied: clean copy, banned terms, unlicensed and licensed
#: claims, over-length headlines, offer constructions, an image and generated
#: copy. A corpus of a hundred identical strings would prove nothing about
#: ordering.
SAMPLES: list[tuple[str, str | None]] = [
    ("rsa_headline", "Manage safety data sheets"),
    ("rsa_headline", "The best SDS software"),
    ("rsa_headline", "The leading SDS platform"),
    ("rsa_headline", "A cheap way to handle compliance paperwork today"),
    ("rsa_headline", "Beat every competitor"),
    ("rsa_headline", "SDS software from €39"),
    ("rsa_headline", "60% off this week"),
    ("rsa_headline", "Offer ends soon"),
    ("rsa_headline", "A miracle for compliance"),
    ("rsa_description", "Our solution removes the hassle of chemical compliance."),
    ("rsa_description", "Upload a document and let the safety data sheet library do the rest."),
    ("rsa_description", "Guaranteed compliance for every chemical in your inventory, forever."),
    ("rsa_path", "sds-software"),
    ("callout", "ISO 9001 certified"),
    ("sitelink", "Trusted by 500 companies"),
]


def golden_targets(count: int = 100) -> list[LintTarget]:
    """`count` targets, cycling the samples so the corpus stays varied."""
    targets = []
    for index in range(count):
        surface, text = SAMPLES[index % len(SAMPLES)]
        targets.append(
            LintTarget(
                ref=f"t{index:03d}",
                surface=surface,  # type: ignore[arg-type]
                campaign_type="search",
                market="DE",
                language="en",
                text=text,
                generated_by_ai=index % 7 == 0,
            )
        )
    targets.append(
        LintTarget(
            ref="img-000",
            surface="rsa_headline",
            campaign_type="search",
            market="DE",
            language="en",
            image_ref="s3://creative/hero.png",
            image_metrics={"text_coverage_ratio": 0.31, "logo_match_score": 0.71},
        )
    )
    return targets


def golden_ruleset():
    return compile_ruleset(golden_payload(), CONSTANTS, golden_claims(), compiled_at=COMPILED_AT)


def golden_result(count: int = 100):
    return lint(golden_targets(count), golden_ruleset(), now=NOW, offers=golden_offers())


def main() -> int:
    """`compile` prints the hash; `lint` prints the findings as canonical JSON."""
    mode = sys.argv[1] if len(sys.argv) > 1 else "compile"
    if mode == "compile":
        ruleset = golden_ruleset()
        print(json.dumps({"hash": ruleset.hash, "version": ruleset.ruleset_version}))
        return 0
    result = golden_result()
    payload = result.model_dump(mode="json")
    # The one field that legitimately differs between two runs of identical
    # input, and the only thing the determinism test is allowed to exclude.
    payload.pop("elapsed_ms")
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
