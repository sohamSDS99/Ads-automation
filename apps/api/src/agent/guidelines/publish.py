"""Publishing a content rulebook (Stage 03 PRD §12.4, §16, law 26).

Publishing is one transaction and it is the only place a rulebook becomes
authoritative. §12.4 lists what it does; this module is that list, in order,
with the ordering made load-bearing:

1. assert G5 and G6 are `approved`;
2. assert H1 is `completed` with an unexpired signature covering **every** claim
   in the register;
3. assert the critique returned no `blocking` issue;
4. compile the `RuleSet` and assert the compile is reproducible;
5. mint `version_major = max(major) + 1`, `version_minor = 0`;
6. write the immutable `rule_set` row;
7. mark the prior published rulebook `superseded`;
8. write an `AuditLog` row.

**The payload is rewritten before the status changes, not after.** Migration
0016's `content_guideline_published_guard` fires on `OLD.status`, so the single
`UPDATE` carrying a row from `ready_to_publish` to `published` may also carry the
new version, the re-rendered markdown and the `ruleset_id` — and the *next* one
may not. Splitting them would make the second statement the thing the trigger
rejects. This is not a workaround: it is why the trigger checks `OLD.status`
rather than `NEW.status`, and a test asserts both halves. Same argument, same
shape, as `planning/freeze.py`.

**The ruleset row is inserted first, and it has to be.** `content_guideline.
ruleset_id` points at `rule_set.id` while `rule_set.guideline_id` points back;
the cycle is broken by inserting the ruleset (whose `guideline_id` already
exists) and then pointing the guideline at it in the sealing UPDATE.

-----------------------------------------------------------------------------
WHY THE REGISTER IS CHECKED AGAINST ROWS AND NOT AGAINST THE PAYLOAD
-----------------------------------------------------------------------------
Step 2 reads `claim_record` and `claim_signature` live rather than trusting
`guideline.payload.claims_register`. The payload was written when 3.6.1 ran,
which may have been before the legal owner signed anything — and a signature can
be revoked, or voided by a sign-off-matrix reassignment, in the minutes between
the critique passing and somebody pressing Publish. Checking the payload would
make the assertion a statement about the past.

**A refusal lists everything outstanding, not the first thing.** §21's exit
criterion says so, and the reason is that a publish dialog reporting one blocker
at a time turns a five-minute fix into five round trips.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import sqlalchemy as sa
import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent.audit import AuditAction, AuditTarget, write_audit
from agent.db.models import (
    Approval,
    ApprovalStatus,
    ClaimStatus,
    ContentGuideline,
    GuidelineStatus,
    HumanTask,
    HumanTaskStatus,
    Project,
    RuleSet,
    User,
)
from agent.export.guideline_contract import ContentGuideline as GuidelineContract
from agent.export.guideline_contract import CritiqueIssue
from agent.export.guideline_markdown import render_guideline_markdown
from agent.guardrails import compiler
from agent.guidelines import claims_index, versions
from agent.guidelines.constants import ContentConstants, load_content_constants

log = structlog.get_logger(__name__)

#: §11 and §16: two gates, no more. A rulebook cannot be published until both
#: carry an `approved` row — or until neither was ever opened, which is the
#: legitimate reuse path (3.5.1 found a current matrix, 3.1.3 had nothing to
#: confirm) rather than an unfinished one.
REQUIRED_GATES: tuple[str, ...] = ("G5", "G6")

GATE_LABELS = {"G5": "visual identity", "G6": "the sign-off matrix"}


@dataclass(frozen=True, slots=True)
class Blocker:
    """One reason a rulebook may not be published, in §16 rule 1's shape."""

    code: str
    detail: str
    fix_url: str


class PublishRefused(RuntimeError):
    """The rulebook is not in a state that may be sealed. Carries the blockers."""

    def __init__(self, blockers: list[Blocker]) -> None:
        super().__init__("; ".join(item.detail for item in blockers))
        self.blockers = blockers


class PublishConflict(RuntimeError):
    """The caller's `confirm_version` does not match what would be minted."""

    def __init__(self, detail: str, *, expected: int, submitted: int) -> None:
        super().__init__(detail)
        self.expected = expected
        self.submitted = submitted


@dataclass(slots=True)
class PublishResult:
    """What the publish did. `already_published` is the idempotent path."""

    guideline: ContentGuideline
    ruleset: RuleSet | None
    version_major: int
    version_minor: int = 0
    already_published: bool = False
    superseded: list[uuid.UUID] = field(default_factory=list)
    approval_ids: list[uuid.UUID] = field(default_factory=list)

    @property
    def version(self) -> str:
        return f"{self.version_major}.{self.version_minor}"


async def publish_guideline(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    guideline_id: uuid.UUID,
    confirm_version: int,
    actor_id: uuid.UUID,
    ip: str | None = None,
    constants: ContentConstants | None = None,
    now: datetime | None = None,
) -> PublishResult:
    """Seal one rulebook. Raises `PublishRefused` or `PublishConflict`, or commits."""
    stamp = now or datetime.now(UTC)
    guideline = (
        await session.execute(
            sa.select(ContentGuideline).where(
                ContentGuideline.id == guideline_id,
                ContentGuideline.workspace_id == workspace_id,
            )
        )
    ).scalar_one_or_none()
    if guideline is None:
        raise PublishRefused(
            [
                Blocker(
                    code="guideline_not_found",
                    detail=f"No guideline {guideline_id}.",
                    fix_url="/guidelines",
                )
            ]
        )

    if guideline.status is GuidelineStatus.PUBLISHED:
        return _already_published(guideline, confirm_version)

    approvals = await _approvals(session, guideline.guideline_run_id)
    blockers = await _blockers(session, guideline, approvals, now=stamp)
    if blockers:
        raise PublishRefused(blockers)

    version = await versions.next_major(session, guideline.project_id)
    if confirm_version != version:
        raise PublishConflict(
            f"This rulebook will be published as v{version}.0, and the request confirmed "
            f"v{confirm_version}. Reload it and try again — another version was probably "
            "published in this project while the dialog was open.",
            expected=version,
            submitted=confirm_version,
        )

    # The payload carries the version the compiler reads, so it is patched
    # *before* the compile rather than after: `ruleset_version` is
    # "{major}.{minor}+{hash8}", and a ruleset minted under the draft's
    # provisional number would pin to a version the guideline does not have.
    payload = dict(guideline.payload or {})
    payload["project_id"] = str(guideline.project_id)
    payload["guideline_id"] = str(guideline.id)
    payload["version_major"] = version
    payload["version_minor"] = 0
    payload["status"] = "published"

    resolved = constants or load_content_constants()
    refs = claims_index.claim_refs_for(
        await versions.claims_with_signatures(session, guideline.project_id), now=stamp
    )
    try:
        compiled = compiler.compile(payload, resolved, refs, compiled_at=stamp)
        again = compiler.compile(payload, resolved, refs, compiled_at=stamp)
    except Exception as exc:  # noqa: BLE001 — surfaced as a blocker, not a 500
        raise PublishRefused(
            [
                Blocker(
                    code="ruleset_does_not_compile",
                    detail=(
                        f"The rules do not compile, so there is nothing to hand Stage 04: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                    fix_url=f"/guidelines/{guideline.id}",
                )
            ]
        ) from exc

    if compiled.hash != again.hash:
        # §12.4: "compiles the `RuleSet` and asserts the compile is
        # reproducible". A hash that moves between two compiles of one payload
        # means a pinned `ruleset_version` would not name one set of rules, and
        # every audit against it would be a guess.
        raise PublishRefused(
            [
                Blocker(
                    code="ruleset_not_reproducible",
                    detail=(
                        "Compiling the same rules twice produced two different hashes "
                        f"({compiled.hash[:8]} and {again.hash[:8]}). A pinned ruleset "
                        "version would not mean one thing."
                    ),
                    fix_url=f"/guidelines/{guideline.id}",
                )
            ]
        )

    superseded = await _supersede(session, guideline, actor_id=actor_id, ip=ip)
    ruleset = RuleSet(
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
    session.add(ruleset)
    await session.flush()

    actor = await session.get(User, actor_id)
    project = await session.get(Project, guideline.project_id)
    approval_ids = [approvals[key].id for key in REQUIRED_GATES if key in approvals]

    # Order matters, and the module docstring says why: this one statement
    # carries the row from `ready_to_publish` to `published` *and* writes the
    # version, payload, markdown and ruleset_id. A second statement could not.
    guideline.version_major = version
    guideline.version_minor = 0
    guideline.payload, guideline.markdown = _sealed(
        payload,
        project_name=project.name if project else None,
        published_by_name=actor.name if actor else "",
        ruleset_version=compiled.ruleset_version,
    )
    guideline.ruleset_id = ruleset.id
    guideline.published_at = stamp
    guideline.published_by = actor_id
    guideline.published_approval_ids = approval_ids
    guideline.status = GuidelineStatus.PUBLISHED

    write_audit(
        session,
        workspace_id=workspace_id,
        actor_id=actor_id,
        action=AuditAction.GUIDELINE_PUBLISHED,
        target_type=AuditTarget.CONTENT_GUIDELINE,
        target_id=guideline.id,
        meta={
            "guideline_run_id": str(guideline.guideline_run_id),
            "project_id": str(guideline.project_id),
            "version": f"{version}.0",
            "ruleset_version": compiled.ruleset_version,
            "ruleset_hash": compiled.hash,
            "rule_count": len(compiled.rules),
            "approval_ids": [str(value) for value in approval_ids],
            "superseded": [str(value) for value in superseded],
        },
        ip=ip,
    )

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise PublishConflict(
            f"Version {version} was taken by another publish a moment ago. Reload the "
            "rulebook; the next version will be one higher.",
            expected=version,
            submitted=confirm_version,
        ) from exc

    log.info(
        "guideline.published",
        guideline_id=str(guideline.id),
        version=f"{version}.0",
        ruleset_version=compiled.ruleset_version,
        superseded=len(superseded),
    )
    return PublishResult(
        guideline=guideline,
        ruleset=ruleset,
        version_major=version,
        superseded=superseded,
        approval_ids=approval_ids,
    )


# ---------------------------------------------------------------------------
# the checks
# ---------------------------------------------------------------------------


async def _blockers(
    session: AsyncSession,
    guideline: ContentGuideline,
    approvals: dict[str, Approval],
    *,
    now: datetime,
) -> list[Blocker]:
    """Everything standing between this rulebook and a signature."""
    found: list[Blocker] = []
    console = f"/guidelines/{guideline.id}"

    if not guideline.payload:
        found.append(
            Blocker(
                code="no_payload",
                detail=(
                    "This guideline has no rulebook yet — node 3.6.1 has not produced one "
                    "for this run."
                ),
                fix_url=console,
            )
        )
        # Everything below reads the payload. Returning here gives one honest
        # blocker rather than eight derived from its absence.
        return found

    for key in REQUIRED_GATES:
        approval = approvals.get(key)
        if approval is None:
            # Never opened is legitimate: 3.5.1 reuses a current matrix without
            # asking, and 3.1.3 can have nothing to confirm. A gate that was
            # never *asked* is not a gate awaiting an answer.
            continue
        if approval.status is not ApprovalStatus.APPROVED:
            found.append(
                Blocker(
                    code="gate_not_approved",
                    detail=(
                        f"Gate {key} ({GATE_LABELS[key]}) is {approval.status.value}. "
                        "Both gates must be approved before a rulebook can be published."
                    ),
                    fix_url="/approvals",
                )
            )

    found.extend(await _signature_blockers(session, guideline, now=now))

    blocking = _blocking_issues(guideline)
    if blocking:
        found.append(
            Blocker(
                code="blocking_critique",
                detail=(
                    f"The critique found {len(blocking)} blocking issue"
                    f"{'' if len(blocking) == 1 else 's'}: "
                    + "; ".join(issue.finding for issue in blocking[:3])
                ),
                fix_url=console,
            )
        )

    if guideline.status is GuidelineStatus.BLOCKED and not blocking:
        # `blocked` with no blocking issue in the payload means a gate was
        # rejected. The gate loop above has already said which, so this only
        # fires when the row and the payload disagree — worth refusing rather
        # than resolving in favour of either.
        found.append(
            Blocker(
                code="guideline_blocked",
                detail=(
                    "This rulebook is marked blocked. Re-run it; a blocked rulebook is "
                    "not a draft that happens to be unfinished."
                ),
                fix_url=console,
            )
        )
    return found


async def _signature_blockers(
    session: AsyncSession, guideline: ContentGuideline, *, now: datetime
) -> list[Blocker]:
    """§12.4's H1 assertion, read live rather than off the payload.

    Two separate failures: the task itself is not complete, and the register
    holds claims no live signature covers. They are reported apart because they
    are different conversations — the first is "the legal owner has not finished",
    the second is "the register changed after they did".
    """
    found: list[Blocker] = []
    tasks = (
        (
            await session.execute(
                sa.select(HumanTask).where(
                    HumanTask.guideline_run_id == guideline.guideline_run_id,
                    HumanTask.task_key == "H1",
                )
            )
        )
        .scalars()
        .all()
    )
    incomplete = [task for task in tasks if task.status is not HumanTaskStatus.COMPLETED]
    if incomplete:
        found.append(
            Blocker(
                code="h1_incomplete",
                detail=(
                    f"The claims register has not been signed: H1 is "
                    f"{incomplete[0].status.value}. Only the named legal owner can "
                    "complete it, and there is no administrator override."
                ),
                fix_url="/approvals",
            )
        )

    rows = await versions.claims_with_signatures(session, guideline.project_id)
    if not rows:
        # An empty register is not a pass. Publishing a rulebook whose payload
        # lists forty harvested claims while the register holds none would make
        # the signature assertion vacuous — which is the whole failure
        # `guidelines/register.py` exists to prevent, arriving one layer up.
        if _payload_claim_count(guideline) > 0:
            found.append(
                Blocker(
                    code="register_empty",
                    detail=(
                        "The rulebook lists claims but the register holds none, so there "
                        "is nothing for a signature to cover. Re-run the guideline."
                    ),
                    fix_url=f"/guidelines/{guideline.id}",
                )
            )
        return found

    unlicensed = [
        claim.claim_text or str(claim.id)
        for claim, signature in rows
        if claim.status is ClaimStatus.APPROVED
        and not claims_index.signature_is_live(signature, now=now)
    ]
    if unlicensed:
        found.append(
            Blocker(
                code="signature_not_live",
                detail=(
                    f"{len(unlicensed)} approved claim(s) have no live signature: "
                    + ", ".join(unlicensed[:5])
                    + (f" and {len(unlicensed) - 5} more" if len(unlicensed) > 5 else "")
                    + ". A signature that has expired or been voided licenses nothing."
                ),
                fix_url=f"/guidelines/{guideline.id}/claims",
            )
        )

    undecided = [
        claim.claim_text or str(claim.id)
        for claim, _signature in rows
        if claim.status is ClaimStatus.PENDING_SIGNOFF
    ]
    if undecided:
        found.append(
            Blocker(
                code="claims_undecided",
                detail=(
                    f"{len(undecided)} claim(s) are still awaiting the legal owner's "
                    "decision. Every claim in the register needs one — approving some "
                    "and leaving the rest is what a partial signature means, and it is "
                    "not a published position."
                ),
                fix_url=f"/guidelines/{guideline.id}/claims",
            )
        )
    return found


def _blocking_issues(guideline: ContentGuideline) -> list[CritiqueIssue]:
    """The critique's blocking findings, read off the stored payload.

    From the payload rather than from the `node_run` output, because the payload
    is what the exported PDF carries: if the two ever disagree, the document a
    person signed is the one that matters.
    """
    payload = guideline.payload if isinstance(guideline.payload, dict) else {}
    issues = payload.get("critique_issues")
    if not isinstance(issues, list):
        return []
    found = []
    for item in issues:
        if isinstance(item, dict) and item.get("severity") == "blocking":
            found.append(CritiqueIssue.model_validate(item))
    return found


def _payload_claim_count(guideline: ContentGuideline) -> int:
    payload = guideline.payload if isinstance(guideline.payload, dict) else {}
    register = payload.get("claims_register")
    claims = register.get("claims") if isinstance(register, dict) else None
    return len(claims) if isinstance(claims, list) else 0


async def _approvals(session: AsyncSession, run_id: uuid.UUID) -> dict[str, Approval]:
    """The gate rows for this run, keyed by gate. Latest wins on a duplicate."""
    rows = (
        await session.execute(
            sa.select(Approval).where(Approval.run_id == run_id).order_by(Approval.created_at.asc())
        )
    ).scalars()
    found: dict[str, Approval] = {}
    for approval in rows:
        key = (approval.gate_key or "").strip().upper()
        if key in REQUIRED_GATES:
            found[key] = approval
    return found


# ---------------------------------------------------------------------------
# the writes
# ---------------------------------------------------------------------------


def _already_published(guideline: ContentGuideline, confirm_version: int) -> PublishResult:
    """§16 rule 2's shape. Same version is a 200; a different one is a 409."""
    if confirm_version == guideline.version_major:
        return PublishResult(
            guideline=guideline,
            ruleset=None,
            version_major=guideline.version_major,
            version_minor=guideline.version_minor,
            already_published=True,
            approval_ids=list(guideline.published_approval_ids or []),
        )
    raise PublishConflict(
        f"This rulebook is already published as v{guideline.version_major}."
        f"{guideline.version_minor}; the request confirmed v{confirm_version}. Reload it.",
        expected=guideline.version_major,
        submitted=confirm_version,
    )


async def _supersede(
    session: AsyncSession,
    guideline: ContentGuideline,
    *,
    actor_id: uuid.UUID,
    ip: str | None,
) -> list[uuid.UUID]:
    """Mark every currently-published rulebook in this project superseded.

    Their `payload`, `markdown` and version are untouched — the trigger would
    refuse them anyway, and a superseded rulebook has to keep saying exactly what
    it said when it was published. Only `status` moves. Their rulesets stay
    resolvable by pin forever, which is what lets an asset made under v1 be
    re-audited against v1's rules (§12.2).
    """
    rows = (
        (
            await session.execute(
                sa.select(ContentGuideline).where(
                    ContentGuideline.project_id == guideline.project_id,
                    ContentGuideline.status == GuidelineStatus.PUBLISHED,
                )
            )
        )
        .scalars()
        .all()
    )
    superseded: list[uuid.UUID] = []
    for previous in rows:
        if previous.id == guideline.id:
            continue
        previous.status = GuidelineStatus.SUPERSEDED
        superseded.append(previous.id)
        write_audit(
            session,
            workspace_id=guideline.workspace_id,
            actor_id=actor_id,
            action=AuditAction.GUIDELINE_SUPERSEDED,
            target_type=AuditTarget.CONTENT_GUIDELINE,
            target_id=previous.id,
            meta={
                "superseded_by": str(guideline.id),
                "version": f"{previous.version_major}.{previous.version_minor}",
            },
            ip=ip,
        )
    return superseded


def _sealed(
    payload: dict[str, object],
    *,
    project_name: str | None,
    published_by_name: str,
    ruleset_version: str,
) -> tuple[dict[str, object], str]:
    """The payload and markdown a published rulebook carries forever.

    A payload that cannot be parsed is written back with its fields patched in
    place rather than rebuilt. Refusing to publish because a draft written under
    an older contract version cannot round-trip would strand a rulebook a legal
    owner has already signed — the one document in this system whose cost of
    being stranded is somebody's afternoon plus a re-reading of forty claims.
    """
    try:
        parsed = GuidelineContract.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 — see the docstring
        log.warning(
            "guideline.publish_payload_unparseable",
            error=f"{type(exc).__name__}: {exc}",
        )
        return payload, ""

    markdown = render_guideline_markdown(
        parsed,
        project_name=project_name,
        published_by_name=published_by_name,
        ruleset_version=ruleset_version,
    )
    return parsed.model_dump(mode="json"), markdown
