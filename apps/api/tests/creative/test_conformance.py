"""4.6.1 spec conformance — the checks, one per measured constraint (Stage 04 PRD §11 4.6.1).

`checks[]{asset_id, constraint, expected, measured, source ∈ {lint, pillow,
ffprobe}, verdict}` for every character count, pixel size, ratio, byte size,
format, duration, fps and codec. The measurements here are the ones the real
node takes from the linter's own counter, Pillow's decode and ffprobe; these
tests hand them in, so each check's arithmetic is pinned without a file.
"""

from __future__ import annotations

import uuid
from datetime import date

from agent.creative import conformance
from agent.postprod.probe import ImageFacts, VideoFacts
from agent.schemas.guardrails import AssetSpec

ASSET = uuid.UUID("00000000-0000-4000-8000-000000000001")
MEDIA = uuid.UUID("00000000-0000-4000-8000-000000000002")
REVIEWED = date(2026, 9, 24)


def _spec(**values: object) -> AssetSpec:
    return AssetSpec(source="test", reviewed_at=REVIEWED, **values)  # type: ignore[arg-type]


def _image(**overrides: object) -> ImageFacts:
    base: dict[str, object] = {
        "format": "JPEG",
        "media_type": "image/jpeg",
        "width": 1200,
        "height": 628,
        "mode": "RGB",
        "bytes": 180_000,
        "sha256": "0" * 64,
        "has_alpha": False,
        "exif": False,
        "gps": False,
        "xmp": False,
        "icc_profile": False,
    }
    base.update(overrides)
    return ImageFacts(**base)  # type: ignore[arg-type]


def _video(**overrides: object) -> VideoFacts:
    base: dict[str, object] = {
        "container": "mov,mp4,m4a,3gp,3g2,mj2",
        "media_type": "video/mp4",
        "codec": "h264",
        "pix_fmt": "yuv420p",
        "width": 1920,
        "height": 1080,
        "duration_ms": 15_000,
        "fps": 30.0,
        "packets": 450,
        "has_audio": True,
        "bytes": 4_000_000,
        "sha256": "0" * 64,
        "profile": "High",
        "audio_codec": "aac",
    }
    base.update(overrides)
    return VideoFacts(**base)  # type: ignore[arg-type]


def _by(checks: list[conformance.Check], constraint: str) -> list[conformance.Check]:
    return [check for check in checks if check.constraint == constraint]


# ---------------------------------------------------------------------------
# character counts — measured by the linter's own counter
# ---------------------------------------------------------------------------


def test_a_31_character_headline_fails_its_30_character_limit() -> None:
    headline = "SDS software for every site now"  # 31
    assert len(headline) == 31

    (check,) = conformance.text_checks(ASSET, [headline], _spec(max_chars=30))

    assert check.constraint == "max_chars"
    assert (check.expected, check.measured) == (30, 31)
    assert check.source == "lint"
    assert check.verdict == "fail"
    assert check.asset_id == ASSET and check.media_id is None


def test_every_line_of_a_multi_line_asset_is_counted() -> None:
    checks = conformance.text_checks(
        ASSET, ["Pricing", "Plans for teams of any size", "x" * 26], _spec(max_chars=25)
    )
    assert [c.verdict for c in checks] == ["pass", "fail", "fail"]
    assert [c.measured for c in checks] == [7, 27, 26]


def test_a_spec_without_a_character_limit_counts_nothing() -> None:
    assert conformance.text_checks(ASSET, ["anything"], _spec(max_count=15)) == []


# ---------------------------------------------------------------------------
# images — measured by Pillow's decode
# ---------------------------------------------------------------------------


def test_an_image_that_meets_its_spec_passes_every_check() -> None:
    checks = conformance.image_checks(
        ASSET,
        MEDIA,
        _image(),
        _spec(ratio="1.91:1", min_px="600x314", max_bytes=5_242_880),
        declared_media_type="image/jpeg",
        tolerance=0.005,
    )
    assert {c.constraint for c in checks} == {"min_px", "ratio", "max_bytes", "format"}
    assert {c.verdict for c in checks} == {"pass"}
    assert {c.source for c in checks} == {"pillow"}
    assert all(c.media_id == MEDIA for c in checks)


def test_an_image_under_its_minimum_size_fails_min_px() -> None:
    (check,) = _by(
        conformance.image_checks(
            ASSET,
            MEDIA,
            _image(width=599, height=314),
            _spec(ratio="1.91:1", min_px="600x314"),
            declared_media_type="image/jpeg",
            tolerance=0.005,
        ),
        "min_px",
    )
    assert (check.expected, check.measured, check.verdict) == ("600x314", "599x314", "fail")


def test_a_stretched_ratio_fails_and_one_inside_the_tolerance_passes() -> None:
    def ratio(width: int, height: int) -> str:
        (check,) = _by(
            conformance.image_checks(
                ASSET,
                MEDIA,
                _image(width=width, height=height),
                _spec(ratio="1:1"),
                declared_media_type="image/jpeg",
                tolerance=0.005,
            ),
            "ratio",
        )
        return check.verdict

    assert ratio(1200, 1200) == "pass"
    assert ratio(1200, 1197) == "pass"  # 0.25 % off
    assert ratio(1200, 1100) == "fail"


def test_an_image_over_its_byte_cap_fails_max_bytes() -> None:
    (check,) = _by(
        conformance.image_checks(
            ASSET,
            MEDIA,
            _image(bytes=5_242_881),
            _spec(max_bytes=5_242_880),
            declared_media_type="image/jpeg",
            tolerance=0.005,
        ),
        "max_bytes",
    )
    assert check.verdict == "fail" and check.measured == 5_242_881


def test_a_file_that_is_not_what_its_row_declares_fails_format() -> None:
    (check,) = _by(
        conformance.image_checks(
            ASSET,
            MEDIA,
            _image(format="PNG", media_type="image/png"),
            _spec(),
            declared_media_type="image/jpeg",
            tolerance=0.005,
        ),
        "format",
    )
    assert (check.expected, check.measured, check.verdict) == ("image/jpeg", "image/png", "fail")


# ---------------------------------------------------------------------------
# video — measured by ffprobe
# ---------------------------------------------------------------------------


def test_a_video_that_meets_its_spec_passes_every_check() -> None:
    checks = conformance.video_checks(
        ASSET,
        MEDIA,
        _video(),
        _spec(ratio="16:9", min_px="1280x720", min_duration_s=10, max_duration_s=30),
        declared_media_type="video/mp4",
        tolerance=0.005,
        fps=30,
    )
    assert {c.constraint for c in checks} == {
        "min_px",
        "ratio",
        "min_duration_s",
        "max_duration_s",
        "fps",
        "codec",
        "audio_codec",
        "format",
    }
    assert {c.verdict for c in checks} == {"pass"}, [c for c in checks if c.verdict != "pass"]
    assert {c.source for c in checks} == {"ffprobe"}


def test_duration_fps_and_codec_are_each_their_own_failure() -> None:
    checks = conformance.video_checks(
        ASSET,
        MEDIA,
        _video(duration_ms=31_000, fps=25.0, codec="hevc", profile="Main", audio_codec=None),
        _spec(ratio="16:9", min_duration_s=10, max_duration_s=30),
        declared_media_type="video/mp4",
        tolerance=0.005,
        fps=30,
    )
    failed = {c.constraint: c for c in checks if c.verdict == "fail"}
    assert set(failed) == {"max_duration_s", "fps", "codec", "audio_codec"}
    assert failed["max_duration_s"].measured == 31.0
    assert failed["fps"].measured == 25.0
    assert failed["codec"].measured == "hevc/main/yuv420p"
    assert failed["codec"].expected == "h264/high/yuv420p"


def test_a_video_without_a_spec_still_checks_what_the_encoder_promises() -> None:
    checks = conformance.video_checks(
        ASSET,
        MEDIA,
        _video(),
        None,
        declared_media_type="video/mp4",
        tolerance=0.005,
        fps=30,
    )
    assert {c.constraint for c in checks} == {"fps", "codec", "audio_codec", "format"}


# ---------------------------------------------------------------------------
# 4.6.4 — a rendered combination against the pin's spec sheet
# ---------------------------------------------------------------------------

SPECS = {
    "headline": _spec(max_chars=30, min_count=3, max_count=15),
    "description": _spec(max_chars=90, min_count=2, max_count=4),
    "path": _spec(max_chars=15),
}


def _element(key: str, surface: str, text: str) -> conformance.Element:
    return conformance.Element(key=key, asset_id=ASSET, surface=surface, text=text)


def test_a_31_character_headline_in_a_combination_is_a_mismatch() -> None:
    diff = conformance.spec_diff(
        [
            _element("headline_1", "rsa_headline", "x" * 31),
            _element("headline_2", "rsa_headline", "y" * 30),
        ],
        counts={"headline": 3, "description": 2},
        specs=SPECS,
    )

    assert [m.model_dump(mode="json") for m in diff.mismatched] == [
        {
            "element": "headline_1",
            "asset_id": str(ASSET),
            "constraint": "max_chars",
            "expected": 30,
            "measured": 31,
        }
    ]
    assert diff.missing == [] and diff.extra == [] and diff.unchecked == []
    assert diff.blocking


def test_an_ad_short_of_its_minimum_count_is_missing_and_one_over_its_maximum_is_extra() -> None:
    diff = conformance.spec_diff(
        [_element("headline_1", "rsa_headline", "Fits")],
        counts={"headline": 2, "description": 5},
        specs=SPECS,
    )

    assert [(m.asset_type, m.constraint, m.expected, m.measured) for m in diff.missing] == [
        ("headline", "min_count", 3, 2)
    ]
    assert [(e.asset_type, e.constraint, e.expected, e.measured) for e in diff.extra] == [
        ("description", "max_count", 4, 5)
    ]
    assert diff.blocking


def test_a_surface_with_no_spec_is_unchecked_never_a_pass() -> None:
    diff = conformance.spec_diff(
        [_element("path_1", "rsa_path", "safety"), _element("x_1", "sitelink", "Pricing")],
        counts={"headline": 3, "description": 2},
        specs=SPECS,
    )

    assert [(u.element, u.surface) for u in diff.unchecked] == [("x_1", "sitelink")]
    assert not diff.blocking


def test_a_combination_inside_every_spec_has_an_empty_diff() -> None:
    diff = conformance.spec_diff(
        [
            _element("headline_1", "rsa_headline", "y" * 30),
            _element("description_1", "rsa_description", "z" * 90),
            _element("path_1", "rsa_path", "p" * 15),
        ],
        counts={"headline": 3, "description": 2, "path": 1},
        specs=SPECS,
    )

    assert diff.mismatched == diff.missing == diff.extra == diff.unchecked == []
    assert not diff.blocking
