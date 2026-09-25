"""What 4.4.4 decides in code (`creative/video.py`, PRD §9.4 video 1–2, §11, Law 38).

The duration window comes from the pinned spec sheet or not at all; the script
is timed by the shot plan, not by the model; captions are derived from the
voiceover so "works with sound off" holds by construction; and every clip
prompt forbids what code places later (text, logos) and what no reference
guides here (the product).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from agent.creative import video
from agent.media.types import CapabilityRecord, Descriptor, PriceLine, VideoCaps
from agent.schemas.creative_media import Concept
from agent.schemas.creative_video import VideoScript
from agent.schemas.guardrails import LintFinding, LintResult

SPEC = {"source": "unverified", "reviewed_at": "2026-09-22"}


def specs(**video_types: dict[str, Any]) -> dict[str, Any]:
    return {
        "performance_max": {
            "headline": {"max_chars": 30, **SPEC},
            "image_landscape": {"ratio": "1.91:1", **SPEC},
            "logo_square": {"ratio": "1:1", **SPEC},
            **{name: {**spec, **SPEC} for name, spec in video_types.items()},
        }
    }


def concept(depiction: str = "reference_guided") -> Concept:
    return Concept(
        id="c-1",
        campaign_ref="c-pmax",
        name="Binder-free morning",
        angle="angle",
        angle_text="Every sheet current",
        rationale="Shows the relief of finding a sheet at once.",
        subject="a safety officer at a workbench",
        setting="a bright workshop in the morning",
        composition_by_ratio={"16:9": "subject on the left third", "9:16": "subject low"},
        palette_tokens=["brand-orange"],
        product_depiction=depiction,  # type: ignore[arg-type]
        prompt="a safety officer at a workbench. Avoid: no text, no logos, no watermark.",
        negative_constraints=["no text, no logos, no watermark"],
        surfaces=["pmax_image"],
    )


def veo(*, audio: bool = True, seed: bool = True) -> CapabilityRecord:
    params: dict[str, Descriptor] = {}
    if audio:
        params["generate_audio"] = Descriptor(kind="boolean")
    if seed:
        params["seed"] = Descriptor(kind="boolean")
    return CapabilityRecord(
        modality="video",
        model_id="google/veo-3.1-lite",
        params=params,
        video=VideoCaps(durations=[4, 6, 8], resolutions=["720p"], aspect_ratios=["16:9", "9:16"]),
        pricing=[
            PriceLine(billable="output_video", unit="second", usd=Decimal("0.03"), audio=False)
        ],
        input_modalities=["text"],
    )


# ---------------------------------------------------------------------------
# the spec window
# ---------------------------------------------------------------------------


def test_video_specs_are_the_video_asset_types_with_a_ratio() -> None:
    found = video.video_specs(
        specs(
            video_landscape={"ratio": "16:9", "min_duration_s": 10},
            video_note={"max_chars": 30},  # no ratio: not a surface
        ),
        "performance_max",
    )
    assert list(found) == ["video_landscape"]


def test_the_window_is_the_intersection_of_every_video_spec() -> None:
    window = video.duration_window(
        video.video_specs(
            specs(
                video_landscape={"ratio": "16:9", "min_duration_s": 10, "max_duration_s": 60},
                video_portrait={"ratio": "9:16", "min_duration_s": 15},
            ),
            "performance_max",
        ),
        campaign_type="performance_max",
    )
    assert (window.min_s, window.max_s) == (15, 60)
    assert window.asset_types == ("video_landscape", "video_portrait")


def test_a_video_spec_with_no_duration_is_spec_missing_naming_each_one() -> None:
    with pytest.raises(video.VideoPlanProblem) as caught:
        video.duration_window(
            video.video_specs(
                specs(
                    video_landscape={"ratio": "16:9"},
                    video_portrait={"ratio": "9:16", "min_duration_s": 10},
                    video_square={"ratio": "1:1"},
                ),
                "performance_max",
            ),
            campaign_type="performance_max",
        )
    assert caught.value.reason == "spec_missing"
    assert "asset_specs.performance_max.video_landscape.min_duration_s" in caught.value.detail
    assert "asset_specs.performance_max.video_square.min_duration_s" in caught.value.detail
    assert "video_portrait" not in caught.value.detail


def test_windows_that_do_not_overlap_are_named_as_a_conflict() -> None:
    with pytest.raises(video.VideoPlanProblem) as caught:
        video.duration_window(
            video.video_specs(
                specs(
                    video_landscape={"ratio": "16:9", "max_duration_s": 6},
                    video_portrait={"ratio": "9:16", "min_duration_s": 10},
                ),
                "performance_max",
            ),
            campaign_type="performance_max",
        )
    assert caught.value.reason == "conflicting_duration_specs"
    assert "10" in caught.value.detail and "6" in caught.value.detail


# ---------------------------------------------------------------------------
# the script
# ---------------------------------------------------------------------------


def draft(model: type[Any], shots: list[dict[str, Any]], cta: str = "Book a demo") -> Any:
    """As the model answers in strict mode: every key present, absent ones null."""
    full = [{"voiceover": None, "on_screen_text": None, **shot} for shot in shots]
    return model.model_validate({"shots": full, "cta": cta})


def test_the_draft_has_exactly_one_shot_per_clip() -> None:
    model = video.script_draft_model(3)
    shot = {"visual": "a workbench", "voiceover": None, "on_screen_text": "Book a demo"}
    assert len(draft(model, [shot, shot, shot]).shots) == 3
    with pytest.raises(ValidationError):
        draft(model, [shot, shot])
    with pytest.raises(ValidationError):
        draft(model, [shot, shot, shot, shot])


def test_a_draft_that_never_shows_the_cta_is_refused_before_it_is_timed() -> None:
    model = video.script_draft_model(1)
    with pytest.raises(ValidationError, match="on-screen text"):
        draft(model, [{"visual": "a bench", "voiceover": "Book a demo", "on_screen_text": None}])


def test_the_script_is_timed_by_the_shot_plan_and_captions_follow_the_voiceover() -> None:
    model = video.script_draft_model(3)
    parsed = draft(
        model,
        [
            {"visual": "a cluttered binder shelf", "voiceover": "Still hunting for sheets?"},
            {"visual": "a tablet lighting up", "on_screen_text": "Every sheet, current"},
            {
                "visual": "the officer smiling",
                "voiceover": "See it today.",
                "on_screen_text": "Book a demo",
            },
        ],
    )
    clips = [
        {"index": 0, "t0": 0, "t1": 8, "duration_s": 8},
        {"index": 1, "t0": 8, "t1": 14, "duration_s": 6},
        {"index": 2, "t0": 14, "t1": 20, "duration_s": 6},
    ]
    script = video.assemble_script(parsed, clips=clips, duration_s=20)
    assert isinstance(script, VideoScript)
    assert [(b.t0, b.t1) for b in script.beats] == [(0, 8), (8, 14), (14, 20)]
    assert [(c.t0, c.t1, c.text) for c in script.captions] == [
        (0, 8, "Still hunting for sheets?"),
        (14, 20, "See it today."),
    ]
    assert script.cta == "Book a demo"


def test_script_texts_are_every_distinct_line_a_viewer_reads_or_hears() -> None:
    script = VideoScript.model_validate(
        {
            "duration_s": 8,
            "beats": [
                {"t0": 0, "t1": 4, "visual": "v", "voiceover": "See it today."},
                {"t0": 4, "t1": 8, "visual": "v", "on_screen_text": "Book a demo now"},
            ],
            "captions": [{"t0": 0, "t1": 4, "text": "See it today."}],
            "cta": "Book a demo",
        }
    )
    texts = video.script_texts(script)
    assert texts == [
        ("beat0.voiceover", "See it today."),
        ("beat1.on_screen_text", "Book a demo now"),
        ("cta", "Book a demo"),
    ]


# ---------------------------------------------------------------------------
# the clip requests
# ---------------------------------------------------------------------------


def test_every_clip_prompt_forbids_text_logos_and_the_product() -> None:
    beat = VideoScript.model_validate(
        {
            "duration_s": 8,
            "beats": [{"t0": 0, "t1": 8, "visual": "a tablet lighting up", "on_screen_text": "Go"}],
            "cta": "Go",
        }
    ).beats[0]
    prompt = video.clip_prompt(
        concept("reference_guided"),
        beat,
        ratio="16:9",
        index=1,
        count=3,
        forbidden_subjects=["hard hats"],
    )
    assert prompt.startswith("Shot 2 of 3 of one continuous video ad.")
    assert "a tablet lighting up" in prompt
    assert "subject on the left third" in prompt
    for negative in (*video.VIDEO_NEGATIVES, "no hard hats"):
        assert negative in prompt


def test_the_request_carries_only_what_the_model_accepts() -> None:
    fields = video.request_fields(
        {
            "resolution": "720p",
            "generate_audio": True,
            "seed": 7,
            "size": "1280x720",
            "duration": 8,
        },
        veo(),
        generate_audio_default=False,
    )
    # duration and aspect ratio are the shot plan's; size would fight the ratio.
    assert fields == {"resolution": "720p", "generate_audio": True, "seed": 7}
    assert video.request_fields({}, veo(), generate_audio_default=False) == {
        "generate_audio": False
    }
    assert video.request_fields({"seed": 7}, veo(audio=False, seed=False), False) == {}


def test_square_is_made_last_so_the_ladder_drops_it_first() -> None:
    assert video.generation_order(["1:1", "16:9", "9:16"]) == ["16:9", "9:16", "1:1"]


# ---------------------------------------------------------------------------
# lint
# ---------------------------------------------------------------------------


def lint(verdict: str, *findings: str) -> LintResult:
    return LintResult(
        ruleset_version="1.0+abc",
        verdict=verdict,  # type: ignore[arg-type]
        findings=tuple(
            LintFinding(
                target_ref=ref,
                rule_id=f"r-{ref}",
                severity="blocking",
                message="m",
                authority_ref="https://example.com",
            )
            for ref in findings
        ),
        targets_checked=1,
        rules_evaluated=4,
        elapsed_ms=2,
        evaluated_at=datetime(2026, 9, 25, tzinfo=UTC),
    )


def test_merged_lint_takes_the_worst_verdict_and_every_finding() -> None:
    merged = video.merge_lint(
        [lint("pass"), lint("fail", "cta"), lint("pass_with_warnings", "beat0.voiceover")]
    )
    assert merged.verdict == "fail"
    assert [f.target_ref for f in merged.findings] == ["cta", "beat0.voiceover"]
    assert merged.targets_checked == 3
    assert merged.ruleset_version == "1.0+abc"
