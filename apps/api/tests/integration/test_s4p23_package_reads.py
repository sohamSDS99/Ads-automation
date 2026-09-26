"""S4-P23's reads — what the QA and Package screens need that no route gave.

- `GET /creative-runs/{id}/package` and `GET /creative-packages/{id}` — one
  `PackageView`: the package as stored, 4.7.2's critique, the thirteen checks
  as 4.7.2 ran them (so the screen renders the server's checklist and never
  re-derives one), and what a release would do — the version it would mint
  and the four stops with who decided each and when.
- `GET /creative-packages/{id}/files/{path}` — a manifest file of a released
  package, as a signed 302 to the file server; a draft has no files yet.
- `GET /render-previews/{id}/screenshot` — the PNG 4.6.4 captured, streamed.

Every assertion reads a route, and checks it against the rows it reports.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from agent.creative import checklist
from agent.creative.package import package_hash
from agent.db.models import PreviewDevice, PreviewVerdict, RenderPreview
from agent.export.tokens import is_valid
from agent.schemas.creative_package import CreativeCritique
from agent.storage.local import LocalStorage
from tests.integration.conftest import ApiClient, build_client, make_member
from tests.integration.creative_support import TEXT_ONLY
from tests.integration.test_s4p5_headlines_combinations import _output
from tests.integration.test_s4p6_descriptions_variant_b import _seed
from tests.integration.test_s4p8_extras import CRAWLED, OFFER_SPECS, _crawl, _run
from tests.integration.test_s4p16_package import (
    _package_of,
    _release,
    golden,
    rendered_form,
    storage,
    web,
    worker_writes,
)

pytestmark = pytest.mark.asyncio

__all__ = ["rendered_form", "storage", "web", "worker_writes"]

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6360000002000100ffff03000006000557bfabd4"
    "0000000049454e44ae426082"
)


@pytest_asyncio.fixture
async def worker_files(storage: LocalStorage) -> AsyncIterator[httpx.AsyncClient]:
    """The worker's file server over `storage`'s directory, reached as `api` reaches it."""
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


def _stops(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {stop["gate"]: stop for stop in body["release"]["stops"]}


# ---------------------------------------------------------------------------
# the package view
# ---------------------------------------------------------------------------


async def test_a_ready_package_carries_the_thirteen_checks_and_what_release_would_mint(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    web: Any,
    rendered_form: None,
) -> None:
    run_id = await golden(admin, db, workspace_id, project_id, admin_user.id)
    row = await _package_of(db, run_id)

    response = await admin.get(f"/creative-runs/{run_id}/package")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["package"]["package_id"] == str(row.id)
    assert (body["package"]["status"], body["row_version"]) == ("ready_to_release", 0)
    assert body["package"] == {**row.payload, "status": row.status.value}

    # The thirteen, in §11's order, each as 4.7.2 recorded it.
    critique = CreativeCritique.model_validate(await _output(db, run_id, "4.7.2"))
    assert body["critique"] == critique.model_dump(mode="json")
    assert [item["check"] for item in body["checklist"]] == [f"check_{n}" for n in range(1, 14)]
    assert [item["title"] for item in body["checklist"]] == list(checklist.TITLES.values())
    assert all(item["passed"] and item["issues"] == [] for item in body["checklist"])

    release = body["release"]
    assert (release["releasable"], release["version_to_mint"], release["reason"]) == (
        True, 1, None,
    )  # fmt: skip
    stops = _stops(body)
    assert list(stops) == ["G7", "G8", "G8b", "H3"]
    g7 = next(d for d in body["package"]["decisions"] if d["gate_key"] == "G7")
    assert stops["G7"]["status"] == "approved"
    assert stops["G7"]["decided_by"] == str(admin_user.id) == g7["decided_by"]
    assert stops["G7"]["decided_by_name"] == admin_user.name
    assert stops["G7"]["decided_at"] == g7["decided_at"]
    # A text-only run opens no media gate.
    assert stops["G8"]["status"] == stops["G8b"]["status"] == "not_required"
    tasks = body["package"]["human_tasks"]
    assert stops["H3"]["status"] == (tasks[-1]["status"] if tasks else "not_required")
    exceptions = body["package"]["exceptions"]
    assert stops["H3"]["detail"].startswith(f"{len(exceptions)} exception") or not exceptions

    # By id, the same view; any member may read it.
    by_id = await admin.get(f"/creative-packages/{row.id}")
    assert by_id.status_code == 200 and by_id.json() == body
    email, password = await make_member(admin, "viewer")
    viewer = build_client()
    async with viewer.raw:
        assert (await viewer.login(email, password)).status_code == 200
        assert (await viewer.get(f"/creative-packages/{row.id}")).json() == body

    # A draft's manifest names files release has not written yet.
    entry = body["package"]["manifest"][0]["path"]
    unreleased = await admin.get(f"/creative-packages/{row.id}/files/{entry}")
    assert unreleased.status_code == 404
    assert "released" in unreleased.json()["detail"]


async def test_after_release_the_view_is_immutable_and_its_files_redirect_signed(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    web: Any,
    rendered_form: None,
    storage: LocalStorage,
    worker_writes: list[dict[str, Any]],
) -> None:
    run_id = await golden(admin, db, workspace_id, project_id, admin_user.id)
    row = await _package_of(db, run_id)
    released = await _release(admin, row.id, 1)
    assert released.status_code == 200, released.text

    body = (await admin.get(f"/creative-packages/{row.id}")).json()
    await db.refresh(row)
    assert (body["package"]["status"], body["row_version"]) == ("released", 1)
    assert body["package_hash"] == row.package_hash == package_hash(row.payload)
    assert body["released_at"] is not None and body["released_by_name"] == admin_user.name
    release = body["release"]
    assert release["releasable"] is False and release["version_to_mint"] is None
    assert "immutable" in release["reason"] and "v1" in release["reason"]
    assert _stops(body)["G7"]["decided_by_name"] == admin_user.name

    # Every manifest file: a 302 to the file server, signed for that one key.
    for entry in body["package"]["manifest"]:
        found = await admin.get(
            f"/creative-packages/{row.id}/files/{entry['path']}", follow_redirects=False
        )
        assert found.status_code == 302, found.text
        location = urlsplit(found.headers["location"])
        key = f"package/{row.id}/{entry['path']}"
        assert unquote(location.path) == f"/files/{key}"
        (token,) = parse_qs(location.query)["token"]
        assert is_valid(key, token)
        assert storage.get(key)  # release wrote it where the redirect points

    # Only what the manifest lists: nothing else under package/ is reachable.
    for path in ("nothing.json", "../../exports/x.json", "%2e%2e/secret"):
        missing = await admin.get(f"/creative-packages/{row.id}/files/{path}")
        assert missing.status_code == 404, path
    assert (await admin.get(f"/creative-packages/{uuid.uuid4()}/files/a.json")).status_code == 404


async def test_a_blocked_package_names_its_failed_checks_and_why_it_cannot_be_released(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    web: Any,
) -> None:
    """S4-P16's blocked run: no offers loaded, so the price minimum is unmet."""
    await _crawl(db, project_id, CRAWLED)
    run_id, _, status = await _run(
        admin, db, workspace_id, project_id, admin_user.id, extra_specs=OFFER_SPECS
    )
    assert status.value == "succeeded"
    critique = CreativeCritique.model_validate(await _output(db, run_id, "4.7.2"))

    body = (await admin.get(f"/creative-runs/{run_id}/package")).json()
    failed = [item for item in body["checklist"] if not item["passed"]]
    assert {item["check"] for item in failed} == set(critique.failed_checks)
    assert "check_11" in {item["check"] for item in failed}
    for item in failed:
        assert item["issues"] and all(
            issue["check"] == item["check"] and issue["severity"] == "blocking"
            for issue in item["issues"]
        )
    release = body["release"]
    assert release["releasable"] is False and release["version_to_mint"] == 1
    assert f"found {critique.blocking} blocking" in release["reason"]


async def test_a_run_before_4_7_1_has_no_package_yet(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
) -> None:
    await _seed(db, workspace_id, project_id, admin_user.id)
    started = await admin.post(
        f"/projects/{project_id}/creative/runs", json={"scope": TEXT_ONLY, "media_models": []}
    )
    assert started.status_code == 202, started.text
    response = await admin.get(f"/creative-runs/{started.json()['run_id']}/package")
    assert response.status_code == 404
    assert response.json()["title"] == "No package yet" and "4.7.1" in response.json()["detail"]
    assert (await admin.get(f"/creative-runs/{uuid.uuid4()}/package")).status_code == 404
    assert (await admin.get(f"/creative-packages/{uuid.uuid4()}")).status_code == 404


# ---------------------------------------------------------------------------
# the preview capture
# ---------------------------------------------------------------------------


async def test_a_preview_capture_is_streamed_and_an_undrawn_one_says_why(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    storage: LocalStorage,
) -> None:
    await _seed(db, workspace_id, project_id, admin_user.id)
    started = await admin.post(
        f"/projects/{project_id}/creative/runs", json={"scope": TEXT_ONLY, "media_models": []}
    )
    run_id = uuid.UUID(started.json()["run_id"])
    key = f"creative/{run_id}/previews/sds-a-mobile.png"
    storage.put(key, PNG, content_type="image/png")
    drawn, undrawn = (
        RenderPreview(
            creative_run_id=run_id,
            ad_ref="c-sds-us/sds software/A",
            device=PreviewDevice.MOBILE,
            combination={"roles": ["likely_1"]},
            storage_path=path,
            dom_metrics={} if path else {"error": "Chromium did not start"},
            spec_diff={},
            template_version="serp-mobile-1",
            verdict=PreviewVerdict.PASS if path else PreviewVerdict.UNAVAILABLE,
        )  # fmt: skip
        for path in (key, None)
    )
    db.add_all([drawn, undrawn])
    await db.commit()

    response = await admin.get(f"/render-previews/{drawn.id}/screenshot")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "image/png" and response.content == PNG

    missing = await admin.get(f"/render-previews/{undrawn.id}/screenshot")
    assert missing.status_code == 404 and "Chromium did not start" in missing.json()["detail"]
    assert (await admin.get(f"/render-previews/{uuid.uuid4()}/screenshot")).status_code == 404

    # Another workspace's caller cannot reach it by id.
    stranger = build_client()
    async with stranger.raw:
        assert (await stranger.get(f"/render-previews/{drawn.id}/screenshot")).status_code == 401
