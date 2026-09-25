"""The pure parts of video post-production (Stage 04 PRD §9.4 video 3–5): the
geometry that never stretches, the generated `.ass`, the ffmpeg arguments and
the one retry, the caption similarity and the MP4 box walk. No ffmpeg here."""

from __future__ import annotations

import struct
import uuid
from fractions import Fraction
from pathlib import Path

import pytest

from agent.postprod import verify, video
from agent.postprod.image import StretchError
from agent.postprod.probe import VideoFacts
from agent.schemas.creative_video import ScriptBeat, ScriptCaption, VideoScript

# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("src", "ratio", "min_px", "size"),
    [
        ((1280, 720), "16:9", None, (1280, 720)),
        ((640, 360), "16:9", "1280x720", (1280, 720)),
        ((1280, 720), "9:16", None, (404, 720)),  # a crop at the source's own resolution
        ((1280, 720), "9:16", "1080x1920", (1080, 1920)),
        ((360, 640), "9:16", "720x1280", (720, 1280)),
        ((1279, 719), "16:9", None, (1276, 718)),  # the even width nearest the ratio
        ((1024, 1024), "1:1", None, (1024, 1024)),
    ],
)
def test_the_frame_is_even_sided_and_the_spec_ratio(
    src: tuple[int, int], ratio: str, min_px: str | None, size: tuple[int, int]
) -> None:
    width, height = video.even_size(*src, ratio, min_px, tolerance=0.005)
    assert (width, height) == size
    assert width % 2 == 0 and height % 2 == 0


@pytest.mark.parametrize(
    ("src", "out"),
    [
        ((640, 360), (1280, 720)),
        ((1280, 720), (1920, 1080)),
        ((1280, 720), (404, 720)),
        ((1280, 720), (720, 1280)),
        ((1279, 719), (1278, 718)),
        ((1281, 721), (1920, 1080)),
        ((1280, 720), (854, 480)),
        ((720, 1280), (480, 854)),
        ((997, 541), (1280, 720)),
    ],
)
def test_the_scale_is_one_exact_factor_on_both_axes(
    src: tuple[int, int], out: tuple[int, int]
) -> None:
    placed = video.uniform_scale(*src, *out)
    x, y, pre_w, pre_h = placed.pre
    assert Fraction(placed.scaled[0], pre_w) == Fraction(placed.scaled[1], pre_h) == placed.scale
    assert x >= 0 and y >= 0 and x + pre_w <= src[0] and y + pre_h <= src[1]
    assert placed.out == out
    assert placed.offset[0] + out[0] <= placed.scaled[0]
    assert placed.offset[1] + out[1] <= placed.scaled[1]
    transform = placed.transform()
    assert transform["sx"] == transform["sy"]


def test_a_near_miss_ratio_wastes_only_a_sliver_of_the_source() -> None:
    """1279x719 has gcd 1: without the pre-trim the only exact factor would
    throw away a quarter of the frame."""
    placed = video.uniform_scale(1279, 719, 1278, 718)
    assert placed.scale == 1
    assert placed.pre[2] >= 1278 and placed.pre[3] >= 718


def test_a_stretched_geometry_cannot_be_built() -> None:
    with pytest.raises(StretchError):
        video.UniformScale(
            src=(1280, 720),
            pre=(0, 0, 1280, 720),
            scaled=(1920, 1082),
            offset=(0, 0),
            out=(1920, 1080),
            scale=Fraction(3, 2),
        )


def test_the_filters_crop_scale_crop_and_reset_the_sample_aspect() -> None:
    placed = video.uniform_scale(1280, 720, 1920, 1080)
    assert placed.filters(flags="lanczos") == (
        "crop=1280:720:0:0,scale=1920:1080:flags=lanczos,crop=1920:1080:0:0,setsar=1"
    )


@pytest.mark.parametrize(
    ("size", "proxy"),
    [
        ((1280, 720), (854, 480)),
        ((720, 1280), (480, 854)),
        ((1080, 1080), (480, 480)),
        ((404, 718), (404, 718)),
    ],
)
def test_the_proxy_is_480p(size: tuple[int, int], proxy: tuple[int, int]) -> None:
    assert video.proxy_size(*size) == proxy


# ---------------------------------------------------------------------------
# the .ass
# ---------------------------------------------------------------------------


def _script() -> VideoScript:
    return VideoScript(
        duration_s=10,
        beats=[
            ScriptBeat(
                t0=0,
                t1=6,
                visual="v",
                voiceover="Save {time} today",
                on_screen_text="Safety sheets",
            ),
            ScriptBeat(
                t0=6, t1=10, visual="v", voiceover="Find it fast", on_screen_text="Start now"
            ),
        ],
        captions=[
            ScriptCaption(t0=0, t1=6, text="Save {time} today"),
            ScriptCaption(t0=6, t1=10, text="Find it fast"),
        ],
        cta="Start now",
    )


def _layout(**overrides: object) -> video.TextLayout:
    base = video.text_layout(
        1280, 720, caption_height_pct=0.055, safe_bottom_pct=0.2, safe_edge_pct=0.05, logo=None
    )
    return base if not overrides else video.TextLayout(**{**base.__dict__, **overrides})  # type: ignore[arg-type]


def test_the_caption_style_is_inter_semi_bold_on_a_60_percent_box_above_the_safe_zone() -> None:
    document = video.ass_document(_script(), _layout())
    style = next(line for line in document.splitlines() if line.startswith("Style: Caption,"))
    fields = style.removeprefix("Style: ").split(",")
    assert fields[1] == "Inter Semi Bold"
    assert fields[2] == str(round(0.055 * 720))  # 40 px line height
    assert fields[5] == fields[6] == "&H66000000"  # black, 0x66/255 transparent = 60% opaque
    assert fields[15] == "3"  # BorderStyle 3: an opaque box
    assert fields[18] == "2"  # bottom centre
    assert fields[21] == str(round(0.2 * 720))  # MarginV = the safe-zone bottom
    assert "PlayResX: 1280" in document and "PlayResY: 720" in document


def test_captions_and_on_screen_text_are_timed_by_the_script() -> None:
    document = video.ass_document(_script(), _layout())
    events = [line for line in document.splitlines() if line.startswith("Dialogue:")]
    assert events == [
        "Dialogue: 0,0:00:00.00,0:00:06.00,Caption,,0,0,0,,Save \\{time\\} today",
        "Dialogue: 0,0:00:06.00,0:00:10.00,Caption,,0,0,0,,Find it fast",
        "Dialogue: 1,0:00:00.00,0:00:06.00,OnScreen,,0,0,0,,Safety sheets",
        "Dialogue: 1,0:00:06.00,0:00:10.00,OnScreen,,0,0,0,,Start now",
    ]


def test_on_screen_text_keeps_clear_of_the_logo_corner() -> None:
    from PIL import Image

    from agent.postprod.image import LogoArt, LogoPlacement

    art = LogoArt(uuid.uuid4(), "logo", Image.new("RGBA", (10, 10)), 0.1)
    placed = LogoPlacement(art, "top_right", (1002, 22, 1258, 108), 22, 0.5, 4.0)
    layout = video.text_layout(
        1280, 720, caption_height_pct=0.055, safe_bottom_pct=0.2, safe_edge_pct=0.05, logo=placed
    )
    assert layout.top_side_px == 256 + 2 * 22 + 36
    style = next(
        line
        for line in video.ass_document(_script(), layout).splitlines()
        if line.startswith("Style: OnScreen,")
    )
    fields = style.split(",")
    assert fields[19] == fields[20] == str(layout.top_side_px)


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(0, "0:00:00.00"), (6, "0:00:06.00"), (61.237, "0:01:01.24"), (3600.5, "1:00:00.50")],
)
def test_ass_time(seconds: float, text: str) -> None:
    assert video.ass_time(seconds) == text


def test_text_is_drawn_literally() -> None:
    assert video.ass_text("A {\\b1}bold\nclaim") == "A \\{\\b1\\}bold claim"


def test_libass_fontselect_lines_are_read() -> None:
    stderr = (
        "[Parsed_ass_0 @ 0x1] fontselect: (Inter Semi Bold, 400, 0) -> Inter-SemiBold, 0, "
        "Inter-SemiBold\n[Parsed_ass_0 @ 0x1] fontselect: (Inter SemiBold, 400, 0) -> "
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf, 0, DejaVuSans\n"
    )
    assert video.fonts_selected(stderr) == [
        ("Inter Semi Bold", "Inter-SemiBold"),
        ("Inter SemiBold", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]


# ---------------------------------------------------------------------------
# the brand colour
# ---------------------------------------------------------------------------


def test_the_primary_token_is_the_brand_colour() -> None:
    tokens = [
        {"name": "sand", "hex": "#f1e9da", "role": "neutral"},
        {"name": "ink", "hex": "#1d3557", "role": "Primary"},
    ]
    assert video.brand_colour(tokens) == {"name": "ink", "hex": "#1d3557", "role": "Primary"}


def test_without_a_primary_the_first_readable_token_is_used() -> None:
    tokens = [{"name": "bad", "hex": "blue"}, {"name": "sand", "hex": "#fed"}]
    assert video.brand_colour(tokens) == {"name": "sand", "hex": "#fed", "role": ""}
    assert video.parse_hex("#fed") == (255, 238, 221)
    assert video.brand_colour([]) is None


# ---------------------------------------------------------------------------
# the ffmpeg run and its one retry
# ---------------------------------------------------------------------------


def _facts(width: int = 640, height: int = 360, *, audio: bool = False) -> VideoFacts:
    return VideoFacts(
        container="mov",
        media_type="video/mp4",
        codec="h264",
        pix_fmt="yuv420p",
        width=width,
        height=height,
        duration_ms=6000,
        fps=24.0,
        packets=144,
        has_audio=audio,
        bytes=1,
        sha256="0",
    )


def _plan(*, audio: bool = False, logo: bool = True) -> video.Assembly:
    geometry = video.uniform_scale(640, 360, 1280, 720)
    return video.Assembly(
        segments=(
            video.Segment(Path("/w/a.mp4"), _facts(), 6, geometry),
            video.Segment(Path("/w/b.mp4"), _facts(audio=audio), 4, geometry),
        ),
        width=1280,
        height=720,
        fps=30,
        end_card_png=Path("/w/end_card.png"),
        end_card_s=2,
        ass_path=Path("/w/captions.ass"),
        logo_png=Path("/w/logo.png") if logo else None,
        logo_xy=(1002, 22) if logo else None,
        loudness_lufs=-16,
        comment="AI-generated video (m)",
    )


def _value(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


def test_the_encode_is_the_spec() -> None:
    args = video.ffmpeg_args(_plan(), Path("/w/out.mp4"), "standard")
    assert _value(args, "-c:v") == "libx264"
    assert _value(args, "-profile:v") == "high"
    assert _value(args, "-pix_fmt") == "yuv420p"
    assert _value(args, "-crf") == "18"
    assert _value(args, "-movflags") == "+faststart"
    assert _value(args, "-r") == "30"
    assert _value(args, "-c:a") == "aac"
    assert args[len(args) - 1 - args[::-1].index("-t") + 1] == "12"  # the output's -t
    assert _value(args, "-map_metadata") == "-1"
    assert _value(args, "-metadata") == "comment=AI-generated video (m)"
    graph = _value(args, "-filter_complex")
    assert graph.count("setsar=1") == 3  # both clips and the end card
    assert "overlay=x=1002:y=22:enable='between(t,0.5,4.5)'" in graph
    assert "ass=filename=/w/captions.ass:fontsdir=/usr/share/fonts/opentype/inter" in graph
    assert "tpad=stop_mode=clone:stop_duration=6,trim=duration=6" in graph


def test_with_no_clip_audio_the_track_is_silent_aac_for_the_whole_file() -> None:
    graph = _value(video.ffmpeg_args(_plan(), Path("/o.mp4"), "standard"), "-filter_complex")
    assert "anullsrc=r=48000:cl=stereo,atrim=duration=12[aout]" in graph
    assert "loudnorm" not in graph


def test_clip_audio_is_measured_then_normalised_linearly() -> None:
    plan = _plan(audio=True)
    measuring = _value(video.loudness_args(plan, "standard"), "-filter_complex")
    assert "loudnorm=I=-16:TP=-1.5:print_format=json[aout]" in measuring
    measured = video.Loudness(-52.5, -48.9, 0.0, -62.6, 0.1)
    graph = _value(video.ffmpeg_args(plan, Path("/o.mp4"), "standard", measured), "-filter_complex")
    assert (
        "loudnorm=I=-16:TP=-1.5:measured_I=-52.5:measured_TP=-48.9:measured_LRA=0:"
        "measured_thresh=-62.6:offset=0.1:linear=true"
    ) in graph
    assert "[0:a]" not in graph  # clip a has no audio: silence stands in for it
    assert "[1:a]aresample=48000" in graph


def test_the_measuring_pass_is_parsed_from_loudnorms_json() -> None:
    stderr = (
        '[Parsed_loudnorm_7 @ 0x1] \n{\n\t"input_i" : "-52.13",\n\t"input_tp" : "-48.61",'
        '\n\t"input_lra" : "0.00",\n\t"input_thresh" : "-62.13",\n\t"output_i" : '
        '"-16.02",\n\t"target_offset" : "0.02"\n}\n'
    )
    assert video.parse_loudness(stderr) == video.Loudness(-52.13, -48.61, 0.0, -62.13, 0.02)
    silent = stderr.replace('"-52.13"', '"-inf"')
    assert video.parse_loudness(silent).silent is True


def test_conservative_args_change_only_threads_preset_scaler_and_queue() -> None:
    standard = video.ffmpeg_args(_plan(), Path("/o.mp4"), "standard")
    conservative = video.ffmpeg_args(_plan(), Path("/o.mp4"), "conservative")
    for flag in ("-c:v", "-profile:v", "-pix_fmt", "-crf", "-r", "-movflags", "-c:a", "-t"):
        assert _value(standard, flag) == _value(conservative, flag)
    assert (_value(standard, "-preset"), _value(conservative, "-preset")) == ("medium", "veryfast")
    assert _value(conservative, "-threads") == "1"
    assert "flags=bicubic" in _value(conservative, "-filter_complex")
    assert _value(conservative, "-max_muxing_queue_size") == "4096"


class _Runner:
    """ffmpeg that fails the first `fail` runs with a stderr of its own."""

    def __init__(self, fail: int, *, font: str = "Inter-SemiBold") -> None:
        self.fail = fail
        self.calls: list[list[str]] = []
        self.font = font

    def __call__(self, args: list[str], *, what: str) -> str:
        self.calls.append(args)
        if len(self.calls) <= self.fail:
            raise video.FfmpegFailed(1, f"run {len(self.calls)}: Invalid argument\n" * 3, what)
        return f"fontselect: (Inter Semi Bold, 400, 0) -> {self.font}, 0, x\n"


def test_a_failed_run_keeps_its_stderr_tail_and_retries_once_conservatively() -> None:
    runner = _Runner(fail=1)
    done = video.assemble(_plan(), Path("/o.mp4"), runner=runner)
    assert done.profile == "conservative"
    assert [a.args for a in done.failures] == ["standard"]
    assert done.failures[0].exit_code == 1
    assert "run 1: Invalid argument" in done.failures[0].stderr_tail
    assert _value(runner.calls[1], "-preset") == "veryfast"
    assert len(runner.calls) == 2


def test_two_failed_runs_are_a_gap_with_both_tails_and_no_third_run() -> None:
    runner = _Runner(fail=5)
    with pytest.raises(video.AssemblyFailed) as raised:
        video.assemble(_plan(), Path("/o.mp4"), runner=runner)
    assert [a.args for a in raised.value.attempts] == ["standard", "conservative"]
    assert "run 2" in raised.value.attempts[1].stderr_tail
    assert len(runner.calls) == 2


def test_the_measuring_pass_failing_counts_as_the_attempt() -> None:
    runner = _Runner(fail=1)
    plan = _plan(audio=True)

    def measured_ok(args: list[str], *, what: str) -> str:
        if what.startswith("loudness") and len(runner.calls) >= 1:
            runner.calls.append(args)
            return '{"input_i":"-20","input_tp":"-5","input_lra":"1","input_thresh":"-30","target_offset":"0"}'
        return runner(args, what=what)

    done = video.assemble(plan, Path("/o.mp4"), runner=measured_ok)
    assert [a.args for a in done.failures] == ["standard"]
    assert done.loudness == video.Loudness(-20.0, -5.0, 1.0, -30.0, 0.0)


def test_a_fallback_caption_face_fails_the_assembly() -> None:
    runner = _Runner(fail=0, font="/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    with pytest.raises(video.AssemblyError, match="fallback face"):
        video.assemble(_plan(), Path("/o.mp4"), runner=runner)


def test_the_stderr_tail_is_bounded() -> None:
    assert len(video.tail("x" * 100_000)) == video.STDERR_TAIL_CHARS


# ---------------------------------------------------------------------------
# verify: similarity and the container
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expected", "read", "passes"),
    [
        ("Every safety sheet in one place", "Every safety sheet in one place", True),
        (
            "Every safety sheet in one place",
            "La d . Every safety «©, e ot sheet in one place |.",
            True,
        ),
        ("Find any chemical in seconds", "Findany-chemicalin‘seconds noise noise", True),
        ("Every safety sheet in one place", "Every safety sheet", False),
        ("Every safety sheet in one place", "", False),
        ("Every safety sheet in one place", "Evry sfty shet in one plce", True),  # 0.91
        ("Every safety sheet in one place", "Evry sfty shet", False),
    ],
)
def test_caption_similarity(expected: str, read: str, passes: bool) -> None:
    assert (verify.similarity(expected, read) >= 0.85) is passes


def _box(kind: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def test_faststart_is_moov_before_mdat() -> None:
    fast = (
        _box(b"ftyp", b"isom") + _box(b"moov", b"x" * 20) + _box(b"uuid") + _box(b"mdat", b"y" * 9)
    )
    slow = _box(b"ftyp", b"isom") + _box(b"mdat", b"y" * 9) + _box(b"moov", b"x" * 20)
    assert verify.top_level_boxes(fast) == ["ftyp", "moov", "uuid", "mdat"]
    assert verify.is_faststart(verify.top_level_boxes(fast)) is True
    assert verify.is_faststart(verify.top_level_boxes(slow)) is False
    large = struct.pack(">I4sQ", 1, b"mdat", 16 + 4) + b"zzzz"
    assert verify.top_level_boxes(_box(b"ftyp") + _box(b"moov") + large) == ["ftyp", "moov", "mdat"]
    assert verify.top_level_boxes(b"\x00\x00\x00\x04moov") == []
