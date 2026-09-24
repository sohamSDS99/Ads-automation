"""`creative_constants.yaml` and its validation (Stage 04 PRD §9.5, law 25).

The one that matters is `test_a_constant_without_a_source_fails_startup_naming_it`:
a Stage 04 threshold nobody can attribute must stop the process, and the
refusal must say which key — otherwise "fails startup" is a stack trace that
somebody reads for twenty minutes.

`EXPECTED` is §9.5 transcribed by hand rather than read back from the file. A
test that derives its expectation from the thing it checks only proves the
code agrees with itself; this one proves the file agrees with the document.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from agent.creative.constants import (
    CONSTANTS_PATH,
    CreativeConstants,
    CreativeConstantsError,
    creative_constants_for,
    get_creative_constants,
    load_creative_constants,
)

REVIEWED = date(2026, 9, 24)

#: §9.5, key by key. `extras.*` are checked separately: the PRD writes their
#: values as `[...]` and the lists were resolved against Google's own sources.
EXPECTED: dict[str, tuple[Any, str]] = {
    "copy.headline_pool_size": (25, "internal"),
    "copy.headline_quotas": (
        {"keyword": 3, "benefit": 3, "offer": 2, "proof": 2, "objection": 2, "cta": 2},
        "internal",
    ),
    "copy.description_pool_size": (8, "internal"),
    "copy.near_duplicate_trigram": (0.80, "internal"),
    "copy.variant_min_distance": (0.65, "internal"),
    "copy.pair_repair_rounds": (1, "internal"),
    "copy.temperature_copywrite": (0.7, "internal"),
    "landing.message_match_min": (0.55, "internal"),
    "landing.viewport_mobile": ("390x844", "internal"),
    "landing.viewport_desktop": ("1280x800", "internal"),
    "offers.offer_max_age_days": (7, "internal"),
    "media.candidates_per_concept": (2, "internal"),
    "media.ratio_tolerance": (0.005, "internal"),
    "media.crop_min_saliency_retained": (0.85, "internal"),
    "media.jpeg_quality_floor": (80, "internal"),
    "media.reference_max_bytes": (8388608, "internal"),
    "media.image_tokens_per_megapixel": (1300, "unverified"),
    "media.semaphore_image": (4, "internal"),
    "media.semaphore_video": (2, "internal"),
    "media.video_poll_initial_s": (10, "openrouter docs"),
    "media.video_poll_max_s": (30, "openrouter docs"),
    "media.video_job_timeout_s": (900, "internal"),
    "media.degrade_ladder": (("candidates", "third_concept", "video_square", "video"), "internal"),
    "video.brand_within_ms": (5000, "stage board 4.4"),
    "video.end_card_ms": (2000, "internal"),
    "video.caption_height_pct": (0.055, "internal"),
    "video.caption_ocr_min_similarity": (0.85, "internal"),
    "video.target_fps": (30, "internal"),
    "video.loudness_lufs": (-16, "internal"),
    "video.generate_audio_default": (False, "internal"),
    "logo.permitted_surfaces": (
        ("pmax_image", "display_image", "demand_gen_image", "video"),
        "unverified",
    ),
    "exceptions.max_exceptions_per_run": (20, "internal"),
    "retention.unreleased_media_days": (30, "internal"),
    "retention.superseded_package_days": (180, "internal"),
    "preview.serp_template_version": ("2026.09", "unverified"),
}

#: Google Ads Help, "About structured snippet assets" (answer 6280012): the
#: predefined headers, verbatim and in Google's order.
SNIPPET_HEADERS = (
    "Amenities",
    "Brands",
    "Courses",
    "Degree programs",
    "Destinations",
    "Featured hotels",
    "Insurance coverage",
    "Models",
    "Neighborhoods",
    "Service catalog",
    "Shows",
    "Styles",
    "Types",
)


def _without_source(key: str) -> str:
    """The shipped file with one constant's `source` removed."""
    group, _, name = key.partition(".")
    lines = CONSTANTS_PATH.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    in_group = False
    for line in lines:
        if line and not line.startswith(" ") and not line.startswith("#"):
            in_group = line.startswith(f"{group}:")
        if in_group and line.strip().startswith(f"{name}:"):
            assert "source:" in line, f"{key} is not written on one line"
            before, _, after = line.partition("source:")
            # Drop `source: <value>,` — the value runs to the next comma.
            _, _, rest = after.partition(",")
            line = before + rest.lstrip()
        out.append(line)
    return "\n".join(out) + "\n"


def test_the_shipped_file_is_exactly_section_9_5() -> None:
    constants = load_creative_constants()
    assert constants.version == "2026.09.1"
    for key, (value, source) in EXPECTED.items():
        constant = constants.get(key)
        assert constant.value == value, key
        assert constant.source == source, key
        assert constant.reviewed_at == REVIEWED, key
    assert set(constants.keys()) == set(EXPECTED) | {
        "extras.snippet_headers",
        "extras.lead_form_question_types",
    }


def test_the_extras_lists_are_googles_own_and_stay_unverified() -> None:
    constants = load_creative_constants()
    snippets = constants.get("extras.snippet_headers")
    assert snippets.value == SNIPPET_HEADERS
    assert snippets.source == "unverified"
    questions = constants.get("extras.lead_form_question_types")
    assert questions.source == "unverified"
    # googleapis `LeadFormFieldUserInputTypeEnum`, less its two sentinels.
    assert "FULL_NAME" in questions.value and "EMAIL" in questions.value
    assert "UNSPECIFIED" not in questions.value and "UNKNOWN" not in questions.value
    assert len(questions.value) == len(set(questions.value)) == 116


def test_a_constant_without_a_source_fails_startup_naming_it(tmp_path: Path) -> None:
    path = tmp_path / "creative_constants.yaml"
    path.write_text(_without_source("copy.headline_pool_size"), encoding="utf-8")
    with pytest.raises(CreativeConstantsError, match=r"copy\.headline_pool_size"):
        load_creative_constants(path)


def test_a_nested_constant_without_a_source_names_its_own_key(tmp_path: Path) -> None:
    path = tmp_path / "creative_constants.yaml"
    path.write_text(_without_source("video.loudness_lufs"), encoding="utf-8")
    with pytest.raises(CreativeConstantsError, match=r"video\.loudness_lufs"):
        load_creative_constants(path)


def test_a_blank_source_is_not_a_source(tmp_path: Path) -> None:
    text = CONSTANTS_PATH.read_text(encoding="utf-8").replace(
        "semaphore_video:             { value: 2,     source: internal,",
        'semaphore_video:             { value: 2,     source: "",',
    )
    assert 'source: "",' in text
    path = tmp_path / "creative_constants.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(CreativeConstantsError, match=r"media\.semaphore_video"):
        load_creative_constants(path)


def test_a_typo_is_refused_rather_than_ignored(tmp_path: Path) -> None:
    text = CONSTANTS_PATH.read_text(encoding="utf-8").replace(
        "target_fps:                  { value: 30,    source: internal,",
        "target_fps:                  { value: 30,    sources: internal,",
    )
    path = tmp_path / "creative_constants.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(CreativeConstantsError, match=r"video\.target_fps"):
        load_creative_constants(path)


def test_a_boolean_is_not_a_count(tmp_path: Path) -> None:
    text = CONSTANTS_PATH.read_text(encoding="utf-8").replace(
        "headline_pool_size:          { value: 25,",
        "headline_pool_size:          { value: true,",
    )
    path = tmp_path / "creative_constants.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(CreativeConstantsError, match=r"copy\.headline_pool_size"):
        load_creative_constants(path)


def test_a_missing_file_names_its_path(tmp_path: Path) -> None:
    missing = tmp_path / "nope.yaml"
    with pytest.raises(CreativeConstantsError, match="nope.yaml"):
        load_creative_constants(missing)


def test_get_refuses_an_unknown_key() -> None:
    constants = load_creative_constants()
    with pytest.raises(CreativeConstantsError, match="copy.nope"):
        constants.get("copy.nope")
    with pytest.raises(CreativeConstantsError, match="nope.headline_pool_size"):
        constants.get("nope.headline_pool_size")


def test_the_process_wide_copy_is_cached() -> None:
    assert get_creative_constants() is get_creative_constants()


def test_an_override_changes_value_source_and_version() -> None:
    base = load_creative_constants()
    merged = base.merged({"copy.temperature_copywrite": 0.4})
    assert merged.get("copy.temperature_copywrite").value == 0.4
    assert merged.get("copy.temperature_copywrite").source == "project_override"
    assert merged.version.startswith(f"{base.version}+ovr.")
    assert merged.get("copy.headline_pool_size").value == 25
    # Nested and dotted shapes are the same override.
    nested = base.merged({"copy": {"temperature_copywrite": 0.4}})
    assert nested.version == merged.version


def test_an_override_keeps_the_value_type() -> None:
    base = load_creative_constants()
    with pytest.raises(CreativeConstantsError, match="copy.headline_pool_size"):
        base.merged({"copy.headline_pool_size": "many"})
    with pytest.raises(CreativeConstantsError, match="copy.nope"):
        base.merged({"copy.nope": 1})


def test_no_override_is_the_same_object() -> None:
    base = load_creative_constants()
    assert base.merged(None) is base
    assert base.merged({}) is base


def test_a_project_reads_its_own_overrides() -> None:
    class _Project:
        settings = {"creative_overrides": {"copy.headline_pool_size": 30}}

    merged = creative_constants_for(_Project())  # type: ignore[arg-type]
    assert merged.get("copy.headline_pool_size").value == 30
    assert merged.version != get_creative_constants().version


def test_the_file_ships_inside_the_package() -> None:
    assert CONSTANTS_PATH.name == "creative_constants.yaml"
    assert CONSTANTS_PATH.parent.name == "creative"
    assert isinstance(load_creative_constants(), CreativeConstants)


async def test_the_api_refuses_to_start_on_an_unsourced_constant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Law 25 at the process boundary, not only in the loader."""
    from fastapi import FastAPI

    import agent.creative.constants as module
    from agent.main import lifespan

    path = tmp_path / "creative_constants.yaml"
    path.write_text(_without_source("media.semaphore_image"), encoding="utf-8")
    monkeypatch.setattr(module, "CONSTANTS_PATH", path)
    get_creative_constants.cache_clear()
    try:
        with pytest.raises(CreativeConstantsError, match=r"media\.semaphore_image"):
            async with lifespan(FastAPI()):
                pass
    finally:
        get_creative_constants.cache_clear()


async def test_the_worker_refuses_to_start_on_an_unsourced_constant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent.creative.constants as module
    from agent.worker import startup

    path = tmp_path / "creative_constants.yaml"
    path.write_text(_without_source("retention.unreleased_media_days"), encoding="utf-8")
    monkeypatch.setattr(module, "CONSTANTS_PATH", path)
    get_creative_constants.cache_clear()
    try:
        with pytest.raises(CreativeConstantsError, match=r"retention\.unreleased_media_days"):
            await startup({})
    finally:
        get_creative_constants.cache_clear()
