"""`postprod/probe.py` — the Pillow facts of one file (Stage 04 PRD §9.4)."""

from __future__ import annotations

import hashlib
import io

import pytest
from PIL import Image

from agent.postprod.probe import ProbeError, probe_image


def _jpeg_with_gps() -> bytes:
    exif = Image.Exif()
    exif[0x010F] = "Studio camera"  # Make
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    gps[2] = (51.0, 30.0, 0.0)
    buffer = io.BytesIO()
    Image.new("RGB", (120, 80), (40, 90, 160)).save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()


def test_a_jpeg_reports_its_shape_hash_and_the_exif_and_gps_it_carries() -> None:
    content = _jpeg_with_gps()
    facts = probe_image(content)
    assert (facts.format, facts.media_type) == ("JPEG", "image/jpeg")
    assert (facts.width, facts.height, facts.mode) == (120, 80, "RGB")
    assert facts.bytes == len(content)
    assert facts.sha256 == hashlib.sha256(content).hexdigest()
    assert facts.exif and facts.gps
    assert not facts.has_alpha and not facts.xmp


def test_a_png_with_transparency_has_alpha_and_no_exif() -> None:
    buffer = io.BytesIO()
    Image.new("RGBA", (32, 16), (0, 0, 0, 0)).save(buffer, format="PNG")
    facts = probe_image(buffer.getvalue())
    assert (facts.format, facts.media_type, facts.has_alpha) == ("PNG", "image/png", True)
    assert not facts.exif and not facts.gps


def test_the_json_form_is_what_media_artifact_probe_stores() -> None:
    facts = probe_image(_jpeg_with_gps())
    assert facts.as_json() == {
        "format": "JPEG",
        "media_type": "image/jpeg",
        "width": 120,
        "height": 80,
        "mode": "RGB",
        "bytes": facts.bytes,
        "sha256": facts.sha256,
        "has_alpha": False,
        "exif": True,
        "gps": True,
        "xmp": False,
        "icc_profile": False,
    }


@pytest.mark.parametrize("content", [b"", b"not an image", b"\x89PNG\r\n\x1a\n\x00\x00"])
def test_bytes_that_are_not_an_image_are_refused(content: bytes) -> None:
    with pytest.raises(ProbeError):
        probe_image(content)
