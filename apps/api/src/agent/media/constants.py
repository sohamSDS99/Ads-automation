"""The `media:` constants S4-P1 needs, with the values PRD §9.5 gives them.

`creative_constants.yaml` and its startup validation are S4-P4's (§21.3). Until
then the numbers the gateway runs on are spelled once, here, each with the
source §9.5 records for it — the same "one place, cited" rule the YAML will
enforce — and `version` is stamped from `Settings.creative_constants_version`
(still S4-P0's placeholder), exactly as `CreativeInput.constants_version` is.
S4-P4 replaces `media_constants()` with a read of the file.

Pure: `calc/` receives a `MediaConstants` as an argument.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MediaConstants:
    version: str
    #: source: internal, reviewed 2026-09-24.
    candidates_per_concept: int = 2
    #: source: internal. Relative: |supported / required − 1|.
    ratio_tolerance: float = 0.005
    #: source: internal.
    crop_min_saliency_retained: float = 0.85
    #: source: **unverified** — the one estimate input nobody has measured,
    #: which is why a token-priced image estimate is confidence `low`.
    image_tokens_per_megapixel: int = 1300
    #: source: internal.
    semaphore_image: int = 4
    semaphore_video: int = 2
    #: source: openrouter docs.
    video_poll_initial_s: float = 10
    video_poll_max_s: float = 30
    #: source: internal.
    video_job_timeout_s: float = 900
    #: source: internal.
    degrade_ladder: tuple[str, ...] = ("candidates", "third_concept", "video_square", "video")
    #: source: internal (§9.5 `video.generate_audio_default`).
    generate_audio_default: bool = False


def media_constants() -> MediaConstants:
    from agent.config import get_settings

    return MediaConstants(version=get_settings().creative_constants_version)
