"""The two reads P7 asks of the evidence store: a citation set, and a creative.

`?ids=` is what a Report Viewer opens with — `ResearchReport.evidence_ids()`
hands over every id cited anywhere in the document, and one request for the set
is the difference between a report that renders and one that fires ninety.

The screenshot route is the only place the Evidence Explorer leaves the API
process: captures live on the worker's Volume, which `api` cannot read (PRD
§5.2). The tests below run the real token signing against a real file server.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import EvidenceSource
from agent.evidence.embedding import HashingEmbedder
from agent.evidence.normalize import EvidenceDraft
from agent.evidence.store import EvidenceStore
from tests.integration.conftest import ApiClient

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

    app = create_file_server()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker:8081"
    ) as client:
        yield client


@pytest.fixture
def api_dependency_overrides(worker_files: httpx.AsyncClient) -> dict[Any, Any]:
    """Point the API's worker client at the in-process file server.

    Overriding the dependency rather than patching httpx leaves the route's own
    URL building and token signing running — the parts most likely to be wrong,
    and the only part of this path that is a security control.
    """
    from agent.api.worker_files import get_worker_client

    async def override() -> httpx.AsyncClient:
        return worker_files

    return {get_worker_client: override}


async def seed(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    *,
    screenshot_key: str | None = None,
) -> list[uuid.UUID]:
    """Three keyword rows and one competitor ad, in that order."""
    drafts = [
        EvidenceDraft(
            source=EvidenceSource.DATAFORSEO,
            kind="keyword_metrics",
            payload={"keyword": f"sds software {index}", "volume": 100 + index},
        )
        for index in range(3)
    ]
    drafts.append(
        EvidenceDraft(
            source=EvidenceSource.TRANSPARENCY,
            kind="competitor_ad",
            payload={
                "advertiser": "Chemwatch",
                "headline": "SDS management, simplified",
                **({"screenshot_path": screenshot_key} if screenshot_key else {}),
            },
        )
    )
    written = await EvidenceStore(db, workspace_id, embedder=HashingEmbedder()).write(
        drafts, project_id=project_id
    )
    await db.commit()
    return list(written.evidence_ids)


# ---------------------------------------------------------------------------
# ?ids=
# ---------------------------------------------------------------------------


async def test_ids_returns_exactly_the_rows_asked_for(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> None:
    ids = await seed(db, workspace_id, project_id)
    wanted = [ids[0], ids[2]]

    response = await admin.get("/evidence", params=[("ids", str(one)) for one in wanted])

    assert response.status_code == 200, response.text
    assert sorted(row["id"] for row in response.json()["items"]) == sorted(
        str(one) for one in wanted
    )


async def test_an_id_from_another_workspace_is_simply_absent(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> None:
    """Not a 404: a citation index that 404s on one stale id renders nothing."""
    ids = await seed(db, workspace_id, project_id)

    response = await admin.get(
        "/evidence", params=[("ids", str(ids[0])), ("ids", str(uuid.uuid4()))]
    )

    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()["items"]] == [str(ids[0])]


async def test_asking_for_more_ids_than_a_page_holds_is_refused(admin: ApiClient) -> None:
    response = await admin.get("/evidence", params=[("ids", str(uuid.uuid4())) for _ in range(201)])

    assert response.status_code == 422
    assert "201" in response.json()["detail"]


async def test_ids_compose_with_the_other_filters(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> None:
    ids = await seed(db, workspace_id, project_id)

    response = await admin.get(
        "/evidence",
        params=[*[("ids", str(one)) for one in ids], ("source", "transparency")],
    )

    assert [row["kind"] for row in response.json()["items"]] == ["competitor_ad"]


# ---------------------------------------------------------------------------
# screenshots
# ---------------------------------------------------------------------------


async def test_a_stored_capture_is_streamed_back(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> None:
    from agent.storage.backend import get_storage

    key = f"creatives/{uuid.uuid4()}/chemwatch-ab12cd34.png"
    get_storage().put(key, PNG, content_type="image/png")
    ids = await seed(db, workspace_id, project_id, screenshot_key=key)

    response = await admin.get(f"/evidence/{ids[-1]}/screenshot")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "image/png"
    assert response.content == PNG


async def test_the_list_says_which_rows_have_one(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> None:
    from agent.storage.backend import get_storage

    key = f"creatives/{uuid.uuid4()}/chemwatch-ab12cd34.png"
    get_storage().put(key, PNG, content_type="image/png")
    await seed(db, workspace_id, project_id, screenshot_key=key)

    items = (await admin.get("/evidence")).json()["items"]

    by_kind = {row["kind"]: row["has_screenshot"] for row in items}
    assert by_kind == {"keyword_metrics": False, "competitor_ad": True}


async def test_a_row_without_a_capture_is_a_404_not_an_empty_image(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> None:
    ids = await seed(db, workspace_id, project_id)

    response = await admin.get(f"/evidence/{ids[0]}/screenshot")

    assert response.status_code == 404
    assert "screenshot" in response.json()["detail"]


async def test_a_capture_the_volume_lost_is_a_502_before_any_body(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> None:
    """The row points at a key nothing wrote — a truncated image would be worse."""
    ids = await seed(db, workspace_id, project_id, screenshot_key="creatives/gone/missing.png")

    response = await admin.get(f"/evidence/{ids[-1]}/screenshot")

    assert response.status_code == 502
    assert response.json()["title"] == "Screenshot could not be read"


async def test_the_capture_of_another_workspace_is_not_readable(admin: ApiClient) -> None:
    response = await admin.get(f"/evidence/{uuid.uuid4()}/screenshot")

    assert response.status_code == 404
