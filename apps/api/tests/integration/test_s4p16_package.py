"""S4-P16 exit criteria — the package (4.7.1, 4.7.2), release, and the Stage 05 read contract.

The golden run is S4-P8's whole-DAG run — variant A and B ads on licensed
claims, sitelinks through the scripted web, promotions and prices bound to
the offer rows, the H3 withdrawal S4-P6's copy makes necessary — through the
real 4.7.1 and 4.7.2. Every assertion reads the database or a route, never
the event stream.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent.creative import checklist
from agent.creative.package import package_hash, package_row
from agent.db.models import CreativePackage as CreativePackageRow
from agent.db.models import CreativePackageStatus, RunStatus
from agent.orchestrator.dag import Dag
from agent.schemas.creative_package import CreativeCritique, CreativePackage, PackageAssembly
from tests.integration.conftest import ApiClient
from tests.integration.creative_support import TEXT_ONLY
from tests.integration.runs_support import execute
from tests.integration.s4p14_support import past_h3
from tests.integration.test_s4p4_brief_g7 import _g7
from tests.integration.test_s4p5_headlines_combinations import _output
from tests.integration.test_s4p6_descriptions_variant_b import _registry, _seed
from tests.integration.test_s4p8_extras import (
    CRAWLED,
    EXTRA_SPECS,
    FRESH,
    OFFER_SPECS,
    _crawl,
    _offers,
    _run,
    _Script,
    _Web,
    web,
)
from tests.integration.test_stage04_schema import _creative_run, _package

pytestmark = pytest.mark.asyncio

__all__ = ["web"]


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
    """S4-P8's run with every extra specified, the offers fresh and the site crawled."""
    await _offers(db, project_id, FRESH)
    await _crawl(db, project_id, CRAWLED)
    await _seed(db, ws, project_id, actor, extra_specs={"search": {**EXTRA_SPECS, **OFFER_SPECS}})
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
