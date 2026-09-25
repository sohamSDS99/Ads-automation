"""§8.6's consequence table. Deterministic code, and no model anywhere in it.

`classifier.py` proposes a class. This module decides what happens, and the
mapping is a `match` over four values rather than anything cleverer, because
law 29 — *a policy change never silently rewrites a signed rule* — is only as
strong as the most surprising branch in this file.

| class                | consequence                                              |
|----------------------|----------------------------------------------------------|
| `mechanical`         | auto-applies, mints a MINOR, recompiles, notifies, audits |
| `substantive`        | `needs_review`; never auto-applies                        |
| `signature_affecting`| voids signatures, re-queues claims into H1, marks stale   |
| `unclassified`       | exactly `substantive` — ambiguity resolves to the human   |

---------------------------------------------------------------------------
WHAT "AUTO-APPLIES" DOES AND DOES NOT MEAN
---------------------------------------------------------------------------
A mechanical amendment mints `v{major}.{minor+1}` and compiles a new immutable
`RuleSet` from the *existing* guideline payload. It does not edit the payload.
§8.6 defines mechanical as "a value inside an existing rule changes", and the
values a rule is evaluated against live in `content_constants.yaml`, not in the
payload — so recompiling against current constants is the whole of applying it.
Anything that would need the payload rewritten is, by definition, not
mechanical, and lands in front of a person.

That is also why this module never writes `ContentGuideline.payload`: a DB
trigger rejects the UPDATE (law 26), and working around it is forbidden by the
same law. A new version is a new row's worth of state, not an edit.

---------------------------------------------------------------------------
SIGNATURE_AFFECTING IS THE ONLY ONE THAT TAKES SOMETHING AWAY
---------------------------------------------------------------------------
It voids live signatures, moves their claims back to `pending_signoff`, opens
an H1 for the named legal owner and sets `signature_stale`. From that moment
the linter treats those claims as un-licensed — which is automatic, because
`claims_index.ref_status` reads the signature, and a voided signature is not
live. There is no second place to un-license a claim, and that is deliberate.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import sqlalchemy as sa
import structlog
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
    GuidelineStatus,
    HumanTask,
    HumanTaskBlocking,
    HumanTaskStatus,
    PolicyAmendment,
    RuleSet,
    SignOffMatrix,
)
from agent.guardrails import compiler
from agent.guidelines import claims_index, versions
from agent.guidelines.constants import ContentConstants, get_content_constants
from agent.policy.classifier import Classification

log = structlog.get_logger(__name__)

#: `HumanTask.task_key` for a re-queued claim signature. The same key 3.2.3
#: opens, on purpose: the legal owner is being asked the same question about
#: the same claims, and a second key would put it in a second inbox.
CLAIM_SIGNOFF_TASK = "H1"


@dataclass(slots=True)
class Outcome:
    """What applying one amendment actually did."""

    amendment_id: uuid.UUID
    change_kind: AmendmentChangeKind
    status: AmendmentStatus
    #: Set when a MINOR was minted.
    ruleset_version: str | None = None
    ruleset_id: uuid.UUID | None = None
    voided_signature_ids: tuple[uuid.UUID, ...] = ()
    requeued_claim_ids: tuple[uuid.UUID, ...] = ()
    human_task_id: uuid.UUID | None = None
    notify: tuple[uuid.UUID, ...] = ()
    notes: list[str] = field(default_factory=list)


async def apply(
    db: AsyncSession,
    amendment: PolicyAmendment,
    classification: Classification,
    *,
    constants: ContentConstants | None = None,
    now: datetime | None = None,
) -> Outcome:
    """Route one classified amendment through §8.6. The whole table, one place."""
    stamp = now or datetime.now(UTC)
    conf = constants or get_content_constants()

    amendment.change_kind = classification.change_kind
    amendment.rationale = classification.rationale
    if classification.proposed_rule_changes:
        amendment.proposed_rule_changes = {"changes": list(classification.proposed_rule_changes)}

    kind = classification.effective_kind
    match kind:
        case AmendmentChangeKind.MECHANICAL:
            outcome = await _mechanical(db, amendment, constants=conf, now=stamp)
        case AmendmentChangeKind.SIGNATURE_AFFECTING:
            outcome = await _signature_affecting(
                db, amendment, classification, constants=conf, now=stamp
            )
        case _:
            outcome = await _substantive(db, amendment, classification)

    if classification.below_floor:
        outcome.notes.append(
            f"classifier confidence {classification.confidence:.2f} was below the floor, "
            "so the class was recorded as unclassified and handled as substantive"
        )
    if classification.escalated:
        outcome.notes.append(
            "escalated to signature_affecting: the change bears on a claim a named "
            "person has personally signed for"
        )

    db.add(
        AuditLog(
            workspace_id=amendment.workspace_id,
            actor_id=None,  # the watcher is not a person, and pretending is worse
            action=f"policy_amendment.{outcome.status.value}",
            target_type="policy_amendment",
            target_id=amendment.id,
            meta={
                "change_kind": classification.change_kind.value,
                "effective_kind": kind.value,
                "confidence": classification.confidence,
                "ruleset_version": outcome.ruleset_version,
                "voided_signatures": [str(item) for item in outcome.voided_signature_ids],
                "requeued_claims": [str(item) for item in outcome.requeued_claim_ids],
                "notes": outcome.notes,
            },
        )
    )
    await db.flush()
    return outcome


# ---------------------------------------------------------------------------
# mechanical — the only branch that applies itself
# ---------------------------------------------------------------------------


async def _mechanical(
    db: AsyncSession,
    amendment: PolicyAmendment,
    *,
    constants: ContentConstants,
    now: datetime,
) -> Outcome:
    guideline = await _published_guideline(db, amendment.project_id)
    if guideline is None:
        # Nothing published means nothing to amend. Not an error: the watcher
        # only opens amendments for projects holding a published guideline, so
        # this is the race where it was unpublished in between.
        amendment.status = AmendmentStatus.DISMISSED
        amendment.review_note = "no published guideline to amend"
        return Outcome(
            amendment_id=amendment.id,
            change_kind=AmendmentChangeKind.MECHANICAL,
            status=AmendmentStatus.DISMISSED,
            notes=["dismissed: the project has no published guideline"],
        )

    ruleset = await mint_minor(db, guideline, constants=constants, now=now)
    amendment.status = AmendmentStatus.AUTO_APPLIED
    amendment.applied_ruleset_id = ruleset.id
    amendment.reviewed_at = now
    owners = await _notify_targets(db, amendment.project_id)
    log.info(
        "amendment.auto_applied",
        amendment_id=str(amendment.id),
        ruleset_version=ruleset.ruleset_version,
    )
    return Outcome(
        amendment_id=amendment.id,
        change_kind=AmendmentChangeKind.MECHANICAL,
        status=AmendmentStatus.AUTO_APPLIED,
        ruleset_version=ruleset.ruleset_version,
        ruleset_id=ruleset.id,
        notify=owners,
    )


# ---------------------------------------------------------------------------
# the decisions a person makes in the inbox
# ---------------------------------------------------------------------------


class AmendmentDecisionError(ValueError):
    """An amendment that cannot be decided. The message is shown verbatim."""


async def apply_by_person(
    db: AsyncSession,
    amendment: PolicyAmendment,
    *,
    decided_by: uuid.UUID,
    constants: ContentConstants | None = None,
    now: datetime | None = None,
) -> Outcome:
    """Apply an amendment a human decided on (PRD §15.3 G).

    The counterpart to `apply()`, which runs on the watcher's classification
    with no actor. Everything that *happens* to signatures has already happened
    by the time a row reaches the inbox — `_signature_affecting` voids and
    re-queues at classification time, because leaving a signature standing over
    copy the policy no longer permits is the thing §8.6 exists to prevent. What
    is left for a person to decide is whether the rulebook changes, and that is
    a MINOR.

    `mechanical` rows are refused rather than re-applied: they applied
    themselves, and minting a second MINOR for one change would put a version
    in the history that nothing caused.
    """
    stamp = now or datetime.now(UTC)
    conf = constants or get_content_constants()

    if amendment.status in (AmendmentStatus.APPLIED, AmendmentStatus.AUTO_APPLIED):
        raise AmendmentDecisionError(
            "This amendment has already been applied. Applying it twice would mint a "
            "version nothing caused."
        )
    if amendment.status is AmendmentStatus.DISMISSED:
        raise AmendmentDecisionError(
            "This amendment was dismissed. Re-opening it is not a thing the inbox does — "
            "if the policy still needs to change, the watcher will raise it again."
        )

    guideline = await _published_guideline(db, amendment.project_id)
    if guideline is None:
        raise AmendmentDecisionError(
            "This project has no published guideline, so there is no rulebook to amend."
        )

    ruleset = await mint_minor(db, guideline, constants=conf, now=stamp)
    amendment.status = AmendmentStatus.APPLIED
    amendment.applied_ruleset_id = ruleset.id
    amendment.reviewed_by = decided_by
    amendment.reviewed_at = stamp
    await db.flush()
    log.info(
        "amendment.applied_by_person",
        amendment_id=str(amendment.id),
        ruleset_version=ruleset.ruleset_version,
    )
    return Outcome(
        amendment_id=amendment.id,
        change_kind=amendment.change_kind,
        status=AmendmentStatus.APPLIED,
        ruleset_version=ruleset.ruleset_version,
        ruleset_id=ruleset.id,
        voided_signature_ids=tuple(amendment.voided_signature_ids or ()),
    )


async def dismiss_by_person(
    db: AsyncSession,
    amendment: PolicyAmendment,
    *,
    decided_by: uuid.UUID,
    reason: str,
    now: datetime | None = None,
) -> Outcome:
    """Record that a person read this and decided the rulebook does not change.

    **Dismissal does not un-void anything.** A `signature_affecting` amendment
    has already voided its signatures by the time anyone sees it, and dismissing
    it says "no further rule change is needed", not "that never happened". The
    reason is mandatory because this is the branch where the audit trail is the
    only artifact left.
    """
    stamp = now or datetime.now(UTC)
    if amendment.status in (AmendmentStatus.APPLIED, AmendmentStatus.AUTO_APPLIED):
        raise AmendmentDecisionError(
            "This amendment has already been applied, so there is nothing to dismiss."
        )
    if amendment.status is AmendmentStatus.DISMISSED:
        raise AmendmentDecisionError("This amendment was already dismissed.")

    amendment.status = AmendmentStatus.DISMISSED
    amendment.reviewed_by = decided_by
    amendment.reviewed_at = stamp
    amendment.review_note = reason
    await db.flush()
    log.info("amendment.dismissed_by_person", amendment_id=str(amendment.id))
    return Outcome(
        amendment_id=amendment.id,
        change_kind=amendment.change_kind,
        status=AmendmentStatus.DISMISSED,
        voided_signature_ids=tuple(amendment.voided_signature_ids or ()),
        notes=[reason],
    )


async def current_minor(db: AsyncSession, guideline: ContentGuideline) -> int:
    """The highest MINOR minted for this guideline so far.

    Read from `rule_set`, not from `content_guideline.version_minor`, and that
    is the whole of why minting works at all — see `mint_minor`.
    """
    prefix = f"{guideline.version_major}."
    versions = (
        (
            await db.execute(
                sa.select(RuleSet.ruleset_version).where(RuleSet.guideline_id == guideline.id)
            )
        )
        .scalars()
        .all()
    )
    minors = [guideline.version_minor]
    for version in versions:
        # `ruleset_version` is "{major}.{minor}+{digest}" (compiler.py).
        if not version.startswith(prefix):
            continue
        tail = version[len(prefix) :].split("+", 1)[0]
        if tail.isdigit():
            minors.append(int(tail))
    return max(minors)


async def mint_minor(
    db: AsyncSession,
    guideline: ContentGuideline,
    *,
    constants: ContentConstants,
    now: datetime,
) -> RuleSet:
    """Mint `v{major}.{minor+1}` as a new immutable `RuleSet` row.

    -----------------------------------------------------------------------
    WHY THIS DOES NOT TOUCH `content_guideline`
    -----------------------------------------------------------------------
    The obvious implementation — bump `guideline.version_minor` — cannot work,
    and finding that out at 04:00 in production would be an expensive way to
    learn it. Migration 0016's `content_guideline_published_guard` raises
    `restrict_violation` when a *published* row changes `payload`, `markdown`,
    `ruleset_id`, `version_major` **or `version_minor`**. Law 26 says do not
    work around it, so this does not.

    Minting a whole new `content_guideline` row does not work either:
    `guideline_run_id` is NOT NULL and UNIQUE, and an amendment has no run to
    point at. Inventing one would put a fake run in the run table.

    So a MINOR is a new `rule_set` row and nothing else, which is what §8.6
    actually asks for — "mints `v{major}.{minor+1}`, recompiles the RuleSet".
    `rule_set.guideline_id` is deliberately not unique; many rulesets per
    guideline is the anticipated shape. The published guideline keeps pointing
    at the ruleset it was published with, and the *current* ruleset is the
    highest version for that guideline — which is what `current_minor` reads
    and what S3-P6's `GET /guidelines/published/ruleset` must resolve.

    The payload is copied, never written back. Recompiling it against current
    constants is the whole of applying a mechanical change: §8.6 defines the
    class as "a value inside an existing rule changes", and those values live
    in `content_constants.yaml`.
    """
    minor = await current_minor(db, guideline) + 1
    payload = dict(guideline.payload or {})
    payload["project_id"] = str(guideline.project_id)
    payload["guideline_id"] = str(guideline.id)
    payload["version_major"] = guideline.version_major
    payload["version_minor"] = minor

    refs = claims_index.claim_refs_for(
        await _claims_with_signatures(db, guideline.project_id), now=now
    )
    compiled = compiler.compile(payload, constants, refs, compiled_at=now)

    existing = (
        (await db.execute(sa.select(RuleSet).where(RuleSet.hash == compiled.hash)))
        .scalars()
        .first()
    )
    if existing is not None:
        # `uq_rule_set_hash` is global and has no allow-list. Identical inputs
        # compiled to identical bytes, so there is nothing new to write, and an
        # INSERT would abort the whole amendment transaction on a constraint
        # rather than report "no material change". Returning the existing row
        # keeps the amendment `auto_applied` and pointed at the ruleset that
        # genuinely governs — which is true, because it is the same ruleset.
        log.info(
            "amendment.recompile_identical",
            guideline_id=str(guideline.id),
            ruleset_version=existing.ruleset_version,
        )
        return existing

    row = RuleSet(
        workspace_id=guideline.workspace_id,
        project_id=guideline.project_id,
        guideline_id=guideline.id,
        ruleset_version=compiled.ruleset_version,
        compiled=compiled.model_dump(mode="json"),
        compiler_version=compiled.compiler_version,
        constants_version=compiled.constants_version,
        rule_count=len(compiled.rules),
        hash=compiled.hash,
    )
    db.add(row)
    await db.flush()
    return row


async def mint_reviewed_minor(
    db: AsyncSession,
    guideline: ContentGuideline,
    *,
    origin: AmendmentOrigin,
    reviewed_by: uuid.UUID,
    rationale: str,
    constants: ContentConstants,
    now: datetime,
) -> tuple[RuleSet, PolicyAmendment]:
    """`mint_minor`, for a change a named person has already reviewed.

    Stage 04 §8.6 step 4 calls this `mint_minor(origin='creative_exception',
    reviewed_by=signer)`: H3's cleared claims are new register rows with a
    live signature, so recompiling the payload is the whole of the change, and
    **the signer's signature is the review**. The amendment is therefore
    recorded `applied` with its reviewer, never `needs_review` — it does not
    enter the substantive-amendment inbox, which is `needs_review` and nothing
    else. It is still recorded: a MINOR nothing explains is a version nothing
    caused, and `current_ruleset` would serve it to every later run.

    `change_kind` is `substantive` because a licence appeared, which is what
    §8.6 calls a substantive change; `applied` + `reviewed_by` is what makes it
    one that needs no second look. Flushes, never commits: the caller's
    transaction holds the signature it depends on.
    """
    ruleset = await mint_minor(db, guideline, constants=constants, now=now)
    amendment = PolicyAmendment(
        workspace_id=guideline.workspace_id,
        project_id=guideline.project_id,
        origin=origin,
        detected_at=now,
        change_kind=AmendmentChangeKind.SUBSTANTIVE,
        rationale=rationale,
        status=AmendmentStatus.APPLIED,
        applied_ruleset_id=ruleset.id,
        reviewed_by=reviewed_by,
        reviewed_at=now,
    )
    db.add(amendment)
    await db.flush()
    log.info(
        "amendment.minted_reviewed_minor",
        origin=origin.value,
        ruleset_version=ruleset.ruleset_version,
        amendment_id=str(amendment.id),
    )
    return ruleset, amendment


# ---------------------------------------------------------------------------
# substantive (and unclassified, which is substantive)
# ---------------------------------------------------------------------------


async def _substantive(
    db: AsyncSession, amendment: PolicyAmendment, classification: Classification
) -> Outcome:
    """Park it in the inbox. Never auto-applies, whatever it looks like."""
    amendment.status = AmendmentStatus.NEEDS_REVIEW
    owners = await _notify_targets(db, amendment.project_id)
    log.info(
        "amendment.needs_review",
        amendment_id=str(amendment.id),
        change_kind=classification.change_kind.value,
    )
    return Outcome(
        amendment_id=amendment.id,
        change_kind=classification.change_kind,
        status=AmendmentStatus.NEEDS_REVIEW,
        notify=owners,
    )


# ---------------------------------------------------------------------------
# signature_affecting — the branch that takes something away
# ---------------------------------------------------------------------------


async def _signature_affecting(
    db: AsyncSession,
    amendment: PolicyAmendment,
    classification: Classification,
    *,
    constants: ContentConstants,
    now: datetime,
) -> Outcome:
    """Void, re-queue, mark stale, and ask the named person again."""
    claim_ids = list(classification.affected_claim_ids)
    outcome = Outcome(
        amendment_id=amendment.id,
        change_kind=AmendmentChangeKind.SIGNATURE_AFFECTING,
        status=AmendmentStatus.NEEDS_REVIEW,
    )

    if not claim_ids:
        # The class said signatures are affected and named none. Do not guess a
        # set — voiding every signature on a hunch is worse than asking. It
        # still goes to a human, which is the class's floor behaviour anyway.
        amendment.status = AmendmentStatus.NEEDS_REVIEW
        outcome.notes.append(
            "classified signature_affecting but no signed claim was identified, "
            "so nothing was voided; a person decides which signatures this touches"
        )
        outcome.notify = await _notify_targets(db, amendment.project_id)
        return outcome

    claims = list(
        (
            await db.execute(
                sa.select(ClaimRecord).where(
                    ClaimRecord.project_id == amendment.project_id, ClaimRecord.id.in_(claim_ids)
                )
            )
        )
        .scalars()
        .all()
    )
    signature_ids = {claim.current_signature_id for claim in claims if claim.current_signature_id}
    signatures = list(
        (
            await db.execute(
                sa.select(ClaimSignature).where(
                    ClaimSignature.id.in_(signature_ids), ClaimSignature.voided_at.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )

    for signature in signatures:
        # Append-only: the row is marked void, never deleted and never edited
        # into a different decision. The signer said what they said.
        signature.voided_at = now
        signature.void_reason = (
            f"voided by policy amendment {amendment.id}: {classification.rationale}"
        )
    for claim in claims:
        claim.status = ClaimStatus.PENDING_SIGNOFF
        claim.current_signature_id = None

    guideline = await _published_guideline(db, amendment.project_id)
    if guideline is not None:
        guideline.signature_stale = True

    task = await _requeue_h1(db, amendment, claims=claims, now=now)

    amendment.status = AmendmentStatus.NEEDS_REVIEW
    amendment.voided_signature_ids = [signature.id for signature in signatures]
    outcome.voided_signature_ids = tuple(signature.id for signature in signatures)
    outcome.requeued_claim_ids = tuple(claim.id for claim in claims)
    outcome.human_task_id = task.id if task is not None else None
    outcome.notify = await _notify_targets(db, amendment.project_id)
    if task is None:
        outcome.notes.append(
            "no current sign-off matrix, so no H1 could be routed; the claims are "
            "un-licensed and the signature is stale either way"
        )
    log.info(
        "amendment.signatures_voided",
        amendment_id=str(amendment.id),
        signatures=len(signatures),
        claims=len(claims),
    )
    return outcome


async def _requeue_h1(
    db: AsyncSession,
    amendment: PolicyAmendment,
    *,
    claims: list[ClaimRecord],
    now: datetime,
) -> HumanTask | None:
    """Open an H1 for the named legal owner, or report that there is nobody.

    Not routed through `guidelines/tasks.open_task`: that helper keys on
    `guideline_run_id`, and this task belongs to an amendment rather than to a
    run. A task with a null run is the shape §8.5's living loop needs — the
    claim-expiry sweep opens the same kind — and forcing a run id here would
    mean inventing one.
    """
    matrix = (
        (
            await db.execute(
                sa.select(SignOffMatrix)
                .where(
                    SignOffMatrix.project_id == amendment.project_id,
                    SignOffMatrix.superseded_at.is_(None),
                )
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if matrix is None:
        log.error("amendment.no_legal_owner", amendment_id=str(amendment.id))
        return None

    task = HumanTask(
        workspace_id=amendment.workspace_id,
        project_id=amendment.project_id,
        guideline_run_id=None,
        node_id="3.2.3",
        task_key=CLAIM_SIGNOFF_TASK,
        title=f"Re-sign {len(claims)} claim(s) after a policy change",
        instructions=(
            "A watched Google policy page changed in a way that bears on claims you "
            "previously signed for. Those signatures have been voided and the claims are "
            "un-licensed until you decide again — copy asserting them is blocked in the "
            "meantime. Read the amendment diff, then approve or reject each claim."
        ),
        assignee_id=matrix.legal_owner_id,
        required_artifacts={
            "decisions": "one approved/rejected decision per claim",
            "statement": "the attestation text you are confirming",
            "step_up": "your current password, re-entered",
        },
        status=HumanTaskStatus.PENDING,
        blocking_for=HumanTaskBlocking.PUBLISH,
    )
    db.add(task)
    await db.flush()
    return task


# ---------------------------------------------------------------------------
# shared reads
# ---------------------------------------------------------------------------


async def _published_guideline(db: AsyncSession, project_id: uuid.UUID) -> ContentGuideline | None:
    return (
        (
            await db.execute(
                sa.select(ContentGuideline)
                .where(
                    ContentGuideline.project_id == project_id,
                    ContentGuideline.status == GuidelineStatus.PUBLISHED,
                )
                .order_by(
                    ContentGuideline.version_major.desc(), ContentGuideline.version_minor.desc()
                )
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


#: Every current claim with its signature. One definition, in `guidelines/
#: versions`: publish and an amendment both compile a `claims_index`, and if the
#: two read the register differently then a published ruleset and its own MINOR
#: would licence different text. This copy also omitted the `superseded_by IS
#: NULL` filter, so an amendment's index carried stale rows whose replacements
#: may have been rejected.
_claims_with_signatures = versions.claims_with_signatures


async def _notify_targets(db: AsyncSession, project_id: uuid.UUID) -> tuple[uuid.UUID, ...]:
    """The brand and performance owners §8.6 says to notify.

    Ids only. Returned rather than emailed from here: this module runs inside
    the amendment transaction, and sending mail before a commit is how people
    get told about a change that then rolls back.
    """
    matrix = (
        (
            await db.execute(
                sa.select(SignOffMatrix)
                .where(
                    SignOffMatrix.project_id == project_id,
                    SignOffMatrix.superseded_at.is_(None),
                )
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if matrix is None:
        return ()
    return (matrix.brand_owner_id, matrix.performance_owner_id)
