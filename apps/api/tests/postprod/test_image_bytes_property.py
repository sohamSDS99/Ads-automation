"""CC6's third fact, for every input: a rendition's file never exceeds the
spec's `max_bytes` (Stage 04 PRD §17 CC6, §9.4 items 2 and 4).

`test_image_geometry.py` holds `sx == sy` and the ratio over 1,000 inputs, and
the DB CHECK holds `sx == sy` on the row; neither says anything about bytes.
The contract `postprod/image.py` states is: the highest JPEG quality in
[`jpeg_quality_floor`, 95] whose file — stamp included — fits `max_bytes`,
else **None, which 4.4.3 records as a gap**. Never an over-size file, and never
a gap the floor could have filled. So both halves are asserted, on the same
path 4.4.3's `_postprocess` takes: `target_size` → `geometry` → `render` →
encode.

Two properties, because the stamp is an `exiftool` process per call:

* the byte-cap search (`encode_jpeg`, with the stamp's reserve as `overhead`)
  over 1,000 derandomized crop/scale/budget inputs — pure Pillow, fast,
  runs everywhere;
* the whole `encode_and_stamp` — the STAMPED file against the cap — over 100,
  in the worker image, where exiftool is (two exiftool runs per stamp).

Images are small (≤ 96 px a side before scaling) so a thousand of them take
seconds; the budget is drawn relative to the frame's own ceiling-quality size,
so every branch (fits at 95, searched down, refused at the floor) is reached.
"""

from __future__ import annotations

import io
import shutil
from fractions import Fraction

import numpy as np
import pytest
from hypothesis import event, given, settings
from hypothesis import strategies as st
from PIL import Image

from agent.media.capability import parse_ratio
from agent.postprod.image import (
    JPEG_QUALITY_CEILING,
    encode_and_stamp,
    encode_jpeg,
    flatten,
    geometry,
    render,
    stamp,
    target_size,
)
from agent.postprod.probe import probe_image

TOLERANCE = 0.005
#: About what the stamp adds to a file; only where budgets are drawn, never asserted.
_XMP_BYTES = 2930

_RATIOS = st.one_of(
    st.sampled_from(["1:1", "1.91:1", "4:5", "16:9", "9:16", "4:1", "3:2"]),
    st.tuples(st.integers(1, 12), st.integers(1, 12)).map(lambda wh: f"{wh[0]}:{wh[1]}"),
)


def _source(width: int, height: int, seed: int, grain: int) -> Image.Image:
    """A gradient under `grain` of noise: 0 compresses to almost nothing, 255
    barely compresses — the file size a budget is judged against varies."""
    rng = np.random.default_rng(seed)
    ramp = np.linspace(0, 255, width, dtype=np.float64)[None, :, None]
    base = np.broadcast_to(ramp, (height, width, 3))
    noise = rng.integers(-grain, grain + 1, (height, width, 3)) if grain else 0
    return Image.fromarray(np.clip(base + noise, 0, 255).astype(np.uint8), mode="RGB")


def _jpeg(frame: Image.Image, quality: int) -> bytes:
    """Exactly what `encode_jpeg` writes at one quality."""
    buffer = io.BytesIO()
    flatten(frame).save(buffer, format="JPEG", quality=quality, optimize=True)
    return buffer.getvalue()


_GEOMETRY = {
    "src_w": st.integers(8, 96),
    "src_h": st.integers(8, 96),
    "ratio": _RATIOS,
    "min_w": st.integers(1, 96),
    "min_h": st.integers(1, 96),
    "fx": st.fractions(0, 1),
    "fy": st.fractions(0, 1),
}


@settings(max_examples=1000, deadline=None, derandomize=True)
@given(
    **_GEOMETRY,
    seed=st.integers(0, 2**32 - 1),
    grain=st.integers(0, 255),
    floor=st.integers(1, JPEG_QUALITY_CEILING),
    overhead=st.integers(0, 4096),
    share=st.fractions(0, 2, max_denominator=1000),
)
def test_the_encoded_file_never_exceeds_max_bytes_for_every_input(
    src_w: int,
    src_h: int,
    ratio: str,
    min_w: int,
    min_h: int,
    fx: Fraction,
    fy: Fraction,
    seed: int,
    grain: int,
    floor: int,
    overhead: int,
    share: Fraction,
) -> None:
    out_w, out_h = target_size(src_w, src_h, ratio, f"{min_w}x{min_h}", tolerance=TOLERANCE)
    placed = geometry(src_w, src_h, out_w, out_h, origin=(fx * src_w, fy * src_h))
    assert placed.sx == placed.sy == placed.scale
    record = placed.transform()
    assert record["sx"] == record["sy"] == float(placed.scale)
    assert abs(Fraction(out_w, out_h) / Fraction(parse_ratio(ratio)) - 1) <= TOLERANCE
    frame = render(_source(src_w, src_h, seed, grain), placed)
    assert frame.size == (out_w, out_h)

    ceiling = len(_jpeg(frame, JPEG_QUALITY_CEILING)) + overhead
    max_bytes = max(1, int(ceiling * share))
    encoded = encode_jpeg(frame, max_bytes=max_bytes, quality_floor=floor, overhead=overhead)

    if encoded is None:
        # A gap is allowed only when the floor itself cannot fit.
        event("gap: the floor does not fit")
        assert len(_jpeg(frame, floor)) + overhead > max_bytes
        return
    event("fits at 95" if encoded.quality == JPEG_QUALITY_CEILING else "searched below 95")
    assert len(encoded.content) + overhead <= max_bytes  # never an over-size file
    assert floor <= encoded.quality <= JPEG_QUALITY_CEILING  # never under the floor
    assert encoded.content == _jpeg(frame, encoded.quality)
    with Image.open(io.BytesIO(encoded.content)) as decoded:
        assert decoded.format == "JPEG"
        assert decoded.size == (out_w, out_h)  # the bytes are the planned rendition


@pytest.mark.skipif(shutil.which("exiftool") is None, reason="the stamp runs in the worker image")
@settings(max_examples=100, deadline=None, derandomize=True)
@given(
    **_GEOMETRY,
    seed=st.integers(0, 2**32 - 1),
    grain=st.integers(0, 255),
    floor=st.integers(60, JPEG_QUALITY_CEILING),
    composited=st.booleans(),
    share=st.fractions(Fraction(-1, 4), Fraction(5, 4), max_denominator=1000),
)
def test_the_stamped_file_never_exceeds_max_bytes_for_every_input(
    src_w: int,
    src_h: int,
    ratio: str,
    min_w: int,
    min_h: int,
    fx: Fraction,
    fy: Fraction,
    seed: int,
    grain: int,
    floor: int,
    composited: bool,
    share: Fraction,
) -> None:
    out_w, out_h = target_size(src_w, src_h, ratio, f"{min_w}x{min_h}", tolerance=TOLERANCE)
    placed = geometry(src_w, src_h, out_w, out_h, origin=(fx * src_w, fy * src_h))
    assert placed.sx == placed.sy == placed.scale
    frame = render(_source(src_w, src_h, seed, grain), placed)

    # ~2.9 KB of XMP rides on every stamped file (measured: 2,926 B trained,
    # 2,939 B composite). Budgets land between the stamped floor and the
    # stamped ceiling (0 ≤ share ≤ 1, the quality search) and either side of it.
    low, high = len(_jpeg(frame, floor)), len(_jpeg(frame, JPEG_QUALITY_CEILING))
    max_bytes = max(1, _XMP_BYTES + low + int((high - low) * share))
    done = encode_and_stamp(frame, max_bytes=max_bytes, quality_floor=floor, composited=composited)

    if done is None:
        # The gap was necessary: the floor's file, once stamped, is over the cap.
        event("gap: the stamped floor does not fit")
        floor_stamped, _ = stamp(_jpeg(frame, floor), composited=composited)
        assert len(floor_stamped) > max_bytes
        return
    content, encoder_args, disclosure = done
    event("fits at 95" if encoder_args["quality"] == JPEG_QUALITY_CEILING else "searched below 95")
    assert len(content) <= max_bytes  # the STAMPED file, not the bare encode
    assert floor <= encoder_args["quality"] <= JPEG_QUALITY_CEILING
    assert disclosure["xmp_digital_source_type"].endswith(
        "compositeWithTrainedAlgorithmicMedia" if composited else "/trainedAlgorithmicMedia"
    )
    facts = probe_image(content)
    assert (facts.width, facts.height) == (out_w, out_h)
    assert facts.xmp and not facts.exif and not facts.gps
