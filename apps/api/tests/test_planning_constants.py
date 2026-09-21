"""`planning_constants.yaml` and its validation (PRD §9.3, global law 15).

The one that matters is `test_a_constant_without_a_source_fails_and_names_it`:
law 15's whole claim is that an unattributable threshold cannot reach a plan,
and that claim is only true if startup actually refuses and says which key.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.planning.constants import (
    CONSTANTS_PATH,
    Constant,
    ConstantsError,
    PlanningConstants,
    get_planning_constants,
    load_planning_constants,
)

GOOD = """
version: "2026.09.1"
learning:
  tcpa_min_conv_30d:     {value: 30, source: "url", reviewed_at: 2026-09-21}
  troas_min_conv_30d:    {value: 50, source: "url", reviewed_at: 2026-09-21}
  absolute_min_conv_30d: {value: 15, source: "internal", reviewed_at: 2026-09-21}
  learning_period_days:  {value: 7, source: "url", reviewed_at: 2026-09-21}
structure:
  min_keywords_per_ad_group:  {value: 5, source: "internal", reviewed_at: 2026-09-21}
  max_keywords_per_ad_group:  {value: 20, source: "internal", reviewed_at: 2026-09-21}
  min_ad_groups_per_campaign: {value: 3, source: "internal", reviewed_at: 2026-09-21}
economics:
  target_cac_ratio:  {value: 3.0, source: "internal", reviewed_at: 2026-09-21}
  safety_margin_pct: {value: 15, source: "internal", reviewed_at: 2026-09-21}
budget:
  min_monthly_per_campaign_usd: {value: 1000, source: "internal", reviewed_at: 2026-09-21}
  experiment_reserve_pct:       {value: 10, source: "internal", reviewed_at: 2026-09-21}
  cautious_step_pct:            {value: 25, source: "internal", reviewed_at: 2026-09-21}
  aggressive_step_pct:          {value: 40, source: "internal", reviewed_at: 2026-09-21}
forecast:
  impression_share_target_pct: {value: 45, source: "internal", reviewed_at: 2026-09-21}
  default_ctr_pct:             {value: 3.2, source: "internal", reviewed_at: 2026-09-21}
  default_cvr_pct:             {value: 2.5, source: "internal", reviewed_at: 2026-09-21}
reallocation:
  max_shift_pct: {value: 20, source: "internal", reviewed_at: 2026-09-21}
  lookback_days: {value: 14, source: "internal", reviewed_at: 2026-09-21}
  cooldown_days: {value: 14, source: "internal", reviewed_at: 2026-09-21}
measurement:
  tolerance_floor_pct:      {value: 5, source: "internal", reviewed_at: 2026-09-21}
  tolerance_cap_pct:        {value: 40, source: "internal", reviewed_at: 2026-09-21}
  modelled_conversion_pct:  {value: 20, source: "internal", reviewed_at: 2026-09-21}
  action_stale_days:        {value: 30, source: "internal", reviewed_at: 2026-09-21}
  click_upload_window_days: {value: 90, source: "internal", reviewed_at: 2026-09-21}
  manual_preparation_days:  {value: 2, source: "internal", reviewed_at: 2026-09-21}
test:
  alpha:       {value: 0.05, source: "internal", reviewed_at: 2026-09-21}
  power:       {value: 0.80, source: "internal", reviewed_at: 2026-09-21}
  min_mde_pct: {value: 20, source: "internal", reviewed_at: 2026-09-21}
"""


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "constants.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_the_fixture_is_a_valid_file(tmp_path: Path) -> None:
    """Every failure test below breaks one thing in `GOOD` and reads the message.

    If `GOOD` is itself invalid — a group added to the model and not to the
    fixture — those tests keep passing while raising for a second, unrelated
    reason, and `test_a_missing_group_fails` stops proving anything at all.
    So the fixture's own validity is asserted first.
    """
    assert load_planning_constants(write(tmp_path, GOOD)).version == "2026.09.1"


def test_the_shipped_file_is_valid() -> None:
    constants = load_planning_constants()
    assert constants.version
    assert constants.value("learning.tcpa_min_conv_30d") == 30
    assert constants.value("test.alpha") == pytest.approx(0.05)


def test_every_constant_in_the_shipped_file_carries_a_source_and_a_date() -> None:
    constants = load_planning_constants()
    groups = set(PlanningConstants.model_fields).difference({"version"})
    assert groups, "PlanningConstants declares no groups"
    for group_name in sorted(groups):
        group = getattr(constants, group_name)
        for key in type(group).model_fields:
            constant = getattr(group, key)
            assert constant.source, f"{group_name}.{key} has an empty source"
            assert constant.reviewed_at, f"{group_name}.{key} has no reviewed_at"


def test_get_planning_constants_is_cached() -> None:
    get_planning_constants.cache_clear()
    first = get_planning_constants()
    assert get_planning_constants() is first


def test_a_constant_without_a_source_fails_and_names_it(tmp_path: Path) -> None:
    body = GOOD.replace(
        '  tcpa_min_conv_30d:     {value: 30, source: "url", reviewed_at: 2026-09-21}',
        "  tcpa_min_conv_30d:     {value: 30, reviewed_at: 2026-09-21}",
    )
    with pytest.raises(ConstantsError) as raised:
        load_planning_constants(write(tmp_path, body))
    assert "learning.tcpa_min_conv_30d.source" in str(raised.value)


def test_a_blank_source_is_not_a_source(tmp_path: Path) -> None:
    body = GOOD.replace(
        'source: "url", reviewed_at: 2026-09-21}\n  troas',
        'source: "", reviewed_at: 2026-09-21}\n  troas',
    )
    with pytest.raises(ConstantsError) as raised:
        load_planning_constants(write(tmp_path, body))
    assert "learning.tcpa_min_conv_30d.source" in str(raised.value)


def test_a_constant_without_a_reviewed_at_fails_and_names_it(tmp_path: Path) -> None:
    body = GOOD.replace(
        '  alpha:       {value: 0.05, source: "internal", reviewed_at: 2026-09-21}',
        '  alpha:       {value: 0.05, source: "internal"}',
    )
    with pytest.raises(ConstantsError) as raised:
        load_planning_constants(write(tmp_path, body))
    assert "test.alpha.reviewed_at" in str(raised.value)


def test_a_typo_in_a_field_name_is_caught_rather_than_ignored(tmp_path: Path) -> None:
    """`sources:` would leave `source` missing while passing as an extra key."""
    body = GOOD.replace(
        '  alpha:       {value: 0.05, source: "internal", reviewed_at: 2026-09-21}',
        '  alpha:       {value: 0.05, sources: "internal", reviewed_at: 2026-09-21}',
    )
    with pytest.raises(ConstantsError) as raised:
        load_planning_constants(write(tmp_path, body))
    message = str(raised.value)
    assert "test.alpha.source" in message
    assert "test.alpha.sources" in message


def test_a_missing_group_fails(tmp_path: Path) -> None:
    """Drop the whole `forecast:` block, by indentation rather than by name.

    Structurally, because a group gains keys: a filter that listed them would
    leave the ones it had not heard of orphaned under the previous group, and
    the load would then fail for a parsing reason rather than the missing-group
    reason this test is about.
    """
    lines = GOOD.splitlines()
    start = lines.index("forecast:")
    end = next(
        index
        for index in range(start + 1, len(lines))
        if lines[index] and not lines[index].startswith(" ")
    )
    body = "\n".join(lines[:start] + lines[end:])
    with pytest.raises(ConstantsError) as raised:
        load_planning_constants(write(tmp_path, body))
    assert "forecast" in str(raised.value)


def test_a_missing_file_fails_with_its_path(tmp_path: Path) -> None:
    with pytest.raises(ConstantsError, match="is missing"):
        load_planning_constants(tmp_path / "nope.yaml")


def test_broken_yaml_fails(tmp_path: Path) -> None:
    with pytest.raises(ConstantsError, match="not valid YAML"):
        load_planning_constants(write(tmp_path, "version: [unclosed\n"))


def test_a_non_mapping_file_fails(tmp_path: Path) -> None:
    with pytest.raises(ConstantsError, match="must be a mapping"):
        load_planning_constants(write(tmp_path, "- one\n- two\n"))


def test_get_refuses_an_unknown_key() -> None:
    constants = load_planning_constants()
    with pytest.raises(ConstantsError, match="unknown constant"):
        constants.get("budget.no_such_constant")
    with pytest.raises(ConstantsError, match="unknown constants group"):
        constants.get("nonsense.thing")
    with pytest.raises(ConstantsError, match="unknown constants group"):
        constants.get("budget")


def test_as_int_refuses_a_fraction() -> None:
    assert Constant(value=5.0, source="internal", reviewed_at="2026-09-21").as_int() == 5
    with pytest.raises(ConstantsError, match="not a whole number"):
        Constant(value=5.5, source="internal", reviewed_at="2026-09-21").as_int()


def test_an_override_changes_the_value_the_source_and_the_version() -> None:
    base = load_planning_constants()
    merged = base.merged({"economics.target_cac_ratio": 4.0})

    assert merged.value("economics.target_cac_ratio") == 4.0
    assert merged.get("economics.target_cac_ratio").source == "project_override"
    # The reviewed_at date of the underlying figure is untouched: somebody still
    # checked it on that day, and the override does not re-verify anything.
    assert (
        merged.get("economics.target_cac_ratio").reviewed_at
        == base.get("economics.target_cac_ratio").reviewed_at
    )
    assert merged.version != base.version
    assert merged.version.startswith(f"{base.version}+ovr.")
    # Untouched constants are untouched.
    assert merged.value("test.alpha") == base.value("test.alpha")
    assert merged.get("test.alpha").source == base.get("test.alpha").source


def test_the_nested_override_shape_works_too() -> None:
    base = load_planning_constants()
    dotted = base.merged({"budget.experiment_reserve_pct": 5})
    nested = base.merged({"budget": {"experiment_reserve_pct": 5}})
    assert dotted.version == nested.version
    assert nested.value("budget.experiment_reserve_pct") == 5


def test_different_overrides_produce_different_versions() -> None:
    """Two projects must not write PlanCalc rows claiming the same provenance."""
    base = load_planning_constants()
    one = base.merged({"economics.target_cac_ratio": 4.0})
    two = base.merged({"economics.target_cac_ratio": 5.0})
    assert one.version != two.version


def test_the_same_override_produces_the_same_version() -> None:
    base = load_planning_constants()
    keys = {"economics.target_cac_ratio": 4.0, "test.alpha": 0.1}
    assert base.merged(keys).version == base.merged(dict(reversed(list(keys.items())))).version


def test_no_overrides_returns_the_same_object() -> None:
    base = load_planning_constants()
    assert base.merged(None) is base
    assert base.merged({}) is base


def test_an_override_of_an_unknown_key_is_refused() -> None:
    base = load_planning_constants()
    with pytest.raises(ConstantsError, match="unknown constant"):
        base.merged({"budget.made_up": 1})


def test_a_non_numeric_override_is_refused() -> None:
    base = load_planning_constants()
    with pytest.raises(ConstantsError, match="must be a number"):
        base.merged({"budget.experiment_reserve_pct": "lots"})
    with pytest.raises(ConstantsError, match="must be a number"):
        base.merged({"budget.experiment_reserve_pct": True})


def test_the_constants_file_ships_inside_the_package() -> None:
    """`Dockerfile` copies `src/`, so the file has to live there and nowhere else."""
    assert CONSTANTS_PATH.is_file()
    assert CONSTANTS_PATH.parent.name == "planning"
    assert PlanningConstants.model_validate is not None
