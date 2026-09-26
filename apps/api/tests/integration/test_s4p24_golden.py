"""S4-P24 — PRD §17 CC16: three golden `CreativeInput` fixtures produce packages
passing every §11 blocking check, against their cassettes.

Each fixture (`golden_creative.GOLDENS`) is seeded, started through the real
route with media models off the allowlist ∩ the recorded catalogue, and driven
through the whole 24-node DAG by the real executor — every stop decided as an
operator decides it — with every provider response replayed from the
fixture's cassette. The package is then read from the database: 4.7.2 ran all
13 checks and found nothing blocking, and the package is what the fixture is
for (a lead form; a PMax asset group with images and bound offers; video).

Runs in the worker image (`ffmpeg`, `exiftool`, `tesseract`).
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
import respx
from sqlalchemy.ext.asyncio import AsyncSession

from agent.creative import checklist
from agent.creative.package import package_hash, package_row
from agent.db.models import CreativePackageStatus
from agent.schemas.creative_package import CreativeCritique, CreativePackage
from agent.storage.local import LocalStorage
from tests.integration.conftest import ApiClient
from tests.integration.golden_creative import GOLDENS, Golden, World, run_golden
from tests.integration.test_s4p5_headlines_combinations import _output
from tests.integration.test_s4p8_extras import _Web, rendered_form, web

pytestmark = pytest.mark.asyncio

__all__ = ["rendered_form", "web"]


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> LocalStorage:
    from agent.config import get_settings

    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    get_settings.cache_clear()
    return LocalStorage(str(tmp_path))


def _stops(golden: Golden) -> list[str]:
    """The stops a fixture makes: G7 always; G8 when it generates media; H3
    because S4-P6's scripted copy carries an unlicensed claim the operator
    withdraws (S4-P14)."""
    return ["G7", *(["G8"] if golden.images or golden.video else []), "H3"]


@pytest.mark.parametrize("golden", GOLDENS, ids=[g.name for g in GOLDENS])
async def test_a_golden_creative_input_produces_a_package_passing_every_blocking_check(
    golden: Golden,
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    web: _Web,
    rendered_form: None,
    storage: LocalStorage,
) -> None:
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        world = World(golden, router)
        run_id, stops = await run_golden(
            admin, db, golden, (workspace_id, project_id, admin_user.id), world
        )
    assert stops == _stops(golden)
    assert world.cassette.unplayed() == []

    critique = CreativeCritique.model_validate(await _output(db, run_id, "4.7.2"))
    assert critique.checks_run == len(checklist.CHECKS) == 13
    assert [i for i in critique.issues if i.severity == "blocking"] == []
    assert (critique.blocking, critique.failed_checks, critique.status) == (
        0,
        [],
        "ready_to_release",
    )

    row = await package_row(db, run_id)
    assert row is not None and row.status is CreativePackageStatus.READY_TO_RELEASE
    package = CreativePackage.model_validate(row.payload)
    assert package.package_hash == package_hash(row.payload)
    by_ref = {campaign.campaign_ref: campaign for campaign in package.campaigns}
    assert sorted(by_ref) == sorted(golden.campaign_refs)
    assert all(campaign.launch_minimums.met for campaign in package.campaigns)

    search = by_ref["c-sds-us"]
    assert sorted(ad.variant for ad in search.ads) == ["A", "B"]
    assert search.extensions.sitelinks and search.extensions.callouts
    assert bool(search.extensions.lead_form) is golden.lead_form
    assert bool(search.extensions.promotions) is golden.offers
    assert bool(search.extensions.prices) is golden.offers

    if golden.pmax:
        (group,) = by_ref["c-pmax-us"].asset_groups
        assert group.headlines and group.long_headlines and group.descriptions
        assert group.business_name is not None
    modalities = {str(m.modality) for c in package.campaigns for m in c.media}
    assert ("image" in modalities) is golden.images
    assert ("video" in modalities) is golden.video
