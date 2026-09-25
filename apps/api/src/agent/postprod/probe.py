"""The Pillow facts of one image file, and the ffprobe facts of one video file
(Stage 04 PRD §9.4) — `MediaArtifact.probe`.

What a file IS, read from its bytes, never from what the request asked for or
what a provider said it returned: its container format, its pixel size and
mode, its hash, and the metadata it carries. The last group is the point for
post-production — a rendition must leave with no EXIF and no GPS (§9.4 item 5),
and "we stripped it" is only true if a probe of the written file says so.
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess  # noqa: S404 — ffprobe, with a fixed argv
import tempfile
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


# ---------------------------------------------------------------------------
# video — ffprobe (PRD §9.4 video 4)
# ---------------------------------------------------------------------------

#: A probe never waits longer than this on one file.
FFPROBE_TIMEOUT_S = 60
#: Resolved on PATH by the process, as `worker.py` runs its tools.
FFPROBE = "ffprobe"

_VIDEO_TYPES = {"mov": "video/mp4", "mp4": "video/mp4", "webm": "video/webm"}


@dataclass(frozen=True, slots=True)
class VideoFacts:
    """What one video file is, read from its bytes by ffprobe."""

    container: str
    media_type: str
    codec: str
    pix_fmt: str | None
    width: int
    height: int
    duration_ms: int
    fps: float | None
    packets: int
    has_audio: bool
    bytes: int
    sha256: str
    #: The video stream's codec profile as ffprobe names it (`High`).
    profile: str | None = None
    audio_codec: str | None = None
    #: The container's `comment` tag — where a master carries its disclosure.
    comment: str | None = None

    def as_json(self) -> dict[str, Any]:
        return asdict(self)


def probe_video(content: bytes) -> VideoFacts:
    """ffprobe the bytes, counting every packet, so a download cut short fails
    here instead of in an encoder later.

    The bytes go through a temporary file rather than a pipe: an MP4 whose
    index (`moov`) sits at the end cannot be read from a stream it cannot seek.
    """
    if not content:
        raise ProbeError("not a video: the file is empty")
    with tempfile.NamedTemporaryFile(suffix=".bin") as handle:
        handle.write(content)
        handle.flush()
        try:
            completed = subprocess.run(  # noqa: S603 — fixed argv, no shell
                [
                    FFPROBE,
                    "-v",
                    "error",
                    "-count_packets",
                    "-print_format",
                    "json",
                    "-show_format",
                    "-show_streams",
                    handle.name,
                ],
                capture_output=True,
                timeout=FFPROBE_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProbeError(f"ffprobe took longer than {FFPROBE_TIMEOUT_S} s") from exc
        except FileNotFoundError as exc:
            raise ProbeError(
                "ffprobe is not installed; video is probed in the worker image"
            ) from exc
    errors = completed.stderr.decode("utf-8", "replace").strip()
    if completed.returncode != 0 or errors:
        raise ProbeError(f"not a whole video: {errors or f'ffprobe exited {completed.returncode}'}")
    try:
        report = json.loads(completed.stdout)
    except ValueError as exc:
        raise ProbeError(f"ffprobe returned no report: {exc}") from exc
    streams = report.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise ProbeError("not a video: the file has no video stream")
    fmt = report.get("format") or {}
    duration = _seconds(fmt.get("duration")) or _seconds(video.get("duration"))
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    packets = int(video.get("nb_read_packets") or 0)
    if not duration or width <= 0 or height <= 0 or packets <= 0:
        raise ProbeError(
            f"not a whole video: {width}x{height}, {duration or 0} s, {packets} packets"
        )
    container = str(fmt.get("format_name") or "").split(",")[0]
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    tags = {str(k).lower(): v for k, v in (fmt.get("tags") or {}).items()}
    return VideoFacts(
        container=container,
        media_type=_VIDEO_TYPES.get(container, "application/octet-stream"),
        codec=str(video.get("codec_name") or ""),
        pix_fmt=video.get("pix_fmt"),
        width=width,
        height=height,
        duration_ms=round(duration * 1000),
        fps=_rate(video.get("avg_frame_rate")),
        packets=packets,
        has_audio=audio is not None,
        bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        profile=video.get("profile"),
        audio_codec=audio.get("codec_name") if audio is not None else None,
        comment=tags.get("comment"),
    )


def _seconds(value: Any) -> float | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def _rate(value: Any) -> float | None:
    """`24/1` → 24.0; `0/0` (unknown) → None."""
    try:
        numerator, denominator = (int(part) for part in str(value).split("/"))
    except (TypeError, ValueError):
        return None
    return numerator / denominator if denominator else None
