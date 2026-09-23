"""One assembled rulebook, and the knobs a test needs to break it.

Shared by the synthesis, critique and export suites. A fixture builder rather
than a stored JSON file: the three suites assert on different *properties* of
the same object, and a frozen blob would drift from the contract silently the
first time a section grew a field.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from agent.export.guideline_contract import ContentGuideline
from agent.guidelines.constants import load_content_constants
from agent.guidelines.synthesis import GuidelineFacts, assemble

#: Fixed, so two builds of the same fixture are the same object. A clock here
#: would make every export-determinism assertion a coin flip.
GENERATED_AT = datetime(2026, 9, 23, 12, 0, 0, tzinfo=UTC)

PROJECT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b0")
RUN_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c0")
GUIDELINE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000d0")
LEGAL_OWNER = uuid.UUID("00000000-0000-0000-0000-0000000000e1")
BRAND_OWNER = uuid.UUID("00000000-0000-0000-0000-0000000000e2")
PERF_OWNER = uuid.UUID("00000000-0000-0000-0000-0000000000e3")


def node_outputs(**overrides: Any) -> dict[str, dict[str, Any]]:
    """A complete, healthy set of upstream outputs."""
    outputs: dict[str, dict[str, Any]] = {
        "3.1.1": {
            "voice_words": ["clear", "direct"],
            "definition_per_word": {"clear": "Say the thing.", "direct": "No hedging."},
            "do_examples": [
                {"text": "Find your SDS in seconds.", "source_ref": "ad-1", "why": "Concrete."}
            ],
            "dont_examples": [
                {"text": "Leverage synergies.", "rewritten_as": "Work together.", "why": "Jargon."}
            ],
            "voice_register": {"formality": "plain", "person": "second", "tense": "present"},
            "readability_targets": {"max_sentence_words": 20},
            "input_mode": "bound",
        },
        "3.1.2": {
            "never": [
                {
                    "term": "cheap",
                    "surface_forms": ["cheapest"],
                    "reason": "We do not compete on price.",
                    "severity": "warning",
                    "suggested_replacement": "affordable",
                }
            ],
            "always": [{"term": "Safety Data Sheet", "context": "Spell it out on first use."}],
            "case_and_spelling": [],
            "conflicts": [],
        },
        "3.1.3": {
            "logo": {"min_width_px": 120, "clear_space_ratio": 0.25},
            "colour": {"tokens": "brand-orange #C2410C"},
            "imagery": {"stock_policy": "no stock photography of people"},
            "extraction_confidence": 0.82,
        },
        "3.2.1": {"candidates": [], "detector_recall_note": "two surfaces read"},
        "3.2.2": {"claims": [], "unsupported_count": 0, "expiry_basis": "qualitative"},
        "3.2.4": {
            "rules": [
                {
                    "id": "o1",
                    "construction": "from_price",
                    "requirement": "A from-price must match the cheapest live offer.",
                    "data_binding": {"field": "price", "tolerance": 0.0},
                    "severity": "blocking",
                }
            ],
            "live_violations": [],
        },
        "3.3.1": {
            "applicable": [
                {
                    "area": "healthcare",
                    "policy_ref": "https://support.google.com/adspolicy/answer/176031",
                    "why_applicable": "We sell safety documentation to clinical sites.",
                    "markets": ["GB"],
                    "obligations": ["No implied medical outcome"],
                    "no_rule_needed": "Covered by the claims register's licence rule.",
                }
            ],
            "not_applicable": [{"area": "gambling", "why_not": "We do not sell it."}],
            "requires_verification": [],
            "open_interpretation": [],
        },
        "3.3.2": {"status": "not_required", "blocking_for": "launch"},
        "3.3.3": {"competitor_mentions": {}, "personalization": {}},
        "3.3.4": {
            "disclosure_rules": [
                {
                    "disclosure_id": "d1",
                    "surfaces": ["rsa_headline", "rsa_description"],
                    "markets": ["GB"],
                    "required_text": "AI-generated",
                    "placement": "suffix",
                }
            ],
            "internal_policy_addendum": "",
        },
        "3.4.1": {"scope": "unscoped"},
        "3.4.2": {
            "minimums": [
                {
                    "campaign_type": "search",
                    "required_assets": [{"asset_type": "headline", "count": 3}],
                    "blocking_for_launch": True,
                }
            ],
            "readiness_checklist": [],
        },
        "3.4.3": {"rules": [], "logo_templates": []},
        "3.5.1": {
            "owners": {
                "brand_owner_id": str(BRAND_OWNER),
                "legal_owner_id": str(LEGAL_OWNER),
                "performance_owner_id": str(PERF_OWNER),
            },
            "rationale": "The only approver in this workspace.",
            "reused": False,
        },
        "3.5.2": {
            "triggers": [
                {
                    "id": "t1",
                    "pattern_kind": "term",
                    "pattern": "clinically proven",
                    "why": "A health claim",
                    "reviewer_role": "approver",
                    "severity": "blocking",
                }
            ],
            "always_review": [],
        },
    }
    for node_id, value in overrides.items():
        key = node_id.replace("_", ".")
        outputs[key] = value
    return outputs


def build(
    *,
    outputs: dict[str, dict[str, Any]] | None = None,
    claims: Any = (),
    decisions: Any = (),
    signatures: Any = (),
    human_tasks: Any = (),
    summary: str = "This rulebook covers voice, claims, policy and asset specs.",
    version_major: int = 1,
    unbound: list[str] | None = None,
) -> ContentGuideline:
    """One assembled rulebook."""
    constants = load_content_constants()
    facts = GuidelineFacts(
        project_id=PROJECT_ID,
        guideline_run_id=RUN_ID,
        guideline_id=GUIDELINE_ID,
        version_major=version_major,
        version_minor=0,
        mode="standalone",
        bindings={},
        unbound_inputs=unbound or [],
        degraded_sources=[],
        constants_version=constants.version,
        generated_at=GENERATED_AT,
    )
    return assemble(
        outputs if outputs is not None else node_outputs(),
        facts,
        constants=constants,
        claims=claims,
        decisions=decisions,
        signatures=signatures,
        human_tasks=human_tasks,
        executive_summary=summary,
    ).guideline
