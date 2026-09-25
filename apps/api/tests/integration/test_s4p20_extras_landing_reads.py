"""S4-P20's two reads — what the Extras and Landing audit screens need that no route gave.

- `GET /creative-runs/{id}/assets` now carries a bound asset's `offer_binding`
  and the `OfferRecord` it was rendered from (`offer`): the observation in the
  run's pinned snapshot, its window, and the `offer_record` evidence row it
  came from — so `OfferBindingField` can show `20% off · ends … · 18 days` and
  link to the record. Read, never re-resolved: a price that moved after the
  run started does not move the window shown.
- `GET /landing-audits/{id}/screenshot?device=` streams the capture 4.5.1
  stored, through the worker's file server with a real signed token.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.creative import offers
from agent.db.models import (
    Evidence,
    EvidenceSource,
    LandingAuditVerdict,
    LandingPageAudit,
)
from agent.schemas.guardrails import OfferRecord
from tests.integration.conftest import ApiClient
from tests.integration.creative_support import TEXT_ONLY
from tests.integration.test_s4p6_descriptions_variant_b import _seed
from tests.integration.test_s4p8_extras import (
    FRESH,
    OFFER_SPECS,
    STALE,
    _offers,
    _run,
    _Web,
    web,  # noqa: F401 — the scripted web 4.3.1/4.3.3 check URLs against
)

pytestmark = pytest.mark.asyncio

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6360000002000100ffff03000006000557bfabd4"
    "0000000049454e44ae426082"
)


@pytest.fixture(autouse=True)
def storage_on_a_tmp_volume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The compose `test` service has no Volume mounted; give it one per test."""
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    from agent.config import get_settings

    get_settings.cache_clear()


@pytest_asyncio.fixture
async def worker_files() -> AsyncIterator[httpx.AsyncClient]:
    """The worker's file server, reachable the way `api` reaches it."""
    from agent.fileserver import create_file_server

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_file_server()), base_url="http://worker:8081"
    ) as client:
        yield client


@pytest.fixture
def api_dependency_overrides(worker_files: httpx.AsyncClient) -> dict[Any, Any]:
    """The route's own URL building and token signing run; only the hop is in-process."""
    from agent.api.worker_files import get_worker_client

    async def override() -> httpx.AsyncClient:
        return worker_files

    return {get_worker_client: override}


# ---------------------------------------------------------------------------
# the offer record behind a bound asset
# ---------------------------------------------------------------------------


async def _evidence_id(db: AsyncSession, project_id: uuid.UUID, sku: str) -> uuid.UUID:
    rows = (
        await db.execute(
            sa.select(Evidence.id, Evidence.payload).where(
                Evidence.project_id == project_id, Evidence.kind == "offer_record"
            )
        )
    ).all()
    (found,) = [row.id for row in rows if row.payload["sku"] == sku]
    return found


async def test_every_bound_asset_carries_its_binding_and_the_record_it_was_rendered_from(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    web: _Web,  # noqa: F811
) -> None:
    await _offers(db, project_id, [*FRESH, *STALE])
    run_id, _script, _ = await _run(
        admin, db, workspace_id, project_id, admin_user.id, extra_specs=OFFER_SPECS
    )
    response = await admin.get(f"/creative-runs/{run_id}/assets")
    assert response.status_code == 200, response.text
    items = response.json()["items"]

    bound = [item for item in items if item["kind"] in ("promotion", "price")]
    assert {item["kind"] for item in bound} == {"promotion", "price"}
    by_sku = {record["sku"]: OfferRecord.model_validate(record) for record in FRESH}
    for item in bound:
        binding, source = item["offer_binding"], item["offer"]
        assert binding is not None and source is not None, item
        record = by_sku[source["sku"]]
        # The record is the one the binding names, and renders what it holds.
        assert binding["offer_record_id"] == str(offers.record_id(record))
        assert offers.resolve(record, binding["fields"]) == binding["resolved"]
        # Its window and its evidence row, for the field's link.
        assert source["evidence_id"] == str(await _evidence_id(db, project_id, record.sku))
        assert source["product_set"] == "plans" and source["market"] == "US"
        assert datetime.fromisoformat(source["ends_at"]) == record.ends_at
        assert datetime.fromisoformat(source["effective_from"]) == record.effective_from
        assert source["effective_to"] is None

    # Everything else is unbound: no binding, no record.
    for item in items:
        if item["kind"] not in ("promotion", "price"):
            assert item["offer_binding"] is None and item["offer"] is None, item["kind"]


async def test_the_window_is_the_snapshot_s_not_a_newer_observation_and_a_deleted_row_unlinks(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    web: _Web,  # noqa: F811
) -> None:
    await _offers(db, project_id, FRESH)
    run_id, _script, _ = await _run(
        admin, db, workspace_id, project_id, admin_user.id, extra_specs=OFFER_SPECS
    )
    before = {
        item["id"]: item
        for item in (await admin.get(f"/creative-runs/{run_id}/assets")).json()["items"]
        if item["offer"] is not None
    }
    assert before

    # A newer observation of SDS-PRO after the run started: same identity, a
    # later end. The run was written against the old one, so nothing moves.
    now = datetime.now(UTC)
    pro = next(record for record in FRESH if record["sku"] == "SDS-PRO")
    db.add(
        Evidence(
            project_id=project_id,
            source=EvidenceSource.CSV,
            kind="offer_record",
            payload={
                **pro,
                "ends_at": (now + timedelta(days=60)).isoformat(),
                "observed_at": now.isoformat(),
            },
            hash="offer-SDS-PRO-later",
        )
    )
    # And SDS-TEAM's row is deleted: its asset keeps the window, loses the link.
    team = await _evidence_id(db, project_id, "SDS-TEAM")
    await db.execute(sa.delete(Evidence).where(Evidence.id == team))
    await db.commit()

    after = {
        item["id"]: item
        for item in (await admin.get(f"/creative-runs/{run_id}/assets")).json()["items"]
        if item["offer"] is not None
    }
    assert after.keys() == before.keys()
    for asset_id, item in after.items():
        was = before[asset_id]["offer"]
        if item["offer"]["sku"] == "SDS-TEAM":
            assert item["offer"] == {**was, "evidence_id": None}
        else:
            assert item["offer"] == was


# ---------------------------------------------------------------------------
# the landing screenshot
# ---------------------------------------------------------------------------


async def _audit(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    actor: uuid.UUID,
    screenshots: dict[str, str | None],
) -> uuid.UUID:
    """A creative run and one landing audit row, as 4.5.1/4.5.2 leave it."""
    await _seed(db, workspace_id, project_id, actor)
    started = await admin.post(
        f"/projects/{project_id}/creative/runs", json={"scope": TEXT_ONLY, "media_models": []}
    )
    assert started.status_code == 202, started.text
    row = LandingPageAudit(
        creative_run_id=uuid.UUID(started.json()["run_id"]),
        url="https://example.com/sds",
        final_url="https://example.com/sds",
        http_status=200,
        ad_group_refs=["sds software"],
        metrics={},
        verdict=LandingAuditVerdict.OK,
        screenshots=screenshots,
    )
    db.add(row)
    await db.commit()
    return row.id


async def test_a_device_s_capture_is_streamed_through_the_worker(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
) -> None:
    from agent.storage.backend import get_storage

    key = f"creative/{uuid.uuid4()}/landing/mobile.png"
    get_storage().put(key, PNG, content_type="image/png")
    audit_id = await _audit(
        admin, db, workspace_id, project_id, admin_user.id, {"mobile": key, "desktop": None}
    )

    response = await admin.get(f"/landing-audits/{audit_id}/screenshot?device=mobile")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "image/png"
    assert response.content == PNG

    # A device that never rendered has no capture: a 404 that says so.
    missing = await admin.get(f"/landing-audits/{audit_id}/screenshot?device=desktop")
    assert missing.status_code == 404
    assert "desktop" in missing.json()["detail"]
    # A device is one of two; the caller never names a key.
    assert (
        await admin.get(f"/landing-audits/{audit_id}/screenshot?device=tablet")
    ).status_code == 422
    assert (await admin.get(f"/landing-audits/{audit_id}/screenshot")).status_code == 422
    unknown = await admin.get(f"/landing-audits/{uuid.uuid4()}/screenshot?device=mobile")
    assert unknown.status_code == 404


async def test_a_key_the_volume_does_not_hold_is_a_502_not_a_broken_image(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
) -> None:
    audit_id = await _audit(
        admin,
        db,
        workspace_id,
        project_id,
        admin_user.id,
        {"mobile": "creative/gone/mobile.png", "desktop": None},
    )
    response = await admin.get(f"/landing-audits/{audit_id}/screenshot?device=mobile")
    assert response.status_code == 502
    assert response.headers["content-type"].startswith("application/problem+json")
