"""S4-P16 exit criteria — the package (4.7.1, 4.7.2), release, and the Stage 05 read contract.

The golden run is S4-P8's whole-DAG run — variant A and B ads on licensed
claims, sitelinks through the scripted web, promotions and prices bound to
the offer rows, the H3 withdrawal S4-P6's copy makes necessary — through the
real 4.7.1 and 4.7.2. Every assertion reads the database or a route, never
the event stream.
"""

from __future__ import annotations

import asyncio
import functools
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent import queue, worker
from agent.audit import AuditAction
from agent.creative import checklist, offers, release
from agent.creative.package import package_hash, package_row, sha256
from agent.db.models import (
    AuditLog,
    CampaignPlan,
    CampaignPlanStatus,
    CreativeAsset,
    CreativePackageStatus,
    Evidence,
    EvidenceSource,
    RunStatus,
)
from agent.db.models import CreativePackage as CreativePackageRow
from agent.orchestrator.dag import Dag
from agent.schemas.creative_package import CreativeCritique, CreativePackage, PackageAssembly
from agent.schemas.guardrails import OfferRecord
from agent.storage.local import LocalStorage
from tests.integration import test_s4p6_descriptions_variant_b as s4p6
from tests.integration.conftest import ApiClient, build_client, make_member
from tests.integration.creative_support import TEXT_ONLY, seed_published
from tests.integration.runs_support import execute
from tests.integration.s4p14_support import past_h3
from tests.integration.test_s4p4_brief_g7 import _g7
from tests.integration.test_s4p5_headlines_combinations import _output
from tests.integration.test_s4p6_descriptions_variant_b import _registry, _seed
from tests.integration.test_s4p8_extras import (
    CRAWLED,
    DISQUALIFIERS,
    EXTRA_SPECS,
    FRESH,
    OFFER_SPECS,
    REQUIRED,
    _crawl,
    _offers,
    _run,
    _Script,
    _Web,
    rendered_form,
    web,
)
from tests.integration.test_stage04_schema import _creative_run, _package

pytestmark = pytest.mark.asyncio

__all__ = ["rendered_form", "web"]


def _now() -> str:
    return datetime.now(UTC).isoformat()


#: The seeded plan's second campaign, `c-brand`, has no ad groups — nothing for
#: Stage 04 to write, so check 11 rightly blocks any package that includes it.
#: A golden run is scoped to the campaign that has them.
GOLDEN_SCOPE = {**TEXT_ONLY, "campaign_refs": ["c-sds-us"]}


async def run_creative(admin: ApiClient, db: AsyncSession, project_id: uuid.UUID) -> uuid.UUID:
    """A creative run on what is seeded: started, G7 approved, H3 withdrawn, run to its end."""
    started = await admin.post(
        f"/projects/{project_id}/creative/runs", json={"scope": GOLDEN_SCOPE, "media_models": []}
    )
    assert started.status_code == 202, started.text
    run_id = uuid.UUID(started.json()["run_id"])
    script = _Script()
    registry = _registry()

    def again() -> Any:
        return execute(
            run_id, script.fake, registry=registry, dag=Dag.from_registry(registry), max_attempts=1
        )

    halted = await execute(run_id, script.fake, registry=registry, dag=Dag.from_registry(registry))
    assert halted.status is RunStatus.AWAITING_APPROVAL, halted.error
    decided = await admin.post(
        f"/approvals/{(await _g7(db, run_id)).id}", json={"decision": "approve"}
    )
    assert decided.status_code == 200, decided.text
    result = await past_h3(admin, run_id, await again(), again)
    assert result.status is RunStatus.SUCCEEDED, result.error
    return run_id


async def golden(
    admin: ApiClient, db: AsyncSession, ws: uuid.UUID, project_id: uuid.UUID, actor: uuid.UUID
) -> uuid.UUID:
    """S4-P8's run with every extra specified, the offers fresh and the site crawled.

    With `rendered_form` requested, the landing page renders with its seven
    field form; the plan then carries S4-P8's qualified lead (the signals its
    scripted field labels answer), 4.5.2 trims the form and writes a patch —
    so the package ships landing-patch files for release to write.
    """
    await _offers(db, project_id, FRESH)
    await _crawl(db, project_id, CRAWLED)
    await _seed(
        db,
        ws,
        project_id,
        actor,
        extra_specs={"search": {**EXTRA_SPECS, **OFFER_SPECS}},
        plan={"required_signals": REQUIRED, "disqualifiers": DISQUALIFIERS},
    )
    return await run_creative(admin, db, project_id)


async def _package_of(db: AsyncSession, run_id: uuid.UUID) -> CreativePackageRow:
    row = await package_row(db, run_id)
    assert row is not None, f"run {run_id} wrote no package"
    return row


# ---------------------------------------------------------------------------
# migration 0022 — drafts are version 0; minted versions stay unique
# ---------------------------------------------------------------------------


async def test_unreleased_packages_share_version_0_and_minted_versions_stay_unique(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    first_run = await _creative_run(db, workspace_id, project_id, admin_user.id)
    first = await _package(db, first_run, admin_user.id, status=CreativePackageStatus.DRAFT)
    first.version = 0
    second_run = await _creative_run(db, workspace_id, project_id, admin_user.id)
    second = CreativePackageRow(
        workspace_id=workspace_id,
        project_id=project_id,
        creative_run_id=second_run.id,
        schema_version="1.0",
        version=0,
        status=CreativePackageStatus.BLOCKED,
        plan_id=first.plan_id,
        plan_version=1,
        guideline_id=first.guideline_id,
        ruleset_version="v1.0",
        payload={},
    )
    db.add(second)
    await db.flush()  # two unreleased packages in one project: both version 0

    first.version, second.version = 1, 1
    with pytest.raises(IntegrityError, match="uq_creative_package_project_version_minted"):
        await db.flush()


# ---------------------------------------------------------------------------
# 4.7.1 + 4.7.2 — a golden run passes all thirteen
# ---------------------------------------------------------------------------


async def test_a_golden_run_produces_a_package_passing_all_thirteen_blocking_checks(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    web: _Web,
) -> None:
    run_id = await golden(admin, db, workspace_id, project_id, admin_user.id)

    critique = CreativeCritique.model_validate(await _output(db, run_id, "4.7.2"))
    assert critique.checks_run == len(checklist.CHECKS) == 13
    assert [i for i in critique.issues if i.severity == "blocking"] == []
    assert (critique.blocking, critique.failed_checks, critique.status) == (
        0,
        [],
        "ready_to_release",
    )

    row = await _package_of(db, run_id)
    assert (row.status, row.version) is not None
    assert row.status is CreativePackageStatus.READY_TO_RELEASE
    assert row.version == 0 and row.manifest is None and row.package_hash is None
    package = CreativePackage.model_validate(row.payload)
    assert package.status == "ready_to_release"
    assert package.package_hash == package_hash(row.payload)
    assembly = PackageAssembly.model_validate(await _output(db, run_id, "4.7.1"))
    assert assembly.package_id == package.package_id == row.id
    assert assembly.package_hash == package.package_hash

    # What the golden run ships: A and B ads, and the extras 4.3.x wrote.
    (campaign,) = package.campaigns
    assert sorted(ad.variant for ad in campaign.ads) == ["A", "B"]
    assert all(ad.pair_report.unresolved == [] for ad in campaign.ads)
    b = next(ad for ad in campaign.ads if ad.variant == "B")
    assert b.distinctness_vs_a is not None and b.hypothesis
    assert campaign.extensions.sitelinks and campaign.extensions.promotions
    assert campaign.extensions.prices
    kinds = {asset.kind for asset in campaign.text_assets}
    assert {"headline", "description", "sitelink", "promotion", "price"} <= kinds
    assert campaign.launch_minimums.met
    assert package.pins.ruleset_version == row.ruleset_version
    assert {d.gate_key for d in package.decisions} >= {"G7"}
    (h3,) = package.human_tasks
    assert h3.status in ("not_required", "decided")


async def test_a_package_whose_checks_fail_is_blocked_and_the_run_still_succeeds(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    web: _Web,
) -> None:
    """Without the extras specs the pin names no sitelink minimum — so the one
    thing this run breaks is the offers: none were loaded, so 4.3.2 writes no
    promotion, and the price minimum (3) of OFFER_SPECS is unmet with no
    dependency to blame. §8 terminal state: `succeeded`, package `blocked`."""
    await _crawl(db, project_id, CRAWLED)
    run_id, _, status = await _run(
        admin, db, workspace_id, project_id, admin_user.id, extra_specs=OFFER_SPECS
    )
    assert status.value == "succeeded"
    critique = CreativeCritique.model_validate(await _output(db, run_id, "4.7.2"))
    assert critique.status == "blocked" and "check_11" in critique.failed_checks
    row = await _package_of(db, run_id)
    assert row.status is CreativePackageStatus.BLOCKED
    assert (
        await db.scalar(
            sa.select(sa.func.count())
            .select_from(CreativePackageRow)
            .where(CreativePackageRow.creative_run_id == run_id)
        )
        == 1
    )


# ---------------------------------------------------------------------------
# release (§12.4) — one transaction
# ---------------------------------------------------------------------------


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> LocalStorage:
    from agent.config import get_settings

    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    get_settings.cache_clear()
    return LocalStorage(str(tmp_path))


@pytest.fixture
def worker_writes(monkeypatch: pytest.MonkeyPatch, storage: LocalStorage) -> list[dict[str, Any]]:
    """`queue.write_package_files` answered by the real worker job, in process."""
    calls: list[dict[str, Any]] = []

    async def write(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(payload)
        return await worker.write_package_files({}, payload)

    monkeypatch.setattr(release.queue, "write_package_files", write)
    return calls


async def _release(api: ApiClient, package_id: uuid.UUID, version: int = 1) -> Any:
    return await api.post(
        f"/creative-packages/{package_id}/release", json={"confirm_version": version}
    )


def _shipped(package: CreativePackage) -> set[uuid.UUID]:
    return {
        *(a.asset_id for c in package.campaigns for a in c.text_assets),
        *(m.asset_id for c in package.campaigns for m in (*c.media, *c.logos)),
    }


async def _frozen(db: AsyncSession, run_id: uuid.UUID) -> dict[uuid.UUID, Any]:
    rows = (
        await db.execute(
            sa.select(CreativeAsset.id, CreativeAsset.frozen_at)
            .where(CreativeAsset.creative_run_id == run_id)
            .execution_options(populate_existing=True)
        )
    ).all()
    return {row.id: row.frozen_at for row in rows}


async def _audits(db: AsyncSession, package_id: uuid.UUID) -> list[AuditLog]:
    return list(
        (
            await db.execute(
                sa.select(AuditLog).where(
                    AuditLog.action == AuditAction.CREATIVE_PACKAGE_RELEASED,
                    AuditLog.target_id == package_id,
                )
            )
        )
        .scalars()
        .all()
    )


async def _unchanged(db: AsyncSession, run_id: uuid.UUID) -> None:
    """Nothing a refused or failed release touched survived it."""
    row = await _package_of(db, run_id)
    await db.refresh(row)
    assert row.status is CreativePackageStatus.READY_TO_RELEASE and row.version == 0
    assert row.manifest is None and row.package_hash is None and row.released_at is None
    assert all(frozen is None for frozen in (await _frozen(db, run_id)).values())
    assert await _audits(db, row.id) == []


async def test_release_mints_v1_freezes_every_shipped_asset_and_writes_verified_files(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    web: _Web,
    rendered_form: None,
    storage: LocalStorage,
    worker_writes: list[dict[str, Any]],
) -> None:
    run_id = await golden(admin, db, workspace_id, project_id, admin_user.id)
    row = await _package_of(db, run_id)
    draft = CreativePackage.model_validate(row.payload)
    assert draft.manifest, "the golden run ships landing patches, so release writes files"

    released = await _release(admin, row.id)
    assert released.status_code == 200, released.text
    body = released.json()
    assert (body["version"], body["status"]) == (1, "released")

    await db.refresh(row)
    assert row.status is CreativePackageStatus.RELEASED and row.version == 1
    assert row.released_by == admin_user.id and row.released_at is not None
    package = CreativePackage.model_validate(row.payload)
    assert (package.version, package.status) == (1, "released")
    assert row.package_hash == package.package_hash == package_hash(row.payload)
    assert row.package_hash == body["package_hash"] != draft.package_hash  # the version moved
    assert row.manifest == [entry.model_dump(mode="json") for entry in package.manifest]
    assert row.released_approval_ids and set(row.released_approval_ids) >= {
        d.approval_id for d in package.decisions if d.gate_key == "G7"
    }

    # Every shipped asset frozen at release; nothing that did not ship.
    frozen = await _frozen(db, run_id)
    shipped = _shipped(package)
    # Captured before the rollback below: a row read after it is a lazy load.
    package_id, released_hash, actor_id = row.id, row.package_hash, admin_user.id
    assert shipped and all(frozen[asset] == row.released_at for asset in shipped)
    assert all(frozen[asset] is None for asset in set(frozen) - shipped)
    with pytest.raises(DBAPIError, match="frozen at release"):
        await db.execute(
            sa.update(CreativeAsset).where(CreativeAsset.id == next(iter(shipped))).values(text="x")
        )
    await db.rollback()

    # The files are under package/, and each hashes to its manifest entry.
    (call,) = worker_writes
    assert {f["path"] for f in call["files"]} == {e.path for e in package.manifest}
    for entry in package.manifest:
        data = storage.get(f"package/{package_id}/{entry.path}")
        assert (sha256(data), len(data)) == (entry.sha256, entry.bytes)

    (audit,) = await _audits(db, package_id)
    assert audit.actor_id == actor_id
    assert audit.meta["version"] == 1 and audit.meta["package_hash"] == released_hash


async def test_release_is_one_transaction_so_a_failed_step_changes_nothing(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    web: _Web,
    rendered_form: None,
    storage: LocalStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = await golden(admin, db, workspace_id, project_id, admin_user.id)
    row = await _package_of(db, run_id)

    # The worker never writes the files — after the assets were frozen in the
    # transaction: the freeze must not survive.
    async def dead(payload: dict[str, Any]) -> dict[str, Any]:
        raise queue.WorkerUnavailable("no worker")

    monkeypatch.setattr(release.queue, "write_package_files", dead)
    refused = await _release(admin, row.id)
    assert refused.status_code == 503, refused.text
    await _unchanged(db, run_id)

    # A file the worker wrote that does not hash to its manifest entry.
    async def corrupt(payload: dict[str, Any]) -> dict[str, Any]:
        receipt = await worker.write_package_files({}, payload)
        receipt["files"][0]["sha256"] = "0" * 64
        return receipt

    monkeypatch.setattr(release.queue, "write_package_files", corrupt)
    mismatch = await _release(admin, row.id)
    assert mismatch.status_code == 409 and mismatch.json()["code"] == "package_file_mismatch"
    await _unchanged(db, run_id)

    # The dialog confirmed a version other than the one release would mint.
    wrong = await _release(admin, row.id, version=2)
    assert wrong.status_code == 409, wrong.text
    assert (wrong.json()["code"], wrong.json()["expected"]) == ("version_mismatch", 1)
    await _unchanged(db, run_id)


async def test_a_mutated_offer_makes_release_409_naming_the_asset(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    web: _Web,
    worker_writes: list[dict[str, Any]],
) -> None:
    run_id = await golden(admin, db, workspace_id, project_id, admin_user.id)
    row = await _package_of(db, run_id)
    package = CreativePackage.model_validate(row.payload)
    pro = offers.record_id(OfferRecord.model_validate(FRESH[0]))
    bound = sorted(
        str(a.asset_id)
        for c in package.campaigns
        for a in c.text_assets
        if a.offer_binding is not None and a.offer_binding.offer_record_id == pro
    )
    assert bound, "the golden run binds a promotion or price to SDS-PRO"

    # The offer data moved on after assembly: a newer observation, a new price.
    db.add(
        Evidence(
            project_id=project_id,
            source=EvidenceSource.CSV,
            kind="offer_record",
            payload={**FRESH[0], "current_price": 89.0, "observed_at": _now()},
            hash="offer-SDS-PRO-reobserved",
        )
    )
    await db.commit()
    refused = await _release(admin, row.id)
    assert refused.status_code == 409, refused.text
    problem = refused.json()
    assert problem["code"] == "offer_drift"
    named = sorted({a for item in problem["offending"] for a in item["asset_ids"]})
    assert named == bound
    assert all(asset in problem["detail"] for asset in bound)
    assert worker_writes == []
    await _unchanged(db, run_id)


async def test_an_expired_claim_makes_release_409_naming_the_asset(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    web: _Web,
    worker_writes: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expires = datetime.now(UTC) + timedelta(days=2)
    monkeypatch.setattr(
        s4p6, "seed_published", functools.partial(seed_published, claim_expires_at=expires)
    )
    run_id = await golden(admin, db, workspace_id, project_id, admin_user.id)
    row = await _package_of(db, run_id)
    package = CreativePackage.model_validate(row.payload)
    descriptions = sorted(
        str(a.asset_id) for c in package.campaigns for a in c.text_assets if a.kind == "description"
    )

    # Released three days on: the claim every description stands on has expired.
    monkeypatch.setattr(release, "clock", lambda: expires + timedelta(days=1))
    refused = await _release(admin, row.id)
    assert refused.status_code == 409, refused.text
    problem = refused.json()
    assert problem["code"] == "claim_unlicensed"
    assert sorted({a for item in problem["offending"] for a in item["asset_ids"]}) == descriptions
    await _unchanged(db, run_id)


async def test_a_superseded_plan_makes_release_409(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    web: _Web,
    worker_writes: list[dict[str, Any]],
) -> None:
    run_id = await golden(admin, db, workspace_id, project_id, admin_user.id)
    row = await _package_of(db, run_id)
    await db.execute(
        sa.update(CampaignPlan)
        .where(CampaignPlan.id == row.plan_id)
        .values(status=CampaignPlanStatus.SUPERSEDED)
    )
    await db.commit()
    refused = await _release(admin, row.id)
    assert refused.status_code == 409 and refused.json()["code"] == "plan_superseded"
    await _unchanged(db, run_id)


async def test_concurrent_releases_yield_exactly_one_200_and_one_409(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    web: _Web,
    worker_writes: list[dict[str, Any]],
) -> None:
    run_id = await golden(admin, db, workspace_id, project_id, admin_user.id)
    row = await _package_of(db, run_id)
    first, second = await asyncio.gather(_release(admin, row.id), _release(admin, row.id))
    assert sorted([first.status_code, second.status_code]) == [200, 409], (first.text, second.text)
    loser = first if first.status_code == 409 else second
    assert loser.json()["code"] == "package_not_releasable"
    await db.refresh(row)
    assert row.status is CreativePackageStatus.RELEASED and row.version == 1
    assert len(await _audits(db, row.id)) == 1


async def test_only_a_creative_release_holder_may_release(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    web: _Web,
    worker_writes: list[dict[str, Any]],
) -> None:
    run_id = await golden(admin, db, workspace_id, project_id, admin_user.id)
    row = await _package_of(db, run_id)
    email, password = await make_member(admin, "operator")
    operator = build_client()
    async with operator.raw:
        assert (await operator.login(email, password)).status_code == 200
        assert (await _release(operator, row.id)).status_code == 403
    await _unchanged(db, run_id)


# ---------------------------------------------------------------------------
# the Stage 05 contract — GET /packages/released, the diff, the JSON export
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def worker_files(storage: LocalStorage) -> AsyncIterator[httpx.AsyncClient]:
    """The worker's file server, reachable the way `api` reaches it — over the
    same `STORAGE_DIR` the in-process worker jobs write to."""
    from agent.fileserver import create_file_server

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_file_server()), base_url="http://worker:8081"
    ) as client:
        yield client


@pytest.fixture
def api_dependency_overrides(worker_files: httpx.AsyncClient) -> dict[Any, Any]:
    from agent.api.worker_files import get_worker_client

    async def override() -> httpx.AsyncClient:
        return worker_files

    return {get_worker_client: override}


async def _released(api: ApiClient, project_id: uuid.UUID, pin: int | None = None) -> Any:
    params = {"project_id": str(project_id), **({"pin": str(pin)} if pin is not None else {})}
    return await api.get("/packages/released", params=params)


async def _two_releases(
    admin: ApiClient, db: AsyncSession, ws: uuid.UUID, project_id: uuid.UUID, actor: uuid.UUID
) -> tuple[CreativePackageRow, CreativePackageRow]:
    """v1 released; the offer re-observed at a new price; a second run released as v2."""
    first = await _package_of(db, await golden(admin, db, ws, project_id, actor))
    assert (await _release(admin, first.id, 1)).status_code == 200
    db.add(
        Evidence(
            project_id=project_id,
            source=EvidenceSource.CSV,
            kind="offer_record",
            payload={**FRESH[0], "current_price": 89.0, "observed_at": _now()},
            hash="offer-SDS-PRO-reobserved",
        )
    )
    await db.commit()
    second = await _package_of(db, await run_creative(admin, db, project_id))
    assert second.status is CreativePackageStatus.READY_TO_RELEASE
    released = await _release(admin, second.id, 2)
    assert released.status_code == 200, released.text
    for row in (first, second):
        await db.refresh(row)
    return first, second


async def test_released_is_404_before_release_and_the_exact_version_by_pin_after(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    web: _Web,
    worker_writes: list[dict[str, Any]],
) -> None:
    run_id = await golden(admin, db, workspace_id, project_id, admin_user.id)
    draft = await _package_of(db, run_id)
    assert draft.status is CreativePackageStatus.READY_TO_RELEASE
    # A package ready to release is still nothing Stage 05 may load.
    nothing = await _released(admin, project_id)
    assert nothing.status_code == 404, nothing.text
    assert "nothing" in nothing.json()["detail"].lower()

    assert (await _release(admin, draft.id, 1)).status_code == 200
    await db.refresh(draft)
    v1 = await _released(admin, project_id)
    assert v1.status_code == 200, v1.text
    body = v1.json()
    assert (body["package_id"], body["version"], body["status"]) == (str(draft.id), 1, "released")
    assert CreativePackage.model_validate(body).package_hash == draft.package_hash
    assert package_hash(body) == draft.package_hash
    assert (await _released(admin, project_id, pin=1)).json() == body
    assert (await _released(admin, project_id, pin=2)).status_code == 404
    assert (await _released(admin, project_id, pin=0)).status_code == 422
    assert (await _released(admin, uuid.uuid4())).status_code == 404


async def test_pin_returns_the_exact_version_even_when_superseded_and_diff_names_changes(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    web: _Web,
    worker_writes: list[dict[str, Any]],
) -> None:
    first, second = await _two_releases(admin, db, workspace_id, project_id, admin_user.id)
    assert first.status is CreativePackageStatus.SUPERSEDED and first.version == 1
    assert second.status is CreativePackageStatus.RELEASED and second.version == 2

    latest = (await _released(admin, project_id)).json()
    assert (latest["package_id"], latest["version"], latest["status"]) == (
        str(second.id), 2, "released",
    )  # fmt: skip
    pinned = await _released(admin, project_id, pin=1)
    assert pinned.status_code == 200, pinned.text
    old = pinned.json()
    assert (old["package_id"], old["version"], old["status"]) == (str(first.id), 1, "superseded")
    # Superseded, and still the exact bytes that were released: the hash holds.
    assert package_hash(old) == first.package_hash

    # v2 against v1: the offer moved between the runs, so its promotion changed.
    diffed = await admin.get(
        f"/creative-packages/{second.id}/diff", params={"against": str(first.id)}
    )
    assert diffed.status_code == 200, diffed.text
    result = diffed.json()
    assert (result["version"], result["against_version"]) == (2, 1)
    kinds = {change["kind"] for change in result["changed"]}
    assert "promotion" in kinds, result
    promotion = next(c for c in result["changed"] if c["kind"] == "promotion")
    assert promotion["from"]["fields"]["bound"] != promotion["to"]["fields"]["bound"]
    assert result["added"] == [] and result["removed"] == []
    assert (
        await admin.get(
            f"/creative-packages/{second.id}/diff", params={"against": str(uuid.uuid4())}
        )
    ).status_code == 404
    assert (await admin.get(f"/creative-packages/{second.id}/diff")).status_code == 422


async def test_the_json_export_validates_and_recomputes_to_the_released_hash(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    web: _Web,
    worker_writes: list[dict[str, Any]],
) -> None:
    from agent.export.jobs import generate_export

    run_id = await golden(admin, db, workspace_id, project_id, admin_user.id)
    row = await _package_of(db, run_id)
    assert (await _release(admin, row.id, 1)).status_code == 200
    await db.refresh(row)

    unsupported = await admin.post(
        f"/creative-packages/{row.id}/export", params={"format": "editor_zip"}
    )
    assert unsupported.status_code == 422, unsupported.text

    downloads: list[bytes] = []
    for _ in range(2):
        accepted = await admin.post(
            f"/creative-packages/{row.id}/export", params={"format": "json"}
        )
        assert accepted.status_code == 202, accepted.text
        job_id = accepted.json()["job_id"]
        done = await generate_export({}, job_id)
        assert done["status"] == "ready", done
        status_now = await admin.get(f"/exports/{job_id}")
        assert status_now.status_code == 200 and status_now.json()["status"] == "ready"
        download = await admin.get(f"/exports/{job_id}/download")
        assert download.status_code == 200, download.text
        downloads.append(download.content)

    first, second = downloads
    assert first == second  # two exports of one released package are byte-identical
    exported = json.loads(first)
    package = CreativePackage.model_validate(exported)
    assert package.version == 1 and exported["status"] == "released"
    assert package_hash(exported) == row.package_hash == exported["package_hash"]
