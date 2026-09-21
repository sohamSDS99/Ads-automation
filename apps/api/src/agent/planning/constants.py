"""`planning_constants.yaml`, loaded once and validated hard.

PRD §9.3 and global law 15: Google's thresholds, our internal minimums and the
statistics defaults live in one YAML file with a `source` and a `reviewed_at`
per value. Never in a prompt, never in a function body. **A constant without a
source fails startup**, and the failure names the key — a plan built on an
unattributable threshold is a plan nobody can check.

The file's `version` is stamped into every `PlanCalc` row through
`CalcResult.calc_version`, so any number in any plan can be re-derived against
the constants it was actually built with. Per-project overrides
(`Project.settings.planning_overrides`) are merged at run start by `merged()`,
which *changes the version string* — see the comment there for why that matters
more than it looks.
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

#: Shipped alongside this module so the file travels with the code that reads
#: it. `Dockerfile` copies `src/`, so there is nothing further to install.
CONSTANTS_PATH = Path(__file__).with_name("planning_constants.yaml")


class ConstantsError(RuntimeError):
    """The constants file cannot produce a usable set of planning constants."""


class Constant(BaseModel):
    """One threshold, with the provenance that makes it auditable.

    `extra="forbid"` is deliberate: a typo like `sources:` would otherwise pass
    validation as an ignored extra while `source` stayed missing, which is the
    exact failure this model exists to prevent.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: float
    source: str = Field(min_length=1)
    reviewed_at: date

    def as_int(self) -> int:
        """The value where a count is meant. Refuses a fraction rather than truncating."""
        rounded = round(self.value)
        if abs(self.value - rounded) > 1e-9:
            raise ConstantsError(f"{self.value} is not a whole number")
        return int(rounded)


class _Group(BaseModel):
    """A named group of constants. Unknown keys are an error, not a shrug."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class LearningConstants(_Group):
    tcpa_min_conv_30d: Constant
    troas_min_conv_30d: Constant
    absolute_min_conv_30d: Constant
    learning_period_days: Constant


class StructureConstants(_Group):
    min_keywords_per_ad_group: Constant
    max_keywords_per_ad_group: Constant
    min_ad_groups_per_campaign: Constant
    overlap_report_pct: Constant


class EconomicsConstants(_Group):
    target_cac_ratio: Constant
    safety_margin_pct: Constant


class BudgetConstants(_Group):
    min_monthly_per_campaign_usd: Constant
    experiment_reserve_pct: Constant
    cautious_step_pct: Constant
    aggressive_step_pct: Constant
    days_per_month: Constant


class ForecastConstants(_Group):
    impression_share_target_pct: Constant
    #: Used only when the account has no readable search history. See the file.
    default_ctr_pct: Constant
    default_cvr_pct: Constant


class ReallocationConstants(_Group):
    max_shift_pct: Constant
    lookback_days: Constant
    cooldown_days: Constant


class MeasurementConstants(_Group):
    tolerance_floor_pct: Constant
    tolerance_cap_pct: Constant
    modelled_conversion_pct: Constant
    action_stale_days: Constant
    click_upload_window_days: Constant
    manual_preparation_days: Constant


class TestConstants(_Group):
    alpha: Constant
    power: Constant
    min_mde_pct: Constant


class PlanningConstants(BaseModel):
    """The whole file, validated. Immutable — `merged()` returns a new one."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(min_length=1)
    learning: LearningConstants
    structure: StructureConstants
    economics: EconomicsConstants
    budget: BudgetConstants
    forecast: ForecastConstants
    reallocation: ReallocationConstants
    measurement: MeasurementConstants
    test: TestConstants

    def get(self, dotted: str) -> Constant:
        """`constants.get("learning.tcpa_min_conv_30d")`. Raises on an unknown key."""
        group_name, _, key = dotted.partition(".")
        group = getattr(self, group_name, None)
        if not isinstance(group, _Group) or not key:
            raise ConstantsError(f"unknown constants group in {dotted!r}")
        constant = getattr(group, key, None)
        if not isinstance(constant, Constant):
            raise ConstantsError(f"unknown constant {dotted!r}")
        return constant

    def value(self, dotted: str) -> float:
        return self.get(dotted).value

    def merged(self, overrides: Mapping[str, Any] | None) -> PlanningConstants:
        """Apply `Project.settings.planning_overrides` and re-validate.

        Accepts either dotted keys (`{"economics.target_cac_ratio": 4.0}`) or
        the nested shape a settings blob naturally has
        (`{"economics": {"target_cac_ratio": 4.0}}`). An override replaces the
        `value` and nothing else; `source` becomes `project_override` so the
        provenance still reads true, and `reviewed_at` stays the date somebody
        last checked the underlying figure.

        The returned object's `version` carries a digest of the overrides. This
        is not cosmetic: `calc_version` is how a `PlanCalc` row claims which
        constants produced it, and two projects running the same formula on the
        same inputs under *different* overrides would otherwise write rows that
        claim identical provenance for different arithmetic.
        """
        if not overrides:
            return self

        flat = _flatten(overrides)
        payload = self.model_dump(mode="json")
        for dotted, value in flat.items():
            self.get(dotted)  # refuses an unknown key before anything is written
            group_name, _, key = dotted.partition(".")
            if not isinstance(value, int | float) or isinstance(value, bool):
                raise ConstantsError(f"override {dotted!r} must be a number, got {value!r}")
            payload[group_name][key]["value"] = float(value)
            payload[group_name][key]["source"] = "project_override"

        digest = hashlib.sha256(
            json.dumps(flat, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        payload["version"] = f"{self.version}+ovr.{digest[:8]}"
        return PlanningConstants.model_validate(payload)


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


def load_planning_constants(path: Path | None = None) -> PlanningConstants:
    """Read and validate the constants file. Raises `ConstantsError` on anything."""
    source = path or CONSTANTS_PATH
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConstantsError(f"{source} is missing") from exc
    except yaml.YAMLError as exc:
        raise ConstantsError(f"{source} is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConstantsError(f"{source} must be a mapping, got {type(raw).__name__}")

    try:
        return PlanningConstants.model_validate(raw)
    except ValidationError as exc:
        raise ConstantsError(f"{source.name} is invalid:\n{_describe(exc)}") from exc


@lru_cache(maxsize=1)
def get_planning_constants() -> PlanningConstants:
    """The process-wide constants. Cached like `get_settings()`."""
    return load_planning_constants()
