"""CC7's blocking facts, each on a REAL file that breaks only that fact
(Stage 04 PRD §17 CC7, §9.4 video 4).

`test_video_assemble.py` proves a good master passes and that one file
breaking seven facts at once fails all seven. What it leaves open is whether
each fact gates on its own — a file that is right in every other way:

* **brand after 5 s** — the committed clips assembled through the product's
  own filtergraph with only the logo's schedule moved (`LOGO_WINDOW_S`): shown
  from 5.5 s it fails verification, and the file demonstrably carries the brand
  later; shown from 4.5 s it is first seen at exactly 5000 ms and passes
  (`brand_first_at_ms ≤ 5000` is inclusive);
* **not `yuv420p`** — the good master re-encoded as `yuvj420p`, still H.264
  High (every other non-4:2:0 format also changes the profile, which would
  hide the pixel-format check behind the profile one);
* **not `+faststart`** — the good master remuxed with `moov` after `mdat`.

And the question the audit raised about §11 check 10: the package's
`VideoFacts` carry no pixel format and no faststart flag, so check 10 cannot
see either. They cannot reach the package: a `VideoRendition` exists only
after 4.4.4's `_post_produce` returned a `_Made`, which it does only when
verification passed — anything else is a `_Refused`, which 4.4.4 records as a
`VideoGap`. The last test drives `_post_produce` itself with the master
degraded just before verification reads it, and shows it refused. (Pixel
format is also re-measured at 4.6.1 as the `codec` constraint
`h264/high/yuv420p`, which check 9 blocks on.)

The degradation is applied after the stamp, not after assembly, because
exiftool's in-place XMP write rewrites the MP4 with `moov` first: a master
assembled without `+faststart` would come out of the stamp with it (measured:
`ftyp free mdat moov` in, `ftyp free moov uuid mdat` out). So faststart is
held twice before it is verified; the file verification refuses is one that
reached it anyway.

ffmpeg, ffprobe, exiftool, tesseract and `fonts-inter` exist in the worker
image only.
"""

from __future__ import annotations

import dataclasses
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from agent.nodes.creative import n4_4_4_video_production as n444
from agent.postprod import verify, video
from agent.postprod.probe import probe_video
from agent.schemas.creative_video import PlannedClip
from tests.postprod import video_support as support
from tests.postprod.test_video_assemble import BRAND, Made, _make, knobs

pytestmark = pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in ("ffmpeg", "ffprobe", "exiftool", "tesseract"))
    or not Path(video.FONTS_DIR).is_dir(),
    reason="video post-production runs in the worker image",
)

PIX_FMT_FAILURE = "pixel format is yuvj420p, not yuv420p"
FASTSTART_FAILURE = "moov is not before mdat: the file was not written with +faststart"
LATE_FAILURE = "no registered logo is recognised in any 1 fps sample over 0–5 s"


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True, timeout=300)  # noqa: S603, S607


def as_yuvj420p(source: Path, out: Path) -> None:
    """The same frames, full-range 4:2:0 — H.264 High, 30 fps, audio copied."""
    _ffmpeg(
        "-i", str(source), "-map", "0", "-c:v", "libx264", "-profile:v", "high",
        "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuvj420p", "-r", "30",
        "-c:a", "copy", "-movflags", "+faststart", str(out),
    )  # fmt: skip


def without_faststart(source: Path, out: Path) -> None:
    """The same streams, remuxed without `+faststart`: `moov` after `mdat`."""
    _ffmpeg("-i", str(source), "-map", "0", "-c", "copy", str(out))


def _verify(path: Path, made: Made) -> verify.Verification:
    """`path` against the plan `made` was assembled and verified from."""
    placement = made.prepared.logo.placement
    assert placement is not None
    return verify.verify(
        path,
        expected=verify.Expected(
            width=made.prepared.assembly.width,
            height=made.prepared.assembly.height,
            fps=30,
            duration_ms=12_000,
            min_duration_s=10,
            max_duration_s=None,
        ),
        captions=support.script().captions,
        templates=support.templates(),
        logos=support.logos(),
        logo=verify.LogoWindow(box=placement.box, clear_space_px=placement.clear_space_px),
        brand_within_ms=5000,
        min_similarity=0.85,
        caption_band=made.prepared.layout.caption_band,
    )


@pytest.fixture(scope="module")
def master(tmp_path_factory: pytest.TempPathFactory) -> Made:
    made = _make(tmp_path_factory.mktemp("cc7"), support.LANDSCAPE, "16:9", "1280x720")
    assert made.check.passed, made.check.failures
    return made


# -- brand_first_at_ms ≤ 5000 ------------------------------------------------------


def test_cc7_a_brand_first_shown_after_5_s_fails_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(video, "LOGO_WINDOW_S", (5.5, 9.5))
    made = _make(tmp_path, support.LANDSCAPE, "16:9", "1280x720")
    check = made.check

    assert not check.passed
    assert check.failures == [LATE_FAILURE]  # the timing, and nothing else
    assert check.brand_first_at_ms is None
    assert [frame.t_ms for frame in check.logo_frames] == [0, 1000, 2000, 3000, 4000, 5000]
    assert not any(frame.detected for frame in check.logo_frames)
    # The brand IS on the file — later. Measured the way verification measures.
    placement = made.prepared.logo.placement
    assert placement is not None
    window = verify.LogoWindow(box=placement.box, clear_space_px=placement.clear_space_px)
    shown = verify.shown_templates(support.logos(), support.templates(), window)
    late = verify.logo_frame(video.frame_at(made.path, 7.0), shown, 7000, window)
    assert late.detected, late


def test_cc7_a_brand_first_shown_at_exactly_5_s_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same pipeline one second earlier: the 5 s sample is the first to
    see it, and 5000 ms is inside the window — which is also what shows the
    5.5 s case failed on timing alone."""
    monkeypatch.setattr(video, "LOGO_WINDOW_S", (4.5, 8.5))
    made = _make(tmp_path, support.LANDSCAPE, "16:9", "1280x720")
    assert made.check.passed, made.check.failures
    assert made.check.brand_first_at_ms == 5000
    assert [frame.detected for frame in made.check.logo_frames] == [False] * 5 + [True]


# -- yuv420p and +faststart, each on its own ---------------------------------------


def test_cc7_a_file_in_another_pixel_format_fails_verification_on_that_alone(
    master: Made, tmp_path: Path
) -> None:
    out = tmp_path / "yuvj420p.mp4"
    as_yuvj420p(master.path, out)
    facts = probe_video(out.read_bytes())
    assert (facts.codec, facts.profile, facts.pix_fmt) == ("h264", "High", "yuvj420p")

    check = _verify(out, master)
    assert not check.passed
    assert check.failures == [PIX_FMT_FAILURE]
    assert check.faststart is True


def test_cc7_a_file_without_faststart_fails_verification_on_that_alone(
    master: Made, tmp_path: Path
) -> None:
    out = tmp_path / "moov-last.mp4"
    without_faststart(master.path, out)
    boxes = verify.top_level_boxes(out.read_bytes())
    assert boxes.index("mdat") < boxes.index("moov")

    check = _verify(out, master)
    assert not check.passed
    assert check.failures == [FASTSTART_FAILURE]
    facts = check.facts
    assert facts is not None and facts.pix_fmt == "yuv420p"


# -- §11 check 10: neither can reach the package ----------------------------------


def _degrading(how: Callable[[Path, Path], None]) -> Callable[..., dict[str, Any]]:
    """The real stamp, then the master rewritten by `how` — the last write
    before verification reads the file. The proxy is stamped as it is."""
    real = video.stamp

    def stamp(path: Path, *, composited: bool) -> dict[str, Any]:
        stamped = real(path, composited=composited)
        if path.name == "master.mp4":
            rewritten = path.with_name("degraded.mp4")
            how(path, rewritten)
            rewritten.replace(path)
        return stamped

    return stamp


@pytest.mark.parametrize(
    ("how", "failure"),
    [(None, None), (as_yuvj420p, PIX_FMT_FAILURE), (without_faststart, FASTSTART_FAILURE)],
    ids=["as-assembled", "yuvj420p", "moov-last"],
)
def test_cc7_a_video_that_fails_verification_never_becomes_a_rendition(
    how: Callable[[Path, Path], None] | None,
    failure: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if how is not None:
        monkeypatch.setattr(video, "stamp", _degrading(how))
    timings = ((0, 6, 6), (6, 10, 4))
    clips = [
        (PlannedClip(index=i, t0=t0, t1=t1, duration_s=d), path.read_bytes())
        for i, (path, (t0, t1, d)) in enumerate(zip(support.PORTRAIT, timings, strict=True))
    ]
    result = n444._post_produce(
        clips,
        ratio="9:16",
        min_px="720x1280",
        script=support.script(),
        logos=support.logos(),
        templates=support.templates(),
        colour=BRAND,
        # The node places its logo on the `video` surface.
        knobs=dataclasses.replace(knobs(), permitted_surfaces=(n444.LOGO_SURFACE,)),
        expected=verify.Expected(720, 1280, 30, 12_000, 10, None),
        model_id="google/veo-3.1-fast",
        brand_within_ms=5000,
        min_similarity=0.85,
    )
    if failure is None:
        # The control: the same clips, undegraded, are a rendition.
        assert isinstance(result, n444._Made), result
        assert result.verification.passed
        return
    assert isinstance(result, n444._Refused), "a file that failed verification was kept"
    assert result.reason == "verification_failed"
    assert failure in result.detail
    assert result.verification is not None
    assert result.verification.passed is False
    # "On its own" is the two tests above; here what matters is that the file
    # was refused. (Re-encoded full-range, the portrait's logo also scores
    # under its floor — one more reason, not a different outcome.)
    assert failure in result.verification.failures
