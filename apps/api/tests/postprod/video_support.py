"""Shared fixtures for the video post-production tests (S4-P12).

The clips are COMMITTED (`tests/fixtures/video/`), made once with the worker
image's ffmpeg 4.4.2 so every run assembles the same bytes:

    enc="-c:v libx264 -preset veryslow -crf 30 -pix_fmt yuv420p -movflags +faststart
         -map_metadata -1 -fflags +bitexact -flags:v +bitexact"
    landscape_6s_silent.mp4  mandelbrot=size=640x360:rate=24,noise=alls=10:allf=t  -t 6
    landscape_4s_tone.mp4    cellauto=size=640x360:rate=24:rule=110,…,noise=…      -t 4
                             + sine=frequency=220:sample_rate=48000,volume=-30dB (AAC):
                               -48 dBFS peak, -52 LUFS
    portrait_6s_silent.mp4   mandelbrot=size=360x640:rate=24:start_x=…,noise=…     -t 6
    portrait_4s_silent.mp4   gradients=size=360x640:rate=24:speed=0.03:…,noise=…   -t 4

Grain on every clip: flat synthetic footage is degenerate for the image
metrics (S4-P10 trap 3). The logo is a wordmark with corners and counters, as
a real one has — a plain rectangle gives ORB four keypoints to match.

The templates are registered exactly as Stage 03's 3.4.3 registers them:
`template_from_bytes` at `ocr_working_width_px` (1280) with
`logo_match_score_min` (0.62).
"""

from __future__ import annotations

import io
import uuid
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from agent.imaging.precheck import LogoTemplateData, template_from_bytes
from agent.postprod.image import LogoArt, load_logo
from agent.schemas.creative_video import ScriptBeat, ScriptCaption, VideoScript

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "video"
LANDSCAPE = (FIXTURES / "landscape_6s_silent.mp4", FIXTURES / "landscape_4s_tone.mp4")
PORTRAIT = (FIXTURES / "portrait_6s_silent.mp4", FIXTURES / "portrait_4s_silent.mp4")

#: Stage 03's `image_policy.ocr_working_width_px` and `logo_match_score_min`.
TEMPLATE_WORKING_WIDTH = 1280
TEMPLATE_MIN_SCORE = 0.62

DARK_ID = uuid.UUID("00000000-0000-4000-8000-00000000da4c")
LIGHT_ID = uuid.UUID("00000000-0000-4000-8000-0000000011e7")


def logo_bytes(plate: tuple[int, int, int], ink: tuple[int, int, int]) -> bytes:
    """A wordmark on its own opaque plate, as many registered logos are. (A
    logo on a TRANSPARENT field is flattened onto black by Stage 03's
    `template_from_bytes`, so a dark-ink one registers as a near-empty
    template no frame can match — docs/stage-04-questions.md § S4-P12.)"""
    image = Image.new("RGBA", (600, 200), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, 599, 199), radius=28, fill=plate + (255,))
    draw.rounded_rectangle((24, 24, 176, 176), radius=30, outline=ink + (255,), width=16)
    draw.polygon([(62, 146), (100, 54), (138, 146)], fill=ink + (255,))
    font = ImageFont.truetype("/usr/share/fonts/opentype/inter/Inter-Bold.otf", 112)
    draw.text((200, 30), "SDSM", font=font, fill=ink + (255,))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


INK, ORANGE, PAPER, EMERALD = (12, 12, 14), (194, 65, 12), (250, 249, 247), (4, 120, 87)
VARIANTS = ((INK, ORANGE, DARK_ID, "logo dark"), (PAPER, EMERALD, LIGHT_ID, "logo light"))


def logos() -> list[LogoArt]:
    """The registered variants: a black plate and a paper one."""
    return [
        load_logo(logo_bytes(plate, ink), asset_id=asset_id, label=label)
        for plate, ink, asset_id, label in VARIANTS
    ]


def templates() -> tuple[LogoTemplateData, ...]:
    return tuple(
        template_from_bytes(
            logo_bytes(plate, ink),
            asset_id=asset_id,
            label=label,
            min_score=TEMPLATE_MIN_SCORE,
            working_width=TEMPLATE_WORKING_WIDTH,
        )
        for plate, ink, asset_id, label in VARIANTS
    )


def script() -> VideoScript:
    """10 s in two shots (6 + 4), voiceover on both, CTA on screen at the end."""
    return VideoScript(
        duration_s=10,
        beats=[
            ScriptBeat(
                t0=0,
                t1=6,
                visual="A lab bench at dawn",
                voiceover="Every safety sheet in one place",
                on_screen_text="Safety sheets, sorted",
            ),
            ScriptBeat(
                t0=6,
                t1=10,
                visual="A tablet on the bench",
                voiceover="Find any chemical in seconds",
                on_screen_text="Start a free trial",
            ),
        ],
        captions=[
            ScriptCaption(t0=0, t1=6, text="Every safety sheet in one place"),
            ScriptCaption(t0=6, t1=10, text="Find any chemical in seconds"),
        ],
        cta="Start a free trial",
    )
