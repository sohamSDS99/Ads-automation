"""S4-P12's exit criteria, on the committed fixture clips (Stage 04 PRD §21.3):
the clips assemble into 16:9 and 9:16 masters with `brand_first_at_ms ≤ 5000`,
caption OCR ≥ 0.85, `yuv420p` and `+faststart`; a silent-track video still
carries an AAC track. Everything is read back from the written file.

ffmpeg (with libass), tesseract, exiftool and `fonts-inter` ship in the worker
image, the only one that runs post-production.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from agent.postprod import verify, video
from agent.postprod.image import COMPOSITE
from agent.postprod.probe import probe_video
from tests.postprod import video_support as support

pytestmark = pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in ("ffmpeg", "ffprobe", "exiftool", "tesseract"))
    or not Path(video.FONTS_DIR).is_dir(),
    reason="video post-production runs in the worker image",
)

BRAND = {"name": "ink", "hex": "#1d3557", "role": "primary"}


def knobs() -> video.Knobs:
    """The shipped constants (§9.5 `video.*`, `logo.*`) and a 0.25 clear space."""
    return video.Knobs(
        fps=30,
        caption_height_pct=0.055,
        safe_bottom_pct=0.2,
        safe_edge_pct=0.05,
        end_card_ms=2000,
        loudness_lufs=-16,
        ratio_tolerance=0.005,
        logo_width_ratio=0.2,
        permitted_surfaces=("video_frame",),
        clear_space_ratio=0.25,
        min_width_px=None,
    )


@dataclass
class Made:
    prepared: video.Prepared
    assembled: video.Assembled
    path: Path
    stamp: dict[str, object]
    check: verify.Verification


def _make(tmp: Path, clips: tuple[Path, Path], ratio: str, min_px: str) -> Made:
    files = [
        video.ClipFile(path, probe_video(path.read_bytes()), duration)
        for path, duration in zip(clips, (6, 4), strict=True)
    ]
    prepared = video.prepare(
        files,
        ratio=ratio,
        min_px=min_px,
        script=support.script(),
        logos=support.logos(),
        colour=BRAND,
        surface="video_frame",
        knobs=knobs(),
        workdir=tmp / "work",
        model_id="google/veo-3.1-fast",
    )
    out = tmp / "master.mp4"
    assembled = video.assemble(prepared.assembly, out)
    stamp = video.stamp(out, composited=True)
    placement = prepared.logo.placement
    assert placement is not None, prepared.logo.reason
    check = verify.verify(
        out,
        expected=verify.Expected(
            width=prepared.assembly.width,
            height=prepared.assembly.height,
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
        caption_band=prepared.layout.caption_band,
    )
    return Made(prepared, assembled, out, stamp, check)


@pytest.fixture(scope="module")
def landscape(tmp_path_factory: pytest.TempPathFactory) -> Made:
    return _make(tmp_path_factory.mktemp("landscape"), support.LANDSCAPE, "16:9", "1280x720")


@pytest.fixture(scope="module")
def portrait(tmp_path_factory: pytest.TempPathFactory) -> Made:
    return _make(tmp_path_factory.mktemp("portrait"), support.PORTRAIT, "9:16", "720x1280")


@pytest.mark.parametrize("which", ["landscape", "portrait"])
def test_fixture_clips_assemble_into_a_master_that_verifies(
    which: str, request: pytest.FixtureRequest
) -> None:
    made: Made = request.getfixturevalue(which)
    check = made.check
    assert check.passed, check.failures
    assert check.brand_first_at_ms is not None and check.brand_first_at_ms <= 5000
    # The logo is overlaid from 0.5 s: the frame at 0 s must NOT show it.
    assert check.logo_frames[0].detected is False
    assert check.brand_first_at_ms == 1000
    assert check.caption_ocr_min_similarity is not None
    assert check.caption_ocr_min_similarity >= 0.85
    assert len(check.caption_frames) == 2
    facts = check.facts
    assert facts is not None
    assert (facts.codec, facts.profile, facts.pix_fmt) == ("h264", "High", "yuv420p")
    assert facts.fps == 30.0
    assert check.faststart is True
    assert check.boxes.index("moov") < check.boxes.index("mdat")
    assert facts.has_audio and facts.audio_codec == "aac"
    assert facts.duration_ms == 12_000  # 6 + 4 s of clips + the 2 s end card


def test_the_16x9_master_is_1280x720_and_the_9x16_is_720x1280(
    landscape: Made, portrait: Made
) -> None:
    assert (landscape.prepared.assembly.width, landscape.prepared.assembly.height) == (1280, 720)
    assert (portrait.prepared.assembly.width, portrait.prepared.assembly.height) == (720, 1280)


def test_every_clip_is_scaled_uniformly_never_stretched(landscape: Made, portrait: Made) -> None:
    for made in (landscape, portrait):
        for segment in made.prepared.assembly.segments:
            g = segment.geometry
            assert g.scaled[0] * g.pre[3] == g.scaled[1] * g.pre[2]  # sx == sy, exactly
        transform = made.prepared.transform(encoder={}, node_id="4.4.4")
        assert transform["sx"] == transform["sy"] == 2.0


def test_captions_are_drawn_in_inter_semi_bold_not_a_fallback(landscape: Made) -> None:
    assert landscape.assembled.fonts
    assert {got for _, got in landscape.assembled.fonts} == {"Inter-SemiBold"}


def _audio_stats(path: Path, start: float, duration: float) -> tuple[float | None, float]:
    """(integrated loudness in LUFS over the window, max volume in dBFS)."""
    done = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-ss",
            f"{start}",
            "-t",
            f"{duration}",
            "-i",
            str(path),
            "-map",
            "0:a",
            "-af",
            "ebur128=peak=true,volumedetect",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    integrated = re.findall(r"I:\s+(-?[\d.]+|-inf) LUFS", done.stderr)
    peak = re.search(r"max_volume: (-?[\d.]+|-inf) dB", done.stderr)
    assert peak is not None, done.stderr[-2000:]
    loudness = float(integrated[-1]) if integrated and integrated[-1] != "-inf" else None
    return loudness, float(peak.group(1))


def test_clip_audio_is_loudness_normalised_to_minus_16_lufs(landscape: Made) -> None:
    """The 4 s clip carries a quiet tone (-52 LUFS) behind a SILENT 6 s shot —
    the case single-pass loudnorm leaves at -50.9 LUFS. Two-pass lifts it to -16."""
    measured = landscape.assembled.loudness
    assert measured is not None and measured.input_i == pytest.approx(-52.5, abs=1.0)
    loudness, peak = _audio_stats(landscape.path, 6.0, 4.0)
    assert loudness is not None and abs(loudness - (-16)) <= 1.0
    assert peak <= -1.0  # true peak held under -1.5 dBTP (sample peak reads a hair higher)


def test_a_video_whose_clips_have_no_audio_still_carries_a_silent_aac_track(
    portrait: Made,
) -> None:
    facts = portrait.check.facts
    assert facts is not None
    assert facts.has_audio is True and facts.audio_codec == "aac"
    _, peak = _audio_stats(portrait.path, 0.0, 12.0)
    assert peak <= -90.0


def test_the_stamp_is_the_mp4_comment_and_xmp_and_reads_back(landscape: Made) -> None:
    assert landscape.stamp["xmp_digital_source_type"] == COMPOSITE
    back = video.read_stamp(landscape.path)
    assert back["DigitalSourceType"] == COMPOSITE
    assert COMPOSITE in str(back["Comment"])
    assert "google/veo-3.1-fast" in str(back["Comment"])
    assert not [key for key in back if key.startswith("GPS")]
    facts = probe_video(landscape.path.read_bytes())
    assert facts.comment == back["Comment"]
    # The XMP write kept the index before the media.
    assert verify.is_faststart(verify.top_level_boxes(landscape.path.read_bytes()))


def test_the_preview_proxy_is_480p_h264_faststart(landscape: Made, portrait: Made) -> None:
    for made, size in ((landscape, (854, 480)), (portrait, (480, 854))):
        out = made.path.with_name("preview.mp4")
        placed = video.preview_proxy(
            made.path,
            out,
            width=made.prepared.assembly.width,
            height=made.prepared.assembly.height,
        )
        facts = probe_video(out.read_bytes())
        assert (facts.width, facts.height) == size
        assert (facts.codec, facts.pix_fmt, facts.has_audio) == ("h264", "yuv420p", True)
        assert verify.is_faststart(verify.top_level_boxes(out.read_bytes()))
        assert placed.scaled[0] * placed.pre[3] == placed.scaled[1] * placed.pre[2]


def test_the_poster_is_the_frame_the_brand_is_first_seen_in(landscape: Made) -> None:
    ms = landscape.check.brand_first_at_ms
    assert ms is not None
    content = video.poster_jpeg(video.frame_at(landscape.path, ms / 1000))
    assert content[:3] == b"\xff\xd8\xff"


def test_the_end_card_carries_the_brand_colour_logo_and_cta(landscape: Made) -> None:
    card = landscape.prepared.end_card
    assert card["colour_token"] == BRAND
    assert card["background"] == "#1d3557"
    assert card["logo"] is not None and card["cta"] == "Start a free trial"
    assert card["ink_contrast"] >= 4.5
    frame = video.frame_at(landscape.path, 11.0)
    assert frame.getpixel((4, 4)) == pytest.approx((29, 53, 87), abs=6)
    assert "start a free trial" in verify.read_text(frame).casefold()


def test_a_master_without_the_logo_fails_verification_as_blocking(
    landscape: Made, tmp_path: Path
) -> None:
    """The same clips assembled with no registered logo to place: the file is
    fine in every other way, and still not shippable."""
    files = [
        video.ClipFile(path, probe_video(path.read_bytes()), duration)
        for path, duration in zip(support.LANDSCAPE, (6, 4), strict=True)
    ]
    prepared = video.prepare(
        files,
        ratio="16:9",
        min_px="1280x720",
        script=support.script(),
        logos=[],
        colour=BRAND,
        surface="video_frame",
        knobs=knobs(),
        workdir=tmp_path / "w",
        model_id="m",
    )
    out = tmp_path / "nologo.mp4"
    video.assemble(prepared.assembly, out)
    placed = landscape.prepared.logo.placement
    assert placed is not None
    check = verify.verify(
        out,
        expected=verify.Expected(1280, 720, 30, 12_000, 10, None),
        captions=support.script().captions,
        templates=support.templates(),
        logos=support.logos(),
        # Looking exactly where the other master's logo is: nothing is there.
        logo=verify.LogoWindow(box=placed.box, clear_space_px=placed.clear_space_px),
        brand_within_ms=5000,
        min_similarity=0.85,
    )
    assert not check.passed
    assert check.brand_first_at_ms is None
    assert any("no registered logo is recognised" in f for f in check.failures)
    assert not any(frame.detected for frame in check.logo_frames)


def test_a_file_that_is_not_the_planned_one_fails_every_fact_it_breaks(
    landscape: Made, tmp_path: Path
) -> None:
    """A 24 fps yuv444p High 4:4:4 file with no audio and moov at the end."""
    bad = tmp_path / "bad.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(support.LANDSCAPE[0]),
            "-t",
            "3",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv444p",
            "-an",
            str(bad),
        ],
        check=True,
    )
    check = verify.verify(
        bad,
        expected=verify.Expected(1280, 720, 30, 12_000, 10, 15),
        captions=[],
        templates=support.templates(),
        logos=support.logos(),
        logo=None,
        brand_within_ms=5000,
        min_similarity=0.85,
    )
    joined = " | ".join(check.failures)
    for part in (
        "not H.264 High",
        "yuv444p",
        "640x360",
        "24.0",
        "not an AAC track",
        "not the planned 12000 ms",
        "under the spec's 10 s",
        "+faststart",
        "no logo was placed",
    ):
        assert part in joined, joined


def test_the_verification_record_is_json(landscape: Made) -> None:
    record = landscape.check.record()
    assert json.loads(json.dumps(record)) == record
    assert record["brand_first_at_ms"] == 1000
    assert len(record["logo_frames"]) == 6
