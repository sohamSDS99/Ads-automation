"""The Media Library's preview proxies for still images (PRD §15.5 item 2).

The grid never loads a master or a rendition: every tile is a small WebP of the
file it stands for, stored beside it as a `MediaArtifact(role=preview)` whose
`derived_from` is that file. `GET /media/{id}/content?variant=preview` finds it
by that link. Videos already get theirs — the 480p proxy — from 4.4.4.
"""

from __future__ import annotations

import asyncio
import hashlib

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import MediaArtifact, MediaArtifactDerivation, MediaArtifactRole
from agent.postprod.image import preview_webp
from agent.storage.backend import StorageBackend


def preview_key(source_key: str) -> str:
    """`…/renditions/{asset}/1x1.jpg` → `…/renditions/{asset}/1x1_preview.webp`."""
    folder, _, name = source_key.rpartition("/")
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return f"{folder}/{stem}_preview.webp" if folder else f"{stem}_preview.webp"


async def store_preview(
    db: AsyncSession, storage: StorageBackend, source: MediaArtifact, content: bytes
) -> MediaArtifact:
    """The preview of `source` (whose bytes are `content`), written once: a
    node retried after writing it finds the row it wrote."""
    existing = await db.scalar(
        sa.select(MediaArtifact).where(
            MediaArtifact.derived_from == source.id,
            MediaArtifact.role == MediaArtifactRole.PREVIEW,
        )
    )
    if existing is not None:
        return existing
    made = await asyncio.to_thread(preview_webp, content)
    key = preview_key(source.storage_path)
    await asyncio.to_thread(storage.put, key, made.content, content_type="image/webp")
    row = MediaArtifact(
        workspace_id=source.workspace_id,
        asset_id=source.asset_id,
        job_id=source.job_id,
        role=MediaArtifactRole.PREVIEW,
        storage_path=key,
        media_type="image/webp",
        width=made.width,
        height=made.height,
        bytes=len(made.content),
        sha256=hashlib.sha256(made.content).hexdigest(),
        aspect_ratio=source.aspect_ratio,
        derivation=MediaArtifactDerivation.ENCODED,
        derived_from=source.id,
        transform=made.transform,
        probe={"format": "WEBP", "width": made.width, "height": made.height},
        # A proxy for the screen, never shipped: the file it stands for carries
        # the disclosure stamp (§13), and the drawer reads it from there.
        disclosure=None,
    )
    db.add(row)
    await db.flush()
    return row
