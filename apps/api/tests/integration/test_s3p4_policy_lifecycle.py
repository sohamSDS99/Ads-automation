"""S3-P4's exit criteria (PRD §21), executed against a real Postgres.

> 3.3.1 emits both `applicable` and `not_applicable` with reasons; a changed
> fixture policy page opens an amendment with the right class; a `mechanical`
> amendment auto-applies and mints a MINOR; a `signature_affecting` amendment
> voids the right signatures and re-queues the claims; 3.3.2 emits
> `not_required` and creates no task when nothing is required.

Clauses 1 and 5 are node behaviour and live in `tests/test_guideline_nodes_3_3.py`.
The three in the middle are this file, and they need a real database for a
reason that is not incidental: two of the three things being proved are
*enforced by the database*. `content_guideline_published_guard` freezes
`version_minor` on a published row, and `claim_signature` is append-only. A
fake session would have let the first implementation of `mint_minor` pass
while raising `restrict_violation` in production at 04:00.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import (
    AmendmentChangeKind,
    AmendmentOrigin,
    AmendmentStatus,
    AuditLog,
    ClaimRecord,
    ClaimSignature,
    ClaimStatus,
    ContentGuideline,
    Evidence,
    GuidelineStatus,
    HumanTask,
    HumanTaskStatus,
    PolicyAmendment,
    PolicySource,
    RuleSet,
    SignatureMethod,
)
from agent.guidelines.constants import get_content_constants
from agent.policy import lifecycle, watcher
from agent.policy.classifier import Classification
from tests.integration.claims_support import cast, seed_register, user_id_of
from tests.integration.conftest import ApiClient

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 23, 4, 0, tzinfo=UTC)

BEFORE = (
    "<html><body><nav>Help Center</nav><article><h1>Trademarks</h1>"
    "<p>Ad text may include a trademark in up to 30 characters.</p>"
    "<p>Complaints are owner-initiated.</p></article></body></html>"
)
#: One number changed inside one existing rule. §8.6's definition of mechanical.
AFTER_MECHANICAL = BEFORE.replace("30 characters", "40 characters")


async def publish(db: AsyncSession, guideline_id: uuid.UUID) -> ContentGuideline:
    """Move a seeded guideline to `published` with a minimal compilable payload."""
    guideline = (
        await db.execute(sa.select(ContentGuideline).where(ContentGuideline.id == guideline_id))
    ).scalar_one()
    guideline.payload = {
        "project_id": str(guideline.project_id),
        "guideline_id": str(guideline.id),
        "version_major": 1,
        "version_minor": 0,
        "rules": [],
    }
    guideline.status = GuidelineStatus.PUBLISHED
    guideline.published_at = NOW
    await db.commit()
    await db.refresh(guideline)
    return guideline


async def a_source(db: AsyncSession, workspace_id: uuid.UUID, **kw) -> PolicySource:
    source = PolicySource(
        workspace_id=workspace_id,
        url=kw.get("url", "https://support.google.com/adspolicy/answer/6118?hl=en"),
        label="Trademarks",
        area="trademark",
        selector="article",
        enabled=True,
        last_hash=kw.get("last_hash"),
    )
    db.add(source)
    await db.commit()
    await db.refresh(source)
    return source


def classification(kind: AmendmentChangeKind, **kw) -> Classification:
    return Classification(
        change_kind=kind,
        confidence=kw.get("confidence", 0.95),
        rationale=kw.get("rationale", "a character limit moved from 30 to 40"),
        affected_claim_ids=kw.get("affected_claim_ids", ()),
        proposed_rule_changes=(),
        below_floor=kw.get("below_floor", False),
        escalated=kw.get("escalated", False),
    )


# ---------------------------------------------------------------------------
# "a changed fixture policy page opens an amendment with the right class"
# ---------------------------------------------------------------------------


async def test_an_unchanged_page_opens_nothing(
    admin: ApiClient, project_id: uuid.UUID, db: AsyncSession
) -> None:
    """The half that protects the inbox.

    Measured against the live site: hashing the response body would report a
    change on every fetch. This asserts the watcher does not.
    """
    await cast(admin)
    guideline_id, _claims = await seed_register(admin, db, project_id)
    guideline = await publish(db, guideline_id)
    source = await a_source(db, guideline.workspace_id)

    first = await watcher.check_source(db, source, html=BEFORE)
    assert first.outcome == "first_seen"
    second = await watcher.check_source(db, source, html=BEFORE)
    assert second.outcome == "unchanged"

    opened = (await db.execute(sa.select(sa.func.count()).select_from(PolicyAmendment))).scalar()
    assert opened == 0


async def test_a_changed_page_opens_an_amendment_with_the_diff(
    admin: ApiClient, project_id: uuid.UUID, db: AsyncSession
) -> None:
    await cast(admin)
    guideline_id, _claims = await seed_register(admin, db, project_id)
    guideline = await publish(db, guideline_id)
    source = await a_source(db, guideline.workspace_id)

    await watcher.check_source(db, source, html=BEFORE)
    result = await watcher.check_source(db, source, html=AFTER_MECHANICAL)
    await db.commit()

    assert result.outcome == "changed"
    amendment = (await db.execute(sa.select(PolicyAmendment))).scalars().one()
    assert amendment.origin is AmendmentOrigin.POLICY_WATCH
    assert amendment.status is AmendmentStatus.OPEN
    # Opened unclassified, on purpose: classification is a separate call with
    # its own failure mode, and §8.6 resolves unclassified to a human.
    assert amendment.change_kind is AmendmentChangeKind.UNCLASSIFIED
    assert amendment.diff["added"] >= 1
    assert "40 characters" in "\n".join(amendment.diff["unified"])


async def test_a_stale_selector_reports_stale_rather_than_unchanged(
    admin: ApiClient, project_id: uuid.UUID, db: AsyncSession
) -> None:
    """An unmatched selector hashes the empty string, stably, forever."""
    await cast(admin)
    guideline_id, _claims = await seed_register(admin, db, project_id)
    guideline = await publish(db, guideline_id)
    source = await a_source(db, guideline.workspace_id)
    source.selector = "main"
    await db.commit()

    result = await watcher.check_source(db, source, html=BEFORE)

    assert result.outcome == "stale"
    assert "matched no element" in (result.detail or "")


async def test_a_project_with_nothing_published_gets_no_amendment(
    admin: ApiClient, project_id: uuid.UUID, db: AsyncSession
) -> None:
    """A policy change is only an amendment to something that exists."""
    await cast(admin)
    guideline_id, _claims = await seed_register(admin, db, project_id)
    guideline = (
        await db.execute(sa.select(ContentGuideline).where(ContentGuideline.id == guideline_id))
    ).scalar_one()
    source = await a_source(db, guideline.workspace_id)

    await watcher.check_source(db, source, html=BEFORE)
    await watcher.check_source(db, source, html=AFTER_MECHANICAL)
    await db.commit()

    assert (await db.execute(sa.select(sa.func.count()).select_from(PolicyAmendment))).scalar() == 0


# ---------------------------------------------------------------------------
# "a `mechanical` amendment auto-applies and mints a MINOR"
# ---------------------------------------------------------------------------


async def test_a_mechanical_amendment_auto_applies_and_mints_a_minor(
    admin: ApiClient, project_id: uuid.UUID, db: AsyncSession
) -> None:
    await cast(admin)
    guideline_id, _claims = await seed_register(admin, db, project_id)
    guideline = await publish(db, guideline_id)
    source = await a_source(db, guideline.workspace_id)
    await watcher.check_source(db, source, html=BEFORE)
    await watcher.check_source(db, source, html=AFTER_MECHANICAL)
    await db.commit()
    amendment = (await db.execute(sa.select(PolicyAmendment))).scalars().one()

    outcome = await lifecycle.apply(
        db,
        amendment,
        classification(AmendmentChangeKind.MECHANICAL),
        constants=get_content_constants(),
        now=NOW,
    )
    await db.commit()

    assert outcome.status is AmendmentStatus.AUTO_APPLIED
    assert outcome.ruleset_version is not None
    # The MINOR moved: 1.0 was never compiled, so the first mint is 1.1.
    assert outcome.ruleset_version.startswith("1.1+")

    ruleset = (
        await db.execute(sa.select(RuleSet).where(RuleSet.id == outcome.ruleset_id))
    ).scalar_one()
    assert ruleset.guideline_id == guideline.id
    assert (
        await db.execute(
            sa.select(PolicyAmendment.status).where(PolicyAmendment.id == amendment.id)
        )
    ).scalar() is AmendmentStatus.AUTO_APPLIED


async def test_minting_never_writes_the_frozen_guideline_row(
    admin: ApiClient, project_id: uuid.UUID, db: AsyncSession
) -> None:
    """The bug this design exists to avoid.

    `content_guideline_published_guard` raises `restrict_violation` when a
    published row changes `version_minor`. An implementation that bumped it
    there would pass every unit test and fail in production on the first
    mechanical amendment.
    """
    await cast(admin)
    guideline_id, _claims = await seed_register(admin, db, project_id)
    guideline = await publish(db, guideline_id)
    source = await a_source(db, guideline.workspace_id)
    await watcher.check_source(db, source, html=BEFORE)
    await watcher.check_source(db, source, html=AFTER_MECHANICAL)
    await db.commit()
    amendment = (await db.execute(sa.select(PolicyAmendment))).scalars().one()

    await lifecycle.apply(db, amendment, classification(AmendmentChangeKind.MECHANICAL), now=NOW)
    await db.commit()
    await db.refresh(guideline)

    assert guideline.version_minor == 0  # untouched, and it has to be
    assert guideline.status is GuidelineStatus.PUBLISHED


async def test_two_mechanical_amendments_mint_successive_minors(
    admin: ApiClient, project_id: uuid.UUID, db: AsyncSession
) -> None:
    """The MINOR is read back from `rule_set`, so it keeps climbing."""
    await cast(admin)
    guideline_id, _claims = await seed_register(admin, db, project_id)
    guideline = await publish(db, guideline_id)
    source = await a_source(db, guideline.workspace_id)

    versions = []
    for index, html in enumerate((AFTER_MECHANICAL, BEFORE)):
        await watcher.check_source(db, source, html=BEFORE if index == 0 else AFTER_MECHANICAL)
        await watcher.check_source(db, source, html=html)
        await db.commit()
        amendment = (
            (
                await db.execute(
                    sa.select(PolicyAmendment)
                    .where(PolicyAmendment.status == AmendmentStatus.OPEN)
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if amendment is None:
            continue
        # A claim is added between the two so the compiled bytes differ and the
        # global `uq_rule_set_hash` does not collapse them into one row.
        db.add(
            ClaimRecord(
                workspace_id=guideline.workspace_id,
                project_id=project_id,
                first_seen_guideline_id=guideline.id,
                claim_text=f"Claim {index}",
                normalized_text=f"claim {index}",
                surface_forms=[f"claim {index}"],
                claim_type="superlative",
                market_scope=["DE"],
                languages=["en"],
                observed_on=[],
                evidence_ids=[],
                status=ClaimStatus.UNSUPPORTED,
            )
        )
        await db.flush()
        outcome = await lifecycle.apply(
            db, amendment, classification(AmendmentChangeKind.MECHANICAL), now=NOW
        )
        await db.commit()
        versions.append(outcome.ruleset_version)

    assert versions[0].startswith("1.1+")
    assert versions[1].startswith("1.2+")


# ---------------------------------------------------------------------------
# "a `signature_affecting` amendment voids the right signatures
#  and re-queues the claims"
# ---------------------------------------------------------------------------


async def sign(
    db: AsyncSession,
    *,
    guideline: ContentGuideline,
    claims: list[ClaimRecord],
    signer_id: uuid.UUID,
) -> ClaimSignature:
    """Put a live signature over `claims` and license them."""
    signature = ClaimSignature(
        workspace_id=guideline.workspace_id,
        project_id=guideline.project_id,
        signer_id=signer_id,
        claim_ids=[claim.id for claim in claims],
        set_hash=uuid.uuid4().hex,
        decisions=[{"claim_id": str(claim.id), "decision": "approved"} for claim in claims],
        statement="I confirm these claims are safe to run.",
        method=SignatureMethod.STEP_UP_PASSWORD,
        reauth_token_id=uuid.uuid4().hex,
        signed_at=NOW - timedelta(days=1),
    )
    db.add(signature)
    await db.flush()
    for claim in claims:
        claim.status = ClaimStatus.APPROVED
        claim.current_signature_id = signature.id
    await db.commit()
    await db.refresh(signature)
    return signature


async def test_a_signature_affecting_amendment_voids_and_requeues(
    admin: ApiClient, project_id: uuid.UUID, db: AsyncSession
) -> None:
    await cast(admin)
    guideline_id, claims = await seed_register(admin, db, project_id)
    guideline = await publish(db, guideline_id)
    legal_id = await user_id_of(db, "legal@example.com")
    touched = claims[:2]
    signature = await sign(db, guideline=guideline, claims=claims, signer_id=legal_id)

    source = await a_source(db, guideline.workspace_id)
    await watcher.check_source(db, source, html=BEFORE)
    await watcher.check_source(db, source, html=AFTER_MECHANICAL)
    await db.commit()
    amendment = (await db.execute(sa.select(PolicyAmendment))).scalars().one()

    outcome = await lifecycle.apply(
        db,
        amendment,
        classification(
            AmendmentChangeKind.SIGNATURE_AFFECTING,
            affected_claim_ids=tuple(claim.id for claim in touched),
            rationale="the substantiation standard for this claim type changed",
        ),
        now=NOW,
    )
    await db.commit()

    # 1. the signature is voided, not deleted — it is append-only
    await db.refresh(signature)
    assert signature.voided_at is not None
    assert str(amendment.id) in (signature.void_reason or "")
    assert outcome.voided_signature_ids == (signature.id,)

    # 2. the touched claims are un-licensed and back in front of the owner
    for claim in touched:
        await db.refresh(claim)
        assert claim.status is ClaimStatus.PENDING_SIGNOFF
        assert claim.current_signature_id is None

    # 3. an H1 is open for the named legal owner, and nobody else
    task = (
        (await db.execute(sa.select(HumanTask).where(HumanTask.task_key == "H1"))).scalars().one()
    )
    assert task.assignee_id == legal_id
    assert task.status is HumanTaskStatus.PENDING
    assert outcome.human_task_id == task.id

    # 4. the published version says its signature is stale
    await db.refresh(guideline)
    assert guideline.signature_stale is True

    # 5. it never auto-applies
    assert outcome.status is AmendmentStatus.NEEDS_REVIEW
    assert outcome.ruleset_version is None


async def test_it_voids_only_the_signatures_the_change_touches(
    admin: ApiClient, project_id: uuid.UUID, db: AsyncSession
) -> None:
    """ "the *right* signatures" is the clause under test."""
    await cast(admin)
    guideline_id, claims = await seed_register(admin, db, project_id)
    guideline = await publish(db, guideline_id)
    legal_id = await user_id_of(db, "legal@example.com")

    touched = await sign(db, guideline=guideline, claims=claims[:1], signer_id=legal_id)
    spared = await sign(db, guideline=guideline, claims=claims[1:], signer_id=legal_id)

    source = await a_source(db, guideline.workspace_id)
    await watcher.check_source(db, source, html=BEFORE)
    await watcher.check_source(db, source, html=AFTER_MECHANICAL)
    await db.commit()
    amendment = (await db.execute(sa.select(PolicyAmendment))).scalars().one()

    await lifecycle.apply(
        db,
        amendment,
        classification(
            AmendmentChangeKind.SIGNATURE_AFFECTING,
            affected_claim_ids=(claims[0].id,),
        ),
        now=NOW,
    )
    await db.commit()

    await db.refresh(touched)
    await db.refresh(spared)
    assert touched.voided_at is not None
    assert spared.voided_at is None
    for claim in claims[1:]:
        await db.refresh(claim)
        assert claim.status is ClaimStatus.APPROVED


async def test_signature_affecting_with_no_named_claim_voids_nothing(
    admin: ApiClient, project_id: uuid.UUID, db: AsyncSession
) -> None:
    """Do not guess a set. Voiding every signature on a hunch is worse than asking."""
    await cast(admin)
    guideline_id, claims = await seed_register(admin, db, project_id)
    guideline = await publish(db, guideline_id)
    legal_id = await user_id_of(db, "legal@example.com")
    signature = await sign(db, guideline=guideline, claims=claims, signer_id=legal_id)

    source = await a_source(db, guideline.workspace_id)
    await watcher.check_source(db, source, html=BEFORE)
    await watcher.check_source(db, source, html=AFTER_MECHANICAL)
    await db.commit()
    amendment = (await db.execute(sa.select(PolicyAmendment))).scalars().one()

    outcome = await lifecycle.apply(
        db, amendment, classification(AmendmentChangeKind.SIGNATURE_AFFECTING), now=NOW
    )
    await db.commit()

    await db.refresh(signature)
    assert signature.voided_at is None
    assert outcome.status is AmendmentStatus.NEEDS_REVIEW
    assert any("no signed claim was identified" in note for note in outcome.notes)


# ---------------------------------------------------------------------------
# substantive and unclassified
# ---------------------------------------------------------------------------


async def test_a_substantive_amendment_never_auto_applies(
    admin: ApiClient, project_id: uuid.UUID, db: AsyncSession
) -> None:
    await cast(admin)
    guideline_id, _claims = await seed_register(admin, db, project_id)
    guideline = await publish(db, guideline_id)
    source = await a_source(db, guideline.workspace_id)
    await watcher.check_source(db, source, html=BEFORE)
    await watcher.check_source(db, source, html=AFTER_MECHANICAL)
    await db.commit()
    amendment = (await db.execute(sa.select(PolicyAmendment))).scalars().one()

    outcome = await lifecycle.apply(
        db, amendment, classification(AmendmentChangeKind.SUBSTANTIVE), now=NOW
    )
    await db.commit()

    assert outcome.status is AmendmentStatus.NEEDS_REVIEW
    assert outcome.ruleset_version is None
    assert (await db.execute(sa.select(sa.func.count()).select_from(RuleSet))).scalar() == 0


async def test_unclassified_is_handled_exactly_as_substantive(
    admin: ApiClient, project_id: uuid.UUID, db: AsyncSession
) -> None:
    """Law 29: ambiguity resolves toward the human, always."""
    await cast(admin)
    guideline_id, _claims = await seed_register(admin, db, project_id)
    guideline = await publish(db, guideline_id)
    source = await a_source(db, guideline.workspace_id)
    await watcher.check_source(db, source, html=BEFORE)
    await watcher.check_source(db, source, html=AFTER_MECHANICAL)
    await db.commit()
    amendment = (await db.execute(sa.select(PolicyAmendment))).scalars().one()

    outcome = await lifecycle.apply(
        db,
        amendment,
        classification(AmendmentChangeKind.UNCLASSIFIED, confidence=0.4, below_floor=True),
        now=NOW,
    )
    await db.commit()

    assert outcome.status is AmendmentStatus.NEEDS_REVIEW
    assert (await db.execute(sa.select(sa.func.count()).select_from(RuleSet))).scalar() == 0
    assert any("below the floor" in note for note in outcome.notes)
    # The class recorded is what the classifier concluded, not what it was
    # handled as — a reviewer needs to see that confidence was the reason.
    await db.refresh(amendment)
    assert amendment.change_kind is AmendmentChangeKind.UNCLASSIFIED


async def test_every_applied_amendment_writes_an_audit_row(
    admin: ApiClient, project_id: uuid.UUID, db: AsyncSession
) -> None:
    await cast(admin)
    guideline_id, _claims = await seed_register(admin, db, project_id)
    guideline = await publish(db, guideline_id)
    source = await a_source(db, guideline.workspace_id)
    await watcher.check_source(db, source, html=BEFORE)
    await watcher.check_source(db, source, html=AFTER_MECHANICAL)
    await db.commit()
    amendment = (await db.execute(sa.select(PolicyAmendment))).scalars().one()

    await lifecycle.apply(db, amendment, classification(AmendmentChangeKind.MECHANICAL), now=NOW)
    await db.commit()

    row = (
        (await db.execute(sa.select(AuditLog).where(AuditLog.target_id == amendment.id)))
        .scalars()
        .one()
    )
    assert row.action == "policy_amendment.auto_applied"
    # The watcher is not a person, and recording one would be a lie in the
    # one table whose whole job is saying who did what.
    assert row.actor_id is None
    assert row.meta["ruleset_version"].startswith("1.1+")


# ---------------------------------------------------------------------------
# the snapshot evidence 3.3.1 reads
# ---------------------------------------------------------------------------


async def test_a_changed_page_stores_snapshot_evidence_a_node_can_cite(
    admin: ApiClient, project_id: uuid.UUID, db: AsyncSession
) -> None:
    await cast(admin)
    guideline_id, _claims = await seed_register(admin, db, project_id)
    guideline = await publish(db, guideline_id)
    source = await a_source(db, guideline.workspace_id)

    await watcher.check_source(db, source, html=BEFORE)
    await db.commit()

    row = (
        (await db.execute(sa.select(Evidence).where(Evidence.kind == watcher.POLICY_SNAPSHOT)))
        .scalars()
        .one()
    )
    assert row.project_id == project_id
    assert row.source_url == source.url
    assert "Complaints are owner-initiated." in (row.content_text or "")
    # nav and footer are outside the selector
    assert "Help Center" not in (row.content_text or "")
