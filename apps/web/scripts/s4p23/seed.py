"""Seed S4-P23's stack. Runs inside the `worker` container, which owns /data —
the Volume the file server serves:

    docker compose -p s4p23 ... exec -T worker python /app/s4p23/seed.py

One project ("SDS Manager", sdsmanager.com) and four creative runs, each the
REAL whole DAG through S4-P16's golden path — G7 approved, H3 withdrawn, the
thirteen checks at 4.7.2 — with 4.6.4 rendering its previews in the image's
REAL Chromium (the integration suite fakes the renderer; this does not):

- **A** — S4-P16's golden run on offer SDS-PRO at $99, `ready_to_release`.
  The browser check releases it as v1 from the UI; nothing here touches it
  after 4.7.2.
- **C** — a second run on the same offers ($99).
- **D** — a run after SDS-PRO is re-observed at $89, so its promotion and
  price show the new figures.
- **B** — a run after SDS-PRO is observed back at $99 (a newer row), so the
  LIVE offer at the end of seeding is A's again: release re-resolves every
  binding at `now` (law 35), and A and B release, where a live $89 would make
  both `offer_drift`. B is untouched: v2 for the browser check.

**Media for the diff.** No fixture run ships media through 4.7 — S4-P16's
golden runs are text-only (docs/stage-04-questions.md § S4-P16) — so C and D
each get one AI image asset at `c-sds-us` concept `c1` (real JPEGs on the
Volume, real WebP previews from `creative.previews.store_preview`, real
`MediaArtifact` rows), appended to their packages' payloads through the
`CreativePackage` schema and re-hashed; D also adds concept `c2`. D against C
is then the diff the exit criterion names: the promotion's changed text, and
`c1` before and after, side by side. Neither is released, and release would
refuse both as `package_stale` — which is right: their rows no longer
assemble to what 4.7.2 checked.

Members (password `quarry-lantern-98-fog`): Ada Approver, approver@ (holds
creative_release); Oli Ops, ops@ (operator); Vic Viewer, viewer@ (viewer).
The bootstrap admin is renamed Pat Performance: they are the performance
owner who decides G7.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import itertools
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

sys.path.insert(0, "/app")


def _ordered_uuid4() -> uuid.UUID:
    """Creation-ordered ids for this process: a counter in the top 48 bits,
    the rest derived from it — the same seed makes the same ids.

    4.6.4 orders an ad's pool by `(node_id, created_at, id)`, and one node's
    assets share a transaction's `created_at` — so with random ids the preview
    combinations (and every capture) differ from one seed to the next, and no
    visual baseline could hold. Installed before any `agent` import, so the
    models' `default=uuid.uuid4` is this. The product's tie-break is recorded
    in docs/stage-04-questions.md § S4-P23.
    """
    n = next(_ID_COUNTER)
    low = int.from_bytes(hashlib.blake2b(n.to_bytes(8, "big"), digest_size=10).digest(), "big")
    return uuid.UUID(int=(n << 80) | low, version=4)


_ID_COUNTER = itertools.count(1)
uuid.uuid4 = _ordered_uuid4

import httpx  # noqa: E402
import numpy as np  # noqa: E402
import pytest  # noqa: E402
import sqlalchemy as sa  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from agent.config import get_settings  # noqa: E402
from agent.creative.package import hashed, package_row  # noqa: E402
from agent.creative.previews import store_preview  # noqa: E402
from agent.db.models import (  # noqa: E402
    CreativeAsset,
    CreativeAssetKind,
    CreativeAssetStatus,
    CreativePackageStatus,
    CredentialKind,
    Evidence,
    EvidenceSource,
    MediaArtifact,
    MediaArtifactDerivation,
    MediaArtifactRole,
    Membership,
    Project,
    SourceConnection,
    User,
)
from agent.db.session import get_sessionmaker  # noqa: E402
from agent.postprod.image import TRAINED  # noqa: E402
from agent.preview import landing, urlcheck  # noqa: E402
from agent.schemas.creative_package import CreativePackage, MediaAsset  # noqa: E402
from agent.schemas.landing import Box  # noqa: E402
from agent.storage.local import LocalStorage  # noqa: E402
from tests.integration.conftest import ADMIN_PASSWORD, build_client  # noqa: E402
from tests.integration.s4p9_support import BASE, IMAGE_MODEL  # noqa: E402
from tests.integration.test_s4p8_extras import FORM, FRESH, WEB, _Web  # noqa: E402
from tests.integration.test_s4p16_package import golden, run_creative  # noqa: E402

ADMIN_EMAIL = os.environ.get("BOOTSTRAP_ADMIN_EMAIL", "admin@example.com")
ADMIN_BOOT_PASSWORD = os.environ.get("BOOTSTRAP_ADMIN_PASSWORD", "change-me-at-least-12-chars")
CAMPAIGN = "c-sds-us"
RATIOS = (("1.91:1", 1200, 628), ("1:1", 1200, 1200))


def _picture(width: int, height: int, seed: int) -> bytes:
    """A smooth two-tone field with a soft subject: a different picture per
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


def _scripted_world(mp: pytest.MonkeyPatch) -> None:
    """S4-P8's `web` and `rendered_form` fixtures, outside pytest: the sitelink
    checks answer from the scripted web, and every landing page renders with
    its seven-field form (no crawl leaves this stack). The ad previews do NOT
    get this treatment — 4.6.4 renders them in Chromium for real."""
    scripted = _Web(dict(WEB))
    mp.setattr(urlcheck, "new_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(scripted.handler)))

    async def render(urls: Any, *, viewports: Any, timeout_ms: int = landing.NETWORKIDLE_TIMEOUT_MS) -> list[Any]:
        def device(name: landing.Device, url: str) -> landing.DeviceRender:
            return landing.DeviceRender(
                device=name, viewport=viewports[name], user_agent="s4p23-seed", reached=True, final_url=url,
                http_status=200, settled=True, fold_px=viewports[name].height, h1="Keep every SDS current",
                text_nodes=[landing.TextNode(text="Keep every SDS current", box=Box(x=0, y=40, width=300, height=40))],
                controls=[
                    landing.FormControl(form=0, tag="input", name=field, id=None, label=field.replace("_", " "),
                                        type="email" if field == "email" else "text", required=field == "email",
                                        visible=True)
                    for field in FORM
                ],
            )  # fmt: skip

        return [landing.LandingRender(url=url, mobile=device("mobile", url), desktop=device("desktop", url)) for url in urls]

    mp.setattr(landing, "render_pages", render)


async def _identity() -> tuple[uuid.UUID, uuid.UUID]:
    async with get_sessionmaker()() as db:
        admin = (await db.execute(sa.select(User).where(User.email == ADMIN_EMAIL))).scalar_one()
        # The performance owner decides G7; a stop that says "Admin" says nothing.
        admin.name = "Pat Performance"
        workspace_id = (
            (await db.execute(sa.select(Membership.workspace_id).where(Membership.user_id == admin.id)))
            .scalars()
            .first()
        )
        assert workspace_id is not None, "the bootstrap admin has no workspace"
        await db.commit()
        return workspace_id, admin.id


async def _member(admin: Any, role: str, email: str, name: str) -> uuid.UUID:
    created = await admin.post("/users/invite", json={"email": email, "name": name, "role": role})
    assert created.status_code == 201, created.text
    joiner = build_client()
    async with joiner.raw:
        accepted = await joiner.post(
            f"/invites/{created.json()['link'].rsplit('/', 1)[-1]}/accept",
            json={"name": name, "password": ADMIN_PASSWORD},
        )
        assert accepted.status_code == 200, accepted.text
    async with get_sessionmaker()() as db:
        return (await db.execute(sa.select(User.id).where(User.email == email))).scalar_one()


async def _project(workspace_id: uuid.UUID, actor: uuid.UUID) -> uuid.UUID:
    """The integration suite's `project` fixture: the domain the scripted web answers for."""
    async with get_sessionmaker()() as db:
        row = Project(
            workspace_id=workspace_id, created_by=actor, name="SDS Manager", domain="sdsmanager.com",
            product_context={"pitch": "safety data sheet management"},
            markets=[{"country": "US", "language": "en", "currency": "USD"}], settings={},
        )  # fmt: skip
        db.add(row)
        await db.flush()
        existing = await db.scalar(
            sa.select(SourceConnection).where(
                SourceConnection.workspace_id == workspace_id, SourceConnection.kind == CredentialKind.OPENROUTER
            )
        )
        assert existing is not None, "OpenRouter is not connected for the workspace"
        await db.commit()
        return row.id


async def _observe(db: Any, project_id: uuid.UUID, price: float, digest: str) -> None:
    """SDS-PRO observed again at `price`, now — the newest row is the live one."""
    db.add(
        Evidence(
            project_id=project_id, source=EvidenceSource.CSV, kind="offer_record",
            payload={**FRESH[0], "current_price": price, "observed_at": datetime.now(UTC).isoformat()},
            hash=digest,
        )  # fmt: skip
    )
    await db.commit()


async def _package_id(run_id: uuid.UUID, want: CreativePackageStatus) -> uuid.UUID:
    async with get_sessionmaker()() as db:
        row = await package_row(db, run_id)
        assert row is not None, f"run {run_id} wrote no package"
        assert row.status is want, f"run {run_id}'s package is {row.status}, not {want}"
        return row.id


async def _with_media(storage: LocalStorage, run_id: uuid.UUID, concepts: list[tuple[str, int]]) -> None:
    """Give a run's (unreleased) package AI image assets, one per concept, each
    with a 1.91:1 and a 1:1 rendition on the Volume — see the module docstring."""
    now = datetime.now(UTC)
    async with get_sessionmaker()() as db:
        row = await package_row(db, run_id, lock=True)
        assert row is not None
        assert row.status is not CreativePackageStatus.RELEASED
        payload = dict(row.payload)
        media: list[dict[str, Any]] = []
        manifest = list(payload["manifest"])
        for concept, seed in concepts:
            asset = CreativeAsset(
                workspace_id=row.workspace_id, project_id=row.project_id, creative_run_id=run_id, node_id="4.4.3",
                campaign_ref=CAMPAIGN, ad_group_ref=None, kind=CreativeAssetKind.IMAGE, surface="search_image",
                generated_by_ai=True, status=CreativeAssetStatus.APPROVED,
                fields={"concept_id": f"{CAMPAIGN}:{concept}"},
                lineage={"origin": "generated", "node_id": "4.4.3"}, content_hash=f"s4p23-{run_id}-{concept}",
            )  # fmt: skip
            db.add(asset)
            await db.flush()
            renditions: list[dict[str, Any]] = []
            for ratio, width, height in RATIOS:
                content = _picture(width, height, seed * 31 + width + height)
                slug = ratio.replace(":", "x").replace(".", "_")
                key = f"creative/{run_id}/renditions/{asset.id}/{slug}.jpg"
                storage.put(key, content, content_type="image/jpeg")
                sha = hashlib.sha256(content).hexdigest()
                artifact = MediaArtifact(
                    workspace_id=row.workspace_id, asset_id=asset.id, job_id=None, role=MediaArtifactRole.RENDITION,
                    storage_path=key, media_type="image/jpeg", width=width, height=height, bytes=len(content),
                    sha256=sha, aspect_ratio=ratio, derivation=MediaArtifactDerivation.NATIVE, probe={},
                    disclosure={"xmp_digital_source_type": TRAINED, "visible_labels": []},
                )  # fmt: skip
                db.add(artifact)
                await db.flush()
                await store_preview(db, storage, artifact, content)
                path = f"media/{asset.id}/{slug}.jpg"
                renditions.append({
                    "media_id": str(artifact.id), "path": path, "surface": "search_image", "aspect_ratio": ratio,
                    "width": width, "height": height, "bytes": len(content), "media_type": "image/jpeg",
                    "sha256": sha, "derivation": "native", "lint": {"verdict": "pass", "ruleset_version": None},
                    "disclosure": {"xmp_digital_source_type": TRAINED, "visible_labels": []},
                })  # fmt: skip
                manifest.append({"path": path, "sha256": sha, "bytes": len(content), "media_type": "image/jpeg"})
            media.append(
                MediaAsset.model_validate({
                    "asset_id": str(asset.id), "modality": "image", "campaign_ref": CAMPAIGN,
                    "concept_id": f"{CAMPAIGN}:{concept}", "generated_by_ai": True, "renditions": renditions,
                    "provenance": {"model_id": IMAGE_MODEL, "provider": "black-forest-labs",
                                   "cost_usd": str(Decimal("0.0100"))},
                    "review": {"gate": "G8", "round": 1, "decision": "approve", "decided_at": now.isoformat()},
                }).model_dump(mode="json")  # fmt: skip
            )
        for campaign in payload["campaigns"]:
            if campaign["campaign_ref"] == CAMPAIGN:
                campaign["media"] = [*campaign.get("media", []), *media]
        payload["manifest"] = sorted(manifest, key=lambda entry: entry["path"])
        rehashed = hashed(CreativePackage.model_validate(payload))
        row.payload = rehashed.model_dump(mode="json")
        await db.commit()


async def main() -> None:
    storage = LocalStorage(os.environ.get("STORAGE_DIR", "/data"))
    workspace_id, actor = await _identity()
    admin = build_client()
    mp = pytest.MonkeyPatch()
    async with admin.raw:
        logged = await admin.post("/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_BOOT_PASSWORD})
        assert logged.status_code == 200, logged.text
        connected = await admin.post("/connections/openrouter/connect")
        assert connected.status_code == 200, connected.text
        await _member(admin, "approver", "approver@example.com", "Ada Approver")
        await _member(admin, "operator", "ops@example.com", "Oli Ops")
        await _member(admin, "viewer", "viewer@example.com", "Vic Viewer")
        # The in-process pipeline answers through the suite's scripted model,
        # which mocks OpenRouter at its real base URL; the stack's `api` keeps
        # reading the recorded catalogue from the `catalogue` service.
        os.environ["OPENROUTER_BASE_URL"] = BASE
        get_settings.cache_clear()
        _scripted_world(mp)
        project_id = await _project(workspace_id, actor)
        async with get_sessionmaker()() as db:
            run_a = await golden(admin, db, workspace_id, project_id, actor)
            run_c = await run_creative(admin, db, project_id)
            await _observe(db, project_id, 89.0, "offer-SDS-PRO-reobserved")
            run_d = await run_creative(admin, db, project_id)
            await _observe(db, project_id, FRESH[0]["current_price"], "offer-SDS-PRO-restored")
            run_b = await run_creative(admin, db, project_id)
    mp.undo()

    ready = CreativePackageStatus.READY_TO_RELEASE
    seeded = {
        "project_id": str(project_id),
        "run_a": str(run_a),
        "package_a": str(await _package_id(run_a, ready)),
        "run_b": str(run_b),
        "package_b": str(await _package_id(run_b, ready)),
        "run_c": str(run_c),
        "package_c": str(await _package_id(run_c, ready)),
        "run_d": str(run_d),
        "package_d": str(await _package_id(run_d, ready)),
    }
    await _with_media(storage, run_c, [("c1", 1)])
    await _with_media(storage, run_d, [("c1", 2), ("c2", 3)])
    print(json.dumps(seeded))


if __name__ == "__main__":
    asyncio.run(main())
