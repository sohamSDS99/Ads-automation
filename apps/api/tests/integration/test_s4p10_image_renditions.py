"""S4-P10 — 4.4.3 `image_renditions`, run through the executor (PRD §9.4, §11).

Exit criteria, asserted on the stored files and rows rather than on the output
alone:
* every required ratio is `native`, `relaid`, `crop` or a recorded gap;
* `sx == sy` on every persisted rendition;
* no logo on any `search_image` — with a registered logo on the pin;
* the XMP DigitalSourceType reads back from every stored file;
* a crop keeping too little saliency becomes a gap, never a bad crop.

Runs in the worker image (`exiftool`, `tesseract`): the real measurement and
the real stamp, no stand-ins.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest
import respx
import sqlalchemy as sa
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import (
    CreativeAsset,
    CreativeAssetKind,
    CreativeAssetStatus,
    Evidence,
    EvidenceSource,
    MediaArtifact,
    MediaArtifactRole,
)
from agent.imaging.precheck import template_from_bytes
from agent.media.capability import parse_ratio
from agent.postprod.image import COMPOSITE, TRAINED, read_stamp
from agent.postprod.probe import probe_image
from agent.storage.local import LocalStorage
from tests.integration.conftest import ApiClient
from tests.integration.s4p9_support import (
    SPEC,
    Provider,
    approve_g7,
    image_model,
    output_of,
    run_until_done,
    start_image_run,
)

TEXT = {
    "headline": {"max_chars": 30, "min_count": 3, "max_count": 15, **SPEC},
    "description": {"max_chars": 90, "min_count": 2, "max_count": 4, **SPEC},
}
SEARCH = {
    "search": {
        **TEXT,
        "path": {"max_chars": 15, "max_count": 2, **SPEC},
        "image_square": {"ratio": "1:1", "min_px": "300x300", "max_bytes": 5242880, **SPEC},
        "image_wide": {"ratio": "16:9", "min_px": "600x338", "max_bytes": 5242880, **SPEC},
        "image_landscape": {"ratio": "1.91:1", "min_px": "600x314", "max_bytes": 5242880, **SPEC},
        "image_portrait": {"ratio": "9:16", "min_px": "338x600", "max_bytes": 5242880, **SPEC},
    }
}
PMAX = {
    "performance_max": {
        **TEXT,
        "image_square": {"ratio": "1:1", "min_px": "300x300", "max_bytes": 5242880, **SPEC},
        "image_landscape": {"ratio": "1.91:1", "min_px": "600x314", "max_bytes": 5242880, **SPEC},
        "logo": {"ratio": "1:1", "min_px": "128x128", "max_bytes": 5242880, **SPEC},
    }
}
LOGO_RULES = {"clear_space_ratio": 0.25, "min_width_px": 64}


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> LocalStorage:
    from agent.config import get_settings

    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    get_settings.cache_clear()
    return LocalStorage(str(tmp_path))


def _photo(size: tuple[int, int], seed: int, tone: int = 200) -> bytes:
    """Photograph-like: a soft gradient, sensor noise and one soft subject — no
    text for OCR to find, and real structure for saliency."""
    width, height = size
    rng = np.random.default_rng(seed)
    ys, xs = np.mgrid[0:height, 0:width]
    field = tone - 30 * (ys / height) + rng.normal(0, 4, (height, width))
    subject = np.exp(-(((xs - width * 0.55) / (width * 0.12)) ** 2
                       + ((ys - height * 0.5) / (height * 0.14)) ** 2))  # fmt: skip
    gray = np.clip(field - 90 * subject, 0, 255).astype(np.uint8)
    rgb = np.stack([gray, np.clip(gray.astype(int) + 8, 0, 255).astype(np.uint8), gray], axis=2)
    buffer = io.BytesIO()
    Image.fromarray(rgb, mode="RGB").save(buffer, format="PNG")
    return buffer.getvalue()


class _Painter(Provider):
    """A provider that paints what it is asked for: every image at the
    request's `aspect_ratio`, each one a different photograph."""

    SIZES = {"1:1": (512, 512), "16:9": (1024, 576)}

    def __init__(self) -> None:
        super().__init__([])
        self.painted = 0

    def _paint(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        size = self.SIZES[payload["aspect_ratio"]]
        self.images = [_photo(size, self.painted + i) for i in range(int(payload.get("n") or 1))]
        self.painted += len(self.images)
        return super()._paint(request)


async def _registered_logo(
    db: AsyncSession, storage: LocalStorage, project_id: uuid.UUID
) -> dict[str, Any]:
    """A loose logo upload as Stage 03 registers it: the file on the Volume, a
    `brand_book_asset` row pointing at it, and the ruleset's template."""
    image = Image.new("RGBA", (440, 140), (0, 0, 0, 0))
    image.paste(Image.new("RGBA", (400, 100), (20, 24, 32, 255)), (20, 20))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    content = buffer.getvalue()
    key = f"brand/{project_id}/logo-primary.png"
    storage.put(key, content, content_type="image/png")
    row = Evidence(
        project_id=project_id,
        source=EvidenceSource.WEB,
        kind="brand_book_asset",
        payload={"asset_path": key, "page": None, "width_px": 440, "height_px": 140},
        content_text="brand asset 440x140",
        hash=hashlib.sha256(content).hexdigest(),
        fetched_at=datetime.now(UTC),
    )
    db.add(row)
    await db.commit()
    template = template_from_bytes(
        content, asset_id=row.id, label="primary logo", min_score=0.62, working_width=440
    )
    return {
        "asset_id": str(template.asset_id),
        "label": template.label,
        "phash": template.phash,
        "min_score": template.min_score,
        "descriptors_b64": template.descriptors_b64,
        "keypoint_count": template.keypoint_count,
    }


async def _renditions(
    admin: ApiClient,
    db: AsyncSession,
    ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    storage: LocalStorage,
    *,
    campaign_type: str,
    specs: dict[str, Any],
    image_input: bool,
) -> tuple[dict[str, Any], dict[str, Any], _Painter, uuid.UUID]:
    workspace_id, project_id, actor = ids
    template = await _registered_logo(db, storage, project_id)
    run_id = await start_image_run(
        admin, db, workspace_id, project_id, actor,
        capability=image_model(image_input=image_input),
        campaign_type=campaign_type, specs=specs,
        logo_templates=(template,), logo_rules=LOGO_RULES,
    )  # fmt: skip
    provider = _Painter()
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        provider.install(router)
        await run_until_done(run_id, through="4.4.3")
        await approve_g7(admin, db, run_id)
        await run_until_done(run_id, through="4.4.3")
    return (
        await output_of(db, run_id, "4.4.3"),
        await output_of(db, run_id, "4.4.2"),
        provider,
        run_id,
    )


async def _stored(db: AsyncSession, run_id: uuid.UUID) -> list[MediaArtifact]:
    return list(
        (
            await db.execute(
                sa.select(MediaArtifact)
                .join(CreativeAsset, CreativeAsset.id == MediaArtifact.asset_id)
                .where(
                    CreativeAsset.creative_run_id == run_id,
                    MediaArtifact.role == MediaArtifactRole.RENDITION,
                )
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )


async def test_search_every_ratio_is_accounted_for_uniform_stamped_and_logo_free(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    storage: LocalStorage,
) -> None:
    out, mastered, provider, run_id = await _renditions(
        admin, db, (workspace_id, project_id, admin_user.id), storage,
        campaign_type="search", specs=SEARCH, image_input=True,
    )  # fmt: skip

    required = {"1:1", "16:9", "1.91:1", "9:16"}
    master_ratio = {c["concept_id"]: c["aspect_ratio"] for c in mastered["concepts"]}
    assert len(master_ratio) == 2 and set(master_ratio.values()) <= {"1:1", "16:9"}
    for concept, own in master_ratio.items():
        other = ({"1:1", "16:9"} - {own}).pop()
        made = {
            r["ratio"]: r["derivation"] for r in out["renditions"] if r["concept_id"] == concept
        }
        gaps = {g["ratio"]: g["why"] for g in out["gaps"] if g["concept_id"] == concept}
        # relaid > native > crop: the master's own ratio is the master; the
        # other ratio the model paints is re-laid FROM it; 1.91:1 is cut from
        # the 16:9 frame that covers 93% of it; 9:16 is covered by nothing the
        # model paints — a recorded gap.
        assert made == {own: "native", other: "relaid", "1.91:1": "crop"}
        assert set(gaps) == {"9:16"}
        assert set(made) | set(gaps) == required

    # Never a logo on search_image — a registered logo was on the pin.
    assert all(r["surface"] == "search_image" for r in out["renditions"])
    assert not any(r["logo_composited"] for r in out["renditions"])
    assert all("search_image" in (r["logo_note"] or "") for r in out["renditions"])
    assert out["logos"] == []  # the search spec sheet has no logo slot

    # The relay was painted FROM the master: its POST carried the master's bytes.
    master_rows = {
        str(row.id): row
        for row in (
            await db.execute(
                sa.select(MediaArtifact).where(MediaArtifact.role == MediaArtifactRole.MASTER)
            )
        ).scalars()
    }
    relay_posts = provider.image_posts[2:]
    relayed = {({"1:1", "16:9"} - {own}).pop() for own in master_ratio.values()}
    assert {post["aspect_ratio"] for post in relay_posts} == relayed and len(relay_posts) == 2
    sent = json.dumps(relay_posts)
    for row in master_rows.values():
        assert base64.b64encode(storage.get(row.storage_path)).decode() in sent

    stored = await _stored(db, run_id)
    assert len(stored) == 6
    for row in stored:
        assert row.transform is not None and row.transform["sx"] == row.transform["sy"]
        content = storage.get(row.storage_path)
        assert read_stamp(content)["DigitalSourceType"] == TRAINED
        facts = probe_image(content)
        assert (facts.format, facts.exif, facts.gps) == ("JPEG", False, False)
        assert (facts.width, facts.height) == (row.width, row.height)
        assert abs(facts.width / facts.height / parse_ratio(row.aspect_ratio) - 1) <= 0.005
        assert row.disclosure == {"xmp_digital_source_type": TRAINED, "visible_labels": []}
    for rendition in out["renditions"]:
        assert rendition["lint"]["verdict"] in ("pass", "pass_with_warnings")
        assert rendition["scale"]["sx"] == rendition["scale"]["sy"]


async def test_pmax_logo_is_composited_fitted_and_a_thin_crop_is_a_gap(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    storage: LocalStorage,
) -> None:
    out, _, provider, run_id = await _renditions(
        admin, db, (workspace_id, project_id, admin_user.id), storage,
        campaign_type="performance_max", specs=PMAX, image_input=False,
    )  # fmt: skip

    # 1:1 is the master's own ratio. 1.91:1 is a crop by the plan (16:9 would
    # cover it) — but the only frame painted is the 1:1 master, whose widest
    # 1.91:1 window keeps about half its saliency: a gap, never a bad crop.
    assert {(r["ratio"], r["derivation"]) for r in out["renditions"]} == {("1:1", "native")}
    thin = [g for g in out["gaps"] if g["ratio"] == "1.91:1"]
    assert len(thin) == 2 and all("saliency" in g["why"] for g in thin)
    assert len(provider.image_posts) == 2  # a crop costs nothing; no job was sent

    for rendition in out["renditions"]:
        assert rendition["surface"] == "pmax_image"
        assert rendition["logo_composited"] is True
        assert rendition["disclosure"]["xmp_digital_source_type"] == COMPOSITE
    stored = await _stored(db, run_id)
    renditions = [row for row in stored if row.aspect_ratio == "1:1" and row.transform
                  and "logo" in row.transform and row.transform.get("logo")]  # fmt: skip
    assert len(renditions) == 2
    for row in renditions:
        assert read_stamp(storage.get(row.storage_path))["DigitalSourceType"] == COMPOSITE
        box = row.transform["logo"]["box"]
        assert box[2] - box[0] >= LOGO_RULES["min_width_px"]
        assert row.transform["sx"] == row.transform["sy"]

    # The spec sheet's logo slot: the registered logo, fitted by padding.
    (fitted,) = out["logos"]
    assert (fitted["asset_type"], fitted["ratio"]) == ("logo", "1:1")
    assert fitted["scale"]["sx"] == fitted["scale"]["sy"]
    asset = await db.get(CreativeAsset, uuid.UUID(fitted["asset_id"]))
    assert asset is not None
    assert (asset.kind, asset.generated_by_ai, asset.status) == (
        CreativeAssetKind.LOGO, False, CreativeAssetStatus.LINTED,
    )  # fmt: skip
    logo_file = await db.get(MediaArtifact, uuid.UUID(fitted["media_id"]))
    assert logo_file is not None and logo_file.disclosure is None  # nobody generated it
    with Image.open(io.BytesIO(storage.get(logo_file.storage_path))) as png:
        assert png.size[0] == png.size[1] >= 128
        alpha = np.asarray(png.convert("RGBA").getchannel("A"))
    ys, xs = np.nonzero(alpha > 128)
    assert (xs.max() - xs.min() + 1) / (ys.max() - ys.min() + 1) == pytest.approx(4.0, abs=0.05)
