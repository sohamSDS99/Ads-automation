"""Seed S4-P21's stack. Runs inside the `worker` container, which owns /data —
the Volume the file server serves:

    docker compose -p s4p21 ... exec -T worker python /app/s4p21/seed.py

Three projects, each with one creative run, and a JSON line naming them:

- **images** — the REAL pipeline, 4.1.1 → 4.4.1 → 4.4.2 → 4.4.3, as S4-P10's
  suite drives it (`_Painter` paints at the requested ratio; a registered logo
  is fitted): concepts, masters, native / relaid / cropped renditions, a
  fitted logo slot, recorded gaps — and, since S4-P21, every WebP proxy. The
  brand's colour tokens are on the published guideline, so the brief, the
  concepts and the board's swatches carry them.
- **video** — the REAL 4.4.4, as S4-P12's suite drives it: the committed
  fixture clips assembled, captioned, end-carded, loudness-normalised,
  verified and stamped, with the 480p proxy and poster.
- **scale** — 500 tiles for the frame-budget check (§15.5 item 2): 25
  concepts × 4 image surfaces × 5 ratios, a gap every 23rd slot, each file a
  real JPEG on the Volume with a real WebP proxy written by
  `creative.previews.store_preview`, recorded as 4.4.1–4.4.3 would record
  them (validated through the node output schemas). Only this run's rows are
  synthetic, and only because no fixture model paints 475 files.

Everything else is exactly what the integration suite seeds (`creative_support`).
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

sys.path.insert(0, "/app")

import numpy as np  # noqa: E402
import respx  # noqa: E402
import sqlalchemy as sa  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

import tests.integration.creative_support as creative_support  # noqa: E402
from agent.creative.previews import store_preview  # noqa: E402
from agent.db.models import (  # noqa: E402
    CreativeAsset,
    CreativeAssetKind,
    CreativeAssetStatus,
    GenerationJob,
    GenerationModality,
    GenerationStatus,
    MediaArtifact,
    MediaArtifactDerivation,
    MediaArtifactRole,
    Membership,
    NodeRun,
    NodeRunStatus,
    Project,
    Run,
    User,
)
from agent.db.session import get_sessionmaker  # noqa: E402
from agent.postprod.image import TRAINED  # noqa: E402
from agent.schemas.creative_input import CreativeInput  # noqa: E402
from agent.schemas.creative_media import CreativeConcepts, ImageMasters, ImageRenditions  # noqa: E402
from agent.storage.local import LocalStorage  # noqa: E402
from tests.integration.conftest import build_client  # noqa: E402
from tests.integration.creative_support import TEXT_ONLY, seed_plan, seed_published, seed_signoff  # noqa: E402
from agent.config import get_settings  # noqa: E402
from tests.integration.s4p9_support import (  # noqa: E402
    BASE,
    IMAGE_MODEL,
    SPEC,
    approve_g7,
    image_model,
    model_choice,
    run_until_done,
    start_image_run,
)
from tests.integration.test_media_routes import ALLOWLIST  # noqa: E402
from tests.integration.test_s4p10_image_renditions import (  # noqa: E402
    LOGO_RULES,
    TEXT,
    _Painter,
    _registered_logo,
)
from tests.integration.test_s4p12_video_postprod import FixtureVideos, _produce, _start  # noqa: E402

ADMIN_EMAIL = os.environ.get("BOOTSTRAP_ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD = os.environ.get("BOOTSTRAP_ADMIN_PASSWORD", "change-me-at-least-12-chars")
LIMIT = 5242880

#: The brand's colour tokens as Stage 03 records them (S4-P12's pair and one more).
TOKENS = [
    {"name": "sand", "hex": "#f1e9da", "role": "neutral"},
    {"name": "ink", "hex": "#1d3557", "role": "primary"},
    {"name": "signal", "hex": "#e76f51", "role": "accent"},
]

#: A Performance Max sheet with four image ratios and a logo slot: 1:1 and
#: 16:9 the fixture model paints (native, and relaid from the master), 1.91:1
#: and 4:5 cut from them or recorded as gaps.
IMAGE_SPECS = {
    "performance_max": {
        **TEXT,
        "image_square": {"ratio": "1:1", "min_px": "300x300", "max_bytes": LIMIT, **SPEC},
        "image_wide": {"ratio": "16:9", "min_px": "600x338", "max_bytes": LIMIT, **SPEC},
        "image_landscape": {"ratio": "1.91:1", "min_px": "600x314", "max_bytes": LIMIT, **SPEC},
        "image_portrait": {"ratio": "4:5", "min_px": "480x600", "max_bytes": LIMIT, **SPEC},
        "logo": {"ratio": "1:1", "min_px": "128x128", "max_bytes": LIMIT, **SPEC},
    }
}

_payload = creative_support.guideline_payload


def _with_tokens(guideline: Any, **kwargs: Any) -> dict[str, Any]:
    payload = _payload(guideline, **kwargs)
    payload["brand_rules"]["visual_identity"]["colour"] = {"primary": "ink", "tokens": TOKENS}
    return payload


creative_support.guideline_payload = _with_tokens


async def _identity() -> tuple[uuid.UUID, uuid.UUID]:
    async with get_sessionmaker()() as db:
        admin = (await db.execute(sa.select(User).where(User.email == ADMIN_EMAIL))).scalar_one()
        workspace_id = (
            (await db.execute(sa.select(Membership.workspace_id).where(Membership.user_id == admin.id)))
            .scalars()
            .first()
        )
        assert workspace_id is not None, "the bootstrap admin has no workspace"
        return workspace_id, admin.id


async def _project(workspace_id: uuid.UUID, actor: uuid.UUID, name: str) -> uuid.UUID:
    async with get_sessionmaker()() as db:
        row = Project(
            workspace_id=workspace_id,
            created_by=actor,
            name=name,
            domain=f"{name.lower().replace(' ', '-').replace('—', '')}.example",
        )
        db.add(row)
        await db.commit()
        return row.id


async def image_run(admin: Any, workspace_id: uuid.UUID, actor: uuid.UUID, storage: LocalStorage) -> dict[str, str]:
    project_id = await _project(workspace_id, actor, "Media library images")
    async with get_sessionmaker()() as db:
        template = await _registered_logo(db, storage, project_id)
        run_id = await start_image_run(
            admin, db, workspace_id, project_id, actor,
            capability=image_model(image_input=True), campaign_type="performance_max",
            specs=IMAGE_SPECS, logo_templates=(template,), logo_rules=LOGO_RULES,
        )  # fmt: skip
        await db.commit()
        provider = _Painter()
        with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
            provider.install(router)
            await run_until_done(run_id, through="4.4.3")
            await approve_g7(admin, db, run_id)
            await run_until_done(run_id, through="4.4.3")
    return {"image_project_id": str(project_id), "image_run_id": str(run_id)}


async def video_run(admin: Any, workspace_id: uuid.UUID, actor: uuid.UUID, storage: LocalStorage) -> dict[str, str]:
    project_id = await _project(workspace_id, actor, "Media library video")
    async with get_sessionmaker()() as db:
        run_id = await _start(admin, db, storage, (workspace_id, project_id, actor))
        out = await _produce(admin, db, run_id, FixtureVideos())
    assert out["status"] == "produced" and out["videos"], out
    return {"video_project_id": str(project_id), "video_run_id": str(run_id)}


# ---------------------------------------------------------------------------
# the 500-tile run
# ---------------------------------------------------------------------------

SURFACES = ("search_image", "pmax_image", "display_image", "demand_gen_image")
RATIOS = (("1.91:1", 1200, 628), ("1:1", 1200, 1200), ("4:5", 960, 1200), ("9:16", 1080, 1920), ("16:9", 1920, 1080))
DERIVATIONS = ("native", "relaid", "crop")
CAMPAIGNS = 5
CONCEPTS_PER = 5
GAP_EVERY = 23


def _picture(width: int, height: int, seed: int) -> bytes:
    """A smooth two-tone field with a soft subject — compresses like a photo,
    reads as a different picture per concept, and has no text."""
    rng = np.random.default_rng(seed)
    base = rng.integers(40, 200, 3)
    other = rng.integers(40, 200, 3)
    ys = np.linspace(0, 1, height, dtype=np.float32)[:, None, None]
    field = (base * (1 - ys) + other * ys).astype(np.uint8)
    image = Image.fromarray(np.broadcast_to(field, (height, width, 3)).copy(), mode="RGB")
    draw = ImageDraw.Draw(image)
    cx, cy, r = width * rng.uniform(0.3, 0.7), height * rng.uniform(0.35, 0.65), min(width, height) * 0.22
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=tuple(int(v) for v in rng.integers(0, 255, 3)))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=82, optimize=True)
    return buffer.getvalue()


def _lint(version: str) -> dict[str, Any]:
    return {"verdict": "pass", "unchecked": False, "ruleset_version": version, "rule_ids": []}


async def scale_run(admin: Any, workspace_id: uuid.UUID, actor: uuid.UUID, storage: LocalStorage) -> dict[str, str]:
    project_id = await _project(workspace_id, actor, "Media library 500 tiles")
    async with get_sessionmaker()() as db:
        await seed_plan(db, workspace_id, project_id, actor, campaign_type="performance_max")
        await seed_published(db, workspace_id, project_id, actor, asset_specs=IMAGE_SPECS)
        await seed_signoff(db, workspace_id, project_id, actor)
        await db.commit()
    started = await admin.post(
        f"/projects/{project_id}/creative/runs", json={"scope": TEXT_ONLY, "media_models": []}
    )
    assert started.status_code == 202, started.text
    run_id = uuid.UUID(started.json()["run_id"])
    # As a real image run is pinned: images on, the fixture image model chosen
    # (`start_image_run` widens its run the same way), so the drawer can price
    # a regeneration of any tile.
    async with get_sessionmaker()() as db:
        run = await db.get(Run, run_id)
        stored = CreativeInput.model_validate(run.creative_input)
        widened = stored.model_copy(
            update={
                "scope": stored.scope.model_copy(update={"images": True}),
                "media_models": [model_choice(image_model(image_input=True))],
            }
        )
        run.creative_input = widened.model_dump(mode="json", by_alias=True)
        run.input_hash = widened.content_hash()
        await db.commit()

    now = datetime.now(UTC)
    version = "1.0"
    concepts: list[dict[str, Any]] = []
    masters: list[dict[str, Any]] = []
    renditions: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    slot = 0
    async with get_sessionmaker()() as db:
        for c in range(CAMPAIGNS):
            campaign = f"c-scale-{c + 1}"
            listed: list[dict[str, Any]] = []
            for k in range(CONCEPTS_PER):
                concept_id = f"{campaign}:c{k + 1}"
                seed = c * 100 + k
                asset = CreativeAsset(
                    workspace_id=workspace_id, project_id=project_id, creative_run_id=run_id,
                    node_id="4.4.2", campaign_ref=campaign, kind=CreativeAssetKind.IMAGE,
                    surface="pmax_image", generated_by_ai=True, status=CreativeAssetStatus.LINTED,
                    fields={"concept_id": concept_id, "product_depiction": "none"},
                    lineage={"origin": "generated", "node_id": "4.4.2"}, content_hash="seed",
                )  # fmt: skip
                db.add(asset)
                await db.flush()
                job = GenerationJob(
                    workspace_id=workspace_id, project_id=project_id, creative_run_id=run_id,
                    node_id="4.4.2", asset_id=asset.id, round=1, modality=GenerationModality.IMAGE,
                    model_id=IMAGE_MODEL, provider_tag="black-forest-labs", capability_hash="s4p21-seed",
                    request={"aspect_ratio": "1:1", "seed": seed}, idempotency_key=f"s4p21-scale-{run_id}-{concept_id}",
                    status=GenerationStatus.COMPLETED, estimate_usd=Decimal("0.0400"), cost_usd=Decimal("0.0400"),
                    attempts=1, submitted_at=now, completed_at=now,
                )  # fmt: skip
                db.add(job)
                await db.flush()
                master_bytes = _picture(1200, 1200, seed)
                master_key = f"creative/{run_id}/masters/{asset.id}.jpg"
                storage.put(master_key, master_bytes, content_type="image/jpeg")
                master = MediaArtifact(
                    workspace_id=workspace_id, asset_id=asset.id, job_id=job.id, role=MediaArtifactRole.MASTER,
                    storage_path=master_key, media_type="image/jpeg", width=1200, height=1200,
                    bytes=len(master_bytes), sha256=hashlib.sha256(master_bytes).hexdigest(),
                    aspect_ratio="1:1", derivation=MediaArtifactDerivation.NATIVE, probe={},
                )  # fmt: skip
                db.add(master)
                await db.flush()
                await store_preview(db, storage, master, master_bytes)
                asset.fields = {**asset.fields, "master_media_id": str(master.id)}
                listed.append(
                    {
                        "id": concept_id, "campaign_ref": campaign, "name": f"Scene {c + 1}.{k + 1}",
                        "angle": "angle", "angle_text": "Every sheet, current, on every site",
                        "rationale": "A worksite at rest, the product implied by the order around it.",
                        "subject": "a tidy worksite", "setting": "morning light",
                        "composition_by_ratio": {}, "palette_tokens": ["ink", "sand"],
                        "product_depiction": "none", "prompt": "a tidy worksite", "negative_constraints": [],
                        "surfaces": list(SURFACES),
                    }
                )  # fmt: skip
                masters.append(
                    {
                        "concept_id": concept_id, "campaign_ref": campaign, "asset_id": str(asset.id),
                        "aspect_ratio": "1:1", "reference_sha256s": [],
                        "candidates": [
                            {"job_id": str(job.id), "media_id": str(master.id), "seed": seed, "attempt": 1,
                             "lint": _lint(version), "vision_advisory": None}
                        ],
                        "master": {"media_id": str(master.id), "why": "The only candidate that passed image lint."},
                        "gap": None,
                    }
                )  # fmt: skip
                pictures = {ratio: _picture(w, h, seed * 10 + i) for i, (ratio, w, h) in enumerate(RATIOS)}
                for surface in SURFACES:
                    for i, (ratio, width, height) in enumerate(RATIOS):
                        slot += 1
                        if slot % GAP_EVERY == 0:
                            gaps.append(
                                {"campaign_ref": campaign, "concept_id": concept_id, "surface": surface, "ratio": ratio,
                                 "why": f"the best {ratio} window keeps 80% of the saliency, under the 85% floor"}
                            )  # fmt: skip
                            continue
                        content = pictures[ratio]
                        derivation = DERIVATIONS[(slot + i) % 3]
                        key = f"creative/{run_id}/renditions/{asset.id}/{surface}-{ratio.replace(':', 'x').replace('.', '_')}.jpg"
                        storage.put(key, content, content_type="image/jpeg")
                        row = MediaArtifact(
                            workspace_id=workspace_id, asset_id=asset.id, job_id=job.id,
                            role=MediaArtifactRole.RENDITION, storage_path=key, media_type="image/jpeg",
                            width=width, height=height, bytes=len(content),
                            sha256=hashlib.sha256(content).hexdigest(), aspect_ratio=ratio,
                            derivation=MediaArtifactDerivation(derivation),
                            derived_from=None if derivation == "native" else master.id,
                            transform=None if derivation == "native" else {"sx": 1.0, "sy": 1.0},
                            probe={}, disclosure={"xmp_digital_source_type": TRAINED, "visible_labels": []},
                        )  # fmt: skip
                        db.add(row)
                        await db.flush()
                        await store_preview(db, storage, row, content)
                        renditions.append(
                            {
                                "concept_id": concept_id, "campaign_ref": campaign, "asset_id": str(asset.id),
                                "media_id": str(row.id), "job_id": str(job.id), "surface": surface,
                                "ratio": ratio, "px": f"{width}x{height}", "derivation": derivation,
                                "scale": {"sx": 1.0, "sy": 1.0},
                                "retained_saliency": 0.93 if derivation == "crop" else 1.0,
                                "logo_composited": False, "logo_note": f"no logo on {surface} in this seed",
                                "bytes": len(content), "max_bytes": LIMIT, "lint": _lint(version),
                                "disclosure": {"xmp_digital_source_type": TRAINED, "visible_labels": []},
                            }
                        )  # fmt: skip
            concepts.append({"campaign_ref": campaign, "campaign_type": "performance_max", "concepts": listed})

        outputs = {
            "4.4.1": CreativeConcepts.model_validate(
                {"product_depiction": "none", "reference_refusals": {}, "campaigns": concepts}
            ),
            "4.4.2": ImageMasters.model_validate({"concepts": masters}),
            "4.4.3": ImageRenditions.model_validate({"renditions": renditions, "logos": [], "gaps": gaps}),
        }
        for node_id, output in outputs.items():
            db.add(
                NodeRun(
                    run_id=run_id, node_id=node_id, status=NodeRunStatus.SUCCEEDED, attempt=1,
                    output=output.model_dump(mode="json"), started_at=now, finished_at=now,
                )  # fmt: skip
            )
        await db.commit()
    assert len(renditions) + len(gaps) == 500, (len(renditions), len(gaps))
    return {"scale_project_id": str(project_id), "scale_run_id": str(run_id), "scale_files": str(len(renditions))}


async def main() -> None:
    storage = LocalStorage(os.environ.get("STORAGE_DIR", "/data"))
    workspace_id, actor = await _identity()
    admin = build_client()
    async with admin.raw:
        logged = await admin.post("/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
        assert logged.status_code == 200, logged.text
        # CR-E7: a key in the environment resolves only once the source is on.
        connected = await admin.post("/connections/openrouter/connect")
        assert connected.status_code == 200, connected.text
        allowed = await admin.put("/settings/media", json={"media_allowlist": ALLOWLIST})
        assert allowed.status_code == 200, allowed.text
        # The in-process pipeline answers through the suite's respx providers,
        # which mock OpenRouter at its real base URL; the stack's `api` keeps
        # reading the recorded catalogue from the `catalogue` service.
        os.environ["OPENROUTER_BASE_URL"] = BASE
        get_settings.cache_clear()
        seeded: dict[str, str] = {}
        seeded |= await image_run(admin, workspace_id, actor, storage)
        seeded |= await video_run(admin, workspace_id, actor, storage)
        seeded |= await scale_run(admin, workspace_id, actor, storage)
    print(json.dumps(seeded))


if __name__ == "__main__":
    asyncio.run(main())
