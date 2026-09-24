"""`select.headlines_v1` — quota-constrained, maximum-diversity, deterministic.

The model writes the pool; this picks from it (PRD §11 4.2.1, design principle
4). Every property 4.2.1's exit criteria lean on is pinned here, on the pure
function, before any node exists: quotas met, no near-duplicate pair selected,
reserves complete and ordered, and the same bytes in two processes.
"""

from __future__ import annotations

import itertools
import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from agent.creative import metrics
from agent.creative.select import (
    HEADLINES_V1,
    Candidate,
    Selection,
    SelectionError,
    headlines_v1,
)

QUOTAS = {"keyword": 3, "benefit": 3, "offer": 2, "proof": 2, "objection": 2, "cta": 2}
THRESHOLD = 0.80

#: Twenty-five headlines of an SDS-software ad group: every category over its
#: quota, one planted near-duplicate (`benefit-4` of `benefit-1`), one exact
#: duplicate after normalising (`objection-3` of `objection-0`).
POOL_TEXTS: dict[str, list[str]] = {
    "keyword": [
        "SDS Software For Your Team",
        "SDS Management Software",
        "Safety Data Sheet Software",
        "SDS Software Online",
    ],
    "benefit": [
        "Every SDS Current, Always",
        "Find Any SDS In Seconds",
        "One Library For Every Sheet",
        "Audit-Ready Chemical Records",
        "Find Any SDS In A Second",
    ],
    "offer": ["Book A Free SDS Demo", "Free Trial For Your Team", "No Setup Fees, Ever"],
    "proof": ["SDS Updates Within 24 Hours", "Updated Within 24 Hours", "Trusted By EHS Teams"],
    "objection": [
        "No IT Team Needed",
        "Set Up In One Afternoon",
        "Works With Your Files",
        "no it team needed!",
    ],
    "cta": ["Book Your Demo Today", "Book A Walkthrough Now", "Book A Demo In Minutes"],
}
assert sum(len(texts) for texts in POOL_TEXTS.values()) == 22


def _pool(texts: dict[str, list[str]] = POOL_TEXTS) -> list[Candidate]:
    return [
        Candidate(ref=f"{category}-{index}", text=text, category=category)
        for category, items in texts.items()
        for index, text in enumerate(items)
    ]


def _full_pool() -> list[Candidate]:
    extra = {
        "benefit": [*POOL_TEXTS["benefit"], "Less Paperwork Every Week"],
        "offer": [*POOL_TEXTS["offer"], "Plans For Every Team Size"],
        "cta": [*POOL_TEXTS["cta"], "Book Time With Our Team"],
    }
    pool = _pool({**POOL_TEXTS, **extra})
    assert len(pool) == 25
    return pool


def _select(pool: list[Candidate], **overrides: object) -> Selection:
    arguments: dict[str, object] = {"quotas": QUOTAS, "limit": 15, "near_duplicate": THRESHOLD}
    arguments.update(overrides)
    return headlines_v1(pool, **arguments)  # type: ignore[arg-type]


def _by_ref(pool: list[Candidate]) -> dict[str, Candidate]:
    return {candidate.ref: candidate for candidate in pool}


# ---------------------------------------------------------------------------
# the RSA gets 15 headlines that meet every quota
# ---------------------------------------------------------------------------


def test_fifteen_are_selected_and_every_quota_is_met() -> None:
    pool = _full_pool()
    chosen = _select(pool)
    categories = [_by_ref(pool)[ref].category for ref in chosen.selected]

    assert len(chosen.selected) == 15
    assert chosen.quota_report.met
    for category, required in QUOTAS.items():
        assert categories.count(category) >= required, category
    assert chosen.method == HEADLINES_V1 == "select.headlines_v1"


def test_the_quota_report_says_required_selected_and_available_per_category() -> None:
    pool = _full_pool()
    report = _select(pool).quota_report
    lines = {line.category: line for line in report.lines}

    assert [line.category for line in report.lines] == list(QUOTAS)
    assert {c: lines[c].required for c in QUOTAS} == QUOTAS
    assert lines["keyword"].available == 4
    assert sum(line.selected for line in report.lines) == report.selected == 15
    assert report.limit == 15


def test_no_two_selected_headlines_are_near_duplicates() -> None:
    pool = _full_pool()
    by_ref = _by_ref(pool)
    chosen = _select(pool)
    for left, right in itertools.combinations(chosen.selected, 2):
        assert metrics.similarity(by_ref[left].text, by_ref[right].text) < THRESHOLD, (left, right)


def test_a_planted_near_duplicate_is_never_selected_beside_its_twin() -> None:
    pool = _full_pool()
    near = metrics.similarity("Find Any SDS In Seconds", "Find Any SDS In A Second")
    assert near >= THRESHOLD, "the plant must actually be a near-duplicate"

    chosen = _select(pool)
    twins = {"benefit-1", "benefit-4"}
    assert len(twins & set(chosen.selected)) <= 1
    excluded = {item.ref: item for item in chosen.near_duplicates}
    loser = (twins - set(chosen.selected)).pop()
    assert excluded[loser].of in twins and excluded[loser].similarity >= THRESHOLD


def test_an_exact_duplicate_after_normalising_is_never_selected_twice() -> None:
    chosen = _select(_full_pool())
    assert not {"objection-0", "objection-3"} <= set(chosen.selected)


def test_a_near_duplicate_threshold_of_one_still_excludes_exact_duplicates() -> None:
    chosen = _select(_full_pool(), near_duplicate=1.0)
    assert not {"objection-0", "objection-3"} <= set(chosen.selected)


# ---------------------------------------------------------------------------
# quotas are constraints, diversity is the objective
# ---------------------------------------------------------------------------


def test_a_scarce_category_is_not_starved_by_a_diverse_first_pick() -> None:
    """Picking `benefit-0` first would exclude `cta-0`, one of only two CTAs.

    `benefit-0` is the most distinctive candidate in the pool (it shares little
    with anything but its CTA near-twin), so greedy diversity alone takes it
    first and the CTA quota becomes unreachable. The feasibility guard defers a
    pick that would leave a category short while a pick exists that does not.
    """
    texts = {
        "keyword": ["alpha keyword one", "bravo keyword two", "charlie keyword three"],
        "benefit": [
            "zulu yankee xray whiskey quebec",
            "delta benefit one",
            "echo benefit two",
            "mike benefit three",
        ],
        "offer": ["foxtrot offer one", "golf offer two"],
        "proof": ["hotel proof one", "india proof two"],
        "objection": ["juliet objection one", "kilo objection two"],
        "cta": ["zulu yankee xray whiskey", "lima call now"],
    }
    pool = _pool(texts)
    twin, cta = texts["benefit"][0], texts["cta"][0]
    assert metrics.similarity(twin, cta) >= THRESHOLD
    others = [c.text for c in pool if c.text not in (twin, cta)]
    assert sum(1 - metrics.similarity(twin, o) for o in others) > sum(
        1 - metrics.similarity(cta, o) for o in others
    ), "the plant must be the pick greedy diversity would make first"

    chosen = _select(pool, limit=15)
    lines = {line.category: line for line in chosen.quota_report.lines}
    assert lines["cta"].selected == 2
    assert chosen.quota_report.met
    assert "benefit-0" not in chosen.selected


def test_a_short_category_is_reported_and_the_rest_is_still_filled() -> None:
    texts = {**POOL_TEXTS, "cta": ["Book Your Demo Today"]}
    chosen = _select(_pool(texts))
    lines = {line.category: line for line in chosen.quota_report.lines}

    assert lines["cta"].required == 2 and lines["cta"].selected == 1
    assert lines["cta"].available == 1
    assert not chosen.quota_report.met
    assert len(chosen.selected) == 15


def test_the_limit_bounds_the_selection_and_a_small_pool_is_taken_whole() -> None:
    assert len(_select(_full_pool(), limit=14).selected) == 14
    small = _pool({"keyword": ["alpha one", "bravo two"], "cta": ["charlie three"]})
    chosen = _select(small, quotas={"keyword": 1, "cta": 1})
    assert sorted(chosen.selected) == ["cta-0", "keyword-0", "keyword-1"]
    assert chosen.reserve == ()


def test_the_most_distinctive_candidate_is_picked_before_its_neighbours() -> None:
    texts = {"benefit": ["sds sheets sds", "sds sheets sdss", "quantum hydrogen ledger"]}
    chosen = _select(_pool(texts), quotas={"benefit": 0}, limit=2)
    assert chosen.selected[0] == "benefit-2"


def test_quotas_that_exceed_the_limit_are_refused() -> None:
    with pytest.raises(SelectionError, match="quotas sum to 14"):
        _select(_full_pool(), limit=13)


def test_a_pool_with_a_repeated_ref_is_refused() -> None:
    pool = _full_pool()
    with pytest.raises(SelectionError, match="keyword-0"):
        _select([*pool, pool[0]])


def test_a_negative_quota_or_a_bad_threshold_is_refused() -> None:
    with pytest.raises(SelectionError):
        _select(_full_pool(), quotas={**QUOTAS, "cta": -1})
    with pytest.raises(SelectionError):
        _select(_full_pool(), near_duplicate=0.0)
    with pytest.raises(SelectionError):
        _select(_full_pool(), limit=0)


# ---------------------------------------------------------------------------
# reserves
# ---------------------------------------------------------------------------


def test_every_unselected_candidate_is_a_reserve_exactly_once() -> None:
    pool = _full_pool()
    chosen = _select(pool)
    assert sorted([*chosen.selected, *chosen.reserve]) == sorted(c.ref for c in pool)
    assert len(set(chosen.reserve)) == len(chosen.reserve) == 10


def test_reserves_that_duplicate_a_selected_headline_come_last() -> None:
    chosen = _select(_full_pool())
    blocked = {item.ref for item in chosen.near_duplicates}
    assert blocked
    tail = chosen.reserve[-len(blocked) :]
    assert set(tail) == blocked


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------


def test_the_input_order_does_not_change_the_selection() -> None:
    pool = _full_pool()
    forward = _select(pool)
    for seed_order in (
        list(reversed(pool)),
        pool[7:] + pool[:7],
        sorted(pool, key=lambda c: c.text),
    ):
        assert _select(seed_order) == forward


_SUBPROCESS = """
import json, sys
from dataclasses import asdict
sys.path[:0] = {paths!r}
from tests.creative.test_select import _full_pool, _select
print(json.dumps(asdict(_select(_full_pool())), sort_keys=True))
"""


def _selection_bytes(hash_seed: str) -> bytes:
    api_root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONHASHSEED": hash_seed}
    completed = subprocess.run(  # noqa: S603 - the interpreter running this suite
        [sys.executable, "-c", _SUBPROCESS.format(paths=[str(api_root), str(api_root / "src")])],
        capture_output=True,
        check=True,
        env=env,
        cwd=api_root,
    )
    return completed.stdout


def test_the_selection_is_byte_identical_across_two_processes() -> None:
    """Different hash seeds give sets a different iteration order per process.

    Anything that let a set's order leak into the result — a tie broken by
    iteration, a category walked from a set — shows up here as a byte diff.
    """
    first, second = _selection_bytes("1"), _selection_bytes("4242")
    assert first == second
    decoded = json.loads(first)
    assert decoded == json.loads(json.dumps(asdict(_select(_full_pool())), sort_keys=True))
    assert len(decoded["selected"]) == 15
