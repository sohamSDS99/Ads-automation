"""The Pillow facts of one image file (Stage 04 PRD §9.4) — `MediaArtifact.probe`.

What a file IS, read from its bytes, never from what the request asked for or
what a provider said it returned: its container format, its pixel size and
mode, its hash, and the metadata it carries. The last group is the point for
post-production — a rendition must leave with no EXIF and no GPS (§9.4 item 5),
and "we stripped it" is only true if a probe of the written file says so.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import asdict, dataclass
from typing import Any

from PIL import Image, UnidentifiedImageError

#: The EXIF pointer to the GPS IFD (EXIF 2.3, tag 0x8825).
GPS_IFD = 0x8825

_MEDIA_TYPES = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


class ProbeError(ValueError):
    """The bytes are not an image Pillow can decode."""


@dataclass(frozen=True, slots=True)
class ImageFacts:
    format: str
    media_type: str
    width: int
    height: int
    mode: str
    bytes: int
    sha256: str
    has_alpha: bool
    exif: bool
    gps: bool
    xmp: bool
    icc_profile: bool

    def as_json(self) -> dict[str, Any]:
        return asdict(self)


def probe_image(content: bytes) -> ImageFacts:
    """Decode fully — a truncated file fails here, not in an encoder later."""
    try:
        with Image.open(io.BytesIO(content)) as image:
            image.load()
            exif = image.getexif()
            fmt = (image.format or "").upper()
            return ImageFacts(
                format=fmt,
                media_type=_MEDIA_TYPES.get(fmt, "application/octet-stream"),
                width=image.width,
                height=image.height,
                mode=image.mode,
                bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                has_alpha="A" in image.getbands() or "transparency" in image.info,
                exif=len(exif) > 0,
                gps=len(exif.get_ifd(GPS_IFD)) > 0,
                xmp=bool(image.info.get("xmp") or image.info.get("XML:com.adobe.xmp")),
                icc_profile=bool(image.info.get("icc_profile")),
            )
    except (OSError, SyntaxError, ValueError, UnidentifiedImageError,
            Image.DecompressionBombError) as exc:  # fmt: skip
        raise ProbeError(f"not a decodable image: {exc}") from exc
