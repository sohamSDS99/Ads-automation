"""S4-P9 — media references: upload, retire, and Law 44 at the point of sending.

PRD §10.3: an upload needs a rights statement and records `attested_by`;
references are retired, never deleted. §9.1 item 5 / Law 44: a reference reaches
a provider only when the project allows it, the uploader attested rights,
`origin != third_party` (or an H3 `image_right` is cleared for it in this run),
and the model accepts image input. The exit criterion — a third-party reference
is never sent without a cleared `image_right` — is asserted at the loader here
and on the wire in `test_s4p9_image_masters.py`.

No test reaches OpenRouter: respx runs with `assert_all_mocked=True`.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import respx
import sqlalchemy as sa
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from agent.audit import AuditAction
from agent.db.models import (
    AuditLog,
    CreativeException,
    CreativeExceptionKind,
    CreativeExceptionStatus,
    GenerationStatus,
    MediaReference,
    MediaReferenceKind,
    MediaReferenceOrigin,
    Project,
)
from agent.media import references
from agent.media.constants import media_constants
from agent.media.references import ReferenceRefused
from agent.storage.local import LocalStorage
from tests.integration.conftest import ApiClient, build_client, make_member
from tests.integration.test_media_jobs import (
    BASE,
    IMAGE,
    SimulatedCrash,
    World,
    _approve_brief,
    _creative_run,
    choice,
    flux,
)
from tests.media.openrouter_mock import response

THIRD_PARTY = MediaReferenceOrigin.THIRD_PARTY
MAX = media_constants().reference_max_bytes
RIGHTS = "Photographed by our studio in 2026; we hold every right to this image."


def _png(colour: tuple[int, int, int] = (10, 120, 200), size: tuple[int, int] = (16, 9)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def storage_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    from agent.config import get_settings

    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    get_settings.cache_clear()
    return tmp_path


@pytest.fixture
def worker_stores(monkeypatch: pytest.MonkeyPatch, storage_dir: Path) -> list[dict[str, Any]]:
    """`queue.store_reference` answered by the real worker job, in process.

    The api never writes the Volume (the worker owns it, §22); the route hands
    the bytes to the worker and waits. Here the worker's own function does the
    write, so what is tested is the code that runs in production.
    """
    from agent import worker
    from agent.api import routes_media

    calls: list[dict[str, Any]] = []

    async def store(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(payload)
        return await worker.store_reference({}, payload)

    monkeypatch.setattr(routes_media.queue, "store_reference", store)
    return calls


async def _upload(
    client: ApiClient,
    project_id: uuid.UUID,
    content: bytes,
    *,
    filename: str = "product.png",
    content_type: str = "image/png",
    **fields: str,
) -> Any:
    data = {"kind": "product_reference", "origin": "own", "rights_statement": RIGHTS}
    data.update(fields)
    return await client.post(
        f"/projects/{project_id}/media-references",
        files={"file": (filename, content, content_type)},
        data=data,
    )


# ---------------------------------------------------------------------------
# upload
# ---------------------------------------------------------------------------


async def test_an_upload_records_the_attestation_and_stores_the_bytes_content_addressed(
    admin: ApiClient,
    db: AsyncSession,
    project_id: uuid.UUID,
    admin_user: Any,
    worker_stores: list[dict[str, Any]],
    storage_dir: Path,
) -> None:
    content = _png()
    sha = hashlib.sha256(content).hexdigest()

    created = await _upload(admin, project_id, content, product_ref="SKU-42")

    assert created.status_code == 201, created.text
    body = created.json()
    assert body["kind"] == "product_reference"
    assert body["origin"] == "own"
    assert body["product_ref"] == "SKU-42"
    assert body["rights_statement"] == RIGHTS
    assert body["attested_by"] == str(admin_user.id)
    assert body["attested_at"] is not None
    assert body["retired_at"] is None
    assert (body["media_type"], body["width"], body["height"]) == ("image/png", 16, 9)
    assert (body["bytes"], body["sha256"]) == (len(content), sha)
    assert "storage_path" not in body

    row = await db.get(MediaReference, uuid.UUID(body["id"]))
    assert row is not None
    assert row.storage_path == f"references/{project_id}/{sha}.png"
    assert (storage_dir / row.storage_path).read_bytes() == content
    assert len(worker_stores) == 1

    audit = (
        await db.execute(
            sa.select(AuditLog).where(AuditLog.action == AuditAction.MEDIA_REFERENCE_UPLOADED)
        )
    ).scalar_one()
    assert audit.target_id == row.id
    assert audit.meta["origin"] == "own"


async def test_a_third_party_upload_keeps_its_origin(
    admin: ApiClient, project_id: uuid.UUID, worker_stores: list[dict[str, Any]]
) -> None:
    created = await _upload(admin, project_id, _png(), origin="third_party")
    assert created.status_code == 201, created.text
    assert created.json()["origin"] == "third_party"


@pytest.mark.parametrize("rights", ["", "   "])
async def test_a_rights_statement_is_required(
    admin: ApiClient,
    db: AsyncSession,
    project_id: uuid.UUID,
    worker_stores: list[dict[str, Any]],
    rights: str,
) -> None:
    refused = await _upload(admin, project_id, _png(), rights_statement=rights)
    assert refused.status_code == 422, refused.text
    assert await db.scalar(sa.select(sa.func.count()).select_from(MediaReference)) == 0
    assert worker_stores == []


async def test_a_missing_rights_statement_field_is_a_422(
    admin: ApiClient, project_id: uuid.UUID, worker_stores: list[dict[str, Any]]
) -> None:
    refused = await admin.post(
        f"/projects/{project_id}/media-references",
        files={"file": ("p.png", _png(), "image/png")},
        data={"kind": "product_reference", "origin": "own"},
    )
    assert refused.status_code == 422


async def test_a_file_over_reference_max_bytes_is_a_413(
    admin: ApiClient, project_id: uuid.UUID, worker_stores: list[dict[str, Any]]
) -> None:
    refused = await _upload(admin, project_id, b"\x89PNG" + b"0" * MAX)
    assert refused.status_code == 413, refused.text
    assert worker_stores == []


async def test_the_format_is_read_from_the_bytes(
    admin: ApiClient, project_id: uuid.UUID, worker_stores: list[dict[str, Any]]
) -> None:
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4)).save(buffer, format="GIF")
    refused = await _upload(admin, project_id, buffer.getvalue(), filename="renamed.png")
    assert refused.status_code == 422, refused.text
    assert "PNG, JPEG or WebP" in refused.json()["detail"]


@pytest.mark.parametrize(("field", "value"), [("kind", "logo"), ("origin", "stock")])
async def test_an_unknown_kind_or_origin_is_a_422(
    admin: ApiClient,
    project_id: uuid.UUID,
    worker_stores: list[dict[str, Any]],
    field: str,
    value: str,
) -> None:
    refused = await _upload(admin, project_id, _png(), **{field: value})
    assert refused.status_code == 422


async def test_the_same_image_twice_is_a_409_naming_the_first(
    admin: ApiClient, project_id: uuid.UUID, worker_stores: list[dict[str, Any]]
) -> None:
    first = await _upload(admin, project_id, _png())
    again = await _upload(admin, project_id, _png(), origin="licensed")
    assert again.status_code == 409, again.text
    assert first.json()["id"] in again.json()["detail"]


async def test_no_row_is_written_when_the_worker_cannot_store_the_file(
    admin: ApiClient,
    db: AsyncSession,
    project_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent import queue
    from agent.api import routes_media

    async def dead(payload: dict[str, Any]) -> dict[str, Any]:
        raise queue.WorkerUnavailable("no worker answered")

    monkeypatch.setattr(routes_media.queue, "store_reference", dead)
    refused = await _upload(admin, project_id, _png())
    assert refused.status_code == 503, refused.text
    assert await db.scalar(sa.select(sa.func.count()).select_from(MediaReference)) == 0


async def test_a_viewer_lists_but_may_not_upload_or_retire(
    admin: ApiClient, project_id: uuid.UUID, worker_stores: list[dict[str, Any]]
) -> None:
    created = await _upload(admin, project_id, _png())
    address, password = await make_member(admin, "viewer")
    viewer = build_client()
    assert (await viewer.login(address, password)).status_code == 200

    listed = await viewer.get(f"/projects/{project_id}/media-references")
    assert listed.status_code == 200
    assert [r["id"] for r in listed.json()] == [created.json()["id"]]
    assert (await _upload(viewer, project_id, _png((1, 2, 3)))).status_code == 403
    retired = await viewer.post(f"/media-references/{created.json()['id']}/retire")
    assert retired.status_code == 403


async def test_a_project_in_another_workspace_is_a_404(
    admin: ApiClient, worker_stores: list[dict[str, Any]]
) -> None:
    missing = uuid.uuid4()
    assert (await _upload(admin, missing, _png())).status_code == 404
    assert (await admin.get(f"/projects/{missing}/media-references")).status_code == 404


# ---------------------------------------------------------------------------
# retire — never delete
# ---------------------------------------------------------------------------


async def test_retiring_keeps_the_row_and_is_idempotent(
    admin: ApiClient,
    db: AsyncSession,
    project_id: uuid.UUID,
    worker_stores: list[dict[str, Any]],
) -> None:
    created = (await _upload(admin, project_id, _png())).json()

    first = await admin.post(f"/media-references/{created['id']}/retire")
    assert first.status_code == 200, first.text
    retired_at = first.json()["retired_at"]
    assert retired_at is not None

    again = await admin.post(f"/media-references/{created['id']}/retire")
    assert again.status_code == 200
    assert again.json()["retired_at"] == retired_at

    listed = (await admin.get(f"/projects/{project_id}/media-references")).json()
    assert [(r["id"], r["retired_at"]) for r in listed] == [(created["id"], retired_at)]
    audits = await db.scalar(
        sa.select(sa.func.count())
        .select_from(AuditLog)
        .where(AuditLog.action == AuditAction.MEDIA_REFERENCE_RETIRED)
    )
    assert audits == 1


async def test_there_is_no_way_to_delete_a_reference(
    admin: ApiClient,
    db: AsyncSession,
    project_id: uuid.UUID,
    worker_stores: list[dict[str, Any]],
) -> None:
    created = (await _upload(admin, project_id, _png())).json()
    deleted = await admin.delete(f"/media-references/{created['id']}")
    assert deleted.status_code in (404, 405)
    assert await db.get(MediaReference, uuid.UUID(created["id"])) is not None


async def test_retiring_an_unknown_reference_is_a_404(admin: ApiClient) -> None:
    assert (await admin.post(f"/media-references/{uuid.uuid4()}/retire")).status_code == 404


# ---------------------------------------------------------------------------
# Law 44 at the point of sending
# ---------------------------------------------------------------------------


async def _reference(
    db: AsyncSession,
    storage: LocalStorage,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    actor: uuid.UUID,
    *,
    origin: MediaReferenceOrigin = MediaReferenceOrigin.OWN,
    content: bytes | None = None,
    retired: bool = False,
) -> MediaReference:
    content = content or _png((int.from_bytes(uuid.uuid4().bytes[:1], "big"), 50, 60))
    sha = hashlib.sha256(content).hexdigest()
    key = references.storage_key(project_id, sha, "image/png")
    storage.put(key, content, content_type="image/png")
    row = MediaReference(
        workspace_id=workspace_id,
        project_id=project_id,
        kind=MediaReferenceKind.PRODUCT_REFERENCE,
        storage_path=key,
        media_type="image/png",
        width=16,
        height=9,
        bytes=len(content),
        sha256=sha,
        origin=origin,
        rights_statement=RIGHTS,
        attested_by=actor,
        attested_at=datetime.now(UTC),
        retired_at=datetime.now(UTC) if retired else None,
    )
    db.add(row)
    await db.commit()
    return row


async def _image_right(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    reference: MediaReference,
    *,
    status: CreativeExceptionStatus = CreativeExceptionStatus.CLEARED,
    decided_by: uuid.UUID | None,
) -> None:
    db.add(
        CreativeException(
            workspace_id=workspace_id,
            project_id=project_id,
            creative_run_id=run_id,
            kind=CreativeExceptionKind.IMAGE_RIGHT,
            subject_text=f"reference {reference.id}",
            proposed=references.image_right_proposal(reference.id, basis="licence on file"),
            status=status,
            decided_by=decided_by,
            decided_at=datetime.now(UTC) if decided_by else None,
        )
    )
    await db.commit()


async def _load(
    db: AsyncSession,
    world: World,
    project_id: uuid.UUID,
    reference: MediaReference,
    *,
    allowed: bool = True,
) -> list[Any]:
    return await references.load_for_request(
        db,
        world.storage,
        run_id=world.run_id,
        project_id=project_id,
        allowed=allowed,
        capability=flux(),
        reference_ids=[reference.id],
        max_bytes=MAX,
    )


@pytest.fixture
async def world(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    tmp_path: Path,
) -> World:
    run_id = await _creative_run(db, workspace_id, project_id, admin_user.id)
    await _approve_brief(db, workspace_id, project_id, run_id)
    return World(tmp_path, run_id)


async def test_an_own_reference_loads_with_the_bytes_it_was_attested_with(
    db: AsyncSession, world: World, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    content = _png((1, 2, 3))
    ref = await _reference(
        db, world.storage, workspace_id, project_id, admin_user.id, content=content
    )
    (image,) = await _load(db, world, project_id, ref)
    assert (image.sha256, image.media_type, image.data) == (ref.sha256, "image/png", content)


async def test_a_third_party_reference_without_a_cleared_image_right_is_refused(
    db: AsyncSession, world: World, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    ref = await _reference(
        db, world.storage, workspace_id, project_id, admin_user.id, origin=THIRD_PARTY
    )
    with pytest.raises(ReferenceRefused) as refused:
        await _load(db, world, project_id, ref)
    assert refused.value.reason == "third_party_without_image_right"


async def test_a_cleared_image_right_in_this_run_lets_it_go(
    db: AsyncSession, world: World, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    ref = await _reference(
        db, world.storage, workspace_id, project_id, admin_user.id, origin=THIRD_PARTY
    )
    await _image_right(db, workspace_id, project_id, world.run_id, ref, decided_by=admin_user.id)
    (image,) = await _load(db, world, project_id, ref)
    assert image.sha256 == ref.sha256


@pytest.mark.parametrize("status", [CreativeExceptionStatus.OPEN, CreativeExceptionStatus.REJECTED])
async def test_an_undecided_or_rejected_image_right_is_not_a_clearance(
    db: AsyncSession,
    world: World,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    status: CreativeExceptionStatus,
) -> None:
    ref = await _reference(
        db, world.storage, workspace_id, project_id, admin_user.id, origin=THIRD_PARTY
    )
    await _image_right(
        db, workspace_id, project_id, world.run_id, ref, status=status, decided_by=admin_user.id
    )
    with pytest.raises(ReferenceRefused):
        await _load(db, world, project_id, ref)


async def test_a_cleared_status_nobody_decided_is_not_a_clearance(
    db: AsyncSession, world: World, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    ref = await _reference(
        db, world.storage, workspace_id, project_id, admin_user.id, origin=THIRD_PARTY
    )
    await _image_right(db, workspace_id, project_id, world.run_id, ref, decided_by=None)
    with pytest.raises(ReferenceRefused):
        await _load(db, world, project_id, ref)


async def test_another_runs_clearance_does_not_carry_over(
    db: AsyncSession, world: World, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    ref = await _reference(
        db, world.storage, workspace_id, project_id, admin_user.id, origin=THIRD_PARTY
    )
    other_run = await _creative_run(db, workspace_id, project_id, admin_user.id)
    await _image_right(db, workspace_id, project_id, other_run, ref, decided_by=admin_user.id)
    with pytest.raises(ReferenceRefused):
        await _load(db, world, project_id, ref)


async def test_retired_or_disallowed_references_never_load(
    db: AsyncSession, world: World, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    retired = await _reference(
        db, world.storage, workspace_id, project_id, admin_user.id, retired=True
    )
    with pytest.raises(ReferenceRefused) as refused:
        await _load(db, world, project_id, retired)
    assert refused.value.reason == "retired"

    own = await _reference(db, world.storage, workspace_id, project_id, admin_user.id)
    with pytest.raises(ReferenceRefused) as refused:
        await _load(db, world, project_id, own, allowed=False)
    assert refused.value.reason == "references_not_allowed"


async def test_bytes_that_no_longer_match_their_hash_are_refused(
    db: AsyncSession, world: World, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    ref = await _reference(db, world.storage, workspace_id, project_id, admin_user.id)
    world.storage.put(ref.storage_path, _png((9, 9, 9)), content_type="image/png")
    with pytest.raises(ReferenceRefused) as refused:
        await _load(db, world, project_id, ref)
    assert refused.value.reason == "bytes_changed"


async def test_another_projects_reference_is_unknown_here(
    db: AsyncSession,
    world: World,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    second_project_id: uuid.UUID,
    admin_user: Any,
) -> None:
    foreign = await _reference(db, world.storage, workspace_id, second_project_id, admin_user.id)
    with pytest.raises(ReferenceRefused) as refused:
        await _load(db, world, project_id, foreign)
    assert refused.value.reason == "unknown_reference"


# ---------------------------------------------------------------------------
# "Check again" re-reads reference bytes — the row holds only their hash
# ---------------------------------------------------------------------------


async def _allow_references(db: AsyncSession, project_id: uuid.UUID, allowed: bool) -> None:
    project = await db.get(Project, project_id)
    assert project is not None
    project.settings = {**(project.settings or {}), "media_references_allowed": allowed}
    await db.commit()


async def _stuck_with_reference(
    world: World, ref: MediaReference, content: bytes
) -> tuple[Any, dict[str, Any]]:
    submit: dict[str, Any] = dict(
        run_id=world.run_id,
        node_id="4.4.2",
        asset_id=None,
        round=1,
        request=IMAGE.model_copy(
            update={
                "input_references": [
                    references.ReferenceImage(
                        sha256=ref.sha256, media_type="image/png", data=content
                    )
                ]
            }
        ),
        choice=choice(flux()),
        estimate_usd=Decimal("0.03"),
    )
    with pytest.raises(SimulatedCrash):
        await world.jobs(crash_at="submitting_committed").submit_or_resume(**submit)
    stuck = await world.jobs().submit_or_resume(**submit)
    assert stuck.status == GenerationStatus.UNKNOWN_SUBMIT_STATE
    return stuck, submit


async def test_check_again_re_reads_the_reference_bytes_and_posts_them(
    db: AsyncSession, world: World, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    await _allow_references(db, project_id, True)
    content = _png((4, 5, 6))
    ref = await _reference(
        db, world.storage, workspace_id, project_id, admin_user.id, content=content
    )
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        route = router.post(f"{BASE}/images").mock(return_value=response("image_generate.json"))
        stuck, _ = await _stuck_with_reference(world, ref, content)

        checked = await world.jobs().check(stuck.id, choice=choice(flux()))

    assert checked.status == GenerationStatus.COMPLETED
    assert checked.idempotency_key == stuck.idempotency_key
    assert route.call_count == 1
    sent = json.loads(route.calls[0].request.content)["input_references"]
    encoded = base64.b64encode(content).decode()
    assert sent == [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}}]


async def test_check_again_does_not_post_a_reference_law_44_now_refuses(
    db: AsyncSession, world: World, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    await _allow_references(db, project_id, True)
    content = _png((7, 8, 9))
    ref = await _reference(
        db, world.storage, workspace_id, project_id, admin_user.id, content=content
    )
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        route = router.post(f"{BASE}/images").mock(return_value=response("image_generate.json"))
        stuck, _ = await _stuck_with_reference(world, ref, content)
        await _allow_references(db, project_id, False)

        checked = await world.jobs().check(stuck.id, choice=choice(flux()))

    assert checked.status == GenerationStatus.UNKNOWN_SUBMIT_STATE
    assert route.call_count == 0
