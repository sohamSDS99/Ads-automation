"""The ffprobe facts of one video file (Stage 04 PRD §9.4 video 4) — what a
downloaded clip IS, read from its bytes, never from what the request asked for.

Runs against the clip OpenRouter actually returned (`video_content_0.mp4`,
recorded in S4-P1). ffprobe ships in the worker image, the only one that runs
post-production.
"""

from __future__ import annotations

import hashlib
import shutil

import pytest

from agent.postprod.probe import ProbeError, probe_video
from tests.media.openrouter_mock import video_bytes

pytestmark = pytest.mark.skipif(
    shutil.which("ffprobe") is None, reason="ffprobe ships in the worker image"
)


def test_the_recorded_clip_reads_back_as_what_it_is() -> None:
    content = video_bytes()
    facts = probe_video(content)
    assert (facts.width, facts.height) == (1280, 720)
    assert facts.duration_ms == 4000
    assert facts.codec == "h264"
    assert facts.pix_fmt == "yuv420p"
    assert facts.fps == 24.0
    assert facts.has_audio is False
    assert facts.media_type == "video/mp4"
    assert facts.bytes == len(content)
    assert facts.sha256 == hashlib.sha256(content).hexdigest()


def test_the_facts_are_json_for_the_artifact_row() -> None:
    probe = probe_video(video_bytes()).as_json()
    assert probe["codec"] == "h264"
    assert probe["duration_ms"] == 4000


@pytest.mark.parametrize(
    "content",
    [b"", b"not a video at all", video_bytes()[:4096]],
    ids=["empty", "garbage", "truncated"],
)
def test_bytes_that_are_not_a_whole_video_are_a_probe_error(content: bytes) -> None:
    with pytest.raises(ProbeError):
        probe_video(content)
