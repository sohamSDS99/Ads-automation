"""`VideoScript` — "works with sound off" as a validator (PRD §9.4 video 1).

Every `voiceover` interval must be fully covered by captions, and the CTA must
appear as on-screen text. The beats tile the video, so every second of it has
a picture; captions never overlap, because two burned-in captions at once is
one unreadable caption.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from agent.schemas.creative_video import VideoScript

CTA = "Book a demo"


def beat(t0: float, t1: float, **extra: Any) -> dict[str, Any]:
    return {"t0": t0, "t1": t1, "visual": f"shot {t0}-{t1}", **extra}


def script(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "duration_s": 12,
        "beats": [
            beat(0, 4, voiceover="Every sheet, current."),
            beat(4, 8, on_screen_text="No more hunting for binders"),
            beat(8, 12, voiceover="See it in minutes.", on_screen_text="Book a demo today"),
        ],
        "captions": [
            {"t0": 0, "t1": 4, "text": "Every sheet, current."},
            {"t0": 8, "t1": 12, "text": "See it in minutes."},
        ],
        "cta": CTA,
    }
    return {**base, **overrides}


def rejected(payload: dict[str, Any]) -> str:
    with pytest.raises(ValidationError) as caught:
        VideoScript.model_validate(payload)
    return str(caught.value)


def test_a_script_that_works_with_sound_off_validates() -> None:
    parsed = VideoScript.model_validate(script())
    assert parsed.duration_s == 12
    assert [b.t1 for b in parsed.beats] == [4, 8, 12]


def test_voiceover_not_covered_by_captions_is_rejected() -> None:
    message = rejected(
        script(
            captions=[
                {"t0": 0, "t1": 3, "text": "Every sheet,"},  # 3–4 s is spoken, not captioned
                {"t0": 8, "t1": 12, "text": "See it in minutes."},
            ]
        )
    )
    assert "voiceover at 0–4 s" in message
    assert "3–4 s" in message


def test_voiceover_with_no_caption_at_all_is_rejected() -> None:
    message = rejected(script(captions=[{"t0": 0, "t1": 4, "text": "Every sheet, current."}]))
    assert "voiceover at 8–12 s" in message


def test_a_gap_between_two_captions_inside_a_voiced_beat_is_rejected() -> None:
    message = rejected(
        script(
            captions=[
                {"t0": 0, "t1": 1.5, "text": "Every sheet,"},
                {"t0": 2.5, "t1": 4, "text": "current."},
                {"t0": 8, "t1": 12, "text": "See it in minutes."},
            ]
        )
    )
    assert "1.5–2.5 s" in message


def test_touching_captions_cover_a_voiced_beat_between_them() -> None:
    parsed = VideoScript.model_validate(
        script(
            captions=[
                {"t0": 0, "t1": 2, "text": "Every sheet,"},
                {"t0": 2, "t1": 4, "text": "current."},
                {"t0": 8, "t1": 12, "text": "See it in minutes."},
            ]
        )
    )
    assert len(parsed.captions) == 3


def test_a_cta_only_spoken_and_never_on_screen_is_rejected() -> None:
    message = rejected(
        script(
            beats=[
                beat(0, 4, voiceover="Every sheet, current."),
                beat(4, 8, on_screen_text="No more hunting for binders"),
                beat(8, 12, voiceover="Book a demo."),
            ],
            captions=[
                {"t0": 0, "t1": 4, "text": "Every sheet, current."},
                {"t0": 8, "t1": 12, "text": "Book a demo."},
            ],
        )
    )
    assert "on-screen text" in message
    assert CTA in message


def test_the_cta_is_found_in_on_screen_text_regardless_of_case_and_punctuation() -> None:
    parsed = VideoScript.model_validate(script(cta="BOOK a demo!"))
    assert parsed.cta == "BOOK a demo!"


def test_a_cta_that_is_only_part_of_a_word_on_screen_is_not_found() -> None:
    message = rejected(script(cta="demo t"))
    assert "on-screen text" in message


def test_a_blank_voiceover_needs_no_caption() -> None:
    parsed = VideoScript.model_validate(
        script(
            beats=[
                beat(0, 4, voiceover="   "),
                beat(4, 8, on_screen_text="No more hunting for binders"),
                beat(8, 12, voiceover="See it in minutes.", on_screen_text="Book a demo"),
            ],
            captions=[{"t0": 8, "t1": 12, "text": "See it in minutes."}],
        )
    )
    assert parsed.beats[0].voiceover is None


@pytest.mark.parametrize(
    ("beats", "expected"),
    [
        ([beat(1, 4), beat(4, 8), beat(8, 12, on_screen_text=CTA)], "start at 0 s"),
        ([beat(0, 4), beat(5, 8), beat(8, 12, on_screen_text=CTA)], "4–5 s"),
        ([beat(0, 5), beat(4, 8), beat(8, 12, on_screen_text=CTA)], "overlap"),
        ([beat(0, 4), beat(4, 8), beat(8, 11, on_screen_text=CTA)], "end at 12 s"),
        ([beat(0, 4), beat(4, 8), beat(8, 13, on_screen_text=CTA)], "end at 12 s"),
        ([beat(0, 4), beat(4, 4), beat(4, 12, on_screen_text=CTA)], "after it starts"),
    ],
)
def test_beats_tile_the_video_exactly(beats: list[dict[str, Any]], expected: str) -> None:
    assert expected in rejected(script(beats=beats, captions=[]))


def test_a_caption_past_the_end_of_the_video_is_rejected() -> None:
    message = rejected(
        script(
            captions=[
                {"t0": 0, "t1": 4, "text": "Every sheet, current."},
                {"t0": 8, "t1": 13, "text": "See it in minutes."},
            ]
        )
    )
    assert "caption at 8–13 s" in message


def test_overlapping_captions_are_rejected() -> None:
    message = rejected(
        script(
            captions=[
                {"t0": 0, "t1": 4, "text": "Every sheet, current."},
                {"t0": 3, "t1": 5, "text": "Two at once"},
                {"t0": 8, "t1": 12, "text": "See it in minutes."},
            ]
        )
    )
    assert "overlap" in message


def test_every_problem_is_named_at_once() -> None:
    message = rejected(script(captions=[], cta="Start a trial"))
    assert "voiceover at 0–4 s" in message
    assert "voiceover at 8–12 s" in message
    assert "Start a trial" in message
