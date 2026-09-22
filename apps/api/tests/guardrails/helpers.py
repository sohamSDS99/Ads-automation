"""Shared builders for the guardrails suite.

Everything here builds *arguments*. The whole point of `guardrails/` is that it
reads nothing for itself, so a test needs no database, no network and no clock
— it needs a `LintContext` with the values spelled out, which is what makes the
expectations in these tests hand-checkable.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID

from agent.guardrails.normalize import normalize
from agent.guardrails.registry import LintContext
from agent.schemas.guardrails import (
    Authority,
    ClaimRef,
    LintTarget,
    OfferRecord,
    RuleSet,
)

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
PROJECT_ID = UUID("00000000-0000-0000-0000-0000000000a1")
GUIDELINE_ID = UUID("00000000-0000-0000-0000-0000000000b2")

BRAND = Authority(source="brand", reference="brand_book#p3", reviewed_at=date(2026, 9, 22))
LEGAL = Authority(source="legal_signature", reference="sig-7f3a", reviewed_at=date(2026, 9, 22))
GOOGLE = Authority(
    source="google_policy",
    reference="https://support.google.com/adspolicy",
    reviewed_at=date(2026, 9, 22),
)
INTERNAL = Authority(
    source="internal", reference="content_constants.yaml#claims", reviewed_at=date(2026, 9, 22)
)


def target(
    text: str | None = None,
    *,
    ref: str = "t1",
    surface: str = "rsa_headline",
    campaign_type: str = "search",
    market: str = "DE",
    language: str = "en",
    **extra: object,
) -> LintTarget:
    return LintTarget.model_validate(
        {
            "ref": ref,
            "surface": surface,
            "campaign_type": campaign_type,
            "market": market,
            "language": language,
            "text": text,
            **extra,
        }
    )


def ruleset(**overrides: object) -> RuleSet:
    payload: dict[str, object] = {
        "ruleset_version": "1.0+deadbeef",
        "project_id": PROJECT_ID,
        "guideline_id": GUIDELINE_ID,
        "compiler_version": "guardrails/1.0",
        "constants_version": "2026.09.1",
        "compiled_at": NOW,
        "hash": "deadbeef",
    }
    payload.update(overrides)
    return RuleSet.model_validate(payload)


def context(
    targets: list[LintTarget],
    *,
    now: datetime = NOW,
    claims: tuple[ClaimRef, ...] = (),
    detectors: tuple[object, ...] = (),
    offers: tuple[OfferRecord, ...] = (),
    **ruleset_overrides: object,
) -> LintContext:
    built = ruleset(claims_index=claims, detectors=detectors, **ruleset_overrides)
    return LintContext(
        ruleset=built,
        now=now,
        targets=tuple(targets),
        normalized={item.ref: normalize(item.text or "", locale=item.language) for item in targets},
        offers=offers,
    )
