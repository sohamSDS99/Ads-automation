"""`creative_constants.yaml`, loaded once and validated hard (Stage 04 PRD §9.5).

The same discipline as Stage 02 §9.3 and Stage 03 §9.6, and law 25: every
constant carries `value`, `source` and `reviewed_at`. **A constant without a
source fails startup, and the failure names the key** — `main.lifespan` and
`worker.startup` both load this before they do anything else, so a process
that would write a package citing a threshold nobody can check never comes up.

Every constant is typed by what it is. A count is a strict integer, so
`value: true` for `headline_pool_size` is refused rather than read as 1; a
ratio is a float; a label list is a tuple of strings. The groups are closed,
so a typo like `sources:` fails instead of leaving the real key unset.

Google's character limits, asset counts, ratios, pixel minimums and file-size
caps are deliberately absent: they come from the pinned `RuleSet.asset_specs`,
and a second copy here would be a second answer to "how long may a headline
be".

`version` is stamped on every `CreativeInput` (`constants_version`) and every
package. Per-project overrides (`Project.settings.creative_overrides`, PRD
§7.1) are applied by `merged()`, which changes that version string — two
projects producing copy under different thresholds must not claim identical
provenance for it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
)

if TYPE_CHECKING:  # pragma: no cover
    from agent.db.models import Project
    from agent.media.constants import MediaConstants

#: Shipped next to this module, so the file travels with the code that reads it.
CONSTANTS_PATH = Path(__file__).with_name("creative_constants.yaml")

#: Where a project's overrides live (PRD §7.1).
OVERRIDES_KEY = "creative_overrides"

#: What an override writes into `source`, so the provenance still reads true.
OVERRIDE_SOURCE = "project_override"


class CreativeConstantsError(RuntimeError):
    """The constants file cannot produce a usable set of creative constants."""


class _Constant(BaseModel):
    """One value and the provenance that makes it auditable. Closed and frozen."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: StrictStr = Field(min_length=1)
    reviewed_at: date

    @field_validator("source")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a blank source is not a source")
        return value


class IntConstant(_Constant):
    value: StrictInt


class FloatConstant(_Constant):
    value: StrictFloat


class StrConstant(_Constant):
    value: StrictStr = Field(min_length=1)


class BoolConstant(_Constant):
    value: StrictBool


class ListConstant(_Constant):
    value: tuple[StrictStr, ...] = Field(min_length=1)


class QuotaConstant(_Constant):
    """`copy.headline_quotas`: a category → minimum count map."""

    value: dict[StrictStr, StrictInt] = Field(min_length=1)

    @field_validator("value")
    @classmethod
    def _non_negative(cls, value: dict[str, int]) -> dict[str, int]:
        negative = sorted(key for key, count in value.items() if count < 0)
        if negative:
            raise ValueError(f"quotas cannot be negative: {', '.join(negative)}")
        return value


Constant = IntConstant | FloatConstant | StrConstant | BoolConstant | ListConstant | QuotaConstant


class _Group(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CopyConstants(_Group):
    headline_pool_size: IntConstant
    headline_quotas: QuotaConstant
    description_pool_size: IntConstant
    near_duplicate_trigram: FloatConstant
    #: 1 − similarity.
    variant_min_distance: FloatConstant
    pair_repair_rounds: IntConstant
    #: `TaskClass.COPYWRITE`'s temperature (PRD §9.6: "temperature from constants").
    temperature_copywrite: FloatConstant


class LandingConstants(_Group):
    message_match_min: FloatConstant
    viewport_mobile: StrConstant
    viewport_desktop: StrConstant


class OffersConstants(_Group):
    offer_max_age_days: IntConstant


class ExtrasConstants(_Group):
    snippet_headers: ListConstant
    lead_form_question_types: ListConstant
    price_types: ListConstant
    lead_form_cta_types: ListConstant
    #: Completions kept per lead-form question added (S4-P8, beyond §9.5).
    lead_form_field_retention: FloatConstant


class MediaGroup(_Group):
    candidates_per_concept: IntConstant
    ratio_tolerance: FloatConstant
    crop_min_saliency_retained: FloatConstant
    jpeg_quality_floor: IntConstant
    reference_max_bytes: IntConstant
    image_tokens_per_megapixel: IntConstant
    semaphore_image: IntConstant
    semaphore_video: IntConstant
    video_poll_initial_s: IntConstant
    video_poll_max_s: IntConstant
    video_job_timeout_s: IntConstant
    degrade_ladder: ListConstant


class VideoConstants(_Group):
    brand_within_ms: IntConstant
    end_card_ms: IntConstant
    caption_height_pct: FloatConstant
    caption_ocr_min_similarity: FloatConstant
    target_fps: IntConstant
    loudness_lufs: IntConstant
    generate_audio_default: BoolConstant


class LogoConstants(_Group):
    permitted_surfaces: ListConstant
    #: The logo's width as a share of the rendition's width (§9.4 item 3 sets
    #: only a floor, `min_width_px`).
    width_ratio: FloatConstant


class ExceptionsConstants(_Group):
    max_exceptions_per_run: IntConstant


class RetentionConstants(_Group):
    unreleased_media_days: IntConstant
    superseded_package_days: IntConstant


class PreviewConstants(_Group):
    serp_template_version: StrConstant


class CreativeConstants(BaseModel):
    """The whole file, validated. Immutable — `merged()` returns a new one."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: StrictStr = Field(min_length=1)
    copy_: CopyConstants = Field(alias="copy")
    landing: LandingConstants
    offers: OffersConstants
    extras: ExtrasConstants
    media: MediaGroup
    video: VideoConstants
    logo: LogoConstants
    exceptions: ExceptionsConstants
    retention: RetentionConstants
    preview: PreviewConstants

    # -- reads ---------------------------------------------------------------

    def get(self, dotted: str) -> Constant:
        """`constants.get("copy.headline_pool_size")`. Raises on an unknown key."""
        group = self._group(dotted)
        _, _, key = dotted.partition(".")
        constant = getattr(group, key, None) if key else None
        if not isinstance(constant, _Constant):
            raise CreativeConstantsError(f"unknown creative constant {dotted!r}")
        return constant  # type: ignore[return-value]

    def keys(self) -> Iterator[str]:
        """Every constant's dotted key, in file order."""
        for group_name, field in type(self).model_fields.items():
            if group_name == "version":
                continue
            name = field.alias or group_name
            group = getattr(self, group_name)
            for key in type(group).model_fields:
                yield f"{name}.{key}"

    def _group(self, dotted: str) -> _Group:
        group_name, _, _ = dotted.partition(".")
        attribute = "copy_" if group_name == "copy" else group_name
        group = getattr(self, attribute, None) if group_name != "version" else None
        if not isinstance(group, _Group):
            raise CreativeConstantsError(f"unknown creative constants group in {dotted!r}")
        return group

    # -- overrides -----------------------------------------------------------

    def merged(self, overrides: Mapping[str, Any] | None) -> CreativeConstants:
        """Apply `Project.settings.creative_overrides` and re-validate.

        Dotted (`{"copy.headline_pool_size": 30}`) or nested
        (`{"copy": {"headline_pool_size": 30}}`). An override replaces `value`
        and nothing else, is validated against the constant's own type, and
        records `source: project_override`. The returned `version` carries a
        digest of the overrides for the reason the module docstring gives.
        """
        if not overrides:
            return self
        flat = _flatten(overrides)
        payload = self.model_dump(mode="python", by_alias=True)
        for dotted, value in flat.items():
            constant = self.get(dotted)
            candidate = float(value) if _widens_to_float(constant, value) else value
            try:
                replaced = type(constant).model_validate(
                    {
                        "value": candidate,
                        "source": OVERRIDE_SOURCE,
                        "reviewed_at": constant.reviewed_at,
                    }
                )
            except ValidationError as exc:
                raise CreativeConstantsError(
                    f"override {dotted!r} is invalid:\n{_describe(exc)}"
                ) from exc
            group_name, _, key = dotted.partition(".")
            payload[group_name][key] = replaced.model_dump(mode="python")
        digest = hashlib.sha256(
            json.dumps(flat, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()
        payload["version"] = f"{self.version}+ovr.{digest[:8]}"
        return CreativeConstants.model_validate(payload)

    # -- projections ---------------------------------------------------------

    def media_constants(self) -> MediaConstants:
        """The `media:` slice the gateway and `calc/media.py` run on."""
        from agent.media.constants import MediaConstants

        media = self.media
        return MediaConstants(
            version=self.version,
            candidates_per_concept=media.candidates_per_concept.value,
            ratio_tolerance=media.ratio_tolerance.value,
            crop_min_saliency_retained=media.crop_min_saliency_retained.value,
            image_tokens_per_megapixel=media.image_tokens_per_megapixel.value,
            semaphore_image=media.semaphore_image.value,
            semaphore_video=media.semaphore_video.value,
            video_poll_initial_s=float(media.video_poll_initial_s.value),
            video_poll_max_s=float(media.video_poll_max_s.value),
            video_job_timeout_s=float(media.video_job_timeout_s.value),
            degrade_ladder=media.degrade_ladder.value,
            generate_audio_default=self.video.generate_audio_default.value,
            reference_max_bytes=media.reference_max_bytes.value,
        )


def _widens_to_float(constant: Constant, value: Any) -> bool:
    """`temperature_copywrite: 1` is a float override, not a type error."""
    return (
        isinstance(constant, FloatConstant)
        and isinstance(value, int)
        and not isinstance(value, bool)
    )


def _flatten(overrides: Mapping[str, Any]) -> dict[str, Any]:
    """Normalise nested or dotted overrides into one dotted mapping."""
    flat: dict[str, Any] = {}
    for key, value in overrides.items():
        if "." not in key and isinstance(value, Mapping):
            for inner, inner_value in value.items():
                flat[f"{key}.{inner}"] = inner_value
        else:
            flat[key] = value
    return flat


def _describe(error: ValidationError) -> str:
    """One `group.key.field: message` line per failure — the key is the message."""
    lines = []
    for detail in error.errors():
        location = ".".join(str(part) for part in detail["loc"]) or "<root>"
        lines.append(f"  {location}: {detail['msg']}")
    return "\n".join(lines)


def load_creative_constants(path: Path | None = None) -> CreativeConstants:
    """Read and validate the file. Raises `CreativeConstantsError` on anything."""
    source = path or CONSTANTS_PATH
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CreativeConstantsError(f"{source} is missing") from exc
    except yaml.YAMLError as exc:
        raise CreativeConstantsError(f"{source} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise CreativeConstantsError(f"{source} must be a mapping, got {type(raw).__name__}")
    try:
        return CreativeConstants.model_validate(raw)
    except ValidationError as exc:
        raise CreativeConstantsError(f"{source.name} is invalid:\n{_describe(exc)}") from exc


@lru_cache(maxsize=1)
def get_creative_constants() -> CreativeConstants:
    """The process-wide constants. Cached like `get_settings()`."""
    return load_creative_constants(CONSTANTS_PATH)


def creative_constants_for(project: Project) -> CreativeConstants:
    """The constants one project's creative runs execute under."""
    settings = project.settings or {}
    overrides = settings.get(OVERRIDES_KEY)
    if overrides is not None and not isinstance(overrides, Mapping):
        raise CreativeConstantsError(
            f"project.settings.{OVERRIDES_KEY} must be an object of constant → value"
        )
    return get_creative_constants().merged(overrides)
