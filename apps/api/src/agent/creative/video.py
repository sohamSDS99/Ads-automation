"""What 4.4.4 `video_production` decides in code (Stage 04 PRD §9.4 video 1–2, §11).

* **The length comes from the pinned spec sheet or not at all.** A campaign's
  video asset types carry a duration window (`min_duration_s` /
  `max_duration_s`, §9.5); a video type with neither bound is `spec_missing`,
  never a guess (Q6). One script serves every ratio of a campaign's video, so
  its types' windows must overlap.
* **The model writes one shot per clip; code times it.** What is seen, said and
  shown in each shot, and the CTA, are the model's; `t0`/`t1` are the shot
  plan's (`media.shot_plan_v1`), so no beat can straddle a clip. Captions are
  the voiceover, one per voiced beat, so every voiceover interval is captioned
  by construction — and `VideoScript` still checks it (§9.4 video 1).
* **Every clip prompt forbids what code places and what nothing guides.** Code
  burns the captions and places the logo and the end card (Law 38), so a clip
  carries no text and no marks; 4.4.4 sends the video model no reference and
  nothing composites a product into a clip, so a depicted product could only be
  an invented one.

Pure: records and strings in, records and strings out.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator

from agent.creative.concepts import ALWAYS_NEGATIVES
from agent.media.capability import parse_ratio
from agent.media.types import CapabilityRecord
from agent.schemas.creative_media import Concept
from agent.schemas.creative_video import ScriptBeat, ScriptCaption, VideoScript, words
from agent.schemas.guardrails import LintResult

#: What a clip may never show. Captions, logos and the end card are code's
#: (Law 38); the product has no reference to be drawn from here.
VIDEO_NEGATIVES: tuple[str, ...] = (
    "no text, captions, subtitles, letters or numbers anywhere in the frame",
    "no logos, watermarks or graphic overlays",
    "do not depict the product, its packaging or its labels",
)

#: The fields of a model's `defaults` a clip request may carry. `duration`
#: and `aspect_ratio` are the shot plan's and the ratio's; `size` names pixel
#: dimensions and would contradict the ratio being made.
REQUEST_DEFAULT_FIELDS: tuple[str, ...] = ("resolution", "generate_audio", "seed")

_VERDICT_RANK = {"pass": 0, "pass_with_warnings": 1, "fail": 2}


class VideoPlanProblem(ValueError):
    """Why a campaign's video cannot be planned — recorded as its gap."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True, slots=True)
class DurationWindow:
    asset_types: tuple[str, ...]
    min_s: int | None
    max_s: int | None


# ---------------------------------------------------------------------------
# the spec window
# ---------------------------------------------------------------------------


def video_specs(specs: Mapping[str, Any], campaign_type: str) -> dict[str, dict[str, Any]]:
    """The campaign type's video surfaces — the asset types `ratios_for` reads
    as video (a `ratio`, "video" in the name, never a logo) — by asset type.

    Sorted by name: a spec sheet round-trips through JSONB, which does not keep
    the file's key order, and the window must not depend on it."""
    found: dict[str, dict[str, Any]] = {}
    for asset_type in sorted((specs.get(campaign_type) or {}).keys()):
        spec = specs[campaign_type][asset_type]
        is_video = "video" in asset_type and "logo" not in asset_type
        if is_video and isinstance(spec, dict) and spec.get("ratio"):
            found[asset_type] = spec
    return found


def duration_window(
    video_types: Mapping[str, Mapping[str, Any]], *, campaign_type: str
) -> DurationWindow:
    """The one window every video type of the campaign allows."""
    missing = [
        f"asset_specs.{campaign_type}.{asset_type}.min_duration_s or .max_duration_s"
        for asset_type, spec in video_types.items()
        if spec.get("min_duration_s") is None and spec.get("max_duration_s") is None
    ]
    if missing:
        raise VideoPlanProblem(
            "spec_missing",
            f"The pinned spec sheet has no {', '.join(missing)}; Stage 04 never guesses a "
            "video's length (§9.5, Q6).",
        )
    mins = {t: int(s["min_duration_s"]) for t, s in video_types.items() if s.get("min_duration_s")}
    maxes = {t: int(s["max_duration_s"]) for t, s in video_types.items() if s.get("max_duration_s")}
    low = max(mins.values()) if mins else None
    high = min(maxes.values()) if maxes else None
    if low is not None and high is not None and low > high:
        longest = max(mins, key=lambda t: (mins[t], t))
        shortest = min(maxes, key=lambda t: (maxes[t], t))
        raise VideoPlanProblem(
            "conflicting_duration_specs",
            f"{longest} needs at least {low} s but {shortest} allows at most {high} s; one "
            "script cannot be both.",
        )
    return DurationWindow(asset_types=tuple(video_types), min_s=low, max_s=high)


# ---------------------------------------------------------------------------
# the script
# ---------------------------------------------------------------------------


class _Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ShotDraft(_Draft):
    visual: str = Field(min_length=1, description="What the camera shows in this shot.")
    voiceover: str | None = Field(description="What is said over this shot, or null.")
    on_screen_text: str | None = Field(description="Text shown over this shot, or null.")


class _ScriptDraft(_Draft):
    shots: list[ShotDraft]
    cta: str = Field(min_length=1, description="The call to action, shown on screen.")

    @model_validator(mode="after")
    def _cta_is_shown(self) -> _ScriptDraft:
        wanted = words(self.cta)
        shown = [words(shot.on_screen_text) for shot in self.shots if shot.on_screen_text]
        if not wanted or not any(f" {wanted} " in f" {text} " for text in shown):
            raise ValueError(
                f"the CTA {self.cta!r} must appear word for word in some shot's on-screen "
                "text — with the sound off it is never seen otherwise"
            )
        return self


def script_draft_model(shots: int) -> type[_ScriptDraft]:
    """Exactly one shot per clip of the shot plan."""
    return create_model(
        "VideoScriptDraft",
        __base__=_ScriptDraft,
        shots=(list[ShotDraft], Field(min_length=shots, max_length=shots)),
    )


def assemble_script(
    draft: _ScriptDraft, *, clips: Sequence[Mapping[str, Any]], duration_s: int
) -> VideoScript:
    """The model's shots on the shot plan's clock, captioned from the voiceover."""
    if len(draft.shots) != len(clips):
        raise ValueError(f"{len(draft.shots)} shots for {len(clips)} clips")
    beats = [
        ScriptBeat(
            t0=float(clip["t0"]),
            t1=float(clip["t1"]),
            visual=shot.visual,
            voiceover=shot.voiceover,
            on_screen_text=shot.on_screen_text,
        )
        for shot, clip in zip(draft.shots, clips, strict=True)
    ]
    captions = [
        ScriptCaption(t0=beat.t0, t1=beat.t1, text=beat.voiceover)
        for beat in beats
        if beat.voiceover is not None
    ]
    return VideoScript(duration_s=duration_s, beats=beats, captions=captions, cta=draft.cta)


def script_texts(script: VideoScript) -> list[tuple[str, str]]:
    """Every distinct line a viewer hears or reads, with where it first appears —
    the lint targets of the script (Law 33)."""
    lines: list[tuple[str, str]] = []
    for index, beat in enumerate(script.beats):
        if beat.voiceover:
            lines.append((f"beat{index}.voiceover", beat.voiceover))
        if beat.on_screen_text:
            lines.append((f"beat{index}.on_screen_text", beat.on_screen_text))
    lines.extend((f"caption{i}", caption.text) for i, caption in enumerate(script.captions))
    lines.append(("cta", script.cta))
    seen: set[str] = set()
    distinct: list[tuple[str, str]] = []
    for ref, text in lines:
        if text not in seen:
            seen.add(text)
            distinct.append((ref, text))
    return distinct


def render_script(script: VideoScript) -> str:
    """The script as a person reads it, one beat a line."""
    lines = []
    for beat in script.beats:
        parts = [f"{beat.t0:g}–{beat.t1:g} s", beat.visual]
        if beat.voiceover:
            parts.append(f"VO: {beat.voiceover}")
        if beat.on_screen_text:
            parts.append(f"ON SCREEN: {beat.on_screen_text}")
        lines.append(" | ".join(parts))
    lines.append(f"CTA: {script.cta}")
    return "\n".join(lines)


def merge_lint(results: Sequence[LintResult]) -> LintResult:
    """One script's lint: the worst verdict of its lines and every finding."""
    if not results:
        raise ValueError("no lint results to merge")
    worst = max(results, key=lambda item: _VERDICT_RANK[item.verdict]).verdict
    return LintResult(
        ruleset_version=results[0].ruleset_version,
        verdict=worst,
        findings=tuple(finding for result in results for finding in result.findings),
        targets_checked=sum(result.targets_checked for result in results),
        rules_evaluated=max(result.rules_evaluated for result in results),
        elapsed_ms=sum(result.elapsed_ms for result in results),
        evaluated_at=max(result.evaluated_at for result in results),
    )


# ---------------------------------------------------------------------------
# the clip requests
# ---------------------------------------------------------------------------


def clip_prompt(
    concept: Concept,
    beat: ScriptBeat,
    *,
    ratio: str,
    index: int,
    count: int,
    forbidden_subjects: Iterable[str],
) -> str:
    """One clip: its place in the sequence, the shot, the concept's subject and
    setting, the framing at this ratio, and every negative in the prompt itself
    (the video API has no negative-prompt field)."""
    parts = [
        f"Shot {index + 1} of {count} of one continuous video ad.",
        beat.visual.strip().rstrip(".") + ".",
        f"Subject: {concept.subject.strip().rstrip('.')}.",
        f"Setting: {concept.setting.strip().rstrip('.')}.",
    ]
    note = concept.composition_by_ratio.get(ratio)
    if note:
        parts.append(f"Framing at {ratio}: {note.strip().rstrip('.')}.")
    parts.append(f"Avoid: {'; '.join(video_negatives(forbidden_subjects))}.")
    return " ".join(parts)


def video_negatives(forbidden_subjects: Iterable[str]) -> list[str]:
    wanted = [
        *VIDEO_NEGATIVES,
        *ALWAYS_NEGATIVES,
        *(f"no {subject.strip()}" for subject in forbidden_subjects if subject.strip()),
    ]
    return list(dict.fromkeys(wanted))


def request_fields(
    defaults: Mapping[str, Any], capability: CapabilityRecord, generate_audio_default: bool
) -> dict[str, Any]:
    """The model defaults a clip request carries — only those the capability
    record accepts (Law 36) — with `generate_audio` from the constants when the
    model takes it and the run did not choose (Q9: off)."""
    resolutions = capability.video.resolutions if capability.video else []
    accepted = set(capability.params) | ({"resolution"} if resolutions else set())
    fields = {
        name: defaults[name]
        for name in REQUEST_DEFAULT_FIELDS
        if name in defaults and defaults[name] is not None and name in accepted
    }
    if "generate_audio" in capability.params and "generate_audio" not in fields:
        fields["generate_audio"] = generate_audio_default
    return fields


def generation_order(ratios: Sequence[str]) -> list[str]:
    """Widest first, square last. Square last because the degrade ladder drops
    `video_square` before `video`, so a budget that runs out mid-video runs out
    on the ratio it would drop; an order of its own at all because the ratios
    come from a spec sheet that JSONB does not keep in file order."""
    return sorted(set(ratios), key=lambda r: (parse_ratio(r) == 1.0, -parse_ratio(r), r))
