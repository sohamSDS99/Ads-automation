"""The watcher's hashing, and the false positive it exists to prevent.

The fixture is a real Google policy page — `fixtures/google_policy_trademark.html`
is the actual `<article>` served by `support.google.com/adspolicy/answer/6118`,
wrapped in the nav, survey widget and nonce-carrying script tag the live page
ships. `__NONCE__` is substituted per "request" so the tests can reproduce, on
demand, the exact churn measured against the live site on 2026-09-23.

`test_body_hash_would_change_on_every_fetch` is the important one. It asserts
the *wrong* implementation fails, which is the only way to keep a correct one
from being quietly simplified back into the bug: whole-body hashing would open
an amendment on every source every day, and §8.6 routes an unclassified
amendment to a human — so the inbox fills with noise until the watcher gets
turned off.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from agent.policy.watcher import (
    MAX_DIFF_LINES,
    SelectorStale,
    diff_of,
    normalise,
    snapshot,
)

FIXTURE = Path(__file__).parent / "fixtures" / "google_policy_trademark.html"


def page(nonce: str = "ryGVvoGzTunVq7Ych3LX") -> str:
    """The fixture as one 'request' would serve it."""
    return FIXTURE.read_text(encoding="utf-8").replace("__NONCE__", nonce)


class TestSnapshotStability:
    def test_the_same_page_hashes_the_same_twice(self) -> None:
        first = snapshot(page(), "article")
        second = snapshot(page(), "article")
        assert first.hash == second.hash
        assert first.text == second.text

    def test_a_changed_nonce_does_not_change_the_hash(self) -> None:
        """The measurement this module is built around.

        Two live fetches three seconds apart differed only in CSP nonces and
        per-request stat tokens. Scoped to `article`, they hashed identically.
        """
        assert (
            snapshot(page("AAAAAAAAAAAAAAAAAAAA"), "article").hash
            == snapshot(page("BBBBBBBBBBBBBBBBBBBB"), "article").hash
        )

    def test_body_hash_would_change_on_every_fetch(self) -> None:
        """Proof that the naive implementation is wrong, kept as a test.

        If this ever starts passing, the fixture stopped reproducing the real
        page and the stability tests above have become vacuous.
        """
        first = hashlib.sha256(page("AAAAAAAAAAAAAAAAAAAA").encode()).hexdigest()
        second = hashlib.sha256(page("BBBBBBBBBBBBBBBBBBBB").encode()).hexdigest()
        assert first != second

    def test_nav_and_footer_are_outside_the_hashed_region(self) -> None:
        text = snapshot(page(), "article").text
        assert "Help Center Navigation" not in text
        assert "Google Privacy Terms" not in text

    def test_whitespace_reflow_is_not_a_policy_change(self) -> None:
        original = page()
        reflowed = original.replace("<p>", "<p>\n    ")
        assert snapshot(original, "article").hash == snapshot(reflowed, "article").hash

    def test_a_real_wording_change_does_change_the_hash(self) -> None:
        """The other half. A watcher that never fires is as useless as one that
        always does."""
        before = snapshot(page(), "article")
        after = snapshot(page().replace("Trademarks", "Trademarks and brand names"), "article")
        assert before.hash != after.hash


class TestSelectorStale:
    def test_an_unmatched_selector_raises_rather_than_reporting_no_change(self) -> None:
        with pytest.raises(SelectorStale) as exc:
            snapshot(page(), "main")
        assert "matched no element" in str(exc.value)

    def test_the_prds_seed_selector_is_the_one_that_does_not_match(self) -> None:
        """§9.7 shipped `selector: "main"`. There is no <main> on these pages.

        Pinned as a test because the failure is silent: an unmatched selector
        hashes the empty string, which is perfectly stable forever.
        """
        assert "<main" not in page()
        snapshot(page(), "article")  # the corrected selector still works

    def test_a_matching_but_empty_selector_also_raises(self) -> None:
        with pytest.raises(SelectorStale):
            snapshot("<html><article>   </article></html>", "article")

    def test_scripts_inside_the_region_are_stripped(self) -> None:
        noisy = '<article><script>var t="__NONCE__";</script><p>Real policy.</p></article>'
        first = snapshot(noisy.replace("__NONCE__", "one"), "article")
        second = snapshot(noisy.replace("__NONCE__", "two"), "article")
        assert first.hash == second.hash
        assert "var t" not in first.text


class TestNormalise:
    def test_a_non_breaking_space_is_the_same_policy(self) -> None:
        assert normalise("from £10") == normalise("from £10")

    def test_runs_of_whitespace_collapse(self) -> None:
        assert normalise("a   b\n\n c") == "a b c"


class TestDiff:
    def test_it_counts_what_moved(self) -> None:
        result = diff_of(
            "Ads must not mislead. Limit is 30 characters.",
            "Ads must not mislead. Limit is 40 characters.",
        )
        assert result["added"] == 1
        assert result["removed"] == 1
        assert result["truncated"] is False

    def test_an_unchanged_page_produces_an_empty_diff(self) -> None:
        text = "Ads must not mislead."
        assert diff_of(text, text)["unified"] == []

    def test_a_huge_diff_is_truncated_and_says_so(self) -> None:
        before = ". ".join(f"Rule {index} applies" for index in range(600))
        after = ". ".join(f"Policy {index} applies" for index in range(600))
        result = diff_of(before, after)
        assert result["truncated"] is True
        assert len(result["unified"]) == MAX_DIFF_LINES
        assert result["total_lines"] > MAX_DIFF_LINES
