"""`CreativeContext` — Stage 03's one read-only projection for Stage 04 (PRD §4.3).

Law 27 forbids Stage 04 from reading `ContentGuideline.payload`, yet the voice
words, do/don't examples, colour tokens and imagery rules a copywriter needs
live only there — the `RuleSet` holds matchers, not guidance. This module is
the one sanctioned door, and it keeps Law 27's intent in three ways:

* **Never a draft.** Only a guideline that was published — `published` now or
  `superseded` since — is projected. A pin into anything else is "not found".
* **Never unpinned.** The projection is of exactly one `ruleset_version`, and
  the ruleset row is what names the guideline, so a pin cannot be paired with
  the wrong rulebook.
* **Guidance only.** Nothing here is a verdict. Enforcement stays exclusively
  in the `RuleSet` and `guardrails.linter.lint()`.

It is deterministic and has no clock: the same pin projects to the same object
and the same `hash` in any process, which is what lets `CreativeInput` pin
`CreativeContextRef.hash` and have it mean something a year later.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import ContentGuideline, GuidelineStatus, RuleSet
from agent.export.guideline_contract import ContentGuideline as GuidelineContract
from agent.schemas.creative_input import (
    CompetitorRules,
    CreativeContext,
    LexiconGuidance,
    PersonalizationRules,
)

#: A guideline in either of these states was published at some point, so its
#: payload is frozen by trigger `content_guideline_published` and safe to
#: project. `superseded` is included because a pin is historical by nature:
#: "pin= returns the historical projection" (§23 item 5).
PROJECTABLE: frozenset[GuidelineStatus] = frozenset(
    {GuidelineStatus.PUBLISHED, GuidelineStatus.SUPERSEDED}
)


class ProjectionNotFound(LookupError):
    """Nothing published at that pin. The route turns this into a 404."""


async def build_creative_context(
    db: AsyncSession,
    guideline_id: uuid.UUID,
    ruleset_version: str,
    *,
    workspace_id: uuid.UUID,
) -> CreativeContext:
    """The `CreativeContext` of one published guideline at exactly one pin."""
    ruleset = (
        await db.execute(
            sa.select(RuleSet).where(
                RuleSet.workspace_id == workspace_id,
                RuleSet.guideline_id == guideline_id,
                RuleSet.ruleset_version == ruleset_version,
            )
        )
    ).scalar_one_or_none()
    if ruleset is None:
        raise ProjectionNotFound(
            f"No ruleset {ruleset_version!r} was compiled from guideline {guideline_id}."
        )
    guideline = (
        await db.execute(
            sa.select(ContentGuideline).where(
                ContentGuideline.workspace_id == workspace_id,
                ContentGuideline.id == guideline_id,
            )
        )
    ).scalar_one_or_none()
    if guideline is None or guideline.status not in PROJECTABLE or not guideline.payload:
        raise ProjectionNotFound(
            f"Guideline {guideline_id} has never been published, so it has no creative "
            "context. Stage 04 reads published rulebooks only."
        )
    return project(guideline, ruleset)


def project(guideline: ContentGuideline, ruleset: RuleSet) -> CreativeContext:
    """The pure half: a published row and its ruleset in, a hashed context out."""
    payload = GuidelineContract.model_validate(guideline.payload)
    brand = payload.brand_rules
    policy = payload.policy_profile
    fields = {
        "guideline_id": guideline.id,
        "guideline_version": f"{guideline.version_major}.{guideline.version_minor}",
        "ruleset_version": ruleset.ruleset_version,
        "voice": brand.voice,
        "lexicon_guidance": LexiconGuidance(
            always=brand.lexicon.always,
            never=brand.lexicon.never,
            case_and_spelling=brand.lexicon.case_and_spelling,
        ),
        "visual_identity": brand.visual_identity,
        # The policy profile's list, which the synthesis also flattens into
        # `disclosure_requirements`. Read from the section a writer reads.
        "disclosure_rules": policy.disclosure_rules,
        "competitor_rules": CompetitorRules(**policy.competitor_mentions),
        "personalization_rules": PersonalizationRules(**policy.personalization),
    }
    draft = CreativeContext.model_validate({**fields, "hash": "pending"})
    return draft.model_copy(update={"hash": draft.computed_hash()})
