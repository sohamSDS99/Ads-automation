"""Encode to fit, then stamp and prove it (Stage 04 PRD §9.4 items 4–5, §13).

JPEG quality is binary-searched down to `jpeg_quality_floor` (80) to fit
`max_bytes` — else a gap. The stamp strips EXIF/GPS and writes XMP
`Iptc4xmpExt:DigitalSourceType` with exiftool, then READS IT BACK: a stamp
that is not on the written file is not a stamp. Runs in the worker image —
exiftool is installed there and nowhere else.
"""

from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image

from agent.postprod import image as postprod
from agent.postprod.image import (
    COMPOSITE,
    TRAINED,
    StampError,
    encode_and_stamp,
    encode_jpeg,
    read_stamp,
    stamp,
)
from agent.postprod.probe import probe_image


def _noisy(size: tuple[int, int] = (1200, 628)) -> Image.Image:
    pixels = np.random.default_rng(7).integers(0, 256, (size[1], size[0], 3), dtype=np.uint8)
    return Image.fromarray(pixels, mode="RGB")


def _jpeg_size(frame: Image.Image, quality: int) -> int:
    buffer = io.BytesIO()
    frame.save(buffer, format="JPEG", quality=quality, optimize=True)
    return len(buffer.getvalue())


def _jpeg_with_gps() -> bytes:
    exif = Image.Exif()
    exif[0x010F] = "Studio camera"
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    gps[2] = (51.0, 30.0, 0.0)
    buffer = io.BytesIO()
    Image.new("RGB", (64, 48), (200, 40, 40)).save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()


# -- encoding -----------------------------------------------------------------


def test_the_highest_quality_that_fits_is_chosen() -> None:
    frame = _noisy()
    at_80, at_95 = _jpeg_size(frame, 80), _jpeg_size(frame, 95)
    budget = (at_80 + at_95) // 2
    encoded = encode_jpeg(frame, max_bytes=budget, quality_floor=80)
    assert encoded is not None
    assert 80 < encoded.quality < 95
    assert len(encoded.content) <= budget
    assert _jpeg_size(frame, encoded.quality + 1) > budget  # the next step up does not fit
    assert encoded.encoder_args == {"format": "JPEG", "quality": encoded.quality, "optimize": True}


def test_below_the_floor_nothing_is_encoded_it_is_a_gap() -> None:
    frame = _noisy()
    assert encode_jpeg(frame, max_bytes=_jpeg_size(frame, 80) - 1, quality_floor=80) is None


def test_with_no_byte_cap_the_ceiling_quality_is_used() -> None:
    encoded = encode_jpeg(_noisy((64, 64)), max_bytes=None, quality_floor=80)
    assert encoded is not None and encoded.quality == 95


def test_the_byte_budget_leaves_room_for_the_stamp() -> None:
    frame = _noisy()
    at_80 = _jpeg_size(frame, 80)
    # Exactly the floor's size fits bare, but not once 2 KiB of XMP rides along.
    assert encode_jpeg(frame, max_bytes=at_80, quality_floor=80) is not None
    assert encode_jpeg(frame, max_bytes=at_80, quality_floor=80, overhead=2048) is None


# -- the stamp ------------------------------------------------------------------


def test_the_stamp_strips_exif_and_gps_and_reads_back() -> None:
    stamped, disclosure = stamp(_jpeg_with_gps(), composited=False)
    facts = probe_image(stamped)
    assert not facts.exif and not facts.gps
    assert facts.xmp
    assert read_stamp(stamped)["DigitalSourceType"] == TRAINED
    assert TRAINED == "http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia"
    assert disclosure == {"xmp_digital_source_type": TRAINED}


def test_a_composited_logo_changes_the_source_type() -> None:
    stamped, disclosure = stamp(_jpeg_with_gps(), composited=True)
    assert read_stamp(stamped)["DigitalSourceType"] == COMPOSITE
    assert COMPOSITE.endswith("/compositeWithTrainedAlgorithmicMedia")
    assert disclosure == {"xmp_digital_source_type": COMPOSITE}


def test_a_stamp_that_does_not_read_back_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(postprod, "read_stamp", lambda content: {"DigitalSourceType": "nope"})
    with pytest.raises(StampError, match="read back"):
        stamp(_jpeg_with_gps(), composited=False)


def test_bytes_exiftool_cannot_write_are_refused() -> None:
    with pytest.raises(StampError):
        stamp(b"not a jpeg", composited=False)


def test_encode_and_stamp_fits_the_stamped_file_under_the_cap() -> None:
    frame = _noisy()
    budget = _jpeg_size(frame, 90)
    done = encode_and_stamp(frame, max_bytes=budget, quality_floor=80, composited=True)
    assert done is not None
    content, encoder_args, disclosure = done
    assert len(content) <= budget  # the STAMPED file, not the bare encode
    assert encoder_args["quality"] < 90
    assert read_stamp(content)["DigitalSourceType"] == COMPOSITE
    assert disclosure == {"xmp_digital_source_type": COMPOSITE}


def test_encode_and_stamp_gaps_when_the_floor_cannot_fit() -> None:
    frame = _noisy()
    done = encode_and_stamp(
        frame, max_bytes=_jpeg_size(frame, 80) // 2, quality_floor=80, composited=False
    )
    assert done is None
