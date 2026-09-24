"""Media references — the one place a reference image is judged, stored and sent.

Stage 04 PRD §9.1 item 5, §10.3, §13 and Law 44. A `MediaReference` is a product
or style image someone uploaded and attested the rights to. Three things are
decided here and nowhere else:

* **Whether a reference may leave the building** (`refusal`). Law 44: only when
  `project.settings.media_references_allowed` is true, the uploader attested
  rights, `origin != third_party` — or an H3 `image_right` for it has been
  cleared in this run — and the chosen model accepts image input. Retired
  references never go, and every file is capped at `media.reference_max_bytes`.
* **`product_depiction`** (`product_depiction`), which follows from the above:
  `reference_guided` when a registered product reference may go,
  `composited_real` when one is registered but may not, `none` when there is
  none (§18, "No references, or references not allowed"). Code decides it; a
  model is never asked (Law 38).
* **What a request carries** (`select_references`): the product reference(s) of
  a `reference_guided` concept, then style references, every one of them
  re-judged, capped at what the model takes.

Bytes travel only as base64 data URLs built by `media/images.py`; everywhere
else a reference is its sha256 (§9.1 item 7). Nothing here logs one.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import uuid
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import sqlalchemy as sa
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import (
    CreativeException,
    CreativeExceptionKind,
    CreativeExceptionStatus,
    MediaReference,
)
from agent.media.capability import accepts_master
from agent.media.types import CapabilityRecord, ReferenceImage
from agent.schemas.creative_input import MediaReferenceRef
from agent.storage.backend import StorageBackend, StorageError

__all__ = ["ReferenceImage"]  # re-exported: what `load_for_request` returns

Depiction = Literal["reference_guided", "composited_real", "none"]
ReferenceKind = Literal["product_reference", "style_reference"]
ReferenceOrigin = Literal["own", "licensed", "third_party"]

#: Why a reference may not be sent — one Law 44 clause each.
Refusal = Literal[
    "retired",
    "no_rights_attestation",
    "references_not_allowed",
    "model_takes_no_image_input",
    "third_party_without_image_right",
    "over_size_cap",
]

#: Why the loader could not produce bytes it may send.
LoadFailure = Literal["unknown_reference", "bytes_missing", "bytes_changed", "over_size_cap"]

#: `Project.settings` key (PRD §7.1). Absent means false (Q3's default).
REFERENCES_ALLOWED = "media_references_allowed"

#: `CreativeException.proposed.flag` of an `image_right` that is about a
#: reference image — as opposed to one raised on a VISION flag (§13, 4.6.2).
#: `proposed.reference_id` then names the `MediaReference` it clears.
IMAGE_RIGHT_FLAG = "third_party_reference"

#: Pillow's format name → (media type, file extension). PRD §10.3: PNG, JPEG, WebP.
FORMATS: dict[str, tuple[str, str]] = {
    "PNG": ("image/png", "png"),
    "JPEG": ("image/jpeg", "jpg"),
    "WEBP": ("image/webp", "webp"),
}
EXTENSIONS: dict[str, str] = {media_type: ext for media_type, ext in FORMATS.values()}


# ---------------------------------------------------------------------------
# Law 44
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReferenceFacts:
    """What Law 44 needs to know about one reference, and nothing it may not.

    `bytes` is None when the caller holds only the `CreativeInput` snapshot,
    which records no size; the cap is then enforced where the bytes are read.
    """

    reference_id: uuid.UUID
    kind: ReferenceKind
    origin: ReferenceOrigin
    rights_statement: str
    bytes: int | None
    retired: bool

    @classmethod
    def of_row(cls, row: MediaReference) -> ReferenceFacts:
        return cls(
            reference_id=row.id,
            kind=row.kind.value,
            origin=row.origin.value,
            rights_statement=row.rights_statement,
            bytes=row.bytes,
            retired=row.retired_at is not None,
        )

    @classmethod
    def of_snapshot(cls, ref: MediaReferenceRef) -> ReferenceFacts:
        """From the run's `CreativeInput`, which holds only non-retired rows
        and no sizes (§4.3)."""
        return cls(
            reference_id=ref.reference_id,
            kind=ref.kind,
            origin=ref.origin,
            rights_statement=ref.rights_statement,
            bytes=None,
            retired=False,
        )


def references_allowed(project_settings: Mapping[str, Any] | None) -> bool:
    """`project.settings.media_references_allowed`, false unless set true (§13)."""
    return (project_settings or {}).get(REFERENCES_ALLOWED) is True


def accepts_image_input(capability: CapabilityRecord | None) -> bool:
    """Would the gateway accept a reference image for this model?

    Exactly the rule `capability.validate()` applies to `input_references`: an
    `image` input modality and a range descriptor admitting at least one. An
    image model that lists `image` among its input modalities but offers no
    `input_references` parameter would otherwise resolve `reference_guided` for
    a request the gateway then refuses with a 422.
    """
    return capability is not None and capability.modality == "image" and accepts_master(capability)


def refusal(
    ref: ReferenceFacts,
    *,
    allowed: bool,
    capability: CapabilityRecord | None,
    cleared: Collection[uuid.UUID],
    max_bytes: int,
) -> Refusal | None:
    """None when `ref` may be sent to the provider; otherwise the clause it fails.

    `cleared` is the ids of references with a cleared H3 `image_right` in THIS
    run — clearances are package-scoped (§8.6), so another run's do not count.
    """
    if ref.retired:
        return "retired"
    if not ref.rights_statement.strip():
        return "no_rights_attestation"
    if not allowed:
        return "references_not_allowed"
    if not accepts_image_input(capability):
        return "model_takes_no_image_input"
    if ref.origin == "third_party" and ref.reference_id not in cleared:
        return "third_party_without_image_right"
    if ref.bytes is not None and ref.bytes > max_bytes:
        return "over_size_cap"
    return None


def product_depiction(
    refs: Iterable[ReferenceFacts],
    *,
    images_in_scope: bool,
    allowed: bool,
    capability: CapabilityRecord | None,
    cleared: Collection[uuid.UUID],
    max_bytes: int,
) -> Depiction:
    """`reference_guided` | `composited_real` | `none`, resolved in code (§11 4.4.1).

    A product photo is *registered* when it is a non-retired, rights-attested
    `product_reference`. Registered and sendable ⇒ `reference_guided`;
    registered but not sendable ⇒ `composited_real` (the model paints the scene
    and `postprod/` composites the real photo locally — nothing leaves);
    nothing registered ⇒ `none`.
    """
    registered = [
        ref
        for ref in refs
        if ref.kind == "product_reference" and not ref.retired and ref.rights_statement.strip()
    ]
    if not images_in_scope or not registered:
        return "none"
    for ref in registered:
        if (
            refusal(
                ref, allowed=allowed, capability=capability, cleared=cleared, max_bytes=max_bytes
            )
            is None
        ):
            return "reference_guided"
    return "composited_real"


def select_references(
    refs: Sequence[ReferenceFacts],
    depiction: Depiction,
    *,
    allowed: bool,
    capability: CapabilityRecord | None,
    cleared: Collection[uuid.UUID],
    max_bytes: int,
) -> list[ReferenceFacts]:
    """The references one image request may carry, in the order they go.

    Product references only for a `reference_guided` concept — a
    `composited_real` or `none` concept must not show the model the product —
    then style references. Every one is re-judged by `refusal`, so a third-party
    reference never rides along because another reference made the concept
    `reference_guided`. Capped at the model's `input_references` maximum.
    """
    sendable = [
        ref
        for ref in refs
        if refusal(
            ref, allowed=allowed, capability=capability, cleared=cleared, max_bytes=max_bytes
        )
        is None
    ]
    products = (
        [ref for ref in sendable if ref.kind == "product_reference"]
        if depiction == "reference_guided"
        else []
    )
    styles = [ref for ref in sendable if ref.kind == "style_reference"]
    return [*products, *styles][: reference_limit(capability)]


def reference_limit(capability: CapabilityRecord | None) -> int:
    """How many references the model takes in one request (0 if none)."""
    if not accepts_image_input(capability):
        return 0
    assert capability is not None  # noqa: S101 — accepts_image_input checked it
    descriptor = capability.params["input_references"]
    return max(0, descriptor.max or 0)


# ---------------------------------------------------------------------------
# H3 image_right clearances — package-scoped, so per run
# ---------------------------------------------------------------------------


def image_right_proposal(reference_id: uuid.UUID, *, basis: str) -> dict[str, Any]:
    """`CreativeException.proposed` for an `image_right` about one reference.

    `{basis, flag}` is the PRD's shape (§7.2); `reference_id` is what lets a
    clearance name the reference it clears — the table has no column for it.
    4.6.2 writes this; `cleared_image_rights` reads it. One constructor, so the
    two cannot drift.
    """
    return {"basis": basis, "flag": IMAGE_RIGHT_FLAG, "reference_id": str(reference_id)}


def image_right_reference(exception: CreativeException) -> uuid.UUID | None:
    """The reference an `image_right` exception is about, or None."""
    proposed = exception.proposed or {}
    if proposed.get("flag") != IMAGE_RIGHT_FLAG:
        return None
    try:
        return uuid.UUID(str(proposed.get("reference_id")))
    except ValueError:
        return None


async def cleared_image_rights(db: AsyncSession, run_id: uuid.UUID) -> frozenset[uuid.UUID]:
    """References with a cleared H3 `image_right` in this run.

    Both halves, because each alone is forgeable by a bug (the same reasoning
    as the G7 spend gate): `status='cleared'` with no decider is a clearance
    nobody gave. Another run's clearance does not count — they are
    package-scoped (§8.6).
    """
    rows = (
        (
            await db.execute(
                sa.select(CreativeException).where(
                    CreativeException.creative_run_id == run_id,
                    CreativeException.kind == CreativeExceptionKind.IMAGE_RIGHT,
                    CreativeException.status == CreativeExceptionStatus.CLEARED,
                    CreativeException.decided_by.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    return frozenset(ref for row in rows if (ref := image_right_reference(row)) is not None)


# ---------------------------------------------------------------------------
# the point of sending
# ---------------------------------------------------------------------------


class ReferenceRefused(RuntimeError):
    """A reference a caller asked to send, and why it may not go.

    Raised, never skipped: a caller only asks for references `select_references`
    chose, so a refusal here means something changed underneath it (the project
    stopped allowing references, the reference was retired, its bytes changed) —
    and a request quietly sent without the reference the concept was resolved
    around would be a different request from the one recorded.
    """

    def __init__(
        self,
        reference_id: uuid.UUID | None,
        reason: Refusal | LoadFailure,
        *,
        sha256: str | None = None,
    ) -> None:
        named = f"reference {reference_id}" if reference_id else f"reference sha256 {sha256}"
        super().__init__(f"{named} may not be sent: {reason}")
        self.reference_id = reference_id
        self.sha256 = sha256
        self.reason = reason


async def load_for_request(
    db: AsyncSession,
    storage: StorageBackend,
    *,
    run_id: uuid.UUID,
    project_id: uuid.UUID,
    allowed: bool,
    capability: CapabilityRecord | None,
    reference_ids: Sequence[uuid.UUID],
    max_bytes: int,
) -> list[ReferenceImage]:
    """The bytes of `reference_ids`, judged again against live rows (Law 44).

    Everything is re-read at the moment of sending: the row (retired? which
    project?), this run's clearances, and the bytes — which must still hash to
    the sha256 the uploader attested, and fit `max_bytes`. Raises
    `ReferenceRefused` naming the first reference that may not go.
    """
    if not reference_ids:
        return []
    if len(reference_ids) > reference_limit(capability):
        raise ReferenceRefused(reference_ids[0], "model_takes_no_image_input")
    rows = {
        row.id: row
        for row in (
            await db.execute(
                sa.select(MediaReference).where(
                    MediaReference.id.in_(list(reference_ids)),
                    MediaReference.project_id == project_id,
                )
            )
        )
        .scalars()
        .all()
    }
    cleared = await cleared_image_rights(db, run_id)
    images: list[ReferenceImage] = []
    for reference_id in reference_ids:
        row = rows.get(reference_id)
        if row is None:
            raise ReferenceRefused(reference_id, "unknown_reference")
        reason = refusal(
            ReferenceFacts.of_row(row),
            allowed=allowed,
            capability=capability,
            cleared=cleared,
            max_bytes=max_bytes,
        )
        if reason is not None:
            raise ReferenceRefused(reference_id, reason)
        try:
            data = await asyncio.to_thread(storage.get, row.storage_path)
        except (StorageError, OSError) as exc:
            raise ReferenceRefused(reference_id, "bytes_missing") from exc
        if len(data) > max_bytes:
            raise ReferenceRefused(reference_id, "over_size_cap")
        if hashlib.sha256(data).hexdigest() != row.sha256:
            raise ReferenceRefused(reference_id, "bytes_changed")
        images.append(ReferenceImage(sha256=row.sha256, media_type=row.media_type, data=data))
    return images


async def reload_by_sha256(
    db: AsyncSession,
    storage: StorageBackend,
    *,
    run_id: uuid.UUID,
    project_id: uuid.UUID,
    allowed: bool,
    capability: CapabilityRecord | None,
    sha256s: Sequence[str],
    max_bytes: int,
) -> list[ReferenceImage]:
    """`load_for_request` for a stored request, which names references only by
    sha256 (§9.1 item 7) — what "Check again" re-sends (§18)."""
    rows = {
        row.sha256: row.id
        for row in (
            await db.execute(
                sa.select(MediaReference).where(
                    MediaReference.project_id == project_id,
                    MediaReference.sha256.in_(list(sha256s)),
                )
            )
        )
        .scalars()
        .all()
    }
    for sha in sha256s:
        if sha not in rows:
            raise ReferenceRefused(None, "unknown_reference", sha256=sha)
    return await load_for_request(
        db,
        storage,
        run_id=run_id,
        project_id=project_id,
        allowed=allowed,
        capability=capability,
        reference_ids=[rows[sha] for sha in sha256s],
        max_bytes=max_bytes,
    )


# ---------------------------------------------------------------------------
# uploads
# ---------------------------------------------------------------------------


class ReferenceRejected(ValueError):
    """An upload `POST /projects/{id}/media-references` will not store."""

    def __init__(
        self, code: Literal["empty", "too_large", "unsupported_format", "undecodable"], message: str
    ) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class InspectedUpload:
    content: bytes
    media_type: str
    extension: str
    width: int
    height: int
    size: int
    sha256: str


def inspect_upload(content: bytes, *, max_bytes: int) -> InspectedUpload:
    """Decode the bytes and say what they are. Raises `ReferenceRejected`.

    The format is read from the bytes, never from the client's content type: a
    GIF renamed `.png` is still a GIF. The image is decoded in full here, so a
    truncated file is refused at upload rather than failing a paid generation.
    """
    if not content:
        raise ReferenceRejected("empty", "That file is empty.")
    if len(content) > max_bytes:
        raise ReferenceRejected(
            "too_large",
            f"That file is {len(content) // 1024} KB; a reference may be at most "
            f"{max_bytes // 1024} KB (media.reference_max_bytes).",
        )
    try:
        with Image.open(io.BytesIO(content)) as image:
            fmt = (image.format or "").upper()
            if fmt not in FORMATS:
                raise ReferenceRejected(
                    "unsupported_format",
                    f"That file is {fmt or 'not an image format we recognise'}; a reference "
                    "must be PNG, JPEG or WebP.",
                )
            image.load()
            width, height = image.size
    except ReferenceRejected:
        raise
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError) as exc:
        raise ReferenceRejected(
            "undecodable", "That file could not be read as an image; upload it again."
        ) from exc
    media_type, extension = FORMATS[fmt]
    return InspectedUpload(
        content=content,
        media_type=media_type,
        extension=extension,
        width=width,
        height=height,
        size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


def storage_key(project_id: uuid.UUID, sha256: str, media_type: str) -> str:
    """`references/{project_id}/{sha256}.{ext}` (PRD §7.4) — content-addressed."""
    return f"references/{project_id}/{sha256}.{EXTENSIONS[media_type]}"
