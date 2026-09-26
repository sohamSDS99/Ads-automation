"""Release a `CreativePackage` — one transaction (Stage 04 PRD §12.4, §16 contract rule 5).

`release_package` commits everything or nothing, in this order:

1. **Serialise and claim.** A transaction-scoped advisory lock on the project
   serialises releases within it (two packages of one project would otherwise
   both mint `max(version) + 1`), then `UPDATE … WHERE status =
   'ready_to_release'` claims the row — the loser of two concurrent releases
   of one package finds it `released` and gets `409 package_not_releasable`.
2. **The plan is not superseded** (`plan_superseded = false`, and the plan
   row itself not `superseded`).
3. **It is still the package 4.7.2 checked.** The package is assembled again
   from the rows; any difference in content (the version and the text spend
   aside) is `409 package_stale`, naming the assets that moved.
4. **The blocking checklist again, at `now`** (`checklist.run_checks` —
   claims can expire and offers can end between assembly and release), with
   every `OfferBinding` re-resolved against the LIVE `offer_record` rows
   (check 6). Any blocking issue is a 409 naming each offending asset:
   `offer_drift`, `claim_unlicensed`, or `release_blocked` for a mixture.
5. **Mint** `version = max(version) + 1` for the project; it must be the
   version the approver confirmed (`409 version_mismatch`).
6. **Freeze** every shipped `CreativeAsset` (`frozen_at`; trigger
   `creative_asset_frozen` makes it immutable from here).
7. **Write the package files** under `package/{package_id}/` through the
   worker, which owns the Volume, and check the bytes that landed against
   the manifest (`409 package_file_mismatch`; no worker is a 503).
8. **Supersede** the project's previously released package.
9. **Release** the row in ONE statement — status, version, the payload with
   its new `package_hash`, the manifest, who and when — because trigger
   `creative_package_released` freezes the row the moment its status is
   `released`, so a second statement could not follow.
10. **Audit** the release, and commit.

Any refusal or failure rolls the whole transaction back. The one effect a
rollback cannot take back is a file already written under
`package/{package_id}/`: it is keyed by the package, not the version, so a
retry overwrites it with the same bytes and nothing ever reads an unreleased
package's files.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

import sqlalchemy as sa
import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent import queue
from agent.audit import AuditAction, AuditTarget, write_audit
from agent.creative import checklist, lint_adapter
from agent.creative.lint_adapter import LintAdapterError
from agent.creative.package import (
    AssemblyError,
    PackageFile,
    assemble,
    content_digest,
    hashed,
    read_outputs,
    read_snapshot,
)
from agent.db.models import CampaignPlan, CampaignPlanStatus, CreativeAsset, Project, Run
from agent.db.models import CreativePackage as CreativePackageRow
from agent.db.models import CreativePackageStatus as Status
from agent.orchestrator.creative_run import CreativeRunError, load_resources
from agent.schemas.creative_package import CreativePackage, CritiqueIssue

log = structlog.get_logger(__name__)

#: The problem code a refusal carries when every blocking issue is of one check.
CHECK_CODES: Final[Mapping[str, str]] = {
    "check_4": "claim_unlicensed",
    "check_6": "offer_drift",
}
#: How many offending findings a 409's `detail` spells out.
DETAIL_FINDINGS: Final = 8


def clock() -> datetime:
    """`now` for a release — one seam, so a test can release after a claim expires."""
    return datetime.now(UTC)


class PackageNotFound(LookupError):
    pass


class ReleaseRefused(Exception):
    """A release that cannot go ahead. Nothing was changed."""

    def __init__(self, code: str, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.extra = extra


@dataclass(slots=True)
class Released:
    row: CreativePackageRow
    package: CreativePackage
    superseded: list[uuid.UUID] = field(default_factory=list)


async def release_package(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    package_id: uuid.UUID,
    actor_id: uuid.UUID,
    confirm_version: int,
    ip: str | None = None,
) -> Released:
    """Release one package. Raises `PackageNotFound`, `ReleaseRefused` or
    `queue.WorkerUnavailable` — each after rolling back — or commits."""
    try:
        return await _release(
            db,
            workspace_id=workspace_id,
            package_id=package_id,
            actor_id=actor_id,
            confirm_version=confirm_version,
            ip=ip,
        )
    except BaseException:
        await db.rollback()
        raise


async def _release(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    package_id: uuid.UUID,
    actor_id: uuid.UUID,
    confirm_version: int,
    ip: str | None,
) -> Released:
    project_id = await db.scalar(
        sa.select(CreativePackageRow.project_id).where(
            CreativePackageRow.id == package_id, CreativePackageRow.workspace_id == workspace_id
        )
    )
    if project_id is None:
        raise PackageNotFound(f"No creative package {package_id}.")

    # 1 — one release at a time per project, then the guard.
    await db.execute(
        sa.select(
            sa.func.pg_advisory_xact_lock(
                sa.func.hashtextextended(f"creative_package.release:{project_id}", 0)
            )
        )
    )
    claimed = await db.execute(
        sa.update(CreativePackageRow)
        .where(
            CreativePackageRow.id == package_id,
            CreativePackageRow.status == Status.READY_TO_RELEASE,
        )
        .values(updated_at=sa.func.now())
        .returning(CreativePackageRow.id)
    )
    if claimed.first() is None:
        row = await _row(db, package_id)
        raise ReleaseRefused(
            "package_not_releasable",
            f"Package {package_id} is {row.status.value.replace('_', ' ')}"
            + (f" as v{row.version}" if row.version else "")
            + "; only a package ready to release can be released.",
            status=row.status.value,
        )
    row = await _row(db, package_id)
    run = await db.get(Run, row.creative_run_id)
    project = await db.get(Project, row.project_id)
    assert run is not None and project is not None  # noqa: S101 — FKs
    try:
        resources = await load_resources(db, run, project)
        ruleset = (
            await lint_adapter.load(
                db, workspace_id=workspace_id, pin=lint_adapter.current_pin(run)
            )
        ).ruleset
    except (CreativeRunError, LintAdapterError) as exc:
        raise ReleaseRefused("run_unreadable", f"Run {run.id} cannot be re-checked: {exc}") from exc
    stored = CreativePackage.model_validate(row.payload)
    now = clock()

    # 2 — the plan it was written for is still the plan.
    plan = await db.get(CampaignPlan, row.plan_id)
    if row.plan_superseded or plan is None or plan.status is CampaignPlanStatus.SUPERSEDED:
        raise ReleaseRefused(
            "plan_superseded",
            f"Plan v{row.plan_version}, which package {package_id} was written for, has been "
            "superseded. Start a new creative run on the current plan.",
        )

    # 3 — still what 4.7.2 checked.
    outputs = await read_outputs(db, run.id)
    snap = await read_snapshot(
        db,
        run,
        inp=resources.input,
        ruleset=ruleset,
        constants=resources.constants,
        outputs=outputs,
        package_id=row.id,
        version=row.version,
        status=row.status.value,
    )
    try:
        rebuilt, files = assemble(snap)
    except AssemblyError as exc:
        raise ReleaseRefused("package_stale", f"The rows no longer assemble: {exc}") from exc
    if content_digest(rebuilt.model_dump(mode="json")) != content_digest(row.payload):
        changed = changed_assets(stored, rebuilt)
        raise ReleaseRefused(
            "package_stale",
            f"Package {package_id} is no longer what 4.7.2 checked: "
            + (
                f"asset(s) {', '.join(map(str, changed))} changed since"
                if changed
                else "it changed"
            )
            + ". Re-run 4.7 to assemble and check it again.",
            offending=[{"check": "stale", "asset_ids": [str(a) for a in changed]}],
        )

    # 4 — the thirteen checks, at now, against live rows.
    context = await checklist.read_context(
        db,
        package=stored,
        project=project,
        ruleset=ruleset,
        constants=resources.constants,
        outputs=outputs,
        now=now,
    )
    blocking = checklist.blocking(checklist.run_checks(context))
    if blocking:
        raise ReleaseRefused(_code(blocking), _detail(package_id, blocking), offending=[
            {"check": i.check, "asset_ids": [str(a) for a in i.asset_ids], "finding": i.finding}
            for i in blocking
        ])  # fmt: skip

    # 5 — mint.
    version = await _next_version(db, row.project_id)
    if confirm_version != version:
        raise ReleaseRefused(
            "version_mismatch",
            f"This package will be released as v{version}, and the request confirmed "
            f"v{confirm_version}. Reload the package: another version was probably released "
            "in this project while the dialog was open.",
            expected=version,
            submitted=confirm_version,
        )
    final = hashed(stored.model_copy(update={"version": version, "status": "released"}))

    # 6 — freeze what ships.
    shipped = sorted(shipped_assets(final), key=str)
    frozen = await db.execute(
        sa.update(CreativeAsset)
        .where(
            CreativeAsset.id.in_(shipped),
            CreativeAsset.creative_run_id == run.id,
            CreativeAsset.frozen_at.is_(None),
        )
        .values(frozen_at=now)
    )
    if frozen.rowcount != len(shipped):  # type: ignore[attr-defined]
        raise ReleaseRefused(
            "asset_frozen",
            f"{len(shipped) - frozen.rowcount} of the package's {len(shipped)} assets are already "  # type: ignore[attr-defined]
            "frozen or gone, so it cannot be released as assembled.",
        )

    # 7 — the files, written by the worker and checked against the manifest.
    receipt = await queue.write_package_files(
        {"package_id": str(row.id), "files": [_file(file) for file in files]}
    )
    _verify(final, receipt)

    # 8 — the prior release steps aside.
    superseded = list(
        (
            await db.execute(
                sa.update(CreativePackageRow)
                .where(
                    CreativePackageRow.project_id == row.project_id,
                    CreativePackageRow.status == Status.RELEASED,
                    CreativePackageRow.id != row.id,
                )
                .values(status=Status.SUPERSEDED)
                .returning(CreativePackageRow.id)
            )
        ).scalars()
    )

    # 9 — the one statement that releases.
    approvals = [d.approval_id for d in final.decisions if d.status == "approved"]
    released = await db.execute(
        sa.update(CreativePackageRow)
        .where(
            CreativePackageRow.id == row.id, CreativePackageRow.status == Status.READY_TO_RELEASE
        )
        .values(
            status=Status.RELEASED,
            version=version,
            payload=final.model_dump(mode="json"),
            manifest=[entry.model_dump(mode="json") for entry in final.manifest],
            package_hash=final.package_hash,
            released_at=now,
            released_by=actor_id,
            released_approval_ids=approvals,
        )
    )
    if released.rowcount != 1:  # type: ignore[attr-defined]  # pragma: no cover — the lock
        raise ReleaseRefused("package_not_releasable", f"Package {package_id} changed status.")

    # 10 — audit, and commit.
    write_audit(
        db,
        workspace_id=workspace_id,
        actor_id=actor_id,
        action=AuditAction.CREATIVE_PACKAGE_RELEASED,
        target_type=AuditTarget.CREATIVE_PACKAGE,
        target_id=row.id,
        meta={
            "project_id": str(row.project_id),
            "creative_run_id": str(row.creative_run_id),
            "version": version,
            "package_hash": final.package_hash,
            "files": len(final.manifest),
            "assets": len(shipped),
            "superseded": [str(value) for value in superseded],
        },
        ip=ip,
    )
    try:
        await db.commit()
    except IntegrityError as exc:
        raise ReleaseRefused(
            "version_taken",
            f"Version {version} was taken by another release a moment ago. Reload the "
            "package; the next version will be one higher.",
            expected=version + 1,
            submitted=confirm_version,
        ) from exc
    log.info(
        "creative_package.released",
        package_id=str(row.id),
        version=version,
        files=len(final.manifest),
        superseded=len(superseded),
    )
    return Released(row=await _row(db, package_id), package=final, superseded=superseded)


async def _row(db: AsyncSession, package_id: uuid.UUID) -> CreativePackageRow:
    row = (
        await db.execute(
            sa.select(CreativePackageRow)
            .where(CreativePackageRow.id == package_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return row


async def _next_version(db: AsyncSession, project_id: uuid.UUID) -> int:
    """§12.4: `max(version) + 1` over every package of the project — drafts are 0."""
    highest = await db.scalar(
        sa.select(sa.func.max(CreativePackageRow.version)).where(
            CreativePackageRow.project_id == project_id
        )
    )
    return int(highest or 0) + 1


def shipped_assets(package: CreativePackage) -> set[uuid.UUID]:
    """Every asset the package ships — what release freezes."""
    return {
        *(asset.asset_id for c in package.campaigns for asset in c.text_assets),
        *(media.asset_id for c in package.campaigns for media in (*c.media, *c.logos)),
    }


def changed_assets(stored: CreativePackage, rebuilt: CreativePackage) -> list[uuid.UUID]:
    """The assets whose shipped form differs between two assemblies of one run."""

    def forms(package: CreativePackage) -> dict[uuid.UUID, str]:
        found: dict[uuid.UUID, str] = {}
        for campaign in package.campaigns:
            for asset in campaign.text_assets:
                found[asset.asset_id] = asset.model_dump_json()
            for media in (*campaign.media, *campaign.logos):
                found[media.asset_id] = media.model_dump_json()
        return found

    old, new = forms(stored), forms(rebuilt)
    return sorted(
        (asset for asset in old.keys() | new.keys() if old.get(asset) != new.get(asset)), key=str
    )


def _code(blocking: Sequence[CritiqueIssue]) -> str:
    checks = {issue.check for issue in blocking}
    if len(checks) == 1:
        return CHECK_CODES.get(next(iter(checks)), "release_blocked")
    return "release_blocked"


def _detail(package_id: uuid.UUID, blocking: Sequence[CritiqueIssue]) -> str:
    shown = "; ".join(issue.finding.rstrip(".") for issue in blocking[:DETAIL_FINDINGS])
    more = len(blocking) - DETAIL_FINDINGS
    return (
        f"Package {package_id} cannot be released now — {len(blocking)} blocking issue(s): "
        f"{shown}{f'; and {more} more' if more > 0 else ''}."
    )


def _file(file: PackageFile) -> dict[str, Any]:
    item: dict[str, Any] = {"path": file.path, "media_type": file.media_type}
    if file.source_key is not None:
        item["source_key"] = file.source_key
    else:
        item["content"] = file.content
    return item


def _verify(package: CreativePackage, receipt: Mapping[str, Any]) -> None:
    """Every manifest file was written, and hashes to its entry — nothing else was written."""
    landed = {str(item["path"]): item for item in receipt.get("files", [])}
    wrong: list[str] = []
    for entry in package.manifest:
        item = landed.pop(entry.path, None)
        if item is None or item.get("sha256") != entry.sha256 or item.get("bytes") != entry.bytes:
            wrong.append(entry.path)
    wrong.extend(sorted(landed))
    if wrong:
        raise ReleaseRefused(
            "package_file_mismatch",
            f"{len(wrong)} package file(s) did not land as the manifest says: "
            f"{', '.join(wrong[:DETAIL_FINDINGS])}. Nothing was released.",
            paths=wrong,
        )
