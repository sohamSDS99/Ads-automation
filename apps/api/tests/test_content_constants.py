"""`content_constants.yaml` and its validation (Stage 03 PRD §9.6, law 25).

The one that matters is `test_a_constant_without_a_source_fails_and_names_it`:
law 25's whole claim is that an unattributable threshold cannot reach a
rulebook, and that claim is only true if startup actually refuses and says
which key.

These run against the **shipped** file rather than a copy of it pasted into the
test. A literal would drift from the real file the first time somebody edited
one and not the other, and the drift would look like a passing suite.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from agent.guidelines.constants import (
    CONSTANTS_PATH,
    ContentConstantsError,
    get_content_constants,
    load_content_constants,
)


def raw() -> dict[str, Any]:
    return yaml.safe_load(CONSTANTS_PATH.read_text(encoding="utf-8"))


def write(tmp_path: Path, payload: dict[str, Any]) -> Path:
    path = tmp_path / "content_constants.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


def test_the_shipped_file_loads() -> None:
    constants = load_content_constants()
    assert constants.version
    assert constants.claims.match_threshold.value == pytest.approx(0.88)
    assert constants.asset_specs["search"]["headline"].max_chars == 30


def test_get_and_value_resolve_dotted_keys() -> None:
    constants = load_content_constants()
    assert constants.value("image_policy.logo_match_score_min") == pytest.approx(0.62)
    assert constants.get("claims.high_risk_types").value[0] == "superlative"
    with pytest.raises(ContentConstantsError, match="unknown constant"):
        constants.get("claims.nonexistent")
    with pytest.raises(ContentConstantsError, match="unknown constants group"):
        constants.get("nonexistent.key")
    with pytest.raises(ContentConstantsError, match="not a numeric constant"):
        constants.value("claims.high_risk_types")


# --- law 25 -----------------------------------------------------------------


def test_a_constant_without_a_source_fails_and_names_it(tmp_path: Path) -> None:
    """The load-bearing one. An unattributable threshold must not start up."""
    payload = raw()
    del payload["image_policy"]["logo_match_score_min"]["source"]

    with pytest.raises(ContentConstantsError) as caught:
        load_content_constants(write(tmp_path, payload))

    message = str(caught.value)
    assert "image_policy.logo_match_score_min.source" in message, message
    assert "Field required" in message


def test_an_asset_spec_without_a_source_fails_and_names_it(tmp_path: Path) -> None:
    """Asset specs are the ones most likely to be edited, and least verified."""
    payload = raw()
    del payload["asset_specs"]["search"]["headline"]["source"]

    with pytest.raises(ContentConstantsError) as caught:
        load_content_constants(write(tmp_path, payload))

    assert "asset_specs.search.headline.source" in str(caught.value)


def test_an_empty_source_fails_and_names_it(tmp_path: Path) -> None:
    """`source: ""` satisfies "the key is present" and attributes nothing."""
    payload = raw()
    payload["review"]["guideline_review_days"]["source"] = ""

    with pytest.raises(ContentConstantsError) as caught:
        load_content_constants(write(tmp_path, payload))

    assert "review.guideline_review_days.source" in str(caught.value)


def test_a_typo_is_an_error_not_a_shrug(tmp_path: Path) -> None:
    """`sources:` would leave `source` missing while looking deliberate."""
    payload = raw()
    spec = payload["review"]["guideline_review_days"]
    spec["sources"] = spec.pop("source")

    with pytest.raises(ContentConstantsError) as caught:
        load_content_constants(write(tmp_path, payload))

    assert "review.guideline_review_days" in str(caught.value)


def test_every_shipped_value_carries_a_source_and_a_review_date() -> None:
    constants = load_content_constants()
    for group_name in ("image_policy", "claims", "signature", "amendment", "review", "offers"):
        group = getattr(constants, group_name)
        for key, constant in group:
            assert constant.source, f"{group_name}.{key} has no source"
            assert constant.reviewed_at, f"{group_name}.{key} has no reviewed_at"
    for campaign_type, assets in constants.asset_specs.items():
        for asset_type, spec in assets.items():
            assert spec.source, f"{campaign_type}.{asset_type} has no source"


def test_every_asset_spec_is_still_flagged_unverified() -> None:
    """Q7, on purpose. When somebody verifies these against Google's docs they
    will change `source` and this test with it — which is the point: the change
    should be deliberate and visible in a diff, not a number quietly edited."""
    constants = load_content_constants()
    unverified = {
        f"{campaign}.{asset}"
        for campaign, assets in constants.asset_specs.items()
        for asset, spec in assets.items()
        if spec.source == "unverified"
    }
    assert unverified == {
        "search.headline",
        "search.description",
        "search.path",
        "performance_max.headline",
        "performance_max.long_headline",
        "performance_max.description",
        "performance_max.image_landscape",
    }


# --- detectors (PRD §9.3) ---------------------------------------------------


def test_every_detector_compiles_and_is_uniquely_identified() -> None:
    detectors = load_content_constants().detectors()
    assert detectors
    ids = [detector.detector_id for detector in detectors]
    assert len(ids) == len(set(ids)), "duplicate detector_id"
    for detector in detectors:
        re.compile(detector.pattern)


def test_detectors_are_written_lower_case_for_normalized_text() -> None:
    """Patterns run against case-folded text, so an upper-case literal in one
    would silently never match. Character classes and escapes are exempt."""
    for detector in load_content_constants().detectors():
        literal = re.sub(r"\\[a-zA-Z]", "", detector.pattern)
        assert literal == literal.lower(), detector.detector_id


# --- overrides --------------------------------------------------------------


def test_an_override_changes_the_value_the_source_and_the_version() -> None:
    constants = load_content_constants()
    merged = constants.merged({"claims.match_threshold": 0.95})

    assert merged.value("claims.match_threshold") == pytest.approx(0.95)
    assert merged.get("claims.match_threshold").source == "project_override"
    assert merged.version != constants.version
    assert merged.version.startswith(f"{constants.version}+ovr.")
    # `reviewed_at` is when somebody last checked the underlying figure, and an
    # override does not check anything.
    assert merged.get("claims.match_threshold").reviewed_at == constants.get(
        "claims.match_threshold"
    ).reviewed_at
    # The original is untouched — `merged` returns a new object.
    assert constants.value("claims.match_threshold") == pytest.approx(0.88)


def test_the_same_override_always_produces_the_same_version() -> None:
    """`constants_version` is a provenance claim; it has to be a function of
    the overrides and nothing else."""
    constants = load_content_constants()
    nested = constants.merged({"claims": {"match_threshold": 0.95}})
    dotted = constants.merged({"claims.match_threshold": 0.95})
    assert nested.version == dotted.version


def test_no_overrides_returns_the_same_object() -> None:
    constants = load_content_constants()
    assert constants.merged(None) is constants
    assert constants.merged({}) is constants


def test_an_override_of_an_unknown_key_raises() -> None:
    with pytest.raises(ContentConstantsError, match="unknown constant"):
        load_content_constants().merged({"claims.made_up": 1})


def test_an_override_must_have_the_right_shape() -> None:
    constants = load_content_constants()
    with pytest.raises(ContentConstantsError, match="must be a number"):
        constants.merged({"claims.match_threshold": "high"})
    with pytest.raises(ContentConstantsError, match="must be a number"):
        constants.merged({"claims.match_threshold": True})
    with pytest.raises(ContentConstantsError, match="must be a list of strings"):
        constants.merged({"claims.high_risk_types": 3})


def test_a_list_constant_can_be_overridden() -> None:
    merged = load_content_constants().merged({"claims.high_risk_types": ["guarantee"]})
    assert merged.get("claims.high_risk_types").value == ("guarantee",)


# --- loading ----------------------------------------------------------------


def test_a_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ContentConstantsError, match="is missing"):
        load_content_constants(tmp_path / "nope.yaml")


def test_a_file_that_is_not_a_mapping_raises(tmp_path: Path) -> None:
    path = tmp_path / "content_constants.yaml"
    path.write_text("- one\n- two\n", encoding="utf-8")
    with pytest.raises(ContentConstantsError, match="must be a mapping"):
        load_content_constants(path)


def test_the_process_wide_constants_are_cached() -> None:
    assert get_content_constants() is get_content_constants()
