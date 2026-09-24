"""A `CreativeInput` and a pinned `RuleSet` to write briefs from, in memory."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from agent.guardrails.matchers.claims import claim_licence
from agent.guidelines.constants import load_content_constants
from agent.schemas.creative_input import CreativeInput
from agent.schemas.guardrails import Authority, ClaimRef, Rule, RuleSet

RUN_ID = uuid.UUID(int=41)
EVIDENCE_ICP = uuid.UUID(int=101)
EVIDENCE_GONE = uuid.UUID(int=102)
LICENSED = uuid.UUID(int=201)
DRAFT = uuid.UUID(int=202)
EXPIRED = uuid.UUID(int=203)
NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)


def creative_input(**overrides: Any) -> CreativeInput:
    fields: dict[str, Any] = {
        "project_id": str(uuid.UUID(int=1)),
        "creative_run_id": str(RUN_ID),
        "plan_ref": {
            "plan_id": str(uuid.UUID(int=2)),
            "version": 1,
            "schema_version": "1.0",
            "plan_run_id": str(uuid.UUID(int=3)),
        },
        "research_ref": {
            "research_run_id": str(uuid.UUID(int=4)),
            "report_id": str(uuid.UUID(int=5)),
            "acceptance_id": str(uuid.UUID(int=6)),
        },
        "ruleset_ref": {
            "guideline_id": str(uuid.UUID(int=7)),
            "ruleset_version": "1.0+aa",
            "hash": "aa",
        },
        "context_ref": {"hash": "cc"},
        "scope": {"campaign_refs": [], "images": False, "video": False, "concepts_per_campaign": 2},
        "media_models": [],
        "audience": {
            "best_customers": [
                {
                    "label": "EHS managers at chemical distributors",
                    "jobs_to_be_done": ["keep every SDS current"],
                    "evidence_ids": [str(EVIDENCE_ICP), str(EVIDENCE_GONE)],
                }
            ],
            "not_wanted": [
                {"persona": "students", "disqualifier": "no buying authority", "evidence_ids": []}
            ],
        },
        "differentiation": {"recommended_claim": "SDS updates within 24 hours"},
        "competitor_messages": [{"theme": "compliance"}],
        "objectives": {
            "north_star_metric": "qualified leads",
            "campaign_objectives": [
                {"campaign_ref": "c-sds", "objective": "lead_gen", "primary_kpi": "cost per lead"}
            ],
        },
        "lead_definition": {"required_signals": ["company email"]},
        "channel_slate": {},
        "account_structure": {
            "campaigns": [
                {
                    "name": "Search - SDS",
                    "campaign_ref": "c-sds",
                    "ad_groups": [
                        {
                            "name": "sds software",
                            "theme": "SDS management",
                            "landing_url": "https://example.com/sds",
                            "primary_message": "Keep every SDS current",
                            "keywords": [
                                {"term": "sds software", "search_volume": 900},
                                {"term": "sds app", "search_volume": 100},
                                {"term": "msds tool", "search_volume": 300},
                                {"term": "sds binder", "search_volume": 50},
                            ],
                        }
                    ],
                }
            ]
        },
        "naming": None,
        "creative_context": {
            "guideline_id": str(uuid.UUID(int=7)),
            "guideline_version": "1.0",
            "ruleset_version": "1.0+aa",
            "voice": {"voice_words": ["plain", "exact"]},
            "lexicon_guidance": {
                "always": [{"term": "safety data sheet"}],
                "never": [{"term": "guaranteed"}],
            },
            "visual_identity": {
                "colour": {"tokens": [{"name": "brand-orange", "hex": "#C2410C"}]},
                "imagery": {
                    "permitted_subjects": ["warehouses"],
                    "forbidden_subjects": ["children"],
                },
            },
            "disclosure_rules": [{"disclosure_id": "ai", "required_text": "Made with AI"}],
            "competitor_rules": {"allowed": False},
            "personalization_rules": {},
            "hash": "cc",
        },
        "offer_records": [],
        "offer_snapshot_at": NOW.isoformat(),
        "references": [],
        "signoff_matrix": {
            "matrix_id": str(uuid.UUID(int=8)),
            "version": 1,
            "brand_owner_id": str(uuid.UUID(int=9)),
            "legal_owner_id": str(uuid.UUID(int=10)),
            "performance_owner_id": str(uuid.UUID(int=11)),
        },
        "constants_version": "2026.09.1",
    }
    fields.update(overrides)
    return CreativeInput.model_validate(fields)


def ruleset() -> RuleSet:
    return RuleSet(
        ruleset_version="1.0+aa",
        project_id=uuid.UUID(int=1),
        guideline_id=uuid.UUID(int=7),
        compiler_version="test",
        constants_version="test",
        compiled_at=NOW,
        claims_index=(
            ClaimRef(
                claim_id=LICENSED, normalized_text="sds updates within 24 hours", status="approved"
            ),
            ClaimRef(claim_id=DRAFT, normalized_text="the fastest sds tool", status="draft"),
            ClaimRef(
                claim_id=EXPIRED,
                normalized_text="rated best by users",
                status="approved",
                expires_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
        ),
        hash="aa",
    )


LEGAL = Authority(source="legal_signature", reference="sig-7f3a", reviewed_at=NOW.date())


def claims_ruleset(*, rules: tuple[Rule, ...] = (), claims: tuple[ClaimRef, ...] = ()) -> RuleSet:
    """`ruleset()` with Stage 03's real claim-licence rule and the shipped detectors.

    The detectors are `content_constants.yaml`'s, not invented ones: a family
    that stopped matching real copy must fail here, not in production.
    """
    constants = load_content_constants()
    detectors = constants.detectors()
    licence = claim_licence(
        tuple(d.detector_id for d in detectors if d.locale == "en"),
        authority=LEGAL,
        match_threshold=float(constants.value("claims.match_threshold")),
    )
    base = ruleset()
    return base.model_copy(
        update={
            "rules": (licence, *rules),
            "detectors": tuple(detectors),
            "claims_index": (*base.claims_index, *claims),
        }
    )


ESTIMATE: dict[str, Any] = {
    "jobs": {"image": 0, "video": 0},
    "media_usd": "0.00",
    "total_usd": "3.00",
    "confidence": "low",
    "fits": True,
}
