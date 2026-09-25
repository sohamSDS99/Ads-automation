"""The contracts of 4.4.4 `video_production` (Stage 04 PRD §9.4 video 1–2, §11):
the script, and the node's output up to downloaded clips.

`VideoScript` is written before a clip is paid for, and "works with sound off"
is its validator, not a review note: every `voiceover` interval is fully
covered by captions, and the CTA appears as on-screen text (§9.4 video 1). The
beats tile the video — every second of it has a picture — and captions never
overlap, because two burned-in captions at once read as one garbled one.

Times are seconds. Two edges within `EPSILON_S` of each other are one edge, so
a caption ending at 4.0 and one starting at 4.0000001 are touching, not a gap.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: Two time edges this close are the same edge (1 ms).
EPSILON_S = 0.001


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _s(value: float) -> str:
    return f"{value:g}"


def _span(t0: float, t1: float) -> str:
    return f"{_s(t0)}–{_s(t1)} s"


def words(text: str) -> str:
    """Case-folded words joined by single spaces — punctuation and spacing
    are not part of what a viewer reads."""
    return " ".join(re.findall(r"[^\W_]+", text.casefold()))


class ScriptBeat(_Frozen):
    """One stretch of the video: what is seen, and what is said or shown over it."""

    t0: float = Field(ge=0)
    t1: float = Field(ge=0)
    visual: str = Field(min_length=1)
    voiceover: str | None = None
    on_screen_text: str | None = None

    @field_validator("voiceover", "on_screen_text")
    @classmethod
    def _blank_is_absent(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class ScriptCaption(_Frozen):
    t0: float = Field(ge=0)
    t1: float = Field(ge=0)
    text: str = Field(min_length=1)


class VideoScript(_Frozen):
    """`VideoScript{duration_s, beats[]{t0, t1, visual, voiceover?, on_screen_text?},
    captions[]{t0, t1, text}, cta}` (§9.4 video 1)."""

    duration_s: int = Field(ge=1)
    beats: list[ScriptBeat] = Field(min_length=1)
    captions: list[ScriptCaption] = Field(default_factory=list)
    cta: str = Field(min_length=1)

    @model_validator(mode="after")
    def _works_with_sound_off(self) -> VideoScript:
        # Every problem at once: a script that fails on one should not hide the next.
        problems = [*self._beat_timeline(), *self._caption_timeline(), *self._uncaptioned()]
        problems.extend(self._cta_off_screen())
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def _beat_timeline(self) -> list[str]:
        problems: list[str] = []
        for item in self.beats:
            if item.t1 - item.t0 <= EPSILON_S:
                problems.append(f"the beat at {_span(item.t0, item.t1)} must end after it starts")
        first, last = self.beats[0], self.beats[-1]
        if first.t0 > EPSILON_S:
            problems.append(f"beats must start at 0 s; the first starts at {_s(first.t0)} s")
        for before, after in zip(self.beats, self.beats[1:], strict=False):
            if after.t0 > before.t1 + EPSILON_S:
                problems.append(f"no beat covers {_span(before.t1, after.t0)}")
            elif after.t0 < before.t1 - EPSILON_S:
                problems.append(
                    f"the beats at {_span(before.t0, before.t1)} and "
                    f"{_span(after.t0, after.t1)} overlap"
                )
        if abs(last.t1 - self.duration_s) > EPSILON_S:
            problems.append(
                f"beats must end at {self.duration_s} s, the video's length; the last ends at "
                f"{_s(last.t1)} s"
            )
        return problems

    def _caption_timeline(self) -> list[str]:
        problems: list[str] = []
        for item in self.captions:
            if item.t1 - item.t0 <= EPSILON_S:
                problems.append(
                    f"the caption at {_span(item.t0, item.t1)} must end after it starts"
                )
            elif item.t1 > self.duration_s + EPSILON_S:
                problems.append(
                    f"the caption at {_span(item.t0, item.t1)} runs past the end of the "
                    f"{self.duration_s} s video"
                )
        ordered = sorted(self.captions, key=lambda item: (item.t0, item.t1))
        for before, after in zip(ordered, ordered[1:], strict=False):
            if after.t0 < before.t1 - EPSILON_S:
                problems.append(
                    f"the captions at {_span(before.t0, before.t1)} and "
                    f"{_span(after.t0, after.t1)} overlap"
                )
        return problems

    def _uncaptioned(self) -> list[str]:
        covered = _union((item.t0, item.t1) for item in self.captions)
        problems: list[str] = []
        for item in self.beats:
            if item.voiceover is None:
                continue
            gaps = _uncovered(item.t0, item.t1, covered)
            if gaps:
                problems.append(
                    f"the voiceover at {_span(item.t0, item.t1)} is not captioned at "
                    f"{', '.join(_span(a, b) for a, b in gaps)} — a viewer with the sound off "
                    "misses it"
                )
        return problems

    def _cta_off_screen(self) -> list[str]:
        wanted = words(self.cta)
        if not wanted:
            return [f"the CTA {self.cta!r} has no words to show"]
        shown = (words(item.on_screen_text) for item in self.beats if item.on_screen_text)
        if any(f" {wanted} " in f" {text} " for text in shown):
            return []
        return [
            f"the CTA {self.cta!r} does not appear in any beat's on-screen text — with the "
            "sound off it is never seen"
        ]


def _union(spans: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    """Merge intervals that overlap or touch (within `EPSILON_S`)."""
    merged: list[list[float]] = []
    for t0, t1 in sorted(spans):
        if merged and t0 <= merged[-1][1] + EPSILON_S:
            merged[-1][1] = max(merged[-1][1], t1)
        else:
            merged.append([t0, t1])
    return [(a, b) for a, b in merged]


def _uncovered(
    t0: float, t1: float, covered: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    """The parts of `[t0, t1]` no interval in `covered` (merged, sorted) reaches."""
    gaps: list[tuple[float, float]] = []
    cursor = t0
    for a, b in covered:
        if b <= cursor + EPSILON_S:
            continue
        if a >= t1 - EPSILON_S:
            break
        if a > cursor + EPSILON_S:
            gaps.append((cursor, a))
        cursor = max(cursor, b)
        if cursor >= t1 - EPSILON_S:
            return gaps
    if cursor < t1 - EPSILON_S:
        gaps.append((cursor, t1))
    return gaps


# ---------------------------------------------------------------------------
# 4.4.4 video_production — up to downloaded clips (S4-P11)
# ---------------------------------------------------------------------------

#: Why a campaign's video, a ratio of it or one clip was not made.
VideoGapReason = Literal[
    "spec_missing",
    "conflicting_duration_specs",
    "no_plannable_duration",
    "ratio_unsupported",
    "no_source_ratio",
    "script_failed_lint",
    "blocked_by_budget",
    "generation_failed",
    "unknown_submit_state",
    "undecodable_clip",
]

#: A clip that timed out is not here: its OpenRouter job is still alive, so
#: the node fails and names it for Check again instead of calling it a gap.
ClipStatus = Literal[
    "completed",
    "failed",
    "cancelled",
    "expired",
    "unknown_submit_state",
    "blocked_by_budget",
    "undecodable",
]


class DurationWindowOut(_Frozen):
    asset_types: list[str]
    min_s: int | None
    max_s: int | None


class PlannedClip(_Frozen):
    index: int = Field(ge=0)
    t0: int = Field(ge=0)
    t1: int = Field(ge=1)
    duration_s: int = Field(ge=1)


class ShotPlanOut(_Frozen):
    clips: list[PlannedClip] = Field(min_length=1)
    #: The `media.shot_plan_v1` row this node computed (Law 14).
    calc_evidence_ids: list[uuid.UUID] = Field(min_length=1)


class ScriptLint(_Frozen):
    verdict: str
    ruleset_version: str
    rule_ids: list[str] = Field(default_factory=list)


class VideoClip(_Frozen):
    ratio: str = Field(min_length=1)
    index: int = Field(ge=0)
    t0: int = Field(ge=0)
    t1: int = Field(ge=1)
    duration_s: int = Field(ge=1)
    job_id: uuid.UUID
    status: ClipStatus
    #: The `MediaArtifact(role=clip)` of the downloaded file.
    media_id: uuid.UUID | None = None


class CampaignVideo(_Frozen):
    campaign_ref: str = Field(min_length=1)
    campaign_type: str = Field(min_length=1)
    concept_id: str = Field(min_length=1)
    #: The `video` asset every clip job and clip artifact belongs to.
    asset_id: uuid.UUID
    #: The `video_script` asset: committed, linted, before any clip was paid for.
    script_asset_id: uuid.UUID
    duration_s: int = Field(ge=1)
    duration_window: DurationWindowOut
    script: VideoScript
    script_lint: ScriptLint
    shot_plan: ShotPlanOut
    #: Per required ratio: `{"plan": "native"}` or `{"plan": "crop", "from": …}`
    #: (a crop is made in post-production from a generated ratio).
    ratio_plan: dict[str, dict[str, str]]
    clips: list[VideoClip] = Field(default_factory=list)
    #: Degrade-ladder rungs taken because the media cap refused a clip (§9.3).
    degraded: list[str] = Field(default_factory=list)


class VideoGap(_Frozen):
    campaign_ref: str = Field(min_length=1)
    reason: VideoGapReason
    detail: str = Field(min_length=1)
    ratio: str | None = None
    clip_index: int | None = None
    job_id: uuid.UUID | None = None


class VideoProduction(_Frozen):
    status: Literal["produced", "not_required"]
    why: str | None = None
    videos: list[CampaignVideo] = Field(default_factory=list)
    gaps: list[VideoGap] = Field(default_factory=list)
