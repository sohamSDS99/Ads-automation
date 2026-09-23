"""`content_constants.yaml`, loaded once and validated hard.

Stage 03 PRD §9.6 and global law 25: Google's asset specs, the image
thresholds, the claim-detector families and our internal minimums live in one
YAML file with a `source` and a `reviewed_at` per value. Never in a prompt,
never in a function body. **A constant without a source fails startup**, and
the failure names the key.

That rule earns its keep here more than it did in Stage 02. Every `asset_specs`
value ships `source: unverified` (Q7), and a rulebook that enforces an
unverified character limit as though Google had published it is worse than one
that enforces nothing — people trust a rule that cites an authority. Carrying
`source` all the way onto the finding is what keeps that honest.

The file's `version` is stamped into every `RuleSet`, so any verdict any asset
ever received can be re-derived against the constants that produced it.
Per-project overrides (`Project.settings.content_overrides`) are merged by
`merged()`, which *changes the version string* — two projects running the same
rule under different overrides must not write rulesets claiming identical
provenance.

Deliberately a sibling of `guardrails/`, not a member of it: this module reads
a file, and `guardrails/` may not. The constants cross the boundary as an
argument.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent.schemas.guardrails import AssetSpec, AssetSpecSheet, DetectorSpec

#: Shipped alongside this module so the file travels with the code that reads
#: it. `Dockerfile` copies `src/`, so there is nothing further to install.
CONSTANTS_PATH = Path(__file__).with_name("content_constants.yaml")


class ContentConstantsError(RuntimeError):
    """The constants file cannot produce a usable set of content constants."""


class _Node(BaseModel):
    """Closed and frozen. A typo like `sources:` must fail, not be ignored."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class NumberConstant(_Node):
    """One threshold, with the provenance that makes it auditable."""

    value: float
    source: str = Field(min_length=1)
    reviewed_at: date

    def as_int(self) -> int:
        """The value where a count is meant. Refuses a fraction rather than truncating."""
        rounded = round(self.value)
        if abs(self.value - rounded) > 1e-9:
            raise ContentConstantsError(f"{self.value} is not a whole number")
        return int(rounded)


class ListConstant(_Node):
    """A constant whose value is a set of labels rather than a number."""

    value: tuple[str, ...] = Field(min_length=1)
    source: str = Field(min_length=1)
    reviewed_at: date


class ImagePolicyConstants(_Node):
    search_image_text_coverage_max: NumberConstant
    logo_match_score_min: NumberConstant
    ocr_working_width_px: NumberConstant
    #: Bounds on how much of an image the matched logo may occupy (§11, 3.4.3).
    #: Both sides, because a logo can be wrong by being too big as well as too
    #: small, and §11's `logo_area_ratio` metric has no meaning without them.
    logo_area_ratio_max: NumberConstant
    logo_area_ratio_min: NumberConstant


class ClaimsConstants(_Node):
    default_expiry_days: NumberConstant
    quantified_expiry_days: NumberConstant
    #: Trigram similarity floor for matching a candidate span to a registered
    #: claim's surface forms (PRD §9.3). Raising it denies more; lowering it
    #: licenses more. It is a constant precisely because that trade-off is a
    #: decision somebody should have to review, not a literal in a matcher.
    match_threshold: NumberConstant
    high_risk_types: ListConstant


class SignatureConstants(_Node):
    reauth_ttl_seconds: NumberConstant
    max_claims_per_signature: NumberConstant


class AmendmentConstants(_Node):
    confidence_floor: NumberConstant


class ReviewConstants(_Node):
    guideline_review_days: NumberConstant


class OffersConstants(_Node):
    countdown_max_extensions: NumberConstant
    staleness_warning_days: NumberConstant


class ClaimDetectorConstants(_Node):
    """The claim-shaped-language families of PRD §9.3.

    One `source`/`reviewed_at` for the family set rather than per pattern: they
    are reviewed together, by one person, in one sitting, and eight copies of
    the same date would be eight chances for them to drift apart.
    """

    source: str = Field(min_length=1)
    reviewed_at: date
    detectors: tuple[DetectorSpec, ...] = Field(min_length=1)


class ContentConstants(BaseModel):
    """The whole file, validated. Immutable — `merged()` returns a new one."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(min_length=1)
    #: `campaign_type -> asset_type -> AssetSpec`. Left as a mapping rather
    #: than named fields because the campaign types Google offers change on
    #: Google's schedule, and a new one should be a YAML edit.
    asset_specs: dict[str, dict[str, AssetSpec]]
    image_policy: ImagePolicyConstants
    claims: ClaimsConstants
    signature: SignatureConstants
    amendment: AmendmentConstants
    review: ReviewConstants
    offers: OffersConstants
    claim_detectors: ClaimDetectorConstants

    def get(self, dotted: str) -> NumberConstant | ListConstant:
        """`constants.get("claims.match_threshold")`. Raises on an unknown key."""
        group_name, _, key = dotted.partition(".")
        group = getattr(self, group_name, None)
        if not isinstance(group, _Node) or not key:
            raise ContentConstantsError(f"unknown constants group in {dotted!r}")
        constant = getattr(group, key, None)
        if not isinstance(constant, NumberConstant | ListConstant):
            raise ContentConstantsError(f"unknown constant {dotted!r}")
        return constant

    def value(self, dotted: str) -> float:
        constant = self.get(dotted)
        if not isinstance(constant, NumberConstant):
            raise ContentConstantsError(f"{dotted!r} is not a numeric constant")
        return constant.value

    def asset_sheet(self) -> AssetSpecSheet:
        """The specs as they are carried in a compiled `RuleSet`."""
        return AssetSpecSheet(specs=self.asset_specs)

    def detectors(self) -> tuple[DetectorSpec, ...]:
        return self.claim_detectors.detectors

    def merged(self, overrides: Mapping[str, Any] | None) -> ContentConstants:
        """Apply `Project.settings.content_overrides` and re-validate.

        Accepts dotted keys (`{"claims.match_threshold": 0.9}`) or the nested
        shape a settings blob naturally has. An override replaces the `value`
        and nothing else; `source` becomes `project_override` so the provenance
        still reads true, and `reviewed_at` stays the date somebody last
        checked the underlying figure.

        The returned object's `version` carries a digest of the overrides, and
        that is not cosmetic: `constants_version` is how a `RuleSet` claims
        which constants produced it, and two projects linting the same copy
        under *different* thresholds would otherwise mint rulesets claiming
        identical provenance for different verdicts.

        Asset-spec overrides are deliberately not supported. They are three
        levels deep and multi-field, nothing in this phase needs them, and an
        unknown key raising beats a silently-ignored one — S3-P1 ships the
        shape Stage 02 already proved and leaves the rest to whoever needs it.
        """
        if not overrides:
            return self

        flat = _flatten(overrides)
        payload = self.model_dump(mode="json")
        for dotted, value in flat.items():
            constant = self.get(dotted)  # refuses an unknown key before anything is written
            group_name, _, key = dotted.partition(".")
            if isinstance(constant, ListConstant):
                if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                    raise ContentConstantsError(
                        f"override {dotted!r} must be a list of strings, got {value!r}"
                    )
                payload[group_name][key]["value"] = list(value)
            else:
                if not isinstance(value, int | float) or isinstance(value, bool):
                    raise ContentConstantsError(
                        f"override {dotted!r} must be a number, got {value!r}"
                    )
                payload[group_name][key]["value"] = float(value)
            payload[group_name][key]["source"] = "project_override"

        digest = hashlib.sha256(
            json.dumps(flat, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        payload["version"] = f"{self.version}+ovr.{digest[:8]}"
        return ContentConstants.model_validate(payload)


def _flatten(overrides: Mapping[str, Any]) -> dict[str, Any]:
    """Normalise nested or dotted overrides into one dotted mapping."""
    flat: dict[str, Any] = {}
    for key, value in overrides.items():
        if isinstance(value, Mapping):
            for inner, inner_value in value.items():
                flat[f"{key}.{inner}"] = inner_value
        else:
            flat[key] = value
    return flat


def _describe(error: ValidationError) -> str:
    """Render a validation failure as `group.key.field: message`, one per line.

    The whole point of this module is that a bad constant names itself, so the
    Pydantic `loc` tuple is the message rather than a detail inside it.
    """
    lines = []
    for detail in error.errors():
        location = ".".join(str(part) for part in detail["loc"]) or "<root>"
        lines.append(f"  {location}: {detail['msg']}")
    return "\n".join(lines)


def load_content_constants(path: Path | None = None) -> ContentConstants:
    """Read and validate the constants file. Raises `ContentConstantsError` on anything."""
    source = path or CONSTANTS_PATH
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ContentConstantsError(f"{source} is missing") from exc
    except yaml.YAMLError as exc:
        raise ContentConstantsError(f"{source} is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise ContentConstantsError(f"{source} must be a mapping, got {type(raw).__name__}")

    try:
        return ContentConstants.model_validate(raw)
    except ValidationError as exc:
        raise ContentConstantsError(f"{source.name} is invalid:\n{_describe(exc)}") from exc


@lru_cache(maxsize=1)
def get_content_constants() -> ContentConstants:
    """The process-wide constants. Cached like `get_settings()`."""
    return load_content_constants()
