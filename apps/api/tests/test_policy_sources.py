"""`policy_sources.yaml` and its loader (PRD §9.7, law 25).

The registry is configuration a daily job acts on unattended, so the failures
worth testing are the quiet ones: a source with no attribution, a duplicate URL
that only fails at seed time, and a selector that would hash the whole document
including its per-request nonces.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from agent.policy.sources import (
    PolicySourcesError,
    get_policy_sources,
    load_policy_sources,
)

VALID = """
version: "2026.09.1"
poll_default_cron: "0 4 * * *"
sources:
  - label: "Trademarks"
    url: "https://support.google.com/adspolicy/answer/6118?hl=en"
    area: trademark
    selector: "article"
    source: unverified
    reviewed_at: 2026-09-23
"""


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "policy_sources.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


class TestLoader:
    def test_it_loads_the_shipped_registry(self) -> None:
        registry = get_policy_sources()
        assert registry.sources
        assert registry.poll_default_cron == "0 4 * * *"

    def test_a_source_without_attribution_fails_with_its_own_label(self, tmp_path: Path) -> None:
        """Law 25, and the message has to name the offender."""
        path = write(tmp_path, VALID.replace("    source: unverified\n", ""))
        with pytest.raises(PolicySourcesError) as exc:
            load_policy_sources(path)
        assert "sources.0.source" in str(exc.value)

    def test_a_source_without_a_selector_fails(self, tmp_path: Path) -> None:
        """A missing selector hashes the whole document, nonces and all."""
        path = write(tmp_path, VALID.replace('    selector: "article"\n', ""))
        with pytest.raises(PolicySourcesError) as exc:
            load_policy_sources(path)
        assert "sources.0.selector" in str(exc.value)

    def test_a_typo_is_refused_rather_than_ignored(self, tmp_path: Path) -> None:
        path = write(tmp_path, VALID.replace("selector:", "selecter:"))
        with pytest.raises(PolicySourcesError):
            load_policy_sources(path)

    def test_a_duplicate_url_fails_at_startup_naming_both_labels(self, tmp_path: Path) -> None:
        """`UNIQUE(workspace_id, url)` would otherwise fail at seed time,
        naming neither source."""
        body = (
            VALID
            + """  - label: "Trademarks again"
    url: "https://support.google.com/adspolicy/answer/6118?hl=en"
    area: trademark
    selector: "article"
    source: unverified
    reviewed_at: 2026-09-23
"""
        )
        with pytest.raises(PolicySourcesError) as exc:
            load_policy_sources(write(tmp_path, body))
        message = str(exc.value)
        assert "Trademarks" in message and "Trademarks again" in message

    def test_a_missing_file_says_so(self, tmp_path: Path) -> None:
        with pytest.raises(PolicySourcesError) as exc:
            load_policy_sources(tmp_path / "nope.yaml")
        assert "missing" in str(exc.value)


class TestShippedRegistry:
    def test_every_url_is_pinned_to_english(self) -> None:
        """An unpinned URL localises by IP, so the same page diffs against
        itself when the watcher runs from a different region. The index states
        English "is the official language used to enforce Google Ads policies"."""
        for spec in get_policy_sources().sources:
            assert "hl=en" in spec.url, spec.label

    def test_no_source_uses_the_prds_seed_selector(self) -> None:
        """§9.7 shipped `selector: "main"`. There is no <main> on these pages."""
        for spec in get_policy_sources().sources:
            assert spec.selector == "article", spec.label

    def test_disclosure_has_two_sources_and_that_is_deliberate(self) -> None:
        """Recorded deviation from §9.7's six-source list.

        No single Google page carries both the election-ad synthetic content
        rule (Google's own policy) and the EU/India/New York AI-labelling market
        list (external regulation Google surfaces). Node 3.3.4 emits rules
        scoped by market and needs both.
        """
        assert len(get_policy_sources().by_area("disclosure")) == 2

    def test_every_seeded_url_ships_unverified(self) -> None:
        """Law 25: a seed value is unverified until a human checks it."""
        registry = get_policy_sources()
        assert len(registry.unverified) == len(registry.sources)

    def test_the_integration_corpus_covers_every_seeded_source(self) -> None:
        """The drift guard.

        `tests/integration/guideline_gates.py` seeds one stored snapshot per
        watched source so 3.3.1 never opens a socket. A URL added here without
        a snapshot there would send the integration suite to the live Google
        site — which does not fail loudly, it just goes quiet for minutes.
        """
        from tests.integration.guideline_gates import POLICY_PAGES

        seeded = {spec.url for spec in get_policy_sources().sources}
        assert seeded == set(POLICY_PAGES), (
            "policy_sources.yaml and the integration corpus have drifted: "
            f"missing a snapshot for {sorted(seeded - set(POLICY_PAGES))}"
        )
