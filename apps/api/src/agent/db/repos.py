"""Concrete repositories. Every query starts here, already workspace-scoped.

`WorkspaceScopedRepo.select()` is the only query constructor, so forgetting the
`workspace_id` filter is not something a route can do by accident (PRD §6).

Four repositories cannot use that base class because their model carries no
`workspace_id` column — `UserRepo`, `ApprovalRepo`, `ReportRepo`, `ExportRepo`.
Each reaches the workspace through a join instead, in one `_scoped()` method
that every other method on the class is built from. The rule is the same; only
the number of tables between the row and the workspace differs.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import (
    Approval,
    ApprovalRequiredRole,
    ApprovalStatus,
    AuditLog,
    CampaignPlan,
    CampaignPlanStatus,
    Export,
    ExportArtifactType,
    ExportFormat,
    Invite,
    Membership,
    Project,
    Report,
    Run,
    RunStage,
    RunStatus,
    Schedule,
    User,
    UserRole,
    UserStatus,
)
from agent.db.repo import WorkspaceScopedRepo


@dataclass(frozen=True, slots=True)
class GateDecider:
    """Who is asking, for the queries that filter by what a person may decide.

    A pair rather than a `User` because the role is no longer on the account:
    the same person is an approver in one workspace and a viewer in the next,
    and a query that took a `User` could not tell which workspace it was
    being asked about.
    """

    id: uuid.UUID
    role: UserRole


class Member:
    """A person and the membership that puts them in this workspace.

    A pair rather than a widened `User` because the two halves answer
    different questions and change for different reasons: `user.name` follows
    the person between workspaces, `membership.role` does not.
    """

    __slots__ = ("user", "membership")

    def __init__(self, user: User, membership: Membership) -> None:
        self.user = user
        self.membership = membership

    # The response models read a member as one flat object, and writing the
    # same six lines at each call site is how they drift apart.
    @property
    def id(self) -> uuid.UUID:
        return self.user.id

    @property
    def email(self) -> str:
        return self.user.email

    @property
    def name(self) -> str:
        return self.user.name

    @property
    def role(self) -> UserRole:
        return self.membership.role

    @property
    def status(self) -> UserStatus:
        """The workspace's answer, narrowed by the account's.

        A globally disabled account cannot sign in anywhere, so showing it as
        an active member of this workspace would be a lie the Team page tells
        about someone who has left the company.
        """
        if self.user.status is UserStatus.DISABLED:
            return UserStatus.DISABLED
        return self.membership.status

    @property
    def last_login_at(self) -> datetime | None:
        return self.user.last_login_at

    @property
    def created_at(self) -> datetime:
        """When they joined *this* workspace, which is what the Team page means."""
        return self.membership.created_at


class UserRepo:
    """Members of one workspace.

    Not a `WorkspaceScopedRepo`: `user` has no `workspace_id` any more, and the
    base class refuses a model it cannot scope rather than quietly returning
    every account on the installation. The scope is the join to `membership`,
    applied by `_scoped()` and by nothing else — same contract, one table
    further along. `ApprovalRepo` and `ReportRepo` below reach their workspace
    the same way.
    """

    def __init__(self, session: AsyncSession, workspace_id: uuid.UUID) -> None:
        self.session = session
        self.workspace_id = workspace_id

    def _scoped(self) -> sa.Select[tuple[User, Membership]]:
        return (
            sa.select(User, Membership)
            .join(Membership, Membership.user_id == User.id)
            .where(Membership.workspace_id == self.workspace_id)
        )

    async def get(self, user_id: uuid.UUID) -> Member | None:
        """A member of *this* workspace. An account with no membership here is
        not found, which is the answer that keeps one workspace's admin from
        editing another's people."""
        result = await self.session.execute(self._scoped().where(User.id == user_id))
        row = result.first()
        return Member(*row) if row is not None else None

    async def by_email(self, email: str) -> Member | None:
        result = await self.session.execute(self._scoped().where(User.email == email))
        row = result.first()
        return Member(*row) if row is not None else None

    async def all_ordered(self) -> list[Member]:
        result = await self.session.execute(self._scoped().order_by(Membership.created_at.asc()))
        return [Member(user, membership) for user, membership in result.all()]

    async def names(self, ids: Collection[uuid.UUID] | None = None) -> dict[uuid.UUID, str]:
        """Id → display name, for the whole workspace or just the ids asked for.

        One query for a whole page of rows. A join per row would read more
        tidily and would issue a query per project on a list of thirty.

        Ids outside this workspace are answered for as well when they are asked
        for by name: a run launched by someone whose membership was later
        revoked still has to render its author, and "Unknown" on a year of run
        history is worse than a name the workspace can no longer edit.
        """
        if ids is not None and not ids:
            return {}
        if ids is None:
            statement = (
                sa.select(User.id, User.name)
                .join(Membership, Membership.user_id == User.id)
                .where(Membership.workspace_id == self.workspace_id)
            )
        else:
            statement = sa.select(User.id, User.name).where(User.id.in_(list(ids)))
        rows = await self.session.execute(statement)
        return {row[0]: row[1] for row in rows.all()}

    async def lock_active_admins(self) -> list[Member]:
        """Row-lock every active admin of this workspace, then return them.

        The lock is what makes the last-admin rule hold under concurrency: two
        simultaneous demotions serialise here, so the second one sees the first
        one's effect instead of both counting the same two admins and both
        succeeding (PRD §6.1, Authorization 4).

        Locked `FOR UPDATE OF membership`: the rows being changed are the
        memberships, and locking the `user` rows too would make an unrelated
        rename in another workspace wait on this transaction.
        """
        result = await self.session.execute(
            self._scoped()
            .where(
                Membership.role == UserRole.ADMIN,
                Membership.status == UserStatus.ACTIVE,
                User.status == UserStatus.ACTIVE,
            )
            .with_for_update(of=Membership)
        )
        return [Member(user, membership) for user, membership in result.all()]


async def account_by_email(db: AsyncSession, email: str) -> User | None:
    """An account anywhere on the installation, membership or not.

    A free function rather than a `UserRepo` method, and the distinction is
    load-bearing: everything on that class is scoped to one workspace, and a
    lookup that deliberately is not has no business borrowing the shape of one.
    Sign-in needs it — the password is on the account, and which workspace the
    person lands in is decided afterwards — and so does inviting someone who
    already works in a different workspace.
    """
    result = await db.execute(sa.select(User).where(User.email == email))
    return result.scalar_one_or_none()


class InviteRepo(WorkspaceScopedRepo[Invite]):
    model = Invite

    async def open_for_email(self, email: str) -> Invite | None:
        result = await self.session.execute(
            self.select().where(Invite.email == email, Invite.accepted_at.is_(None))
        )
        return result.scalar_one_or_none()


class AuditRepo(WorkspaceScopedRepo[AuditLog]):
    model = AuditLog

    async def page(
        self,
        *,
        actor_id: uuid.UUID | None = None,
        action: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        before: tuple[datetime, uuid.UUID] | None = None,
        limit: int = 50,
    ) -> list[tuple[AuditLog, str | None]]:
        """One page of the audit log, newest first, with the actor's email joined in.

        Ordering is `(created_at DESC, id DESC)` and the cursor carries both, so
        rows written inside the same transaction — which share a timestamp —
        still paginate without repeats or gaps.
        """
        stmt = (
            sa.select(AuditLog, User.email)
            .outerjoin(User, User.id == AuditLog.actor_id)
            .where(AuditLog.workspace_id == self.workspace_id)
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(limit)
        )
        if actor_id is not None:
            stmt = stmt.where(AuditLog.actor_id == actor_id)
        if action:
            stmt = stmt.where(AuditLog.action == action)
        if since is not None:
            stmt = stmt.where(AuditLog.created_at >= since)
        if until is not None:
            stmt = stmt.where(AuditLog.created_at <= until)
        if before is not None:
            cursor_created_at, cursor_id = before
            stmt = stmt.where(
                sa.tuple_(AuditLog.created_at, AuditLog.id)
                < sa.tuple_(sa.literal(cursor_created_at), sa.literal(cursor_id))
            )
        result = await self.session.execute(stmt)
        return [(row[0], row[1]) for row in result.all()]


class ProjectRepo(WorkspaceScopedRepo[Project]):
    model = Project


class RunRepo(WorkspaceScopedRepo[Run]):
    model = Run

    async def for_project(
        self, project_id: uuid.UUID, *, stage: RunStage | None = None, limit: int = 50
    ) -> list[Run]:
        query = self.select().where(Run.project_id == project_id)
        if stage is not None:
            query = query.where(Run.stage == stage)
        result = await self.session.execute(
            query.order_by(Run.started_at.desc().nullslast(), Run.id.desc()).limit(limit)
        )
        return list(result.scalars().all())

    async def latest_succeeded(
        self, project_id: uuid.UUID, *, stage: RunStage = RunStage.RESEARCH
    ) -> Run | None:
        """The newest run of this project and pipeline that finished.

        `parent_run_id` points here, so the Report Viewer's compare toggle is
        only ever offered against a run that has something to compare. A failed
        or cancelled run wrote no report.

        Scoped by stage because `parent_run_id` means "the previous run of the
        *same* stage" (Stage 02 PRD §7.1): a plan run whose parent is a
        research run would make the compare view diff two different documents.
        """
        result = await self.session.execute(
            self.select()
            .where(
                Run.project_id == project_id,
                Run.status == RunStatus.SUCCEEDED,
                Run.stage == stage,
            )
            .order_by(Run.finished_at.desc().nullslast(), Run.id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


class ScheduleRepo(WorkspaceScopedRepo[Schedule]):
    """Recurring runs. The poller reaches these through its own claim query —
    it runs on the worker with no session, so it has no workspace to scope to
    and instead narrows by the `Schedule` row's own `workspace_id` when it
    launches."""

    model = Schedule

    async def all_ordered(self, *, project_id: uuid.UUID | None = None) -> list[Schedule]:
        stmt = self.select()
        if project_id is not None:
            stmt = stmt.where(Schedule.project_id == project_id)
        result = await self.session.execute(stmt.order_by(Schedule.next_at.asc().nullslast()))
        return list(result.scalars().all())


class ApprovalRepo:
    """Approvals, scoped to a workspace through the run that owns them.

    `Approval` has no `workspace_id` of its own — it hangs off `Run` — so this
    deliberately does not subclass `WorkspaceScopedRepo`, which refuses models
    it cannot scope directly. Every query below joins `Run` instead, and there
    is no constructor that lets a caller skip that join.
    """

    def __init__(self, session: AsyncSession, workspace_id: uuid.UUID) -> None:
        self.session = session
        self.workspace_id = workspace_id

    def _scoped(self) -> sa.Select[tuple[Approval]]:
        return (
            sa.select(Approval)
            .join(Run, Run.id == Approval.run_id)
            .where(Run.workspace_id == self.workspace_id)
        )

    async def get(self, approval_id: uuid.UUID) -> Approval | None:
        result = await self.session.execute(self._scoped().where(Approval.id == approval_id))
        return result.scalar_one_or_none()

    async def run_for(self, approval: Approval) -> Run | None:
        result = await self.session.execute(
            sa.select(Run).where(Run.id == approval.run_id, Run.workspace_id == self.workspace_id)
        )
        return result.scalar_one_or_none()

    async def page(
        self,
        *,
        run_id: uuid.UUID | None = None,
        status: ApprovalStatus | None = None,
        decidable_by: GateDecider | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Approval]:
        """One page of the inbox, oldest first — a gate that has waited longest is first.

        `decidable_by` is `?mine=true`: the approvals this user may actually act
        on, which is not "assigned to me". It is every gate whose `required_role`
        they hold and which is either unassigned or assigned to them, because
        `assignee_id IS NULL` means "any holder of the role may decide" (PRD §6).
        """
        stmt = self._scoped()
        if run_id is not None:
            stmt = stmt.where(Approval.run_id == run_id)
        if status is not None:
            stmt = stmt.where(Approval.status == status)
        if decidable_by is not None:
            if decidable_by.role is UserRole.ADMIN:
                pass  # an admin may decide any gate (PRD §6.1 Authorization 3)
            elif decidable_by.role is UserRole.APPROVER:
                stmt = stmt.where(
                    Approval.required_role == ApprovalRequiredRole.APPROVER,
                    sa.or_(
                        Approval.assignee_id.is_(None),
                        Approval.assignee_id == decidable_by.id,
                    ),
                )
            else:
                # An operator or a viewer can decide nothing. Returning an empty
                # page is the honest answer to "what is in my inbox".
                stmt = stmt.where(sa.false())
        result = await self.session.execute(
            stmt.order_by(Approval.created_at.asc(), Approval.id.asc()).limit(limit).offset(offset)
        )
        return list(result.scalars().all())


class ReportRepo:
    """Reports, scoped through the run that produced them.

    `Report` has no `workspace_id` column, so it cannot subclass
    `WorkspaceScopedRepo` — that base class refuses a model it cannot scope,
    which is what stops an unscoped query being written by accident. The scope
    is still mandatory here; it just arrives over a join, exactly as
    `db/repo.py` instructs ("reach it through its owning Project or Run").
    """

    def __init__(self, session: AsyncSession, workspace_id: uuid.UUID) -> None:
        self.session = session
        self.workspace_id = workspace_id

    def _scoped(self) -> sa.Select[tuple[Report]]:
        return (
            sa.select(Report)
            .join(Run, Run.id == Report.run_id)
            .where(Run.workspace_id == self.workspace_id)
        )

    async def for_run(self, run_id: uuid.UUID) -> Report | None:
        result = await self.session.execute(self._scoped().where(Report.run_id == run_id))
        return result.scalar_one_or_none()

    async def get(self, report_id: uuid.UUID) -> Report | None:
        result = await self.session.execute(self._scoped().where(Report.id == report_id))
        return result.scalar_one_or_none()

    async def upsert(
        self, *, run_id: uuid.UUID, schema_version: str, payload: dict[str, Any], markdown: str
    ) -> Report:
        """Write the run's report, replacing it if this run already has one.

        `Report.run_id` is unique — one report per run (PRD §6) — and node 1.6.2
        re-synthesises once when its critique finds a blocking issue. That second
        write has to land on the same row: a run with two reports would leave
        `GET /reports/{run_id}` picking one at random, and the export the reader
        already downloaded would disagree with the one the API serves next.
        """
        existing = await self.for_run(run_id)
        if existing is not None:
            existing.schema_version = schema_version
            existing.payload = payload
            existing.markdown = markdown
            await self.session.flush()
            return existing
        report = Report(
            run_id=run_id, schema_version=schema_version, payload=payload, markdown=markdown
        )
        self.session.add(report)
        await self.session.flush()
        return report

    async def run_for(self, report_id: uuid.UUID) -> Run | None:
        """The run behind a report — the project id and the SSE channel live on it."""
        result = await self.session.execute(
            sa.select(Run)
            .join(Report, Report.run_id == Run.id)
            .where(Report.id == report_id, Run.workspace_id == self.workspace_id)
        )
        return result.scalar_one_or_none()


class CampaignPlanRepo(WorkspaceScopedRepo[CampaignPlan]):
    """Campaign plans. One per plan run, many per project (Stage 02 §7.2).

    `WorkspaceScopedRepo` rather than a join, because `campaign_plan` carries
    its own `workspace_id`.

    The one thing worth knowing here is `DRAFT_VERSION`. `version` is minted at
    freeze (§12.2), so every plan is written before it has one, and migration
    0014 made uniqueness partial (`WHERE version > 0`) precisely so that more
    than one unfrozen plan can exist in a project — which happens the first
    time a gate is rejected and the run is repeated.
    """

    model = CampaignPlan

    #: What an unfrozen plan's `version` is. `version > 0` is the test for
    #: "this plan has been frozen at least once", and the Plan Viewer renders
    #: 0 as an em dash rather than as "version zero".
    DRAFT_VERSION = 0

    async def for_run(self, plan_run_id: uuid.UUID) -> CampaignPlan | None:
        result = await self.session.execute(
            self.select().where(CampaignPlan.plan_run_id == plan_run_id)
        )
        return result.scalar_one_or_none()

    async def upsert(
        self,
        *,
        plan_run_id: uuid.UUID,
        project_id: uuid.UUID,
        acceptance_id: uuid.UUID,
        schema_version: str,
        payload: dict[str, Any],
        markdown: str,
        status: CampaignPlanStatus,
    ) -> CampaignPlan:
        """Write the run's plan, replacing it if this run already has one.

        Node 2.6.2 re-synthesises once when its critique finds a blocking
        issue, and that second write has to land on the same row: `plan_run_id`
        is unique, and two plans for one run would leave the Plan Viewer
        picking one at random.

        A **frozen** row is never rewritten. The database trigger would reject
        it anyway (law 17), and raising here names the reason rather than
        surfacing `restrict_violation` from three layers down.
        """
        existing = await self.for_run(plan_run_id)
        if existing is not None:
            if existing.status is CampaignPlanStatus.FROZEN:
                raise FrozenPlanError(
                    f"campaign_plan {existing.id} is frozen at v{existing.version}. "
                    "A change means a new version from a new plan run (§12.2)."
                )
            existing.schema_version = schema_version
            existing.payload = payload
            existing.markdown = markdown
            existing.status = status
            await self.session.flush()
            return existing

        plan = CampaignPlan(
            workspace_id=self.workspace_id,
            project_id=project_id,
            plan_run_id=plan_run_id,
            acceptance_id=acceptance_id,
            schema_version=schema_version,
            version=self.DRAFT_VERSION,
            status=status,
            payload=payload,
            markdown=markdown,
        )
        self.session.add(plan)
        await self.session.flush()
        return plan

    async def next_version(self, project_id: uuid.UUID) -> int:
        """§12.2: `max(version) + 1` for the project.

        Over **every** row, not only the frozen ones. A superseded plan is no
        longer `status='frozen'` but its version must never be reissued, and a
        predicate on status is how that would quietly happen.
        """
        highest = (
            await self.session.execute(
                sa.select(sa.func.max(CampaignPlan.version)).where(
                    CampaignPlan.project_id == project_id,
                    CampaignPlan.workspace_id == self.workspace_id,
                )
            )
        ).scalar_one_or_none()
        return int(highest or 0) + 1

    async def frozen_for_project(self, project_id: uuid.UUID) -> list[CampaignPlan]:
        """Every currently-frozen plan in a project. The freeze supersedes these.

        `version DESC` is right *here* and wrong in the history list, and the
        difference is the `status` filter: every row this returns is frozen,
        so every one has a minted version. A query without that filter must
        order by `created_at` — see `list_plans`.
        """
        result = await self.session.execute(
            self.select()
            .where(
                CampaignPlan.project_id == project_id,
                CampaignPlan.status == CampaignPlanStatus.FROZEN,
            )
            .order_by(CampaignPlan.version.desc())
        )
        return list(result.scalars().all())


class FrozenPlanError(RuntimeError):
    """A write was attempted against a frozen plan (Stage 02 law 17)."""


class ExportRepo:
    """Export jobs, scoped through whichever artifact they render.

    `Export.artifact_id` has no foreign key — two tables are exportable and one
    column cannot reference both (Stage 02 PRD §7.1) — so this repo is the only
    place that turns `(artifact_type, artifact_id)` back into a row, and
    therefore the only place that can answer "may this workspace see it".

    Both branches are written out even though nothing creates a plan export
    until S2-P5. A predicate that silently excludes a whole artifact type is
    not a narrower scope, it is a row that will one day be invisible to the
    workspace that owns it.
    """

    def __init__(self, session: AsyncSession, workspace_id: uuid.UUID) -> None:
        self.session = session
        self.workspace_id = workspace_id

    def _in_workspace(self) -> sa.ColumnElement[bool]:
        """`True` for exports of an artifact this workspace owns, of either kind."""
        research = (
            sa.select(sa.literal(1))
            .select_from(Report)
            .join(Run, Run.id == Report.run_id)
            .where(Report.id == Export.artifact_id, Run.workspace_id == self.workspace_id)
            .exists()
        )
        plan = (
            sa.select(sa.literal(1))
            .select_from(CampaignPlan)
            .where(
                CampaignPlan.id == Export.artifact_id,
                CampaignPlan.workspace_id == self.workspace_id,
            )
            .exists()
        )
        return sa.or_(
            sa.and_(Export.artifact_type == ExportArtifactType.RESEARCH_REPORT, research),
            sa.and_(Export.artifact_type == ExportArtifactType.CAMPAIGN_PLAN, plan),
        )

    def _scoped(self) -> sa.Select[tuple[Export]]:
        return sa.select(Export).where(self._in_workspace())

    async def get(self, export_id: uuid.UUID) -> Export | None:
        result = await self.session.execute(self._scoped().where(Export.id == export_id))
        return result.scalar_one_or_none()

    async def for_report(self, report_id: uuid.UUID, *, limit: int = 50) -> list[Export]:
        result = await self.session.execute(
            self._scoped()
            .where(
                Export.artifact_type == ExportArtifactType.RESEARCH_REPORT,
                Export.artifact_id == report_id,
            )
            .order_by(Export.created_at.desc(), Export.id.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def run_for(self, export_id: uuid.UUID) -> Run | None:
        """The research run behind a report export. `None` for a plan export."""
        result = await self.session.execute(
            sa.select(Run)
            .join(Report, Report.run_id == Run.id)
            .join(
                Export,
                sa.and_(
                    Export.artifact_id == Report.id,
                    Export.artifact_type == ExportArtifactType.RESEARCH_REPORT,
                ),
            )
            .where(Export.id == export_id, Run.workspace_id == self.workspace_id)
        )
        return result.scalar_one_or_none()

    def add(
        self,
        artifact_id: uuid.UUID,
        fmt: ExportFormat,
        *,
        artifact_type: ExportArtifactType,
        requested_by: uuid.UUID,
    ) -> Export:
        """Stage a queued export. The row exists before the file does (PRD §12)."""
        export = Export(
            artifact_type=artifact_type,
            artifact_id=artifact_id,
            format=fmt,
            requested_by=requested_by,
        )
        self.session.add(export)
        return export


def utcnow() -> datetime:
    return datetime.now(UTC)
