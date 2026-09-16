"""Projects: create, read, edit (PRD §14).

Two things here are load-bearing beyond CRUD.

`requirements` is the server's single answer to "can this be run yet", so the
wizard's last step and the launch endpoint cannot disagree about it.

`PATCH` honours `If-Unmodified-Since` and answers `412` when the row moved
underneath the caller (PRD §16, "Concurrent project edits … never a silent
overwrite"). The header carries HTTP-date, which has one-second resolution, so
the comparison truncates `updated_at` to the second — otherwise every edit
would 412 against the `Last-Modified` value we ourselves just sent.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from email.utils import format_datetime, parsedate_to_datetime
from typing import Annotated

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from agent.api import problems
from agent.api.middleware import client_ip
from agent.api.schemas_projects import (
    SETTINGS_APPROVALS,
    SETTINGS_MODELS,
    CreateProjectRequest,
    GateAssignment,
    GateInfo,
    Market,
    ModelRouting,
    ProductContext,
    ProjectDetail,
    ProjectListResponse,
    ProjectRequirement,
    ProjectSummary,
    RunListResponse,
    RunSummary,
    UpdateProjectRequest,
)
from agent.audit import AuditAction, AuditTarget, write_audit
from agent.auth.deps import Principal, require
from agent.auth.rbac import Permission
from agent.db.models import (
    Credential,
    CredentialKind,
    Project,
    Run,
    User,
    UserRole,
    UserStatus,
)
from agent.db.repos import ProjectRepo, RunRepo
from agent.db.session import get_session
from agent.gates import GATES, GateSpec
from agent.orchestrator.state import TERMINAL_STATUSES

log = structlog.get_logger(__name__)

router = APIRouter(tags=["projects"])

Db = Annotated[AsyncSession, Depends(get_session)]
AnyMember = Annotated[Principal, Depends(require(Permission.READ))]
ProjectWriter = Annotated[Principal, Depends(require(Permission.PROJECT_WRITE))]

#: How many runs `GET /projects/{id}/runs` returns. The console paginates in
#: P7; a project with more history than this is not a P6 problem.
RUN_PAGE_SIZE = 50


# ---------------------------------------------------------------------------
# list and create
# ---------------------------------------------------------------------------


@router.get("/projects", response_model=ProjectListResponse, summary="Every project")
async def list_projects(me: AnyMember, db: Db) -> ProjectListResponse:
    repo = ProjectRepo(db, me.workspace_id)
    projects = list(
        (await db.execute(repo.select().order_by(Project.created_at.desc()))).scalars().all()
    )
    if not projects:
        return ProjectListResponse(projects=[])

    names = await _user_names(db, me.workspace_id)
    counts, latest = await _run_rollup(db, me.workspace_id, [item.id for item in projects])
    return ProjectListResponse(
        projects=[
            ProjectSummary(
                id=project.id,
                name=project.name,
                domain=project.domain,
                created_at=project.created_at,
                updated_at=project.updated_at,
                created_by=project.created_by,
                created_by_name=names.get(project.created_by),
                run_count=counts.get(project.id, 0),
                last_run=_run_summary(latest[project.id], names) if project.id in latest else None,
            )
            for project in projects
        ]
    )


@router.post(
    "/projects",
    response_model=ProjectDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Create a project",
)
async def create_project(
    body: CreateProjectRequest,
    me: ProjectWriter,
    request: Request,
    response: Response,
    db: Db,
) -> ProjectDetail:
    """Create the row and nothing else. The wizard fills it in with `PATCH`.

    Deliberately minimal: a person who abandons the wizard halfway has a project
    they can come back to, not a half-written form they have to retype.
    """
    project = Project(
        created_by=me.user.id,
        name=body.name,
        domain=body.domain,
        product_context={},
        markets=[],
        settings={},
    )
    ProjectRepo(db, me.workspace_id).add(project)
    await db.flush()

    write_audit(
        db,
        workspace_id=me.workspace_id,
        actor_id=me.user.id,
        action=AuditAction.PROJECT_CREATED,
        target_type=AuditTarget.PROJECT,
        target_id=project.id,
        meta={"name": project.name, "domain": project.domain},
        ip=client_ip(request),
    )
    await db.commit()
    await db.refresh(project)
    return await _detail(db, me, project, response)


# ---------------------------------------------------------------------------
# read and edit one
# ---------------------------------------------------------------------------


@router.get("/projects/{project_id}", response_model=ProjectDetail, summary="One project")
async def get_project(
    project_id: uuid.UUID, me: AnyMember, response: Response, db: Db
) -> ProjectDetail:
    project = await _load(db, me, project_id)
    return await _detail(db, me, project, response)


@router.patch("/projects/{project_id}", response_model=ProjectDetail, summary="Edit a project")
async def update_project(
    project_id: uuid.UUID,
    body: UpdateProjectRequest,
    me: ProjectWriter,
    request: Request,
    response: Response,
    db: Db,
) -> ProjectDetail:
    project = await _load(db, me, project_id)
    _assert_unmodified(request, project)

    changed: dict[str, object] = {}
    if body.name is not None and body.name != project.name:
        changed["name"] = {"from": project.name, "to": body.name}
        project.name = body.name
    if body.domain is not None and body.domain != project.domain:
        changed["domain"] = {"from": project.domain, "to": body.domain}
        project.domain = body.domain
    if body.product_context is not None:
        project.product_context = body.product_context.model_dump()
        changed["product_context"] = True
    if body.markets is not None:
        project.markets = [market.model_dump() for market in body.markets]
        changed["markets"] = [market.country for market in body.markets]

    if body.models is not None or body.approvals is not None:
        # Model routing and gate assignment are settings writes, not project
        # writes: an operator may describe the business, an admin decides what
        # it costs to research and who signs the claims off (PRD §13.4).
        if Permission.SETTINGS_WRITE not in me.permissions:
            raise problems.forbidden(missing_permission=Permission.SETTINGS_WRITE.value)
        await _assert_assignees_exist(db, me, body.approvals)
        # Reassigning rather than mutating: SQLAlchemy does not track in-place
        # edits of a JSONB dict, so a mutated `settings` would never be written.
        settings = dict(project.settings)
        if body.models is not None:
            settings[SETTINGS_MODELS] = body.models.as_settings()
            changed["models"] = settings[SETTINGS_MODELS]
        if body.approvals is not None:
            settings[SETTINGS_APPROVALS] = {
                node_id: assignment.model_dump(mode="json")
                for node_id, assignment in body.approvals.items()
            }
            changed["approvals"] = sorted(body.approvals)
        project.settings = settings

    if not changed:
        # Nothing to write, so nothing to audit. Returning the row unchanged is
        # honest; writing an audit entry saying "updated" would not be.
        return await _detail(db, me, project, response)

    write_audit(
        db,
        workspace_id=me.workspace_id,
        actor_id=me.user.id,
        action=AuditAction.PROJECT_UPDATED,
        target_type=AuditTarget.PROJECT,
        target_id=project.id,
        meta={"fields": changed},
        ip=client_ip(request),
    )
    await db.commit()
    await db.refresh(project)
    return await _detail(db, me, project, response)


@router.get(
    "/projects/{project_id}/runs",
    response_model=RunListResponse,
    summary="This project's run history",
)
async def list_project_runs(project_id: uuid.UUID, me: AnyMember, db: Db) -> RunListResponse:
    await _load(db, me, project_id)
    runs = await RunRepo(db, me.workspace_id).for_project(project_id, limit=RUN_PAGE_SIZE)
    names = await _user_names(db, me.workspace_id)
    return RunListResponse(runs=[_run_summary(run, names) for run in runs])


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _load(db: AsyncSession, me: Principal, project_id: uuid.UUID) -> Project:
    project = await ProjectRepo(db, me.workspace_id).get(project_id)
    if project is None:
        raise problems.not_found(f"No project {project_id}.")
    return project


def _assert_unmodified(request: Request, project: Project) -> None:
    """412 when the row changed since the caller last read it."""
    header = request.headers.get("if-unmodified-since")
    if not header:
        return
    try:
        seen = parsedate_to_datetime(header)
    except (TypeError, ValueError) as exc:
        raise problems.unprocessable(
            "`If-Unmodified-Since` is not an HTTP-date.",
        ) from exc
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=UTC)
    current = _aware(project.updated_at)
    # HTTP-date has one-second resolution; `updated_at` has microseconds.
    # Comparing them untruncated would 412 against the very value we sent.
    if current.replace(microsecond=0) > seen:
        raise problems.Problem(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            title="Project changed",
            detail=(
                "Someone else edited this project after you opened it. "
                "Reload to see their changes before saving yours."
            ),
            type_=problems.TYPE_CONFLICT,
            modified_at=format_datetime(current, usegmt=True),
        )


async def _assert_assignees_exist(
    db: AsyncSession, me: Principal, approvals: dict[str, GateAssignment] | None
) -> None:
    """A gate may only be assigned to someone who could actually decide it.

    Without this, a gate can be pointed at a viewer and the run halts on a
    person the API will refuse — discovered days later, by the run not moving.
    """
    if not approvals:
        return
    wanted = {item.assignee_id for item in approvals.values() if item.assignee_id is not None}
    if not wanted:
        return
    rows = (
        (
            await db.execute(
                sa.select(User.id).where(
                    User.workspace_id == me.workspace_id,
                    User.id.in_(wanted),
                    User.role.in_([UserRole.APPROVER, UserRole.ADMIN]),
                    User.status != UserStatus.DISABLED,
                )
            )
        )
        .scalars()
        .all()
    )
    missing = sorted(str(item) for item in wanted - set(rows))
    if missing:
        raise problems.unprocessable(
            "A gate can only be assigned to an active approver or admin.",
            unknown_assignees=missing,
        )


async def _user_names(db: AsyncSession, workspace_id: uuid.UUID) -> dict[uuid.UUID, str]:
    """Id → display name for everyone in the workspace.

    One query for the whole page. A join per row would be tidier to read and
    would issue a query per project on a list of thirty.
    """
    rows = await db.execute(sa.select(User.id, User.name).where(User.workspace_id == workspace_id))
    return {row[0]: row[1] for row in rows.all()}


async def _run_rollup(
    db: AsyncSession, workspace_id: uuid.UUID, project_ids: list[uuid.UUID]
) -> tuple[dict[uuid.UUID, int], dict[uuid.UUID, Run]]:
    """Run counts and the newest run per project, in two queries rather than 2N."""
    counts = {
        row[0]: row[1]
        for row in (
            await db.execute(
                sa.select(Run.project_id, sa.func.count())
                .where(Run.workspace_id == workspace_id, Run.project_id.in_(project_ids))
                .group_by(Run.project_id)
            )
        ).all()
    }
    # `started_at` is NULL while a run is still queued, so ordering by it alone
    # would bury a brand-new run under every finished one.
    ranked = (
        sa.select(
            Run,
            sa.func.row_number()
            .over(
                partition_by=Run.project_id,
                order_by=(
                    sa.func.coalesce(Run.started_at, sa.text("'-infinity'::timestamptz")).desc(),
                    Run.id.desc(),
                ),
            )
            .label("rank"),
        )
        .where(Run.workspace_id == workspace_id, Run.project_id.in_(project_ids))
        .subquery()
    )
    newest = aliased(Run, ranked)
    rows = (await db.execute(sa.select(newest).where(ranked.c.rank == 1))).scalars().all()
    return counts, {run.project_id: run for run in rows}


def _run_summary(run: Run, names: dict[uuid.UUID, str]) -> RunSummary:
    return RunSummary(
        id=run.id,
        project_id=run.project_id,
        status=run.status,
        mode=run.mode,
        trigger=run.trigger,
        triggered_by=run.triggered_by,
        triggered_by_name=names.get(run.triggered_by) if run.triggered_by else None,
        started_at=run.started_at,
        finished_at=run.finished_at,
        cost_usd=run.cost_usd,
        token_in=run.token_in,
        token_out=run.token_out,
        error=run.error,
    )


async def _detail(
    db: AsyncSession, me: Principal, project: Project, response: Response
) -> ProjectDetail:
    names = await _user_names(db, me.workspace_id)
    counts, latest = await _run_rollup(db, me.workspace_id, [project.id])
    assignments = _assignments(project)

    # So the browser can echo it back as `If-Unmodified-Since` on the next save.
    response.headers["Last-Modified"] = format_datetime(_aware(project.updated_at), usegmt=True)

    return ProjectDetail(
        id=project.id,
        name=project.name,
        domain=project.domain,
        created_at=project.created_at,
        updated_at=project.updated_at,
        created_by=project.created_by,
        created_by_name=names.get(project.created_by),
        run_count=counts.get(project.id, 0),
        last_run=_run_summary(latest[project.id], names) if project.id in latest else None,
        product_context=ProductContext.model_validate(project.product_context or {}),
        markets=[Market.model_validate(market) for market in project.markets or []],
        models=ModelRouting.from_settings(project.settings),
        gates=[_gate_info(gate, assignments[gate.node_id], names) for gate in GATES],
        requirements=await _requirements(db, me, project),
    )


def _gate_info(gate: GateSpec, assignment: GateAssignment, names: dict[uuid.UUID, str]) -> GateInfo:
    """One gate, merged with whoever this project has pointed it at."""
    assignee_id = assignment.assignee_id
    return GateInfo(
        node_id=gate.node_id,
        stage=gate.stage,
        name=gate.name,
        audience=gate.audience,
        description=gate.description,
        required_role=gate.required_role,
        assignee_id=assignee_id,
        assignee_name=names.get(assignee_id) if assignee_id is not None else None,
        sla_hours=assignment.sla_hours,
    )


def _assignments(project: Project) -> dict[str, GateAssignment]:
    """Stored gate assignments, with an empty one for every gate that has none."""
    raw = (project.settings or {}).get(SETTINGS_APPROVALS)
    stored: dict[str, GateAssignment] = {}
    if isinstance(raw, dict):
        for node_id, value in raw.items():
            if isinstance(value, dict):
                try:
                    stored[node_id] = GateAssignment.model_validate(value)
                except ValueError:  # pragma: no cover — tolerate a stale shape
                    log.warning("project.gate_assignment_unreadable", node_id=node_id)
    return {gate.node_id: stored.get(gate.node_id, GateAssignment()) for gate in GATES}


async def _requirements(
    db: AsyncSession, me: Principal, project: Project
) -> list[ProjectRequirement]:
    """What stands between this project and a run.

    Blocking entries stop a launch. Non-blocking ones are the sources PRD §15
    NF4 lets a run proceed without: the report names them under
    `degraded_sources` rather than the run refusing to start.
    """
    found: list[ProjectRequirement] = []
    context = ProductContext.model_validate(project.product_context or {})
    if not context.summary.strip():
        found.append(
            ProjectRequirement(
                code="product_context",
                detail="Describe what this brand sells — every research node is grounded on it.",
                blocking=True,
            )
        )
    if not project.markets:
        found.append(
            ProjectRequirement(
                code="markets",
                detail="Add at least one market. Keyword volume and CPC are per country.",
                blocking=True,
            )
        )

    kinds = set(
        (
            await db.execute(
                sa.select(Credential.kind).where(Credential.workspace_id == me.workspace_id)
            )
        )
        .scalars()
        .all()
    )
    if CredentialKind.OPENROUTER not in kinds:
        found.append(
            ProjectRequirement(
                code="openrouter_credential",
                detail="Add an OpenRouter key in Settings. Every node is a model call.",
                blocking=True,
            )
        )
    if CredentialKind.GOOGLE_ADS not in kinds:
        found.append(
            ProjectRequirement(
                code="google_ads_credential",
                detail=(
                    "No Google Ads credential. The run will skip account history rather than "
                    "invent it — upload the same reports as CSV if you have them."
                ),
                blocking=False,
            )
        )
    if CredentialKind.DATAFORSEO not in kinds:
        found.append(
            ProjectRequirement(
                code="dataforseo_credential",
                detail="No DataForSEO credential. Keyword volume and CPC will be missing.",
                blocking=False,
            )
        )

    in_flight = (
        await db.execute(
            sa.select(Run.id)
            .where(
                Run.workspace_id == me.workspace_id,
                Run.project_id == project.id,
                Run.status.not_in(list(TERMINAL_STATUSES)),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if in_flight is not None:
        found.append(
            ProjectRequirement(
                code="run_in_flight",
                detail="A run is already in progress for this project.",
                blocking=True,
            )
        )
    return found


def _aware(value: datetime) -> datetime:
    """The column is `timestamptz`, but SQLite-backed unit tests are not."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
