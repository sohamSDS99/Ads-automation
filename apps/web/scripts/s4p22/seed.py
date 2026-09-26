"""Seed S4-P22's stack. Runs inside the `worker` container, which owns /data —
the Volume the file server serves:

    docker compose -p s4p22 ... exec -T worker python /app/s4p22/seed.py

Four projects, one creative run each, and a JSON line naming them:

- **review** — G8 with 20 assets. The run reaches G8 through the REAL path —
  4.1.1 → G7 → 4.4.1–4.4.5, S4-P13's `_to_g8` (painted by S4-P10's
  `_Painter`) — so the card, the approval, its routing to the pinned brand
  owner and the pinned image model are what production makes. The card is
  then widened to 20 items: 17 more images (real JPEGs on the Volume, real
  WebP proxies from `creative.previews.store_preview`) and one video (the
  committed fixture clip, its proxy and poster) — only because no fixture
  model paints 20. Half the images name the `sds-binder` product reference,
  whose file is on the Volume, uploaded before the gate opened.
- **rereview** — G8b, reached for real: `_to_g8`, the brand owner records G8
  with one regeneration, and the executor runs 4.4.6 → 4.4.7.
- **clear** ("Spring launch") / **withdraw** ("Autumn refresh") — each a text run parked at H3 with three
  exceptions (a claim with a same-slot fallback, an image right that drops,
  a disclaimer), S4-P14's `h3_run`: one for the legal owner to clear, one
  for an operator to withdraw.

Members (password `quarry-lantern-98-fog`): Bea Brand, brand@ (approver,
G8's brand owner); Lee Legal, legal@ (approver, the named legal owner); Oli
Ops, ops@ (operator); Vic Viewer, viewer@ (viewer). The bootstrap admin is the performance owner.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, "/app")

import numpy as np  # noqa: E402
import respx  # noqa: E402
import sqlalchemy as sa  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from agent.config import get_settings  # noqa: E402
from agent.creative.previews import store_preview  # noqa: E402
from agent.db.models import (  # noqa: E402
    Approval,
    CreativeAsset,
    CreativeAssetKind,
    GenerationJob,
    GenerationModality,
    GenerationStatus,
    MediaArtifact,
    MediaArtifactDerivation,
    MediaArtifactRole,
    MediaReference,
    MediaReferenceKind,
    MediaReferenceOrigin,
    Membership,
    NodeRun,
    Project,
    User,
)
from agent.db.session import get_sessionmaker  # noqa: E402
from agent.postprod.image import TRAINED  # noqa: E402
from agent.schemas.creative_review import (  # noqa: E402
    AiAssetReview,
    ReviewAdvisory,
    ReviewItem,
    ReviewRendition,
)
from agent.storage.local import LocalStorage  # noqa: E402
from tests.integration.conftest import ADMIN_PASSWORD, build_client  # noqa: E402
from tests.integration.s4p14_support import h3_run  # noqa: E402
from tests.integration.s4p9_support import BASE, IMAGE_MODEL  # noqa: E402
from tests.integration.test_media_routes import ALLOWLIST  # noqa: E402
from tests.integration.test_s4p13_asset_review import TICKED, _execute, _gate, _to_g8  # noqa: E402

ADMIN_EMAIL = os.environ.get("BOOTSTRAP_ADMIN_EMAIL", "admin@example.com")
ADMIN_BOOT_PASSWORD = os.environ.get("BOOTSTRAP_ADMIN_PASSWORD", "change-me-at-least-12-chars")
TOTAL = 20
VIDEO_AT = 6
PRODUCT_REF = "sds-binder"
FIXTURE_CLIP = Path("/app/tests/fixtures/video/landscape_6s_silent.mp4")

NOTES = [
    "The binder's spine label is legible at 1:1 and softens at 1.91:1.",
    "A hand at the left edge is partly cropped; no face is shown.",
    "The background shelf carries no third-party logo.",
]
FLAGS = ["small_text", "person_partial", "logo_like_shape"]


def _picture(width: int, height: int, seed: int) -> bytes:
    """A smooth two-tone field with a soft subject — a different picture per
    seed, compresses like a photo, carries no text."""
    rng = np.random.default_rng(seed)
    base = rng.integers(40, 200, 3)
    other = rng.integers(40, 200, 3)
    ys = np.linspace(0, 1, height, dtype=np.float32)[:, None, None]
    field = (base * (1 - ys) + other * ys).astype(np.uint8)
    image = Image.fromarray(np.broadcast_to(field, (height, width, 3)).copy(), mode="RGB")
    draw = ImageDraw.Draw(image)
    cx, cy, r = width * rng.uniform(0.3, 0.7), height * rng.uniform(0.35, 0.65), min(width, height) * 0.22
    draw.rounded_rectangle((cx - r, cy - r * 1.3, cx + r * 0.8, cy + r * 1.3), radius=r * 0.15,
                           fill=tuple(int(v) for v in rng.integers(0, 255, 3)))  # fmt: skip
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=82, optimize=True)
    return buffer.getvalue()


def _binder() -> bytes:
    """The product reference: a flat studio shot of a red ring binder."""
    image = Image.new("RGB", (800, 800), (236, 233, 226))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((250, 140, 560, 660), radius=24, fill=(178, 34, 34))
    draw.rectangle((250, 140, 300, 660), fill=(140, 24, 24))
    draw.rectangle((340, 260, 520, 330), fill=(245, 245, 240))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


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


async def _member(admin: Any, role: str, email: str, name: str) -> uuid.UUID:
    """Invite and accept one named member (conftest's `make_member` names
    everyone after their role, and "Awaiting Approver" would not say who)."""
    created = await admin.post("/users/invite", json={"email": email, "name": name, "role": role})
    assert created.status_code == 201, created.text
    joiner = build_client()
    async with joiner.raw:
        accepted = await joiner.post(
            f"/invites/{created.json()['link'].rsplit('/', 1)[-1]}/accept",
            json={"name": name, "password": ADMIN_PASSWORD},
        )
        assert accepted.status_code == 200, accepted.text
    return await _user_id(email)


async def _user_id(email: str) -> uuid.UUID:
    async with get_sessionmaker()() as db:
        return (await db.execute(sa.select(User.id).where(User.email == email))).scalar_one()


async def _project(workspace_id: uuid.UUID, actor: uuid.UUID, name: str) -> uuid.UUID:
    async with get_sessionmaker()() as db:
        row = Project(
            workspace_id=workspace_id,
            created_by=actor,
            name=name,
            domain=f"{name.lower().replace(' ', '-')}.example",
        )
        db.add(row)
        await db.commit()
        return row.id


def _artifact(storage: LocalStorage, *, workspace_id: uuid.UUID, asset_id: uuid.UUID, job_id: uuid.UUID | None,
              role: MediaArtifactRole, key: str, content: bytes, media_type: str, width: int, height: int,
              ratio: str, derived_from: uuid.UUID | None = None) -> MediaArtifact:  # fmt: skip
    storage.put(key, content, content_type=media_type)
    return MediaArtifact(
        workspace_id=workspace_id, asset_id=asset_id, job_id=job_id, role=role, storage_path=key,
        media_type=media_type, width=width, height=height, bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(), aspect_ratio=ratio,
        derivation=MediaArtifactDerivation.NATIVE, derived_from=derived_from, probe={},
        disclosure={"xmp_digital_source_type": TRAINED, "visible_labels": []},
    )  # fmt: skip


async def review_run(admin: Any, workspace_id: uuid.UUID, actor: uuid.UUID, brand_id: uuid.UUID,
                     storage: LocalStorage) -> dict[str, str]:  # fmt: skip
    project_id = await _project(workspace_id, actor, "Review twenty")
    async with get_sessionmaker()() as db:
        with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
            run_id, _ = await _to_g8(admin, db, (workspace_id, project_id, actor), router, brand_owner=brand_id)

    async with get_sessionmaker()() as db:
        approval = await _gate(db, run_id, "G8")
        assert approval is not None
        card = AiAssetReview.model_validate(approval.proposal)
        real = list(card.items)
        template = await db.get(CreativeAsset, real[0].asset_id)
        assert template is not None

        # The product reference, on the Volume before the gate opened.
        binder = _binder()
        sha = hashlib.sha256(binder).hexdigest()
        key = f"references/{project_id}/{sha}.png"
        storage.put(key, binder, content_type="image/png")
        db.add(
            MediaReference(
                workspace_id=workspace_id, project_id=project_id, kind=MediaReferenceKind.PRODUCT_REFERENCE,
                storage_path=key, media_type="image/png", width=800, height=800, bytes=len(binder), sha256=sha,
                product_ref=PRODUCT_REF, origin=MediaReferenceOrigin.OWN,
                rights_statement="Photographed in house for SDS Manager.", attested_by=actor,
                attested_at=approval.created_at - timedelta(hours=1),
                created_at=approval.created_at - timedelta(hours=1),
            )
        )  # fmt: skip

        now = datetime.now(UTC)
        items: list[ReviewItem] = [
            item.model_copy(update={"product_refs": [PRODUCT_REF]}) if index == 0 else item
            for index, item in enumerate(real)
        ]
        extra = TOTAL - len(items) - 1
        for i in range(extra):
            asset = CreativeAsset(
                workspace_id=workspace_id, project_id=project_id, creative_run_id=run_id,
                node_id=template.node_id, campaign_ref=template.campaign_ref, ad_group_ref=template.ad_group_ref,
                kind=CreativeAssetKind.IMAGE, surface=template.surface, generated_by_ai=True,
                status=template.status,
                fields={**(template.fields or {}), "concept_id": f"{template.campaign_ref}:s{i + 1}"},
                lineage={"origin": "generated", "node_id": template.node_id}, content_hash=f"s4p22-{i}",
            )  # fmt: skip
            db.add(asset)
            await db.flush()
            job = GenerationJob(
                workspace_id=workspace_id, project_id=project_id, creative_run_id=run_id, node_id="4.4.2",
                asset_id=asset.id, round=1, modality=GenerationModality.IMAGE, model_id=IMAGE_MODEL,
                provider_tag="black-forest-labs", capability_hash="s4p22-seed",
                request={"aspect_ratio": "1:1", "seed": i}, idempotency_key=f"s4p22-{run_id}-{i}",
                status=GenerationStatus.COMPLETED, estimate_usd=Decimal("0.0100"), cost_usd=Decimal("0.0100"),
                attempts=1, submitted_at=now, completed_at=now,
            )  # fmt: skip
            db.add(job)
            await db.flush()
            shown: list[ReviewRendition] = []
            for ratio, width, height in (("1.91:1", 1200, 628), ("1:1", 1200, 1200)):
                content = _picture(width, height, 1000 + i * 7 + width)
                row = _artifact(
                    storage, workspace_id=workspace_id, asset_id=asset.id, job_id=job.id,
                    role=MediaArtifactRole.RENDITION,
                    key=f"creative/{run_id}/renditions/{asset.id}/{ratio.replace(':', 'x').replace('.', '_')}.jpg",
                    content=content, media_type="image/jpeg", width=width, height=height, ratio=ratio,
                )  # fmt: skip
                db.add(row)
                await db.flush()
                await store_preview(db, storage, row, content)
                shown.append(
                    ReviewRendition(
                        media_id=row.id, surface="search_image", ratio=ratio, px=f"{width}x{height}",
                        derivation="native", bytes=len(content), lint_verdict="pass",
                    )  # fmt: skip
                )
            items.append(
                ReviewItem(
                    asset_id=asset.id, kind="image", campaign_ref=template.campaign_ref,
                    concept_id=f"{template.campaign_ref}:s{i + 1}", renditions=shown,
                    disclosure={"xmp_digital_source_type": TRAINED, "visible_labels": []},
                    product_refs=[PRODUCT_REF] if i % 2 == 0 else [],
                    vision_advisory=ReviewAdvisory(
                        notes=[NOTES[i % len(NOTES)]] if i % 3 != 2 else [],
                        flags=[FLAGS[i % len(FLAGS)]] if i % 4 == 1 else [],
                    ),
                )  # fmt: skip
            )

        # One video: the committed fixture clip, its proxy and its poster.
        video = CreativeAsset(
            workspace_id=workspace_id, project_id=project_id, creative_run_id=run_id, node_id="4.4.4",
            campaign_ref=template.campaign_ref, kind=CreativeAssetKind.VIDEO, surface="youtube_video",
            generated_by_ai=True, status=template.status, fields={"concept_id": f"{template.campaign_ref}:v1"},
            lineage={"origin": "generated", "node_id": "4.4.4"}, content_hash="s4p22-video",
        )  # fmt: skip
        db.add(video)
        await db.flush()
        clip = FIXTURE_CLIP.read_bytes()
        master = _artifact(
            storage, workspace_id=workspace_id, asset_id=video.id, job_id=None, role=MediaArtifactRole.RENDITION,
            key=f"creative/{run_id}/video/{video.id}/16x9.mp4", content=clip, media_type="video/mp4",
            width=1280, height=720, ratio="16:9",
        )  # fmt: skip
        db.add(master)
        await db.flush()
        proxy = _artifact(
            storage, workspace_id=workspace_id, asset_id=video.id, job_id=None, role=MediaArtifactRole.PREVIEW,
            key=f"creative/{run_id}/video/{video.id}/16x9-480p.mp4", content=clip, media_type="video/mp4",
            width=1280, height=720, ratio="16:9", derived_from=master.id,
        )  # fmt: skip
        poster_bytes = _picture(1280, 720, 4242)
        poster = _artifact(
            storage, workspace_id=workspace_id, asset_id=video.id, job_id=None, role=MediaArtifactRole.POSTER,
            key=f"creative/{run_id}/video/{video.id}/16x9-poster.jpg", content=poster_bytes,
            media_type="image/jpeg", width=1280, height=720, ratio="16:9", derived_from=master.id,
        )  # fmt: skip
        db.add_all([proxy, poster])
        await db.flush()
        items.insert(
            VIDEO_AT,
            ReviewItem(
                asset_id=video.id, kind="video", campaign_ref=template.campaign_ref,
                concept_id=f"{template.campaign_ref}:v1",
                renditions=[
                    ReviewRendition(
                        media_id=master.id, surface="video", ratio="16:9", px="1280x720", derivation="native",
                        bytes=len(clip), duration_ms=6000, brand_first_at_ms=0, preview_media_id=proxy.id,
                        poster_media_id=poster.id,
                    )
                ],
                disclosure={"mp4_comment": "Made with AI", "visible_labels": ["Made with AI"]},
                vision_advisory=ReviewAdvisory(notes=["The logo is on screen from 0.4 s."]),
            ),
        )  # fmt: skip
        assert len(items) == TOTAL, len(items)

        widened = card.model_copy(update={"items": items}).model_dump(mode="json")
        approval.proposal = widened
        node = (
            await db.execute(
                sa.select(NodeRun)
                .where(NodeRun.run_id == run_id, NodeRun.node_id == approval.node_id)
                .order_by(NodeRun.attempt.desc())
            )
        ).scalars().first()
        if node is not None and node.output:
            node.output = widened
        await db.commit()
        return {"review_project_id": str(project_id), "review_run_id": str(run_id), "review_approval_id": str(approval.id)}


async def rereview_run(admin: Any, brand: Any, workspace_id: uuid.UUID, actor: uuid.UUID,
                       brand_id: uuid.UUID) -> dict[str, str]:  # fmt: skip
    project_id = await _project(workspace_id, actor, "Re-review")
    async with get_sessionmaker()() as db:
        with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
            run_id, g8 = await _to_g8(admin, db, (workspace_id, project_id, actor), router, brand_owner=brand_id)
            keep, again = (item["asset_id"] for item in g8.proposal["items"])
            decided = await brand.post(
                f"/approvals/{g8.id}",
                json={
                    "decision": "approve",
                    "edited_proposal": {
                        "items": [
                            {"asset_id": keep, "decision": "approve", "checklist": TICKED},
                            {"asset_id": again, "decision": "regenerate", "note": "Warmer light, closer framing."},
                        ]
                    },
                },
            )
            assert decided.status_code == 200, decided.text
            await _execute(run_id)
        g8b = await _gate(db, run_id, "G8b")
        assert g8b is not None and g8b.status.value == "pending", "G8b did not open"
    return {"rereview_project_id": str(project_id), "rereview_run_id": str(run_id)}


async def h3(admin: Any, workspace_id: uuid.UUID, actor: uuid.UUID, legal_id: uuid.UUID, slug: str,
             name: str) -> dict[str, str]:  # fmt: skip
    project_id = await _project(workspace_id, actor, name)
    async with get_sessionmaker()() as db:
        parked = await h3_run(admin, db, workspace_id, project_id, actor, legal_id)
    return {f"{slug}_project_id": str(project_id), f"{slug}_run_id": str(parked.run_id)}


async def main() -> None:
    storage = LocalStorage(os.environ.get("STORAGE_DIR", "/data"))
    workspace_id, actor = await _identity()
    admin = build_client()
    async with admin.raw:
        logged = await admin.post("/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_BOOT_PASSWORD})
        assert logged.status_code == 200, logged.text
        connected = await admin.post("/connections/openrouter/connect")
        assert connected.status_code == 200, connected.text
        allowed = await admin.put("/settings/media", json={"media_allowlist": ALLOWLIST})
        assert allowed.status_code == 200, allowed.text
        brand_id = await _member(admin, "approver", "brand@example.com", "Bea Brand")
        legal_id = await _member(admin, "approver", "legal@example.com", "Lee Legal")
        await _member(admin, "operator", "ops@example.com", "Oli Ops")
        await _member(admin, "viewer", "viewer@example.com", "Vic Viewer")
        # The in-process pipeline answers through the suite's fakes, which mock
        # OpenRouter at its real base URL; the stack's `api` keeps reading the
        # recorded catalogue from the `catalogue` service.
        os.environ["OPENROUTER_BASE_URL"] = BASE
        get_settings.cache_clear()
        brand = build_client()
        async with brand.raw:
            assert (await brand.login("brand@example.com", ADMIN_PASSWORD)).status_code == 200
            seeded: dict[str, str] = {}
            seeded |= await review_run(admin, workspace_id, actor, brand_id, storage)
            seeded |= await rereview_run(admin, brand, workspace_id, actor, brand_id)
        seeded |= await h3(admin, workspace_id, actor, legal_id, "clear", "Spring launch")
        seeded |= await h3(admin, workspace_id, actor, legal_id, "withdraw", "Autumn refresh")
    print(json.dumps(seeded))


if __name__ == "__main__":
    asyncio.run(main())
