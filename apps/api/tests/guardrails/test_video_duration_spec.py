"""A video spec can carry a duration window (Stage 04 PRD §9.5, Q6 — additive).

§9.5: a surface the spec sheet lacks — "video durations" among them — is
`spec_missing`, never a guess, until a Stage 03 amendment adds the spec. An
`AssetSpec` that forbade the key could never be amended to carry one, so the
window is two optional fields. Absent, they are left out of the dump, so every
ruleset compiled before them hashes exactly as it did.
"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from agent.guardrails.compiler import ruleset_hash
from agent.schemas.guardrails import AssetSpec, AssetSpecSheet

PROVENANCE = {"source": "unverified", "reviewed_at": date(2026, 9, 22)}


def test_a_video_spec_carries_its_duration_window() -> None:
    spec = AssetSpec(ratio="16:9", min_duration_s=10, max_duration_s=60, **PROVENANCE)
    assert (spec.min_duration_s, spec.max_duration_s) == (10, 60)
    dumped = spec.model_dump(mode="json")
    assert dumped["min_duration_s"] == 10
    assert AssetSpec.model_validate(dumped) == spec


def test_either_bound_alone_is_a_window() -> None:
    assert AssetSpec(ratio="16:9", max_duration_s=6, **PROVENANCE).min_duration_s is None
    assert AssetSpec(ratio="16:9", min_duration_s=10, **PROVENANCE).max_duration_s is None


def test_a_minimum_above_the_maximum_is_refused() -> None:
    with pytest.raises(ValidationError, match="min_duration_s 30 s is above max_duration_s 15 s"):
        AssetSpec(ratio="16:9", min_duration_s=30, max_duration_s=15, **PROVENANCE)


@pytest.mark.parametrize("field", ["min_duration_s", "max_duration_s"])
def test_a_duration_is_a_positive_whole_second(field: str) -> None:
    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        AssetSpec(ratio="16:9", **{field: 0}, **PROVENANCE)


def test_a_spec_without_a_window_dumps_and_hashes_exactly_as_before() -> None:
    spec = AssetSpec(max_chars=30, min_count=3, max_count=15, **PROVENANCE)
    dumped = spec.model_dump(mode="json")
    assert "min_duration_s" not in dumped
    assert "max_duration_s" not in dumped
    # The dump the compiler hashed before the window existed, key for key.
    before = {
        "max_chars": 30,
        "min_count": 3,
        "max_count": 15,
        "ratio": None,
        "min_px": None,
        "max_bytes": None,
        "source": "unverified",
        "reviewed_at": "2026-09-22",
    }
    assert dumped == before
    sheet = AssetSpecSheet(specs={"search": {"headline": spec}})
    assert ruleset_hash({"asset_specs": sheet.model_dump(mode="json")}) == ruleset_hash(
        {"asset_specs": {"specs": {"search": {"headline": before}}}}
    )
