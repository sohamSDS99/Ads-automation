"""S4-P21 — the Media Library's server side (Stage 04 PRD §15.4 G, §15.5 items
2–3, §16 "Media"), on a real image run through 4.4.3 (S4-P10's harness).

1. Every file the grid or the Concept Board shows has a WebP proxy beside it:
   each master (4.4.2), each rendition and each fitted logo (4.4.3); and each
   rendition carries its slot's byte limit.
2. `GET /media/{id}/content` never reads a byte: it answers `302` to the file
   server, signed for 300 s. `preview` and `poster` resolve by `derived_from`,
   and a variant that was never made is a 404 — never the master instead. The
   redirect target, on the real file server, seeks with `Range` (`206`).
3. `POST /creative-assets/{id}/regeneration-estimate` prices one regeneration
   at the live capability record against what remains of the media cap, and
   refuses — with the start's own 422s — what the regeneration would refuse.
"""

from __future__ import annotations

import io
import uuid
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
import sqlalchemy as sa
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from agent.calc.media import image_job_price
from agent.creative.constants import creative_constants_for
from agent.db.models import (
    CreativeAsset,
    CreativeAssetKind,
    MediaArtifact,
    MediaArtifactDerivation,
    MediaArtifactRole,
    Project,
)
from agent.export.tokens import verify
from agent.fileserver import create_file_server
from agent.media.types import CapabilityRecord
from agent.postprod.image import PREVIEW_LONG_SIDE
from agent.storage.local import LocalStorage
from tests.integration.conftest import ApiClient, make_member
from tests.integration.test_media_routes import ALLOWLIST
from tests.integration.test_s4p10_image_renditions import PMAX, _renditions
from tests.integration.test_s4p10_image_renditions import (
    storage as storage,  # noqa: PLC0414 — the fixture
)
from tests.media.openrouter_mock import FLUX, GEMINI

LIMIT = 5242880


async def _pmax_run(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    storage: LocalStorage,
) -> tuple[dict[str, Any], dict[str, Any], uuid.UUID]:
    out, mastered, _, run_id = await _renditions(
        admin, db, (workspace_id, project_id, admin_user.id), storage,
        campaign_type="performance_max", specs=PMAX, image_input=False,
    )  # fmt: skip
    return out, mastered, run_id


async def _previews(db: AsyncSession) -> dict[uuid.UUID, MediaArtifact]:
    rows = (
        await db.execute(
            sa.select(MediaArtifact)
            .where(MediaArtifact.role == MediaArtifactRole.PREVIEW)
            .execution_options(populate_existing=True)
        )
    ).scalars()
    return {row.derived_from: row for row in rows if row.derived_from is not None}


def _location(response: httpx.Response) -> tuple[str, str]:
    """The storage key and token a content redirect points at."""
    assert response.status_code == 302, response.text
    parts = urlsplit(response.headers["location"])
    assert parts.path.startswith("/files/") and not parts.netloc  # same origin, relative
    return unquote(parts.path.removeprefix("/files/")), parse_qs(parts.query)["token"][0]


async def test_every_master_rendition_and_logo_has_a_webp_proxy_and_its_byte_limit(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    storage: LocalStorage,
) -> None:
    out, mastered, _ = await _pmax_run(admin, db, workspace_id, project_id, admin_user, storage)
    previews = await _previews(db)

    masters = [uuid.UUID(c["master"]["media_id"]) for c in mastered["concepts"] if c["master"]]
    renditions = [uuid.UUID(r["media_id"]) for r in out["renditions"]]
    logos = [uuid.UUID(logo["media_id"]) for logo in out["logos"]]
    assert masters and renditions and logos
    assert set(previews) == {*masters, *renditions, *logos}  # one each, nothing else

    for source_id, preview in previews.items():
        source = await db.get(MediaArtifact, source_id)
        assert source is not None
        assert preview.asset_id == source.asset_id and preview.job_id == source.job_id
        assert (preview.media_type, preview.derivation) == (
            "image/webp", MediaArtifactDerivation.ENCODED,
        )  # fmt: skip
        assert preview.aspect_ratio == source.aspect_ratio
        assert preview.transform is not None
        assert preview.transform["sx"] == preview.transform["sy"]  # Law 39, for a proxy too
        assert preview.disclosure is None  # the file it stands for carries the stamp
        with Image.open(io.BytesIO(storage.get(preview.storage_path))) as opened:
            assert opened.format == "WEBP"
            assert opened.size == (preview.width, preview.height)
        assert max(preview.width, preview.height) == min(
            PREVIEW_LONG_SIDE, max(source.width, source.height)
        )
        assert preview.bytes < source.bytes

    for rendition in out["renditions"]:
        assert rendition["max_bytes"] == LIMIT
    for logo in out["logos"]:
        assert (logo["surface"], logo["max_bytes"]) == ("pmax_image", LIMIT)


async def test_content_redirects_signed_resolves_variants_and_seeks_with_range(
    admin: ApiClient,
    second_client: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    storage: LocalStorage,
) -> None:
    out, _, _ = await _pmax_run(admin, db, workspace_id, project_id, admin_user, storage)
    rendition = await db.get(MediaArtifact, uuid.UUID(out["renditions"][0]["media_id"]))
    assert rendition is not None
    preview = (await _previews(db))[rendition.id]
    email, password = await make_member(admin, "viewer")
    assert (await second_client.login(email, password)).status_code == 200
    viewer = second_client

    default = await viewer.get(f"/media/{rendition.id}/content")
    master = await viewer.get(f"/media/{rendition.id}/content", params={"variant": "master"})
    itself = await viewer.get(f"/media/{preview.id}/content", params={"variant": "preview"})
    poster = await viewer.get(f"/media/{rendition.id}/content", params={"variant": "poster"})
    unknown = await viewer.get(f"/media/{uuid.uuid4()}/content")
    bad = await viewer.get(f"/media/{rendition.id}/content", params={"variant": "original"})

    # The grid's default is the proxy — never the file — for any role that reads.
    key, token = _location(default)
    assert key == preview.storage_path
    verify(key, token)
    assert default.headers["cache-control"] == "private, max-age=240"
    assert _location(master)[0] == rendition.storage_path
    assert _location(itself)[0] == preview.storage_path
    # A variant never made is named, and nothing is served in its place.
    assert poster.status_code == 404
    assert poster.json()["code"] == "media_variant_missing"
    assert poster.json()["variant"] == "poster"
    assert unknown.status_code == 404
    assert bad.status_code == 422

    # The redirect target, on the real file server: seekable, and typed.
    app = create_file_server(storage=storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker"
    ) as fileserver:
        location = default.headers["location"]
        part = await fileserver.get(location, headers={"Range": "bytes=0-99"})
        whole = await fileserver.get(master.headers["location"])
    assert part.status_code == 206
    assert part.headers["content-range"] == f"bytes 0-99/{preview.bytes}"
    assert part.headers["content-type"] == "image/webp"
    assert part.content == storage.get(preview.storage_path)[:100]
    assert whole.status_code == 200 and whole.headers["content-type"] == "image/jpeg"


async def test_a_regeneration_is_priced_at_the_live_record_against_the_media_cap(
    admin: ApiClient,
    second_client: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    storage: LocalStorage,
) -> None:
    out, mastered, run_id = await _pmax_run(
        admin, db, workspace_id, project_id, admin_user, storage
    )
    response = await admin.put("/settings/media", json={"media_allowlist": ALLOWLIST})
    assert response.status_code == 200, response.text
    concept = next(c for c in mastered["concepts"] if c["master"])
    asset_id = concept["asset_id"]
    logo_asset = out["logos"][0]["asset_id"]
    email, password = await make_member(admin, "viewer")
    assert (await second_client.login(email, password)).status_code == 200

    path = f"/creative-assets/{asset_id}/regeneration-estimate"
    kept = await second_client.post(path, json={})  # READ: a viewer may see the price
    switched = await admin.post(path, json={"model_override": GEMINI})
    unlisted = await admin.post(path, json={"model_override": "acme/no-such-model"})
    unsupported = await admin.post(path, json={"params_override": {"quality": "ultra"}})
    logo = await admin.post(f"/creative-assets/{logo_asset}/regeneration-estimate", json={})
    missing = await admin.post(f"/creative-assets/{uuid.uuid4()}/regeneration-estimate", json={})
    spend = (await admin.get(f"/runs/{run_id}")).json()["creative_spend"]["media"]

    assert kept.status_code == 200, kept.text
    body = kept.json()
    assert (body["modality"], body["model_id"], body["requests"]) == ("image", FLUX, 1)
    assert body["params"][0]["aspect_ratio"] == concept["aspect_ratio"]
    models = (await admin.get("/media/models", params={"modality": "image"})).json()
    live = next(row for row in models["models"] if row["model_id"] == FLUX)
    project = await db.get(Project, project_id)
    expected = image_job_price(
        CapabilityRecord.model_validate(live["capability"]),
        body["params"][0],
        creative_constants_for(project).media_constants(),
    ).usd.quantize(Decimal("0.0001"))
    assert Decimal(body["estimate_usd"]) == expected > 0
    # What remains is the cap less spent and reserved — the console's own meter.
    assert body["media"] == spend
    remaining = (
        Decimal(spend["cap_usd"]) - Decimal(spend["spent_usd"]) - Decimal(spend["reserved_usd"])
    )
    assert Decimal(body["remaining_usd"]) == remaining
    assert Decimal(body["remaining_after_usd"]) == remaining - expected
    assert body["fits"] is True

    assert switched.status_code == 200, switched.text
    assert switched.json()["model_id"] == GEMINI
    assert unlisted.status_code == 422
    assert unlisted.json()["code"] == "media_model_not_allowlisted"
    assert unsupported.status_code == 422
    assert unsupported.json()["code"] == "capability_unsupported"
    assert logo.status_code == 422 and logo.json()["code"] == "not_generated_media"
    assert missing.status_code == 404

    # Nothing was reserved or written by pricing.
    after = (await admin.get(f"/runs/{run_id}")).json()["creative_spend"]["media"]
    assert after == spend
    kinds = (
        await db.execute(
            sa.select(CreativeAsset.kind).where(CreativeAsset.creative_run_id == run_id)
        )
    ).scalars()
    assert CreativeAssetKind.IMAGE in set(kinds)
