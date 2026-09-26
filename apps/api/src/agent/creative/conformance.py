"""4.6.1 spec conformance — one check per measured constraint (Stage 04 PRD §11 4.6.1).

`checks[]{asset_id, constraint, expected, measured, source, verdict}` for every
character count, pixel size, ratio, byte size, format, duration, fps and codec.
This module turns measurements into checks and holds no I/O; node 4.6.1 reads
the rows and the files and hands the facts in.

Nothing is re-specified here (Law 33). Limits come from the pin's spec sheet
(`RuleSet.asset_specs`); character counts are measured by the linter's own
counter (`guardrails.matchers.assets.measure` — source `lint`); pixels, bytes
and format by Pillow's full decode (`postprod.probe.probe_image` — `pillow`);
duration, fps and codecs by ffprobe (`postprod.probe.probe_video` —
`ffprobe`), against what the encoder promises (`postprod.verify`) and the
frame rate the constants pin. `format` is the file against what its row
declares: the spec sheet names no format, and a file that is not what the
package says it is is the failure worth catching.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence

from agent.guardrails.matchers.assets import measure
from agent.postprod.image import parse_px, same_ratio
from agent.postprod.probe import ImageFacts, VideoFacts
from agent.postprod.verify import AUDIO_CODEC, PIXEL_FORMAT, VIDEO_CODEC, VIDEO_PROFILE
from agent.schemas.creative_qa import ConformanceCheck, ConformanceSource, Constraint
from agent.schemas.guardrails import SURFACE_ASSET_TYPES, AssetSpec

Check = ConformanceCheck

#: The codec triple the encoder writes, in the order a person reads it.
EXPECTED_CODEC = f"{VIDEO_CODEC}/{VIDEO_PROFILE}/{PIXEL_FORMAT}"


def text_checks(asset_id: uuid.UUID, lines: Sequence[str], spec: AssetSpec) -> list[Check]:
    """Every line against the spec's character limit, counted as the linter counts."""
    if spec.max_chars is None:
        return []
    checks: list[Check] = []
    for line in lines:
        count = measure(line, "chars")
        checks.append(
            _check(
                asset_id, None, "max_chars", spec.max_chars, count, "lint", count <= spec.max_chars
            )
        )
    return checks


def image_checks(
    asset_id: uuid.UUID,
    media_id: uuid.UUID,
    facts: ImageFacts,
    spec: AssetSpec,
    *,
    declared_media_type: str,
    tolerance: float,
) -> list[Check]:
    checks = _frame(asset_id, media_id, facts.width, facts.height, spec, "pillow", tolerance)
    if spec.max_bytes is not None:
        checks.append(
            _check(
                asset_id,
                media_id,
                "max_bytes",
                spec.max_bytes,
                facts.bytes,
                "pillow",
                facts.bytes <= spec.max_bytes,
            )
        )
    checks.append(
        _check(
            asset_id,
            media_id,
            "format",
            declared_media_type,
            facts.media_type,
            "pillow",
            facts.media_type == declared_media_type,
        )
    )
    return checks


def video_checks(
    asset_id: uuid.UUID,
    media_id: uuid.UUID,
    facts: VideoFacts,
    spec: AssetSpec | None,
    *,
    declared_media_type: str,
    tolerance: float,
    fps: int,
) -> list[Check]:
    """The spec's frame, bytes and duration window — and, with or without a
    spec, what the encoder promises: the frame rate, the codecs, the container."""
    checks: list[Check] = []
    seconds = facts.duration_ms / 1000
    if spec is not None:
        checks.extend(
            _frame(asset_id, media_id, facts.width, facts.height, spec, "ffprobe", tolerance)
        )
        if spec.max_bytes is not None:
            checks.append(
                _check(
                    asset_id,
                    media_id,
                    "max_bytes",
                    spec.max_bytes,
                    facts.bytes,
                    "ffprobe",
                    facts.bytes <= spec.max_bytes,
                )
            )
        if spec.min_duration_s is not None:
            checks.append(
                _check(
                    asset_id,
                    media_id,
                    "min_duration_s",
                    spec.min_duration_s,
                    seconds,
                    "ffprobe",
                    seconds >= spec.min_duration_s,
                )
            )
        if spec.max_duration_s is not None:
            checks.append(
                _check(
                    asset_id,
                    media_id,
                    "max_duration_s",
                    spec.max_duration_s,
                    seconds,
                    "ffprobe",
                    seconds <= spec.max_duration_s,
                )
            )
    measured_fps = facts.fps if facts.fps is not None else 0.0
    checks.append(
        _check(
            asset_id,
            media_id,
            "fps",
            fps,
            measured_fps,
            "ffprobe",
            facts.fps is not None and abs(facts.fps - fps) <= 1e-6,
        )
    )
    codec = f"{facts.codec}/{(facts.profile or '').lower()}/{facts.pix_fmt or ''}"
    checks.append(
        _check(
            asset_id, media_id, "codec", EXPECTED_CODEC, codec, "ffprobe", codec == EXPECTED_CODEC
        )
    )
    audio = facts.audio_codec if facts.has_audio and facts.audio_codec else "none"
    checks.append(
        _check(
            asset_id, media_id, "audio_codec", AUDIO_CODEC, audio, "ffprobe", audio == AUDIO_CODEC
        )
    )
    checks.append(
        _check(
            asset_id,
            media_id,
            "format",
            declared_media_type,
            facts.media_type,
            "ffprobe",
            facts.media_type == declared_media_type,
        )
    )
    return checks


# ---------------------------------------------------------------------------
# which spec a measured thing answers to
# ---------------------------------------------------------------------------


def text_spec(specs: Mapping[str, AssetSpec], surface: str) -> AssetSpec | None:
    """The spec of the asset type the linter maps this surface to (`SURFACE_ASSET_TYPES`)."""
    asset_type = SURFACE_ASSET_TYPES.get(surface)
    return specs.get(asset_type) if asset_type is not None else None


def media_spec(
    specs: Mapping[str, AssetSpec],
    ratio: str | None,
    *,
    kind: str,
    tolerance: float,
) -> tuple[str, AssetSpec] | None:
    """The spec a file of this `ratio` was made for: the campaign type's image,
    logo or video asset types with a ratio, matched within the tolerance.

    Sorted by name — a spec sheet round-trips through JSONB, which does not keep
    the file's key order (S4-P10), and the answer must not depend on it.
    """
    if not ratio:
        return None
    for asset_type in sorted(specs):
        spec = specs[asset_type]
        if spec.ratio is None or _family(asset_type) != kind:
            continue
        if same_ratio(spec.ratio, ratio, tolerance=tolerance):
            return asset_type, spec
    return None


def _family(asset_type: str) -> str:
    if "logo" in asset_type:
        return "logo"
    if "video" in asset_type:
        return "video"
    return "image"


def _frame(
    asset_id: uuid.UUID,
    media_id: uuid.UUID,
    width: int,
    height: int,
    spec: AssetSpec,
    source: ConformanceSource,
    tolerance: float,
) -> list[Check]:
    checks: list[Check] = []
    if spec.min_px is not None:
        min_w, min_h = parse_px(spec.min_px)
        checks.append(
            _check(
                asset_id,
                media_id,
                "min_px",
                spec.min_px,
                f"{width}x{height}",
                source,
                width >= min_w and height >= min_h,
            )
        )
    if spec.ratio is not None:
        checks.append(
            _check(
                asset_id,
                media_id,
                "ratio",
                spec.ratio,
                f"{width}:{height}",
                source,
                height > 0 and same_ratio(spec.ratio, f"{width}:{height}", tolerance=tolerance),
            )
        )
    return checks


def _check(
    asset_id: uuid.UUID,
    media_id: uuid.UUID | None,
    constraint: Constraint,
    expected: str | int | float,
    measured: str | int | float,
    source: ConformanceSource,
    passed: bool,
) -> Check:
    return Check(
        asset_id=asset_id,
        media_id=media_id,
        constraint=constraint,
        expected=expected,
        measured=measured,
        source=source,
        verdict="pass" if passed else "fail",
    )
