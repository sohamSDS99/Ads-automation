"""Dedupe keys and the text that gets indexed."""

from __future__ import annotations

from agent.evidence.normalize import (
    MAX_CONTENT_TEXT,
    EvidenceDraft,
    content_hash,
    render_content_text,
)


def draft(**kwargs: object) -> EvidenceDraft:
    payload = {"source": "google_ads", "kind": "campaign_perf", "payload": {"a": 1}}
    return EvidenceDraft(**{**payload, **kwargs})  # type: ignore[arg-type]


def test_key_order_does_not_change_the_hash() -> None:
    """JSON round-trips reorder keys; a reorder is not new data."""
    assert content_hash("web", "page", {"a": 1, "b": 2}) == content_hash(
        "web", "page", {"b": 2, "a": 1}
    )


def test_an_int_and_its_float_hash_the_same() -> None:
    """`1` becoming `1.0` across a JSON hop must not resurrect a row."""
    assert content_hash("web", "page", {"cost": 1}) == content_hash("web", "page", {"cost": 1.0})


def test_a_changed_value_changes_the_hash() -> None:
    assert content_hash("web", "page", {"cost": 1}) != content_hash("web", "page", {"cost": 2})


def test_fetch_timestamps_are_excluded() -> None:
    """Including them would mean every refetch wrote a duplicate row."""
    assert content_hash("web", "page", {"a": 1, "fetched_at": "t1"}) == content_hash(
        "web", "page", {"a": 1, "fetched_at": "t2"}
    )


def test_volatile_keys_are_stripped_at_depth() -> None:
    assert content_hash("web", "p", {"outer": {"a": 1, "request_id": "x"}}) == content_hash(
        "web", "p", {"outer": {"a": 1, "request_id": "y"}}
    )


def test_source_and_kind_are_part_of_the_key() -> None:
    """The same numbers from two connectors are two facts, not one."""
    assert content_hash("web", "page", {"a": 1}) != content_hash("csv", "page", {"a": 1})
    assert content_hash("web", "page", {"a": 1}) != content_hash("web", "other", {"a": 1})


def test_source_url_is_part_of_the_key() -> None:
    """Two landing pages with identical copy are still two pages."""
    assert content_hash("web", "page", {"a": 1}, "https://x.test") != content_hash(
        "web", "page", {"a": 1}, "https://y.test"
    )


def test_nested_list_order_is_significant() -> None:
    """Headline order is a real difference in an RSA; sorting would hide it."""
    assert content_hash("web", "p", {"h": ["a", "b"]}) != content_hash(
        "web", "p", {"h": ["b", "a"]}
    )


def test_content_text_names_the_kind_and_every_leaf() -> None:
    text = render_content_text("search_term_pnl", {"search_term": "sds software", "cost": 12.5})
    assert "search term pnl" in text
    assert "sds software" in text
    assert "12.5" in text


def test_scalar_lists_render_inline() -> None:
    text = render_content_text("creative_history", {"headlines": ["Buy now", "Book a demo"]})
    assert "headlines: Buy now, Book a demo" in text


def test_nested_dicts_are_walked() -> None:
    text = render_content_text("serp_snapshot", {"results": [{"domain": "chemwatch.net"}]})
    assert "chemwatch.net" in text


def test_none_and_empty_values_are_dropped() -> None:
    text = render_content_text("page", {"title": "Hello", "meta": None, "h1": ""})
    assert "Hello" in text
    assert "meta" not in text
    assert "h1" not in text


def test_content_text_is_capped() -> None:
    """The index is a retrieval surface, not an archive."""
    text = render_content_text("page", {"body": "x " * 20_000})
    assert len(text) <= MAX_CONTENT_TEXT


def test_a_connector_override_wins_over_the_default_rendering() -> None:
    assert draft(content_text="hand written").text() == "hand written"


def test_a_blank_kind_is_refused() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        draft(kind="   ")


def test_draft_hash_agrees_with_the_free_function() -> None:
    one = draft()
    assert one.hash() == content_hash(one.source, one.kind, one.payload, one.source_url)
