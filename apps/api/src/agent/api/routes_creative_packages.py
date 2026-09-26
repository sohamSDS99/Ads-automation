"""Package, release and the Stage 05 contract (Stage 04 PRD §16, §12.3, §12.4).

* `POST /creative-packages/{id}/release` — `CREATIVE_RELEASE`. One
  transaction (`creative/release.py`); guarded by `UPDATE … WHERE
  status='ready_to_release'`, so the loser of two concurrent releases gets
  `409`. Offer drift, a claim that expired, or `plan_superseded` at `now` is
  a `409` naming each offending asset (contract rule 5).
"""

from __future__ import annotations

import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from agent import queue
from agent.api import problems
from agent.api.middleware import client_ip
from agent.api.schemas_creative_packages import ReleaseRequest, ReleaseResponse
from agent.auth.deps import Principal, require
from agent.auth.rbac import Permission
from agent.creative import release
from agent.db.session import get_session

log = structlog.get_logger(__name__)

router = APIRouter(tags=["creative"])

Db = Annotated[AsyncSession, Depends(get_session)]
AnyMember = Annotated[Principal, Depends(require(Permission.READ))]
#: `admin` and `approver` (PRD §5.1).
Releaser = Annotated[Principal, Depends(require(Permission.CREATIVE_RELEASE))]


@router.post("/creative-packages/{package_id}/release", response_model=ReleaseResponse)
async def release_package(
    package_id: uuid.UUID, body: ReleaseRequest, me: Releaser, db: Db, request: Request
) -> ReleaseResponse:
    try:
        released = await release.release_package(
            db,
            workspace_id=me.workspace_id,
            package_id=package_id,
            actor_id=me.user.id,
            confirm_version=body.confirm_version,
            ip=client_ip(request),
        )
    except release.PackageNotFound as exc:
        raise problems.not_found(str(exc)) from exc
    except release.ReleaseRefused as exc:
        raise problems.conflict(
            exc.detail, title="Release refused", code=exc.code, **exc.extra
        ) from exc
    except queue.WorkerUnavailable as exc:
        raise problems.Problem(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            title="Package files not written",
            detail="The worker that owns file storage did not write this package's files, so "
            "nothing was released. Try again in a minute.",
        ) from exc
    row = released.row
    assert row.package_hash is not None and row.released_at is not None  # noqa: S101
    return ReleaseResponse(
        package_id=row.id,
        creative_run_id=row.creative_run_id,
        version=row.version,
        status=row.status.value,
        package_hash=row.package_hash,
        released_at=row.released_at,
        released_by=row.released_by,
        superseded=released.superseded,
        files=len(released.package.manifest),
    )
