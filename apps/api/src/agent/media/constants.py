"""The `media:` constants the gateway and `calc/media.py` run on (PRD §9.5).

A projection of `creative_constants.yaml`, not a second copy of it: every
field is filled by `CreativeConstants.media_constants()` from the file, whose
startup validation refuses a value without a `source` (law 25). There are no
defaults here on purpose — a number spelled in two places is two answers.

Pure: `calc/` receives a `MediaConstants` as an argument.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MediaConstants:
    version: str
    candidates_per_concept: int
    #: Relative: |supported / required − 1|.
    ratio_tolerance: float
    crop_min_saliency_retained: float
    #: The one estimate input nobody has measured (`source: unverified`), which
    #: is why a token-priced image estimate is confidence `low`.
    image_tokens_per_megapixel: int
    semaphore_image: int
    semaphore_video: int
    video_poll_initial_s: float
    video_poll_max_s: float
    video_job_timeout_s: float
    degrade_ladder: tuple[str, ...]
    #: `video.generate_audio_default`.
    generate_audio_default: bool


def media_constants() -> MediaConstants:
    """The shipped file's media slice (no project overrides)."""
    from agent.creative.constants import get_creative_constants

    return get_creative_constants().media_constants()
