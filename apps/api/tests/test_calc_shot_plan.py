"""`media.shot_plan_v1` — a video's length cut into clips a model can make
(PRD §9.4 video 2), and `video_duration`, the length a spec window allows.

Every clip duration must be one the model's catalogue record lists in
`supported_durations`, and nothing else: a model asked for a duration it does
not support rejects the job, or worse, bills it and returns something else.
"""

from __future__ import annotations

import itertools
import random

import pytest

from agent.calc.media import partition, shot_plan_v1, video_duration
from agent.calc.registry import FORMULAS, CalcError
from agent.media.constants import media_constants

CONSTANTS = media_constants()


def clips(duration: int, supported: list[int]) -> list[int]:
    result = shot_plan_v1(duration_s=duration, supported_durations=supported, constants=CONSTANTS)
    return [clip["duration_s"] for clip in result.result["clips"]]


def test_it_is_registered_with_the_shot_plan_evidence_kind() -> None:
    spec = FORMULAS["media.shot_plan_v1"]
    assert spec.kind == "calc_shot_plan"


def test_a_length_one_clip_can_make_is_one_clip() -> None:
    assert clips(8, [4, 6, 8]) == [8]


def test_the_fewest_clips_that_sum_exactly_to_the_length() -> None:
    assert clips(16, [4, 6, 8]) == [8, 8]
    assert clips(14, [4, 6, 8]) == [8, 6]
    assert clips(24, [4, 6, 8]) == [8, 8, 8]


def test_among_the_fewest_clips_the_most_even_split_wins() -> None:
    # 12 s in two clips: 8 + 4 or 6 + 6 — even shots, not one long and one short.
    assert clips(12, [4, 5, 6, 8]) == [6, 6]
    # 20 s in three: 8 + 8 + 4 or 8 + 6 + 6.
    assert clips(20, [4, 6, 8]) == [8, 6, 6]


def test_clips_run_back_to_back_from_zero_to_the_length() -> None:
    result = shot_plan_v1(duration_s=20, supported_durations=[4, 6, 8], constants=CONSTANTS)
    spans = [(clip["t0"], clip["t1"]) for clip in result.result["clips"]]
    assert spans == [(0, 8), (8, 14), (14, 20)]
    assert [clip["index"] for clip in result.result["clips"]] == [0, 1, 2]
    assert result.result["duration_s"] == 20


def test_a_length_no_supported_durations_sum_to_is_a_calc_error() -> None:
    with pytest.raises(CalcError, match="15 s"):
        clips(15, [4, 6, 8])
    with pytest.raises(CalcError, match="7 s"):
        clips(7, [4, 6, 8])


@pytest.mark.parametrize("supported", [[], [0, 4], [-4, 8]])
def test_a_catalogue_with_no_usable_duration_is_a_calc_error(supported: list[int]) -> None:
    with pytest.raises(CalcError):
        clips(8, supported)


def test_order_and_repeats_in_the_catalogue_do_not_change_the_plan() -> None:
    assert clips(20, [8, 4, 6, 4, 8]) == clips(20, [4, 6, 8])


def test_the_evidence_records_what_was_cut_and_by_which_catalogue() -> None:
    result = shot_plan_v1(duration_s=14, supported_durations=[8, 6, 4], constants=CONSTANTS)
    assert result.inputs == {"duration_s": 14, "supported_durations": [4, 6, 8]}
    assert result.summary == "14 s as 2 clips: 8 s + 6 s"


def _brute_force_fewest(duration: int, supported: list[int]) -> int | None:
    for count in range(1, duration // min(supported) + 1):
        for combo in itertools.combinations_with_replacement(sorted(set(supported)), count):
            if sum(combo) == duration:
                return count
    return None


def test_every_plan_uses_only_supported_durations_and_the_fewest_clips() -> None:
    rng = random.Random(20260925)
    for _ in range(400):
        supported = rng.sample(range(2, 13), rng.randint(1, 4))
        duration = rng.randint(1, 40)
        expected = _brute_force_fewest(duration, supported)
        found = partition(duration, supported)
        if expected is None:
            assert found is None, (duration, supported, found)
            continue
        assert found is not None, (duration, supported)
        assert set(found) <= set(supported), (duration, supported, found)
        assert sum(found) == duration
        assert len(found) == expected, (duration, supported, found)
        assert list(found) == sorted(found, reverse=True)


# ---------------------------------------------------------------------------
# the length a spec window allows
# ---------------------------------------------------------------------------


def test_with_a_minimum_the_shortest_length_that_can_be_cut_at_or_above_it() -> None:
    assert video_duration(min_s=10, max_s=None, supported=[4, 6, 8]) == 10
    assert video_duration(min_s=11, max_s=None, supported=[4, 6, 8]) == 12
    assert video_duration(min_s=10, max_s=30, supported=[8]) == 16


def test_with_only_a_maximum_the_longest_length_that_can_be_cut_within_it() -> None:
    assert video_duration(min_s=None, max_s=6, supported=[4, 6, 8]) == 6
    assert video_duration(min_s=None, max_s=7, supported=[4, 6, 8]) == 6


def test_a_window_no_supported_durations_can_fill_is_none() -> None:
    assert video_duration(min_s=10, max_s=11, supported=[4, 8]) is None
    assert video_duration(min_s=None, max_s=3, supported=[4, 6, 8]) is None


def test_a_window_with_neither_bound_is_refused() -> None:
    with pytest.raises(ValueError, match="neither"):
        video_duration(min_s=None, max_s=None, supported=[4, 6, 8])
